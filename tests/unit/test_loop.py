"""Design §5: the loop's rules, each asserted on the events it writes and the requests it
sends. Models are scripted (lite-model's AsyncFakeModel, or `Scripted` when usage matters);
nothing here opens a socket."""

from collections.abc import Sequence
from typing import Any

import pytest
from lite_model import (
    AssistantTurn,
    CallFailed,
    EmptyReply,
    Message,
    Opaque,
    Text,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    Usage,
)
from lite_model.testing import AsyncFakeModel

from lite_agent import Budget, InMemorySessionStore, RunError, UnknownSession, UnresolvedCalls
from lite_agent.events import (
    Event,
    ModelErrorEvent,
    ModelTurn,
    RunFinished,
    RunStarted,
    SessionStarted,
    ToolDef,
    ToolResultEvent,
    ToolStarted,
    UsageData,
    UserMessage,
    blocks_of,
    provenance_data,
)
from tests.helpers import (
    T0,
    THINKING,
    WHO,
    Billed,
    FailingOnce,
    Recording,
    agent,
    call,
    events_of,
    kinds,
    say,
)

ENV: dict[str, Any] = {"session_id": "id1", "time": T0, "run_id": "id2"}
CLOCK_DEF = ToolDef(
    name="get_time", description="The get_time tool.", parameters={"type": "object"}
)
NO_USAGE = UsageData()
FAKE = provenance_data(WHO)


def turn_event(seq: int, turn: AssistantTurn, stop: str, usage: UsageData = NO_USAGE) -> ModelTurn:
    return ModelTurn(
        seq=seq,
        blocks=blocks_of(turn),
        stop=stop,
        role="agent",
        provenance=FAKE,
        usage=usage,
        **ENV,
    )


# --- the basic run ---


async def test_two_tool_rounds_then_an_answer_writes_exactly_these_events() -> None:
    tools = Recording()
    first, second = (
        call(("c1", "get_time", {})),
        call(("c2", "get_time", {"tz": "UTC"}), text="One more."),
    )
    model = AsyncFakeModel(agent=[first, second, say("It is noon.")])
    a = agent(model, tools.tool("get_time", "12:00"), system="Be brief.")

    result = await a.run("What time is it?")

    assert (result.session_id, result.run_id) == ("id1", "id2")
    assert (result.outcome, result.stop, result.text, result.error) == (
        "completed",
        "end_turn",
        "It is noon.",
        None,
    )
    assert await events_of(a, "id1") == [
        SessionStarted(seq=1, **ENV),
        RunStarted(seq=2, role="agent", system="Be brief.", tools=(CLOCK_DEF,), **ENV),
        UserMessage(seq=3, text="What time is it?", **ENV),
        turn_event(4, first, "tool_calls"),
        ToolStarted(seq=5, call_id="c1", name="get_time", arguments={}, **ENV),
        ToolResultEvent(
            seq=6, call_id="c1", content="12:00", executed=True, original_chars=5, **ENV
        ),
        turn_event(7, second, "tool_calls"),
        ToolStarted(seq=8, call_id="c2", name="get_time", arguments={"tz": "UTC"}, **ENV),
        ToolResultEvent(
            seq=9, call_id="c2", content="12:00", executed=True, original_chars=5, **ENV
        ),
        turn_event(10, say("It is noon."), "end"),
        RunFinished(seq=11, outcome="completed", stop="end_turn", usage=NO_USAGE, **ENV),
    ]
    assert tools.ran == [("get_time", {}), ("get_time", {"tz": "UTC"})]
    assert [(c.session_id, c.run_id, c.call_id) for c in tools.contexts] == [
        ("id1", "id2", "c1"),
        ("id1", "id2", "c2"),
    ]


async def test_each_request_is_the_runs_snapshot_and_the_history_so_far() -> None:
    tools = Recording()
    first = call(("c1", "get_time", {}))
    model = AsyncFakeModel(agent=[first, say("Noon.")])
    a = agent(model, tools.tool("get_time", "12:00"), system="Be brief.")
    await a.run("Time?")

    spec = ToolSpec("get_time", "The get_time tool.", {"type": "object"})
    ask = (Message("system", "Be brief."), Message("user", "Time?"))
    assert [(c.role, c.tools) for c in model.turn_calls] == [("agent", (spec,)), ("agent", (spec,))]
    assert model.turn_calls[0].messages == ask
    assert model.turn_calls[1].messages == (*ask, first, ToolResults((ToolResult("c1", "12:00"),)))


