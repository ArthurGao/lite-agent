"""Shared pieces for the loop tests: fixed time and ids so event sequences compare exactly,
a few tools that record their calls, and lite-model's fake with usage added."""

import asyncio
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from lite_model import (
    AssistantTurn,
    Opaque,
    Text,
    ToolCall,
    ToolSpec,
    TurnMessage,
    TurnReply,
    Usage,
)
from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, FunctionTool, InMemorySessionStore, Tool, ToolContext
from lite_agent.events import Event, ToolResultEvent

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
# The provenance lite-model's fake gives the "agent" role, read from the fake itself.
WHO = asyncio.run(
    AsyncFakeModel(agent=AssistantTurn((Text("x"),))).turn("agent", [], [])
).provenance
THINKING = {"type": "thinking", "thinking": "", "signature": "sig/AbC+123=="}


def counter(prefix: str = "id") -> Callable[[], str]:
    """`id1`, `id2`, ...: the first is the session, the second the first run."""
    n = 0

    def next_id() -> str:
        nonlocal n
        n += 1
        return f"{prefix}{n}"

    return next_id


def say(text: str) -> AssistantTurn:
    return AssistantTurn((Text(text),))


def call(*calls: tuple[str, str, Mapping[str, Any] | None], text: str = "") -> AssistantTurn:
    """A turn that calls tools: `call(("c1", "get_time", {}))`."""
    blocks: list[Text | ToolCall | Opaque] = [Text(text)] if text else []
    return AssistantTurn((*blocks, *(ToolCall(i, n, a) for i, n, a in calls)))


class Recording:
    """Tools that note every call, so a test can tell what ran and in which order."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.contexts: list[ToolContext] = []

    def tool(
        self,
        name: str,
        result: str | Exception = "ok",
        *,
        terminal: bool = False,
        delay: float = 0,
        timeout_s: float | None = None,
    ) -> FunctionTool:
        """`delay`: seconds the tool sleeps after noting the call (for timeout tests)."""

        async def fn(args: Mapping[str, Any], ctx: ToolContext) -> str:
            self.ran.append((name, dict(args)))
            self.contexts.append(ctx)
            await asyncio.sleep(delay)
            if isinstance(result, Exception):
                raise result
            return result

        return FunctionTool(
            name,
            f"The {name} tool.",
            {"type": "object"},
            fn,
            terminal=terminal,
            timeout_s=timeout_s,
        )


class Billed(AsyncFakeModel):
    """lite-model's fake, whose replies always carry `Usage()`, with one usage per successful
    turn. Exceptions bring their own (`EmptyReply(..., usage=...)`)."""

    def __init__(self, usages: Sequence[Usage], **scripts: Any) -> None:
        super().__init__(**scripts)
        self._usages = list(usages)

    async def turn(
        self, role: str, messages: Sequence[TurnMessage], tools: Sequence[ToolSpec]
    ) -> TurnReply:
        reply = await super().turn(role, messages, tools)
        return dataclasses.replace(reply, usage=self._usages.pop(0))


def agent(model: Any, *tools: Tool, **fields: Any) -> Agent:
    """An agent with fixed time and counting ids on a fresh in-memory store."""
    defaults: dict[str, Any] = {
        "role": "agent",
        "model": model,
        "tools": tools,
        "sessions": InMemorySessionStore(),
        "clock": lambda: T0,
        "ids": counter(),
    }
    return Agent(**(defaults | fields))


async def events_of(a: Agent, session_id: str) -> list[Event]:
    return list(await a.sessions.read(session_id))


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]


def results_of(events: Sequence[Event]) -> list[ToolResultEvent]:
    return [e for e in events if isinstance(e, ToolResultEvent)]
