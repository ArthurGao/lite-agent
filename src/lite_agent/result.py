"""What a run returns (design §3)."""

from dataclasses import dataclass
from typing import Literal

from lite_model import Usage

from lite_agent.events import Outcome, StopReason


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
