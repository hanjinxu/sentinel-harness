# 03 · 设计说明与技术问答

> 口径：结论先行、术语精确、数字可复算。按「设计动机 → 实现分层 → 关键取舍」展开，并附常见问题与后续工作。

---

## 1. 项目概览

本项目实现的是**外部舆情监控 Agent 的 Harness**，不是 Agent 本身。业务上它监控外部提及、做主体消歧与影响分级，产出带证据链的简报；工程上用 LangGraph 承担控制流与 checkpoint，自研权限门、注入防护、上下文工程、eval 门禁和轨迹飞轮。核心判断是：**模型只负责产出意图，凡是需要保证的行为都由确定性代码控制**。离线可跑，21 项能力要求逐条有代码与评测用例对应，nightly 36 个 trial 回归/对抗/负例全绿，CI 门禁 PASS。

---

## 2. 设计动机

第一性原理：模型是概率性意图生成器，生产系统要的是确定性行为保证。失配有四个：

| 失配 | 后果 | 必须补的能力 |
| --- | --- | --- |
| 会提出越权动作 | 注入成功即数据外泄 | 执行前权限门 |
| 会被外部文本操纵 | 舆情文本天然是注入载体 | 数据/指令分离 + taint 传播 |
| 会「幻觉式完成」 | 简报引用不存在的信源 | 独立 Prover 与 Evaluator |
| 输出非确定 | 单次通过率不能代表可靠性 | 多 trial + pass^k + 回归门禁 |

选择舆情业务是因为它强制 Agent 阅读大量不可信外部文本、结论影响公司决策、对外预警不可逆——这三点把 harness 的四条主线（运行时 / 评测 / 治理 / 飞轮）全部逼出来。

---

## 3. 实现分层

**分层原则**：LangGraph 管控制流，其余全部自研并由 `Runtime` 容器显式注入。

- **运行时**：StateGraph 条件边形成 ReAct 循环；`checkpointer` 做 super-step 快照与断点恢复；`interrupt` + `Command(resume=...)` 做高风险动作人工审批；独立 `EvaluatorState` 子图做评估。
- **工具治理**：`ToolSpec` 声明 risk / scopes / JSON Schema / allowed_hosts，同时导出 OpenAI function 与 MCP schema；执行前做参数校验、scope 交集、出站白名单、taint、路径约束、DLP；执行时做重试退避、熔断、超时、幂等、只读并发。
- **上下文**：分层 prompt 优先级栈、关键约束首尾重注入、observation masking、大结果 offload、带 schema 版本与 `original_goal` 的结构化 handoff、三层记忆与投毒防护。
- **评测**：14 个版本化任务（含留出集）→ 每任务 k trial 干净环境 → 确定性 outcome/trajectory grader → 校准过的 LLM judge（只做软信号）→ 逐指标门禁 + baseline diff + 噪声判定 → 分层套件（PR 冒烟 / 夜间 / 全量）。
- **飞轮**：失败轨迹聚类成新回归任务；成功轨迹导出 SFT/GRPO 样本，奖励 = 质量 + 新颖度 + token 节约 − 早停惩罚 − 循环惩罚，捷径轨迹即便高分也剔除。

---

## 4. 关键取舍

三个最关键的取舍：

1. **为什么用 LangGraph 而不是手写 while 循环**：手写循环轻量，但 checkpoint、interrupt、time-travel、子图隔离都要自己造；框架把这些做成原语，可以把精力放在框架不提供的质量与安全层。代价是节点必须返回部分状态、要理解 super-step 与递归限制。
2. **为什么 taint 只禁「对外副作用」而不禁所有写**：舆情 Agent 必然读外部文本，若读过就禁写，它连简报都产不出来。真正的威胁模型是「敏感数据 + 不可信内容 + 外发通道」三要素同时成立，所以切断的是外发通道，内部可逆写入仍允许但留审计。
3. **为什么 LLM-as-judge 不进 CI 硬门禁**：judge 有位置、长度、自我偏好偏差且本身非确定。放进发布门禁会让 CI flaky，门禁最终会被关掉，eval 退化成没人看的报表。所以门禁只用确定性 grader，judge 只跑夜间并需对黄金集校准。

