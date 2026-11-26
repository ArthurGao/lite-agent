# lite-agent 设计文档

**状态：S1–S7 完成（2026-11-21）；v0.3.1（2026-11-26）：PG store 并发写同一尾部时给 `SeqConflict`；v0.3.0（2026-11-26，事件词汇表 V=2）：用 lite-context 的 `fit()` 遮盖旧的 tool 结果（§6）；v0.2.5：压缩器改用 lite-context（§6、§13）；事件与投影、tool loop、`@tool`、预算、gate、暂停 / 恢复、`fallback_role`、skills、memory、压缩、PG session store、CLI、lite-eval target（S5）、HTTP 壳（S6）、Temporal runner 与 inbox / timer（S7）。v0.1.0（S5.5，事件词汇表 V=1 冻结）与 v0.2.0 已发布。P0 是 lite-model v0.4.0 的 `turn()`。仓库公开在 github.com/ArthurGao/lite-agent。第一个宿主：llm-steward 的 reconcile 排查助手（2026-11-21 owner 定，§16）。**
与代码冲突时，在同一次改动里更新本文，并在 commit 里说明原因。

## 0. 决策记录

决策的理由只写在这里，其它章节引用（§0）。"owner" 是主人定的；"提议" 是起草时的判断，主人没否决前按它做，
否决时在这里改。

| 日期 | 决策 | 谁 | 原因 |
| --- | --- | --- | --- |
| 2026-11-20 | 形态：library；CLI 和 HTTP 是它上面的薄壳，不放逻辑 | owner | tool 是宿主的 Python 函数；审批、写回、预算要和宿主业务代码在同一进程；宿主注入 fake 就能测。与 lite-rag / lite-mem 一致 |
| 2026-11-20 | 一个通用 lib，从"两个 tool 的简单 loop"到"完整 harness"到"long agent"都由同一个 `Agent` 装配出来，靠配置和初始化参数区分 | owner | 以后每个项目都能用，不为每种用法各写一份 |
| 2026-11-20 | 收尾流程与其它 lite 模块相同：`/simplify` → Codex 只读 review → 修复或写下否决理由 → 重跑全部检查 | owner | workspace 约定 |
| 2026-11-20 | TypeScript 项目通过 HTTP 壳使用，不用 TypeScript 重写一份 | owner 提出需求，方案为提议 | 两份实现会各自漂移，§4、§5 的规则要验证两次 |
| 2026-11-20 | 一个仓库：harness（单次 run）和 long agent（durable 外壳）是同一个东西的两层，不拆成 lite-agent + lite-harness | 提议 | 没有第二个只用内核的宿主时，拆开等于提前定死一个公共接口，还多一条 pin 链。出现第二个宿主再拆 |
| 2026-11-20 | 不设 `mode` 开关。差异是两条独立的轴：装哪些能力（构造 `Agent` 时注入，不给就没有）、在哪里跑（换 `Runner`） | 提议 | `if mode ==` 会散进核心，每加一种用法都要改核心（KB `llm-app-architecture` §10.1） |
| 2026-11-20 | 自己写 loop 和 event log；pi、DeepSeek Harness 只读它们的设计，不作依赖；不用 Pydantic AI / LangGraph。协议和基础设施用现成的（MCP SDK、`temporalio`、lite-model） | owner | 目标是掌握并验证这套架构（把 KB §9、§11、§12 从 "reported" 变成 "observed"，与 lite-rag / lite-mem "built to learn from" 一致），不是尽快拿到一个能用的 agent；event log、fork、回放是必须的，而框架自己维护 message history、自带 provider 层（绕过 lite-model 的 role 和 provenance）、loop 规则由它定。pi 和 dsh 是 TypeScript。已知代价：tool calling 的后端差异要在 lite-model P0 里自己处理 |
| 2026-11-20 | 每次模型调用走 lite-model role；不 import `litellm`、不直接用厂商 SDK | workspace 规则 | AGENTS.md "Shared conventions" |
| 2026-11-20 | 模型调用失败后可以换一次模型续跑：`fallback_role`，默认关，细则见 §5 规则 11 | owner 提出，细则为提议 | 无人值守的长任务不该因为一个厂商暂时不可用就整个失败。workspace "不做 fallback" 的理由是换模型不可见；这里每次切换都是 log 里的事件，每一轮都记 provenance，所以可见。KB §3.2 写的也是"默认不做" |
| 2026-11-20 | 核心是 async；提供 `run_sync()` 包装 | 提议 | MCP Python SDK、Temporal Python SDK、llm-steward 都是 async；同步宿主（lite-rag CLI 那样的）用包装 |
| 2026-11-20 | "暂停"是 run 的正常结局之一（完成 / 失败 / 暂停），不是异常 | 提议 | 等审批、等客户端执行 tool、等 Temporal signal 是同一个机制：状态写进 event log，本次执行结束，之后凭 session id 恢复。后加的话要回头改 loop |
| 2026-11-20 | 默认一个 loop + skills；不做 subagent | 提议 | KB §14.1：拆 subagent 要有测量过的理由 |
| 2026-11-20 | Temporal 是可选 extra（`lite-agent[temporal]`），在核心稳定之后做；event log 是事实来源，Temporal 只保存进度 | 提议 | KB §7.3 "两个 orchestrator"；cairn 已是"Temporal holds progress only" |
| 2026-11-20 | TypeScript 宿主的 tool 首选 MCP（宿主起 MCP server，lite-agent 作 client）；其次是"暂停，宿主执行后回传结果" | 提议 | MCP 是现成标准，同一个 server 也能给 Claude Code / Codex 用 |
| 2026-11-20 | v1 不做 streaming，事件粒度是"一轮" | 提议 | lite-model 不做 streaming（它的设计 §1 非目标）；见 §16 |

## 1. 概述

lite-agent 是一个 **agent harness 库**：给它一个 role、一组 tool 和一个任务，它跑 tool loop 直到完成、失败或
暂停，全过程记在一条 append-only 的 event log 里。

三个词在本文里的意思：

- **agent loop**：调模型 → 执行 tool → 把结果送回 → 决定是否继续。只是 harness 的一个部件（§5）。
- **harness**：让 loop 能干活的全部外围——transcript、context 组装、tool 注册、预算与闸门、入口（§3–§8）。
- **long agent**：harness 加上 durable 外壳——inbox、timer、跨进程恢复（§9、§10）。

### 来源

- KB `global/llm-app-architecture.md`：§7（durable workflow 里的模型调用）、§9（event log + 纯投影）、
  §10（可插拔能力）、§11（loop 机制）、§12（always-on）、§13（memory）、§14（单 loop 还是 subagent）。
  §9–§14 目前都是 "reported"，本项目是它们第一次落地；实测与规则不符时改 KB（`kb-maintain`）。
- cairn：Temporal 上的 agent run，模型调用是 activity，文件是事实来源
  （KB `projects/cairn/integrations/temporal-runtime/`、`harness-home-and-core/`）。
- 读过设计、不作依赖：DeepSeek Harness（session event log、`deriveMessages()`、能力接缝）、pi（`agent-loop.ts`）。

### 目标

1. 一个 `Agent` 对象覆盖从简单到复杂的用法，区别只在装了什么（§3、§11）。
2. transcript 是 event log，发给模型的 messages 是它的纯投影；resume、fork、审计、评测都是对同一条 log 的
   不同读法（§4）。
3. loop 的边界行为是写明并测到的：截断、停止条件、自动续跑、预算、暂停（§5、§8）。
4. 每个能力是 Protocol + 一个默认实现，宿主在构造 `Agent` 时注入自己的实现来替换（§3）。
5. 不联网、不花钱就能测：`FakeModel` 脚本化每一轮（§12）。

### 非目标（v1）

- 不做 subagent / 多 agent 编排（§0）。
- 不做 sandbox：tool 在宿主进程里跑，隔离是宿主的事。
- 不做 TUI，不做 coding agent 的内置工具集（读写文件、shell）；需要时由宿主注册。
- 不做 "一切皆 plugin" 的元框架：可替换的只有 §3 列出的那几个 Protocol。
- 不做 streaming（§16）。
- 不做自动的模型 fallback 链。唯一的例外是显式配置的 `fallback_role`：只换一次、写进 log（§5 规则 11）。

## 2. 分层

```
宿主（Python）──import──► Agent ──► Runner ──► loop ──► lite-model role ──HTTP──► 模型
                            │                    │
                            │                    ├─► tools（宿主函数 / MCP / lite-rag）
                            │                    └─► gates（预算、审批）
                            └─► SessionStore（event log）◄── derive_messages()（纯投影）

宿主（TypeScript）──HTTP 壳（§10）──► 同一个 Agent
long agent：inbox / timer ──► TemporalRunner（§9）──► 同一个 loop，一步一个 activity
```

依赖方向：核心（`events`、`projection`、`loop`、`agent`）只依赖 lite-model 的类型和 Protocol。
PostgreSQL、MCP、lite-mem、lite-rag、Temporal、FastAPI 各是一个 extra，核心不 import 它们。

## 3. 接口

S1（2026-11-21）定稿了 `Agent`、`SessionStore`、`Runner`、`RunResult` 和 `Tool` 的最小形状。标了阶段的是之后
要加的部分：都是新增的可选项，不改已有的。

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

- `run(task)` 开一个新 session；`run(task, session_id=…)` 是那个 session 的下一次 run（多轮对话），不存在的
  session 抛 `UnknownSession`。`Runner.run` 的输入是任务（`str`；`session_id=None` 表示新 session）或 `Resume`。
- `resume(session_id, input)`（S2b）接着一个暂停、失败或被打断的 session 往下走（§8）。不带输入也可以调用：重试
  失败的那一轮，或恢复可以安全重跑的 call。输入对不上（不存在的 call id、给已开始的 call 批准、给没开始的 call
  结果、session 已经完整结束）抛 `ValueError`，不写任何事件。
- **调用方给 run 命名**（S6，S7 的 activity 重试靠它）：`run_id` 给定时，再用同一个 id 调用永远不会开第二个 run——
  log 里这个 run 已经结束，就从 log 还原结果返回、什么都不写；没结束（进程死在中途），就**在同一个 run 里接着跑**：
  不再写开头的事件，上一轮留下的 call 按 §8 处理（开始过的非幂等 call 不重跑，要求核对），然后继续 loop。
  `new_session=True` 用给定的 `session_id` 建一个新 session；这个 id 已被别的 run 用掉时抛 `SeqConflict`
  （两个调用方同时抢同一个 id 也是：输的一方得到 `SeqConflict`，不是一个失败的 run）。
  一个 `run_id` 只命名一个 run：它只能是这个 session 的**最后一个** run（重试的是最新的那次调用），而且任务要和第一次
  相同；否则是复用了 id，抛 `ValueError`。重试时要传和第一次相同的输入：`resume` 的 `Resume` 只作用于还没有结果的 call。
- `Tool` 用 JSON Schema 字典而不是 Pydantic 模型：loop 只需要把定义交给模型、把参数交给函数。两个实现：
  `@tool`（S2a）做出 `ModelTool`——schema 是模型的 `model_json_schema()`，`validate()` 用 Pydantic 校验，函数拿到的
  是模型实例；`FunctionTool` 包一个拿原始参数的 async 函数，不校验。
- `RunResult.error`：run 自己的失败。模型调用失败是 `model`（log 里有 `model_error`）；store 读写失败是 `store`、
  投影不变量不成立是 `harness`，这两种不再写 log（§4）。不是 `ModelError` 的异常（宿主或测试的 bug）原样抛出。
- `@tool` 装饰器把一个带类型标注的函数变成 `Tool`（schema 来自参数的 Pydantic 模型）。
- 配置放数据，初始化参数放代码对象（§11）。
- 能力在构造 `Agent` 时注入，构造后不可变；要换实现就构造一个新的 `Agent`（session 在 store 里，不受影响）。
  v1 不做运行时注册表：KB §10.1 的"可撤销、可寻址"等有宿主需要热替换时再做。`liteagent config` 打印
  最终装配结果（KB §10.3）。
- 状态全部在 `SessionStore` 里：在组件 A 下产生的 session 必须能在组件 B 下恢复（KB §10.3 的检验，S4 验收）。

## 4. Event log 与投影

**规则（KB §9）：event log 只追加，是事实来源；发给模型的 messages 每一轮由 log 纯投影得出，从不单独保存、
从不原地修改。**

信封：`session_id`、`seq`（单调，从 1 起）、`time`、`run_id`、`type`、`v`（事件词汇表版本，现在是 1）；各类型自己的
字段平铺在事件上。事件是 frozen 的 Pydantic 模型，`dump()` / `load()` 做 JSON 往返；`load()` 拒绝未知的 `type`、
多余的字段和更新的 `v`。S1 实现了下表里的 8 种：`session_started`、`run_started`、`user_message`、`model_turn`、
`tool_started`、`tool_result`、`model_error`、`run_finished`；S2b 加了 `paused`、`resumed`；其余在用到它的阶段加。
**V=2（2026-11-26，v0.3.0）**：加了 `tool_results_masked`；V=1 的事件形状一个没变，V=1 的 log 照原样读出、投影不变
（`tests/fixtures/v1_session.json` 是 v0.2.5 写的一段真实 log 和它当时的投影）。从 V=2 起信封里的 `v` 是**这种事件所属的
版本**，不是代码的最新版本：V=1 就有的类型照旧写 `v=1`，只有 `tool_results_masked` 写 `v=2`（`v=1` 的它被拒绝）。所以没开
遮盖的 session 全是 `v=1`，v0.2.5 的 worker、壳照样读；只有写过遮盖事件的 session，旧代码才会拒绝。
**从 v0.1.0 起 `V = 1` 冻结**（2026-11-21 发版）：发版之前一直保持 1，因为没有旧 log 要读。从此以后，改动任何
事件的形状都要把 `V` 升一级，并保留读旧版本的代码：PostgreSQL store（S4 起）里存着的 log 必须一直读得出来。
`tests/unit/test_vocabulary.py` 为每个 V 存一份所有事件类型 JSON Schema 的指纹，形状变了而 V 没升就失败。

| type | data | 模型可见 |
| --- | --- | --- |
| `session_started` | session 的第一条事件；它的 `run_id` 是开这个 session 的那次 run。fork 的血缘不是事件，是 store 的元数据（父 session、fork 点；S4a 定，两种 store 一样） | 否 |
| `run_started` | `run_id`、触发方式（run / resume）、**本次 run 的快照**：主 role、`fallback_role`、system prompt 原文、每个 tool 的完整定义（名字、描述、JSON schema） | system 和 tools |
| `user_message` | 文本；`origin`：task（S1）；steering / follow_up / auto_continue / resume 在 S2 加 | 是 |
| `model_turn` | lite-model 的 `AssistantTurn.blocks`，按端点给的顺序原样保存（`Text`、`ToolCall`、`Opaque`——Anthropic 的 thinking block）；`stop`、role、provenance、usage | 是 |
| `tool_started` | `call_id`、tool 名、参数、`external`；过了校验和 gate、在执行（或交给宿主执行）**之前**写。校验失败、`Deny`、`PauseFor` 都不写它（§8） | 否 |
| `tool_result` | `call_id`、内容（已截断）、`is_error`、`error_kind`（`invalid_args` / `denied` / `exception` / `timeout` / `truncated_turn` / `outcome_unknown`）、`executed`（tool 是否被调用过）、`origin`（`executed` / `external` / `reconciled`）、`terminal`、原始长度 | 是 |
| `compaction` | `summary`（模型看到的全文，含固定的前言）、`covers`（覆盖的 seq 区间，两端都含）、做摘要的 role、provenance、usage | 是（替代被覆盖的区间） |
| `tool_results_masked`（V=2） | `masked`：每条被遮的 `tool_result` 的 seq 和从此给模型看的原文（lite-context 的 `MASKED_RESULT`，带 tool 名）；`state`：lite-context 的 `ContextState`（下一次在同一处切）；`tokens_before` / `tokens_after`（估算） | 是（替代那几条结果的内容） |
| `paused` / `resumed` | `paused.waiting`：每个等待的 call 一项（`call_id`、原因 `approval` / `tool` / `reconcile`、gate 给人看的说明）；`resumed`：`approve`（call id → 是否批准）和带了结果的 call id 列表（结果本身在 `tool_result` 里） | 否 |
| `model_error` | 失败调用的错误类型、role、`purpose`（`turn`，或 `compact`：一次没写成的摘要）、usage（拿不到时为 `None`） | 否 |
| `model_switched` | from / to role、原因（§5 规则 11） | 否 |
| `run_finished` | outcome、stop、usage 合计 | 否 |

- **写入时校验，比读取严**：不合法的事件被拒绝，不进 log，没有副作用。
- **投影纯且逐字**：`derive_messages(events) -> list[Message]` 不加任何文字；包装文本在写事件时就已经在
  内容里。
- **新的模型可见输入 = 新的事件类型**，不在 loop 里临时拼 message。
- **投影是后端无关的**：`derive_messages` 只读事件，输出 lite-model 的 `TurnMessage` 序列。`model_turn` →
  `AssistantTurn`（blocks 的顺序不变）；同一轮的全部 `tool_result` → **一个** `ToolResults`（Anthropic 要求
  同一轮的结果在一条消息里，lite-model 设计 §3.1）。把 `Opaque` 发给另一种协议时怎么处理是 lite-model
  后端 adapter 的事（同协议原样发，另一种协议不发）。
- **`Opaque` 只在它的前缀没变时进投影。** 较新的 Anthropic 模型把每个 thinking block 绑定在产生它时的
  `system`、`tools` 和之前的全部消息上；这些变了之后再发回去，请求是 400（lite-model 设计 §3.1 的
  "preserved thinking"）。只追加的历史不受影响。所以投影丢掉这样的 `Opaque`：它所在的 `model_turn` 早于
  最近一条 `compaction`，或早于最近一条 system / tools 快照与上一条不同的 `run_started`。这条规则只看 log，
  投影仍然是纯的；文本和 tool call 照常保留。代价是模型失去那之前的思考内容，摘要是它对那段工作的全部了解。
  规则有意保守：快照 A → B → A 之后，A 时期的 `Opaque` 也不再进投影（它们其实仍然有效），换来规则只有一条、
  永远不会发出失效的 block。
