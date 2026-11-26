"""The durable runner (design §9, S7a) on Temporal's time-skipping test server: an agent run as
a workflow, a paused run answered by signal, a host tool routed to another task queue, retried
activities that never start a second run, Continue-As-New, and a replayed history."""

import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_asyncio
from lite_model.testing import AsyncFakeModel
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from lite_agent import Agent, FunctionTool, InMemorySessionStore, ToolContext
from lite_agent.registry import Registry
from lite_agent.temporal import (
    RUN,
    SANDBOX,
    AgentActivities,
    AgentWorkflow,
    TemporalRunner,
    ToolResultData,
    make_worker,
)
from lite_agent.temporal.types import RunInput, Step
from tests.helpers import Ask, Recording, call, kinds, say

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


def scripted(*replies: Any) -> AsyncFakeModel:
    """Replies by position in the conversation: the n-th request of a session gets the n-th
    reply, however often a retried run asks again."""
    turns = list(replies)
    return AsyncFakeModel(agent=lambda asked: turns[min(_turns(asked), len(turns) - 1)])


def _turns(asked: Any) -> int:
    return sum(1 for m in asked.messages if type(m).__name__ == "AssistantTurn")


class Shop:
    """A registry with the agents the tests use, on one in-memory store."""

    def __init__(self) -> None:
        self.store = InMemorySessionStore()
        self.tools = Recording()
        self.registry = Registry(
            self.store,
            {
                "clock": Agent(
                    role="agent",
                    model=scripted(call(("c1", "get_time", {})), say("Noon.")),
                    tools=(self.tools.tool("get_time", "12:00"),),
                    sessions=self.store,
                ),
                "payer": Agent(
                    role="agent",
                    model=scripted(call(("c1", "pay", {"amount": 5})), say("Paid.")),
                    tools=(self.tools.tool("pay", "receipt"),),
                    gates=(Ask(),),
                    sessions=self.store,
                ),
                "looker": Agent(
                    role="agent",
                    model=scripted(call(("c1", "lookup", {"q": "B03"})), say("Found it.")),
                    tools=(
                        FunctionTool(
                            "lookup", "Looks up.", {"type": "object"}, _never, external=True
                        ),
                    ),
                    sessions=self.store,
                ),
            },
        )


async def _never(args: Mapping[str, Any], ctx: ToolContext) -> str:
    raise AssertionError("an external tool is executed by the host, not here")


def queue() -> str:
    return f"q-{uuid.uuid4().hex[:8]}"


async def test_a_run_to_its_end(env: WorkflowEnvironment) -> None:
    shop, q = Shop(), queue()
    runner = TemporalRunner(env.client, q)
    async with make_worker(env.client, q, shop.registry):
        wid = await runner.start("clock", "Time?")
        step = await runner.result(wid)
    assert (step.outcome, step.text) == ("completed", "Noon.")
    assert shop.tools.ran == [("get_time", {})]
    assert kinds(await shop.store.read(step.session_id))[-1] == "run_finished"


async def test_a_paused_run_waits_for_a_decision_by_signal(env: WorkflowEnvironment) -> None:
    shop, q = Shop(), queue()
    runner = TemporalRunner(env.client, q)
    async with make_worker(env.client, q, shop.registry):
        wid = await runner.start("payer", "Pay.")
        status = await _paused(env, runner, wid)
        assert [(c.call_id, c.reason, c.name, c.arguments) for c in status.waiting] == [
            ("c1", "approval", "pay", {"amount": 5})
        ]
        assert shop.tools.ran == []
        await runner.decide(wid, approve={"c1": True})
        step = await runner.result(wid)
    assert (step.outcome, step.text) == ("completed", "Paid.")
    assert shop.tools.ran == [("pay", {"amount": 5})]


async def _paused(env: WorkflowEnvironment, runner: TemporalRunner, wid: str) -> Step:
    """The workflow's status once its run has paused. Time is skipped while waiting: a test
    that only queries does not move the test server's clock, and a retry waits on it."""
    for _ in range(60):
        status = await runner.status(wid)
        if status is not None and status.outcome == "paused":
            return status
        await env.sleep(1)
    raise AssertionError("the run never paused")


