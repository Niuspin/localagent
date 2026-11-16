"""多模态 Content Parts 规范化与上下文降级（M0 / Session Schema v2）。

统一前端 UI 与后端 History 的内容分片结构：
支持 `text | image | file | table | chart | audio | artifact_ref`，
并负责将任意 `parts` 无损或安全降级为 llama-server 兼容的 OpenAI `content`。
"""
from __future__ import annotations

import json
from typing import Any

PART_TYPES: tuple[str, ...] = (
    "text",
    "image",
    "file",
    "table",
    "chart",
    "audio",
    "artifact_ref",
)


def _safe_str(v: Any, limit: int = 0) -> str:
    s = "" if v is None else str(v)
    return s[:limit] if limit > 0 else s


def part_summary(part: dict[str, Any], *, omitted_image_text: str = "[图片已省略]") -> str:
    """将单个非文本（或需降级的）part 渲染成给模型/摘要用的紧凑文本占位。"""
    t = part.get("type")
    if t == "text":
        return _safe_str(part.get("text"))
    if t == "image":
        alt = _safe_str(part.get("alt") or part.get("title"), 80)
        aid = _safe_str(part.get("artifact_id"), 48)
        if aid and alt:
            return f"{omitted_image_text}（{alt}，artifact={aid}）"
        if aid:
            return f"{omitted_image_text}（artifact={aid}）"
        return f"{omitted_image_text}（{alt}）" if alt else omitted_image_text
    if t == "file":
        name = _safe_str(part.get("name") or part.get("path") or part.get("title") or "未命名文件", 120)
        mime = _safe_str(part.get("mime"), 64)
        aid = _safe_str(part.get("artifact_id"), 48)
        extras = [x for x in (mime, f"artifact={aid}" if aid else "") if x]
        return f"[文件: {name}" + (f" ({', '.join(extras)})" if extras else "") + "]"
    if t == "table":
        title = _safe_str(part.get("title") or "数据表", 100)
        summary = _safe_str(part.get("summary"), 300)
        rows = part.get("row_count")
        aid = _safe_str(part.get("artifact_id"), 48)
        hid = _safe_str(part.get("handle_id"), 48)
        meta = [x for x in (f"{rows} 行" if isinstance(rows, int) else "", f"artifact={aid}" if aid else "", f"handle={hid}" if hid else "") if x]
        head = f"[表格: {title}" + (f" ({', '.join(meta)})" if meta else "") + "]"
        return f"{head} {summary}".strip()
    if t == "chart":
        title = _safe_str(part.get("title") or "图表", 100)
        summary = _safe_str(part.get("summary"), 300)
        aid = _safe_str(part.get("artifact_id"), 48)
        head = f"[图表: {title}" + (f" (artifact={aid})" if aid else "") + "]"
        return f"{head} {summary}".strip()
    if t == "audio":
        transcript = _safe_str(part.get("transcript"), 300)
        aid = _safe_str(part.get("artifact_id"), 48)
        dur = part.get("duration_s")
        meta = [x for x in (f"{dur:.1f}s" if isinstance(dur, (int, float)) else "", f"artifact={aid}" if aid else "") if x]
        head = "[音频" + (f" ({', '.join(meta)})" if meta else "") + "]"
        return f"{head} 转写: {transcript}" if transcript else head
    if t == "artifact_ref":
        title = _safe_str(part.get("title") or part.get("kind") or "产物", 100)
        aid = _safe_str(part.get("artifact_id"), 48)
        summary = _safe_str(part.get("summary"), 300)
        head = f"[产物引用: {title}" + (f" id={aid}" if aid else "") + "]"
        return f"{head} {summary}".strip()
    return _safe_str(part.get("text") or part.get("summary") or "", 300)


def normalize_part(part: Any) -> dict[str, Any]:
    """将各种历史或简写分片标准化为 Schema v2 Part。"""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return {"type": "text", "text": _safe_str(part)}
    ptype = part.get("type")
    if ptype == "image_url":
        img = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
        out = {"type": "image", "url": _safe_str(img.get("url") or part.get("url"))}
        if part.get("alt"):
            out["alt"] = _safe_str(part["alt"], 160)
        if part.get("artifact_id"):
            out["artifact_id"] = _safe_str(part["artifact_id"], 64)
        return out
    if ptype in PART_TYPES:
        out = dict(part)
        if ptype == "text":
            out["text"] = _safe_str(out.get("text"))
        return out
    if part.get("artifact_id"):
        return {
            "type": "artifact_ref",
            "artifact_id": _safe_str(part["artifact_id"], 64),
            "kind": _safe_str(part.get("kind") or "file", 32),
            "title": _safe_str(part.get("title") or part.get("name"), 160),
            "mime": _safe_str(part.get("mime"), 128),
            "summary": _safe_str(part.get("summary"), 500),
        }
    return {"type": "text", "text": _safe_str(part.get("text") or part.get("summary") or json.dumps(part, ensure_ascii=False), 2000)}


def content_to_parts(
    content: Any,
    *,
    images: list[str] | None = None,
    files: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """把历史字符串/OpenAI list 或 UI `{text, images, files}` 转为标准 parts 列表。"""
    parts: list[dict[str, Any]] = []
    if isinstance(content, list):
        for p in content:
            parts.append(normalize_part(p))
    elif content is not None and content != "":
        parts.append({"type": "text", "text": str(content)})
    elif content == "" and not images and not files:
        parts.append({"type": "text", "text": ""})

    for img in images or []:
        if isinstance(img, str) and img:
            parts.append({"type": "image", "url": img})
        elif isinstance(img, dict):
            parts.append(normalize_part({"type": "image", **img}))

    for f in files or []:
        if isinstance(f, str) and f:
            parts.append({"type": "file", "name": f, "path": f})
        elif isinstance(f, dict):
            parts.append(normalize_part({"type": "file", **f}))

    return parts


def parts_to_content(
    parts: list[dict[str, Any]] | None,
    *,
    vision: bool = True,
    omit_images: bool = False,
) -> Any:
    """将标准 parts 投影为 OpenAI Chat Completions 兼容的 `content`（str 或 text/image_url 列表）。"""
    if not parts:
        return ""
    norm = [normalize_part(p) for p in parts]
    has_live_image = vision and not omit_images and any(p.get("type") == "image" and p.get("url") for p in norm)
    if not has_live_image:
        texts: list[str] = []
        for p in norm:
            if p.get("type") == "text":
                if p.get("text") is not None:
                    texts.append(str(p.get("text")))
            elif p.get("type") == "image":
                texts.append(part_summary(p, omitted_image_text="[图片已省略]" if omit_images else "[图片]"))
            else:
                texts.append(part_summary(p))
        return "\n".join(t for t in texts if t != "") if any(t != "" for t in texts) else (texts[0] if texts else "")

    out: list[dict[str, Any]] = []
    for p in norm:
        if p.get("type") == "text":
            out.append({"type": "text", "text": str(p.get("text") or "")})
        elif p.get("type") == "image" and p.get("url"):
            out.append({"type": "image_url", "image_url": {"url": str(p["url"])}})
        else:
            out.append({"type": "text", "text": part_summary(p)})
    return out