- **system 和 tools 也来自 log**：投影取最近一条 `run_started` 的快照。一次 run 之内它们不变；宿主换了
  `Agent` 的配置再 `resume`，新的 `run_started` 记下新的快照，回放时每个请求都能按它所属的 run 还原。
- **不变量**：每次真实请求前断言 "交给 lite-model 的 messages 和 tools == log 的投影"，不等就失败。实现（S4a）：
  loop 手里有一份自己追加过的事件，请求由它投影得出。run 开始时把 session 整个读一遍；之后每次请求之前，只读
  "上次核对之后"的那一段，要求与自己写的逐条相等，否则 `HarnessError`、不发请求（store 丢了或改了刚写的事件
  时在这里被抓住）。已经核对过的前缀不再重读：这依赖 store 从不改写——PostgreSQL 那边由数据库保证（`events`
  表上的触发器拒绝 `UPDATE`、`DELETE`、`TRUNCATE`），自己实现 store 的宿主要做到同样的事。S1 到 S3 是每步全量
  读、比较两次投影；换成 PostgreSQL 后每步一次整 session 的读取代价太高。它比较的是规范表示；后端实际发出的请求体由 lite-model 的契约测试保证。
- **压缩只做遮盖**：`compaction` 事件记下它覆盖哪些 seq，原事件留在 log 里；投影时用摘要替代那个区间。
- **fork**：新 session 记 `(parent_session_id, at_seq)`，不复制事件。读子 session = 父 session 里
  `seq ≤ at_seq` 的事件，接上子 session 自己的事件；子 session 的 seq 从 `at_seq + 1` 起，存在自己名下。
  父 session 只追加，所以这个前缀永远不变：父 session 在 fork 之后写的事件（包括覆盖旧区间的
  `compaction`，它的 seq 大于 `at_seq`）子 session 看不到。`expected_seq` 只检查子 session 自己的尾部。
  fork 的 fork 沿链向上读。
- **并发**：`append` 带 `expected_seq`，不匹配就拒绝（同一 session 只能有一个写入者）。一次 `append` 的
  一批事件要么全写入、要么全不写。模型已经回复但 `append` 失败时，run 失败，这个回复丢掉（钱已经花了，
  log 里没有它）；`resume` 会重发这一轮。
- tool 结果超过 `Budget.tool_result_chars` 时在写入 `tool_result` 前截断，末尾加一行
  `[truncated: the first N of M characters]`，`original_chars` 记原长度。预算管的是保留下来的原文长度，说明那
  一行另算（所以存下来的内容比预算多几十个字符）；log 里存的就是模型看到的。出错信息同样
  处理。完整结果要不要另存见 §16。

## 5. Loop 规则

1. **被长度上限截断的一轮不执行任何 tool call**，但每个 call 都写一条错误 `tool_result`（"输出被截断，参数
   可能不完整，请重发"），然后继续（KB §11.1）。这一轮计入 usage 和 turn 预算。被截断的一轮只有文本、没有
   call 时没有东西可回，run 以 `failed`、`stop=truncated` 结束，`text` 是已有的那部分（S1；S2c 起先续写，见规则 3）。
2. **一批 tool 结果全部是 terminal 才结束**；只要有一个普通结果，就再给模型一轮（KB §11.2）。
3. **纯文本的 `end_turn` 不等于完成**（KB §11.4；S2c）：宿主给了完成条件 `Agent(done=…)` 时，每个纯文本的轮次
   问它一次——它拿到包含这一轮在内的整条 log，返回 `None` 表示做完了，返回文字表示还有什么没做。这一轮和它
   后面的那条事件（发回去的消息，或 run 的结束）在同一批里写入：中间不会留下"看起来结束了、其实没做完"的状态。有没做完的，就把
   那段文字作为 `origin=auto_continue` 的 `user_message` 发回去，每次 run 最多 `budget.auto_continues` 次
   （默认 2）；用完还没做完，run 以 `failed`、`stop=incomplete` 结束，等人来看。没给 `done` 时纯文本即结束。
   以 terminal tool 结束的 run 不问 `done`。`done` 返回空字符串等同于 `None`。`done` 是宿主的同步代码：它抛的
   异常原样抛出（和宿主的其它 bug 一样，不吞），耗时的检查会占住 event loop——要做 I/O 的完成条件等 S7 的
   durable runner 再给它一个 activity 的位置。`Budget(auto_continues=0)` 同时关掉截断续写。**被截断的纯文本**也走这条路：追加一条"接着写"的消息继续，占用
   同一个次数上限，用完才是 `stop=truncated`。
4. **快照按 run 取**：`run()` / `resume()` 开始时把 role、`fallback_role`、system、tool 定义写进
   `run_started`，本次 run 的每一轮都从它取（§4）。KB §11.3 说的是每一轮取一次；这里收紧到每个 run 一次，
   换来"请求可以从 log 完整还原"。run 中途要改 system 或 tool，就结束这次 run 再开一次。
   建议一个 session 里 system 和 tools 一直不变：改了是允许的，但之前的 thinking block 不再进投影（§4），
   prompt cache 也从头开始。
5. **运行中到达的输入**分两个队列：steering 在两轮之间注入，follow-up 在即将停止时读取并重启 loop
   （KB §11.3）。两者都是给 long agent 用的，现在没有调用方；随 S7 一起设计（2026-11-21 从 S2 移出）。
6. **预算包含失败和重试**：每次模型调用（包括 lite-model 抛错的、做压缩的）都写事件、计 usage；下一次
   调用会超出预算时在调用前停，`outcome=failed`、`stop=budget_*`（任务没做完，不算 completed；KB §7.2）。`turns`、`tokens`、`cost_usd` 按**整个
   session** 算，每次从 log 合计（暂停再恢复不会得到一份新预算；fork 出来的 session 包含继承的前缀）。
   `auto_continues` 按 run 算。拿不到 usage 的调用（`CallFailed`）记 `None`、不估算，token 预算里按 0 计，
   turn 预算照算。压缩调用计入 token 和成本，不计入 turn。检查的是"已经用掉的是否达到预算"：token 是每次
   调用的 input + output 之和，下一次调用会用多少事先不知道，所以最后一次调用可以超出一点，之后不再调用。
7. tool 抛的异常变成 `is_error` 的 `tool_result` 送回模型；`Gate` 的 `Deny` 同样。只有 harness 自己的错误
   （store 写失败、投影不变量不成立）才让 run 失败。同一轮里有两个 call 用了同一个 id 时，结果没法和 call
   对上：这一轮不记成 `model_turn`，写 `model_error`（`DuplicateCallIds`，带这次调用的 usage），run 失败。
8. 同一轮里的多个 tool call 默认顺序执行；并行执行见 §16。
9. **模型调用失败（超时、连接错误、5xx、429）**：重试只在 lite-model 里做（同一个 target，`max_retries`，
   它的设计 §8），loop 不再叠一层重试。重试用尽后 lite-model 抛 `CallFailed`：写 `model_error` 事件，run 以
   `outcome="failed"`、`stop=error` 结束。默认**不换模型**；配置了 `fallback_role` 时先按规则 11 换一次。
   log 的最后是 `model_error`（投影不包含它），
   `resume(session_id)` 重发这一轮，之前的 tool 不重跑。超时的调用拿不到 usage（`None`），事件里
   如实记 `None`，不估算。durable runner（§9）可以用更长的重试策略并尊重 Retry-After，那是 runner 的事。
10. **tool 超时**：每个 tool 有 `timeout_s`（默认 `Budget.tool_timeout_s`，60 秒）。超时时这次调用被取消（tool
    自己抛的 `TimeoutError` 不算，是普通的 `exception`），写 `tool_result`，
    `error_kind=timeout`、`executed=True`，内容写明"是否已生效未知，不要直接重试"。它和"没有执行"
    （`invalid_args`、`denied`、`truncated_turn`，`executed=False`）是不同的结果，模型和宿主都分得清。
    同一个 `call_id` 不会再执行。模型想重试，只能发一个新的 call（新的 `call_id`），它照常过 gate；gate
    通过 `RunContext` 读得到 session 的事件，可以据此拒绝或要求审批"上一次超时、结果未知"的重复调用。
11. **换模型续跑（可选，默认关）**：`Agent(fallback_role=...)`。
    - 只在 `CallFailed`（超时、连接、5xx、429，重试已用尽）时触发。`Truncated`、空回复、拒答、tool 参数
      不合法都**不**触发：那是质量问题，换模型会把它盖住。
    - 顺序：`model_error` 和 `model_switched`（from / to role、原因）在同一批里写入 → 用 `fallback_role` 重发
      这一轮（请求完全相同）。fallback 也 `CallFailed` 就按规则 9 失败。一次 run 最多换一次。`model_turn` 和
      `model_error` 都记下实际用的 role。
    - **用哪个 role 完全由 log 决定**：当前 run（最近一条 `run_started` 之后）里有 `model_switched` 就用
      fallback，否则用主 role。换了之后本次 run 不换回去（来回切换会反复丢 prompt cache）。
    - 每次 `run()` / `resume()` 都写新的 `run_started`，所以新 run 从主 role 开始。进程在 `model_error`
      之后、`model_switched` 之前死掉，或在 `model_switched` 之后、`model_turn` 之前死掉，恢复时都是新 run、
      主 role：结果确定，最坏情况是多试一次主 role。
    - 切换之后在同一次 run 里暂停（等审批、等外部 tool），再 `resume`：也是新 run，先试主 role；主 role
      仍不可用就再切一次。这是有意的：规则只有一条，不需要把"已切换"作为跨 run 的状态保存。
    - `fallback_role` 是另一个 lite-model role，指向一个具体模型；不用 LiteLLM 的路由别名或 Proxy 的
      `fallbacks`（那是在看不见的地方选模型，路由调用还可能读不到实际回答的模型）。lite-model 不用改，
      它的"不做 fallback"规则不变：换模型是 loop 的决定，并且写在 log 里。
    - 把模型 A 的历史发给模型 B：投影不变。换到另一种协议时 lite-model 不发 `Opaque`；同是 Anthropic 时原样
      发，读不了的由 API 丢掉（不计费）。回到主 role 之后，前缀没变的 `Opaque` 照常发回。
    - 上下文窗口：v1 不为换模型专门压缩。fallback 因上下文过长被拒绝，就是一次普通的失败（规则 9）。
      配了 `Compactor` 时，它的阈值按**当前要调用的 role** 取（配置里每个 role 一个 `context_tokens`），
      所以切换后的第一轮会按 fallback 的阈值先压缩；压缩是普通的 `compaction` 事件，留在 log 里，之后
      主 role 恢复时也看到它。压缩调用自己失败或超预算 = run 失败。
    - `RunResult` 标明 `switched=True` 和每个模型各自的 usage。评测默认关掉 fallback，或按模型分组报告。

## 6. Context：system、skills、memory、压缩

- **system prompt** 是 `run_started` 快照的一部分，一次 run 之内不变（§4、§5 规则 4）。快照里存的是**拼好的
  全文**（`context.build()`，在构造 `Agent` 时拼一次，之后不变）：宿主给的 `system`，然后是 skills 的索引段，然后是 memory 的说明段，空的部分
  跳过。拼装是纯函数，同一个 `Agent` 每次得到逐字相同的结果（prompt cache 靠它）；发给模型的就是 log 里这一份。
- **没有 `context_added` 事件**（2026-11-21 从事件表里去掉）：skills 的正文和 memory 的内容都以 tool 结果的形式
  进 log，system 的部分在 `run_started` 里，没有第三种"注入的文本"。
- **skills**（S3a；`SkillSource` 协议：`index()` 给名字和一行描述，`load(name)` 给正文）。默认实现 `SkillDir(root)`
  读 `<root>/<name>/SKILL.md`，格式与 Claude Code / cairn 相同：开头的 frontmatter 里有 `name` 和 `description`，
  其余是正文。只读这两个键，值按单行的纯文本取（不处理引号和折行），自己解析，不引入 YAML 依赖。构造时读
  一遍：没有 frontmatter、缺字段、同一个键出现两次、`name` 和目录名不一致，当场报错。**skill 文件是宿主自己的
  文件，按可信内容对待**：名字和描述原样进 system prompt，正文原样作为 tool 结果给模型；目录里的符号链接照常
  跟随。
  - system 里只放索引（一行一个：名字和描述，按名字排序）和一句"对得上就先用 `load_skill` 读"。索引的条目
    （不含那句固定的说明）超过 `Budget.skills_index_chars`（默认 4000）时构造 `Agent` 就报错，不截断、不静默全量加载（cairn 的
    `ContextBudgetError`）。
  - 给了 `skills` 就自动多一个 tool `load_skill(name)`：返回正文；名字不存在是一条普通的 tool 错误，并列出有哪些。
- **memory**（S3a；`Memory` 协议：`async recall(query) -> str`、`async remember(note)`）：默认没有。给了就自动多
  两个 tool `recall` / `remember`，system 里加一小段，只说 memory 是做什么的，不写"什么时候记、记什么"的规则
  （KB §13.1）。run 开始时不自动 `recall`：要不要先查按模型而定（KB §13.3），由宿主的 system 决定。库里带一个
  `NotesMemory`（内存里的列表，按共同的词查找），给测试和单进程脚本用，不是检索引擎；lite-mem 的 adapter 仍在
  "按需"里。`Memory` 的实现抛出的异常和别的 tool 一样，连同消息文字回给模型（§5 规则 7）：消息里放什么由实现
  负责。
- system prompt 和内置 tool 在构造 `Agent` 时定下来（`Agent.system_prompt()`、`Agent.all_tools`），之后 skills 的
  来源再变也不影响这个 `Agent` 发出去的索引——预算检查的就是发出去的那一份。`Agent.tools` 保持宿主给的原样。
  换了 skills 再跑同一个 session，快照就变了，之前的 thinking block 不再进投影（§4），与换 system 的代价相同。
- 这三个内置 tool 排在宿主的 tool 之后，和宿主的 tool 走同一条路：校验、gate、超时、事件一样不少（所以宿主的
  gate 可以拦 `remember`）。与宿主的 tool 重名时构造 `Agent` 报错。
- **遮盖**（v0.3.0，V=2；`Masker` 协议：`check(roles)`、`mask(events, role)`）。不给 `masking` 就不遮。库里自带
  `ToolResultMasker(context_tokens={role: n}, keep_last=3)`：每次模型调用之前、压缩之前（它不调模型）。把**没遮过的**
  历史映射成 lite-context 的中立消息（key 是事件 seq，`e<seq>`；摘要是 `summary`），加上 run 快照里的 system 和 tool 定义
  算作 fixed，用 lite-context 的 `fit()` + `MaskToolResults(keep_last)` 和上一次 `tool_results_masked` 里存的 `state`；
  估算超过这个 role 阈值的 90% 才遮，遮到 70%。新被遮的结果写进一条 `tool_results_masked`（每条结果和它从此显示的原文），
  投影照着这条事件替换，自己不加字（§4 规则 2）；同一条结果不会被遮两次。`fit()` 装不下（`DoesNotFit`）就什么都不遮，
  交给压缩；lite-context 的任何别的拒绝也一样什么都不遮：遮盖从不让 run 失败。`keep_last` 至少是 1（最新的结果总是给模型看）。
  `keep_tools`（默认 `load_skill`、`recall`）的结果从不遮：它们是模型接着要照着做的说明和笔记，不是读一次的数据——
  在 lite-context 里把装着它们的那条消息标成 pinned。Ollama 评测里没有这条时，qwen3:8b 的 skill 任务从 3/3 掉到 0/3
  （§15 阶段记录）。压缩之后存的 `state` 对不上（`StaleState`），重新量；读不出的 `state`（更新版本的
  lite-context 写的）也重新量。两个都配时压缩的阈值应不低于遮盖的。估算不算 thinking block（与渲染一样）。
  **什么时候开**（长任务评测，§15 阶段记录 2026-11-26）：默认关，保持关。遮盖省输入 token（−32%）和时间（−57%），
  但 qwen3:8b 从不照占位符重读被遮的结果（9 次 0 次），只按还看得见的最后一两份作答，看不见的就编（第一份的毛重 43.6
  答成 7.5）；摘要压缩留住了早先的事实（45 个里 22 个对 9 个，第一份的毛重 3/3 对 0/3）。只在旧结果读过一次就用完的
  任务上开（查找、逐条处理、答案只依赖最近的结果），并且先在宿主自己的任务上量。
