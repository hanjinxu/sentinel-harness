"""评测报告：人能读的 Markdown + 机器能 diff 的 JSON。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import SuiteResult, to_dict
from .gate import GateReport


def render_report(suite: SuiteResult, category_rates: Dict[str, float],
                  gate: Optional[GateReport] = None, ab: Optional[Dict[str, Any]] = None) -> str:
    lines = [
        f"# Eval Report — `{suite.suite}`",
        "",
        f"- 配置指纹：`{suite.config_fingerprint}`",
        f"- Trial 数：{len(suite.trials)}（每任务 k={suite.k}）",
        f"- 总体通过率：**{suite.per_metric.get('pass_rate', 0):.2%}**",
        f"- pass^k 均值：{suite.per_metric.get('pass_pow_k_mean', 0):.4f}",
        f"- 总成本：¥{suite.cost_cny:.6f}；平均时延：{suite.per_metric.get('avg_latency_ms', 0)} ms",
        "",
        "## 逐类别通过率（门禁按类别判定）",
        "",
        "| 类别 | 通过率 | 门禁 |",
        "| --- | --- | --- |",
    ]
    for c, v in sorted(category_rates.items()):
        hard = "硬门" if c in {"regression", "adversarial", "negative"} else "趋势"
        lines.append(f"| {c} | {v:.2%} | {hard} |")
    lines += ["", "## 指标", "", "| 指标 | 值 |", "| --- | --- |"]
    for k, v in suite.per_metric.items():
        lines.append(f"| {k} | {v} |")

    lines += ["", "## pass@k / pass^k（能力天花板 vs 生产可靠性）", "",
              "| Task | pass@k | pass^k |", "| --- | --- | --- |"]
    for key in sorted(suite.pass_at_k):
        lines.append(f"| {key} | {suite.pass_at_k[key]:.3f} | {suite.pass_pow_k.get(key, 0):.3f} |")

    if gate:
        lines += ["", f"## CI 门禁：{gate.verdict}", "",
                  "| 指标 | 实测 | 阈值 | 结果 |", "| --- | --- | --- | --- |"]
        for c in gate.checks:
            lines.append(f"| {c['metric']} | {c['actual']} | {c['op']} {c['threshold']} | "
                         f"{'PASS' if c['ok'] else 'FAIL'}{'' if c['hard_gate'] else ' (软指标)'} |")
        if gate.baseline_diff:
            lines += ["", "### 与 baseline 的差异", ""]
            for k, v in gate.baseline_diff.items():
                lines.append(f"- {k}: {v:+.4f}")
        for n in gate.notes:
            lines.append(f"- 注：{n}")

    if ab:
        lines += ["", "## A/B（SBS）", "",
                  f"- 配对 trial：{ab['paired_trials']}；A 胜 {ab.get('A_wins')} / B 胜 {ab.get('B_wins')} / 平 {ab['ties']}",
                  f"- 精确符号检验 p = {ab['sign_test_p_value']}；结论：{ab['verdict']}"]

    failed = [t for t in suite.trials if not t.passed]
    if failed:
        lines += ["", "## 失败轨迹（error analysis 入口）", "",
                  "| Task | trial | stop_reason | 归因 |", "| --- | --- | --- | --- |"]
        for t in failed[:20]:
            lines.append(f"| {t.task_key} | {t.trial} | "
                         f"{(t.trace.stop_reason.value if t.trace else 'n/a')} | "
                         f"{t.failure_signature[:90]} |")
    lines += ["", "---", "",
              "> 报告由 eval.report 生成。失败轨迹必须人工阅读：数字说明「变了」，轨迹说明「为什么」。"]
    return "\n".join(lines) + "\n"


def write_report(suite: SuiteResult, category_rates: Dict[str, float],
                 gate: Optional[GateReport] = None, ab: Optional[Dict[str, Any]] = None,
                 out_dir: str = "artifacts") -> Dict[str, str]:
    base = Path(out_dir) / "eval" / suite.suite
    base.mkdir(parents=True, exist_ok=True)
    md = base / "report.md"
    js = base / "report.json"
    md.write_text(render_report(suite, category_rates, gate, ab), encoding="utf-8")
    payload = {"suite": to_dict(suite), "category_rates": category_rates,
               "gate": gate.as_dict() if gate else None, "ab": ab}
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"markdown": str(md), "json": str(js)}
