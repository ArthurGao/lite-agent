"""Where a session's events live (design §4). A store only appends and reads; it never
rewrites. `InMemorySessionStore` is the default and the reference for the contract."""

import uuid
from collections.abc import Callable, Sequence
from typing import Protocol

from lite_agent.errors import EventRejected, SeqConflict, UnknownSession
from lite_agent.events import EVENT_TYPES, Event, SessionStarted


class SessionStore(Protocol):
    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        """Append a batch, all or nothing. `expected_seq` is the caller's view of the session's
        tail (0 for a new session); a different tail raises SeqConflict."""
        ...

    async def read(
        self, session_id: str, *, after_seq: int = 0, upto_seq: int | None = None
    ) -> Sequence[Event]:
        """The session's events in order, inherited ones first, with seqs in (after_seq, upto_seq];
        UnknownSession if there is none."""
        ...

    async def fork(self, session_id: str, *, at_seq: int) -> str:
        """A new session that shares `session_id`'s events up to `at_seq` without copying them."""
        ...


def new_id() -> str:
    return uuid.uuid4().hex


def check_batch(session_id: str, events: Sequence[Event], *, tail: int) -> None:
    """The append contract every store enforces before it writes anything (design §4): the batch
    is for this session, continues its tail without a gap, and a session begins with
    `session_started`. Raises EventRejected; a store calls this once it knows the tail."""
    for offset, event in enumerate(events, start=1):
        if not isinstance(event, EVENT_TYPES):
            raise EventRejected(f"not an event: {type(event).__name__}")
        if event.session_id != session_id:
            raise EventRejected(f"event for session {event.session_id} in {session_id}")
        if event.seq != tail + offset:
            raise EventRejected(f"seq {event.seq} where {tail + offset} was due")
    if tail == 0 and events and not isinstance(events[0], SessionStarted):
        raise EventRejected("a session's first event must be session_started")


class InMemorySessionStore:
    def __init__(self, ids: Callable[[], str] = new_id) -> None:
        self._events: dict[str, list[Event]] = {}
        self._parents: dict[str, tuple[str, int]] = {}  # child -> (parent, at_seq)
        self._ids = ids

    def _known(self, session_id: str) -> None:
        if session_id not in self._events and session_id not in self._parents:
            raise UnknownSession(f"no session {session_id}")

    def _tail(self, session_id: str) -> int:
        own = self._events.get(session_id)
        if own:
            return own[-1].seq
        parent = self._parents.get(session_id)
        return parent[1] if parent else 0

    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        tail = self._tail(session_id)
        if expected_seq != tail:
            raise SeqConflict(f"session {session_id}: expected seq {expected_seq}, tail is {tail}")
        check_batch(session_id, events, tail=tail)  # before anything is stored: whole or not at all
        if events:  # a session is created by its first batch, never by an empty one
            self._events.setdefault(session_id, []).extend(events)

    async def read(
        self, session_id: str, *, after_seq: int = 0, upto_seq: int | None = None
    ) -> Sequence[Event]:
        self._known(session_id)
        events: list[Event] = []
        if session_id in self._parents:  # the prefix never changes: a parent only appends
            parent, at_seq = self._parents[session_id]
            events += await self.read(parent, upto_seq=at_seq)
        events += self._events.get(session_id, [])
        return [e for e in events if e.seq > after_seq and (upto_seq is None or e.seq <= upto_seq)]

    async def fork(self, session_id: str, *, at_seq: int) -> str:
        self._known(session_id)
        if not 1 <= at_seq <= self._tail(session_id):  # seqs run from 1 without a gap
            raise EventRejected(f"session {session_id} has no seq {at_seq} to fork at")
        child = self._ids()
        if child in self._events or child in self._parents:
            raise EventRejected(f"session {child} already exists")
        self._parents[child] = (session_id, at_seq)
        return child


async def copy_session(
    source: SessionStore,
    target: SessionStore,
    session_id: str,
    *,
    new_session_id: str | None = None,
) -> str:
    """Write `session_id`'s events, inherited ones included, into `target` as a new session of
    its own, and return its id (`new_session_id`, or a fresh one). A fork arrives flat: its
    lineage is store metadata and does not travel. Nothing else changes, so under the same agent
    the copy resumes, forks and compacts as the original would (design §4, §15 S4b). It is the
    session as `source` held it when read: events appended after that are not in it. One batch:
    the copy is whole or absent, and an id `target` already has is refused (SeqConflict)."""
    if new_session_id == "":
        raise ValueError("new_session_id must not be empty (None makes a fresh one)")
    copied = new_session_id if new_session_id is not None else new_id()
    events = [e.model_copy(update={"session_id": copied}) for e in await source.read(session_id)]
    await target.append(copied, events, expected_seq=0)
    return copied
