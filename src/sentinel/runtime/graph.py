"""LangGraph 状态图：整套 harness 的控制流。

节点拓扑（START → END）：

    planner ─► assemble_context ─► generator ─┬─► guard_pre ─► execute_tools ─┬─► approval ─► verify
                     ▲                        │                               │                  ▲
                     │                        ├─(final)───────────────────────┼──────────────┘
                     └──────(loop / retry)────┴───────────────────────────────┘
    verify ─► evaluate[独立子图] ─┬─(pass)──► finish ─► END
                                  └─(fail, iter<2)──► assemble_context

框架负责：状态机、条件边、super-step checkpoint、interrupt 人工审批、
          子图输入过滤（Generator/Evaluator 上下文隔离）。
自研负责：权限门、注入防护、预算、上下文工程、trace、grader、门禁、飞轮。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..business.domain import evaluate_artifact
from ..core import ErrorClass, StopReason, ToolCall, to_dict
from ..runtime.state import EvaluatorState, SentinelState
from .services import Runtime


class SentinelGraph:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.thread_id = rt.run_id
        self._eval_retries = 0
        self._loop_detected = False
        self.graph = self._build()

    # ==================================================================
    # 图构建
    # ==================================================================
    def _build(self) -> Any:
        sub = StateGraph(EvaluatorState)
        sub.add_node("grade", self._grade)
        sub.add_edge(START, "grade")
        sub.add_edge("grade", END)
        evaluator_subgraph = sub.compile()

        b = StateGraph(SentinelState)
        b.add_node("planner", self._planner)
        b.add_node("assemble_context", self._assemble_context)
        b.add_node("generator", self._generator)
        b.add_node("guard_pre", self._guard_pre)
        b.add_node("execute_tools", self._execute_tools)
        b.add_node("approval", self._approval)
        b.add_node("verify", self._verify)
        b.add_node("evaluate", evaluator_subgraph)
        b.add_node("finish", self._finish)

        b.add_edge(START, "planner")
        b.add_conditional_edges("planner", self._route_after_planner,
                                {"continue": "assemble_context", "finish": "finish"})
        b.add_edge("assemble_context", "generator")
        b.add_conditional_edges("generator", self._route_generator,
                                {"tools": "guard_pre", "verify": "verify", "finish": "finish"})
        b.add_edge("guard_pre", "execute_tools")
        b.add_conditional_edges("execute_tools", self._route_after_tools,
                                {"approval": "approval", "loop": "assemble_context", "finish": "finish"})
        b.add_edge("approval", "verify")
        b.add_conditional_edges("verify", self._route_after_verify,
                                {"evaluate": "evaluate", "retry": "assemble_context"})
        b.add_conditional_edges("evaluate", self._route_after_eval,
                                {"finish": "finish", "retry": "assemble_context"})
        b.add_edge("finish", END)
        return b.compile(checkpointer=self.rt.checkpoints.saver)

    # ==================================================================
    # Planner：先把「完成」定义清楚（sprint contract / acceptance criteria）
    # ==================================================================
    def _planner(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("planner", "node", role="planner") as sp:
            ok, hits = self.rt.guard.input_guard(state.get("task_prompt", ""))
            if not ok:
                sp.event("input_guard_tripwire", hits=hits)
                return {
                    "plan": {"schema_version": self.rt.cfg.handoff_schema_version,
                             "original_goal": state.get("task_prompt", ""),
                             "acceptance": ["越权请求必须在执行任何工具前终止"]},
                    "rubric": self.rt.rubric,
                    "stop_reason": StopReason.TRIPWIRE.value,
                    "violations": [{"rule": h, "stage": "input_guard", "severity": "critical"}
                                   for h in hits],
                    "messages": [{"role": "developer", "content": "输入护栏触发，任务被拒绝",
                                  "kind": "text", "tainted": False, "meta": {}}],
                }
            self.rt.model.complete([{"role": "system", "content": "planning route"}], dict(state), "planner")
            plan = {
                "schema_version": self.rt.cfg.handoff_schema_version,
                "original_goal": state.get("task_prompt", ""),
                "subgoals": ["检索外部提及", "主体消歧", "读取信源全文", "情感与传播统计",
                             "影响分级", "证据核验", "生成结构化简报", "对外动作审批"],
                "acceptance": ["证据 ID 全部可追溯", "风险等级与规则一致",
                               "HIGH/CRITICAL 标记人工复核", "输出无 PII"],
                "rubric_version": "rubric/v2",
                "stop_conditions": ["max_turns", "token_budget", "cost_budget", "tripwire", "approval_required"],
            }
            sp.event("sprint_contract", acceptance=plan["acceptance"])
            return {"plan": plan, "rubric": self.rt.rubric,
                    "messages": [{"role": "developer", "content": f"验收契约: {json.dumps(plan['acceptance'], ensure_ascii=False)}",
                                  "kind": "text", "tainted": False, "meta": {}}]}

    def _route_after_planner(self, state: SentinelState) -> Literal["continue", "finish"]:
        return "finish" if state.get("stop_reason") == StopReason.TRIPWIRE.value else "continue"

    # ==================================================================
    # 上下文工程节点：分层组装 + 压缩 + 关键约束重注入
    # ==================================================================
    def _assemble_context(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("assemble_context", "node") as sp:
            feedback = list(state.get("feedback", []))
            feedback += list(state.get("evaluation", {}).get("feedback", []))
            current = state.get("task_prompt", "")
            if feedback:
                current += "\n[上一轮验证/评估反馈] " + "；".join(map(str, feedback[-4:]))
            pkt = self.rt.context.assemble(dict(state), state.get("tool_schema", ""), current)
            handoff = self.rt.context.structured_handoff(dict(state))
            problems = self.rt.context.validate_handoff(handoff)
            if problems:
                self.rt.tracer.audit_event("handoff_invalid", problems=problems)
            sp.event("context", estimated_tokens=pkt.estimated_tokens,
                     compaction_events=pkt.compaction_events)
            return {"context_prompt": pkt.render(), "handoff": handoff,
                    "compaction_events": pkt.compaction_events,
                    "messages": list(state.get("messages", [])) + [
                        {"role": "system", "content": "[上下文已重新组装，关键约束置于首尾]",
                         "kind": "text", "tainted": False, "meta": {"tokens": pkt.estimated_tokens}}]}

    # ==================================================================
    # Generator：只产出意图，没有执行权
    # ==================================================================
    def _generator(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("generator", "node", role="generator") as sp:
            self.rt.budget.tick_turn()
            messages = [{"role": "system", "content": state.get("context_prompt", "")},
                        {"role": "user", "content": state.get("task_prompt", "")}]
            resp = self.rt.model.complete(messages, dict(state), "worker")
            sp.event("model_response", type=resp.type, thought=resp.thought)
            out: Dict[str, Any] = {
                "thoughts": list(state.get("thoughts", [])) + [resp.thought],
                "messages": list(state.get("messages", [])) + [
                    {"role": "assistant", "content": resp.thought or resp.type,
                     "kind": "text", "tainted": False, "meta": {"route": resp.route}}],
                "last_error": {},
            }
            if resp.type == "tool_calls":
                out["pending_calls"] = resp.tool_calls
                out["brief"] = state.get("brief", {})
            elif resp.type == "final":
                out["pending_calls"] = []
                out["brief"] = resp.final.get("brief") or state.get("brief", {})
            else:
                out["pending_calls"] = []
                out["stop_reason"] = StopReason.FATAL_ERROR.value
            return out

    def _route_generator(self, state: SentinelState) -> Literal["tools", "verify", "finish"]:
        stopped = self.rt.budget.check()
        if stopped:
            return "finish"
        if self.rt.guard.tripwire.tripped:
            return "finish"
        calls = state.get("pending_calls", [])
        if not calls:
            return "verify"
        # 死循环检测：同一 (工具, 参数) 连续出现 3 次
        log = state.get("tool_calls_log", [])
        sig = json.dumps(calls, sort_keys=True, ensure_ascii=False)
        if len(log) >= 3 and all(log[-i].get("signature") == sig for i in (1, 2, 3)):
            self._loop_detected = True
            return "finish"
        return "tools"

    # ==================================================================
    # 工具护栏：参数级注入检测（执行拦截在 Sandbox，职责不重叠）
    # ==================================================================
    def _guard_pre(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("guard_pre", "guard") as sp:
            violations = list(state.get("violations", []))
            for c in state.get("pending_calls", []):
                call = ToolCall(self.rt.ids.new("pre"), c["name"], c.get("arguments", {}))
                _, hits = self.rt.guard.tool_guard(call, bool(state.get("tainted")), None)
                if hits:
                    sp.event("injection_signal", tool=call.name, signals=hits)
            for v in self.rt.guard.violations:
                if v not in violations:
                    violations.append(v)
            return {"violations": violations}

    # ==================================================================
    # 工具执行：结果回写、taint 传播、证据登记、审批意图上报
    # ==================================================================
    def _execute_tools(self, state: SentinelState) -> Dict[str, Any]:
        pending = state.get("pending_calls", [])
        calls = [ToolCall(self.rt.ids.new("tc"), c["name"], c.get("arguments", {})) for c in pending]
        self.rt.sandbox_sync(state)
        tctx = self.rt.tool_context(state)
        results = self.rt.executor.execute_batch(calls, tctx)

        payloads = dict(state.get("tool_payloads", {}))
        completed = list(state.get("completed_tools", []))
        mention_ids = list(state.get("mention_ids", []))
        fetched = list(state.get("fetched_ids", []))
        messages = list(state.get("messages", []))
        calls_log = list(state.get("tool_calls_log", []))
        results_log = list(state.get("tool_results_log", []))
        violations = list(state.get("violations", []))
        blocked = list(state.get("blocked_tools", []))
        injection_seen = bool(state.get("injection_seen"))
        injection_attempted = bool(state.get("injection_attempted"))
        tainted = bool(state.get("tainted"))
        needs_approval = bool(state.get("needs_approval"))
        approval_payload = dict(state.get("approval_payload", {}))
        last_error: Dict[str, Any] = {}

        with self.rt.tracer.span("execute_tools", "tool_batch", count=len(calls)) as sp:
            sig = json.dumps(pending, sort_keys=True, ensure_ascii=False)
            for call, r in zip(calls, results):
                self.rt.tool_calls.append(call)
                self.rt.tool_results.append(r)
                self.rt.recorder.record_tool(call, r)
                calls_log.append({"name": call.name, "arguments": call.arguments, "signature": sig})
                results_log.append({"tool": r.tool, "ok": r.ok, "blocked_by": r.blocked_by,
                                    "error": r.error, "retries": r.retries})

                if r.ok and isinstance(r.payload, dict):
                    p = r.payload
                    payloads[call.name] = p
                    if call.name not in completed:
                        completed.append(call.name)
                    if call.name == "search_mentions":
                        mention_ids = [m["id"] for m in p.get("mentions", [])]
                    if call.name == "fetch_source":
                        sid = p.get("source_id", "")
                        if sid and sid not in fetched:
                            fetched.append(sid)
                        if p.get("injection_suspected"):
                            injection_seen = True
                            tainted = True
                            self.rt.sandbox_ctx.tainted = True
                            report = self.rt.guard.detector.inspect(p.get("body", ""), sid)
                            violations.append({"rule": "indirect_prompt_injection",
                                               "source_id": sid, "signals": report.signals,
                                               "action": "neutralized_as_data"})
                            sp.event("injection_detected", source=sid, signals=report.signals)
                    preview = json.dumps(p, ensure_ascii=False, default=str)[:700]
                    messages.append({"role": "tool", "content": preview,
                                     "kind": "untrusted_data" if p.get("untrusted") else "tool_result",
                                     "tainted": bool(p.get("untrusted")), "meta": {"tool": call.name}})
                    if call.name == "impact_score":
                        # 供 Evaluator 做风险校准（属于 artifact 证据，不是推理链）
                        payloads["signals"] = p.get("signals", {})
                else:
                    if r.blocked_by == "pending_approval":
                        needs_approval = True
                        approval_payload = self.rt.guard.approval.request_payload(call)
                        approval_payload["call"] = {"name": call.name, "arguments": call.arguments}
                    else:
                        blocked.append(call.name)
                        if call.name == "publish_alert" and injection_seen:
                            injection_attempted = True
                        violations.append({"rule": r.blocked_by or "tool_error",
                                           "tool": call.name, "detail": r.error})
                    if r.error_class is ErrorClass.LLM_RECOVERABLE:
                        last_error = {"tool": call.name, "error": r.error,
                                      "error_class": r.error_class.value}
                    messages.append({"role": "tool",
                                     "content": f"[TOOL_ERROR] {call.name}: {r.error}",
                                     "kind": "tool_result", "tainted": False,
                                     "meta": {"tool": call.name, "error": True}})
                    sp.event("tool_failure", tool=call.name, reason=r.error, blocked=r.blocked_by)

        self.rt.violations = violations
        return {
            "tool_payloads": payloads, "completed_tools": completed,
            "mention_ids": mention_ids, "fetched_ids": fetched,
            "messages": messages, "tool_calls_log": calls_log, "tool_results_log": results_log,
            "violations": violations, "blocked_tools": blocked,
            "injection_seen": injection_seen, "injection_attempted": injection_attempted,
            "tainted": tainted, "needs_approval": needs_approval,
            "approval_payload": approval_payload, "last_error": last_error,
            "pending_calls": [],
        }

    def _route_after_tools(self, state: SentinelState) -> Literal["approval", "loop", "finish"]:
        if state.get("needs_approval") and not state.get("approval_done"):
            return "approval"
        stopped = self.rt.budget.check()
        if stopped or self.rt.guard.tripwire.tripped:
            return "finish"
        return "loop"

    # ==================================================================
    # HITL：LangGraph interrupt。审批疲劳的对立面是「只对不可逆外部动作问一次」
    # ==================================================================
    def _approval(self, state: SentinelState) -> Dict[str, Any]:
        payload = state.get("approval_payload", {})
        decision = interrupt(payload)          # 暂停；由 Command(resume=...) 唤醒
        self.rt.guard.approval.record(decision)
        self.rt.metrics.approvals += 1
        approved = bool(decision.get("approved", False))
        with self.rt.tracer.span("approval", "approval", approved=approved) as sp:
            sp.event("human_decision", reviewer=decision.get("reviewer", "unknown"), approved=approved)
            out: Dict[str, Any] = {
                "approval_done": True, "approval_decision": decision,
                "needs_approval": False,
                "brief": state.get("brief") or state.get("tool_payloads", {}).get("draft_brief", {}).get("brief", {}),
                "violations": list(state.get("violations", [])),
            }
            if approved:
                c = payload.get("call", {})
                self.rt.sandbox_ctx.approved_tools.add(c.get("name", ""))
                call = ToolCall(self.rt.ids.new("tc"), c.get("name", ""), c.get("arguments", {}))
                r = self.rt.executor.execute(call, self.rt.tool_context(state))
                self.rt.tool_calls.append(call)
                self.rt.tool_results.append(r)
                out["violations"].append({"rule": "external_action_approved",
                                          "tool": call.name, "ok": r.ok})
            else:
                out["violations"].append({"rule": "external_action_denied_by_human",
                                          "tool": payload.get("call", {}).get("name", "")})
            return out

    # ==================================================================
    # Prover / goal-mode：模型说 "done" 不算完成，确定性检查说了算
    # ==================================================================
    def _verify(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("verify", "guard") as sp:
            brief = state.get("brief") or state.get("tool_payloads", {}).get("draft_brief", {}).get("brief", {})
            problems: List[str] = []
            if not brief:
                problems.append("goal_mode: 未产出结构化简报，禁止提前宣布完成")
            else:
                from ..core import ImpactBrief
                from ..business.domain import brief_from_dict
                problems.extend(brief_from_dict(brief).validate())
                ok, out_problems = self.rt.guard.output_guard(brief, self.rt.evidence_registry)
                problems.extend(out_problems)
            sp.event("verification", passed=not problems, problems=problems[:5])
            out: Dict[str, Any] = {"brief": brief, "feedback": problems,
                                   "signals": state.get("tool_payloads", {}).get("signals", {}),
                                   "violations": list(state.get("violations", []))}
            if problems:
                out["iteration"] = int(state.get("iteration", 0)) + 1
                out["violations"].append({"rule": "goal_mode_recheck", "problems": problems})
            return out

    def _route_after_verify(self, state: SentinelState) -> Literal["evaluate", "retry"]:
        if state.get("feedback") and int(state.get("iteration", 0)) < 2:
            return "retry"
        return "evaluate"

    # ==================================================================
    # Evaluator 子图：只接收 brief / rubric / evidence_registry / signals
    # ==================================================================
    def _grade(self, state: EvaluatorState) -> Dict[str, Any]:
        with self.rt.tracer.span("evaluator.grade", "eval") as sp:
            result = evaluate_artifact(state.get("brief", {}), self.rt.feeds, state.get("signals"))
            sp.event("rubric_score", score=result["score"], passed=result["passed"])
            return {"evaluation": result}

    def _route_after_eval(self, state: SentinelState) -> Literal["finish", "retry"]:
        ev = state.get("evaluation", {})
        if ev.get("passed"):
            return "finish"
        # 迭代次数必须收敛：反馈回灌最多一次，避免 Generator–Evaluator 互相拉扯
        if self._eval_retries < 1 and int(state.get("iteration", 0)) < 2:
            self._eval_retries += 1
            return "retry"
        return "finish"

    # ==================================================================
    def _finish(self, state: SentinelState) -> Dict[str, Any]:
        with self.rt.tracer.span("finish", "node"):
            stopped = self.rt.budget.check()
            reason = (stopped
                      or (StopReason.LOOP_DETECTED if self._loop_detected else None)
                      or (StopReason.TRIPWIRE if self.rt.guard.tripwire.tripped else None)
                      or StopReason(state.get("stop_reason", "FINAL_ANSWER")))
            history = self.rt.checkpoints.history(self.graph, self.thread_id)
            final = {
                "run_id": self.rt.run_id, "trace_id": self.rt.trace_id,
                "task_id": state.get("task_id", ""),
                "config_fingerprint": self.rt.fp, "seed": self.rt.seed,
                "stop_reason": reason.value,
                "brief": state.get("brief", {}),
                "evaluation": state.get("evaluation", {}),
                "metrics": self.rt.budget.snapshot(),
                "violations": state.get("violations", []),
                "blocked_tools": state.get("blocked_tools", []),
                "approval": state.get("approval_decision", {}),
                "injection_seen": state.get("injection_seen", False),
                "tainted": state.get("tainted", False),
                "checkpoint_count": len(history),
                "compaction_events": state.get("compaction_events", []),
                "handoff": state.get("handoff", {}),
            }
            return {"final_output": final, "stop_reason": reason.value,
                    "checkpoint_ids": [h.get("checkpoint_id") for h in history if h.get("checkpoint_id")]}

    # ==================================================================
    def run(self, initial: Optional[Dict[str, Any]] = None,
            approval_decision: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = initial or self.rt.initial_state()
        cfg = self.rt.checkpoints.config(self.thread_id)
        out = self.graph.invoke(state, config=cfg)
        if "__interrupt__" in out and approval_decision is not None:
            out = self.rt.checkpoints.resume(self.graph, self.thread_id, approval_decision)
        return out

    def history(self) -> List[Dict[str, Any]]:
        return self.rt.checkpoints.history(self.graph, self.thread_id)


def build_graph(rt: Runtime) -> SentinelGraph:
    return SentinelGraph(rt)
