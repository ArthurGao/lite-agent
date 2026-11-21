"""Budgets and usage accounting (design §5 rule 6)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from lite_model import Usage

from lite_agent.events import Event, ModelErrorEvent, ModelTurn


@dataclass(frozen=True)
class Budget:
    """What a session may spend, and the limits on one tool call. `turns`, `tokens` and
    `cost_usd` are the whole session's, summed from its log, failed calls included; pausing and
    resuming never grants a fresh budget."""

    turns: int = 30  # model calls
    tokens: int | None = None  # input + output
    cost_usd: float | None = None
    tool_result_chars: int = 20_000  # a longer result is cut when it is written
    tool_timeout_s: float = 60.0  # for a tool without its own `timeout_s`
    auto_continues: int = 2  # per run (design §5 rule 3)
    skills_index_chars: int = 4_000  # the skills index in the system prompt may not pass this

    def __post_init__(self) -> None:
        if self.turns < 1:
            raise ValueError("Budget.turns must be at least 1")
        if self.tokens is not None and self.tokens < 1:
            raise ValueError("Budget.tokens must be at least 1")
        if self.cost_usd is not None and not (math.isfinite(self.cost_usd) and self.cost_usd > 0):
            raise ValueError("Budget.cost_usd must be a positive number")
        if self.tool_result_chars < 1:
            raise ValueError("Budget.tool_result_chars must be at least 1")
        if not (math.isfinite(self.tool_timeout_s) and self.tool_timeout_s > 0):
            raise ValueError("Budget.tool_timeout_s must be a positive number")
        if self.auto_continues < 0:
            raise ValueError("Budget.auto_continues must not be negative")
        if self.skills_index_chars < 1:
            raise ValueError("Budget.skills_index_chars must be at least 1")


@dataclass(frozen=True)
class Spent:
    turns: int
    tokens: int
    cost_usd: float


def spent(events: Sequence[Event]) -> Spent:
    """What the session's model calls have used, read from the log. A call whose usage is
    unknown counts as a turn and adds nothing else: usage is never estimated."""
    turns, tokens, cost = 0, 0, 0.0
    for e in events:
        if not isinstance(e, ModelTurn | ModelErrorEvent):
            continue
        turns += 1
        if e.usage is not None:
            tokens += (e.usage.input or 0) + (e.usage.output or 0)
            cost += e.usage.cost_usd or 0.0
    return Spent(turns, tokens, cost)


def _add[N: (int, float)](a: N | None, b: N | None) -> N | None:
    if a is None or b is None:
        return a if b is None else b
    return a + b


def add_usage(total: Usage, call: Usage | None) -> Usage:
    """`total` plus one call's usage. An unknown part stays out of the sum: it is never
    estimated, so a total can be lower than what was really spent, never invented."""
    if call is None:
        return total
    return Usage(
        input=_add(total.input, call.input),
        output=_add(total.output, call.output),
        reasoning=_add(total.reasoning, call.reasoning),
        cost_usd=_add(total.cost_usd, call.cost_usd),
    )