async def _continued(env: WorkflowEnvironment, wid: str, first: str) -> None:
    """Wait until workflow `wid` has continued as new from run `first` (describe, not a query)."""
    for _ in range(60):
        if (await env.client.get_workflow_handle(wid).describe()).run_id != first:
            return
        await env.sleep(1)
    raise AssertionError("the workflow never continued as new")


@activity.defn(name="lookup")
async def lookup(arguments: dict[str, Any]) -> str:
    return f"found {arguments['q']}: an office chair"


async def test_a_host_tool_runs_as_an_activity_on_the_hosts_task_queue(
    env: WorkflowEnvironment,
) -> None:
    shop, q, host = Shop(), queue(), queue()
    runner = TemporalRunner(env.client, q)
    async with (
        make_worker(env.client, q, shop.registry),
        Worker(env.client, task_queue=host, activities=[lookup]),  # e.g. a TypeScript host's worker
    ):
        wid = await runner.start("looker", "Find B03.", routes={"lookup": host})
        step = await runner.result(wid)
    assert (step.outcome, step.text) == ("completed", "Found it.")
    results = [e for e in await shop.store.read(step.session_id) if e.type == "tool_result"]
    assert [(r.content, r.origin) for r in results] == [("found B03: an office chair", "external")]


class Flaky(AgentActivities):
    """The run activity, failing once after the run did its work: Temporal retries it."""

    failed = False

    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        step = await super().run(given)
        if not Flaky.failed:
            Flaky.failed = True
            raise RuntimeError("the worker died before Temporal recorded the result")
        return step


async def test_a_retried_activity_never_starts_a_second_run(env: WorkflowEnvironment) -> None:
    shop, q = Shop(), queue()
    runner = TemporalRunner(env.client, q)
    Flaky.failed = False
    flaky = Flaky(shop.registry)
    async with Worker(
        env.client,
        task_queue=q,
        workflows=[AgentWorkflow],
        activities=[flaky.run],
        workflow_runner=SANDBOX,
    ):
        wid = await runner.start("clock", "Time?")
        step = await runner.result(wid)
    events = list(await shop.store.read(step.session_id))
    assert Flaky.failed and (step.outcome, step.text) == ("completed", "Noon.")
    assert kinds(events).count("run_started") == 1
    assert shop.tools.ran == [("get_time", {})]  # the tool ran once


class WorkerDied(BaseException):
    """The process going away in the middle of a tool."""


class DiesInTool(AgentActivities):
    @activity.defn(name=RUN)
    async def run(self, given: RunInput) -> Step:
        try:
            return await super().run(given)
        except WorkerDied:
            raise RuntimeError("the worker died inside a tool") from None


async def test_a_worker_that_died_inside_a_tool_leaves_it_to_a_person(
    env: WorkflowEnvironment,
) -> None:
    store, ran = InMemorySessionStore(), []

    async def pay(args: Mapping[str, Any], ctx: ToolContext) -> str:
        ran.append("pay")
        raise WorkerDied()  # after tool_started was written; never returns

    payer = Agent(
        role="agent",
        model=scripted(call(("c1", "pay", {})), say("Paid.")),
        tools=(FunctionTool("pay", "Pays.", {"type": "object"}, pay),),  # not idempotent
        sessions=store,
    )
    q = queue()
    runner = TemporalRunner(env.client, q)
    acts = DiesInTool(Registry(store, {"payer": payer}))
    async with Worker(
        env.client,
        task_queue=q,
        workflows=[AgentWorkflow],
        activities=[acts.run],
        workflow_runner=SANDBOX,
    ):
        wid = await runner.start("payer", "Pay.")
        status = await _paused(env, runner, wid)
        assert [(c.call_id, c.reason) for c in status.waiting] == [("c1", "reconcile")]
        await runner.decide(wid, results={"c1": ToolResultData("paid, checked by hand")})
        step = await runner.result(wid)
    assert (step.outcome, step.text, ran) == ("completed", "Paid.", ["pay"])


