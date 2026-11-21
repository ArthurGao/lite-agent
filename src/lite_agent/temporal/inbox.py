"""The inbox's worker and the scheduler as Temporal activities (design §9.1). The inbox is
PostgreSQL (`PgInbox`); these only read it, mark it, and wake the right workflow."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from lite_agent.temporal.types import InboxInput, TimerInput, ToolResultData, WorkItem

if TYPE_CHECKING:  # the pg extra: a worker without an inbox never needs psycopg
    from lite_agent.pg.inbox import InboxEvent, PgInbox, Status

TAKE = "lite_agent.inbox.take"
FINISH = "lite_agent.inbox.finish"
FIRE = "lite_agent.timers.fire"
INBOX_WORKFLOW = "lite_agent.InboxWorkflow"
TIMER_WORKFLOW = "lite_agent.TimerWorkflow"
TIMERS_ID = "lite-agent-timers"


def inbox_workflow_id(inbox: str) -> str:
    """One workflow per inbox: Temporal then keeps it to one worker (KB §12.1)."""
    return f"inbox:{inbox}"


def batched(events: "list[InboxEvent]") -> list[WorkItem]:
    """Work items in arrival order: a run of consecutive messages for one session is one item."""
    items: list[WorkItem] = []
    for e in events:
        last = items[-1] if items else None
        if (
            e.kind == "message"
            and last
            and last.kind == "message"
            and last.session_id == e.session_id
        ):
            last.ids.append(e.id)
            last.text = f"{last.text}\n\n{e.text}"
            continue
        if e.kind == "message":
            items.append(WorkItem([e.id], "message", e.session_id, text=e.text))
        else:
            results = {
                k: ToolResultData(
                    str(v.get("content", "")), bool(v.get("is_error")), bool(v.get("unknown"))
                )
                for k, v in e.results.items()
            }
            items.append(
                WorkItem([e.id], "decision", e.session_id, approve=e.approve, results=results)
            )
    return items


async def wake(client: Client, task_queue: str, agent: str, inbox: str) -> None:
    """Start the inbox's worker, or signal the one that runs (signal-with-start)."""
    await client.start_workflow(
        INBOX_WORKFLOW,
        InboxInput(agent=agent, inbox=inbox),
        id=inbox_workflow_id(inbox),
        task_queue=task_queue,
        start_signal="wake",
    )


async def enqueue(
    client: Client,
    inbox: "PgInbox",
    task_queue: str,
    *,
    agent: str,
    name: str,
    source: str,
    source_id: str,
    text: str | None = None,
    session_id: str | None = None,
    approve: dict[str, bool] | None = None,
    results: dict[str, dict[str, object]] | None = None,
) -> bool:
    """What a webhook handler calls (and nothing else): record the event, wake the worker.
    False when this source had delivered the event already (it is not recorded twice)."""
    new = await inbox.put(
        name,
        source,
        source_id,
        text=text,
        session_id=session_id,
        approve=approve,
        results=results,
        agent=agent,
    )
    await wake(client, task_queue, agent, name)
    return new


async def start_timers(client: Client, task_queue: str, *, interval_s: float = 60.0) -> None:
    """Start the scheduler once; starting it again finds the one that runs."""
    await client.start_workflow(
        TIMER_WORKFLOW,
        TimerInput(task_queue=task_queue, interval_s=interval_s),
        id=TIMERS_ID,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


class InboxActivities:
    def __init__(self, inbox: "PgInbox", client: Client) -> None:
        self.inbox = inbox
        self.client = client

    @activity.defn(name=TAKE)
    async def take(self, inbox: str, limit: int) -> list[WorkItem]:
        return batched(await self.inbox.pending(inbox, limit))

    @activity.defn(name=FINISH)
    async def finish(self, ids: list[int], status: str, run_session: str | None) -> None:
        marked: Status = (
            "failed" if status == "failed" else "refused" if status == "refused" else "done"
        )
        await self.inbox.finish(ids, status=marked, run_session=run_session)

    @activity.defn(name=FIRE)
    async def fire(self, now: str, given: TimerInput) -> int:
        """Enqueue what is due at the workflow's `now`, and wake each inbox it wrote to and
        each with work pending for longer than `stalled_s` (a wake that was lost)."""
        at = datetime.fromisoformat(now)
        fired = await self.inbox.fire_due(at)
        stalled = await self.inbox.stalled(at - timedelta(seconds=given.stalled_s))
        for f in dict.fromkeys([*fired, *stalled]):
            await wake(self.client, given.task_queue, f.agent, f.inbox)
        return len(fired)
