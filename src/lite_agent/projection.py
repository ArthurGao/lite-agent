"""What the model sees, derived from the log (design §4): a pure function of the events.
Nothing here adds text; whatever is model-visible is already in an event."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from lite_model import Message, ToolCall, ToolResult, ToolResults, ToolSpec, TurnMessage

from lite_agent.events import (
    Compaction,
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
    prompt, the tools and the messages it was produced after (design §4): once a run starts with
    a different snapshot, or the history before it is replaced by a summary, earlier ones would
    be rejected, so they are left out."""
    boundary, previous = 0, None
    for e in events:
        if isinstance(e, RunStarted):
            snapshot = (e.system, e.tools)
            if previous is not None and snapshot != previous:
                boundary = e.seq
            previous = snapshot
        elif isinstance(e, Compaction):
            boundary = e.seq
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


def latest_compaction(events: Sequence[Event]) -> Compaction | None:
    """The one compaction that counts: each covers the history from its start, the earlier
    summary included, so the latest stands for everything before its upper bound."""
    return next((e for e in reversed(events) if isinstance(e, Compaction)), None)


def history(events: Sequence[Event]) -> tuple[TurnMessage, ...]:
    """The conversation as the model is shown it, without the system prompt. The latest
    compaction's summary stands for every event it covers (a later compaction takes in an
    earlier one's summary, so only the latest counts); every result of one turn's calls becomes
    one `ToolResults`, in the calls' order."""
    opaque_from = _opaque_from(events)
    summary = latest_compaction(events)
    covered = summary.covers[1] if summary is not None else 0
    shown: list[TurnMessage] = [Message("user", summary.summary)] if summary is not None else []
    calls: list[str] = []  # the open turn's call ids, in order
    results: dict[str, ToolResult] = {}

    def close_turn() -> None:
        if results:
            shown.append(ToolResults(tuple(results[c] for c in calls if c in results)))
        calls.clear()
        results.clear()

    for e in events:
        if e.seq <= covered:
            continue
        if isinstance(e, UserMessage):
            close_turn()
            shown.append(Message("user", e.text))
        elif isinstance(e, ModelTurn):
            close_turn()
            shown.append(turn_of(e.blocks, opaque=e.seq >= opaque_from))
            calls += [b.id for b in e.blocks if isinstance(b, ToolCallBlock)]
        elif isinstance(e, ToolResultEvent):
            results[e.call_id] = ToolResult(e.call_id, e.content, e.is_error)
    close_turn()
    return tuple(shown)


def derive(events: Sequence[Event]) -> Projection:
    """The request a run would send next: the latest run's system prompt and tools, and the
    history."""
    snapshot = next((e for e in reversed(events) if isinstance(e, RunStarted)), None)
    if snapshot is None:
        return Projection(history(events), ())
    system = [Message("system", snapshot.system)] if snapshot.system else []
    tools = tuple(ToolSpec(t.name, t.description, t.parameters) for t in snapshot.tools)
    return Projection((*system, *history(events)), tools)
