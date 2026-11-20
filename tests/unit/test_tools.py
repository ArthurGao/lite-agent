"""Design §7, §5 rules 6 and 10 (stage S2a): `@tool` validates arguments before anything is
written or run, results are cut to the budget when they are written, a tool that overruns its
time is answered as a timeout, and the token and cost budgets stop a run before the next call."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from lite_model import CallFailed, EmptyReply, ToolResult, ToolResults, Usage
from lite_model.testing import AsyncFakeModel
from pydantic import BaseModel, Field

from lite_agent import Budget, FunctionTool, ToolContext, tool
from lite_agent.events import RunFinished
from tests.helpers import Billed, Recording, agent, call, events_of, kinds, results_of, say


class Weather(BaseModel):
    city: str
    days: int = Field(default=1, ge=1, le=7)


SEEN: list[tuple[Weather, ToolContext]] = []


@tool(description="Forecast for a city.")
async def get_weather(args: Weather, ctx: ToolContext) -> str:
    SEEN.append((args, ctx))
    return f"{args.city}: sunny for {args.days} day(s)"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    SEEN.clear()


# --- @tool ---


def test_tool_takes_its_name_schema_and_flags_from_the_function_and_its_model() -> None:
    assert (get_weather.name, get_weather.description) == ("get_weather", "Forecast for a city.")
    assert get_weather.parameters == Weather.model_json_schema()
    assert get_weather.parameters["required"] == ["city"]
    assert (get_weather.terminal, get_weather.idempotent, get_weather.external) == (
        False,
        False,
        False,
    )
    assert get_weather.timeout_s is None

    @tool(description="Submit.", name="submit", terminal=True, idempotent=True, timeout_s=5)
    async def finish(args: Weather, ctx: ToolContext) -> str:
        return "ok"

    assert (finish.name, finish.terminal, finish.idempotent, finish.timeout_s) == (
        "submit",
        True,
        True,
        5,
    )


def test_tool_refuses_a_function_whose_first_parameter_is_not_a_pydantic_model() -> None:
    with pytest.raises(TypeError, match="must be a pydantic BaseModel"):

        @tool(description="Bad.")  # type: ignore[type-var]
        async def bad(args: dict[str, Any], ctx: ToolContext) -> str:
            return "x"


def test_validate_says_what_is_wrong_without_echoing_the_input() -> None:
    assert get_weather.validate({"city": "Paris"}) is None
    problem = get_weather.validate({"days": 99, "secret": "sk-live-123"})
    assert problem is not None
    assert "city: Field required" in problem
    assert "days: Input should be less than or equal to 7" in problem
    assert "sk-live-123" not in problem and "99" not in problem


def test_a_function_tool_has_the_new_flags_with_safe_defaults_and_does_not_validate() -> None:
    plain = Recording().tool("plain")
    assert isinstance(plain, FunctionTool)
    assert (plain.idempotent, plain.external, plain.timeout_s) == (False, False, None)
    assert plain.validate({"anything": object()}) is None


# --- validation in the loop ---


async def test_valid_arguments_reach_the_function_as_the_model() -> None:
    model = AsyncFakeModel(
        agent=[call(("c1", "get_weather", {"city": "Paris", "days": 2})), say("Ok.")]
    )
    a = agent(model, get_weather)
    await a.run("Forecast?")
    ((args, ctx),) = SEEN
    assert args == Weather(city="Paris", days=2)
    assert ctx.call_id == "c1"
    assert model.turn_calls[1].messages[-1] == ToolResults(
        (ToolResult("c1", "Paris: sunny for 2 day(s)"),)
    )


async def test_invalid_arguments_are_answered_before_anything_is_started_or_run() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "get_weather", {"days": 0})), say("Sorry.")])
    a = agent(model, get_weather)

    result = await a.run("Forecast?")

    assert (result.outcome, result.text) == ("completed", "Sorry.")
    events = await events_of(a, "id1")
    assert kinds(events)[3:5] == ["model_turn", "tool_result"]  # no tool_started
    (refused,) = results_of(events)
    assert (refused.error_kind, refused.executed, refused.is_error) == ("invalid_args", False, True)
    assert "city: Field required" in refused.content
    assert "days: Input should be greater than or equal to 1" in refused.content
    assert SEEN == []
    sent = model.turn_calls[1].messages[-1]
    assert isinstance(sent, ToolResults) and sent.results[0].is_error


# --- results are cut to the budget when written ---


async def test_a_long_result_is_cut_when_written_and_the_original_length_is_kept() -> None:
    long = "x" * 50 + "TAIL"
    model = AsyncFakeModel(agent=[call(("c1", "dump", {})), say("Done.")])
    a = agent(model, Recording().tool("dump", long), budget=Budget(tool_result_chars=50))

    await a.run("Dump it.")

    (written,) = results_of(await events_of(a, "id1"))
    assert written.content == "x" * 50 + "\n[truncated: the first 50 of 54 characters]"
    assert (written.original_chars, written.is_error) == (54, False)
    sent = model.turn_calls[1].messages[-1]
    assert isinstance(sent, ToolResults)
    assert sent.results[0].content == written.content  # the model sees what the log holds
    assert "TAIL" not in sent.results[0].content


async def test_a_result_within_the_budget_is_written_as_it_is() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "dump", {})), say("Done.")])
    a = agent(model, Recording().tool("dump", "y" * 50), budget=Budget(tool_result_chars=50))
    await a.run("Dump it.")
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.content, written.original_chars) == ("y" * 50, 50)


async def test_a_long_exception_text_is_cut_too() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "dump", {})), say("Done.")])
    boom = Recording().tool("dump", RuntimeError("z" * 200))
    a = agent(model, boom, budget=Budget(tool_result_chars=40))
    await a.run("Dump it.")
    (written,) = results_of(await events_of(a, "id1"))
    assert written.content.startswith("RuntimeError: zzz")
    assert written.content.endswith("[truncated: the first 40 of 214 characters]")
    assert (written.error_kind, written.original_chars) == ("exception", 214)


# --- §5 rule 10: a tool that overruns its time ---


async def test_a_tool_that_overruns_its_timeout_is_answered_as_a_timeout_and_the_run_goes_on() -> (
    None
):
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "slow", {})), say("It hung.")])
    a = agent(model, tools.tool("slow", delay=30, timeout_s=0.01))

    result = await a.run("Go.")

    assert (result.outcome, result.text) == ("completed", "It hung.")
    events = await events_of(a, "id1")
    assert kinds(events)[4:6] == ["tool_started", "tool_result"]
    (timed_out,) = results_of(events)
    assert (timed_out.error_kind, timed_out.executed, timed_out.is_error, timed_out.terminal) == (
        "timeout",
        True,
        True,
        False,
    )
    assert "may or may not have taken effect" in timed_out.content
    assert [c.call_id for c in tools.contexts] == ["c1"]  # it ran once and was not tried again


async def test_the_budgets_default_timeout_applies_to_a_tool_without_its_own() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "slow", {})), say("It hung.")])
    a = agent(model, Recording().tool("slow", delay=30), budget=Budget(tool_timeout_s=0.01))
    await a.run("Go.")
    (timed_out,) = results_of(await events_of(a, "id1"))
    assert timed_out.error_kind == "timeout"


async def test_a_tool_raising_timeout_error_itself_is_an_ordinary_failure() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "fetch", {})), say("Hm.")])
    a = agent(model, Recording().tool("fetch", TimeoutError("upstream took too long")))
    await a.run("Go.")
    (failed,) = results_of(await events_of(a, "id1"))
    assert (failed.error_kind, failed.content) == (
        "exception",
        "TimeoutError: upstream took too long",
    )


# --- §5 rule 6: token and cost budgets ---


async def test_the_token_budget_stops_before_the_call_after_it_is_reached() -> None:
    again = call(("c1", "get_time", {}))
    model = Billed(
        [Usage(input=60, output=10), Usage(input=80, output=10), Usage(input=1, output=1)],
        agent=[again, again, again],
    )
    a = agent(model, Recording().tool("get_time", "12:00"), budget=Budget(tokens=150))

    result = await a.run("Time?")

    assert (result.outcome, result.stop) == ("failed", "budget_tokens")
    assert len(model.turn_calls) == 2  # 70, then 160 >= 150: the third call is not made
    last = (await events_of(a, "id1"))[-1]
    assert isinstance(last, RunFinished) and last.stop == "budget_tokens"


async def test_the_cost_budget_stops_before_the_call_after_it_is_reached() -> None:
    again = call(("c1", "get_time", {}))
    model = Billed(
        [Usage(input=1, output=1, cost_usd=0.03), Usage(input=1, output=1, cost_usd=0.03)],
        agent=[again, again, again],
    )
    a = agent(model, Recording().tool("get_time", "12:00"), budget=Budget(cost_usd=0.05))
    result = await a.run("Time?")
    assert (result.outcome, result.stop) == ("failed", "budget_cost")
    assert len(model.turn_calls) == 2


async def test_token_and_cost_budgets_are_the_sessions_and_count_failed_calls() -> None:
    model = Billed(
        [Usage(input=40, output=10)],
        agent=[
            say("One."),
            EmptyReply("agent: empty reply", usage=Usage(input=45, output=0)),  # billed, failed
            CallFailed("agent failed: TimeoutError"),  # never answered: unknown, counts as nothing
            say("never asked"),
        ],
    )
    a = agent(model, budget=Budget(tokens=95))
    first = await a.run("One?")
    second = await a.run("Two?", session_id=first.session_id)  # the billed failure: 50 + 45 = 95
    assert second.error is not None and second.error.type == "EmptyReply"

    third = await a.run("Three?", session_id=first.session_id)

    assert (third.outcome, third.stop) == ("failed", "budget_tokens")
    assert len(model.turn_calls) == 2  # the session had already spent its 95


async def test_a_call_whose_usage_is_unknown_is_not_counted_against_the_token_budget() -> None:
    model = AsyncFakeModel(agent=[CallFailed("agent failed: TimeoutError"), say("Two.")])
    a = agent(model, budget=Budget(tokens=10))
    failed = await a.run("One?")
    assert failed.usage == Usage()
    again = await a.run("Two?", session_id=failed.session_id)
    assert (again.outcome, again.text) == ("completed", "Two.")  # nothing known was spent


@pytest.mark.parametrize(
    "fields",
    [
        {"tokens": 0},
        {"cost_usd": 0},
        {"cost_usd": -1.0},
        {"tool_result_chars": 0},
        {"tool_timeout_s": 0},
        {"auto_continues": -1},
    ],
)
def test_a_budget_that_allows_nothing_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Budget(**fields)


def test_the_default_budget() -> None:
    b = Budget()
    assert (b.turns, b.tokens, b.cost_usd) == (30, None, None)
    assert (b.tool_result_chars, b.tool_timeout_s, b.auto_continues) == (20_000, 60.0, 2)


@pytest.mark.parametrize("timeout_s", [0, -1])
def test_a_tool_timeout_must_be_positive(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="timeout_s must be a positive number"):
        Recording().tool("t", timeout_s=timeout_s)
    with pytest.raises(ValueError, match="timeout_s must be a positive number"):
        tool(description="T.", timeout_s=timeout_s)(get_weather.fn)


def test_an_agent_takes_an_external_tool() -> None:
    # S2a refused these until runs could pause; S2b lifted that (tests/unit/test_pause_resume.py).
    plain = Recording().tool("plain")
    assert isinstance(plain, FunctionTool)
    remote = FunctionTool("remote", "Remote.", {"type": "object"}, plain.fn, external=True)
    assert agent(AsyncFakeModel(), remote).tools == (remote,)


async def test_a_tool_that_swallows_its_cancellation_is_still_a_timeout() -> None:
    # Codex review 2026-11-21: a tool that catches CancelledError and returns must not turn an
    # overrun into a success.
    async def stubborn(args: Mapping[str, Any], ctx: ToolContext) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass
        return "done anyway"

    late = FunctionTool("stubborn", "Stubborn.", {"type": "object"}, stubborn, timeout_s=0.01)
    model = AsyncFakeModel(agent=[call(("c1", "stubborn", {})), say("Hm.")])
    a = agent(model, late)
    await a.run("Go.")
    (written,) = results_of(await events_of(a, "id1"))
    assert (written.error_kind, written.is_error, written.terminal) == ("timeout", True, False)
    assert "done anyway" not in written.content


def test_tool_refuses_a_function_it_could_not_call() -> None:
    # Codex review 2026-11-21: fail when the tool is declared, not on its first call.
    with pytest.raises(TypeError, match="must be an async function"):

        @tool(description="Sync.")  # type: ignore[arg-type]
        def sync(args: Weather, ctx: ToolContext) -> str:
            return "x"

    with pytest.raises(TypeError, match=r"must take \(args, ctx\)"):

        @tool(description="One.")  # type: ignore[arg-type]
        async def one(args: Weather) -> str:
            return "x"


@pytest.mark.parametrize(
    "fields",
    [{"cost_usd": float("nan")}, {"cost_usd": float("inf")}, {"tool_timeout_s": float("nan")}],
)
def test_a_budget_must_be_finite(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Budget(**fields)


def test_a_tool_timeout_must_be_finite() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        Recording().tool("t", timeout_s=float("nan"))
