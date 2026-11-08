"""工具层：参数校验、协议导出、权限门、幂等、重试与熔断。"""
from __future__ import annotations

import unittest

from tests._utils import runtime_for

from sentinel.core import ErrorClass, ToolCall
from sentinel.tools.builtin import register_builtin_tools
from sentinel.tools.executor import FaultProfile, ToolContext, ToolExecutor
from sentinel.tools.registry import ToolRegistry, validate_args
from sentinel.tools.sandbox import Sandbox, SandboxContext, redact


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = register_builtin_tools(ToolRegistry())

    def test_schema_validation(self):
        self.assertEqual(self.reg.validate("fetch_source", {"source_id": "SRC-001"}), [])
        self.assertIn("missing_required:source_id", self.reg.validate("fetch_source", {}))
        errs = self.reg.validate("fetch_source", {"source_id": 123})
        self.assertTrue(any("type_mismatch" in e for e in errs))
        errs = self.reg.validate("unknown_tool", {})
        self.assertEqual(errs, ["unknown_tool:unknown_tool"])

    def test_json_schema_features(self):
        schema = {"type": "object", "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
            "s": {"type": "string", "enum": ["a", "b"]}},
            "required": ["n"], "additionalProperties": False}
        self.assertEqual(validate_args(schema, {"n": 5, "s": "a"}), [])
        self.assertTrue(validate_args(schema, {"n": 0, "s": "a"}))
        self.assertTrue(validate_args(schema, {"n": 5, "extra": 1}))

    def test_protocol_export(self):
        openai = self.reg.openai_tools()
        mcp = self.reg.mcp_tools()
        self.assertEqual(len(openai), len(mcp))
        self.assertEqual(openai[0]["type"], "function")
        self.assertIn("inputSchema", mcp[0])
        self.assertIn("readOnlyHint", mcp[0]["annotations"])
        self.assertIn("_sentinel", mcp[0])


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.reg = register_builtin_tools(ToolRegistry())
        self.sb = Sandbox(["news.mock", "feeds.internal", "riskdb.internal"], "artifacts")

    def ctx(self, scopes=None, tainted=False):
        return SandboxContext(run_id="r1", granted_scopes=scopes or ["feed:read"],
                              allowed_hosts=["news.mock"], workspace_root="artifacts",
                              tainted=tainted)

    def test_least_privilege_scope_intersection(self):
        spec = self.reg.require("publish_alert")
        d = self.sb.check(spec, {"brief_id": "b", "channel": "risk_alert",
                                 "destination": "alerts.internal"}, self.ctx(["feed:read"]))
        self.assertTrue(d.blocked)
        self.assertEqual(d.rule, "least_privilege")

    def test_egress_allowlist(self):
        spec = self.reg.require("publish_alert")
        d = self.sb.check(spec, {"brief_id": "b", "channel": "webhook",
                                 "destination": "attacker-collect.example.com"},
                          self.ctx(["alert:publish"]))
        self.assertEqual(d.rule, "egress_allowlist")

    def test_taint_blocks_external_even_with_whitelisted_host(self):
        spec = self.reg.require("publish_alert")
        d = self.sb.check(spec, {"brief_id": "b", "channel": "risk_alert",
                                 "destination": "alerts.internal"},
                          self.ctx(["alert:publish"], tainted=True))
        self.assertEqual(d.rule, "taint_propagation")

    def test_internal_write_allowed_after_taint(self):
        spec = self.reg.require("draft_brief")
        args = {"event_summary": "摘要内容足够长以通过校验", "sentiment": "negative",
                "topic": "数据安全", "window_hours": 24, "evidence_ids": ["SRC-001"]}
        d = self.sb.check(spec, args, self.ctx(["brief:write"], tainted=True))
        self.assertTrue(d.allowed, "内部可逆写入不应被 taint 一刀切禁止")

    def test_path_escape_blocked(self):
        spec = self.reg.require("read_internal_note")
        d = self.sb.check(spec, {"path": "../../../etc/passwd"}, self.ctx(["note:read"]))
        self.assertEqual(d.rule, "sandbox_path")

    def test_dlp(self):
        self.assertIn("[REDACTED_PHONE]", redact("联系电话 13812345678"))
        self.assertIn("[REDACTED_EMAIL]", redact("mail to a.b@corp.com"))


class TestExecutor(unittest.TestCase):
    def setUp(self):
        rt, _ = runtime_for()
        self.rt = rt
        self.ex = ToolExecutor(rt.registry, rt.sandbox,
                               fault=FaultProfile(seed=1), metrics=rt.metrics,
                               tracer=rt.tracer)
        self.tctx = ToolContext(run_id=rt.run_id, feeds=rt.feeds,
                                state={"tool_payloads": {}, "injection_source_ids": []},
                                sandbox_ctx=rt.sandbox_ctx, ids=rt.ids,
                                clock=lambda: 0, artifacts_dir=rt.artifacts_dir)
        # 测试需要写工具权限
        self.rt.sandbox_ctx.granted_scopes = list(self.rt.sandbox_ctx.granted_scopes) + [
            "brief:write", "alert:publish"]

    def test_validation_error_is_llm_recoverable(self):
        r = self.ex.execute(ToolCall("c1", "fetch_source", {}), self.tctx)
        self.assertFalse(r.ok)
        self.assertIs(r.error_class, ErrorClass.LLM_RECOVERABLE)

    def test_idempotent_cache(self):
        a = self.ex.execute(ToolCall("c2", "search_mentions",
                                     {"query": "星澜科技", "window_hours": 24, "limit": 8}), self.tctx)
        b = self.ex.execute(ToolCall("c3", "search_mentions",
                                     {"query": "星澜科技", "window_hours": 24, "limit": 8}), self.tctx)
        self.assertTrue(a.ok and b.ok)
        self.assertTrue(b.cached)

    def test_retry_then_circuit_breaker(self):
        from sentinel.tools.executor import FaultInjector
        self.ex.fault = FaultInjector(FaultProfile(persistent={"risk_lookup": 1.0}, seed=3))
        call = lambda i: ToolCall(i, "risk_lookup", {"topic": "数据安全"})
        r1 = self.ex.execute(call("c4"), self.tctx)
        self.assertFalse(r1.ok)
        self.assertEqual(r1.error_class, ErrorClass.TRANSIENT)
        r2 = self.ex.execute(call("c5"), self.tctx)
        self.assertFalse(r2.ok)
        r3 = self.ex.execute(call("c6"), self.tctx)
        self.assertEqual(r3.error, "circuit_open")


if __name__ == "__main__":
    unittest.main()
