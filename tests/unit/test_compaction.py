"""Design §4, §6 (stage S3b): a long history is summarised, not rewritten. The `compaction`
event says which seqs it covers; the projection shows the summary in their place; every covered
event stays in the log."""

from typing import Any

import pytest
from lite_model import (
    CallFailed,
    Message,
    ToolResult,
    ToolResults,
    Usage,
)
from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, Budget, Projection, RunError, SummaryCompactor, derive
from lite_agent.compaction import estimate_tokens
from lite_agent.events import Compaction, ModelErrorEvent, ModelTurn
from tests.helpers import Billed, Recording, agent, call, events_of, kinds, say

ALWAYS = {
    "agent": 1,
    "backup": 1,
}  # any history is over this: compaction is due whenever it can cut


def tight(keep_turns: int = 1, **tokens: int) -> SummaryCompactor:
    return SummaryCompactor(context_tokens=tokens or ALWAYS, keep_turns=keep_turns)


async def three_runs(*more_summaries: str, **fields: Any) -> tuple[Agent, Any]:
    """Three short runs on one session; with `tight()` the third compacts the first two.
    `more_summaries` script further summaries for runs a test adds."""
    first_summary = "The user asked two things; the answers were One and Two."
    model = AsyncFakeModel(
        agent=[say("One."), say("Two."), say("Three."), say("Four.")],
        compact=[say(text) for text in (first_summary, *more_summaries)],
    )
    a = agent(model, compaction=tight(), **fields)
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    await a.run("Third?", session_id=first.session_id)
    return a, model


def asked(model: Any, role: str) -> list[Any]:
    return [c for c in model.turn_calls if c.role == role]


# --- what compaction writes and what the projection shows ---


async def test_compaction_writes_one_event_and_the_projection_shows_the_summary_instead() -> None:
    a, model = await three_runs()

    events = await events_of(a, "id1")
    assert kinds(events)[9:] == [
        "run_started",
        "user_message",
        "compaction",
        "model_turn",
        "run_finished",
    ]
    done = events[11]
    assert isinstance(done, Compaction)
    assert done.covers == (3, 7)  # the first user message up to the one before the kept turn
    assert "The user asked two things" in done.summary
    assert (done.role, done.provenance.backend) == ("compact", "fake")

    sent = asked(model, "agent")[2].messages
    assert sent == (Message("user", done.summary), say("Two."), Message("user", "Third?"))
    assert derive(events).messages == (*sent, say("Three."))


async def test_every_covered_event_is_still_in_the_log() -> None:
    a, _ = await three_runs()
    events = await events_of(a, "id1")
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    texts = [e.text for e in events if e.type == "user_message"]
    assert texts == ["First?", "Second?", "Third?"]
    assert sum(isinstance(e, ModelTurn) for e in events) == 3


async def test_the_compact_role_is_asked_with_the_covered_history_and_no_tools() -> None:
    _, model = await three_runs()
    (request,) = asked(model, "compact")
    assert request.tools == ()
    system, transcript = request.messages
    assert system.role == "system" and transcript.role == "user"
    for part in ("First?", "One.", "Second?"):
        assert part in transcript.content
    assert (
        "Two." not in transcript.content and "Third?" not in transcript.content
    )  # kept, not covered


async def test_the_summary_text_in_the_event_is_what_the_model_is_shown() -> None:
    a, model = await three_runs()
    done = next(e for e in await events_of(a, "id1") if isinstance(e, Compaction))
    shown = asked(model, "agent")[2].messages[0]
    assert shown == Message("user", done.summary)  # the projection adds nothing to it
    assert (
        done.summary != "The user asked two things; the answers were One and Two."
    )  # it is framed


async def test_a_later_compaction_takes_in_the_earlier_summary() -> None:
    a, model = await three_runs("Earlier: One and Two. Then Three.")
    await a.run("Fourth?", session_id="id1")

    events = await events_of(a, "id1")
    first, second = (e for e in events if isinstance(e, Compaction))
    assert second.covers[0] == first.covers[0] and second.covers[1] > first.covers[1]
    again = asked(model, "compact")[1].messages[1].content
    assert "The user asked two things" in again and "Third?" in again  # the old summary, then more
    sent = asked(model, "agent")[3].messages
    assert sent == (Message("user", second.summary), say("Three."), Message("user", "Fourth?"))


