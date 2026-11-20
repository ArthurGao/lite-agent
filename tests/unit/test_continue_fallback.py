"""Design §5 rules 3, 9 and 11 (stage S2c): a text-only end of turn is a report, not proof the
task is done, when the host says how to tell; cut-off text is asked to go on; and a model that
cannot be reached is replaced once, visibly, by a fallback role."""

from collections.abc import Sequence
from typing import Any

import pytest
from lite_model import CallFailed, EmptyReply, Message, Usage
from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, Budget, PauseFor, Resume, RunContext, RunError
from lite_agent.events import Event, ModelErrorEvent, ModelSwitched, ModelTurn, UserMessage
from tests.helpers import Billed, FailingOnce, Recording, agent, call, events_of, kinds, say

DOWN = CallFailed("agent anthropic:claude-x failed: APITimeoutError")


def roles_asked(model: Any) -> list[str]:
    return [c.role for c in model.turn_calls]


def user_messages(events: Sequence[Event]) -> list[tuple[str, str]]:
    return [(e.origin, e.text) for e in events if isinstance(e, UserMessage)]


# --- §5 rule 3: a text-only end of turn ---


class Checklist:
    """A completion condition: the open items, one fewer each time it is asked."""

    def __init__(self, *open_items: str) -> None:
        self.open = list(open_items)
        self.seen: list[int] = []

    def __call__(self, events: Sequence[Event]) -> str | None:
        self.seen.append(len(events))
        if not self.open:
            return None
        return "Still open: " + ", ".join(self.open) + f". ({self.open.pop(0)} first.)"


async def test_an_unfinished_task_is_sent_back_with_what_is_still_open() -> None:
    done = Checklist("tests")
    model = AsyncFakeModel(agent=[say("I wrote the code."), say("Tests added.")])
    a = agent(model, done=done)

    result = await a.run("Write it and test it.")

    assert (result.outcome, result.stop, result.text) == ("completed", "end_turn", "Tests added.")
    events = await events_of(a, "id1")
    assert kinds(events)[3:] == ["model_turn", "user_message", "model_turn", "run_finished"]
    assert user_messages(events) == [
        ("task", "Write it and test it."),
        ("auto_continue", "Still open: tests. (tests first.)"),
    ]
    assert model.turn_calls[1].messages[-2:] == (
        say("I wrote the code."),
        Message("user", "Still open: tests. (tests first.)"),
    )
    assert done.seen == [4, 6]  # asked after each text-only turn, with the log up to it


async def test_automatic_continuation_stops_after_the_budgets_count() -> None:
    done = Checklist("a", "b", "c", "d")
    model = AsyncFakeModel(agent=[say("1"), say("2"), say("3"), say("never asked")])
    a = agent(model, done=done, budget=Budget(auto_continues=2))

    result = await a.run("Do it all.")

    assert (result.outcome, result.stop, result.text) == ("failed", "incomplete", "3")
    assert len(model.turn_calls) == 3  # the task, and two nudges
    assert [origin for origin, _ in user_messages(await events_of(a, "id1"))] == [
        "task",
        "auto_continue",
        "auto_continue",
    ]


async def test_without_a_completion_condition_text_ends_the_run() -> None:
    model = AsyncFakeModel(agent=[say("Done."), say("never asked")])
    result = await agent(model).run("Do it.")
    assert (result.outcome, result.stop) == ("completed", "end_turn")
    assert len(model.turn_calls) == 1


async def test_a_condition_with_nothing_to_say_means_done() -> None:
    # simplify review 2026-11-21: an empty answer must not become an empty message to the model
    model = AsyncFakeModel(agent=[say("Done."), say("never asked")])
    a = agent(model, done=lambda events: "")
    result = await a.run("Do it.")
    assert (result.outcome, result.stop) == ("completed", "end_turn")
    assert len(model.turn_calls) == 1
    assert [e.type for e in await events_of(a, "id1")].count("user_message") == 1


