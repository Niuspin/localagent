"""会话产物仓储与 SQLite 索引（M0）。

每个会话在 `data/artifacts/<session_id>/` 下存放独立产物文件（图片、表格、图表、报告、音频、文档提取等），
由 `data/artifacts/index.db`（WAL 模式 SQLite）提供结构化元数据检索、去重摘要与生命周期配额清理。
所有持久化与读取均校验 session_id / artifact_id 与真实路径边界，防止目录穿越与跨会话访问。
"""
from __future__ import annotations

import json
import mimetypes
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .journal import atomic_write, sha256_bytes

SID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
AID_OK = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_EXT_OK = re.compile(r"^\.[a-z0-9]{1,10}$")

_MIME_EXT_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
    "application/pdf": ".pdf",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
}


def _safe_ext(filename: str = "", mime: str = "") -> str:
    ext = Path(filename).suffix.lower() if filename else ""
    if ext and _EXT_OK.match(ext):
        return ext
    m = (mime or "").split(";")[0].strip().lower()
    if m in _MIME_EXT_MAP:
        return _MIME_EXT_MAP[m]
    guessed = mimetypes.guess_extension(m) if m else ""
    if guessed and _EXT_OK.match(guessed.lower()):
        return guessed.lower()
    return ".bin"


def _guess_mime(filename: str = "", explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.split(";")[0].strip().lower()
    if filename:
        g, _ = mimetypes.guess_type(filename)
        if g:
            return g.lower()
    return "application/octet-stream"


@dataclass
class Artifact:
    id: str
    session_id: str
    kind: str
    title: str
    path: str                                       # 相对 data/artifacts/ 的安全路径：<sid>/<filename>
    mime: str
    size: int
    sha256: str
    created_by: str = "main"
    preview: dict[str, Any] | str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["download_url"] = f"/api/artifacts/{self.id}"
        return d


class ArtifactStore:
    """线程安全的 Artifact 落盘 + SQLite WAL 索引管理器。"""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._root_real = self.dir.resolve()
        self._db_path = self.dir / "index.db"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT 'main',
                    preview_json TEXT NOT NULL DEFAULT 'null',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, created_at DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_sha ON artifacts(session_id, sha256)")
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _validate_sid(sid: str) -> str:
        if not sid or not SID_OK.match(sid):
            raise ValueError(f"非法的 session_id: {sid!r}")
        return sid

    @staticmethod
    def _validate_aid(aid: str) -> str:
        if not aid or not AID_OK.match(aid):
            raise ValueError(f"非法的 artifact_id: {aid!r}")
        return aid

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        try:
            preview = json.loads(row["preview_json"])
        except ValueError:
            preview = None
        try:
            meta = json.loads(row["meta_json"])
            if not isinstance(meta, dict):
                meta = {}
        except ValueError:
            meta = {}
        return Artifact(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            title=row["title"],
            path=row["rel_path"],
            mime=row["mime"],
            size=int(row["size"]),
            sha256=row["sha256"],
            created_by=row["created_by"],
            preview=preview,
            meta=meta,
            created_at=float(row["created_at"]),
        )

    def resolve_path(self, artifact: Artifact) -> Path | None:
        """安全解析产物真实路径，确保严格位于 artifacts 根目录内部。"""
        if not artifact or not artifact.path:
            return None
        candidate = (self.dir / artifact.path).resolve()
        try:
            candidate.relative_to(self._root_real)
        except ValueError:
            return None
        return candidate

    def save_bytes(
        self,
        session_id: str,
        data: bytes,
        *,
        kind: str = "file",
        title: str = "",
        filename: str = "",
        mime: str | None = None,
        created_by: str = "main",
        preview: Any = None,
        meta: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> Artifact:
        sid = self._validate_sid(session_id)
        aid = self._validate_aid(artifact_id) if artifact_id else f"art_{uuid.uuid4().hex[:12]}"
        resolved_mime = _guess_mime(filename, mime)
        ext = _safe_ext(filename, resolved_mime)
        sdir = self.dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        fname = f"{aid}{ext}"
        target = (sdir / fname).resolve()
        target.relative_to(sdir.resolve())
        atomic_write(target, data)

        rel_path = f"{sid}/{fname}"
        digest = sha256_bytes(data)
        now = time.time()
        clean_title = (title or filename or fname)[:200]
        clean_kind = re.sub(r"[^a-z0-9_-]", "", (kind or "file").lower())[:32] or "file"
        clean_by = (created_by or "main")[:64]
        clean_meta = dict(meta) if isinstance(meta, dict) else {}

        art = Artifact(
            id=aid,
            session_id=sid,
            kind=clean_kind,
            title=clean_title,
            path=rel_path,
            mime=resolved_mime,
            size=len(data),
            sha256=digest,
            created_by=clean_by,
            preview=preview,
            meta=clean_meta,
            created_at=now,
        )
        with self._lock:
            self.db.execute(
                """INSERT OR REPLACE INTO artifacts
                   (id, session_id, kind, title, rel_path, mime, size, sha256, created_by, preview_json, meta_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    art.id,
                    art.session_id,
                    art.kind,
                    art.title,
                    art.path,
                    art.mime,
                    art.size,
                    art.sha256,
                    art.created_by,
                    json.dumps(art.preview, ensure_ascii=False),
                    json.dumps(art.meta, ensure_ascii=False),
                    art.created_at,
                ),
            )
            self.db.commit()
        return art

    def register_file(
        self,
        session_id: str,
        src_path: Path,
        *,
        kind: str = "file",
        title: str = "",
        mime: str | None = None,
        created_by: str = "main",
        preview: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        p = Path(src_path)
        if not p.is_file():
            raise FileNotFoundError(f"产物源文件不存在: {p}")
        data = p.read_bytes()
        merged_meta = dict(meta or {})
        merged_meta.setdefault("source_name", p.name)
        return self.save_bytes(
            session_id,
            data,
            kind=kind,
            title=title or p.name,
            filename=p.name,
            mime=mime,
            created_by=created_by,
            preview=preview,
            meta=merged_meta,
        )

    def get(self, artifact_id: str) -> Artifact | None:
        if not artifact_id or not AID_OK.match(artifact_id):
            return None
        with self._lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._row_to_artifact(row) if row else None

    def read_bytes(self, artifact_id: str) -> bytes | None:
        art = self.get(artifact_id)
        if not art:
            return None
        p = self.resolve_path(art)
        if not p or not p.is_file():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def list(
        self,
        session_id: str | None = None,
        *,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[Artifact]:
        lim = max(1, min(1000, int(limit)))
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            if not SID_OK.match(session_id):
                return []
            clauses.append("session_id=?")
            params.append(session_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM artifacts{where} ORDER BY created_at DESC LIMIT ?"
        params.append(lim)
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def total_bytes(self, session_id: str | None = None) -> int:
        with self._lock:
            if session_id is not None:
                if not SID_OK.match(session_id):
                    return 0
                row = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM artifacts WHERE session_id=?", (session_id,)).fetchone()
            else:
                row = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM artifacts").fetchone()
        return int(row[0]) if row else 0

    def delete(self, artifact_id: str) -> bool:
        if not artifact_id or not AID_OK.match(artifact_id):
            return False
        art = self.get(artifact_id)
        if not art:
            return False
        p = self.resolve_path(art)
        if p:
            try:
                p.unlink()
            except OSError:
                pass
        with self._lock:
            cur = self.db.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
            self.db.commit()
            return cur.rowcount > 0

    def prune_max_bytes(self, max_bytes: int, *, session_id: str | None = None) -> int:
        """按最旧优先淘汰超额产物，返回被删除的产物数量。"""
        if max_bytes <= 0:
            return 0
        removed = 0
        with self._lock:
            current = self.total_bytes(session_id)
            if current <= max_bytes:
                return 0
            if session_id is not None:
                if not SID_OK.match(session_id):
                    return 0
                rows = self.db.execute(
                    "SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at ASC",
                    (session_id,),
                ).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM artifacts ORDER BY created_at ASC").fetchall()
            for row in rows:
                if current <= max_bytes:
                    break
                art = self._row_to_artifact(row)
                p = self.resolve_path(art)
                if p:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                self.db.execute("DELETE FROM artifacts WHERE id=?", (art.id,))
                current -= art.size
                removed += 1
            self.db.commit()
        return removed

    def drop_session(self, session_id: str) -> int:
        """删除某个会话的全部产物与索引。"""
        if not session_id or not SID_OK.match(session_id):
            return 0
        with self._lock:
            cur = self.db.execute("DELETE FROM artifacts WHERE session_id=?", (session_id,))
            self.db.commit()
            removed = cur.rowcount
        sdir = (self.dir / session_id).resolve()
        try:
            sdir.relative_to(self._root_real)
            if sdir.is_dir():
                shutil.rmtree(sdir, ignore_errors=True)
        except (ValueError, OSError):
            pass
        return removed

    def sweep(self, live_sids: set[str]) -> int:
        """清掉已不存在会话遗留的产物目录与索引记录（启动时调用）。"""
        removed = 0
        with self._lock:
            rows = self.db.execute("SELECT DISTINCT session_id FROM artifacts").fetchall()
            orphan_sids = {r["session_id"] for r in rows if r["session_id"] not in live_sids}
            for sid in orphan_sids:
                removed += self.drop_session(sid)
        try:
            for d in list(self.dir.iterdir()):
                if d.is_dir() and SID_OK.match(d.name) and d.name not in live_sids:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
        return removed