- **压缩**（S3b；`Compactor` 协议：`check(roles)`、`due(projection, role)`、`cut(events)`、`prompt(history)`）。
  2026-11-26 起提问、渲染和摘要外框来自 lite-context（§13）：`render()` 把历史映射成 lite-context 的中立消息
  （`neutral.to_neutral`）再用它的 `render_transcript`；`frame(reply)` 用 lite-context 的 `SUMMARY` 包住回复，去掉抄回来的
  头标行仍是这里的规则（任何 16 位以上十六进制的头标都去掉，不只这一次的；lite-context 只去这一次的）。历史不含 system
  prompt（`projection.history`），传了 system 消息是 `ValueError`。切点按
  `model_turn` 算、在事件 seq 上，仍是 `SummaryCompactor.cut` 的事；lite-context 的 `plan_compaction`（按人的轮次、
  在带 key 的消息上）不用。
  不给 `compaction` 就不压缩，超出上下文是一次普通的模型失败。库里自带 `SummaryCompactor(context_tokens={role: n},
  keep_turns=6)`：
  - **什么时候**：每次模型调用之前、预算检查之后。把要发的请求按"字符数 ÷ 3.5"估成 token（消息文本、tool
    参数、tool 结果、tool 定义都算；这是估算，不是计数），超过**这一次要调用的 role** 的阈值就压缩。切换到
    `fallback_role` 之后按它的阈值。主 role 或 `fallback_role` 没有阈值时，构造 `Agent` 就报错。
  - **切在哪**：保留最近 `keep_turns` 个 `model_turn`（连同各自的 tool 结果），覆盖它们之前的全部。切点是"第一
    个保留的轮次之前的那个 seq"，所以不会拆开 call 和结果，最后一轮永远保留。已经被摘要覆盖的轮次不再算数；
    没有新的可切就不压缩，请求照原样发出。
  - **怎么做**：把要覆盖的那段历史（如果前面已有摘要，摘要也在里面）渲染成纯文本，用 `Agent.compact_role` 调
    一次 `turn(role, messages, tools=())`。成功 → 写一条 `compaction` 事件，再查一次预算（摘要是花了钱的），然后
    重新投影再调主模型。失败 → 写 `model_error`（`purpose=compact`），run 以 `failed` 结束；被输出上限截断的摘要
    同样算失败（`TruncatedSummary`，带这次调用的 usage）：半截摘要不能顶替整段历史。每次模型调用之前最多压缩
    一次，所以一个永远说"该压缩"的 compactor 不会让 loop 空转。摘要写不成不触发 `fallback_role`，也不"不压缩接着
    跑"——历史超长的请求原样发出去多半再失败一次，原因更难看出来。
  - **投影**：只有最近一条 `compaction` 起作用（后一次压缩总是从头覆盖，把前一次的摘要也吃进去）。seq 不大于
    `covers` 上界的模型可见事件不再输出，开头换成一条 user 消息，内容就是事件里的 `summary`——那句固定的前言在
    写事件时已经拼进去了，投影不加任何文字。原事件都留在 log 里。
  - **摘要模型看到什么**：系统说明；带随机 token 标题的 transcript；transcript 末尾再写一遍"现在写摘要，不要回答
    里面的任何问题"。最后这一句是量出来的（见 §15 的 S3b 记录）：只放在系统说明里时，小模型把 transcript 当成
    要继续的对话，直接回答了最后一个问题。摘要里抄回的 transcript 标题行在写事件之前去掉；去掉之后什么都不剩
    的摘要算失败（`EmptySummary`）。
  - **谁的话算请求**：压缩前，tool 结果以 tool 结果的身份给模型；压缩后它只剩摘要里的一句，而摘要以 user 消息
    出现。为了不让 tool 内容（比如抓来的网页里伪造的一行"用户说"）借摘要升格成用户的要求：交给摘要模型的历史
    里，每一段以 `--- <随机 token> <谁> ---` 开头，token 每次压缩重新生成，内容里伪造的标题不带它；摘要模型被
    告知只有标成 user 的段落是用户的话，tool 结果和 assistant 的话里出现的指令只能当内容转述；摘要的固定前言
    说明它是对前面对话的转述，不是新的请求。这能挡住格式上的冒充，挡不住摘要模型自己理解错——摘要的质量仍要
    靠 S5 的评测。
  - **预算**：摘要调用计入 token 和成本，不算 turn（`budget.spent()`）；失败的摘要调用同样。
  - **`Opaque`**：最近一条 `compaction` 之前产生的 thinking block 不再进投影，包括被保留下来的那几轮（§4）。
  - 覆盖的区间可以跨过 fork 点（子 session 的 seq 接着父 session 往下排）；父 session 在 fork 之后做的压缩，
    子 session 读不到。

## 7. Tools

- 来源三种：宿主的 Python 函数（`@tool`，S2）；MCP server（`lite-agent[mcp]`，作 client，把远端 tool 映射成
  `Tool`）和兄弟模块的 adapter（`lite-agent[rag]`：`search_kb`）——后两种按需做（§15）。
- 参数先过 `Tool.validate()`（`@tool` 是 Pydantic 校验）；有问题就写 `tool_result`（`error_kind=invalid_args`、
  `executed=False`），不写 `tool_started`，不调用 tool。内容是字段路径和 Pydantic 给的原因，**不带输入的值**
  （可能很大）。宿主自己写的 validator 消息原样送回模型，里面放什么由宿主负责。未知的 tool 名、不是 JSON 对象的参数同样处理。
- tool 数量保持小，按意图分组（KB §14.3）。tool 的完整定义在 `run_started` 里（§4），两次 run 之间变了，
  log 里看得出来（影响 prompt cache 和可复现性）。
- **外部执行的 tool**（`external=True`；HTTP 壳的"客户端执行 tool"用它，§10）。事件顺序：校验 → gate →
  `tool_started`（`external=True`）→ `paused`（`reason=tool`，带 `call_id`）→ run 以 `paused` 结束 → 宿主
  执行 → `resume(session_id, ToolResults({call_id: ...}))` → `resumed` → `tool_result`（`origin=external`）。
  同一轮里有多个外部 call 时一起暂停。结果可以分几次交：没交的那些让 run 再次暂停（实现比"必须一次交齐"简单，
  对宿主也更宽容）。已有结果的 `call_id` 再交一次抛 `ValueError`，不写事件。宿主执行了但提交丢了：这个 call
  仍是"已开始、没有结果"，宿主重新提交即可；宿主那边的副作用用 `call_id` 去重。宿主报告的结果 `origin=external`，
  报错时 `error_kind=exception`；`terminal` 的外部 tool 成功时照样可以结束 run。

## 8. 预算、闸门、暂停

- `Budget(turns, tokens, cost_usd, auto_continues, tool_result_chars, tool_timeout_s)`：全部可选，默认只有 `turns=30`。范围见 §5 规则 6。
- `Gate` 在每个 tool call 执行前运行：`Allow`、`Deny(reason)`（作为错误结果送回模型）、
  `PauseFor(approval)`（run 以 `paused` 结束）。凭据在 agent 之外；审批让 run 结束而不是让进程挂着等
  （KB §12.4）。
- **暂停与恢复**：暂停时写 `paused` 事件，`RunResult.outcome="paused"`；`resume()` 读 log，写 `resumed`
  和对应输入（审批结论或 tool 结果），从下一步继续。恢复不依赖原进程的内存。
- **tool 调用的生命周期：至少一次，不是恰好一次。** 顺序是：校验 → gate → 写 `tool_started` → 执行 →
  写 `tool_result`。`call_id` 在整个 session 里稳定，通过 `ToolContext.call_id` 交给 tool，tool 应把它
  作为幂等键传给有副作用的下游系统。
- 恢复时按 log 判断每个 call：
  - 有 `tool_result`：不再执行。同一个 `call_id` 至多有一条 `tool_result`。
  - 没有 `tool_started`：正常走一遍（校验、gate、执行）。
  - 有 `tool_started` 没有 `tool_result`（进程死在副作用和写结果之间）：结果未知。
    - `idempotent=True` 的 tool：用同一个 `call_id` 重新执行。
    - 外部 tool：继续等宿主提交结果（§7）。
    - 其它：**不写** `tool_result`，写 `paused`（`reason=reconcile`，带 `call_id`），run 以 `paused` 结束。
      人核对下游系统后 `resume(session_id, ToolResults({call_id: ...}))`，把核实过的结果作为
      `tool_result`（`origin=reconciled`）写入；核实不了就提交一个 `error_kind=outcome_unknown` 的错误
      结果，让模型知道这一步状态不明。
  - harness 不替宿主保证恰好一次。
- **一个机制**（S2b，`loop._Run.settle` / `settle_turn`）：新的一轮和恢复走同一段代码——把最后一轮里还没有结果的
  call 按顺序逐个处理。需要等待的 call 不挡住同一轮里其它的 call（它们是模型并行发出的，互不依赖）；只要有一个
  在等，run 就写 `paused`、以 `outcome=paused` 结束，`RunResult.pause` 列出每个等待的 call 和原因。
  - 批准（`approve=True`）之后不再问 gate：人的决定代替 gate。`approve=False` → `error_kind=denied`。
  - **等审批是写在 log 里的**（`paused` 事件）：没有拿到决定的 call 一直等下去，恢复时不再问 gate，也不管恢复
    它的那个 `Agent` 现在配了哪些 gate。给一个 call 的决定不会连带放行同一轮里另一个在等的 call。
  - `Resume` 带来的决定和结果只作用于它要处理的那一轮：之后的轮次即使复用了同一个 call id（模型常这样做），
    也要重新过 gate。
  - **gate 出错按拒绝处理**：gate 抛异常，或者返回的不是 `Allow` / `Deny` / `PauseFor`，这个 call 以
    `error_kind=denied` 回给模型（内容写明是哪个 gate、什么类型的错，不带异常的文字），run 继续。没有得到许可
    就不执行。
  - 被打断的幂等 tool 重跑之前，先用现在的 `validate()` 检查当时存下的参数；不再合法就不重跑，转成
    `reconcile` 等人核对。
  - 校验先于 gate：参数不合法的 call 不会拿去问人。
  - 没有未完成的 call 时 `resume()` 只是重发上一轮（上一次模型调用失败了，§5 规则 9）。已经 `completed` 的 run
    （包括以 terminal tool 结束、历史停在 tool 结果上的）和历史以 assistant 的完整回复结尾的 session 没有东西可
    恢复，抛 `ValueError`。
  - 每次 `resume()` 是一次新的 run（新的 `run_started` 和快照，然后 `resumed`）；预算从 log 合计，不会重置。
- 对有未完成 call 的 session 调 `run(task)` 仍然抛 `UnresolvedCalls`（提示用 `resume()`）：在缺结果的历史上再加一条
  任务，API 会拒绝，而且可能已经执行过的 tool 不能盲目重跑。

## 9. Runner 与 Temporal（S7）

- `InProcessRunner`（默认）：loop 在调用方的 event loop 里跑完或暂停。
- **Temporal**（`lite-agent[temporal]`，temporalio 1.36.1；S7a）：`lite_agent.temporal`。

**粒度：一个 activity 是一次完整的 `Agent.run` / `resume`**（跑到结束或暂停，边跑边发心跳），不是"每次模型调用、每次
tool 执行各一个 activity"（这一节原来的方向）。理由：
- event log 已经让 loop 在事件粒度上可以接着跑：§3 的 `run_id` 让重试的 activity 不开第二个 run，§8 的规则处理死在
  tool 里的 call；
- 再把每一步拆成 activity，就有了两个编排者，各自记录进度（KB `llm-app-architecture` §7.1）。
模型调用的重试仍归 lite-model（同一个 target，Retry-After 由它处理）；activity 的重试只针对 worker 崩溃。

**`AgentWorkflow`**（`lite_agent.AgentWorkflow`）：
- 它只根据 activity 的返回值做决定（`Step`：outcome、stop、text、等待的 call 及其 tool 名和参数）；读写 log 只在
  activity 里，所以 workflow 代码是确定的。
- 每个 run 的 `run_id` 和新 session 的 `session_id` 来自 `workflow.uuid4()`（replay 时相同）。activity 重试时带着
  同一个 `run_id`，由 §3 的规则保证不开第二个 run。
- 暂停时：
  - 等宿主执行的 tool（reason `tool`），名字在 `routes` 里的 → 在那个 task queue 上调用同名 activity，结果作为
    `Resume.results` 交回（TypeScript 宿主就在自己的 worker 里实现这些 activity）；activity 失败 → 给模型一个只带类型名的错误结果；
  - 其余（审批、核对）→ 等 signal `decide(approve, results)`；一个 decision 只对这次暂停还在等的 call 有效（给已经答完的暂停、
    或已经由宿主 tool 解决的 call 的回答会被忽略）；resume 进行时到的回答，只为 resume 之后仍在等的同一批 call 保留；
  - 然后用新的 `run_id` 调 activity 做 resume。
- query `status`：上一个 run 的 `Step`（一个暂停的 session 在等什么）。
- **Continue-As-New**：在暂停处，这一段 workflow 已经执行了 `max_runs` 个 run（默认 50）、或 Temporal 建议时；暂停的
  `Step` 和已收到的 decision 带到下一段，下一段从 log 接着跑。从带过来的暂停开始的一段，在自己执行过 run 之前不再
  Continue-As-New（否则会一直接续下去：测试抓到过）。
- 重试策略：最多 5 次；`UnknownAgent`、`UnknownSession`、`UnresolvedCalls`、`ValueError` 不重试。超时可配：run 一小时、
  心跳两分钟、宿主 tool 十分钟。
- 沙箱：`lite_agent`、`lite_model`、`pydantic` 在沙箱外加载一次（`SANDBOX`），workflow 代码只让数据穿过它们。

**入口**：
- worker 进程：`make_worker(client, task_queue, registry)` 或 `liteagent worker --agents agents.toml --task-queue …`（§10 的 agent 登记）。
- 宿主：`TemporalRunner(client, task_queue)`：`start(agent, task, routes=…)` → workflow id、`decide(...)`、`status(...)`、`result(...)`。
  它没有实现 `Runner` 协议：一个 `Agent` 对象不能跨进程传过去，worker 里的 agent 按名字登记。

### 9.1 inbox 与 timer（S7b；KB `llm-app-architecture` §12.1–12.2）

常驻 agent：webhook、定时、人的回复都**只写进一个持久的 inbox**，从不直接调用 agent；一个 inbox 一个 worker。

- **表**（PostgreSQL 迁移 `0002_inbox.sql`，与 session 同一个 schema）：
  - `inbox_events`：每个事件一行，按 `(inbox, source, source_id)` 去重（来源投递两次只记一次）。kind 是 `message`
    （文本；带 `session_id` 就接着那个 session）或 `decision`（审批结论 / 外部结果，必须带 `session_id`）。状态 pending →
    done / refused，记下这次 run 用的 session。
  - `timers`：`follow_up`（agent 自己定的期限，触发一次）和 `schedule`（每 `every_s` 秒）。它们不是工作：到期时
    `fire_due(now)` 在同一个事务里写入 inbox 事件并推进 timer；一次触发的来源 id 是"timer id @ 到期时间"，所以只写一次；
    停机错过的多个周期只补触发一次，下一次是 now 之后的第一个时刻。
- **`PgInbox`**（`lite_agent.pg.inbox`）建在 `PgSessionStore` 上：`put`、`pending`、`finish`、`add_follow_up`、`add_schedule`、`fire_due`。
- **`InboxWorkflow`**（每个 inbox 一个，workflow id `inbox:<名字>`，由 Temporal 保证只有一个 worker）：取出待处理事件，
  按到达顺序处理——同一 session 的连续消息合成一次 run（几条快速消息得到一个回复），decision 走 resume；每个工作项用
  §9 的 run activity（`run_id` 来自 `workflow.uuid4()`，崩溃后的重试不重复），然后标记完成。需要审批的 run 暂停后就结束；
  结论作为 decision 事件进来再接着跑（KB §12.4）。agent 接不了的事件（session 并不在等 decision、或在等 resume 时又来了一条消息）
  标记 refused，不无限重试；**其余失败**（模型或数据库不在的时间比一项的重试还长）不丢工作：这一项留在 pending，worker 等一段
  （30 秒起，每次翻倍，最多一小时）后**原样**重试它——同样的事件、同样的 session id 和 run id，做了一半的 run 接着跑。没有事件
  但一项从第一次失败起超过 `max_retry_s`（默认一天）还在失败，就标成 **`failed`** 留给人看（毒消息不能永远挡住后面的事件；
  迁移 `0003` 加了这个状态）。没有事件
  就等 signal `wake`，最多等 `idle_check_s`（五分钟）也会自己看一眼；批次够多或 Temporal 建议时 Continue-As-New（工作在 inbox
  里，不在 history 里；待重试的一项带过去）。
- **`TimerWorkflow`**（全局一个，id `lite-agent-timers`）：每 `interval_s` 醒一次，用 workflow 的时间调 `fire_due`，对写到的
  inbox、以及有事件 pending 超过 `stalled_s`（一分钟）的 inbox 做 signal-with-start——写了行却没来得及唤醒就死掉的处理函数，
  它的事件由这里找回（每个事件都记着它的 agent）。它从不起 run。时间来自 workflow（`workflow.now()`），time-skipping 测试服务器可以快进它。
- **入口**：webhook 处理函数只调 `enqueue(client, inbox, task_queue, agent=…, name=…, source=…, source_id=…, text=… | approve=…)`：
  先写行，再 signal-with-start。`start_timers(client, task_queue)` 启动调度器（再启动一次就找到已在跑的那个）。
  `liteagent worker --agents … --inbox --timers`。
- 依赖：`lite_agent.temporal` 不带 `pg` extra 也能导入；用 inbox 时才需要 psycopg。


## 10. HTTP 壳（S6）

`liteagent serve --agents agents.toml [--host 127.0.0.1] [--port 8730]`（extra `server`：FastAPI 0.162.2、uvicorn
0.54.0，与 lite-mem 相同）。代码里是 `lite_agent.server.create_app(registry)`。它不含自己的逻辑：检查请求，然后交给
`Agent`（规则 10）。

**agent 登记**（`lite_agent.registry`，S7 的 worker 也用）：`agents.toml` 给每个名字指一个 Python 工厂
`模块:函数`，工厂收到共享的 session store、返回一个 `Agent`；`[store] kind` 是 `postgres`（读 `LITEAGENT_DATABASE_URL`
/ `LITEAGENT_SCHEMA`）或 `memory`。文件、导入、返回值、agent 不在共享 store 上，都在启动时报错（`RegistryError`）。
请求只能按名字选 agent，不能带 tool 或代码：请求体 `extra="forbid"`，多一个字段就是 422。

| 操作 | 行为 |
| --- | --- |
| `POST /runs` `{agent, task, session_id?}` | 在后台起一个 run，立刻 `202 {session_id, run_id}`（服务端生成两者，用 §3 的 `run_id` / `new_session`）；给了 `session_id` 而它在等 resume → 409 |
| `GET /sessions/{id}/events?after=N` | log，`events.dump()` 原样 |
| `GET /sessions/{id}/events/stream?after=N` | SSE（FastAPI 的 `EventSourceResponse`），每条事件 `event: <type>`、`id: <seq>`、`data: <dump>`；run 结束后发完剩下的就关闭 |
| `POST /sessions/{id}/resume` `{agent, expected_seq, approve?, results?}` | `expected_seq` 不是尾部 → 409；决定或结果给了一个不在等它的 call（包括已有结果的）→ 422，不覆盖；否则后台 resume，202 |
| `POST /sessions/{id}/fork` `{at_seq}` | `{session_id}`；fork 点不存在 → 422 |
| `GET /agents` | 名字、role、tool 的 schema |

