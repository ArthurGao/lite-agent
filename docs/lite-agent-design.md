# lite-agent 设计文档

**状态：设计草稿（2026-11-20），未开始实现。S0 完成（2026-11-21，骨架，只在本地，尚无远端仓库）。P0：lite-model v0.4.0（`turn()`）。下一步 S1。**
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

名字是草案，S1 定稿。

```python
class Agent:
    def __init__(
        self,
        *,
        role: str,                              # a lite-model role; never a model id
        fallback_role: str | None = None,       # another role, used once after CallFailed (§5 rule 11)
        model: AsyncModel,                      # lite_model.AsyncModel (FakeModel in tests)
        system: str | SystemPrompt = "",
        tools: Sequence[Tool] = (),
        sessions: SessionStore | None = None,   # default: InMemorySessionStore
        skills: SkillSource | None = None,
        memory: Memory | None = None,
        compaction: Compactor | None = None,
        budget: Budget = Budget(),
        gates: Sequence[Gate] = (),
    ) -> None: ...

    async def run(self, task: str, *, session_id: str | None = None) -> RunResult: ...
    async def resume(self, session_id: str, input: ResumeInput | None = None) -> RunResult: ...
    def run_sync(self, task: str, *, session_id: str | None = None) -> RunResult: ...

@dataclass(frozen=True)
class RunResult:
    session_id: str
    outcome: Literal["completed", "failed", "paused"]
    text: str | None            # the final assistant text, when completed
    pause: Pause | None         # why it paused and what it waits for
    error: RunError | None
    usage: Usage                # summed over every model call of this run, failed ones included
    switched: bool              # the run moved to fallback_role (per-model usage is in the log)
    stop: StopReason            # end_turn | terminal_tool | budget_turns | budget_tokens | gate | error | ...

class SessionStore(Protocol):
    async def append(self, session_id: str, events: Sequence[Event], *, expected_seq: int) -> None: ...
    async def read(self, session_id: str, *, upto_seq: int | None = None) -> Sequence[Event]: ...
    async def fork(self, session_id: str, *, at_seq: int) -> str: ...

class Tool(Protocol):
    name: str
    description: str
    input_schema: type[BaseModel]
    terminal: bool                              # a result that may end the run (§5 rule 2)
    idempotent: bool                            # safe to run again with the same call_id (§8)
    timeout_s: float | None                     # None = Budget.tool_timeout_s
    async def __call__(self, args: BaseModel, ctx: ToolContext) -> ToolOutput: ...

class Gate(Protocol):                           # runs before a tool call executes
    name: str
    async def check(self, call: ToolCall, ctx: RunContext) -> Allow | Deny | PauseFor: ...

class Runner(Protocol):                         # where the loop's steps execute
    async def run(self, agent: Agent, session_id: str) -> RunResult: ...
```

- `@tool` 装饰器把一个带类型标注的函数变成 `Tool`（schema 来自参数的 Pydantic 模型）。
- 配置放数据，初始化参数放代码对象（§11）。
- 能力在构造 `Agent` 时注入，构造后不可变；要换实现就构造一个新的 `Agent`（session 在 store 里，不受影响）。
  v1 不做运行时注册表：KB §10.1 的"可撤销、可寻址"等有宿主需要热替换时再做。`liteagent config` 打印
  最终装配结果（KB §10.3）。
- 状态全部在 `SessionStore` 里：在组件 A 下产生的 session 必须能在组件 B 下恢复（KB §10.3 的检验，S4 验收）。

## 4. Event log 与投影

**规则（KB §9）：event log 只追加，是事实来源；发给模型的 messages 每一轮由 log 纯投影得出，从不单独保存、
从不原地修改。**

信封：`session_id`、`seq`（单调，从 1 起）、`time`、`run_id`、`type`、`data`、`v`（事件词汇表版本）。

