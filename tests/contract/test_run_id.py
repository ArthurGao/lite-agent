"""A run named by its caller (`run_id`, design §3, §9): calling again with the same id never
starts a second run. A finished run is answered from the log; one cut off mid-way (a worker
that died) goes on in the same run. This is what lets a durable runner retry an activity."""

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from lite_model.testing import AsyncFakeModel

from lite_agent import FunctionTool, Resume, SeqConflict, SessionStore, ToolContext
from tests.helpers import Ask, Recording, agent, call, kinds, say

Make = Callable[..., SessionStore]


class Died(BaseException):
    """The worker process going away: not an error the loop may catch."""


class Dies(AsyncFakeModel):
    """The fake, but the process dies when it is asked for the `at`-th turn."""

    def __init__(self, at: int, **scripts: Any) -> None:
        super().__init__(**scripts)
        self.at = at

    async def turn(self, role: str, messages: Any, tools: Any) -> Any:
        if len(self.turn_calls) + 1 == self.at:
            raise Died()
        return await super().turn(role, messages, tools)


async def test_a_named_run_in_a_named_new_session(make_store: Make) -> None:
    a = agent(AsyncFakeModel(agent=[say("Hi.")]), sessions=make_store())
    result = await a.run("Hello?", session_id="s-given", run_id="r-given", new_session=True)
    events = list(await a.sessions.read("s-given"))
    assert (result.session_id, result.run_id, result.text) == ("s-given", "r-given", "Hi.")
    assert {e.run_id for e in events} == {"r-given"}
    assert kinds(events) == [
        "session_started",
        "run_started",
        "user_message",
        "model_turn",
        "run_finished",
    ]


async def test_calling_again_after_the_run_finished_writes_nothing(make_store: Make) -> None:
    store = make_store()
    model = AsyncFakeModel(agent=[say("Hi.")])
    first = await agent(model, sessions=store).run(
        "Hello?", session_id="s1", run_id="r1", new_session=True
    )
    before = list(await store.read("s1"))

    again = await agent(model, sessions=store).run(
        "Hello?", session_id="s1", run_id="r1", new_session=True
    )

    assert list(await store.read("s1")) == before
    assert (again.outcome, again.stop, again.text, again.run_id) == (
        "completed",
        "end_turn",
        "Hi.",
        "r1",
    )
    assert again.usage == first.usage
    assert len(model.turn_calls) == 1  # no second model call


async def test_a_run_cut_off_between_turns_goes_on_in_the_same_run(make_store: Make) -> None:
    store, tools = make_store(), Recording()
    dying = Dies(2, agent=[call(("c1", "get_time", {}))])
    with pytest.raises(Died):
        await agent(dying, tools.tool("get_time", "12:00"), sessions=store).run(
            "Time?", session_id="s1", run_id="r1", new_session=True
        )
    assert kinds(await store.read("s1"))[-1] == "tool_result"

    model = AsyncFakeModel(agent=[say("Noon.")])
    done = await agent(model, tools.tool("get_time", "12:00"), sessions=store).run(
        "Time?", session_id="s1", run_id="r1", new_session=True
    )

    events = list(await store.read("s1"))
    assert (done.outcome, done.text) == ("completed", "Noon.")
    assert kinds(events).count("run_started") == 1 and kinds(events).count("user_message") == 1
    assert {e.run_id for e in events} == {"r1"}
    assert tools.ran == [("get_time", {})]  # the tool ran once


async def test_a_worker_that_died_inside_a_tool_leaves_the_call_to_reconcile(
    make_store: Make,
) -> None:
    store = make_store()
    ran: list[str] = []

    async def pay(args: Mapping[str, Any], ctx: ToolContext) -> str:
        ran.append("pay")
        if len(ran) == 1:
            raise Died()  # after tool_started was written
        return "receipt"

    pay_tool = FunctionTool("pay", "Pays.", {"type": "object"}, pay)  # not idempotent
    with pytest.raises(Died):
        await agent(AsyncFakeModel(agent=[call(("c1", "pay", {}))]), pay_tool, sessions=store).run(
            "Pay.", session_id="s1", run_id="r1", new_session=True
        )

    again = await agent(AsyncFakeModel(agent=[]), pay_tool, sessions=store).run(
        "Pay.", session_id="s1", run_id="r1", new_session=True
    )

    assert again.outcome == "paused" and again.pause is not None
    assert [w.reason for w in again.pause.waiting] == ["reconcile"]
    assert ran == ["pay"]  # never run a second time
    assert kinds(await store.read("s1")).count("run_started") == 1