- **同一 session 同时只有一个 run**：请求一通过检查就"占住"这个 session（检查和占住之间没有 await，两个同时到的请求
  不会都通过），run 结束才放开；第二个 → 409。这只是进程内的：跨进程时真正的保证是 store 的 `expected_seq`
  检查。服务端开始的 session 在第一批事件写下之前也能读（空），不存在的 session 仍是 404。
- **错误**：404 `UnknownSession` / 未知 agent，409 冲突，422 不合法；错误体不含上游原文（规则 14）。
- **只绑定 loopback**：`--host` 必须是数字形式的回环地址（`127.0.0.1`、`::1`）；名字（包括 `localhost`）可能解析
  到别处，一律拒绝。没有认证；要对外提供时再设计认证和多租户。
- **网页够不着它**：绑在 loopback 上，网页仍然可以把自己的域名重新解析到 127.0.0.1（DNS rebinding），或者直接从浏览器
  发请求。所以每个请求的 `Host` 必须是 loopback 名字（`127.0.0.1`、`::1`、`localhost`，带不带端口），否则 400；带了
  `Origin` 的必须**正好是壳自己的**（`http://` 加同一个 `Host`；本机别的端口上的页面也算外来的），否则 403；每个 POST 必须是
  `application/json`（表单或纯文本的 POST 不需要 CORS 预检，网页能盲发），否则 415。
- **关闭**：shutdown 时等还在跑的 run 结束（`drain`），不在一轮中间切断。
- TypeScript 宿主：FastAPI 生成的 `/openapi.json`；不做 TS 包。宿主提供 tool 的方式：MCP server（按需阶段）、外部执行
  （§7，暂停 → `resume` 带结果）、Temporal activity（§9）。

## 11. 配置

- 环境变量前缀 `LITEAGENT_`（pydantic-settings）；模型配置是 lite-model 的 `ModelsConfig`，本模块只声明
  `RoleSpec`：`agent`（宿主可以另起名字）、`compact`。
- 配置文件放数据：role 名、预算数值、skills 目录、store 的 URL、MCP server 列表。
- 初始化参数放代码对象：tool 函数、gate、自定义 store / compactor / memory。
- 启动时校验（role 存在、tool 名字唯一、schema 可导出、MCP server 可达性不在启动时检查），失败在启动时
  而不是第一次调用时暴露（KB §1.5）。

## 12. 测试与评测

- 单元测试用 lite-model 的 `AsyncFakeModel` 按轮脚本化（需要它支持 tool-call 轮，见 §13）。
  `tests/conftest.py` 拒绝一切 socket 连接。
- 每条 §4、§5 的规则至少一个测试，断言具体的事件序列，不只是"跑完了"。
- 回放测试：一条录下来的 log，投影结果逐字等于当时发出的 messages。
- `-m ollama`：真实本地模型跑一个小任务（免费，opt-in）。永远没有调用订阅或付费 API 的测试。
- 评测（S5）：lite-eval target，输入任务、输出 trajectory（event log）；指标至少有任务完成率、turn 数、
  token、无效 tool call 数。改 loop 规则或压缩策略前后各跑一次（"measure, don't guess"）。

## 13. 与其它项目的关系

| 项目 | 关系 |
| --- | --- |
| lite-context | **运行时依赖**（release wheel，2026-11-26 起，owner 决定；它的核心没有依赖）。压缩器的提问（`ASK`、`END`）、摘要外框（`SUMMARY`）和 transcript 的渲染（`render_transcript`）用它的，逐字是 S3b 的原文（lite-context 当初就是从这里抄过去的）；切点、`compaction` 事件、投影仍在这里（§6）。lite-context 的 `fit()` / masking 没有接（C7 的范围只到压缩） |
| lite-model | **运行时依赖**（release wheel，≥ 0.4.0）。用它的 `AsyncToolModel.turn(role, messages, tools) -> TurnReply`（lite-model 设计 §3.1，2026-11-21 实现）：有序的 `AssistantTurn.blocks`；`ToolResults`；被截断的一轮**返回** `stop="length"` 和它的 call（§5 规则 1 靠它）；参数不是合法 JSON 对象时 `ToolCall.arguments is None`（当作 `invalid_args`）；`Opaque` 的跨协议规则在 adapter 里；`AsyncFakeModel.turn` 脚本化轮次。`turn()` 不经过 lite-model 的 hook。lite-model 不处理 preserved thinking 的历史检查，这里的投影负责（§4）；那种 400 现在是 `CallFailed`，与别的失败分不开（lite-model §15） |
| lite-cache | v1 不接（预期 L1 几乎不命中：每一轮上下文都不同；没有测过）。S5 的评测显示有重复调用时再定。评测本身必须绕过缓存 |
| lite-mem | 可选 extra `mem`：`Memory` 的一个实现 |
| lite-rag | 可选 extra `rag`：一个 `search_kb` tool |
| lite-eval | `eval` extra 和 dev 依赖：trajectory 评测（§12） |
| llm-steward | 可能的宿主；它不被任何模块 import。agent 的 memory 写入是否要经过它的治理，见 §16 |
| llm-gateway | 只经 lite-model 的 HTTP 到达；测试永不调用订阅 profile |

兄弟模块都不 import lite-agent。workspace `AGENTS.md` 的仓库表、依赖图、pin 表和端口表在 S0 更新。

## 14. 项目结构

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

## 15. 阶段与验收

§4–§8 的每条规则都要对应到至少一个有名字的测试；阶段收尾时列出"规则 → 测试"的对照。

每个阶段收尾都按 AGENTS.md Workflow 第 4 步执行：`/simplify`，然后 Codex 只读 review，然后修复（否决的
写在本节的阶段记录里），最后重跑全部检查。

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P0（在 lite-model 仓库） | **完成，v0.4.0 于 2026-11-21 发版**：`turn()`（它的设计 §3.1、§14 阶段记录） | lite-model 的 `tests/contract/test_turns.py`（两种协议、同步和异步）和 `-m ollama` 的一个真实 tool 轮次；现有宿主的测试不变 |
| S0 | 骨架：`git init`、pyproject（uv、ruff、mypy strict、pytest）、CI、`.gitignore`（含 `!CLAUDE.md`）、`.env.example`、README、LICENSE；依赖 lite-model wheel；更新 workspace `AGENTS.md` | ruff、mypy、pytest 全绿；socket 被拒绝的测试存在 |
| S1 | 核心：事件与校验、`InMemorySessionStore`、`derive_messages`、loop（§5 规则 1、2、6、7）、`Agent.run`、`InProcessRunner`；§3 的 `Agent` / `SessionStore` / `Runner` 接口定稿 | 用 Fake 脚本化：两轮 tool 调用后完成，事件序列逐条相等；截断轮不执行任何 tool、每个 call 一条错误结果，并且下一个请求的投影包含这些结果、loop 继续到完成；混合 terminal / 普通结果时继续；每次请求前 "messages == 投影" 断言生效（故意破坏时失败）；带 usage 的失败调用（`Truncated` 等）计入合计，`CallFailed` 的 usage 记 `None`、不估算；请求里的 system 和 tools 等于 `run_started` 的快照；非法事件被拒绝且 log 不变；未知的事件词汇表版本被拒绝；每次 run 以 `run_started` 开头；模型回复拿到了但 `append` 失败时 run 失败、log 不变（用会失败的 store 注入） |
| S2 | tool 注册与 `@tool`、参数校验、结果截断、`Budget`、`Gate`、暂停 / 恢复（进程内）、auto-continue（规则 3）、steering 接口、外部执行的 tool、模型失败与 `fallback_role`（规则 9–11） | 超预算在调用前停；`Deny` 变成错误结果；`PauseFor` 后 `outcome=paused`，`resume` 后继续且已有结果的 call 不重复执行；auto-continue 至多 N 次；external tool 往返；`CallFailed` 时没配 fallback 则 `failed` 且 `resume` 重发这一轮、不重跑 tool；配了则事件序列是 `model_error` → `model_switched` → `model_turn`（provenance 是 fallback），之后各轮都用 fallback；fallback 也失败则 `failed`；`Truncated` 不触发切换；交给 lite-model 的 messages 等于投影（专有内容的丢弃在 P0 测）；`CallFailed`（usage 为 `None`）与带 usage 的错误混在一起时合计正确；故障注入：tool 执行后、写结果前崩溃——幂等 tool 用同一个 `call_id` 重跑，非幂等 tool 以 `reason=reconcile` 暂停且此时不写 `tool_result`；在 `model_error` 后、`model_switched` 后各崩溃一次，恢复都用主 role；参数不合法时 tool 没被调用（`executed=False`、没有 `tool_started`）；tool 超时是 `error_kind=timeout`，同一个 `call_id` 不再执行；切换后暂停再恢复，下一次调用用主 role；预算按 session 合计（暂停再恢复不重置 turn）；`reconcile` 暂停后提交的结果以 `origin=reconciled` 写入且该 call 只有一条结果；外部 tool 重复提交被拒；换了 tool 定义的 `Agent` 恢复时，新的 `run_started` 记下新定义，且那之前的 `Opaque` 不再进投影（system / tools 没变的 `resume` 则保留） |
| S3 | context：skills 索引 + `load_skill`、`Compactor` 默认实现（§6）、`Memory` Protocol（测试用内存实现） | 压缩后原事件仍在 log 里，投影用摘要替代该区间；投影只由 log 决定：去掉 `compaction` 事件就是完整的历史，换一条摘要或区间不同的 `compaction` 事件就得到不同的请求（2026-11-21 改写：原文"换一个 compactor 重新投影"不成立——摘要和区间存在事件里，compactor 不参与投影）；切点不拆开 tool call 和结果；skills 索引超预算报错；fork 之后父 session 再压缩，子 session 的投影不变；压缩之前的 `Opaque` 不在投影里，之后产生的在；同一轮的多个 `tool_result` 投影成一个 `ToolResults` |
| S4 | `PgSessionStore`（migrations，`LITEAGENT_TEST_DATABASE_URL`，schema `lite_agent_test`）、跨进程 resume / fork、`expected_seq` 冲突、CLI | 进程 A 暂停、进程 B 恢复；内存 store 产生的 session 导入 PG store 后能恢复（KB §10.3）；fork 不复制事件且互不影响；并发 append 一个成功一个被拒；一批事件的 append 全有或全无；fork 的 fork 里做一次覆盖区间跨过两个 fork 点的压缩，三层 session 各自的投影都符合预期 |
| S5 | lite-eval target；一个固定的小任务集，每个任务有写明的完成条件（由代码判定）；`-m ollama` 冒烟只跑其中一个用到 tool 的任务 | 指标（完成率、turn、token、无效 tool call）进报告；用 Fake 的确定性跑法进 CI；Ollama 的基线数字记入 KB |
| S5.5 | 发布 v0.1.0 release wheel | 新项目能从 wheel 安装并跑通 README 的例子 |
| S6（完成，v0.2.0） | HTTP 壳（§10）；先定 §10 末尾列的协议 | 原定"有一个 TypeScript 宿主要用时再做"，2026-11-21 先于宿主完成 |
| S7（完成，v0.2.0） | `TemporalRunner`、inbox / timer（§9）；先把 §9 写成完整设计 | 原定"有一个真实的 long-running 任务时再做"，2026-11-21 先于宿主完成；replay 测试、activity 重试不重复写事件、Retry-After、Continue-As-New |
| 按需（各自有宿主要用时） | MCP client（extra `mcp`，用进程内假 server 测）、lite-mem adapter、lite-rag tool | — |
| 按需 | streaming、tool 并行、follow-up 队列、subagent、文件型 session store、运行时注册表 | — |

### 阶段记录

- **设计 review（2026-11-20，Codex，只读，整份设计）**：3 Critical、7 High、6 Medium。
  - 采纳：tool 调用生命周期和崩溃恢复（`tool_started`、`call_id` 作幂等键、`outcome_unknown`，§8）；
    切换模型时用哪个 role 由 log 决定、各崩溃点的恢复（`run_started`，§5 规则 11）；投影后端无关、不变量
    比较规范表示、专有内容由 lite-model adapter 丢弃（§4、§13）；fork 的 seq 空间写明（§4）；Temporal 的
    确定性和 activity 重试（§9，且 S7 前要写完整设计）；P0 作为 lite-model 的正式阶段、验收补全、"四项"
    改"五项"（§15）；fallback 的上下文窗口行为（§5 规则 11）；HTTP 外部 tool 的协议要求（§10）；超时与
    "未执行"分开（`error_kind`、`executed`）；验收补故障注入和规则对照（§15）；参数校验失败的事件内容
    （§7）；默认压缩策略写明（§6）；能力改为构造时注入，不做运行时注册表（§3）；S5 用固定任务和完成条件；
    MCP / lite-mem / lite-rag adapter 移到"按需"；lite-cache 改为"v1 不接，测了再定"。
  - 否决："fork 之后父 session 压缩会改变子 session 的历史"——子 session 只读父 session 里
    `seq ≤ at_seq` 的事件，之后追加的 `compaction` 读不到；原文没写清楚，已写明并在 S3 加了测试。
  - 部分否决："v1 范围过大，S0–S2 之后等真实任务再定"——S6、S7 本来就是按需，adapter 已移走；但 S3
    （skills、压缩）、S4（持久化、跨进程恢复、fork）、S5（评测）保留：目标是验证这套架构（§0，owner），
    这三项正是要验证的部分。`fallback_role` 是 owner 的决定，保留。
- **设计 review 第二轮（2026-11-21，Codex，只读，针对修订后的全文）**：5 High、8 Medium、2 Low，全部采纳。
  - system 和 tool 定义进 `run_started` 快照，不变量包含 tools；快照从"每轮"收紧到"每个 run"（§4、§5
    规则 4），同时回答了"换了配置的 `Agent` 恢复时以谁为准"。
  - `outcome_unknown` 的出路：暂停时不写结果，核对后由宿主提交（`reason=reconcile`、`origin=reconciled`，§8）。
  - 外部 tool 的完整事件顺序和丢失提交的处理（§7）；超时后重试 = 新的 call，同一个 `call_id` 不重跑（§5 规则 10）。
  - 预算范围：turn / token / 成本按 session，`auto_continues` 按 run（§5 规则 6）。
  - 切换后暂停再恢复 = 新 run、主 role，写明是有意的（§5 规则 11）。
  - 压缩：不给就不压缩；`SummaryCompactor` 的阈值配置和缺失时的报错；成功 / 失败的事件、怎么计预算（§6）。
  - `append` 的原子性和"回复已拿到但没写进 log"的行为（§4）；事件表补全各字段。
  - 验收补：截断后继续、`CallFailed` 的 usage、切换后暂停恢复、fork 的 fork 加跨 fork 点的压缩（§15）。
- **S0（2026-11-21）**：`git init`（当时只在本地；S1 之后 owner 决定公开）、pyproject（lite-model v0.4.0 以直接 URL 写进
  元数据，与 lite-cache 相同）、ruff / mypy strict / pytest + pytest-asyncio、CI 文件（test / package / release；
  仓库公开后第一次在 GitHub 上运行，通过）、socket 守卫、`scripts/smoke.py`、README、LICENSE、`.env.example`；workspace `AGENTS.md`
  已更新。检查：ruff、ruff format、mypy 干净，pytest 4 passed（含 review 后加的一条）；最初的三个测试各见过一次红（去掉 socket 守卫；把元数据里
  的 lite-model URL 换成裸名字；对着 lite-model 0.3.0 跑 tool 轮次的测试是 ImportError）；本地按 CI `package` job 的
  步骤从 wheel 装进干净的 venv 并跑通 smoke。没有跑 `/simplify`：这一阶段没有产品代码。
  Codex 只读 review（整个工作区）：2 条，都采纳——socket 守卫补上 UDP `sendto` 和 `getaddrinfo`（回归测试先红）；
  README 开头改成"目标"而不是现状。CI、打包、`.gitignore` 它判为干净。
