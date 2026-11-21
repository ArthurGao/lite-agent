"""The S5 task set (design §15): each task's scripted correct path is judged done, with exact
metrics, and a wrong path is judged not done for the stated reason, so no check passes
everything. lite-model's fake only: no model is called."""

import pytest
from lite_model import AssistantTurn, Usage
from lite_model.testing import AsyncFakeModel

from lite_agent.evals import TASKS, run_task
from lite_agent.evals.tasks import BY_ID, LONG_IDS, Task, say, use
from tests.helpers import Billed


def fake(task: Task, script: tuple[AssistantTurn, ...] | None = None) -> AsyncFakeModel:
    summary = say(task.summary or "Summary.")
    return AsyncFakeModel(agent=list(script or task.script), compact=lambda asked: summary)


def test_the_task_set_is_fixed() -> None:
    assert [t.id for t in TASKS] == [
        "lookup-en", "lookup-zh", "chain-en", "chain-zh", "act-en", "act-zh", "recover-en",
        "no-tool-en", "no-tool-zh", "skill-zh", "long-en", "done-en",
    ]  # fmt: skip
    assert {t.lang for t in TASKS} == {"en", "zh"}


# (turns, tool calls, invalid calls, automatic continues, compacted)
CORRECT: dict[str, tuple[int, int, int, int, bool]] = {
    "lookup-en": (2, 1, 0, 0, False),
    "lookup-zh": (2, 1, 0, 0, False),
    "chain-en": (3, 2, 0, 0, False),
    "chain-zh": (3, 2, 0, 0, False),
    "act-en": (3, 2, 0, 0, False),
    "act-zh": (2, 1, 0, 0, False),
    "recover-en": (4, 3, 0, 0, False),
    "no-tool-en": (1, 0, 0, 0, False),
    "no-tool-zh": (1, 0, 0, 0, False),
    "skill-zh": (4, 3, 0, 0, False),
    "long-en": (9, 8, 0, 0, True),
    "done-en": (5, 3, 0, 1, False),
}


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_the_correct_path_is_done_with_exact_metrics(task: Task) -> None:
    run = await run_task(task, fake(task))
    m = run.metrics
    assert (m.done, m.why, m.outcome) == (True, "done", "completed")
    assert (m.turns, m.tool_calls, m.invalid_calls, m.auto_continues, m.compacted) == CORRECT[
        task.id
    ]


WRONG: dict[str, tuple[tuple[AssistantTurn, ...], str]] = {
    "lookup-en": ((say("It is delivered."),), "never looked A17 up"),
    "lookup-zh": (
        (use(("c1", "get_order", {"order_id": "D03"})), say("订单已送达。")),
        "回答里没有已发货",
    ),
    "chain-en": (
        (use(("c1", "list_orders", {"customer_id": "C2"})), say("268.90 in total.")),
        "did not find the customer, then list orders",
    ),
    "chain-zh": (
        (
            use(("c1", "find_customer", {"email": "wang.fang@example.cn"})),
            use(("c2", "list_orders", {"customer_id": "C3"})),
            say("被取消的是 D01。"),
        ),
        "回答里没有 D02",
    ),
    "act-en": (
        (
            use(("c1", "refund", {"order_id": "B03", "amount": 100})),
            use(("c2", "refund", {"order_id": "B03", "amount": 89})),
            say("Refunded B03."),
        ),
        "2 refunds of B03, not 1",
    ),
    "act-zh": (
        (use(("c1", "refund", {"order_id": "A17", "amount": 3})), say("已退款。")),
        "A18 退了 0 次，不是 1 次",
    ),
    "recover-en": (
        (use(("c1", "get_order", {"order_id": "17"})), say("Order 17 was delivered.")),
        "the answer does not say shipped",
    ),
    "no-tool-en": (
        (use(("c1", "get_order", {"order_id": "A17"})), say("You have 30 days.")),
        "called 1 tools",
    ),
    "no-tool-zh": ((say("早上 9 点以后。"),), "回答里没有 9 点到 18 点"),
    "skill-zh": (
        (
            use(("c1", "refund", {"order_id": "D01", "amount": 268})),
            use(("c2", "send_note", {"customer_id": "C3", "text": "已全额退款。"})),
            say("已退款。"),
        ),
        "没有读退款政策",
    ),
    "long-en": (
        (
            *(use((f"g{i}", "get_order", {"order_id": o})) for i, o in enumerate(LONG_IDS)),
            say("The total is 369.90."),  # D03 kept: the rule from the start was lost
        ),
        "the total is not 249.90",
    ),
    "done-en": (
        (
            use(("c1", "check_stock", {"sku": "CHAIR-2"})),
            say("It is out of stock."),
            say("It is out of stock."),
            say("It is out of stock."),
        ),
        # the nudges ran out with the condition still open: the harness fails the run
        "run failed (incomplete)",
    ),
}


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_a_wrong_path_is_not_done_and_says_why(task: Task) -> None:
    script, why = WRONG[task.id]
    run = await run_task(task, fake(task, script))
    assert (run.metrics.done, run.metrics.why) == (False, why)


