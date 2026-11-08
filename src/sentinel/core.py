"""核心领域模型：所有跨模块流通的对象都定义在这里。

设计原则：
1. 全部可 JSON 序列化 —— trace / checkpoint / eval 报告使用同一份数据契约；
2. 每个对象携带 provenance（run_id / config_fingerprint / seed），保证可复现与可审计；
3. 不依赖任何第三方库，便于在 CI 与离线环境直接运行。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolRisk(str, Enum):
    READ = "READ"                  # 只读，可并发、可自动执行
    WRITE = "WRITE"                # 内部可逆写入，需审计
    EXTERNAL = "EXTERNAL"          # 对外副作用（发预警/发工单），必须人工审批
    DESTRUCTIVE = "DESTRUCTIVE"    # 不可逆，默认拒绝


class ErrorClass(str, Enum):
    """错误分类决定兜底策略（LangGraph 四类 + 本项目的策略/预算扩充）。"""
    TRANSIENT = "TRANSIENT"                # 指数退避重试
    LLM_RECOVERABLE = "LLM_RECOVERABLE"    # 作为 ToolMessage 交回模型自行改参
    USER_FIXABLE = "USER_FIXABLE"          # 中断并请求人工输入
    FATAL = "FATAL"                        # 直接冒泡，保留现场
    POLICY = "POLICY"                      # 护栏/权限拦截，不重试
    BUDGET = "BUDGET"                      # 预算耗尽，不重试


class StopReason(str, Enum):
    FINAL_ANSWER = "FINAL_ANSWER"
    MAX_TURNS = "MAX_TURNS"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    COST_BUDGET = "COST_BUDGET"
    WALL_CLOCK = "WALL_CLOCK"
    TRIPWIRE = "TRIPWIRE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    FATAL_ERROR = "FATAL_ERROR"
    LOOP_DETECTED = "LOOP_DETECTED"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"   # 护栏正确拦截（对抗任务中的期望结果）


def stable_hash(obj: Any) -> str:
    """对 dict 做规范化哈希，用于配置指纹、幂等键、轨迹去重。"""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


class DeterministicIds:
    """同一个 seed + 同一执行序列 → 同一组 ID。回放与回归测试的基石。"""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.counter = 0

    def new(self, prefix: str) -> str:
        self.counter += 1
        raw = f"{self.seed}:{prefix}:{self.counter}"
        return f"{prefix}-{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


@dataclass
class Message:
    role: str                       # system | developer | user | assistant | tool
    content: str
    kind: str = "text"              # text | untrusted_data | tool_result | handoff
    tainted: bool = False           # 是否来源于不可信外部内容
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    thought: str = ""               # 仅用于调试，绝不传给 Evaluator


@dataclass
class ToolResult:
    call_id: str
    tool: str
    ok: bool
    payload: Any = None
    error: Optional[str] = None
    error_class: Optional[ErrorClass] = None
    latency_ms: int = 0
    tokens: int = 0
    tainted: bool = False
    blocked_by: Optional[str] = None
    retries: int = 0
    cached: bool = False


@dataclass
class Span:
    span_id: str
    parent_id: Optional[str]
    name: str
    kind: str                       # run | llm | tool | guard | hook | eval | approval
    start_ms: int
    end_ms: int = 0
    status: str = "OK"
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, **to_dict(attrs)})


@dataclass
class Metrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    retries: int = 0
    guard_blocks: int = 0
    approvals: int = 0
    latency_ms: int = 0
    turns: int = 0
    cost_cny: float = 0.0

    def add(self, other: "Metrics") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(other, k))


@dataclass
class Trace:
    trace_id: str
    run_id: str
    task_id: str
    config_fingerprint: str
    seed: int
    spans: List[Span] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: StopReason = StopReason.FINAL_ANSWER
    final_answer: Dict[str, Any] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    checkpoints: List[str] = field(default_factory=list)

    def tool_names(self) -> List[str]:
        return [c.name for c in self.tool_calls]

    def blocked_tools(self) -> List[str]:
        return [r.tool for r in self.tool_results if r.blocked_by]


@dataclass
class ImpactBrief:
    """业务产物：结构化舆情影响简报。所有结论必须挂证据 ID。"""
    company: str
    window_hours: int
    event_summary: str
    sentiment: str                        # negative | mixed | positive
    risk_level: RiskLevel
    impact_dimensions: Dict[str, float]   # brand / compliance / revenue / capital_market
    evidence_ids: List[str]
    propagation: Dict[str, Any]
    confidence: float
    recommended_actions: List[str]
    needs_human_review: bool
    schema_version: str = "impact_brief/v2"

    def validate(self) -> List[str]:
        """确定性验证器（Prover）：输出到达用户前的最后一道关。"""
        problems: List[str] = []
        if not self.evidence_ids:
            problems.append("no_evidence: 结论缺少可追溯证据 ID")
        if not 0.0 <= self.confidence <= 1.0:
            problems.append("confidence_out_of_range")
        if self.sentiment not in {"negative", "mixed", "positive"}:
            problems.append("invalid_sentiment")
        if not self.event_summary or len(self.event_summary) < 10:
            problems.append("summary_too_short")
        if any(v < 0 or v > 1 for v in self.impact_dimensions.values()):
            problems.append("impact_score_out_of_range")
        if self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} and not self.needs_human_review:
            problems.append("high_risk_without_human_review")
        return problems


@dataclass
class Expected:
    """任务的成功判据。二分式判据优先，避免 Likert 量表噪声。"""
    risk_level: Optional[str] = None
    sentiment: Optional[str] = None
    must_use_tools: List[str] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)
    must_block: List[str] = field(default_factory=list)       # 期望被护栏拦截的工具
    must_include_evidence: List[str] = field(default_factory=list)
    needs_human_review: Optional[bool] = None
    expect_stop_reason: Optional[str] = None   # 输入护栏类任务：期望提前停机而非产出简报
    max_turns: int = 14
    max_cost_cny: float = 2.0
    reference_answer: Optional[str] = None
    rubric: List[str] = field(default_factory=list)


@dataclass
class Task:
    id: str
    version: str
    category: str                    # regression | capability | adversarial | negative
    prompt: str
    expected: Expected
    scenario: Dict[str, Any] = field(default_factory=dict)
    reference_trajectory: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    held_out: bool = False           # 留出集：调 harness 时不许看
    source: str = "authored"         # authored | production_failure | flywheel

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass
class GraderResult:
    grader: str
    passed: bool
    score: float
    reasons: List[str] = field(default_factory=list)
    deterministic: bool = True


@dataclass
class TrialResult:
    task_key: str
    trial: int
    seed: int
    passed: bool
    verdict: Verdict
    grader_results: List[GraderResult]
    trace: Optional[Trace] = None
    metrics: Metrics = field(default_factory=Metrics)
    failure_signature: str = ""


@dataclass
class SuiteResult:
    suite: str
    config_fingerprint: str
    trials: List[TrialResult] = field(default_factory=list)
    pass_at_k: Dict[str, float] = field(default_factory=dict)
    pass_pow_k: Dict[str, float] = field(default_factory=dict)
    per_metric: Dict[str, float] = field(default_factory=dict)
    cost_cny: float = 0.0
    latency_ms: int = 0
    k: int = 3

    @property
    def pass_rate(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.passed for t in self.trials) / len(self.trials)
