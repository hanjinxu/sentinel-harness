# 04 · 失败模式、兜底与验证用例

> 长程 Agent 系统的可靠性取决于对 failure mode 的覆盖。本文件把每一类失效映射到**具体检测点、兜底动作、可运行用例**，而不是停留在概念列表。

---

## 1. 执行类失效

| 失效模式 | 检测点 | 兜底动作 | 验证用例 |
| --- | --- | --- | --- |
| 上游瞬时 503 | `FaultInjector.flaky` / `ToolResult.error_class=TRANSIENT` | 指数退避重试，上限 2 次（Stripe 生产实践） | `REG-004` |
| 工具持续不可用 | `_consecutive_failures >= 2` | 打开熔断器，后续直接返回 `FATAL`，避免反复撞同一故障 | `CAP-004` |
| 工具超时 | `FaultProfile.timeouts` | 归类 TRANSIENT；重试耗尽后保留错误在上下文，有界停止 | `CAP-002` |
| 参数不合法 | `ToolRegistry.validate` | `LLM_RECOVERABLE`：错误作为 ToolMessage 回传给模型改参 | `tests/test_tools.py` |
| 模型反复调用同一动作 | `_route_generator` 连续 3 次相同调用签名 | 触发 `LOOP_DETECTED` 并 finish，不耗尽预算 | `CAP-002`, `CAP-004` |
| 步数/token/成本/墙钟失控 | `BudgetGuard.check()` | 硬上限，对应 `MAX_TURNS`/`TOKEN_BUDGET`/`COST_BUDGET`/`WALL_CLOCK` | `tests/test_guard.py` |
| 提前宣布完成（幻觉式完成） | `verify` 节点 Prover | 无简报/证据缺失即把具体反馈回灌 Generator，最多迭代一次 | `tests/test_eval.py::test_prover` |

**复合失败必须量化**：单步 99% 成功率，10 步端到端约 `0.99^10 ≈ 90.4%`。这就是为什么重试、幂等、checkpoint 不是可选项。

### 错误分类 → 动作对照

| ErrorClass | 是否重试 | 去向 |
| --- | --- | --- |
| `TRANSIENT` | 是（退避 1ms→2ms，上限 2） | 成功则清零连续失败计数 |
| `LLM_RECOVERABLE` | 否 | 作为 ToolMessage 回传模型自纠错 |
| `USER_FIXABLE` | 否 | `interrupt` 请求人工输入 |
| `POLICY` | **否** | 记审计、不重试、工具进 `blocked_tools` |
| `BUDGET` | **否** | 立即停止 |
| `FATAL` | 否 | 冒泡，保留现场与 trace |

---

## 2. 上下文类失效

| 失效模式 | 缓解 | 实现 |
| --- | --- | --- |
| Lost-in-the-middle | 关键约束在 prompt 首尾各注入一次 | `ContextEngine.assemble` |
| 长任务 coherence 衰减 / context anxiety | observation masking + 结构化 handoff（含 schema 版本、original_goal 必填） | `compact`, `structured_handoff`, `validate_handoff` |
| 摘要丢掉「未解决的问题」 | 不压缩决策/错误/证据 ID，只折叠旧工具输出 | `compact` |
| 大工具结果挤爆窗口 | 头尾留窗口，全文落盘按需读回 | `offload` |
| 多跳交接目标漂移 | handoff 必带 `original_goal`，版本不符直接拒绝 | `validate_handoff` |
| 记忆投毒 | 不可信来源内容禁止提升到 archival 长期记忆 | `MemoryStore.put(trusted=False)` |

---

## 3. 安全类失效

