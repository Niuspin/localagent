"""安全多格式 Artifact 预览生成器（M4 §4.2–4.4）。

原则：
- SVG 预览剥离 <script> / on* 事件 / <foreignObject> / 外部 href，防 XSS 与外链请求；
- HTML 产物默认禁止脚本，仅在用户逐产物显式授权后允许 sandbox=\"allow-scripts\"（但仍无网络且无同源权限）；
- PDF 按页渲染 PNG + 文本回退；表格支持分页抽样预览；Zip 打包安全列出 MANIFEST 与成员目录。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from ...artifacts import Artifact, ArtifactStore
from ...tools.files import decode_bytes

_DANGEROUS_SVG_TAGS = re.compile(r"<\s*(?:script|foreignObject|iframe|object|embed)\b[^>]*>.*?<\s*/\s*(?:script|foreignObject|iframe|object|embed)\s*>", re.I | re.S)
_DANGEROUS_SVG_SELF = re.compile(r"<\s*(?:script|foreignObject|iframe|object|embed)\b[^>]*/?\s*>", re.I)
_EVENT_ATTR = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_BAD_URI_ATTR = re.compile(r"""\s+(?:xlink:)?href\s*=\s*(?:"(?:javascript:|vbscript:|https?:|//)[^"]*"|'(?:javascript:|vbscript:|https?:|//)[^']*')""", re.I)
_BAD_SMIL_ATTR = re.compile(r"""\s+attributeName\s*=\s*(?:"(?:xlink:)?href"|'(?:xlink:)?href')""", re.I)
_BAD_CSS_URL = re.compile(r"""url\(\s*['"]?(?:https?:|//|javascript:)[^)]*\)""", re.I)


def sanitize_svg_for_preview(svg_text: str) -> str:
    """移除 SVG 中的可执行脚本与外部引用，确保在宿主页面中预览安全。"""
    s = _DANGEROUS_SVG_TAGS.sub("", svg_text or "")
    s = _DANGEROUS_SVG_SELF.sub("", s)
    s = _EVENT_ATTR.sub("", s)
    s = _BAD_URI_ATTR.sub("", s)
    s = _BAD_SMIL_ATTR.sub("", s)
    s = _BAD_CSS_URL.sub("none", s)
    return s


def html_preview_csp(scripts_authorized: bool) -> str:
    """构造隔离 iframe 预览的严格 CSP（无论是否授权脚本均完全禁止网络与外域资源）。"""
    if scripts_authorized:
        return (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; connect-src 'none'; form-action 'none'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
        )
    return (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
    )


def _table_from_csv(raw: bytes, *, offset: int, limit: int, sep: str = ",") -> dict[str, Any]:
    text, _, _ = decode_bytes(raw[:2_000_000])
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = list(csv.reader(io.StringIO(text), delimiter=sep))
    headers = [str(c) for c in rows[0]] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []
    total = len(data_rows)
    sliced = data_rows[offset : offset + limit]
    return {
        "preview_kind": "table",
        "sheets": ["Sheet1"],
        "active_sheet": 0,
        "headers": headers,
        "rows": sliced,
        "offset": offset,
        "limit": limit,
        "total_rows": total,
        "truncated": (offset + len(sliced)) < total or len(raw) > 2_000_000,
    }


def _table_from_xlsx(path: Path, *, sheet_idx: int, offset: int, limit: int) -> dict[str, Any]:
    import openpyxl  # type: ignore

    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    try:
        names = list(wb.sheetnames)
        if not names:
            return {"preview_kind": "table", "sheets": [], "active_sheet": 0, "headers": [], "rows": [], "offset": 0, "limit": limit, "total_rows": 0, "truncated": False}
        sidx = max(0, min(len(names) - 1, int(sheet_idx)))
        ws = wb[names[sidx]]
        all_rows: list[list[str]] = []
        cap_rows = max(offset + limit + 2, 500)
        for idx, row in enumerate(ws.iter_rows(values_only=True)):
            if idx >= cap_rows:
                break
            all_rows.append(["" if v is None else str(v) for v in row[:40]])
        headers = all_rows[0] if all_rows else []
        data_rows = all_rows[1:] if len(all_rows) > 1 else []
        total = max(len(data_rows), (ws.max_row - 1) if isinstance(ws.max_row, int) and ws.max_row > 0 else len(data_rows))
        sliced = data_rows[offset : offset + limit]
        return {
            "preview_kind": "table",
            "sheets": names,
            "active_sheet": sidx,
            "headers": headers,
            "rows": sliced,
            "offset": offset,
            "limit": limit,
            "total_rows": total,
            "truncated": (offset + len(sliced)) < total,
        }
    finally:
        wb.close()


