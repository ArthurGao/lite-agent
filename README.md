# lite-agent

**Status: S3a** (stage plan: design §15). The core runs: a tool loop over a
[lite-model](https://github.com/ArthurGao/lite-model) role, every step recorded in an append-only
event log, and each request derived from that log. Tool arguments are validated, results are cut to
a budget, and turn, token and cost budgets stop a session. Gates can refuse a call or stop for a person's
approval, a run can pause for a tool the host executes, and `resume()` picks a paused or interrupted
session up from its log. A completion condition can send an unfinished task back to
the model, and a fallback role takes over, visibly and once per run, when a model cannot be reached.
Skills (a directory of `SKILL.md` files) are indexed in the system prompt and loaded on demand,
and a `Memory` gives the model `recall` and `remember`. Sessions live in memory only; compaction and
a persistent store are later stages. Not released yet.

```python
import asyncio

from lite_model import ModelsConfig, RolePolicy, RoleSpec, build_async
from pydantic import BaseModel

from lite_agent import Agent, ToolContext, tool


class Where(BaseModel):
    city: str


@tool(description="Current local time in a city.")
async def get_time(args: Where, ctx: ToolContext) -> str:
    return "12:00"


async def main() -> None:
    # any lite-model configuration; this one is a local Ollama model that can call tools
    config = ModelsConfig(backend="ollama", model="qwen3:8b")
    model = build_async(config, RoleSpec({"agent": RolePolicy()}))
    agent = Agent(role="agent", model=model, tools=[get_time])

    result = await agent.run("What time is it in Paris?")
    print(result.outcome, result.text)                        # completed ...
    for event in await agent.sessions.read(result.session_id):
        print(event.seq, event.type)                          # the log


asyncio.run(main())
```

The design, its decisions and the stage plan are in [`docs/lite-agent-design.md`](docs/lite-agent-design.md) (Chinese). License: Apache-2.0.

## Developing

```
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
```

Tests never open a socket and never call a real model; model turns are scripted with lite-model's
`AsyncFakeModel`. Agent guides: [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md).
