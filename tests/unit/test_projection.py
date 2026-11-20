"""Design §4: the request is a pure projection of the log. One `ToolResults` per turn, the
latest run's system prompt and tools, blocks in their order, and opaque blocks only while the
snapshot they were produced under still holds."""

from typing import Any

from lite_model import (
    AssistantTurn,
    Message,
    Opaque,
    Text,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
)
from lite_model.types import stop_for

from lite_agent import Projection, derive
from lite_agent.events import (
    Event,
    ModelErrorEvent,
    ModelTurn,
    ProvenanceData,
    RunFinished,
    RunStarted,
    SessionStarted,
    ToolDef,
    ToolResultEvent,
    ToolStarted,
    UsageData,
    UserMessage,
    blocks_of,
)
from lite_agent.projection import unanswered_calls
from tests.helpers import T0, THINKING

WHO = ProvenanceData(
    role="agent", backend="fake", requested="m", answered="m", channel="local", routed=False
)
CLOCK = ToolDef(name="get_time", description="Time.", parameters={"type": "object"})
MAP = ToolDef(name="get_map", description="Map.", parameters={"type": "object"})


class Log:
    """Builds a log with consecutive seqs."""

    def __init__(self) -> None:
        self.events: list[Event] = [SessionStarted(session_id="s", seq=1, time=T0, run_id="r1")]
        self.run = "r1"

    def add(self, cls: Any, **fields: Any) -> "Log":
        seq = len(self.events) + 1
        self.events.append(cls(session_id="s", seq=seq, time=T0, run_id=self.run, **fields))
        return self

    def started(self, system: str = "Be brief.", tools: tuple[ToolDef, ...] = (CLOCK,)) -> "Log":
        self.run = f"r{len(self.events)}"
        return self.add(RunStarted, role="agent", system=system, tools=tools)

    def user(self, text: str) -> "Log":
        return self.add(UserMessage, text=text)

    def turn(self, turn: AssistantTurn) -> "Log":
        stop = stop_for(turn)
        return self.add(
            ModelTurn,
            blocks=blocks_of(turn),
            stop=stop,
            role="agent",
            provenance=WHO,
            usage=UsageData(),
        )

    def result(self, call_id: str, content: str, *, is_error: bool = False) -> "Log":
        return self.add(
            ToolResultEvent,
            call_id=call_id,
            content=content,
            is_error=is_error,
            error_kind="exception" if is_error else None,
            executed=True,
            original_chars=len(content),
        )


TWO_CALLS = AssistantTurn(
    (Text("Checking."), ToolCall("c1", "get_time", {}), ToolCall("c2", "get_time", {"tz": "UTC"}))
)
THOUGHT = AssistantTurn(
    (Opaque("anthropic", THINKING), Text("Noon."), ToolCall("c9", "get_time", {}))
)


def test_an_empty_log_projects_to_nothing() -> None:
    assert derive([]) == Projection((), ())


def test_one_tool_round_projects_to_system_user_turn_and_one_results_message() -> None:
    log = Log().started().user("Time?").turn(TWO_CALLS)
    log.add(ToolStarted, call_id="c1", name="get_time", arguments={})
    log.result("c1", "12:00").add(ToolStarted, call_id="c2", name="get_time", arguments={})
    log.result("c2", "boom", is_error=True)

    assert derive(log.events) == Projection(
        messages=(
            Message("system", "Be brief."),
            Message("user", "Time?"),
            TWO_CALLS,  # blocks in the order the endpoint gave them
            ToolResults((ToolResult("c1", "12:00"), ToolResult("c2", "boom", is_error=True))),
        ),
        tools=(ToolSpec("get_time", "Time.", {"type": "object"}),),
    )


def test_results_follow_the_calls_order_whatever_order_they_were_written_in() -> None:
    log = Log().started().user("Time?").turn(TWO_CALLS).result("c2", "second").result("c1", "first")
    assert derive(log.events).messages[-1] == ToolResults(
        (ToolResult("c1", "first"), ToolResult("c2", "second"))
    )


