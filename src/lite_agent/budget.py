"""Budgets and usage accounting (design §5 rule 6)."""

from dataclasses import dataclass

from lite_model import Usage


@dataclass(frozen=True)
class Budget:
    """`turns`: model calls allowed in the whole session, failed ones included."""

    turns: int = 30

    def __post_init__(self) -> None:
        if self.turns < 1:
            raise ValueError("Budget.turns must be at least 1")


def _add[N: (int, float)](a: N | None, b: N | None) -> N | None:
    if a is None or b is None:
        return a if b is None else b
    return a + b


def add_usage(total: Usage, spent: Usage | None) -> Usage:
    """`total` plus one call's usage. An unknown part stays out of the sum: it is never
    estimated, so a total can be lower than what was really spent, never invented."""
    if spent is None:
        return total
    return Usage(
        input=_add(total.input, spent.input),
        output=_add(total.output, spent.output),
        reasoning=_add(total.reasoning, spent.reasoning),
        cost_usd=_add(total.cost_usd, spent.cost_usd),
    )
