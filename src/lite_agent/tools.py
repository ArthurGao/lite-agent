"""Tools as the loop sees them (design §7): a name, a JSON Schema for the model, a check of the
arguments and an async function. `@tool` builds one from a function that takes a Pydantic model;
`FunctionTool` wraps a function that takes the raw arguments."""

import inspect
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, get_type_hints

from pydantic import BaseModel, ValidationError


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

    @property
    def idempotent(self) -> bool:
        """Safe to run again with the same call id after an interrupted run (design §8)."""
        ...

    @property
    def timeout_s(self) -> float | None:
        """Seconds the call may take; None: the budget's `tool_timeout_s`. The call is cancelled
        at its next `await`: a tool that blocks the event loop cannot be stopped."""
        ...

    @property
    def external(self) -> bool:
        """The host executes it: the run pauses and the result comes back on resume (§7)."""
        ...

    def validate(self, args: Mapping[str, Any]) -> str | None:
        """What is wrong with `args`, for the model to read; None when they are fine. Called
        before anything is written or run."""
        ...

    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str: ...


def _check_timeout(timeout_s: float | None) -> None:
    if timeout_s is not None and not (math.isfinite(timeout_s) and timeout_s > 0):
        raise ValueError("timeout_s must be a positive number (None: the budget's default)")


@dataclass(frozen=True)
class FunctionTool:
    """A `Tool` around an async function that takes the arguments as they came. Nothing is
    validated: the function sees whatever the model sent."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(hash=False)
    fn: Callable[[Mapping[str, Any], ToolContext], Awaitable[str]]
    terminal: bool = False
    idempotent: bool = False
    timeout_s: float | None = None
    external: bool = False

    def __post_init__(self) -> None:
        _check_timeout(self.timeout_s)

    def validate(self, args: Mapping[str, Any]) -> str | None:
        return None

    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str:
        return await self.fn(args, ctx)


@dataclass(frozen=True)
class ModelTool[M: BaseModel]:
    """What `@tool` builds: the arguments are validated against `model` and reach the function
    as an instance of it."""

    name: str
    description: str
    model: type[M]
    fn: Callable[[M, ToolContext], Awaitable[str]]
    terminal: bool = False
    idempotent: bool = False
    timeout_s: float | None = None
    external: bool = False

    def __post_init__(self) -> None:
        _check_timeout(self.timeout_s)

    @property
    def parameters(self) -> Mapping[str, Any]:
        return self.model.model_json_schema()

    def validate(self, args: Mapping[str, Any]) -> str | None:
        """Field paths and reasons only: the input is not echoed back (it can be large, and it
        is the model's own)."""
        try:
            self.model.model_validate(args)
        except ValidationError as exc:
            problems = (
                f"- {'.'.join(str(part) for part in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)
            )
            return "The arguments are not valid. Nothing was executed.\n" + "\n".join(problems)
        return None

    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str:
        return await self.fn(self.model.model_validate(args), ctx)


def tool[M: BaseModel](
    *,
    description: str,
    name: str | None = None,
    terminal: bool = False,
    idempotent: bool = False,
    timeout_s: float | None = None,
) -> Callable[[Callable[[M, ToolContext], Awaitable[str]]], ModelTool[M]]:
    """Make a tool of `async def f(args: SomeModel, ctx: ToolContext) -> str`. The model is read
    from the first parameter's annotation; its JSON Schema is what the model is shown."""

    def wrap(fn: Callable[[M, ToolContext], Awaitable[str]]) -> ModelTool[M]:
        who = fn.__name__
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@tool: {who} must be an async function")
        parameters = list(inspect.signature(fn).parameters)
        if len(parameters) != 2:
            raise TypeError(f"@tool: {who} must take (args, ctx)")
        model = get_type_hints(fn).get(parameters[0])
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(f"@tool: the first parameter of {who} must be a pydantic BaseModel")
        return ModelTool(
            name or fn.__name__,
            description,
            cast(type[M], model),
            fn,
            terminal=terminal,
            idempotent=idempotent,
            timeout_s=timeout_s,
        )

    return wrap
