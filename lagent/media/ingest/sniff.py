"""按魔数嗅探文件真实类型，抵御扩展名伪装。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

_IMAGE_MIMES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
}


def _sniff_zip(path: Path | None, ext: str) -> tuple[str, str]:
    if path is not None and path.is_file():
        try:
            with zipfile.ZipFile(path, "r") as z:
                names = set(z.namelist()[:250])
                if "mimetype" in names:
                    try:
                        mt = z.read("mimetype")[:128].decode("ascii", "ignore").strip()
                        if "epub" in mt:
                            return "epub", "application/epub+zip"
                    except Exception:
                        pass
                if any(n.startswith("word/") or n == "[Content_Types].xml" and any(x.startswith("word/") for x in names) for n in names):
                    return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if any(n.startswith("ppt/") for n in names):
                    return "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                if "META-INF/container.xml" in names:
                    return "epub", "application/epub+zip"
                return "archive", "application/zip"
        except Exception:
            # 损坏或畸形 zip：若带 OOXML 扩展名则交由对应 office 校验拒绝，否则视作 archive
            if ext in (".docx",):
                return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if ext in (".xlsx", ".xlsm"):
                return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if ext in (".pptx",):
                return "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if ext == ".epub":
                return "epub", "application/epub+zip"
            return "archive", "application/zip"
    ext_map = {
        ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ".xlsm": ("xlsx", "application/vnd.ms-excel.sheet.macroEnabled.12"),
        ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ".epub": ("epub", "application/epub+zip"),
    }
    return ext_map.get(ext, ("archive", "application/zip"))


def _looks_utf8_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # 可能末尾截断多字节 UTF-8 字符
        for cut in range(1, min(4, len(head))):
            try:
                head[:-cut].decode("utf-8")
                return True
            except UnicodeDecodeError:
                continue
        return False


def sniff_magic(head: bytes, filename: str = "", *, path: Path | None = None) -> tuple[str, str]:
    """根据魔数嗅探真实 `(kind, mime)`；仅对文本或容器做安全扩展名细分。"""
    ext = Path(filename or "").suffix.lower()
    h = head or b""

    # 1. 图片魔数
    for sig, mime in _IMAGE_MIMES.items():
        if h.startswith(sig):
            return "image", mime
    if len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "image", "image/webp"
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
            return "image", "image/heic"
        if brand in (b"M4A ", b"M4B "):
            return "audio", "audio/mp4"
        return "video", "video/mp4"

    # 2. PDF
    if h.lstrip().startswith(b"%PDF-"):
        return "pdf", "application/pdf"

    # 3. 音频/视频容器
    if len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if h.startswith(b"fLaC"):
        return "audio", "audio/flac"
    if h.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if h.startswith(b"ID3") or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and ext in (".mp3", ".aac", "")):
        # 注意防止 UTF-8 BOM/文本误判：若含纯 ASCII/UTF8 则先排掉
        if h.startswith(b"ID3") or not _looks_utf8_text(h[:512]):
            return "audio", "audio/mpeg"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        if b"webm" in h[:128].lower():
            return "video", "video/webm"
        return "video", "video/x-matroska"

    # 4. Parquet & 压缩包 / OOXML / EPUB
    if h.startswith(b"PAR1"):
        return "parquet", "application/vnd.apache.parquet"
    if h.startswith(b"\x1f\x8b"):
        return "archive", "application/gzip"
    if len(h) > 265 and h[257:262] == b"ustar":
        return "archive", "application/x-tar"
    if h.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _sniff_zip(path, ext)

    # 5. 文本类与结构化文本（避免后缀欺骗将脚本/HTML 伪装成图片）
    if _looks_utf8_text(h):
        sample = h.decode("utf-8", "replace").lstrip("\ufeff \t\r\n")
        low = sample[:512].lower()

        if "mime-version:" in low and "multipart/related" in low:
            return "mhtml", "multipart/related"
        if ext == ".ipynb" or (sample.startswith("{") and '"cells"' in sample and '"nbformat"' in sample):
            if ext == ".ipynb" or '"nbformat"' in sample:
                return "ipynb", "application/x-ipynb+json"
        if low.startswith("<!doctype html") or low.startswith("<html") or (ext in (".html", ".htm") and "<" in low):
            return "html", "text/html"
        if ext in (".yaml", ".yml"):
            return "yaml", "application/yaml"
        if ext == ".tsv":
            return "tsv", "text/tab-separated-values"
        if ext == ".csv":
            return "csv", "text/csv"
        if ext == ".jsonl":
            return "jsonl", "application/x-ndjson"
        if ext == ".json" or (sample.startswith(("{", "[")) and _is_valid_json_prefix(sample)):
            return "json", "application/json"
        if ext in (".md", ".markdown"):
            return "md", "text/markdown"
        return "text", "text/plain"

    # 6. 无法识别的二进制（即使扩展名写为 .png / .pdf / .docx 也不按该扩展名伪造解析）
    return "binary", "application/octet-stream"


def _is_valid_json_prefix(txt: str) -> bool:
    if len(txt) < 2048:
        try:
            json.loads(txt)
            return True
        except Exception:
            return False
    return True
