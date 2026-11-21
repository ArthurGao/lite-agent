"""Compaction (design §6): when the history a run would send grows past what its model should
be given, the older part is summarised by another role and a `compaction` event records which
seqs the summary stands for. Nothing is rewritten: the projection shows the summary in their
place, and every covered event stays in the log (design §4)."""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lite_model import AssistantTurn, Message, Text, ToolCall, ToolResults, TurnMessage

from lite_agent.events import Event, ModelTurn
from lite_agent.projection import Projection, latest_compaction

# Wrapped around the summary when the event is written, so the log holds exactly what the model
# is shown and the projection adds nothing (design §4).
SUMMARY = (
    "This conversation is long, so its earlier part was replaced by the summary below. "
    "What follows the summary is the conversation as it continued.\n\n{summary}"
)
ASK = (
    "You are given the earlier part of a conversation between a user and an assistant that "
    "uses tools. Write a summary the assistant can continue from: what was asked, what was "
    "done and found (keep exact names, numbers, paths and commands), what was decided, and "
    "what is still open. Do not address the user and do not continue the conversation."
)
CHARS_PER_TOKEN = 3.5  # a rough figure for mixed text, as cairn uses; an estimate, not a count


def _arguments(call: ToolCall) -> str:
    """A call's arguments as text: as parsed, or as received when they never parsed."""
    if call.arguments is None:
        return call.raw_arguments or ""
    return json.dumps(call.arguments)


def estimate_tokens(projection: Projection) -> int:
    """About how many tokens the request holds: its characters over `CHARS_PER_TOKEN`."""
    chars = 0
    for m in projection.messages:
        if isinstance(m, Message):
            chars += len(m.content)
        elif isinstance(m, AssistantTurn):
            chars += len(m.text)
            chars += sum(len(c.name) + len(_arguments(c)) for c in m.tool_calls)
        else:
            chars += sum(len(r.content) for r in m.results)
    for t in projection.tools:
        chars += len(t.name) + len(t.description) + len(json.dumps(t.parameters))
    return math.ceil(chars / CHARS_PER_TOKEN)


def render(history: Sequence[TurnMessage]) -> str:
    """The covered history as plain text for the role that summarises it."""
    lines: list[str] = []
    for m in history:
        if isinstance(m, Message):
            lines.append(f"[{m.role}]\n{m.content}")
        elif isinstance(m, AssistantTurn):
            said: list[str] = []
            for block in m.blocks:  # in the order the model produced them
                if isinstance(block, Text):
                    said.append(block.text)
                elif isinstance(block, ToolCall):
                    given = _arguments(block)
                    said.append(f"(called {block.name} with {given}; call id {block.id})")
            lines.append("[assistant]\n" + "\n".join(said))
        elif isinstance(m, ToolResults):
            for r in m.results:
                kind = "tool error" if r.is_error else "tool result"
                lines.append(f"[{kind} for call id {r.call_id}]\n{r.content}")
    return "\n\n".join(lines)


class Compactor(Protocol):
    def check(self, roles: Sequence[str]) -> None:
        """Raise ValueError unless it can decide for each of the roles the agent may call."""
        ...

    def due(self, projection: Projection, role: str) -> bool:
        """Whether the request about to go to `role` should be made smaller first."""
        ...

    def cut(self, events: Sequence[Event]) -> int | None:
        """The last seq a summary should cover; None when there is nothing it may cut. Never
        between a turn and its tool results, and never the last turn."""
        ...

    def prompt(self, history: Sequence[TurnMessage]) -> Sequence[Message]:
        """What the compact role is asked, given the history to be covered."""
        ...


@dataclass(frozen=True)
class SummaryCompactor:
    """Summarises everything before the last `keep_turns` model turns, once the estimated size
    of the request passes the threshold of the role about to be called."""

    context_tokens: Mapping[str, int] = field(hash=False)  # role -> estimated tokens to stay under
    keep_turns: int = 6  # model turns, each with its tool results, left as they are

    def __post_init__(self) -> None:
        if self.keep_turns < 1:
            raise ValueError("keep_turns must be at least 1: the last turn is never summarised")
        for role, limit in self.context_tokens.items():
            if limit < 1:
                raise ValueError(f"context_tokens[{role!r}] must be at least 1")

    def check(self, roles: Sequence[str]) -> None:
        missing = [r for r in roles if r not in self.context_tokens]
        if missing:
            raise ValueError(f"context_tokens has no threshold for: {', '.join(missing)}")

    def due(self, projection: Projection, role: str) -> bool:
        return estimate_tokens(projection) > self.context_tokens[role]

    def cut(self, events: Sequence[Event]) -> int | None:
        latest = latest_compaction(events)
        covered = latest.covers[1] if latest is not None else 0
        turns = [e for e in events if isinstance(e, ModelTurn) and e.seq > covered]
        if len(turns) <= self.keep_turns:
            return None
        # everything before the first turn that is kept: a turn's results follow it, so the
        # boundary falls between whole turns
        return turns[-self.keep_turns].seq - 1

    def prompt(self, history: Sequence[TurnMessage]) -> Sequence[Message]:
        return (Message("system", ASK), Message("user", render(history)))
