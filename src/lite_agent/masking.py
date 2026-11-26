"""Masking (design §6, V=2): once the request a run would send grows past what its model should
be given, old tool results are shown as a placeholder. lite-context's `fit()` with
`MaskToolResults` decides which; a `tool_results_masked` event records each one and the text
shown instead, so the projection adds nothing (design §4). It needs no model call, so it runs
before compaction; it never fails a run: what it cannot make fit is left to compaction."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from lite_context import (
    Budget,
    ContextState,
    LiteContextError,
    StaleState,
    ToolResult,
    fit,
    with_keys,
)
from lite_context import Text as NText
from lite_context.steps import MaskToolResults
from lite_model import AssistantTurn, ToolResults

from lite_agent.events import Event, MaskedResult, RunStarted, ToolResultsMasked
from lite_agent.neutral import to_neutral
from lite_agent.projection import Shown, keyed_history, masks


@dataclass(frozen=True)
class MaskDecision:
    """What a `tool_results_masked` event will record."""

    masked: tuple[MaskedResult, ...]
    state: dict[str, Any]
    tokens_before: int
    tokens_after: int


class Masker(Protocol):
    def check(self, roles: Sequence[str]) -> None:
        """Raise ValueError unless it can decide for each of the roles the agent may call."""
        ...

    def mask(self, events: Sequence[Event], role: str) -> MaskDecision | None:
        """The results to mask before the next call to `role`; None when nothing new is."""
        ...


@dataclass(frozen=True)
class ToolResultMasker:
    """Masks the oldest tool results, keeping the last `keep_last` batches whole, once the
    estimated request (system prompt and tool definitions included) passes 90% of the role's
    `context_tokens`; down to 70% (lite-context's water marks)."""

    context_tokens: Mapping[str, int] = field(hash=False)  # role -> estimated tokens
    keep_last: int = 3  # the newest batches of results, always shown whole
    # Tools whose results are instructions or notes the model goes on using, not data it read
    # once: never masked. A masked skill cost qwen3:8b its skill task (3/3 -> 0/3; design §15).
    keep_tools: tuple[str, ...] = ("load_skill", "recall")

    def __post_init__(self) -> None:
        if self.keep_last < 1:
            raise ValueError("keep_last must be at least 1: the newest results are always shown")
        for role, limit in self.context_tokens.items():
            if limit < 1:
                raise ValueError(f"context_tokens[{role!r}] must be at least 1")

    def check(self, roles: Sequence[str]) -> None:
        missing = [r for r in roles if r not in self.context_tokens]
        if missing:
            raise ValueError(f"masking's context_tokens has no threshold for: {', '.join(missing)}")

    def _kept(self, shown: Sequence[Shown]) -> list[bool]:
        """For each message, whether it holds a result of a `keep_tools` tool (pinned for
        lite-context, which never cuts a pinned message)."""
        names: dict[str, str] = {}  # the open turn's call ids -> tool names
        kept: list[bool] = []
        for s in shown:
            if isinstance(s.message, AssistantTurn):
                names = {c.id: c.name for c in s.message.tool_calls}
            kept.append(
                isinstance(s.message, ToolResults)
                and any(names.get(r.call_id) in self.keep_tools for r in s.message.results)
            )
        return kept

    def mask(self, events: Sequence[Event], role: str) -> MaskDecision | None:
        shown = keyed_history(events, masked=False)  # measured as written: fit() is pure on it
        if not shown:
            return None
        keyed = with_keys(to_neutral([s.message for s in shown]), [s.key for s in shown])
        messages = [
            replace(m, pinned=True) if kept else m
            for m, kept in zip(keyed, self._kept(shown), strict=True)
        ]
        budget, fixed = Budget(self.context_tokens[role], 0), _fixed(events)
        steps = [MaskToolResults(keep_last=self.keep_last)]
        try:
            try:
                fitted = fit(
                    messages, budget=budget, fixed=fixed, steps=steps, state=_state(events)
                )
            except StaleState:  # the history the cut was measured on changed: measure again
                fitted = fit(messages, budget=budget, fixed=fixed, steps=steps)
        except LiteContextError:
            # DoesNotFit: masking alone cannot make it fit, which is compaction's turn. Any
            # other refusal of this history masks nothing either: masking never fails a run.
            return None
        done = masks(events).keys()
        by_key = {s.key: s for s in shown}
        masked: list[MaskedResult] = []
        for out in fitted.messages:
            source = by_key.get(out.key or "")
            if source is None or not isinstance(source.message, ToolResults):
                continue
            results = [p for p in out.parts if isinstance(p, ToolResult)]
            for seq, before, part in zip(
                source.results, source.message.results, results, strict=True
            ):
                text = "".join(c.text for c in part.content if isinstance(c, NText))
                if seq not in done and text != before.content:
                    masked.append(MaskedResult(seq=seq, shown=text))
        if not masked:
            return None
        report = fitted.report
        return MaskDecision(
            masked=tuple(masked),
            state=json.loads(fitted.state.to_json()),
            tokens_before=report.fixed_tokens + report.tokens_before,
            tokens_after=report.fixed_tokens + report.tokens_after,
        )


def _fixed(events: Sequence[Event]) -> list[str]:
    """The latest run's system prompt and tool definitions: counted, never cut."""
    snapshot = next((e for e in reversed(events) if isinstance(e, RunStarted)), None)
    if snapshot is None:
        return []
    tools = json.dumps([t.model_dump(mode="json") for t in snapshot.tools])
    return [snapshot.system, tools]


def _state(events: Sequence[Event]) -> ContextState | None:
    """The cut the last masking measured. After a compaction it no longer matches the history
    (a summary stands for its start): `fit()` says so (`StaleState`) and the cut is measured
    again, as lite-context starts its cuts again after a summary."""
    last = next((e for e in reversed(events) if isinstance(e, ToolResultsMasked)), None)
    if last is None:
        return None
    try:
        return ContextState.from_json(json.dumps(last.state))
    except ValueError:  # a state this lite-context cannot read (a newer one): measure again
        return None


__all__ = ["MaskDecision", "Masker", "ToolResultMasker"]
