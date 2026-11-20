# lite-agent

**Status: S0, the skeleton. The package holds no agent code yet.**

What it is being built to be: an agent harness library for the lite modules. Given a
[lite-model](https://github.com/ArthurGao/lite-model) role, a set of tools and a task, it will run
the tool loop until the run completes, fails or pauses, and record everything in an append-only
event log, so that resume, fork and replay are different reads of that log.

The design, its decisions and the stage plan are in [`docs/lite-agent-design.md`](docs/lite-agent-design.md) (Chinese). License: Apache-2.0.

## Developing

```
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
```

Tests never open a socket and never call a real model; model turns are scripted with lite-model's
`AsyncFakeModel`. Agent guides: [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md).
