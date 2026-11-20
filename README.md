# lite-agent

**Status: S1.** The core runs: a tool loop over a
[lite-model](https://github.com/ArthurGao/lite-model) role, every step recorded in an append-only
event log, and each request derived from that log. Sessions live in memory only; validation of tool
arguments, approval gates, pausing, compaction and a persistent store are later stages.

```python
from lite_agent import Agent, FunctionTool

async def get_time(args, ctx):
    return "12:00"

agent = Agent(
    role="agent",                      # a lite-model role
    model=model,                       # lite_model.build_async(config, roles)
    tools=[FunctionTool("get_time", "Current local time.", {"type": "object"}, get_time)],
)
result = await agent.run("What time is it?")
result.outcome, result.text            # "completed", "..."
await agent.sessions.read(result.session_id)   # the events
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
