"""Where a run's steps execute (design §9). `InProcessRunner` runs them in the caller's event
loop; a durable runner (Temporal) is another implementation of the same protocol."""

from typing import TYPE_CHECKING, Protocol

from lite_agent.loop import run_loop
from lite_agent.result import RunResult

if TYPE_CHECKING:
    from lite_agent.agent import Agent


class Runner(Protocol):
    async def run(self, agent: "Agent", session_id: str | None, task: str) -> RunResult:
        """`session_id=None` starts a new session."""
        ...


class InProcessRunner:
    async def run(self, agent: "Agent", session_id: str | None, task: str) -> RunResult:
        return await run_loop(agent, session_id, task)
