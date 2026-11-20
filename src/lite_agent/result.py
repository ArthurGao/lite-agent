"""What a run returns (design §3)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from lite_model import Usage

from lite_agent.events import Outcome, StopReason, Waiting

__all__ = ["Pause", "Resume", "RunError", "RunResult", "ToolOutcome", "Waiting"]


@dataclass(frozen=True)
class Pause:
    waiting: tuple[Waiting, ...]

    @property
    def call_ids(self) -> tuple[str, ...]:
        return tuple(w.call_id for w in self.waiting)


@dataclass(frozen=True)
class ToolOutcome:
    """What the host reports for a call it executed or checked."""

    content: str
    is_error: bool = False
    # Nobody can tell whether it took effect. It is told to the model as an error of its own
    # kind (`outcome_unknown`), whatever `is_error` says.
    unknown: bool = False


@dataclass(frozen=True)
class Resume:
    """What the host brings back to a paused or interrupted session (design §8)."""

    approve: Mapping[str, bool] = field(default_factory=dict, hash=False)  # call id -> go ahead?
    results: Mapping[str, ToolOutcome] = field(
        default_factory=dict, hash=False
    )  # call id -> result


@dataclass(frozen=True)
class RunError:
    kind: Literal["model", "store", "harness"]
    type: str  # the exception's type name; never upstream text


@dataclass(frozen=True)
class RunResult:
    session_id: str
    run_id: str
    outcome: Outcome
    stop: StopReason
    text: str | None  # the last assistant text, when there is one
    error: RunError | None
    usage: Usage  # this run's model calls, summed; an unknown part stays None
    pause: Pause | None = None  # set exactly when outcome is "paused"
    switched: bool = False  # this run moved to the agent's fallback role (design §5 rule 11)
