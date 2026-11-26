"""The durable runner (design §9; the `temporal` extra): an agent run as a Temporal workflow.

worker = make_worker(client, "agents", load_agents("agents.toml"))   # in the worker process
runner = TemporalRunner(client, "agents")                            # in the host
workflow_id = await runner.start("support", "Refund order B03.")
await runner.decide(workflow_id, approve={"c1": True})               # a paused run goes on
step = await runner.result(workflow_id)
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from lite_agent.registry import Registry
from lite_agent.store import new_id
from lite_agent.temporal.activities import RUN, AgentActivities
from lite_agent.temporal.inbox import (
    INBOX_WORKFLOW,
    TIMER_WORKFLOW,
    InboxActivities,
    enqueue,
    inbox_workflow_id,
    start_timers,
)
from lite_agent.temporal.types import AgentInput, Call, Decision, Step, ToolResultData
from lite_agent.temporal.workflows import (
    AGENT_WORKFLOW,
    AgentWorkflow,
    InboxWorkflow,
    TimerWorkflow,
)

if TYPE_CHECKING:
    from lite_agent.pg.inbox import PgInbox

# lite-agent and what it imports are deterministic library code the workflow only passes data
# through: loaded once outside the sandbox, not again for every workflow run.
SANDBOX = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "lite_agent", "lite_model", "lite_context", "pydantic", "pydantic_core"
    )
)


def make_worker(
    client: Client,
    task_queue: str,
    registry: Registry,
    *,
    inbox: "PgInbox | None" = None,
    activities: Sequence[Any] = (),
    heartbeat_s: float = 10.0,
) -> Worker:
    """A worker that runs `AgentWorkflow` and the run activity for `registry`'s agents; with
    `inbox`, also the inbox workers and the scheduler (design §9.1). `activities` adds the
    host's own (host-executed tools)."""
    runs = AgentActivities(registry, heartbeat_s=heartbeat_s)
    workflows: list[type] = [AgentWorkflow]
    acts: list[Any] = [runs.run, *activities]
    if inbox is not None:
        box = InboxActivities(inbox, client)
        workflows += [InboxWorkflow, TimerWorkflow]
        acts += [box.take, box.finish, box.fire]
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=acts,
        workflow_runner=SANDBOX,
    )


class TemporalRunner:
    """The host's side: start a run, answer a paused one, wait for the end."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(
        self,
        agent: str,
        task: str,
        *,
        session_id: str | None = None,
        routes: Mapping[str, str] | None = None,
        workflow_id: str | None = None,
        max_runs: int = 50,
    ) -> str:
        """Start `task` for `agent` (a new session, or the next run of `session_id`); returns
        the workflow id."""
        wid = workflow_id or f"lite-agent-{new_id()}"
        given = AgentInput(
            agent=agent,
            task=task,
            session_id=session_id,
            routes=dict(routes or {}),
            max_runs=max_runs,
        )
        await self.client.start_workflow(AGENT_WORKFLOW, given, id=wid, task_queue=self.task_queue)
        return wid

    async def decide(
        self,
        workflow_id: str,
        *,
        approve: Mapping[str, bool] | None = None,
        results: Mapping[str, ToolResultData] | None = None,
    ) -> None:
        decision = Decision(approve=dict(approve or {}), results=dict(results or {}))
        await self.client.get_workflow_handle(workflow_id).signal(AgentWorkflow.decide, decision)

    async def status(self, workflow_id: str) -> Step | None:
        return await self.client.get_workflow_handle(workflow_id).query(AgentWorkflow.status)

    async def result(self, workflow_id: str) -> Step:
        handle = self.client.get_workflow_handle(workflow_id, result_type=Step)
        step: Step = await handle.result()
        return step


__all__ = [
    "AGENT_WORKFLOW",
    "INBOX_WORKFLOW",
    "RUN",
    "SANDBOX",
    "TIMER_WORKFLOW",
    "AgentActivities",
    "AgentInput",
    "AgentWorkflow",
    "Call",
    "Decision",
    "InboxActivities",
    "InboxWorkflow",
    "Step",
    "TemporalRunner",
    "TimerWorkflow",
    "ToolResultData",
    "enqueue",
    "inbox_workflow_id",
    "make_worker",
    "start_timers",
]
