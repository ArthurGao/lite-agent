"""The fixed task set (design §15 S5): each task is an instruction, a check that reads the shop
and the event log afterwards and decides in code whether it was done, and a scripted correct
path for lite-model's fake (the deterministic run in CI). Metrics come from the log, never from
what the model says about itself."""

import dataclasses
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Generic, Literal, Protocol

from lite_model import AssistantTurn, AsyncToolModel, Text, ToolCall
from typing_extensions import TypeVar

from lite_agent.agent import Agent
from lite_agent.budget import Budget, spent
from lite_agent.compaction import SummaryCompactor
from lite_agent.context import SkillDir
from lite_agent.evals.world import Note, Refund, World
from lite_agent.events import (
    Compaction,
    Event,
    ModelErrorEvent,
    ModelTurn,
    ToolCallBlock,
    ToolResultEvent,
    ToolStarted,
    UserMessage,
)
from lite_agent.projection import masks
from lite_agent.result import RunResult
from lite_agent.tools import Tool

SKILLS = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    why: str  # what was missing, or "done"


class Place(Protocol):
    """Where a task runs, rebuilt for every run: the agent's instructions and tools there, and
    what the tools changed for the task's check to read."""

    @property
    def system(self) -> str: ...

    def tools(self) -> tuple[Tool, ...]: ...


# A task's place: the S5 shop by default, the archive for the long tasks (archive.py).
P = TypeVar("P", bound=Place, default=World)


@dataclass(frozen=True)
class Task(Generic[P]):
    id: str
    lang: Literal["en", "zh"]
    kind: Literal[
        "lookup", "chain", "act", "recover", "no_tool", "skill", "long", "done", "audit", "compare"
    ]
    prompt: str
    check: Callable[[P, Sequence[Event], str], Verdict]  # reads the place and the log afterwards
    script: tuple[AssistantTurn, ...]  # the fake's correct path
    place: Callable[[], P]
    summary: str | None = None  # the fake's compaction summary, for a task that compacts
    compact_at: int | None = None  # estimated tokens before a summary is written
    skills: bool = False
    # The task's completion condition (Agent.done): None when done, else what is still open.
    done: Callable[[P, Sequence[Event]], str | None] | None = None
    turns: int = 16  # the run's turn budget


@dataclass(frozen=True)
class Metrics:
    """One run of one task, read from its log."""

    done: bool
    why: str
    outcome: str
    stop: str
    turns: int  # model calls of the loop, failed ones included (as the budget counts them)
    tool_calls: int  # what the model asked for, refused calls included
    invalid_calls: int  # calls the harness refused: unknown tool or arguments that do not validate
    auto_continues: int
    compacted: bool
    input_tokens: int  # every billed call: turns, summaries, failed calls; unknown counts as 0
    output_tokens: int
    unknown_usage: int  # billed calls whose token counts the endpoint did not report
    rereads: (
        int  # calls repeating an earlier call's tool and arguments (a masked result read again)
    )
    masked: int  # tool results shown as a placeholder (design §6)
    answer: str  # the run's final text, kept so a run can be judged again
    seconds: float

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class TaskRun:
    task: Task[Any]
    result: RunResult
    events: tuple[Event, ...]
    world: Place
    metrics: Metrics = field(repr=False)


# --- helpers for checks and scripts ---


def calls(events: Sequence[Event], name: str | None = None) -> list[ToolStarted]:
    return [e for e in events if isinstance(e, ToolStarted) and (name is None or e.name == name)]


# A negation right before a value: "not shipped", "hasn't shipped", "not yet shipped", "never
# been delivered", "未发货", "不是已发货". Only "yet" / "been" / "be" may stand between, so
# "not only shipped" or "isn't it shipped?" do not negate it.
_NEGATED = re.compile(
    r"(\bnot|n't|\bnever|\bno longer)\s+((yet|been|be|have been|has been)\s+)?$"
    r"|(没有|还没有|还没|未|不是|不|没)\s*$"
)
_CLAUSE = re.compile(r"[.;:!?\n。；：！？，,]")


def says(answer: str, *options: str) -> bool:
    """Whether the answer states one of `options` as a value: whole (30 is not in 300, D02 is
    not in D021; Latin letters and digits mark the edges) and not negated in its own clause.
    Thousands separators are ignored (1,268.90 is 1268.90)."""
    text = re.sub(r"(?<=\d),(?=\d{3})", "", answer.casefold())
    for option in (o.casefold() for o in options):
        for m in re.finditer(re.escape(option), text):
            before, after = text[: m.start()], text[m.end() :]
            joined_left = _word(before[-1:]) and _word(option[:1])
            if joined_left or (_word(after[:1]) and _word(option[-1:])):
                continue  # part of a longer word or number
            if (before.endswith(".") and before[-2:-1].isdigit()) or (
                after[:1] == "." and after[1:2].isdigit()
            ):
                continue  # inside a decimal: 268.9 in 1268.90 or 30 in 30.5
            if _NEGATED.search(_CLAUSE.split(before)[-1]):
                continue
            return True
    return False


