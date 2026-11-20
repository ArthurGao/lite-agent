"""Design §7, §8 (stage S2b): gates decide before a tool runs; a run that must wait ends as
`paused`; `resume()` settles every call the last turn left without a result and goes on. One
mechanism covers approval, tools the host executes, and runs interrupted mid-call."""

from collections.abc import Mapping
from typing import Any

import pytest
from lite_model import (
    AssistantTurn,
    CallFailed,
    Message,
    Opaque,
    Text,
    ToolCall,
    ToolResult,
    ToolResults,
)
from lite_model.testing import AsyncFakeModel
from pydantic import BaseModel

from lite_agent import (
    Allow,
    Budget,
    Deny,
    FunctionTool,
    InMemorySessionStore,
    Pause,
    PauseFor,
    Resume,
    RunContext,
    ToolContext,
    ToolOutcome,
    UnknownSession,
    UnresolvedCalls,
    Waiting,
    tool,
)
from lite_agent.events import Paused, Resumed, RunStarted, ToolStarted
from tests.helpers import (
    THINKING,
    FailingOnce,
    Recording,
    agent,
    call,
    events_of,
    kinds,
    results_of,
    say,
)


class Gate:
    """Decides by tool name and notes what it was asked."""

    name = "test-gate"

    def __init__(self, **decisions: Allow | Deny | PauseFor) -> None:
        self.decisions = decisions
        self.asked: list[tuple[str, str, int]] = []

    async def check(self, call: ToolCall, ctx: RunContext) -> Allow | Deny | PauseFor:
        self.asked.append((call.id, ctx.run_id, len(ctx.events)))
        return self.decisions.get(call.name, Allow())


def external(name: str = "ask_user") -> FunctionTool:
    async def never(args: Mapping[str, Any], ctx: ToolContext) -> str:
        raise AssertionError("an external tool is executed by the host, never here")

    return FunctionTool(name, "Ask.", {"type": "object"}, never, external=True)


# --- gates ---


async def test_a_denied_call_is_answered_without_running_and_the_run_goes_on() -> None:
    tools, gate = Recording(), Gate(delete=Deny("deleting is not allowed here"))
    model = AsyncFakeModel(agent=[call(("c1", "delete", {"path": "/"})), say("Understood.")])
    a = agent(model, tools.tool("delete"), gates=[gate])

    result = await a.run("Clean up.")

    assert (result.outcome, result.text, result.pause) == ("completed", "Understood.", None)
    events = await events_of(a, "id1")
    assert kinds(events)[3:5] == ["model_turn", "tool_result"]  # no tool_started
    (denied,) = results_of(events)
    assert (denied.error_kind, denied.executed, denied.is_error) == ("denied", False, True)
    assert "deleting is not allowed here" in denied.content
    assert tools.ran == []
    assert gate.asked == [("c1", "id2", 4)]  # the call, the run, and the log up to the turn


async def test_gates_are_asked_in_order_and_the_first_refusal_wins() -> None:
    first, second = Gate(pay=Deny("first says no")), Gate(pay=PauseFor("second would ask"))
    model = AsyncFakeModel(agent=[call(("c1", "pay", {})), say("Ok.")])
    a = agent(model, Recording().tool("pay"), gates=[first, second])
    result = await a.run("Pay.")
    assert result.outcome == "completed"
    assert "first says no" in results_of(await events_of(a, "id1"))[0].content
    assert second.asked == []


async def test_invalid_arguments_are_refused_before_any_gate_is_asked() -> None:
    gate = Gate(nope=PauseFor("never reached"))
    model = AsyncFakeModel(agent=[call(("c1", "nope", {})), say("Ok.")])
    a = agent(model, gates=[gate])  # no such tool
    await a.run("Go.")
    assert gate.asked == []
    assert results_of(await events_of(a, "id1"))[0].error_kind == "invalid_args"


# --- approval ---


async def paused_for_approval(**fields: Any) -> tuple[Any, Recording, Gate, Any]:
    tools, gate = Recording(), Gate(pay=PauseFor("payments need a human"))
    model = AsyncFakeModel(
        agent=[call(("c1", "pay", {"amount": 5}), text="Paying."), say("Paid."), say("Not paid.")]
    )
    a = agent(model, tools.tool("pay", "receipt 7"), gates=[gate], **fields)
    result = await a.run("Pay the invoice.")
    return a, tools, gate, result


