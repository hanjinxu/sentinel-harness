# Sentinel Harness — 外部舆情监控 Agent Harness（LangGraph 版）

> 一个把「能跑的 Agent」变成「可交付的 Agent 系统」的工程骨架。
> 业务：监控外部舆情，判定其对本公司的潜在影响，产出带证据链的结构化影响简报。
> 编排框架：**LangGraph 1.x**（StateGraph / checkpointer / interrupt / 子图；本机实测 1.2.20）。

---

## 一句话结论

Harness 不是 Agent 本身，而是**包在模型外面的确定性控制系统**。本项目把 2026 年公开 Agent Harness 工程实践中沉淀出的 21 类能力要求，逐条落到可运行、可评测、可回归的代码上；业务场景选舆情监控，是因为它天然把「不可信外部内容」放在数据通路上，是检验 harness 安全设计最苛刻也最真实的场景。

---

## 为什么业务选舆情（第一性原理）

| 约束 | 推论 |
| --- | --- |
| Agent 必须阅读大量外部文本 | 每条微博/帖文/新闻都是**间接提示注入**载体 |
| 结论影响公司决策 | 影响分级必须**可审计、可复现**，不能由模型自由裁量 |
| 对外发预警不可逆 | 必须有**高风险动作审批门**与出站白名单 |
| 结论要能追溯 | 证据 ID 必须命中信源登记表，否则是幻觉 |
| 监控是常态任务 | 成本/时延必须**有硬预算**，不能无限循环 |

因此这个业务把 harness 的四条主线——**运行时、评测、治理、飞轮**——全部逼出来，而不是只做一个「调 API + 拼 prompt」的 demo。

---

## 快速开始

环境：**Python 3.10+**（本机实测 3.13.12）+ LangGraph 1.x。依赖仅 `langgraph` 与 `langchain-core`，**不依赖 PyTorch / CUDA，不需要 GPU**。无 API Key、无网络依赖，全部离线可跑。所有命令都从项目根目录执行，统一入口是 `main.py`。依赖由 `./setup.sh` 装进项目内的 `.venv`。

```bash
git clone https://github.com/hanjinxu/sentinel-harness.git
cd sentinel-harness
./setup.sh                       # 创建 .venv、清华源安装依赖 + 离线自检
source .venv/bin/activate        # 之后可直接用 python main.py ...
python main.py --help

python main.py requirements     # 打印 21 项能力要求 → 实现模块映射
python main.py demo             # 完整舆情链路（含 HITL 中断/恢复）
python main.py eval --suite pr_smoke     # PR 冒烟 + CI 门禁
python main.py eval --suite nightly      # 夜间全量（k=3）
python main.py eval --suite full --include-held-out
python main.py replay           # 确定性轨迹回放验证
python main.py ab               # 前缀缓存开/关 的 SBS 对比
python main.py export           # 导出 GRPO 训练轨迹
python main.py mcp              # 导出 MCP tool schema
python -m unittest discover -s tests -v
```

也可以直接用根目录下的 `./demo.sh`、`./ci_gate.sh`，或 `make demo` / `make ci` / `make nightly` / `make test`。自定义配置用 `python main.py --config conf/harness.json demo`。

### 运行环境（容器 / AutoDL / conda）

| 约束 | 结论 |
| --- | --- |
| Python | 3.10+；源码只用通用标准库，3.11 / 3.12 / 3.13 均可 |
| PyTorch / CUDA | 完全不 import，版本任意，GPU 非必需 |
| 第三方依赖 | 仅 `langgraph`、`langchain-core` 及其传递依赖（`pydantic`、`xxhash` 等） |

已有 conda 环境时不必再建 `.venv`：

```bash
conda activate <your-env>        # 或任意 Python 3.10+ 解释器
pip install -r requirement.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py demo
PY="$(which python)" ./ci_gate.sh   # 脚本支持用 PY 指定解释器
```

`./setup.sh` 默认执行 `python3 -m venv .venv`；若镜像里 `python3` 不是目标解释器，用 `PY=/path/to/python ./setup.sh` 覆盖。若同一台机器还要跑训练任务，建议为 harness 单独建环境，避免共享 `pydantic` 等传递依赖时被版本升级牵动。

AutoDL 容器里 `git clone` 卡住时，先执行 `source /etc/network_turbo` 开启学术加速（仅对当前终端生效），clone 完成后 `unset http_proxy https_proxy` 再装依赖。conda 的清华 anaconda channel 报 `HTTP 403 FORBIDDEN` 无需处理——本项目不需要 conda，直接用任意 Python 3.10+ 解释器建 venv 即可。

### 实测结果（本机，2026-11-08）

| 项目 | 结果 |
| --- | --- |
| Demo | 12 次工具调用、53 个 checkpoint、HITL 审批被人工拒绝、Evaluator 评分 1.0、风险等级 CRITICAL |
| pr_smoke | 7 trials，回归/对抗通过率 100%，CI 门禁 PASS |
| nightly | 36 trials，总体 90% 左右；回归/对抗/负例 100%，能力集暴露真实缺口；**CAP-004 pass@3=0.963 而 pass³=0.296** |
| replay | 录制 13 个 LLM 决策，回放后工具序列与 stop_reason 完全一致，`DETERMINISTIC=True` |
| A/B | 前缀缓存开：¥0.4324 / 关：¥0.9598，**通过率不变、成本下降约 55%** |

> 说明：能力集故意保留两个已知缺口（持续超时、持续故障），用于展示「能力评测看趋势、回归评测做硬门」的分层原则；把能力集做成 100% 反而失去信息量。

---

## 能力覆盖（21 项，逐条可验证）