async def test_a_second_run_on_the_session_continues_its_history() -> None:
    model = AsyncFakeModel(agent=[say("Noon."), say("Still noon.")])
    a = agent(model)
    first = await a.run("Time?")
    second = await a.run("And now?", session_id=first.session_id)

    assert (second.session_id, second.run_id, second.text) == ("id1", "id3", "Still noon.")
    assert kinds(await events_of(a, "id1")) == [
        "session_started",
        "run_started",
        "user_message",
        "model_turn",
        "run_finished",
        "run_started",
        "user_message",
        "model_turn",
        "run_finished",
    ]
    assert model.turn_calls[1].messages == (
        Message("user", "Time?"),
        say("Noon."),
        Message("user", "And now?"),
    )


async def test_running_on_a_session_that_does_not_exist_is_the_callers_error() -> None:
    with pytest.raises(UnknownSession):
        await agent(AsyncFakeModel(agent=say("x"))).run("Hi", session_id="nope")


# --- §5 rule 1: a truncated turn ---


async def test_a_truncated_turn_runs_no_tool_answers_every_call_and_the_loop_goes_on() -> None:
    tools = Recording()
    cut = AssistantTurn(
        (ToolCall("c1", "get_time", {}), ToolCall("c2", "get_time", None, '{"tz": "U'))
    )
    retry = call(("c3", "get_time", {}))
    model = AsyncFakeModel(agent=[(cut, "length"), retry, say("Noon.")])
    a = agent(model, tools.tool("get_time", "12:00"))

    result = await a.run("Time?")

    assert (result.outcome, result.stop, result.text) == ("completed", "end_turn", "Noon.")
    events = await events_of(a, "id1")
    assert kinds(events) == [
        "session_started",
        "run_started",
        "user_message",
        "model_turn",
        "tool_result",
        "tool_result",  # the cut-off turn: no tool_started
        "model_turn",
        "tool_started",
        "tool_result",
        "model_turn",
        "run_finished",
    ]
    refused = [e for e in events[4:6] if isinstance(e, ToolResultEvent)]
    assert [(e.call_id, e.is_error, e.error_kind, e.executed) for e in refused] == [
        ("c1", True, "truncated_turn", False),
        ("c2", True, "truncated_turn", False),
    ]
    assert tools.ran == [("get_time", {})]  # only the resent call
    # the next request carries the cut-off turn and one error result per call
    sent = model.turn_calls[1].messages
    assert sent[1] == cut
    assert isinstance(sent[2], ToolResults)
    assert [(r.call_id, r.is_error) for r in sent[2].results] == [("c1", True), ("c2", True)]
    assert "cut off" in sent[2].results[0].content


async def test_cut_off_text_with_no_calls_ends_the_run_as_truncated() -> None:
    model = AsyncFakeModel(agent=[(say("The answer is"), "length")])
    a = agent(model)
    result = await a.run("Explain.")
    assert (result.outcome, result.stop, result.text) == ("failed", "truncated", "The answer is")
    assert kinds(await events_of(a, "id1"))[-2:] == ["model_turn", "run_finished"]
    assert len(model.turn_calls) == 1


# --- §5 rule 2: terminal results ---


async def test_a_batch_of_only_terminal_results_ends_the_run() -> None:
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "submit", {"answer": 42}), text="Submitting.")])
    a = agent(model, tools.tool("submit", "accepted", terminal=True))

    result = await a.run("Solve it.")

    assert (result.outcome, result.stop, result.text) == (
        "completed",
        "terminal_tool",
        "Submitting.",
    )
    events = await events_of(a, "id1")
    assert kinds(events)[-3:] == ["tool_started", "tool_result", "run_finished"]
    assert isinstance(events[-2], ToolResultEvent) and events[-2].terminal is True
    assert len(model.turn_calls) == 1  # no further turn was asked for


async def test_a_terminal_result_next_to_a_plain_one_does_not_end_the_run() -> None:
    tools = Recording()
    both = call(("c1", "submit", {}), ("c2", "get_time", {}))
    model = AsyncFakeModel(agent=[both, say("Done.")])
    a = agent(
        model, tools.tool("submit", "accepted", terminal=True), tools.tool("get_time", "12:00")
    )

    result = await a.run("Solve it.")

    assert (result.stop, result.text) == ("end_turn", "Done.")
    assert tools.ran == [
        ("submit", {}),
        ("get_time", {}),
    ]  # both ran: one did not swallow the other
    sent = model.turn_calls[1].messages[-1]
    assert sent == ToolResults((ToolResult("c1", "accepted"), ToolResult("c2", "12:00")))


