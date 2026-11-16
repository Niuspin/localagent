"""多模态输出与产物工具（M4）：make_chart（SVG 图表）、bundle_artifacts（打包归档）与统一 Artifact 注册。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import security as sec
from ..media.artifacts import CHART_TYPES, create_artifact_bundle, make_chart_svg
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx
from .files import _ensure_parent, commit_bytes, err


def record_artifact(
    ctx: ToolCtx,
    *,
    kind: str,
    title: str,
    filename: str,
    data: bytes,
    mime: str | None = None,
    preview: Any = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """将工具生成的产物登记至 ArtifactStore、强制执行配额并推送 artifact_created 事件。"""
    store = getattr(ctx, "artifacts", None)
    if store is None:
        return None
    agent_id = str(getattr(ctx.session, "agent_id", "main") or "main")
    art = store.save_bytes(
        ctx.sid,
        data,
        kind=kind,
        title=title or filename,
        filename=filename,
        mime=mime,
        created_by=agent_id,
        preview=preview,
        meta=meta,
    )
    if getattr(ctx, "cfg", None) is not None:
        max_mb = int(ctx.cfg.get("media", "artifact_max_mb", default=512) or 512)
        store.prune_max_bytes(max_mb * 1024 * 1024, session_id=ctx.sid)
    d = art.to_dict()
    if getattr(ctx, "emit", None):
        ctx.emit("artifact_created", d)
    return d


def _slug_name(title: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", (title or fallback)).strip("-")[:48]
    return s or fallback


async def chart_preview(ctx: ToolCtx, a: dict) -> dict | None:
    ct = a.get("chart_type") or "bar"
    ttl = a.get("title") or ct
    target = a.get("path") or "(仅存为会话产物)"
    return {"kind": "text", "text": f"生成 {ct} SVG 图表: {ttl}\n输出目标: {target}"}


async def make_chart(ctx: ToolCtx, a: dict) -> ToolResult:
    chart_type = str(a.get("chart_type") or "bar").strip().lower()
    title = str(a.get("title") or "").strip()
    subtitle = str(a.get("subtitle") or "").strip()
    theme = str(a.get("theme") or "auto").strip().lower()
    try:
        svg_text, stats = make_chart_svg(
            chart_type,
            title=title,
            subtitle=subtitle,
            labels=a.get("labels"),
            series=a.get("series"),
            values=a.get("values"),
            points=a.get("points"),
            matrix=a.get("matrix"),
            x_labels=a.get("x_labels"),
            y_labels=a.get("y_labels"),
            x_label=str(a.get("x_label") or ""),
            y_label=str(a.get("y_label") or ""),
            theme=theme,
            width=int(a.get("width") or 720),
            height=int(a.get("height") or 440),
        )
    except (ValueError, TypeError) as e:
        return err(f"图表数据或参数有误: {e}")

    data = svg_text.encode("utf-8")
    meta: dict[str, Any] = {"chart": stats}
    raw_path = str(a.get("path") or "").strip()
    if raw_path:
        p = ctx.guard.resolve(raw_path, write=True)
        if p.suffix.lower() != ".svg":
            return err("make_chart 导出到工作区时路径扩展名必须为 .svg")
        if p.exists() and not a.get("overwrite"):
            return err(f"{ctx.guard.rel(p)} 已存在，如需覆盖请设置 overwrite=true")
        _ensure_parent(ctx, p)
        cmeta = commit_bytes(ctx, p, data, "write", f"生成图表 {ctx.guard.rel(p)}")
        meta.update(cmeta)
        fname = p.name
    else:
        fname = _slug_name(title, f"chart-{chart_type}") + ".svg"

    art = record_artifact(
        ctx,
        kind="chart",
        title=title or fname,
        filename=fname,
        data=data,
        mime="image/svg+xml",
        preview={"text": f"{chart_type} chart: {title or fname}", **stats},
        meta={**stats, **({"workspace_path": meta["path"]} if meta.get("path") else {})},
    )
    if art:
        meta["artifact"] = art
    msg = f"已生成 {chart_type} SVG 图表" + (f" {meta['path']}" if meta.get("path") else "") + f"（{len(data)} B）"
    if art:
        msg += f" · 产物 ID: {art['id']}"
    return ToolResult(msg, meta=meta)


async def bundle_artifacts(ctx: ToolCtx, a: dict) -> ToolResult:
    store = getattr(ctx, "artifacts", None)
    if store is None:
        return err("当前运行时未启用 ArtifactStore")

    ws_items: list[tuple[str, bytes]] = []
    for raw_rel in (a.get("files") or [])[:40]:
        rel_s = str(raw_rel or "").strip()
        if not rel_s:
            continue
        p = ctx.guard.resolve(rel_s, write=False)
        rel = ctx.guard.rel(p)
        sens = sec.sensitive_reason(rel)
        if sens:
            return err(f"拒绝打包敏感文件 {rel}（{sens}）")
        if not p.is_file():
            return err(f"要打包的工作区文件不存在: {rel}")
        raw = p.read_bytes()
        if len(raw) > ctx.max_bytes:
            return err(f"文件 {rel} 超过单文件大小上限")
        ws_items.append((rel, raw))

    title = str(a.get("title") or "Artifact Bundle").strip()
    art_ids = [str(x).strip() for x in (a.get("artifact_ids") or []) if str(x).strip()] if a.get("artifact_ids") else None
    agent_id = str(getattr(ctx.session, "agent_id", "main") or "main")
    try:
        art, zip_bytes = create_artifact_bundle(
            store,
            ctx.sid,
            art_ids,
            workspace_files=ws_items or None,
            title=title,
            created_by=agent_id,
        )
    except (ValueError, KeyError, PermissionError) as e:
        return err(str(e))

    if getattr(ctx, "cfg", None) is not None:
        max_mb = int(ctx.cfg.get("media", "artifact_max_mb", default=512) or 512)
        store.prune_max_bytes(max_mb * 1024 * 1024, session_id=ctx.sid)

    meta: dict[str, Any] = {}
    raw_path = str(a.get("path") or "").strip()
    if raw_path:
        p = ctx.guard.resolve(raw_path, write=True)
        if p.suffix.lower() != ".zip":
            return err("bundle_artifacts 输出到工作区时扩展名必须为 .zip")
        if p.exists() and not a.get("overwrite"):
            return err(f"{ctx.guard.rel(p)} 已存在，如需覆盖请设置 overwrite=true")
        _ensure_parent(ctx, p)
        cmeta = commit_bytes(ctx, p, zip_bytes, "write", f"打包产物 {ctx.guard.rel(p)}")
        meta.update(cmeta)
        art = store.update_meta(art.id, {"workspace_path": cmeta["path"]}) or art

    d = art.to_dict()
    if getattr(ctx, "emit", None):
        ctx.emit("artifact_created", d)
    meta["artifact"] = d
    cnt = int((art.meta or {}).get("bundle_count", 0))
    return ToolResult(
        f"已打包 {cnt} 项产物至 {art.title}（{len(zip_bytes)} B，含 MANIFEST.json）· 产物 ID: {art.id}",
        meta=meta,
    )


register(Tool(
    "make_chart",
    "零依赖生成高可读性 SVG 数据图表（bar/line/pie/scatter/heatmap，支持中文与深浅主题），自动登记为会话产物；可选写入工作区 .svg 文件。",
    obj({
        "chart_type": P("string", "图表类型", enum=list(CHART_TYPES)),
        "title": P("string", "图表标题"),
        "subtitle": P("string", "副标题（可选）"),
        "labels": {"type": "array", "description": "分类/X 轴标签数组", "items": {"type": "string"}},
        "series": {"type": "array", "description": "多系列数据 [{name, values}] 或散点 [{name, points}]"},
        "values": {"type": "array", "description": "单系列数值数组或 [{label, value}]"},
        "points": {"type": "array", "description": "散点图点集 [[x, y], ...] 或 [{x, y, label}]"},
        "matrix": {"type": "array", "description": "热力图二维数字矩阵 [[...], ...]"},
        "x_labels": {"type": "array", "description": "热力图 X 轴标签", "items": {"type": "string"}},
        "y_labels": {"type": "array", "description": "热力图 Y 轴标签", "items": {"type": "string"}},
        "x_label": P("string", "X 轴说明文字（可选）"),
        "y_label": P("string", "Y 轴说明文字（可选）"),
        "theme": P("string", "主题 auto | light | dark（默认 auto）", enum=["auto", "light", "dark"]),
        "width": P("integer", "SVG 宽度像素（默认 720）"),
        "height": P("integer", "SVG 高度像素（默认 440）"),
        "path": P("string", "可选：同步保存的工作区相对路径（扩展名 .svg）"),
        "overwrite": P("boolean", "允许覆盖已有 .svg 文件"),
    }, ["chart_type"]),
    make_chart,
    "write",
    group="artifacts",
    title=lambda a: f"生成图表 {a.get('title') or a.get('chart_type', '')}",
    preview=chart_preview,
    path_args=("path",),
))

register(Tool(
    "bundle_artifacts",
    "将当前会话生成的多个产物与/或指定工作区文件打包为含 MANIFEST.json 的 .zip 压缩包并登记为 Artifact。",
    obj({
        "title": P("string", "打包名称/标题（可选）"),
        "artifact_ids": {"type": "array", "description": "可选：要打包的产物 ID 列表；留空打包当前会话全部非包产物", "items": {"type": "string"}},
        "files": {"type": "array", "description": "可选：额外纳入打包的工作区相对路径列表", "items": {"type": "string"}},
        "path": P("string", "可选：同步保存的工作区 .zip 相对路径"),
        "overwrite": P("boolean", "允许覆盖已有 .zip 文件"),
    }, []),
    bundle_artifacts,
    "write",
    group="artifacts",
    title=lambda a: f"打包产物 {a.get('title') or ''}",
    path_args=("path", "files"),
))