- **S1（2026-11-21）**：`events` / `store` / `projection` / `tools` / `budget` / `loop` / `runner` / `agent` /
  `result` / `errors`。检查：ruff、ruff format、mypy（strict）干净；pytest 全绿。
  - 与 plan 的出入：没有 `ToolOutput`（tool 直接返回 `str`）；`RunResult` / `RunError` 放在 `result.py`
    （避免 `agent` ↔ `loop` 循环导入）；多一个 `stop=truncated`（§5 规则 1）；`session_started` 不带 fork 字段（§4）。
  - 见红的方式：这一阶段的实现写在测试之前（偏离了 TDD），所以用逐行破坏来确认——40 处里 39 处有测试变红；剩下
    1 处（`Agent` 把 `tools` 复制成 tuple）补了测试后变红。另有 1 处破坏写成了等价改动，不算。
  - 规则 → 测试（`tests/unit/`）：

    | 规则 | 测试 |
    | --- | --- |
    | 基本流程、事件逐条相等 | `test_loop.py::test_two_tool_rounds_then_an_answer_writes_exactly_these_events` |
    | 请求 = 快照 + 历史 | `test_loop.py::test_each_request_is_the_runs_snapshot_and_the_history_so_far` |
    | §5 规则 1 | `test_loop.py::test_a_truncated_turn_runs_no_tool_answers_every_call_and_the_loop_goes_on`、`…::test_cut_off_text_with_no_calls_ends_the_run_as_truncated` |
    | §5 规则 2 | `test_loop.py::test_a_batch_of_only_terminal_results_ends_the_run`、`…::test_a_terminal_result_next_to_a_plain_one_does_not_end_the_run`、`…::test_a_terminal_tool_that_fails_does_not_end_the_run` |
    | §5 规则 6 | `test_loop.py::test_the_turn_budget_stops_before_the_call_that_would_exceed_it`、`…::test_the_turn_budget_is_the_sessions_not_the_runs`、`…::test_usage_is_summed_over_the_runs_calls_and_recorded`、`…::test_a_failed_call_that_was_billed_counts_and_one_that_never_answered_does_not` |
    | §5 规则 7 | `test_loop.py::test_a_tool_exception_goes_back_to_the_model_and_the_run_continues`、`…::test_an_unknown_tool_or_unparsed_arguments_is_answered_without_running_anything` |
    | §5 规则 9（无 fallback） | `test_loop.py::test_a_failed_model_call_ends_the_run_as_failed_and_is_in_the_log`、`…::test_running_again_after_a_failure_resends_the_turn_without_rerunning_tools` |
    | §4 写入校验、原子性 | `test_store.py::test_a_batch_with_one_bad_event_is_refused_whole`、`…::test_a_stale_expected_seq_is_refused_and_nothing_is_written`、`test_events.py::test_an_invalid_event_cannot_be_built` |
    | §4 版本、JSON 往返 | `test_events.py::test_an_event_this_code_cannot_vouch_for_is_refused_on_load`、`…::test_every_event_survives_a_json_round_trip` |
    | §4 投影 | `test_projection.py`（一轮一个 `ToolResults`、按 call 顺序、最近的快照、纯函数） |
    | §4 `Opaque` | `test_projection.py::test_opaque_blocks_from_before_a_changed_snapshot_are_left_out`、`test_loop.py::test_a_changed_system_prompt_drops_the_earlier_thinking_from_the_request` |
    | §4 不变量 | `test_loop.py::test_a_store_that_lost_an_event_stops_the_run_before_the_next_request` |
    | §4 回复没写进 log | `test_loop.py::test_a_reply_the_store_cannot_take_fails_the_run_and_leaves_the_log_as_it_was` |
    | fork 的读语义（完整验收在 S4） | `test_store.py::test_a_fork_reads_the_parents_prefix_and_then_its_own_events` |
  - `/simplify`（四路）：采纳——store 的契约检查抽成 `check_batch()`，以后每个 store 调同一个函数；`EVENT_TYPES`
    从 `Event` 联合里取，不再手写第二份；loop 里 tool 结果只在 `_answer()` 一处构造，`execute()` 返回是否 terminal，
    截断分支拉平；`ToolResultEvent` 加一致性校验（`error_kind` 与 `is_error`、`terminal` 与 `executed`）；store 的
    读写只在 `_Run.read` / `append` 两处，第一次读失败也变成 `store` 错误（原来会原样抛出，回归测试先红）；
    `provenance_data` / `usage_data` / `ToolDef` 用 `from_attributes`；测试里自写的 model double 换成
    lite-model 的 fake 加 usage（`Billed`）。留到以后——不变量检查只读尾部（要给 `SessionStore.read` 加参数，S4 前
    定）；`Runner` 的输入形状（S2 加 `resume` 时一起定）；把 lite-model 与事件的转换移出 `events.py`（S3）；测试夹具
    去重。没采纳——`turns_used()` 每轮扫 log、`Agent.tool()` 线性查找（量级很小）。
  - Codex 只读 review：5 High、6 Medium、3 Low。修复（回归测试先红）——上一次 run 被打断后再 `run()` 会把缺少
    tool 结果的历史发给模型：现在抛 `UnresolvedCalls`（§8，覆盖了它的前四条 High：崩溃、取消、没有
    `run_finished` 的 run、缺结果的历史）；`load()` 接受 `v=True`；fork 的新 id 与已有 session 相同时会改掉它的
    血缘。写明——`run_sync()` 在运行中的 event loop 里是 `RuntimeError`；`Opaque` 规则在 A→B→A 时有意保守（加了
    测试）。否决——"usage 没有跨 run 累加"（`RunResult.usage` 按设计是本次 run 的；token / 成本预算在 S2）；
    "`ToolResultEvent` 缺 `origin` / `timeout` 等"（S1 的词汇表有意是子集，§4 已写明）；"嵌套的 dict 可变"（只冻结
    一层，与 lite-model 相同）；"tool 返回非 str 不该当作 tool 错误"（它是 tool 的失败，按 §5 规则 7 回给模型）；
    "usage 合计掩盖了未知的部分"（§5 规则 6：未知不估算、不计入）；"`derive()` 应校验任意 log 的结构"（log 只由
    loop 写，能到达的坏状态由 `UnresolvedCalls` 拦住）；非 JSON 值的往返测试（参数来自 JSON 解析，opaque 来自 SDK
    的 `to_dict()`）。
- **S2a（2026-11-21）**：`@tool` / `ModelTool`、`Tool.validate` / `idempotent` / `timeout_s` / `external`、结果截断、
  tool 超时、token 和成本预算（`budget.spent()` 从 log 合计）。测试先写、先红（`tests/unit/test_tools.py`）。
  - 规则 → 测试：校验（§7）`test_invalid_arguments_are_answered_before_anything_is_started_or_run`、
    `test_validate_says_what_is_wrong_without_echoing_the_input`；截断（§4）
    `test_a_long_result_is_cut_when_written_and_the_original_length_is_kept`；超时（§5 规则 10）
    `test_a_tool_that_overruns_its_timeout_is_answered_as_a_timeout_and_the_run_goes_on`、
    `test_a_tool_raising_timeout_error_itself_is_an_ordinary_failure`；预算（§5 规则 6）
    `test_the_token_budget_stops_before_the_call_after_it_is_reached`、`test_the_cost_budget_…`、
    `test_token_and_cost_budgets_are_the_sessions_and_count_failed_calls`、
    `test_a_call_whose_usage_is_unknown_is_not_counted_against_the_token_budget`。
  - `/simplify`（两路，四个角度）：采纳——`_answer` 并进 `_Run.answer`；参数检查改成三个提前返回（`refuse()`）；
    超时和异常两个分支只选值、写一次；`timeout_s` 用 `is None` 判断并在构造时校验；测试的 `results_of`、慢 tool
    并进 `tests/helpers.py`；`external` 的 tool 在 S2b 之前构造 `Agent` 时直接拒绝（否则会被当成普通 tool 在进程内
    执行）。留到 S2b——`execute()` 拆成"检查"和"执行并回答"两步、返回三种结果（暂停要用）；`RunFinished.usage`
    改成从本次 run 的事件合计。不改——`FunctionTool` / `ModelTool` 的重复字段、`spent()` 每轮扫一遍 log、参数校验
    两次（`__call__` 自己再校验一次，单独调用时才安全）。
  - Codex 只读 review：1 High、5 Medium、2 Low。修复（回归测试先红）——吞掉取消信号后照常返回的 tool 会把超时
    记成成功（High）；`@tool` 接受同步函数和参数个数不对的函数，到第一次调用才出错；预算和超时接受 NaN /
    无穷大；README 的例子补全成能运行的脚本、状态改成 S2a。写明——截断的预算不含说明那一行；校验错误里宿主自己
    写的消息原样返回。否决——"校验两次可能有副作用"（validator 有副作用是宿主的问题，见上）；"布尔值能通过整数
    预算的检查"（Python 里 `True` 就是 1，不值得单独拦）。README 的例子原样在本机 Ollama 的 qwen3:8b 上跑通（2026-11-21）：模型调了 tool，再用结果回答，
    log 是 `session_started` 到 `run_finished` 的 8 条事件。这是 lite-agent 第一次对着真实模型跑完整个 loop。
- **S2b（2026-11-21）**：`gates.py`（`Gate`、`Allow` / `Deny` / `PauseFor`、`RunContext`）、`Agent.gates` /
  `resume()`、`Resume` / `ToolOutcome` / `Pause` / `Waiting`、事件 `paused` / `resumed`、`tool_started.external`、
  `tool_result.origin`；`loop` 里 `execute()` 拆成 `settle()`（检查、gate、开始）和 `run_tool()`（执行并回答），
  `settle_turn()` 给新的一轮和恢复共用。测试先写（`tests/unit/test_pause_resume.py`）；实现是一次写完的，所以另用
  逐行破坏确认：24 处里 22 处变红，剩下 2 处（恢复时外部 call 的等待原因、宿主报告的 terminal 结果）补了测试后变红。
  - 与 plan 的出入：`Pause` 是 `waiting: tuple[Waiting, ...]` 而不是 `(reason, call_ids)`——同一轮里可以同时有等
    审批的和等宿主执行的，一个原因装不下，而且审批的人需要看到 gate 给的说明；暂停的 `stop` 叫 `paused` 不叫
    `gate`；外部 tool 的结果可以分几次交（§7）；S2a 的"拒绝 external tool"和它的测试按计划去掉了。
  - 规则 → 测试：gate（§8）`test_a_denied_call_is_answered_without_running_and_the_run_goes_on`、
    `test_gates_are_asked_in_order_and_the_first_refusal_wins`、`test_invalid_arguments_are_refused_before_any_gate_is_asked`；
    审批 `test_a_gate_that_wants_a_person_ends_the_run_as_paused`、`test_an_approved_call_runs_once_and_the_run_completes`、
    `test_a_rejected_call_is_answered_as_denied_and_never_runs`、`test_the_rest_of_a_batch_runs_while_one_call_waits`；
    外部 tool（§7）`test_an_external_tool_pauses_the_run_and_its_result_comes_back_on_resume`、
    `test_external_calls_of_one_turn_pause_together_and_may_come_back_in_parts`、
    `test_a_result_submitted_twice_is_refused_and_nothing_is_written`；被打断的 call（§8）
    `test_an_idempotent_tool_is_run_again_with_the_same_call_id`、
    `test_a_tool_that_may_have_run_is_not_repeated_and_the_run_waits_to_be_reconciled`、
    `test_an_outcome_nobody_can_verify_is_told_to_the_model_as_unknown`、
    `test_a_call_that_was_never_started_simply_runs_on_resume`；`resume()` 的边界
    `test_resume_after_a_failed_model_call_sends_the_turn_again`、
    `test_resume_refuses_what_it_cannot_apply_and_writes_nothing`、
    `test_pausing_and_resuming_does_not_grant_a_fresh_turn_budget`、
    `test_resuming_under_changed_tools_records_them_and_drops_earlier_thinking`。
  - `/simplify`（两路）：采纳——"最后一轮和它之后的事件"抽成 `projection.last_turn()`（原来写了三遍）；`settle()`
    拆成 `recover()`（已开始的 call）和 `admit()`（还没动的 call）；`Resume` 输入改成 `settle_turn()` 的参数，只作用
    于它要处理的那一轮——**这是个真问题**：原来存在 run 上，后面轮次复用同一个 call id 时旧的批准会让它跳过 gate
    （回归测试先红）；`Waiting` 只定义一次，`paused` 事件里存带字段名的记录（原来是无名数组），并记下是哪个 gate；
    `Origin` 起别名；`FailingOnce` 挪进 `tests/helpers.py`；`run_loop()` 里的类型判断收成一次。留着——
    `ToolOutcome` 用两个布尔表示三种状态（接口已批准，文档里写明 `unknown` 优先）；每个 call 建一次
    `RunContext`（后一个 call 的 gate 要看到前一个 call 的结果）。备忘——S7 的 Temporal runner：暂停即 workflow
    结束、resume 起新的 workflow，与现在"每次 resume 是一次新 run"一致。
  - Codex 只读 review：3 High、5 Medium、1 Low。修复（回归测试先红）——以 terminal tool 结束的 session 还能
    `resume()` 并再调一次模型；幂等 tool 重跑时不校验存下的参数；同一轮里重复的 call id 会让结果张冠李戴；gate
    返回的不是决定时被当成放行；gate 抛异常会让 run 停在没有 `run_finished` 的状态；只批准一个 call 时，另一个在
    等审批的 call 可能因为 gate 换了而被执行（现在等待状态取自 log）。否决——"store 提交了但调用方看到失败时
    重交结果会被拒"（这是 store 的原子性契约，`check_batch` 之外不在 loop 里兜底）。不改——宿主交回的结果同样按
    预算截断（log 里存的就是模型看到的，§4）。没有跑第二轮 Codex。
- **S2c（2026-11-21）**：`Agent.done`（auto-continue）、被截断的纯文本续写、`Agent.fallback_role`、事件
  `model_switched`、`user_message.origin=auto_continue`、`stop=incomplete`、`RunResult.switched`。本次 run 做过什么
  （是否已切换、已经续了几次）都从 log 里读：本次 run 的 `run_started` 之后的事件。不靠 `run_id`，所以宿主的 id
  工厂重复给出同一个 run id 也不会串。测试先写（`tests/unit/test_continue_fallback.py`）；另做逐行
  破坏 16 处，15 处变红，剩下 1 处（`model_error` 和 `model_switched` 是否同一批写入）不改变可观察的行为。
  - 改了一条已有测试：`test_cut_off_text_with_no_calls_ends_the_run_as_truncated` 测的是 S1"截断的纯文本直接结束"，
    S2c 按计划改成先续写，这条测试现在显式用 `auto_continues=0` 来测"不续写时的结局"。
  - steering（§5 规则 5）按计划移到"按需"，随 S7 设计。
  - 规则 → 测试：规则 3 `test_an_unfinished_task_is_sent_back_with_what_is_still_open`、
    `test_automatic_continuation_stops_after_the_budgets_count`、`test_each_run_gets_its_own_continuations`、
    `test_text_cut_off_at_the_limit_is_asked_to_continue`、`test_cut_off_text_and_unfinished_tasks_share_one_allowance`；
    规则 9、11 `test_a_failed_call_switches_to_the_fallback_role_once_and_says_so`、
    `test_a_fallback_that_fails_too_ends_the_run_without_a_second_switch`、
    `test_only_an_unreachable_model_triggers_the_switch`、`test_the_next_run_starts_on_the_primary_role_again`、
    `test_a_resume_after_a_switched_run_paused_starts_on_the_primary_role`、
    `test_a_run_interrupted_around_a_switch_resumes_on_the_primary_role`、
    `test_the_failed_call_and_the_fallbacks_calls_all_count_against_the_budget`。
  - S2b 合并后的安全检查（自动）：`resume()` 的批准可以用在从没被 gate 拦过的 call 上，从而绕过 gate。成立，已修：
    只接受对 log 里记录为在等审批的 call 的批准（`test_an_approval_is_only_taken_for_a_call_a_gate_left_waiting`，
    先红后绿）。
  - `/simplify`（一路，四个角度）：采纳——纯文本分支改成一个"还剩什么"的值加三个出口；`RunResult` 的后两个字段
    用关键字传；`done` 返回空或空白等同于完成（原来会发一条空消息给模型，回归测试先红）。留到 S7——`loop()` 按
    activity 的边界再拆；`done` 是同步的宿主代码，要做 I/O 时需要一个 activity 的位置。
  - Codex 只读 review：1 High、4 Medium、1 Low，另有测试缺口。修复（回归测试先红）——本次 run 的状态按 `run_id`
    划分，宿主重复使用 run id 时上一次 run 的切换和续跑次数会串到下一次（High；改成按位置）；纯文本的轮次和它
    后面的那条事件（续跑消息或 `run_finished`）分两次写，中间崩溃会留下一个看起来已结束、其实没做完、又无法恢复
    的 session（改成同一批写入，`_Run.stamp` / `write`）；`done` 返回非字符串时在写入任何事件之前抛 `TypeError`；
    `run_started` 的快照补上 `fallback_role`（设计早就这么写了）。补测试——切换之后没有预算再调 fallback。否决——
    "续跑次数用完后可以靠反复 `resume()` 无限续"：它假设 log 以续跑消息结尾，实际用完时写的是 `model_turn` 和
    `run_finished`，`resume()` 会拒绝（"nothing to resume"）；要继续只能 `run()` 一个新任务，而 turn / token 预算
    是按 session 算的。没有跑第二轮 Codex。
- **S3a（2026-11-21）**：`context.py`（`SkillSource` / `SkillDir` / `SkillInfo`、`Memory` / `NotesMemory`、
  `build()`、三个内置 tool）、`Agent.skills` / `memory` / `system_prompt()` / `all_tools`、`Budget.skills_index_chars`。测试先写
  （`tests/unit/test_skills_memory.py`）；实现一次写完后做逐行破坏：17 处里 15 处变红，剩下 2 处（frontmatter 不在
  第一行、宿主没给 system 时开头多出空行）补了测试后变红。
  - 真实模型：本机 Ollama 的 qwen3:8b，给一个 skills 目录和一个 `NotesMemory`，问"先跑哪条命令"并要求记一条
    笔记。模型自己调了 `load_skill("deploy")`，答出了只在 skill 正文里出现的命令，也调了 `remember`。
  - 规则 → 测试：索引进 system 且与 log 一致 `test_the_skill_index_is_in_the_system_prompt_as_logged`；组装稳定
    `test_the_assembled_prompt_is_the_same_every_time`；按需加载 `test_load_skill_returns_the_body_as_a_tool_result`、
    `test_loading_an_unknown_skill_is_a_tool_error_that_names_the_real_ones`；预算
    `test_an_index_over_its_budget_is_refused_when_the_agent_is_built`；坏的 skill
    `test_a_malformed_skill_is_refused_when_the_directory_is_read`；memory
    `test_remember_and_recall_go_through_the_memory_and_the_log`、
    `test_the_memory_prompt_says_what_memory_is_for_and_scripts_nothing`、
    `test_a_gate_can_refuse_remember_like_any_other_tool`；不给时不变
    `test_without_skills_or_memory_the_prompt_and_tools_are_the_hosts_own`。
  - `/simplify`（一路，四个角度）：采纳——内置 tool 原来写回 `Agent.tools`，`dataclasses.replace()` 一个带 memory 的
    agent 会因为"重名"失败（回归测试先红）：现在 `tools` 保持宿主给的，合并后的在 `all_tools`；预算检查的是构造时的
    索引、每次 run 却重新读来源，来源变大后会发出没检查过的索引（回归测试先红）：现在 prompt 在构造时定下来；
    `_parse` 不再用异常做流程控制；拼装和内置 tool 收进 `context.build()`。
  - Codex 只读 review：7 Medium、2 Low。修复（回归测试先红）——frontmatter 里重复的键被静默覆盖；带 BOM 的
    `SKILL.md` 读不了；索引的顺序取决于来源给的顺序（现在按名字排序）；`NotesMemory` 只去掉句号，带逗号的词查
    不到；只有空白的笔记能存进去。写明——skill 文件按可信内容对待；预算只算条目；`Memory` 的异常消息会回给模型。
    否决——"符号链接可以读到根目录之外"（宿主自己的目录）；"恢复被打断的 `load_skill` 时 skill 可能已经变了"
    （它读的是现在的内容，这就是按需加载的意思）；"稳定性测试比的是一个空目录"（两个 agent 用的是同一个目录）。
    补测试——被打断的 `remember` 等人核对、不重跑。
