"""MCP ⇄ 本项目格式的转换。全是纯函数，不碰进程和网络，方便单独测试。

设计取舍（本地小模型的上下文很贵）：
- 工具描述/参数描述一律截断；整个 schema 超过 3000 字符时丢掉参数描述；每个服务器最多暴露 40 个工具。
- 工具名、描述来自外部服务器，属于不可信输入：先清洗（去不可见字符），再做“提示注入”审查，命中就不暴露这个工具。
- 调用结果默认当作不可信内容（会污染会话、用 <untrusted> 包裹），除非用户明确把该服务器设为“信任结果”。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..security import sanitize_untrusted, suspicious
from ..tools.base import ToolResult

MAX_TOOLS = 40
DESC_TOOL = 300
DESC_PARAM = 120
SCHEMA_BUDGET = 3000
_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
IMG_OK = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
_SLUG = re.compile(r"[^a-z0-9_]+")


def slug(s: str) -> str:
    return _SLUG.sub("_", (s or "").lower()).strip("_") or "x"


def tool_name(server: str, tool: str) -> str:
    """mcp__<服务器>__<工具>，最长 64 字符（很多模板对函数名长度有限制），超长时截断并补哈希保证唯一。"""
    base = f"mcp__{slug(server)}__{slug(tool)}"
    if len(base) <= 64:
        return base
    return base[:57] + "_" + hashlib.sha1(tool.encode("utf-8")).hexdigest()[:6]


def clean_text(s: Any, limit: int) -> str:
    t = re.sub(r"\s+", " ", sanitize_untrusted(str(s or ""))).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


# ---------------------------------------------------------------- schema
def _resolve_ref(ref: Any, defs: dict, seen: frozenset[str]) -> dict | None:
    if not isinstance(ref, str) or ref in seen:
        return None
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            key = ref[len(prefix):]
            target = defs.get(key)
            return target if isinstance(target, dict) else None
    return None


def _spec(sp: Any, depth: int, with_desc: bool, defs: dict | None = None, seen: frozenset[str] = frozenset()) -> dict:
    if not isinstance(sp, dict):
        return {}
    defs = defs or {}
    if isinstance(sp.get("$ref"), str) and depth < 3:
        ref = sp["$ref"]
        target = _resolve_ref(ref, defs, seen)
        if target is not None:
            merged = {**target, **{k: v for k, v in sp.items() if k != "$ref"}}
            return _spec(merged, depth + 1, with_desc, defs, seen | {ref})
    out: dict[str, Any] = {}
    t = sp.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t in _TYPES:
        out["type"] = t
    if with_desc and isinstance(sp.get("description"), str):
        out["description"] = clean_text(sp["description"], DESC_PARAM)
    en = sp.get("enum")
    if isinstance(en, list) and 0 < len(en) <= 50 and all(isinstance(x, (str, int, float, bool)) for x in en):
        out["enum"] = en
    if not t and depth < 2:
        for union_key in ("anyOf", "oneOf"):
            raw_u = sp.get(union_key)
            if isinstance(raw_u, list) and raw_u:
                variants = []
                for v in raw_u[:6]:
                    if isinstance(v, dict) and v.get("type") == "null":
                        continue
                    sv = _spec(v, depth + 1, with_desc, defs, seen)
                    if sv:
                        variants.append(sv)
                if len(variants) == 1:
                    desc = out.get("description")
                    out.update(variants[0])
                    if desc:
                        out["description"] = desc
                elif len(variants) > 1:
                    out[union_key] = variants[:4]
                break
    if t == "array" and depth < 2 and isinstance(sp.get("items"), dict):
        it = _spec(sp["items"], depth + 1, with_desc, defs, seen)
        if it.get("type") or it.get("anyOf") or it.get("oneOf"):
            out["items"] = it
    if t == "object" and depth < 2 and isinstance(sp.get("properties"), dict):
        props = {str(k): _spec(v, depth + 1, with_desc, defs, seen) for k, v in list(sp["properties"].items())[:30]}
        out["properties"] = props
        out["required"] = [r for r in (sp.get("required") or []) if isinstance(r, str) and r in props]
    return out


def convert_schema(input_schema: Any) -> dict:
    s = input_schema if isinstance(input_schema, dict) else {}
    defs: dict[str, Any] = {}
    for dk in ("$defs", "definitions"):
        if isinstance(s.get(dk), dict):
            defs.update(s[dk])
    raw = s.get("properties") if isinstance(s.get("properties"), dict) else {}
    for with_desc in (True, False):
        props = {str(k): _spec(v, 0, with_desc, defs) for k, v in list(raw.items())[:40]}
        out = {"type": "object", "properties": props, "required": [r for r in (s.get("required") or []) if isinstance(r, str) and r in props]}
        if len(json.dumps(out, ensure_ascii=False)) <= SCHEMA_BUDGET:
            break
    return out


def vet_resources(raw_res: list[dict]) -> list[dict]:
    ok: list[dict] = []
    for r in raw_res:
        if not isinstance(r, dict) or not isinstance(r.get("uri"), str) or not r["uri"].strip():
            continue
        blob = " ".join([r.get("uri", ""), str(r.get("name") or ""), str(r.get("description") or "")])
        if suspicious(blob):
            continue
        ok.append({
            "uri": str(r["uri"])[:500],
            "name": clean_text(r.get("name") or r["uri"], 80),
            "description": clean_text(r.get("description") or "", DESC_TOOL),
            "mimeType": str(r.get("mimeType") or "")[:120],
        })
        if len(ok) >= MAX_TOOLS:
            break
    return ok


def vet_prompts(raw_prompts: list[dict]) -> list[dict]:
    ok: list[dict] = []
    for p in raw_prompts:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            continue
        blob = " ".join([p["name"], str(p.get("description") or "")])
        if suspicious(blob):
            continue
        args = []
        for a in (p.get("arguments") or [])[:20]:
            if isinstance(a, dict) and isinstance(a.get("name"), str):
                args.append({"name": clean_text(a["name"], 64), "description": clean_text(a.get("description") or "", DESC_PARAM),
                             "required": bool(a.get("required", False))})
        ok.append({"name": clean_text(p["name"], 80), "description": clean_text(p.get("description") or "", DESC_TOOL), "arguments": args})
        if len(ok) >= MAX_TOOLS:
            break
    return ok


# ---------------------------------------------------------------- 工具审查
def vet_tools(raw_tools: list[dict]) -> tuple[list[dict], list[str]]:
    """清洗并审查服务器给出的工具列表。返回 (可暴露的工具, 警告)。工具名不合法、描述含提示注入的一律丢弃。"""
    ok: list[dict] = []
    warns: list[str] = []
    for t in raw_tools:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"].strip() or len(t["name"]) > 128:
            continue
        name = t["name"]
        blob = " ".join([name, str(t.get("description") or ""), json.dumps(t.get("inputSchema") or {}, ensure_ascii=False)])
        why = suspicious(blob)
        if why:
            warns.append(f"工具 {clean_text(name, 40)} 已忽略：{why[0]}")
            continue
        if len(ok) >= MAX_TOOLS:
            warns.append(f"工具太多，只保留前 {MAX_TOOLS} 个（避免撑大上下文）")
            break
        ok.append({"name": name, "description": clean_text(t.get("description") or name, DESC_TOOL),
                   "inputSchema": convert_schema(t.get("inputSchema"))})
    return ok, warns


# ---------------------------------------------------------------- 调用结果
def result_to_toolresult(res: Any, *, server: str, tool: str, trusted: bool, vision: bool) -> ToolResult:
    res = res if isinstance(res, dict) else {}
    parts: list[str] = []
    images: list[str] = []
    content = res.get("content") if isinstance(res.get("content"), list) else []
    for c in content[:50]:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append(str(c.get("text", "")))
        elif t == "image":
            mime, data = str(c.get("mimeType", "")).lower(), c.get("data")
            if vision and mime in IMG_OK and isinstance(data, str) and len(data) <= 6_000_000:
                images.append(f"data:{mime};base64,{data}")
                parts.append(f"[图片 {mime}，已附在下一条消息中]")
            else:
                parts.append(f"[图片 {mime or '未知类型'}：{'当前模型没有视觉能力' if not vision else '格式不支持或过大'}，已省略]")
        elif t == "audio":
            parts.append("[音频内容已省略]")
        elif t == "resource":
            r = c.get("resource") if isinstance(c.get("resource"), dict) else {}
            uri = r.get("uri", "")
            parts.append(f"[资源 {uri}]\n{r['text']}" if isinstance(r.get("text"), str) else f"[资源 {uri}（二进制，已省略）]")
        elif t == "resource_link":
            parts.append(f"[资源链接 {c.get('name') or ''} {c.get('uri', '')}]".replace("  ", " "))
        else:
            parts.append(f"[未识别的内容类型 {t}]")
    text = "\n".join(p for p in parts if p).strip()
    if not text and res.get("structuredContent") is not None:
        text = json.dumps(res["structuredContent"], ensure_ascii=False)[:20000]
    text = sanitize_untrusted(text) or "(无输出)"
    return ToolResult(text, error=bool(res.get("isError")), images=images, meta={"mcp_server": server, "mcp_tool": tool},
                      taint="" if trusted else f"MCP 服务器 {server} 返回的内容不可信",
                      untrusted_source="" if trusted else f"mcp:{server}/{tool}")