def _pdf_preview(path: Path, *, page: int) -> dict[str, Any]:
    page_num = max(1, int(page or 1))
    page_count = 0
    image_url = None
    text_out = ""

    try:
        import pypdfium2 as pdfium  # type: ignore

        doc = pdfium.PdfDocument(str(path))
        try:
            page_count = len(doc)
            if page_count > 0:
                idx = min(page_count - 1, page_num - 1)
                page_num = idx + 1
                pg = doc[idx]
                bm = pg.render(scale=1.5)
                pil_img = bm.to_pil()
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                image_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        finally:
            doc.close()
    except Exception:
        pass

    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(path)) as pdf:
            page_count = page_count or len(pdf.pages)
            if pdf.pages:
                idx = min(len(pdf.pages) - 1, page_num - 1)
                page_num = idx + 1
                text_out = pdf.pages[idx].extract_text() or ""
    except Exception:
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            page_count = page_count or len(reader.pages)
            if reader.pages:
                idx = min(len(reader.pages) - 1, page_num - 1)
                page_num = idx + 1
                text_out = reader.pages[idx].extract_text() or ""
        except Exception:
            pass

    return {
        "preview_kind": "pdf",
        "page": page_num,
        "page_count": page_count,
        "data_url": image_url,
        "text": text_out[:30_000],
    }


def _pptx_preview(path: Path) -> dict[str, Any]:
    from pptx import Presentation  # type: ignore

    prs = Presentation(str(path))
    slides: list[dict[str, Any]] = []
    for idx, sl in enumerate(prs.slides, 1):
        stitle = ""
        bullets: list[str] = []
        if sl.shapes.title is not None:
            stitle = (sl.shapes.title.text or "").strip()
        for sh in sl.shapes:
            if sh is sl.shapes.title:
                continue
            if getattr(sh, "has_text_frame", False):
                for p in sh.text_frame.paragraphs:
                    t = (p.text or "").strip()
                    if t:
                        bullets.append(t)
        notes = ""
        try:
            if sl.has_notes_slide and sl.notes_slide.notes_text_frame:
                notes = (sl.notes_slide.notes_text_frame.text or "").strip()
        except Exception:
            pass
        slides.append({"index": idx, "title": stitle or f"Slide {idx}", "bullets": bullets[:20], "notes": notes[:1000]})
    return {"preview_kind": "slides", "slides": slides, "slide_count": len(slides)}


