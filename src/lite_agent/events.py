"""The event vocabulary (design §4). The log is append-only and the source of truth; every
event is validated when it is built, and survives a JSON round trip unchanged so a store can
persist it. S1 writes the eight types below; later stages add theirs."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Self, get_args

from lite_model import AssistantTurn, Opaque, Provenance, Text, ToolCall, Usage
from lite_model import Block as ModelBlock
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from pydantic.dataclasses import dataclass as record

from lite_agent.errors import EventRejected

V = 1  # the vocabulary version this code reads and writes

Outcome = Literal["completed", "failed", "paused"]
StopReason = Literal[
    "end_turn",
    "terminal_tool",
    "budget_turns",
    "budget_tokens",
    "budget_cost",
    "truncated",
    "incomplete",
    "paused",
    "error",
]
ErrorKind = Literal[
    "invalid_args", "denied", "exception", "timeout", "truncated_turn", "outcome_unknown"
]
PauseReason = Literal["approval", "tool", "reconcile"]
# executed: this harness ran it.  external: the host ran it.  reconciled: it was interrupted and
# the host checked what became of it.
Origin = Literal["executed", "external", "reconciled"]


@record(frozen=True)
class Waiting:
    """One call a paused run waits on: in the `paused` event and in `RunResult.pause`."""

    call_id: str
    # approval: a gate wants a person's yes or no.  tool: the host executes it and reports the
    # result.  reconcile: it was started and its outcome is unknown (design §8).
    reason: PauseReason
    note: str = ""  # the gate's reason, for the person who decides
    gate: str = ""  # the gate that asked, by name


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- what events carry ---


class ToolDef(_Frozen):
    """A tool as it was offered to the model."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class TextBlock(_Frozen):
    kind: Literal["text"] = "text"
    text: str


class ToolCallBlock(_Frozen):
    kind: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] | None  # None: the model's JSON did not parse (lite-model §3.1)
    raw_arguments: str | None = None


class OpaqueBlock(_Frozen):
    """Content only its own protocol reads (an Anthropic thinking block), kept as received."""

    kind: Literal["opaque"] = "opaque"
    protocol: Literal["openai", "anthropic"]
    data: dict[str, Any]


EventBlock = Annotated[TextBlock | ToolCallBlock | OpaqueBlock, Field(discriminator="kind")]


# The literals below repeat lite-model's `Channel` and `Stop` on purpose: the stored vocabulary is
# versioned here (`V`), so a value lite-model adds later is refused until this file takes it in.


class ProvenanceData(_Frozen):
    role: str
    backend: str
    requested: str
    answered: str | None
    channel: Literal["local", "api", "subscription"]
    routed: bool
    via: dict[str, str] = Field(default_factory=dict)


class UsageData(_Frozen):
    input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cost_usd: float | None = None  # None = unknown; never estimated


# --- the envelope and the events ---


class _Event(_Frozen):
    session_id: str
    seq: int = Field(ge=1)
    time: datetime
    run_id: str
    v: int = Field(default=V, ge=1, le=V)


class SessionStarted(_Event):
    """A session's first event. Its `run_id` is the run that opened the session."""

    type: Literal["session_started"] = "session_started"


class RunStarted(_Event):
    """Opens a run and holds its snapshot: what every request of this run is built from."""

    type: Literal["run_started"] = "run_started"
    role: str
    fallback_role: str | None = None
    system: str
    tools: tuple[ToolDef, ...]


class UserMessage(_Event):
    type: Literal["user_message"] = "user_message"
    text: str
    # task: what the caller asked.  auto_continue: the harness, after a text-only or cut-off
    # turn, saying what is still open (design §5 rule 3).
    origin: Literal["task", "auto_continue"] = "task"


class ModelTurn(_Event):
    type: Literal["model_turn"] = "model_turn"
    blocks: tuple[EventBlock, ...]  # in the endpoint's order
    stop: Literal["end", "tool_calls", "length"]
    role: str
    provenance: ProvenanceData
    usage: UsageData


class ToolStarted(_Event):
    """Written before a tool runs: a call with this and no result has an unknown outcome."""

    type: Literal["tool_started"] = "tool_started"
    call_id: str
    name: str
    arguments: dict[str, Any]
    external: bool = False  # the host executes it; its result arrives on resume


