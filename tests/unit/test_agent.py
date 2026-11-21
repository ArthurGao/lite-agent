"""Design §3: an Agent is assembled once, checked when it is built, and unchanged afterwards."""

import dataclasses

import pytest
from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, Budget, InMemorySessionStore, InProcessRunner, Resume, RunResult
from tests.helpers import Recording, agent, events_of, kinds, say


def test_duplicate_tool_names_are_refused_when_the_agent_is_built() -> None:
    tools = Recording()
    with pytest.raises(ValueError, match="unique: get_time"):
        agent(
            AsyncFakeModel(), tools.tool("get_time"), tools.tool("get_map"), tools.tool("get_time")
        )


def test_an_agent_needs_a_role() -> None:
    with pytest.raises(ValueError, match="role"):
        Agent(role="", model=AsyncFakeModel())


def test_an_agent_cannot_be_changed_after_it_is_built() -> None:
    a = agent(AsyncFakeModel())
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.system = "Now something else."  # type: ignore[misc]
    tools = [Recording().tool("get_time")]
    built = Agent(role="agent", model=AsyncFakeModel(), tools=tools)
    tools.clear()  # the caller's list is not the agent's
    assert [t.name for t in built.tools] == ["get_time"]


def test_the_defaults_are_an_in_memory_store_an_in_process_runner_and_thirty_turns() -> None:
    a = Agent(role="agent", model=AsyncFakeModel())
    assert isinstance(a.sessions, InMemorySessionStore)
    assert isinstance(a.runner, InProcessRunner)
    assert a.budget == Budget(turns=30)
    assert a.sessions is not Agent(role="agent", model=AsyncFakeModel()).sessions  # one each


def test_run_sync_gives_the_same_result_without_an_event_loop() -> None:
    a = agent(AsyncFakeModel(agent=[say("Noon.")]))
    result = a.run_sync("Time?")
    assert isinstance(result, RunResult)
    assert (result.session_id, result.outcome, result.stop, result.text) == (
        "id1",
        "completed",
        "end_turn",
        "Noon.",
    )


async def test_every_run_opens_with_run_started_and_ids_come_from_the_agent() -> None:
    a = Agent(role="agent", model=AsyncFakeModel(agent=[say("One."), say("Two.")]))
    first = await a.run("One?")
    second = await a.run("Two?", session_id=first.session_id)
    assert len({first.session_id, first.run_id, second.run_id}) == 3
    events = await events_of(a, first.session_id)
    starts = [i for i, kind in enumerate(kinds(events)) if kind == "run_started"]
    assert starts == [1, 5]
    assert [events[i].run_id for i in starts] == [first.run_id, second.run_id]


async def test_a_custom_runner_receives_the_agent_the_session_and_the_task() -> None:
    seen: list[tuple[Agent, str | None, str | Resume]] = []

    class Spy(InProcessRunner):
        async def run(
            self,
            agent: Agent,
            session_id: str | None,
            input: str | Resume,
            *,
            run_id: str | None = None,
            new_session: bool = False,
        ) -> RunResult:
            seen.append((agent, session_id, input))
            return await super().run(agent, session_id, input)

    a = agent(AsyncFakeModel(agent=[say("Noon.")]), runner=Spy())
    await a.run("Time?")
    assert seen == [(a, None, "Time?")]