async def test_a_gate_that_wants_a_person_ends_the_run_as_paused() -> None:
    a, tools, _, result = await paused_for_approval()

    assert (result.outcome, result.stop, result.text) == ("paused", "paused", "Paying.")
    assert result.pause == Pause((Waiting("c1", "approval", "payments need a human", "test-gate"),))
    assert result.pause.call_ids == ("c1",)
    events = await events_of(a, "id1")
    assert kinds(events)[3:] == ["model_turn", "paused", "run_finished"]  # nothing was started
    paused = events[4]
    assert isinstance(paused, Paused)
    assert paused.waiting == result.pause.waiting  # the event holds the same records
    assert tools.ran == []


async def test_an_approved_call_runs_once_and_the_run_completes() -> None:
    a, tools, gate, paused = await paused_for_approval()

    result = await a.resume(paused.session_id, Resume(approve={"c1": True}))

    assert (result.outcome, result.stop, result.text) == ("completed", "end_turn", "Paid.")
    assert (result.session_id, result.run_id) == ("id1", "id3")  # the same session, a new run
    assert tools.ran == [("pay", {"amount": 5})]
    assert [c.call_id for c in tools.contexts] == ["c1"]
    assert len(gate.asked) == 1  # the person answered: the gate is not asked again
    events = await events_of(a, "id1")
    assert kinds(events)[6:] == [
        "run_started",
        "resumed",
        "tool_started",
        "tool_result",
        "model_turn",
        "run_finished",
    ]
    resumed = events[7]
    assert isinstance(resumed, Resumed)
    assert (resumed.approve, resumed.results) == ({"c1": True}, ())
    # the model sees one ordinary result for its call
    assert a.model.turn_calls[1].messages[-1] == ToolResults((ToolResult("c1", "receipt 7"),))


async def test_a_rejected_call_is_answered_as_denied_and_never_runs() -> None:
    a, tools, _, paused = await paused_for_approval()

    result = await a.resume(paused.session_id, Resume(approve={"c1": False}))

    assert (result.outcome, result.text) == ("completed", "Paid.")  # the fake's next line
    (denied,) = results_of(await events_of(a, "id1"))
    assert (denied.error_kind, denied.executed, denied.is_error) == ("denied", False, True)
    assert tools.ran == []
    assert kinds(await events_of(a, "id1"))[8] == "tool_result"  # no tool_started before it


async def test_a_call_waiting_for_approval_keeps_waiting_until_it_is_decided() -> None:
    # Codex review 2026-11-21: the wait is in the log, so it holds whatever the resuming agent's
    # gates say now, and whatever else the resume was given.
    a, tools, gate, paused = await paused_for_approval()

    again = await a.resume(paused.session_id)

    assert again.outcome == "paused" and again.pause == paused.pause
    assert tools.ran == [] and len(gate.asked) == 1  # not asked again: the answer is a person's

    ungated = agent(a.model, tools.tool("pay", "receipt 7"), sessions=a.sessions, ids=lambda: "r9")
    still = await ungated.resume(paused.session_id)  # an agent without the gate
    assert still.outcome == "paused" and still.pause == paused.pause
    assert tools.ran == []


async def test_deciding_one_call_does_not_decide_another_that_waits() -> None:
    tools, gate = Recording(), Gate(pay=PauseFor("ask"), refund=PauseFor("ask"))
    model = AsyncFakeModel(agent=[call(("c1", "pay", {}), ("c2", "refund", {})), say("Done.")])
    a = agent(model, tools.tool("pay"), tools.tool("refund"), gates=[gate])
    paused = await a.run("Both.")
    assert paused.pause is not None and paused.pause.call_ids == ("c1", "c2")

    ungated = agent(
        model, tools.tool("pay"), tools.tool("refund"), sessions=a.sessions, ids=lambda: "r9"
    )
    half = await ungated.resume(paused.session_id, Resume(approve={"c1": True}))

    assert half.outcome == "paused"
    assert half.pause is not None and half.pause.call_ids == ("c2",)
    assert tools.ran == [("pay", {})]  # refund was never decided, so it did not run


