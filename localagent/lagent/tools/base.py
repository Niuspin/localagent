"""工具基础设施：注册表、参数校验/纠错（小模型友好）、结果截断。"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    content: str
    error: bool = False
    meta: dict = field(default_factory=dict)
    images: list[str] = field(default_factory=list)   # data URL：由循环作为一条 user 消息附给模型（视觉）
    taint: str = ""                                   # 非空 => 本次结果含不可信内容，会话进入“污染”状态
    untrusted_source: str = ""                        # 非空 => 循环用 <untrusted> 包裹 content


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[ToolResult]]
    kind: str                       # 见 security.Policy
    group: str = "core"
    title: Callable[[dict], str] | None = None
    preview: Callable[..., Awaitable[dict | None]] | None = None   # async (ctx, args) -> {"kind","text"}
    path_args: tuple[str, ...] = ()
    # 执行/审批之前的同步预检：返回错误文本则直接拒绝（不打扰用户审批一个注定失败的操作）
    precheck: Callable[..., str | None] | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    def describe(self, args: dict) -> str:
        try:
            return self.title(args) if self.title else self.name
        except Exception:  # noqa: BLE001
            return self.name


TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    TOOLS[tool.name] = tool
    return tool


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def P(type_: str, desc: str, **kw: Any) -> dict:
    d = {"type": type_, "description": desc}
    d.update(kw)
    return d


# ----------------------------------------------------------------- JSON 容错
def repair_json(s: str) -> Any | None:
    """小模型常见的参数 JSON 瑕疵：代码围栏、单引号、尾逗号、缺括号。"""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if not t:
        return {}
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for cand in (t, re.sub(r",\s*([}\]])", r"\1", t)):
        try:
            return json.loads(cand)
        except ValueError:
            pass
    try:
        v = ast.literal_eval(t)
        if isinstance(v, dict):
            return v
    except (ValueError, SyntaxError):
        pass
    opens = t.count("{") - t.count("}")
    brk = t.count("[") - t.count("]")
    if opens > 0 or brk > 0:
        try:
            return json.loads(re.sub(r",\s*$", "", t) + "]" * max(brk, 0) + "}" * max(opens, 0))
        except ValueError:
            pass
    return None


def _coerce(name: str, spec: dict, v: Any) -> Any:
    t = spec.get("type")
    if t == "string":
        if isinstance(v, str):
            out = v
        elif isinstance(v, (int, float, bool)):
            out = str(v)
        else:
            raise ValueError(f"参数 {name} 应为字符串")
    elif t == "integer":
        if isinstance(v, bool):
            raise ValueError(f"参数 {name} 应为整数")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 应为整数")
        if f != int(f):
            raise ValueError(f"参数 {name} 应为整数")
        out = int(f)
    elif t == "number":
        try:
            out = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 应为数字")
    elif t == "boolean":
        if isinstance(v, bool):
            out = v
        elif isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no"):
            out = v.lower() in ("true", "1", "yes")
        else:
            raise ValueError(f"参数 {name} 应为布尔值")
    elif t == "array":
        if isinstance(v, str):
            v = repair_json(v)
        if not isinstance(v, list):
            raise ValueError(f"参数 {name} 应为数组")
        it = spec.get("items")
        out = [_coerce(f"{name}[]", it, x) for x in v] if it else v
    elif t == "object":
        if isinstance(v, str):
            v = repair_json(v)
        if not isinstance(v, dict):
            raise ValueError(f"参数 {name} 应为对象")
        out = coerce_args(spec, v)[0] if spec.get("properties") else v
    else:
        out = v
    if "enum" in spec and out not in spec["enum"]:
        raise ValueError(f"参数 {name} 必须是 {spec['enum']} 之一")
    return out


def coerce_args(schema: dict, args: Any) -> tuple[dict, str | None]:
    if not isinstance(args, dict):
        return {}, "参数必须是 JSON 对象"
    props = schema.get("properties", {})
    out: dict = {}
    try:
        for k, v in args.items():
            if k in props and v is not None:
                out[k] = _coerce(k, props[k], v)
    except ValueError as e:
        return out, str(e)
    missing = [r for r in schema.get("required", []) if r not in out]
    if missing:
        return out, f"缺少必填参数: {', '.join(missing)}"
    return out, None


def clip(text: str, limit: int, hint: str = "") -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.7), int(limit * 0.2)
    cut = len(text) - head - tail
    return f"{text[:head]}\n…[已截断 {cut} 字符{('；' + hint) if hint else ''}]…\n{text[-tail:]}"
