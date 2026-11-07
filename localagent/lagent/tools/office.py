"""办公格式：读取 docx/xlsx/pptx/pdf；创建 docx/pdf/xlsx/csv/pptx；就地编辑（文本替换、单元格）。

依赖均为可选：缺失时给出明确的安装提示，不影响其它功能。
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from ..security import SecurityError
from .base import P, Tool, ToolResult, clip, obj, register
from .ctx import ToolCtx
from .files import _ensure_parent, commit_bytes, err

_CJK_FONT = "Microsoft YaHei"


def _need(mod: str, pip: str) -> ToolResult:
    return err(f"缺少依赖 {mod}，请运行: pip install {pip}")


# ====================================================================== 读取
def read_document(p: Path, offset: Any = 1, limit: Any = None, max_chars: int = 40000) -> str:
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            text = _read_docx(p)
        elif ext in (".xlsx", ".xlsm"):
            text = _read_xlsx(p)
        elif ext == ".pptx":
            text = _read_pptx(p)
        elif ext == ".pdf":
            text = _read_pdf(p)
        else:
            return f"不支持的文档类型: {ext}"
    except ImportError as e:
        return f"缺少依赖 {e.name}，请 pip install {_PIP.get(e.name or '', e.name)}"
    except Exception as e:  # noqa: BLE001
        return f"无法解析 {p.name}: {type(e).__name__}: {e}"
    lines = text.split("\n")
    off = max(1, int(offset or 1))
    sel = lines[off - 1:]
    if limit:
        sel = sel[: int(limit)]
    out = "\n".join(sel)
    if len(out) > max_chars:
        cut = out[:max_chars]
        used = cut.count("\n") + 1
        out = cut + f"\n…[已截断；用 offset={off + used} 继续读取，共 {len(lines)} 行]"
    return f"[{p.name} · 提取文本共 {len(lines)} 行]\n{out}"


_PIP = {"docx": "python-docx", "openpyxl": "openpyxl", "pptx": "python-pptx", "pdfplumber": "pdfplumber", "pypdf": "pypdf",
        "reportlab": "reportlab", "fitz": "pymupdf"}


def _read_docx(p: Path) -> str:
    import docx  # type: ignore
    from docx.oxml.ns import qn  # type: ignore
    d = docx.Document(str(p))
    out: list[str] = []
    body = d.element.body
    paras = {id(x._element): x for x in d.paragraphs}
    tables = {id(t._element): t for t in d.tables}
    for el in body.iterchildren():
        if el.tag == qn("w:p"):
            para = next((x for k, x in paras.items() if x._element is el), None)
            if para is None:
                continue
            t = para.text.strip()
            if not t:
                continue
            st = (para.style.name or "") if para.style is not None else ""
            m = re.match(r"(?:Heading|标题)\s*(\d)", st)
            if m:
                out.append("#" * int(m.group(1)) + " " + t)
            elif st == "Title":
                out.append("# " + t)
            elif "List" in st or "列表" in st:
                out.append("- " + t)
            else:
                out.append(t)
        elif el.tag == qn("w:tbl"):
            tb = next((x for x in tables.values() if x._element is el), None)
            if tb is not None:
                rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in tb.rows]
                out.extend(_md_table(rows))
    return "\n".join(out)


def _md_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * w]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def _read_xlsx(p: Path) -> str:
    import openpyxl  # type: ignore
    wb_v = openpyxl.load_workbook(str(p), data_only=True, read_only=False)
    wb_f = openpyxl.load_workbook(str(p), data_only=False, read_only=False)
    out = []
    for ws in wb_v.worksheets:
        wf = wb_f[ws.title]
        out.append(f"## 工作表: {ws.title}（{ws.max_row} 行 × {ws.max_column} 列）")
        nform = 0
        for r_idx, (rv, rf) in enumerate(zip(ws.iter_rows(max_row=300, max_col=40), wf.iter_rows(max_row=300, max_col=40)), 1):
            cells = []
            for cv, cf in zip(rv, rf):
                v = cv.value
                f = cf.value
                if isinstance(f, str) and f.startswith("="):
                    nform += 1
                    cells.append(f"{'' if v is None else v} ⟨{f}⟩")
                else:
                    cells.append("" if v is None else str(v).replace("\n", "\\n"))
            while cells and cells[-1] == "":
                cells.pop()
            if cells:
                out.append(f"{r_idx}: " + " | ".join(cells))
        if ws.max_row > 300:
            out.append(f"…[仅显示前 300 行，共 {ws.max_row} 行]")
    return "\n".join(out)


def _read_pptx(p: Path) -> str:
    from pptx import Presentation  # type: ignore
    prs = Presentation(str(p))
    out = []
    for i, s in enumerate(prs.slides, 1):
        out.append(f"## 幻灯片 {i}")
        for sh in s.shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        out.append(("  " * para.level) + "- " + t)
            if getattr(sh, "has_table", False) and sh.has_table:
                out.extend(_md_table([[c.text.strip() for c in r.cells] for r in sh.table.rows]))
        if s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip():
            out.append("备注: " + s.notes_slide.notes_text_frame.text.strip())
    return "\n".join(out)


def _read_pdf(p: Path) -> str:
    try:
        import pdfplumber  # type: ignore
        out = []
        with pdfplumber.open(str(p)) as pdf:
            n = len(pdf.pages)
            for i, pg in enumerate(pdf.pages[:200], 1):
                out.append(f"--- 第 {i}/{n} 页 ---")
                out.append((pg.extract_text() or "").strip())
        text = "\n".join(out)
    except ImportError:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(str(p))
        text = "\n".join(f"--- 第 {i}/{len(r.pages)} 页 ---\n{(pg.extract_text() or '').strip()}" for i, pg in enumerate(r.pages[:200], 1))
    npages = max(1, text.count("--- 第 "))
    if len(re.sub(r"---.*?---|\s", "", text)) < 8 * npages:
        text += "\n[提示：几乎没有可提取文字，可能是扫描件。需要 OCR，或把页面转成图片后用多模态模型查看]"
    return text


# ====================================================================== 写：Markdown → docx / pdf
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`)")


def _blocks(md: str) -> list[tuple]:
    """极简 Markdown → 块列表：(kind, payload)。"""
    lines = md.replace("\r\n", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append(("code", "\n".join(lines[i + 1:j])))
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            out.append(("h", (len(m.group(1)), m.group(2))))
        elif re.match(r"^[-*+]\s+", s):
            out.append(("ul", re.sub(r"^[-*+]\s+", "", s)))
        elif re.match(r"^\d+[.)]\s+", s):
            out.append(("ol", re.sub(r"^\d+[.)]\s+", "", s)))
        elif s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            rows = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                if not re.match(r"^\|?\s*:?-{2,}", lines[j].strip()):
                    rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            out.append(("table", rows))
            i = j
            continue
        elif re.match(r"^(-{3,}|\*{3,})$", s):
            out.append(("hr", None))
        elif s.startswith(">"):
            out.append(("quote", s.lstrip("> ")))
        else:
            out.append(("p", s))
        i += 1
    return out


def _docx_runs(par: Any, text: str) -> None:
    from docx.shared import Pt  # type: ignore
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = par.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            r = par.add_run(part[1:-1])
            r.italic = True
        else:
            r = par.add_run(part)
        _set_cjk(r)


def _set_cjk(run: Any) -> None:
    from docx.oxml.ns import qn  # type: ignore
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        from docx.oxml import OxmlElement  # type: ignore
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:eastAsia"), _CJK_FONT)


def build_docx(md: str, title: str | None = None) -> bytes:
    import docx  # type: ignore
    from docx.shared import Pt  # type: ignore
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.size = Pt(11)
    st.font.name = "Calibri"
    st.element.rPr.rFonts.set(docx.oxml.ns.qn("w:eastAsia"), _CJK_FONT)
    if title:
        d.add_heading(title, 0)
    for kind, pl in _blocks(md):
        if kind == "h":
            lvl, t = pl
            h = d.add_heading(level=min(lvl, 4))
            _docx_runs(h, t)
        elif kind in ("ul", "ol"):
            par = d.add_paragraph(style="List Bullet" if kind == "ul" else "List Number")
            _docx_runs(par, pl)
        elif kind == "p":
            _docx_runs(d.add_paragraph(), pl)
        elif kind == "quote":
            par = d.add_paragraph(style="Intense Quote") if "Intense Quote" in [s.name for s in d.styles] else d.add_paragraph()
            _docx_runs(par, pl)
        elif kind == "code":
            par = d.add_paragraph()
            r = par.add_run(pl)
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
        elif kind == "table":
            w = max(len(r) for r in pl)
            t = d.add_table(rows=len(pl), cols=w)
            t.style = "Table Grid"
            for ri, row in enumerate(pl):
                for ci in range(w):
                    cell = t.cell(ri, ci)
                    cell.text = ""
                    _docx_runs(cell.paragraphs[0], row[ci] if ci < len(row) else "")
                    if ri == 0:
                        for r in cell.paragraphs[0].runs:
                            r.bold = True
            d.add_paragraph()
        elif kind == "hr":
            d.add_paragraph("─" * 30)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def build_pdf(md: str, title: str | None = None) -> bytes:
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib.styles import ParagraphStyle  # type: ignore
    from reportlab.lib import colors  # type: ignore
    from reportlab.pdfbase import pdfmetrics  # type: ignore
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle  # type: ignore
    from xml.sax.saxutils import escape
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    F = "STSong-Light"
    body = ParagraphStyle("b", fontName=F, fontSize=10.5, leading=17, wordWrap="CJK")
    hs = {i: ParagraphStyle(f"h{i}", parent=body, fontSize=[20, 16, 13.5, 12, 11, 11][i - 1], leading=[28, 24, 20, 18, 16, 16][i - 1],
                            spaceBefore=10, spaceAfter=6) for i in range(1, 7)}
    code = ParagraphStyle("c", fontName="Courier", fontSize=8.5, leading=11, backColor=colors.HexColor("#F3F1EA"))

    def inline(t: str) -> str:
        t = escape(t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
        return re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", t)

    story: list = []
    if title:
        story += [Paragraph(inline(title), hs[1]), Spacer(1, 6)]
    bl = _blocks(md)
    i = 0
    while i < len(bl):
        k, pl = bl[i]
        if k in ("ul", "ol"):
            items = []
            while i < len(bl) and bl[i][0] == k:
                items.append(ListItem(Paragraph(inline(bl[i][1]), body)))
                i += 1
            story.append(ListFlowable(items, bulletType="bullet" if k == "ul" else "1", bulletFontName=F, leftIndent=18))
            continue
        if k == "h":
            story.append(Paragraph(inline(pl[1]), hs[min(pl[0], 6)]))
        elif k == "p":
            story.append(Paragraph(inline(pl), body))
        elif k == "quote":
            story.append(Paragraph(inline(pl), ParagraphStyle("q", parent=body, leftIndent=14, textColor=colors.HexColor("#555555"))))
        elif k == "code":
            story.append(Preformatted(pl, code))
        elif k == "table":
            w = max(len(r) for r in pl)
            data = [[Paragraph(inline(r[c] if c < len(r) else ""), body) for c in range(w)] for r in pl]
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F1EA")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t, Spacer(1, 8)]
        elif k == "hr":
            story.append(Spacer(1, 10))
        i += 1
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=54, rightMargin=54, topMargin=54, bottomMargin=54).build(story or [Spacer(1, 1)])
    return buf.getvalue()


def _check_overwrite(ctx: ToolCtx, p: Path, a: dict) -> ToolResult | None:
    if p.exists() and not a.get("overwrite"):
        return err(f"{ctx.guard.rel(p)} 已存在。若要整体重写请设置 overwrite=true（原文件会保留快照，可撤销）；局部修改请用 edit_office。")
    return None


async def write_document(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    ext = p.suffix.lower()
    if ext not in (".docx", ".pdf"):
        return err("write_document 仅支持 .docx 或 .pdf；纯文本/Markdown 请用 write_file")
    if (e := _check_overwrite(ctx, p, a)):
        return e
    try:
        data = build_docx(a["content"], a.get("title")) if ext == ".docx" else build_pdf(a["content"], a.get("title"))
    except ImportError as e:
        return _need(e.name or "", _PIP.get(e.name or "", e.name or ""))
    _ensure_parent(ctx, p)
    meta = commit_bytes(ctx, p, data, "office", f"{'覆盖' if not meta_new(p) else '创建'} {ctx.guard.rel(p)}")
    return ToolResult(f"已生成 {meta['path']}（{len(data) // 1024 or 1} KB）", meta=meta)


def meta_new(p: Path) -> bool:
    return not p.exists()


register(Tool(
    "write_document", "生成 Word(.docx) 或 PDF 文档。content 用 Markdown：# 标题、列表、**粗体**、表格、```代码```。中文字体已处理。",
    obj({"path": P("string", "输出路径，扩展名 .docx 或 .pdf"), "content": P("string", "Markdown 正文"),
         "title": P("string", "文档大标题（可选）"), "overwrite": P("boolean", "允许覆盖已有文件")}, ["path", "content"]),
    write_document, "write", title=lambda a: f"生成文档 {a.get('path', '')}", path_args=("path",),
    preview=None))


# ====================================================================== 写：xlsx / csv
def _cell_value(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s) and len(s) < 16 and not s.startswith("0") or s == "0":
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    return v


def build_xlsx(sheets: list[dict]) -> bytes:
    import openpyxl  # type: ignore
    from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore
    from openpyxl.utils import get_column_letter  # type: ignore
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used = set()
    for i, sh in enumerate(sheets, 1):
        name = re.sub(r"[\[\]:*?/\\]", "_", str(sh.get("name") or f"Sheet{i}"))[:31] or f"Sheet{i}"
        while name.lower() in used:
            name = (name[:28] + f"_{i}")
        used.add(name.lower())
        ws = wb.create_sheet(name)
        rows = sh.get("rows") or []
        for r in rows:
            ws.append([_cell_value(c) for c in (r if isinstance(r, list) else [r])])
        if rows and sh.get("header", True):
            for c in ws[1]:
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor="F3F1EA")
                c.alignment = Alignment(vertical="center")
            ws.freeze_panes = "A2"
        for ci in range(1, (ws.max_column or 0) + 1):
            width = 8
            for row in ws.iter_rows(min_col=ci, max_col=ci, max_row=min(ws.max_row, 200)):
                v = row[0].value
                if v is not None and not (isinstance(v, str) and v.startswith("=")):
                    s = str(v)
                    width = max(width, sum(2 if ord(ch) > 255 else 1 for ch in s) + 2)
            ws.column_dimensions[get_column_letter(ci)].width = min(width, 60)
    if not wb.worksheets:
        wb.create_sheet("Sheet1")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def write_spreadsheet(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    ext = p.suffix.lower()
    if ext not in (".xlsx", ".csv"):
        return err("write_spreadsheet 仅支持 .xlsx 或 .csv")
    if (e := _check_overwrite(ctx, p, a)):
        return e
    sheets = a["sheets"]
    if not sheets:
        return err("sheets 不能为空")
    try:
        if ext == ".csv":
            buf = io.StringIO()
            csv.writer(buf).writerows([[("" if c is None else c) for c in r] for r in sheets[0].get("rows", [])])
            data = ("﻿" + buf.getvalue()).encode("utf-8")  # 带 BOM，Excel 直接打开不乱码
        else:
            data = build_xlsx(sheets)
    except ImportError as e:
        return _need(e.name or "", _PIP.get(e.name or "", e.name or ""))
    _ensure_parent(ctx, p)
    meta = commit_bytes(ctx, p, data, "office", f"{'创建' if not p.exists() else '覆盖'} {ctx.guard.rel(p)}")
    nrows = sum(len(s.get("rows") or []) for s in sheets)
    return ToolResult(f"已生成 {meta['path']}（{len(sheets)} 个工作表，共 {nrows} 行）。公式以 '=' 开头的字符串写入。", meta=meta)


register(Tool(
    "write_spreadsheet", "生成 Excel(.xlsx) 或 CSV 文件。sheets 是工作表数组，每个含 name 与 rows（二维数组，第一行默认为表头；以 '=' 开头的字符串会作为公式）。",
    obj({"path": P("string", "输出路径 .xlsx/.csv"),
         "sheets": {"type": "array", "description": "工作表列表",
                    "items": obj({"name": P("string", "工作表名"), "rows": {"type": "array", "description": "二维数组", "items": {"type": "array", "items": {}}},
                                  "header": P("boolean", "首行是否表头，默认 true")}, ["rows"])},
         "overwrite": P("boolean", "允许覆盖已有文件")}, ["path", "sheets"]),
    write_spreadsheet, "write", title=lambda a: f"生成表格 {a.get('path', '')}", path_args=("path",)))


# ====================================================================== 写：pptx
def build_pptx(slides: list[dict], title: str | None = None) -> bytes:
    from pptx import Presentation  # type: ignore
    from pptx.util import Inches, Pt  # type: ignore
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    first = True
    for s in slides:
        if first and title and not s.get("title"):
            s = {**s, "title": title}
        layout = prs.slide_layouts[0 if (first and not s.get("bullets")) else 1]
        sl = prs.slides.add_slide(layout)
        first = False
        if sl.shapes.title is not None:
            sl.shapes.title.text = str(s.get("title") or "")
            for para in sl.shapes.title.text_frame.paragraphs:
                for r in para.runs:
                    r.font.size = Pt(36)
        bullets = s.get("bullets") or []
        if len(sl.placeholders) > 1:
            ph = sl.placeholders[1]
            if bullets:
                tf = ph.text_frame
                tf.clear()
                for i, b in enumerate(bullets):
                    lvl = 0
                    t = str(b)
                    while t.startswith(("  ", "\t")):
                        lvl += 1
                        t = t[2:] if t.startswith("  ") else t[1:]
                    para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    para.text = t.lstrip("-*• ").strip()
                    para.level = min(lvl, 4)
                    for r in para.runs:
                        r.font.size = Pt(24 if lvl == 0 else 20)
            else:
                ph._element.getparent().remove(ph._element)
        if s.get("notes"):
            sl.notes_slide.notes_text_frame.text = str(s["notes"])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


async def write_presentation(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if p.suffix.lower() != ".pptx":
        return err("write_presentation 仅支持 .pptx")
    if (e := _check_overwrite(ctx, p, a)):
        return e
    if not a["slides"]:
        return err("slides 不能为空")
    try:
        data = build_pptx(a["slides"], a.get("title"))
    except ImportError as e:
        return _need(e.name or "", _PIP.get(e.name or "", e.name or ""))
    _ensure_parent(ctx, p)
    existed = p.exists()
    meta = commit_bytes(ctx, p, data, "office", f"{'覆盖' if existed else '创建'} {ctx.guard.rel(p)}")
    return ToolResult(f"已生成 {meta['path']}（{len(a['slides'])} 页，16:9）", meta=meta)


register(Tool(
    "write_presentation", "生成 PowerPoint(.pptx)。slides 数组，每页含 title、bullets（字符串数组，前置两个空格表示下级）、notes（演讲备注，可选）。",
    obj({"path": P("string", "输出路径 .pptx"), "title": P("string", "演示文稿标题（用于封面）"),
         "slides": {"type": "array", "description": "幻灯片列表",
                    "items": obj({"title": P("string", "页标题"), "bullets": {"type": "array", "items": {"type": "string"}, "description": "要点"},
                                  "notes": P("string", "备注")}, [])},
         "overwrite": P("boolean", "允许覆盖已有文件")}, ["path", "slides"]),
    write_presentation, "write", title=lambda a: f"生成演示文稿 {a.get('path', '')}", path_args=("path",)))


# ====================================================================== 编辑
def _replace_in_paragraph(par: Any, find: str, repl: str) -> int:
    """先在单个 run 内替换（保留格式）；跨 run 的匹配则合并到首个 run。"""
    n = 0
    for r in par.runs:
        if find in r.text:
            n += r.text.count(find)
            r.text = r.text.replace(find, repl)
    if n == 0 and find in "".join(r.text for r in par.runs):
        full = "".join(r.text for r in par.runs)
        n = full.count(find)
        par.runs[0].text = full.replace(find, repl)
        for r in par.runs[1:]:
            r.text = ""
    return n


def _edit_docx(data: bytes, a: dict) -> tuple[bytes, str]:
    import docx  # type: ignore
    d = docx.Document(io.BytesIO(data))
    pars = list(d.paragraphs)
    for t in d.tables:
        for row in t.rows:
            for c in row.cells:
                pars.extend(c.paragraphs)
    for s in d.sections:
        pars.extend(s.header.paragraphs)
        pars.extend(s.footer.paragraphs)
    log = []
    for rp in a.get("replacements") or []:
        n = sum(_replace_in_paragraph(p, rp["find"], rp.get("replace", "")) for p in pars)
        log.append(f"“{rp['find'][:30]}” → {n} 处")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue(), "；".join(log)


def _edit_pptx(data: bytes, a: dict) -> tuple[bytes, str]:
    from pptx import Presentation  # type: ignore
    prs = Presentation(io.BytesIO(data))
    log = []
    for rp in a.get("replacements") or []:
        n = 0
        for s in prs.slides:
            for sh in s.shapes:
                if sh.has_text_frame:
                    for para in sh.text_frame.paragraphs:
                        n += _replace_in_paragraph(para, rp["find"], rp.get("replace", ""))
        log.append(f"“{rp['find'][:30]}” → {n} 处")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), "；".join(log)


def _edit_xlsx(data: bytes, a: dict) -> tuple[bytes, str]:
    import openpyxl  # type: ignore
    wb = openpyxl.load_workbook(io.BytesIO(data))
    log = []
    for c in a.get("cells") or []:
        ws = wb[c["sheet"]] if c.get("sheet") else wb.worksheets[0]
        ws[c["cell"]] = _cell_value(c.get("value"))
    if a.get("cells"):
        log.append(f"写入 {len(a['cells'])} 个单元格")
    for rp in a.get("replacements") or []:
        n = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and rp["find"] in cell.value:
                        cell.value = cell.value.replace(rp["find"], rp.get("replace", ""))
                        n += 1
        log.append(f"“{rp['find'][:30]}” → {n} 个单元格")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), "；".join(log) + "（注意：openpyxl 保存可能丢失原表中的图表/图片/数据透视表）"


async def edit_office(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if not p.is_file():
        return err(f"文件不存在: {a['path']}")
    ext = p.suffix.lower()
    if not (a.get("replacements") or a.get("cells")):
        return err("需要提供 replacements（文本替换）或 cells（仅 xlsx 单元格写入）")
    if a.get("cells") and ext not in (".xlsx", ".xlsm"):
        return err("cells 仅适用于 xlsx")
    fn = {".docx": _edit_docx, ".pptx": _edit_pptx, ".xlsx": _edit_xlsx, ".xlsm": _edit_xlsx}.get(ext)
    if not fn:
        return err(f"不支持编辑 {ext}（PDF 不可就地编辑，请用 write_document 重新生成）")
    try:
        new, log = fn(p.read_bytes(), a)
    except ImportError as e:
        return _need(e.name or "", _PIP.get(e.name or "", e.name or ""))
    except KeyError as e:
        return err(f"找不到工作表或字段: {e}")
    except Exception as e:  # noqa: BLE001
        return err(f"编辑失败: {type(e).__name__}: {e}")
    meta = commit_bytes(ctx, p, new, "office", f"编辑 {ctx.guard.rel(p)}")
    return ToolResult(f"已编辑 {meta['path']}：{log}", meta=meta)


register(Tool(
    "edit_office", "就地修改已有的 docx / pptx / xlsx：replacements=[{find,replace}] 做文本替换（保留格式）；xlsx 另可用 cells=[{sheet,cell:'B2',value}] 写入单元格。",
    obj({"path": P("string", "文件路径"),
         "replacements": {"type": "array", "description": "文本替换", "items": obj({"find": P("string", "原文"), "replace": P("string", "新文")}, ["find"])},
         "cells": {"type": "array", "description": "xlsx 单元格写入", "items": obj({"sheet": P("string", "工作表名，缺省为第一个"),
                                                                                 "cell": P("string", "如 B2"), "value": {}}, ["cell"])}}, ["path"]),
    edit_office, "write", title=lambda a: f"编辑 {a.get('path', '')}", path_args=("path",)))
