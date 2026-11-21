"""Always-on agents (design §9.1, S7b): every trigger goes through the PostgreSQL inbox, one
Temporal workflow per inbox runs the work, timers only enqueue. Time-skipping server + the test
database."""

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from lite_model.testing import AsyncFakeModel
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from lite_agent import Agent
from lite_agent.pg import PgSessionStore
from lite_agent.pg.inbox import PgInbox
from lite_agent.registry import Registry
from lite_agent.temporal import (
    RUN,
    SANDBOX,
    AgentActivities,
    AgentWorkflow,
    InboxActivities,
    InboxWorkflow,
    TimerWorkflow,
    enqueue,
    inbox_workflow_id,
    make_worker,
    start_timers,
)
from lite_agent.temporal.types import RunInput, Step
from tests.helpers import Ask, Recording, call, kinds, say

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


class Box:
    """A test schema's store and inbox, and agents on them."""

    def __init__(self, url: str, schema: str) -> None:
        self.url, self.schema = url, schema
        self.names: set[str] = set()  # inboxes whose workflows the test leaves behind
        self.store = PgSessionStore(url, schema=schema)
        self.inbox = PgInbox(self.store)
        self.tools = Recording()
        self.registry = Registry(
            self.store,
            {
                "echo": Agent(
                    role="agent",
                    model=AsyncFakeModel(agent=lambda asked: say("Got it.")),
                    sessions=self.store,
                ),
                "payer": Agent(
                    role="agent",
                    model=AsyncFakeModel(
                        agent=lambda asked: (
                            say("Paid.")
                            if any(type(m).__name__ == "ToolResults" for m in asked.messages[-1:])
                            else call(("c1", "pay", {"amount": 5}))
                        )
                    ),
                    tools=(self.tools.tool("pay", "receipt"),),
                    gates=(Ask(),),
                    sessions=self.store,
                ),
            },
        )

    def rows(self) -> list[tuple[Any, ...]]:
        """(source_id, status, run_session) of every inbox event, in order."""
        with psycopg.connect(self.url) as conn:
            return conn.execute(
                f"SELECT source_id, status, run_session FROM {self.schema}.inbox_events ORDER BY id"
            ).fetchall()


@pytest_asyncio.fixture(loop_scope="module")
async def box(env: WorkflowEnvironment, pg_url: str, pg_schema: str) -> AsyncIterator[Box]:
    b = Box(pg_url, pg_schema)
    yield b
    # A worker that is gone leaves its inbox workflow waiting on its idle timer: tasks nobody
    # takes, which stop the time-skipping server from skipping time for the next test.
    for name in b.names:
        with contextlib.suppress(Exception):
            await env.client.get_workflow_handle(inbox_workflow_id(name)).terminate()
    await b.store.close()


def queue() -> str:
    return f"q-{uuid.uuid4().hex[:8]}"


async def settled(env: WorkflowEnvironment, box: Box, inbox: str) -> list[tuple[Any, ...]]:
    """The inbox's rows once nothing in it is pending (time is skipped while waiting)."""
    box.names.add(inbox)
    for _ in range(120):
        if not await box.inbox.pending(inbox):
            return box.rows()
        await env.sleep(1)
    raise AssertionError("the inbox never settled")


async def say_to(
    env: WorkflowEnvironment, box: Box, q: str, agent: str, inbox: str, source_id: str, **kw: Any
) -> bool:
    return await enqueue(
        env.client, box.inbox, q, agent=agent, name=inbox, source="chat", source_id=source_id, **kw
    )


