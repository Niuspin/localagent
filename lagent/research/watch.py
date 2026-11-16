"""页面监测（research watch）：受策略与限速约束定期重检关键页面并输出文本 diff。"""
from __future__ import annotations

import difflib
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import net
from ..journal import atomic_write
from .extract import extract_response
from .limiter import DomainLimiter
from .policy import ResearchPolicy, safe_user_agent
from .robots import RobotsCache


class PageWatchManager:
    def __init__(
        self,
        store_path: Path,
        *,
        cfg: Any,
        policy: ResearchPolicy,
        robots: RobotsCache,
        limiter: DomainLimiter,
        ledger: Any = None,
        fetcher: Callable[..., Any] | None = None,
    ):
        self.store_path = Path(store_path)
        self.cfg = cfg
        self.policy = policy
        self.robots = robots
        self.limiter = limiter
        self.ledger = ledger
        self.fetcher = fetcher or net.fetch_sync
        self._lock = threading.RLock()
        self.watches: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.store_path.read_text("utf-8"))
            if isinstance(raw, dict):
                self.watches = raw
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.store_path, json.dumps(self.watches, ensure_ascii=False, indent=2).encode("utf-8"))

    def add(self, url: str, *, label: str = "") -> dict[str, Any]:
        dec = self.policy.evaluate_url(url, purpose="research_watch")
        if not dec.allowed:
            raise PermissionError(dec.reason)
        wid = f"w_{uuid.uuid4().hex[:10]}"
        item = {
            "id": wid,
            "url": url,
            "domain": dec.domain,
            "label": (label or url)[:120],
            "last_sha": "",
            "last_text": "",
            "last_checked": 0.0,
            "changes": 0,
        }
        with self._lock:
            self.watches[wid] = item
            self._save()
        return dict(item)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in w.items() if k != "last_text"} for w in self.watches.values()]

    def check_once(self, watch_id: str) -> dict[str, Any]:
        with self._lock:
            w = self.watches.get(watch_id)
            if not w:
                raise KeyError("watch 不存在")
            url = w["url"]
            prev_text = w.get("last_text") or ""
            prev_sha = w.get("last_sha") or ""

        dec = self.policy.evaluate_url(url, purpose="research_watch")
        if not dec.allowed:
            if self.ledger:
                self.ledger.record(url=url, method="GET", status=0, purpose="research_watch", decision="deny", reason=dec.reason)
            return {"ok": False, "changed": False, "error": dec.reason}

        ok_rob, delay, why_rob = self.robots.check_sync(url, cfg=self.cfg, respect_robots=self.policy.respect_robots())
        if not ok_rob:
            if self.ledger:
                self.ledger.record(url=url, method="GET", status=0, purpose="research_watch", decision="deny", reason=why_rob)
            return {"ok": False, "changed": False, "error": why_rob}

        with self.limiter.acquire_sync(dec.domain, crawl_delay=delay):
            resp = self.fetcher(
                url,
                timeout=15.0,
                max_bytes=int(self.cfg.get("web", "max_bytes", default=3_000_000)),
                headers={"User-Agent": safe_user_agent(self.cfg)},
            )
        if self.ledger:
            self.ledger.record(url=url, method="GET", status=resp.status, bytes_in=len(resp.body), purpose="research_watch", decision="allow")
        if resp.status >= 400:
            return {"ok": False, "changed": False, "error": f"HTTP {resp.status}"}

        doc = extract_response(resp.body, resp.url, headers=resp.headers, charset_text=resp.text())
        new_sha = doc.sha256 or hashlib.sha256(doc.text.encode("utf-8", "replace")).hexdigest()
        changed = bool(prev_sha and new_sha != prev_sha)
        diff_txt = ""
        if changed:
            diff_txt = "\n".join(
                difflib.unified_diff(prev_text.splitlines(), doc.text.splitlines(), fromfile="previous", tofile="current", lineterm="")
            )[:4000]
        with self._lock:
            w = self.watches[watch_id]
            w["last_sha"] = new_sha
            if not (doc.directives.get("noarchive") or doc.directives.get("noai")):
                w["last_text"] = doc.text[:40000]
            w["last_checked"] = time.time()
            if changed:
                w["changes"] = int(w.get("changes", 0)) + 1
            self._save()
        return {"ok": True, "changed": changed, "sha256": new_sha, "diff": diff_txt}
