"""统一多模态输入摄取入口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities import detect_capabilities
from .handlers import ingest_path_direct
from .models import IngestResult
from .sandbox import run_in_restricted_subprocess, run_in_restricted_subprocess_async
from .sniff import sniff_magic


def _extract_media_cfg(cfg: Any) -> dict[str, Any]:
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return dict(cfg.get("media") if isinstance(cfg.get("media"), dict) else cfg)
    try:
        return dict(cfg.get("media", default={}) or {})
    except Exception:
        return {}


def ingest_file(
    path: str | Path,
    *,
    cfg: Any = None,
    engine: Any = None,
    caps: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    sandboxed: bool = True,
) -> IngestResult:
    """同步摄取单个媒体/文档文件（默认走受限子进程）。"""
    p = Path(path)
    m_cfg = _extract_media_cfg(cfg)
    eff_caps = caps if caps is not None else detect_capabilities(engine=engine, cfg=cfg)
    timeout_s = float(m_cfg.get("subprocess_timeout_s") or 30.0)
    if sandboxed:
        return run_in_restricted_subprocess(p, cfg=m_cfg, caps=eff_caps, options=options, timeout_s=timeout_s)
    return ingest_path_direct(p, cfg=m_cfg, caps=eff_caps, options=options)


async def ingest_file_async(
    path: str | Path,
    *,
    cfg: Any = None,
    engine: Any = None,
    caps: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    sandboxed: bool = True,
) -> IngestResult:
    p = Path(path)
    m_cfg = _extract_media_cfg(cfg)
    eff_caps = caps if caps is not None else detect_capabilities(engine=engine, cfg=cfg)
    timeout_s = float(m_cfg.get("subprocess_timeout_s") or 30.0)
    if sandboxed:
        return await run_in_restricted_subprocess_async(p, cfg=m_cfg, caps=eff_caps, options=options, timeout_s=timeout_s)
    return ingest_path_direct(p, cfg=m_cfg, caps=eff_caps, options=options)


__all__ = [
    "IngestResult",
    "ingest_file",
    "ingest_file_async",
    "ingest_path_direct",
    "run_in_restricted_subprocess",
    "run_in_restricted_subprocess_async",
    "sniff_magic",
]