| type | data | 模型可见 |
| --- | --- | --- |
| `session_started` | 创建时间、fork 来源（`parent_session_id`、`at_seq`） | 否 |
| `run_started` | `run_id`、触发方式（run / resume）、**本次 run 的快照**：主 role、`fallback_role`、system prompt 原文、每个 tool 的完整定义（名字、描述、JSON schema） | system 和 tools |
| `user_message` | 文本；`origin`：task / steering / follow_up / auto_continue / resume | 是 |
| `model_turn` | lite-model 的 `AssistantTurn.blocks`，按端点给的顺序原样保存（`Text`、`ToolCall`、`Opaque`——Anthropic 的 thinking block）；`stop`、role、provenance、usage | 是 |
| `tool_started` | `call_id`、tool 名、参数、`external`；过了校验和 gate、在执行（或交给宿主执行）**之前**写。校验失败、`Deny`、`PauseFor` 都不写它（§8） | 否 |
| `tool_result` | `call_id`、内容（已截断）、`is_error`、`error_kind`（`invalid_args` / `denied` / `exception` / `timeout` / `truncated_turn` / `outcome_unknown`）、`executed`（tool 是否被调用过）、`origin`（`executed` / `external` / `reconciled`）、`terminal`、原始长度 | 是 |
| `context_added` | skill / memory 注入的文本（写入时已包好外层标签） | 是 |
| `compaction` | 摘要、覆盖的 seq 区间、做摘要的 role、provenance、usage | 是（替代被覆盖的区间） |
| `paused` / `resumed` | 原因（`approval` / `tool` / `reconcile`）和等待的 `call_id` 列表 / 恢复时的输入 | 否 |
| `model_error` | 失败调用的错误类型、role、`purpose`（`turn` / `compact`）、usage（拿不到时为 `None`） | 否 |
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
- **system 和 tools 也来自 log**：投影取最近一条 `run_started` 的快照。一次 run 之内它们不变；宿主换了
  `Agent` 的配置再 `resume`，新的 `run_started` 记下新的快照，回放时每个请求都能按它所属的 run 还原。
- **不变量**：每次真实请求前断言 "交给 lite-model 的 messages 和 tools == log 的投影"，不等就失败。它
  比较的是规范表示；后端实际发出的请求体由 lite-model 的契约测试保证。
- **压缩只做遮盖**：`compaction` 事件记下它覆盖哪些 seq，原事件留在 log 里；投影时用摘要替代那个区间。
- **fork**：新 session 记 `(parent_session_id, at_seq)`，不复制事件。读子 session = 父 session 里
  `seq ≤ at_seq` 的事件，接上子 session 自己的事件；子 session 的 seq 从 `at_seq + 1` 起，存在自己名下。
  父 session 只追加，所以这个前缀永远不变：父 session 在 fork 之后写的事件（包括覆盖旧区间的
  `compaction`，它的 seq 大于 `at_seq`）子 session 看不到。`expected_seq` 只检查子 session 自己的尾部。
  fork 的 fork 沿链向上读。
- **并发**：`append` 带 `expected_seq`，不匹配就拒绝（同一 session 只能有一个写入者）。一次 `append` 的
  一批事件要么全写入、要么全不写。模型已经回复但 `append` 失败时，run 失败，这个回复丢掉（钱已经花了，
  log 里没有它）；`resume` 会重发这一轮。
- tool 结果过大时在写入 `tool_result` 前截断，事件里记原始长度；完整结果要不要另存见 §16。

## 5. Loop 规则

1. **被长度上限截断的一轮不执行任何 tool call**，但每个 call 都写一条错误 `tool_result`（"输出被截断，参数
   可能不完整，请重发"），然后继续（KB §11.1）。这一轮计入 usage 和 turn 预算。
2. **一批 tool 结果全部是 terminal 才结束**；只要有一个普通结果，就再给模型一轮（KB §11.2）。
3. **纯文本的 `end_turn` 不等于完成**（KB §11.4）：宿主给了完成条件（checklist tool 或 `done` 谓词）且未满足
   时，写一条 `origin=auto_continue` 的 `user_message` 点名未完成项，最多 `budget.auto_continues` 次
   （默认 2）。没给完成条件时，纯文本即结束——简单用法不需要这一条。
