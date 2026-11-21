# lite-agent — Agent Guide

lite-agent is an **agent harness library**: given a lite-model role, a set of tools and a task, it runs the
tool loop until the run completes, fails or pauses, and records everything in an append-only event log. The
same `Agent` object covers a two-tool loop, a full harness (skills, memory, compaction, budgets, approval
gates) and — with a durable runner — a long-running agent; the difference is what is registered and which
runner executes it, never a mode flag. It is a library first; the CLI and the optional HTTP shell (for
TypeScript hosts) are thin layers over it.

This file is the shared guide for every coding agent. `CLAUDE.md` imports it.

Naming: repo `lite-agent`, Python package `lite_agent`, CLI `liteagent`, env prefix `LITEAGENT_`.

## Sources of truth (highest first)

1. `docs/lite-agent-design.md` — the design. Its first line is the project status; §0 the decisions (who made
   each one); §3 interfaces; §4 event log and projection; §5 loop rules; §8 budgets, gates and pausing;
   §13 relations to the other repos; §15 the stages and their acceptance; §16 open questions.
2. Project KB: `/Users/arthurgao/Docs/Knowledgebase/projects/lite-agent/` (created in S0) — findings,
   measured eval results, lessons. The rules this project puts into practice live in
   `KB/global/llm-app-architecture.md` §7, §9–§14; when a measurement contradicts one, fix that page.
   Write with the `kb-maintain` skill.

If a change must contradict the design, update the design in the same change and say why in the commit.

## Current stage

**S1 done 2026-11-21: events, the in-memory store, the projection, the loop and `Agent.run`** (design §3–§5;
the rule → test table is in design §15 "阶段记录"). Public at github.com/ArthurGao/lite-agent since 2026-11-21;
CI runs there. S2 is done in three parts (design §15): **S2a done 2026-11-21** — `@tool` with
Pydantic validation, result truncation, tool timeouts, token and cost budgets. **S2b done 2026-11-21** — gates, `paused` runs,
`resume()` (approval, tools the host executes, calls an interrupted run left open). **S2c done 2026-11-21** —
a completion condition with automatic continuation, cut-off text asked to go on, `fallback_role`. S2 is
complete. S3 is done in two parts: **S3a done 2026-11-21** — skills (an index in
the system prompt, `load_skill`) and memory (`recall` / `remember`). **S3b done 2026-11-21** — compaction: a summary event that masks the
history it covers, `SummaryCompactor`. S3 is complete. S4 in two parts: **S4a done 2026-11-21** — `PgSessionStore`
(extra `pg`), migrations, the invariant check reading only the tail. **S4b done 2026-11-21** — resume and
fork across processes, `copy_session`, the `liteagent` CLI. S4 is complete. S5 in two parts: **S5a done 2026-11-21** — a
synthetic shop, twelve tasks judged in code, the lite-eval target (extra `eval`). **S5b done 2026-11-21** — the Ollama
baseline (qwen3:8b 0.750, with thinking 0.889; qwen3:1.7b 0.417) and the timings. S5 is complete.
**S5.5: v0.1.0 released 2026-11-21** (a GitHub Release wheel; event vocabulary V=1 frozen). **S6 done 2026-11-21** — the HTTP shell
(`liteagent serve`, extra `server`) and caller-named runs (`run_id`). **S7a done 2026-11-21** — the TemporalRunner (a run as an
activity, `AgentWorkflow`, `liteagent worker`; extra `temporal`). **S7b done 2026-11-21** — the inbox and timers for always-on
agents (PostgreSQL tables, `InboxWorkflow`, `TimerWorkflow`, `enqueue`). S7 is complete.
**v0.2.1 (2026-11-22): lite-model 0.5.1, so a host can install lite-agent beside lite-cache 0.3.0 (one lite-model). v0.2.0 released 2026-11-21** (S6 and S7; the event vocabulary is still V=1).

## Rules

1. **The event log is the source of truth; messages are a pure, verbatim projection of it** (design §4).
   Never keep a mutable `messages` list, never rewrite or delete an event. Compaction is an event that
   masks a range. Before every real request: `messages sent == projection of the log`, or fail.
2. **A new kind of model-visible input is a new event type**, validated on write; wrapper text is baked
   into the event when it is written, never added by the projection. The system prompt and the full
   tool definitions are a per-run snapshot in `run_started`; the invariant covers messages and tools.
3. **Loop rules are design §5 and each has a test**: a turn cut off by the length limit executes none of
   its tool calls and answers every one with an error result; the loop ends only when every result of a
   batch is terminal; a text-only end of turn is a report, not completion, when the host gave a
   completion condition (at most `auto_continues` nudges).
4. **Paused is a normal outcome** (completed / failed / paused), never an exception. Resuming reads the
   log; it must not depend on the memory of the process that paused. A tool call that already has a
   result in the log is never executed again.
   Tool execution is **at-least-once**: `tool_started` is written before the call and `call_id` is the
   idempotency key. A call with `tool_started` but no result is re-run only if the tool is declared
   idempotent; otherwise the run pauses (`reconcile`) and the host supplies the verified result on resume.
   One `call_id` has at most one `tool_result` and is never executed twice otherwise; a retry after a
   timeout is a new call (design §5 rule 10, §8).