async def test_a_terminal_tool_that_fails_does_not_end_the_run() -> None:
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "submit", {})), say("It failed.")])
    a = agent(model, tools.tool("submit", RuntimeError("rejected"), terminal=True))

    result = await a.run("Solve it.")

    assert (result.stop, result.text) == ("end_turn", "It failed.")
    failed = (await events_of(a, "id1"))[5]
    assert isinstance(failed, ToolResultEvent)
    assert (failed.is_error, failed.terminal, failed.executed) == (True, False, True)


# --- §5 rule 7: a tool's failure is a result, not the run's ---


async def test_a_tool_exception_goes_back_to_the_model_and_the_run_continues() -> None:
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "get_time", {})), say("The clock is broken.")])
    a = agent(model, tools.tool("get_time", ValueError("no clock")))

    result = await a.run("Time?")

    assert (result.outcome, result.text) == ("completed", "The clock is broken.")
    event = (await events_of(a, "id1"))[5]
    assert event == ToolResultEvent(
        seq=6,
        call_id="c1",
        content="ValueError: no clock",
        is_error=True,
        error_kind="exception",
        executed=True,
        original_chars=20,
        **ENV,
    )
    assert model.turn_calls[1].messages[-1] == ToolResults(
        (ToolResult("c1", "ValueError: no clock", is_error=True),)
    )


async def test_an_unknown_tool_or_unparsed_arguments_is_answered_without_running_anything() -> None:
    tools = Recording()
    bad = AssistantTurn(
        (ToolCall("c1", "no_such_tool", {}), ToolCall("c2", "get_time", None, "{oops"))
    )
    model = AsyncFakeModel(agent=[bad, say("Sorry.")])
    a = agent(model, tools.tool("get_time", "12:00"))

    await a.run("Time?")

    events = await events_of(a, "id1")
    assert kinds(events)[3:6] == ["model_turn", "tool_result", "tool_result"]  # no tool_started
    refused = [e for e in events[4:6] if isinstance(e, ToolResultEvent)]
    assert [(e.call_id, e.error_kind, e.executed, e.is_error) for e in refused] == [
        ("c1", "invalid_args", False, True),
        ("c2", "invalid_args", False, True),
    ]
    assert "no_such_tool" in refused[0].content and "JSON" in refused[1].content
    assert tools.ran == []


# --- §5 rule 6: budgets and usage ---


async def test_the_turn_budget_stops_before_the_call_that_would_exceed_it() -> None:
    tools = Recording()
    again = call(("c1", "get_time", {}))
    model = AsyncFakeModel(agent=[again, again, again])  # would loop for ever
    a = agent(model, tools.tool("get_time", "12:00"), budget=Budget(turns=2))

    result = await a.run("Time?")

    assert (result.outcome, result.stop, result.text) == ("failed", "budget_turns", None)
    assert len(model.turn_calls) == 2  # the third call was never made
    last = (await events_of(a, "id1"))[-1]
    assert isinstance(last, RunFinished) and (last.outcome, last.stop) == ("failed", "budget_turns")


async def test_the_turn_budget_is_the_sessions_not_the_runs() -> None:
    model = AsyncFakeModel(agent=[say("One."), say("Two.")])
    a = agent(model, budget=Budget(turns=2))
    first = await a.run("One?")
    await a.run("Two?", session_id=first.session_id)
    third = await a.run("Three?", session_id=first.session_id)
    assert (third.outcome, third.stop) == ("failed", "budget_turns")
    assert len(model.turn_calls) == 2


def test_a_budget_must_allow_at_least_one_turn() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Budget(turns=0)


async def test_usage_is_summed_over_the_runs_calls_and_recorded() -> None:
    tools = Recording()
    model = Billed(
        [
            Usage(input=100, output=10, reasoning=4, cost_usd=0.01),
            Usage(input=130, output=5, cost_usd=0.02),
        ],
        agent=[call(("c1", "get_time", {})), say("Noon.")],
    )
    a = agent(model, tools.tool("get_time", "12:00"))

    result = await a.run("Time?")

    assert (result.usage.input, result.usage.output, result.usage.reasoning) == (230, 15, 4)
    assert result.usage.cost_usd == pytest.approx(0.03)
    events = await events_of(a, "id1")
    turns = [e for e in events if isinstance(e, ModelTurn)]
    assert [(t.usage.input, t.usage.output) for t in turns] == [(100, 10), (130, 5)]
    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert (finished.usage.input, finished.usage.output, finished.usage.reasoning) == (230, 15, 4)


