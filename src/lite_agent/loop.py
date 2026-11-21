"""One run: the tool loop over the log (design §5). Every step appends events and the next
request is derived from them; the loop keeps no message list of its own."""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Literal

from lite_model import AssistantTurn, CallFailed, ModelError, ToolCall, Usage

from lite_agent.budget import add_usage, spent
from lite_agent.compaction import SUMMARY, Compactor, clean
from lite_agent.errors import HarnessError, StoreError, UnknownSession, UnresolvedCalls
from lite_agent.events import (
    Compaction,
    ErrorKind,
    Event,
    ModelErrorEvent,
    ModelSwitched,
    ModelTurn,
    Origin,
    Outcome,
    Paused,
    Resumed,
    RunFinished,
    RunStarted,
    SessionStarted,
    StopReason,
    ToolDef,
    ToolResultEvent,
    ToolStarted,
    UserMessage,
    blocks_of,
    provenance_data,
    usage_data,
)
from lite_agent.gates import Allow, Deny, PauseFor, RunContext
from lite_agent.projection import Projection, derive, history, last_turn, unanswered_calls
from lite_agent.result import Pause, Resume, RunError, RunResult, ToolOutcome, Waiting
from lite_agent.tools import Tool, ToolContext

if TYPE_CHECKING:
    from lite_agent.agent import Agent

TRUNCATED = (
    "The output was cut off at the length limit, so the arguments may be incomplete. "
    "Nothing was executed: send the call again."
)
UNKNOWN_TOOL = "There is no tool named {name!r}. Nothing was executed."
BAD_ARGUMENTS = (
    "The arguments were not a valid JSON object. Nothing was executed: send the call again."
)
TIMED_OUT = (
    "The tool did not finish within {seconds:g} seconds and was stopped. It may or may not have "
    "taken effect: check before trying it again."
)
CUT = "\n[truncated: the first {kept} of {total} characters]"
NOT_APPROVED = "The call was not approved. Nothing was executed."
DENIED = "The call was refused: {reason} Nothing was executed."
GO_ON = "Your reply was cut off at the output limit. Continue exactly where it stopped."
GATE_FAILED = "The gate {gate!r} could not decide ({error}). Nothing was executed."

# An event still missing its envelope: `partial(RunFinished, outcome=..., ...)`. `_Run.append`
# supplies session, seq, time and run when the batch is written.
Pending = Callable[..., Event]