def _archive_preview(path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    manifest: Any = None
    with zipfile.ZipFile(str(path), "r") as zf:
        for info in zf.infolist()[:300]:
            entries.append({"name": info.filename, "size": info.file_size, "compressed": info.compress_size})
        if "MANIFEST.json" in zf.namelist():
            info_m = zf.getinfo("MANIFEST.json")
            if info_m.file_size <= 512_000:
                try:
                    manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8", "replace"))
                except ValueError:
                    manifest = None
    return {"preview_kind": "archive", "entries": entries, "manifest": manifest}


def build_artifact_preview(
    store: ArtifactStore,
    art: Artifact,
    *,
    page: int = 1,
    sheet: int = 0,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """根据产物 MIME / 扩展名构建安全的预览描述对象。"""
    p = store.resolve_path(art)
    if not p or not p.is_file():
        raise FileNotFoundError("产物文件缺失")
    ext = p.suffix.lower()
    mime = (art.mime or "").lower()
    off = max(0, int(offset or 0))
    lim = max(1, min(300, int(limit or 100)))

    base_info: dict[str, Any] = {
        "artifact": art.to_dict(),
        "download_url": f"/api/artifacts/{art.id}",
    }

    # 1. SVG / Chart
    if mime == "image/svg+xml" or ext == ".svg" or art.kind == "chart":
        raw_svg = p.read_text("utf-8", errors="replace")[:512_000]
        clean_svg = sanitize_svg_for_preview(raw_svg)
        b64 = base64.b64encode(clean_svg.encode("utf-8")).decode("ascii")
        return {
            **base_info,
            "preview_kind": "svg",
            "svg": clean_svg,
            "data_url": f"data:image/svg+xml;base64,{b64}",
            "source_preview": clean_svg[:50_000],
        }

    # 2. Image
    if mime.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        raw = p.read_bytes()
        data_url = f"data:{mime or 'image/png'};base64," + base64.b64encode(raw).decode("ascii") if len(raw) <= 6_000_000 else ""
        return {**base_info, "preview_kind": "image", "data_url": data_url, "too_large": len(raw) > 6_000_000}

    # 3. HTML (sandboxed by default, scripts disabled unless authorized per-artifact)
    if mime == "text/html" or ext in (".html", ".htm") or art.kind == "html":
        scripts_auth = bool((art.meta or {}).get("scripts_authorized", False))
        raw_txt = p.read_text("utf-8", errors="replace")
        return {
            **base_info,
            "preview_kind": "html",
            "iframe_url": f"/api/artifacts/{art.id}/preview?raw=1",
            "sandbox": "allow-scripts" if scripts_auth else "",
            "scripts_authorized": scripts_auth,
            "network_isolated": True,
            "source_preview": raw_txt[:40_000],
            "truncated": len(raw_txt) > 40_000,
        }

    # 4. PDF
    if mime == "application/pdf" or ext == ".pdf":
        return {**base_info, **_pdf_preview(p, page=page)}

    # 5. Spreadsheet / CSV / TSV
    if ext in (".csv", ".tsv") or mime in ("text/csv", "text/tab-separated-values"):
        sep = "\t" if (ext == ".tsv" or mime == "text/tab-separated-values") else ","
        return {**base_info, **_table_from_csv(p.read_bytes(), offset=off, limit=lim, sep=sep)}
    if ext in (".xlsx", ".xlsm") or "spreadsheetml" in mime:
        try:
            return {**base_info, **_table_from_xlsx(p, sheet_idx=sheet, offset=off, limit=lim)}
        except ImportError:
            return {**base_info, "preview_kind": "binary", "note": "缺少 openpyxl，无法预览工作簿"}

    # 6. Word / PowerPoint
    if ext == ".docx" or "wordprocessingml" in mime:
        from ...tools.office import read_document

        txt = read_document(p, 1, None, 60_000)
        return {**base_info, "preview_kind": "markdown", "content": txt, "is_markdown": True, "truncated": len(txt) >= 60_000}
    if ext == ".pptx" or "presentationml" in mime:
        try:
            return {**base_info, **_pptx_preview(p)}
        except ImportError:
            return {**base_info, "preview_kind": "binary", "note": "缺少 python-pptx，无法预览演示文稿"}

    # 7. Bundle / Zip
    if mime == "application/zip" or ext == ".zip" or art.kind == "bundle":
        try:
            return {**base_info, **_archive_preview(p)}
        except Exception as e:
            return {**base_info, "preview_kind": "binary", "note": f"压缩包解析失败: {e}"}

    # 8. Audio
    if mime.startswith("audio/") or ext in (".wav", ".mp3", ".ogg", ".m4a", ".flac"):
        return {**base_info, "preview_kind": "audio", "audio_url": f"/api/artifacts/{art.id}/preview?raw=1"}

    # 9. Markdown / Text / Code
    raw = p.read_bytes()[:200_000]
    if b"\x00" not in raw[:8192]:
        txt, _, _ = decode_bytes(raw)
        is_md = ext in (".md", ".markdown") or mime == "text/markdown" or art.kind in ("markdown", "report")
        lang = ext.lstrip(".") or "txt"
        return {
            **base_info,
            "preview_kind": "markdown" if is_md else "text",
            "content": txt,
            "is_markdown": is_md,
            "lang": lang,
            "line_count": txt.count("\n") + (1 if txt else 0),
            "truncated": art.size > 200_000,
        }

    return {**base_info, "preview_kind": "binary", "note": "二进制文件，请直接下载或打开"}