4. **快照按 run 取**：`run()` / `resume()` 开始时把 role、`fallback_role`、system、tool 定义写进
   `run_started`，本次 run 的每一轮都从它取（§4）。KB §11.3 说的是每一轮取一次；这里收紧到每个 run 一次，
   换来"请求可以从 log 完整还原"。run 中途要改 system 或 tool，就结束这次 run 再开一次。
   建议一个 session 里 system 和 tools 一直不变：改了是允许的，但之前的 thinking block 不再进投影（§4），
   prompt cache 也从头开始。
5. **运行中到达的输入**分两个队列：steering 在两轮之间注入，follow-up 在即将停止时读取并重启 loop
   （KB §11.3）。S2 只做 steering 的接口，follow-up 随 long agent 做。
6. **预算包含失败和重试**：每次模型调用（包括 lite-model 抛错的、做压缩的）都写事件、计 usage；下一次
   调用会超出预算时在调用前停，`stop=budget_*`（KB §7.2）。`turns`、`tokens`、`cost_usd` 按**整个
   session** 算，每次从 log 合计（暂停再恢复不会得到一份新预算；fork 出来的 session 包含继承的前缀）。
   `auto_continues` 按 run 算。拿不到 usage 的调用（`CallFailed`）记 `None`、不估算，token 预算里按 0 计，
   turn 预算照算。压缩调用计入 token 和成本，不计入 turn。
7. tool 抛的异常变成 `is_error` 的 `tool_result` 送回模型；`Gate` 的 `Deny` 同样。只有 harness 自己的错误
   （store 写失败、投影不变量不成立）才让 run 失败。
8. 同一轮里的多个 tool call 默认顺序执行；并行执行见 §16。
9. **模型调用失败（超时、连接错误、5xx、429）**：重试只在 lite-model 里做（同一个 target，`max_retries`，
   它的设计 §8），loop 不再叠一层重试。重试用尽后 lite-model 抛 `CallFailed`：写 `model_error` 事件，run 以
   `outcome="failed"`、`stop=error` 结束。默认**不换模型**；配置了 `fallback_role` 时先按规则 11 换一次。
   log 的最后是 `model_error`（投影不包含它），
   `resume(session_id)` 重发这一轮，之前的 tool 不重跑。超时的调用拿不到 usage（`None`），事件里
   如实记 `None`，不估算。durable runner（§9）可以用更长的重试策略并尊重 Retry-After，那是 runner 的事。
10. **tool 超时**：每个 tool 有 `timeout_s`（默认 `Budget.tool_timeout_s`）。超时写 `tool_result`，
    `error_kind=timeout`、`executed=True`，内容写明"是否已生效未知，不要直接重试"。它和"没有执行"
    （`invalid_args`、`denied`、`truncated_turn`，`executed=False`）是不同的结果，模型和宿主都分得清。
    同一个 `call_id` 不会再执行。模型想重试，只能发一个新的 call（新的 `call_id`），它照常过 gate；gate
    通过 `RunContext` 读得到 session 的事件，可以据此拒绝或要求审批"上一次超时、结果未知"的重复调用。
11. **换模型续跑（可选，默认关）**：`Agent(fallback_role=...)`。
    - 只在 `CallFailed`（超时、连接、5xx、429，重试已用尽）时触发。`Truncated`、空回复、拒答、tool 参数
      不合法都**不**触发：那是质量问题，换模型会把它盖住。
    - 顺序：写 `model_error` → 写 `model_switched`（from / to role、原因）→ 用 `fallback_role` 重发这一轮。
      fallback 也 `CallFailed` 就按规则 9 失败。一次 run 最多换一次。
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

- **system prompt** 是 `run_started` 快照的一部分，一次 run 之内不变（§4、§5 规则 4）。
- **skills**（`SkillSource` Protocol；默认实现读 `SKILL.md` 目录，格式与 Claude Code / cairn 相同）：system 里
  只放索引（名字 + 一行描述），正文由模型通过 `load_skill` tool 按需加载。索引有 token 预算，超出报错，
  不静默全量加载（cairn `ContextBudgetError`）。
- **memory**（`Memory` Protocol：`recall(query)`、`remember(note)`）：默认没有。`lite-agent[mem]` 提供
  lite-mem 的 adapter。prompt 只说 memory 用来做什么，不写死记录规则（KB §13.1）。
