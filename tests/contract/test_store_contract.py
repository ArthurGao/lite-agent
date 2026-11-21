"""Design §4: the session store contract, run against InMemorySessionStore and PgSessionStore.
A store appends whole batches at the expected tail and never rewrites; a fork reads its parent's
prefix without copying it."""

import asyncio
from collections.abc import Callable

import pytest

from lite_agent import EventRejected, SeqConflict, SessionStore, UnknownSession
from lite_agent.events import Event, SessionStarted, UserMessage
from tests.helpers import T0, counter

Make = Callable[..., SessionStore]


def started(session: str = "s1") -> Event:
    return SessionStarted(session_id=session, seq=1, time=T0, run_id="r1")


def said(seq: int, text: str = "hi", session: str = "s1") -> Event:
    return UserMessage(session_id=session, seq=seq, time=T0, run_id="r1", text=text)


async def test_append_then_read_returns_the_events_in_order(make_store: Make) -> None:
    store = make_store()
    await store.append("s1", [started(), said(2)], expected_seq=0)
    await store.append("s1", [said(3, "again")], expected_seq=2)
    assert await store.read("s1") == [started(), said(2), said(3, "again")]
    assert await store.read("s1", upto_seq=2) == [started(), said(2)]
    assert await store.read("s1", after_seq=1) == [said(2), said(3, "again")]
    assert await store.read("s1", after_seq=1, upto_seq=2) == [said(2)]
    assert await store.read("s1", after_seq=3) == []


async def test_reading_up_to_seq_zero_reads_nothing(make_store: Make) -> None:
    # simplify review 2026-11-21: 0 is a bound, not "no bound"
    store = make_store(counter("f"))
    await store.append("s1", [started(), said(2)], expected_seq=0)
    assert await store.read("s1", upto_seq=0) == []
    child = await store.fork("s1", at_seq=2)
    assert await store.read(child, upto_seq=0) == []


async def test_reading_a_session_that_does_not_exist_raises(make_store: Make) -> None:
    with pytest.raises(UnknownSession):
        await make_store().read("nope")


async def test_a_stale_expected_seq_is_refused_and_nothing_is_written(make_store: Make) -> None:
    store = make_store()
    await store.append("s1", [started(), said(2)], expected_seq=0)
    with pytest.raises(SeqConflict):
        await store.append("s1", [said(3)], expected_seq=1)
    with pytest.raises(SeqConflict):
        await store.append("s2", [started("s2")], expected_seq=4)  # a new session's tail is 0
    assert await store.read("s1") == [started(), said(2)]


@pytest.mark.parametrize(
    "batch",
    [
        [said(2), said(4)],
        [said(3)],
        [said(2), said(3, session="s2")],
        [said(2), "not an event"],
    ],
    ids=["gap", "skips", "other_session", "not_an_event"],
)
async def test_a_batch_with_one_bad_event_is_refused_whole(
    make_store: Make, batch: list[Event]
) -> None:
    store = make_store()
    await store.append("s1", [started()], expected_seq=0)
    with pytest.raises(EventRejected):
        await store.append("s1", batch, expected_seq=1)
    assert await store.read("s1") == [started()]


async def test_a_session_must_begin_with_session_started(make_store: Make) -> None:
    store = make_store()
    with pytest.raises(EventRejected):
        await store.append("s1", [said(1)], expected_seq=0)
    with pytest.raises(UnknownSession):
        await store.read("s1")


async def test_an_empty_batch_writes_nothing_and_creates_no_session(make_store: Make) -> None:
    store = make_store()
    await store.append("s1", [], expected_seq=0)
    with pytest.raises(UnknownSession):
        await store.read("s1")
    with pytest.raises(SeqConflict):
        await store.append("s1", [], expected_seq=3)  # an unknown session's tail is 0
    await store.append("s1", [started(), said(2)], expected_seq=0)
    await store.append("s1", [], expected_seq=2)  # at the tail: a no-op
    with pytest.raises(SeqConflict):
        await store.append("s1", [], expected_seq=1)  # a stale tail is refused, batch or not
    assert await store.read("s1") == [started(), said(2)]


