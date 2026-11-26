"""Masking (design §6, vocabulary V=2): old tool results are shown as a placeholder once the
request grows past the role's threshold. lite-context's `fit()` decides; a `tool_results_masked`
event records each result it masked and the exact text shown instead, so the projection stays a
pure function of the log and adds nothing (rules 1 and 2)."""

import json
from pathlib import Path
from typing import Any

import pytest
from lite_context.steps import MASKED_RESULT
from lite_model import ToolResults
from lite_model.testing import AsyncFakeModel

from lite_agent.events import (
    MaskedResult,
    ToolResultEvent,
    ToolResultsMasked,
    UsageData,
    V,
    dump,
    load,
    provenance_data,
)
from lite_agent.masking import ToolResultMasker
from lite_agent.projection import derive
from tests.helpers import T0, WHO, Recording, agent, call, events_of, say

WHO_DATA = provenance_data(WHO)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "v1_session.json"


def long_run(n: int = 6) -> tuple[AsyncFakeModel, Recording]:
    """`n` turns that each call `search` (300-character results), then an answer."""
    turns = [call((f"c{i}", "search", {"q": i})) for i in range(n)]
    return AsyncFakeModel(agent=[*turns, say("Done.")]), Recording()


def masker(tokens: int = 400, keep_last: int = 2) -> ToolResultMasker:
    return ToolResultMasker(context_tokens={"agent": tokens}, keep_last=keep_last)


def asked(model: Any) -> list[Any]:
    return [c for c in model.turn_calls if c.role == "agent"]


def before_last_turn(events: list[Any]) -> list[Any]:
    """The log as it stood when the last request was sent: up to the turn that answered it."""
    last = max(e.seq for e in events if e.type == "model_turn")
    return [e for e in events if e.seq < last]


def shown_results(messages: Any) -> list[str]:
    return [r.content for m in messages if isinstance(m, ToolResults) for r in m.results]


# --- the event ---


def test_the_vocabulary_is_version_2() -> None:
    assert V == 2


def test_a_masked_event_round_trips_and_says_what_is_shown() -> None:
    event = ToolResultsMasked(
        session_id="s",
        seq=9,
        time=T0,
        run_id="r",
        masked=(MaskedResult(seq=4, shown="[cleared]"),),
        state={"version": 1, "cuts": {}, "compaction": None},
        tokens_before=500,
        tokens_after=200,
    )
    assert load(dump(event)) == event
    assert dump(event)["v"] == 2


@pytest.mark.parametrize(
    "masked", [(), (MaskedResult(seq=4, shown="a"), MaskedResult(seq=4, shown="b"))]
)
def test_a_masked_event_names_at_least_one_result_and_each_once(masked: Any) -> None:
    with pytest.raises(ValueError):
        ToolResultsMasked(
            session_id="s",
            seq=9,
            time=T0,
            run_id="r",
            masked=masked,
            state={},
            tokens_before=1,
            tokens_after=1,
        )


def test_a_v1_log_reads_back_and_projects_as_it_did() -> None:
    data = json.loads(FIXTURE.read_text())
    events = [load(e) for e in data["events"]]
    assert {e.v for e in events} == {1}
    projection = derive(events)
    assert [repr(m) for m in projection.messages] == data["projection"]["messages"]
    assert [repr(t) for t in projection.tools] == data["projection"]["tools"]


# --- the projection ---


async def test_without_masking_nothing_is_masked_and_the_requests_are_as_before() -> None:
    model, tools = long_run()
    a = agent(model, tools.tool("search", "r" * 300))
    await a.run("Go.")
    events = await events_of(a, "id1")
    assert not any(isinstance(e, ToolResultsMasked) for e in events)
    assert shown_results(asked(model)[-1].messages) == ["r" * 300] * 6


async def test_old_results_are_shown_as_the_text_the_event_recorded() -> None:
    model, tools = long_run()
    a = agent(model, tools.tool("search", "r" * 300), masking=masker())
    result = await a.run("Go.")
    assert result.outcome == "completed"
    events = await events_of(a, "id1")
    masks = [e for e in events if isinstance(e, ToolResultsMasked)]
    assert masks  # it grew past 90% of 400 tokens
    placeholder = MASKED_RESULT.format(name="search")
    masked_seqs = {m.seq for e in masks for m in e.masked}
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert all(m.shown == placeholder for e in masks for m in e.masked)
    # the request is the projection of the log: masked results show the recorded text
    last = asked(model)[-1].messages
    assert shown_results(last) == [
        placeholder if r.seq in masked_seqs else r.content for r in results
    ]
    # the newest results stay whole
    assert shown_results(last)[-2:] == ["r" * 300] * 2
    assert derive(before_last_turn(events)).messages == tuple(last)


