"""CI 门禁与 A/B（SBS）实验。

两条关键纪律：
1. 逐指标设阈值，不用复合通过率 —— 92% 的总分可能掩盖某类工具路径 40% 的失败；
2. 差异必须大于同配置噪声才判回归 —— 否则 CI 会因为模型抖动而 flaky，
   最终被工程师关掉，eval 就退化成没人看的报表。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import GateThresholds
from ..core import SuiteResult


@dataclass
class GateReport:
    verdict: str = "PASS"                     # PASS | FAIL | INCONCLUSIVE
    passed: bool = True
    checks: List[Dict[str, Any]] = field(default_factory=list)
    baseline_diff: Dict[str, float] = field(default_factory=dict)
    noise: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "passed": self.passed, "checks": self.checks,
                "baseline_diff": self.baseline_diff, "noise": self.noise, "notes": self.notes}


class CIGate:
    def __init__(self, thresholds: Optional[GateThresholds] = None):
        self.t = thresholds or GateThresholds()

    def evaluate(self, suite: SuiteResult, category_rates: Dict[str, float], *,
                 baseline: Optional[Dict[str, Any]] = None, noise: float = 0.0) -> GateReport:
        r = GateReport(noise=noise)
        m = suite.per_metric

        def chk(metric: str, actual: float, threshold: float, op: str, hard: bool = True) -> None:
            ok = actual >= threshold if op == ">=" else actual <= threshold
            r.checks.append({"metric": metric, "actual": round(actual, 6),
                             "threshold": round(threshold, 6), "op": op, "ok": ok,
                             "hard_gate": hard})
            if hard and not ok:
                r.passed = False

        reg = category_rates.get("regression", 0.0)
        adv = category_rates.get("adversarial", 0.0)
        cap = category_rates.get("capability", 0.0)
        neg = category_rates.get("negative", 0.0)
        # 只对本次 suite 实际覆盖的类别设门禁：pr_smoke 不含 capability/negative，
        # 若强行检查会把缺失类别当成 0 分，制造假失败。
        if "regression" in category_rates:
            chk("regression_pass_rate", reg, self.t.regression_pass_rate, ">=")
        if "adversarial" in category_rates:
            chk("adversarial_block_rate", adv, self.t.adversarial_block_rate, ">=")
        if "capability" in category_rates:
            chk("capability_pass_rate", cap, self.t.capability_pass_rate, ">=", hard=False)
            if cap < self.t.capability_pass_rate:
                r.notes.append(f"能力集通过率 {cap:.2%} 低于观察阈值 {self.t.capability_pass_rate:.0%}，"
                               "作为趋势指标记录，不阻断合并")
        if "negative" in category_rates:
            chk("negative_pass_rate", neg, 1.0, ">=")
        chk("trajectory_score", m.get("trajectory_score", 0.0), self.t.trajectory_score, ">=")
        chk("avg_cost_cny_per_task", m.get("avg_cost_cny", 0.0), self.t.max_cost_cny_per_task, "<=")
        chk("avg_latency_ms", m.get("avg_latency_ms", 0), self.t.max_p95_latency_ms, "<=")

        # baseline 缺省 suite 字段时视为同一套件，兼容旧格式
        if baseline and baseline.get("suite", suite.suite) == suite.suite:
            bm = baseline.get("per_metric", {})
            for key in ("pass_rate", "trajectory_score", "outcome_score"):
                if key in bm and key in m:
                    d = round(m[key] - float(bm[key]), 4)
                    r.baseline_diff[key] = d
                    if d < -self.t.max_regression_drop:
                        if abs(d) < max(self.t.min_noise_margin, noise):
                            r.notes.append(f"{key} 下降 {d} 小于噪声上界，判定为不显著，需加 trial")
                            r.verdict = "INCONCLUSIVE"
                        else:
                            r.passed = False
                            r.notes.append(f"{key} 相对 baseline 下降 {d}，超过 {self.t.max_regression_drop}")
        if not r.passed:
            r.verdict = "FAIL"
        return r

    @staticmethod
    def load_baseline(path: str) -> Optional[Dict[str, Any]]:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def save_baseline(suite: SuiteResult, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"suite": suite.suite,
                                 "config_fingerprint": suite.config_fingerprint,
                                 "per_metric": suite.per_metric,
                                 "pass_at_k": suite.pass_at_k,
                                 "pass_pow_k": suite.pass_pow_k},
                                ensure_ascii=False, indent=2), encoding="utf-8")


class ABTest:
    """Side-by-Side 对比：同一任务、同一 seed 下两个 harness 配置的逐 trial 胜负。

    显著性用精确符号检验（不依赖 scipy）：在 n 个非平局中，
    观测到 >= w 次 A 胜的概率按二项分布累计计算。
    """

    @staticmethod
    def compare(a: SuiteResult, b: SuiteResult, label_a: str = "A", label_b: str = "B") -> Dict[str, Any]:
        def keyed(s: SuiteResult) -> Dict[str, bool]:
            return {f"{t.task_key}:{t.trial}": t.passed for t in s.trials}
        ka, kb = keyed(a), keyed(b)
        common = sorted(set(ka) & set(kb))
        wins_a = sum(ka[k] and not kb[k] for k in common)
        wins_b = sum(kb[k] and not ka[k] for k in common)
        ties = len(common) - wins_a - wins_b
        n = wins_a + wins_b
        p_value = 1.0
        if n:
            p_value = round(min(1.0, 2 * sum(math.comb(n, i) for i in range(max(wins_a, wins_b), n + 1)) / (2 ** n)), 6)
        return {
            "label_a": label_a, "label_b": label_b, "paired_trials": len(common),
            f"{label_a}_wins": wins_a, f"{label_b}_wins": wins_b, "ties": ties,
            "sign_test_p_value": p_value,
            "significant_at_0_05": p_value < 0.05,
            "pass_rate_a": round(a.pass_rate, 4), "pass_rate_b": round(b.pass_rate, 4),
            "cost_a": round(a.cost_cny, 6), "cost_b": round(b.cost_cny, 6),
            "verdict": (f"{label_a} 显著更优" if p_value < 0.05 and wins_a > wins_b else
                        f"{label_b} 显著更优" if p_value < 0.05 and wins_b > wins_a else
                        "差异不显著，需增加 trial 或任务数"),
        }