### 取舍一览

| 取舍 | 本项目选择 | 代价 / 反例 |
| --- | --- | --- |
| 框架 vs 自研边界 | 框架只做控制流，其余自研 | 把 eval 也塞进图节点会导致评测与运行时耦合、无法离线复现 |
| 评估器隔离 | 独立子图 + schema 过滤 | 若只是 prompt 里写「请客观」，评估器仍看得到 Generator 的推理链，会系统性偏宽 |
| 业务判定 | 规则引擎，不用模型 | 规则需要领域维护；但模型直接给等级会让 ground truth 也变成概率的，评测失去基准 |
| 能力集不满分 | 保留两个已知失败场景 | 全 100% 的评测集已经饱和，不再提供信息；但要明确标注「预期能力缺口」而非掩盖 |
| 安全强度 | 注入命中即禁外发 | 误报会阻断合法的对外预警，需要人工审批作为第二条路径 |

---

## 5. 实测结果

| 指标 | 实测 |
| --- | --- |
| Demo | 12 次工具调用、53–54 个 checkpoint、审批中断/恢复、Evaluator 评分 1.0、风险等级 CRITICAL |
| pr_smoke | 7 trials，回归 100% / 对抗 100%，门禁 PASS |
| nightly | 36 trials，回归/对抗/负例 100%，能力集 66.7%（刻意保留缺口） |
| pass@k vs pass^k | CAP-004：pass@3 = 0.963，pass³ = 0.296 —— 能解 ≠ 可依赖 |
| 确定性 | replay 13 个 LLM 决策，工具序列与 stop_reason 完全一致 |
| 成本实验 | 前缀缓存开 ¥0.4324 / 关 ¥0.9598，通过率不变，成本降约 55% |
| 单次 run | prompt 32.5k tokens、成本 ¥0.081、时延 83 ms、缓存命中率 64.3% |

---

## 6. 常见技术问题

**Q1：Harness 和 Agent 框架的区别？**
Agent 框架给编排原语；harness 是让模型可靠运行、可观测、可评估、可回归的完整工程层。LangGraph 提供状态图/checkpoint/interrupt，但权限、注入防护、预算、评测、观测它都不提供——这些是平台工程，必须自己拥有。

**Q2：checkpoint 恢复时 `interrupt` 节点会被重复执行吗？会不会重复发预警？**
节点会从 `interrupt()` 处继续执行，前面的代码会重跑，所以节点内必须只做无副作用操作后再 `interrupt`。本项目把「实际执行」放在审批通过分支，并用 `approved_tools` 当次授权 + 工具幂等键约束；生产上还要把外部动作做成幂等消息（idempotency key = run_id + tool + args hash）。

**Q3：间接提示注入具体怎么防？**
三层：① 外部正文包装为 `<untrusted_data>` 数据块并剥离指令行；② 检测到注入就置 `tainted=True`，Sandbox 禁止任何 EXTERNAL/DESTRUCTIVE 工具；③ 出站目的地 allowlist + DLP，即使前两层失效也送不出去。对抗用例 ADV-001（外部域名，egress 拦）与 ADV-002（伪装成白名单域名，taint 拦）分别验证两层独立生效。

**Q4：pass@k 与 pass^k 的公式和工程含义？**
单次成功率 p、k 次独立试验：pass@k = 1 − (1 − p)^k，pass^k = p^k。p=0.8、k=3 时分别是 99.2% 与 51.2%。前者是能力天花板（demo 够用），后者是生产可靠性（用户每次都要能用）。本项目 CAP-004 实测 0.963 / 0.296。

**Q5：CI 里 LLM 输出不稳定导致 flaky 怎么办？**
三条：① 门禁只用确定性 grader；② `ReplayRecorder` 录制 LLM 响应，CI 回放，隔离模型漂移与网络抖动；③ 判定回归前先估同配置噪声，gap 小于噪声或 `min_noise_margin` 判 `INCONCLUSIVE` 并提示加 trial，而不是直接 fail。