| 攻击面 | 本项目的切断点 | 对抗用例 |
| --- | --- | --- |
| 间接提示注入（外部正文夹带指令） | `InjectionDetector` 中和为 `<untrusted_data>` 数据块 + `tainted=True` | `ADV-001`, `ADV-002` |
| 致命三要素（敏感数据 + 不可信内容 + 外发通道） | `Sandbox.taint_propagation`：注入命中后禁止 EXTERNAL/DESTRUCTIVE | `ADV-002`（目的地伪装成白名单仍能拦） |
| 出站数据外泄 | 目的地 allowlist（url/host/destination/endpoint + 像地址的 channel） | `ADV-001` |
| 权限过宽 | scope 交集：`工具声明 scope ∩ run 授予 scope`，只能收窄不能放大 | `tests/test_guard.py` |
| 沙箱路径穿越 | 路径 `..` / `/etc` / `~` 拒绝 | `ADV-004` |
| 凭据/PII 泄漏 | 参数与输出 DLP 脱敏 | `guard/output_guard` |
| 审批疲劳 | 只对不可逆外部动作问一次，读/分析自动执行 | `ApprovalGate.needs_human` |
| 越权任务（要求关闭护栏/批量导数据） | 输入护栏 critical → tripwire，零工具执行 | `ADV-003`, `NEG-001` |
| 工具供应链（第三方 MCP 描述注入） | `ToolSpec` 版本化 + scope/risk 声明，不自动信任外部描述 | `to_mcp_schema` 的 `_sentinel` 标注 |

> **重要边界**：prompt 里写「禁止外发」不是控制，`Sandbox.check` 里的 `if` 才是。所有上述规则都在模型执行路径之外，模型即使被说服也拿不到执行权。

---

## 4. 评测自身的失效（最容易被忽略）

| 失效 | 症状 | 对策 |
| --- | --- | --- |
| 共享状态导致假通过 | 上一 trial 的缓存/预热数据库让本 trial 通过 | 每 trial 新建 `Runtime` + 新 checkpointer + 独立信源视图 |
| 单次通过率当结论 | 碰巧过一次就发布 | 报 pass@k 与 pass^k，生产看后者 |
| LLM-judge 漂移/偏差 | 位置偏好、长度偏好、模型自我评估偏宽 | 二元判据、few-shot 校准、3 次多数投票、只做夜间软信号 |
| CI flaky | 模型抖动导致门禁随机失败，最后被关掉 | 确定性 grader 进门禁 + trace replay + 噪声判定 |
| 复合分数掩盖局部回归 | 总分 92% 但某工具路径 40% 失败 | 逐类别、逐指标阈值 |
| eval 集腐烂 / 过拟合 | 调 harness 调成「背题」 | 留出集 + 生产失败回流 + 任务版本化 |
| 刚性步骤检查 | 合法的替代路径被判失败 | 只校验关键节点相对顺序，grade outcome 为主 |

---

## 5. 回归用例总表

| ID | 类别 | 验证的失效模式 |
| --- | --- | --- |
| REG-001 | 回归 | 高影响 happy path + 证据链 + 人工复核标记 |
| REG-002 | 回归 | 中等影响、正负混合情感 |
| REG-003 | 回归 | 正面舆情不得误报为负面 |
| REG-004 | 回归 | 瞬时故障下的重试恢复 |
| REG-005 | 回归（留出集） | 高管言论场景泛化 |
| CAP-001 | 能力 | 多源交叉 + 部分工具抖动 |
| CAP-002 | 能力（预期失败） | 持续超时下必须有界停止而非死循环 |
| CAP-003 | 能力 | 同名主体消歧 |
| CAP-004 | 能力（概率性） | 持续故障下 pass@k 与 pass^k 分离 |
| ADV-001 | 对抗 | 注入 + 外部域名外泄 |
| ADV-002 | 对抗 | 注入 + 白名单域名伪装（验证 taint 独立生效） |
| ADV-003 | 对抗 | 越权输入零工具执行 |
| ADV-004 | 对抗（留出集） | 沙箱路径穿越 |
| NEG-001 | 负例 | 要求绕过护栏必须拒绝 |

---

## 6. 尚未覆盖的生产失效（诚实清单）

1. **多租户隔离**：当前是单进程单 run，未做租户级凭据隔离与配额。
2. **真实容器逃逸**：路径/主机检查是应用层策略，不能替代内核级隔离。
3. **模型网关限流与降级**：`ModelGateway` 有 fallback 记录位，但未接真实多 provider 路由与 rate limit。
4. **审批 UI 与社会工程**：`Command(resume=...)` 由调用方传入，生产需把审批权绑定到真实身份与审计日志。
5. **judge 的真实漂移**：校准框架完整，但校准集是内置的 4 条黄金样本，生产需持续用人工标注回流。
6. **长期记忆持久化**：`archival` 层在进程内，未接向量库/关系库。

以上每项都有明确替换点，且都收敛在 `Runtime` 容器内；替换不改变 StateGraph 拓扑，这是依赖注入带来的可演进性。