async def test_while_the_cut_does_not_move_each_request_starts_with_the_previous_one() -> None:
    model, tools = long_run(10)
    a = agent(model, tools.tool("search", "r" * 300), masking=masker(tokens=400, keep_last=2))
    await a.run("Go.")
    events = await events_of(a, "id1")
    mask_seqs = [e.seq for e in events if isinstance(e, ToolResultsMasked)]
    turns = [e.seq for e in events if e.type == "model_turn"]
    calls = asked(model)
    moves = 0
    for i in range(1, len(calls)):
        # a request whose turn came after a new mask event may change; the others extend
        prev, now = calls[i - 1].messages, calls[i].messages
        moved = any(turns[i - 1] < s < (turns[i] if i < len(turns) else 10**9) for s in mask_seqs)
        if moved:
            moves += 1
        else:
            assert tuple(now[: len(prev)]) == tuple(prev), i
    assert 0 < moves < len(calls) - 1  # it masked, and not before every call


async def test_under_the_threshold_nothing_is_masked() -> None:
    model, tools = long_run(2)
    a = agent(model, tools.tool("search", "r" * 30), masking=masker(tokens=100_000))
    await a.run("Go.")
    assert not any(isinstance(e, ToolResultsMasked) for e in await events_of(a, "id1"))


async def test_masking_and_compaction_together_keep_the_invariant() -> None:
    from lite_agent.compaction import SummaryCompactor

    # Each turn also says 80 characters masking cannot shorten: masking first, then, once
    # that is not enough, compaction (measured: 2 masks and 3 summaries here).
    turns = [call((f"c{i}", "search", {"q": i}), text="t" * 80) for i in range(8)]
    model = AsyncFakeModel(agent=[*turns, say("Done.")], compact=[say("They searched.")] * 10)
    tools = Recording()
    a = agent(
        model,
        tools.tool("search", "r" * 300),
        masking=masker(tokens=400),
        compaction=SummaryCompactor(context_tokens={"agent": 500}, keep_turns=3),
    )
    result = await a.run("Go.")
    assert result.outcome == "completed"
    events = await events_of(a, "id1")
    kinds = {e.type for e in events}
    assert {"tool_results_masked", "compaction"} <= kinds  # both did their part
    assert derive(before_last_turn(events)).messages == tuple(asked(model)[-1].messages)


def test_every_role_the_agent_may_call_needs_a_threshold() -> None:
    model, _ = long_run(1)
    with pytest.raises(ValueError, match="fallback"):
        agent(model, fallback_role="fallback", masking=masker())


@pytest.mark.parametrize("fields", [{"keep_last": -1}, {"context_tokens": {"agent": 0}}])
def test_a_masker_that_could_never_work_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ToolResultMasker(**({"context_tokens": {"agent": 100}} | fields))


async def test_a_masked_run_reads_back_from_postgresql_and_projects_the_same(
    pg_url: str, pg_schema: str
) -> None:
    from lite_agent.pg import PgSessionStore

    store = PgSessionStore(pg_url, schema=pg_schema)
    try:
        model, tools = long_run()
        a = agent(model, tools.tool("search", "r" * 300), masking=masker(), sessions=store)
        await a.run("Go.")
        events = list(await store.read("id1"))
        assert any(isinstance(e, ToolResultsMasked) for e in events)
        assert derive(before_last_turn(events)).messages == tuple(asked(model)[-1].messages)
    finally:
        await store.close()


async def test_no_result_is_masked_twice() -> None:
    model, tools = long_run(10)
    a = agent(model, tools.tool("search", "r" * 300), masking=masker())
    await a.run("Go.")
    seqs = [
        m.seq
        for e in await events_of(a, "id1")
        if isinstance(e, ToolResultsMasked)
        for m in e.masked
    ]
    assert seqs and len(seqs) == len(set(seqs))


async def test_the_tool_definitions_count_towards_the_threshold() -> None:
    from lite_agent import FunctionTool

    async def helper(args: Any, ctx: Any) -> str:
        return "x"

    async def masked_with(*extra: Any) -> int:
        model, tools = long_run()
        a = agent(model, tools.tool("search", "r" * 300), *extra, masking=masker(tokens=900))
        await a.run("Go.")
        return sum(isinstance(e, ToolResultsMasked) for e in await events_of(a, "id1"))

    big = FunctionTool(
        "helper", "d" * 2000, {"type": "object"}, helper
    )  # ~570 tokens of definition
    assert await masked_with() == 0
    assert await masked_with(big) > 0


async def test_a_stored_state_this_version_cannot_read_measures_the_cut_again() -> None:
    model, tools = long_run(8)
    a = agent(model, tools.tool("search", "r" * 300), masking=masker())
    await a.run("Go.")
    events = await events_of(a, "id1")
    last = next(e for e in reversed(events) if isinstance(e, ToolResultsMasked))
    unreadable = last.model_copy(update={"state": {"version": 99}})
    patched = [unreadable if e is last else e for e in events]
    from lite_agent.masking import _state

    assert _state(patched) is None
    masker().mask(patched, "agent")  # measures again on the full history instead of raising


