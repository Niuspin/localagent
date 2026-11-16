"""会话多格式导出（M4 §4.2）：Markdown / 自包含 HTML / CJK PDF。"""
from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from ...artifacts import Artifact
from ...parts import normalize_part, part_summary

EXPORT_FORMATS = ("md", "html", "pdf")


def _safe_filename(title: str, sid: str, ext: str) -> str:
    slug = re.sub(r"[^\w.\-\u4e00-\u9fff ]", "_", (title or f"session-{sid}")).strip(" .")[:48] or f"session-{sid}"
    return f"{slug}.{ext}"


def _render_user_md(m: dict[str, Any]) -> str:
    chunks: list[str] = ["## 用户", ""]
    if m.get("text"):
        chunks.append(str(m["text"]))
        chunks.append("")
    if m.get("files"):
        chunks.append("附件: " + ", ".join(f"`{f}`" for f in m["files"]))
        chunks.append("")
    if m.get("images"):
        chunks.append(f"[附带图片 × {len(m['images'])}]")
        chunks.append("")
    if not m.get("text") and isinstance(m.get("parts"), list):
        for p in m["parts"]:
            chunks.append(part_summary(normalize_part(p)))
        chunks.append("")
    return "\n".join(chunks)


def _render_assistant_md(m: dict[str, Any], *, include_thinking: bool, include_tools: bool) -> str:
    chunks: list[str] = ["## 助手", ""]
    blocks = m.get("blocks") or []
    for b in blocks:
        bt = b.get("type")
        if bt == "thinking":
            if include_thinking and b.get("text"):
                txt = str(b["text"]).strip()
                chunks.append("<details><summary>思考过程</summary>\n\n" + txt + "\n\n</details>\n")
        elif bt == "text":
            if b.get("text"):
                chunks.append(str(b["text"]))
                chunks.append("")
        elif bt == "tool":
            if include_tools:
                title = b.get("title") or b.get("name") or "工具"
                status = b.get("status") or ""
                chunks.append(f"> 工具: `{title}` — {status}")
                if b.get("content"):
                    preview = str(b["content"]).strip()[:600]
                    chunks.append(">\n> " + preview.replace("\n", "\n> "))
                chunks.append("")
            meta = b.get("meta") or {}
            art = meta.get("artifact")
            if isinstance(art, dict) and art.get("title"):
                chunks.append(f"- 产物: **{art['title']}** (`{art.get('kind', 'file')}`, id=`{art.get('id', '')}`)")
    if not blocks and isinstance(m.get("parts"), list):
        for p in m["parts"]:
            chunks.append(part_summary(normalize_part(p)))
        chunks.append("")
    if m.get("error"):
        chunks.append(f"> 错误: {m['error']}\n")
    return "\n".join(chunks)


def export_session_markdown(
    sess: Any,
    *,
    include_thinking: bool = True,
    include_tools: bool = True,
    artifacts: list[Artifact] | None = None,
) -> str:
    title = getattr(sess, "title", None) or "LocalAgent 会话"
    sid = getattr(sess, "id", "")
    lines = [f"# {title}", ""]
    if getattr(sess, "workspace", ""):
        lines.append(f"- 工作区: `{sess.workspace}`")
    if sid:
        lines.append(f"- 会话 ID: `{sid}`")
    lines.append("")

    for m in getattr(sess, "ui", []) or []:
        if m.get("role") == "user":
            lines.append(_render_user_md(m))
        else:
            lines.append(_render_assistant_md(m, include_thinking=include_thinking, include_tools=include_tools))

    if artifacts:
        lines.append("## 会话产物清单")
        lines.append("")
        lines.append("| ID | 类型 | 标题 | 大小 | MIME |")
        lines.append("|---|---|---|---|---|")
        for a in artifacts:
            lines.append(f"| `{a.id}` | {a.kind} | {a.title} | {a.size} B | `{a.mime}` |")
        lines.append("")
    return "\n".join(lines)


def _html_blocks_for_message(m: dict[str, Any], *, include_thinking: bool, include_tools: bool) -> str:
    if m.get("role") == "user":
        parts = ['<section class="turn user"><div class="role">用户</div>']
        if m.get("text"):
            parts.append(f'<div class="body">{escape(str(m["text"]))}</div>')
        if m.get("files"):
            chips = "".join(f'<span class="chip">{escape(str(f))}</span>' for f in m["files"])
            parts.append(f'<div class="chips">{chips}</div>')
        parts.append("</section>")
        return "\n".join(parts)

    parts = ['<section class="turn assistant"><div class="role">助手</div>']
    for b in m.get("blocks") or []:
        bt = b.get("type")
        if bt == "thinking":
            if include_thinking and b.get("text"):
                parts.append(
                    f'<details class="think"><summary>思考过程</summary><pre>{escape(str(b["text"]))}</pre></details>'
                )
        elif bt == "text":
            if b.get("text"):
                parts.append(f'<div class="body">{escape(str(b["text"]))}</div>')
        elif bt == "tool":
            if include_tools:
                args_txt = json.dumps(b.get("args") or {}, ensure_ascii=False, indent=2) if b.get("args") else ""
                body_txt = str(b.get("content") or "")[:2000]
                parts.append(
                    f'<details class="tool"><summary>{escape(str(b.get("title") or b.get("name") or "工具"))} '
                    f'<span class="badge">{escape(str(b.get("status") or ""))}</span></summary>'
                    + (f'<pre>{escape(args_txt)}</pre>' if args_txt else "")
                    + (f'<pre>{escape(body_txt)}</pre>' if body_txt else "")
                    + "</details>"
                )
            art = (b.get("meta") or {}).get("artifact")
            if isinstance(art, dict) and art.get("title"):
                parts.append(
                    f'<div class="art-ref">产物: <b>{escape(str(art["title"]))}</b> '
                    f'<span class="badge">{escape(str(art.get("kind", "file")))}</span></div>'
                )
    if m.get("error"):
        parts.append(f'<div class="err">{escape(str(m["error"]))}</div>')
    parts.append("</section>")
    return "\n".join(parts)