async def test_a_named_resume_called_twice_resumes_once(make_store: Make) -> None:
    store, tools = make_store(), Recording()
    model = AsyncFakeModel(agent=[call(("c1", "pay", {})), say("Paid.")])
    a = agent(model, tools.tool("pay", "receipt"), gates=[Ask()], sessions=store)
    paused = await a.run("Pay.", session_id="s1", run_id="r1", new_session=True)
    assert paused.outcome == "paused"

    first = await a.resume("s1", Resume(approve={"c1": True}), run_id="r2")
    before = list(await store.read("s1"))
    second = await a.resume("s1", Resume(approve={"c1": True}), run_id="r2")

    assert (first.text, second.text, second.outcome) == ("Paid.", "Paid.", "completed")
    assert list(await store.read("s1")) == before
    assert tools.ran == [("pay", {})]


async def test_a_new_session_whose_id_is_taken_by_another_run_is_refused(make_store: Make) -> None:
    store = make_store()
    await agent(AsyncFakeModel(agent=[say("Hi.")]), sessions=store).run(
        "Hello?", session_id="s1", run_id="r1", new_session=True
    )
    with pytest.raises(SeqConflict):
        await agent(AsyncFakeModel(agent=[say("Hi.")]), sessions=store).run(
            "Hello?", session_id="s1", run_id="r2", new_session=True
        )


def test_new_session_needs_an_id_and_a_task() -> None:
    a = agent(AsyncFakeModel())
    with pytest.raises(ValueError, match="new_session"):
        a.run_sync("Hello?", new_session=True)


# --- Codex review 2026-11-21 ---


async def test_a_run_id_names_only_the_sessions_last_run(make_store: Make) -> None:
    store = make_store()
    model = AsyncFakeModel(agent=[say("One."), say("Two.")])
    a = agent(model, sessions=store)
    await a.run("First?", session_id="s1", run_id="r1", new_session=True)
    await a.run("Second?", session_id="s1", run_id="r2")
    with pytest.raises(ValueError, match="not the session's last run"):
        await a.run("First?", session_id="s1", run_id="r1")


async def test_a_run_id_with_another_task_is_refused(make_store: Make) -> None:
    store = make_store()
    a = agent(AsyncFakeModel(agent=[say("One.")]), sessions=store)
    await a.run("First?", session_id="s1", run_id="r1", new_session=True)
    with pytest.raises(ValueError, match="another task"):
        await a.run("Something else?", session_id="s1", run_id="r1", new_session=True)


async def test_two_new_sessions_racing_for_one_id_one_is_refused(make_store: Make) -> None:
    import asyncio

    store = make_store()
    runs = [
        agent(AsyncFakeModel(agent=[say("Hi.")]), sessions=store).run(
            "Hello?", session_id="s1", run_id=f"r{i}", new_session=True
        )
        for i in (1, 2)
    ]
    outcomes = await asyncio.gather(*runs, return_exceptions=True)
    assert sorted(type(o).__name__ for o in outcomes) == ["RunResult", "_Taken"]
    assert any(isinstance(o, SeqConflict) for o in outcomes)


async def test_a_resume_that_died_part_way_through_its_calls_settles_only_the_rest(
    make_store: Make,
) -> None:
    store = make_store()
    ran: list[str] = []

    def tool(name: str) -> FunctionTool:
        async def fn(args: Mapping[str, Any], ctx: ToolContext) -> str:
            ran.append(name)
            if name == "second" and ran.count("second") == 1:
                raise Died()
            return f"{name} done"

        return FunctionTool(name, name, {"type": "object"}, fn)

    tools = (tool("first"), tool("second"))
    both = call(("c1", "first", {}), ("c2", "second", {}))
    a = agent(AsyncFakeModel(agent=[both]), *tools, gates=[Ask()], sessions=store)
    assert (
        await a.run("Do both.", session_id="s1", run_id="r1", new_session=True)
    ).outcome == "paused"
    decision = Resume(approve={"c1": True, "c2": True})
    with pytest.raises(Died):
        await a.resume("s1", decision, run_id="r2")

    idempotent = tuple(
        FunctionTool(t.name, t.name, {"type": "object"}, t.fn, idempotent=True) for t in tools
    )
    b = agent(AsyncFakeModel(agent=[say("Both done.")]), *idempotent, gates=[Ask()], sessions=store)
    done = await b.resume("s1", decision, run_id="r2")

    assert (done.outcome, done.text) == ("completed", "Both done.")
    assert ran == ["first", "second", "second"]  # first never again; second, safe to rerun, again
    assert kinds(await store.read("s1")).count("resumed") == 1
