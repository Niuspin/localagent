"""Artifact 打包归档器（M4 §4.2）：多个产物 + MANIFEST.json 打包为 Zip 并登记为 Artifact。"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

from ...artifacts import Artifact, ArtifactStore

MAX_BUNDLE_UNCOMPRESSED = 128 * 1024 * 1024


def _safe_entry_name(name: str, fallback: str) -> str:
    clean = re.sub(r"[^\w.\-\u4e00-\u9fff ]", "_", Path(name or fallback).name).strip(" .")
    return clean[:120] or fallback


def create_artifact_bundle(
    store: ArtifactStore,
    session_id: str,
    artifact_ids: list[str] | None = None,
    *,
    workspace_files: list[tuple[str, bytes]] | None = None,
    title: str = "Artifact Bundle",
    created_by: str = "main",
    max_total_bytes: int = MAX_BUNDLE_UNCOMPRESSED,
) -> tuple[Artifact, bytes]:
    """将同会话下的指定产物与可选工作区文件打包为 Zip + MANIFEST.json，返回 `(Artifact, zip_bytes)`。"""
    if artifact_ids is not None:
        selected: list[Artifact] = []
        seen_ids: set[str] = set()
        for aid in artifact_ids:
            aid_s = str(aid).strip()
            if not aid_s or aid_s in seen_ids:
                continue
            art = store.get(aid_s)
            if art is None:
                raise KeyError(f"产物不存在: {aid_s}")
            if art.session_id != session_id:
                raise PermissionError(f"禁止跨会话打包产物: {aid_s}")
            if art.kind == "bundle":
                continue
            seen_ids.add(aid_s)
            selected.append(art)
    else:
        selected = [a for a in store.list(session_id, limit=200) if a.kind != "bundle"]

    ws_items = list(workspace_files or [])
    if not selected and not ws_items:
        raise ValueError("没有可打包的产物或文件")

    total_bytes = 0
    used_names: set[str] = set()
    manifest_items: list[dict[str, Any]] = []
    buf = io.BytesIO()

    def unique_archive_path(raw_name: str, fallback: str) -> str:
        base = _safe_entry_name(raw_name, fallback)
        stem = Path(base).stem
        ext = Path(base).suffix
        cand = f"files/{base}"
        idx = 2
        while cand.lower() in used_names:
            cand = f"files/{stem}_{idx}{ext}"
            idx += 1
        used_names.add(cand.lower())
        return cand

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for art in selected:
            raw = store.read_bytes(art.id)
            if raw is None:
                continue
            total_bytes += len(raw)
            if total_bytes > max_total_bytes:
                raise ValueError("打包总大小超过安全上限")
            orig_name = str((art.meta or {}).get("source_name") or art.title or Path(art.path).name)
            if not Path(orig_name).suffix and Path(art.path).suffix:
                orig_name += Path(art.path).suffix
            arc = unique_archive_path(orig_name, Path(art.path).name)
            zf.writestr(arc, raw)
            manifest_items.append({
                "id": art.id,
                "kind": art.kind,
                "title": art.title,
                "mime": art.mime,
                "size": len(raw),
                "sha256": art.sha256,
                "archive_path": arc,
                "created_by": art.created_by,
            })

        for rel_name, data in ws_items:
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise ValueError("打包总大小超过安全上限")
            arc = unique_archive_path(rel_name, "file.bin")
            zf.writestr(arc, data)
            manifest_items.append({
                "id": None,
                "kind": "workspace_file",
                "title": rel_name,
                "mime": "application/octet-stream",
                "size": len(data),
                "archive_path": arc,
            })

        if not manifest_items:
            raise ValueError("所选产物文件内容缺失，无法生成打包")

        manifest = {
            "version": 1,
            "session_id": session_id,
            "title": title or "Artifact Bundle",
            "created_at": time.time(),
            "count": len(manifest_items),
            "items": manifest_items,
        }
        zf.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    zip_bytes = buf.getvalue()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", (title or "bundle")).strip("-")[:48] or "bundle"
    bundle_art = store.save_bytes(
        session_id,
        zip_bytes,
        kind="bundle",
        title=title or f"打包产物 ({len(manifest_items)} 项)",
        filename=f"{slug}.zip",
        mime="application/zip",
        created_by=created_by,
        preview={"count": len(manifest_items), "items": manifest_items[:20]},
        meta={"bundle_count": len(manifest_items), "source_name": f"{slug}.zip"},
    )
    return bundle_art, zip_bytes