async def test_an_approval_covers_one_call_not_a_later_one_with_the_same_id() -> None:
    # simplify review 2026-11-21: models reuse ids like "call_0" from turn to turn; yesterday's
    # yes must not wave a new call past the gates.
    tools, gate = Recording(), Gate(pay=PauseFor("payments need a human"))
    model = AsyncFakeModel(
        agent=[call(("c1", "pay", {"amount": 5})), call(("c1", "pay", {"amount": 5000}))]
    )
    a = agent(model, tools.tool("pay", "receipt"), gates=[gate])
    paused = await a.run("Pay.")

    again = await a.resume(paused.session_id, Resume(approve={"c1": True}))

    assert again.outcome == "paused"  # the second call waits for its own decision
    assert tools.ran == [("pay", {"amount": 5})]
    assert len(gate.asked) == 2


async def test_an_approved_terminal_tool_ends_the_run_on_resume() -> None:
    gate = Gate(submit=PauseFor("check the answer"))
    model = AsyncFakeModel(agent=[call(("c1", "submit", {}), text="Submitting.")])
    a = agent(model, Recording().tool("submit", "accepted", terminal=True), gates=[gate])
    paused = await a.run("Solve.")
    result = await a.resume(paused.session_id, Resume(approve={"c1": True}))
    assert (result.outcome, result.stop) == ("completed", "terminal_tool")
    assert len(model.turn_calls) == 1


async def test_the_rest_of_a_batch_runs_while_one_call_waits() -> None:
    tools = Recording()
    gate = Gate(pay=PauseFor("payments need a human"), delete=Deny("no"))
    batch = call(("c1", "delete", {}), ("c2", "pay", {}), ("c3", "get_time", {}))
    model = AsyncFakeModel(agent=[batch, say("Done.")])
    a = agent(
        model,
        tools.tool("delete"),
        tools.tool("pay", "receipt"),
        tools.tool("get_time", "12:00"),
        gates=[gate],
    )

    paused = await a.run("Do it all.")

    assert paused.pause == Pause((Waiting("c2", "approval", "payments need a human", "test-gate"),))
    assert tools.ran == [("get_time", {})]  # the call that needed nobody did not wait
    await a.resume(paused.session_id, Resume(approve={"c2": True}))
    sent = model.turn_calls[1].messages[-1]
    assert isinstance(sent, ToolResults)
    assert [(r.call_id, r.is_error) for r in sent.results] == [
        ("c1", True),
        ("c2", False),
        ("c3", False),  # in the calls' order
    ]


class BrokenGate:
    name = "broken"

    def __init__(self, answer: Any) -> None:
        self.answer = answer

    async def check(self, call: ToolCall, ctx: RunContext) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer", [RuntimeError("policy service is down"), None, "yes", True])
async def test_a_gate_that_fails_or_answers_nonsense_refuses_the_call(answer: Any) -> None:
    # Codex review 2026-11-21: anything but a decision must not count as permission, and a
    # gate's failure must not leave the run hanging.
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "pay", {})), say("Could not pay.")])
    a = agent(model, tools.tool("pay"), gates=[BrokenGate(answer)])

    result = await a.run("Pay.")

    assert (result.outcome, result.text) == ("completed", "Could not pay.")
    (refused,) = results_of(await events_of(a, "id1"))
    assert (refused.error_kind, refused.executed) == ("denied", False)
    assert "broken" in refused.content and "policy service is down" not in refused.content
    assert tools.ran == []
    assert kinds(await events_of(a, "id1"))[-1] == "run_finished"


# --- tools the host executes ---


async def test_an_external_tool_pauses_the_run_and_its_result_comes_back_on_resume() -> None:
    model = AsyncFakeModel(
        agent=[call(("c1", "ask_user", {"q": "Which one?"})), say("The blue one.")]
    )
    a = agent(model, external())

    paused = await a.run("Pick.")

    assert (paused.outcome, paused.pause) == ("paused", Pause((Waiting("c1", "tool", ""),)))
    events = await events_of(a, "id1")
    assert kinds(events)[3:] == ["model_turn", "tool_started", "paused", "run_finished"]
    started = events[4]
    assert isinstance(started, ToolStarted)
    assert (started.external, started.arguments) == (True, {"q": "Which one?"})

    result = await a.resume(paused.session_id, Resume(results={"c1": ToolOutcome("blue")}))

    assert (result.outcome, result.text) == ("completed", "The blue one.")
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.content, written.origin, written.executed, written.is_error) == (
        "blue",
        "external",
        True,
        False,
    )
    assert model.turn_calls[1].messages[-1] == ToolResults((ToolResult("c1", "blue"),))


