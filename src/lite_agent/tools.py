"""Tools as the loop sees them (design §7). S1 has the minimum: a name, a JSON Schema for the
model and an async function. Validation against a Pydantic model, `@tool`, timeouts and gates
come in S2."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    run_id: str
    # Stable for the session: hand it to a side-effecting system as the idempotency key (§8).
    call_id: str


class Tool(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters(self) -> Mapping[str, Any]:
        """JSON Schema of the input, sent to the model as is."""
        ...

    @property
    def terminal(self) -> bool:
        """A successful result may end the run (design §5 rule 2)."""
        ...

    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    """A `Tool` around an async function."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(hash=False)
    fn: Callable[[Mapping[str, Any], ToolContext], Awaitable[str]]
    terminal: bool = False

    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str:
        return await self.fn(args, ctx)