- **压缩**（`Compactor` Protocol）。不给 `compaction` 就不压缩，超出上下文是一次普通的失败。库里自带一个
  实现 `SummaryCompactor(context_tokens={role: n}, keep_turns=N)`，要用时由宿主传入：
  - 投影的 token 估算超过**当前要调用的 role** 的阈值时，把"最近 N 轮之前"的全部模型可见事件交给
    `compact` role 做一次摘要。切点不落在一个 tool call 和它的结果之间。
  - 主 role 或 `fallback_role` 在 `context_tokens` 里没有阈值时，构造 `Agent` 就报 `ConfigError`。
  - 成功：写一条 `compaction` 事件（摘要、覆盖的 seq 区间、role、provenance、usage）。失败：写
    `model_error`（`purpose=compact`），run 失败。要压缩的内容连 `compact` role 也装不下，同样是失败。
  - 覆盖的区间可以跨过 fork 点（子 session 的 seq 接着父 session 的往下排，§4）。
  - 压缩之后，保留下来的最近 N 轮里的 `Opaque` 不再进投影（§4）：这正是 Claude API 文档对"保留最近几轮
    原文"的压缩给的做法。不在一个 tool call 和它的结果之间压缩，也是同一份文档的要求。

## 7. Tools

- 来源三种：宿主的 Python 函数（`@tool`，S2）；MCP server（`lite-agent[mcp]`，作 client，把远端 tool 映射成
  `Tool`）和兄弟模块的 adapter（`lite-agent[rag]`：`search_kb`）——后两种按需做（§15）。
- 参数先过 Pydantic 校验；校验失败写 `tool_result`（`error_kind=invalid_args`、`executed=False`），不写
  `tool_started`，不调用 tool。未知的 tool 名同样处理。
- tool 数量保持小，按意图分组（KB §14.3）。tool 的完整定义在 `run_started` 里（§4），两次 run 之间变了，
  log 里看得出来（影响 prompt cache 和可复现性）。
- **外部执行的 tool**（`external=True`；HTTP 壳的"客户端执行 tool"用它，§10）。事件顺序：校验 → gate →
  `tool_started`（`external=True`）→ `paused`（`reason=tool`，带 `call_id`）→ run 以 `paused` 结束 → 宿主
  执行 → `resume(session_id, ToolResults({call_id: ...}))` → `resumed` → `tool_result`（`origin=external`）。
  同一轮里有多个外部 call 时一起暂停，结果要一次交齐。已有结果的 `call_id` 再交一次会被拒绝。宿主执行了
  但提交丢了：这个 call 仍是"已开始、没有结果"，宿主重新提交即可；宿主那边的副作用用 `call_id` 去重。

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

## 9. Runner 与 Temporal（S7，可选）

- `InProcessRunner`（默认）：loop 在调用方的 event loop 里跑完或暂停。
- `TemporalRunner`（`lite-agent[temporal]`）：workflow 只做编排，模型调用和 tool 执行各是一个 activity；
  model activity 的重试尊重厂商的 Retry-After（cairn 的 `MODEL_RETRY`）。
  - event log 仍是事实来源，Temporal history 只保存进度。activity 写 log 要幂等（`expected_seq` + op id）。
  - 暂停 = workflow 等 signal；审批和外部 tool 结果通过 signal 进来。
  - 长 session 用 Continue-As-New，下一段从 log 恢复。
  - TypeScript 宿主可以在自己的 task queue 上实现 tool activity，Python workflow 按名字调用。
  - 确定性：workflow 代码不读 store，只根据 **activity 的返回值**做决定（activity 返回它追加的事件摘要：
    stop 原因、待执行的 call、是否暂停）。读写 log 只在 activity 里。
  - activity 可能在完成副作用后、Temporal 记下完成前失败并被重试：每个 activity 带确定的 op id
    （`run_id` + 步号），追加事件前先查这个 op 是否已写过；tool 的重复执行按 §8 的规则处理。
  - 这一节只是方向。做 S7 之前先把它写成完整设计（边界、重试策略、replay 和 activity 重试的测试）。