async def test_a_failed_call_that_was_billed_counts_and_one_that_never_answered_does_not() -> None:
    billed = Billed(
        [Usage(input=100, output=10)],
        agent=[
            call(("c1", "get_time", {})),
            EmptyReply("agent: empty reply", usage=Usage(input=120, output=0)),
        ],
    )
    a = agent(billed, Recording().tool("get_time", "12:00"))
    result = await a.run("Time?")
    assert result.usage == Usage(input=220, output=10)
    error = (await events_of(a, "id1"))[-2]
    assert isinstance(error, ModelErrorEvent)
    assert (error.error, error.usage) == ("EmptyReply", UsageData(input=120, output=0))

    b = agent(AsyncFakeModel(agent=[CallFailed("agent failed: TimeoutError")]))
    failed = await b.run("Time?")
    assert failed.usage == Usage()  # unknown, not zero and not a guess
    recorded = (await events_of(b, "id1"))[-2]
    assert isinstance(recorded, ModelErrorEvent) and recorded.usage is None


async def test_a_turn_that_repeats_a_call_id_is_a_model_failure_and_nothing_runs() -> None:
    # Codex review 2026-11-21: results are matched to calls by id, so two calls with one id
    # could not both be answered.
    tools = Recording()
    twice = call(("c1", "get_time", {}), ("c1", "get_time", {"tz": "UTC"}))
    model = AsyncFakeModel(agent=[twice, say("never asked")])
    a = agent(model, tools.tool("get_time", "12:00"))

    result = await a.run("Time?")

    assert (result.outcome, result.stop) == ("failed", "error")
    assert result.error == RunError("model", "DuplicateCallIds")
    assert kinds(await events_of(a, "id1"))[3:] == ["model_error", "run_finished"]
    assert tools.ran == [] and len(model.turn_calls) == 1


# --- §5 rule 9: a model failure ---


async def test_a_failed_model_call_ends_the_run_as_failed_and_is_in_the_log() -> None:
    model = AsyncFakeModel(agent=[CallFailed("agent ollama:qwen3 failed: TimeoutError")])
    a = agent(model)

    result = await a.run("Time?")

    assert (result.outcome, result.stop, result.text) == ("failed", "error", None)
    assert result.error == RunError("model", "CallFailed")
    assert (await events_of(a, "id1"))[3:] == [
        ModelErrorEvent(seq=4, error="CallFailed", role="agent", usage=None, **ENV),
        RunFinished(seq=5, outcome="failed", stop="error", usage=NO_USAGE, **ENV),
    ]


async def test_running_again_after_a_failure_resends_the_turn_without_rerunning_tools() -> None:
    tools = Recording()
    first = call(("c1", "get_time", {}))
    model = AsyncFakeModel(agent=[first, CallFailed("agent failed: TimeoutError"), say("Noon.")])
    a = agent(model, tools.tool("get_time", "12:00"))

    failed = await a.run("Time?")
    assert failed.outcome == "failed" and tools.ran == [("get_time", {})]

    again = await a.run("Please continue.", session_id=failed.session_id)

    assert (again.outcome, again.text) == ("completed", "Noon.")
    assert tools.ran == [("get_time", {})]  # the earlier call was not executed again
    assert model.turn_calls[2].messages == (
        Message("user", "Time?"),
        first,
        ToolResults((ToolResult("c1", "12:00"),)),
        Message("user", "Please continue."),
    )


async def test_an_error_that_is_not_the_models_is_not_swallowed() -> None:
    a = agent(AsyncFakeModel(agent=[]))  # the fake raises AssertionError: a bug in the test setup
    with pytest.raises(AssertionError, match="ran out of replies"):
        await a.run("Time?")


# --- §4: the store fails, or does not hold what the run wrote ---


class FailingAt(InMemorySessionStore):
    """Refuses the append that would write the given seq."""

    def __init__(self, seq: int) -> None:
        super().__init__()
        self.fail_at = seq

    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        if any(e.seq == self.fail_at for e in events):
            raise OSError("disk full")
        await super().append(session_id, events, expected_seq=expected_seq)


async def test_a_reply_the_store_cannot_take_fails_the_run_and_leaves_the_log_as_it_was() -> None:
    model = AsyncFakeModel(agent=[say("Noon.")])
    a = agent(model, sessions=FailingAt(seq=4))  # the model_turn

    result = await a.run("Time?")

    assert (result.outcome, result.stop) == ("failed", "error")
    assert result.error == RunError("store", "OSError")
    assert kinds(await events_of(a, "id1")) == ["session_started", "run_started", "user_message"]
    assert len(model.turn_calls) == 1  # the reply was received, paid for, and lost


