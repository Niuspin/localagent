"""多模态统一摄取结果模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IngestResult:
    kind: str
    mime: str
    path: str = ""
    title: str = ""
    text_blocks: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cost_estimate: dict[str, Any] = field(default_factory=dict)
    tainted: bool = True
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IngestResult":
        return cls(
            kind=str(raw.get("kind") or "unknown"),
            mime=str(raw.get("mime") or "application/octet-stream"),
            path=str(raw.get("path") or ""),
            title=str(raw.get("title") or ""),
            text_blocks=list(raw.get("text_blocks") or []),
            images=list(raw.get("images") or []),
            tables=list(raw.get("tables") or []),
            meta=dict(raw.get("meta") or {}),
            warnings=[str(w) for w in (raw.get("warnings") or [])],
            cost_estimate=dict(raw.get("cost_estimate") or {}),
            tainted=bool(raw.get("tainted", True)),
            ok=bool(raw.get("ok", True)),
            error=raw.get("error"),
        )

    def render_summary(self, *, max_chars: int = 8000) -> str:
        """渲染紧凑渐进式摘要（表头+片段），用于上下文预算控制。"""
        if not self.ok:
            return f"[摄取失败 {self.title or self.path} ({self.kind}/{self.mime})]: {self.error or 'unknown error'}"

        parts: list[str] = [f"[{self.title or self.path or self.kind} · {self.kind} ({self.mime})]"]
        if self.warnings:
            parts.append("安全/降级提示: " + "; ".join(self.warnings))
        if self.meta:
            compact_meta = {k: v for k, v in self.meta.items() if k in (
                "size_bytes", "page_count", "width", "height", "exif_stripped", "macro_stripped",
                "external_links_stripped", "js_stripped", "rows", "columns", "entry_count", "duration_s",
            )}
            if compact_meta:
                parts.append("元信息: " + ", ".join(f"{k}={v}" for k, v in compact_meta.items()))

        for tbl in self.tables[:6]:
            name = tbl.get("name") or "table"
            cols = tbl.get("columns") or []
            rows = tbl.get("rows") or []
            total = tbl.get("total_rows", len(rows))
            parts.append(f"\n### 表格 {name}（共 {total} 行，显示 {len(rows)} 行）")
            if cols:
                parts.append("| " + " | ".join(str(c) for c in cols) + " |")
                parts.append("|" + "---|" * len(cols))
            for r in rows[:15]:
                parts.append("| " + " | ".join(str(c).replace("\n", " ")[:120] for c in r) + " |")

        for blk in self.text_blocks:
            hdr = blk.get("title")
            txt = str(blk.get("text") or "").strip()
            if hdr:
                parts.append(f"\n### {hdr}")
            if txt:
                parts.append(txt)

        if self.images:
            parts.append(f"\n[附带渲染/切片图片: {len(self.images)} 张]")

        full = "\n".join(parts)
        if len(full) > max_chars:
            return full[:max_chars] + f"\n…[摘要已截断，完整内容共 {len(full)} 字符；可用 read_tool_output 或分段读取获取]"
        return full

    def render_full_text(self) -> str:
        """导出完整文本供 OutputStore 句柄落盘分页检索。"""
        return self.render_summary(max_chars=500_000)
