"""Design §4: events are validated when built, survive a JSON round trip unchanged, and an
unknown type or a newer vocabulary is refused."""

import json
from typing import Any

import pytest
from lite_model import AssistantTurn, Opaque, Text, ToolCall
from pydantic import ValidationError

from lite_agent import EventRejected
from lite_agent.events import (
    EVENT_TYPES,
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
    V,
    blocks_of,
    dump,
    load,
    turn_of,
)
from tests.helpers import T0, THINKING

ENVELOPE: dict[str, Any] = {"session_id": "s1", "time": T0, "run_id": "r1"}
WHO = ProvenanceData(
    role="agent",
    backend="anthropic",
    requested="claude-x",
    answered="claude-x-20261101",
    channel="api",
    routed=False,
    via={"x-request": "1"},
)
TURN = AssistantTurn(
    (
        Opaque("anthropic", THINKING),
        Text("Checking."),
        ToolCall("c1", "get_weather", {"city": "Paris"}),
        ToolCall("c2", "get_time", None, '{"tz": "U'),
    )
)
ONE_OF_EACH: list[Event] = [
    SessionStarted(seq=1, **ENVELOPE),
    RunStarted(
        seq=2,
        role="agent",
        system="Be brief.",
        tools=(ToolDef(name="get_time", description="Time.", parameters={"type": "object"}),),
        **ENVELOPE,
    ),
    UserMessage(seq=3, text="Time?", **ENVELOPE),
    ModelTurn(
        seq=4,
        blocks=blocks_of(TURN),
        stop="tool_calls",
        role="agent",
        provenance=WHO,
        usage=UsageData(input=10, output=4, cost_usd=0.002),
        **ENVELOPE,
    ),
    ToolStarted(seq=5, call_id="c1", name="get_weather", arguments={"city": "Paris"}, **ENVELOPE),
    ToolResultEvent(
        seq=6,
        call_id="c1",
        content="boom",
        is_error=True,
        error_kind="exception",
        executed=True,
        original_chars=4,
        **ENVELOPE,
    ),
    ModelErrorEvent(seq=7, error="CallFailed", role="agent", usage=None, **ENVELOPE),
    RunFinished(seq=8, outcome="failed", stop="error", usage=UsageData(), **ENVELOPE),
]


def test_the_examples_cover_the_whole_vocabulary() -> None:
    assert {type(e) for e in ONE_OF_EACH} == set(EVENT_TYPES)


@pytest.mark.parametrize("event", ONE_OF_EACH, ids=lambda e: e.type)
def test_every_event_survives_a_json_round_trip(event: Event) -> None:
    wire = json.dumps(dump(event))
    assert load(json.loads(wire)) == event


def test_blocks_round_trip_in_order_with_the_thinking_block_untouched() -> None:
    blocks = blocks_of(TURN)
    assert [b.kind for b in blocks] == ["opaque", "text", "tool_call", "tool_call"]
    assert turn_of(blocks, opaque=True) == TURN
    without = turn_of(blocks, opaque=False)
    assert without.blocks == TURN.blocks[1:]  # text and calls stay, in order


@pytest.mark.parametrize(
    "change",
    [
        {"type": "tool_exploded"},
        {"v": V + 1},
        {"v": 0},
        {"v": "1"},
        {"v": True},  # Codex review 2026-11-21: a bool is an int to isinstance
        {"surprise": 1},
        {"seq": 0},
    ],
    ids=["unknown_type", "newer", "zero", "version_str", "version_bool", "extra", "seq"],
)
def test_an_event_this_code_cannot_vouch_for_is_refused_on_load(change: dict[str, Any]) -> None:
    with pytest.raises(EventRejected):
        load(dump(ONE_OF_EACH[2]) | change)


def test_an_invalid_event_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        UserMessage(seq=0, text="x", **ENVELOPE)
    with pytest.raises(ValidationError):
        ToolResultEvent(seq=1, call_id="c", content="x", original_chars=1, **ENVELOPE)
    with pytest.raises(ValidationError):
        RunFinished(seq=1, outcome="finished", stop="error", usage=UsageData(), **ENVELOPE)


def test_an_event_is_immutable() -> None:
    with pytest.raises(ValidationError):
        ONE_OF_EACH[2].seq = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "fields",
    [
        {"is_error": False, "error_kind": "exception", "executed": True},
        {"is_error": True, "error_kind": None, "executed": True},
        {"is_error": True, "error_kind": "exception", "executed": True, "terminal": True},
        {"executed": False, "terminal": True},
    ],
    ids=["kind_without_error", "error_without_kind", "terminal_error", "terminal_unexecuted"],
)
def test_a_tool_result_whose_fields_contradict_each_other_cannot_be_built(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ToolResultEvent(seq=1, call_id="c", content="x", original_chars=1, **fields, **ENVELOPE)
