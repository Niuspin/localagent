"""网络请求账本（NetLedger）：统一记录全部出站联网请求、缓存命中与策略拦截，支持检索与导出。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _domain_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


class NetLedger:
    """线程安全的出站网络请求账本（SQLite WAL + AuditLog + SSE 广播）。"""

    def __init__(self, db_path: Path, audit: Any = None, notify: Any = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self.notify = notify
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS net_ledger(
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    url TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    method TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    bytes_in INTEGER NOT NULL DEFAULT 0,
                    purpose TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT 'main',
                    session_id TEXT NOT NULL DEFAULT '',
                    research_id TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT 'allow',
                    reason TEXT NOT NULL DEFAULT '',
                    cached INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_net_ledger_ts ON net_ledger(ts DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_net_ledger_domain ON net_ledger(domain, ts DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_net_ledger_res ON net_ledger(research_id, ts DESC)")
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    def record(
        self,
        *,
        url: str,
        method: str = "GET",
        status: int = 0,
        bytes_in: int = 0,
        purpose: str = "web_fetch",
        agent_id: str = "main",
        session_id: str | None = None,
        research_id: str | None = None,
        decision: str = "allow",
        reason: str = "",
        cached: bool = False,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        entry_id = f"net_{uuid.uuid4().hex[:12]}"
        ts = round(time.time(), 3)
        dom = _domain_of(url) or ""
        row = {
            "id": entry_id,
            "ts": ts,
            "url": str(url)[:1000],
            "domain": dom[:200],
            "method": str(method or "GET").upper()[:16],
            "status": int(status or 0),
            "bytes_in": max(0, int(bytes_in or 0)),
            "purpose": str(purpose or "")[:64],
            "agent_id": str(agent_id or "main")[:64],
            "session_id": str(session_id or "")[:64],
            "research_id": str(research_id or "")[:64],
            "decision": str(decision or "allow")[:24],
            "reason": str(reason or "")[:300],
            "cached": bool(cached),
            "duration_ms": max(0, int(duration_ms or 0)),
        }
        with self._lock:
            self.db.execute(
                """INSERT INTO net_ledger
                   (id, ts, url, domain, method, status, bytes_in, purpose, agent_id, session_id, research_id, decision, reason, cached, duration_ms)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    row["ts"],
                    row["url"],
                    row["domain"],
                    row["method"],
                    row["status"],
                    row["bytes_in"],
                    row["purpose"],
                    row["agent_id"],
                    row["session_id"],
                    row["research_id"],
                    row["decision"],
                    row["reason"],
                    1 if row["cached"] else 0,
                    row["duration_ms"],
                ),
            )
            self.db.commit()

        if self.audit is not None:
            try:
                ev_name = "research_blocked" if row["decision"] in ("deny", "blocked") else "net_request"
                self.audit.append(
                    event=ev_name,
                    url=row["url"],
                    domain=row["domain"],
                    method=row["method"],
                    status=row["status"],
                    bytes=row["bytes_in"],
                    purpose=row["purpose"],
                    decision=row["decision"],
                    reason=row["reason"],
                    cached=row["cached"],
                    agent_id=row["agent_id"],
                    session=row["session_id"] or None,
                    research_id=row["research_id"] or None,
                )
            except Exception:  # noqa: BLE001
                pass

        if callable(self.notify):
            try:
                if row["decision"] in ("deny", "blocked"):
                    ev = "research_blocked"
                elif row["purpose"] == "web_search":
                    ev = "research_query"
                else:
                    ev = "research_fetch"
                self.notify(ev, dict(row))
            except Exception:  # noqa: BLE001
                pass
        return row

    def query(
        self,
        *,
        domain: str | None = None,
        decision: str | None = None,
        agent_id: str | None = None,
        research_id: str | None = None,
        q: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        lim = max(1, min(1000, int(limit or 200)))
        clauses: list[str] = []
        params: list[Any] = []
        if domain:
            clauses.append("LOWER(domain) LIKE ?")
            params.append(f"%{domain.strip().lower()}%")
        if decision:
            clauses.append("decision=?")
            params.append(decision.strip())
        if agent_id:
            clauses.append("agent_id=?")
            params.append(agent_id.strip())
        if research_id:
            clauses.append("research_id=?")
            params.append(research_id.strip())
        if since is not None:
            clauses.append("ts>=?")
            params.append(float(since))
        if q:
            clauses.append("(LOWER(url) LIKE ? OR LOWER(reason) LIKE ? OR LOWER(purpose) LIKE ?)")
            pat = f"%{q.strip().lower()}%"
            params.extend([pat, pat, pat])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM net_ledger{where} ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self.db.execute(sql, [*params, lim]).fetchall()
            agg = self.db.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM(CASE WHEN decision='allow' THEN 1 ELSE 0 END), 0) AS allowed,
                           COALESCE(SUM(CASE WHEN decision IN ('deny', 'blocked') THEN 1 ELSE 0 END), 0) AS blocked,
                           COALESCE(SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END), 0) AS cached,
                           COALESCE(SUM(bytes_in), 0) AS total_bytes
                    FROM net_ledger{where}""",
                params,
            ).fetchone()
        entries = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "url": r["url"],
                "domain": r["domain"],
                "method": r["method"],
                "status": r["status"],
                "bytes_in": r["bytes_in"],
                "purpose": r["purpose"],
                "agent_id": r["agent_id"],
                "session_id": r["session_id"],
                "research_id": r["research_id"],
                "decision": r["decision"],
                "reason": r["reason"],
                "cached": bool(r["cached"]),
                "duration_ms": r["duration_ms"],
            }
            for r in rows
        ]
        summary = {
            "total": int(agg["total"] or 0) if agg else 0,
            "allowed": int(agg["allowed"] or 0) if agg else 0,
            "blocked": int(agg["blocked"] or 0) if agg else 0,
            "cached": int(agg["cached"] or 0) if agg else 0,
            "total_bytes": int(agg["total_bytes"] or 0) if agg else 0,
        }
        return {"entries": entries, "summary": summary}

    def export_csv(self, **filters: Any) -> str:
        data = self.query(**filters)
        buf = io.StringIO()
        fields = [
            "id", "ts", "domain", "method", "status", "bytes_in", "purpose",
            "agent_id", "session_id", "research_id", "decision", "reason", "cached", "duration_ms", "url",
        ]
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for row in data["entries"]:
            w.writerow({k: row.get(k, "") for k in fields})
        return buf.getvalue()
