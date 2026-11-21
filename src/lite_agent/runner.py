"""Where a run's steps execute (design §9). `InProcessRunner` runs them in the caller's event
loop. The Temporal runner (`lite_agent.temporal`) is a host-side API of its own, not this
protocol: an `Agent` cannot cross to a worker process, so workers run agents by name."""

from typing import TYPE_CHECKING, Protocol

from lite_agent.loop import run_loop
from lite_agent.result import Resume, RunResult

if TYPE_CHECKING:
    from lite_agent.agent import Agent


class Runner(Protocol):
    async def run(
        self,
        agent: "Agent",
        session_id: str | None,
        input: str | Resume,
        *,
        run_id: str | None = None,
        new_session: bool = False,
    ) -> RunResult:
        """A task (`str`; `session_id=None` starts a new session) or a `Resume` for a session
        that exists. `run_id` and `new_session` as for `Agent.run`."""
        ...


class InProcessRunner:
    async def run(
        self,
        agent: "Agent",
        session_id: str | None,
        input: str | Resume,
        *,
        run_id: str | None = None,
        new_session: bool = False,
    ) -> RunResult:
        return await run_loop(agent, session_id, input, run_id=run_id, new_session=new_session)
