"""`Agent`: the assembly (design §3). What it can do is what it was given when it was built;
there is no mode, and nothing changes afterwards."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lite_model import AsyncToolModel

from lite_agent.budget import Budget
from lite_agent.compaction import Compactor
from lite_agent.context import Memory, SkillSource, build
from lite_agent.events import Event
from lite_agent.gates import Gate
from lite_agent.result import Resume, RunResult
from lite_agent.runner import InProcessRunner, Runner
from lite_agent.store import InMemorySessionStore, SessionStore, new_id
from lite_agent.tools import Tool


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class Agent:
    role: str  # a lite-model role; never a model id
    # Another role, used once per run after the model behind `role` could not be reached
    # (design §5 rule 11). Off unless named; `model` must serve both roles.
    fallback_role: str | None = None
    model: AsyncToolModel
    system: str = ""
    tools: Sequence[Tool] = ()
    gates: Sequence[Gate] = ()  # asked in order before a tool runs (design §8)
    # How to tell the task is done (design §5 rule 3): given the session's log after a text-only
    # turn, return None when it is, or what is still open, which is sent back to the model.
    # Without it a text-only turn ends the run.
    done: Callable[[Sequence[Event]], str | None] | None = None
    # Instructions the model loads on demand, and notes that outlast a session (design §6).
    # Each adds its tools after the host's (`load_skill`; `recall` and `remember`).
    skills: SkillSource | None = None
    memory: Memory | None = None
    # What makes a long history smaller before a call, and the lite-model role that writes the
    # summary (design §6). Without it nothing is summarised.
    compaction: Compactor | None = None
    compact_role: str = "compact"
    sessions: SessionStore = field(default_factory=InMemorySessionStore)
    budget: Budget = field(default_factory=Budget)
    runner: Runner = field(default_factory=InProcessRunner)
    clock: Callable[[], datetime] = _now  # event times
    ids: Callable[[], str] = new_id  # session and run ids
    # Set when the agent is built, and fixed from then on. `tools` stays what the host gave.
    all_tools: tuple[Tool, ...] = field(init=False, repr=False, compare=False)
    _system: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("Agent.role must name a lite-model role")
        if self.fallback_role is not None and self.fallback_role in ("", self.role):
            raise ValueError("Agent.fallback_role must name another lite-model role")
        context = build(self.system, self.skills, self.memory, limit=self.budget.skills_index_chars)
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "all_tools", (*self.tools, *context.tools))
        object.__setattr__(self, "_system", context.system)
        names = [t.name for t in self.all_tools]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"tool names must be unique: {', '.join(repeated)}")
        object.__setattr__(self, "gates", tuple(self.gates))
        if self.compaction is not None:
            self.compaction.check([r for r in (self.role, self.fallback_role) if r is not None])

    def system_prompt(self) -> str:
        """The system prompt every run of this agent is opened with: the host's, then what
        skills and memory add, as it was assembled when the agent was built."""
        return self._system

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.all_tools if t.name == name), None)

    async def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        new_session: bool = False,
    ) -> RunResult:
        """Run `task` to an outcome. With `session_id`, as the next run of that session; with
        `new_session`, in a new session that takes that id. A `run_id` names the run: calling
        again with it never starts a second run (a finished run is answered from the log, one
        that was cut off goes on where it stopped; design §9)."""
        return await self.runner.run(self, session_id, task, run_id=run_id, new_session=new_session)

    async def resume(
        self, session_id: str, input: Resume | None = None, *, run_id: str | None = None
    ) -> RunResult:
        """Go on with a session whose last run paused, failed or was interrupted: settle the
        calls its last turn left without a result, then continue (design §8). `input` carries
        the decisions and results the host brings; nothing is needed to retry a failed turn or
        to recover calls that are safe to run again. `run_id` as for `run()`."""
        return await self.runner.run(self, session_id, input or Resume(), run_id=run_id)

    def run_sync(
        self,
        task: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        new_session: bool = False,
    ) -> RunResult:
        """`run()` for a caller without an event loop. Inside a running loop `asyncio.run`
        raises RuntimeError: await `run()` there instead."""
        return asyncio.run(
            self.run(task, session_id=session_id, run_id=run_id, new_session=new_session)
        )
