"""工具注册与参数校验。

设计要点：
- ToolSpec 同时导出 OpenAI function schema 与 MCP tool schema，
  说明「工具协议」与「执行策略」是解耦的两层；
- 参数校验发生在执行之前，属于确定性控制，不依赖模型自觉。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core import ToolRisk


class ValidationError(ValueError):
    """参数不符合 ToolSpec 声明的 JSON Schema。属于 LLM_RECOVERABLE：错误回传给模型改参。"""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]               # JSON Schema (object)
    risk: ToolRisk
    scopes: List[str]                        # 最小权限声明
    handler: Callable[[Dict[str, Any], Any], Any]
    readonly: bool = True
    timeout_ms: int = 800
    max_retries: int = 2
    idempotent: bool = True
    allowed_hosts: List[str] = field(default_factory=list)
    requires_approval: bool = False
    side_effect: str = "none"                # none | internal | external | destructive
    version: str = "1.0"

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description + f" [risk={self.risk.value}]",
                "parameters": self.parameters,
            },
        }

    def to_mcp_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
            "annotations": {
                "readOnlyHint": self.readonly,
                "destructiveHint": self.side_effect == "destructive",
                "idempotentHint": self.idempotent,
                "openWorldHint": self.side_effect == "external",
            },
            "_sentinel": {"risk": self.risk.value, "scopes": self.scopes, "version": self.version},
        }


def validate_args(schema: Dict[str, Any], args: Dict[str, Any]) -> List[str]:
    """轻量 JSON Schema 校验器（零依赖）。返回错误列表，供模型自纠错。"""
    errors: List[str] = []
    if schema.get("type", "object") != "object":
        return ["schema_root_must_be_object"]
    if not isinstance(args, dict):
        return ["arguments_must_be_object"]
    props: Dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            errors.append(f"missing_required:{key}")
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                errors.append(f"unexpected_property:{key}")
    for key, value in args.items():
        rule = props.get(key)
        if rule is None:
            continue
        t = rule.get("type")
        ok = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }.get(t)
        if ok and not ok(value):
            errors.append(f"type_mismatch:{key}:expected_{t}")
            continue
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"enum_violation:{key}")
        if isinstance(value, str):
            if "minLength" in rule and len(value) < rule["minLength"]:
                errors.append(f"too_short:{key}")
            if "pattern" in rule and not re.search(rule["pattern"], value):
                errors.append(f"pattern_violation:{key}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                errors.append(f"below_minimum:{key}")
            if "maximum" in rule and value > rule["maximum"]:
                errors.append(f"above_maximum:{key}")
        if isinstance(value, list) and "items" in rule:
            item_t = rule["items"].get("type")
            for i, item in enumerate(value):
                if item_t == "string" and not isinstance(item, str):
                    errors.append(f"array_item_type:{key}[{i}]")
    return errors


class ToolRegistry:
    """工具目录。运行时与评测环境各建一份，保证 trial 之间无共享状态。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate_tool:{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ValidationError(f"unknown_tool:{name}")
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def validate(self, name: str, args: Dict[str, Any]) -> List[str]:
        spec = self._tools.get(name)
        if spec is None:
            return [f"unknown_tool:{name}"]
        return validate_args(spec.parameters, args)

    def openai_tools(self) -> List[Dict[str, Any]]:
        return [self._tools[n].to_openai_schema() for n in sorted(self._tools)]

    def mcp_tools(self) -> List[Dict[str, Any]]:
        return [self._tools[n].to_mcp_schema() for n in sorted(self._tools)]

    def specs(self) -> List[ToolSpec]:
        return [self._tools[n] for n in sorted(self._tools)]