能力清单参考 2026 年公开的 Agent Harness 工程实践与技术资料：腾讯混元、DJI、Manus、Tessl、Superagent 等团队的实践，以及 Anthropic 2026 年长程 Harness 设计、agent eval 方法论等。**来源链接与逐条映射见 [docs/01-REQUIREMENTS-MATRIX.md](docs/01-REQUIREMENTS-MATRIX.md)。**

| 层 | 覆盖要点 |
| --- | --- |
| 运行时 | StateGraph 条件边循环、super-step checkpoint、interrupt HITL、Evaluator 独立子图、模型网关路由/缓存/fallback/回放 |
| 工具与治理 | JSON Schema 校验、MCP 导出、最小权限 scope 交集、出站 allowlist、taint 传播、DLP、审批门、重试/退避/熔断/幂等/只读并发 |
| 上下文 | 分层 prompt 优先级栈、首尾重注入、observation masking、大结果 offload、结构化 handoff、三层记忆与投毒防护 |
| 评测 | 版本化任务库 + 留出集、确定性 grader、轨迹 grader、校准过的 LLM-as-judge、pass@k / pass^k、逐指标 CI 门禁、噪声判定、SBS A/B、trace replay |
| 飞轮 | 失败聚类 → 新回归任务；轨迹 → SFT/GRPO 样本，奖励 = 质量 + 新颖度 + token 节约 − 早停/循环惩罚，捷径轨迹过滤 |
| 业务 | 舆情主题本体、确定性影响分级规则、ImpactBrief 结构化简报、Prover 证据核验 |

---

## 目录结构

根目录平铺，入口、配置、数据、脚本同级：

```
main.py                 统一入口（自动把 src/ 加入模块路径）
requirement.txt         依赖清单（仅 LangGraph + langchain-core）
setup.sh                安装 + 离线自检
demo.sh  ci_gate.sh     一键演示 / CI 门禁入口
Makefile                make demo | ci | nightly | full | test
LICENSE                 MIT
README.md  pyproject.toml  .gitignore

conf/                   行为配置：harness.json（默认运行配置）
                        tasks.json（14 个版本化评测任务）
                        suites.json（分层评测套件定义）
db/                     数据资产：feeds.json（离线信源 fixtures）
                        baseline.json（评测基线）
src/sentinel/           代码包
  core.py config.py                 领域模型 / 版本化配置与能力矩阵
  tools/      registry sandbox executor builtin
  guard/      security budget
  context/    engine
  runtime/    graph state model services   ← LangGraph 图在此
  observability/ tracing
  eval/       tasks graders runner gate report
  flywheel/   pipeline
  business/   domain
  cli.py                                 8 个子命令
docs/         01 能力矩阵 · 02 架构与权衡 · 03 设计说明与问答 · 04 失败模式
tests/        51 个单元测试
artifacts/    运行产物（gitignore：trace / spans.jsonl / replay.json / 报告）
```

`conf/` 与 `db/` 的职责边界：**改 conf 改变系统行为，改 db 只改变输入数据与比较基准**。

---

## 三个核心设计决策

1. **框架与 harness 的边界。** LangGraph 只负责控制流、状态、checkpoint、interrupt、子图隔离；权限门、注入防护、预算、评测、观测、业务规则全部自研并显式注入。把这些交给框架是常见的架构错误——框架不提供，也不会替你承担。
2. **Generator 与 Evaluator 的隔离由 schema 保证，而不是靠 prompt 叮嘱。** Evaluator 子图的 `TypedDict` 只声明 `brief/rubric/evidence_registry/signals`，LangGraph 会按子图 schema 过滤父 state，因此 `messages/thoughts` 在框架层就传不进去。这是可验证的硬边界。
3. **安全策略写在代码里。** system prompt 里写「禁止外发」只是建议；本项目在 `Sandbox.check` 里做 scope 交集、出站 allowlist、taint 传播、路径约束，模型即使被注入文本说服也执行不了。

---

## 明确的生产差距（不掩饰）

| 差距 | 现状 | 生产替代 |
| --- | --- | --- |
| 模型 | `ScriptedPolicy` 确定性 stub | 接 Qwen-32B / Claude / 自部署推理服务 |
| 沙箱 | 进程内权限门 + 路径/主机 allowlist | 容器 / Firecracker microVM + 网络策略 |
| checkpoint | `InMemorySaver` | Postgres/Redis checkpointer |
| 审批 | CLI 传入 `Command(resume=...)` | 真实审批 UI / 工单系统 |
| judge | 确定性启发式替身 + 校准框架 | 真实 judge 模型 + 人工黄金集 |
| 数据 | 离线 fixtures | 舆情采集管道 + 信源可信度服务 |

这些替换点都收敛在 `Runtime` 容器内，替换时不需要改图结构。

---

## 文档索引

- [01 能力矩阵与公开来源](docs/01-REQUIREMENTS-MATRIX.md) — 公开资料出处、能力映射、已实现范围与差距
- [02 架构设计与选型权衡](docs/02-ARCHITECTURE.md) — 分层、状态图、数据流、对比表
- [03 设计说明与技术问答](docs/03-DESIGN-NOTES.md) — 设计动机、关键取舍、实测结果、常见技术问题
- [04 失败模式与兜底](docs/04-FAILURE-MODES.md) — 失效分类、对应控制点、验证用例

---

## 开源协议

MIT，见 [LICENSE](LICENSE)。欢迎 issue / PR；提交前请确保 `./ci_gate.sh` 通过（51 个单测 + PR 冒烟门禁）。

维护者发布：先在 GitHub 创建同名 public 空仓库（不勾选初始化文件），再执行 `./publish.sh`。