def _word(c: str) -> bool:
    return c.isascii() and c.isalnum()


def judge(*conditions: tuple[bool, str]) -> Verdict:
    for ok, missing in conditions:
        if not ok:
            return Verdict(False, missing)
    return Verdict(True, "done")


def say(text: str) -> AssistantTurn:
    return AssistantTurn((Text(text),))


def use(*calls_: tuple[str, str, dict[str, Any]]) -> AssistantTurn:
    return AssistantTurn(tuple(ToolCall(i, n, a) for i, n, a in calls_))


def _ordered(events: Sequence[Event], first: str, then: str) -> bool:
    names = [c.name for c in calls(events)]
    return first in names and then in names and names.index(first) < names.index(then)


def _refunds(world: World, order_id: str) -> list[Refund]:
    return [r for r in world.refunds if r.order_id == order_id]


def _notes(world: World, customer_id: str) -> list[Note]:
    return [n for n in world.notes if n.customer_id == customer_id]


def _told_out_of_stock(world: World) -> bool:
    phrases = ("out of stock", "not in stock", "0 in stock", "none in stock", "not back")
    return any(says(n.text, *phrases) for n in _notes(world, "C2"))


# --- the tasks ---

# A chain: each order names the next, so a model cannot look them up all at once (in the first
# baseline both models fetched a list of ids in one turn, and nothing was left to summarise).
LONG_IDS = ("F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08")
# The amounts at most 100: 24 + 61.50 + 9.90 + 33 + 77.25 + 15 + 48.35 (F03 is 140)
LONG_SUM = "269.00"


def _long_script() -> tuple[AssistantTurn, ...]:
    lookups = tuple(use((f"g{i}", "get_order", {"order_id": o})) for i, o in enumerate(LONG_IDS))
    return (*lookups, say(f"The total of the orders at most 100 is {LONG_SUM}."))


