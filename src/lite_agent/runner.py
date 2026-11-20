"""Where a run's steps execute (design §9). `InProcessRunner` runs them in the caller's event
loop; a durable runner (Temporal) is another implementation of the same protocol."""

from typing import TYPE_CHECKING, Protocol

from lite_agent.loop import run_loop
from lite_agent.result import Resume, RunResult

if TYPE_CHECKING:
    from lite_agent.agent import Agent


class Runner(Protocol):
    async def run(self, agent: "Agent", session_id: str | None, input: str | Resume) -> RunResult:
        """A task (`str`; `session_id=None` starts a new session) or a `Resume` for a session
        that exists."""
        ...


class InProcessRunner:
    async def run(self, agent: "Agent", session_id: str | None, input: str | Resume) -> RunResult:
        return await run_loop(agent, session_id, input)