- **S3b（2026-11-21）**：`compaction.py`（`Compactor`、`SummaryCompactor`、`estimate_tokens`、`render`）、事件
  `compaction`、`model_error.purpose`、`projection.history()`（投影里的遮盖和 `Opaque` 规则的另一半）、
  `budget.spent()` 计入摘要调用、`loop._Run.compact()`、`Agent.compaction` / `compact_role`。测试先写
  （`tests/unit/test_compaction.py`、`test_projection.py` 的两条）；其中三条的剧本排错了（不是实现的问题），改正
  后另做逐行破坏：22 处里 20 处变红，剩下 2 处（已被覆盖的轮次不再算数、tool 定义计入估算）补了测试后变红。
  - 真实模型：**没跑成**。准备用本机 Ollama 跑一个多轮 session 验证摘要能保住前面的事实，但那时 Ollama 已经
    不在运行（端口上没有进程），所有调用都是 `CallFailed`；按规矩没有自己启动它。S3a 那次真实运行不涉及压缩。
  - 规则 → 测试：遮盖而不改写 `test_compaction_writes_one_event_and_the_projection_shows_the_summary_instead`、
    `test_every_covered_event_is_still_in_the_log`、`test_the_summary_text_in_the_event_is_what_the_model_is_shown`；
    后一次吃进前一次 `test_a_later_compaction_takes_in_the_earlier_summary`、`test_only_the_latest_compaction_counts`；
    切点 `test_a_turn_and_its_tool_results_are_never_split`、
    `test_nothing_is_cut_while_there_are_no_more_turns_than_it_keeps`、
    `test_turns_a_summary_already_covers_do_not_count_towards_the_next_cut`；阈值
    `test_compaction_waits_until_the_estimate_passes_the_roles_threshold`、`test_the_threshold_is_the_role_being_called`、
    `test_every_role_the_agent_may_call_needs_a_threshold`；预算
    `test_a_compaction_call_counts_tokens_and_cost_but_not_a_turn`、`test_the_token_budget_includes_what_compaction_spent`；
    失败 `test_a_failed_compaction_fails_the_run_and_does_not_switch_models`；`Opaque`
    `test_a_compaction_shows_its_summary_in_place_of_what_it_covers`；fork
    `test_a_parents_later_compaction_does_not_change_a_fork`、`test_a_fork_can_compact_across_the_point_it_forked_at`。
  - `/simplify`（一路，四个角度）：采纳——失败的模型调用在一处记账（`_Run.failure()`，主调用和摘要调用共用）；
    tool 参数的文本化只写一遍（`render` 原来会把从没解析成功的参数写成字面量 `None`）；"哪一条压缩算数"只定义
    一次（`projection.latest_compaction()`）；摘要之后再查一次预算（回归测试先红）；测试不再伸手改 lite-model
    fake 的私有属性。留到 S5——`Compactor` 的四个方法其实是两件事（何时切、怎么问），等有第二个实现再定；
    `covers` 的下界现在总是同一个 seq，链式区间等有测量再考虑；估算可以用实际的 `usage.input` 校准。
  - Codex 只读 review：1 High、3 Medium、1 Low。修复（回归测试先红）——被输出上限截断的摘要被当成完整摘要
    用了；`render` 把一轮里的文字全排在 tool 调用前面，打乱了先后。改写——S3 的验收里"换一个 compactor 重新
    投影"那一条（见 §15 的表）；一条恒真的断言换成了真正的检查。否决——"摘要调用成功但 `compaction` 事件没写
    进去时，这次花费不在 log 里"（High）：属实，但和普通轮次"回复拿到了、`append` 失败"是同一个已写明的取舍
    （§4：回复丢掉，钱已经花了，log 里没有）；要补上得在每次模型调用之前先写一条"意图"事件，那是另一个量级
    的改动，记在 §16。
  - S3b 合并后的安全检查（自动）：交给摘要模型的历史是纯文本，tool 结果里伪造的 `[user]` 可能被当成用户的话
    写进摘要，而摘要以 user 消息出现——tool 内容借此升格成用户的要求。成立，已修（`fix/compaction-trust`）：每段
    带随机 token 的标题、摘要模型的说明里写明谁的话算请求、摘要前言写明"不是新的请求"。回归测试先红
    （`test_a_tool_result_cannot_imitate_a_user_turn_in_the_transcript` 等 5 条）；逐行破坏 5 处全部变红。
  - 真实模型（本机 Ollama 的 qwen3:8b，2026-11-21）：先说"我的服务器叫 heron-7，端口 8431"，再问三个无关问题让
    历史被压缩两次，最后问服务器名和端口。答对 = 答案里同时有两者。
    | 条件 | 版本 | 答对 | 最后一份摘要保住事实 | 摘要写成了"接着聊天" |
    | --- | --- | --- | --- | --- |
    | 关掉思考 | 修复前 | 0/6 | 0/6 | 6/6 |
    | 关掉思考 | 末尾再写一遍指令（库里的代码） | 5/6 | 6/6 | 0/6 |
    | 关掉思考 | 末尾指令 + 强调保留事实 | 5/6 | 5/6 | 0/6 |
    | 开着思考 | 修复前 | 1/3 | 1/3 | 0/3 |
    | 开着思考 | 末尾再写一遍指令 | 1/3 | 2/3 | 0/3 |
    结论：修复针对的失败（摘要写成了聊天）在关掉思考时彻底消失；开着思考时它本来就不出现，修复没有坏处。
    "强调保留事实"没有额外帮助，没采纳。开着思考时 qwen3:8b 仍然只答对 1/3：一部分是"摘要的摘要"丢了事实，
    一部分是摘要保住了事实而主模型没用上；3 次的样本说明不了更多，留给 S5 的评测。修复后摘要 6/6 抄回了标题行，
    所以改成在库里去掉（`fix/compaction-prompt`）。Codex 只读 review：2 条，都采纳——标题的大小写、多余空格、
    `\r`、缩进都要认（回归测试先红）；空摘要的测试补上用量和不切换的断言（破坏用量记录确认它会失败）。
- **S4a（2026-11-21）**：`PgSessionStore`（extra `pg`，`psycopg[binary,pool]==3.3.8`，与兄弟模块相同）、迁移
  `0001_initial.sql`（`sessions` 带 fork 的血缘、`events` 只追加）、迁移运行器（vendor 自 lite-cache `db.py` @
  6afa032）、`config.Settings`、`SessionStore.read(after_seq=…)`、不变量检查只读尾部（§4）。测试先写；一套契约测试
  同时跑两种 store（`tests/contract/test_store_contract.py`），loop 的关键路径（tool、暂停、恢复、压缩、fork、预算、
  store 丢事件）也在两种 store 上各跑一遍（`test_loop_on_stores.py`）。本机建了库和角色 `lite_agent`，口令只在
  gitignored 的 `.env`；CI 加了 PostgreSQL 服务。没有 `LITEAGENT_TEST_DATABASE_URL` 时 PG 测试报错，不跳过。
  - 与 plan 的出入：内存 store 的空批次原来会凭空建一个空 session，改成什么都不写（两种 store 一致，契约测试
    覆盖）；socket 守卫对请求了测试数据库的测试放行，其它测试照旧不能联网。
  - 规则 → 测试：原子批次 `test_a_batch_with_one_bad_event_is_refused_whole[postgres]`；并发
    `test_two_writers_at_the_same_tail_one_wins_one_is_refused`、`test_two_writers_creating_one_session_one_wins`；只追加
    `test_the_database_refuses_to_rewrite_events`；fork `test_a_fork_reads_the_parents_prefix_and_then_its_own_events`、
    `test_a_fork_of_a_fork_reads_along_the_chain`、`test_a_fork_of_a_fork_taken_below_the_first_fork_point`；读回时校验
    `test_an_event_this_code_cannot_vouch_for_is_refused_when_read`；尾部检查
    `test_before_each_request_only_what_was_written_since_the_last_check_is_read`、
    `test_a_store_that_changed_an_event_since_it_was_written_stops_the_run`（两条在旧的全量检查下都会失败）。
  - 收尾 `/simplify`：采纳——schema 名用 `TypeAdapter(OwnSchema)` 校验（不再为此构造一个读 `.env` 的 `Settings`）；
    SQL 在构造时组好一次；锁行和取尾部合成一条查询；`read` 跳过整段都在 `after_seq` 之前的祖先；`connection_error`
    接受任何异常；删掉没人用的 `kind` fixture。它顺带发现 `read(upto_seq=0)` 在 PG store 读出全部（真值判断），
    先写契约测试 `test_reading_up_to_seq_zero_reads_nothing` 见红再修。记下不做：一条递归 CTE 读整条血缘、
    `projection` 里用下标代替过滤（留给 S5 的测量）；错误信息里的 `liteagent migrate` 改为 `lite_agent.pg.migrate`，
    S4b 有了 CLI 已改回。
  - **S4b 要先定的事（simplify 提出）**：继承来的事件保留原 session 的 `session_id`，`fork()` 的新 id 由 store 生成；
    所以 `copy_session` 只能把一个 fork 摊平成独立 session（事件改写 `session_id`），不能在目标 store 里重建血缘。
    S4b 的 plan 里写明。
  - 收尾 Codex（只读）：采纳——`close()` 之后每个调用都报 `StoreError`、不再重开
    （`test_a_closed_store_says_so_and_does_not_reopen`）；打开连接池和探测表的整个过程都只给出脱敏后的错误；
    `redact_conninfo` 隐去所有 `*password*` 参数（`sslpassword` 原来会原样显示，
    `test_every_secret_in_a_connection_string_is_hidden`）；未迁移 schema 的测试在 `finally` 里关池；空批次的契约
    补上 `expected_seq` 非 0 和过期的情形（两种 store 原本就对，临时去掉内存 store 的检查见红）。否决：
    - "`migrate` 信任 schema 里已有的 `schema_migrations`"：schema 是 lite-agent 自己的（`own_schema` 拒绝共享和系统
      schema），别的工具在里面建了同名表属于配置错误；打开 store 时还会检查 `events` 表在不在。
    - "`pending()` 不报未知版本"：与 lite-cache、lite-rag 的 vendor 原件一致，改它要三处一起改；记进 §16。
    - "`read` 跨段没有一致的快照"：祖先段都封顶在 fork 点，事件只追加，那一段不会再变；只有最后一段（自己的）
      会增长，并发写入只会让结果多出一个完整的批次（批次是原子的），返回的总是某个时刻的完整前缀。
    - "socket 守卫放行范围太宽"：放行的只是请求了测试数据库 fixture 的测试，URL 只来自显式设置的
      `LITEAGENT_TEST_DATABASE_URL`；这是测试代码，不是生产路径。
- **S4b（2026-11-21）**：`copy_session`、`liteagent` CLI（`migrate`、`sessions`、`show [--json]`、`fork --at`、`config`）、
  `PgSessionStore.sessions()` 和 `from_settings()`。测试先写。
  - **`copy_session` 摊平 fork**（S4a simplify 提出，与 plan 一致）：继承来的事件在副本里一律改成新 session 的 id，
    血缘不随副本走；一批写入，目标里已有这个 id 就 `SeqConflict`。
  - **CLI 不做 `run` / `resume`**（与 §14 原来的列表不同）：一次 run 要宿主的 tool、gate、完成条件，都是 Python
    对象，命令行给不出来；一个没有 tool 的 run 用处不大，还会被当成入口。
  - 规则 → 测试：进程 A 暂停、进程 B 批准并完成 `test_a_run_paused_in_one_process_is_approved_and_finished_in_another`；
    进程死在 tool 里，下一个进程不重跑、要求核对 `test_a_process_that_died_inside_a_tool_leaves_a_call_the_next_one_reconciles`
    （让非幂等调用照样重跑，它会失败）；一个进程 fork、另一个接着跑 `test_a_session_forked_in_one_process_runs_on_in_another`
    （让 store 不读继承的事件，它会失败）。每一步都是新的 Python 进程（`tests/contract/agent_process.py`），只共用数据库。
    内存 store 的 session 搬进 PG 后用另一个 `Agent` 恢复 `test_a_paused_session_copied_into_another_store_resumes_there`；
    fork 的 fork 里一次压缩跨过两个 fork 点、三层投影都对 `test_a_fork_of_a_fork_compacts_across_both_fork_points`
    （两种 store 各一遍；S3b 只测过一层）；CLI 每个命令一条，输出逐行断言（`tests/contract/test_cli.py`）。
  - 收尾 `/simplify`：采纳——`sessions()` 先 `LIMIT` 再按主键逐行取尾部和最后结局（原来先 `GROUP BY` 整张 `events`）；
    CLI 里 store 有了类型（原来是 `Any`）；"没配数据库"的提示只留一份（`config.NO_DATABASE`）；测试里的 `Ask` 和
    "开一个 store 读完关掉"提到 `tests/helpers.py`。记下不做：给 `migrate` 一个按 URL 的库入口（加公共接口，等需要）；
    `sessions.created_at` 的索引（等 session 多到要用）。
  - 收尾 Codex（只读）：采纳——`copy_session(new_session_id="")` 报错，不再悄悄换成新 id；`sessions(limit)` 小于 1
    报错；CLI 的设置出错是一行、不带值（`LITEAGENT_SCHEMA=public` 原来是 traceback）；`migrate` 的任何 psycopg 错误
    都只给脱敏后的信息（原来只处理 `OperationalError`）；`show` 把控制字符写成转义（存进去的文本不能驱动终端），
    截断的行以 `…` 结尾，`--json` 另把 DEL 和 C1 写成 `\u` 转义（JSON 本身只转义 C0）；`copy_session` 的说明改成
    "同一个 agent 下"等价，并写明它是读取那一刻的快照。七条都先写测试见红。否决或只记下：
    - "复制一个还在运行的 session 会得到过期的副本"：副本是读取那一刻的完整前缀（只追加、批次原子），和 fork 一样
      是快照；"源为空时什么都不写"不会发生——存在的 session 至少有 `session_started`。
    - "很长的 session 一次读进内存、一批写入"：记进 §16；分批又要全有或全无，得在 store 里加事务接口。
    - "`config` 会显示 `sslkey` 这类路径"：路径不是凭据；凡是 `*password*` 的参数都已隐藏。
  - 合并后的自动安全审查（采纳）：`show` 转义了控制字符，`sessions` 的行和每条错误信息（会引用 session id，
    id 由宿主决定）却没有；现在 CLI 打印的每个宿主给的字符串都经过同一个转义
    （`test_ids_and_errors_are_escaped_too`，先见红）。
- **S5a（2026-11-21）**：评测框架。任务集是合成的（owner 选定；§16 的"第一个真实任务"仍然开着）：一个确定性的
  小店（`src/lite_agent/evals/world.py`：客户、订单、库存、退款、留言，6 个 tool），12 个任务（7 英 5 中；查询、
  串联、改状态只改一次、出错后改正、不该调 tool、要读 skill、长任务加压缩、完成条件），每个任务的完成由代码读
  小店和事件 log 判定（`evals/tasks.py`）。指标从 log 算：完成与否和原因、turn、tool 调用（含被拒的）、无效调用、
  自动续上次数、是否压缩、token、耗时。lite-eval target `lite_agent.eval_target:agent`（extra `eval`，lite-eval 0.4.1，
  与 lite-mem 同一个 wheel），suite `agent-fake`（Fake 重放每个任务的正确路径，12/12）和 `agent-ollama`（S5b 用）。
  - 规则 → 测试：每个任务的正确路径判为完成且指标是确定的数字、错误路径判为未完成并说出原因
    （`tests/unit/test_eval_tasks.py`；让检查永远通过，11 条错误路径失败，第 12 条 done-en 失败在 run 的结局上）；
    数据集与 `TASKS` 一致、Fake 上两次结果相同（`tests/integration/test_eval_target.py`）；`-m ollama` 冒烟只跑
    chain-en（qwen3:8b，13 秒，通过）。
  - 与 plan 的出入：`tool_calls` 数的是模型要求的调用（被拒的也算），不是执行了的；错误路径里"完成条件一直没满足"
    的结局是 `failed (incomplete)`（自动续上用完后 harness 就这样结束），不是 check 的原因。
  - 收尾 `/simplify`：采纳——十二个任务改用关键字参数、`kind` 是 `Literal`；tool 的参数模型移到模块级；
    `Metrics.as_dict` 用 `dataclasses.asdict`；`stop` 直接取 `RunResult.stop`；token 指标数每一次计费的调用
    （摘要和失败的调用也算，与预算一致；原来只数 `model_turn`，`test_tokens_count_every_model_call_as_the_budget_does`
    先红）；手动改日期的 `TASKS_VERSION` 换成内容哈希。记下不做：`say` / `use` 与 `tests/helpers.py` 各留一份
    （src 不能导入 tests）。
  - 收尾 Codex（只读）：采纳——
    - 答案按"值"匹配，不再按子串：`says()` 要求整段（30 不在 300 里，D02 不在 D021 里，数字和拉丁字母定边界）、
      不在小数里、本分句里没有否定（not / n't / 没有 / 未）（`test_an_answer_says_a_value_only_as_a_whole_and_not_negated`，
      14 例先红）；recover-en 接受"没送达，已发货"这种对照说法。
    - done-en 的完成条件和它的 check 用同一个后置条件（原来"有任何留言"就结束，写错的留言没有机会改正；
      `test_done_en_keeps_going_until_the_note_is_right`）。
    - 新指标 `unknown_usage`：端点没报 token 的调用数，区分"0 token"和"不知道"。
    - 缓存身份：`versions()` 的 `code` 是整个 `lite_agent` 包（`.py`、`.md`、`.sql`）的哈希，循环代码在工作区里的改动
      也会让缓存失效（原来只有包的版本号）；`test_the_code_version_moves_when_any_of_lite_agents_code_changes`
      （去掉 `.py` 时它失败）。
    否决：
    - "long-en 量的是压缩阈值"：阈值（150 估算 token）低到 8 个 tool 结果本身就超过，任何认真做完的 agent 都会
      触发压缩；这个任务要测的就是"压缩后开头的规则还在不在"，没压缩就没测到它，判为未完成是对的。
    - "无效调用不含 `denied`、`truncated_turn`"：这个指标数的是模型自己调错（不存在的 tool、参数不合法）；gate
      拒绝和被截断的轮次不是调用写错，任务里也没有 gate；名字和注释已经这么说。
    - "测试里 `setup(None)`"：Fake 不读数据集；Ollama 也不读，这是 target 的实际行为。
  - `says()` 是 Codex 修复里新加的逻辑，按规则再做一轮只针对它的 Codex review：否定只认紧挨着值的
    （中间只许 yet / been / be），"not only shipped"、"isn't it shipped?" 不再算否定；认出"不是已发货吗"。4 例先红。
