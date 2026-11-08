"""评测层：任务库、Prover、grader、逐指标门禁、A/B、runner、确定性回放。"""
from __future__ import annotations

import argparse
import unittest

from tests._utils import ARTIFACTS, runtime_for

from sentinel.business.domain import evaluate_artifact
from sentinel.cli import cmd_replay
from sentinel.config import HarnessConfig
from sentinel.core import SuiteResult, TrialResult
from sentinel.eval.gate import ABTest, CIGate
from sentinel.eval.report import render_report
from sentinel.eval.runner import EvalRunner
from sentinel.eval.tasks import filter_feeds, load_feeds, load_task_bank


def _fake_suite(name: str, passed_all: bool = True, n: int = 4) -> SuiteResult:
    s = SuiteResult(suite=name, config_fingerprint="fp", k=1)
    for i in range(n):
        s.trials.append(TrialResult(task_key=f"T{i}@v1", trial=0, seed=1,
                                    passed=passed_all, verdict=None,
                                    grader_results=[]))
    s.per_metric = {"pass_rate": 1.0 if passed_all else 0.5,
                    "trajectory_score": 1.0, "outcome_score": 1.0,
                    "pass_pow_k_mean": 1.0, "avg_cost_cny": 0.05, "avg_latency_ms": 50}
    return s


class TestTaskBank(unittest.TestCase):
    def test_versioned_tasks_with_held_out(self):
        tasks = load_task_bank()
        self.assertGreaterEqual(len(tasks), 13)
        self.assertGreaterEqual(sum(t.held_out for t in tasks), 2)
        self.assertTrue(all(t.version for t in tasks))
        self.assertGreaterEqual(sum(t.category == "adversarial" for t in tasks), 4)

    def test_clean_feed_subset(self):
        feeds = load_feeds()
        subset = filter_feeds(feeds, ["SRC-001", "SRC-002"])
        self.assertEqual(len(subset["sources"]), 2)
        self.assertEqual(len(feeds["sources"]), 9)   # 原对象不可被污染


class TestProver(unittest.TestCase):
    def test_hallucinated_evidence_fails_rubric(self):
        feeds = filter_feeds(load_feeds(), ["SRC-001"])
        brief = {"company": "星澜科技", "window_hours": 24,
                 "event_summary": "一条足够长的事件摘要用于通过 schema 检查",
                 "sentiment": "negative", "risk_level": "LOW",
                 "impact_dimensions": {"brand": 0.2},
                 "evidence_ids": ["SRC-FAKE"], "propagation": {}, "confidence": 0.8,
                 "recommended_actions": ["归档"], "needs_human_review": False}
        res = evaluate_artifact(brief, feeds)
        self.assertFalse(res["dimensions"]["evidence_grounded"])
        self.assertFalse(res["passed"])

    def test_evaluator_context_is_explicit(self):
        feeds = filter_feeds(load_feeds(), ["SRC-001"])
        res = evaluate_artifact({}, feeds)
        self.assertEqual(res["evaluator_context"], ["brief", "rubric", "evidence_registry"])


class TestGate(unittest.TestCase):
    def test_gate_pass(self):
        r = CIGate().evaluate(_fake_suite("smoke"),
                              {"regression": 1.0, "adversarial": 1.0, "negative": 1.0})
        self.assertTrue(r.passed)
        self.assertEqual(r.exit_code, 0)

    def test_gate_fails_on_single_metric(self):
        r = CIGate().evaluate(_fake_suite("smoke"),
                              {"regression": 0.8, "adversarial": 1.0})
        self.assertFalse(r.passed)
        self.assertTrue(any(not c["ok"] for c in r.checks))

    def test_missing_category_not_treated_as_zero(self):
        r = CIGate().evaluate(_fake_suite("smoke"), {"regression": 1.0, "adversarial": 1.0})
        self.assertTrue(r.passed, "pr_smoke 不含 capability/negative，不应制造假失败")

    def test_noise_suppresses_false_regression(self):
        s = _fake_suite("smoke")
        s.per_metric["pass_rate"] = 0.95
        baseline = {"per_metric": {"pass_rate": 1.0, "trajectory_score": 1.0,
                                   "outcome_score": 1.0}}
        r = CIGate().evaluate(s, {"regression": 1.0, "adversarial": 1.0},
                              baseline=baseline, noise=0.08)
        self.assertTrue(r.passed)
        self.assertTrue(any("不显著" in n for n in r.notes))

    def test_ab_sign_test(self):
        res = ABTest.compare(_fake_suite("A"), _fake_suite("B", passed_all=False))
        self.assertEqual(res["paired_trials"], 4)
        self.assertEqual(res["A_wins"], 4)
        self.assertIn("sign_test_p_value", res)

    def test_report_renders(self):
        s = _fake_suite("smoke")
        md = render_report(s, {"regression": 1.0}, CIGate().evaluate(s, {"regression": 1.0}))
        self.assertIn("CI 门禁", md)
        self.assertIn("pass@k", md)


class TestRunner(unittest.TestCase):
    def test_pr_smoke_all_green(self):
        runner = EvalRunner(HarnessConfig(), load_feeds(), load_task_bank(),
                            artifacts_dir=ARTIFACTS)
        suite = runner.run_suite("pr_smoke", k=1, save_traces=False)
        self.assertEqual(len(suite.trials), 7)
        rates = runner.category_rates(suite)
        self.assertEqual(rates["regression"], 1.0)
        self.assertEqual(rates["adversarial"], 1.0)
        self.assertIn("pass_pow_k_mean", suite.per_metric)


class TestReplay(unittest.TestCase):
    def test_recorded_llm_decisions_replay_identically(self):
        rc = cmd_replay(argparse.Namespace(config=None, seed=None))
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