async def test_the_mask_is_the_events_doing_and_the_log_alone_decides_the_request() -> None:
    a, _ = await three_runs()
    events = await events_of(a, "id1")
    done = next(e for e in events if isinstance(e, Compaction))
    without = [e for e in events if not isinstance(e, Compaction)]
    assert derive(without).messages == (
        Message("user", "First?"),
        say("One."),
        Message("user", "Second?"),
        say("Two."),
        Message("user", "Third?"),
        say("Three."),
    )  # take the event away and the whole history is there again
    other = done.model_copy(update={"summary": "ANOTHER SUMMARY", "covers": (3, 3)})
    swapped = [other if e is done else e for e in events]
    assert derive(swapped).messages[:2] == (Message("user", "ANOTHER SUMMARY"), say("One."))


# --- where it may cut ---


async def test_nothing_is_cut_while_there_are_no_more_turns_than_it_keeps() -> None:
    model = AsyncFakeModel(agent=[say("One."), say("Two.")], compact=[])
    a = agent(model, compaction=tight(keep_turns=2))
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    assert asked(model, "compact") == []
    assert "compaction" not in kinds(await events_of(a, "id1"))


async def test_a_turn_and_its_tool_results_are_never_split() -> None:
    tools = Recording()
    model = AsyncFakeModel(
        agent=[
            call(("c1", "get_time", {})),
            call(("c2", "get_time", {})),
            say("Noon."),
        ],
        compact=[say("The time was asked once.")],
    )
    a = agent(model, tools.tool("get_time", "12:00"), compaction=tight(keep_turns=1))

    await a.run("Time?")

    events = await events_of(a, "id1")
    done = next(e for e in events if isinstance(e, Compaction))
    # covered: the task, the first turn, its start and its result; kept: the second turn whole
    assert done.covers == (3, 6)
    sent = asked(model, "agent")[2].messages
    assert sent == (
        Message("user", done.summary),
        call(("c2", "get_time", {})),
        ToolResults((ToolResult("c2", "12:00"),)),
    )


async def test_the_estimate_counts_text_arguments_results_and_tool_definitions() -> None:
    tools = Recording()
    model = AsyncFakeModel(agent=[call(("c1", "get_time", {"tz": "UTC"})), say("Noon.")])
    a = agent(model, tools.tool("get_time", "x" * 700), system="s" * 350)
    await a.run("u" * 350)
    projection = derive(await events_of(a, "id1"))
    estimate = estimate_tokens(projection)
    assert 400 <= estimate <= 480  # about 1,400 characters of content plus the definitions, / 3.5
    assert estimate_tokens(derive([])) == 0


async def test_tool_definitions_are_part_of_the_estimate() -> None:
    a = agent(AsyncFakeModel(agent=[say("Hi.")]), Recording().tool("get_time"))
    await a.run("Hello.")
    full = derive(await events_of(a, "id1"))
    bare = Projection(full.messages, ())
    assert estimate_tokens(full) - estimate_tokens(bare) >= 10  # a name, a description, a schema


async def test_turns_a_summary_already_covers_do_not_count_towards_the_next_cut() -> None:
    a, _ = await three_runs()  # compacted once: the first turn is covered, two came after
    events = await events_of(a, "id1")
    assert tight(keep_turns=2).cut(events) is None  # exactly the two it keeps: nothing new to cut
    assert tight(keep_turns=1).cut(events) == 12  # up to the event before the last turn


async def test_compaction_waits_until_the_estimate_passes_the_roles_threshold() -> None:
    model = AsyncFakeModel(
        agent=[say("a" * 400), say("b" * 400), say("c"), say("d")],
        compact=[say("Summary.")],
    )
    a = agent(model, compaction=tight(keep_turns=1, agent=200))  # 200 tokens: about 700 characters
    first = await a.run("One?")
    await a.run("Two?", session_id=first.session_id)  # ~400 characters so far: under
    assert asked(model, "compact") == []
    await a.run("Three?", session_id=first.session_id)  # ~800: over, and there is a turn to cut
    assert len(asked(model, "compact")) == 1


# --- budgets and failures ---


