"""What the model sees, derived from the log (design §4): a pure function of the events.
Nothing here adds text; whatever is model-visible is already in an event."""

from collections.abc import Sequence
from dataclasses import dataclass

from lite_model import Message, ToolResult, ToolResults, ToolSpec, TurnMessage

from lite_agent.events import (
    Event,
    ModelTurn,
    RunStarted,
    ToolCallBlock,
    ToolResultEvent,
    UserMessage,
    turn_of,
)


@dataclass(frozen=True)
class Projection:
    messages: tuple[TurnMessage, ...]  # the system prompt first, when there is one
    tools: tuple[ToolSpec, ...]


def _opaque_from(events: Sequence[Event]) -> int:
    """The seq from which opaque blocks may be replayed. A thinking block is bound to the system
    prompt and tools it was produced under (design §4): once a run starts with a different
    snapshot, earlier ones would be rejected, so they are left out."""
    boundary, previous = 0, None
    for e in events:
        if isinstance(e, RunStarted):
            snapshot = (e.system, e.tools)
            if previous is not None and snapshot != previous:
                boundary = e.seq
            previous = snapshot
    return boundary


def unanswered_calls(events: Sequence[Event]) -> tuple[str, ...]:
    """The last turn's tool calls that have no result yet, in order. Not empty only while a
    turn's calls are being executed, or after a run was interrupted there (design §8)."""
    last = next((e for e in reversed(events) if isinstance(e, ModelTurn)), None)
    if last is None:
        return ()
    answered = {e.call_id for e in events if isinstance(e, ToolResultEvent) and e.seq > last.seq}
    calls = (b.id for b in last.blocks if isinstance(b, ToolCallBlock))
    return tuple(c for c in calls if c not in answered)


def derive(events: Sequence[Event]) -> Projection:
    """The request a run would send next: the latest run's system prompt and tools, and the
    history. Every result of one turn's calls becomes one `ToolResults`, in the calls' order."""
    opaque_from = _opaque_from(events)
    snapshot: RunStarted | None = None
    history: list[TurnMessage] = []
    calls: list[str] = []  # the open turn's call ids, in order
    results: dict[str, ToolResult] = {}

    def close_turn() -> None:
        if results:
            history.append(ToolResults(tuple(results[c] for c in calls if c in results)))
        calls.clear()
        results.clear()

    for e in events:
        if isinstance(e, RunStarted):
            snapshot = e
        elif isinstance(e, UserMessage):
            close_turn()
            history.append(Message("user", e.text))
        elif isinstance(e, ModelTurn):
            close_turn()
            history.append(turn_of(e.blocks, opaque=e.seq >= opaque_from))
            calls += [b.id for b in e.blocks if isinstance(b, ToolCallBlock)]
        elif isinstance(e, ToolResultEvent):
            results[e.call_id] = ToolResult(e.call_id, e.content, e.is_error)
    close_turn()

    if snapshot is None:
        return Projection(tuple(history), ())
    system = [Message("system", snapshot.system)] if snapshot.system else []
    tools = tuple(ToolSpec(t.name, t.description, t.parameters) for t in snapshot.tools)
    return Projection((*system, *history), tools)
