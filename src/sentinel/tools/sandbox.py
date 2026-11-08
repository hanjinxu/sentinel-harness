"""沙箱与权限门：把「策略」写成代码，而不是写进 system prompt。

三类确定性切断（对应 2026 年公开的 agent 安全实践）：
1. 最小权限：每次 run 授予的 scope = 工具声明 scope ∩ run 授予 scope；
2. lethal trifecta：读过不可信内容(tainted) 后，禁止任何对外/写动作；
3. 出站目的地 allowlist：外部副作用必须命中白名单，且需人工审批。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..core import ToolRisk
from .registry import ToolSpec


class PolicyViolation(Exception):
    def __init__(self, rule: str, detail: str):
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{12,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[REDACTED_ID]"),
]


def redact(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


@dataclass
class SandboxContext:
    run_id: str
    granted_scopes: List[str]
    allowed_hosts: List[str]
    workspace_root: str
    approval_mode: str = "human"
    tainted: bool = False                  # 是否检测到注入（而非「读过任何外部内容」）
    approved_tools: set[str] = field(default_factory=set)   # 人工审批通过后的当次授权
    audit: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Decision:
    allowed: bool
    requires_approval: bool = False
    sanitized_args: Dict[str, Any] = field(default_factory=dict)
    rule: str = ""
    reason: str = ""
    redactions: int = 0

    @property
    def blocked(self) -> bool:
        return not self.allowed


class Sandbox:
    """权限判定不经过模型，模型只能提出意图，能否执行由本类决定。"""

    def __init__(self, allowed_hosts: List[str], workspace_root: str):
        self.allowed_hosts = set(allowed_hosts)
        self.workspace_root = workspace_root

    def check(self, spec: ToolSpec, args: Dict[str, Any], ctx: SandboxContext) -> Decision:
        clean, n_red = self._redact_args(args)
        audit = {"tool": spec.name, "risk": spec.risk.value, "redactions": n_red,
                 "tainted": ctx.tainted, "args_keys": sorted(clean)}
        d = Decision(allowed=True, sanitized_args=clean, redactions=n_red)

        # 1) scope 交集：声明的权限只能被 run 授予的 scope 收窄，不能被放大
        missing = [s for s in spec.scopes if s not in ctx.granted_scopes]
        if missing:
            d.allowed, d.rule, d.reason = False, "least_privilege", f"scope_not_granted:{missing}"
        # 2) 不可逆动作默认拒绝
        elif spec.risk is ToolRisk.DESTRUCTIVE:
            d.allowed, d.rule, d.reason = False, "destructive_denied", "destructive tools are disabled"
        # 3) 出站目的地白名单（URL / host / channel 都是潜在外泄信道）
        elif not self._destination_ok(spec, clean):
            d.allowed, d.rule, d.reason = False, "egress_allowlist", "destination outside allowlist"
        # 4) taint 传播：检测到注入后禁止对外副作用，切断「敏感数据 + 不可信内容 + 外发通道」
        #    注意内部可逆写入(draft_brief)仍允许：否则舆情 Agent 读完外部文本就无法成文。
        elif ctx.tainted and spec.risk in {ToolRisk.EXTERNAL, ToolRisk.DESTRUCTIVE}:
            d.allowed, d.rule, d.reason = False, "taint_propagation", "injection detected -> external action denied"
        # 5) 路径约束
        elif not self._path_ok(clean):
            d.allowed, d.rule, d.reason = False, "sandbox_path", "path escapes workspace"
        else:
            if (spec.risk is ToolRisk.EXTERNAL or spec.requires_approval) and spec.name not in ctx.approved_tools:
                d.requires_approval = True
                d.rule = "approval_required"
            elif spec.name in ctx.approved_tools:
                d.rule = "approval_granted"

        audit.update({"decision": "allow" if d.allowed else "deny", "rule": d.rule,
                      "requires_approval": d.requires_approval})
        ctx.audit.append(audit)
        return d

    def _destination_ok(self, spec: ToolSpec, args: Dict[str, Any]) -> bool:
        if spec.risk not in {ToolRisk.EXTERNAL, ToolRisk.WRITE}:
            return True
        for key in ("url", "host", "destination", "endpoint", "channel"):
            val = args.get(key)
            if not isinstance(val, str) or not val:
                continue
            # channel 常是业务渠道名（如 risk_alert），只有看起来像地址时才做出站校验
            if key == "channel" and "." not in val and "://" not in val:
                continue
            host = val if "://" not in val else (urlparse(val).hostname or "")
            allowed = set(self.allowed_hosts) | set(spec.allowed_hosts)
            if host and host not in allowed:
                return False
        return True

    def _path_ok(self, args: Dict[str, Any]) -> bool:
        for key in ("path", "filepath", "output_path"):
            val = args.get(key)
            if isinstance(val, str) and (".." in val or val.startswith("/etc") or val.startswith("~")):
                return False
        return True

    @staticmethod
    def _redact_args(args: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
        out: Dict[str, Any] = {}
        count = 0
        for k, v in args.items():
            if isinstance(v, str):
                nv = redact(v)
                count += int(nv != v)
                out[k] = nv
            elif isinstance(v, dict):
                sub, n = Sandbox._redact_args(v)
                count += n
                out[k] = sub
            elif isinstance(v, list):
                out[k] = [redact(x) if isinstance(x, str) else x for x in v]
            else:
                out[k] = v
        return out, count