TASKS: tuple[Task, ...] = (
    Task(
        id="lookup-en",
        place=World,
        lang="en",
        kind="lookup",
        prompt="What is the status of order A17?",
        check=lambda w, ev, a: judge(
            (
                any(c.arguments.get("order_id") == "A17" for c in calls(ev, "get_order")),
                "never looked A17 up",
            ),
            (says(a, "shipped"), "the answer does not say shipped"),
        ),
        script=(use(("c1", "get_order", {"order_id": "A17"})), say("Order A17 has shipped.")),
    ),
    Task(
        id="lookup-zh",
        place=World,
        lang="zh",
        kind="lookup",
        prompt="订单 D03 现在是什么状态？",
        check=lambda w, ev, a: judge(
            (bool(calls(ev, "get_order")), "没有查订单"),
            (says(a, "shipped", "已发货", "发货"), "回答里没有已发货"),
        ),
        script=(use(("c1", "get_order", {"order_id": "D03"})), say("订单 D03 已发货。")),
    ),
    Task(
        id="chain-en",
        place=World,
        lang="en",
        kind="chain",
        prompt="How much has the customer with the email bo@example.com spent in total on "
        "delivered orders?",
        check=lambda w, ev, a: judge(
            (
                _ordered(ev, "find_customer", "list_orders"),
                "did not find the customer, then list orders",
            ),
            (says(a, "268.9", "268.90"), "the total is not 268.90"),
        ),
        script=(
            use(("c1", "find_customer", {"email": "bo@example.com"})),
            use(("c2", "list_orders", {"customer_id": "C2"})),
            say("Bo Chen has spent 268.90 on delivered orders (B03 and B05)."),
        ),
    ),
    Task(
        id="chain-zh",
        place=World,
        lang="zh",
        kind="chain",
        prompt="邮箱是 wang.fang@example.cn 的客户，有哪个订单被取消了？请给出订单号。",
        check=lambda w, ev, a: judge(
            (_ordered(ev, "find_customer", "list_orders"), "没有先找客户再列订单"),
            (says(a, "D02"), "回答里没有 D02"),
        ),
        script=(
            use(("c1", "find_customer", {"email": "wang.fang@example.cn"})),
            use(("c2", "list_orders", {"customer_id": "C3"})),
            say("被取消的订单是 D02（茶叶）。"),
        ),
    ),
    Task(
        id="act-en",
        place=World,
        lang="en",
        kind="act",
        prompt="Refund order B03 in full.",
        check=lambda w, ev, a: judge(
            (len(_refunds(w, "B03")) == 1, f"{len(_refunds(w, 'B03'))} refunds of B03, not 1"),
            (w.refunded("B03") == Decimal("189.00"), f"refunded {w.refunded('B03')}, not 189.00"),
            (len(w.refunds) == 1, "refunded another order too"),
        ),
        script=(
            use(("c1", "get_order", {"order_id": "B03"})),
            use(("c2", "refund", {"order_id": "B03", "amount": "189.00"})),
            say("Refunded 189.00 for order B03."),
        ),
    ),
    Task(
        id="act-zh",
        place=World,
        lang="zh",
        kind="act",
        prompt="给订单 A18 退款 3 元，只退一次。",
        check=lambda w, ev, a: judge(
            (len(_refunds(w, "A18")) == 1, f"A18 退了 {len(_refunds(w, 'A18'))} 次，不是 1 次"),
            (w.refunded("A18") == Decimal("3"), f"退了 {w.refunded('A18')}，不是 3"),
            (len(w.refunds) == 1, "还退了别的订单"),
        ),
        script=(
            use(("c1", "refund", {"order_id": "A18", "amount": 3})),
            say("已为订单 A18 退款 3.00 元。"),
        ),
    ),
    Task(
        id="recover-en",
        place=World,
        lang="en",
        kind="recover",
        prompt="Ana (ana@example.com) asks about her order 17. What is its status?",
        check=lambda w, ev, a: judge(
            (says(a, "shipped"), "the answer does not say shipped"),
            (not says(a, "delivered"), "the answer says delivered: another order's status"),
        ),
        script=(
            use(("c1", "get_order", {"order_id": "17"})),
            use(("c2", "find_customer", {"email": "ana@example.com"})),
            use(("c3", "list_orders", {"customer_id": "C1"})),
            say("Order A17 (the desk lamp) has shipped."),
        ),
    ),
    Task(
        id="no-tool-en",
        place=World,
        lang="en",
        kind="no_tool",
        prompt="Our returns window is 30 days from delivery. A customer asks how long they have to "
        "return an item. Answer from this message alone.",
        check=lambda w, ev, a: judge(
            (not calls(ev), f"called {len(calls(ev))} tools"),
            (says(a, "30"), "the answer does not say 30 days"),
        ),
        script=(say("You have 30 days from delivery to return an item."),),
    ),
    Task(
        id="no-tool-zh",
        place=World,
        lang="zh",
        kind="no_tool",
        prompt="本店工作日 9 点到 18 点有人工客服。客户问几点可以找到人工客服？只根据这句话回答。",
        check=lambda w, ev, a: judge(
            (not calls(ev), f"调用了 {len(calls(ev))} 次 tool"),
            (says(a, "9") and says(a, "18"), "回答里没有 9 点到 18 点"),
        ),
        script=(say("工作日 9 点到 18 点可以找到人工客服。"),),
    ),
    Task(
        id="skill-zh",
        place=World,
        lang="zh",
        kind="skill",
        prompt="王芳的订单 D01（茶具套装）已经拆封了，她想退货。请按退款政策处理并告诉她退了多少。",
        check=lambda w, ev, a: judge(
            (bool(calls(ev, "load_skill")), "没有读退款政策"),
            (len(_refunds(w, "D01")) == 1, f"D01 退了 {len(_refunds(w, 'D01'))} 次，不是 1 次"),
            (w.refunded("D01") == Decimal("214.40"), f"退了 {w.refunded('D01')}，不是 214.40"),
            (bool(_notes(w, "C3")), "没有给王芳留言"),
        ),
        script=(
            use(("c1", "load_skill", {"name": "refund-policy"})),
            use(("c2", "refund", {"order_id": "D01", "amount": "214.40"})),
            use(
                (
                    "c3",
                    "send_note",
                    {"customer_id": "C3", "text": "已退款 214.40 元（拆封商品退 80%）。"},
                )
            ),
            say("已按政策退款 214.40 元，并已留言告知王芳。"),
        ),
        skills=True,
    ),
    Task(
        id="long-en",
        place=World,
        lang="en",
        kind="long",
        prompt="Rule for this task: leave out every order whose amount is above 100. Start with "
        "order F01; each order names the next one to look up. Follow them to the last one, then "
        "give the total amount of the orders that are left.",
        check=lambda w, ev, a: judge(
            (len(calls(ev, "get_order")) >= len(LONG_IDS), "did not look up every order"),
            (any(isinstance(e, Compaction) for e in ev), "the history was never summarised"),
            (says(a, LONG_SUM), f"the total is not {LONG_SUM}"),
        ),
        script=_long_script(),
        summary="The user said: leave out every order above 100, follow the orders from F01 to "
        "the last and give the total of the rest. Looked up so far: F01 24.00, F02 61.50, "
        "F03 140.00 (above 100, left out), F04 9.90, F05 33.00, F06 77.25.",
        compact_at=150,
    ),
    Task(
        id="done-en",
        place=World,
        lang="en",
        kind="done",
        prompt="Bo (bo@example.com) asked whether the office chair, SKU CHAIR-2, is back in stock. "
        "Find out and tell him.",
        check=lambda w, ev, a: judge(
            (bool(calls(ev, "check_stock")), "never checked the stock"),
            (bool(_notes(w, "C2")), "left Bo no note"),
            (_told_out_of_stock(w), "the note does not say it is out of stock"),
        ),
        script=(
            use(("c1", "check_stock", {"sku": "CHAIR-2"})),
            say("CHAIR-2 is out of stock."),  # a report, not done: the condition nudges
            use(("c2", "find_customer", {"email": "bo@example.com"})),
            use(
                (
                    "c3",
                    "send_note",
                    {"customer_id": "C2", "text": "The office chair is out of stock."},
                )
            ),
            say("I told Bo the chair is out of stock."),
        ),
        # the same postcondition as the check: a wrong note does not end the run
        done=lambda w, ev: (
            None
            if _told_out_of_stock(w)
            else "Bo has not been told that the chair is out of stock: leave him a note."
        ),
    ),
)

