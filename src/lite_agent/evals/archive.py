"""The long tasks (design §15, masking on long runs): a second synthetic world, a carrier's archive
of shipment reports. Each report is about 1,250 characters of scan log with one status and one
gross weight buried in it, and names the next report, so a run reads them one at a time and what it
sends grows past masking's and compaction's thresholds. The S5 shop's tool results are one-line
rows: masking never engaged there. Rebuilt from fixed seeds: the same archive in every process."""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from lite_agent.evals.tasks import Task, calls, judge, say, says, use
from lite_agent.events import Event
from lite_agent.tools import Tool, ToolContext, tool

ARCHIVE_SYSTEM = (
    "You are an assistant reading a carrier's shipment reports. Use the tools to read them; "
    "never guess an id, a status or a weight. Answer the user briefly, in the user's language."
)
CHAIN_LENGTH = 12
SERIES = ("R", "Q", "T")  # one series per task: audit-en, audit-zh, compare-en
LONG_COMPACT_AT = 3_000  # estimated tokens: past it, a summary (both configs of the long suites)
LONG_TURNS = 40  # twelve reports, room for re-reads and nudges

Status = Literal["delivered", "delayed"]

_CARRIERS = ("Northline Freight", "Kestrel Parcel", "Harbour & Vale", "Tallis Logistics")
_SERVICES = ("Economy road", "Express air", "Standard road", "Two-day priority")
_PLACES = (
    "Ashford hub",
    "Corran depot",
    "Millbrook sort centre",
    "Eastgate cross-dock",
    "Penrith gateway",
    "Selby line-haul yard",
    "Wexham terminal",
    "Dunmore depot",
)
_SCANS = (
    "Received at",
    "Sorted to outbound lane at",
    "Loaded onto trailer at",
    "Departed",
    "Arrived at",
    "Unloaded at",
    "Held for customs paperwork check at",
    "Re-labelled at",
    "Handed to local courier at",
)
_CONTENTS = (
    "machine parts, boxed",
    "printed books on a pallet",
    "ceramic tiles, crated",
    "garden tools",
    "office chairs, flat-packed",
    "canned food, shrink-wrapped",
)
_NOTES = (
    "Driver reported the dock door jammed; loading moved to bay 3 and resumed later.",
    "Label partly torn on arrival; consignment number confirmed from the manifest.",
    "Pallet re-wrapped after the outer film split during unloading. No damage seen.",
    "Receiver asked for a call before arrival; the number on file was confirmed.",
    "Seal number checked against the manifest at each hand-over; all matched.",
)


@dataclass(frozen=True)
class Report:
    id: str
    status: Status
    weight: Decimal  # gross, kg, one decimal place
    next_id: str | None
    text: str


def chain(series: str) -> tuple[str, ...]:
    """The report ids of one series, in reading order."""
    return tuple(f"{series}{i:02d}" for i in range(1, CHAIN_LENGTH + 1))


def _report(rid: str, next_id: str | None) -> Report:
    rng = random.Random(f"lite-agent archive {rid}")  # a str seed is the same in every process
    status: Status = "delayed" if rng.random() < 0.4 else "delivered"
    weight = Decimal(f"{rng.randint(2, 47)}.{rng.randint(1, 9)}")  # never .0: "12.0" vs "12"
    volumetric = Decimal(f"{rng.randint(2, 60)}.{rng.randint(1, 9)}")
    origin, destination = rng.sample(_PLACES, 2)
    lines = [
        f"SHIPMENT REPORT {rid}",
        f"Carrier: {rng.choice(_CARRIERS)}. Service: {rng.choice(_SERVICES)}. "
        f"Consignment: CN-{rng.randint(100000, 999999)}.",
        f"Origin: {origin}. Destination: {destination}.",
        f"Declared contents: {rng.choice(_CONTENTS)}. Pieces: {rng.randint(1, 9)}.",
        "Scan history:",
    ]
    scans: list[str] = []
    minutes = (rng.randint(1, 20) * 24 + rng.randint(5, 9)) * 60  # from 2026-10-01 00:00
    while sum(len(s) for s in scans) < 720:
        minutes += rng.randint(9, 240)  # always later than the scan before
        day, hour, minute = minutes // 1440 + 1, minutes // 60 % 24, minutes % 60
        place = rng.choice(_PLACES)
        scans.append(f"  2026-10-{day:02d} {hour:02d}:{minute:02d}  {rng.choice(_SCANS)} {place}")
    half = len(scans) // 2
    lines += scans[:half]
    lines.append(f"Volumetric weight: {volumetric} kg. Gross weight: {weight} kg.")
    lines += scans[half:]
    lines.append("Handling notes: " + " ".join(rng.sample(_NOTES, 2)))
    lines.append(f"Shipment status: {status}.")
    lines.append(
        f"Next report: {next_id}." if next_id else "This is the last report in the series."
    )
    return Report(rid, status, weight, next_id, "\n".join(lines))


def _archive() -> Mapping[str, Report]:
    reports: dict[str, Report] = {}
    for series in SERIES:
        ids = chain(series)
        for rid, nxt in zip(ids, (*ids[1:], None), strict=True):
            reports[rid] = _report(rid, nxt)
    return MappingProxyType(reports)


REPORTS = _archive()


class ByReport(BaseModel):
    report_id: str = Field(description="A report id such as R01")


