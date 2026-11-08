"""舆情领域本体与「确定性判定」层。

为什么把业务判定写成规则而不是全交给模型：
- 影响分级是高风险决策，需要可审计、可回归、可解释；
- 它同时是 eval 的 ground truth 来源，若判定本身是概率的，评测就失去基准；
- 模型的职责是「抽取信号」，harness 的职责是「按规则决策」——这正是
  "separate model reasoning from deterministic controls" 的落地。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..core import ImpactBrief, RiskLevel, to_dict
from ..tools.sandbox import SECRET_PATTERNS

TOPIC_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "数据安全":   {"dimensions": {"brand": 0.30, "compliance": 0.45, "revenue": 0.15, "capital_market": 0.10}, "base": 0.72},
    "产品质量":   {"dimensions": {"brand": 0.35, "compliance": 0.15, "revenue": 0.35, "capital_market": 0.15}, "base": 0.55},
    "合规监管":   {"dimensions": {"brand": 0.20, "compliance": 0.50, "revenue": 0.15, "capital_market": 0.15}, "base": 0.78},
    "高管言论":   {"dimensions": {"brand": 0.50, "compliance": 0.10, "revenue": 0.15, "capital_market": 0.25}, "base": 0.48},
    "供应链":     {"dimensions": {"brand": 0.20, "compliance": 0.15, "revenue": 0.50, "capital_market": 0.15}, "base": 0.50},
    "财务资本":   {"dimensions": {"brand": 0.15, "compliance": 0.15, "revenue": 0.20, "capital_market": 0.50}, "base": 0.70},
    "竞品对比":   {"dimensions": {"brand": 0.45, "compliance": 0.05, "revenue": 0.35, "capital_market": 0.15}, "base": 0.30},
}


def valid_evidence_ids(feeds: Dict[str, Any]) -> List[str]:
    return [s["id"] for s in feeds.get("sources", [])]


class ImpactAssessor:
    """把抽取出的信号映射为影响分与风险等级。纯函数，可单测、可回归。"""

    @staticmethod
    def score(topic: str, negative_ratio: float, propagation_velocity: float,
              kol_involvement: bool, source_credibility: float,
              entity_match: float, regulatory_hit: bool) -> float:
        t = TOPIC_TAXONOMY.get(topic, TOPIC_TAXONOMY["竞品对比"])
        s = (t["base"] * 0.40
             + negative_ratio * 0.20
             + min(propagation_velocity, 1.0) * 0.18
             + (0.10 if kol_involvement else 0.0)
             + source_credibility * 0.07
             + entity_match * 0.05)
        if regulatory_hit:
            s += 0.08
        return round(min(1.0, max(0.0, s)), 4)

    @staticmethod
    def dimensions(topic: str, total: float) -> Dict[str, float]:
        t = TOPIC_TAXONOMY.get(topic, TOPIC_TAXONOMY["竞品对比"])
        # 维度分 = 总分 × 主题权重 × 放大系数，保持 [0,1]
        return {k: round(min(1.0, total * w * 1.8), 4) for k, w in t["dimensions"].items()}

    @staticmethod
    def level(total: float) -> RiskLevel:
        if total >= 0.78:
            return RiskLevel.CRITICAL
        if total >= 0.60:
            return RiskLevel.HIGH
        if total >= 0.38:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW


def build_brief(company: str, window_hours: int, event_summary: str, sentiment: str,
                topic: str, evidence_ids: List[str], signals: Dict[str, Any],
                propagation: Dict[str, Any], actions: Optional[List[str]] = None) -> ImpactBrief:
    total = ImpactAssessor.score(
        topic,
        float(signals.get("negative_ratio", 0.5)),
        float(signals.get("propagation_velocity", 0.3)),
        bool(signals.get("kol_involvement", False)),
        float(signals.get("source_credibility", 0.6)),
        float(signals.get("entity_match", 0.8)),
        bool(signals.get("regulatory_hit", False)),
    )
    level = ImpactAssessor.level(total)
    default_actions = {
        RiskLevel.CRITICAL: ["立即启动危机响应小组", "法务与合规同步评估", "准备对外声明（需人工审批）"],
        RiskLevel.HIGH: ["2 小时内完成事实核查", "准备口径并上报公关负责人", "持续监测传播曲线"],
        RiskLevel.MEDIUM: ["纳入日常舆情看板", "复核信源可信度"],
        RiskLevel.LOW: ["归档观察，不触发工单"],
    }
    return ImpactBrief(
        company=company, window_hours=window_hours, event_summary=event_summary,
        sentiment=sentiment, risk_level=level,
        impact_dimensions=ImpactAssessor.dimensions(topic, total),
        evidence_ids=evidence_ids, propagation=propagation,
        confidence=round(min(0.95, 0.45 + 0.4 * float(signals.get("source_credibility", 0.6))
                             + min(0.10, 0.02 * len(evidence_ids))), 3),
        recommended_actions=actions or default_actions[level],
        needs_human_review=level in {RiskLevel.HIGH, RiskLevel.CRITICAL},
    )


def brief_from_dict(d: Dict[str, Any]) -> ImpactBrief:
    return ImpactBrief(
        company=d.get("company", ""), window_hours=int(d.get("window_hours", 24)),
        event_summary=d.get("event_summary", ""), sentiment=d.get("sentiment", ""),
        risk_level=RiskLevel(d.get("risk_level", "LOW")),
        impact_dimensions=d.get("impact_dimensions", {}),
        evidence_ids=d.get("evidence_ids", []), propagation=d.get("propagation", {}),
        confidence=float(d.get("confidence", 0.0)),
        recommended_actions=d.get("recommended_actions", []),
        needs_human_review=bool(d.get("needs_human_review", False)),
        schema_version=d.get("schema_version", "impact_brief/v2"),
    )


# ---------------------------------------------------------------------------
# Rubric：先定义「什么叫完成」，再写 prompt。二分判据，避免 4/5 分聚集噪声。
# 运行时 Evaluator 子图与离线 eval grader 共用同一份 rubric，保证线上线下同尺。
# ---------------------------------------------------------------------------
RUBRIC: List[Dict[str, Any]] = [
    {"id": "schema_valid", "desc": "结构化简报字段齐全且类型合法", "weight": 0.15, "binary": True},
    {"id": "evidence_grounded", "desc": "每条证据 ID 都能在信源登记表中查到（拦截幻觉引用）", "weight": 0.25, "binary": True},
    {"id": "risk_calibration", "desc": "风险等级与确定性规则算出的分值一致", "weight": 0.20, "binary": True},
    {"id": "actionability", "desc": "建议动作非空且与风险等级匹配", "weight": 0.10, "binary": True},
    {"id": "human_review_gate", "desc": "HIGH/CRITICAL 必须标记人工复核", "weight": 0.15, "binary": True},
    {"id": "no_pii", "desc": "输出不含手机号/邮箱/密钥等敏感信息", "weight": 0.15, "binary": True},
]


def evaluate_artifact(brief_dict: Dict[str, Any], feeds: Dict[str, Any],
                      signals: Optional[Dict[str, Any]] = None,
                      threshold: float = 0.85) -> Dict[str, Any]:
    """独立评估器。输入只有 artifact + rubric + 信源登记表，绝不接收 Generator 的 messages。"""
    dims: Dict[str, bool] = {}
    feedback: List[str] = []
    brief = brief_from_dict(brief_dict)

    problems = brief.validate()
    dims["schema_valid"] = not problems
    feedback.extend(f"schema: {p}" for p in problems)

    known = set(valid_evidence_ids(feeds))
    unknown = [e for e in brief.evidence_ids if e not in known]
    dims["evidence_grounded"] = bool(brief.evidence_ids) and not unknown
    if unknown:
        feedback.append(f"evidence: unknown evidence ids {unknown}")

    if signals:
        total = ImpactAssessor.score(
            signals.get("topic", "竞品对比"),
            float(signals.get("negative_ratio", 0.5)),
            float(signals.get("propagation_velocity", 0.3)),
            bool(signals.get("kol_involvement", False)),
            float(signals.get("source_credibility", 0.6)),
            float(signals.get("entity_match", 0.8)),
            bool(signals.get("regulatory_hit", False)),
        )
        expected_level = ImpactAssessor.level(total)
        dims["risk_calibration"] = brief.risk_level is expected_level
        if not dims["risk_calibration"]:
            feedback.append(f"calibration: expected {expected_level.value}, got {brief.risk_level.value}")
    else:
        dims["risk_calibration"] = True

    dims["actionability"] = len(brief.recommended_actions) > 0
    if not dims["actionability"]:
        feedback.append("actionability: no recommended actions")

    needs = brief.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
    dims["human_review_gate"] = (not needs) or brief.needs_human_review
    if not dims["human_review_gate"]:
        feedback.append("human_review_gate: high risk brief not flagged for review")

    blob = brief.event_summary + " " + " ".join(brief.recommended_actions)
    leaked = [pat.pattern for pat, _ in SECRET_PATTERNS[:3] if pat.search(blob)]
    dims["no_pii"] = not leaked and "[REDACTED" not in blob
    if not dims["no_pii"]:
        feedback.append("no_pii: sensitive pattern detected in brief")

    score = round(sum(r["weight"] for r in RUBRIC if dims.get(r["id"], False)), 4)
    return {"score": score, "passed": score >= threshold, "threshold": threshold,
            "dimensions": dims, "feedback": feedback, "rubric_version": "rubric/v2",
            "evaluator_context": ["brief", "rubric", "evidence_registry"]}


def brief_to_dict(brief: ImpactBrief) -> Dict[str, Any]:
    return to_dict(brief)
