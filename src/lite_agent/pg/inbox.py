"""The inbox and the timers of always-on agents (design §9.1, S7b; KB llm-app-architecture §12).

Every trigger (a webhook, a schedule, a person's reply) writes a row to one durable inbox and
wakes the inbox's worker; nothing calls an agent directly. A source that delivers twice is
recorded once (its own id is the key). Timers are not work: when one is due, `fire_due` writes
its inbox event in the same transaction and moves the timer on, and the worker does the rest.

`PgInbox` lives on a `PgSessionStore` (its pool and its schema), next to the sessions it feeds.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import sql

from lite_agent.pg.store import PgSessionStore

Status = Literal["done", "refused"]


@dataclass(frozen=True)
class InboxEvent:
    id: int
    inbox: str
    source: str
    source_id: str
    kind: Literal["message", "decision"]
    session_id: str | None  # a continuation of this session; None: work for a new one
    text: str | None = None  # message
    approve: dict[str, bool] = field(default_factory=dict)  # decision
    results: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # decision: content, is_error, unknown


@dataclass(frozen=True)
class Fired:
    """An inbox a due timer wrote to, and the agent its worker runs."""

    inbox: str
    agent: str


class PgInbox:
    def __init__(self, store: PgSessionStore) -> None:
        self.store = store

        def t(name: str) -> sql.Identifier:  # one schema: the inbox is the sessions' neighbour
            return sql.Identifier(store.schema, name)

        self._put = sql.SQL(
            "INSERT INTO {} (inbox, agent, source, source_id, kind, session_id, payload)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (inbox, source, source_id) DO NOTHING"
        ).format(t("inbox_events"))
        self._pending = sql.SQL(
            "SELECT id, inbox, source, source_id, kind, session_id, payload FROM {}"
            " WHERE inbox = %s AND status = 'pending' ORDER BY id LIMIT %s"
        ).format(t("inbox_events"))
        self._finish = sql.SQL(
            "UPDATE {} SET status = %s, done_at = now(), run_session = %s"
            " WHERE id = ANY(%s) AND status = 'pending'"
        ).format(t("inbox_events"))
        self._timer = sql.SQL(
            "INSERT INTO {} (inbox, agent, kind, due_at, every_s, text, session_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"
        ).format(t("timers"))
        self._due = sql.SQL(
            "SELECT id, inbox, agent, kind, due_at, every_s, text, session_id FROM {}"
            " WHERE status = 'active' AND due_at <= %s ORDER BY due_at, id FOR UPDATE SKIP LOCKED"
        ).format(t("timers"))
        self._stalled = sql.SQL(
            "SELECT DISTINCT inbox, agent FROM {} WHERE status = 'pending' AND created_at <= %s"
            " ORDER BY inbox"
        ).format(t("inbox_events"))
        self._done = sql.SQL("UPDATE {} SET status = 'done' WHERE id = %s").format(t("timers"))
        self._next = sql.SQL("UPDATE {} SET due_at = %s WHERE id = %s").format(t("timers"))

    async def put(
        self,
        inbox: str,
        source: str,
        source_id: str,
        *,
        text: str | None = None,
        session_id: str | None = None,
        approve: Mapping[str, bool] | None = None,
        results: Mapping[str, Mapping[str, Any]] | None = None,
        agent: str,
    ) -> bool:
        """Record one event for the inbox whose worker runs `agent`; False when this source
        already delivered it. A message has text (and, to continue a session, its id); a
        decision answers a paused session."""
        decision = bool(approve or results)
        if decision and session_id is None:
            raise ValueError("a decision answers a session: give its session_id")
        if not decision and not (text and text.strip()):
            raise ValueError("a message needs text")
        payload = (
            {
                "approve": dict(approve or {}),
                "results": {k: dict(v) for k, v in (results or {}).items()},
            }
            if decision
            else {"text": text}
        )
        kind = "decision" if decision else "message"
        async with self.store._connection() as conn, conn.cursor() as cur:
            await cur.execute(
                self._put,
                (inbox, agent, source, source_id, kind, session_id, json.dumps(payload)),
            )
            return cur.rowcount == 1

    async def pending(self, inbox: str, limit: int = 100) -> list[InboxEvent]:
        """The inbox's work in arrival order."""
        async with self.store._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._pending, (inbox, limit))
            rows = await cur.fetchall()
        return [
            InboxEvent(
                id=row[0],
                inbox=row[1],
                source=row[2],
                source_id=row[3],
                kind=row[4],
                session_id=row[5],
                text=row[6].get("text"),
                approve=dict(row[6].get("approve") or {}),
                results=dict(row[6].get("results") or {}),
            )
            for row in rows
        ]

    async def finish(
        self, ids: Sequence[int], *, status: Status = "done", run_session: str | None = None
    ) -> None:
        """Mark events handled (or refused: a decision for a session that waits for nothing)."""
        async with self.store._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._finish, (status, run_session, list(ids)))

    async def add_follow_up(
        self, inbox: str, agent: str, due_at: datetime, text: str, *, session_id: str | None = None
    ) -> int:
        """A deadline the agent set itself: at `due_at`, `text` arrives in the inbox once."""
        return await self._add(inbox, agent, "follow_up", due_at, None, text, session_id)

    async def add_schedule(
        self, inbox: str, agent: str, first_at: datetime, every_s: int, text: str
    ) -> int:
        """A recurring trigger: `text` arrives at `first_at` and every `every_s` seconds after."""
        if every_s <= 0:
            raise ValueError("every_s must be positive")
        return await self._add(inbox, agent, "schedule", first_at, every_s, text, None)

    async def _add(self, *row: Any) -> int:
        async with self.store._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._timer, row)
            got = await cur.fetchone()
        assert got is not None
        return int(got[0])

    async def fire_due(self, now: datetime) -> list[Fired]:
        """Every timer due at `now` writes its inbox event, in the same transaction that moves
        it on: a follow-up is done, a schedule's next time is the first one after `now` (a
        scheduler that was down fires a missed schedule once, not once per missed slot). A
        firing's source id names the timer and the time it was due, so it is written once."""
        fired: dict[Fired, None] = {}  # an ordered set
        async with self.store._connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(self._due, (now,))
            for (
                timer_id,
                inbox,
                agent,
                kind,
                due_at,
                every_s,
                text,
                session_id,
            ) in await cur.fetchall():
                firing = f"{timer_id}@{due_at.isoformat()}"
                await cur.execute(
                    self._put,
                    (
                        inbox,
                        agent,
                        "timer",
                        firing,
                        "message",
                        session_id,
                        json.dumps({"text": text}),
                    ),
                )
                if kind == "follow_up":
                    await cur.execute(self._done, (timer_id,))
                else:
                    period = timedelta(seconds=every_s)
                    slots = (now - due_at) // period + 1  # exact: no float at a slot's edge
                    await cur.execute(self._next, (due_at + period * slots, timer_id))
                fired.setdefault(Fired(inbox, agent))
        return list(fired)

    async def stalled(self, before: datetime) -> list[Fired]:
        """Inboxes with an event pending since `before`: whoever wrote it died before it woke
        the worker (or the wake was lost). The scheduler wakes them (design §9.1)."""
        async with self.store._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._stalled, (before,))
            return [Fired(inbox, agent) for inbox, agent in await cur.fetchall()]