class ToolResultEvent(_Event):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False
    error_kind: ErrorKind | None = None
    executed: bool  # was the tool called at all
    origin: Origin = "executed"
    terminal: bool = False
    original_chars: int = Field(ge=0)

    @model_validator(mode="after")
    def _one_story(self) -> Self:
        """The fields may not contradict each other, whoever builds the event (§4)."""
        if (self.error_kind is not None) != self.is_error:
            raise ValueError("error_kind is set exactly when is_error is")
        if self.terminal and (self.is_error or not self.executed):
            raise ValueError("only a result that was executed without error can be terminal")
        return self


class Paused(_Event):
    """The run stops here and waits: per call, why, and the gate's note for the person."""

    type: Literal["paused"] = "paused"
    waiting: tuple[Waiting, ...]


class Resumed(_Event):
    """What `resume()` was given: decisions per call id, and the ids results were brought for."""

    type: Literal["resumed"] = "resumed"
    approve: dict[str, bool] = Field(default_factory=dict)
    results: tuple[str, ...] = ()


class ModelErrorEvent(_Event):
    type: Literal["model_error"] = "model_error"
    error: str  # the exception's type name; never upstream text
    role: str
    usage: UsageData | None  # None: nothing came back, so nothing is known


class ModelSwitched(_Event):
    """From here to the end of this run the calls go to another role (design §5 rule 11)."""

    type: Literal["model_switched"] = "model_switched"
    from_role: str
    to_role: str
    reason: str  # the error that caused it, by type name


class RunFinished(_Event):
    type: Literal["run_finished"] = "run_finished"
    outcome: Outcome
    stop: StopReason
    usage: UsageData  # this run's model calls, summed


Event = Annotated[
    SessionStarted
    | RunStarted
    | UserMessage
    | ModelTurn
    | ToolStarted
    | ToolResultEvent
    | Paused
    | Resumed
    | ModelErrorEvent
    | ModelSwitched
    | RunFinished,
    Field(discriminator="type"),
]
EVENT_TYPES: tuple[type, ...] = get_args(get_args(Event)[0])  # the union's members, once
_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def dump(event: Event) -> dict[str, Any]:
    """The event as JSON-ready data."""
    return event.model_dump(mode="json")


def load(data: Mapping[str, Any]) -> Event:
    """The event `dump()` wrote. An unknown type or a newer vocabulary is refused, not guessed."""
    version = data.get("v")
    if type(version) is not int or not 1 <= version <= V:  # `True` is an int to isinstance
        raise EventRejected(f"event vocabulary version {version!r} is not supported (this is {V})")
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise EventRejected(f"invalid event: {exc.error_count()} validation error(s)") from exc


# --- lite-model's types to and from event data ---


def blocks_of(turn: AssistantTurn) -> tuple[EventBlock, ...]:
    blocks: list[EventBlock] = []
    for b in turn.blocks:
        if isinstance(b, Text):
            blocks.append(TextBlock(text=b.text))
        elif isinstance(b, ToolCall):
            arguments = dict(b.arguments) if b.arguments is not None else None
            blocks.append(
                ToolCallBlock(
                    id=b.id, name=b.name, arguments=arguments, raw_arguments=b.raw_arguments
                )
            )
        else:
            blocks.append(OpaqueBlock(protocol=b.protocol, data=dict(b.data)))
    return tuple(blocks)


def turn_of(blocks: Sequence[EventBlock], *, opaque: bool) -> AssistantTurn:
    """The turn as lite-model takes it back; `opaque=False` leaves the opaque blocks out."""
    out: list[ModelBlock] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            out.append(Text(b.text))
        elif isinstance(b, ToolCallBlock):
            out.append(ToolCall(b.id, b.name, b.arguments, b.raw_arguments))
        elif opaque:
            out.append(Opaque(b.protocol, b.data))
    return AssistantTurn(tuple(out))


def provenance_data(p: Provenance) -> ProvenanceData:
    return ProvenanceData.model_validate(p, from_attributes=True)


def usage_data(u: Usage) -> UsageData:
    return UsageData.model_validate(u, from_attributes=True)
