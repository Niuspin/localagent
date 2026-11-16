"""RFC 9309 robots.txt 解析、缓存与合规检查。

原则（§5.1）：
- 抓取前先获取并缓存 /robots.txt（含 Crawl-delay 与 Sitemap）；
- Disallow 的路径绝对不抓取；
- 4xx（非 429）按标准视为允许；5xx / 429 / 不可达按暂时禁止处理。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import net
from .policy import safe_user_agent


@dataclass
class _Rule:
    allow: bool
    pattern: str


@dataclass
class RobotsRules:
    origin: str
    fetched_at: float
    status: int
    allow_all: bool = True
    disallow_all: bool = False
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float = 0.0
    sitemaps: list[str] = field(default_factory=list)
    reason: str = ""

    def is_allowed(self, path_and_query: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all and not self.rules:
            return True
        target = path_and_query or "/"
        if not target.startswith("/"):
            target = "/" + target
        # RFC 9309: longest matching pattern wins; tie-breaker: Allow wins over Disallow
        best_len = -1
        best_allow = True
        for r in self.rules:
            pat = r.pattern
            if not pat:
                continue
            if _match_robots_pattern(pat, target):
                plen = len(pat)
                if plen > best_len or (plen == best_len and r.allow):
                    best_len = plen
                    best_allow = r.allow
        return best_allow

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "fetched_at": self.fetched_at,
            "status": self.status,
            "allow_all": self.allow_all,
            "disallow_all": self.disallow_all,
            "rules": [{"allow": r.allow, "pattern": r.pattern} for r in self.rules],
            "crawl_delay": self.crawl_delay,
            "sitemaps": self.sitemaps,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RobotsRules":
        return cls(
            origin=str(d.get("origin") or ""),
            fetched_at=float(d.get("fetched_at") or 0.0),
            status=int(d.get("status") or 200),
            allow_all=bool(d.get("allow_all", True)),
            disallow_all=bool(d.get("disallow_all", False)),
            rules=[_Rule(bool(x.get("allow")), str(x.get("pattern") or "")) for x in (d.get("rules") or [])],
            crawl_delay=float(d.get("crawl_delay") or 0.0),
            sitemaps=[str(x) for x in (d.get("sitemaps") or [])],
            reason=str(d.get("reason") or ""),
        )


def _match_robots_pattern(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    parts = [re.escape(p) for p in core.split("*")]
    rx = "^" + ".*".join(parts) + ("$" if anchored else "")
    return bool(re.match(rx, path))


def parse_robots_txt(text: str, user_agent: str = "LocalAgent", origin: str = "") -> RobotsRules:
    """按 RFC 9309 解析 robots.txt，优先匹配专用 User-Agent 分组，其次 '*' 分组。"""
    ua_tok = (user_agent or "LocalAgent").split("/", 1)[0].strip().lower()
    groups: list[tuple[list[str], list[_Rule], float]] = []
    cur_agents: list[str] = []
    cur_rules: list[_Rule] = []
    cur_delay = 0.0
    in_rules = False
    sitemaps: list[str] = []

    for raw_ln in (text or "").splitlines():
        ln = raw_ln.split("#", 1)[0].strip()
        if not ln or ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        key = k.strip().lower()
        val = v.strip()
        if key == "sitemap":
            if val and val not in sitemaps:
                sitemaps.append(val)
            continue
        if key == "user-agent":
            if in_rules and cur_agents:
                groups.append((cur_agents, cur_rules, cur_delay))
                cur_agents, cur_rules, cur_delay, in_rules = [], [], 0.0, False
            cur_agents.append(val.lower())
        elif key in ("disallow", "allow"):
            in_rules = True
            if val == "" and key == "disallow":
                cur_rules.append(_Rule(True, "/"))
            elif val:
                cur_rules.append(_Rule(key == "allow", val))
        elif key == "crawl-delay":
            in_rules = True
            try:
                cur_delay = max(0.0, min(60.0, float(val)))
            except ValueError:
                pass
    if cur_agents:
        groups.append((cur_agents, cur_rules, cur_delay))

    matched_specific: list[tuple[list[_Rule], float]] = []
    matched_star: list[tuple[list[_Rule], float]] = []
    for agents, rules, delay in groups:
        if any(a != "*" and (a in ua_tok or ua_tok.startswith(a) or a.startswith("localagent")) for a in agents):
            matched_specific.append((rules, delay))
        elif "*" in agents:
            matched_star.append((rules, delay))

    chosen = matched_specific or matched_star
    if not chosen:
        return RobotsRules(origin=origin, fetched_at=time.time(), status=200, allow_all=True, sitemaps=sitemaps)

    merged_rules: list[_Rule] = []
    max_delay = 0.0
    for rules, delay in chosen:
        merged_rules.extend(rules)
        max_delay = max(max_delay, delay)
    return RobotsRules(
        origin=origin,
        fetched_at=time.time(),
        status=200,
        allow_all=not merged_rules,
        disallow_all=False,
        rules=merged_rules,
        crawl_delay=max_delay,
        sitemaps=sitemaps,
    )


class RobotsCache:
    """线程安全 + 磁盘可持久化的 robots.txt 缓存。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        ttl_s: float = 3600.0,
        fetcher: Callable[..., Any] | None = None,
        ledger: Any = None,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s
        self.fetcher = fetcher or net.fetch_sync
        self.ledger = ledger
        self._mem: dict[str, RobotsRules] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _origin(url: str) -> str:
        sp = urlsplit(url)
        return f"{sp.scheme.lower()}://{sp.netloc.lower()}"

    def _disk_path(self, origin: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", origin)[:120]
        return self.cache_dir / f"{safe}.json"

    def get_rules_sync(self, url: str, *, cfg: Any = None, proxy: str = "", timeout: float = 10.0, research_id: str | None = None) -> RobotsRules:
        origin = self._origin(url)
        now = time.time()
        with self._lock:
            hit = self._mem.get(origin)
            if hit and now - hit.fetched_at < self.ttl_s:
                return hit
            dp = self._disk_path(origin)
            if dp and dp.is_file():
                try:
                    loaded = RobotsRules.from_dict(json.loads(dp.read_text("utf-8")))
                    if now - loaded.fetched_at < self.ttl_s:
                        self._mem[origin] = loaded
                        return loaded
                except (OSError, ValueError):
                    pass

        robots_url = origin + "/robots.txt"
        ua = safe_user_agent(cfg)
        t0 = time.monotonic()
        try:
            resp = self.fetcher(
                robots_url,
                timeout=min(float(timeout), 10.0),
                max_bytes=512_000,
                proxy=proxy,
                headers={"User-Agent": ua},
            )
            dur = int((time.monotonic() - t0) * 1000)
            if self.ledger is not None:
                self.ledger.record(
                    url=robots_url,
                    method="GET",
                    status=resp.status,
                    bytes_in=len(resp.body),
                    purpose="robots",
                    research_id=research_id,
                    decision="allow",
                    duration_ms=dur,
                )
            if 200 <= resp.status < 300:
                rules = parse_robots_txt(resp.text(), user_agent=ua, origin=origin)
                rules.status = resp.status
            elif 400 <= resp.status < 500 and resp.status != 429:
                # RFC 9309: 4xx (not 429) -> allow all
                rules = RobotsRules(origin=origin, fetched_at=now, status=resp.status, allow_all=True, reason=f"robots_http_{resp.status}")
            else:
                # 5xx or 429 -> temporarily disallowed
                rules = RobotsRules(origin=origin, fetched_at=now, status=resp.status, allow_all=False, disallow_all=True, reason=f"robots_http_{resp.status}")
        except Exception as e:  # noqa: BLE001
            dur = int((time.monotonic() - t0) * 1000)
            if self.ledger is not None:
                self.ledger.record(
                    url=robots_url,
                    method="GET",
                    status=0,
                    bytes_in=0,
                    purpose="robots",
                    research_id=research_id,
                    decision="deny",
                    reason=f"robots_error: {e}",
                    duration_ms=dur,
                )
            rules = RobotsRules(origin=origin, fetched_at=now, status=0, allow_all=False, disallow_all=True, reason=f"robots_error: {e}")

        with self._lock:
            self._mem[origin] = rules
            dp = self._disk_path(origin)
            if dp:
                try:
                    dp.write_text(json.dumps(rules.to_dict(), ensure_ascii=False), "utf-8")
                except OSError:
                    pass
        return rules

    def check_sync(self, url: str, *, cfg: Any = None, proxy: str = "", respect_robots: bool = True, research_id: str | None = None) -> tuple[bool, float, str]:
        if not respect_robots:
            return True, 0.0, ""
        rules = self.get_rules_sync(url, cfg=cfg, proxy=proxy, research_id=research_id)
        sp = urlsplit(url)
        pq = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
        if not rules.is_allowed(pq):
            why = rules.reason or f"robots.txt 禁止抓取路径 {sp.path or '/'}"
            return False, rules.crawl_delay, why
        return True, rules.crawl_delay, ""

    async def check(self, url: str, *, cfg: Any = None, proxy: str = "", respect_robots: bool = True, research_id: str | None = None) -> tuple[bool, float, str]:
        return await asyncio.to_thread(self.check_sync, url, cfg=cfg, proxy=proxy, respect_robots=respect_robots, research_id=research_id)