async def test_continue_as_new_carries_a_paused_run_on(env: WorkflowEnvironment) -> None:
    shop, q = Shop(), queue()
    runner = TemporalRunner(env.client, q)
    async with make_worker(env.client, q, shop.registry):
        wid = await runner.start("payer", "Pay.", max_runs=0)  # continue as new at the first pause
        first = (await env.client.get_workflow_handle(wid).describe()).run_id
        # the time-skipping test server drops a query that arrives while a run continues as new
        # (it times out; a dev server answers it): query only once the new run is there
        await _continued(env, wid, first)
        await _paused(env, runner, wid)
        await runner.decide(wid, approve={"c1": True})
        step = await runner.result(wid)
        last = (await env.client.get_workflow_handle(wid).describe()).run_id
    assert (step.outcome, step.text) == ("completed", "Paid.")
    assert first != last
    assert kinds(await shop.store.read(step.session_id)).count("session_started") == 1


async def test_a_recorded_history_replays_on_the_workflow_code(env: WorkflowEnvironment) -> None:
    shop, q = Shop(), queue()
    runner = TemporalRunner(env.client, q)
    async with make_worker(env.client, q, shop.registry):
        wid = await runner.start("payer", "Pay.")
        await _paused(env, runner, wid)
        await runner.decide(wid, approve={"c1": True})
        await runner.result(wid)
        history = await env.client.get_workflow_handle(wid).fetch_history()
    replayed = await Replayer(workflows=[AgentWorkflow], workflow_runner=SANDBOX).replay_workflow(
        history
    )
    assert replayed.replay_failure is None


# --- Codex review 2026-11-21 ---


class AskAboutPay:
    """A gate that stops only payments for a person."""

    name = "ask-about-pay"

    async def check(self, c: Any, ctx: Any) -> Any:
        from lite_agent import Allow, PauseFor

        return PauseFor("a person decides") if c.name == "pay" else Allow()


async def test_a_decision_for_an_answered_pause_never_reaches_a_later_one(
    env: WorkflowEnvironment,
) -> None:
    # A decision sent while the first run is going is for nothing that waits: the routed tool
    # answers the first pause. The second pause (an approval) must wait for its own decision,
    # not be answered by the stray one.
    store, tools = InMemorySessionStore(), Recording()
    agent = Agent(
        role="agent",
        model=scripted(
            call(("c1", "lookup", {"q": "B03"})), call(("c2", "pay", {"amount": 5})), say("Done.")
        ),
        tools=(
            FunctionTool("lookup", "Looks up.", {"type": "object"}, _never, external=True),
            tools.tool("pay", "receipt"),
        ),
        gates=(AskAboutPay(),),
        sessions=store,
    )
    q, host = queue(), queue()
    runner = TemporalRunner(env.client, q)
    async with (
        make_worker(env.client, q, Registry(store, {"both": agent})),
        Worker(env.client, task_queue=host, activities=[lookup]),
    ):
        wid = await runner.start("both", "Find and pay.", routes={"lookup": host})
        await runner.decide(wid, results={"c1": ToolResultData("a person's guess")})
        second = await _paused(env, runner, wid)
        assert [(c.call_id, c.reason) for c in second.waiting] == [("c2", "approval")]
        await runner.decide(wid, approve={"c2": True})
        step = await runner.result(wid)
    results = [e for e in await store.read(step.session_id) if e.type == "tool_result"]
    assert step.outcome == "completed"
    assert [r.content for r in results] == ["found B03: an office chair", "receipt"]


@activity.defn(name="lookup")
async def broken_lookup(arguments: dict[str, Any]) -> str:
    from temporalio.exceptions import ApplicationError

    raise ApplicationError("the index is down: db=secret-host", non_retryable=True)


async def test_a_host_tool_that_fails_is_an_error_result_for_the_model(
    env: WorkflowEnvironment,
) -> None:
    shop, q, host = Shop(), queue(), queue()
    runner = TemporalRunner(env.client, q)
    async with (
        make_worker(env.client, q, shop.registry),
        Worker(env.client, task_queue=host, activities=[broken_lookup]),
    ):
        wid = await runner.start("looker", "Find B03.", routes={"lookup": host})
        step = await runner.result(wid)
    results = [e for e in await shop.store.read(step.session_id) if e.type == "tool_result"]
    assert step.outcome == "completed"
    assert [(r.content, r.is_error) for r in results] == [
        ("The tool failed (ApplicationError).", True)
    ]
    assert "secret-host" not in results[0].content  # the host's message is not repeated
