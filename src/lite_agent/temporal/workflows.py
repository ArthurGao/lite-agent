"""`AgentWorkflow` (design §9): it orchestrates and decides only from activity results; the log
is read and written in the activity alone, so the workflow code stays deterministic.

    run -> paused? -> host tools on their task queues / a person's `decide` signal -> resume -> ...

Each run gets its id from the workflow (`workflow.uuid4()`, replayed identically), which is what
makes a retried activity safe. Continue-As-New carries a paused run (and any decision already
received) into the next workflow run; the next run goes on from the log.
"""

from dataclasses import replace
from datetime import timedelta
from functools import partial

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from lite_agent.temporal.activities import PERMANENT, RUN
    from lite_agent.temporal.types import (
        AgentInput,
        Call,
        Decision,
        RunInput,
        Step,
        ToolResultData,
    )

AGENT_WORKFLOW = "lite_agent.AgentWorkflow"


def still_relevant(decision: Decision | None, *, asked: set[str], step: Step) -> Decision | None:
    """What of the answers that arrived while a resume ran still stands: only those for calls
    the resumed pause asked about and the new step still waits on (a host that answered one
    pause in two signals). Anything else answered a pause that is over (a new turn may reuse a
    call id), and a host learns a new pause's ids only from `status`, which the step sets."""
    if decision is None or step.outcome != "paused":
        return None
    keep = asked & {c.call_id for c in step.waiting}
    approve = {k: v for k, v in decision.approve.items() if k in keep}
    results = {k: v for k, v in decision.results.items() if k in keep}
    return Decision(approve, results) if approve or results else None


@workflow.defn(name=AGENT_WORKFLOW)
class AgentWorkflow:
    def __init__(self) -> None:
        self.decision: Decision | None = None
        self.step: Step | None = None

    @workflow.signal
    def decide(self, decision: Decision) -> None:
        """A person's answer; answers that arrive before the run asks are kept and merged."""
        if self.decision is None:
            self.decision = decision
        else:
            self.decision.approve.update(decision.approve)
            self.decision.results.update(decision.results)

    def _answers(self, open_ids: set[str]) -> bool:
        d = self.decision
        return d is not None and bool(open_ids & (set(d.approve) | set(d.results)))

    @workflow.query
    def status(self) -> Step | None:
        """How the last run ended: what a paused session waits on."""
        return self.step

    async def _run(self, given: AgentInput, run: RunInput) -> Step:
        step: Step = await workflow.execute_activity(
            RUN,
            run,
            result_type=Step,
            start_to_close_timeout=timedelta(seconds=given.run_timeout_s),
            heartbeat_timeout=timedelta(seconds=given.heartbeat_timeout_s),
            retry_policy=RetryPolicy(maximum_attempts=5, non_retryable_error_types=list(PERMANENT)),
        )
        return step

    async def _tool(self, given: AgentInput, call: Call) -> ToolResultData:
        """A host-executed tool, run by the activity of its name on its task queue."""
        try:
            content = await workflow.execute_activity(
                call.name,
                call.arguments,
                result_type=str,
                task_queue=given.routes[call.name],
                start_to_close_timeout=timedelta(seconds=given.tool_timeout_s),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # the tool's own failure is the model's to see, by type name only (design rule 14)
            kind = type(exc.cause).__name__ if exc.cause is not None else "ActivityError"
            return ToolResultData(f"The tool failed ({kind}).", is_error=True)
        return ToolResultData(content)

    @workflow.run
    async def run(self, given: AgentInput) -> Step:
        self.decision = given.decision
        session_id = given.session_id or str(workflow.uuid4())
        runs = 0  # the runs this workflow run executed: one carried over is not counted
        if given.pending is not None:
            step = given.pending
        else:
            first = RunInput(
                agent=given.agent,
                session_id=session_id,
                run_id=str(workflow.uuid4()),
                task=given.task,
                new_session=given.session_id is None,
            )
            step = await self._run(given, first)
            runs = 1
            self.decision = None  # sent before the run paused: for nothing that waits
        while step.outcome == "paused":
            self.step = step
            # never before this workflow run did anything: one started from a carried-over pause
            # would otherwise continue as new again at once, forever
            due = runs >= given.max_runs or workflow.info().is_continue_as_new_suggested()
            if runs > 0 and due:
                carried = replace(given, task=None, session_id=session_id, pending=step)
                workflow.continue_as_new(replace(carried, decision=self.decision))
            results: dict[str, ToolResultData] = {}
            for call in step.waiting:
                if call.reason == "tool" and call.name in given.routes:
                    results[call.call_id] = await self._tool(given, call)
            approve: dict[str, bool] = {}
            open_ids = {c.call_id for c in step.waiting} - set(results)
            if open_ids:
                # a person's answer counts only for the calls this pause still waits on: one
                # meant for an answered pause, or for a call a host tool settled, is ignored
                await workflow.wait_condition(partial(self._answers, open_ids))
                assert self.decision is not None
                approve = {k: v for k, v in self.decision.approve.items() if k in open_ids}
                results.update({k: v for k, v in self.decision.results.items() if k in open_ids})
                self.decision = None  # consumed: what arrives from here on is weighed below
            resume = RunInput(
                agent=given.agent,
                session_id=session_id,
                run_id=str(workflow.uuid4()),
                approve=approve,
                results=results,
            )
            step = await self._run(given, resume)
            runs += 1
            self.decision = still_relevant(self.decision, asked=open_ids, step=step)
        self.step = step
        return step
