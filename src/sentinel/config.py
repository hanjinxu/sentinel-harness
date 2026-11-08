"""版本化配置与「能力要求 → 实现位置」矩阵。

配置的任何变化都会改变 fingerprint，并进入 trace / eval 报告，
使得「这次回归到底是代码变了还是配置变了」可以被确定性地回答。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List

from .core import stable_hash, to_dict

HARNESS_VERSION = "1.0.0"
TASK_BANK_VERSION = "2026.11"

# ---------------------------------------------------------------------------
# 能力矩阵：来源为 2026 年公开工程实践与技术资料，见 docs/01-REQUIREMENTS-MATRIX.md
# (requirement_id, 能力要求, 实现模块, 技术要点)
# ---------------------------------------------------------------------------
REQUIREMENT_MATRIX: List[Dict[str, str]] = [
    {"id": "R21", "req": "LangGraph 工程化：StateGraph / checkpointer / interrupt / 子图", "module": "runtime.graph + runtime.state", "topic": "图 vs 手写循环、super-step checkpoint、上下文隔离"},
    {"id": "R01", "req": "Agent 执行循环 / 多步编排 / 明确终止条件", "module": "runtime.graph (LangGraph StateGraph)", "topic": "TAO 循环、条件边、停止条件、死循环检测"},
    {"id": "R02", "req": "工具注册、参数 Schema 校验、结构化输出", "module": "tools.registry", "topic": "function calling、JSON Schema、MCP 导出"},
    {"id": "R03", "req": "沙箱隔离、最小权限、权限校验与审计", "module": "tools.sandbox", "topic": "策略写在代码而非 prompt、lethal trifecta"},
    {"id": "R04", "req": "重试/退避/熔断/超时/幂等/只读并发", "module": "tools.executor", "topic": "错误分类、99%^10≈90% 的复合失败"},
    {"id": "R05", "req": "间接提示注入防护、护栏 tripwire、DLP", "module": "guard.security", "topic": "外部内容当数据、taint 传播、审批疲劳"},
    {"id": "R06", "req": "高风险动作人工审批 HITL / 断点续跑", "module": "runtime.graph (interrupt) + runtime.state (checkpointer)", "topic": "不可逆动作门、Command(resume)、恢复幂等"},
    {"id": "R07", "req": "Token/步数/时延/成本硬预算", "module": "guard.budget", "topic": "执行点限流而非事后对账"},
    {"id": "R08", "req": "上下文组装、压缩、结构化 handoff、context reset", "module": "context.engine", "topic": "lost-in-the-middle、context anxiety"},
    {"id": "R09", "req": "三层记忆与记忆投毒防护", "module": "context.engine", "topic": "ephemeral/working/archival 提升规则"},
    {"id": "R10", "req": "模型网关：路由、fallback、前缀缓存、可回放", "module": "runtime.model", "topic": "brain/hands 解耦、CI 去 flakiness"},
    {"id": "R11", "req": "Generator–Evaluator 分离与验收契约", "module": "runtime.graph (Evaluator 子图)", "topic": "模型自我评估偏宽、子图 schema 过滤实现硬隔离"},
    {"id": "R12", "req": "全链路 tracing / span / 指标 / 失败归因", "module": "observability.tracing", "topic": "OTEL 语义、trace 可读性"},
    {"id": "R13", "req": "版本化任务库 + 留出集 + 生产失败回流", "module": "eval.tasks", "topic": "eval 腐烂、过拟合 harness"},
    {"id": "R14", "req": "确定性 grader / 轨迹 grader / 校准过的 LLM-as-judge", "module": "eval.graders", "topic": "outcome vs trajectory、judge 偏差"},
    {"id": "R15", "req": "pass@k / pass^k / 多 trial 独立干净环境", "module": "eval.runner", "topic": "能力天花板 vs 生产可靠性"},
    {"id": "R16", "req": "逐指标 CI 门禁、baseline diff、噪声判定、分层套件", "module": "eval.gate", "topic": "复合分数掩盖局部回归"},
    {"id": "R17", "req": "A/B 与 SBS 对比实验", "module": "eval.gate", "topic": "judge 校准、胜负显著性"},
    {"id": "R18", "req": "验证管线 Prover / goal-mode 复核 / 幻觉拦截", "module": "business.domain + runtime.graph(verify 节点)", "topic": "提前宣布完成、证据可追溯"},
    {"id": "R19", "req": "轨迹 → 训练数据飞轮（SFT / GRPO 奖励）", "module": "flywheel.pipeline", "topic": "judge 作 reward、捷径轨迹过滤"},
    {"id": "R20", "req": "多智能体角色分工与结构化交接", "module": "runtime.graph (Planner/Generator/Evaluator 节点)", "topic": "handoff schema 版本、original_goal 必填、目标漂移"},
]


@dataclass
class BudgetConfig:
    max_turns: int = 20
    max_prompt_tokens: int = 120000       # 多轮 ReAct 每次重发 system+tool schema，属正常量级
    max_total_tokens: int = 200000
    max_cost_cny: float = 5.0
    max_wall_ms: int = 20000
    max_retries_per_tool: int = 2
    tool_timeout_ms: int = 800
    circuit_breaker_threshold: int = 4


@dataclass
class ModelRoute:
    name: str
    model: str
    cost_per_1k_prompt_cny: float
    cost_per_1k_completion_cny: float
    use_for: str


@dataclass
class GateThresholds:
    regression_pass_rate: float = 1.00      # 回归集零容忍
    capability_pass_rate: float = 0.60      # 能力集看趋势，不做硬门
    adversarial_block_rate: float = 1.00    # 对抗集必须全部拦截
    trajectory_score: float = 0.80
    max_cost_cny_per_task: float = 2.00
    max_p95_latency_ms: int = 12000
    max_regression_drop: float = 0.03       # 3 个百分点
    min_noise_margin: float = 0.02          # gap 小于同配置噪声不判回归


@dataclass
class HarnessConfig:
    harness_version: str = HARNESS_VERSION
    seed: int = 42
    company: str = "星澜科技"
    offline: bool = True                     # 离线确定性模式：不触网、不调真实模型
    company_allowed_hosts: List[str] = field(default_factory=lambda: ["feeds.internal", "news.mock", "riskdb.internal"])
    workspace_root: str = "artifacts"
    approval_mode: str = "human"             # human | auto（auto 仅用于离线实验，CI 禁用）
    prefix_cache: bool = True
    keep_errors_in_context: bool = True
    context_window_tokens: int = 8000
    compaction_threshold: float = 0.72
    handoff_schema_version: str = "handoff/v2"
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    thresholds: GateThresholds = field(default_factory=GateThresholds)
    routes: List[ModelRoute] = field(default_factory=lambda: [
        ModelRoute("planner", "qwen3-32b", 0.012, 0.036, "planning/verification"),
        ModelRoute("worker", "qwen3-14b", 0.005, 0.015, "tool_selection/extraction"),
        ModelRoute("judge", "qwen3-32b", 0.012, 0.036, "independent_eval"),
    ])
    fault_profile: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        # fault_profile 属于评测环境，不参与运行时配置指纹
        d = to_dict(asdict(self))
        d.pop("fault_profile", None)
        return stable_hash({"harness": d, "task_bank": TASK_BANK_VERSION})

    def with_overrides(self, **kw: Any) -> "HarnessConfig":
        d = json.loads(json.dumps(to_dict(asdict(self)), ensure_ascii=False))
        d.update(kw)
        return HarnessConfig(
            **{k: v for k, v in d.items() if k != "budget" and k != "thresholds" and k != "routes"},
            budget=BudgetConfig(**d["budget"]),
            thresholds=GateThresholds(**d["thresholds"]),
            routes=[ModelRoute(**r) for r in d["routes"]],
        )

    def route(self, name: str) -> ModelRoute:
        for r in self.routes:
            if r.name == name:
                return r
        return self.routes[0]

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(to_dict(asdict(self)), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "HarnessConfig":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            **{k: v for k, v in d.items() if k not in {"budget", "thresholds", "routes"}},
            budget=BudgetConfig(**d["budget"]),
            thresholds=GateThresholds(**d["thresholds"]),
            routes=[ModelRoute(**r) for r in d["routes"]],
        )


_SUITE_LAYERS_FALLBACK: Dict[str, Dict[str, Any]] = {
    # 分层评测：PR 只跑冒烟，夜间跑核心，模型/拓扑大改才跑全量
    "pr_smoke": {"categories": ["regression", "adversarial"], "k": 1, "budget_cny": 5.0},
    "nightly": {"categories": ["regression", "capability", "adversarial", "negative"], "k": 3, "budget_cny": 60.0},
    "full": {"categories": ["regression", "capability", "adversarial", "negative"], "k": 5, "budget_cny": 200.0, "include_held_out": True},
}


def _load_suite_layers() -> Dict[str, Dict[str, Any]]:
    """套件定义外置到 conf/suites.json：改评测分层不必改代码。"""
    p = Path(__file__).resolve().parents[2] / "conf" / "suites.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        layers = {k: v for k, v in data.items() if isinstance(v, dict)}
        return layers or _SUITE_LAYERS_FALLBACK
    except (OSError, json.JSONDecodeError):
        return _SUITE_LAYERS_FALLBACK


SUITE_LAYERS: Dict[str, Dict[str, Any]] = _load_suite_layers()
