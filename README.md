# lite-agent

**v0.1.0** (2026-11-21; stage plan: design §15). The core runs: a tool loop over a
[lite-model](https://github.com/ArthurGao/lite-model) role, every step recorded in an append-only
event log, and each request derived from that log. Tool arguments are validated, results are cut to
a budget, and turn, token and cost budgets stop a session. Gates can refuse a call or stop for a person's
approval, a run can pause for a tool the host executes, and `resume()` picks a paused or interrupted
session up from its log. A completion condition can send an unfinished task back to
the model, and a fallback role takes over, visibly and once per run, when a model cannot be reached.
Skills (a directory of `SKILL.md` files) are indexed in the system prompt and loaded on demand,
and a `Memory` gives the model `recall` and `remember`. A long history is summarised before it
outgrows the model: the summary is one more event that says what it covers, and nothing is rewritten.
Sessions live in memory by default, or in PostgreSQL with the `pg` extra (`PgSessionStore`, its own
schema, a log the database refuses to rewrite). A session resumes or forks in any process, and
`copy_session` moves one between stores. `liteagent` (with `pg`) applies the migrations and lists,
shows and forks sessions. An evaluation (`lite_agent.evals`, extra `eval`) runs a fixed task set
in a synthetic shop on lite-eval; the baseline on local qwen3 models is in design §15 (S5b).

## Install

From the GitHub Release (lite-agent is not on PyPI; it brings lite-model 0.4.0 along):

```
uv add "lite-agent @ https://github.com/ArthurGao/lite-agent/releases/download/v0.1.0/lite_agent-0.1.0-py3-none-any.whl"
uv add "lite-agent[pg] @ https://github.com/ArthurGao/lite-agent/releases/download/v0.1.0/lite_agent-0.1.0-py3-none-any.whl"     # + the PostgreSQL session store and the liteagent CLI
```

## Example

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

## Over HTTP

For a host in another language (`lite-agent[server]`): name the agents in `agents.toml`, each a
Python factory that builds an `Agent` on the shared store, and serve them on loopback.

```
# agents.toml
[store]
kind = "postgres"                     # or "memory"
[agents.clock]
factory = "myapp.agents:clock"        # def clock(sessions: SessionStore) -> Agent

liteagent serve --agents agents.toml  # 127.0.0.1:8730; the schema is at /openapi.json
curl -s -X POST localhost:8730/runs -H 'content-type: application/json' \
     -d '{"agent": "clock", "task": "What time is it in Paris?"}'      # {"session_id", "run_id"}
curl -sN localhost:8730/sessions/<session_id>/events/stream          # SSE until the run ends
```

A paused run goes on with `POST /sessions/<id>/resume` (`expected_seq`, `approve`, `results`);
`POST /sessions/<id>/fork` branches a session. Design §10 has the protocol.

## Durable runs (Temporal)

With `lite-agent[temporal]`, a run is a Temporal workflow: a worker that dies mid-run is replaced and
the run goes on from its log, a paused run waits for a signal for as long as it takes, and a tool a
host executes (in TypeScript, say) is an activity on that host's task queue.

```
from lite_agent.temporal import TemporalRunner, make_worker

worker = make_worker(client, "agents", load_agents("agents.toml"))  # or: liteagent worker
runner = TemporalRunner(client, "agents")
wid = await runner.start("support", "Refund order B03.", routes={"lookup": "ts-tools"})
await runner.decide(wid, approve={"c1": True})     # answers a paused run
step = await runner.result(wid)
```

An always-on agent reads one durable inbox (`lite-agent[temporal,pg]`): every trigger writes to it
through `enqueue(...)`, deduplicated on its source's id, and one workflow per inbox runs the work.
Follow-ups and schedules (`PgInbox.add_follow_up`, `add_schedule`) only enqueue when due; a person's
answer to a paused run comes back as an inbox event (design §9.1).

The design, its decisions and the stage plan are in [`docs/lite-agent-design.md`](docs/lite-agent-design.md) (Chinese). License: Apache-2.0.

## Developing

```
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
```

Tests never open a socket and never call a real model; model turns are scripted with lite-model's
`AsyncFakeModel`. Agent guides: [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md).
