"""The durable inbox and timers (design §9.1, S7b) on PostgreSQL."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from lite_agent.pg import PgSessionStore
from lite_agent.pg.inbox import Fired, PgInbox

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def inbox(pg_url: str, pg_schema: str) -> AsyncIterator[PgInbox]:
    store = PgSessionStore(pg_url, schema=pg_schema)
    yield PgInbox(store)
    await store.close()


async def test_a_source_that_delivers_twice_is_recorded_once(inbox: PgInbox) -> None:
    assert (
        await inbox.put("ana", "webhook", "evt-1", text="Where is my lamp?", agent="echo") is True
    )
    assert (
        await inbox.put("ana", "webhook", "evt-1", text="Where is my lamp?", agent="echo") is False
    )
    assert (
        await inbox.put("ana", "email", "evt-1", text="Same id, another source.", agent="echo")
        is True
    )
    assert [(e.source, e.text) for e in await inbox.pending("ana")] == [
        ("webhook", "Where is my lamp?"),
        ("email", "Same id, another source."),
    ]
    assert await inbox.pending("bo") == []


async def test_finished_events_leave_the_pending_list(inbox: PgInbox) -> None:
    await inbox.put("ana", "chat", "1", text="One.", agent="echo")
    await inbox.put("ana", "chat", "2", text="Two.", session_id="s1", agent="echo")
    await inbox.put("ana", "chat", "3", session_id="s1", approve={"c1": True}, agent="echo")
    events = await inbox.pending("ana")
    assert [(e.kind, e.session_id, e.approve) for e in events] == [
        ("message", None, {}),
        ("message", "s1", {}),
        ("decision", "s1", {"c1": True}),
    ]
    await inbox.finish([events[0].id, events[1].id], run_session="s1")
    await inbox.finish([events[2].id], status="refused")
    assert await inbox.pending("ana") == []


async def test_a_decision_needs_a_session_and_a_message_text(inbox: PgInbox) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await inbox.put("ana", "chat", "1", approve={"c1": True}, agent="echo")
    with pytest.raises(ValueError, match="text"):
        await inbox.put("ana", "chat", "2", text="  ", agent="echo")


async def test_a_follow_up_fires_once_when_it_is_due(inbox: PgInbox) -> None:
    await inbox.add_follow_up(
        "ana", "support", T0 + timedelta(hours=1), "Check B03 again.", session_id="s1"
    )
    assert await inbox.fire_due(T0) == []
    assert await inbox.fire_due(T0 + timedelta(hours=1)) == [Fired("ana", "support")]
    assert await inbox.fire_due(T0 + timedelta(hours=2)) == []
    (event,) = await inbox.pending("ana")
    assert (event.source, event.text, event.session_id) == ("timer", "Check B03 again.", "s1")


async def test_a_schedule_fires_each_period_and_a_missed_stretch_once(inbox: PgInbox) -> None:
    await inbox.add_schedule("ops", "reporter", T0, 3600, "Daily report.")
    assert await inbox.fire_due(T0) == [Fired("ops", "reporter")]
    assert await inbox.fire_due(T0 + timedelta(minutes=30)) == []
    assert await inbox.fire_due(T0 + timedelta(hours=1)) == [Fired("ops", "reporter")]
    # down for five hours: one firing, and the next one is the first slot after now
    assert await inbox.fire_due(T0 + timedelta(hours=6, minutes=10)) == [Fired("ops", "reporter")]
    assert await inbox.fire_due(T0 + timedelta(hours=6, minutes=50)) == []
    assert await inbox.fire_due(T0 + timedelta(hours=7)) == [Fired("ops", "reporter")]
    assert len(await inbox.pending("ops")) == 4