- **S5b（2026-11-21）**：Ollama 基线，两轮（评测卡片在 KB `global/llm-evals/`）。每个任务 3 次、一次只跑一个
  任务。lite-model 对 Ollama 的默认温度是 0（贪心），3 次结果相同；所以第二轮加了温度 0.6 的抽样配置看波动。
  - **第一轮**（贪心）：qwen3:8b 8/12，qwen3:1.7b 5/12。读完每一个失败的事件 log 后，发现评测工具有两处缺陷，
    不是模型的问题：long-en 里两个模型都在一轮里并行查完 8 个订单，没有可压缩的内容，测不到压缩；tool 参数
    说明的示例 id（"such as C1"）是小店里真实的客户，两个模型都把 Bo 猜成 C1。修法：long-en 改成一条订单链
    （每个订单写明下一个，`test_the_long_tasks_orders_can_only_be_followed_in_turn`）；示例改成小店里没有的
    id（`test_no_tool_example_names_something_in_the_shop`）。两条都先红。
  - **第二轮**：

    | 配置 | 完成率 | 波动（3 次的最低 / 最高） |
    | --- | --- | --- |
    | qwen3:8b 贪心 | 0.750 | 0.750 / 0.750 |
    | qwen3:8b 温度 0.6 | 0.722 | 0.667 / 0.750 |
    | qwen3:8b thinking | 0.889 | 0.833 / 0.917 |
    | qwen3:1.7b 贪心 | 0.417 | 0.417 / 0.417 |
    | qwen3:1.7b 温度 0.6 | 0.417 | 0.417 / 0.417 |

    8b 贪心和抽样的差距在波动以内，是噪声；thinking 高出波动之外，但每个任务慢约 10 倍（中位数 22.6 秒 vs 2.4 秒）。
  - **失败归类**（读过事件 log）：
    - 不查就猜：act-en "全额退款" 两个模型都直接退 100，没先查订单金额（thinking 3/3 做对）。
    - 不会纠错：recover-en 查 "17" 得到 "no order 17" 就放弃或原样复述，没想到去按邮箱找客户的订单（五个配置
      全部 0/3）。
    - 小模型不调 tool：1.7b 的 chain 任务直接说"查不到"，或用文字描述"我会调用 find_customer"而不调用；
      skill-zh 不读退款政策就说能全额退。
    - 压缩后的摘要出错：long-en 现在真的会压缩（9 轮、6 次摘要），关 thinking 的配置 0/3。重放一次看到摘要里有
      事实错误（"F03 没有查"、"F03 (140) 低于 100"、F02 金额写错），最终答案靠后面的轮次碰巧才对。这接上 S3b
      记下的摘要质量问题；改摘要提示要按规则 13 前后各跑一次这个任务集。
    - 第一轮 done-en 的 C1 错误在第二轮消失（8b 3/3），说明确实是示例 id 引起的。
  - 记下：贪心在 Ollama 上不保证可复现——评测里 long-en 三次都错，同样的配置单独重放一次却做对了。
  - 出处：lite-eval 的 run 目录在本机 `eval/lite-eval/runs/agent-ollama/`（gitignored，与 lite-mem 一样），run id：
    第一轮 `20261121T043144397423Z-qwen3-1.7b-c09861`, `20261121T043223892153Z-qwen3-8b-3fbf1e`；第二轮 `20261121T044011332103Z-qwen3-1.7b-62d6d6`, `20261121T044144876458Z-qwen3-1.7b-t0.6-23eacc`, `20261121T044301422408Z-qwen3-8b-07e8fc`, `20261121T044937768209Z-qwen3-8b-t0.6-d3384a`, `20261121T045636952523Z-qwen3-8b-think-e1f855`。
    逐个任务的完成数、原因和耗时抄进 KB 的评测卡片。失败归类来自重放（`run_task` 加上打印事件的脚本），
    不在 run 目录里：target 只记事件类型，不记内容。
  - 收尾 `/simplify`：采纳——基准脚本的一条注释和 `original_chars` 的值。记下不改：long-en 的订单链有一条捷径，
    F01–F08 都属于客户 C5，模型可以 `list_orders C5` 一次拿到全部，就又不需要压缩了；重放里 qwen3:8b 是逐个查的；
    评测的 run 只记了调用次数、没记调用了哪个 tool，查不出别的 run 有没有走捷径。改它会让基线不可比，留到下次改任务集时一起改（给每个 F 订单不同的客户）。
  - 收尾 Codex（只读）：采纳——写明上面的出处。否决——"suite 的 `timeout_s: 600` 没传给 lite-model"：说得对，
    但基线有意按宿主拿到的默认值（120 秒）跑，thinking 撞上它本身就是要记的发现（见 §16）。
- **S5.5（2026-11-21）**：v0.1.0。版本号、README 加安装方式、事件词汇表 V=1 从此冻结（§4）。
  `tests/unit/test_readme.py` 把 README 的例子原样执行一遍，只把 `build_async` 换成脚本化的 fake
  （把 README 里一个方法名改坏，它会失败）。Codex 只读 review 一条，采纳：冻结原来只是文字，加了 `test_vocabulary.py`
  守住它（给 `model_switched` 加一个字段而不升 V，它会失败）。
  - 发版：tag `v0.1.0`（`4fa94d5`），CI run 44876227588 的 test / package（tag 与 wheel 版本一致）/ release 全部成功，
    Release 带 wheel 和 sdist。发版后的验收：一个全新的项目从 Release 的 URL 安装（带上 lite-model 0.4.0），README
    的例子原样在本机 qwen3:8b 上跑出 `completed`，8 个事件与 README 一致。Release：`https://github.com/ArthurGao/lite-agent/releases/download/v0.1.0/lite_agent-0.1.0-py3-none-any.whl`。
- **S6（2026-11-21）**：HTTP 壳（§10）、agent 登记 `lite_agent.registry`、§3 的 `run_id` / `new_session`。测试先写。
  - 规则 → 测试：同一个 `run_id` 不开第二个 run——结束了从 log 还原、中途死掉在同一个 run 里接着跑、死在非幂等 tool
    里要求核对、resume 调两次只执行一次（`tests/contract/test_run_id.py`，两种 store；关掉"log 里已有这个 run"的分支，
    13 条里 8 条失败）。HTTP：发起 → SSE 跟到结束、过期的 `expected_seq` 得 409、不在等的 call 得 422、同一 session
    第二个 run 得 409、fork、未知 agent / session 得 404、请求多带字段得 422、只绑 loopback、`agents.toml` 的各种错
    （`tests/contract/test_server.py`，两种 store；分别关掉尾部检查、单 run 检查、loopback 检查，对应的测试失败）。
  - 与 plan 的出入：SSE 是单独的路由 `/events/stream`，不按 `Accept` 切换（FastAPI 的 SSE 路由是生成器，不能同时返回
    JSON）；服务端开始的 session 在第一批事件写下前就能被读（PG 上先读会 404，测试抓到的竞态）；`drain` 跟踪每个开始过
    的 run，不只每个 session 的当前 run（关掉单 run 检查时，旧的写法让 PG 上的测试在关 store 时挂住）。
  - 收尾 `/simplify`：采纳——从 log 还原的回答文本与现场的拼法一致（`turn_of(...).text`）；只有续跑的路径才累计 usage；
    只在给了 `run_id` 时扫描 log；流的第一次读取直接用作第一批；`server.py` 用具体类型；`/agents` 用与 `run_started` 相同的
    `ToolDef` 转换；去掉多余的 `started` 集合。记下不做：把 `_check_resume` 公开（加导出，壳之外没人用）；把"一个 session
    一个 run"放进库里（跨进程的保证已经是 store 的 `expected_seq`）。
  - 收尾 Codex（只读），采纳五条，都先写测试见红：复用的 `run_id`（不是最后一个 run、或任务不同）抛 `ValueError`，不再回放
    旧结果；两个并发的 `new_session` 抢同一个 id，输的一方得到 `SeqConflict`；同一 session 两个同时到的 run / resume 请求
    只有一个 202（检查和占住之间没有 await）；工厂抛错、数据库没配置都是 `RegistryError`；`localhost` 也被拒绝。另补一条
    测试：resume 执行到一半死掉，重试只补没有结果的 call。否决一条："错误体可能含上游原文"——这些响应都是壳自己写的文字，
    后台 run 的异常只进日志。
  - 修复改了壳的大半，按规则再做一轮只针对修复的 Codex：一条，采纳——`create_task` 本身失败时占住的 session 没人放开
    （`test_a_run_that_could_not_be_started_releases_its_session`，去掉释放时它失败）。
  - 合并前的自动安全审查（采纳）：没有认证的 loopback 服务会被 DNS rebinding 或浏览器里的页面调用（包括批准一个暂停的
    run）。加了 `Host` / `Origin` 检查（`test_a_request_from_a_web_page_is_refused`，不注册这个检查时 4 例失败）。顺带修了
    一条依赖时序的测试：一个 session 一个 run 的测试原来假定第一个 run 还没跑完，现在第一个 run 的模型等测试放行才回复。
  - 推送后的第二次安全审查（采纳）：`Origin` 只要是 loopback 就放行太宽，本机另一个开发服务器的页面也能调用；改成必须
    正好是壳自己的 origin，POST 只收 JSON（`test_another_origin_on_this_machine_is_refused`、`test_a_post_that_is_not_json_is_refused`，
    先红）。没做 bearer token：要对外或多用户使用时随认证一起设计。
  - CI 第一次红：`agents.toml` 的几条测试没写 `[store]`，默认是 postgres，读环境变量和 `.env`——本机有 `.env` 就过了，CI 没有。
    这几条测的是工厂，现在显式写 `kind = "memory"`；整个测试集也在没有 `.env` 的目录外跑过一遍（PG 测试按设计报错，其余通过）。
  - 本机验收：`liteagent serve`（loopback 8730）用本机 qwen3:8b 跑一次，curl 发起、SSE 跟随到 `run_finished completed`，
    `/openapi.json` 200；跑完关掉。
- **S7a（2026-11-21）**：TemporalRunner（§9）。测试在 time-skipping 测试服务器上跑（标记 `temporal`，默认就跑，CI 也跑）：
  跑到结束；暂停 → signal 批准 → 完成；宿主 tool 路由到另一个 task queue 上的 activity；activity 在 run 做完之后、Temporal
  记下之前失败 → 重试不开第二个 run、tool 只跑一次；worker 死在非幂等 tool 里 → 重试时要求核对、signal 带结果后完成；
  Continue-As-New 把暂停的 run 带过去；录下的 history 在 `Replayer` 上回放无误（`tests/temporal/test_runner.py`）。关掉
  "log 里已有这个 run"的识别，两条重试测试失败。
  - 测试抓到的 bug：从带过来的暂停开始的一段立刻又 Continue-As-New，无限接续（测试服务器报 http2 错误）；修成这一段执行过
    run 之后才会接续。
  - 测试里的一个坑：只发 query 不会推动 time-skipping 服务器的时钟，activity 重试的退避就一直等着；等暂停时用 `env.sleep`。
  - 第二个坑（2026-11-27，偶发失败查清）：time-skipping 测试服务器会丢掉 Continue-As-New 那一刻到的 query——worker 报
    "workflow execution already completed"，客户端约 30 s 后 `Timeout expired`。紧着发 query 时 20 次 20 次失败，Temporal CLI
    的 dev server（Server 1.32.0）上 0 次，所以 `TemporalRunner.status` 本身没问题。Continue-As-New 那条测试先用 `describe`
    等到新的 run 再 query（`_continued`）；把 `_paused` 改成不睡紧发时，没有它就失败、有它就通过。
  - 收尾 `/simplify`：采纳——Continue-As-New 用 `dataclasses.replace` 复制输入（以后加的字段不会被丢掉）；调用方错误的列表
    只写一处；`step_of` 用具体类型；`runner.py` 的说明改正（Temporal 不是 `Runner` 协议的实现）；测试里的 `Worker` 导入一次。
    记下不做：`ToolResultData` 与 `ToolOutcome` 合并、`Waiting` 带上 tool 名和参数（都改 §3 的接口）；`start()` 暴露超时、
    把 `RUN` / `PERMANENT` 移进 `types.py`（让 workflow 模块不再导入整个 agent）。
  - 收尾 Codex（只读）：采纳四条，都先写测试见红——一个旧的 decision 会答到后来的暂停上（`test_a_decision_for_an_answered_pause_never_reaches_a_later_one`，
    原来 workflow 因 resume 被拒而失败）；人的结果会覆盖宿主 tool 的结果（同一个修法：只认这次暂停还在等的 call）；心跳任务取消后要等它结束；
    `liteagent worker` 缺 extra 时给安装提示。另补测试：宿主 tool 永久失败 → 模型拿到只带类型名的错误结果，run 继续
    （把异常原样抛出时它失败）。修复之后第二轮只针对 workflow 的 Codex：分两次 signal 回答同一个暂停时，resume 期间到的第二个回答
    会被清掉——改成只为仍在等的同一批 call 保留（`still_relevant`，单元测试 `tests/unit/test_temporal_decisions.py` 先红）。
- **S7b（2026-11-21）**：inbox 与 timer（§9.1）。测试先写的是 `PgInbox`（`tests/contract/test_inbox.py`：去重、顺序、完成、
  decision 必须带 session、follow-up 只触发一次、schedule 每个周期触发且停机只补一次）；Temporal 上（PostgreSQL + time-skipping，
  `tests/temporal/test_inbox.py`）：重复投递只处理一次、两条快速消息合成一次 run、回复接着原 session、审批暂停后 decision 事件
  让它完成、接不了的 decision 标记 refused 而 worker 继续、follow-up 和 schedule 到期入队并被处理（时间快进两小时）、
  run activity 崩溃后重试只有一个 run。这些第一次跑就通过，所以都用变异看过红：去掉合并、把 refused 改成抛出、不关闭到期的
  follow-up（这一条 Temporal 测试抓不到——同一次触发的来源 id 去重兜住了——由 `PgInbox` 的测试抓住），对应的测试失败。
  - 收尾 `/simplify`：采纳——`PgSessionStore._connection()` 一处打开连接池，store 和 inbox 共用（原来各写一遍）；周期的算术用
    timedelta 整除（没有浮点）；activity 里的导入移到模块顶部。记下不做：inbox 到 agent 的映射集中到一处（加接口）、`WorkItem.kind`
    用 `Literal`。
  - 收尾 Codex（只读），两条都采纳，都先写测试见红：一项的重试用完就标 refused，模型或数据库宕机久一点工作就永远丢了
    （`test_an_outage_longer_than_the_retries_leaves_the_work_pending_not_refused`，把它改回 refused 时失败）；写了行、还没唤醒就
    死掉的处理函数留下没人处理的事件（`test_an_event_nobody_woke_for_is_found_by_the_scheduler`，去掉找回时失败）。
    simplify 的 D3 与第一条是同一个问题。
    修复之后第二轮只针对修复的 Codex，两条都否决：
    - "重试期间新事件会被并进同一批、旧 run_id 失效"：带着待重试的一项时 workflow 不调 `TAKE`，只重试那一项，成功之后才取新的；
      一个 inbox 只有一个 worker，期间没有别的 run 进这个 session。
    - "重试后才出现的永久错误被当成短暂的"：Temporal 最终给出的 `ActivityError` 不论第几次尝试都是同一个形状（cause 是 activity
      抛出的 `ApplicationError`，`type` 是错误类名）；`test_an_event_the_agent_cannot_take_is_refused_and_the_worker_goes_on` 证明分类成立。
  - 合并后的自动安全审查（采纳）："工作永不丢"也意味着一条每次都失败的消息（毒消息）会让 inbox 永远重试它、挡住后面所有事件。
    改成一项从第一次失败起超过 `max_retry_s`（默认一天）就标 `failed` 留给人看，inbox 继续；`failed` 状态由迁移 `0003` 加
    （`0002` 已经在用，不改它）。`test_a_message_that_always_fails_is_set_aside_and_the_inbox_goes_on`，去掉上限时 inbox 永远不空。
  - 测试里的一个坑：同一个 time-skipping 服务器上，前面测试留下的 inbox workflow 有空闲计时器、却没有 worker 了——时间就跳
    不过去；每个测试结束时终止它开的 inbox workflow。
  - `test_migrate_applies_then_says_up_to_date` 的期望输出改成两个迁移（`0001_initial, 0002_inbox`）：这是新迁移带来的、
    预期中的行为变化，不是为了让测试通过而改测试。
- **v0.2.0（2026-11-21）**：S6 与 S7 的发版。公共接口的加法：`Agent.run` / `resume` 的 `run_id`、`new_session`，`Runner.run` 同步；
  extra `server`、`temporal`；迁移 `0002`、`0003`（inbox）。事件词汇表没有变（`tests/unit/test_vocabulary.py` 的 V=1 指纹不变）。
  发版：tag `v0.2.0`（`b8b2d41`），CI run 44885745742 的 test / package / release 全部成功。发版后的验收：一个全新的项目从 Release 的
  URL 装上全部 extra（`pg`、`server`、`temporal`，带上 lite-model 0.4.0），新模块都能导入，README 的例子在本机 qwen3:8b 上跑出 `completed`。
  Release：`https://github.com/ArthurGao/lite-agent/releases/download/v0.2.0/lite_agent-0.2.0-py3-none-any.whl`。
