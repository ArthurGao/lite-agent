"""`copy_session` (design §15 S4b, KB llm-app-architecture §10.3): a session's whole state is in
its events, so a copy in another store runs on as if it had never moved."""

from collections.abc import Callable

import pytest
from lite_model.testing import AsyncFakeModel

from lite_agent import (
    InMemorySessionStore,
    Resume,
    SeqConflict,
    SessionStore,
    UnknownSession,
    copy_session,
)
from tests.helpers import Ask, Recording, agent, call, kinds, say

Make = Callable[..., SessionStore]


async def test_a_paused_session_copied_into_another_store_resumes_there(make_store: Make) -> None:
    tools = Recording()
    source = InMemorySessionStore()
    paying = agent(
        AsyncFakeModel(agent=[call(("c1", "pay", {"amount": 5}))]),
        tools.tool("pay", "receipt"),
        gates=[Ask()],
        sessions=source,
    )
    paused = await paying.run("Pay.")
    assert paused.outcome == "paused"

    target = make_store()
    copied = await copy_session(source, target, paused.session_id, new_session_id="moved")
    # another agent object, on the other store, with nothing but the copied events
    elsewhere = agent(
        AsyncFakeModel(agent=[say("Paid.")]),
        tools.tool("pay", "receipt"),
        gates=[Ask()],
        sessions=target,
    )
    done = await elsewhere.resume(copied, Resume(approve={"c1": True}))

    assert (copied, done.outcome, done.text) == ("moved", "completed", "Paid.")
    assert tools.ran == [("pay", {"amount": 5})]
    original = list(await source.read(paused.session_id))
    moved = list(await target.read("moved"))
    # the copy holds the original's events exactly, under the new id, then what the resume wrote
    assert moved[: len(original)] == [
        e.model_copy(update={"session_id": "moved"}) for e in original
    ]
    assert kinds(moved[len(original) :]) == [
        "run_started",
        "resumed",
        "tool_started",
        "tool_result",
        "model_turn",
        "run_finished",
    ]
    assert list(await source.read(paused.session_id)) == original  # the source is untouched


async def test_a_fork_is_copied_flat_with_its_inherited_events(make_store: Make) -> None:
    source = InMemorySessionStore()
    a = agent(AsyncFakeModel(agent=[say("One."), say("Child.")]), sessions=source)
    first = await a.run("First?")
    child = await source.fork(first.session_id, at_seq=5)
    await a.run("In the fork?", session_id=child)

    target = make_store()
    copied = await copy_session(source, target, child)

    flat = list(await target.read(copied))
    assert copied != child
    assert [e.seq for e in flat] == list(range(1, 10))
    assert {e.session_id for e in flat} == {copied}
    # inherited events carry the parent's id in the source: in the copy every one is the copy's
    assert flat == [e.model_copy(update={"session_id": copied}) for e in await source.read(child)]


async def test_a_copy_never_lands_on_an_existing_session(make_store: Make) -> None:
    source = InMemorySessionStore()
    first = await agent(AsyncFakeModel(agent=[say("One.")]), sessions=source).run("First?")
    target = make_store()
    await copy_session(source, target, first.session_id, new_session_id="taken")
    with pytest.raises(SeqConflict):
        await copy_session(source, target, first.session_id, new_session_id="taken")
    assert len(await target.read("taken")) == 5  # the first copy, unchanged


async def test_copying_a_session_that_does_not_exist_raises(make_store: Make) -> None:
    with pytest.raises(UnknownSession):
        await copy_session(InMemorySessionStore(), make_store(), "nowhere")


async def test_an_empty_new_id_is_refused_not_replaced(make_store: Make) -> None:
    # Codex review 2026-11-21: "" is a caller's mistake, not "make one up"
    source = InMemorySessionStore()
    first = await agent(AsyncFakeModel(agent=[say("One.")]), sessions=source).run("First?")
    with pytest.raises(ValueError, match="new_session_id"):
        await copy_session(source, make_store(), first.session_id, new_session_id="")
