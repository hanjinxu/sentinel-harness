# 01 · 能力矩阵与公开来源

> 资料整理时间：2026-11-08。范围覆盖 2025 Q4–2026 Q4 公开的 Agent Harness / Agent Infra / Agent Eval 工程实践与技术资料。本文只列可被代码验证的硬性要求，不列「极客精神」这类不可验证项。

---

## 1. 公开资料来源

| # | 资料来源 | 时间 | 关键工程要求 |
| --- | --- | --- | --- |
| 1 | [腾讯混元 · Agent Harness 工程实践](https://careers.tencent.com/jobdesc.html?postId=2052685072754196480) | 2026-10-12 更新 | 执行全链路 tracing & observability；自动化 eval pipeline、A/B testing、regression detection；Agent debugging 工具；标注、SBS 评测、数据管道；重度使用 Cursor / Claude Code / Codex，具备 agentic failure mode 分析能力 |
| 2 | [DJI · Agent 系统（Harness 方向）工程实践](https://freehire.me/jobs/zhong-gao-ji-agentgong-cheng-shi-harness-fang-xiang-dji-pzi5avag) | 2026 | Agent Loop、长任务调度、断点续跑、上下文管理；Tool 基座（注册/校验/重试/熔断/权限隔离/并行调度）；全链路 trace、执行回放、单步调试；Eval Harness（评测流水线、数据集管理、回归、A/B）；稳定性与性能优化；多智能体协同 |
| 3 | [Manus · Agent Harness 工程实践](https://www.thejobsmap.com/job/ccef040c-36cf-4942-baf5-58171f6db4fe) | 2026-10-09 | context/memory、model & tool orchestration、MCP 集成、evaluation & quality feedback；可复现评测与端到端可观测；执行隔离、最小权限、多租户、提示注入防御、高风险动作审批与审计 |
| 4 | [Agent Harness 平台工程能力清单（公开资料）](https://m.liepin.com/job/1986300105.shtml) | 2026-10-23 更新 | 执行循环、状态持久化与断点恢复、上下文压缩；Tool/Skill 注册与参数校验、沙箱隔离；全链路 Tracing、失败归因、回放与单步调试；离线评测与任务完成度的确定性验证；明确列出四类失败模式：上下文丢失、工具误用、循环卡死、幻觉式「完成」 |
| 5 | [Agent Harness 工程能力清单（公开资料）](https://www.zhaopin.com/jobdetail/CCL1528993620J40881781801.htm) | 2026 | 状态机/Graph Runtime、多轮规划与反思闭环、断点续跑、HITL、统一 Agent SDK；Sandbox、权限校验、Guardrails、高风险操作审计与拦截；eval 流水线、A/B、回归检测、Verification/Prover；反馈回路与上下文工程；另提及 seed prover / verifier |
| 6 | [Superagent · Agent Infrastructure / Core Harness](https://www.remotera.ai/jobs/agent-infrastructure-engineer-core-harness-superagent-sGMlcVMQwn) | 2026 | 执行循环针对 latency / reliability / token efficiency / cost 优化；context management、tool routing、function schemas、structured outputs、retries、error recovery；多 provider 集成与 benchmark；缓存、批处理、并行工具执行、prompt/context 压缩；tracing / metrics / regression detection |
| 7 | [Tessl · Harness Engineering 实践](https://www.remote1stjobs.com/jobs/member-of-technical-staff-research-engineer-harness-engineering-and-agentic-orchestration-tessl-mvwjqihp) | 2026-10-31 | outer loop：production traces → 下一轮改进；agentic eval methodology 与数据集构建；failure-mode analysis；训练数据飞轮；另提及 RL / bandits 应用于 LLM |
| 8 | [Agentic AI / Harness 工程实践](https://www.digitaljobs.com/?p=475866) | 2026 | durable state、checkpoints、retries、idempotency；针对 outcome / evidence / tool path 的回归评测；least-privilege tool access、prompt-injection defenses；**把模型推理与确定性控制分离** |
| 9 | [Anthropic · Harness design for long-running application development](https://anthropic.com/engineering/harness-design-long-running-apps) | 2026-03-24 | Planner / Generator / Evaluator 三角色；模型自我评估系统性偏宽，评估器必须独立上下文；context reset + 结构化 handoff vs compaction；grading criteria 是持久资产；sprint contract；面向客户的动作必须人工审批 |
| 10 | [Eval Harness 概念与指标 / Agent Eval 指南](https://serhiismetanskyi.com/docs/ai-harness/03-eval-harness-concepts) · [Agent Evaluation in Production](https://www.llms.blog/posts/ai-agent-evaluation-in-production-trajectory-benchmarks-sandbox-harnesses-and-flakiness-mitigation) · [Awesome Agent Eval 2026](https://baeseokjae.github.io/posts/awesome-agent-eval-curated-list) | 2026 | pass@k 与 pass^k；确定性 grader 优先、LLM-judge 需校准；轨迹级评分（工具白名单/顺序/信息边界）；deterministic trace replay 消除 CI flakiness；分层套件（PR 冒烟 / 夜间 / 模型升级全量）；**逐指标设阈值**；生产失败回流回归集 |
| 11 | [AI 测试工程体系变化（字节/阿里/腾讯）](https://www.sohu.com/a/1093262990_120635785) · [Harness Engineering 实践指南](https://github.com/ai-infra-curriculum/ai-agent-guidebook/blob/main/best-practices/harness-engineering.md) | 2026 | Test Harness 工程体系成为基础要求；MCP 优先；工具调用失败的分层兜底；budget caps、`--max-turns` / `--max-budget-usd`；留出集防止 harness 过拟合 eval |
| 12 | [月之暗面 Kimi Hosted Agents](https://www.sohu.com/a/1097519144_121627717) | 2026-11-05 | 行业趋势佐证：Harness 被云端化为独立服务（云沙箱、会话持久化、中断恢复、执行观测、多智能体协作） |

**共性结论**：2026 年的 Harness 工程已经不是「会调 LLM API / 会写 LangGraph」，而是 **运行时 + 评测 + 治理 + 数据飞轮** 四位一体；公开资料普遍强调「把关键决策从概率模型搬到确定性代码」。

---

## 2. 能力矩阵（21 项 → 代码 → 验证命令）

| ID | 能力要求 | 实现位置 | 验证方式 |
| --- | --- | --- | --- |
| R21 | LangGraph 工程化：StateGraph / checkpointer / interrupt / 子图 | `runtime/graph.py`, `runtime/state.py` | `cli demo` 输出 53–54 个 checkpoint；`tests/test_graph.py` |
| R01 | 执行循环、多步编排、明确终止条件 | `runtime/graph.py` 条件边 | `cli demo` stop_reason；`tests/test_graph.py::test_budget_stop` |
| R02 | 工具注册、参数 Schema、结构化输出、MCP | `tools/registry.py` | `cli mcp`；`tests/test_tools.py::test_schema_validation` |
| R03 | 沙箱、最小权限、权限校验与审计 | `tools/sandbox.py` | `tests/test_guard.py::test_least_privilege` |
| R04 | 重试/退避/熔断/超时/幂等/只读并发 | `tools/executor.py` | `tests/test_tools.py`；`REG-004`、`CAP-004` |
| R05 | 间接提示注入、护栏 tripwire、DLP | `guard/security.py` | `ADV-001`/`ADV-002`；`tests/test_guard.py` |
| R06 | 高风险动作 HITL 审批、断点续跑 | `runtime/graph.py::approval`, `state.py::resume` | `cli demo` 审批中断/恢复；`tests/test_graph.py` |
| R07 | Token/步数/时延/成本硬预算 | `guard/budget.py` | `tests/test_guard.py::test_budget` |
| R08 | 上下文组装、压缩、结构化 handoff、reset | `context/engine.py` | `tests/test_context.py` |
| R09 | 三层记忆与记忆投毒防护 | `context/engine.py::MemoryStore` | `tests/test_context.py::test_memory_poisoning` |
| R10 | 模型网关：路由、fallback、前缀缓存、回放 | `runtime/model.py` | `cli ab`、`cli replay` |
| R11 | Generator–Evaluator 分离与验收契约 | `runtime/graph.py` Evaluator 子图 | `tests/test_graph.py::test_evaluator_isolation` |
| R12 | 全链路 tracing / span / 指标 / 失败归因 | `observability/tracing.py` | `artifacts/*/spans.jsonl` |
| R13 | 版本化任务库、留出集、生产失败回流 | `eval/tasks.py`, `flywheel/pipeline.py` | `conf/tasks.json`；`python main.py export` |
| R14 | 确定性 grader / 轨迹 grader / 校准 LLM-judge | `eval/graders.py` | `tests/test_eval.py` |
| R15 | pass@k / pass^k、多 trial 干净环境 | `eval/runner.py` | `cli eval --suite nightly` |
| R16 | 逐指标 CI 门禁、baseline diff、噪声判定、分层套件 | `eval/gate.py`, `config.py::SUITE_LAYERS` | `cli eval`；`tests/test_eval.py::test_gate` |
| R17 | A/B 与 SBS 对比实验（精确符号检验） | `eval/gate.py::ABTest` | `cli ab` |
| R18 | Prover / goal-mode 复核 / 幻觉引用拦截 | `business/domain.py`, `graph.py::verify` | `tests/test_eval.py::test_prover` |
| R19 | 轨迹 → SFT/GRPO 奖励飞轮、捷径过滤 | `flywheel/pipeline.py` | `cli export` |
| R20 | 多角色分工与结构化交接（schema 版本 + original_goal） | `runtime/graph.py::planner`, `context/engine.py` | `tests/test_context.py::test_handoff` |

---

## 3. 实现范围与已知差距

| 维度 | 当前实现 | 后续工作 |
| --- | --- | --- |
| 运行时与状态 | StateGraph + 条件边 + super-step checkpoint + interrupt HITL | 换持久化 checkpointer；补多租户与分布式调度 |
| 工具与治理 | 进程内权限门、出站 allowlist、taint、DLP、重试/熔断/幂等 | 容器 / microVM 隔离 + 网络策略；接真实 IAM 短期凭据 |
| 可观测与评测 | span / metrics / 确定性 judge 替身 + 校准框架 | 接 LangSmith / Braintrust；真实 judge + 人工黄金集 |
| 上下文工程 | 规则式折叠、首尾重注入、结构化 handoff | 模型摘要式 compaction；实现真正的 context reset 重启会话 |
| 飞轮与业务规则 | 奖励分解可复算、专家规则做影响分级 | 实跑 GRPO；分级规则改为可学习的判定模型 |

> 上述替换点均收敛在 `Runtime` 容器内，替换时不需要改图结构；完整清单见 README「明确的生产差距」。