- **inbox / timer**（KB §12）：每个触发源写同一个 durable inbox，一个 inbox 一个 worker；timer 只负责入队。
  随 TemporalRunner 一起设计，届时在本文加一节。

## 10. HTTP 壳（S6，按需）

`liteagent serve`（extra `server`，FastAPI，绑定 127.0.0.1，端口见 §16）。只有四类操作，全部转给 `Agent`：

| 操作 | 说明 |
| --- | --- |
| `POST /runs` | 发起一次 run（agent 名字来自服务端配置，请求里不能带 tool 的代码） |
| `GET /sessions/{id}/events` | 读 event log；SSE 跟随新事件（粒度是"一轮"） |
| `POST /sessions/{id}/resume` | 提交审批结论或外部 tool 的结果 |
| `POST /sessions/{id}/fork` | 从某个 seq 分叉 |

TypeScript 宿主提供 tool 的三种方式：MCP server（首选）、外部执行（§7）、Temporal activity（§9）。
认证、多租户在做 S6 时设计；没有认证之前只绑定 loopback。

做 S6 之前要先定的协议（现在只列要求）：agent 和它的 tool schema 在服务端配置里登记，请求不能新增 tool；
暂停的外部 tool 用 `call_id` 对应，提交结果时带 `expected_seq`；已有结果的 `call_id` 再提交、或 seq 过期，
一律拒绝（409），不覆盖。

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
| lite-model | **唯一的运行时依赖**（release wheel，≥ 0.4.0）。用它的 `AsyncToolModel.turn(role, messages, tools) -> TurnReply`（lite-model 设计 §3.1，2026-11-21 实现）：有序的 `AssistantTurn.blocks`；`ToolResults`；被截断的一轮**返回** `stop="length"` 和它的 call（§5 规则 1 靠它）；参数不是合法 JSON 对象时 `ToolCall.arguments is None`（当作 `invalid_args`）；`Opaque` 的跨协议规则在 adapter 里；`AsyncFakeModel.turn` 脚本化轮次。`turn()` 不经过 lite-model 的 hook。lite-model 不处理 preserved thinking 的历史检查，这里的投影负责（§4）；那种 400 现在是 `CallFailed`，与别的失败分不开（lite-model §15） |
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
  cli.py          liteagent run | sessions | show | resume | fork | config
  pg/             PgSessionStore, migrations/            (extra: pg)
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
| S3 | context：skills 索引 + `load_skill`、`Compactor` 默认实现（§6）、`Memory` Protocol（测试用内存实现） | 压缩后原事件仍在 log 里，投影用摘要替代该区间，换一个 compactor 重新投影得到不同结果而 log 不变；切点不拆开 tool call 和结果；skills 索引超预算报错；fork 之后父 session 再压缩，子 session 的投影不变；压缩之前的 `Opaque` 不在投影里，之后产生的在；同一轮的多个 `tool_result` 投影成一个 `ToolResults` |
| S4 | `PgSessionStore`（migrations，`LITEAGENT_TEST_DATABASE_URL`，schema `lite_agent_test`）、跨进程 resume / fork、`expected_seq` 冲突、CLI | 进程 A 暂停、进程 B 恢复；内存 store 产生的 session 导入 PG store 后能恢复（KB §10.3）；fork 不复制事件且互不影响；并发 append 一个成功一个被拒；一批事件的 append 全有或全无；fork 的 fork 里做一次覆盖区间跨过两个 fork 点的压缩，三层 session 各自的投影都符合预期 |
| S5 | lite-eval target；一个固定的小任务集，每个任务有写明的完成条件（由代码判定）；`-m ollama` 冒烟只跑其中一个用到 tool 的任务 | 指标（完成率、turn、token、无效 tool call）进报告；用 Fake 的确定性跑法进 CI；Ollama 的基线数字记入 KB |
| S5.5 | 发布 v0.1.0 release wheel | 新项目能从 wheel 安装并跑通 README 的例子 |
| S6（按需） | HTTP 壳（§10）；先定 §10 末尾列的协议 | 有一个 TypeScript 宿主要用时再做 |
| S7（按需） | `TemporalRunner`、inbox / timer（§9）；先把 §9 写成完整设计 | 有一个真实的 long-running 任务时再做；replay 测试、activity 重试不重复写事件、Retry-After、Continue-As-New |
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
- **S0（2026-11-21）**：`git init`（只在本地；远端仓库等 owner 决定）、pyproject（lite-model v0.4.0 以直接 URL 写进
  元数据，与 lite-cache 相同）、ruff / mypy strict / pytest + pytest-asyncio、CI 文件（test / package / release，
  尚未在 GitHub 上跑过）、socket 守卫、`scripts/smoke.py`、README、LICENSE、`.env.example`；workspace `AGENTS.md`
  已更新。检查：ruff、ruff format、mypy 干净，pytest 4 passed（含 review 后加的一条）；最初的三个测试各见过一次红（去掉 socket 守卫；把元数据里
  的 lite-model URL 换成裸名字；对着 lite-model 0.3.0 跑 tool 轮次的测试是 ImportError）；本地按 CI `package` job 的
  步骤从 wheel 装进干净的 venv 并跑通 smoke。没有跑 `/simplify`：这一阶段没有产品代码。
  Codex 只读 review（整个工作区）：2 条，都采纳——socket 守卫补上 UDP `sendto` 和 `getaddrinfo`（回归测试先红）；
  README 开头改成"目标"而不是现状。CI、打包、`.gitignore` 它判为干净。
