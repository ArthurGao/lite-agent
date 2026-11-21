"""The loop's own guarantees, on both stores: nothing it relies on is particular to the
in-memory one (design §3, KB llm-app-architecture §10.3)."""

from collections.abc import Callable, Sequence

from lite_model import Message
from lite_model.testing import AsyncFakeModel

from lite_agent import (
    Budget,
    PauseFor,
    Resume,
    RunContext,
    RunError,
    SessionStore,
    SummaryCompactor,
    derive,
)
from lite_agent.events import Event, ToolResultEvent
from tests.helpers import Recording, agent, call, kinds, say

Make = Callable[..., SessionStore]


class Ask:
    name = "ask"

    async def check(self, c: object, ctx: RunContext) -> PauseFor:
        return PauseFor("a person decides")


async def test_a_run_with_tools_a_pause_a_resume_and_a_compaction(make_store: Make) -> None:
    tools = Recording()
    model = AsyncFakeModel(
        agent=[say("One."), call(("c1", "pay", {"amount": 5})), say("Paid."), say("Done.")],
        compact=lambda asked: say("Earlier the user asked one thing."),  # as often as asked
    )
    store = make_store()
    compactor = SummaryCompactor(context_tokens={"agent": 1}, keep_turns=1)
    a = agent(
        model, tools.tool("pay", "receipt"), gates=[Ask()], sessions=store, compaction=compactor
    )

    first = await a.run("First?")
    paused = await a.run("Pay.", session_id=first.session_id)
    assert paused.outcome == "paused"
    done = await a.resume(paused.session_id, Resume(approve={"c1": True}))
    last = await a.run("Anything else?", session_id=first.session_id)

    assert (done.text, last.text) == ("Paid.", "Done.")
    events = list(await store.read(first.session_id))
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert "compaction" in kinds(events) and tools.ran == [("pay", {"amount": 5})]
    first_message = derive(events).messages[0]
    assert isinstance(first_message, Message)
    assert first_message.content.endswith("Earlier the user asked one thing.")


async def test_a_fork_runs_on_without_touching_its_parent(make_store: Make) -> None:
    model = AsyncFakeModel(agent=[say("One."), say("Child."), say("Parent.")])
    store = make_store()
    a = agent(model, sessions=store)
    first = await a.run("First?")
    child = await store.fork(first.session_id, at_seq=5)

    await a.run("In the fork?", session_id=child)
    await a.run("In the parent?", session_id=first.session_id)

    assert [e.seq for e in await store.read(child)] == list(range(1, 10))
    parent_texts = [getattr(e, "text", None) for e in await store.read(first.session_id)]
    assert "In the fork?" not in parent_texts and "In the parent?" in parent_texts


class Forgetful:
    """A store that keeps only what it is told to and loses one tool result."""

    def __init__(self, inner: SessionStore) -> None:
        self.inner = inner

    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        await self.inner.append(session_id, events, expected_seq=expected_seq)

    async def read(
        self, session_id: str, *, after_seq: int = 0, upto_seq: int | None = None
    ) -> Sequence[Event]:
        got = await self.inner.read(session_id, after_seq=after_seq, upto_seq=upto_seq)
        return [e for e in got if not isinstance(e, ToolResultEvent)]

    async def fork(self, session_id: str, *, at_seq: int) -> str:
        return await self.inner.fork(session_id, at_seq=at_seq)


async def test_a_store_that_returns_less_than_was_written_stops_the_run(make_store: Make) -> None:
    model = AsyncFakeModel(agent=[call(("c1", "get_time", {})), say("Noon.")])
    a = agent(model, Recording().tool("get_time", "12:00"), sessions=Forgetful(make_store()))
    result = await a.run("Time?")
    assert result.error == RunError("harness", "HarnessError")
    assert len(model.turn_calls) == 1


async def test_the_budget_holds_across_runs_on_either_store(make_store: Make) -> None:
    model = AsyncFakeModel(agent=[say("One."), say("Two.")])
    a = agent(model, sessions=make_store(), budget=Budget(turns=2))
    first = await a.run("One?")
    await a.run("Two?", session_id=first.session_id)
    third = await a.run("Three?", session_id=first.session_id)
    assert third.stop == "budget_turns" and len(model.turn_calls) == 2