async def test_a_compaction_call_counts_tokens_and_cost_but_not_a_turn() -> None:
    usages = [
        Usage(input=10, output=2),  # the first run's turn
        Usage(input=10, output=2),  # the second's
        Usage(input=50, output=8, cost_usd=0.01),  # the summary
        Usage(input=10, output=2),  # the third's turn
        Usage(input=50, output=8),  # a second summary
        Usage(input=10, output=2),  # the fourth's turn
    ]
    model = Billed(
        usages,
        agent=[say("One."), say("Two."), say("Three."), say("Four.")],
        compact=[say("Summary."), say("Summary again.")],
    )
    a = agent(
        model, compaction=tight(), budget=Budget(turns=4)
    )  # four turns; summaries are not turns
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    third = await a.run("Third?", session_id=first.session_id)
    fourth = await a.run("Fourth?", session_id=first.session_id)

    assert (fourth.outcome, fourth.text) == ("completed", "Four.")  # the budget of 4 was enough
    done = next(e for e in await events_of(a, "id1") if isinstance(e, Compaction))
    assert (done.usage.input, done.usage.output, done.usage.cost_usd) == (50, 8, 0.01)
    assert third.usage == Usage(input=60, output=10, cost_usd=0.01)  # the summary and the turn


async def test_the_token_budget_includes_what_compaction_spent() -> None:
    usages = (
        [Usage(input=10, output=0)] * 2 + [Usage(input=100, output=0)] + [Usage(input=10, output=0)]
    )
    model = Billed(
        usages, agent=[say("One."), say("Two."), say("Three."), say("x")], compact=[say("Summary.")]
    )
    a = agent(model, compaction=tight(), budget=Budget(tokens=125))
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    await a.run("Third?", session_id=first.session_id)  # 20 + 100 (summary) + 10 = 130
    fourth = await a.run("Fourth?", session_id=first.session_id)
    assert (fourth.outcome, fourth.stop) == ("failed", "budget_tokens")


async def test_a_summary_that_uses_up_the_budget_stops_the_run_before_the_next_call() -> None:
    # simplify review 2026-11-21: the budget is checked again after a summary was paid for
    usages = [Usage(input=10, output=0)] * 2 + [Usage(input=100, output=0)]
    model = Billed(
        usages, agent=[say("One."), say("Two."), say("never asked")], compact=[say("S.")]
    )
    a = agent(model, compaction=tight(), budget=Budget(tokens=110))
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)

    third = await a.run("Third?", session_id=first.session_id)  # 20 spent, then 100 for the summary

    assert (third.outcome, third.stop) == ("failed", "budget_tokens")
    assert len(asked(model, "agent")) == 2  # the call after the summary was not made
    assert kinds(await events_of(a, "id1"))[-2:] == ["compaction", "run_finished"]


async def test_a_summary_cut_off_at_the_limit_is_not_used() -> None:
    # Codex review 2026-11-21: half a summary must not stand for the whole history
    model = AsyncFakeModel(
        agent=[say("One."), say("Two."), say("never asked")],
        compact=[(say("The user asked two th"), "length")],
    )
    a = agent(model, compaction=tight())
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)

    third = await a.run("Third?", session_id=first.session_id)

    assert (third.outcome, third.stop) == ("failed", "error")
    assert third.error == RunError("model", "TruncatedSummary")
    events = await events_of(a, "id1")
    assert kinds(events)[-2:] == ["model_error", "run_finished"] and "compaction" not in kinds(
        events
    )
    failed = events[-2]
    assert isinstance(failed, ModelErrorEvent) and failed.purpose == "compact"
    assert len(asked(model, "agent")) == 2


def test_the_history_is_rendered_in_the_order_things_were_said() -> None:
    from lite_model import AssistantTurn, Text, ToolCall

    from lite_agent.compaction import render

    turn = AssistantTurn(
        (
            ToolCall("c1", "get_time", {"tz": "UTC"}),
            Text("Checking the map too."),
            ToolCall("c2", "get_map", None),
        )
    )
    text = render(
        [Message("user", "Where and when?"), turn, ToolResults((ToolResult("c1", "12:00"),))]
    )
    order = [
        text.index(part)
        for part in ("Where and when?", "get_time", "Checking the map", "get_map", "12:00")
    ]
    assert order == sorted(order)
    assert "None" not in text  # a call whose arguments never parsed is shown without them


