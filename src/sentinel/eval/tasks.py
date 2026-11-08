"""版本化任务库（golden set）。

遵循 2026 年 eval 实践的几条硬规则：
- 任务带 version / category / tags / source，改判据必须升版本；
- 每个任务的 success criteria 是二分的，两位专家对结论不应有分歧；
- 留出集在调 harness 时不许看，防止把 harness 过拟合到评测集；
- task.source 标明 authored / production_failure / flywheel，失败回流可追溯。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import Expected, Task, to_dict

TASKS_PATH = str(Path(__file__).resolve().parents[3] / "conf" / "tasks.json")
FEEDS_PATH = str(Path(__file__).resolve().parents[3] / "db" / "feeds.json")


def load_feeds(path: str = FEEDS_PATH) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_task_bank(path: str = TASKS_PATH) -> List[Task]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = []
    for d in data["tasks"]:
        exp = Expected(**d.get("expected", {}))
        tasks.append(Task(
            id=d["id"], version=d["version"], category=d["category"], prompt=d["prompt"],
            expected=exp, scenario=d.get("scenario", {}),
            reference_trajectory=d.get("reference_trajectory", []),
            tags=d.get("tags", []), held_out=bool(d.get("held_out", False)),
            source=d.get("source", "authored"),
        ))
    return tasks


def save_task(task: Task, path: str = TASKS_PATH) -> None:
    """飞轮：把生产失败固化成新回归任务（追加，不覆盖历史）。"""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if any(t["id"] == task.id for t in data["tasks"]):
        return
    d = to_dict(task)
    d["expected"] = to_dict(task.expected)
    data["tasks"].append(d)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_feeds(feeds: Dict[str, Any], subset: Optional[List[str]]) -> Dict[str, Any]:
    """每个 trial 用独立信源视图，杜绝 trial 之间的共享状态与假通过。"""
    if not subset:
        return feeds
    allowed = set(subset)
    out = dict(feeds)
    out["sources"] = [s for s in feeds.get("sources", []) if s["id"] in allowed]
    return out


def task_bank_stats(tasks: List[Task]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"total": len(tasks), "by_category": {}, "held_out": 0, "sources": {}}
    for t in tasks:
        stats["by_category"][t.category] = stats["by_category"].get(t.category, 0) + 1
        stats["held_out"] += int(t.held_out)
        stats["sources"][t.source] = stats["sources"].get(t.source, 0) + 1
    return stats