class _Run:
    def __init__(self, agent: "Agent", session_id: str, run_id: str) -> None:
        self.agent = agent
        self.session_id = session_id
        self.run_id = run_id
        self.log: list[Event] = []  # what this run believes the session holds
        self.checked = 0  # the last seq the store was seen to hold exactly as this run does
        self.opened = 0  # the seq of this run's run_started: what follows it is this run's
        self.usage = Usage()

    # --- the store: the only two places it is touched ---

    async def read(self, after_seq: int = 0) -> list[Event]:
        """UnknownSession is the caller's error and passes; any other failure is the store's."""
        try:
            return list(await self.agent.sessions.read(self.session_id, after_seq=after_seq))
        except UnknownSession:
            raise
        except Exception as exc:
            raise StoreError(f"read failed: {type(exc).__name__}") from exc

    def stamp(self, *pending: Pending, after: int | None = None) -> list[Event]:
        """Give pending events their envelope: this session and run, now, and the seqs that
        follow `after` (default: the log's tail)."""
        tail = after if after is not None else (self.log[-1].seq if self.log else 0)
        time = self.agent.clock()
        return [
            make(session_id=self.session_id, seq=tail + offset, time=time, run_id=self.run_id)
            for offset, make in enumerate(pending, start=1)
        ]

    async def write(self, batch: list[Event]) -> None:
        """Append one batch, all or nothing. A store that fails ends the run (§4)."""
        tail = self.log[-1].seq if self.log else 0
        try:
            await self.agent.sessions.append(self.session_id, batch, expected_seq=tail)
        except Exception as exc:
            raise StoreError(f"append failed: {type(exc).__name__}") from exc
        self.log += batch

    async def append(self, *pending: Pending) -> None:
        await self.write(self.stamp(*pending))

    # --- what is derived from the log ---

    async def projection(self) -> Projection:
        """The next request, and the invariant that guards it (§4): the store holds exactly the
        events this run wrote since it last checked, so the request is the projection of what
        the store holds. Only that tail is read; what was checked before cannot change, since
        stores never rewrite (the PostgreSQL store refuses it in the database). A store that
        lost or changed an event stops the run here."""
        mine = [e for e in self.log if e.seq > self.checked]
        if await self.read(after_seq=self.checked) != mine:
            raise HarnessError("the store does not hold the events this run wrote")
        self.checked = self.log[-1].seq if self.log else 0
        return derive(self.log)

    # What a run has done is read from the log: the events after its own `run_started`. A run
    # resumed elsewhere therefore starts clean, on the primary role and with its own
    # continuations (§5 rules 3 and 11); run ids play no part, so a host may reuse them.

    def switched(self) -> bool:
        return any(isinstance(e, ModelSwitched) and e.seq > self.opened for e in self.log)

    def role(self) -> str:
        """The role the next call goes to: the fallback once this run has switched."""
        fallback = self.agent.fallback_role
        return fallback if fallback is not None and self.switched() else self.agent.role

    def may_continue(self) -> bool:
        """Whether this run may still send the model on by itself (§5 rule 3)."""
        sent = sum(
            isinstance(e, UserMessage) and e.seq > self.opened and e.origin == "auto_continue"
            for e in self.log
        )
        return sent < self.agent.budget.auto_continues

    def still_open(self, log: tuple[Event, ...]) -> str | None:
        """What the host's completion condition says is left to do; None when the task is done
        or there is no condition. A wrong kind of answer is the host's error and raises."""
        if self.agent.done is None:
            return None
        answer = self.agent.done(log)
        if answer is None:
            return None
        if not isinstance(answer, str):
            raise TypeError(f"Agent.done must return text or None, not {type(answer).__name__}")
        return answer if answer.strip() else None

    def over_budget(self) -> StopReason | None:
        """The budget the session has used up, if any: checked before a model call, so the call
        that would exceed it is never made (§5 rule 6)."""
        budget, used = self.agent.budget, spent(self.log)
        if used.turns >= budget.turns:
            return "budget_turns"
        if budget.tokens is not None and used.tokens >= budget.tokens:
            return "budget_tokens"
        if budget.cost_usd is not None and used.cost_usd >= budget.cost_usd:
            return "budget_cost"
        return None

    def answer(
        self,
        call: ToolCall,
        content: str,
        *,
        error: ErrorKind | None = None,
        executed: bool,
        terminal: bool = False,
        origin: Origin = "executed",
    ) -> Pending:
        """The one place a tool result is built, executed or not. A result longer than the
        budget is cut here, before it is written, so the log holds exactly what the model is
        shown (§4)."""
        total, limit = len(content), self.agent.budget.tool_result_chars
        if total > limit:
            content = content[:limit] + CUT.format(kept=limit, total=total)
        return partial(
            ToolResultEvent,
            call_id=call.id,
            content=content,
            is_error=error is not None,
            error_kind=error,
            executed=executed,
            origin=origin,
            terminal=terminal,
            original_chars=total,
        )

    # --- steps ---

    def result(
        self,
        outcome: Outcome,
        stop: StopReason,
        *,
        text: str | None = None,
        error: RunError | None = None,
        pause: Pause | None = None,
    ) -> RunResult:
        return RunResult(
            self.session_id,
            self.run_id,
            outcome,
            stop,
            text,
            error,
            self.usage,
            pause=pause,
            switched=self.switched(),
        )

    async def finish(
        self,
        outcome: Outcome,
        stop: StopReason,
        *,
        text: str | None = None,
        error: RunError | None = None,
        pause: Pause | None = None,
        after: Event | None = None,
    ) -> RunResult:
        """Close the run. `after` is a stamped event that has not been written yet: it goes in
        the same batch, so the log never holds it without the run's end."""
        closing = partial(RunFinished, outcome=outcome, stop=stop, usage=usage_data(self.usage))
        if after is None:
            await self.append(closing)
        else:
            await self.write([after, *self.stamp(closing, after=after.seq)])
        return self.result(outcome, stop, text=text, error=error, pause=pause)

    # --- one tool call (§8) ---

    async def refuse(self, call: ToolCall, why: str, kind: ErrorKind = "invalid_args") -> bool:
        """Answer a call that will not be executed; nothing was started."""
        await self.append(self.answer(call, why, error=kind, executed=False))
        return False

    async def run_tool(self, tool: Tool, call: ToolCall, arguments: dict[str, object]) -> bool:
        """Run a started call and write its result; returns whether the result is terminal. A
        tool's exception or timeout is a result for the model, not a failure of the run (§5
        rules 7 and 10)."""
        seconds = self.agent.budget.tool_timeout_s if tool.timeout_s is None else tool.timeout_s
        try:
            async with asyncio.timeout(seconds) as deadline:
                content = await tool(arguments, ToolContext(self.session_id, self.run_id, call.id))
            if deadline.expired():  # the tool swallowed its cancellation and returned anyway
                raise TimeoutError
            if not isinstance(content, str):
                raise TypeError(f"tool returned {type(content).__name__}, not str")
        except Exception as exc:
            kind: ErrorKind = "exception"
            text = f"{type(exc).__name__}: {exc}"
            if deadline.expired():  # ours, not a TimeoutError the tool raised itself
                kind, text = "timeout", TIMED_OUT.format(seconds=seconds)
            await self.append(self.answer(call, text, error=kind, executed=True))
            return False
        await self.append(self.answer(call, content, executed=True, terminal=tool.terminal))
        return tool.terminal

    async def recover(
        self, call: ToolCall, started: ToolStarted, outcome: ToolOutcome | None
    ) -> bool | Waiting:
        """A call an earlier run got as far as starting. With the host's `outcome` for it, write
        that; otherwise wait for it, or run it again when the tool says that is safe (§8)."""
        tool = self.agent.tool(call.name)
        if outcome is not None:
            kind: ErrorKind | None = None
            if outcome.unknown:
                kind = "outcome_unknown"
            elif outcome.is_error:
                kind = "exception"
            terminal = kind is None and tool is not None and tool.terminal
            origin: Origin = "external" if started.external else "reconciled"
            reported = self.answer(
                call, outcome.content, error=kind, executed=True, terminal=terminal, origin=origin
            )
            await self.append(reported)
            return terminal
        if started.external:
            return Waiting(call.id, "tool")
        # Safe to run again only if the tool says so and still takes these arguments; the same
        # call id is its dedup key. Anything else may have taken effect: a person checks.
        if tool is not None and tool.idempotent and tool.validate(started.arguments) is None:
            return await self.run_tool(tool, call, started.arguments)
        return Waiting(call.id, "reconcile")

    async def admit(self, call: ToolCall, approved: bool | None) -> bool | Waiting:
        """A call nothing has been done about yet: refuse it, wait for a person, or start it.
        `approved` is a person's answer from `resume()`: it stands in for the gates."""
        tool = self.agent.tool(call.name)
        if approved is False:
            return await self.refuse(call, NOT_APPROVED, "denied")
        if tool is None:
            return await self.refuse(call, UNKNOWN_TOOL.format(name=call.name))
        if call.arguments is None:
            return await self.refuse(call, BAD_ARGUMENTS)
        if (problem := tool.validate(call.arguments)) is not None:
            return await self.refuse(call, problem)
        if approved is None and self.agent.gates:
            # built per call: a gate sees what the calls before this one did (§5 rule 10)
            context = RunContext(self.session_id, self.run_id, tuple(self.log))
            for gate in self.agent.gates:
                # Fail closed: a gate that raises, or answers with anything but a decision, has
                # not given permission. The call is refused and the run goes on (§5 rule 7).
                try:
                    decision = await gate.check(call, context)
                    failure = (
                        None if isinstance(decision, Allow | Deny | PauseFor) else "no decision"
                    )
                except Exception as exc:
                    decision, failure = None, type(exc).__name__
                if failure is not None:
                    why = GATE_FAILED.format(gate=gate.name, error=failure)
                    return await self.refuse(call, why, "denied")
                if isinstance(decision, Deny):
                    return await self.refuse(call, DENIED.format(reason=decision.reason), "denied")
                if isinstance(decision, PauseFor):
                    return Waiting(call.id, "approval", decision.reason, gate.name)
        arguments = dict(call.arguments)
        await self.append(
            partial(
                ToolStarted,
                call_id=call.id,
                name=call.name,
                arguments=arguments,
                external=tool.external,
            )
        )
        if tool.external:
            return Waiting(call.id, "tool")
        return await self.run_tool(tool, call, arguments)

    async def settle_turn(self, given: Resume | None = None) -> RunResult | None:
        """Settle every call of the last turn that has no result yet, in order: the same steps
        for a fresh turn and for a resumed one. A call that must wait does not hold up the
        others; if any waits, the run ends as paused. `given` is what `resume()` brought for
        this turn's calls and reaches no later turn. Returns the run's result, or None to go on
        with the next model call."""
        given = given or Resume()
        turn = last_turn(self.log)
        assert turn is not None
        terminal = {call_id: result.terminal for call_id, result in turn.results.items()}
        waiting: list[Waiting] = []
        for call in turn.calls:
            if call.id in terminal:
                continue
            approved = given.approve.get(call.id)
            if (started := turn.started.get(call.id)) is not None:
                settled = await self.recover(call, started, given.results.get(call.id))
            elif approved is None and call.id in turn.awaiting:
                settled = turn.awaiting[call.id]  # still a person's to decide: no gate is asked
            else:
                settled = await self.admit(call, approved)
            if isinstance(settled, Waiting):
                waiting.append(settled)
            else:
                terminal[call.id] = settled
        text = turn.text or None
        if waiting:
            await self.append(partial(Paused, waiting=tuple(waiting)))
            return await self.finish("paused", "paused", text=text, pause=Pause(tuple(waiting)))
        if all(terminal.values()):  # §5 rule 2: the whole batch, or go on
            return await self.finish("completed", "terminal_tool", text=text)
        return None

    def failure(
        self, exc: ModelError, role: str, purpose: Literal["turn", "compact"] = "turn"
    ) -> Pending:
        """A model call that failed, as the event to write; what it spent is added to the run's
        usage here, so a failed call is billed wherever it happens (§5 rule 6)."""
        self.usage = add_usage(self.usage, exc.usage)
        usage = usage_data(exc.usage) if exc.usage is not None else None
        return partial(
            ModelErrorEvent, error=type(exc).__name__, role=role, purpose=purpose, usage=usage
        )

    async def compact(self, compactor: Compactor, cut: int) -> RunResult | None:
        """Have the compact role summarise the history up to `cut` and record that the summary
        stands for it (§6). Returns the run's result if the summary could not be written: a run
        whose history is too long is not sent on as it is, to fail again less clearly."""
        agent = self.agent
        ask = compactor.prompt(history([e for e in self.log if e.seq <= cut]))
        try:
            reply = await agent.model.turn(agent.compact_role, ask, ())
        except ModelError as exc:
            await self.append(self.failure(exc, agent.compact_role, "compact"))
            error = RunError("model", type(exc).__name__)
            return await self.finish("failed", "error", error=error)
        self.usage = add_usage(self.usage, reply.usage)
        summary = clean(reply.turn.text)
        if reply.stop == "length" or not summary:
            # half a summary, or none, must not stand for the whole history: the call is recorded
            # as a failed one (it was billed) and the run ends, as for any summary not written
            name = "TruncatedSummary" if reply.stop == "length" else "EmptySummary"
            cut_off = partial(
                ModelErrorEvent,
                error=name,
                role=agent.compact_role,
                purpose="compact",
                usage=usage_data(reply.usage),
            )
            await self.append(cut_off)
            return await self.finish("failed", "error", error=RunError("model", name))
        first = next(e.seq for e in self.log if isinstance(e, UserMessage | ModelTurn))
        await self.append(
            partial(
                Compaction,
                summary=SUMMARY.format(summary=summary),
                covers=(first, cut),
                role=agent.compact_role,
                provenance=provenance_data(reply.provenance),
                usage=usage_data(reply.usage),
            )
        )
        return None

    async def loop(self) -> RunResult:
        agent = self.agent
        while True:
            if (used_up := self.over_budget()) is not None:
                return await self.finish("failed", used_up)
            request = await self.projection()
            role = self.role()
            compactor = agent.compaction
            if compactor is not None and compactor.due(request, role):
                # at most once before a call: a compactor that is always due cannot loop
                cut = compactor.cut(self.log)
                if cut is not None:  # nothing it may cut: the call goes out as it is
                    if (ended := await self.compact(compactor, cut)) is not None:
                        return ended
                    if (used_up := self.over_budget()) is not None:  # the summary was paid for
                        return await self.finish("failed", used_up)
                    request = await self.projection()
            try:
                reply = await agent.model.turn(role, request.messages, request.tools)
            except ModelError as exc:
                # after lite-model's own retries on the same model (§5 rule 9)
                failed, name = self.failure(exc, role), type(exc).__name__
                fallback = agent.fallback_role
                if isinstance(exc, CallFailed) and fallback is not None and not self.switched():
                    # §5 rule 11: only a model that could not be reached, and once per run. The
                    # failure and the switch are written together; the turn is sent again.
                    switch = partial(ModelSwitched, from_role=role, to_role=fallback, reason=name)
                    await self.append(failed, switch)
                    continue
                await self.append(failed)
                return await self.finish("failed", "error", error=RunError("model", name))
            self.usage = add_usage(self.usage, reply.usage)
            turn = partial(
                ModelTurn,
                blocks=blocks_of(reply.turn),
                stop=reply.stop,
                role=role,
                provenance=provenance_data(reply.provenance),
                usage=usage_data(reply.usage),
            )
            calls = reply.turn.tool_calls
            text = reply.turn.text or None
            if len({c.id for c in calls}) != len(calls):
                # results are matched to calls by id: two calls with one id cannot both be
                # answered, so the turn is unusable and nothing of it is recorded as a turn
                usage = usage_data(reply.usage)
                name = "DuplicateCallIds"
                await self.append(partial(ModelErrorEvent, error=name, role=role, usage=usage))
                return await self.finish("failed", "error", error=RunError("model", name))
            if reply.stop == "length" and calls:
                # §5 rule 1: none of a cut-off turn's calls runs, and each still gets its result
                refusals = (
                    self.answer(c, TRUNCATED, error="truncated_turn", executed=False) for c in calls
                )
                await self.append(turn, *refusals)
                continue
            if calls:
                await self.append(turn)
                if (ended := await self.settle_turn()) is not None:
                    return ended
                continue
            # A text-only turn. Cut off: ask it to go on. Otherwise it is a report, and the
            # host's completion condition, shown the log with this turn in it, says whether the
            # task is done (§5 rule 3). The turn is written together with what follows it (the
            # message that sends it back, or the run's end): a crash in between would leave a
            # session that looks finished and is not.
            cut = reply.stop == "length"
            (said,) = self.stamp(turn)
            still_open = GO_ON if cut else self.still_open((*self.log, said))
            if still_open is None:
                return await self.finish("completed", "end_turn", text=text, after=said)
            if self.may_continue():
                again = partial(UserMessage, text=still_open, origin="auto_continue")
                await self.write([said, *self.stamp(again, after=said.seq)])
                continue
            # still open, and the run has used its continuations: a person looks at it
            stop: StopReason = "truncated" if cut else "incomplete"
            return await self.finish("failed", stop, text=text, after=said)


