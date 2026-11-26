# lite-agent Design Document

**Status: S1–S7 done (2026-11-21); v0.3.1 (2026-11-26): the PG store gives `SeqConflict` when two writers append to the same tail at once; v0.3.0 (2026-11-26, event vocabulary V=2): masking of old tool results with lite-context's `fit()` (§6); v0.2.5: the compactor now uses lite-context (§6, §13); events and projection, tool loop, `@tool`, budgets, gate, pause / resume, `fallback_role`, skills, memory, compaction, PG session store, CLI, lite-eval target (S5), HTTP shell (S6), Temporal runner and inbox / timer (S7). v0.1.0 (S5.5, event vocabulary V=1 frozen) and v0.2.0 are released. P0 is lite-model v0.4.0's `turn()`. The repo is public at github.com/ArthurGao/lite-agent. First host: llm-steward's reconcile troubleshooting assistant (decided by the owner 2026-11-21, §16).**
When this document conflicts with the code, update it in the same change and say why in the commit.

## 0. Decision log

The reasons for decisions are written only here; other sections cite §0. "owner" means the owner decided;
"proposed" means my judgement while drafting, followed until the owner rejects it, and then changed here.

| Date | Decision | Who | Reason |
| --- | --- | --- | --- |
| 2026-11-20 | Form: a library; the CLI and HTTP are thin shells on top of it and hold no logic | owner | Tools are the host's Python functions; approval, write-back and budgets must be in the same process as the host's business code; the host can inject a fake to test. Same as lite-rag / lite-mem |
| 2026-11-20 | One general lib: everything from a "simple loop with two tools" to a "full harness" to a "long agent" is assembled from the same `Agent`, and differs only by configuration and init arguments | owner | Every future project can use it, with no separate copy for each way of using it |
| 2026-11-20 | The close-out is the same as the other lite modules: `/simplify` → read-only Codex review → fix or write down the reason for rejecting → re-run all checks | owner | Workspace convention |
| 2026-11-20 | TypeScript projects use it through the HTTP shell; no second implementation in TypeScript | owner raised the need, the approach is proposed | Two implementations drift apart, and the rules in §4 and §5 would have to be verified twice |
| 2026-11-20 | One repo: the harness (one run) and the long agent (the durable shell) are two layers of the same thing, not split into lite-agent + lite-harness | proposed | Without a second host that uses only the core, splitting fixes a public interface too early and adds one more pin chain. Split when a second host appears |
| 2026-11-20 | No `mode` switch. The differences are two independent axes: which capabilities are installed (injected when building the `Agent`; not given means not present), and where it runs (swap the `Runner`) | proposed | `if mode ==` spreads into the core, and every new way of using it needs a core change (KB `llm-app-architecture` §10.1) |
| 2026-11-20 | Write our own loop and event log; pi and DeepSeek Harness are only read for their design, not used as dependencies; no Pydantic AI / LangGraph. Use ready-made parts for protocols and infrastructure (MCP SDK, `temporalio`, lite-model) | owner | The goal is to master and verify this architecture (turn KB §9, §11, §12 from "reported" into "observed", in line with lite-rag / lite-mem "built to learn from"), not to get a working agent as fast as possible; the event log, fork and replay are required, whereas a framework keeps its own message history, ships its own provider layer (bypassing lite-model's role and provenance) and decides the loop rules. pi and dsh are TypeScript. Known cost: differences between backends in tool calling must be handled by ourselves in lite-model P0 |
| 2026-11-20 | Every model call goes through a lite-model role; no `import litellm`, no direct vendor SDK | workspace rule | AGENTS.md "Shared conventions" |
| 2026-11-20 | After a model call fails, the run may continue on another model once: `fallback_role`, off by default, details in §5 rule 11 | owner raised it, details proposed | An unattended long task should not fail entirely because one vendor is briefly unavailable. The workspace reason for "no fallback" is that switching the model is invisible; here every switch is an event in the log and every turn records provenance, so it is visible. KB §3.2 also says "off by default" |
| 2026-11-20 | The core is async; a `run_sync()` wrapper is provided | proposed | The MCP Python SDK, the Temporal Python SDK and llm-steward are all async; a sync host (like the lite-rag CLI) uses the wrapper |
| 2026-11-20 | "Paused" is one of the normal outcomes of a run (completed / failed / paused), not an exception | proposed | Waiting for approval, waiting for the client to execute a tool and waiting for a Temporal signal are the same mechanism: the state is written to the event log, this execution ends, and it is resumed later by session id. Adding it later means going back to change the loop |
| 2026-11-20 | By default one loop + skills; no subagents | proposed | KB §14.1: splitting out subagents needs a measured reason |
| 2026-11-20 | Temporal is an optional extra (`lite-agent[temporal]`), done after the core is stable; the event log is the source of truth, Temporal holds only progress | proposed | KB §7.3 "two orchestrators"; cairn is already "Temporal holds progress only" |
| 2026-11-20 | For a TypeScript host, tools go through MCP first (the host starts an MCP server and lite-agent is the client); second choice is "pause, the host executes and sends back the result" | proposed | MCP is a ready-made standard, and the same server can also serve Claude Code / Codex |
| 2026-11-20 | No streaming in v1; the event granularity is "one turn" | proposed | lite-model does not do streaming (its design §1 Non-goals); see §16 |

## 1. Overview

lite-agent is an **agent harness library**: give it a role, a set of tools and a task, and it runs the tool loop
until it completes, fails or pauses, recording the whole process in one append-only event log.

Three terms as used in this document:

- **agent loop**: call the model → execute tools → send the results back → decide whether to continue. It is
  only one part of the harness (§5).
- **harness**: everything around the loop that lets it do work: transcript, context assembly, tool registration,
  budgets and gates, entry points (§3–§8).
- **long agent**: the harness plus a durable shell: inbox, timers, recovery across processes (§9, §10).

### Origins

- KB `global/llm-app-architecture.md`: §7 (model calls inside a durable workflow), §9 (event log + pure
  projection), §10 (pluggable capabilities), §11 (loop mechanics), §12 (always-on), §13 (memory), §14 (single
  loop or subagents). §9–§14 are all "reported" so far, and this project is the first time they land; when a
  measurement disagrees with a rule, change the KB (`kb-maintain`).
- cairn: an agent run on Temporal, where model calls are activities and files are the source of truth
  (KB `projects/cairn/integrations/temporal-runtime/`, `harness-home-and-core/`).
- Read for their design, not used as dependencies: DeepSeek Harness (session event log, `deriveMessages()`,
  capability seams), pi (`agent-loop.ts`).

### Goals

1. One `Agent` object covers use from simple to complex; the difference is only what is installed (§3, §11).
2. The transcript is the event log, and the messages sent to the model are a pure projection of it; resume, fork,
   audit and eval are different readings of the same log (§4).
3. The boundary behaviour of the loop is written down and tested: truncation, stop conditions, automatic
   continuation, budgets, pausing (§5, §8).
4. Each capability is a Protocol plus a default implementation, and the host replaces it by injecting its own
   implementation when building the `Agent` (§3).
5. It can be tested without network and without cost: `FakeModel` scripts every turn (§12).

### Non-goals (v1)

- No subagent / multi-agent orchestration (§0).
- No sandbox: tools run in the host process, and isolation is the host's job.
- No TUI, and no built-in toolset of a coding agent (read/write files, shell); the host registers them when
  needed.
- No "everything is a plugin" meta-framework: only the Protocols listed in §3 can be replaced.
- No streaming (§16).
- No automatic model fallback chain. The only exception is an explicitly configured `fallback_role`: it switches
  only once and is written to the log (§5 rule 11).
## 2. Layers

```
host (Python) ──import──► Agent ──► Runner ──► loop ──► lite-model role ──HTTP──► model
                            │                    │
                            │                    ├─► tools (host functions / MCP / lite-rag)
                            │                    └─► gates (budgets, approval)
                            └─► SessionStore (event log) ◄── derive_messages() (pure projection)

host (TypeScript) ──HTTP shell (§10)──► the same Agent
long agent: inbox / timer ──► TemporalRunner (§9) ──► the same loop, one activity per step
```

Direction of dependencies: the core (`events`, `projection`, `loop`, `agent`) depends only on lite-model's types
and Protocols. PostgreSQL, MCP, lite-mem, lite-rag, Temporal and FastAPI are each an extra, and the core does not
import them.

## 3. Interfaces

S1 (2026-11-21) fixed the minimal shape of `Agent`, `SessionStore`, `Runner`, `RunResult` and `Tool`. The parts
marked with a stage are added later: all are new optional items and change nothing that exists.

```python
@dataclass(frozen=True, kw_only=True)
class Agent:
    role: str                                   # a lite-model role; never a model id
    fallback_role: str | None = None            # S2c: another role, once per run after CallFailed
    model: AsyncToolModel                       # lite_model.AsyncToolModel (AsyncFakeModel in tests)
    system: str = ""
    tools: Sequence[Tool] = ()                  # kept as a tuple; names must be unique
    sessions: SessionStore = InMemorySessionStore()   # one per agent by default
    budget: Budget = Budget()
    runner: Runner = InProcessRunner()
    clock: Callable[[], datetime] = ...         # event times
    ids: Callable[[], str] = ...                # session and run ids
    gates: Sequence[Gate] = ()                  # S2b: asked in order before a tool runs (§8)
    done: Callable[[Sequence[Event]], str | None] | None = None   # S2c: None = done; text = still open
    skills: SkillSource | None = None           # S3a: index in the system prompt + `load_skill`
    memory: Memory | None = None                # S3a: `recall` / `remember`
    compaction: Compactor | None = None         # S3b: None = nothing is summarised
    compact_role: str = "compact"               # S3b: the lite-model role that writes summaries

    async def run(self, task: str, *, session_id: str | None = None,
                  run_id: str | None = None, new_session: bool = False) -> RunResult: ...  # S6: run_id, new_session
    async def resume(self, session_id: str, input: Resume | None = None, *,
                     run_id: str | None = None) -> RunResult: ...   # S2b; S6: run_id
    def run_sync(self, task: str, *, session_id: str | None = None, run_id: str | None = None,
                 new_session: bool = False) -> RunResult: ...

@dataclass(frozen=True)
class RunResult:
    session_id: str
    run_id: str
    outcome: Literal["completed", "failed", "paused"]
    stop: StopReason            # end_turn | terminal_tool | budget_turns | truncated | error   (S2 adds more)
    text: str | None            # the last assistant text, when there is one
    error: RunError | None      # kind: model | store | harness; the exception's type name, never upstream text
    usage: Usage                # this run's model calls, summed; an unknown part stays None
    pause: Pause | None = None  # S2b: set exactly when outcome is "paused"
    switched: bool = False      # S2c: this run moved to fallback_role

@dataclass(frozen=True)
class Waiting:                  # S2b: one call a paused run waits on
    call_id: str
    reason: Literal["approval", "tool", "reconcile"]
    note: str = ""              # the gate's reason, for the person who decides

@dataclass(frozen=True)
class Pause:                    # S2b
    waiting: tuple[Waiting, ...]          # .call_ids

@dataclass(frozen=True)
class ToolOutcome:              # S2b: what the host reports for a call it executed or checked
    content: str
    is_error: bool = False
    unknown: bool = False       # nobody can tell whether it took effect

@dataclass(frozen=True)
class Resume:                   # S2b: what the host brings back
    approve: Mapping[str, bool] = {}          # call id -> go ahead?   (calls not started yet)
    results: Mapping[str, ToolOutcome] = {}   # call id -> result      (calls already started)

class SessionStore(Protocol):
    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None: ...
    async def read(self, session_id: str, *, after_seq: int = 0, upto_seq: int | None = None) -> Sequence[Event]: ...  # S4a: after_seq
    async def fork(self, session_id: str, *, at_seq: int) -> str: ...
# implementations: InMemorySessionStore; PgSessionStore (S4a, extra `pg`)

async def copy_session(source, target, session_id, *, new_session_id=None) -> str: ...  # S4b: a fork arrives flat

class Tool(Protocol):                           # the attributes are read-only properties
    name: str
    description: str
    parameters: Mapping[str, Any]               # JSON Schema of the input, sent to the model as is
    terminal: bool                              # a successful result may end the run (§5 rule 2)
    idempotent: bool                            # S2a: safe to run again with the same call_id (§8)
    timeout_s: float | None                     # S2a: None = Budget.tool_timeout_s
    external: bool                              # S2a (used from S2b): the host executes it
    def validate(self, args: Mapping[str, Any]) -> str | None: ...   # S2a: what is wrong, or None
    async def __call__(self, args: Mapping[str, Any], ctx: ToolContext) -> str: ...

def tool(*, description, name=None, terminal=False, idempotent=False, timeout_s=None):   # S2a
    """Decorates `async def f(args: SomeModel, ctx: ToolContext) -> str` into a ModelTool."""

@dataclass(frozen=True)
class Budget:                                   # S2a; turns / tokens / cost_usd are the session's
    turns: int = 30
    tokens: int | None = None                   # input + output
    cost_usd: float | None = None
    tool_result_chars: int = 20_000
    tool_timeout_s: float = 60.0
    auto_continues: int = 2                     # per run (S2c)

class Gate(Protocol):                           # S2b: asked before a tool call executes
    name: str
    async def check(self, call: ToolCall, ctx: RunContext) -> Allow | Deny | PauseFor: ...
    # Deny(reason): the reason goes back to the model.  PauseFor(reason): for the person.
    # RunContext: session_id, run_id, events (the log so far)

class Runner(Protocol):                         # where the loop's steps execute
    async def run(self, agent: Agent, session_id: str | None, input: str | Resume, *,
                  run_id: str | None = None, new_session: bool = False) -> RunResult: ...
```

- `run(task)` opens a new session; `run(task, session_id=…)` is the next run of that session (multi-turn
  conversation), and a session that does not exist raises `UnknownSession`. The input of `Runner.run` is the task
  (`str`; `session_id=None` means a new session) or a `Resume`.
- `resume(session_id, input)` (S2b) carries on with a paused, failed or interrupted session (§8). It can also be
  called without input: it retries the failed turn, or resumes calls that are safe to run again. An input that does
  not fit (a call id that does not exist, approval for a call that has already started, a result for a call that
  has not started, a session that has already ended completely) raises `ValueError` and writes no event.
- **The caller names the run** (S6; S7's activity retries rely on it): when `run_id` is given, calling again with
  the same id never opens a second run. If the run has already ended in the log, the result is rebuilt from the log
  and returned, and nothing is written. If it has not ended (the process died midway), it **carries on in the same
  run**: the opening events are not written again, the calls left by the previous turn are handled as in §8 (a
  non-idempotent call that had started is not re-run and needs checking), and then the loop continues.
  `new_session=True` creates a new session with the given `session_id`; if another run has already used this id,
  it raises `SeqConflict` (also when two callers race for the same id: the loser gets `SeqConflict`, not a failed
  run). A `run_id` names exactly one run: it can only be the **last** run of this session (what is retried is the
  latest call), and the task must be the same as the first time; otherwise the id has been reused and it raises
  `ValueError`. A retry must pass the same input as the first time: the `Resume` of `resume` only applies to calls
  that have no result yet.
- `Tool` uses a JSON Schema dict rather than a Pydantic model: the loop only needs to hand the definition to the
  model and the arguments to the function. Two implementations: `@tool` (S2a) produces a `ModelTool` (the schema is
  the model's `model_json_schema()`, `validate()` validates with Pydantic, and the function receives a model
  instance); `FunctionTool` wraps an async function that takes the raw arguments and does no validation.
- `RunResult.error`: the failure of the run itself. A failed model call is `model` (the log has `model_error`); a
  failed store read or write is `store`, and a projection invariant that does not hold is `harness`; these two are
  no longer written to the log (§4). An exception that is not a `ModelError` (a bug in the host or the test) is
  raised as is.
- The `@tool` decorator turns a typed function into a `Tool` (the schema comes from the Pydantic model of the
  parameters).
- Configuration is data; init arguments are code objects (§11).
- Capabilities are injected when the `Agent` is built and are immutable afterwards; to change an implementation,
  build a new `Agent` (sessions are in the store and are not affected). No runtime registry in v1: the
  "revocable, addressable" and similar parts of KB §10.1 wait until a host needs hot swapping. `liteagent config`
  prints the final assembly (KB §10.3).
- All state is in the `SessionStore`: a session created under component A must be resumable under component B (the
  test from KB §10.3, S4 acceptance).
## 4. Event log and projection

**Rule (KB §9): the event log is append-only and is the source of truth; the messages sent to the model are derived
from the log by a pure projection on every turn, and are never saved separately or modified in place.**

Envelope: `session_id`, `seq` (monotonic, from 1), `time`, `run_id`, `type`, `v` (the event vocabulary version,
currently 1); the fields of each type are laid flat on the event. Events are frozen Pydantic models, `dump()` /
`load()` do a JSON round trip, and `load()` rejects an unknown `type`, extra fields and a newer `v`. S1 implemented
the 8 types in the table below: `session_started`, `run_started`, `user_message`, `model_turn`, `tool_started`,
`tool_result`, `model_error`, `run_finished`; S2b added `paused` and `resumed`; the rest are added in the stage that
uses them.
**V=2 (2026-11-26, v0.3.0)**: added `tool_results_masked`; not one V=1 event shape changed, a V=1 log is read as it
is and its projection is unchanged (`tests/fixtures/v1_session.json` is a real log written by v0.2.5 together with
its projection at that time). From V=2 on, the `v` in the envelope is **the version this kind of event belongs
to**, not the newest version of the code: types that already existed in V=1 are still written with `v=1`, and only
`tool_results_masked` is written with `v=2` (it is rejected with `v=1`). So a session that has not turned on
masking is all `v=1`, and a v0.2.5 worker or shell still reads it; only for a session that has had a masking event
written does old code reject it.
**`V = 1` has been frozen since v0.1.0** (released 2026-11-21): it stayed 1 until the release because there were no
old logs to read. From then on, changing the shape of any event must raise `V` by one and keep the code that reads
the old version: a log stored in the PostgreSQL store (from S4 on) must always stay readable.
`tests/unit/test_vocabulary.py` stores, for each V, a fingerprint of the JSON Schema of all event types, and fails
if a shape changed while V did not.

| type | data | Visible to the model |
| --- | --- | --- |
| `session_started` | The first event of a session; its `run_id` is the run that opened the session. The lineage of a fork is not an event but store metadata (parent session, fork point; fixed in S4a, the same in both stores) | no |
| `run_started` | `run_id`, how it was triggered (run / resume), **a snapshot of this run**: the main role, `fallback_role`, the system prompt verbatim, the full definition of every tool (name, description, JSON schema) | system and tools |
| `user_message` | The text; `origin`: task (S1); steering / follow_up / auto_continue / resume are added in S2 | yes |
| `model_turn` | lite-model's `AssistantTurn.blocks`, stored as is in the order the endpoint gave them (`Text`, `ToolCall`, `Opaque`, which is Anthropic's thinking block); `stop`, role, provenance, usage | yes |
| `tool_started` | `call_id`, tool name, arguments, `external`; written after validation and the gates, **before** the tool executes (or is handed to the host to execute). A validation failure, `Deny` and `PauseFor` do not write it (§8) | no |
| `tool_result` | `call_id`, the content (already truncated), `is_error`, `error_kind` (`invalid_args` / `denied` / `exception` / `timeout` / `truncated_turn` / `outcome_unknown`), `executed` (whether the tool was called), `origin` (`executed` / `external` / `reconciled`), `terminal`, the original length | yes |
| `compaction` | `summary` (the full text the model sees, including the fixed preamble), `covers` (the range of seq it covers, both ends included), the role that wrote the summary, provenance, usage | yes (replaces the covered range) |
| `tool_results_masked` (V=2) | `masked`: for each masked `tool_result`, its seq and the text the model is shown from then on (lite-context's `MASKED_RESULT`, with the tool name); `state`: lite-context's `ContextState` (the next cut is made at the same place); `tokens_before` / `tokens_after` (estimates) | yes (replaces the content of those results) |
| `paused` / `resumed` | `paused.waiting`: one item for each call waited on (`call_id`, the reason `approval` / `tool` / `reconcile`, the note the gate gives to people); `resumed`: `approve` (call id → approved or not) and the list of call ids that came with results (the results themselves are in `tool_result`) | no |
| `model_error` | The error type of the failed call, role, `purpose` (`turn`, or `compact`: a summary that was not written), usage (`None` when unavailable) | no |
| `model_switched` | from / to role, the reason (§5 rule 11) | no |
| `run_finished` | outcome, stop, total usage | no |

- **Validated on write, stricter than on read**: an invalid event is rejected, does not enter the log and has no
  side effect.
- **The projection is pure and verbatim**: `derive_messages(events) -> list[Message]` adds no text; wrapper text is
  already in the content when the event is written.
- **New model-visible input = a new event type**, and the loop does not assemble a message ad hoc.
- **The projection is backend-independent**: `derive_messages` reads only events and outputs a sequence of
  lite-model `TurnMessage`. `model_turn` → `AssistantTurn` (the order of blocks is unchanged); all `tool_result`
  of the same turn → **one** `ToolResults` (Anthropic requires the results of the same turn in one message,
  lite-model design §3.1). How an `Opaque` is handled when sent to another protocol is the job of the lite-model
  backend adapter (sent as is to the same protocol, not sent to another).
- **An `Opaque` enters the projection only if its prefix has not changed.** Newer Anthropic models bind every
  thinking block to the `system`, `tools` and all earlier messages at the time it was produced; if these change and
  the block is sent back, the request gets a 400 (lite-model design §3.1, "preserved thinking"). Append-only
  history is not affected. So the projection drops such an `Opaque`: the `model_turn` it is in is earlier than the
  latest `compaction`, or earlier than the latest `run_started` whose system / tools snapshot differs from the
  previous one. This rule looks only at the log, so the projection stays pure; text and tool calls are kept as
  usual. The cost is that the model loses its thinking from before that point, and the summary is all it knows of
  that work. The rule is deliberately conservative: after snapshots A → B → A, the `Opaque` of the A period also no
  longer enters the projection (they are in fact still valid), in exchange for a single rule that never sends an
  invalid block.
- **system and tools also come from the log**: the projection takes the snapshot of the latest `run_started`.
  Within one run they do not change; if the host changes the configuration of the `Agent` and then calls `resume`,
  the new `run_started` records the new snapshot, and in a replay every request can be restored according to the
  run it belongs to.
- **Invariant**: before every real request, assert that "the messages and tools handed to lite-model == the
  projection of the log", and fail if not equal. Implementation (S4a): the loop holds a list of events it has
  appended itself, and the request is projected from it. When a run starts, the whole session is read once; before
  every later request, only the part "since the last check" is read and must equal what the loop wrote, event by
  event, otherwise `HarnessError` and no request is sent (this is where a store that lost or changed an event
  just written is caught). A prefix that has been checked is not re-read: this depends on the store never
  rewriting; on the PostgreSQL side the database guarantees it (a trigger on the `events` table rejects `UPDATE`,
  `DELETE` and `TRUNCATE`), and a host that implements its own store must do the same. S1 to S3 read the whole
  session at each step and compared two projections; with PostgreSQL, one whole-session read per step is too
  expensive. What it compares is the canonical representation; the request body the backend actually sends is
  guaranteed by lite-model's contract tests.
- **Compaction only masks**: the `compaction` event records which seq it covers, and the original events stay in
  the log; the projection replaces that range with the summary.
- **fork**: the new session records `(parent_session_id, at_seq)` and does not copy events. Reading the child
  session = the events of the parent session with `seq ≤ at_seq`, followed by the child session's own events; the
  child session's seq starts at `at_seq + 1` and is stored under its own name. The parent session is append-only,
  so this prefix never changes: events the parent session writes after the fork (including a `compaction` that
  covers an old range, whose seq is greater than `at_seq`) are not seen by the child session. `expected_seq` checks
  only the child session's own tail. A fork of a fork is read up along the chain.
- **Concurrency**: `append` carries `expected_seq` and is rejected on a mismatch (a session can have only one
  writer). The batch of events in one `append` is either all written or none written. When the model has replied
  but `append` fails, the run fails and the reply is lost (the money is already spent and the log does not have
  it); `resume` re-sends this turn.
- When a tool result exceeds `Budget.tool_result_chars` it is truncated before `tool_result` is written, with a
  line `[truncated: the first N of M characters]` added at the end, and `original_chars` records the original
  length. The budget governs the length of the original text that is kept; the note line is counted separately (so
  the stored content is a few dozen characters over the budget); what the log stores is what the model sees. Error
  messages are handled the same way. Whether to store the full result elsewhere is in §16.
## 5. Loop rules

1. **A turn cut off by the length limit executes no tool call**, but every call gets an error `tool_result`
   ("output was truncated, the arguments may be incomplete, please resend"), and then the loop continues
   (KB §11.1). This turn counts toward usage and the turn budget. When the truncated turn has only text and no
   call, there is nothing to answer, and the run ends with `failed`, `stop=truncated`, and `text` is the part that
   exists (S1; from S2c it first continues writing, see rule 3).
2. **The run ends only if all the tool results of a batch are terminal**; as long as one result is ordinary, the
   model gets another turn (KB §11.2).
3. **A plain-text `end_turn` does not mean done** (KB §11.4; S2c): when the host gives a completion condition
   `Agent(done=…)`, every plain-text turn asks it once: it receives the whole log including this turn, and
   returning `None` means finished, while returning text means something is not done. This turn and the event after
   it (the message sent back, or the end of the run) are written in the same batch: no state that "looks finished
   but is not" is left in between. If something is not done, that text is sent back as a `user_message` with
   `origin=auto_continue`, at most `budget.auto_continues` times per run (default 2); if it is still not done after
   that, the run ends with `failed`, `stop=incomplete`, and waits for a person to look. When `done` is not given, a
   plain text ends the run. A run that ends with a terminal tool does not ask `done`. `done` returning an empty
   string is the same as `None`. `done` is the host's synchronous code: an exception it raises is raised as is
   (not swallowed, like the host's other bugs), and a slow check holds up the event loop; a completion condition
   that needs I/O waits until S7's durable runner gives it a place as an activity. `Budget(auto_continues=0)` also
   turns off truncation continuation. **Truncated plain text** also takes this path: a "keep writing" message is
   appended and it continues, using the same count limit, and only when that is used up is it `stop=truncated`.
4. **The snapshot is taken per run**: when `run()` / `resume()` starts, the role, `fallback_role`, system and tool
   definitions are written into `run_started`, and every turn of this run takes them from it (§4). KB §11.3 says
   once per turn; here it is tightened to once per run, in exchange for "a request can be fully restored from the
   log". To change the system prompt or a tool in the middle of a run, end this run and start another. It is
   recommended that system and tools stay unchanged within a session: changing them is allowed, but the earlier
   thinking blocks no longer enter the projection (§4), and the prompt cache starts from scratch.
5. **Input that arrives while running** goes into two queues: steering is injected between two turns, and
   follow-up is read when the run is about to stop and restarts the loop (KB §11.3). Both are for the long agent
   and have no caller now; they are designed together with S7 (moved out of S2 on 2026-11-21).
6. **The budget includes failures and retries**: every model call (including those where lite-model raised an
   error, and compaction ones) writes an event and counts usage; when the next call would exceed the budget, the
   run stops before the call, with `outcome=failed`, `stop=budget_*` (the task is not done, so it does not count
   as completed; KB §7.2). `turns`, `tokens` and `cost_usd` are counted over the **whole session**, summed from the
   log each time (pausing and resuming does not give a new budget; a forked session includes the inherited
   prefix). `auto_continues` is counted per run. A call whose usage is unavailable (`CallFailed`) is recorded as
   `None` and not estimated; it counts as 0 in the token budget, and the turn budget still counts it. A compaction
   call counts toward tokens and cost, and not toward turns. What is checked is "whether what has been used has
   reached the budget": tokens are the sum of input + output of each call, and how much the next call will use is
   not known beforehand, so the last call may exceed the budget a little, and no call is made after that.
7. An exception raised by a tool becomes a `tool_result` with `is_error` and is sent back to the model; a `Deny`
   from a `Gate` is the same. Only the harness's own errors (a failed store write, a projection invariant that does
   not hold) make the run fail. When two calls in the same turn use the same id, the results cannot be matched to
   the calls: this turn is not recorded as a `model_turn`, a `model_error` is written (`DuplicateCallIds`, with
   this call's usage), and the run fails.
8. Several tool calls in the same turn execute in order by default; parallel execution is in §16.
9. **A failed model call (timeout, connection error, 5xx, 429)**: retries are done only inside lite-model (the same
   target, `max_retries`, its design §8), and the loop adds no layer of retry on top. When the retries are used up,
   lite-model raises `CallFailed`: a `model_error` event is written and the run ends with `outcome="failed"`,
   `stop=error`. By default it **does not switch models**; when `fallback_role` is configured, it first switches
   once according to rule 11. The log ends with `model_error` (the projection does not include it), and
   `resume(session_id)` re-sends this turn without re-running earlier tools. A call that timed out has no usage
   (`None`), and the event records `None` truthfully and does not estimate. The durable runner (§9) may use a
   longer retry policy and respect Retry-After, and that is the runner's business.
10. **Tool timeout**: each tool has `timeout_s` (default `Budget.tool_timeout_s`, 60 seconds). On timeout this
    call is cancelled (a `TimeoutError` raised by the tool itself does not count; it is an ordinary `exception`),
    and a `tool_result` is written with `error_kind=timeout`, `executed=True`, and the content says "whether it
    took effect is unknown, do not retry directly". It is a different result from "not executed"
    (`invalid_args`, `denied`, `truncated_turn`, `executed=False`), and both the model and the host can tell them
    apart. The same `call_id` is never executed again. If the model wants to retry, it can only send a new call (a
    new `call_id`), which passes the gates as usual; a gate can read the session's events through `RunContext` and
    can refuse, or require approval for, a repeated call whose "previous one timed out, result unknown".
11. **Continuing on another model (optional, off by default)**: `Agent(fallback_role=...)`.
    - It fires only on `CallFailed` (timeout, connection, 5xx, 429, with the retries used up). `Truncated`, an
      empty reply, a refusal and invalid tool arguments do **not** fire it: those are quality problems, and
      switching models would hide them.
    - Order: `model_error` and `model_switched` (from / to role, reason) are written in the same batch → this turn
      is re-sent with `fallback_role` (the request is exactly the same). If the fallback also gives `CallFailed`,
      it fails according to rule 9. A run switches at most once. `model_turn` and `model_error` both record the role
      actually used.
    - **Which role is used is decided entirely by the log**: if the current run (after the latest `run_started`) has
      a `model_switched`, the fallback is used, otherwise the main role. After the switch this run does not switch
      back (switching back and forth would lose the prompt cache again and again).
    - Every `run()` / `resume()` writes a new `run_started`, so a new run starts from the main role. If the process
      dies after `model_error` and before `model_switched`, or after `model_switched` and before `model_turn`, the
      recovery is a new run on the main role: the result is deterministic, and the worst case is one extra try of
      the main role.
    - If the run pauses after the switch within the same run (waiting for approval, waiting for an external tool)
      and then `resume` is called, it is also a new run and tries the main role first; if the main role is still
      unavailable, it switches once more. This is on purpose: there is a single rule, and "already switched" does
      not need to be saved as state across runs.
    - `fallback_role` is another lite-model role that points at a concrete model; it does not use LiteLLM's routing
      aliases or the Proxy's `fallbacks` (that picks the model where it cannot be seen, and a routed call may also
      be unable to read which model actually answered). lite-model needs no change, and its "no fallback" rule is
      unchanged: switching models is the loop's decision and is written in the log.
    - Sending model A's history to model B: the projection is unchanged. When switching to another protocol,
      lite-model does not send `Opaque`; for the same Anthropic protocol it is sent as is, and what cannot be read
      is dropped by the API (not billed). After returning to the main role, an `Opaque` whose prefix has not
      changed is sent back as usual.
    - Context window: v1 does not compact specially for a model switch. If the fallback is rejected because the
      context is too long, it is an ordinary failure (rule 9). When a `Compactor` is configured, its threshold is
      taken for the **role that is about to be called** (one `context_tokens` per role in the configuration), so
      the first turn after the switch is first compacted with the fallback's threshold; the compaction is an
      ordinary `compaction` event that stays in the log, and the main role sees it too when it comes back. A
      compaction call that fails itself or goes over budget = the run fails.
    - `RunResult` states `switched=True` and the usage of each model separately. Evals turn fallback off by
      default, or report grouped by model.
## 6. Context: system, skills, memory, compaction

- The **system prompt** is part of the `run_started` snapshot and does not change within a run (§4, §5 rule 4).
  What the snapshot stores is the **assembled full text** (`context.build()`, assembled once when the `Agent` is
  built and unchanged afterwards): the `system` given by the host, then the skills index section, then the memory
  description section, with empty parts skipped. The assembly is a pure function, and the same `Agent` gets a
  verbatim identical result every time (the prompt cache relies on it); what is sent to the model is exactly this
  copy in the log.
- **There is no `context_added` event** (removed from the event table on 2026-11-21): the body of skills and the
  content of memory both enter the log as tool results, the system part is in `run_started`, and there is no third
  kind of "injected text".
- **skills** (S3a; the `SkillSource` protocol: `index()` gives names and one-line descriptions, `load(name)` gives
  the body). The default implementation `SkillDir(root)` reads `<root>/<name>/SKILL.md`, in the same format as
  Claude Code / cairn: the frontmatter at the start has `name` and `description`, and the rest is the body. Only
  these two keys are read, values are taken as single-line plain text (quotes and line folding are not handled), and
  we parse them ourselves without adding a YAML dependency. It is read once at construction: a missing frontmatter,
  a missing field, the same key appearing twice, or a `name` that does not match the directory name raises an error
  on the spot. **Skill files are the host's own files and are treated as trusted content**: the name and
  description go into the system prompt as is, the body is given to the model as is as a tool result, and symbolic
  links in the directory are followed as usual.
  - The system prompt holds only the index (one per line: name and description, sorted by name) and one sentence,
    "if one matches, read it first with `load_skill`". When the entries of the index (not counting that fixed
    sentence) exceed `Budget.skills_index_chars` (default 4000), building the `Agent` raises an error; there is no
    truncation and no silent loading of everything (cairn's `ContextBudgetError`).
  - Giving `skills` automatically adds one tool, `load_skill(name)`: it returns the body; a name that does not exist
    is an ordinary tool error and lists what there is.
- **memory** (S3a; the `Memory` protocol: `async recall(query) -> str`, `async remember(note)`): none by default.
  When given, two tools `recall` / `remember` are added automatically, and a short paragraph is added to the
  system prompt that says only what memory is for and does not write rules about "when to remember and what to
  remember" (KB §13.1). `recall` is not called automatically when a run starts: whether to look up first depends on
  the model (KB §13.3) and is decided by the host's system prompt. The library ships a `NotesMemory` (a list in
  memory, searched by shared words) for tests and single-process scripts; it is not a retrieval engine; the
  lite-mem adapter is still under "on demand". An exception raised by a `Memory` implementation is returned to the
  model with its message text like any other tool's (§5 rule 7): what goes in the message is the implementation's
  responsibility.
- The system prompt and the built-in tools are fixed when the `Agent` is built (`Agent.system_prompt()`,
  `Agent.all_tools`); after that, even if the skills source changes, the index this `Agent` sends is not affected;
  the budget check is on what is sent. `Agent.tools` stays as the host gave it. If the skills are changed and the
  same session is run again, the snapshot changes, the earlier thinking blocks no longer enter the projection (§4),
  and the cost is the same as changing the system prompt.
- These three built-in tools come after the host's tools and take the same path as the host's tools: validation,
  gates, timeout and events are all the same (so the host's gate can block `remember`). A name that collides with a
  host tool raises an error when the `Agent` is built.
- **Masking** (v0.3.0, V=2; the `Masker` protocol: `check(roles)`, `mask(events, role)`). Without `masking`,
  nothing is masked. The library ships `ToolResultMasker(context_tokens={role: n}, keep_last=3)`: before every
  model call and before compaction (it does not call a model). It maps the history **that has not been masked yet**
  into lite-context neutral messages (the key is the event seq, `e<seq>`; the summary is `summary`), counts the
  system prompt and tool definitions in the run snapshot as fixed, and uses lite-context's `fit()` +
  `MaskToolResults(keep_last)` with the `state` stored in the previous `tool_results_masked`; it masks only when
  the estimate exceeds 90% of this role's threshold, and masks down to 70%. The newly masked results are written
  into one `tool_results_masked` (each result and the text shown from then on), and the projection replaces them
  according to this event and adds no text itself (§4 rule 2); the same result is never masked twice. If `fit()`
  cannot make it fit (`DoesNotFit`), nothing is masked and it is left to compaction; any other refusal from
  lite-context also masks nothing: masking never makes a run fail. `keep_last` is at least 1 (the latest result is
  always shown to the model). The results of `keep_tools` (default `load_skill`, `recall`) are never masked: they
  are instructions and notes the model goes on to follow, not data read once; in lite-context the message that
  holds them is marked as pinned. In the Ollama eval, without this, qwen3:8b's skill task fell from 3/3 to 0/3
  (§15 Stage records). When the `state` stored after compaction does not match (`StaleState`), it is measured
  again; a `state` that cannot be read (written by a newer lite-context) is also measured again. When both are
  configured, the compaction threshold should not be lower than the masking one. The estimate does not count
  thinking blocks (same as the rendering).
  **When to turn it on** (long-task eval, §15 Stage records 2026-11-26): off by default, and keep it off. Masking
  saves input tokens (−32%) and time (−57%), but qwen3:8b never re-reads a masked result following the placeholder
  (0 times in 9), answers only from the last one or two copies it can still see, and makes up what it cannot see
  (the first copy's gross weight 43.6 was answered as 7.5); summary compaction kept the earlier facts (22 right
  against 9 out of 45, the first copy's gross weight 3/3 right against 0/3). Turn it on only for tasks where an old
  result is used up after being read once (lookups, item-by-item processing, answers that depend only on the most
  recent results), and measure it first on the host's own tasks.
- **Compaction** (S3b; the `Compactor` protocol: `check(roles)`, `due(projection, role)`, `cut(events)`,
  `prompt(history)`). Since 2026-11-26 the prompt, the rendering and the summary frame come from lite-context
  (§13): `render()` maps the history into lite-context neutral messages (`neutral.to_neutral`) and then uses its
  `render_transcript`; `frame(reply)` wraps the reply with lite-context's `SUMMARY`, and removing a copied-back
  header line is still a rule here (any header with 16 or more hexadecimal characters is removed, not only this
  time's; lite-context removes only this time's). The history does not contain the system prompt
  (`projection.history`), and passing a system message is a `ValueError`. The cut is computed by `model_turn`, on
  event seq, and is still the job of `SummaryCompactor.cut`; lite-context's `plan_compaction` (by human turns, on
  messages that carry a key) is not used.
  Without `compaction` nothing is compacted, and exceeding the context is an ordinary model failure. The library
  ships `SummaryCompactor(context_tokens={role: n}, keep_turns=6)`:
  - **When**: before every model call, after the budget check. The request to be sent is estimated into tokens as
    "characters ÷ 3.5" (message text, tool arguments, tool results and tool definitions all count; this is an
    estimate, not a count), and if it exceeds the threshold of **the role to be called this time**, it compacts.
    After switching to `fallback_role`, its threshold is used. If the main role or `fallback_role` has no threshold,
    building the `Agent` raises an error.
  - **Where to cut**: keep the most recent `keep_turns` `model_turn` (with their tool results) and cover everything
    before them. The cut point is "the seq just before the first kept turn", so it never splits a call from its
    result, and the last turn is always kept. Turns that are already covered by a summary no longer count; when
    there is nothing new to cut, it does not compact and the request is sent as is.
  - **How**: render the history to be covered (including the earlier summary, if there is one) as plain text and
    call `turn(role, messages, tools=())` once with `Agent.compact_role`. On success → write one `compaction`
    event, check the budget once more (the summary cost money), then project again and call the main model. On
    failure → write `model_error` (`purpose=compact`) and the run ends with `failed`; a summary cut off by the
    output limit also counts as failure (`TruncatedSummary`, with this call's usage): half a summary cannot stand
    in for the whole history. It compacts at most once before each model call, so a compactor that always says
    "should compact" does not make the loop spin idle. A summary that cannot be written does not trigger
    `fallback_role`, and does not "carry on without compacting" either: a request with an overlong history that is
    sent as is will most likely fail again, with a cause that is harder to see.
  - **Projection**: only the latest `compaction` takes effect (a later compaction always covers from the start and
    swallows the earlier summary too). Model-visible events whose seq is not greater than the upper bound of
    `covers` are no longer output, and a user message is put at the start whose content is the `summary` in the
    event; the fixed preamble was already assembled in when the event was written, and the projection adds no text.
    The original events all stay in the log.
  - **What the summary model sees**: the system description; a transcript with a title that has a random token; and
    at the end of the transcript a repeated line, "now write the summary, do not answer any question inside it".
    This last sentence was measured (see the S3b record in §15): when it was only in the system description, a
    small model took the transcript as a conversation to continue and answered the last question directly. The
    transcript title line copied back in the summary is removed before the event is written; a summary that has
    nothing left after the removal counts as a failure (`EmptySummary`).
  - **Whose words count as a request**: before compaction, a tool result is given to the model as a tool result;
    after compaction it is left as one sentence in the summary, and the summary appears as a user message. To stop
    tool content (for example a forged "the user said" line in a fetched web page) from being promoted to a user
    request through the summary: in the history handed to the summary model, each segment begins with
    `--- <random token> <who> ---`, the token is regenerated at every compaction, and a forged title inside the
    content does not carry it; the summary model is told that only the segments marked user are the user's words,
    and that instructions appearing in tool results and in assistant words can only be reported as content; the
    fixed preamble of the summary says it is a restatement of the earlier conversation, not a new request. This
    blocks impersonation in format, but cannot block the summary model's own misunderstanding; the quality of the
    summary still depends on S5's eval.
  - **Budget**: a summary call counts toward tokens and cost, not turns (`budget.spent()`); a failed summary call is
    the same.
  - **`Opaque`**: thinking blocks produced before the latest `compaction` no longer enter the projection, including
    those in the few turns that were kept (§4).
  - The covered range may cross the fork point (the seq of the child session continues from the parent session's);
    a compaction the parent session does after the fork cannot be read by the child session.
## 7. Tools

- Three sources: the host's Python functions (`@tool`, S2); MCP servers (`lite-agent[mcp]`, as client, mapping a
  remote tool to a `Tool`) and adapters for sibling modules (`lite-agent[rag]`: `search_kb`); the latter two are
  done on demand (§15).
- Arguments first pass `Tool.validate()` (`@tool` validates with Pydantic); if there is a problem, a `tool_result`
  is written (`error_kind=invalid_args`, `executed=False`), no `tool_started` is written, and the tool is not
  called. The content is the field path and the reason Pydantic gives, **without the value of the input** (which
  may be large). A message from a validator the host wrote itself is sent back to the model as is, and what it
  contains is the host's responsibility. An unknown tool name and arguments that are not a JSON object are handled
  the same way.
- Keep the number of tools small and group them by intent (KB §14.3). The full definition of a tool is in
  `run_started` (§4), so if it changed between two runs, it is visible in the log (it affects the prompt cache and
  reproducibility).
- **Externally executed tools** (`external=True`; the HTTP shell's "client executes the tool" uses it, §10). Event
  order: validation → gate → `tool_started` (`external=True`) → `paused` (`reason=tool`, with `call_id`) → the run
  ends with `paused` → the host executes → `resume(session_id, ToolResults({call_id: ...}))` → `resumed` →
  `tool_result` (`origin=external`). When there are several external calls in the same turn, they pause together.
  The results can be handed in over several times: those not handed in make the run pause again (the
  implementation is simpler than "must hand in all at once", and more tolerant for the host). Handing in a result
  again for a `call_id` that already has one raises `ValueError` and writes no event. If the host executed it but
  the submission was lost: this call is still "started, no result", and the host just submits again; side effects
  on the host's side are de-duplicated by `call_id`. A result reported by the host has `origin=external`, and on an
  error `error_kind=exception`; a `terminal` external tool can still end the run when it succeeds.
## 8. Budgets, gates, pausing

- `Budget(turns, tokens, cost_usd, auto_continues, tool_result_chars, tool_timeout_s)`: all optional, and by default
  only `turns=30`. For the scope see §5 rule 6.
- A `Gate` runs before every tool call executes: `Allow`, `Deny(reason)` (sent back to the model as an error
  result), `PauseFor(approval)` (the run ends with `paused`). Credentials stay outside the agent; approval makes the
  run end instead of leaving the process hanging and waiting (KB §12.4).
- **Pause and resume**: on pausing, a `paused` event is written and `RunResult.outcome="paused"`; `resume()` reads
  the log, writes `resumed` and the matching input (the approval decision or the tool result), and continues from
  the next step. Resuming does not depend on the memory of the original process.
- **Lifecycle of a tool call: at least once, not exactly once.** The order is: validation → gate → write
  `tool_started` → execute → write `tool_result`. The `call_id` is stable across the whole session and is handed to
  the tool through `ToolContext.call_id`; the tool should pass it as an idempotency key to downstream systems that
  have side effects.
- On resume, each call is judged from the log:
  - Has a `tool_result`: it is not executed again. A `call_id` has at most one `tool_result`.
  - No `tool_started`: it goes through normally (validation, gate, execution).
  - Has `tool_started` but no `tool_result` (the process died between the side effect and writing the result): the
    result is unknown.
    - A tool with `idempotent=True`: execute again with the same `call_id`.
    - An external tool: keep waiting for the host to submit the result (§7).
    - Otherwise: **do not write** a `tool_result`, write `paused` (`reason=reconcile`, with `call_id`), and the run
      ends with `paused`. After a person checks the downstream system, `resume(session_id,
      ToolResults({call_id: ...}))` writes the verified result as a `tool_result` (`origin=reconciled`); if it
      cannot be verified, submit an error result with `error_kind=outcome_unknown`, so that the model knows the
      state of this step is unclear.
  - The harness does not guarantee exactly once on the host's behalf.
- **One mechanism** (S2b, `loop._Run.settle` / `settle_turn`): a new turn and a resume go through the same code: the
  calls in the last turn that have no result yet are handled one by one in order. A call that needs to wait does not
  block the other calls in the same turn (the model sent them in parallel and they do not depend on each other); as
  long as one is waiting, the run writes `paused` and ends with `outcome=paused`, and `RunResult.pause` lists each
  waiting call and its reason.
  - After approval (`approve=True`) the gate is not asked again: the person's decision replaces the gate.
    `approve=False` → `error_kind=denied`.
  - **Waiting for approval is written in the log** (the `paused` event): a call that has not got a decision keeps
    waiting, and on resume the gate is not asked again, regardless of which gates the `Agent` that resumes it is
    configured with now. A decision for one call does not release another call in the same turn that is waiting.
  - The decisions and results that a `Resume` brings apply only to the turn it is meant to handle: even if a later
    turn reuses the same call id (models often do this), it has to pass the gates again.
  - **A gate error is treated as a denial**: if a gate raises an exception, or returns something that is not
    `Allow` / `Deny` / `PauseFor`, this call goes back to the model with `error_kind=denied` (the content says which
    gate and what kind of error, without the text of the exception), and the run continues. Without permission, it
    is not executed.
  - Before an interrupted idempotent tool is re-run, the arguments stored at the time are first checked with the
    current `validate()`; if they are no longer valid, it is not re-run and is turned into a `reconcile` that waits
    for a person to check.
  - Validation comes before the gate: a call with invalid arguments is not taken to ask a person.
  - When there is no unfinished call, `resume()` only re-sends the last turn (the previous model call failed, §5
    rule 9). A run that has already `completed` (including one that ended with a terminal tool, with the history
    stopping at a tool result) and a session whose history ends with a complete assistant reply have nothing to
    resume and raise `ValueError`.
  - Every `resume()` is a new run (a new `run_started` and snapshot, then `resumed`); the budget is summed from the
    log and is not reset.
- Calling `run(task)` on a session that has unfinished calls still raises `UnresolvedCalls` (suggesting `resume()`):
  adding another task on top of a history with missing results would be rejected by the API, and a tool that may
  already have executed must not be re-run blindly.
## 9. Runner and Temporal (S7)

- `InProcessRunner` (default): the loop runs to completion or pause in the caller's event loop.
- **Temporal** (`lite-agent[temporal]`, temporalio 1.36.1; S7a): `lite_agent.temporal`.

**Granularity: one activity is one complete `Agent.run` / `resume`** (runs to the end or to a pause, sending
heartbeats as it goes), not "one activity for each model call and each tool execution" (the direction this section
originally had). Reasons:
- The event log already lets the loop carry on at event granularity: the `run_id` in §3 keeps a retried activity
  from opening a second run, and the rules in §8 handle a call that died inside a tool;
- If each step were split into activities again, there would be two orchestrators, each recording progress (KB
  `llm-app-architecture` §7.1).
Retrying model calls still belongs to lite-model (the same target; Retry-After is handled by it); activity retries
are only for a worker crash.

**`AgentWorkflow`** (`lite_agent.AgentWorkflow`):
- It decides only from the return values of activities (`Step`: outcome, stop, text, the waiting calls with their
  tool names and arguments); reading and writing the log happens only in activities, so the workflow code is
  deterministic.
- The `run_id` of each run and the `session_id` of a new session come from `workflow.uuid4()` (the same on replay).
  When an activity is retried it carries the same `run_id`, and the rule in §3 guarantees that a second run is not
  opened.
- On pausing:
  - For a tool the host executes (reason `tool`) whose name is in `routes` → call the activity of the same name on
    that task queue, and hand the result back as `Resume.results` (a TypeScript host implements these activities in
    its own worker); if the activity fails → give the model an error result carrying only the type name;
  - Otherwise (approval, reconcile) → wait for the signal `decide(approve, results)`; a decision is valid only for
    the calls that this pause is still waiting for (an answer for a pause that has already been answered, or for a
    call already resolved by a host tool, is ignored); an answer that arrives while a resume is in progress is kept
    only for the same batch of calls that are still waiting after the resume;
  - Then call the activity to resume with a new `run_id`.
- Query `status`: the `Step` of the last run (what a paused session is waiting for).
- **Continue-As-New**: at a pause, when this stretch of the workflow has already executed `max_runs` runs (default
  50), or when Temporal suggests it; the paused `Step` and the decisions already received are carried to the next
  stretch, which carries on from the log. A stretch that starts from a carried-over pause does not Continue-As-New
  again before it has executed a run of its own (otherwise it would keep continuing forever: a test caught this).
- Retry policy: at most 5 times; `UnknownAgent`, `UnknownSession`, `UnresolvedCalls` and `ValueError` are not
  retried. Timeouts are configurable: the run one hour, the heartbeat two minutes, a host tool ten minutes.
- Sandbox: `lite_agent`, `lite_model` and `pydantic` are loaded once outside the sandbox (`SANDBOX`), and the
  workflow code lets only data pass through them.

**Entry points**:
- Worker process: `make_worker(client, task_queue, registry)` or `liteagent worker --agents agents.toml
  --task-queue …` (the agent registration in §10).
- Host: `TemporalRunner(client, task_queue)`: `start(agent, task, routes=…)` → workflow id, `decide(...)`,
  `status(...)`, `result(...)`. It does not implement the `Runner` protocol: an `Agent` object cannot be passed
  across processes, so the agents in the worker are registered by name.

### 9.1 inbox and timer (S7b; KB `llm-app-architecture` §12.1–12.2)

A resident agent: webhooks, schedules and human replies are **only written into one durable inbox** and never call
the agent directly; one inbox has one worker.

- **Tables** (PostgreSQL migration `0002_inbox.sql`, in the same schema as sessions):
  - `inbox_events`: one row per event, de-duplicated by `(inbox, source, source_id)` (a source that delivers twice is
    recorded once). The kind is `message` (text; with a `session_id` it continues that session) or `decision` (an
    approval decision / an external result, which must carry a `session_id`). The status goes pending → done /
    refused, and records the session this run used.
  - `timers`: `follow_up` (a deadline the agent sets itself, fires once) and `schedule` (every `every_s` seconds).
    They are not work: when due, `fire_due(now)` writes an inbox event and advances the timer in the same
    transaction; the source id of one firing is "timer id @ due time", so it is written only once; several periods
    missed during downtime are made up with a single firing, and the next one is the first moment after now.
- **`PgInbox`** (`lite_agent.pg.inbox`) is built on `PgSessionStore`: `put`, `pending`, `finish`, `add_follow_up`,
  `add_schedule`, `fire_due`.
- **`InboxWorkflow`** (one per inbox, workflow id `inbox:<name>`, with Temporal guaranteeing there is only one
  worker): it takes out the pending events and handles them in order of arrival: consecutive messages of the same
  session are merged into one run (several quick messages get one reply), and a decision goes through resume; each
  work item uses the run activity of §9 (the `run_id` comes from `workflow.uuid4()`, so a retry after a crash does
  not repeat), and then it is marked done. A run that needs approval ends after it pauses; the decision comes in as
  a decision event and the run carries on (KB §12.4). An event the agent cannot take (the session is not waiting
  for a decision, or another message arrives while it is waiting for a resume) is marked refused and not retried
  forever; **other failures** (the model or the database being away for longer than one item's retries) do not lose
  work: this item stays pending, and the worker waits a while (starting at 30 seconds, doubling each time, at most
  one hour) and then retries it **as is**: the same event, the same session id and run id, and a half-done run
  carries on. With no events, but an item that has still been failing more than `max_retry_s` (default one day)
  after its first failure, it is marked **`failed`** and left for a person to look at (a poison message must not
  block the following events forever; migration `0003` added this status). With no events, it waits for the signal
  `wake`, and also looks by itself after at most `idle_check_s` (five minutes); when the batch is large enough or
  Temporal suggests it, it does Continue-As-New (the work is in the inbox, not in the history; the item waiting for
  retry is carried over).
- **`TimerWorkflow`** (one globally, id `lite-agent-timers`): it wakes every `interval_s`, calls `fire_due` with the
  workflow's time, and does a signal-with-start on the inboxes written to and on inboxes that have an event pending
  for more than `stalled_s` (one minute): a handler that wrote the row but died before it could wake the worker has
  its events recovered here (every event records its agent). It never starts a run. The time comes from the
  workflow (`workflow.now()`), and the time-skipping test server can fast-forward it.
- **Entry point**: a webhook handler only calls `enqueue(client, inbox, task_queue, agent=…, name=…, source=…,
  source_id=…, text=… | approve=…)`: it writes the row first and then does the signal-with-start.
  `start_timers(client, task_queue)` starts the scheduler (starting it once more finds the one already running).
  `liteagent worker --agents … --inbox --timers`.
- Dependencies: `lite_agent.temporal` can be imported without the `pg` extra; psycopg is needed only when the inbox
  is used.


## 10. HTTP shell (S6)

`liteagent serve --agents agents.toml [--host 127.0.0.1] [--port 8730]` (extra `server`: FastAPI 0.162.2, uvicorn
0.54.0, the same as lite-mem). In code it is `lite_agent.server.create_app(registry)`. It holds no logic of its
own: it checks the request and then hands it to the `Agent` (rule 10).

**Agent registry** (`lite_agent.registry`, also used by S7's worker): `agents.toml` points each name at a Python
factory `module:function`; the factory receives the shared session store and returns an `Agent`; `[store] kind` is
`postgres` (reads `LITEAGENT_DATABASE_URL` / `LITEAGENT_SCHEMA`) or `memory`. The file, the import, the return
value, and an agent that is not on the shared store all report errors at startup (`RegistryError`). A request can
choose an agent only by name and cannot carry tools or code: the request body is `extra="forbid"`, and one extra
field is a 422.

| Operation | Behaviour |
| --- | --- |
| `POST /runs` `{agent, task, session_id?}` | Starts a run in the background and immediately returns `202 {session_id, run_id}` (the server generates both, using §3's `run_id` / `new_session`); if `session_id` is given and it is waiting for a resume → 409 |
| `GET /sessions/{id}/events?after=N` | The log, `events.dump()` as is |
| `GET /sessions/{id}/events/stream?after=N` | SSE (FastAPI's `EventSourceResponse`), each event `event: <type>`, `id: <seq>`, `data: <dump>`; after the run ends it sends the rest and then closes |
| `POST /sessions/{id}/resume` `{agent, expected_seq, approve?, results?}` | `expected_seq` is not the tail → 409; a decision or result given for a call that is not waiting for it (including one that already has a result) → 422, without overwriting; otherwise resume in the background, 202 |
| `POST /sessions/{id}/fork` `{at_seq}` | `{session_id}`; a fork point that does not exist → 422 |
| `GET /agents` | Names, roles, tool schemas |

- **Only one run per session at a time**: as soon as a request passes the checks it "claims" this session (there is
  no await between the check and the claim, so two requests that arrive at the same time cannot both pass), and
  the claim is released when the run ends; the second one → 409. This is in-process only: across processes, the
  real guarantee is the store's `expected_seq` check. A session started by the server can be read (empty) even
  before the first batch of events is written, and a session that does not exist is still 404.
- **Errors**: 404 `UnknownSession` / unknown agent, 409 conflict, 422 invalid; the error body contains no upstream
  text (rule 14).
- **Binds to loopback only**: `--host` must be a numeric loopback address (`127.0.0.1`, `::1`); names (including
  `localhost`) may resolve elsewhere and are all refused. There is no authentication; authentication and
  multi-tenancy will be designed when it is offered externally.
- **A web page cannot reach it**: bound on loopback, a web page can still re-resolve its own domain to 127.0.0.1
  (DNS rebinding), or send requests straight from the browser. So the `Host` of every request must be a loopback
  name (`127.0.0.1`, `::1`, `localhost`, with or without a port), otherwise 400; an `Origin`, if present, must be
  **exactly the shell's own** (`http://` plus the same `Host`; a page on another port of the same machine also
  counts as foreign), otherwise 403; every POST must be `application/json` (a form or plain-text POST needs no CORS
  preflight, so a web page can send it blind), otherwise 415.
- **Shutdown**: on shutdown it waits for runs still in progress to end (`drain`) and does not cut one off in the
  middle of a turn.
- TypeScript host: the `/openapi.json` generated by FastAPI; no TS package. Ways for the host to provide tools: an
  MCP server (the on-demand stage), external execution (§7, pause → `resume` with the result), a Temporal activity
  (§9).

## 11. Configuration

- The environment variable prefix is `LITEAGENT_` (pydantic-settings); model configuration is lite-model's
  `ModelsConfig`, and this module only declares `RoleSpec`: `agent` (the host may choose another name), `compact`.
- Configuration files hold data: role names, budget values, the skills directory, the store URL, the list of MCP
  servers.
- Init arguments hold code objects: tool functions, gates, a custom store / compactor / memory.
- Validated at startup (the role exists, tool names are unique, the schema can be exported; whether an MCP server is
  reachable is not checked at startup), so failures show up at startup and not at the first call (KB §1.5).

## 12. Tests and evals

- Unit tests script each turn with lite-model's `AsyncFakeModel` (this needs it to support tool-call turns, see
  §13). `tests/conftest.py` refuses every socket connection.
- Each rule in §4 and §5 has at least one test that asserts a concrete event sequence, not just "it ran through".
- Replay test: for a recorded log, the projection result is verbatim equal to the messages sent at the time.
- `-m ollama`: a real local model runs a small task (free, opt-in). There is never a test that calls a subscription
  or a paid API.
- Eval (S5): a lite-eval target, with the task as input and the trajectory (event log) as output; the metrics have
  at least task completion rate, number of turns, tokens and number of invalid tool calls. Run once before and once
  after changing a loop rule or the compaction strategy ("measure, don't guess").

## 13. Relationship to other projects

| Project | Relationship |
| --- | --- |
| lite-context | **Runtime dependency** (release wheel, since 2026-11-26, decided by the owner; its core has no dependencies). The compactor's prompt (`ASK`, `END`), the summary frame (`SUMMARY`) and the transcript rendering (`render_transcript`) use its versions, verbatim the original text of S3b (lite-context was copied from here in the first place); the cut point, the `compaction` event and the projection are still here (§6). lite-context's `fit()` / masking is not connected (C7's scope reaches only compaction) |
| lite-model | **Runtime dependency** (release wheel, ≥ 0.4.0). It uses its `AsyncToolModel.turn(role, messages, tools) -> TurnReply` (lite-model design §3.1, implemented 2026-11-21): ordered `AssistantTurn.blocks`; `ToolResults`; a truncated turn **returns** `stop="length"` and its calls (§5 rule 1 relies on this); when the arguments are not a valid JSON object, `ToolCall.arguments is None` (treated as `invalid_args`); the cross-protocol rule for `Opaque` is in the adapter; `AsyncFakeModel.turn` scripts the turns. `turn()` does not go through lite-model's hook. lite-model does not handle the history check for preserved thinking, and the projection here is responsible (§4); that kind of 400 is now `CallFailed` and cannot be told apart from other failures (lite-model §15) |
| lite-cache | Not connected in v1 (L1 is expected to almost never hit: the context is different on every turn; not measured). To be decided when S5's eval shows repeated calls. The eval itself must bypass the cache |
| lite-mem | Optional extra `mem`: an implementation of `Memory` |
| lite-rag | Optional extra `rag`: a `search_kb` tool |
| lite-eval | `eval` extra and dev dependency: trajectory eval (§12) |
| llm-steward | A possible host; it is imported by no module. Whether the agent's memory writes must go through its governance is in §16 |
| llm-gateway | Reached only through lite-model's HTTP; tests never call a subscription profile |

None of the sibling modules imports lite-agent. The repo table, dependency graph, pin table and port table of the
workspace `AGENTS.md` are updated in S0.
## 14. Project structure

```
src/lite_agent/
  events.py       Event types, validation, vocabulary version
  projection.py   derive_messages(), token estimate
  store.py        SessionStore Protocol, InMemorySessionStore
  tools.py        Tool Protocol, @tool, registry, argument validation, truncation
  gates.py        Gate Protocol, Budget
  loop.py         one step: model call -> tool calls -> results; stop rules
  agent.py        Agent (assembly), run / resume / run_sync
  runner.py       Runner Protocol, InProcessRunner
  context/        skills.py, memory.py, compaction.py
  config.py       Settings (LITEAGENT_), startup validation
  cli.py          liteagent migrate | sessions | show | fork | config   (S4b; no run / resume, §15)
  config.py       Settings (LITEAGENT_DATABASE_URL, LITEAGENT_SCHEMA)
  pg/             PgSessionStore, db.py (migrations, vendored), migrations/  (extra: pg)
  mcp.py                                                 (extra: mcp)
  adapters/       lite_mem.py, lite_rag.py               (extras: mem, rag)
  temporal/       workflow.py, activities.py             (extra: temporal, S7)
  server.py                                              (extra: server, S6)
tests/unit/  tests/integration/  tests/replay/
eval/lite-eval/
docs/lite-agent-design.md
```

## 15. Stages and acceptance

Every rule in §4–§8 must map to at least one named test; at the close of a stage, the "rule → test" mapping is
listed.

Every stage closes with step 4 of the AGENTS.md Workflow: `/simplify`, then a read-only Codex review, then fixes
(rejected ones are written in the Stage records of this section), and finally a re-run of all checks.

| Stage | Content | Acceptance |
| --- | --- | --- |
| P0 (in the lite-model repo) | **Done, v0.4.0 released on 2026-11-21**: `turn()` (its design §3.1, §14 Stage records) | lite-model's `tests/contract/test_turns.py` (two protocols, sync and async) and one real tool turn under `-m ollama`; the existing hosts' tests are unchanged |
| S0 | Skeleton: `git init`, pyproject (uv, ruff, mypy strict, pytest), CI, `.gitignore` (including `!CLAUDE.md`), `.env.example`, README, LICENSE; depends on the lite-model wheel; update the workspace `AGENTS.md` | ruff, mypy and pytest all green; a test that refuses sockets exists |
| S1 | Core: events and validation, `InMemorySessionStore`, `derive_messages`, the loop (§5 rules 1, 2, 6, 7), `Agent.run`, `InProcessRunner`; the `Agent` / `SessionStore` / `Runner` interfaces of §3 fixed | Scripted with a Fake: completes after two turns of tool calls, with the event sequence equal event by event; a truncated turn executes no tool, gives one error result per call, and the projection of the next request contains these results and the loop continues to completion; with a mix of terminal / ordinary results it continues; before each request the "messages == projection" assertion is in force (it fails when deliberately broken); a failed call that carries usage (`Truncated` etc.) counts toward the total, and the usage of `CallFailed` is recorded as `None` and not estimated; the system and tools in the request equal the snapshot of `run_started`; an invalid event is rejected and the log is unchanged; an unknown event vocabulary version is rejected; every run starts with `run_started`; when the model reply was obtained but `append` fails, the run fails and the log is unchanged (injected with a store that fails) |
| S2 | Tool registration and `@tool`, argument validation, result truncation, `Budget`, `Gate`, pause / resume (in-process), auto-continue (rule 3), the steering interface, externally executed tools, model failure and `fallback_role` (rules 9–11) | Over budget stops before the call; `Deny` becomes an error result; after `PauseFor` it is `outcome=paused`, and after `resume` it continues and calls that already have results are not executed again; auto-continue at most N times; external tool round trip; on `CallFailed` without a fallback configured it is `failed` and `resume` re-sends this turn without re-running tools; with one configured the event sequence is `model_error` → `model_switched` → `model_turn` (provenance is the fallback), and every later turn uses the fallback; if the fallback also fails it is `failed`; `Truncated` does not trigger a switch; the messages handed to lite-model equal the projection (dropping of proprietary content is tested in P0); the total is correct when `CallFailed` (usage `None`) is mixed with errors that carry usage; fault injection: a crash after the tool executed and before the result was written: an idempotent tool is re-run with the same `call_id`, and a non-idempotent tool pauses with `reason=reconcile` and no `tool_result` is written at that point; one crash after `model_error` and one after `model_switched`, and both recover using the main role; when the arguments are invalid the tool is not called (`executed=False`, no `tool_started`); a tool timeout is `error_kind=timeout` and the same `call_id` is not executed again; pause after the switch and then resume, the next call uses the main role; the budget is summed per session (pause and resume does not reset turns); a result submitted after a `reconcile` pause is written with `origin=reconciled` and that call has only one result; a repeated submission for an external tool is rejected; when an `Agent` with a changed tool definition resumes, the new `run_started` records the new definition and the `Opaque` before that no longer enters the projection (a `resume` with unchanged system / tools keeps it) |
| S3 | Context: skills index + `load_skill`, the default `Compactor` implementation (§6), the `Memory` Protocol (an in-memory implementation for tests) | After compaction the original events are still in the log, and the projection replaces that range with the summary; the projection is decided only by the log: removing the `compaction` event gives the complete history, and swapping in a `compaction` event with a different summary or range gives a different request (rewritten 2026-11-21: the original text "swap a compactor and re-project" does not hold, because the summary and range are stored in the event and the compactor takes no part in the projection); the cut point does not split a tool call from its result; a skills index over budget raises an error; after a fork, when the parent session compacts again, the child session's projection is unchanged; an `Opaque` from before compaction is not in the projection, and one produced after is; several `tool_result` in the same turn are projected into one `ToolResults` |
| S4 | `PgSessionStore` (migrations, `LITEAGENT_TEST_DATABASE_URL`, schema `lite_agent_test`), cross-process resume / fork, `expected_seq` conflicts, CLI | Process A pauses, process B resumes; a session produced by the in-memory store can be resumed after being imported into the PG store (KB §10.3); a fork does not copy events and the two do not affect each other; with concurrent appends one succeeds and one is rejected; the append of a batch of events is all or nothing; in a fork of a fork, one compaction whose covered range crosses both fork points is done, and the projections of all three layers of sessions are as expected |
| S5 | A lite-eval target; a fixed small set of tasks, each with a written completion condition (judged by code); the `-m ollama` smoke test runs only one of them that uses a tool | The metrics (completion rate, turns, tokens, invalid tool calls) go into the report; the deterministic run with a Fake goes into CI; the Ollama baseline numbers are recorded in the KB |
| S5.5 | Release the v0.1.0 release wheel | A new project can install from the wheel and run through the README example |
| S6 (done, v0.2.0) | HTTP shell (§10); first fix the protocol listed at the end of §10 | Originally "do it when a TypeScript host needs it"; done on 2026-11-21, ahead of the host |
| S7 (done, v0.2.0) | `TemporalRunner`, inbox / timer (§9); first write §9 as a complete design | Originally "do it when there is a real long-running task"; done on 2026-11-21, ahead of the host; replay tests, activity retries do not write events twice, Retry-After, Continue-As-New |
| On demand (when each has a host that needs it) | MCP client (extra `mcp`, tested with an in-process fake server), lite-mem adapter, lite-rag tool | — |
| On demand | streaming, parallel tools, follow-up queue, subagents, a file-based session store, a runtime registry | — |

### Stage records

- **Design review (2026-11-20, Codex, read-only, the whole design)**: 3 Critical, 7 High, 6 Medium.
  - Accepted: the tool call lifecycle and crash recovery (`tool_started`, `call_id` as the idempotency key,
    `outcome_unknown`, §8); which role is used when switching models is decided by the log, and recovery at each
    crash point (`run_started`, §5 rule 11); the projection is backend-independent, the invariant compares the
    canonical representation, and proprietary content is dropped by the lite-model adapter (§4, §13); the seq space
    of a fork written out (§4); Temporal determinism and activity retries (§9, and the complete design must be
    written before S7); P0 as a formal lite-model stage, acceptance completed, "four items" changed to "five
    items" (§15); the fallback's context window behaviour (§5 rule 11); the protocol requirements for HTTP external
    tools (§10); timeout separated from "not executed" (`error_kind`, `executed`); fault injection and the rule
    mapping added to acceptance (§15); the event content for an argument validation failure (§7); the default
    compaction strategy written out (§6); capabilities changed to be injected at construction, with no runtime
    registry (§3); S5 uses fixed tasks and completion conditions; the MCP / lite-mem / lite-rag adapters moved to
    "on demand"; lite-cache changed to "not connected in v1, decide after measuring".
  - Rejected: "after a fork, a compaction in the parent session changes the child session's history": the child
    session reads only the events of the parent session with `seq ≤ at_seq`, and a `compaction` appended later
    cannot be read; the original text was not clear, it has been written out and a test added in S3.
  - Partly rejected: "the v1 scope is too large, decide after S0–S2 when there are real tasks": S6 and S7 were
    already on demand, and the adapters have been moved away; but S3 (skills, compaction), S4 (persistence,
    cross-process resume, fork) and S5 (eval) are kept: the goal is to verify this architecture (§0, owner), and
    these three items are exactly the parts to verify. `fallback_role` is the owner's decision and is kept.
- **Design review, second round (2026-11-21, Codex, read-only, on the revised full text)**: 5 High, 8 Medium, 2 Low,
  all accepted.
  - The system prompt and tool definitions go into the `run_started` snapshot, and the invariant includes tools; the
    snapshot is tightened from "per turn" to "per run" (§4, §5 rule 4), which also answers "which one governs when
    an `Agent` with a changed configuration resumes".
  - The way out of `outcome_unknown`: no result is written on pausing, and after checking, the host submits it
    (`reason=reconcile`, `origin=reconciled`, §8).
  - The full event order for an external tool and the handling of a lost submission (§7); a retry after a timeout =
    a new call, and the same `call_id` is not re-run (§5 rule 10).
  - Budget scope: turns / tokens / cost per session, `auto_continues` per run (§5 rule 6).
  - Pause after a switch and then resume = a new run, on the main role, written down as on purpose (§5 rule 11).
  - Compaction: none given means no compaction; the threshold configuration of `SummaryCompactor` and the error when
    it is missing; the events for success / failure, and how the budget is counted (§6).
  - The atomicity of `append` and the behaviour of "the reply was obtained but not written to the log" (§4); the
    event table completed with all fields.
  - Acceptance added: continuing after truncation, the usage of `CallFailed`, pause and resume after a switch, a fork
    of a fork plus compaction across fork points (§15).
- **S0 (2026-11-21)**: `git init` (local only at the time; the owner decided to make it public after S1), pyproject
  (lite-model v0.4.0 written into the metadata as a direct URL, the same as lite-cache), ruff / mypy strict / pytest
  + pytest-asyncio, CI files (test / package / release; after the repo went public it ran on GitHub for the first
  time and passed), socket guard, `scripts/smoke.py`, README, LICENSE, `.env.example`; the workspace `AGENTS.md` has
  been updated. Checks: ruff, ruff format and mypy clean, pytest 4 passed (including one added after the review);
  each of the first three tests was seen red once (remove the socket guard; replace the lite-model URL in the
  metadata with a bare name; the tool-turn test run against lite-model 0.3.0 is an ImportError); locally, following
  the steps of the CI `package` job, installed from the wheel into a clean venv and ran the smoke test through.
  `/simplify` was not run: this stage has no production code.
  Codex read-only review (the whole workspace): 2 findings, both accepted: the socket guard gets UDP `sendto` and
  `getaddrinfo` added (the regression test red first); the README opening changed to state the "goal" and not the
  current state. It judged CI, packaging and `.gitignore` clean.
- **S1 (2026-11-21)**: `events` / `store` / `projection` / `tools` / `budget` / `loop` / `runner` / `agent` /
  `result` / `errors`. Checks: ruff, ruff format and mypy (strict) clean; pytest all green.
  - Differences from the plan: there is no `ToolOutput` (a tool returns `str` directly); `RunResult` / `RunError`
    are in `result.py` (to avoid a circular import `agent` ↔ `loop`); one extra `stop=truncated` (§5 rule 1);
    `session_started` does not carry fork fields (§4).
  - How it was seen red: the implementation of this stage was written before the tests (a departure from TDD), so
    it was confirmed by breaking line by line: of 40 breakages, 39 had a test turn red; the remaining 1 (`Agent`
    copying `tools` into a tuple) turned red after a test was added. Another breakage turned out to be written as an
    equivalent change and does not count.
  - Rule → test (`tests/unit/`):

    | Rule | Test |
    | --- | --- |
    | Basic flow, event sequence equal event by event | `test_loop.py::test_two_tool_rounds_then_an_answer_writes_exactly_these_events` |
    | Request = snapshot + history | `test_loop.py::test_each_request_is_the_runs_snapshot_and_the_history_so_far` |
    | §5 rule 1 | `test_loop.py::test_a_truncated_turn_runs_no_tool_answers_every_call_and_the_loop_goes_on`, `…::test_cut_off_text_with_no_calls_ends_the_run_as_truncated` |
    | §5 rule 2 | `test_loop.py::test_a_batch_of_only_terminal_results_ends_the_run`, `…::test_a_terminal_result_next_to_a_plain_one_does_not_end_the_run`, `…::test_a_terminal_tool_that_fails_does_not_end_the_run` |
    | §5 rule 6 | `test_loop.py::test_the_turn_budget_stops_before_the_call_that_would_exceed_it`, `…::test_the_turn_budget_is_the_sessions_not_the_runs`, `…::test_usage_is_summed_over_the_runs_calls_and_recorded`, `…::test_a_failed_call_that_was_billed_counts_and_one_that_never_answered_does_not` |
    | §5 rule 7 | `test_loop.py::test_a_tool_exception_goes_back_to_the_model_and_the_run_continues`, `…::test_an_unknown_tool_or_unparsed_arguments_is_answered_without_running_anything` |
    | §5 rule 9 (no fallback) | `test_loop.py::test_a_failed_model_call_ends_the_run_as_failed_and_is_in_the_log`, `…::test_running_again_after_a_failure_resends_the_turn_without_rerunning_tools` |
    | §4 write validation, atomicity | `test_store.py::test_a_batch_with_one_bad_event_is_refused_whole`, `…::test_a_stale_expected_seq_is_refused_and_nothing_is_written`, `test_events.py::test_an_invalid_event_cannot_be_built` |
    | §4 version, JSON round trip | `test_events.py::test_an_event_this_code_cannot_vouch_for_is_refused_on_load`, `…::test_every_event_survives_a_json_round_trip` |
    | §4 projection | `test_projection.py` (one `ToolResults` per turn, in call order, the latest snapshot, pure function) |
    | §4 `Opaque` | `test_projection.py::test_opaque_blocks_from_before_a_changed_snapshot_are_left_out`, `test_loop.py::test_a_changed_system_prompt_drops_the_earlier_thinking_from_the_request` |
    | §4 invariant | `test_loop.py::test_a_store_that_lost_an_event_stops_the_run_before_the_next_request` |
    | §4 reply not written to the log | `test_loop.py::test_a_reply_the_store_cannot_take_fails_the_run_and_leaves_the_log_as_it_was` |
    | Read semantics of fork (full acceptance in S4) | `test_store.py::test_a_fork_reads_the_parents_prefix_and_then_its_own_events` |
  - `/simplify` (four passes): accepted: the store's contract check extracted into `check_batch()`, so every future
    store calls the same function; `EVENT_TYPES` taken from the `Event` union instead of a second hand-written copy;
    in the loop the tool result is built in one place only, `_answer()`, `execute()` returns whether it is terminal,
    and the truncation branch is flattened; `ToolResultEvent` gets consistency validation (`error_kind` with
    `is_error`, `terminal` with `executed`); the store's reads and writes are only in `_Run.read` / `append`, and a
    failure of the first read also becomes a `store` error (it used to be raised as is; the regression test red
    first); `provenance_data` / `usage_data` / `ToolDef` use `from_attributes`; the self-written model double in the
    tests replaced with lite-model's fake plus usage (`Billed`). Left for later: the invariant check reading only
    the tail (needs a parameter added to `SessionStore.read`, to be fixed before S4); the input shape of `Runner`
    (fixed together when S2 adds `resume`); moving the conversion between lite-model and events out of `events.py`
    (S3); removing duplication in test fixtures. Not accepted: `turns_used()` scanning the log every turn,
    `Agent.tool()` linear lookup (the scale is tiny).
  - Codex read-only review: 5 High, 6 Medium, 3 Low. Fixed (regression test red first): after a previous run was
    interrupted, calling `run()` again would send the model a history missing tool results: it now raises
    `UnresolvedCalls` (§8, which covers its first four Highs: crash, cancellation, a run without `run_finished`, a
    history with missing results); `load()` accepted `v=True`; when the new id of a fork was the same as an existing
    session it would change its lineage. Written down: `run_sync()` inside a running event loop is a
    `RuntimeError`; the `Opaque` rule is deliberately conservative for A→B→A (a test added). Rejected: "usage is not
    accumulated across runs" (`RunResult.usage` is, by design, of this run; the token / cost budget is in S2); "
    `ToolResultEvent` lacks `origin` / `timeout` etc." (S1's vocabulary is deliberately a subset, written in §4);
    "nested dicts are mutable" (only one level is frozen, the same as lite-model); "a tool returning a non-str should
    not be treated as a tool error" (it is the tool's failure and goes back to the model per §5 rule 7); "the usage
    total hides the unknown part" (§5 rule 6: unknown is not estimated and not counted); "`derive()` should validate
    the structure of any log" (the log is written only by the loop, and the bad states that can be reached are
    stopped by `UnresolvedCalls`); a round-trip test for non-JSON values (arguments come from JSON parsing, opaque
    comes from the SDK's `to_dict()`).
    `test_a_call_whose_usage_is_unknown_is_not_counted_against_the_token_budget`.
  - `/simplify` (two passes, four angles): accepted: `_answer` merged into `_Run.answer`; the argument check turned
    into three early returns (`refuse()`); the timeout and exception branches only pick the value and write once;
    `timeout_s` is tested with `is None` and validated at construction; the tests' `results_of` and the slow tool
    merged into `tests/helpers.py`; a tool with `external` is rejected directly when the `Agent` is built before S2b
    (otherwise it would be executed in-process like an ordinary tool). Left for S2b: splitting `execute()` into two
    steps, "check" and "execute and answer", returning three kinds of result (needed for pausing);
    `RunFinished.usage` changed to be summed from this run's events. Not changed: the duplicated fields of
    `FunctionTool` / `ModelTool`, `spent()` scanning the log each turn, argument validation twice (`__call__`
    validates once more itself, which is safe only when called on its own).
  - Codex read-only review: 1 High, 5 Medium, 2 Low. Fixed (regression test red first): a tool that swallowed the
    cancellation signal and then returned as usual would have its timeout recorded as a success (High); `@tool`
    accepted a sync function and a function with the wrong number of parameters, and the error came only at the
    first call; the budget and timeout accepted NaN / infinity; the README example completed into a runnable script
    and the status changed to S2a. Written down: the truncation budget does not include the note line; a message
    from a validator the host wrote itself is returned as is in a validation error. Rejected: "validating twice may
    have side effects" (a validator with side effects is the host's problem, see above); "a boolean can pass the
    integer budget check" (`True` is 1 in Python, not worth blocking separately). The README example ran through
    as is on the local Ollama's qwen3:8b (2026-11-21): the model called the tool, then answered with the result, and
    the log is 8 events from `session_started` to `run_finished`. This was the first time lite-agent ran the whole
    loop against a real model.
- **S2b (2026-11-21)**: `gates.py` (`Gate`, `Allow` / `Deny` / `PauseFor`, `RunContext`), `Agent.gates` /
  `resume()`, `Resume` / `ToolOutcome` / `Pause` / `Waiting`, events `paused` / `resumed`, `tool_started.external`,
  `tool_result.origin`; in `loop` `execute()` is split into `settle()` (check, gate, start) and `run_tool()`
  (execute and answer), and `settle_turn()` is shared by a new turn and by resume. The tests were written first
  (`tests/unit/test_pause_resume.py`); the implementation was written in one go, so it was also confirmed by line
  by line breaking: of 24 breakages, 22 turned red, and the remaining 2 (the waiting reason of an external call on
  resume, a terminal result reported by the host) turned red after tests were added.
  - Differences from the plan: `Pause` is `waiting: tuple[Waiting, ...]` and not `(reason, call_ids)`: the same
    turn can have calls waiting for approval and calls waiting for the host to execute at once, one reason cannot
    hold that, and the person who approves needs to see the note the gate gives; the `stop` of a pause is called
    `paused` and not `gate`; the results of an external tool can be handed in over several times (§7); S2a's
    "reject external tool" and its test were removed as planned.
  - Rule → test: gate (§8) `test_a_denied_call_is_answered_without_running_and_the_run_goes_on`,
    `test_gates_are_asked_in_order_and_the_first_refusal_wins`,
    `test_invalid_arguments_are_refused_before_any_gate_is_asked`; approval
    `test_a_gate_that_wants_a_person_ends_the_run_as_paused`,
    `test_an_approved_call_runs_once_and_the_run_completes`,
    `test_a_rejected_call_is_answered_as_denied_and_never_runs`,
    `test_the_rest_of_a_batch_runs_while_one_call_waits`; external tool (§7)
    `test_an_external_tool_pauses_the_run_and_its_result_comes_back_on_resume`,
    `test_external_calls_of_one_turn_pause_together_and_may_come_back_in_parts`,
    `test_a_result_submitted_twice_is_refused_and_nothing_is_written`; interrupted calls (§8)
    `test_an_idempotent_tool_is_run_again_with_the_same_call_id`,
    `test_a_tool_that_may_have_run_is_not_repeated_and_the_run_waits_to_be_reconciled`,
    `test_an_outcome_nobody_can_verify_is_told_to_the_model_as_unknown`,
    `test_a_call_that_was_never_started_simply_runs_on_resume`; the boundaries of `resume()`
    `test_resume_after_a_failed_model_call_sends_the_turn_again`,
    `test_resume_refuses_what_it_cannot_apply_and_writes_nothing`,
    `test_pausing_and_resuming_does_not_grant_a_fresh_turn_budget`,
    `test_resuming_under_changed_tools_records_them_and_drops_earlier_thinking`.
  - `/simplify` (two passes): accepted: "the last turn and the events after it" extracted into
    `projection.last_turn()` (it had been written three times); `settle()` split into `recover()` (calls that have
    started) and `admit()` (calls not yet touched); the `Resume` input changed into a parameter of `settle_turn()`
    and applies only to the turn it is meant to handle: **this was a real problem**: it used to be stored on the run,
    and when a later turn reused the same call id the old approval would let it skip the gate (regression test red
    first); `Waiting` is defined only once, and the `paused` event stores records with field names (it used to be an
    unnamed array) and records which gate it was; `Origin` given an alias; `FailingOnce` moved into
    `tests/helpers.py`; the type check in `run_loop()` collapsed into one. Kept: `ToolOutcome` uses two booleans for
    three states (the interface is approved, and the docs say `unknown` takes precedence); one `RunContext` is built
    per call (the gate of a later call needs to see the result of the earlier call). Memo: S7's Temporal runner:
    pausing means the workflow ends and resume starts a new workflow, which is consistent with the present "every
    resume is a new run".
  - Codex read-only review: 3 High, 5 Medium, 1 Low. Fixed (regression test red first): a session that ended with a
    terminal tool could still `resume()` and call the model once more; an idempotent tool re-run did not validate the
    stored arguments; duplicate call ids in the same turn would attach results to the wrong calls; when a gate
    returned something that was not a decision it was treated as allow; a gate that raised an exception would leave
    the run in a state without `run_finished`; when only one call was approved, another call waiting for approval
    could be executed because the gate had changed (the waiting state is now taken from the log). Rejected: "when the
    store committed but the caller saw a failure, resubmitting the result would be refused" (this is the store's
    atomicity contract, and apart from `check_batch` the loop does not cover for it). Not changed: a result the host
    hands back is truncated by the budget in the same way (what the log stores is what the model sees, §4). No second
    Codex round was run.
- **S2c (2026-11-21)**: `Agent.done` (auto-continue), continuing truncated plain text, `Agent.fallback_role`, the
  event `model_switched`, `user_message.origin=auto_continue`, `stop=incomplete`, `RunResult.switched`. What this
  run has done (whether it has switched, how many times it has continued) is read from the log: the events after
  this run's `run_started`. It does not rely on `run_id`, so even if the host's id factory gives the same run id
  twice, nothing gets mixed up. The tests were written first (`tests/unit/test_continue_fallback.py`); in addition,
  16 line-by-line breakages were done, 15 turned red, and the remaining 1 (whether `model_error` and
  `model_switched` are written in the same batch) does not change observable behaviour.
  - One existing test was changed: `test_cut_off_text_with_no_calls_ends_the_run_as_truncated` tested S1's
    "truncated plain text ends directly", and S2c changed that as planned to continue writing first; this test now
    explicitly uses `auto_continues=0` to test "the outcome when there is no continuation".
  - Steering (§5 rule 5) was moved to "on demand" as planned, to be designed together with S7.
  - Rule → test: rule 3 `test_an_unfinished_task_is_sent_back_with_what_is_still_open`,
    `test_automatic_continuation_stops_after_the_budgets_count`, `test_each_run_gets_its_own_continuations`,
    `test_text_cut_off_at_the_limit_is_asked_to_continue`,
    `test_cut_off_text_and_unfinished_tasks_share_one_allowance`; rules 9 and 11
    `test_a_failed_call_switches_to_the_fallback_role_once_and_says_so`,
    `test_a_fallback_that_fails_too_ends_the_run_without_a_second_switch`,
    `test_only_an_unreachable_model_triggers_the_switch`, `test_the_next_run_starts_on_the_primary_role_again`,
    `test_a_resume_after_a_switched_run_paused_starts_on_the_primary_role`,
    `test_a_run_interrupted_around_a_switch_resumes_on_the_primary_role`,
    `test_the_failed_call_and_the_fallbacks_calls_all_count_against_the_budget`.
  - Safety check after the S2b merge (automatic): an approval in `resume()` could be applied to a call that no gate
    had ever blocked, thereby bypassing the gate. This held and has been fixed: an approval is accepted only for a
    call that the log records as waiting for approval
    (`test_an_approval_is_only_taken_for_a_call_a_gate_left_waiting`, red first and then green).
  - `/simplify` (one pass, four angles): accepted: the plain-text branch changed into one "what is left" value plus
    three exits; the last two fields of `RunResult` passed by keyword; `done` returning empty or whitespace is the
    same as finished (it used to send an empty message to the model; regression test red first). Left for S7:
    splitting `loop()` again along the activity boundary; `done` is the host's synchronous code, and when it needs
    to do I/O it needs a place as an activity.
  - Codex read-only review: 1 High, 4 Medium, 1 Low, plus test gaps. Fixed (regression test red first): the state of
    this run was divided by `run_id`, and when the host reused a run id the switch and continuation count of the
    previous run would leak into the next (High; changed to division by position); a plain-text turn and the event
    after it (the continuation message or `run_finished`) were written in two steps, and a crash in between would
    leave a session that looked ended, was not really done, and could not be recovered (changed to be written in the
    same batch, `_Run.stamp` / `write`); when `done` returns a non-string, `TypeError` is raised before any event is
    written; the `run_started` snapshot now includes `fallback_role` (the design had always said so). Test added:
    after a switch there is no budget to call the fallback again. Rejected: "after the continuation count is used up,
    it can continue without limit by calling `resume()` repeatedly": it assumes the log ends with a continuation
    message, but in fact when it is used up what is written is `model_turn` and `run_finished`, and `resume()`
    refuses ("nothing to resume"); to continue one can only `run()` a new task, and the turn / token budget is
    counted per session. No second Codex round was run.
- **S3a (2026-11-21)**: `context.py` (`SkillSource` / `SkillDir` / `SkillInfo`, `Memory` / `NotesMemory`, `build()`,
  the three built-in tools), `Agent.skills` / `memory` / `system_prompt()` / `all_tools`,
  `Budget.skills_index_chars`. The tests were written first (`tests/unit/test_skills_memory.py`); after the
  implementation was written in one go, line by line breaking was done: of 17 breakages 15 turned red, and the
  remaining 2 (frontmatter not on the first line, an extra blank line at the start when the host gave no system
  prompt) turned red after tests were added.
  - Real model: the local Ollama's qwen3:8b, given a skills directory and a `NotesMemory`, asked "which command to
    run first" and told to record one note. The model itself called `load_skill("deploy")`, answered with a command
    that appears only in the skill body, and also called `remember`.
  - Rule → test: the index is in the system prompt and consistent with the log
    `test_the_skill_index_is_in_the_system_prompt_as_logged`; stable assembly
    `test_the_assembled_prompt_is_the_same_every_time`; loading on demand
    `test_load_skill_returns_the_body_as_a_tool_result`,
    `test_loading_an_unknown_skill_is_a_tool_error_that_names_the_real_ones`; budget
    `test_an_index_over_its_budget_is_refused_when_the_agent_is_built`; a bad skill
    `test_a_malformed_skill_is_refused_when_the_directory_is_read`; memory
    `test_remember_and_recall_go_through_the_memory_and_the_log`,
    `test_the_memory_prompt_says_what_memory_is_for_and_scripts_nothing`,
    `test_a_gate_can_refuse_remember_like_any_other_tool`; unchanged when not given
    `test_without_skills_or_memory_the_prompt_and_tools_are_the_hosts_own`.
  - `/simplify` (one pass, four angles): accepted: the built-in tools used to be written back into `Agent.tools`,
    and `dataclasses.replace()` on an agent with memory would fail because of a "duplicate name" (regression test
    red first): now `tools` stays as the host gave it and the merged ones are in `all_tools`; the budget check was
    on the index at construction while every run re-read the source, so when the source grew an unchecked index
    would be sent (regression test red first): now the prompt is fixed at construction; `_parse` no longer uses
    exceptions for flow control; the assembly and built-in tools collected into `context.build()`.
  - Codex read-only review: 7 Medium, 2 Low. Fixed (regression test red first): a repeated key in the frontmatter was
    silently overwritten; a `SKILL.md` with a BOM could not be read; the order of the index depended on the order the
    source gave (now sorted by name); `NotesMemory` removed only full stops, so a word with a comma could not be
    found; a note of only whitespace could be stored. Written down: skill files are treated as trusted content; the
    budget counts only the entries; the exception message of `Memory` goes back to the model. Rejected: "a symbolic
    link can read outside the root" (it is the host's own directory); "when an interrupted `load_skill` is resumed,
    the skill may have changed" (it reads the current content, which is what loading on demand means); "the
    stability test compares an empty directory" (the two agents use the same directory). Test added: an interrupted
    `remember` waits for a person to check and is not re-run.
- **S3b (2026-11-21)**: `compaction.py` (`Compactor`, `SummaryCompactor`, `estimate_tokens`, `render`), the event
  `compaction`, `model_error.purpose`, `projection.history()` (the masking in the projection and the other half of
  the `Opaque` rule), `budget.spent()` counting summary calls, `loop._Run.compact()`, `Agent.compaction` /
  `compact_role`. The tests were written first (`tests/unit/test_compaction.py`, two in `test_projection.py`); the
  scripts of three of them were set up wrongly (not a problem of the implementation), and after correcting them,
  line by line breaking was done: of 22 breakages 20 turned red, and the remaining 2 (a turn that is already
  covered no longer counts, tool definitions count in the estimate) turned red after tests were added.
  - Real model: **did not run**. I meant to use the local Ollama to run a multi-turn session and verify that the
    summary keeps the earlier facts, but by then Ollama was no longer running (no process on the port) and every call
    was `CallFailed`; as a rule I did not start it myself. The real run of S3a did not involve compaction.
  - Rule → test: mask rather than rewrite
    `test_compaction_writes_one_event_and_the_projection_shows_the_summary_instead`,
    `test_every_covered_event_is_still_in_the_log`, `test_the_summary_text_in_the_event_is_what_the_model_is_shown`;
    a later one takes in the earlier one `test_a_later_compaction_takes_in_the_earlier_summary`,
    `test_only_the_latest_compaction_counts`; cut point `test_a_turn_and_its_tool_results_are_never_split`,
    `test_nothing_is_cut_while_there_are_no_more_turns_than_it_keeps`,
    `test_turns_a_summary_already_covers_do_not_count_towards_the_next_cut`; threshold
    `test_compaction_waits_until_the_estimate_passes_the_roles_threshold`,
    `test_the_threshold_is_the_role_being_called`, `test_every_role_the_agent_may_call_needs_a_threshold`; budget
    `test_a_compaction_call_counts_tokens_and_cost_but_not_a_turn`,
    `test_the_token_budget_includes_what_compaction_spent`; failure
    `test_a_failed_compaction_fails_the_run_and_does_not_switch_models`; `Opaque`
    `test_a_compaction_shows_its_summary_in_place_of_what_it_covers`; fork
    `test_a_parents_later_compaction_does_not_change_a_fork`,
    `test_a_fork_can_compact_across_the_point_it_forked_at`.
  - `/simplify` (one pass, four angles): accepted: a failed model call is accounted for in one place
    (`_Run.failure()`, shared by the main call and the summary call); the textualisation of tool arguments is
    written once (`render` used to write arguments that never parsed successfully as the literal `None`); "which
    compaction counts" is defined only once (`projection.latest_compaction()`); the budget is checked once more
    after the summary (regression test red first); tests no longer reach in to change the private attributes of the
    lite-model fake. Left for S5: the four methods of `Compactor` are in fact two things (when to cut, how to ask),
    to be decided when there is a second implementation; the lower bound of `covers` is now always the same seq, and
    chained ranges will be considered when there is a measurement; the estimate can be calibrated with the actual
    `usage.input`.
  - Codex read-only review: 1 High, 3 Medium, 1 Low. Fixed (regression test red first): a summary cut off by the
    output limit was used as if it were complete; `render` put all the text of a turn before the tool calls and
    scrambled the order. Rewritten: the S3 acceptance item "swap a compactor and re-project" (see the table in §15);
    an always-true assertion was replaced with a real check. Rejected: "when the summary call succeeds but the
    `compaction` event is not written, the cost is not in the log" (High): true, but it is the same already written
    trade-off as for an ordinary turn, "the reply was obtained but `append` failed" (§4: the reply is lost, the
    money is already spent, and the log does not have it); to fix it, an "intent" event would have to be written
    before every model call, which is a change of another order of magnitude, and is noted in §16.
  - Safety check after the S3b merge (automatic): the history handed to the summary model is plain text, a forged
    `[user]` inside a tool result might be written into the summary as the user's words, and the summary appears as
    a user message, so tool content would be promoted to a user request. This held and has been fixed
    (`fix/compaction-trust`): each segment has a title with a random token, the summary model's instructions say
    whose words count as a request, and the summary preamble says "not a new request". The regression tests were
    red first (`test_a_tool_result_cannot_imitate_a_user_turn_in_the_transcript` and 4 others); all 5 line by line
    breakages turned red.
  - Real model (the local Ollama's qwen3:8b, 2026-11-21): first say "my server is called heron-7, port 8431", then
    ask three unrelated questions so that the history is compacted twice, and finally ask for the server name and
    port. Correct = the answer has both.
    | Condition | Version | Correct | The last summary keeps the facts | The summary was written as "carry on chatting" |
    | --- | --- | --- | --- | --- |
    | Thinking off | Before the fix | 0/6 | 0/6 | 6/6 |
    | Thinking off | Instruction repeated at the end (the code in the library) | 5/6 | 6/6 | 0/6 |
    | Thinking off | End instruction + emphasis on keeping facts | 5/6 | 5/6 | 0/6 |
    | Thinking on | Before the fix | 1/3 | 1/3 | 0/3 |
    | Thinking on | Instruction repeated at the end | 1/3 | 2/3 | 0/3 |
    Conclusion: the failure the fix targets (the summary written as a chat) disappears completely with thinking off;
    with thinking on it did not occur anyway, and the fix does no harm. "Emphasis on keeping facts" gave no extra
    help and was not accepted. With thinking on, qwen3:8b still answers only 1/3 correctly: in part the "summary of
    a summary" lost the facts, and in part the summary kept the facts but the main model did not use them; a sample
    of 3 cannot say more, and it is left to S5's eval. After the fix, the summary copied back the title line 6/6
    times, so it was changed to be removed in the library (`fix/compaction-prompt`). Codex read-only review: 2
    findings, both accepted: the title's case, extra spaces, `\r` and indentation all have to be recognised
    (regression test red first); the empty-summary test was given usage and no-switch assertions (confirmed it
    fails by breaking the usage record).
- **S4a (2026-11-21)**: `PgSessionStore` (extra `pg`, `psycopg[binary,pool]==3.3.8`, the same as the sibling
  modules), migration `0001_initial.sql` (`sessions` carries the fork lineage, `events` is append-only), the
  migration runner (vendored from lite-cache `db.py` @ 6afa032), `config.Settings`, `SessionStore.read(after_seq=…)`,
  the invariant check reading only the tail (§4). The tests were written first; one set of contract tests runs both
  stores (`tests/contract/test_store_contract.py`), and the key paths of the loop (tool, pause, resume, compaction,
  fork, budget, the store losing an event) also run once on each of the two stores (`test_loop_on_stores.py`). The
  database and role `lite_agent` were created on this machine, with the password only in the gitignored `.env`; CI
  gained a PostgreSQL service. Without `LITEAGENT_TEST_DATABASE_URL` the PG tests error out and are not skipped.
  - Differences from the plan: an empty batch in the in-memory store used to create an empty session out of
    nothing, and was changed to write nothing (the two stores are consistent, covered by a contract test); the
    socket guard lets through tests that requested the test database, and other tests still cannot use the network.
  - Rule → test: atomic batch `test_a_batch_with_one_bad_event_is_refused_whole[postgres]`; concurrency
    `test_two_writers_at_the_same_tail_one_wins_one_is_refused`, `test_two_writers_creating_one_session_one_wins`;
    append-only `test_the_database_refuses_to_rewrite_events`; fork
    `test_a_fork_reads_the_parents_prefix_and_then_its_own_events`, `test_a_fork_of_a_fork_reads_along_the_chain`,
    `test_a_fork_of_a_fork_taken_below_the_first_fork_point`; validation on read-back
    `test_an_event_this_code_cannot_vouch_for_is_refused_when_read`; tail check
    `test_before_each_request_only_what_was_written_since_the_last_check_is_read`,
    `test_a_store_that_changed_an_event_since_it_was_written_stops_the_run` (both would fail under the old full
    check).
  - Close-out `/simplify`: accepted: the schema name is validated with `TypeAdapter(OwnSchema)` (no longer building
    a `Settings` that reads `.env` for this); the SQL is assembled once at construction; locking the row and getting
    the tail combined into one query; `read` skips ancestors whose whole segment is before `after_seq`;
    `connection_error` accepts any exception; the unused `kind` fixture deleted. It also found that
    `read(upto_seq=0)` reads everything in the PG store (a truthiness test); the contract test
    `test_reading_up_to_seq_zero_reads_nothing` was written first and seen red before the fix. Noted and not done:
    one recursive CTE reading the whole lineage, using an index in `projection` instead of a filter (left to S5's
    measurement); `liteagent migrate` in the error message was changed to `lite_agent.pg.migrate`, and changed back
    when S4b gave a CLI.
  - **What S4b has to settle first (raised by simplify)**: inherited events keep the `session_id` of the original
    session, and the new id of `fork()` is generated by the store; so `copy_session` can only flatten a fork into an
    independent session (events get their `session_id` rewritten), and cannot rebuild the lineage in the target
    store. This is written in S4b's plan.
  - Close-out Codex (read-only): accepted: after `close()` every call reports `StoreError` and does not reopen
    (`test_a_closed_store_says_so_and_does_not_reopen`); the whole process of opening the connection pool and
    probing the table gives only redacted errors; `redact_conninfo` hides all `*password*` parameters (`sslpassword`
    used to be shown as is, `test_every_secret_in_a_connection_string_is_hidden`); the test for an unmigrated schema
    closes the pool in `finally`; the contract for an empty batch gained the cases of `expected_seq` non-zero and
    stale (both stores were already right; temporarily removing the in-memory store's check turned it red).
    Rejected:
    - "`migrate` trusts the `schema_migrations` already in the schema": the schema is lite-agent's own
      (`own_schema` rejects shared and system schemas), and another tool creating a table of the same name in it is
      a configuration error; opening the store also checks whether the `events` table is there.
    - "`pending()` does not report an unknown version": consistent with the vendored originals in lite-cache and
      lite-rag, and changing it means changing three places together; noted in §16.
    - "`read` has no consistent snapshot across segments": the ancestor segments are all capped at the fork point,
      events are append-only, and those segments do not change again; only the last segment (its own) grows, and a
      concurrent write only makes the result contain one more complete batch (batches are atomic), so what is
      returned is always a complete prefix at some moment.
    - "the socket guard lets through too much": what it lets through is only the tests that requested the test
      database fixture, and the URL comes only from the explicitly set `LITEAGENT_TEST_DATABASE_URL`; this is test
      code, not a production path.
- **S4b (2026-11-21)**: `copy_session`, the `liteagent` CLI (`migrate`, `sessions`, `show [--json]`, `fork --at`,
  `config`), `PgSessionStore.sessions()` and `from_settings()`. The tests were written first.
  - **`copy_session` flattens a fork** (raised by S4a's simplify, consistent with the plan): inherited events are all
    changed to the new session's id in the copy, and the lineage does not travel with the copy; it is one batch
    write, and if the id already exists in the target, `SeqConflict`.
  - **The CLI has no `run` / `resume`** (different from the original list in §14): a run needs the host's tools,
    gates and completion condition, which are all Python objects that the command line cannot supply; a run without
    tools is of little use and would be mistaken for an entry point.
  - Rule → test: process A pauses, process B approves and finishes
    `test_a_run_paused_in_one_process_is_approved_and_finished_in_another`; a process dies inside a tool, and the next
    process does not re-run and requires checking
    `test_a_process_that_died_inside_a_tool_leaves_a_call_the_next_one_reconciles` (if a non-idempotent call is let
    re-run, it fails); one process forks and another carries on
    `test_a_session_forked_in_one_process_runs_on_in_another` (if the store does not read inherited events, it
    fails). Every step is a new Python process (`tests/contract/agent_process.py`), sharing only the database. A
    session of the in-memory store, after being moved into PG, is resumed with another `Agent`
    `test_a_paused_session_copied_into_another_store_resumes_there`; in a fork of a fork, one compaction crosses both
    fork points and all three layers of projection are right `test_a_fork_of_a_fork_compacts_across_both_fork_points`
    (once on each of the two stores; S3b tested only one layer); one test for each CLI command, with the output
    asserted line by line (`tests/contract/test_cli.py`).
  - Close-out `/simplify`: accepted: `sessions()` first applies `LIMIT` and then fetches the tail and the last
    outcome by primary key row by row (it used to `GROUP BY` the whole `events` table first); the store in the CLI
    got a type (it was `Any`); the "no database configured" message is kept in one copy (`config.NO_DATABASE`);
    `Ask` and "open a store, read it, close it" in the tests lifted into `tests/helpers.py`. Noted and not done:
    giving `migrate` an entry by URL (adds a public interface, wait until needed); an index on
    `sessions.created_at` (wait until there are enough sessions to need it).
  - Close-out Codex (read-only): accepted: `copy_session(new_session_id="")` raises an error and no longer quietly
    changes to a new id; `sessions(limit)` smaller than 1 raises an error; a settings error in the CLI is one line
    without the value (`LITEAGENT_SCHEMA=public` used to be a traceback); any psycopg error in `migrate` gives only
    redacted information (it used to handle only `OperationalError`); `show` writes control characters as escapes
    (stored text must not be able to drive the terminal), truncated lines end with `…`, and `--json` additionally
    writes DEL and C1 as `\u` escapes (JSON itself escapes only C0); the description of `copy_session` changed to
    be equivalent "under the same agent", and states that it is a snapshot of the moment of reading. All seven had a
    test written first and seen red. Rejected or only noted:
    - "copying a session that is still running gets a stale copy": the copy is a complete prefix of the moment of
      reading (append-only, batches atomic), a snapshot like a fork; "nothing is written when the source is empty"
      cannot happen, since a session that exists has at least `session_started`.
    - "a very long session read into memory at once, written as one batch": noted in §16; writing in batches while
      still being all or nothing needs a transaction interface added to the store.
    - "`config` shows paths like `sslkey`": a path is not a credential; every `*password*` parameter is already
      hidden.
  - Automatic safety review after the merge (accepted): `show` escaped control characters, but the rows of `sessions`
    and every error message (which quotes the session id, and the id is decided by the host) did not; now every
    host-given string the CLI prints goes through the same escape (`test_ids_and_errors_are_escaped_too`, seen red
    first).
- **S5a (2026-11-21)**: the eval framework. The task set is synthetic (chosen by the owner; "the first real task" in
  §16 is still open): a deterministic small shop (`src/lite_agent/evals/world.py`: customers, orders, stock,
  refunds, notes, 6 tools), 12 tasks (7 English, 5 Chinese; lookup, chaining, change state only once, correct
  after an error, a case where no tool should be called, a skill that has to be read, a long task plus compaction,
  completion conditions), and the completion of each task is judged by code reading the shop and the event log
  (`evals/tasks.py`). The metrics are computed from the log: completed or not and the reason, turns, tool calls
  (including refused ones), invalid calls, number of automatic continuations, whether it compacted, tokens, elapsed
  time. The lite-eval target is `lite_agent.eval_target:agent` (extra `eval`, lite-eval 0.4.1, the same wheel as
  lite-mem), with the suite `agent-fake` (a Fake replays the correct path of each task, 12/12) and `agent-ollama`
  (for S5b).
  - Rule → test: the correct path of each task is judged completed and the metrics are deterministic numbers, and the
    wrong path is judged not completed and says the reason (`tests/unit/test_eval_tasks.py`; make the checks always
    pass and 11 wrong paths fail, and the 12th, done-en, fails on the run's outcome); the dataset is consistent with
    `TASKS` and two results on the Fake are the same (`tests/integration/test_eval_target.py`); the `-m ollama`
    smoke test runs only chain-en (qwen3:8b, 13 seconds, passed).
  - Differences from the plan: `tool_calls` counts the calls the model asked for (refused ones too), not the ones
    executed; the outcome of a wrong path where "the completion condition was never met" is `failed (incomplete)`
    (this is how the harness ends after the automatic continuations are used up), not a reason from the check.
  - Close-out `/simplify`: accepted: the twelve tasks switched to keyword arguments and `kind` is a `Literal`; the
    tools' parameter models moved to module level; `Metrics.as_dict` uses `dataclasses.asdict`; `stop` taken directly
    from `RunResult.stop`; the token metric counts every billed call (summary and failed calls too, consistent with
    the budget; it used to count only `model_turn`, and `test_tokens_count_every_model_call_as_the_budget_does` was
    red first); `TASKS_VERSION`, which was changed by hand with a date, replaced by a content hash. Noted and not
    done: `say` / `use` and `tests/helpers.py` each keep a copy (src cannot import tests).
  - Close-out Codex (read-only): accepted:
    - Answers are matched by "value", no longer by substring: `says()` requires the whole token (30 is not in 300,
      D02 is not in D021, digits and Latin letters set the boundary), not inside a decimal, and no negation in the
      same clause (not / n't / 没有 / 未) (`test_an_answer_says_a_value_only_as_a_whole_and_not_negated`, 14 cases
      red first); recover-en accepts a contrastive statement such as "没送达，已发货" (not delivered, shipped).
    - done-en's completion condition and its check use the same postcondition (it used to end as soon as "there is
      any note", so a wrongly written note had no chance to be corrected; `test_done_en_keeps_going_until_the_note_is_right`).
    - New metric `unknown_usage`: the number of calls for which the endpoint did not report tokens, to tell "0 tokens"
      from "unknown".
    - Cache identity: the `code` in `versions()` is a hash of the whole `lite_agent` package (`.py`, `.md`, `.sql`),
      so a change in the loop code in the workspace also invalidates the cache (it used to be only the package's
      version number); `test_the_code_version_moves_when_any_of_lite_agents_code_changes` (it fails when `.py` is
      left out).
    Rejected:
    - "long-en measures the compaction threshold": the threshold (150 estimated tokens) is so low that 8 tool results
      alone exceed it, and any agent that works seriously will trigger compaction; what this task is meant to test
      is "whether the rules at the start are still there after compaction", and without compaction it was not
      tested, so judging it not completed is right.
    - "invalid calls do not include `denied`, `truncated_turn`": this metric counts the model's own wrong calls (a
      tool that does not exist, invalid arguments); a gate refusal and a truncated turn are not wrongly written
      calls, and there is no gate in the tasks; the name and the comments already say so.
    - "`setup(None)` in the tests": the Fake does not read the dataset; Ollama does not either, and this is the
      target's actual behaviour.
  - `says()` is logic newly added in Codex's fixes, so by the rule one more Codex review round was done aimed only at
    it: negation is recognised only when it is right next to the value (only yet / been / be allowed in between),
    "not only shipped" and "isn't it shipped?" no longer count as negation; "不是已发货吗" (isn't it shipped?) is
    recognised. 4 cases red first.
- **S5b (2026-11-21)**: the Ollama baseline, two rounds (the eval card is in KB `global/llm-evals/`). 3 runs per task,
  one task at a time. lite-model's default temperature for Ollama is 0 (greedy), and the 3 results were the same; so
  the second round added a sampling configuration with temperature 0.6 to see the variance.
  - **Round 1** (greedy): qwen3:8b 8/12, qwen3:1.7b 5/12. After reading the event log of every failure, two defects
    were found in the eval tooling, not problems of the models: in long-en both models looked up all 8 orders in
    parallel in one turn, so there was nothing to compact and compaction was not tested; the example id in the tool
    parameter description ("such as C1") is a real customer in the shop, and both models guessed Bo to be C1. The
    fix: long-en was changed into a chain of orders (each order names the next,
    `test_the_long_tasks_orders_can_only_be_followed_in_turn`); the example was changed to an id that is not in the
    shop (`test_no_tool_example_names_something_in_the_shop`). Both were red first.
  - **Round 2**:

    | Configuration | Completion rate | Variance (lowest / highest of 3 runs) |
    | --- | --- | --- |
    | qwen3:8b greedy | 0.750 | 0.750 / 0.750 |
    | qwen3:8b temperature 0.6 | 0.722 | 0.667 / 0.750 |
    | qwen3:8b thinking | 0.889 | 0.833 / 0.917 |
    | qwen3:1.7b greedy | 0.417 | 0.417 / 0.417 |
    | qwen3:1.7b temperature 0.6 | 0.417 | 0.417 / 0.417 |

    The gap between 8b greedy and sampling is within the variance, so it is noise; thinking is above the variance, but
    each task is about 10 times slower (median 22.6 seconds vs 2.4 seconds).
  - **Classification of failures** (after reading the event logs):
    - Guessing without looking up: in act-en "full refund", both models refunded 100 directly without first looking
      up the order amount (thinking got it right 3/3).
    - Not correcting errors: in recover-en, after looking up "17" and getting "no order 17", they gave up or repeated
      it as is, and did not think of finding the customer's orders by email (all five configurations 0/3).
    - A small model does not call tools: 1.7b's chain task said "cannot find it" directly, or described in words "I
      will call find_customer" without calling it; skill-zh said a full refund was possible without reading the
      refund policy.
    - Errors in the summary after compaction: long-en now really compacts (9 turns, 6 summaries), and the
      configurations with thinking off got 0/3. Replaying once showed factual errors in the summary ("F03 was not
      looked up", "F03 (140) is below 100", F02's amount written wrongly), and the final answer was right only by
      chance thanks to later turns. This connects to the summary quality problem noted in S3b; changing the summary
      prompt requires running this task set once before and once after, per rule 13.
    - The C1 error of done-en in round 1 disappeared in round 2 (8b 3/3), which shows it was indeed caused by the
      example id.
  - Noted: greedy does not guarantee reproducibility on Ollama: in the eval long-en was wrong all three times, while
    the same configuration replayed alone once did it right.
  - Provenance: lite-eval's run directory is at `eval/lite-eval/runs/agent-ollama/` on this machine (gitignored, as
    in lite-mem), run ids: round 1 `20261121T043144397423Z-qwen3-1.7b-c09861`, `20261121T043223892153Z-qwen3-8b-3fbf1e`; round 2 `20261121T044011332103Z-qwen3-1.7b-62d6d6`, `20261121T044144876458Z-qwen3-1.7b-t0.6-23eacc`, `20261121T044301422408Z-qwen3-8b-07e8fc`, `20261121T044937768209Z-qwen3-8b-t0.6-d3384a`, `20261121T045636952523Z-qwen3-8b-think-e1f855`.
    The per-task completion counts, reasons and elapsed times were copied into the KB eval card. The failure
    classification comes from replays (`run_task` plus a script that prints events), and is not in the run
    directory: the target records only the event types and not the content.
  - Close-out `/simplify`: accepted: a comment in the benchmark script and the value of `original_chars`. Noted and
    not changed: the order chain of long-en has a shortcut: F01–F08 all belong to customer C5, so the model can get
    all of them with one `list_orders C5`, and then there is again no need to compact; in the replay qwen3:8b looked
    them up one by one; the eval's runs recorded only the number of calls and not which tool was called, so it
    cannot be found out whether other runs took the shortcut. Changing it would make the baseline not comparable, so
    it is left to the next time the task set is changed (give each F order a different customer).
  - Close-out Codex (read-only): accepted: state the provenance above. Rejected: "the suite's `timeout_s: 600` was
    not passed to lite-model": correct, but the baseline is deliberately run with the default the host gets (120
    seconds), and thinking hitting it is itself a finding to record (see §16).
- **S5.5 (2026-11-21)**: v0.1.0. The version number, README gains the install method, and the event vocabulary V=1
  is frozen from here on (§4). `tests/unit/test_readme.py` runs the README example once as is, replacing only
  `build_async` with a scripted fake (if a method name in the README is broken, it fails). One Codex read-only
  review finding, accepted: the freeze used to be only text, and `test_vocabulary.py` was added to guard it (add a
  field to `model_switched` without raising V, and it fails).
  - Release: tag `v0.1.0` (`4fa94d5`), CI run 44876227588 with test / package (the tag matches the wheel version) /
    release all succeeded, and the Release carries the wheel and sdist. Post-release acceptance: a brand new project
    installed from the Release URL (with lite-model 0.4.0), and the README example ran as is on the local qwen3:8b
    to `completed`, with 8 events consistent with the README. Release: `https://github.com/ArthurGao/lite-agent/releases/download/v0.1.0/lite_agent-0.1.0-py3-none-any.whl`.
- **S6 (2026-11-21)**: the HTTP shell (§10), the agent registry `lite_agent.registry`, and §3's `run_id` /
  `new_session`. The tests were written first.
  - Rule → test: the same `run_id` does not open a second run: when it has ended it is rebuilt from the log, when it
    died midway it carries on in the same run, when it died inside a non-idempotent tool it requires checking, and a
    resume called twice executes only once (`tests/contract/test_run_id.py`, both stores; turn off the branch "the
    log already has this run" and 8 of the 13 fail). HTTP: start → follow the SSE to the end, a stale `expected_seq`
    gets 409, a call that is not waiting gets 422, a second run of the same session gets 409, fork, an unknown agent
    / session gets 404, a request with an extra field gets 422, binds only to loopback, the various errors of
    `agents.toml` (`tests/contract/test_server.py`, both stores; turning off the tail check, the single-run check
    and the loopback check separately makes the corresponding tests fail).
  - Differences from the plan: SSE is a separate route `/events/stream` and does not switch on `Accept` (FastAPI's
    SSE route is a generator and cannot also return JSON); a session started by the server can be read before the
    first batch of events is written (on PG reading first would give 404, a race the tests caught); `drain` tracks
    every run that has started, not only each session's current run (with the single-run check turned off, the old
    way made the PG test hang when closing the store).
  - Close-out `/simplify`: accepted: the answer text rebuilt from the log is put together the same way as on the
    spot (`turn_of(...).text`); usage is accumulated only on the continuation path; the log is scanned only when a
    `run_id` is given; the first read of the stream is used directly as the first batch; `server.py` uses concrete
    types; `/agents` uses the same `ToolDef` conversion as `run_started`; the redundant `started` set removed. Noted
    and not done: making `_check_resume` public (it adds an export, and nobody outside the shell uses it); putting
    "one run per session" in the library (the cross-process guarantee is already the store's `expected_seq`).
  - Close-out Codex (read-only), five findings accepted, each with a test written first and seen red: a reused
    `run_id` (not the last run, or a different task) raises `ValueError` and no longer replays the old result; two
    concurrent `new_session` racing for the same id, and the loser gets `SeqConflict`; of two run / resume requests
    that arrive at the same time for the same session only one gets 202 (there is no await between the check and the
    claim); a factory that raises and a database that is not configured are both `RegistryError`; `localhost` is
    also refused. One more test was added: a resume that died halfway, and the retry only fills in the calls that
    have no result. One rejected: "the error body may contain upstream text": these responses are all text the shell
    wrote itself, and exceptions of background runs go only to the log.
  - The fixes changed most of the shell, so by the rule one more Codex round was done aimed only at the fixes: one
    finding, accepted: when `create_task` itself fails, the claimed session is never released by anyone
    (`test_a_run_that_could_not_be_started_releases_its_session`, it fails when the release is removed).
  - Automatic safety review before the merge (accepted): a loopback service without authentication can be called by
    DNS rebinding or by a page in the browser (including approving a paused run). `Host` / `Origin` checks were added
    (`test_a_request_from_a_web_page_is_refused`, 4 cases fail when this check is not registered). Along the way a
    test that depended on timing was fixed: the one-run-per-session test used to assume the first run had not
    finished, and now the first run's model replies only after the test lets it.
  - Second safety review after the push (accepted): letting any loopback `Origin` through was too wide, and a page of
    another development server on this machine could also call it; changed so it must be exactly the shell's own
    origin, and POST accepts only JSON (`test_another_origin_on_this_machine_is_refused`,
    `test_a_post_that_is_not_json_is_refused`, red first). No bearer token was done: it will be designed together
    with authentication when it is used externally or by several users.
  - CI was red the first time: a few `agents.toml` tests did not write `[store]`, the default is postgres, and it
    reads the environment variables and `.env`: it passed on this machine because it has a `.env`, and CI does not.
    These tests exercise the factory, and now write `kind = "memory"` explicitly; the whole test set was also run
    once in a directory outside the one with `.env` (the PG tests error by design, and the rest pass).
  - Acceptance on this machine: `liteagent serve` (loopback 8730) ran once with the local qwen3:8b, started with
    curl, followed by SSE to `run_finished completed`, `/openapi.json` 200; shut it down after it finished.
- **S7a (2026-11-21)**: TemporalRunner (§9). The tests run on the time-skipping test server (marker `temporal`, run by
  default, CI runs them too): run to the end; pause → signal approve → complete; a host tool routed to an activity
  on another task queue; the activity fails after the run is done and before Temporal records it → the retry does
  not open a second run and the tool runs only once; the worker dies inside a non-idempotent tool → on retry it
  requires checking, and completes after a signal that carries the result; Continue-As-New carries the paused run
  over; the recorded history replays without error on a `Replayer` (`tests/temporal/test_runner.py`). Turn off the
  recognition of "the log already has this run" and the two retry tests fail.
  - A bug the tests caught: a stretch that starts from a carried-over pause immediately did Continue-As-New again,
    continuing endlessly (the test server reported an http2 error); fixed so that it continues only after this
    stretch has executed a run.
  - A pitfall in the tests: sending only a query does not advance the time-skipping server's clock, and the backoff
    of the activity retry just keeps waiting; use `env.sleep` when waiting for a pause.
  - A second pitfall (2026-11-27, an occasional failure traced down): the time-skipping test server drops a query that
    arrives at the moment of Continue-As-New: the worker reports "workflow execution already completed", and the
    client gets `Timeout expired` after about 30 s. When queries were sent back to back it failed 20 times out of
    20, and 0 times on the Temporal CLI's dev server (Server 1.32.0), so `TemporalRunner.status` itself is fine. The
    Continue-As-New test first uses `describe` to wait for the new run and then queries (`_continued`); with
    `_paused` changed to send back to back without sleeping, it fails without it and passes with it.
  - Close-out `/simplify`: accepted: Continue-As-New copies the input with `dataclasses.replace` (fields added later
    are not dropped); the list of caller errors is written in one place; `step_of` uses a concrete type; the
    description in `runner.py` corrected (Temporal is not an implementation of the `Runner` protocol); `Worker` in
    the tests imported once. Noted and not done: merging `ToolResultData` with `ToolOutcome`, and `Waiting` carrying
    the tool name and arguments (both change the interface of §3); `start()` exposing the timeouts, and moving
    `RUN` / `PERMANENT` into `types.py` (so that the workflow module no longer imports the whole agent).
  - Close-out Codex (read-only): four accepted, each with a test written first and seen red: an old decision would
    answer a later pause (`test_a_decision_for_an_answered_pause_never_reaches_a_later_one`; the workflow used to
    fail because the resume was refused); a person's result would overwrite a host tool's result (the same fix: only
    the calls this pause is still waiting for are accepted); after the heartbeat task is cancelled, wait for it to
    finish; `liteagent worker` gives an installation hint when an extra is missing. A test was also added: a host
    tool fails permanently → the model gets an error result carrying only the type name, and the run continues (it
    fails when the exception is raised as is). After the fixes, a second Codex round aimed only at the workflow:
    when the same pause is answered with two signals, the second answer that arrives during the resume would be
    cleared; changed so that it is kept only for the same batch of calls that are still waiting (`still_relevant`,
    the unit test `tests/unit/test_temporal_decisions.py` red first).
- **S7b (2026-11-21)**: inbox and timer (§9.1). The tests written first are for `PgInbox`
  (`tests/contract/test_inbox.py`: de-duplication, order, completion, a decision must carry a session, a follow-up
  fires only once, a schedule fires every period and downtime is made up only once); on Temporal (PostgreSQL +
  time-skipping, `tests/temporal/test_inbox.py`): a duplicate delivery is handled only once, two quick messages are
  merged into one run, a reply carries on the original session, after an approval pause a decision event lets it
  complete, a decision that cannot be taken is marked refused while the worker carries on, a follow-up and a
  schedule enqueue when due and are handled (time fast-forwarded by two hours), and after a crash of the run
  activity the retry has only one run. These passed the first time they ran, so each was seen red by mutation:
  remove the merging, change refused to raising, do not close a due follow-up (this one the Temporal test does not
  catch, since the de-duplication by the source id of the same firing covers it, and the `PgInbox` test catches it),
  and the corresponding tests fail.
  - Close-out `/simplify`: accepted: `PgSessionStore._connection()` opens the connection pool in one place, shared by
    the store and the inbox (they used to each write one); the period arithmetic uses timedelta integer division (no
    floating point); the imports in the activity moved to the top of the module. Noted and not done: gathering the
    inbox-to-agent mapping into one place (adds an interface), `WorkItem.kind` using `Literal`.
  - Close-out Codex (read-only), both accepted, each with a test written first and seen red: when the retries of an
    item are used up it was marked refused, so with a longer model or database outage the work would be lost for
    good (`test_an_outage_longer_than_the_retries_leaves_the_work_pending_not_refused`, it fails when changed back to
    refused); a handler that wrote the row and died before waking the worker leaves an event nobody handles
    (`test_an_event_nobody_woke_for_is_found_by_the_scheduler`, it fails when the recovery is removed). Simplify's D3
    was the same problem as the first. After the fixes, a second Codex round aimed only at the fixes, both rejected:
    - "during a retry new events would be merged into the same batch and the old run_id would become invalid": when
      carrying an item to retry, the workflow does not call `TAKE` and only retries that item, and takes new ones
      only after it succeeds; an inbox has only one worker, and no other run enters this session in the meantime.
    - "a permanent error that appears only after retries is treated as transient": the `ActivityError` Temporal
      finally gives has the same shape whichever attempt it is (the cause is the `ApplicationError` the activity
      raised, and `type` is the error class name);
      `test_an_event_the_agent_cannot_take_is_refused_and_the_worker_goes_on` proves that the classification holds.
  - Automatic safety review after the merge (accepted): "work is never lost" also means that a message that fails every
    time (a poison message) would make the inbox retry it forever and block all the events behind it. Changed so
    that an item that has been failing for more than `max_retry_s` (default one day) since its first failure is
    marked `failed` and left for a person to look at, and the inbox goes on; the `failed` status was added by
    migration `0003` (`0002` is already in use and is not changed).
    `test_a_message_that_always_fails_is_set_aside_and_the_inbox_goes_on`; when the limit is removed the inbox is
    never empty.
  - A pitfall in the tests: on the same time-skipping server, the inbox workflow left by an earlier test has an idle
    timer but no worker any more, so time cannot jump forward; at the end of each test the inbox workflow it started
    is terminated.
  - The expected output of `test_migrate_applies_then_says_up_to_date` was changed to two migrations
    (`0001_initial, 0002_inbox`): this is the expected change in behaviour brought by the new migration, and the
    test was not changed just to make it pass.
- **v0.2.0 (2026-11-21)**: the release of S6 and S7. Additions to the public interface: `run_id` and `new_session`
  of `Agent.run` / `resume`, with `Runner.run` in step; extras `server`, `temporal`; migrations `0002`, `0003`
  (inbox). The event vocabulary did not change (the V=1 fingerprint of `tests/unit/test_vocabulary.py` is
  unchanged). Release: tag `v0.2.0` (`b8b2d41`), CI run 44885745742 with test / package / release all succeeded.
  Post-release acceptance: a brand new project installed all extras (`pg`, `server`, `temporal`, with lite-model
  0.4.0) from the Release URL, the new modules all import, and the README example ran to `completed` on the local
  qwen3:8b. Release: `https://github.com/ArthurGao/lite-agent/releases/download/v0.2.0/lite_agent-0.2.0-py3-none-any.whl`.
- **Revision after P0 (2026-11-21)**: changed according to lite-model 0.4.0's actual interface and the Claude API's
  preserved thinking rule: §4 (`model_turn` stores ordered blocks, `ToolResults` grouping, the conditions for an
  `Opaque` to enter the projection), §5 rules 4 and 11, §6, §13, §15, §16.

- **lite-context C7 (2026-11-26, v0.2.5)**: the compactor's prompt, rendering and summary frame now use lite-context
  0.3.1 (decided by the owner: lite-context is a core dependency and only compaction changes; rule 8, §6 and §13
  were changed in step). `SUMMARY` / `ASK` / `END` are re-exported from `lite_context.compaction`, and are verbatim
  the same on both sides; `render()` = `render_transcript(mark, neutral.to_neutral(history))` (lite-context added the
  public `render_transcript` for this, without the trailing `END`; the tests here count header lines by
  `--- <mark> `); `frame()` wraps the reply with `SUMMARY`, and the rule of removing headers stays here (`clean`:
  remove any hexadecimal header of 16 or more characters, whereas lite-context removes only this time's); the loop
  uses it when writing the `compaction` event. There is no `SummaryPolicy` subclass of our own: `plan_compaction` /
  `accept` are not called here, and a subclass only to override `wrap` is superfluous (simplify). The cut point (by
  `model_turn`, on event seq), the event and the projection are unchanged: lite-context's `plan_compaction` counts
  by human turns and does not apply; the "own way of counting turns" that C7 asked for is just keeping `cut`. The
  estimate of 3.5 characters / token is taken from lite-context's `CharEstimate`, and the algorithm is unchanged.
  Acceptance: before the change, sha256 was recorded for 38 existing test files and afterwards each was identical,
  and 503 tests passed without a line changed; a new `tests/unit/test_compaction_bytes.py`: v0.2.4's own code first
  generated, as the gold standard, the rendering results of 10 kinds of history (parallel calls, unparsed arguments,
  error results, a turn with only thinking, an earlier summary already present, forged headers, empty text,
  Unicode) and the frame results of 5 kinds of reply, and after the change they are byte-for-byte identical;
  `tests/unit/test_neutral.py` covers the mapping. A turn with only thinking is mapped to an empty text part (v0.2.4
  renders an empty assistant segment for it; lite-context renders nothing for an empty message). All 6 mutations
  were caught. Rule 13: the fake eval is the same before and after (all 12 tasks 1.000, 13 calls); the bytes sent
  have been shown to be identical, so the Ollama eval need not be re-run.
  Close-out: `/simplify` (accepted: `frame()` in place of a superfluous policy subclass, `_new_mark` made private,
  a note that `neutral` is not a general adapter) + review (the Codex quota is used up, a Claude subagent): hypothesis
  generated 20 thousand histories and 5 thousand replies, and the old implementation (v0.2.4 code taken out with
  `git show`) and the new one are byte-for-byte identical; the only difference is on the public `render()` when a
  system message is passed in: v0.2.4 rendered a system segment, and now it is a `ValueError`; the loop never passes
  one (`projection.history` does not contain system), and this is written into the docs and the tests.

- **v0.3.0 (2026-11-26, event vocabulary V=2)**: masking (§6). lite-context's `fit()` + `MaskToolResults` decide
  before every model call, before compaction, which old tool results to replace with placeholder text; the new
  event `tool_results_masked` records each result and the text shown, and the projection replaces accordingly and
  adds no text itself. The `v` of each event is the version its type belongs to: a session that has not turned on
  masking is all `v=1`, and v0.2.5 still reads it (§4).
  Acceptance: before the change, sha256 was recorded for 40 existing test files, and after the change only two
  differ, both as the vocabulary rules require: `test_vocabulary.py` gained the V=2 fingerprint (V=1 untouched), and
  the "one example per event type" in `test_events.py` gained an example of the new event; a real log written by
  v0.2.5 (`tests/fixtures/v1_session.json`, including compaction) reads back with a verbatim identical projection;
  a run with masking in the PG store reads back with an identical projection; 557+ tests all pass.
  Mutation check: of 9, 4 were not caught at first: "do not mask what was already masked" and "tool definitions count
  toward the threshold" got tests added; "do not use the state after compaction" was redundant (`StaleState` already
  makes it re-measure) and was deleted; "replay the stored cut point" shows no difference on a short run, and the
  review measured a long one: with 50 turns and a threshold of 2000, 3 maskings with state against 12 without
  (the cut point moves almost every time and the prompt cache prefix breaks), and this test was added.
  The close-out review (the Codex quota is used up, a Claude subagent) found and fixed (each with a failing test
  written first): `keep_last=0` let the latest result be masked too; a `v=1` masking event was accepted; a turn whose
  result matches no call made the projection crash (it used to be an empty result); a lite-context error other than
  `DoesNotFit`/`StaleState` made the run fail; writing `v=2` on every event would make v0.2.5 unable to read a
  session that did not turn on masking (changed to write by type).
  Eval (rule 13, Ollama started with the owner's authorisation and stopped after use): qwen3:8b greedy, 12 tasks 3
  times each. Masking off 0.750 (the same as S5b); masking on (900 tokens, keep the latest batch) the first version
  0.667: the skill task 3/3 → 0/3, because the result of `load_skill` (the body of the skill) was masked; so
  `keep_tools` was added (default `load_skill`, `recall`; these results are never masked), and the re-run gave 0.750,
  but this time masking did not fire once (the tasks are too short): it shows "no longer harms", not "has a
  benefit". The benefit on long runs is shown by the scripted tests. KB card
  `global/llm-evals/evals/2026-11-26-lite-agent-masking.md`.
  Unresolved: one Temporal test (`test_continue_as_new_carries_a_paused_run_on`) occasionally has an RPC timeout in
  the full test suite of this branch (about 4 out of 36 times, all early on, while the eval was running at the same
  time), and it does not appear when run alone or without `test_masking.py`; on main it did not appear in 18 runs;
  after adding `lite_context` to the Temporal sandbox's passthrough (which should have been done anyway), 8 out of 8
  passed. The cause was not determined.

- **v0.3.1 (2026-11-26)**: when two writers of `PgSessionStore` are at the same tail, the one waiting for the lock got
  PostgreSQL's `UniqueViolation` instead of `SeqConflict` (present since S4a, unrelated to masking; found with a
  controlled experiment under full CPU load while investigating the occasional Temporal timeout: it appears both
  before and after the masking change). Cause: the lock and the tail read are one statement,
  `SELECT … (SELECT max(seq) …) … FOR UPDATE OF s`: under READ COMMITTED, a statement that has waited for the lock
  only re-checks the locked row and does not re-run the subquery, so what it read was the tail from before the
  other's commit. The fix: first take the lock with a separate `SELECT 1 … FOR UPDATE`, and then read the tail with
  a new statement (a new statement sees the other side's committed write); plus a backstop: hitting `events_pkey`
  on insert also reports `SeqConflict` (it can only be a writer that did not get the lock). The data was never
  corrupted: the primary key stopped the second write, and what was wrong was only the type of the exception; the
  HTTP shell's conflict response and cross-process resume both rely on `SeqConflict`. Tests: `test_pg_store.py` makes
  the second writer wait on the lock (recognised by `pg_blocking_pids`), and after the first commits it asserts that
  it is the `SeqConflict` given by the tail check (changing back to the old statement must fail, and the backstop
  cannot save this assertion); the backstop has a separate test. Under full load the contract tests passed 20 times
  out of 20 (before the fix it reproduced on the first); the review's stress script has 8 writers per round, with a
  few dozen rounds each for a new session, an existing tail and a fork, and exactly one succeeds in each round.
  The occasional Temporal timeout: under full load it did not appear in 12 runs each before and after the masking
  change, so the hypothesis of insufficient CPU does not hold; it is still recorded as unresolved, to be
  investigated when it appears again in CI.

- **Masking on long tasks (2026-11-26, eval code, not released)**: in S5's 12 tasks masking never really fired (a
  result is one line, and the longest has 8 lookups), so a separate synthetic world `evals/archive.py` was made: 36
  waybill reports of about 1,250 characters, each with a status and a gross weight buried in it, and each stating
  the number of the next; three tasks each read a chain of 12: `audit-en` / `audit-zh` (count the delayed tickets and
  total their gross weight) and `compare-en` (after reading, give the gross weight of the **first** and the last
  copy and how much they differ: this tests the risk of masking specifically, since the first through the last have
  long been masked). The dataset is `agent-long`, and the suites are `agent-long-fake.yaml` (CI) /
  `agent-long-ollama.yaml`. The thresholds were calibrated on the fake so that each of the two configurations really
  takes effect: compaction only (a summary is written at 3,000 estimated tokens, exactly once per task) against
  masking first (masks at 2,000, keeping the latest 2 batches; each task masks 9 results and no longer needs a
  summary). These two thresholds are deliberately far lower than qwen3:8b's context on this machine (32,768 given by
  Ollama 0.40.15): they simulate a tight budget, not truncation. `Metrics` gained `rereads` (the same tool called
  again with the same arguments), `masked` (the number of masked results, `projection.masks()`) and `answer` (the
  final answer, kept so it can be re-judged later). `Task` became generic over its world (`Task[World]` /
  `Task[Archive]`, a `typing_extensions` TypeVar default, with `typing-extensions>=4.16.0` stated in the
  dependencies, which is pydantic's own lower bound), and the system prompt is given by the world; the behaviour of
  S5's 12 tasks is unchanged (1.000 on the fake, 13 calls, the original test files byte-for-byte unchanged). The data
  under test (the reports, the system prompt, the three prompts) is pinned in a test with a hash: changing it means a
  new measurement.
  **Two Ollama runs** (started with the owner's authorisation and stopped after finishing; qwen3:8b greedy, thinking
  off, 3 runs per task per configuration). The judging of the first one was wrong (found by the close-out review):
  `compare-en` then asked "which is heavier", and the judge only looked at whether `T01` appeared in the answer, so
  an answer that mentioned both ids counted as right whether it was right or wrong; writings such as `43.6kg` (kg
  immediately after the number) and "eight" counted as wrong; the answers were not stored either, so they could not
  be re-judged. The judging was fixed (`compare-en` now asks for the difference, and all three conditions are
  numbers; weights accept `kg` attached and two decimals, counts accept English words and Chinese numerals),
  `Metrics` gained `answer`, and it was re-run. The first run's numbers (compaction only 3/9, masking first 0/9,
  input tokens, time) are void and are left only in git history. The second one:

  | Configuration | Completed | Input tokens / run | Median time | Summaries | Masked | Re-reads | audits: gross weight of the really delayed ones right | compare: the first copy's gross weight right |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | Compaction only | 0/9 | ≈34.2k (a run that read all 12 copies) | 78 s | 9/9 runs | 0 | 0 | **22/45** | **3/3** |
  | Masking first | 0/9 | ≈23.2k (−32%) | 34 s (−57%) | 0 | 9 / run | **0** | **9/45** | **0/3** |

  The completion rate is 0 on both sides and cannot tell them apart; what tells them apart is **which facts were
  kept**: this column was computed afterwards by a script from the stored answers (`scripts/long_eval_facts.py`),
  and is not a metric fixed in advance. How to read it: (1) with masking on, the model answers only from the last
  one or two copies it can still see (`keep_last=2`): audit-en said only 1 ticket (the last copy R12) all three
  times, and compare-en made up 7.5 kg for the first copy's 43.6 kg all three times; in 9 runs it did not once
  re-read following the placeholder `[result cleared to save context; call read_report again if you need it]`.
  (2) With compaction only, the summary kept the earlier facts (audit-en had two runs where the ids and gross weights
  of all 8 tickets were right, and compare-en's first-copy gross weight was 3/3), and the failures are in qwen3:8b's
  own counting and arithmetic (listing 8 ids but saying "5 tickets"; computing the difference as 16.7) and in
  losing its place on the chain after the summary (2 times it took T07 as the last copy, and in 2 runs it stopped
  after reading the 10th and the 8th copy). The conclusion is written into §6: off by default, and turn it on only
  for tasks where an old result is used up after being read once. Limits: synthetic tasks, one model, 3 runs per
  cell (greedy, and the three runs with masking on are almost the same); the prompt cache was not measured. Not done
  (§16): a more informative placeholder, a stronger model. KB card
  `global/llm-evals/evals/2026-11-26-lite-agent-masking-long.md`.
  Close-out: `/simplify` (four angles) accepted the generic `Task`, the system prompt belonging to the world, one
  constructor for long tasks, `masked` using `masks()`, and one `fake()` in the tests; not accepted: the `.0` branch
  of `_kg` is dead code (a total can end in .0), recomputing the answers independently in the tests (deliberately
  not computed with the code under test), `Report.next_id` is redundant (the test uses it to check the chain), one
  `kind` per world (a shared vocabulary lets the dataset labels be checked). Correctness review (the Codex quota is
  used up, done by a Claude subagent): the three problems of the judging (above) were fixed, with failing tests
  written first; the comment on the report length was changed to 1,250; `rereads` does not recognise the same id in a
  different case and counts a retry after an error: it is only a metric, and is left; the identity of the eval cache
  does not include lite-context's version (masking and compaction use its estimate): this is a long-standing gap,
  and fixing it would touch the S5 test that pins `versions()`, so it is done separately (§16); neither of the two
  runs hit the cache. One mutation check left stale bytecode (a change of the same size, restored within the same
  second; Python goes by mtime and size and kept running the mutant), which made the data-pinning test fail during
  the review; it passed after deleting the `.pyc`, and every later mutation check carries `PYTHONDONTWRITEBYTECODE=1`.

## 16. Open questions

- **Copying a very long session** (S4b Codex): `copy_session` reads the whole session into memory and writes it as one
  batch; writing in batches while still being all or nothing needs the store to provide transactions, to be decided
  when there really is such a long session.

- **A version the migration runner does not recognise** (S4a Codex): for a version that appears in
  `schema_migrations` but is not in the package (a database migrated by newer code), `pending()` gives no error.
  Three repos share this code, and changing it means changing them together.

- ~~**[Blocks the decisions after S1] What is the first real task**~~: decided by the owner on 2026-11-21:
  llm-steward's **reconcile troubleshooting assistant**. After a reconcile finds an orphan provider record, the agent
  uses read-only tools to look up change_log / external_refs / the provider record, writes a diagnosis and a
  suggested action (ignore / remirror / erase), and hands it to a person to decide in the Admin; it writes no
  governed state itself. The design is in llm-steward design §11, P11. This host uses only the core library and the
  `pg` extra, and not the HTTP shell or Temporal.
- ~~**[Blocks P0] lite-model's tool-call interface**~~: decided and implemented on 2026-11-21 (lite-model design
  §3.1).
- **Reasoning content of OpenAI-compatible backends**: lite-model 0.4.0 neither collects nor sends it back (its §15).
  S5b verified on Ollama: qwen3:8b with thinking on (`reasoning_effort: medium`) and the reasoning content not sent
  back, the task set completion rate was 0.889, higher than 0.750 with thinking off, so Ollama does not require it
  to be sent back. Other endpoints such as DeepSeek still need to be verified. A new problem: a single call with
  thinking can exceed lite-model's default 120-second timeout (one `CallFailed` in long-en), and a host that uses
  thinking must raise `timeout_s`.
- **A 400 for preserved thinking cannot be told apart from other failures**: the projection rule (§4) should keep it
  from happening; if it does happen it is `CallFailed` and will trigger `fallback_role`. Whether to have lite-model
  give it a separate error type is to be decided in S2 when the fallback is done.
- **streaming**: in v1 the event granularity is one turn. When an interactive UI needs it token by token,
  lite-model has to support streaming first.
- ~~**Default persistence of the session store**~~: S4a did PostgreSQL (`PgSessionStore`). A file store is still under
  "on demand".
- **Oversized tool results**: only truncate, or also store the full result elsewhere (a blob table / files) and
  leave a reference in the event. On 2026-11-27 **how to do it when storing elsewhere** was decided (not yet
  scheduled into a stage, to be done when a host needs it; it is only a proposal, and the interface in §3 was not
  changed):
  - **When**: wait until some host's tool result really exceeds `tool_result_chars` and it has been measured that
    the truncation affected the answer; measure first, then do it. Until then, the behaviour is as it is now (§4:
    truncate, record `original_chars`).
  - **Shape**: add a Protocol `ArtifactStore` (`async put(data: bytes, media_type) -> ref`,
    `async get(ref, offset, limit) -> bytes`), injected when the `Agent` is built like `Memory` and `SkillSource`,
    and absent by default. When given: a result over the budget is `put` in full, the `tool_result` event still
    stores the truncated preview and also records `ref` and `original_chars`; at the same time one tool
    `read_artifact(ref, offset, limit)` is added automatically so that the model can read the truncated part in
    sections. Storing without being able to read is the same as not storing. `tool_result` gains one field, the event
    vocabulary has to go up one version (V=3), and old logs are read as usual.
  - **Implementations**: `LocalDirArtifacts` (core, for tests and single machines); `PgArtifacts` (extra `pg`, in the
    same database as `PgSessionStore`); `S3Artifacts` (a new extra `s3`, or implemented by the host). Like the other
    Protocols, a session can resume with another implementation (AGENTS.md rule 6), provided the content the ref
    points to is still there.
  - **Constraints**: addressed by content hash, append-only and never overwritten. The event log cannot be changed,
    and fork and resume depend on it, so the artifacts the events reference also cannot be changed or deleted
    alone; they can only be deleted together with the whole session.
  - **Boundary**: this covers only the large output produced while the agent runs. The original text of knowledge
    documents belongs to the ingestion flow of lite-rag / llm-steward, and business files uploaded by users belong to
    the host. No separate repo (KB `global/llm-app-architecture.md` §13.11).
- **Parallel tools**: whether several calls in the same turn run in parallel; when parallel, the result events are
  written in call order.
- **Calls that cost money but did not enter the log**: when the model has replied but `append` fails (an ordinary
  turn or a summary), this call's usage is not in the log, and the budget undercounts this amount (§4). The thorough
  way is to write an "intent" event before the call and match it up afterwards; S7's durable runner needs something
  similar, to be decided together then.
- ~~**Cost of the projection**~~: measured in S5b (`scripts/bench_projection.py`). `derive()` per step: for 200 / 1000
  / 5000 events the median is 0.22 / 1.13 / 5.96 ms; PG tail read 0.47 / 0.50 / 0.74 ms, the end of a three-layer
  fork 1.6 / 2.0 / 4.6 ms; a whole read 2 / 9 / 59 ms (only at the start of a run). A model call takes seconds, so
  **the projection is not cached, and the read is not changed into one recursive CTE**; measure again when a session
  reaches tens of thousands of events.
- **memory and llm-steward**: whether the agent writes memory directly with lite-mem, or must go through
  llm-steward's governance.
- ~~**HTTP port**~~: 8730 (decided in S6, registered in KB `global/machine/ports.md`).
- **Placeholders and re-reads for masking** (long-task eval, 2026-11-26): qwen3:8b does not re-read following the
  placeholder (0 times in 9) and makes up facts it cannot see. Worth trying: a placeholder that carries what the
  model may still need from the result (lite-context's placeholder is a fixed constant, §5.2 rule 6; to change it,
  change it over there and back it with an eval), or measure again with a stronger model. qwen3:8b cannot count or
  calculate accurately over 12 copies, and to let the completion rate tell the two configurations apart, a shorter
  chain or a stronger model is needed.
- ~~**The identity of the eval cache does not include lite-context**~~ (close-out review of the long-task eval): added
  on 2026-11-27. `AgentTarget.versions()` now has lite-agent, lite-model, lite-context and the code hash; masking
  and compaction use lite-context's estimate, so upgrading it means new output and no longer hits the old cache.
  S5's `test_the_target_says_what_decides_its_output` pins the set of keys and was changed accordingly (the owner
  agreed), and it checks that the value is the installed lite-context version.
- **Location of the project KB**: proposed `KB/projects/lite-agent/` (created in S0); lite-cache keeps it in the
  repo's `kb/`.