async def test_a_failed_compaction_fails_the_run_and_does_not_switch_models() -> None:
    model = AsyncFakeModel(
        agent=[say("One."), say("Two."), say("never asked")],
        compact=[CallFailed("compact failed: TimeoutError")],
        backup=[say("never asked")],
    )
    a = agent(model, compaction=tight(), fallback_role="backup")
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)

    third = await a.run("Third?", session_id=first.session_id)

    assert (third.outcome, third.stop, third.switched) == ("failed", "error", False)
    assert third.error == RunError("model", "CallFailed")
    events = await events_of(a, "id1")
    assert kinds(events)[-2:] == ["model_error", "run_finished"]
    failed = events[-2]
    assert isinstance(failed, ModelErrorEvent) and (failed.purpose, failed.role) == (
        "compact",
        "compact",
    )
    assert len(asked(model, "agent")) == 2 and asked(model, "backup") == []
    assert "compaction" not in kinds(events)


async def test_the_threshold_is_the_role_being_called() -> None:
    model = AsyncFakeModel(
        agent=[say("a" * 400), say("b"), CallFailed("agent failed: TimeoutError")],
        backup=[say("From the backup.")],
        compact=[say("Summary.")],
    )
    roomy = SummaryCompactor(context_tokens={"agent": 100_000, "backup": 1}, keep_turns=1)
    a = agent(model, compaction=roomy, fallback_role="backup")
    first = await a.run("One?")
    await a.run("Two?", session_id=first.session_id)
    assert asked(model, "compact") == []  # the primary has room

    third = await a.run("Three?", session_id=first.session_id)

    assert (third.switched, third.text) == (True, "From the backup.")
    assert kinds(await events_of(a, "id1"))[-5:] == [
        "model_error",
        "model_switched",
        "compaction",
        "model_turn",
        "run_finished",
    ]  # compacted for the smaller model before it was called


# --- what an agent is built with ---


def test_every_role_the_agent_may_call_needs_a_threshold() -> None:
    only_agent = SummaryCompactor(context_tokens={"agent": 1000})
    Agent(role="agent", model=AsyncFakeModel(), compaction=only_agent)
    with pytest.raises(ValueError, match="backup"):
        Agent(role="agent", fallback_role="backup", model=AsyncFakeModel(), compaction=only_agent)
    with pytest.raises(ValueError, match="agent"):
        Agent(role="agent", model=AsyncFakeModel(), compaction=SummaryCompactor(context_tokens={}))


@pytest.mark.parametrize("fields", [{"keep_turns": 0}, {"context_tokens": {"agent": 0}}])
def test_a_compactor_that_could_keep_nothing_or_fit_nothing_is_refused(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        SummaryCompactor(**({"context_tokens": {"agent": 100}} | fields))


async def test_without_a_compactor_nothing_is_summarised() -> None:
    model = AsyncFakeModel(agent=[say("One."), say("Two."), say("Three.")])
    a = agent(model)
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    await a.run("Third?", session_id=first.session_id)
    assert "compaction" not in kinds(await events_of(a, "id1"))
    assert len(asked(model, "agent")[2].messages) == 5


# --- forks ---


async def test_a_parents_later_compaction_does_not_change_a_fork() -> None:
    model = AsyncFakeModel(
        agent=[say("One."), say("Two."), say("Three.")], compact=[say("Summary.")]
    )
    a = agent(model, compaction=tight())
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    child = await a.sessions.fork("id1", at_seq=9)  # after the second run
    before = derive(await a.sessions.read(child))

    await a.run("Third?", session_id=first.session_id)  # the parent compacts

    assert "compaction" in kinds(await events_of(a, "id1"))
    assert derive(await a.sessions.read(child)) == before
    assert before.messages[0] == Message("user", "First?")


async def test_a_fork_can_compact_across_the_point_it_forked_at() -> None:
    model = AsyncFakeModel(
        agent=[say("One."), say("Two."), say("Three.")], compact=[say("Summary.")]
    )
    a = agent(model, compaction=tight())
    first = await a.run("First?")
    await a.run("Second?", session_id=first.session_id)
    child = await a.sessions.fork("id1", at_seq=9)

    await a.run("Third?", session_id=child)

    events = list(await a.sessions.read(child))
    done = next(e for e in events if isinstance(e, Compaction))
    assert done.session_id == child and done.covers == (3, 7)  # seqs the fork inherited
    assert derive(events).messages == (
        Message("user", done.summary),
        say("Two."),
        Message("user", "Third?"),
        say("Three."),
    )
    assert "compaction" not in kinds(await events_of(a, "id1"))  # the parent is untouched
