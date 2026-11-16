"""网页磁盘缓存（WebCache）：支持条件请求（ETag / If-Modified-Since）、TTL、容量 LRU 淘汰与 noarchive 遵从。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class WebCache:
    def __init__(self, cache_dir: Path, *, default_ttl_h: float = 24.0, max_mb: float = 256.0):
        self.dir = Path(cache_dir)
        self.blobs = self.dir / "blobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.default_ttl_s = max(60.0, float(default_ttl_h) * 3600.0)
        self.max_bytes = max(1024 * 1024, int(float(max_mb) * 1024 * 1024))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.dir / "cache.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS webcache(
                    key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    headers_json TEXT NOT NULL,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    fetched_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    blob_name TEXT NOT NULL
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_wc_acc ON webcache(accessed_at ASC)")
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:32]

    def get(self, url: str, *, ttl_s: float | None = None, allow_stale: bool = False) -> dict[str, Any] | None:
        k = self._key(url)
        eff_ttl = self.default_ttl_s if ttl_s is None else float(ttl_s)
        now = time.time()
        with self._lock:
            row = self.db.execute("SELECT * FROM webcache WHERE key=?", (k,)).fetchone()
            if not row:
                return None
            fresh = (now - float(row["fetched_at"])) <= eff_ttl
            if not fresh and not allow_stale:
                return None
            bp = self.blobs / row["blob_name"]
            if not bp.is_file():
                self.db.execute("DELETE FROM webcache WHERE key=?", (k,))
                self.db.commit()
                return None
            try:
                body = bp.read_bytes()
            except OSError:
                return None
            self.db.execute("UPDATE webcache SET accessed_at=? WHERE key=?", (now, k))
            self.db.commit()
            try:
                hdrs = json.loads(row["headers_json"])
            except ValueError:
                hdrs = {}
            return {
                "url": row["url"],
                "status": int(row["status"]),
                "headers": hdrs,
                "etag": row["etag"],
                "last_modified": row["last_modified"],
                "fetched_at": float(row["fetched_at"]),
                "fresh": fresh,
                "body": body,
            }

    def conditional_headers(self, url: str) -> dict[str, str]:
        k = self._key(url)
        with self._lock:
            row = self.db.execute("SELECT etag, last_modified FROM webcache WHERE key=?", (k,)).fetchone()
        if not row:
            return {}
        out: dict[str, str] = {}
        if row["etag"]:
            out["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            out["If-Modified-Since"] = row["last_modified"]
        return out

    def touch_304(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
        k = self._key(url)
        now = time.time()
        with self._lock:
            row = self.db.execute("SELECT * FROM webcache WHERE key=?", (k,)).fetchone()
            if not row:
                return None
            etag = (headers or {}).get("etag") or row["etag"]
            lm = (headers or {}).get("last-modified") or row["last_modified"]
            self.db.execute(
                "UPDATE webcache SET fetched_at=?, accessed_at=?, etag=?, last_modified=? WHERE key=?",
                (now, now, etag, lm, k),
            )
            self.db.commit()
        return self.get(url, allow_stale=True)

    def put(self, url: str, *, status: int, headers: dict[str, str], body: bytes, directives: dict[str, Any] | None = None) -> bool:
        if status < 200 or status >= 300:
            return False
        d = directives or {}
        if d.get("noarchive") or d.get("noindex") or d.get("noai"):
            # 若此前有旧缓存，同时删除以严格遵守禁止存档声明
            self.delete(url)
            return False
        hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        k = self._key(url)
        blob_name = f"{k}.bin"
        bp = self.blobs / blob_name
        now = time.time()
        with self._lock:
            bp.write_bytes(body)
            self.db.execute(
                """INSERT OR REPLACE INTO webcache
                   (key, url, status, headers_json, etag, last_modified, fetched_at, accessed_at, size_bytes, blob_name)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    k,
                    url,
                    int(status),
                    json.dumps(hdrs, ensure_ascii=False),
                    hdrs.get("etag", ""),
                    hdrs.get("last-modified", ""),
                    now,
                    now,
                    len(body),
                    blob_name,
                ),
            )
            self.db.commit()
            self._prune_lru_unlocked()
        return True

    def delete(self, url: str) -> None:
        k = self._key(url)
        with self._lock:
            row = self.db.execute("SELECT blob_name FROM webcache WHERE key=?", (k,)).fetchone()
            if row:
                try:
                    (self.blobs / row["blob_name"]).unlink(missing_ok=True)
                except OSError:
                    pass
                self.db.execute("DELETE FROM webcache WHERE key=?", (k,))
                self.db.commit()

    def _prune_lru_unlocked(self) -> None:
        row = self.db.execute("SELECT COALESCE(SUM(size_bytes), 0) AS s FROM webcache").fetchone()
        total = int(row["s"] or 0) if row else 0
        if total <= self.max_bytes:
            return
        rows = self.db.execute("SELECT key, size_bytes, blob_name FROM webcache ORDER BY accessed_at ASC").fetchall()
        for r in rows:
            if total <= self.max_bytes:
                break
            try:
                (self.blobs / r["blob_name"]).unlink(missing_ok=True)
            except OSError:
                pass
            self.db.execute("DELETE FROM webcache WHERE key=?", (r["key"],))
            total -= int(r["size_bytes"] or 0)
        self.db.commit()