async def test_a_store_that_fails_on_the_first_write_fails_the_run_with_no_session() -> None:
    a = agent(AsyncFakeModel(agent=[say("x")]), sessions=FailingAt(seq=1))
    result = await a.run("Time?")
    assert result.error == RunError("store", "OSError")
    with pytest.raises(UnknownSession):
        await a.sessions.read("id1")


class Unreadable(InMemorySessionStore):
    async def read(self, session_id: str, *, upto_seq: int | None = None) -> Sequence[Event]:
        raise ConnectionError("database is away")


async def test_a_store_that_cannot_be_read_fails_the_run_at_any_point() -> None:
    # simplify review 2026-11-21: the first read used to escape as a raw exception
    store = Unreadable()
    await InMemorySessionStore.append(
        store, "s1", [SessionStarted(session_id="s1", seq=1, time=T0, run_id="r0")], expected_seq=0
    )
    model = AsyncFakeModel(agent=[say("x")])
    resumed = await agent(model, sessions=store).run("Hi", session_id="s1")
    fresh = await agent(model, sessions=Unreadable()).run("Hi")
    for result in (resumed, fresh):
        assert (result.outcome, result.stop) == ("failed", "error")
        assert result.error == RunError("store", "ConnectionError")
    assert model.turn_calls == []


@pytest.mark.parametrize(
    ("fail_at", "left_open"),
    [(5, "tool_started was never written"), (6, "the tool ran but its result was lost")],
)
async def test_a_session_left_with_an_unanswered_call_is_not_sent_to_the_model_again(
    fail_at: int, left_open: str
) -> None:
    # Codex review 2026-11-21: an assistant turn whose calls have no results is a request the
    # APIs reject, and a tool that may already have run must not be repeated blindly.
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "pay", {"amount": 5})), say("Paid.")])
    store = FailingOnce(seq=fail_at)
    a = agent(model, tools.tool("pay", "receipt 1"), sessions=store)

    broken = await a.run("Pay the invoice.")
    assert broken.error == RunError("store", "OSError"), left_open
    ran_before = list(tools.ran)

    with pytest.raises(UnresolvedCalls, match="c1") as caught:
        await a.run("Carry on.", session_id=broken.session_id)

    assert caught.value.call_ids == ("c1",)
    assert tools.ran == ran_before  # nothing was executed a second time
    assert len(model.turn_calls) == 1  # and no request was built on the broken history
    # the refused run wrote nothing
    assert kinds(await events_of(a, "id1"))[-1] in {"model_turn", "tool_started"}


class Forgetful(InMemorySessionStore):
    """Accepts every append and silently drops tool results."""

    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None:
        kept = [e for e in events if not isinstance(e, ToolResultEvent)]
        self._events.setdefault(session_id, []).extend(kept)


async def test_a_store_that_lost_an_event_stops_the_run_before_the_next_request() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "get_time", {})), say("Noon.")])
    a = agent(model, Recording().tool("get_time", "12:00"), sessions=Forgetful())

    result = await a.run("Time?")

    assert (result.outcome, result.stop) == ("failed", "error")
    assert result.error == RunError("harness", "HarnessError")
    assert len(model.turn_calls) == 1  # the request built on the damaged log was never sent


# --- opaque blocks through a whole run ---


async def test_a_thinking_block_goes_back_unchanged_within_a_session() -> None:
    thought = AssistantTurn((Opaque("anthropic", THINKING), ToolCall("c1", "get_time", {})))
    model = AsyncFakeModel(agent=[thought, say("Noon."), say("Still.")])
    a = agent(model, Recording().tool("get_time", "12:00"))

    first = await a.run("Time?")
    assert model.turn_calls[1].messages[1] == thought

    await a.run("And now?", session_id=first.session_id)  # the same snapshot
    assert model.turn_calls[2].messages[1] == thought


async def test_a_changed_system_prompt_drops_the_earlier_thinking_from_the_request() -> None:
    thought = AssistantTurn((Opaque("anthropic", THINKING), Text("Noon.")))
    model = AsyncFakeModel(agent=[thought, say("Formally noon.")])
    store = InMemorySessionStore()
    first = await agent(model, sessions=store, system="Be brief.").run("Time?")

    formal = agent(model, sessions=store, system="Be formal.", ids=lambda: "run2")
    await formal.run("Again?", session_id=first.session_id)

    assert model.turn_calls[1].messages == (
        Message("system", "Be formal."),
        Message("user", "Time?"),
        AssistantTurn((Text("Noon."),)),
        Message("user", "Again?"),
    )
