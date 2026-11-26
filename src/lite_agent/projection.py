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
    ToolResultsMasked,
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


@dataclass(frozen=True)
class Shown:
    """One message of the history, with what it was made from: `key` names it for lite-context
    (`e<seq>` of its event, or `summary`), `results` the seqs of the `tool_result` events a
    `ToolResults` holds, in its order."""

    message: TurnMessage
    key: str
    results: tuple[int, ...] = ()


def masks(events: Sequence[Event]) -> dict[int, str]:
    """What each masked tool result is shown as, by its seq (every `tool_results_masked`)."""
    return {m.seq: m.shown for e in events if isinstance(e, ToolResultsMasked) for m in e.masked}


def keyed_history(events: Sequence[Event], *, masked: bool = True) -> list[Shown]:
    """The conversation as the model is shown it, without the system prompt. The latest
    compaction's summary stands for every event it covers (a later compaction takes in an
    earlier one's summary, so only the latest counts); every result of one turn's calls becomes
    one `ToolResults`, in the calls' order. A result a `tool_results_masked` event names shows
    the text that event recorded, unless `masked` is False (what the masker measures)."""
    opaque_from = _opaque_from(events)
    summary = latest_compaction(events)
    covered = summary.covers[1] if summary is not None else 0
    replaced = masks(events) if masked else {}
    shown: list[Shown] = (
        [Shown(Message("user", summary.summary), "summary")] if summary is not None else []
    )
    calls: list[str] = []  # the open turn's call ids, in order
    results: dict[str, tuple[ToolResult, int]] = {}

    def close_turn() -> None:
        if results:
            kept = [results[c] for c in calls if c in results]
            seqs = tuple(seq for _, seq in kept)
            # keyed by its first result; results that answer none of the turn's calls still
            # close it with an empty batch, as before V=2
            first = seqs[0] if seqs else min(seq for _, seq in results.values())
            shown.append(Shown(ToolResults(tuple(r for r, _ in kept)), f"e{first}", seqs))
        calls.clear()
        results.clear()

    for e in events:
        if e.seq <= covered:
            continue
        if isinstance(e, UserMessage):
            close_turn()
            shown.append(Shown(Message("user", e.text), f"e{e.seq}"))
        elif isinstance(e, ModelTurn):
            close_turn()
            shown.append(Shown(turn_of(e.blocks, opaque=e.seq >= opaque_from), f"e{e.seq}"))
            calls += [b.id for b in e.blocks if isinstance(b, ToolCallBlock)]
        elif isinstance(e, ToolResultEvent):
            content = replaced.get(e.seq, e.content)
            results[e.call_id] = (ToolResult(e.call_id, content, e.is_error), e.seq)
    close_turn()
    return shown


def history(events: Sequence[Event]) -> tuple[TurnMessage, ...]:
    """The conversation as the model is shown it, without the system prompt (`keyed_history`)."""
    return tuple(s.message for s in keyed_history(events))


def derive(events: Sequence[Event]) -> Projection:
    """The request a run would send next: the latest run's system prompt and tools, and the
    history."""
    snapshot = next((e for e in reversed(events) if isinstance(e, RunStarted)), None)
    if snapshot is None:
        return Projection(history(events), ())
    system = [Message("system", snapshot.system)] if snapshot.system else []
    tools = tuple(ToolSpec(t.name, t.description, t.parameters) for t in snapshot.tools)
    return Projection((*system, *history(events)), tools)
