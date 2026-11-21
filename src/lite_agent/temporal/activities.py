"""The one activity (design §9): a whole `Agent.run` or `resume`, to its end or a pause. The run
is named by the workflow (`run_id`), so a retried activity never starts a second run: a run that
finished is answered from the log, one cut off goes on where it stopped (design §3)."""

import asyncio
import contextlib

from temporalio import activity
from temporalio.exceptions import ApplicationError

from lite_agent.agent import Agent
from lite_agent.errors import UnknownSession, UnresolvedCalls
from lite_agent.events import ModelTurn, ToolCallBlock
from lite_agent.registry import Registry
from lite_agent.result import Resume, RunResult, ToolOutcome
from lite_agent.temporal.types import Call, RunInput, Step

RUN = "lite_agent.run"
# The caller's mistakes: retrying cannot help, so the workflow sees them at once.
_CALLER_ERRORS = (UnknownSession, UnresolvedCalls, ValueError)
PERMANENT = ("UnknownAgent", *(e.__name__ for e in _CALLER_ERRORS))


class AgentActivities:
    """Activities over a registry; `heartbeat_s` must be well under the workflow's heartbeat
    timeout, so a worker that died is noticed and the run retried on another."""

    def __init__(self, registry: Registry, *, heartbeat_s: float = 10.0) -> None:
        self.registry = registry
        self.heartbeat_s = heartbeat_s

    async def _beat(self) -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(self.heartbeat_s)

    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        agent = self.registry.get(given.agent)
        if agent is None:
            raise ApplicationError(
                f"no agent {given.agent}", type="UnknownAgent", non_retryable=True
            )
        beat = asyncio.create_task(self._beat())
        try:
            if given.task is not None:
                result = await agent.run(
                    given.task,
                    session_id=given.session_id,
                    run_id=given.run_id,
                    new_session=given.new_session,
                )
            else:
                decision = Resume(
                    approve=dict(given.approve),
                    results={
                        k: ToolOutcome(v.content, v.is_error, v.unknown)
                        for k, v in given.results.items()
                    },
                )
                result = await agent.resume(given.session_id, decision, run_id=given.run_id)
        except _CALLER_ERRORS as exc:
            raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat  # gone before the activity reports
        return await step_of(agent, result)


async def step_of(agent: Agent, result: RunResult) -> Step:
    """The run's end as the workflow needs it: each waiting call with its tool and arguments,
    read from the call in the log."""
    waiting: list[Call] = []
    if result.pause is not None:
        events = await agent.sessions.read(result.session_id)
        turn = next((e for e in reversed(events) if isinstance(e, ModelTurn)), None)
        calls: dict[str, ToolCallBlock] = {
            b.id: b for b in (turn.blocks if turn else ()) if isinstance(b, ToolCallBlock)
        }
        for w in result.pause.waiting:
            block = calls.get(w.call_id)
            waiting.append(
                Call(
                    call_id=w.call_id,
                    reason=w.reason,
                    name=block.name if block else "",
                    arguments=dict(block.arguments or {}) if block else {},
                    note=w.note,
                    gate=w.gate,
                )
            )
    error = f"{result.error.kind}:{result.error.type}" if result.error else None
    return Step(
        session_id=result.session_id,
        run_id=result.run_id,
        outcome=result.outcome,
        stop=result.stop,
        text=result.text,
        waiting=waiting,
        error=error,
    )