def export_session_html(
    sess: Any,
    *,
    include_thinking: bool = True,
    include_tools: bool = True,
    artifacts: list[Artifact] | None = None,
) -> str:
    title = escape(str(getattr(sess, "title", None) or "LocalAgent 会话"))
    sid = escape(str(getattr(sess, "id", "")))
    ws = escape(str(getattr(sess, "workspace", "") or ""))
    turns = "\n".join(
        _html_blocks_for_message(m, include_thinking=include_thinking, include_tools=include_tools)
        for m in (getattr(sess, "ui", []) or [])
    )
    art_rows = ""
    if artifacts:
        trs = "".join(
            f"<tr><td><code>{escape(a.id)}</code></td><td>{escape(a.kind)}</td>"
            f"<td>{escape(a.title)}</td><td>{a.size} B</td><td>{escape(a.mime)}</td></tr>"
            for a in artifacts
        )
        art_rows = (
            '<section class="artifacts"><h2>会话产物</h2>'
            "<table><thead><tr><th>ID</th><th>类型</th><th>标题</th><th>大小</th><th>MIME</th></tr></thead>"
            f"<tbody>{trs}</tbody></table></section>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · LocalAgent 导出</title>
<style>
:root {{ --bg: #fcfbf9; --fg: #1f242c; --muted: #5c626e; --card: #f6f3ec; --border: #e3ddd1; --accent: #ac4e2f; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #14171d; --fg: #e6e9ef; --muted: #a1aab8; --card: #1c2029; --border: #2c3342; --accent: #e07a5f; }}
}}
body {{ margin: 0; padding: 32px 16px; background: var(--bg); color: var(--fg); font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; line-height: 1.7; }}
main {{ max-width: 860px; margin: 0 auto; }}
header {{ border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 22px; }}
.meta {{ color: var(--muted); font-size: 0.9em; }}
.turn {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; }}
.turn.user {{ border-left: 3px solid var(--accent); }}
.role {{ font-weight: 700; font-size: 0.85em; color: var(--muted); margin-bottom: 6px; }}
.body {{ white-space: pre-wrap; word-break: break-word; }}
details {{ margin: 8px 0; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: var(--bg); }}
summary {{ cursor: pointer; font-weight: 600; font-size: 0.88em; color: var(--muted); }}
pre {{ overflow-x: auto; font-size: 0.84em; white-space: pre-wrap; word-break: break-word; }}
.badge {{ display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 0.78em; background: var(--card); border: 1px solid var(--border); }}
.chip {{ display: inline-block; margin-right: 6px; padding: 2px 8px; border-radius: 6px; border: 1px solid var(--border); font-size: 0.84em; }}
.art-ref {{ margin-top: 8px; padding: 6px 10px; border-radius: 8px; background: var(--bg); border: 1px solid var(--border); font-size: 0.88em; }}
.err {{ color: #dc2626; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.88em; }}
th, td {{ border: 1px solid var(--border); padding: 6px 8px; text-align: left; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{title}</h1>
    <div class="meta">会话 ID: <code>{sid}</code>{f" · 工作区: <code>{ws}</code>" if ws else ""}</div>
  </header>
  {turns}
  {art_rows}
</main>
</body>
</html>"""


def export_session(
    sess: Any,
    fmt: str = "md",
    *,
    include_thinking: bool = True,
    include_tools: bool = True,
    artifacts: list[Artifact] | None = None,
) -> tuple[bytes, str, str]:
    """按 md / html / pdf 导出会话，返回 `(content_bytes, mime_type, filename)`。"""
    f = (fmt or "md").strip().lower()
    if f == "markdown":
        f = "md"
    if f not in EXPORT_FORMATS:
        raise ValueError(f"不支持的导出格式 {fmt!r}，可选: {', '.join(EXPORT_FORMATS)}")
    sid = str(getattr(sess, "id", "") or "session")
    title = str(getattr(sess, "title", None) or f"session-{sid}")

    if f == "md":
        md_txt = export_session_markdown(sess, include_thinking=include_thinking, include_tools=include_tools, artifacts=artifacts)
        return md_txt.encode("utf-8"), "text/markdown; charset=utf-8", _safe_filename(title, sid, "md")

    if f == "html":
        html_txt = export_session_html(sess, include_thinking=include_thinking, include_tools=include_tools, artifacts=artifacts)
        return html_txt.encode("utf-8"), "text/html; charset=utf-8", _safe_filename(title, sid, "html")

    # PDF（含中文字体）
    md_txt = export_session_markdown(sess, include_thinking=include_thinking, include_tools=include_tools, artifacts=artifacts)
    from ...tools.office import build_pdf

    try:
        pdf_bytes = build_pdf(md_txt, title=title)
    except ImportError as e:
        raise RuntimeError(f"导出 PDF 需要安装 {e.name or 'reportlab'}: pip install reportlab") from e
    return pdf_bytes, "application/pdf", _safe_filename(title, sid, "pdf")
