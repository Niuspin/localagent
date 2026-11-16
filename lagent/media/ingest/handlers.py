"""多模态格式摄取处理器：图片/PDF/Office/数据/Notebook/压缩包/音视频，强制炸弹防护与元信息剥离。"""
from __future__ import annotations

import base64
import csv
import email
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import wave
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import IngestResult
from .sniff import sniff_magic

_ZIP_SLIP_ERR = "检测到压缩包路径穿越/符号链接风险"
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_RESOLUTION_EDGE = {"low": 768, "auto": 1568, "high": 2048}


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(131072)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ====================================================================== ZIP / 炸弹防护
def _validate_member_path(name: str) -> None:
    fn = (name or "").replace("\\", "/")
    if not fn or fn.startswith("/") or (len(fn) > 1 and fn[1] == ":") or ".." in fn.split("/"):
        raise ValueError(f"{_ZIP_SLIP_ERR}: {name}")


def _scan_zip_container(
    path: Path,
    *,
    max_entries: int = 500,
    max_uncompressed_mb: int = 64,
    max_ratio: float = 60.0,
    verify_stream: bool = True,
) -> dict[str, Any]:
    """预检 ZIP 容器：防御 Zip Slip、符号链接、声明炸弹与伪造头实际膨胀炸弹。"""
    max_bytes = max(1, int(max_uncompressed_mb)) * 1024 * 1024
    entries: list[dict[str, Any]] = []
    macros: list[str] = []
    ext_links: list[str] = []
    symlinks: list[str] = []
    declared_total = 0

    with zipfile.ZipFile(path, "r") as z:
        infos = z.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"压缩包条目数超出上限 ({len(infos)} > {max_entries})")
        for m in infos:
            _validate_member_path(m.filename)
            mode = m.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                symlinks.append(m.filename)
                raise ValueError(f"拒绝含符号链接条目: {m.filename}")
            low = m.filename.lower()
            if "vbaproject" in low or low.endswith((".vba", ".bin")) and "vba" in low:
                macros.append(m.filename)
            if m.file_size > max_bytes:
                raise ValueError(f"压缩包单条目体积超出上限: {m.filename} ({m.file_size} bytes)")
            declared_total += max(0, int(m.file_size))
            if declared_total > max_bytes:
                raise ValueError(f"压缩包总解压体积超出上限 (> {max_uncompressed_mb} MB)")
            comp = max(1, int(m.compress_size))
            ratio = float(m.file_size) / comp if not m.is_dir() else 0.0
            if ratio > max_ratio and m.file_size > 1024 * 1024:
                raise ValueError(f"压缩率异常 ({ratio:.1f}:1 > {max_ratio}:1)，疑似解压炸弹: {m.filename}")
            entries.append({
                "name": m.filename,
                "size": m.file_size,
                "compress_size": m.compress_size,
                "ratio": round(ratio, 1),
                "is_dir": m.is_dir(),
            })

        if verify_stream:
            actual_total = 0
            for m in infos:
                if m.is_dir():
                    continue
                comp = max(1, int(m.compress_size))
                with z.open(m, "r") as fp:
                    per_file = 0
                    while True:
                        chunk = fp.read(65536)
                        if not chunk:
                            break
                        per_file += len(chunk)
                        actual_total += len(chunk)
                        if actual_total > max_bytes:
                            raise ValueError(f"压缩包流式解压超出体积上限 (> {max_uncompressed_mb} MB)")
                        if per_file > 1024 * 1024 and (per_file / comp) > max_ratio:
                            raise ValueError(f"流式解压触发压缩率炸弹拦截: {m.filename}")
                if m.filename.lower().endswith(".rels") and per_file <= 512 * 1024:
                    try:
                        rel_txt = z.read(m.filename).decode("utf-8", "ignore")
                        if 'TargetMode="External"' in rel_txt or "TargetMode='External'" in rel_txt:
                            ext_links.append(m.filename)
                    except Exception:
                        pass

    return {"entries": entries, "macros": macros, "external_links": ext_links, "symlinks": symlinks, "declared_bytes": declared_total}


# ====================================================================== 图片 + OCR
def _run_local_ocr(img_bytes: bytes, caps: dict[str, Any]) -> str:
    ocr_kind = str(caps.get("ocr") or "none")
    if ocr_kind == "rapidocr":
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            engine = RapidOCR()
            res, _ = engine(img_bytes)
            return "\n".join(str(row[1]) for row in (res or []) if len(row) >= 2).strip()
        except Exception:
            return ""
    if ocr_kind == "tesseract":
        exe = shutil.which("tesseract")
        if not exe:
            return ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(img_bytes)
                tp = Path(tf.name)
            try:
                cp = subprocess.run([exe, str(tp), "stdout", "--psm", "3"], capture_output=True, text=True, timeout=15)
                return (cp.stdout or "").strip() if cp.returncode == 0 else ""
            finally:
                tp.unlink(missing_ok=True)
        except Exception:
            return ""
    return ""


