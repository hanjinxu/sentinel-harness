"""测试公用构造器：保证每个用例拿到全新、离线的 Runtime。"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from sentinel.config import HarnessConfig  # noqa: E402
from sentinel.eval.tasks import filter_feeds, load_feeds  # noqa: E402
from sentinel.runtime.graph import SentinelGraph  # noqa: E402
from sentinel.runtime.services import Runtime  # noqa: E402

ARTIFACTS = str(ROOT / "artifacts" / "tests")


def runtime_for(task_id: str = "T-TEST", prompt: str = "监控星澜科技外部舆情并判定影响",
                subset=None, scenario=None, seed: int = 42, **cfg_kw):
    cfg = HarnessConfig(workspace_root=ARTIFACTS, **cfg_kw)
    feeds = filter_feeds(load_feeds(), subset or ["SRC-001", "SRC-002", "SRC-006"])
    sc = {"feed_subset": subset or ["SRC-001", "SRC-002", "SRC-006"], **(scenario or {})}
    rt = Runtime(cfg, feeds, task_id=task_id, task_prompt=prompt, scenario=sc,
                 seed=seed, artifacts_dir=ARTIFACTS)
    return rt, SentinelGraph(rt)


def run(rt_and_graph, decision=None):
    rt, graph = rt_and_graph
    d = decision or {"approved": False, "reviewer": "unit-test"}
    return rt, graph.run(approval_decision=d)