# --- from the close-out review ---


def test_keep_last_must_keep_at_least_the_newest_batch() -> None:
    with pytest.raises(ValueError, match="keep_last"):
        ToolResultMasker(context_tokens={"agent": 100}, keep_last=0)


def test_a_masked_event_is_refused_at_a_version_that_did_not_have_it() -> None:
    from lite_agent.errors import EventRejected

    data = dump(
        ToolResultsMasked(
            session_id="s",
            seq=9,
            time=T0,
            run_id="r",
            masked=(MaskedResult(seq=4, shown="x"),),
            state={},
            tokens_before=1,
            tokens_after=1,
        )
    )
    with pytest.raises(EventRejected):
        load({**data, "v": 1})


async def test_a_session_without_masking_stays_readable_by_v1_readers() -> None:
    # Each event carries the vocabulary version its type belongs to: without masking every
    # event is still v=1, so a v0.2.5 worker or shell can go on reading the session.
    model, tools = long_run()
    a = agent(model, tools.tool("search", "r" * 300))
    await a.run("Go.")
    assert {dump(e)["v"] for e in await events_of(a, "id1")} == {1}
    b = agent(*long_run()[:1], tools.tool("search", "r" * 300), masking=masker())
    await b.run("Go.")
    by_type = {(e.type, dump(e)["v"]) for e in await events_of(b, "id1")}
    assert ("tool_results_masked", 2) in by_type
    assert all(v == 1 for t, v in by_type if t != "tool_results_masked")


def test_results_that_match_no_call_project_as_an_empty_batch_as_before() -> None:
    from lite_agent.events import ModelTurn, ToolCallBlock, UserMessage

    env = {"session_id": "s", "time": T0, "run_id": "r"}
    events: list[Any] = [
        UserMessage(seq=1, text="Go.", **env),
        ModelTurn(
            seq=2,
            blocks=(ToolCallBlock(id="c1", name="search", arguments={}),),
            stop="tool_calls",
            role="agent",
            provenance=WHO_DATA,
            usage=UsageData(),
            **env,
        ),
        ToolResultEvent(
            seq=3, call_id="other", content="x", executed=True, original_chars=1, **env
        ),
    ]
    messages = derive(events).messages
    assert messages[-1] == ToolResults(())


def test_any_lite_context_error_while_masking_masks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lite_context import BrokenPairing

    import lite_agent.masking as masking

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise BrokenPairing("a call without its result")

    monkeypatch.setattr(masking, "fit", broken)
    events = json.loads(FIXTURE.read_text())["events"]
    assert masker().mask([load(e) for e in events], "agent") is None


async def test_the_stored_cut_keeps_a_long_run_from_masking_on_every_call() -> None:
    # Measured: replaying the stored state, 50 turns mask in 3 steps at 2,000 tokens;
    # re-measuring from scratch each call, 12 (the cut moves and the prefix breaks).
    model, tools = long_run(50)
    a = agent(model, tools.tool("search", "r" * 300), masking=masker(tokens=2000, keep_last=2))
    await a.run("Go.")
    masks = [e for e in await events_of(a, "id1") if isinstance(e, ToolResultsMasked)]
    assert 0 < len(masks) <= 4


# --- from the Ollama eval: a loaded skill is instructions, not data ---


async def test_a_loaded_skill_and_recalled_notes_are_never_masked(tmp_path: Path) -> None:
    # On qwen3:8b the skill task went from 3/3 to 0/3 with masking on: the skill's text, the
    # result of load_skill, was masked away on the next call (design §15, Phase 4 record).
    from lite_agent import SkillDir

    root = tmp_path / "review"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: review\ndescription: How to review.\n---\n" + "Rule. " * 60
    )
    turns = [
        call(("k", "load_skill", {"name": "review"})),
        *(call((f"c{i}", "search", {"q": i})) for i in range(6)),
    ]
    model, tools = AsyncFakeModel(agent=[*turns, say("Done.")]), Recording()
    a = agent(
        model,
        tools.tool("search", "r" * 300),
        skills=SkillDir(tmp_path),
        masking=masker(tokens=900, keep_last=1),
    )
    await a.run("Review it.")
    events = await events_of(a, "id1")
    masked = {m.seq for e in events if isinstance(e, ToolResultsMasked) for m in e.masked}
    skill = next(e for e in events if isinstance(e, ToolResultEvent) and e.call_id == "k")
    assert masked  # other results were masked
    assert skill.seq not in masked
    assert "Rule. Rule." in shown_results(asked(model)[-1].messages)[0]


def test_which_tools_are_kept_whole_is_the_masker_s_to_say() -> None:
    assert ToolResultMasker(context_tokens={"agent": 1}).keep_tools == ("load_skill", "recall")
    assert ToolResultMasker(context_tokens={"agent": 1}, keep_tools=()).keep_tools == ()