def _strip_and_encode_pil(im: Any, *, max_edge: int = 1568) -> tuple[bytes, int, int, str]:
    """剥离 EXIF/GPS 并缩放为干净的 JPEG 字节流。"""
    from PIL import Image  # type: ignore
    rgb = im.convert("RGB")
    w, h = rgb.size
    if max(w, h) > max_edge:
        scale = max_edge / float(max(w, h))
        rgb = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    nw, nh = rgb.size
    clean = Image.new("RGB", (nw, nh))
    clean.paste(rgb)
    buf = io.BytesIO()
    clean.save(buf, format="JPEG", quality=85)
    raw = buf.getvalue()
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    return raw, nw, nh, url


def _ingest_image(path: Path, mime: str, cfg: dict[str, Any], caps: dict[str, Any], options: dict[str, Any]) -> IngestResult:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return IngestResult("image", mime, str(path), path.name, ok=False, error="缺少 Pillow 依赖，无法解析图片")

    max_px = int(cfg.get("max_image_pixels") or 40_000_000)
    Image.MAX_IMAGE_PIXELS = max_px
    res_tier = str(options.get("resolution") or "auto").lower()
    max_edge = _RESOLUTION_EDGE.get(res_tier, 1568)
    warnings: list[str] = []

    try:
        with Image.open(path) as im:
            w, h = im.size
            if w <= 0 or h <= 0 or (w * h) > max_px:
                return IngestResult(
                    "image", mime, str(path), path.name, ok=False,
                    error=f"图片尺寸异常或触发像素炸弹上限: {w}x{h} ({w * h} px > {max_px})",
                )
            exif_obj = None
            try:
                exif_obj = im.getexif()
            except Exception:
                exif_obj = None
            has_exif = bool(im.info.get("exif") or (exif_obj and len(exif_obj) > 0))
            has_gps = bool(exif_obj and 0x8825 in exif_obj)
            if has_gps:
                warnings.append("exif_gps_stripped")
            elif has_exif:
                warnings.append("exif_stripped")

            images_out: list[dict[str, Any]] = []
            ocr_texts: list[str] = []

            # 超长截图自动切片，避免单张压缩损失文字清晰度
            if h > max_edge and (h / max(1, w)) >= 2.5 and int(options.get("max_tiles", 6)) > 1:
                rgb = im.convert("RGB")
                if w > max_edge:
                    sc = max_edge / float(w)
                    rgb = rgb.resize((max_edge, max(1, int(h * sc))), Image.Resampling.LANCZOS)
                sw, sh = rgb.size
                tile_h = min(max_edge, max(512, sw))
                overlap = max(48, tile_h // 10)
                step = max(256, tile_h - overlap)
                max_tiles = min(6, int(options.get("max_tiles") or 6))
                y = 0
                idx = 0
                while y < sh and idx < max_tiles:
                    y2 = min(sh, y + tile_h)
                    tile = rgb.crop((0, y, sw, y2))
                    raw_b, tw, th, url = _strip_and_encode_pil(tile, max_edge=max_edge)
                    images_out.append({"data_url": url, "width": tw, "height": th, "mime": "image/jpeg", "label": f"tile_{idx + 1}"})
                    if not caps.get("vision") and caps.get("ocr") in ("rapidocr", "tesseract"):
                        t = _run_local_ocr(raw_b, caps)
                        if t:
                            ocr_texts.append(t)
                    idx += 1
                    if y2 >= sh:
                        break
                    y += step
                if y < sh:
                    warnings.append("tall_image_tiles_truncated")
            else:
                raw_b, nw, nh, url = _strip_and_encode_pil(im, max_edge=max_edge)
                images_out.append({"data_url": url, "width": nw, "height": nh, "mime": "image/jpeg", "label": "main"})
                if not caps.get("vision") and caps.get("ocr") in ("rapidocr", "tesseract"):
                    t = _run_local_ocr(raw_b, caps)
                    if t:
                        ocr_texts.append(t)

            text_blocks: list[dict[str, Any]] = []
            if ocr_texts:
                text_blocks.append({"title": "OCR 提取文本", "text": "\n\n".join(ocr_texts)})
            elif not caps.get("vision"):
                warnings.append("no_vision_or_ocr_capability")

            return IngestResult(
                kind="image",
                mime=mime,
                path=str(path),
                title=path.name,
                text_blocks=text_blocks,
                images=images_out,
                meta={
                    "width": w,
                    "height": h,
                    "size_bytes": path.stat().st_size,
                    "exif_present": has_exif,
                    "gps_present": has_gps,
                    "exif_stripped": True,
                    "tiles": len(images_out),
                },
                warnings=warnings,
                cost_estimate={"image_count": len(images_out), "tokens_est": len(images_out) * 750 + sum(len(x["text"]) for x in text_blocks) // 3},
                tainted=True,
                ok=True,
            )
    except Exception as e:
        return IngestResult("image", mime, str(path), path.name, ok=False, error=f"图片解析失败: {e}")


# ====================================================================== PDF（文本层 + 扫描页渲染 + JS 剥离提示）
def _ingest_pdf(path: Path, cfg: dict[str, Any], caps: dict[str, Any], options: dict[str, Any]) -> IngestResult:
    max_pages = int(cfg.get("max_pdf_pages") or 50)
    warnings: list[str] = []
    raw_head = path.read_bytes()[:4_000_000]
    if re.search(rb"/(?:JavaScript|JS|OpenAction|AA|Launch|EmbeddedFiles?)\b", raw_head):
        warnings.append("pdf_active_content_stripped")

    pages_text: list[tuple[int, str]] = []
    total_pages = 0
    tables_out: list[dict[str, Any]] = []
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(str(path)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages > max_pages:
                warnings.append(f"pdf_pages_capped_at_{max_pages}")
            for i, pg in enumerate(pdf.pages[:max_pages], start=1):
                txt = (pg.extract_text() or "").strip()
                pages_text.append((i, txt))
                if len(tables_out) < 6:
                    try:
                        for t in (pg.extract_tables() or [])[:2]:
                            if t and len(t) >= 2:
                                hdr = [str(c or "") for c in t[0]]
                                rows = [[str(c or "") for c in r] for r in t[1:16]]
                                tables_out.append({"name": f"page_{i}", "columns": hdr, "rows": rows, "total_rows": len(t) - 1})
                    except Exception:
                        pass
    except ImportError:
        try:
            from pypdf import PdfReader  # type: ignore
            r = PdfReader(str(path))
            total_pages = len(r.pages)
            if total_pages > max_pages:
                warnings.append(f"pdf_pages_capped_at_{max_pages}")
            for i, pg in enumerate(r.pages[:max_pages], start=1):
                pages_text.append((i, (pg.extract_text() or "").strip()))
        except Exception as e:
            return IngestResult("pdf", "application/pdf", str(path), path.name, ok=False, error=f"PDF 解析失败: {e}")
    except Exception as e:
        return IngestResult("pdf", "application/pdf", str(path), path.name, ok=False, error=f"PDF 损坏或加密拒绝: {e}")

    sparse_pages = [i for i, txt in pages_text if len(re.sub(r"\s+", "", txt)) < 20]
    rendered_imgs: list[dict[str, Any]] = []
    ocr_blocks: list[dict[str, Any]] = []

    if sparse_pages and caps.get("pdf_render") == "pypdfium2" and bool(options.get("render_scanned", True)):
        try:
            import pypdfium2 as pdfium  # type: ignore
            doc = pdfium.PdfDocument(str(path))
            try:
                max_render = min(8, int(options.get("max_render_pages") or 8))
                for p_num in sparse_pages[:max_render]:
                    page = doc[p_num - 1]
                    bmp = page.render(scale=1.5)
                    pil_im = bmp.to_pil()
                    raw_b, rw, rh, url = _strip_and_encode_pil(pil_im, max_edge=1568)
                    rendered_imgs.append({"data_url": url, "width": rw, "height": rh, "mime": "image/jpeg", "label": f"page_{p_num}"})
                    if not caps.get("vision") and caps.get("ocr") in ("rapidocr", "tesseract"):
                        ot = _run_local_ocr(raw_b, caps)
                        if ot:
                            ocr_blocks.append({"title": f"第 {p_num} 页 OCR", "text": ot, "page": p_num})
            finally:
                doc.close()
        except Exception as e:
            warnings.append(f"pdf_render_failed: {e}")
    elif sparse_pages:
        warnings.append("scanned_pdf_needs_vision_or_ocr")

    text_blocks = [{"title": f"第 {i}/{total_pages} 页", "text": txt, "page": i} for i, txt in pages_text if txt]
    text_blocks.extend(ocr_blocks)
    if not text_blocks and not rendered_imgs:
        warnings.append("pdf_empty_text_layer")

    chars = sum(len(b["text"]) for b in text_blocks)
    return IngestResult(
        kind="pdf",
        mime="application/pdf",
        path=str(path),
        title=path.name,
        text_blocks=text_blocks,
        images=rendered_imgs,
        tables=tables_out,
        meta={
            "size_bytes": path.stat().st_size,
            "page_count": total_pages,
            "scanned_pages": len(sparse_pages),
            "js_stripped": "pdf_active_content_stripped" in warnings,
        },
        warnings=warnings,
        cost_estimate={"tokens_est": chars // 3 + len(rendered_imgs) * 750, "image_count": len(rendered_imgs), "chars": chars},
        tainted=True,
        ok=True,
    )


# ====================================================================== Office (docx / xlsx / pptx)
def _extract_embedded_images(z: zipfile.ZipFile, prefix: str, *, max_imgs: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return out
    media_names = [n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")][:max_imgs]
    for mn in media_names:
        try:
            raw = z.read(mn)
            if len(raw) > 8 * 1024 * 1024:
                continue
            with Image.open(io.BytesIO(raw)) as im:
                if (im.size[0] * im.size[1]) > 25_000_000:
                    continue
                _, w, h, url = _strip_and_encode_pil(im, max_edge=1280)
                out.append({"data_url": url, "width": w, "height": h, "mime": "image/jpeg", "label": Path(mn).name})
        except Exception:
            continue
    return out


def _ingest_office(path: Path, kind: str, mime: str, cfg: dict[str, Any]) -> IngestResult:
    max_entries = int(cfg.get("max_archive_entries") or 500)
    max_mb = int(cfg.get("max_archive_mb") or 64)
    try:
        scan = _scan_zip_container(path, max_entries=max_entries, max_uncompressed_mb=max_mb)
    except Exception as e:
        return IngestResult(kind, mime, str(path), path.name, ok=False, error=f"Office 文档容器校验失败（已拦截）: {e}")

    warnings: list[str] = []
    if scan["macros"] or path.suffix.lower() == ".xlsm":
        warnings.append("office_macros_stripped")
    if scan["external_links"]:
        warnings.append("external_links_stripped")

    text_blocks: list[dict[str, Any]] = []
    tables_out: list[dict[str, Any]] = []
    images_out: list[dict[str, Any]] = []

    try:
        if kind == "docx":
            import docx  # type: ignore
            d = docx.Document(str(path))
            paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
            if paras:
                text_blocks.append({"title": "文档正文", "text": "\n".join(paras)})
            for idx, tbl in enumerate(d.tables[:12], start=1):
                rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in tbl.rows]
                if rows:
                    tables_out.append({"name": f"table_{idx}", "columns": rows[0], "rows": rows[1:30], "total_rows": max(0, len(rows) - 1)})
            with zipfile.ZipFile(path, "r") as z:
                if "word/comments.xml" in z.namelist():
                    root = ET.fromstring(z.read("word/comments.xml"))
                    comments = ["".join(c.itertext()).strip() for c in root.iter() if c.tag.endswith("}comment")]
                    comments = [c for c in comments if c]
                    if comments:
                        text_blocks.append({"title": f"批注 ({len(comments)})", "text": "\n".join(f"- {c}" for c in comments[:50])})
                # 追踪修订 w:ins / w:del
                if "word/document.xml" in z.namelist():
                    doc_root = ET.fromstring(z.read("word/document.xml"))
                    ins = ["".join(x.itertext()).strip() for x in doc_root.iter() if x.tag.endswith("}ins")]
                    dels = ["".join(x.itertext()).strip() for x in doc_root.iter() if x.tag.endswith("}del")]
                    revs = [f"+ 插入: {t}" for t in ins if t] + [f"- 删除: {t}" for t in dels if t]
                    if revs:
                        text_blocks.append({"title": f"修订记录 ({len(revs)})", "text": "\n".join(revs[:40])})
                images_out = _extract_embedded_images(z, "word/media/")

        elif kind == "xlsx":
            import openpyxl  # type: ignore
            # keep_vba=False 确保即便 .xlsm 也不载入宏对象
            wb_v = openpyxl.load_workbook(str(path), data_only=True, read_only=True, keep_vba=False)
            try:
                for ws in wb_v.worksheets[:15]:
                    rows_all: list[list[str]] = []
                    for r in ws.iter_rows(max_row=200, max_col=30, values_only=True):
                        vals = ["" if v is None else str(v) for v in r]
                        while vals and vals[-1] == "":
                            vals.pop()
                        if vals:
                            rows_all.append(vals)
                    cols = rows_all[0] if rows_all else []
                    body = rows_all[1:25] if len(rows_all) > 1 else []
                    tables_out.append({"name": ws.title, "columns": cols, "rows": body, "total_rows": ws.max_row or len(rows_all)})
            finally:
                wb_v.close()

        elif kind == "pptx":
            from pptx import Presentation  # type: ignore
            prs = Presentation(str(path))
            for idx, sl in enumerate(prs.slides, start=1):
                if idx > 80:
                    break
                slide_lines: list[str] = []
                for sh in sl.shapes:
                    if sh.has_text_frame:
                        for para in sh.text_frame.paragraphs:
                            t = "".join(r.text for r in para.runs).strip()
                            if t:
                                slide_lines.append(("  " * para.level) + "- " + t)
                    if getattr(sh, "has_table", False) and sh.has_table:
                        rows = [[c.text.strip() for c in r.cells] for r in sh.table.rows]
                        if rows:
                            tables_out.append({"name": f"slide_{idx}", "columns": rows[0], "rows": rows[1:20], "total_rows": len(rows) - 1})
                if sl.has_notes_slide and sl.notes_slide.notes_text_frame.text.strip():
                    slide_lines.append("备注: " + sl.notes_slide.notes_text_frame.text.strip())
                if slide_lines:
                    text_blocks.append({"title": f"幻灯片 {idx}", "text": "\n".join(slide_lines)})
            with zipfile.ZipFile(path, "r") as z:
                images_out = _extract_embedded_images(z, "ppt/media/")
    except Exception as e:
        return IngestResult(kind, mime, str(path), path.name, ok=False, error=f"Office 解析失败: {e}")

    chars = sum(len(b["text"]) for b in text_blocks)
    return IngestResult(
        kind=kind,
        mime=mime,
        path=str(path),
        title=path.name,
        text_blocks=text_blocks,
        images=images_out,
        tables=tables_out,
        meta={
            "size_bytes": path.stat().st_size,
            "macro_stripped": bool(scan["macros"] or path.suffix.lower() == ".xlsm"),
            "external_links_stripped": bool(scan["external_links"]),
        },
        warnings=warnings,
        cost_estimate={"tokens_est": chars // 3 + len(images_out) * 750, "image_count": len(images_out), "chars": chars},
        tainted=True,
        ok=True,
    )


# ====================================================================== 结构化数据 (csv / tsv / json / jsonl / yaml / parquet)
def _infer_col_types(rows: list[list[str]], ncols: int) -> list[str]:
    types: list[str] = []
    for c in range(ncols):
        seen_t: set[str] = set()
        for r in rows[:200]:
            v = r[c].strip() if c < len(r) else ""
            if not v:
                continue
            if v.lower() in ("true", "false"):
                seen_t.add("bool")
            elif re.fullmatch(r"-?\d+", v):
                seen_t.add("int")
            elif re.fullmatch(r"-?\d+(?:\.\d+)?", v):
                seen_t.add("float")
            else:
                seen_t.add("str")
        if not seen_t:
            types.append("empty")
        elif seen_t == {"int"}:
            types.append("int")
        elif seen_t <= {"int", "float"}:
            types.append("float")
        elif seen_t == {"bool"}:
            types.append("bool")
        else:
            types.append("str")
    return types


def _ingest_data(path: Path, kind: str, mime: str) -> IngestResult:
    try:
        if kind in ("csv", "tsv"):
            delim = "\t" if kind == "tsv" else ","
            total_rows = 0
            sample_rows: list[list[str]] = []
            headers: list[str] = []
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                rd = csv.reader(f, delimiter=delim)
                for idx, row in enumerate(rd):
                    if idx == 0:
                        headers = [str(x) for x in row]
                    else:
                        total_rows += 1
                        if len(sample_rows) < 25:
                            sample_rows.append([str(x) for x in row])
                    if total_rows >= 50_000:
                        break
            col_types = _infer_col_types(sample_rows, len(headers))
            schema_txt = "\n".join(f"- `{h}`: {t}" for h, t in zip(headers, col_types))
            return IngestResult(
                kind=kind,
                mime=mime,
                path=str(path),
                title=path.name,
                text_blocks=[{"title": "推断 Schema", "text": schema_txt}] if headers else [],
                tables=[{"name": path.name, "columns": headers, "rows": sample_rows[:15], "total_rows": total_rows}],
                meta={"size_bytes": path.stat().st_size, "rows": total_rows, "columns": len(headers)},
                tainted=True,
                ok=True,
            )

        if kind == "parquet":
            try:
                import pyarrow.parquet as pq  # type: ignore
            except ImportError:
                return IngestResult(
                    kind="parquet", mime=mime, path=str(path), title=path.name,
                    text_blocks=[{"title": "提示", "text": "未安装可选依赖 pyarrow，无法解析 Parquet 文件（可运行 pip install pyarrow 后重试）。"}],
                    warnings=["missing_optional_pyarrow"],
                    meta={"size_bytes": path.stat().st_size},
                    tainted=True, ok=True,
                )
            pf = pq.ParquetFile(str(path))
            sch = pf.schema_arrow
            cols = [f.name for f in sch]
            tbl_sample = pf.read_row_group(0).slice(0, 15).to_pydict() if pf.num_row_groups > 0 else {}
            rows = [[str((tbl_sample.get(c) or [""] * 15)[i]) for c in cols] for i in range(min(15, pf.metadata.num_rows))]
            return IngestResult(
                kind="parquet", mime=mime, path=str(path), title=path.name,
                text_blocks=[{"title": "Parquet Schema", "text": "\n".join(f"- `{f.name}`: {f.type}" for f in sch)}],
                tables=[{"name": path.name, "columns": cols, "rows": rows, "total_rows": pf.metadata.num_rows}],
                meta={"size_bytes": path.stat().st_size, "rows": pf.metadata.num_rows, "columns": len(cols)},
                tainted=True, ok=True,
            )

        raw = path.read_text("utf-8", "replace")[:500_000]
        if kind == "yaml":
            import yaml  # type: ignore
            obj = yaml.safe_load(raw)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)[:10_000]
            return IngestResult("yaml", mime, str(path), path.name, text_blocks=[{"title": "YAML 结构预览", "text": pretty}], meta={"size_bytes": path.stat().st_size}, tainted=True, ok=True)
        if kind == "jsonl":
            recs = [json.loads(ln) for ln in raw.splitlines()[:50] if ln.strip()]
            keys = sorted({k for r in recs if isinstance(r, dict) for k in r.keys()})
            return IngestResult(
                "jsonl", mime, str(path), path.name,
                text_blocks=[{"title": "JSONL 样本预览", "text": json.dumps(recs[:5], ensure_ascii=False, indent=2)[:8000]}],
                meta={"size_bytes": path.stat().st_size, "sampled_lines": len(recs), "keys": keys[:30]},
                tainted=True, ok=True,
            )
        # json
        obj = json.loads(raw)
        summary = json.dumps(obj, ensure_ascii=False, indent=2)[:10_000]
        return IngestResult("json", mime, str(path), path.name, text_blocks=[{"title": "JSON 预览", "text": summary}], meta={"size_bytes": path.stat().st_size}, tainted=True, ok=True)
    except Exception as e:
        return IngestResult(kind, mime, str(path), path.name, ok=False, error=f"结构化数据解析失败: {e}")


# ====================================================================== Notebook / Ebook / HTML / MHTML / Markdown
class _CleanHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "iframe", "object"):
            self._skip += 1
        style = dict(attrs).get("style") or ""
        if "display:none" in style.replace(" ", "").lower() or "visibility:hidden" in style.replace(" ", "").lower():
            self._skip += 1
        if t == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "iframe", "object") and self._skip > 0:
            self._skip -= 1
        if t == "title":
            self._in_title = False
        if t in ("p", "div", "li", "h1", "h2", "h3", "h4", "tr", "br"):
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip > 0:
            return
        if self._in_title and not self.title:
            self.title = data.strip()
        s = _ZERO_WIDTH_RE.sub("", data)
        if s.strip():
            self.chunks.append(s.strip() + " ")


def _html_to_text(html_str: str) -> tuple[str, str]:
    p = _CleanHtmlParser()
    p.feed(html_str)
    txt = re.sub(r"\n{3,}", "\n\n", "".join(p.chunks)).strip()
    return p.title, txt


def _ingest_notebook_or_doc(path: Path, kind: str, mime: str, cfg: dict[str, Any]) -> IngestResult:
    try:
        if kind == "ipynb":
            nb = json.loads(path.read_text("utf-8", "replace"))
            cells = nb.get("cells") or []
            blocks: list[dict[str, Any]] = []
            stripped_blobs = 0
            for idx, cell in enumerate(cells[:120], start=1):
                ctype = cell.get("cell_type") or "code"
                src = "".join(cell.get("source") or []) if isinstance(cell.get("source"), list) else str(cell.get("source") or "")
                lines = [src.strip()]
                for out in (cell.get("outputs") or [])[:10]:
                    data = out.get("data") or {}
                    if any(k.startswith("image/") for k in data):
                        stripped_blobs += 1
                    txt = out.get("text") or data.get("text/plain")
                    if txt:
                        joined = "".join(txt) if isinstance(txt, list) else str(txt)
                        lines.append("[output]\n" + joined[:600])
                blocks.append({"title": f"Cell #{idx} ({ctype})", "text": "\n".join(x for x in lines if x)})
            warns = [f"stripped_{stripped_blobs}_binary_outputs"] if stripped_blobs else []
            return IngestResult("ipynb", mime, str(path), path.name, text_blocks=blocks, warnings=warns, meta={"cells": len(cells)}, tainted=True, ok=True)

        if kind == "epub":
            _scan_zip_container(path, max_entries=int(cfg.get("max_archive_entries") or 500), max_uncompressed_mb=int(cfg.get("max_archive_mb") or 64))
            blocks = []
            with zipfile.ZipFile(path, "r") as z:
                html_files = [n for n in sorted(z.namelist()) if n.lower().endswith((".xhtml", ".html", ".htm"))][:40]
                for hf in html_files:
                    title, txt = _html_to_text(z.read(hf).decode("utf-8", "replace"))
                    if txt:
                        blocks.append({"title": title or Path(hf).name, "text": txt[:8000]})
            return IngestResult("epub", mime, str(path), path.name, text_blocks=blocks, meta={"chapters": len(blocks)}, tainted=True, ok=True)

        if kind == "mhtml":
            raw_bytes = path.read_bytes()
            msg = email.message_from_bytes(raw_bytes)
            html_body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    html_body = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    break
            title, txt = _html_to_text(html_body or raw_bytes.decode("utf-8", "replace"))
            return IngestResult("mhtml", mime, str(path), title or path.name, text_blocks=[{"title": title or "MHTML 正文", "text": txt}], tainted=True, ok=True)

        if kind == "html":
            title, txt = _html_to_text(path.read_text("utf-8", "replace"))
            return IngestResult("html", mime, str(path), title or path.name, text_blocks=[{"title": title or "HTML 正文", "text": txt}], warnings=["html_scripts_stripped"], tainted=True, ok=True)

        # markdown / plain text
        txt = path.read_text("utf-8", "replace")[:200_000]
        return IngestResult(kind, mime, str(path), path.name, text_blocks=[{"title": path.name, "text": txt}], meta={"size_bytes": path.stat().st_size}, tainted=True, ok=True)
    except Exception as e:
        return IngestResult(kind, mime, str(path), path.name, ok=False, error=f"文档解析失败: {e}")


# ====================================================================== 压缩包（仅安全列目录 + 按需查看，不盲目解压）
def _ingest_archive(path: Path, mime: str, cfg: dict[str, Any], options: dict[str, Any]) -> IngestResult:
    max_entries = int(cfg.get("max_archive_entries") or 500)
    max_mb = int(cfg.get("max_archive_mb") or 64)
    select_member = str(options.get("select_member") or "").strip()

    try:
        if mime == "application/zip":
            scan = _scan_zip_container(path, max_entries=max_entries, max_uncompressed_mb=max_mb, verify_stream=True)
            entries = scan["entries"]
            blocks: list[dict[str, Any]] = []
            if select_member:
                _validate_member_path(select_member)
                with zipfile.ZipFile(path, "r") as z:
                    if select_member in z.namelist():
                        data = z.read(select_member)[:100_000]
                        blocks.append({"title": f"成员预览: {select_member}", "text": data.decode("utf-8", "replace")})
            rows = [[e["name"], str(e["size"]), str(e["compress_size"]), str(e["ratio"])] for e in entries[:50]]
            return IngestResult(
                kind="archive", mime=mime, path=str(path), title=path.name,
                text_blocks=blocks,
                tables=[{"name": "archive_entries", "columns": ["name", "size", "compressed", "ratio"], "rows": rows, "total_rows": len(entries)}],
                meta={"size_bytes": path.stat().st_size, "entry_count": len(entries)},
                warnings=["archive_list_only_no_auto_extract"],
                tainted=True, ok=True,
            )

        if mime in ("application/x-tar", "application/gzip"):
            name_low = path.name.lower()
            if mime == "application/x-tar" or name_low.endswith((".tar.gz", ".tgz")):
                entries_rows: list[list[str]] = []
                total_u = 0
                max_bytes = max_mb * 1024 * 1024
                with tarfile.open(path, "r:*") as tf:
                    members = tf.getmembers()
                    if len(members) > max_entries:
                        raise ValueError(f"Tar 条目数超限 ({len(members)} > {max_entries})")
                    for m in members:
                        _validate_member_path(m.name)
                        if m.issym() or m.islnk():
                            raise ValueError(f"拒绝含符号链接条目: {m.name}")
                        total_u += max(0, int(m.size))
                        if total_u > max_bytes:
                            raise ValueError(f"Tar 解压体积超限 (> {max_mb} MB)")
                        entries_rows.append([m.name, str(m.size), "dir" if m.isdir() else "file"])
                return IngestResult(
                    kind="archive", mime=mime, path=str(path), title=path.name,
                    tables=[{"name": "archive_entries", "columns": ["name", "size", "type"], "rows": entries_rows[:50], "total_rows": len(entries_rows)}],
                    meta={"size_bytes": path.stat().st_size, "entry_count": len(entries_rows)},
                    warnings=["archive_list_only_no_auto_extract"],
                    tainted=True, ok=True,
                )
            # 单文件 .gz：流式解压探测压缩炸弹
            max_bytes = max_mb * 1024 * 1024
            comp_size = max(1, path.stat().st_size)
            acc = bytearray()
            with gzip.open(path, "rb") as gf:
                while True:
                    chunk = gf.read(65536)
                    if not chunk:
                        break
                    acc.extend(chunk)
                    if len(acc) > max_bytes or (len(acc) > 1024 * 1024 and (len(acc) / comp_size) > 60.0):
                        raise ValueError("GZIP 解压超出大小或压缩比上限（疑似解压炸弹）")
            txt = bytes(acc[:64_000]).decode("utf-8", "replace")
            return IngestResult("archive", mime, str(path), path.name, text_blocks=[{"title": "GZIP 内容预览", "text": txt}], meta={"uncompressed_bytes": len(acc)}, tainted=True, ok=True)
    except Exception as e:
        return IngestResult("archive", mime, str(path), path.name, ok=False, error=f"压缩包拦截（安全或格式错误）: {e}")

    return IngestResult("archive", mime, str(path), path.name, ok=False, error=f"不支持的压缩格式: {mime}")


# ====================================================================== 音频 / 视频（能力协商 + 可选 whisper / ffmpeg）
def _ingest_audio(path: Path, mime: str, cfg: dict[str, Any], caps: dict[str, Any]) -> IngestResult:
    meta: dict[str, Any] = {"size_bytes": path.stat().st_size}
    warnings: list[str] = []
    if mime == "audio/wav":
        try:
            with wave.open(str(path), "rb") as w:
                frames = w.getnframes()
                rate = max(1, w.getframerate())
                meta["duration_s"] = round(frames / float(rate), 2)
                meta["channels"] = w.getnchannels()
                meta["sample_rate"] = rate
        except Exception as e:
            return IngestResult("audio", mime, str(path), path.name, ok=False, error=f"WAV 文件损坏: {e}")

    whisper = caps.get("whisper")
    if caps.get("audio_model"):
        return IngestResult("audio", mime, str(path), path.name, text_blocks=[{"title": "音频元数据", "text": f"模型原生支持音频模态；时长 {meta.get('duration_s', '未知')}s"}], meta={**meta, "native_audio": True}, tainted=True, ok=True)

    if whisper:
        try:
            cp = subprocess.run(
                [str(whisper), "-f", str(path), "--no-timestamps"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=min(int(cfg.get("subprocess_timeout_s") or 30), 120),
            )
            if cp.returncode == 0 and (cp.stdout or "").strip():
                return IngestResult("audio", mime, str(path), path.name, text_blocks=[{"title": "Whisper 本地转写", "text": cp.stdout.strip()}], meta=meta, tainted=True, ok=True)
            warnings.append(f"whisper_exit_{cp.returncode}")
        except Exception as e:
            warnings.append(f"whisper_failed: {e}")

    warnings.append("audio_transcription_unavailable")
    return IngestResult(
        "audio", mime, str(path), path.name,
        text_blocks=[{"title": "音频降级说明", "text": "未检测到本地 whisper.cpp 可执行文件且当前模型未开启音频模态，无法直接转写此音频。"}],
        meta=meta, warnings=warnings, tainted=True, ok=True,
    )


def _ingest_video(path: Path, mime: str, cfg: dict[str, Any], caps: dict[str, Any]) -> IngestResult:
    meta: dict[str, Any] = {"size_bytes": path.stat().st_size}
    ffmpeg = caps.get("ffmpeg")
    if not ffmpeg:
        return IngestResult(
            "video", mime, str(path), path.name,
            text_blocks=[{"title": "视频降级说明", "text": f"未检测到 ffmpeg，无法从视频 ({mime}, {meta['size_bytes']} bytes) 抽取关键帧或音轨。"}],
            meta=meta, warnings=["ffmpeg_unavailable"], tainted=True, ok=True,
        )

    max_frames = min(8, int(cfg.get("max_video_frames") or 6))
    timeout = min(int(cfg.get("subprocess_timeout_s") or 30), 60)
    imgs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="la_vid_") as td:
        out_pat = str(Path(td) / "frame_%02d.jpg")
        try:
            cp = subprocess.run(
                [str(ffmpeg), "-y", "-i", str(path), "-vf", "fps=1/5,scale=1024:-1", "-frames:v", str(max_frames), out_pat],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
            if cp.returncode != 0:
                return IngestResult("video", mime, str(path), path.name, ok=False, error=f"视频解析失败: {(cp.stderr or '')[:240]}")
            for fp in sorted(Path(td).glob("frame_*.jpg"))[:max_frames]:
                b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
                imgs.append({"data_url": f"data:image/jpeg;base64,{b64}", "mime": "image/jpeg", "label": fp.name})
        except Exception as e:
            return IngestResult("video", mime, str(path), path.name, ok=False, error=f"ffmpeg 抽帧异常: {e}")

    return IngestResult("video", mime, str(path), path.name, images=imgs, meta={**meta, "frames": len(imgs)}, tainted=True, ok=True)


# ====================================================================== 统一入口（供 worker 与直接调用复用）
def ingest_path_direct(
    path: Path,
    *,
    cfg: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> IngestResult:
    t0 = time.monotonic()
    p = Path(path)
    if not p.is_file():
        return IngestResult("unknown", "application/octet-stream", str(p), p.name, ok=False, error="文件不存在")
    cfg = dict(cfg or {})
    caps = dict(caps or {})
    options = dict(options or {})

    with p.open("rb") as f:
        head = f.read(16384)
    kind, mime = sniff_magic(head, p.name, path=p)

    if kind == "image":
        res = _ingest_image(p, mime, cfg, caps, options)
    elif kind == "pdf":
        res = _ingest_pdf(p, cfg, caps, options)
    elif kind in ("docx", "xlsx", "pptx"):
        res = _ingest_office(p, kind, mime, cfg)
    elif kind in ("csv", "tsv", "json", "jsonl", "yaml", "parquet"):
        res = _ingest_data(p, kind, mime)
    elif kind in ("ipynb", "epub", "html", "mhtml", "md", "text"):
        res = _ingest_notebook_or_doc(p, kind, mime, cfg)
    elif kind == "archive":
        res = _ingest_archive(p, mime, cfg, options)
    elif kind == "audio":
        res = _ingest_audio(p, mime, cfg, caps)
    elif kind == "video":
        res = _ingest_video(p, mime, cfg, caps)
    else:
        res = IngestResult("binary", mime, str(p), p.name, ok=False, error=f"未受支持的二进制文件或未知格式 ({mime})")

    res.tainted = True
    res.meta.setdefault("size_bytes", p.stat().st_size)
    res.meta.setdefault("sha256", _sha256_file(p))
    res.cost_estimate.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
    return res
