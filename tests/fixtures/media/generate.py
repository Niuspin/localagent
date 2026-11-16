"""生成 M3 多模态黄金样本与畸形/炸弹测试夹具到 tests/fixtures/media/。"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
import zipfile
import zlib
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent


def _make_png(w: int, h: int, path: Path, color=(240, 240, 250)) -> None:
    im = Image.new("RGB", (w, h), color)
    im.save(path, format="PNG")


def _make_exif_gps_jpeg(path: Path) -> None:
    im = Image.new("RGB", (64, 48), (120, 180, 220))
    exif = im.getexif()
    exif[0x010F] = "LocalAgentCamera"
    exif[0x8825] = {1: "N", 2: (31.0, 14.0, 0.0)}  # GPSInfo IFD
    im.save(path, format="JPEG", exif=exif)


def _make_pixel_bomb_png(path: Path) -> None:
    # 仅伪造 IHDR 声明 30000x30000 像素（900MP），文件体积 < 100 bytes
    ihdr_data = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    idat_data = zlib.compress(b"\x00\x00\x00\x00")
    idat_crc = zlib.crc32(b"IDAT" + idat_data) & 0xFFFFFFFF
    idat = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + struct.pack(">I", idat_crc)
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend)


def _make_minimal_pdf(path: Path, *, text: str = "", extra_dict: str = "") -> None:
    stream_body = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin1") if text else b""
    objs = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R " + extra_dict.encode("latin1") + b" >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        b"4 0 obj << /Length " + str(len(stream_body)).encode("ascii") + b" >> stream\n" + stream_body + b"\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for o in objs:
        offsets.append(len(buf))
        buf.extend(o)
    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    path.write_bytes(bytes(buf))


def _make_docx_sample(path: Path) -> None:
    import docx  # type: ignore
    d = docx.Document()
    d.add_heading("M3 需求评审纪要", level=1)
    d.add_paragraph("验证正文与表格结构化抽取。")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "模块"
    t.cell(0, 1).text = "状态"
    t.cell(1, 0).text = "ingest"
    t.cell(1, 1).text = "done"
    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    # 额外注入 word/comments.xml 与一张内嵌 PNG 图片测试完整性
    img_buf = io.BytesIO()
    Image.new("RGB", (32, 32), (80, 160, 90)).save(img_buf, format="PNG")
    with zipfile.ZipFile(buf, "r") as zin, zipfile.ZipFile(path, "w") as zout:
        for item in zin.infolist():
            if item.filename == "xl/_rels/workbook.xml.rels":
                continue
            zout.writestr(item, zin.read(item.filename))
        zout.writestr(
            "word/comments.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:comment w:id="0" w:author="Reviewer"><w:p><w:r><w:t>请确保剥离外部链接</w:t></w:r></w:p></w:comment>'
            "</w:comments>",
        )
        zout.writestr("word/media/image1.png", img_buf.getvalue())


def _make_xlsm_macro(path: Path) -> None:
    import openpyxl  # type: ignore
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Metrics"
    ws.append(["Service", "LatencyMs"])
    ws.append(["media-worker", 42])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with zipfile.ZipFile(buf, "r") as zin, zipfile.ZipFile(path, "w") as zout:
        rels_orig = ""
        for item in zin.infolist():
            if item.filename == "xl/_rels/workbook.xml.rels":
                rels_orig = zin.read(item.filename).decode("utf-8")
                continue
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("xl/vbaProject.bin", b"FAKE_VBA_MACRO_PAYLOAD_DO_NOT_EXEC")
        ext_rel = '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="https://evil.example.com/exfil" TargetMode="External"/>'
        if "</Relationships>" in rels_orig:
            rels_new = rels_orig.replace("</Relationships>", ext_rel + "</Relationships>")
        else:
            rels_new = f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{ext_rel}</Relationships>'
        zout.writestr("xl/_rels/workbook.xml.rels", rels_new)


def _make_pptx_sample(path: Path) -> None:
    from pptx import Presentation  # type: ignore
    prs = Presentation()
    sl = prs.slides.add_slide(prs.slide_layouts[1])
    sl.shapes.title.text = "M3 Architecture"
    sl.notes_slide.notes_text_frame.text = "Speaker note: isolated worker subprocess"
    prs.save(str(path))


def _make_zip_bomb_and_slip(bomb_path: Path, slip_path: Path) -> None:
    # 70MB 零字节压缩成 ~70KB，触及 max_archive_mb=64 与 >60:1 压缩比拦截
    with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\x00" * (70 * 1024 * 1024))
    with zipfile.ZipFile(slip_path, "w") as z:
        z.writestr("../../escaped.txt", "evil traversal")


def _make_wav(path: Path) -> None:
    rate = 8000
    n = 1600
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            val = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            frames.extend(struct.pack("<h", val))
        w.writeframes(bytes(frames))


def generate_all() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _make_exif_gps_jpeg(OUT / "exif_gps_sample.jpg")
    _make_png(400, 3600, OUT / "tall_screenshot.png")
    _make_pixel_bomb_png(OUT / "pixel_bomb.png")
    (OUT / "spoofed_html.png").write_text("<html><script>alert(1)</script><body><p>Hello Spoofed</p></body></html>", encoding="utf-8")
    _make_minimal_pdf(OUT / "scanned_empty.pdf", text="")
    _make_minimal_pdf(OUT / "js_active.pdf", text="Normal PDF Text", extra_dict="/OpenAction << /S /JavaScript /JS (app.alert('xss')) >>")
    (OUT / "malformed.pdf").write_bytes(b"%PDF-1.4\n\x00\xffcorrupt_body_no_xref")
    _make_docx_sample(OUT / "sample_notes.docx")
    (OUT / "malformed.docx").write_bytes(b"PK\x03\x04corrupted_not_valid_zip_table")
    _make_xlsm_macro(OUT / "macro_links.xlsm")
    _make_pptx_sample(OUT / "notes_slides.pptx")
    (OUT / "sample.ipynb").write_text(json.dumps({
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": [
            {"cell_type": "markdown", "source": ["# Analysis Notebook"]},
            {"cell_type": "code", "source": ["print(1 + 1)"], "outputs": [{"output_type": "display_data", "data": {"text/plain": ["2"], "image/png": "AAAA" * 500}}]},
        ],
    }), encoding="utf-8")
    (OUT / "sample.csv").write_text("id,score,active\n1,98.5,true\n2,77.0,false\n", encoding="utf-8")
    _make_zip_bomb_and_slip(OUT / "zip_bomb.zip", OUT / "zip_slip.zip")
    _make_wav(OUT / "sample_tone.wav")
    (OUT / "corrupt_video.mp4").write_bytes(b"\x00\x00\x00\x18ftypisomcorrupt_atoms_invalid")


if __name__ == "__main__":
    generate_all()
    print("Generated fixtures in", OUT)
