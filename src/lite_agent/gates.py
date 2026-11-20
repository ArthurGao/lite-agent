"""Gates: what decides, before a tool runs, whether it may (design §8). A gate answers for one
call: allow it, refuse it with a reason the model will read, or stop and wait for a person."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from lite_model import ToolCall

from lite_agent.events import Event


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str  # goes back to the model as the call's result


@dataclass(frozen=True)
class PauseFor:
    reason: str  # for the person who decides; the run ends as paused


Decision = Allow | Deny | PauseFor


@dataclass(frozen=True)
class RunContext:
    session_id: str
    run_id: str
    events: Sequence[Event]  # the session's log so far


class Gate(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self, call: ToolCall, ctx: RunContext) -> Decision: ...