BY_ID = {t.id: t for t in TASKS}


def metrics(
    task: Task[P], world: P, result: RunResult, events: Sequence[Event], seconds: float
) -> Metrics:
    answer = result.text or ""
    verdict = task.check(world, events, answer)
    turns = [e for e in events if isinstance(e, ModelTurn)]
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    # every billed call, as the budget counts them: summaries and failed calls too (§5 rule 6)
    usages = [e.usage for e in events if isinstance(e, ModelTurn | Compaction | ModelErrorEvent)]
    billed = [u for u in usages if u is not None]
    return Metrics(
        done=verdict.ok and result.outcome == "completed",
        why=verdict.why
        if result.outcome == "completed"
        else f"run {result.outcome} ({result.stop})",
        outcome=result.outcome,
        stop=result.stop,
        turns=spent(events).turns,
        tool_calls=sum(len([b for b in t.blocks if isinstance(b, ToolCallBlock)]) for t in turns),
        invalid_calls=sum(1 for r in results if r.error_kind == "invalid_args"),
        auto_continues=sum(
            1 for e in events if isinstance(e, UserMessage) and e.origin == "auto_continue"
        ),
        compacted=any(isinstance(e, Compaction) for e in events),
        input_tokens=sum(u.input or 0 for u in billed),
        output_tokens=sum(u.output or 0 for u in billed),
        unknown_usage=sum(1 for u in usages if u is None or u.input is None or u.output is None),
        rereads=_rereads(events),
        masked=len(masks(events)),
        answer=answer,
        seconds=round(seconds, 3),
    )


def _rereads(events: Sequence[Event]) -> int:
    seen: set[str] = set()
    again = 0
    for c in calls(events):
        key = json.dumps([c.name, c.arguments], sort_keys=True)
        again += key in seen
        seen.add(key)
    return again


def build_agent(task: Task[P], world: P, model: AsyncToolModel, **fields: Any) -> Agent:
    """The agent a task runs with: its place's tools, the skills when the task needs them, a
    summary before `compact_at` estimated tokens, the task's completion condition."""
    done = task.done
    return Agent(
        role="agent",
        model=model,
        system=world.system,
        tools=world.tools(),
        skills=SkillDir(SKILLS) if task.skills else None,
        compaction=SummaryCompactor(context_tokens={"agent": task.compact_at}, keep_turns=2)
        if task.compact_at
        else None,
        done=(lambda events: done(world, events)) if done else None,
        budget=Budget(turns=task.turns),
        **fields,
    )


async def run_task(task: Task[P], model: AsyncToolModel, **fields: Any) -> TaskRun:
    """Run one task in a fresh place; `fields` reach `Agent` (a store, a clock, ids)."""
    world = task.place()
    agent = build_agent(task, world, model, **fields)
    started = time.perf_counter()
    result = await agent.run(task.prompt)
    seconds = time.perf_counter() - started
    events = tuple(await agent.sessions.read(result.session_id))
    return TaskRun(task, result, events, world, metrics(task, world, result, events, seconds))
