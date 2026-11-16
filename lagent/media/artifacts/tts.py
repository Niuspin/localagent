"""可选本地 TTS 语音输出（M4 §4.2，默认关闭，检测到 piper 时可用）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities import _find_exe


def detect_tts(cfg: Any = None, bin_dir: Path | None = None) -> dict[str, Any]:
    """探测本地 piper TTS 可执行文件与启用开关状态（默认关闭）。"""
    enabled = False
    if isinstance(cfg, dict):
        media_d = cfg.get("media") if isinstance(cfg.get("media"), dict) else cfg
        enabled = bool(media_d.get("tts_enabled", False))
    elif cfg is not None:
        enabled = bool(cfg.get("media", "tts_enabled", default=False))
    piper_bin = _find_exe(("piper",), bin_dir)
    return {
        "enabled": enabled,
        "available": bool(enabled and piper_bin),
        "binary": piper_bin,
    }


def synthesize_tts_optional(text: str, *, cfg: Any = None, bin_dir: Path | None = None) -> dict[str, Any]:
    """在开启且存在 piper 时返回语音合成参数，否则明确提示不可用。"""
    st = detect_tts(cfg, bin_dir)
    if not st["enabled"]:
        return {"ok": False, "available": False, "error": "本地 TTS 默认关闭（可在 config.json 设置 media.tts_enabled=true）"}
    if not st["binary"]:
        return {"ok": False, "available": False, "error": "未检测到本地 piper 二进制文件，已跳过语音合成"}
    clean = str(text or "").strip()
    if not clean:
        return {"ok": False, "available": True, "error": "合成文本为空"}
    return {"ok": False, "available": True, "binary": st["binary"], "error": "当前未配置 piper 语音模型路径"}
