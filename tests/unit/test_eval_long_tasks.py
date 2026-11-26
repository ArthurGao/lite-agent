"""The long tasks (design §15, masking on long runs): a second synthetic world, the archive, whose
reports are long, so that what a run sends grows past masking's and compaction's thresholds.
Judged in code like the S5 tasks; the twelve S5 tasks are untouched."""

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from lite_model import AssistantTurn
from lite_model.testing import AsyncFakeModel

from lite_agent.evals.archive import (
    ARCHIVE_SYSTEM,
    LONG_BY_ID,
    LONG_TASKS,
    REPORTS,
    Archive,
    chain,
)
from lite_agent.evals.tasks import BY_ID, TASKS, Task, Verdict, run_task, say, use
from lite_agent.events import RunStarted, ToolStarted
from lite_agent.masking import ToolResultMasker
from lite_agent.tools import ToolContext

T0 = datetime(2026, 11, 26, tzinfo=UTC)


def fake(task: Task[Archive], script: Sequence[AssistantTurn] | None = None) -> AsyncFakeModel:
    """lite-model's fake playing `script` (the task's correct path by default)."""
    summary = say(task.summary or "Summary.")
    return AsyncFakeModel(agent=list(script or task.script), compact=lambda asked: summary)


def test_the_long_tasks_are_their_own_set() -> None:
    assert [t.id for t in LONG_TASKS] == ["audit-en", "audit-zh", "compare-en"]
    assert {t.lang for t in LONG_TASKS} == {"en", "zh"}
    assert not set(LONG_BY_ID) & set(BY_ID)  # the S5 set is not extended
    assert len(TASKS) == 12


def test_reports_are_long_deterministic_and_name_the_next() -> None:
    ids = chain("R")
    assert ids[0] == "R01" and len(ids) == 12
    for i, rid in enumerate(ids):
        report = REPORTS[rid]
        assert 1_200 <= len(report.text) <= 2_000
        assert f"Shipment status: {report.status}" in report.text
        assert f"Gross weight: {report.weight} kg" in report.text
        nxt = ids[i + 1] if i + 1 < len(ids) else None
        assert report.next_id == nxt
        assert (f"Next report: {nxt}" if nxt else "This is the last report") in report.text


def test_the_archive_and_prompts_are_the_ones_measured() -> None:
    """The data the second Ollama run of 2026-11-26 measured (design §15; the first asked
    compare-en which report is heavier): changing a report, the system prompt or a prompt is a
    new measurement, so this pin moves only with a new run."""
    texts = "\x00".join(REPORTS[r].text for r in sorted(REPORTS))
    prompts = (LONG_BY_ID[k].prompt for k in sorted(LONG_BY_ID))
    joined = "\x00".join([texts, Archive.system, *prompts])
    assert len(REPORTS) == 36
    assert hashlib.sha256(joined.encode()).hexdigest()[:16] == "03d2d2c02bc7e560"


def test_a_report_reads_like_a_log_scans_in_order_from_one_place_to_another() -> None:
    for report in REPORTS.values():
        stamps = re.findall(r"2026-10-\d\d \d\d:\d\d", report.text)
        assert len(stamps) >= 8
        assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
        origin = re.search(r"Origin: (.+?)\. Destination: (.+?)\.", report.text)
        assert origin is not None and origin[1] != origin[2]


async def test_read_report_returns_the_text_and_refuses_an_unknown_id() -> None:
    (read,) = Archive().tools()
    assert read.name == "read_report"
    ctx = ToolContext(session_id="s", run_id="r", call_id="c")
    assert await read({"report_id": " r01"}, ctx) == REPORTS["R01"].text
    assert await read({"report_id": "R99"}, ctx) == "no report R99"


@pytest.mark.parametrize("task", LONG_TASKS, ids=lambda t: t.id)
async def test_the_correct_path_is_done_reading_every_report_once(task: Task[Archive]) -> None:
    run = await run_task(task, fake(task))
    m = run.metrics
    reports = len([s for s in task.script if s.tool_calls])
    assert (m.done, m.why) == (True, "done"), m.why
    assert (m.turns, m.tool_calls, m.rereads, m.masked) == (reports + 1, reports, 0, 0)
    started = next(e for e in run.events if isinstance(e, RunStarted))
    assert started.system == ARCHIVE_SYSTEM  # the archive's own instructions, not the shop's
    assert [t.name for t in started.tools] == ["read_report"]


@pytest.mark.parametrize("task", LONG_TASKS, ids=lambda t: t.id)
async def test_a_wrong_answer_is_not_done_and_says_why(task: Task[Archive]) -> None:
    wrong = (*task.script[:-1], say("The answer is 0 and 0.0 kg."))
    run = await run_task(task, fake(task, wrong))
    assert not run.metrics.done
    assert run.metrics.why != "done"