def _check_resume(log: list[Event], given: Resume) -> None:
    """Raises ValueError, before anything is written, for a `Resume` that does not fit the
    session: a decision or a result for a call that is not waiting for one, or nothing to do."""
    turn = last_turn(log)
    waiting = turn.unanswered if turn is not None else ()
    started = turn.started if turn is not None else {}
    awaiting = turn.awaiting if turn is not None else {}
    for call_id in given.approve:
        # An approval stands in for the gates, so it may only answer a question a gate asked:
        # the call must be one the log records as waiting for a person.
        if call_id not in waiting or call_id in started or call_id not in awaiting:
            raise ValueError(f"call {call_id} is not waiting for approval")
    for call_id in given.results:
        if call_id not in waiting or call_id not in started:
            raise ValueError(f"call {call_id} is not waiting for a result")
    if not waiting:
        # Nothing to settle: only a run that failed or was interrupted before its next model
        # call can be sent again. A run that completed is over, and a history that ends with
        # the assistant's own words has nothing left to answer.
        done = log and isinstance(log[-1], RunFinished) and log[-1].outcome == "completed"
        last = derive(log).messages[-1:]
        if done or not last or isinstance(last[0], AssistantTurn):
            raise ValueError("nothing to resume: the session's last run is complete")


async def run_loop(agent: "Agent", session_id: str | None, input: str | Resume) -> RunResult:
    """Run a task in a new session (`session_id=None`) or as the next run of an existing one, or
    resume a session (design §8). Raises UnknownSession; UnresolvedCalls for a task sent to a
    session whose last turn still has unanswered calls; ValueError for a `Resume` that does not
    fit. A failure of the harness itself (the store, the projection invariant) ends the run
    without another write: the log is exactly what was stored before it."""
    given = input if isinstance(input, Resume) else None
    if given is not None and session_id is None:
        raise ValueError("resume() needs a session")
    run = _Run(agent, session_id or agent.ids(), agent.ids())
    tools = tuple(ToolDef.model_validate(t, from_attributes=True) for t in agent.all_tools)
    opening: list[Pending] = [
        partial(
            RunStarted,
            role=agent.role,
            fallback_role=agent.fallback_role,
            system=agent.system_prompt(),
            tools=tools,
        )
    ]
    try:
        if session_id is None:
            opening.insert(0, SessionStarted)
        else:
            run.log = await run.read()
            run.checked = run.log[-1].seq if run.log else 0  # read whole: all of it is checked
        open_calls = unanswered_calls(run.log)
        if given is not None:
            _check_resume(run.log, given)
            opening.append(
                partial(Resumed, approve=dict(given.approve), results=tuple(given.results))
            )
        elif open_calls:
            raise UnresolvedCalls(run.session_id, open_calls)
        else:
            assert isinstance(input, str)
            opening.append(partial(UserMessage, text=input))
        await run.append(*opening)
        run.opened = next(e.seq for e in reversed(run.log) if isinstance(e, RunStarted))
        if open_calls and (ended := await run.settle_turn(given)) is not None:
            return ended
        return await run.loop()
    except UnknownSession:
        raise
    except StoreError as exc:
        error = RunError("store", type(exc.__cause__ or exc).__name__)
    except HarnessError as exc:
        error = RunError("harness", type(exc).__name__)
    return run.result("failed", "error", error=error)
