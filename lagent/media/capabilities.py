"""多模态能力协商：探测模型模态与可选本地二进制/解析库，生成前端提示与处理计划。"""
from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any


def _has_mod(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _find_exe(names: tuple[str, ...], bin_dir: Path | None = None) -> str | None:
    dirs: list[Path] = []
    if bin_dir:
        dirs.append(Path(bin_dir))
    for name in names:
        candidates = [name + ".exe", name] if os.name == "nt" else [name]
        for d in dirs:
            if not d.is_dir():
                continue
            for c in candidates:
                p = d / c
                if p.is_file():
                    return str(p)
                for sub in d.glob(f"*/{c}"):
                    if sub.is_file():
                        return str(sub)
        for c in candidates:
            found = shutil.which(c)
            if found:
                return found
    return None


def detect_capabilities(engine: Any = None, cfg: Any = None, bin_dir: Path | None = None) -> dict[str, Any]:
    """探测并返回当前运行时支持的多模态输入能力矩阵。"""
    bdir = bin_dir or (getattr(engine, "bin_dir", None) if engine else None)
    vision = bool(getattr(engine, "vision", False)) if engine else False
    audio_model = bool(getattr(engine, "audio", False)) if engine else False

    whisper_bin = _find_exe(("whisper-cli", "whisper", "whisper-main"), bdir)
    ffmpeg_bin = _find_exe(("ffmpeg",), bdir)
    ffprobe_bin = _find_exe(("ffprobe",), bdir)
    tesseract_bin = _find_exe(("tesseract",), bdir)

    has_rapidocr = _has_mod("rapidocr_onnxruntime")
    has_pytesseract = _has_mod("pytesseract") and bool(tesseract_bin)
    if has_rapidocr:
        ocr_mode = "rapidocr"
    elif tesseract_bin:
        ocr_mode = "tesseract"
    elif vision:
        ocr_mode = "vlm"
    else:
        ocr_mode = "none"

    has_pdfium = _has_mod("pypdfium2")
    has_pdfplumber = _has_mod("pdfplumber")
    has_pypdf = _has_mod("pypdf")
    has_pil = _has_mod("PIL")

    if isinstance(cfg, dict):
        media_d = cfg.get("media") if isinstance(cfg.get("media"), dict) else cfg
        recorder_enabled = bool(media_d.get("recorder_enabled", False))
        tts_enabled = bool(media_d.get("tts_enabled", False))
    elif cfg is not None:
        recorder_enabled = bool(cfg.get("media", "recorder_enabled", default=False))
        tts_enabled = bool(cfg.get("media", "tts_enabled", default=False))
    else:
        recorder_enabled = False
        tts_enabled = False
    piper_bin = _find_exe(("piper",), bdir)

    return {
        "vision": vision,
        "audio_model": audio_model,
        "audio": bool(audio_model or whisper_bin),
        "whisper": whisper_bin,
        "ffmpeg": ffmpeg_bin,
        "ffprobe": ffprobe_bin,
        "ocr": ocr_mode,
        "has_pytesseract": has_pytesseract,
        "pdf_render": "pypdfium2" if has_pdfium else "none",
        "pdf_text": has_pdfplumber or has_pypdf,
        "pillow": has_pil,
        "parquet": _has_mod("pyarrow"),
        "lxml": _has_mod("lxml"),
        "yaml": _has_mod("yaml"),
        "chart_svg": True,
        "matplotlib": _has_mod("matplotlib"),
        "tts": {"enabled": tts_enabled, "available": bool(tts_enabled and piper_bin), "binary": piper_bin},
        "office": {
            "docx": _has_mod("docx"),
            "xlsx": _has_mod("openpyxl"),
            "pptx": _has_mod("pptx"),
            "pdfplumber": has_pdfplumber,
            "pypdf": has_pypdf,
        },
        "recorder_enabled": recorder_enabled,
    }


def explain_ingest_plan(filename: str, mime: str, caps: dict[str, Any], *, kind: str = "") -> dict[str, Any]:
    """给出单文件的处理方式标签与前端可见说明（直读/VLM/PDF渲染/OCR/转写/抽样/不支持）。"""
    ext = Path(filename or "").suffix.lower()
    k = (kind or "").lower()
    m = (mime or "").lower()

    if k == "image" or m.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif"):
        if caps.get("vision"):
            return {"handler": "vlm", "label": "VLM 视觉", "supported": True, "note": "剥离 EXIF 后按分辨率直接送入视觉模型"}
        if caps.get("ocr") in ("rapidocr", "tesseract"):
            return {"handler": "ocr", "label": f"OCR ({caps['ocr']})", "supported": True, "note": "当前无 mmproj 视觉投影，将用本地 OCR 抽取文字"}
        return {"handler": "unsupported", "label": "缺视觉/OCR", "supported": False, "note": "未加载 mmproj 视觉模型且无本地 OCR，模型无法看到图片内容"}

    if k == "pdf" or m == "application/pdf" or ext == ".pdf":
        if caps.get("pdf_render") != "none" and (caps.get("vision") or caps.get("ocr") in ("rapidocr", "tesseract")):
            return {"handler": "pdf_render", "label": "文本+扫描页渲染", "supported": True, "note": "抽取文本层；扫描页自动渲染后交由 VLM/OCR"}
        if caps.get("pdf_text"):
            return {"handler": "pdf_text", "label": "PDF 文本抽取", "supported": True, "note": "受限子进程抽取 PDF 文本与表格"}
        return {"handler": "unsupported", "label": "缺 PDF 库", "supported": False, "note": "需安装 pdfplumber 或 pypdf"}

    if k in ("docx", "xlsx", "pptx") or ext in (".docx", ".xlsx", ".xlsm", ".pptx"):
        return {"handler": "office", "label": "文档结构化", "supported": True, "note": "剥离宏与外链后提取正文、表格、批注与备注"}

    if k in ("csv", "tsv", "json", "jsonl", "yaml", "parquet") or ext in (".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".parquet"):
        if (k == "parquet" or ext == ".parquet") and not caps.get("parquet"):
            return {"handler": "unsupported", "label": "缺 pyarrow", "supported": False, "note": "读取 Parquet 需安装 pyarrow"}
        return {"handler": "data", "label": "Schema 抽样", "supported": True, "note": "流式推断列 Schema、统计分布与样本预览"}

    if k in ("ipynb", "epub", "html", "mhtml") or ext in (".ipynb", ".epub", ".html", ".htm", ".mhtml", ".mht"):
        return {"handler": "notebook", "label": "章节/单元解析", "supported": True, "note": "切分章节或单元并剥离脚本/大二进制"}

    if k == "archive" or ext in (".zip", ".tar", ".gz", ".tgz"):
        return {"handler": "archive", "label": "仅列目录", "supported": True, "note": "受限校验炸弹与路径穿越，仅列目录与按需查看"}

    if k == "audio" or m.startswith("audio/") or ext in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
        if caps.get("audio_model"):
            return {"handler": "audio", "label": "原生音频输入", "supported": True, "note": "引擎支持音频模态直读"}
        if caps.get("whisper"):
            return {"handler": "audio", "label": "Whisper 转写", "supported": True, "note": "受限调用本地 whisper 转写文本"}
        return {"handler": "unsupported", "label": "不可转写", "supported": False, "note": "未检测到 whisper.cpp 且当前模型不支持音频模态"}

    if k == "video" or m.startswith("video/") or ext in (".mp4", ".mkv", ".webm", ".mov"):
        if caps.get("ffmpeg"):
            return {"handler": "video", "label": "关键帧抽帧", "supported": True, "note": "ffmpeg 抽关键帧 + 可选音轨转写"}
        return {"handler": "unsupported", "label": "缺 ffmpeg", "supported": False, "note": "未检测到 ffmpeg，无法抽取视频关键帧"}

    if k in ("text", "markdown", "md") or ext in (".txt", ".md", ".py", ".js", ".ts", ".css", ".log", ".toml", ".ini"):
        return {"handler": "direct", "label": "直读文本", "supported": True, "note": "工作区受控直读"}

    return {"handler": "direct", "label": "文件引用", "supported": True, "note": "放入工作区 uploads/ 供按需检测"}