**Q6：为什么逐指标设阈值？**
复合 92% 可能掩盖某类工具路径 40% 的失败。本项目对 regression / adversarial / negative 分别设 100% 硬门，capability 只作趋势，另设 trajectory_score、单任务成本、时延阈值。

**Q7：工具调用失败怎么兜底？**
按错误分类而非一律重试：`TRANSIENT` 指数退避上限 2 次；`LLM_RECOVERABLE`（参数校验失败）把错误作为 ToolMessage 回传让模型改参；`USER_FIXABLE` 中断请求人工；`POLICY`/`BUDGET` 不重试；`FATAL` 冒泡保留现场。连续 2 次工具级失败触发熔断。复合失败要注意：单步 99% 成功率，10 步端到端只有约 90.4%。

**Q8：上下文压缩丢了关键信息怎么办？**
不做摘要式压缩，改为 observation masking：折叠旧工具输出但保留工具调用、**错误**、决策与证据 ID；大结果头尾留窗口、全文落盘按需读回；跨会话交接用带 schema 版本 + `original_goal` 必填字段的结构化 handoff，并做 `validate_handoff`。关键约束在 prompt 首尾各放一次，缓解 lost-in-the-middle。

**Q9：怎么证明一次 harness 改动真的有效？**
固定任务集与模型、一次只改一个变量、每任务多 trial、跑 headless、记录通过率/成本/轮次；`ABTest` 做同 seed 配对的 SBS 对比并用精确符号检验判显著；同时保留留出集防止把 harness 过拟合到评测集。本项目的 A/B：开前缀缓存，通过率不变、成本降 55%。

**Q10：轨迹怎么变成训练信号？**
`export` 把 trace 转成 SFT 完整轨迹 / GRPO 组内样本，judge 分数作为 reward；分解为质量 + 新颖度 + token 节约 − 早停惩罚 − 循环惩罚。关键是**捷径过滤**：检测到幻觉证据、可疑外发、篡改判定的轨迹，无论最终是否成功都标为不可训练（行为本身就是监督信号），否则会把 reward hacking 训进策略。

**Q11：模型升级后哪些 harness 组件可以删？**
Anthropic 2026 的实践是「每换一次模型，逐个移除组件测影响」。本项目里最容易随模型变强而退化的是规则式压缩、显式 planner 与部分重试策略；但权限门、出站 allowlist、审批门、证据 Prover **不能删**——它们防的是权限越界而非模型能力不足。

**Q12：这个系统离生产还差什么？**
checkpointer 换 Postgres/Redis；进程内权限门换容器或 microVM + 网络策略；审批接真实工单 UI；judge 接真实模型并用人工黄金集校准；模型换成自部署 Qwen-32B 并加网关限流/降级。这些替换点都收敛在 `Runtime` 容器内，不动图结构。

---

## 7. 后续工作

| 组件 | 当前实现 | 生产替代 |
| --- | --- | --- |
| 模型 | `ScriptedPolicy` 确定性 stub | 自部署 Qwen-32B / Claude / 推理服务，加网关限流与降级 |
| checkpointer | `InMemorySaver` | Postgres / Redis checkpointer |
| 权限门 | 进程内策略门 + 路径/主机 allowlist | 容器或 microVM + 网络策略 |
| 审批 | CLI 传入 `Command(resume=...)` | 真实审批 UI / 工单系统，绑定身份与审计日志 |
| judge | 确定性替身 + 校准框架 | 真实 judge 模型 + 人工黄金集 |
| 数据 | 离线 fixtures | 舆情采集管道 + 信源可信度服务 |
| 训练 | 奖励分解与轨迹导出 | 实跑 GRPO，并持续检测 reward hacking |

这些替换点都收敛在 `Runtime` 容器内，替换时不需要改图结构。

**一句话结论**：这个项目的核心不是「用了 LangGraph」，而是明确了**哪些东西不能交给框架、也不能交给模型**。
