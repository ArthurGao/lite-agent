"""`PgSessionStore`: the session store in PostgreSQL (design §15 S4). It keeps the contract of
`SessionStore` exactly as `InMemorySessionStore` does: whole batches at the expected tail, forks
that read their parent's prefix, nothing ever rewritten (the database refuses it)."""

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any

from psycopg import AsyncCursor, sql
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter

from lite_agent.config import OwnSchema
from lite_agent.errors import EventRejected, SeqConflict, StoreError, UnknownSession
from lite_agent.events import Event, dump, load
from lite_agent.pg.db import connection_error
from lite_agent.store import check_batch, new_id

_SCHEMA: TypeAdapter[str] = TypeAdapter(OwnSchema)
MIGRATE = "the schema has no lite-agent tables: apply the migrations first (lite_agent.pg.migrate)"


class PgSessionStore:
    """Opens its connection pool on first use; `close()` when done. `schema` must be migrated
    first (`lite_agent.pg.migrate`)."""

    def __init__(
        self,
        database_url: str,
        *,
        schema: str = "lite_agent",
        ids: Callable[[], str] = new_id,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        self.schema = _SCHEMA.validate_python(schema)  # ValueError for a schema not our own
        self._url = database_url
        self._ids = ids
        self._pool = AsyncConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"autocommit": True},
        )
        self._opened = False
        self._closed = False  # close() is final: a closed store never reopens
        self._opening = asyncio.Lock()  # concurrent first calls open the pool once
        sessions, events = sql.Identifier(schema, "sessions"), sql.Identifier(schema, "events")
        self._events_table = events
        # Composed once; every query names its schema, so no search_path is involved.
        self._create = sql.SQL(
            "INSERT INTO {} (id) VALUES (%s) ON CONFLICT (id) DO NOTHING"
        ).format(sessions)
        self._head = sql.SQL(
            "SELECT s.parent_id, s.fork_seq,"
            " (SELECT max(seq) FROM {} WHERE session_id = s.id) FROM {} s WHERE s.id = %s"
        ).format(events, sessions)
        self._lock = self._head + sql.SQL(" FOR UPDATE OF s")
        self._insert = sql.SQL(
            "INSERT INTO {} (session_id, seq, type, v, data) VALUES (%s, %s, %s, %s, %s)"
        ).format(events)
        self._select = sql.SQL(
            "SELECT data FROM {} WHERE session_id = %s AND seq > %s"
            " AND (%s::int IS NULL OR seq <= %s::int) ORDER BY seq"
        ).format(events)
        self._fork = sql.SQL(
            "INSERT INTO {} (id, parent_id, fork_seq) VALUES (%s, %s, %s)"
            " ON CONFLICT (id) DO NOTHING"
        ).format(sessions)

    async def _open(self) -> None:
        if self._opened and not self._closed:
            return
        async with self._opening:
            if self._closed:
                raise StoreError("the session store is closed")
            if not self._opened:
                await self._open_once()

    async def _open_once(self) -> None:
        # Everything up to the first successful query can fail on the connection: none of it
        # may pass psycopg's message on, which can quote the connection string.
        try:
            await self._pool.open(wait=True, timeout=5)
            async with self._pool.connection() as conn:
                found = await conn.execute(
                    "SELECT to_regclass(%s)", (self._events_table.as_string(conn),)
                )
                row = await found.fetchone()
        except Exception as exc:
            await self._pool.close()
            raise connection_error(exc, self._url) from None
        if row is None or row[0] is None:
            raise StoreError(f"{MIGRATE} (schema {self.schema})")
        self._opened = True

    async def close(self) -> None:
        self._closed = True
        await self._pool.close()

    # --- the contract ---

    async def _head_of(
        self, cur: AsyncCursor[Any], session_id: str, *, lock: bool = False
    ) -> tuple[Any, ...] | None:
        """(parent_id, fork_seq, tail) of a session, or None when there is none."""
        await cur.execute(self._lock if lock else self._head, (session_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        parent, fork_seq, own_max = row
        return parent, fork_seq, own_max if own_max is not None else (fork_seq or 0)

    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        await self._open()
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            if events and expected_seq == 0:  # a session is created by its first batch only
                await cur.execute(self._create, (session_id,))
            # one writer per session at a time: the second waits here, then sees the new tail
            head = await self._head_of(cur, session_id, lock=True)
            tail = 0 if head is None else head[2]
            if expected_seq != tail:
                raise SeqConflict(
                    f"session {session_id}: expected seq {expected_seq}, tail is {tail}"
                )
            check_batch(session_id, events, tail=tail)
            if events:
                rows = [(session_id, e.seq, e.type, e.v, json.dumps(dump(e))) for e in events]
                await cur.executemany(self._insert, rows)

    async def read(
        self, session_id: str, *, after_seq: int = 0, upto_seq: int | None = None
    ) -> Sequence[Event]:
        await self._open()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            # the session and its ancestors, nearest first, each with the last seq it gives
            chain: list[tuple[str, int | None]] = []
            sid: str | None = session_id
            limit = upto_seq
            while sid is not None:
                head = await self._head_of(cur, sid)
                if head is None:
                    raise UnknownSession(f"no session {sid}")
                chain.append((sid, limit))
                parent, fork_seq, _ = head
                if parent is not None:
                    limit = fork_seq if limit is None else min(limit, fork_seq)
                sid = parent
            rows: list[Any] = []
            for sid, upper in reversed(chain):  # the oldest ancestor's events come first
                if upper is not None and upper <= after_seq:
                    continue  # nothing in this part is after what was asked for
                await cur.execute(self._select, (sid, after_seq, upper, upper))
                rows += await cur.fetchall()
        return [load(data) for (data,) in rows]  # EventRejected for what this code cannot read

    async def fork(self, session_id: str, *, at_seq: int) -> str:
        await self._open()
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            head = await self._head_of(cur, session_id)
            if head is None:
                raise UnknownSession(f"no session {session_id}")
            if not 1 <= at_seq <= head[2]:
                raise EventRejected(f"session {session_id} has no seq {at_seq} to fork at")
            child = self._ids()
            await cur.execute(self._fork, (child, session_id, at_seq))
            if cur.rowcount != 1:
                raise EventRejected(f"session {child} already exists")
            return child
