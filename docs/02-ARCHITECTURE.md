# 02 · 架构设计与选型权衡

## 1. 第一性原理：harness 到底解决什么问题

模型是一个**概率性的意图生成器**；生产系统需要的是**确定性的行为保证**。二者的失配体现在四点：

1. 模型会提出越权动作 → 需要执行前的确定性权限门；
2. 模型会读错/被外部文本操纵 → 需要把外部内容当数据、并把污点传播下去；
3. 模型会「幻觉式完成」→ 需要独立的验证器与评估器；
4. 模型输出非确定 → 评测必须是统计测量（多 trial、pass^k），而不是单次断言。

所以 harness 的定义是：**把模型的意图，转换为受约束、可观测、可测量、可回归的执行**。图框架只覆盖其中「控制流与状态」这一小块。

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ CLI / CI / 审批 UI            （入口与门禁）                        │
├─────────────────────────────────────────────────────────────────┤
│ eval/      任务库 → runner → grader → gate → report → flywheel    │
│            离线质量闭环（与运行时解耦）                            │
├─────────────────────────────────────────────────────────────────┤
│ runtime/   LangGraph StateGraph（控制流）+ ModelGateway（意图）     │
│            Runtime 容器（依赖注入）                                │
├──────────┬──────────┬──────────┬──────────────┬─────────────────┤
│ context/ │ guard/   │ tools/   │ observability│ business/        │
│ 组装压缩 │ 注入/预算│ 注册/沙箱│ span/replay  │ 本体/判定/Prover│
│ 记忆交接 │ 审批/护栏│ 执行治理 │ metrics      │ ImpactBrief      │
└──────────┴──────────┴──────────┴──────────────┴─────────────────┘
```

**依赖方向单向**：`runtime` 依赖下层，`tools` / `guard` / `business` 互不依赖。`business.domain` 里的 `evaluate_artifact` 被运行时 Evaluator 子图与离线 grader 共用，保证线上线下同一把尺。

---

## 3. LangGraph 状态图（实际拓扑）

```
START ─► planner ─┬─(input guard tripwire)────────────────────────► finish ─► END
                  └─► assemble_context ─► generator ─┬─(tool_calls)─► guard_pre
                            ▲                        │                    │
                            │                        │                    ▼
                            │                        │             execute_tools
                            │                        │                    ├─(approval)─► approval ─┐
                            │                        │                    └─(loop)─────┘          │
                            │                        └─(final / budget / loop)──┐              │
                            │                                                   ▼              │
                            └──────────(retry with feedback)────── verify ◄──────────────────┘
                                                                     │
                                                                     ▼
                                              evaluate[独立子图] ─┬─(pass)─► finish ─► END
                                                                  └─(fail, iter<2)─► assemble_context
