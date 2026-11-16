"""证据库（EvidenceStore）：SQLite WAL 持久化调研任务、来源元数据与结构化论断。

合规约束（§5.1 #7 & #8）：
- 默认仅保存有限摘录（<= quote_max_chars）+ 原链接 + 抓取时间 + 内容哈希；
- 若响应含 noarchive / noai / noindex，不落盘正文或摘录（仅记录元信息与策略标识）；
- 存储前按配置执行 PII 脱敏。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .policy import redact_pii


def _domain_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


class EvidenceStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS research_tasks(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    depth TEXT NOT NULL DEFAULT 'standard',
                    mode TEXT NOT NULL DEFAULT 'open',
                    sub_questions_json TEXT NOT NULL DEFAULT '[]',
                    blocked_domains_json TEXT NOT NULL DEFAULT '{}',
                    report_md TEXT NOT NULL DEFAULT '',
                    report_artifact_id TEXT NOT NULL DEFAULT '',
                    cite_check_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY,
                    research_id TEXT NOT NULL,
                    ref_num INTEGER NOT NULL DEFAULT 0,
                    url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    fetched_at REAL NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    license TEXT NOT NULL DEFAULT '',
                    robots_ok INTEGER NOT NULL DEFAULT 1,
                    directives_json TEXT NOT NULL DEFAULT '{}',
                    cred_score REAL NOT NULL DEFAULT 0.5,
                    blocked_reason TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS claims(
                    id TEXT PRIMARY KEY,
                    research_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    sub_question TEXT NOT NULL DEFAULT '',
                    claim_text TEXT NOT NULL,
                    quote TEXT NOT NULL DEFAULT '',
                    span TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    verified INTEGER NOT NULL DEFAULT 0,
                    conflicts_with TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sources_res ON sources(research_id, ref_num ASC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_claims_res ON claims(research_id, created_at ASC)")
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    def create_task(
        self,
        *,
        question: str,
        session_id: str = "",
        depth: str = "standard",
        mode: str = "open",
        research_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rid = research_id or f"res_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO research_tasks
                   (id, session_id, question, status, depth, mode, created_at, updated_at, meta_json)
                   VALUES(?, ?, ?, 'running', ?, ?, ?, ?, ?)""",
                (rid, session_id or "", question, depth, mode, now, now, json.dumps(meta or {}, ensure_ascii=False)),
            )
            self.db.commit()
        return self.get_task(rid)  # type: ignore[return-value]

    def update_task(self, research_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "sub_questions_json", "blocked_domains_json", "report_md",
            "report_artifact_id", "cite_check_json", "error", "meta_json",
        }
        sets: list[str] = ["updated_at=?"]
        vals: list[Any] = [time.time()]
        for k, v in fields.items():
            if k == "sub_questions":
                k, v = "sub_questions_json", json.dumps(v, ensure_ascii=False)
            elif k == "blocked_domains":
                k, v = "blocked_domains_json", json.dumps(v, ensure_ascii=False)
            elif k == "cite_check":
                k, v = "cite_check_json", json.dumps(v, ensure_ascii=False)
            elif k == "meta":
                k, v = "meta_json", json.dumps(v, ensure_ascii=False)
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(research_id)
        with self._lock:
            self.db.execute(f"UPDATE research_tasks SET {', '.join(sets)} WHERE id=?", vals)
            self.db.commit()
        return self.get_task(research_id)

    def _row_to_task(self, r: sqlite3.Row) -> dict[str, Any]:
        def _j(raw: str, default: Any) -> Any:
            try:
                return json.loads(raw)
            except ValueError:
                return default

        return {
            "id": r["id"],
            "session_id": r["session_id"],
            "question": r["question"],
            "status": r["status"],
            "depth": r["depth"],
            "mode": r["mode"],
            "sub_questions": _j(r["sub_questions_json"], []),
            "blocked_domains": _j(r["blocked_domains_json"], {}),
            "report_md": r["report_md"],
            "report_artifact_id": r["report_artifact_id"],
            "cite_check": _j(r["cite_check_json"], {}),
            "error": r["error"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "meta": _j(r["meta_json"], {}),
        }

    def get_task(self, research_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM research_tasks WHERE id=?", (research_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, *, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        lim = max(1, min(200, int(limit or 50)))
        with self._lock:
            if session_id:
                rows = self.db.execute(
                    "SELECT * FROM research_tasks WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, lim),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM research_tasks ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def add_source(
        self,
        research_id: str,
        *,
        url: str,
        canonical_url: str = "",
        title: str = "",
        sha256: str = "",
        excerpt: str = "",
        author: str = "",
        published_at: str = "",
        license: str = "",
        robots_ok: bool = True,
        directives: dict[str, Any] | None = None,
        cred_score: float = 0.5,
        blocked_reason: str = "",
        quote_max_chars: int = 400,
        do_redact_pii: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = f"src_{uuid.uuid4().hex[:12]}"
        d = directives or {}
        forbid_store = bool(d.get("noarchive") or d.get("noai") or d.get("nosnippet") or d.get("noindex"))
        qcap = max(100, min(2000, int(quote_max_chars or 400)))
        stored_excerpt = "" if forbid_store else (redact_pii(excerpt) if do_redact_pii else excerpt)[:qcap]
        stored_title = (redact_pii(title) if do_redact_pii else title)[:200]
        now = time.time()
        dom = _domain_of(canonical_url or url)

        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(ref_num), 0) AS m FROM sources WHERE research_id=?",
                (research_id,),
            ).fetchone()
            ref_num = int(row["m"] or 0) + 1
            self.db.execute(
                """INSERT INTO sources
                   (id, research_id, ref_num, url, canonical_url, domain, title, fetched_at, sha256, excerpt,
                    author, published_at, license, robots_ok, directives_json, cred_score, blocked_reason, meta_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    research_id,
                    ref_num,
                    url,
                    canonical_url or url,
                    dom,
                    stored_title,
                    now,
                    sha256,
                    stored_excerpt,
                    author[:120],
                    published_at[:64],
                    license[:120],
                    1 if robots_ok else 0,
                    json.dumps(d, ensure_ascii=False),
                    float(cred_score),
                    blocked_reason[:200],
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            self.db.commit()
        return self.get_source(sid)  # type: ignore[return-value]

    def _row_to_source(self, r: sqlite3.Row) -> dict[str, Any]:
        try:
            directives = json.loads(r["directives_json"])
        except ValueError:
            directives = {}
        try:
            meta = json.loads(r["meta_json"])
        except ValueError:
            meta = {}
        return {
            "id": r["id"],
            "research_id": r["research_id"],
            "ref_num": int(r["ref_num"]),
            "url": r["url"],
            "canonical_url": r["canonical_url"],
            "domain": r["domain"],
            "title": r["title"],
            "fetched_at": r["fetched_at"],
            "sha256": r["sha256"],
            "excerpt": r["excerpt"],
            "author": r["author"],
            "published_at": r["published_at"],
            "license": r["license"],
            "robots_ok": bool(r["robots_ok"]),
            "directives": directives,
            "cred_score": float(r["cred_score"]),
            "blocked_reason": r["blocked_reason"],
            "meta": meta,
        }

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return self._row_to_source(r) if r else None

    def list_sources(self, research_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM sources WHERE research_id=? ORDER BY ref_num ASC",
                (research_id,),
            ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def add_claim(
        self,
        research_id: str,
        *,
        source_id: str,
        claim_text: str,
        quote: str = "",
        span: str = "",
        sub_question: str = "",
        confidence: float = 0.5,
        verified: bool = False,
        conflicts_with: str = "",
        quote_max_chars: int = 400,
        do_redact_pii: bool = True,
    ) -> dict[str, Any]:
        cid = f"clm_{uuid.uuid4().hex[:12]}"
        qcap = max(100, min(2000, int(quote_max_chars or 400)))
        q_clean = (redact_pii(quote) if do_redact_pii else quote)[:qcap]
        c_clean = (redact_pii(claim_text) if do_redact_pii else claim_text)[:1000]
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO claims
                   (id, research_id, source_id, sub_question, claim_text, quote, span, confidence, verified, conflicts_with, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    research_id,
                    source_id,
                    sub_question[:300],
                    c_clean,
                    q_clean,
                    span[:120],
                    max(0.0, min(1.0, float(confidence))),
                    1 if verified else 0,
                    conflicts_with[:200],
                    now,
                ),
            )
            self.db.commit()
        return {
            "id": cid,
            "research_id": research_id,
            "source_id": source_id,
            "sub_question": sub_question[:300],
            "claim_text": c_clean,
            "quote": q_clean,
            "span": span[:120],
            "confidence": max(0.0, min(1.0, float(confidence))),
            "verified": bool(verified),
            "conflicts_with": conflicts_with[:200],
            "created_at": now,
        }

    def list_claims(self, research_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM claims WHERE research_id=? ORDER BY created_at ASC",
                (research_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "research_id": r["research_id"],
                "source_id": r["source_id"],
                "sub_question": r["sub_question"],
                "claim_text": r["claim_text"],
                "quote": r["quote"],
                "span": r["span"],
                "confidence": float(r["confidence"]),
                "verified": bool(r["verified"]),
                "conflicts_with": r["conflicts_with"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_evidence_bundle(self, research_id: str) -> dict[str, Any] | None:
        task = self.get_task(research_id)
        if not task:
            return None
        return {
            "task": task,
            "sources": self.list_sources(research_id),
            "claims": self.list_claims(research_id),
        }