def test_each_turn_gets_its_own_results_message() -> None:
    log = Log().started().user("Go.")
    log.turn(AssistantTurn((ToolCall("a", "get_time", {}),))).result("a", "1")
    log.turn(AssistantTurn((ToolCall("b", "get_time", {}),))).result("b", "2")
    messages = derive(log.events).messages
    assert [type(m).__name__ for m in messages] == [
        "Message",
        "Message",
        "AssistantTurn",
        "ToolResults",
        "AssistantTurn",
        "ToolResults",
    ]
    assert messages[3] == ToolResults((ToolResult("a", "1"),))
    assert messages[5] == ToolResults((ToolResult("b", "2"),))


def test_events_the_model_never_sees_add_nothing() -> None:
    log = Log().started().user("Time?")
    before = derive(log.events)
    log.add(ModelErrorEvent, error="CallFailed", role="agent", usage=None)
    log.add(RunFinished, outcome="failed", stop="error", usage=UsageData())
    assert derive(log.events) == before


def test_the_system_prompt_and_tools_are_the_latest_runs() -> None:
    log = Log().started("Old.", (CLOCK,)).user("One.").turn(AssistantTurn((Text("Done."),)))
    log.started("New.", (CLOCK, MAP)).user("Two.")
    projection = derive(log.events)
    assert projection.messages[0] == Message("system", "New.")
    assert [t.name for t in projection.tools] == ["get_time", "get_map"]
    assert projection.messages[1:] == (
        Message("user", "One."),
        AssistantTurn((Text("Done."),)),
        Message("user", "Two."),
    )


def test_no_system_prompt_sends_no_system_message() -> None:
    log = Log().started("").user("Hi.")
    assert derive(log.events).messages == (Message("user", "Hi."),)


def test_opaque_blocks_are_replayed_while_the_snapshot_is_unchanged() -> None:
    log = Log().started().user("One.").turn(THOUGHT).result("c9", "12:00")
    log.started().user("Two.")  # the same system prompt and tools
    assert derive(log.events).messages[2] == THOUGHT


def test_opaque_blocks_from_before_a_changed_snapshot_are_left_out() -> None:
    for system, tools in (("Be formal.", (CLOCK,)), ("Be brief.", (CLOCK, MAP))):
        log = Log().started().user("One.").turn(THOUGHT).result("c9", "12:00")
        log.started(system, tools).user("Two.").turn(THOUGHT).result("c9", "13:00")
        messages = derive(log.events).messages
        # the earlier turn keeps its text and its call, in order, without the thinking block
        assert messages[2] == AssistantTurn((Text("Noon."), ToolCall("c9", "get_time", {})))
        assert messages[3] == ToolResults((ToolResult("c9", "12:00"),))
        assert messages[5] == THOUGHT  # produced under the current snapshot: replayed


def test_going_back_to_an_earlier_snapshot_does_not_bring_its_opaque_blocks_back() -> None:
    # Conservative on purpose: after any change, every earlier opaque block stays out, even
    # one produced under a snapshot equal to the current one.
    log = Log().started("A").user("One.").turn(THOUGHT).result("c9", "1")
    log.started("B").user("Two.").turn(THOUGHT).result("c9", "2")
    log.started("A").user("Three.")
    turns = [m for m in derive(log.events).messages if isinstance(m, AssistantTurn)]
    assert turns == [AssistantTurn((Text("Noon."), ToolCall("c9", "get_time", {})))] * 2


def test_unanswered_calls_are_the_last_turns_calls_without_a_result() -> None:
    log = Log().started().user("Go.").turn(TWO_CALLS).result("c1", "12:00")
    assert unanswered_calls(log.events) == ("c2",)
    assert unanswered_calls(log.result("c2", "x").events) == ()
    assert unanswered_calls(Log().started().user("Go.").events) == ()


def test_derive_is_pure() -> None:
    log = Log().started().user("Time?").turn(TWO_CALLS).result("c1", "12:00")
    snapshot = list(log.events)
    assert derive(log.events) == derive(log.events)
    assert log.events == snapshot
