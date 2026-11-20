"""What the model sees, derived from the log (design §4): a pure function of the events.
Nothing here adds text; whatever is model-visible is already in an event."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from lite_model import Message, ToolCall, ToolResult, ToolResults, ToolSpec, TurnMessage

from lite_agent.events import (
    Event,
    ModelTurn,
    Paused,
    RunStarted,
    ToolCallBlock,
    ToolResultEvent,
    ToolStarted,
    UserMessage,
    Waiting,
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


@dataclass(frozen=True)
class LastTurn:
    """The session's last model turn and what has happened to its calls since. Call ids are only
    unique within a turn, so everything about a call is read from here."""

    calls: tuple[ToolCall, ...]
    text: str
    started: Mapping[str, ToolStarted] = field(hash=False)
    results: Mapping[str, ToolResultEvent] = field(hash=False)
    # calls a gate left waiting for a person, as the latest `paused` event recorded them: the
    # wait holds until that person answers, whatever gates a later agent has (§8)
    awaiting: Mapping[str, Waiting] = field(hash=False)

    @property
    def unanswered(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.calls if c.id not in self.results)


def last_turn(events: Sequence[Event]) -> LastTurn | None:
    """Walks back from the end to the last model turn: the cost is that turn's events."""
    started: dict[str, ToolStarted] = {}
    results: dict[str, ToolResultEvent] = {}
    awaiting: dict[str, Waiting] = {}
    for e in reversed(events):
        if isinstance(e, ToolStarted):
            started[e.call_id] = e
        elif isinstance(e, ToolResultEvent):
            results[e.call_id] = e
        elif isinstance(e, Paused):
            for w in e.waiting:
                if w.reason == "approval":
                    awaiting.setdefault(w.call_id, w)  # walking back: the latest one wins
        elif isinstance(e, ModelTurn):
            turn = turn_of(e.blocks, opaque=False)
            return LastTurn(turn.tool_calls, turn.text, started, results, awaiting)
    return None


def unanswered_calls(events: Sequence[Event]) -> tuple[str, ...]:
    """The last turn's tool calls that have no result yet, in order. Not empty only while a
    turn's calls are being settled, or after a run paused or was interrupted there (§8)."""
    turn = last_turn(events)
    return turn.unanswered if turn is not None else ()


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
