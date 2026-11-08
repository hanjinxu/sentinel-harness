"""命令行入口：一条命令跑通「运行 → 评测 → 门禁 → 回放 → 飞轮」全链路。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HARNESS_VERSION, REQUIREMENT_MATRIX, HarnessConfig
from .core import Task, Expected, Verdict, to_dict
from .eval.gate import ABTest, CIGate
from .eval.report import render_report, write_report
from .eval.runner import EvalRunner
from .eval.tasks import filter_feeds, load_feeds, load_task_bank
from .flywheel.pipeline import cluster_failures, export_trajectories
from .runtime.graph import SentinelGraph
from .runtime.services import Runtime

ROOT = Path(__file__).resolve().parents[2]


def _cfg(args: argparse.Namespace) -> HarnessConfig:
    cfg = HarnessConfig(workspace_root=str(ROOT / "artifacts"))
    if getattr(args, "config", None):
        cfg = HarnessConfig.load(args.config)
    if getattr(args, "seed", None) is not None:
        cfg = cfg.with_overrides(seed=args.seed)
    return cfg


def _demo_task() -> Task:
    return Task(
        id="DEMO-001", version="2026.11", category="capability",
        prompt="监控过去 24 小时关于星澜科技的外部舆情，判断是否对公司有影响，输出带证据的结构化简报。",
        expected=Expected(must_use_tools=["search_mentions", "draft_brief"], max_turns=20),
        scenario={"feed_subset": ["SRC-001", "SRC-002", "SRC-006"], "susceptible_to_injection": True},
        tags=["demo"],
    )


def cmd_requirements(args: argparse.Namespace) -> int:
    print(f"Sentinel Harness {HARNESS_VERSION} — 能力覆盖矩阵（{len(REQUIREMENT_MATRIX)} 项）\n")
    print(f"{'ID':<5} {'要求点':<48} {'实现模块':<38} 技术要点")
    print("-" * 130)
    for r in REQUIREMENT_MATRIX:
        print(f"{r['id']:<5} {r['req'][:46]:<48} {r['module'][:36]:<38} {r['topic']}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    rt = Runtime(_cfg(args), load_feeds(), artifacts_dir=str(ROOT / "artifacts"))
    out = Path(args.out) if args.out else ROOT / "artifacts" / "mcp_tools.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tools": rt.registry.mcp_tools()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {len(rt.registry.names())} 个工具的 MCP schema -> {out}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    task = _demo_task()
    feeds = filter_feeds(load_feeds(), task.scenario["feed_subset"])
    art = str(ROOT / "artifacts" / "demo")
    rt = Runtime(cfg, feeds, task_id=task.id, task_prompt=task.prompt,
                 scenario=task.scenario, expected=to_dict(task.expected),
                 artifacts_dir=art)
    graph = SentinelGraph(rt)
    state = graph.run(approval_decision={
        "approved": False, "reviewer": "值班人（离线演示替身）",
        "note": "生产环境必须由真实人工在审批 UI 中确认；此处为可复现的回放替身"})
    final = state.get("final_output", {})
    trace = rt.build_trace(state)
    paths = rt.save(state, trace)

    print("=" * 78)
    print("Sentinel Harness — 外部舆情监控 Demo")
    print("=" * 78)
    print(f"run_id            : {rt.run_id}")
    print(f"config_fingerprint: {rt.fp}")
    print(f"framework         : LangGraph StateGraph + checkpointer + interrupt + subgraph")
    print(f"stop_reason       : {final.get('stop_reason')}")
    print(f"checkpoints       : {final.get('checkpoint_count')}")
    print(f"HITL 审批         : {final.get('approval') or '未触发'}")
    print(f"工具调用          : {len(rt.tool_calls)} 次；被拦截 {len(final.get('blocked_tools', []))} 次")
    print(f"护栏违规          : {len(final.get('violations', []))} 条")
    for v in final.get("violations", [])[:6]:
        print(f"    - {v.get('rule')}: {str(v.get('detail') or v.get('signals') or '')[:70]}")
    print(f"运行时评估        : score={final.get('evaluation', {}).get('score')} "
          f"passed={final.get('evaluation', {}).get('passed')}")
    brief = final.get("brief", {})
    if brief:
        print("-" * 78)
        print(f"风险等级: {brief.get('risk_level')} | 情感: {brief.get('sentiment')} | 置信度: {brief.get('confidence')}")
        print(f"影响维度: {brief.get('impact_dimensions')}")
        print(f"证据链  : {brief.get('evidence_ids')}")
        print(f"摘要    : {brief.get('event_summary')}")
        print(f"建议    : {brief.get('recommended_actions')}")
    print("-" * 78)
    print(f"成本/性能: {final.get('metrics')}")
    print(f"轨迹产物: {paths['dir']}")
    print("=" * 78)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    tasks = load_task_bank()
    runner = EvalRunner(cfg, load_feeds(), tasks, artifacts_dir=str(ROOT / "artifacts"))
    suite = runner.run_suite(args.suite, k=args.k, include_held_out=args.include_held_out,
                             task_ids=args.task_ids, save_traces=not args.no_traces)
    rates = runner.category_rates(suite)
    noise = runner.estimate_noise() if args.noise else 0.0
    baseline = CIGate.load_baseline(str(ROOT / "db" / "baseline.json"))
    gate = CIGate(cfg.thresholds).evaluate(suite, rates, baseline=baseline, noise=noise)
    paths = write_report(suite, rates, gate, out_dir=str(ROOT / "artifacts"))
    if args.update_baseline:
        CIGate.save_baseline(suite, str(ROOT / "db" / "baseline.json"))

    print(f"suite={suite.suite} k={suite.k} trials={len(suite.trials)} "
          f"pass_rate={suite.per_metric.get('pass_rate')} categories={rates}")
    print(f"pass^k mean={suite.per_metric.get('pass_pow_k_mean')} "
          f"cost=¥{suite.cost_cny:.6f} noise={noise}")
    print(f"CI GATE: {gate.verdict} (exit={gate.exit_code})")
    for c in gate.checks:
        if not c["ok"]:
            print(f"  FAIL {c['metric']}: {c['actual']} {c['op']} {c['threshold']}")
    for n in gate.notes:
        print(f"  NOTE {n}")
    print(f"report: {paths['markdown']}")
    print("\n" + render_report(suite, rates, gate)[:1600])
    return gate.exit_code


def cmd_ab(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    feeds, tasks = load_feeds(), load_task_bank()
    a = EvalRunner(cfg.with_overrides(prefix_cache=True), feeds, tasks,
                   artifacts_dir=str(ROOT / "artifacts" / "abA")).run_suite("pr_smoke", k=1, save_traces=False)
    b = EvalRunner(cfg.with_overrides(prefix_cache=False), feeds, tasks,
                   artifacts_dir=str(ROOT / "artifacts" / "abB")).run_suite("pr_smoke", k=1, save_traces=False)
    res = ABTest.compare(a, b, "cache_on", "cache_off")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n成本对比: cache_on=¥{res['cost_a']:.6f}  cache_off=¥{res['cost_b']:.6f}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """验证确定性：录制一次 LLM 决策，回放应得到完全相同的工具序列与结论。"""
    cfg = _cfg(args)
    task = _demo_task()
    feeds = filter_feeds(load_feeds(), task.scenario["feed_subset"])
    common = dict(task_id=task.id, task_prompt=task.prompt, scenario=task.scenario,
                  expected=to_dict(task.expected), artifacts_dir=str(ROOT / "artifacts" / "replay"))
    rt1 = Runtime(cfg, feeds, **common)
    s1 = SentinelGraph(rt1).run(approval_decision={"approved": False, "reviewer": "bot"})
    paths = rt1.save(s1, rt1.build_trace(s1))
    rt2 = Runtime(cfg, feeds, replay_path=paths["replay"], **common)
    s2 = SentinelGraph(rt2).run(approval_decision={"approved": False, "reviewer": "bot"})
    seq1 = [(c.name, c.arguments) for c in rt1.tool_calls]
    seq2 = [(c.name, c.arguments) for c in rt2.tool_calls]
    same = seq1 == seq2 and s1.get("final_output", {}).get("stop_reason") == s2.get("final_output", {}).get("stop_reason")
    print(f"recorded_llm_steps={len(rt1.recorder.llm)}  replayed_llm_steps={rt2.replay.cursor if rt2.replay else 0}")
    print(f"tool_sequence_identical={seq1 == seq2}  stop_reason_identical="
          f"{s1.get('final_output', {}).get('stop_reason') == s2.get('final_output', {}).get('stop_reason')}")
    print(f"DETERMINISTIC={same}")
    print(f"replay_file={paths['replay']}")
    return 0 if same else 1


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    tasks = load_task_bank()
    runner = EvalRunner(cfg, load_feeds(), tasks, artifacts_dir=str(ROOT / "artifacts"))
    suite = runner.run_suite("pr_smoke", k=1, save_traces=False)
    traces = [t.trace for t in suite.trials if t.trace]
    task_map = {t.key: t for t in tasks}
    out = export_trajectories(traces, task_map, str(ROOT / "artifacts" / "flywheel" / "grpo_samples.json"))
    clusters = cluster_failures(suite.trials)
    print(f"轨迹导出: {out}")
    print(f"失败聚类: {len(clusters)} 类")
    for c in clusters[:5]:
        print(f"  - {c['signature']} x{c['count']} (建议类别 {c['suggested_category']})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="sentinel", description="Sentinel Agent Harness (LangGraph)")
    p.add_argument("--config", help="自定义 HarnessConfig JSON")
    p.add_argument("--seed", type=int, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("requirements", help="打印企业要求覆盖矩阵")
    q = sub.add_parser("mcp", help="导出 MCP tool schema"); q.add_argument("--out")
    sub.add_parser("demo", help="跑一条完整舆情监控链路")
    e = sub.add_parser("eval", help="运行评测套件 + CI 门禁")
    e.add_argument("--suite", default="pr_smoke", choices=["pr_smoke", "nightly", "full"])
    e.add_argument("--k", type=int, default=None)
    e.add_argument("--include-held-out", action="store_true")
    e.add_argument("--task-ids", nargs="*", default=None)
    e.add_argument("--noise", action="store_true", help="估计同配置噪声")
    e.add_argument("--no-traces", action="store_true")
    e.add_argument("--update-baseline", action="store_true")
    sub.add_parser("ab", help="prefix cache 开/关 的 SBS 对比实验")
    sub.add_parser("replay", help="确定性轨迹回放验证")
    sub.add_parser("export", help="导出 GRPO 训练轨迹与失败聚类")

    args = p.parse_args(argv)
    handlers = {"requirements": cmd_requirements, "mcp": cmd_mcp, "demo": cmd_demo,
                "eval": cmd_eval, "ab": cmd_ab, "replay": cmd_replay, "export": cmd_export}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