async def test_an_external_error_result_is_an_error_for_the_model() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "ask_user", {})), say("Never mind.")])
    a = agent(model, external())
    paused = await a.run("Pick.")
    await a.resume(
        paused.session_id,
        Resume(results={"c1": ToolOutcome("user closed the dialog", is_error=True)}),
    )
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.is_error, written.error_kind, written.origin) == (True, "exception", "external")


async def test_external_calls_of_one_turn_pause_together_and_may_come_back_in_parts() -> None:
    both = call(("c1", "ask_user", {"q": 1}), ("c2", "ask_user", {"q": 2}))
    model = AsyncFakeModel(agent=[both, say("Thanks.")])
    a = agent(model, external())

    paused = await a.run("Ask.")
    assert paused.pause is not None and paused.pause.call_ids == ("c1", "c2")

    half = await a.resume(paused.session_id, Resume(results={"c2": ToolOutcome("two")}))
    assert half.outcome == "paused"
    assert half.pause == Pause((Waiting("c1", "tool", ""),))  # still the host's to run

    done = await a.resume(paused.session_id, Resume(results={"c1": ToolOutcome("one")}))
    assert done.outcome == "completed"
    assert model.turn_calls[1].messages[-1] == ToolResults(
        (ToolResult("c1", "one"), ToolResult("c2", "two"))
    )


async def test_the_hosts_result_for_a_terminal_tool_ends_the_run() -> None:
    async def never(args: Mapping[str, Any], ctx: ToolContext) -> str:
        raise AssertionError("executed by the host")

    hand_off = FunctionTool(
        "hand_off", "Hand off.", {"type": "object"}, never, external=True, terminal=True
    )
    model = AsyncFakeModel(agent=[call(("c1", "hand_off", {}), text="Handing off.")])
    a = agent(model, hand_off)
    paused = await a.run("Escalate.")

    result = await a.resume(paused.session_id, Resume(results={"c1": ToolOutcome("taken over")}))

    assert (result.outcome, result.stop, result.text) == (
        "completed",
        "terminal_tool",
        "Handing off.",
    )
    assert results_of(await events_of(a, "id1"))[0].terminal is True
    assert len(model.turn_calls) == 1

    failed = agent(AsyncFakeModel(agent=[call(("c1", "hand_off", {})), say("Ok.")]), hand_off)
    waiting = await failed.run("Escalate.")
    report = Resume(results={"c1": ToolOutcome("nobody answered", is_error=True)})
    after = await failed.resume(waiting.session_id, report)
    assert (after.stop, after.text) == ("end_turn", "Ok.")  # a failed hand-off does not end it


async def test_a_result_submitted_twice_is_refused_and_nothing_is_written() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "ask_user", {})), say("Ok.")])
    a = agent(model, external())
    paused = await a.run("Pick.")
    await a.resume(paused.session_id, Resume(results={"c1": ToolOutcome("blue")}))
    before = await events_of(a, "id1")

    with pytest.raises(ValueError, match="c1"):
        await a.resume(paused.session_id, Resume(results={"c1": ToolOutcome("red")}))

    assert await events_of(a, "id1") == before
    assert [r.content for r in results_of(before)] == ["blue"]


# --- a run interrupted between a call and its result (§8) ---


async def interrupted(lost: str, **tool_fields: Any) -> tuple[Any, Recording, Any]:
    """A run whose store failed while writing `lost` ("tool_started" or "tool_result") for the
    one call of its first turn, leaving that call without a result."""
    seq = {"tool_started": 5, "tool_result": 6}[lost]
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "pay", {"amount": 5})), say("Paid.")])
    a = agent(model, tools.tool("pay", "receipt 1", **tool_fields), sessions=FailingOnce(seq))
    broken = await a.run("Pay the invoice.")
    assert broken.error is not None and broken.error.kind == "store"
    last = {"tool_started": "model_turn", "tool_result": "tool_started"}[lost]
    assert kinds(await events_of(a, "id1"))[-1] == last  # the log stops just before `lost`
    return a, tools, broken


