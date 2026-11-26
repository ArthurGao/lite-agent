"""Compaction (design §6): when the history a run would send grows past what its model should
be given, the older part is summarised by another role and a `compaction` event records which
seqs the summary stands for. Nothing is rewritten: the projection shows the summary in their
place, and every covered event stays in the log (design §4)."""

import json
import math
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lite_context import CharEstimate
from lite_context.compaction import ASK, END, SUMMARY, render_transcript
from lite_model import AssistantTurn, Message, TurnMessage

from lite_agent.events import Event, ModelTurn
from lite_agent.neutral import arguments, to_neutral
from lite_agent.projection import Projection, latest_compaction

# The prompt texts and the summary's framing are lite-context's (its §6), which took them from
# here (S3b) word for word: SUMMARY is wrapped around the summary when the event is written, so
# the log holds exactly what the model is shown and the projection adds nothing (design §4);
# ASK says which header lines are real, since a tool result could imitate a user turn; END
# repeats the instruction after the transcript (with it only in the system message, qwen3:8b,
# thinking off, answered the transcript's last question in 6 runs of 6; with it, 0 of 6:
# docs/lite-agent-design.md §15, S3b record).

# A header the summariser copied from the transcript into its reply: any request's mark, not
# only the one just sent (lite-agent's rule; lite-context drops only the exact mark).
_HEADER = re.compile(
    r"^[ \t]*---[ \t]+[0-9a-f]{16,}[ \t]+[^\n]*?---[ \t]*\r?$", re.MULTILINE | re.IGNORECASE
)
CHARS_PER_TOKEN = CharEstimate().chars_per_token  # 3.5, lite-context's estimate; not a count


def clean(summary: str) -> str:
    """The summary without header lines copied from the transcript (a summariser imitates the
    format it is shown: qwen3:8b did in 6 runs of 6). Empty when nothing else is left."""
    return _HEADER.sub("", summary).strip()


def estimate_tokens(projection: Projection) -> int:
    """About how many tokens the request holds: its characters over `CHARS_PER_TOKEN`."""
    chars = 0
    for m in projection.messages:
        if isinstance(m, Message):
            chars += len(m.content)
        elif isinstance(m, AssistantTurn):
            chars += len(m.text)
            chars += sum(len(c.name) + len(arguments(c)) for c in m.tool_calls)
        else:
            chars += sum(len(r.content) for r in m.results)
    for t in projection.tools:
        chars += len(t.name) + len(t.description) + len(json.dumps(t.parameters))
    return math.ceil(chars / CHARS_PER_TOKEN)


def _new_mark() -> str:
    """A fresh header token for one summary request: text inside a part cannot know it."""
    return secrets.token_hex(12)


def render(history: Sequence[TurnMessage], *, boundary: str | None = None) -> str:
    """The covered history as plain text for the role that summarises it, in the order it was
    said (lite-context's `render_transcript`). Each part opens with `--- <boundary> <who> ---`;
    `boundary` is a fresh random token unless one is given. The history is the conversation
    without the system prompt (`projection.history`); a system message is a `ValueError`."""
    return render_transcript(boundary or _new_mark(), to_neutral(history))


def frame(reply: str) -> str:
    """What the model is shown for the summariser's reply: lite-context's `SUMMARY` around it,
    once copied header lines are gone (`clean`); "" when nothing is left (`EmptySummary`)."""
    summary = clean(reply)
    return SUMMARY.format(summary=summary) if summary else ""


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
        mark = _new_mark()
        transcript = render(history, boundary=mark) + END.format(mark=mark)
        return (Message("system", ASK.format(mark=mark)), Message("user", transcript))


__all__ = [
    "ASK",
    "CHARS_PER_TOKEN",
    "END",
    "SUMMARY",
    "Compactor",
    "SummaryCompactor",
    "clean",
    "estimate_tokens",
    "frame",
    "render",
]