class Archive:
    """One task's archive. Read-only: every task reads the same `REPORTS`."""

    system: ClassVar[str] = ARCHIVE_SYSTEM  # the agent's instructions in the archive

    def tools(self) -> tuple[Tool, ...]:
        @tool(description="Read one shipment report by its id: the whole report.", idempotent=True)
        async def read_report(args: ByReport, ctx: ToolContext) -> str:
            report = REPORTS.get(args.report_id.strip().upper())
            return report.text if report else f"no report {args.report_id.strip()}"

        return (read_report,)


# --- the tasks ---


def _read_all(events: Sequence[Event], series: str) -> bool:
    read = {
        str(c.arguments.get("report_id", "")).strip().upper() for c in calls(events, "read_report")
    }
    return set(chain(series)) <= read


def _delayed(series: str) -> tuple[int, Decimal]:
    delayed = [REPORTS[r] for r in chain(series) if REPORTS[r].status == "delayed"]
    return len(delayed), sum((r.weight for r in delayed), Decimal(0))


def _kg(value: Decimal) -> tuple[str, ...]:
    """A weight as an answer may write it: 51.3, 51.30, 48 for 48.0 (a total can end in .0),
    each also with "kg" attached (`says` reads 51.3kg as one word)."""
    forms = {str(value), f"{value:.2f}"}
    forms |= {f.removesuffix(".0") for f in forms if f.endswith(".0")}
    return tuple(sorted(forms | {f + "kg" for f in forms}))


_EN = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
       "eleven", "twelve")  # fmt: skip
_ZH = "零一二三四五六七八九十"


def _count(n: int) -> tuple[str, ...]:
    """A count as an answer may write it: 8, eight, 八."""
    return (str(n), _EN[n], *((_ZH[n],) if n <= 10 else ()))


def _long_task(
    task_id: str,
    lang: Literal["en", "zh"],
    kind: Literal["audit", "compare"],
    series: str,
    prompt: str,
    answered: Callable[[str], tuple[tuple[bool, str], ...]],  # the answer's conditions
    answer: str,  # the fake's correct answer
    asked: str,  # what the fake's summary says the user asked
) -> Task[Archive]:
    """A task that reads one series to the last report, then answers: done when every report
    was read and the answer meets `answered`."""
    return Task(
        id=task_id,
        lang=lang,
        kind=kind,
        prompt=prompt,
        check=lambda w, ev, a: judge(
            (_read_all(ev, series), "did not read every report"), *answered(a)
        ),
        script=(
            *(
                use((f"{series.lower()}{i}", "read_report", {"report_id": r}))
                for i, r in enumerate(chain(series))
            ),
            say(answer),
        ),
        place=Archive,
        summary=f"The user asked: {asked}",
        compact_at=LONG_COMPACT_AT,
        turns=LONG_TURNS,
    )


def _audit(task_id: str, lang: Literal["en", "zh"], series: str, prompt: str) -> Task[Archive]:
    count, total = _delayed(series)
    return _long_task(
        task_id,
        lang,
        "audit",
        series,
        prompt,
        lambda a: (
            (says(a, *_count(count)), f"the number of delayed shipments is not {count}"),
            (says(a, *_kg(total)), f"the total gross weight is not {total} kg"),
        ),
        f"{count} shipments were delayed; their gross weight totals {total} kg.",
        f"follow the reports from {series}01 to the last, count the delayed shipments and total "
        "their gross weight. Read so far: the first reports.",
    )


def _compare() -> Task[Archive]:
    ids = chain("T")
    first, last = REPORTS[ids[0]], REPORTS[ids[-1]]
    difference = abs(first.weight - last.weight)
    return _long_task(
        "compare-en",
        "en",
        "compare",
        "T",
        f"Start with shipment report {ids[0]}; each report names the next one. Read them "
        "all, to the last. Then give the gross weight of the first report and of the last "
        "report, and how many kg heavier the heavier of the two is.",
        lambda a: (
            (says(a, *_kg(first.weight)), f"the first report's weight {first.weight} is not given"),
            (says(a, *_kg(last.weight)), f"the last report's weight {last.weight} is not given"),
            (says(a, *_kg(difference)), f"the difference {difference} kg is not given"),
        ),
        f"{first.id} weighs {first.weight} kg and {last.id} weighs {last.weight} kg: "
        f"a difference of {difference} kg.",
        f"read the reports from {ids[0]} to the last, then compare the gross weights of the "
        "first and the last.",
    )


LONG_TASKS: tuple[Task[Archive], ...] = (
    _audit(
        "audit-en",
        "en",
        "R",
        "Start with shipment report R01; each report names the next one. Read them all, to the "
        "last. Then tell me how many of the shipments were delayed and the total gross weight of "
        "the delayed ones, in kg.",
    ),
    _audit(
        "audit-zh",
        "zh",
        "Q",
        "从运单报告 Q01 开始读，每份报告都写着下一份的编号，一直读到最后一份。然后告诉我其中有几票"
        "货的状态是 delayed，以及这些 delayed 货物的毛重（Gross weight）合计多少千克。",
    ),
    _compare(),
)
LONG_BY_ID = {t.id: t for t in LONG_TASKS}

__all__ = ["ARCHIVE_SYSTEM", "LONG_BY_ID", "LONG_TASKS", "REPORTS", "Archive", "Report", "chain"]