async def test_each_run_gets_its_own_continuations() -> None:
    done = Checklist("a", "b", "c", "d")
    model = AsyncFakeModel(agent=[say("1"), say("2"), say("3"), say("4")])
    a = agent(model, done=done, budget=Budget(auto_continues=1))
    first = await a.run("Go.")
    assert first.stop == "incomplete" and len(model.turn_calls) == 2
    second = await a.run("Go on.", session_id=first.session_id)
    assert second.stop == "incomplete" and len(model.turn_calls) == 4  # one more nudge, not zero


async def test_a_terminal_tool_ends_the_run_without_asking_whether_it_is_done() -> None:
    done = Checklist("something")
    model = AsyncFakeModel(agent=[call(("c1", "submit", {}))])
    a = agent(model, Recording().tool("submit", "accepted", terminal=True), done=done)
    result = await a.run("Solve.")
    assert (result.outcome, result.stop) == ("completed", "terminal_tool")
    assert done.seen == []


# --- cut-off text is asked to go on ---


async def test_text_cut_off_at_the_limit_is_asked_to_continue() -> None:
    model = AsyncFakeModel(agent=[(say("The answer is"), "length"), say(" 42.")])
    a = agent(model)

    result = await a.run("Explain.")

    assert (result.outcome, result.stop, result.text) == ("completed", "end_turn", " 42.")
    events = await events_of(a, "id1")
    assert kinds(events)[3:] == ["model_turn", "user_message", "model_turn", "run_finished"]
    (origin, text) = user_messages(events)[1]
    assert origin == "auto_continue" and "cut off" in text
    assert model.turn_calls[1].messages[-2] == say("The answer is")  # the partial turn stays


async def test_text_that_keeps_being_cut_off_ends_the_run_as_truncated() -> None:
    cut: Any = (say("and so on"), "length")
    model = AsyncFakeModel(agent=[cut, cut, cut, say("never asked")])
    a = agent(model, budget=Budget(auto_continues=2))
    result = await a.run("Explain.")
    assert (result.outcome, result.stop, result.text) == ("failed", "truncated", "and so on")
    assert len(model.turn_calls) == 3


async def test_cut_off_text_and_unfinished_tasks_share_one_allowance() -> None:
    done = Checklist("a", "b")
    model = AsyncFakeModel(agent=[(say("partial"), "length"), say("1"), say("never asked")])
    a = agent(model, done=done, budget=Budget(auto_continues=1))
    result = await a.run("Go.")
    assert result.stop == "incomplete" and len(model.turn_calls) == 2


# --- §5 rules 9 and 11: a model that cannot be reached ---


async def test_without_a_fallback_role_a_failed_call_ends_the_run() -> None:
    model = AsyncFakeModel(agent=[DOWN, say("never asked")])
    result = await agent(model).run("Hi.")
    assert (result.outcome, result.error, result.switched) == (
        "failed",
        RunError("model", "CallFailed"),
        False,
    )


async def test_a_failed_call_switches_to_the_fallback_role_once_and_says_so() -> None:
    tools = Recording()
    model = AsyncFakeModel(
        agent=[call(("c1", "get_time", {})), DOWN],
        backup=[call(("c2", "get_time", {})), say("Noon.")],
    )
    a = agent(model, tools.tool("get_time", "12:00"), fallback_role="backup")

    result = await a.run("Time?")

    assert (result.outcome, result.text, result.switched) == ("completed", "Noon.", True)
    assert roles_asked(model) == ["agent", "agent", "backup", "backup"]  # it stays on the fallback
    events = await events_of(a, "id1")
    assert kinds(events)[6:] == [
        "model_error",
        "model_switched",
        "model_turn",
        "tool_started",
        "tool_result",
        "model_turn",
        "run_finished",
    ]
    failed, switched = events[6], events[7]
    assert isinstance(failed, ModelErrorEvent) and (failed.error, failed.role) == (
        "CallFailed",
        "agent",
    )
    assert isinstance(switched, ModelSwitched)
    assert (switched.from_role, switched.to_role, switched.reason) == (
        "agent",
        "backup",
        "CallFailed",
    )
    assert [e.role for e in events if isinstance(e, ModelTurn)] == ["agent", "backup", "backup"]
    # the fallback is sent the same request the primary could not answer
    assert model.turn_calls[2].messages == model.turn_calls[1].messages
    assert tools.ran == [("get_time", {}), ("get_time", {})]


