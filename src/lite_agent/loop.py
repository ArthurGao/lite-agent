"""One run: the tool loop over the log (design §5). Every step appends events and the next
request is derived from them; the loop keeps no message list of its own."""

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from lite_model import ModelError, ToolCall, Usage

from lite_agent.budget import add_usage
from lite_agent.errors import HarnessError, StoreError, UnknownSession, UnresolvedCalls
from lite_agent.events import (
    ErrorKind,
    Event,
    ModelErrorEvent,
    ModelTurn,
    Outcome,
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
from lite_agent.projection import Projection, derive, unanswered_calls
from lite_agent.result import RunError, RunResult
from lite_agent.tools import ToolContext

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

# An event still missing its envelope: `partial(RunFinished, outcome=..., ...)`. `_Run.append`
# supplies session, seq, time and run when the batch is written.
Pending = Callable[..., Event]


def _answer(
    call: ToolCall,
    content: str,
    *,
    error: ErrorKind | None = None,
    executed: bool,
    terminal: bool = False,
) -> Pending:
    """The one place a tool result is built, executed or not."""
    return partial(
        ToolResultEvent,
        call_id=call.id,
        content=content,
        is_error=error is not None,
        error_kind=error,
        executed=executed,
        terminal=terminal,
        original_chars=len(content),
    )


class _Run:
    def __init__(self, agent: "Agent", session_id: str, run_id: str) -> None:
        self.agent = agent
        self.session_id = session_id
        self.run_id = run_id
        self.log: list[Event] = []  # what this run believes the session holds
        self.usage = Usage()

    # --- the store: the only two places it is touched ---

    async def read(self) -> list[Event]:
        """UnknownSession is the caller's error and passes; any other failure is the store's."""
        try:
            return list(await self.agent.sessions.read(self.session_id))
        except UnknownSession:
            raise
        except Exception as exc:
            raise StoreError(f"read failed: {type(exc).__name__}") from exc

    async def append(self, *pending: Pending) -> None:
        """Append one batch, all or nothing. A store that fails ends the run (§4)."""
        tail = self.log[-1].seq if self.log else 0
        time = self.agent.clock()
        batch = [
            make(session_id=self.session_id, seq=tail + offset, time=time, run_id=self.run_id)
            for offset, make in enumerate(pending, start=1)
        ]
        try:
            await self.agent.sessions.append(self.session_id, batch, expected_seq=tail)
        except Exception as exc:
            raise StoreError(f"append failed: {type(exc).__name__}") from exc
        self.log += batch

    # --- what is derived from the log ---

    async def projection(self) -> Projection:
        """The next request, and the invariant that guards it: what the store holds projects to
        exactly what this run built (§4). A store that lost or changed an event stops here."""
        built = derive(self.log)
        if derive(await self.read()) != built:
            raise HarnessError("the stored log does not project to the request this run built")
        return built

    def turns_used(self) -> int:
        """Model calls the session has made, failed ones included (§5 rule 6)."""
        return sum(isinstance(e, ModelTurn | ModelErrorEvent) for e in self.log)

    # --- steps ---

    def result(
        self,
        outcome: Outcome,
        stop: StopReason,
        *,
        text: str | None = None,
        error: RunError | None = None,
    ) -> RunResult:
        return RunResult(self.session_id, self.run_id, outcome, stop, text, error, self.usage)

    async def finish(
        self,
        outcome: Outcome,
        stop: StopReason,
        *,
        text: str | None = None,
        error: RunError | None = None,
    ) -> RunResult:
        await self.append(
            partial(RunFinished, outcome=outcome, stop=stop, usage=usage_data(self.usage))
        )
        return self.result(outcome, stop, text=text, error=error)

    async def execute(self, call: ToolCall) -> bool:
        """One call: refused without a `tool_started`, or started, run and answered (§8). Returns
        whether its result is terminal. A tool's exception is a result for the model, not a
        failure of the run (§5 rule 7)."""
        tool = self.agent.tool(call.name)
        if tool is None:
            content = UNKNOWN_TOOL.format(name=call.name)
            await self.append(_answer(call, content, error="invalid_args", executed=False))
            return False
        if call.arguments is None:
            await self.append(_answer(call, BAD_ARGUMENTS, error="invalid_args", executed=False))
            return False
        arguments = dict(call.arguments)
        await self.append(
            partial(ToolStarted, call_id=call.id, name=call.name, arguments=arguments)
        )
        try:
            content = await tool(arguments, ToolContext(self.session_id, self.run_id, call.id))
            if not isinstance(content, str):
                raise TypeError(f"tool returned {type(content).__name__}, not str")
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            await self.append(_answer(call, failure, error="exception", executed=True))
            return False
        await self.append(_answer(call, content, executed=True, terminal=tool.terminal))
        return tool.terminal

    async def loop(self) -> RunResult:
        agent = self.agent
        while True:
            if self.turns_used() >= agent.budget.turns:  # before the call that would exceed it
                return await self.finish("failed", "budget_turns")
            request = await self.projection()
            try:
                reply = await agent.model.turn(agent.role, request.messages, request.tools)
            except ModelError as exc:
                # after lite-model's own retries on the same model; no fallback here (§5 rule 9)
                self.usage = add_usage(self.usage, exc.usage)
                name = type(exc).__name__
                usage = usage_data(exc.usage) if exc.usage is not None else None
                await self.append(
                    partial(ModelErrorEvent, error=name, role=agent.role, usage=usage)
                )
                return await self.finish("failed", "error", error=RunError("model", name))
            self.usage = add_usage(self.usage, reply.usage)
            turn = partial(
                ModelTurn,
                blocks=blocks_of(reply.turn),
                stop=reply.stop,
                role=agent.role,
                provenance=provenance_data(reply.provenance),
                usage=usage_data(reply.usage),
            )
            calls = reply.turn.tool_calls
            text = reply.turn.text or None
            if reply.stop == "length" and calls:
                # §5 rule 1: none of a cut-off turn's calls runs, and each still gets its result
                refusals = (
                    _answer(c, TRUNCATED, error="truncated_turn", executed=False) for c in calls
                )
                await self.append(turn, *refusals)
                continue
            await self.append(turn)
            if reply.stop == "length":  # cut-off text: nothing to answer, no way to go on yet (S2)
                return await self.finish("failed", "truncated", text=text)
            if not calls:
                return await self.finish("completed", "end_turn", text=text)
            # in order, one at a time (design §16); §5 rule 2: the whole batch, or go on
            if all([await self.execute(c) for c in calls]):
                return await self.finish("completed", "terminal_tool", text=text)


async def run_loop(agent: "Agent", session_id: str | None, task: str) -> RunResult:
    """Run `task` in a new session (`session_id=None`) or as the next run of an existing one.
    Raises UnknownSession, or UnresolvedCalls for a session an interrupted run left mid-call.
    A failure of the harness itself (the store, the projection invariant) ends the run without
    another write: the log is exactly what was stored before it."""
    run = _Run(agent, session_id or agent.ids(), agent.ids())
    tools = tuple(ToolDef.model_validate(t, from_attributes=True) for t in agent.tools)
    opening: list[Pending] = [
        partial(RunStarted, role=agent.role, system=agent.system, tools=tools),
        partial(UserMessage, text=task),
    ]
    try:
        if session_id is None:
            opening.insert(0, SessionStarted)
        else:
            run.log = await run.read()
            if open_calls := unanswered_calls(run.log):
                # an interrupted run: the caller's to settle, like UnknownSession (§8)
                raise UnresolvedCalls(run.session_id, open_calls)
        await run.append(*opening)
        return await run.loop()
    except UnknownSession:
        raise
    except StoreError as exc:
        error = RunError("store", type(exc.__cause__ or exc).__name__)
    except HarnessError as exc:
        error = RunError("harness", type(exc).__name__)
    return run.result("failed", "error", error=error)