async def test_an_idempotent_tool_is_run_again_with_the_same_call_id() -> None:
    a, tools, broken = await interrupted("tool_result", idempotent=True)
    assert tools.ran == [("pay", {"amount": 5})]

    result = await a.resume(broken.session_id)

    assert (result.outcome, result.text) == ("completed", "Paid.")
    assert [c.call_id for c in tools.contexts] == ["c1", "c1"]  # the same key both times
    events = await events_of(a, "id1")
    assert sum(isinstance(e, ToolStarted) for e in events) == 1  # not started a second time
    assert [r.origin for r in results_of(events)] == ["executed"]


async def test_a_tool_that_may_have_run_is_not_repeated_and_the_run_waits_to_be_reconciled() -> (
    None
):
    a, tools, broken = await interrupted("tool_result")  # it ran; not idempotent

    waiting = await a.resume(broken.session_id)

    assert waiting.outcome == "paused"
    assert waiting.pause == Pause((Waiting("c1", "reconcile", ""),))
    assert tools.ran == [("pay", {"amount": 5})]  # once, in the interrupted run
    assert results_of(await events_of(a, "id1")) == []  # no result was invented

    result = await a.resume(
        broken.session_id, Resume(results={"c1": ToolOutcome("receipt 1 (checked)")})
    )

    assert (result.outcome, result.text) == ("completed", "Paid.")
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.content, written.origin, written.is_error) == (
        "receipt 1 (checked)",
        "reconciled",
        False,
    )
    assert tools.ran == [("pay", {"amount": 5})]


async def test_an_idempotent_tool_whose_arguments_no_longer_validate_is_not_run_again() -> None:
    # Codex review 2026-11-21: the stored arguments go through today's validation first.
    seen: list[Any] = []

    class Amount(BaseModel):
        amount: int

    class Strict(BaseModel):
        amount: int
        currency: str

    def paying(model: type[BaseModel]) -> Any:
        @tool(description="Pay.", name="pay", idempotent=True)
        async def pay(args: model, ctx: ToolContext) -> str:  # type: ignore[valid-type]
            seen.append(args)
            return "receipt"

        return pay

    store = FailingOnce(6)
    model = AsyncFakeModel(agent=[call(("c1", "pay", {"amount": 5})), say("Paid.")])
    broken = await agent(model, paying(Amount), sessions=store).run("Pay.")
    assert len(seen) == 1

    stricter = agent(model, paying(Strict), sessions=store, ids=lambda: "r9")
    result = await stricter.resume(broken.session_id)

    assert result.pause == Pause((Waiting("c1", "reconcile", ""),))  # a person looks at it
    assert len(seen) == 1 and results_of(await store.read("id1")) == []


async def test_an_outcome_nobody_can_verify_is_told_to_the_model_as_unknown() -> None:
    a, _, broken = await interrupted("tool_result")
    await a.resume(broken.session_id)
    unknown = ToolOutcome("The payment system has no record either way.", unknown=True)
    await a.resume(broken.session_id, Resume(results={"c1": unknown}))
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.is_error, written.error_kind, written.origin) == (
        True,
        "outcome_unknown",
        "reconciled",
    )


async def test_a_call_that_was_never_started_simply_runs_on_resume() -> None:
    a, tools, broken = await interrupted("tool_started")
    assert tools.ran == []
    result = await a.resume(broken.session_id)
    assert (result.outcome, result.text) == ("completed", "Paid.")
    assert tools.ran == [("pay", {"amount": 5})]


async def test_run_still_refuses_a_session_with_unanswered_calls_and_points_to_resume() -> None:
    a, _, _, paused = await paused_for_approval()
    before = await events_of(a, "id1")
    with pytest.raises(UnresolvedCalls, match=r"resume\(\)"):
        await a.run("Something else.", session_id=paused.session_id)
    assert await events_of(a, "id1") == before


# --- resume without open calls, and what resume refuses ---


async def test_resume_after_a_failed_model_call_sends_the_turn_again() -> None:
    tools = Recording()
    first = call(("c1", "get_time", {}))
    model = AsyncFakeModel(agent=[first, CallFailed("agent failed: TimeoutError"), say("Noon.")])
    a = agent(model, tools.tool("get_time", "12:00"))
    failed = await a.run("Time?")

    result = await a.resume(failed.session_id)

    assert (result.outcome, result.text) == ("completed", "Noon.")
    assert tools.ran == [("get_time", {})]  # not run again
    assert model.turn_calls[2].messages == (
        Message("user", "Time?"),
        first,
        ToolResults((ToolResult("c1", "12:00"),)),
    )  # exactly the request that failed: nothing was added