- **P0 之后的修订（2026-11-21）**：按 lite-model 0.4.0 的实际接口和 Claude API 的 preserved thinking 规则改了
  §4（`model_turn` 存有序 blocks、`ToolResults` 分组、`Opaque` 进投影的条件）、§5 规则 4 和 11、§6、§13、§15、§16。

## 16. 待确认

- **【阻塞 S1 之后的取舍】第一个真实任务是什么。** 现在 workspace 里没有需要"模型自选 tool"的宿主（cairn
  等现有项目都明确不做 model-driven tool loop，llm-steward 是治理层）。没有真实任务，S3 做哪些 tool、
  S5 的评测集、S7 做不做都只能猜。S0–S2 不依赖这个答案。
- ~~**【阻塞 P0】lite-model 的 tool 调用接口**~~：2026-11-21 定并实现（lite-model 设计 §3.1）。
- **OpenAI 兼容后端的 reasoning 内容**：lite-model 0.4.0 不采集、不回传（它的 §15）。用这类模型（Ollama、
  DeepSeek 的思考模式）跑 agent 之前要先核实它们是否要求在 tool 轮次之间传回。
- **preserved thinking 的 400 与别的失败分不开**：投影规则（§4）应当让它不发生；真发生了是 `CallFailed`，
  会触发 `fallback_role`。要不要让 lite-model 给它一个单独的错误类型，S2 做 fallback 时定。
- **streaming**：v1 事件粒度是一轮。交互式 UI 需要逐 token 时，要 lite-model 先支持 streaming。
- **session store 的默认持久化**：提议 PostgreSQL（workspace 惯例，每个项目一个库）。cairn 用文件 +
  锁；要不要加一个 JSONL 文件 store 给单机脚本用。
- **过大的 tool 结果**：只截断，还是把完整结果另存一份（blob 表 / 文件）并在事件里留引用。
- **tool 并行**：同一轮多个 call 是否并行执行；并行时结果事件的顺序按 call 顺序写。
- **投影的开销**：长 session 每一步全量投影；是否按 seq 缓存（KB §9.3 的开放问题，S5 测了再定）。
- **memory 与 llm-steward**：agent 写 memory 是直接用 lite-mem，还是必须经过 llm-steward 的治理。
- **HTTP 端口**：提议 8730（8700 lite-rag、8710 lite-mem、8720–8722 lite-eval）；S6 时在 KB
  `global/machine/ports.md` 登记后才算数。
- **项目 KB 的位置**：提议 `KB/projects/lite-agent/`（S0 创建）；lite-cache 是放在仓库内 `kb/`。