async def test_a_delivery_twice_is_handled_once_and_quick_messages_are_one_run(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    await box.inbox.put(name, "chat", "m1", text="Hi.", agent="echo")
    await box.inbox.put(name, "chat", "m2", text="Where is my lamp?", agent="echo")
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        assert await say_to(env, box, q, "echo", name, "m2", text="Where is my lamp?") is False
        rows = await settled(env, box, name)
    assert [(r[0], r[1]) for r in rows] == [("m1", "done"), ("m2", "done")]
    (session,) = {r[2] for r in rows}  # both in one run of one session
    events = list(await box.store.read(session))
    assert kinds(events).count("run_started") == 1
    (task,) = [e for e in events if e.type == "user_message"]
    assert task.text == "Hi.\n\nWhere is my lamp?"


async def test_a_reply_continues_its_session(env: WorkflowEnvironment, box: Box) -> None:
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        await say_to(env, box, q, "echo", name, "m1", text="Hi.")
        (first,) = await settled(env, box, name)
        await say_to(env, box, q, "echo", name, "m2", text="And now?", session_id=first[2])
        rows = await settled(env, box, name)
    assert [r[2] for r in rows] == [first[2], first[2]]
    assert kinds(await box.store.read(first[2])).count("run_started") == 2


async def test_an_approval_comes_back_as_a_decision_event(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"bo-{uuid.uuid4().hex[:6]}"
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        await say_to(env, box, q, "payer", name, "m1", text="Pay.")
        (asked,) = await settled(env, box, name)
        session = asked[2]
        assert kinds(await box.store.read(session))[-1] == "run_finished"  # paused: the run ended
        assert box.tools.ran == []
        await say_to(env, box, q, "payer", name, "d1", session_id=session, approve={"c1": True})
        rows = await settled(env, box, name)
    assert [(r[0], r[1]) for r in rows] == [("m1", "done"), ("d1", "done")]
    assert box.tools.ran == [("pay", {"amount": 5})]
    assert [e for e in await box.store.read(session) if e.type == "run_finished"][
        -1
    ].outcome == "completed"


async def test_an_event_the_agent_cannot_take_is_refused_and_the_worker_goes_on(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        await say_to(env, box, q, "echo", name, "m1", text="Hi.")
        (first,) = await settled(env, box, name)
        # nothing in that session waits for a decision
        await say_to(env, box, q, "echo", name, "d1", session_id=first[2], approve={"c1": True})
        await say_to(env, box, q, "echo", name, "m2", text="Still there?")
        rows = await settled(env, box, name)
    assert [(r[0], r[1]) for r in rows] == [("m1", "done"), ("d1", "refused"), ("m2", "done")]


async def test_a_follow_up_and_a_schedule_arrive_when_due(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"ops-{uuid.uuid4().hex[:6]}"
    now = await env.get_current_time()
    await box.inbox.add_follow_up(name, "echo", now + timedelta(hours=1), "Check B03 again.")
    await box.inbox.add_schedule(name, "echo", now + timedelta(minutes=30), 3600, "Hourly report.")
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        await start_timers(env.client, q, interval_s=60)
        await start_timers(
            env.client, q, interval_s=60
        )  # starting it again finds the one that runs
        await env.sleep(timedelta(hours=2, minutes=5))
        rows = await settled(env, box, name)
        await env.client.get_workflow_handle("lite-agent-timers").terminate()
    texts = []
    for _, _, session in rows:
        texts += [e.text for e in await box.store.read(session) if e.type == "user_message"]
    assert sorted(texts) == [
        "Check B03 again.",
        "Hourly report.",
        "Hourly report.",
    ]  # 0:30, 1:00, 1:30
    assert all(r[1] == "done" for r in rows) and len(rows) == 3


class Flaky(AgentActivities):
    failed = False

    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        step = await super().run(given)
        if not Flaky.failed:
            Flaky.failed = True
            raise RuntimeError("the worker died after the run")
        return step


async def test_a_run_retried_after_a_crash_is_one_run_and_one_handled_event(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    Flaky.failed = False
    box_acts = InboxActivities(box.inbox, env.client)
    async with Worker(
        env.client,
        task_queue=q,
        workflows=[AgentWorkflow, InboxWorkflow, TimerWorkflow],
        activities=[Flaky(box.registry).run, box_acts.take, box_acts.finish, box_acts.fire],
        workflow_runner=SANDBOX,
    ):
        await say_to(env, box, q, "echo", name, "m1", text="Hi.")
        rows = await settled(env, box, name)
    assert Flaky.failed and [(r[0], r[1]) for r in rows] == [("m1", "done")]
    assert kinds(await box.store.read(rows[0][2])).count("run_started") == 1


# --- Codex and simplify review 2026-11-21: work is never lost ---


class Down(AgentActivities):
    """The model or the database away for longer than one item's retries, then back."""

    attempts = 0

    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        Down.attempts += 1
        if Down.attempts <= 7:  # more than one item's five attempts
            raise RuntimeError("the model is unreachable")
        return await super().run(given)


async def test_an_outage_longer_than_the_retries_leaves_the_work_pending_not_refused(
    env: WorkflowEnvironment, box: Box
) -> None:
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    Down.attempts = 0
    acts = InboxActivities(box.inbox, env.client)
    async with Worker(
        env.client,
        task_queue=q,
        workflows=[AgentWorkflow, InboxWorkflow, TimerWorkflow],
        activities=[Down(box.registry).run, acts.take, acts.finish, acts.fire],
        workflow_runner=SANDBOX,
    ):
        await say_to(env, box, q, "echo", name, "m1", text="Hi.")
        rows = await settled(env, box, name)
    assert Down.attempts > 7
    assert [(r[0], r[1]) for r in rows] == [("m1", "done")]  # done once it came back, never refused
    assert kinds(await box.store.read(rows[0][2])).count("run_started") == 1


async def test_an_event_nobody_woke_for_is_found_by_the_scheduler(
    env: WorkflowEnvironment, box: Box
) -> None:
    # the handler wrote the row and died before it woke the worker
    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    await box.inbox.put(name, "chat", "m1", text="Hi.", agent="echo")
    async with make_worker(env.client, q, box.registry, inbox=box.inbox):
        await start_timers(env.client, q, interval_s=60)
        await env.sleep(timedelta(minutes=3))
        rows = await settled(env, box, name)
        await env.client.get_workflow_handle("lite-agent-timers").terminate()
    assert [(r[0], r[1]) for r in rows] == [("m1", "done")]


class Broken(AgentActivities):
    """A run that fails every time on one message (a bug, or content that breaks it)."""

    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        if given.task and "poison" in given.task:
            raise RuntimeError("this message breaks the run")
        return await super().run(given)


async def test_a_message_that_always_fails_is_set_aside_and_the_inbox_goes_on(
    env: WorkflowEnvironment, box: Box
) -> None:
    # security review 2026-11-21: retrying for ever would hold up every event behind it
    from lite_agent.temporal import INBOX_WORKFLOW
    from lite_agent.temporal.types import InboxInput

    q, name = queue(), f"ana-{uuid.uuid4().hex[:6]}"
    box.names.add(name)
    await box.inbox.put(name, "chat", "m1", text="poison", agent="echo")
    acts = InboxActivities(box.inbox, env.client)
    async with Worker(
        env.client,
        task_queue=q,
        workflows=[AgentWorkflow, InboxWorkflow, TimerWorkflow],
        activities=[Broken(box.registry).run, acts.take, acts.finish, acts.fire],
        workflow_runner=SANDBOX,
    ):
        await env.client.start_workflow(
            INBOX_WORKFLOW,
            InboxInput(agent="echo", inbox=name, max_retry_s=600),
            id=inbox_workflow_id(name),
            task_queue=q,
            start_signal="wake",
        )
        for _ in range(60):  # retried with a growing pause, then set aside
            if not await box.inbox.pending(name):
                break
            await env.sleep(60)
        # sent once m1 is set aside (sent earlier, it would have been batched with it)
        await enqueue(
            env.client,
            box.inbox,
            q,
            agent="echo",
            name=name,
            source="chat",
            source_id="m2",
            text="Hello.",
        )
        rows = await settled(env, box, name)
    assert [(r[0], r[1]) for r in rows] == [("m1", "failed"), ("m2", "done")]