```

节点与要求对应：

| 节点 | 职责 | 对应要求 |
| --- | --- | --- |
| `planner` | 产出 sprint contract / 验收条件；输入护栏前置 | R20, R05 |
| `assemble_context` | 分层组装、压缩、关键约束重注入、结构化 handoff 校验 | R08, R09 |
| `generator` | 调模型网关产出意图；只提意图，无执行权 | R01, R10 |
| `guard_pre` | 参数级注入信号检测与违规记录 | R05 |
| `execute_tools` | 权限门 → 重试/熔断 → 结果回写 → taint 传播 | R02–R04 |
| `approval` | `interrupt()` 暂停，等待人工 `Command(resume=...)` | R06 |
| `verify` | Prover：schema、证据可追溯、无 PII、goal-mode 复核 | R18 |
| `evaluate` | **独立子图**，只看 artifact + rubric | R11 |
| `finish` | 汇总 stop_reason / metrics / checkpoint 数 | R12 |

### 子图隔离是可验证的

`EvaluatorState` 只声明 `brief / rubric / evidence_registry / signals / evaluation`。LangGraph 在调用编译子图时按子图 schema 过滤父 state，因此父图中的 `messages`、`thoughts`、`pending_calls` 在框架层就被丢弃。实测：

```
evaluation.keys_visible_to_evaluator = ['brief', 'rubric']
```

这不是「在 prompt 里叮嘱评估器别看推理链」，而是结构性隔离，对应 Anthropic 2026 长程 harness 里「模型自我评估系统性偏宽」的结论。

---

## 4. 选型权衡表

| 决策点 | 方案 A | 方案 B（本项目） | 取舍 |
| --- | --- | --- | --- |
| 控制流 | 手写 `while` ReAct 循环 | **LangGraph StateGraph + 条件边** | A 更轻、无框架依赖，但 checkpoint / interrupt / time-travel 要自己造；B 用框架换可靠性与可调试性，代价是递归限制、节点函数必须返回部分状态、调试需理解 super-step |
| 状态持久化 | 自研 JSON checkpoint | **LangGraph checkpointer** | B 免费获得 super-step 快照与 `get_state_history`；但生产要换成 Postgres/Redis saver |
| HITL | 抛异常 + 外部轮询 | **`interrupt()` + `Command(resume=...)`** | B 由框架保存暂停点，恢复时节点从 `interrupt()` 处继续；要求节点内 `interrupt` 调用顺序确定 |
| 评估器隔离 | 同图节点，靠 prompt 约束 | **独立 TypedDict 子图** | B 由 schema 过滤保证；代价是父子状态键必须显式对齐，输出键要在父 schema 声明 |
| 安全边界 | system prompt 写禁令 | **代码级权限门** | prompt 是建议，代码是控制；A 在注入下必然失效。代价是工具接入必须先声明 risk / scope / allowed_hosts |
| taint 粒度 | 读过任何外部内容即禁写 | **仅注入命中时禁止对外副作用** | 舆情 Agent 天生读外部文本，全量禁写会让它无法成文；按「敏感数据 + 不可信内容 + 外发通道」三要素切断更贴近威胁模型 |
| 评测判据 | 全用 LLM-as-judge | **确定性 grader 进门禁，judge 只做软信号** | judge 有位置/长度/自我偏好偏差且非确定，放进 CI 会让门禁 flaky，最终被关掉 |
| 可靠性指标 | 单次通过率 | **pass@k + pass^k 双报** | pass@k 是能力天花板，pass^k 是用户能否依赖；本项目 CAP-004 实测 0.963 vs 0.296 |
| 上下文策略 | 摘要式 compaction | **observation masking + 首尾重注入 + 结构化 handoff** | 摘要会丢掉「未解决的问题」，是幻觉式完成的高发点；折叠旧观测保留原文在磁盘，更安全 |
| 业务判定 | 让模型直接给风险等级 | **规则引擎 ImpactAssessor** | 高风险决策要可审计、可回归，且它同时是 eval 的 ground truth；模型只负责抽取信号 |

---

## 5. 一次 run 的数据流

1. `Runtime.initial_state()` 生成可复现身份：`seed`、`config_fingerprint`、`run_id`、`trace_id`。
2. `planner` 生成验收契约；若任务越界，输入护栏触发 tripwire，零工具调用直接 `finish`。
3. `assemble_context` 渲染分层 prompt（system → 硬约束 → 工具 schema → 记忆 → 历史 → 当前），关键约束在首尾各出现一次。
4. `generator` 通过 `ModelGateway` 产出 `tool_calls` 或 `final`；网关按路由计费、命中前缀缓存、记录 LLM span、录制响应用于回放。
5. `execute_tools`：`ToolRegistry.validate` → `Sandbox.check`（scope 交集 / 出站白名单 / taint / 路径 / DLP）→ 幂等缓存 → 故障注入 → 重试退避/熔断 → post hook（大结果 offload）。
6. 结果回写 `tool_payloads`，外部全文标记 `untrusted`；若检测到注入指令，置 `tainted=True` 并把正文中和为 `<untrusted_data>` 数据块。
7. 需要对外发布时进入 `approval`，`interrupt` 暂停；人工决定回灌后 `approval_done=True`。
8. `verify` 做 Prover 检查；不通过则把具体反馈回灌 `generator`，最多迭代一次。
9. `evaluate` 子图按 rubric 打分；`finish` 汇总 `Trace`、`Metrics`、checkpoint 数、违规清单。
10. `Runtime.save` 落盘 `trace.json` / `spans.jsonl` / `replay.json` / `state.json`。

---

## 6. 框架负责什么、自研负责什么

| LangGraph 提供 | 本项目自研（框架不提供） |
| --- | --- |
| 状态图、条件边、super-step checkpoint | 工具权限模型与沙箱 |
| `interrupt` / `Command(resume)` | 间接注入检测与 taint 传播 |
| 子图输入 schema 过滤 | Token/成本/时延硬预算 |
| 状态历史与 time-travel | 上下文组装/压缩/handoff/记忆投毒防护 |
| — | 全链路 span、指标、审计、轨迹回放 |
| — | 任务库、grader、runner、门禁、A/B、报告 |
| — | 业务判定规则、Prover、GRPO 轨迹导出 |

**结论**：把 harness 等同于「用了某个 Agent 框架」是错的。框架只解决控制流；质量与安全是平台工程，必须自己拥有。这也是本项目在 `Runtime` 里显式注入全部依赖、而不让节点直接 import 全局单例的原因——只有这样，每个 eval trial 才能拿到干净环境。

---

## 7. 可复现性契约

| 变量 | 控制方式 |
| --- | --- |
| 模型输出 | `ScriptedPolicy` 确定性 / `ReplaySource` 回放录制响应 |
| 随机源 | `DeterministicIds(seed)`、`FaultInjector.rng = random.Random(seed)` |
| 配置漂移 | `HarnessConfig.fingerprint()` 写进 trace 与评测报告 |
| 环境共享 | 每 trial 新建 `Runtime` + 新 `InMemorySaver` + 独立信源视图 |
| 时间 | `Tracer` 注入式 clock；预算墙钟使用 `time.monotonic` |

`cli replay` 实测：13 个 LLM 决策回放后工具调用序列与 `stop_reason` 完全一致。