async def test_a_call_the_harness_refuses_counts_as_invalid() -> None:
    task = BY_ID["act-en"]
    script = (
        use(("c1", "refund", {"order_id": "B03", "amount": -5})),  # amount must be above 0
        use(("c2", "no_such_tool", {})),
        use(("c3", "refund", {"order_id": "B03", "amount": "189.00"})),
        say("Refunded 189.00."),
    )
    m = (await run_task(task, fake(task, script))).metrics
    assert (m.done, m.tool_calls, m.invalid_calls) == (True, 3, 2)


async def test_a_run_that_does_not_complete_is_not_done() -> None:
    task = BY_ID["lookup-en"]
    script = tuple(use((f"c{i}", "get_order", {"order_id": "A17"})) for i in range(20))
    m = (await run_task(task, fake(task, script))).metrics
    assert (m.done, m.why, m.turns) == (False, "run failed (budget_turns)", 16)


async def test_tokens_count_every_model_call_as_the_budget_does() -> None:
    # simplify review 2026-11-21: the summaries' calls are billed too (design §5 rule 6)
    task = BY_ID["long-en"]
    model = Billed(
        [Usage(input=10, output=1)] * 40,
        agent=list(task.script),
        compact=lambda asked: say(task.summary or ""),
    )
    m = (await run_task(task, model)).metrics
    calls = len(model.turn_calls)
    assert calls > m.turns  # at least one summary was written
    assert (m.input_tokens, m.output_tokens) == (10 * calls, calls)


# --- Codex review 2026-11-21: answers are matched as values, not substrings ---


@pytest.mark.parametrize(
    ("answer", "options", "expected"),
    [
        ("You have 30 days.", ("30",), True),
        ("You have 300 days.", ("30",), False),
        ("The total is 268.90.", ("268.9", "268.90"), True),
        ("The total is 1268.90.", ("268.9", "268.90"), False),
        ("It was D021.", ("D02",), False),
        ("Order D02, the tea.", ("D02",), True),
        ("It has not shipped yet.", ("shipped",), False),
        ("It hasn't shipped.", ("shipped",), False),
        ("Not delivered: it shipped yesterday.", ("shipped",), True),
        ("Not delivered: it shipped yesterday.", ("delivered",), False),
        ("工作日 9 点到 18 点。", ("9",), True),
        ("工作日 19 点前。", ("9",), False),
        ("订单已发货。", ("已发货",), True),
        ("订单还没有已发货。", ("已发货",), False),
        # second Codex pass: a negation reaches the value only right before it
        ("Not only shipped: it arrives today.", ("shipped",), True),
        ("It has not yet shipped.", ("shipped",), False),
        ("不是已发货吗？", ("已发货",), False),
        ("不是未发货，而是已发货。", ("已发货",), True),
    ],
)
def test_an_answer_says_a_value_only_as_a_whole_and_not_negated(
    answer: str, options: tuple[str, ...], expected: bool
) -> None:
    from lite_agent.evals.tasks import says

    assert says(answer, *options) is expected


async def test_recover_accepts_a_status_named_against_another() -> None:
    task = BY_ID["recover-en"]
    script = (*task.script[:-1], say("Order A17 is not delivered yet: it has shipped."))
    assert (await run_task(task, fake(task, script))).metrics.why == "done"


async def test_done_en_keeps_going_until_the_note_is_right() -> None:
    # a wrong note does not end the run: the condition is the check's, not "any note"
    task = BY_ID["done-en"]
    script = (
        use(("c1", "check_stock", {"sku": "CHAIR-2"})),
        use(("c2", "send_note", {"customer_id": "C2", "text": "Your chair is ready."})),
        say("Done."),
        use(("c3", "send_note", {"customer_id": "C2", "text": "Sorry: it is out of stock."})),
        say("Corrected."),
    )
    m = (await run_task(task, fake(task, script))).metrics
    assert (m.done, m.auto_continues, m.turns) == (True, 1, 5)


async def test_usage_a_model_did_not_report_is_counted_as_unknown() -> None:
    task = BY_ID["lookup-en"]
    m = (await run_task(task, fake(task))).metrics  # the fake reports no token counts
    assert (m.input_tokens, m.unknown_usage) == (0, 2)
    billed = Billed([Usage(input=5, output=1)] * 2, agent=list(task.script))
    m = (await run_task(task, billed)).metrics
    assert (m.input_tokens, m.unknown_usage) == (10, 0)