async def test_a_fallback_that_fails_too_ends_the_run_without_a_second_switch() -> None:
    model = AsyncFakeModel(agent=[DOWN, say("never asked")], backup=[DOWN, say("never asked")])
    a = agent(model, fallback_role="backup")

    result = await a.run("Hi.")

    assert (result.outcome, result.stop, result.switched) == ("failed", "error", True)
    assert result.error == RunError("model", "CallFailed")
    assert roles_asked(model) == ["agent", "backup"]
    events = await events_of(a, "id1")
    assert kinds(events)[3:] == ["model_error", "model_switched", "model_error", "run_finished"]
    assert [e.role for e in events if isinstance(e, ModelErrorEvent)] == ["agent", "backup"]


@pytest.mark.parametrize(
    "failure",
    [EmptyReply("agent: empty reply", usage=Usage(input=3, output=0)), (say("cut"), "length")],
    ids=["empty_reply", "truncated_text"],
)
async def test_only_an_unreachable_model_triggers_the_switch(failure: Any) -> None:
    model = AsyncFakeModel(agent=[failure, say("after")], backup=[say("never asked")])
    a = agent(model, fallback_role="backup", budget=Budget(auto_continues=1))
    result = await a.run("Hi.")
    assert result.switched is False and "backup" not in roles_asked(model)
    assert "model_switched" not in kinds(await events_of(a, "id1"))


async def test_the_next_run_starts_on_the_primary_role_again() -> None:
    model = AsyncFakeModel(agent=[DOWN, say("Back.")], backup=[say("From the backup.")])
    a = agent(model, fallback_role="backup")
    first = await a.run("One.")
    assert (first.text, first.switched) == ("From the backup.", True)

    second = await a.run("Two.", session_id=first.session_id)

    assert (second.text, second.switched) == ("Back.", False)
    assert roles_asked(model) == ["agent", "backup", "agent"]


async def test_a_resume_after_a_switched_run_paused_starts_on_the_primary_role() -> None:
    class Ask:
        name = "ask"

        async def check(self, c: Any, ctx: RunContext) -> PauseFor:
            return PauseFor("ask")

    model = AsyncFakeModel(agent=[DOWN, say("Paid.")], backup=[call(("c1", "pay", {}))])
    a = agent(model, Recording().tool("pay"), gates=[Ask()], fallback_role="backup")
    paused = await a.run("Pay.")
    assert (paused.outcome, paused.switched) == ("paused", True)

    result = await a.resume(paused.session_id, Resume(approve={"c1": True}))

    assert (result.text, result.switched) == ("Paid.", False)
    assert roles_asked(model) == ["agent", "backup", "agent"]


@pytest.mark.parametrize("lost", [4, 6], ids=["the_switch_itself", "the_fallbacks_turn"])
async def test_a_run_interrupted_around_a_switch_resumes_on_the_primary_role(lost: int) -> None:
    # seq 4-5: model_error + model_switched, written together; seq 6: the fallback's model_turn
    model = AsyncFakeModel(agent=[DOWN, say("Primary again.")], backup=[say("Backup."), say("x")])
    a = agent(model, fallback_role="backup", sessions=FailingOnce(lost))
    broken = await a.run("Hi.")
    assert broken.error is not None and broken.error.kind == "store"

    result = await a.resume(broken.session_id)

    assert (result.outcome, result.text) == ("completed", "Primary again.")
    assert roles_asked(model)[-1] == "agent"


async def test_the_failed_call_and_the_fallbacks_calls_all_count_against_the_budget() -> None:
    again = call(("c1", "get_time", {}))
    model = AsyncFakeModel(agent=[DOWN], backup=[again, again])
    a = agent(model, Recording().tool("get_time"), fallback_role="backup", budget=Budget(turns=2))
    result = await a.run("Time?")
    assert (result.outcome, result.stop, result.switched) == ("failed", "budget_turns", True)
    assert roles_asked(model) == ["agent", "backup"]  # one failed, one answered: the budget of 2


