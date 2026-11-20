@AGENTS.md

# Claude Code notes

- At the start of a session: read the status line, §15 (the current stage and its acceptance) and §16
  (open questions) of `docs/lite-agent-design.md`, and skim the project KB at
  `/Users/arthurgao/Docs/Knowledgebase/projects/lite-agent/` once it exists.
- Load the `llm-app-architecture` skill before touching the loop, the event log, context assembly or the
  runner: design §4–§9 put its §7 and §9–§14 into practice, and several of its warnings describe exactly
  what a model writes by default (a mutable `messages` array, executing a truncated turn's tool calls,
  stopping at the first text-only turn, a team of subagents).
- Stage close-out is AGENTS.md Workflow step 4: `/simplify` through the Skill tool, then the Codex
  review command there (foreground, it waits). Do not start the next stage before it is done.
- Plan mode before: any change to an interface in design §3, to the event vocabulary (§4), or anything
  that needs lite-model to change.
- Other relevant user-level skills: `testing-and-ci` (fakes, Temporal tests with timers),
  `backend-reliability` (idempotent writes, at-least-once delivery, the inbox), `postgres-rls-migrations`
  (S4), `llm-model-eval` (S5), `dev-machine` (ports, local PostgreSQL, Ollama), `kb-maintain`.
- Never start long-lived services (a Temporal dev server, LiteLLM Proxy, llm-gateway) unasked. Ollama
  runs on the host; check it for free with `curl -s http://127.0.0.1:11434/api/tags` before `-m ollama`.
