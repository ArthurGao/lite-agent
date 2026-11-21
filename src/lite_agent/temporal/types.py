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


@dataclass
class WorkItem:
    """Inbox events handled by one run: consecutive messages for the same session are batched
    (several quick messages get one reply); a decision is one item of its own."""

    ids: list[int]
    kind: str  # message | decision
    session_id: str | None = None  # None: a new session
    text: str | None = None
    approve: dict[str, bool] = field(default_factory=dict)
    results: dict[str, ToolResultData] = field(default_factory=dict)


@dataclass
class Retry:
    """A work item that failed for a reason that may pass, to be retried exactly as it was:
    the same events, the same session and run ids, so a run cut off part way goes on."""

    item: WorkItem
    session_id: str
    run_id: str
    backoff_s: float
    since: str = ""  # when it first failed (workflow time, ISO): retried until max_retry_s


@dataclass
class InboxInput:
    """One inbox's worker (design §9.1): its agent, and how much it takes per batch."""

    agent: str
    inbox: str
    batch_limit: int = 100
    max_batches: int = 100  # then Continue-As-New: the inbox, not the history, holds the work
    run_timeout_s: float = 3600.0
    heartbeat_timeout_s: float = 120.0
    idle_check_s: float = 300.0  # look at the inbox this often even if nobody wakes it
    backoff_s: float = 30.0  # before retrying an item that failed for a passing reason; doubles
    max_backoff_s: float = 3600.0
    # An item still failing this long after its first failure is set aside as `failed` (a
    # poison message must not hold up the inbox for ever); an outage shorter than this is lived.
    max_retry_s: float = 86400.0
    retry: Retry | None = None  # carried across Continue-As-New


@dataclass
class TimerInput:
    """The scheduler: it wakes every `interval_s`, enqueues what is due, and goes back to sleep."""

    task_queue: str
    interval_s: float = 60.0
    stalled_s: float = 60.0  # an event pending this long had its wake lost: wake its inbox
    max_ticks: int = 500
