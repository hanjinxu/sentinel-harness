"""全链路 tracing：span 树 + 指标 + 审计事件 + 轨迹录制/回放。

关键工程价值：把「模型不确定性」与「编排逻辑缺陷」分开。
CI 用录制的 LLM 响应回放，同一份 harness 代码重复执行应得到同一轨迹；
若结果不同，问题在编排/工具/护栏，而不在模型抖动。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..core import (DeterministicIds, Message, Metrics, Span, StopReason, ToolCall,
                    ToolResult, Trace, Verdict, to_dict)


class Tracer:
    def __init__(self, trace_id: str, run_id: str, config_fingerprint: str, seed: int,
                 clock=time.monotonic):
        self.trace_id = trace_id
        self.run_id = run_id
        self.config_fingerprint = config_fingerprint
        self.seed = seed
        self.clock = clock
        self.ids = DeterministicIds(seed)
        self.spans: List[Span] = []
        self._stack: List[str] = []
        self._t0 = self.clock()
        self.audit: List[Dict[str, Any]] = []

    def _now(self) -> int:
        return int((self.clock() - self._t0) * 1000)

    @contextmanager
    def span(self, name: str, kind: str = "node", **attrs: Any) -> Iterator[Span]:
        s = Span(span_id=self.ids.new("sp"), parent_id=self._stack[-1] if self._stack else None,
                 name=name, kind=kind, start_ms=self._now(), attributes=to_dict(attrs))
        self.spans.append(s)
        self._stack.append(s.span_id)
        try:
            yield s
        except Exception as e:  # noqa: BLE001
            s.status = "ERROR"
            s.event("exception", type=type(e).__name__, message=str(e)[:300])
            raise
        finally:
            s.end_ms = self._now()
            self._stack.pop()

    def completed_span(self, name: str, kind: str, duration_ms: int = 0,
                       status: str = "OK", **attrs: Any) -> Span:
        s = Span(span_id=self.ids.new("sp"), parent_id=self._stack[-1] if self._stack else None,
                 name=name, kind=kind, start_ms=self._now(), end_ms=self._now() + duration_ms,
                 status=status, attributes=to_dict(attrs))
        self.spans.append(s)
        return s

    def tool_span(self, call: ToolCall, result: ToolResult, rule: str = "") -> None:
        self.completed_span(
            f"tool.{call.name}", "tool", result.latency_ms,
            "OK" if result.ok else "ERROR",
            tool=call.name, ok=result.ok, blocked_by=result.blocked_by,
            error_class=result.error_class.value if result.error_class else None,
            retries=result.retries, cached=result.cached, tainted=result.tainted,
            policy_rule=rule, args=call.arguments,
        )

    def llm_span(self, route: str, prompt_tokens: int, completion_tokens: int,
                 cached_tokens: int, action: str) -> None:
        self.completed_span(f"llm.{route}", "llm", 0, "OK",
                            route=route, prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cached_tokens=cached_tokens, action=action)

    def audit_event(self, name: str, **kw: Any) -> None:
        self.audit.append({"ts_ms": self._now(), "event": name, **to_dict(kw)})

    def export_jsonl(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"trace_id": self.trace_id, "run_id": self.run_id,
                             "config_fingerprint": self.config_fingerprint,
                             "seed": self.seed, **to_dict(s)}, ensure_ascii=False)
                 for s in self.spans]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)


class ReplayRecorder:
    """录制 LLM 决策序列；CI 回放时不触网、不调模型。"""

    def __init__(self, config_fingerprint: str, seed: int):
        self.config_fingerprint = config_fingerprint
        self.seed = seed
        self.llm: List[Dict[str, Any]] = []
        self.tools: List[Dict[str, Any]] = []

    def record_llm(self, messages: List[Dict[str, Any]], response: Dict[str, Any]) -> None:
        self.llm.append({"messages": [to_dict(m) for m in messages], "response": to_dict(response)})

    def record_tool(self, call: ToolCall, result: ToolResult) -> None:
        self.tools.append({"call": to_dict(call), "result": to_dict(result)})

    def save(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"config_fingerprint": self.config_fingerprint,
                                 "seed": self.seed, "llm": self.llm, "tools": self.tools},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)


class ReplaySource:
    def __init__(self, path: str):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.config_fingerprint = d["config_fingerprint"]
        self.seed = d["seed"]
        self.llm = d["llm"]
        self.tools = d["tools"]
        self.cursor = 0

    def next_response(self) -> Optional[Dict[str, Any]]:
        if self.cursor >= len(self.llm):
            return None
        r = self.llm[self.cursor]["response"]
        self.cursor += 1
        return r


def build_trace(tracer: Tracer, state: Dict[str, Any], metrics: Metrics,
                tool_calls: List[ToolCall], tool_results: List[ToolResult],
                violations: List[Dict[str, Any]], checkpoints: List[str]) -> Trace:
    def _msg(m: Any) -> Message:
        if isinstance(m, dict):
            return Message(role=m.get("role", "system"), content=str(m.get("content", "")),
                           kind=m.get("kind", "text"), tainted=bool(m.get("tainted", False)),
                           meta=m.get("meta", {}) if isinstance(m.get("meta", {}), dict) else {})
        return Message("system", str(m))

    messages = [_msg(m) for m in state.get("messages", [])]
    return Trace(
        trace_id=tracer.trace_id, run_id=tracer.run_id,
        task_id=state.get("task_id", ""), config_fingerprint=tracer.config_fingerprint,
        seed=tracer.seed, spans=tracer.spans, messages=messages,
        tool_calls=tool_calls, tool_results=tool_results, violations=violations,
        stop_reason=StopReason(state.get("stop_reason", "FINAL_ANSWER")),
        final_answer=state.get("brief", {}), metrics=metrics,
        checkpoints=checkpoints,
    )
