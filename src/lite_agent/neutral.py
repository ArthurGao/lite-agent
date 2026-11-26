"""lite-model's history as lite-context's neutral messages (design §6, C7): what the compactor
renders for the summariser. One way and lossy on purpose (thinking left out, an empty turn as an
empty text, no system messages), so it is not a general lite-model adapter: the log stays the
source of truth, and nothing is mapped back."""

import json
from collections.abc import Sequence

from lite_context import Message as Neutral
from lite_context import Text as NText
from lite_context import ToolCall as NCall
from lite_context import ToolResult as NResult
from lite_model import AssistantTurn, Message, Text, ToolCall, ToolResults, TurnMessage


def arguments(call: ToolCall) -> str:
    """A call's arguments as text: as parsed, or as received when they never parsed."""
    if call.arguments is None:
        return call.raw_arguments or ""
    return json.dumps(call.arguments)


def to_neutral(history: Sequence[TurnMessage]) -> list[Neutral]:
    out: list[Neutral] = []
    for m in history:
        if isinstance(m, Message):
            if (
                m.role == "system"
            ):  # the run's snapshot, never history (lite-context refuses it too)
                raise ValueError("a system message is not part of the history")
            out.append(Neutral(m.role, (NText(m.content),)))
        elif isinstance(m, AssistantTurn):
            # In the order the model said them; opaque blocks (thinking) are not summarised.
            parts = tuple(
                NText(b.text) if isinstance(b, Text) else NCall(b.id, b.name, arguments(b))
                for b in m.blocks
                if isinstance(b, Text | ToolCall)
            )
            out.append(Neutral("assistant", parts or (NText(""),)))  # an empty part, as before
        elif isinstance(m, ToolResults):
            results = tuple(
                NResult(r.call_id, (NText(r.content),), is_error=r.is_error) for r in m.results
            )
            out.append(Neutral("user", results))
    return out
