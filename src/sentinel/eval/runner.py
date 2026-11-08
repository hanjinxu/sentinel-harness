"""Eval Runner：干净环境 + 多 trial + 全轨迹采集 + 聚合指标。

一次 trial 的全部状态（checkpointer、工具缓存、上下文、预算、tracer）都在
新建的 Runtime 内，trial 之间零共享 —— 否则会出现「缓存 warmed up 导致假通过」。
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import SUITE_LAYERS, HarnessConfig
from ..core import (Metrics, SuiteResult, Task, TrialResult, Verdict, to_dict)
from ..runtime.graph import SentinelGraph
from ..runtime.services import Runtime
from .graders import grade_trial
from .tasks import filter_feeds


class EvalRunner:
    def __init__(self, cfg: HarnessConfig, feeds: Dict[str, Any], tasks: List[Task],
                 artifacts_dir: str = "artifacts"):
        self.cfg = cfg
        self.feeds = feeds
        self.tasks = tasks
        self.artifacts_dir = artifacts_dir
        self.saved_traces: List[str] = []

    # ------------------------------------------------------------------
    def run_suite(self, suite: str = "nightly", *, k: Optional[int] = None,
                  include_held_out: bool = False, task_ids: Optional[List[str]] = None,
                  save_traces: bool = True) -> SuiteResult:
        layer = SUITE_LAYERS.get(suite)
        if layer is None:
            raise ValueError(f"unknown_suite:{suite}")
        trials_k = k or layer["k"]
        categories = set(layer["categories"])
        selected = [t for t in self.tasks if t.category in categories]
        if not include_held_out:
            selected = [t for t in selected if not t.held_out]
        if task_ids:
            selected = [t for t in selected if t.id in task_ids]

        result = SuiteResult(suite=suite, config_fingerprint=self.cfg.fingerprint(),
                             k=trials_k)
        for task in selected:
            for i in range(trials_k):
                tr = self.run_task(task, i, suite, save_traces=save_traces)
                result.trials.append(tr)
                m = Metrics()
                result.cost_cny += tr.metrics.cost_cny
                result.latency_ms += tr.metrics.latency_ms
        self._aggregate(result)
        return result

    # ------------------------------------------------------------------
    def run_task(self, task: Task, trial: int, suite: str = "nightly",
                 save_traces: bool = True) -> TrialResult:
        seed = self.cfg.seed + trial * 101 + (sum(map(ord, task.id)) % 97)
        scenario = task.scenario or {}
        cfg = self.cfg.with_overrides(
            seed=seed, fault_profile=scenario.get("fault", {}),
            metadata={"suite": suite, "task": task.id, "trial": trial},
        )
        feeds = filter_feeds(self.feeds, scenario.get("feed_subset"))
        out_dir = str(Path(self.artifacts_dir) / "eval" / suite / f"{task.id}-t{trial}")
        rt = Runtime(cfg, feeds, task_id=task.id, task_prompt=task.prompt,
                     scenario=scenario, expected=to_dict(task.expected), seed=seed,
                     fault_profile=scenario.get("fault", {}), artifacts_dir=out_dir)
        graph = SentinelGraph(rt)
        decision = scenario.get("approval_decision") or {
            "approved": False, "reviewer": "eval-bot", "note": "离线评测默认拒绝对外动作"}
        state = graph.run(approval_decision=decision)

        interrupted = "__interrupt__" in state
        final_metrics = state.get("final_output", {}).get("metrics", {})
        if final_metrics:
            rt.metrics.latency_ms = int(final_metrics.get("elapsed_ms", 0))
        trace = rt.build_trace(state)
        if interrupted:
            passed, verdict, graders = False, Verdict.FAIL, []
            reasons = ["unresolved_interrupt: HITL 审批未恢复，轨迹不完整"]
        else:
            passed, verdict, graders = grade_trial(task, state, trace, feeds)
            reasons = [r for g in graders for r in g.reasons]

        if save_traces:
            paths = rt.save(state, trace)
            self.saved_traces.append(paths["trace"])

        stop = state.get("final_output", {}).get("stop_reason", state.get("stop_reason", ""))
        signature = f"{stop}:{';'.join(reasons[:2])}" if not passed else ""
        return TrialResult(task_key=task.key, trial=trial, seed=seed, passed=passed,
                           verdict=verdict, grader_results=graders, trace=trace,
                           metrics=rt.metrics, failure_signature=signature)

    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(result: SuiteResult) -> None:
        by_task: Dict[str, List[bool]] = defaultdict(list)
        cat: Dict[str, List[bool]] = defaultdict(list)
        traj_scores: List[float] = []
        out_scores: List[float] = []
        for t in result.trials:
            by_task[t.task_key].append(t.passed)
            cat[t.task_key.split("@")[0].split("-")[0]].append(t.passed)
            for g in t.grader_results:
                if g.grader == "trajectory":
                    traj_scores.append(g.score)
                elif g.grader == "outcome":
                    out_scores.append(g.score)
        for key, flags in by_task.items():
            p = sum(flags) / len(flags)
            k = len(flags)
            result.pass_at_k[key] = round(1 - (1 - p) ** k, 4)
            result.pass_pow_k[key] = round(p ** k, 4)
        result.per_metric = {
            "pass_rate": round(sum(t.passed for t in result.trials) / max(1, len(result.trials)), 4),
            "trajectory_score": round(sum(traj_scores) / max(1, len(traj_scores)), 4),
            "outcome_score": round(sum(out_scores) / max(1, len(out_scores)), 4),
            "pass_pow_k_mean": round(sum(result.pass_pow_k.values()) / max(1, len(result.pass_pow_k)), 4),
            "avg_cost_cny": round(result.cost_cny / max(1, len(result.trials)), 6),
            "avg_latency_ms": int(result.latency_ms / max(1, len(result.trials))),
        }

    # ------------------------------------------------------------------
    def category_rates(self, result: SuiteResult) -> Dict[str, float]:
        """逐类别通过率：门禁按类别/指标分别判定，复合分数会掩盖局部回归。"""
        id_to_cat = {t.key: t.category for t in self.tasks}
        buckets: Dict[str, List[bool]] = defaultdict(list)
        for t in result.trials:
            buckets[id_to_cat.get(t.task_key, "unknown")].append(t.passed)
        return {c: round(sum(v) / len(v), 4) for c, v in buckets.items()}

    def estimate_noise(self, suite: str = "pr_smoke") -> float:
        """同一配置连跑两次，估计 CI 噪声；gap 小于噪声时不判回归。"""
        a = self.run_suite(suite, k=1, save_traces=False)
        b = self.run_suite(suite, k=1, save_traces=False)
        return round(abs(a.pass_rate - b.pass_rate), 4)