5. **Budgets count every model call, failed and retried ones included**; stop before the call that would
   exceed the budget. Turn, token and cost budgets are per session, summed from the log; pausing and
   resuming never grants a fresh budget.
6. **Every capability is a Protocol with one default implementation** (`SessionStore`, `Tool`, `Gate`,
   `Runner`, `SkillSource`, `Memory`, `Compactor`), injected when the `Agent` is constructed (immutable afterwards), never by
   subclassing; no run-time registry in v1. No `mode`
   parameter anywhere. A session produced under one implementation must resume under another.
7. **Every model call names a lite-model role**, never a model id; no vendor SDK, no `litellm` import, no
   automatic fallback chain. Every `model_turn` event stores provenance and usage. The one exception is an
   explicit `fallback_role` (design §5 rule 11): off by default, only after `CallFailed`, once per run,
   written to the log as `model_switched`, never a LiteLLM router alias or Proxy `fallbacks`.
8. **lite-model is the only run-time dependency among the sibling projects.** PostgreSQL, MCP, lite-mem,
   lite-rag, lite-eval, Temporal and FastAPI are extras; the core (`events`, `projection`, `loop`,
   `agent`) imports none of them. No sibling imports lite-agent.
9. **One loop, with skills; no subagents** until a measurement says otherwise (design §0).
10. **The CLI and the HTTP shell hold no logic**: they parse input and call `Agent`.
11. **Tests never call a real model or a paid API.** Turns are scripted with lite-model's fakes;
    `tests/conftest.py` refuses every socket outside the opt-in markers. Real Ollama only under
    `-m ollama`. PostgreSQL tests use `LITEAGENT_TEST_DATABASE_URL` and only the schema
    `lite_agent_test`. Never call llm-gateway subscription profiles.
12. **Assert the event sequence, not that the run finished**: concrete types, order, counts and contents.
13. **Measure, don't guess**: a change to a loop rule, the compactor or a prompt comes with an eval run
    before and after (from S5).
14. **Secrets only from environment variables.** Never commit `.env*` except `.env.example`.
15. **Pinned dependencies**: lite-model as a release wheel; engine libraries (psycopg, the MCP SDK,
    temporalio) at exact versions. Never answer an MCP SDK / Temporal SDK / psycopg API question from
    memory: check context7 or the pinned version's docs.

## Tech stack

Python 3.12, uv, pydantic / pydantic-settings, Typer, lite-model (every model call), pytest
(+ pytest-asyncio), ruff, mypy (strict). Extras: `pg` (psycopg 3), `eval`; on demand: `mcp`, `mem`, `rag`,
`server` (FastAPI), `temporal`.

## Commands

```
uv run --env-file .env pytest    # everything; PostgreSQL tests need LITEAGENT_TEST_DATABASE_URL
uv run pytest -m ollama          # opt-in: real local Ollama
uv run lite-eval run eval/lite-eval/suites/agent-fake.yaml     # the eval on the fake (deterministic)
uv run lite-eval run eval/lite-eval/suites/agent-ollama.yaml   # S5b baseline: local Ollama, slow
uv run ruff check && uv run ruff format --check && uv run mypy
uv run liteagent migrate | sessions | show <id> [--json] | fork <id> --at <seq> | config   # needs `pg`
uv run liteagent serve --agents agents.toml       # the HTTP shell, loopback :8730 (extra `server`)
uv run liteagent worker --agents agents.toml --task-queue q [--inbox --timers]   # Temporal worker (extra `temporal`)
```

## Workflow

1. Work stage by stage (design §15). Each stage ends with every check green; every new test seen red
   once (write it first, or break the line under test and watch it fail).
2. Definition of done: ruff, ruff format, mypy, tests pass; acceptance items covered by tests; design
   updated if behaviour changed.
3. Conventional Commits (`feat(loop): …`). All code-facing text in English. No commit without the
   owner's go-ahead for that commit; `git add <paths>` only.
4. **Stage close-out — every stage, after its code passes the checks, before its commit and before the
   next stage starts:**
   1. **Simplify**: run `/simplify` on the stage's full diff and apply its fixes.
   2. **Codex review**, read-only, on the same diff, from the repo root:
      ```
      node "$(ls -d ~/.claude/plugins/cache/openai-codex/codex/*/ | sort -V | tail -1)/scripts/codex-companion.mjs" \
        review --wait --scope working-tree        # or --base <commit before the stage>
      ```
      For focus text use `adversarial-review --wait --scope working-tree "<focus>"`. Fallback without
      the plugin: `codex exec -s read-only -o <out.md> "<prompt>" < /dev/null` — the `< /dev/null`
      matters: with a piped stdin `codex exec` waits for stdin to close and hangs in a background shell.
      If Codex is unavailable, say so; never skip silently.
   3. **Fix what holds up**: verify each finding before acting; a bug gets a failing regression test
      first. A rejected finding is written down with a one-line reason (design §15 "阶段记录"), never
      silently dropped.
   4. **Re-run every check** (step 2) so they cover the final code. If step 3 changed code
      substantially, run steps 1 and 2 once more.
   5. Report the checks' output, Codex's findings and what was fixed or rejected; propose the commit.
5. A change that needs lite-model to change: plan mode first, library first (its tests, close-out,
   release), then the pin bump here (workspace `AGENTS.md` "How pins work").
