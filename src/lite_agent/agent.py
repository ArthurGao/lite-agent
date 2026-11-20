"""`Agent`: the assembly (design §3). What it can do is what it was given when it was built;
there is no mode, and nothing changes afterwards."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lite_model import AsyncToolModel

from lite_agent.budget import Budget
from lite_agent.result import RunResult
from lite_agent.runner import InProcessRunner, Runner
from lite_agent.store import InMemorySessionStore, SessionStore, new_id
from lite_agent.tools import Tool


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class Agent:
    role: str  # a lite-model role; never a model id
    model: AsyncToolModel
    system: str = ""
    tools: Sequence[Tool] = ()
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    budget: Budget = field(default_factory=Budget)
    runner: Runner = field(default_factory=InProcessRunner)
    clock: Callable[[], datetime] = _now  # event times
    ids: Callable[[], str] = new_id  # session and run ids

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("Agent.role must name a lite-model role")
        object.__setattr__(self, "tools", tuple(self.tools))
        names = [t.name for t in self.tools]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"tool names must be unique: {', '.join(repeated)}")
        if external := [t.name for t in self.tools if t.external]:
            # until pause / resume exists (S2b) nothing could hand the call to the host
            raise ValueError(f"external tools are not supported yet: {', '.join(external)}")

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def run(self, task: str, *, session_id: str | None = None) -> RunResult:
        """Run `task` to an outcome. With `session_id`, as the next run of that session."""
        return await self.runner.run(self, session_id, task)

    def run_sync(self, task: str, *, session_id: str | None = None) -> RunResult:
        """`run()` for a caller without an event loop. Inside a running loop `asyncio.run`
        raises RuntimeError: await `run()` there instead."""
        return asyncio.run(self.run(task, session_id=session_id))
