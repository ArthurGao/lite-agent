"""Design §4: the store appends whole batches at the expected tail, never rewrites, and a fork
reads its parent's prefix without copying it."""

import pytest

from lite_agent import EventRejected, InMemorySessionStore, SeqConflict, UnknownSession
from lite_agent.events import Event, SessionStarted, UserMessage
from lite_agent.store import check_batch
from tests.helpers import T0, counter


def started(session: str = "s1") -> Event:
    return SessionStarted(session_id=session, seq=1, time=T0, run_id="r1")


def said(seq: int, text: str = "hi", session: str = "s1") -> Event:
    return UserMessage(session_id=session, seq=seq, time=T0, run_id="r1", text=text)


async def test_append_then_read_returns_the_events_in_order() -> None:
    store = InMemorySessionStore()
    await store.append("s1", [started(), said(2)], expected_seq=0)
    await store.append("s1", [said(3, "again")], expected_seq=2)
    assert await store.read("s1") == [started(), said(2), said(3, "again")]
    assert await store.read("s1", upto_seq=2) == [started(), said(2)]


async def test_reading_a_session_that_does_not_exist_raises() -> None:
    with pytest.raises(UnknownSession):
        await InMemorySessionStore().read("nope")


async def test_a_stale_expected_seq_is_refused_and_nothing_is_written() -> None:
    store = InMemorySessionStore()
    await store.append("s1", [started(), said(2)], expected_seq=0)
    with pytest.raises(SeqConflict):
        await store.append("s1", [said(3)], expected_seq=1)  # another writer got there first
    assert await store.read("s1") == [started(), said(2)]


@pytest.mark.parametrize(
    "batch",
    [
        [said(2), said(4)],  # a gap
        [said(3)],  # does not continue the tail
        [said(2), said(3, session="s2")],  # another session's event
        [said(2), "not an event"],
    ],
    ids=["gap", "skips", "other_session", "not_an_event"],
)
async def test_a_batch_with_one_bad_event_is_refused_whole(batch: list[Event]) -> None:
    store = InMemorySessionStore()
    await store.append("s1", [started()], expected_seq=0)
    with pytest.raises(EventRejected):
        await store.append("s1", batch, expected_seq=1)
    assert await store.read("s1") == [started()]  # not even the valid first event of the batch


async def test_a_session_must_begin_with_session_started() -> None:
    store = InMemorySessionStore()
    with pytest.raises(EventRejected):
        await store.append("s1", [said(1)], expected_seq=0)
    with pytest.raises(UnknownSession):
        await store.read("s1")


async def test_a_fork_reads_the_parents_prefix_and_then_its_own_events() -> None:
    store = InMemorySessionStore(ids=counter("fork"))
    await store.append("s1", [started(), said(2), said(3, "later")], expected_seq=0)

    child = await store.fork("s1", at_seq=2)
    assert child == "fork1"
    assert await store.read(child) == [started(), said(2)]  # the parent's own events, not copies

    await store.append(child, [said(3, "mine", session=child)], expected_seq=2)
    await store.append("s1", [said(4, "parent goes on")], expected_seq=3)

    assert [e.seq for e in await store.read(child)] == [1, 2, 3]
    assert (await store.read(child))[-1] == said(3, "mine", session=child)
    assert [e.seq for e in await store.read("s1")] == [1, 2, 3, 4]  # the fork changed nothing


async def test_a_fork_point_must_exist() -> None:
    store = InMemorySessionStore()
    await store.append("s1", [started()], expected_seq=0)
    for at_seq in (0, 2):
        with pytest.raises(EventRejected):
            await store.fork("s1", at_seq=at_seq)


def test_check_batch_is_the_contract_any_store_can_call() -> None:
    check_batch("s1", [started(), said(2)], tail=0)  # a valid opening batch
    check_batch("s1", [], tail=5)  # nothing to check
    with pytest.raises(EventRejected, match="seq 3 where 6 was due"):
        check_batch("s1", [said(3)], tail=5)
    with pytest.raises(EventRejected, match="session_started"):
        check_batch("s1", [said(1)], tail=0)


async def test_forking_a_session_that_does_not_exist_raises_unknown_session() -> None:
    with pytest.raises(UnknownSession):
        await InMemorySessionStore().fork("nope", at_seq=1)


async def test_a_fork_never_takes_the_id_of_a_session_that_exists() -> None:
    # Codex review 2026-11-21: an id factory that repeats must not re-parent a session.
    store = InMemorySessionStore(ids=lambda: "s2")
    await store.append("s1", [started()], expected_seq=0)
    await store.append("s2", [started("s2"), said(2, session="s2")], expected_seq=0)
    with pytest.raises(EventRejected, match="s2"):
        await store.fork("s1", at_seq=1)
    assert await store.read("s2") == [started("s2"), said(2, session="s2")]