async def test_two_writers_at_the_same_tail_one_wins_one_is_refused(make_store: Make) -> None:
    store = make_store()
    await store.append("s1", [started()], expected_seq=0)
    outcomes = await asyncio.gather(
        store.append("s1", [said(2, "from A")], expected_seq=1),
        store.append("s1", [said(2, "from B")], expected_seq=1),
        return_exceptions=True,
    )
    assert sorted(type(o).__name__ for o in outcomes) == ["NoneType", "SeqConflict"]
    assert len(await store.read("s1")) == 2


async def test_two_writers_creating_one_session_one_wins(make_store: Make) -> None:
    store = make_store()
    outcomes = await asyncio.gather(
        store.append("s1", [started(), said(2, "from A")], expected_seq=0),
        store.append("s1", [started(), said(2, "from B")], expected_seq=0),
        return_exceptions=True,
    )
    assert sorted(type(o).__name__ for o in outcomes) == ["NoneType", "SeqConflict"]
    assert len(await store.read("s1")) == 2


# --- forks ---


async def test_a_fork_reads_the_parents_prefix_and_then_its_own_events(make_store: Make) -> None:
    store = make_store(counter("fork"))
    await store.append("s1", [started(), said(2), said(3, "later")], expected_seq=0)

    child = await store.fork("s1", at_seq=2)
    assert child == "fork1"
    assert await store.read(child) == [started(), said(2)]

    await store.append(child, [said(3, "mine", session=child)], expected_seq=2)
    await store.append("s1", [said(4, "parent goes on")], expected_seq=3)

    assert await store.read(child) == [started(), said(2), said(3, "mine", session=child)]
    assert [e.seq for e in await store.read("s1")] == [1, 2, 3, 4]
    assert await store.read(child, after_seq=2) == [said(3, "mine", session=child)]
    assert await store.read(child, upto_seq=1) == [started()]


async def test_a_fork_of_a_fork_reads_along_the_chain(make_store: Make) -> None:
    store = make_store(counter("f"))
    await store.append("s1", [started(), said(2, "a"), said(3, "b")], expected_seq=0)
    child = await store.fork("s1", at_seq=3)
    await store.append(child, [said(4, "c", session=child)], expected_seq=3)
    grandchild = await store.fork(child, at_seq=4)
    await store.append(grandchild, [said(5, "d", session=grandchild)], expected_seq=4)

    texts = [getattr(e, "text", None) for e in await store.read(grandchild)]
    assert texts == [None, "a", "b", "c", "d"]
    await store.append("s1", [said(4, "parent")], expected_seq=3)
    await store.append(child, [said(5, "child", session=child)], expected_seq=4)
    assert [getattr(e, "text", None) for e in await store.read(grandchild)] == texts


async def test_a_fork_of_a_fork_taken_below_the_first_fork_point(make_store: Make) -> None:
    store = make_store(counter("f"))
    await store.append("s1", [started(), said(2, "a"), said(3, "b")], expected_seq=0)
    child = await store.fork("s1", at_seq=3)
    await store.append(child, [said(4, "c", session=child)], expected_seq=3)
    early = await store.fork(child, at_seq=2)  # inside what the child inherited
    assert [getattr(e, "text", None) for e in await store.read(early)] == [None, "a"]
    await store.append(early, [said(3, "e", session=early)], expected_seq=2)
    assert [getattr(e, "text", None) for e in await store.read(early)] == [None, "a", "e"]


async def test_a_fork_point_must_exist(make_store: Make) -> None:
    store = make_store()
    await store.append("s1", [started()], expected_seq=0)
    for at_seq in (0, 2):
        with pytest.raises(EventRejected):
            await store.fork("s1", at_seq=at_seq)
    with pytest.raises(UnknownSession):
        await store.fork("nope", at_seq=1)


async def test_a_fork_never_takes_the_id_of_a_session_that_exists(make_store: Make) -> None:
    store = make_store(lambda: "s2")
    await store.append("s1", [started()], expected_seq=0)
    await store.append("s2", [started("s2"), said(2, session="s2")], expected_seq=0)
    with pytest.raises(EventRejected, match="s2"):
        await store.fork("s1", at_seq=1)
    assert await store.read("s2") == [started("s2"), said(2, session="s2")]