@pytest.mark.parametrize("task", LONG_TASKS, ids=lambda t: t.id)
async def test_skipping_a_report_is_not_done(task: Task[Archive]) -> None:
    skipped = (*task.script[:2], *task.script[3:])  # the third report is never read
    run = await run_task(task, fake(task, skipped))
    assert (run.metrics.done, run.metrics.why) == (False, "did not read every report")


async def test_reading_a_report_again_counts_as_a_reread() -> None:
    task = LONG_BY_ID["compare-en"]
    again = use(("again", "read_report", {"report_id": chain("T")[0]}))
    script = (*task.script[:-1], again, task.script[-1])
    run = await run_task(task, fake(task, script))
    assert (run.metrics.done, run.metrics.rereads) == (True, 1)


async def test_masking_on_a_long_task_is_counted() -> None:
    task = LONG_BY_ID["audit-en"]
    masking = ToolResultMasker(context_tokens={"agent": 3_000}, keep_last=2)
    run = await run_task(task, fake(task), masking=masking)
    assert run.metrics.done
    assert run.metrics.masked > 0


def test_the_answers_are_what_the_archive_says() -> None:
    delayed = [REPORTS[r] for r in chain("R") if REPORTS[r].status == "delayed"]
    audit = LONG_BY_ID["audit-en"].script[-1].text
    assert str(len(delayed)) in audit
    assert str(sum((r.weight for r in delayed), Decimal(0))) in audit


async def test_compare_needs_the_first_reports_weight_the_fact_masking_hides() -> None:
    task = LONG_BY_ID["compare-en"]
    first, last = (REPORTS[r] for r in (chain("T")[0], chain("T")[-1]))
    heavier = first if first.weight > last.weight else last
    forgot = say(f"{last.id} weighs {last.weight} kg; {heavier.id} is the heavier one.")
    script = (*task.script[:-1], forgot)
    run = await run_task(task, fake(task, script))
    assert (run.metrics.done, run.metrics.why) == (
        False,
        f"the first report's weight {first.weight} is not given",
    )


async def test_a_long_run_has_room_past_the_s5_turn_budget() -> None:
    task = LONG_BY_ID["compare-en"]
    again = tuple(use((f"a{i}", "read_report", {"report_id": "T01"})) for i in range(8))
    script = (*task.script[:-1], *again, task.script[-1])  # 21 turns: past S5's 16
    run = await run_task(task, fake(task, script))
    assert (run.metrics.done, run.metrics.turns, run.metrics.rereads) == (True, 21, 8)


# --- from the close-out review: answers judged on their values, whatever their form ---


@pytest.mark.parametrize(
    "answer",
    [
        "Delayed: 8; total gross weight 229.3kg",
        "Eight shipments were delayed, 229.30 kg in all.",
    ],
)
def test_an_audit_answer_is_judged_on_its_values_not_its_form(answer: str) -> None:
    task = LONG_BY_ID["audit-en"]
    every = tuple(task.script[:-1])
    run_events = _reads(every)
    assert task.check(Archive(), run_events, answer) == Verdict(True, "done")


@pytest.mark.parametrize(
    "answer", ["共有七票 delayed，毛重合计 160.3kg。", "7 票，合计 160.3 千克"]
)
def test_a_chinese_audit_answer_is_judged_on_its_values(answer: str) -> None:
    task = LONG_BY_ID["audit-zh"]
    assert task.check(Archive(), _reads(task.script[:-1]), answer) == Verdict(True, "done")


def test_compare_is_judged_on_the_two_weights_and_their_difference() -> None:
    task = LONG_BY_ID["compare-en"]
    events = _reads(task.script[:-1])
    right = "T01 is 43.6kg, T12 is 38.2 kg: the first is heavier by 5.4 kg."
    assert task.check(Archive(), events, right) == Verdict(True, "done")
    wrong = "T01 is 43.6 kg and T12 is 38.2 kg, so T12 is heavier by 4.4 kg."
    assert task.check(Archive(), events, wrong) == Verdict(
        False, "the difference 5.4 kg is not given"
    )


async def test_the_answer_is_kept_in_the_metrics_for_judging_again() -> None:
    task = LONG_BY_ID["compare-en"]
    run = await run_task(task, fake(task))
    assert run.metrics.answer == task.script[-1].text


def _reads(script: Sequence[AssistantTurn]) -> tuple[ToolStarted, ...]:
    """The `tool_started` events a run following `script` writes."""
    return tuple(
        ToolStarted(
            seq=i + 1,
            session_id="s",
            time=T0,
            run_id="r",
            call_id=c.id,
            name=c.name,
            arguments=dict(c.arguments or {}),
        )
        for i, c in enumerate(c for turn in script for c in turn.tool_calls)
    )
