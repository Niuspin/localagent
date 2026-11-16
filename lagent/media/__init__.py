"""LocalAgent 多模态输入摄取、能力协商与产物输出包（M3 / M4）。"""
from __future__ import annotations

from .artifacts import (
    CHART_TYPES,
    EXPORT_FORMATS,
    build_artifact_preview,
    create_artifact_bundle,
    detect_tts,
    export_session,
    export_session_html,
    export_session_markdown,
    html_preview_csp,
    make_chart_svg,
    sanitize_svg_for_preview,
    synthesize_tts_optional,
)
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
    "CHART_TYPES",
    "EXPORT_FORMATS",
    "IngestResult",
    "build_artifact_preview",
    "create_artifact_bundle",
    "detect_capabilities",
    "detect_tts",
    "explain_ingest_plan",
    "export_session",
    "export_session_html",
    "export_session_markdown",
    "html_preview_csp",
    "ingest_file",
    "ingest_file_async",
    "ingest_path_direct",
    "make_chart_svg",
    "run_in_restricted_subprocess",
    "run_in_restricted_subprocess_async",
    "sanitize_svg_for_preview",
    "sniff_magic",
    "synthesize_tts_optional",
]
