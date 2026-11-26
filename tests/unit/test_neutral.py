"""lite-model's history as lite-context's neutral messages (C7): what the summariser is rendered
from. One way only; lite-agent's log stays the source of truth."""

from lite_context import Message as Neutral
from lite_context import Text as NText
from lite_context import ToolCall as NCall
from lite_context import ToolResult as NResult
from lite_model import (
    AssistantTurn,
    Message,
    Opaque,
    Text,
    ToolCall,
    ToolResult,
    ToolResults,
    TurnMessage,
)

from lite_agent.neutral import to_neutral


def test_each_kind_of_history_item_maps_to_one_neutral_message() -> None:
    history: list[TurnMessage] = [
        Message("user", "Where?"),
        AssistantTurn(
            (
                Text("Looking."),
                ToolCall("c1", "find", {"q": "x"}),
                ToolCall("c2", "g", None, "{bad"),
            )
        ),
        ToolResults((ToolResult("c1", "here"), ToolResult("c2", "oops", True))),
    ]
    assert to_neutral(history) == [
        Neutral("user", (NText("Where?"),)),
        Neutral(
            "assistant",
            (NText("Looking."), NCall("c1", "find", '{"q": "x"}'), NCall("c2", "g", "{bad")),
        ),
        Neutral(
            "user",
            (
                NResult("c1", (NText("here"),), is_error=False),
                NResult("c2", (NText("oops"),), is_error=True),
            ),
        ),
    ]


def test_opaque_blocks_are_left_out_and_a_turn_with_nothing_else_says_nothing() -> None:
    thinking = Opaque("anthropic", {"type": "thinking"})
    assert to_neutral([AssistantTurn((thinking, Text("Done.")))]) == [
        Neutral("assistant", (NText("Done."),))
    ]
    # The summariser was shown an empty assistant part for it, and still is.
    assert to_neutral([AssistantTurn((thinking,))]) == [Neutral("assistant", (NText(""),))]


def test_unparsed_arguments_with_nothing_received_are_empty() -> None:
    turn = AssistantTurn((ToolCall("c9", "x", None, None),))
    assert to_neutral([turn]) == [Neutral("assistant", (NCall("c9", "x", ""),))]