async def test_usage_covers_both_roles() -> None:
    model = Billed(
        [Usage(input=10, output=2), Usage(input=30, output=4)],
        agent=[call(("c1", "get_time", {})), DOWN],
        backup=[say("Noon.")],
    )
    a = agent(model, Recording().tool("get_time"), fallback_role="backup")
    result = await a.run("Time?")
    assert result.usage == Usage(input=40, output=6)


@pytest.mark.parametrize("fallback", ["", "agent"])
def test_a_fallback_role_must_name_another_role(fallback: str) -> None:
    with pytest.raises(ValueError, match="fallback_role"):
        Agent(role="agent", model=AsyncFakeModel(), fallback_role=fallback)


# --- Codex review 2026-11-21 ---


def fixed_run_ids() -> Any:
    """An ids factory a host might write: a fresh session id, then the same run id every time."""
    given = iter(["s1"])
    return lambda: next(given, "run")


async def test_a_runs_state_is_its_own_even_when_run_ids_repeat() -> None:
    done = Checklist("a", "b", "c", "d")
    model = AsyncFakeModel(agent=[DOWN, say("2"), say("3")], backup=[say("1"), say("x")])
    a = agent(
        model,
        fallback_role="backup",
        done=done,
        budget=Budget(auto_continues=1),
        ids=fixed_run_ids(),
    )
    first = await a.run("Go.")
    assert (first.run_id, first.switched, first.stop) == ("run", True, "incomplete")

    second = await a.run("Again.", session_id="s1")

    assert second.run_id == "run"
    assert second.switched is False  # the earlier run's switch is not this run's
    # the first run: primary down, then the fallback twice; the second: primary, twice
    assert roles_asked(model) == ["agent", "backup", "backup", "agent", "agent"]


async def test_a_text_turn_and_what_follows_it_are_written_together() -> None:
    # A crash between the turn and the message that sends it back would leave a session that
    # looks finished and is not. seq 5 is that message: with it the turn is not written either.
    def done(events: Sequence[Event]) -> str | None:  # open until the log holds two turns
        turns = sum(isinstance(e, ModelTurn) for e in events)
        return "Still open: tests." if turns < 2 else None

    model = AsyncFakeModel(agent=[say("Code written."), say("Code written."), say("Tests added.")])
    a = agent(model, done=done, sessions=FailingOnce(5))

    broken = await a.run("Write it and test it.")

    assert broken.error == RunError("store", "OSError")
    assert kinds(await events_of(a, "id1")) == ["session_started", "run_started", "user_message"]
    result = await a.resume(broken.session_id)  # so the turn is simply asked for again
    assert (result.outcome, result.text) == ("completed", "Tests added.")


@pytest.mark.parametrize("answer", [True, 3, ["tests"]])
async def test_a_completion_condition_must_answer_with_text_or_none(answer: Any) -> None:
    model = AsyncFakeModel(agent=[say("Done.")])
    a = agent(model, done=lambda events: answer)
    with pytest.raises(TypeError, match="done"):
        await a.run("Do it.")
    assert kinds(await events_of(a, "id1")) == ["session_started", "run_started", "user_message"]


async def test_a_blank_answer_means_done() -> None:
    model = AsyncFakeModel(agent=[say("Done."), say("never asked")])
    result = await agent(model, done=lambda events: "  \n").run("Do it.")
    assert (result.outcome, len(model.turn_calls)) == ("completed", 1)


async def test_a_switch_with_no_budget_left_for_the_fallback_is_recorded_and_stops() -> None:
    model = AsyncFakeModel(agent=[DOWN], backup=[say("never asked")])
    a = agent(model, fallback_role="backup", budget=Budget(turns=1))
    result = await a.run("Hi.")
    assert (result.outcome, result.stop, result.switched) == ("failed", "budget_turns", True)
    assert roles_asked(model) == ["agent"]
    assert kinds(await events_of(a, "id1"))[3:] == ["model_error", "model_switched", "run_finished"]


async def test_the_run_snapshot_records_the_fallback_role() -> None:
    a = agent(AsyncFakeModel(agent=[say("Hi.")]), fallback_role="backup")
    await a.run("Hi.")
    started = (await events_of(a, "id1"))[1]
    assert (started.type, started.role, started.fallback_role) == ("run_started", "agent", "backup")  # type: ignore[union-attr]
