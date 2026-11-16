"""LocalAgent 多模态输入与能力协商包（M3）。"""
from __future__ import annotations

from .capabilities import detect_capabilities, explain_ingest_plan
from .ingest import (
    IngestResult,
    ingest_file,
    ingest_file_async,
    ingest_path_direct,
    run_in_restricted_subprocess,
    run_in_restricted_subprocess_async,
    sniff_magic,
)

__all__ = [
    "IngestResult",
    "detect_capabilities",
    "explain_ingest_plan",
    "ingest_file",
    "ingest_file_async",
    "ingest_path_direct",
    "run_in_restricted_subprocess",
    "run_in_restricted_subprocess_async",
    "sniff_magic",
]
