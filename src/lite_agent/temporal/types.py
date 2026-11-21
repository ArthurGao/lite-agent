"""What crosses the workflow / activity boundary (design §9): plain dataclasses, so Temporal's
default converter carries them and the workflow sandbox imports nothing but the standard library
for them."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResultData:
    """A result the host brings for a call it executed or checked (`ToolOutcome`)."""

    content: str
    is_error: bool = False
    unknown: bool = False


@dataclass
class Call:
    """One call a paused run waits on, with what the workflow needs to route it."""

    call_id: str
    reason: str  # approval | tool | reconcile (design §8)
    name: str  # the tool
    arguments: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    gate: str = ""


@dataclass
class Step:
    """How one run ended, as the activity reports it to the workflow."""

    session_id: str
    run_id: str
    outcome: str  # completed | failed | paused
    stop: str
    text: str | None = None
    waiting: list[Call] = field(default_factory=list)
    error: str | None = None  # the error's kind and type name, never upstream text


@dataclass
class RunInput:
    """One run: a task, or (task None) a resume with decisions and results."""

    agent: str
    session_id: str
    run_id: str
    task: str | None = None
    new_session: bool = False
    approve: dict[str, bool] = field(default_factory=dict)
    results: dict[str, ToolResultData] = field(default_factory=dict)


@dataclass
class Decision:
    """A person's answer to a paused run (the `decide` signal)."""

    approve: dict[str, bool] = field(default_factory=dict)
    results: dict[str, ToolResultData] = field(default_factory=dict)


@dataclass
class AgentInput:
    """The workflow's input. `routes` maps a host-executed tool to the task queue whose activity
    of that name runs it (a TypeScript host's worker, say). `pending` / `decision` carry a paused
    run across Continue-As-New, which happens at a pause once this workflow run has executed
    `max_runs` runs (or Temporal suggests it), so one run's history stays bounded."""

    agent: str
    task: str | None = None
    session_id: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    max_runs: int = 50
    run_timeout_s: float = 3600.0
    heartbeat_timeout_s: float = 120.0
    tool_timeout_s: float = 600.0
    pending: Step | None = None
    decision: Decision | None = None