async def test_resume_refuses_what_it_cannot_apply_and_writes_nothing() -> None:
    a, _, _, paused = await paused_for_approval()
    before = await events_of(a, "id1")
    for bad in (
        Resume(approve={"nope": True}),  # not a call of the last turn
        Resume(results={"c1": ToolOutcome("x")}),  # c1 was never started: nothing to report
    ):
        with pytest.raises(ValueError):
            await a.resume(paused.session_id, bad)
    assert await events_of(a, "id1") == before

    ext = agent(AsyncFakeModel(agent=[call(("c1", "ask_user", {}))]), external())
    waiting = await ext.run("Pick.")
    with pytest.raises(ValueError):  # a started call takes a result, not an approval
        await ext.resume(waiting.session_id, Resume(approve={"c1": True}))


async def test_resume_refuses_a_session_that_completed_on_a_terminal_tool() -> None:
    # Codex review 2026-11-21: its history ends on tool results, but the run is over.
    model = AsyncFakeModel(agent=[call(("c1", "submit", {})), say("never asked")])
    a = agent(model, Recording().tool("submit", "accepted", terminal=True))
    done = await a.run("Solve.")
    assert done.stop == "terminal_tool"
    before = await events_of(a, "id1")

    with pytest.raises(ValueError, match="nothing to resume"):
        await a.resume(done.session_id)

    assert await events_of(a, "id1") == before and len(model.turn_calls) == 1


async def test_resume_refuses_a_finished_session_and_an_unknown_one() -> None:
    a = agent(AsyncFakeModel(agent=[say("Done.")]))
    done = await a.run("Hi.")
    with pytest.raises(ValueError, match="nothing to resume"):
        await a.resume(done.session_id)
    with pytest.raises(UnknownSession):
        await a.resume("nope")


# --- what a resume carries over, and what it does not ---


async def test_pausing_and_resuming_does_not_grant_a_fresh_turn_budget() -> None:
    gate = Gate(pay=PauseFor("ask"))
    again = call(("c1", "pay", {}))
    model = AsyncFakeModel(agent=[again, call(("c2", "pay", {})), say("never asked")])
    a = agent(model, Recording().tool("pay"), gates=[gate], budget=Budget(turns=2))
    paused = await a.run("Pay.")
    second = await a.resume(paused.session_id, Resume(approve={"c1": True}))  # turn 2, pauses on c2
    assert second.outcome == "paused"

    third = await a.resume(paused.session_id, Resume(approve={"c2": True}))

    assert (third.outcome, third.stop) == ("failed", "budget_turns")
    assert len(model.turn_calls) == 2


async def test_resuming_under_changed_tools_records_them_and_drops_earlier_thinking() -> None:
    thought = AssistantTurn(
        (Opaque("anthropic", THINKING), Text("Paying."), ToolCall("c1", "pay", {}))
    )
    model = AsyncFakeModel(agent=[thought, say("Paid.")])
    store, tools = InMemorySessionStore(), Recording()
    first = agent(
        model, tools.tool("pay", "receipt"), gates=[Gate(pay=PauseFor("ask"))], sessions=store
    )
    paused = await first.run("Pay.")

    wider = agent(
        model,
        tools.tool("pay", "receipt"),
        tools.tool("refund"),
        sessions=store,
        ids=lambda: "run2",
    )
    await wider.resume(paused.session_id, Resume(approve={"c1": True}))

    starts = [e for e in await store.read("id1") if isinstance(e, RunStarted)]
    assert [[t.name for t in s.tools] for s in starts] == [["pay"], ["pay", "refund"]]
    sent = model.turn_calls[1]
    assert [t.name for t in sent.tools] == ["pay", "refund"]
    assert sent.messages[1] == AssistantTurn((Text("Paying."), ToolCall("c1", "pay", {})))


async def test_an_unchanged_resume_keeps_the_thinking_block() -> None:
    thought = AssistantTurn((Opaque("anthropic", THINKING), ToolCall("c1", "pay", {})))
    model = AsyncFakeModel(agent=[thought, say("Paid.")])
    a = agent(model, Recording().tool("pay"), gates=[Gate(pay=PauseFor("ask"))])
    paused = await a.run("Pay.")
    await a.resume(paused.session_id, Resume(approve={"c1": True}))
    assert model.turn_calls[1].messages[1] == thought