- **P0 之后的修订（2026-11-21）**：按 lite-model 0.4.0 的实际接口和 Claude API 的 preserved thinking 规则改了
  §4（`model_turn` 存有序 blocks、`ToolResults` 分组、`Opaque` 进投影的条件）、§5 规则 4 和 11、§6、§13、§15、§16。

- **lite-context C7（2026-11-26，v0.2.5）**：压缩器的提问、渲染和摘要外框改用 lite-context 0.3.1（owner 决定：lite-context
  是核心依赖、只改压缩；规则 8、§6、§13 同步改了）。`SUMMARY` / `ASK` / `END` 从 `lite_context.compaction` 再导出，两边逐字
  相同；`render()` = `render_transcript(mark, neutral.to_neutral(history))`（lite-context 为此加了公开的
  `render_transcript`，不带结尾的 `END`——这里的测试按 `--- <mark> ` 数头标行）；`frame()` 用 `SUMMARY` 包住回复、去头标
  的规则留在这里（`clean`：去掉任何 16 位以上十六进制头标，lite-context 只去这一次的）；loop 写 `compaction` 事件时用它。
  没有自己的 `SummaryPolicy` 子类：这里不调 `plan_compaction` / `accept`，一个只为了覆盖 `wrap` 的子类是多余的（simplify）。切点
  （按 `model_turn`、在事件 seq 上）、事件和投影不变：lite-context 的 `plan_compaction` 按人的轮次算，不适用；C7 要求的
  "自己的轮次计法"就是保留 `cut`。估算的 3.5 字符 / token 取自 lite-context 的 `CharEstimate`，算法不变。
  验收：改之前给 38 个已有测试文件记了 sha256，之后逐个相同，503 个测试一行没改全部通过；新加
  `tests/unit/test_compaction_bytes.py`：用 v0.2.4 自己的代码先生成 10 种历史（并行调用、没解析的参数、错误结果、只有 thinking
  的一轮、前面已有摘要、伪造头标、空文本、Unicode）的渲染结果和 5 种回复的外框结果当金标准，改完逐字节相同；
  `tests/unit/test_neutral.py` 覆盖映射。只有 thinking 的一轮映射成一个空文本部分（v0.2.4 给它渲染一个空的 assistant 段；
  lite-context 对空消息什么都不渲染）。变异 6 处都被抓到。规则 13：fake 评测改前改后相同（12 个任务全 1.000，13 次调用）；
  发出去的字节已证明相同，Ollama 评测不必重跑。
  收尾：`/simplify`（采纳：`frame()` 代替多余的 policy 子类、`_new_mark` 私有化、注明 `neutral` 不是通用 adapter）+ review
  （Codex 额度用完，Claude 子代理）：用 hypothesis 生成 2 万段历史、5 千条回复，旧实现（`git show` 出来的 v0.2.4 代码）和新实现
  逐字节相同；唯一的差别在公开的 `render()` 上传入 system 消息时——v0.2.4 渲染成一个 system 段，现在 `ValueError`；loop 永远
  不会传（`projection.history` 不含 system），写进文档和测试。

- **v0.3.0（2026-11-26，事件词汇表 V=2）**：遮盖（§6）。lite-context 的 `fit()` + `MaskToolResults` 在每次模型调用前、压缩前决定把哪些旧
  的 tool 结果换成占位文字，新事件 `tool_results_masked` 记下每条结果和显示的原文，投影照着替换，自己不加字。每个事件的 `v`
  是它这种类型所属的版本：没开遮盖的 session 全是 `v=1`，v0.2.5 照样读（§4）。
  验收：改之前给 40 个已有测试文件记了 sha256，改完只有两个变了，都是词汇表规定的——`test_vocabulary.py` 加了 V=2 的指纹（V=1 的
  不动）、`test_events.py` 的"每种事件一个例子"加了新事件的例子；v0.2.5 写的一段真实 log（`tests/fixtures/v1_session.json`，含
  压缩）读回来投影逐字相同；PG store 里遮过的 run 读回投影相同；557+ 测试全过。
  变异检查：9 处里 4 处一开始没被抓到——"已经遮过的不再遮"和"tool 定义计入阈值"补了测试；"压缩之后的 state 不再用"是多余的
  （`StaleState` 本来就会让它重新量），删了；"重放存下的切点"在短的 run 上看不出差别，review 量了长的：50 轮、阈值 2000 时有
  state 3 次遮盖、没有 12 次（切点几乎每次都动，prompt cache 的前缀断掉），补了这个测试。
  收尾 review（Codex 额度用完，Claude 子代理）找到并修掉（都先写了失败的测试）：`keep_last=0` 让最新的结果也被遮；`v=1` 的
  遮盖事件被接受；结果对不上任何 call 的那一轮让投影崩溃（以前是一个空的结果）；`DoesNotFit`/`StaleState` 以外的 lite-context
  错误会让 run 失败；每个事件都写 `v=2` 会让 v0.2.5 读不了没开遮盖的 session（改成按类型写）。
  评测（规则 13，Ollama 由 owner 授权启动、用完停掉）：qwen3:8b greedy，12 个任务各 3 次。遮盖关 0.750（与 S5b 相同）；遮盖开
  （900 token、保留最新一批）第一版 0.667——skill 任务 3/3 → 0/3，`load_skill` 的结果（skill 的正文）被遮掉了；于是加了
  `keep_tools`（默认 `load_skill`、`recall`，这些结果从不遮），重跑 0.750，但这一次遮盖一次都没触发（任务太短）：它说明的是
  "不再伤害"，不是"有好处"。长 run 上的好处由脚本化的测试说明。KB 卡片 `global/llm-evals/evals/2026-11-26-lite-agent-masking.md`。
  没解决：一个 Temporal 测试（`test_continue_as_new_carries_a_paused_run_on`）在这个分支的全套测试里偶发 RPC 超时（约 36 次里 4 次，
  都在早期、评测同时在跑时），单独跑和不带 `test_masking.py` 时都不出现，main 上 18 次都没出现；把 `lite_context` 加进 Temporal
  沙箱的 passthrough（本来就该加）之后 8 次都过。原因没确定。

- **v0.3.1（2026-11-26）**：`PgSessionStore` 两个写者在同一个尾部时，等锁的那个拿到的是 PostgreSQL 的 `UniqueViolation` 而不是
  `SeqConflict`（S4a 起就有，与遮盖无关；查 Temporal 偶发超时时在 CPU 满负载下用对照实验找到：改遮盖前后都会出现）。原因：锁和读尾部
  是同一条语句 `SELECT … (SELECT max(seq) …) … FOR UPDATE OF s`——READ COMMITTED 下等到锁的语句只重查被锁的行，不重跑子查询，
  读到的是别人提交前的尾部。修法：先单独一条 `SELECT 1 … FOR UPDATE` 拿锁，再用新的一条语句读尾部（新语句能看到对方已提交的写）；
  另加兜底：插入时撞到 `events_pkey` 也报 `SeqConflict`（只可能是没拿到锁的写者）。数据从来没坏过：主键挡住了第二次写，错的只是异常的
  类型——HTTP 壳的冲突响应、跨进程续跑都靠 `SeqConflict`。测试：`test_pg_store.py` 让第二个写者在锁上等（按 `pg_blocking_pids` 认），
  第一个提交后断言它是尾部检查给的 `SeqConflict`（换回旧语句必失败，兜底也救不了这条断言）；兜底另有一条测试。满负载下契约测试 20 次全过
  （修之前第 1 次就复现）；review 的压力脚本每轮 8 个写者，新 session、已有尾部、fork 各几十轮，每轮恰好一个成功。
  Temporal 的偶发超时：满负载下改遮盖前后各 12 次都没出现，CPU 不足的假设不成立；仍记作未解决，CI 再出现时再查。

- **长任务上的遮盖（2026-11-26，评测代码，没有发布）**：S5 的 12 个任务里遮盖从没真正触发（结果是一行、最长 8 次查找），
  所以另做了一个合成世界 `evals/archive.py`：36 份约 1,250 字符的运单报告，每份埋着一个状态和一个毛重，并写着下一份的编号；
  三个任务各读一串 12 份：`audit-en` / `audit-zh`（数 delayed 的票数、合计它们的毛重）和 `compare-en`（读完后给出**第一份**和
  最后一份的毛重、两者差多少——专门测遮盖的风险：第一份到最后早已被遮）。数据集 `agent-long`，套件 `agent-long-fake.yaml`
  （CI）/ `agent-long-ollama.yaml`。阈值在 fake 上标定，让两种配置各自真正起作用：只压缩（3,000 估算 token 处写摘要，每个任务
  恰好一次）对比先遮盖（2,000 处遮，保留最新 2 批；每个任务遮 9 条、不再需要摘要）。这两个阈值故意比 qwen3:8b 在这台机器上的
  上下文（Ollama 0.40.15 给的 32,768）低得多：模拟紧的预算，不是截断。`Metrics` 加了 `rereads`（同一 tool 同样参数再调一次）、
  `masked`（被遮的结果数，`projection.masks()`）和 `answer`（最后的回答，留着以后重新判）。`Task` 变成以它的场所为参数的泛型
  （`Task[World]` / `Task[Archive]`，`typing_extensions` 的 TypeVar 默认值，依赖里写明 `typing-extensions>=4.16.0`，即
  pydantic 自己的下限），system prompt 由场所给出；S5 的 12 个任务行为不变（fake 上 1.000、13 次调用，原有测试文件逐字节不变）。
  被测的数据（报告、system prompt、三个 prompt）用一个哈希钉在测试里：要改就是新的一次测量。
  **两次 Ollama 运行**（owner 授权启动、跑完停掉；qwen3:8b greedy、关 thinking，每个任务每种配置 3 次）。第一次的判定有错
  （close-out 审查发现）：`compare-en` 当时问"哪个重"，判定只看答案里有没有出现 `T01`，两个 id 都提到的答案无论对错都算对；
  `43.6kg`（数字后紧跟 kg）和 "eight" 这样的写法算错；答案也没存下来，没法重判。修了判定（`compare-en` 改问差值，三个条件都是
  数；重量接受 `kg` 连写和两位小数，票数接受英文词和中文数字），`Metrics` 加 `answer`，重跑。第一次的数（只压缩 3/9、
  先遮盖 0/9、输入 token、用时）作废，只留在 git 历史里。第二次：

  | 配置 | 完成 | 输入 token / run | 用时中位数 | 摘要 | 被遮 | 重读 | audits：真 delayed 的毛重答对 | compare：第一份毛重对 |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | 只压缩 | 0/9 | ≈34.2k（读满 12 份的 run） | 78 s | 9/9 次 run | 0 | 0 | **22/45** | **3/3** |
  | 先遮盖 | 0/9 | ≈23.2k（−32%） | 34 s（−57%） | 0 | 9 / run | **0** | **9/45** | **0/3** |

  完成率两边都是 0，分不开；分开它们的是**哪些事实留下了**——这一列是事后用脚本从存下的回答里算的
  （`scripts/long_eval_facts.py`），不是事先定的指标。读法：（1）遮盖开时模型只按还看得见的最后一两份作答（`keep_last=2`）：
  audit-en 三次都只说出 1 票（最后一份 R12），compare-en 三次都把第一份 43.6 kg 编成 7.5 kg；9 次 run 一次都没照占位符
  `[result cleared to save context; call read_report again if you need it]` 重读。（2）只压缩时摘要留住了早先的事实
  （audit-en 有两次 8 票的 id 和毛重全对，compare-en 第一份毛重 3/3），失败在 qwen3:8b 自己的计数和算术（列出 8 个 id 却说
  "5 票"；差值算成 16.7）和摘要后丢了在链上的位置（2 次把 T07 当成最后一份，2 次 run 读到第 10、第 8 份就停了）。结论写进 §6：
  默认关，只在旧结果读过一次就用完的任务上开。限度：合成任务、一个模型、每格 3 次（greedy，遮盖开的三次几乎一样）；
  没有量 prompt cache。没做的（§16）：更有信息的占位符、更强的模型。KB 卡片
  `global/llm-evals/evals/2026-11-26-lite-agent-masking-long.md`。
  收尾：`/simplify`（四个角度）采纳了泛型 `Task`、system prompt 归场所、长任务一个构造函数、`masked` 用 `masks()`、测试里
  一个 `fake()`；没采纳：`_kg` 的 `.0` 分支是死代码（合计可以以 .0 结尾）、测试里独立重算答案（故意不用被测代码算）、
  `Report.next_id` 多余（测试用它核对链）、每个世界一个 `kind`（一个共享词汇表让数据集标签受检）。正确性审查（Codex 额度用完，
  由一个 Claude 子代理做）：判定的三个问题（上面）修了，先写失败的测试；报告长度的注释改成 1,250；`rereads` 不认大小写不同的
  同一 id、会把出错后的重试算进去——只是一个指标，留着；评测缓存的身份不含 lite-context 的版本（遮盖和压缩用它的估算）——
  这是早就有的缺口，改它要动 S5 钉住 `versions()` 的那条测试，单独做（§16）；两次运行都没有命中缓存。一次 mutation 检查
  留下了过期的字节码（同样大小的改动、同一秒内还原，Python 认 mtime 和大小，继续跑变异体），让钉数据的测试在审查时失败；
  删掉 `.pyc` 后通过，之后的 mutation 检查都带 `PYTHONDONTWRITEBYTECODE=1`。

## 16. 待确认

- **很长的 session 的复制**（S4b Codex）：`copy_session` 把整个 session 读进内存、一批写入；分批而仍全有或全无
  需要 store 提供事务，等真有这么长的 session 再定。

- **迁移运行器不认识的版本**（S4a Codex）：`pending()` 对 `schema_migrations` 里出现、包里却没有的版本
  （更新的代码迁移过的库）不报错。三个仓库共用这份代码，要改一起改。

- ~~**【阻塞 S1 之后的取舍】第一个真实任务是什么**~~：2026-11-21 owner 定：llm-steward 的 **reconcile 排查
  助手**。对账发现孤儿 provider 记录后，agent 用只读 tool 查 change_log / external_refs / provider 记录，写出
  诊断和建议动作（ignore / remirror / erase），交给人在 Admin 里决定；它自己不写任何受治理的状态。设计在
  llm-steward 设计 §11 的 P11。这个宿主只用核心库和 `pg` extra，不用 HTTP 壳和 Temporal。
- ~~**【阻塞 P0】lite-model 的 tool 调用接口**~~：2026-11-21 定并实现（lite-model 设计 §3.1）。
- **OpenAI 兼容后端的 reasoning 内容**：lite-model 0.4.0 不采集、不回传（它的 §15）。S5b 在 Ollama 上核实：
  qwen3:8b 开 thinking（`reasoning_effort: medium`）、推理内容不回传，任务集完成率 0.889，高于关 thinking 的
  0.750，Ollama 不要求回传。DeepSeek 等别的端点仍待核实。新问题：thinking 的单次调用会超过 lite-model 默认
  的 120 秒超时（long-en 一次 `CallFailed`），用 thinking 的宿主要把 `timeout_s` 调大。
- **preserved thinking 的 400 与别的失败分不开**：投影规则（§4）应当让它不发生；真发生了是 `CallFailed`，
  会触发 `fallback_role`。要不要让 lite-model 给它一个单独的错误类型，S2 做 fallback 时定。
- **streaming**：v1 事件粒度是一轮。交互式 UI 需要逐 token 时，要 lite-model 先支持 streaming。
- ~~**session store 的默认持久化**~~：S4a 做了 PostgreSQL（`PgSessionStore`）。文件 store 仍在"按需"里。
- **过大的 tool 结果**：只截断，还是把完整结果另存一份（blob 表 / 文件）并在事件里留引用。
- **tool 并行**：同一轮多个 call 是否并行执行；并行时结果事件的顺序按 call 顺序写。
- **花了钱但没进 log 的调用**：模型已回复而 `append` 失败时（普通轮次或摘要），这次调用的 usage 不在 log 里，
  预算会少算这一笔（§4）。彻底的做法是调用之前先写一条"意图"事件，之后再对上；S7 的 durable runner 需要
  类似的东西，到时一起定。
- ~~**投影的开销**~~：S5b 测了（`scripts/bench_projection.py`）。每一步的 `derive()`：200 / 1000 / 5000 个事件
  中位数 0.22 / 1.13 / 5.96 ms；PG 尾部读取 0.47 / 0.50 / 0.74 ms，三层 fork 末端 1.6 / 2.0 / 4.6 ms；整读 2 / 9 / 59 ms
  （只在 run 开始时）。模型调用是秒级，**不缓存投影，读取也不改成一条递归 CTE**；session 到几万个事件时再测。
- **memory 与 llm-steward**：agent 写 memory 是直接用 lite-mem，还是必须经过 llm-steward 的治理。
- ~~**HTTP 端口**~~：8730（S6 定，KB `global/machine/ports.md` 登记）。
- **遮盖的占位符与重读**（长任务评测，2026-11-26）：qwen3:8b 不照占位符重读（9 次 0 次），看不见的事实就编。值得试：
  占位符带上结果里模型可能还要的东西（lite-context 的占位符是固定常量，§5.2 规则 6，要改得在那边改并配评测），或换更强的
  模型再量。qwen3:8b 在 12 份上数不准、算不对，要让完成率分开两种配置，需要更短的链或更强的模型。
- ~~**评测缓存的身份不含 lite-context**~~（长任务评测的 close-out 审查）：2026-11-27 加上了。`AgentTarget.versions()` 现在有
  lite-agent、lite-model、lite-context 和代码哈希；遮盖和压缩用 lite-context 的估算，升级它就是新的输出，不再命中旧缓存。
  S5 的 `test_the_target_says_what_decides_its_output` 钉住键的集合，随之改了（owner 同意），并核对值就是装着的 lite-context 版本。
- **项目 KB 的位置**：提议 `KB/projects/lite-agent/`（S0 创建）；lite-cache 是放在仓库内 `kb/`。
