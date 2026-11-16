"""礼貌访问限流器（DomainLimiter）：
全局与每域令牌桶、并发限制、Crawl-delay、429/503 Retry-After 退避、单任务请求上限与封禁域熔断。
"""
from __future__ import annotations

import asyncio
import email.utils
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any


def parse_retry_after(header_val: str | None, default_s: float = 2.0, cap_s: float = 30.0) -> float:
    raw = str(header_val or "").strip()
    if not raw:
        return default_s
    try:
        secs = float(raw)
        return max(0.2, min(cap_s, secs))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        delta = dt.timestamp() - time.time()
        return max(0.2, min(cap_s, delta))
    except Exception:  # noqa: BLE001
        return default_s


class DomainLimiter:
    """支持同步与异步调用的礼貌限流器。"""

    def __init__(self, *, per_domain_rps: float = 0.5, per_domain_conc: int = 2, global_rps: float = 4.0):
        self.per_domain_rps = max(0.1, min(5.0, float(per_domain_rps)))
        self.per_domain_conc = max(1, min(4, int(per_domain_conc)))
        self.global_rps = max(0.5, float(global_rps))
        self._lock = threading.Lock()
        self._domain_next: dict[str, float] = {}
        self._domain_backoff_until: dict[str, float] = {}
        self._domain_fail_streak: dict[str, int] = {}
        self._domain_active: dict[str, int] = {}
        self._blocked_domains: dict[str, str] = {}
        self._global_next: float = 0.0
        self._task_counts: dict[str, int] = {}

    def configure(self, *, per_domain_rps: float | None = None, per_domain_conc: int | None = None) -> None:
        with self._lock:
            if per_domain_rps is not None:
                self.per_domain_rps = max(0.1, min(5.0, float(per_domain_rps)))
            if per_domain_conc is not None:
                self.per_domain_conc = max(1, min(4, int(per_domain_conc)))

    def mark_blocked(self, domain: str, reason: str) -> None:
        d = (domain or "").lower()
        if d:
            with self._lock:
                self._blocked_domains[d] = reason

    def is_blocked(self, domain: str) -> str | None:
        d = (domain or "").lower()
        with self._lock:
            return self._blocked_domains.get(d)

    def blocked_map(self) -> dict[str, str]:
        with self._lock:
            return dict(self._blocked_domains)

    def check_and_count_task(self, task_id: str | None, max_requests: int) -> bool:
        if not task_id:
            return True
        with self._lock:
            cur = self._task_counts.get(task_id, 0)
            if cur >= max(1, int(max_requests)):
                return False
            self._task_counts[task_id] = cur + 1
            return True

    def task_count(self, task_id: str) -> int:
        with self._lock:
            return self._task_counts.get(task_id, 0)

    def note_response(self, domain: str, status: int, retry_after: str | None = None) -> float:
        """记录响应状态；429/503 时施加 Retry-After / 指数退避并返回等待秒数。"""
        d = (domain or "").lower()
        if not d:
            return 0.0
        with self._lock:
            if status in (429, 503):
                streak = self._domain_fail_streak.get(d, 0) + 1
                self._domain_fail_streak[d] = streak
                exp_s = min(30.0, 1.0 * (2 ** min(4, streak - 1)))
                delay = max(exp_s, parse_retry_after(retry_after, default_s=exp_s))
                self._domain_backoff_until[d] = max(self._domain_backoff_until.get(d, 0.0), time.monotonic() + delay)
                return delay
            if 200 <= status < 400:
                self._domain_fail_streak[d] = 0
                self._domain_backoff_until.pop(d, None)
        return 0.0

    def _reserve_slot_sync(self, domain: str, crawl_delay: float = 0.0) -> float:
        d = (domain or "").lower() or "_default"
        while True:
            wait_s = 0.0
            with self._lock:
                if d in self._blocked_domains:
                    raise RuntimeError(f"域名 {d} 已被拦截 ({self._blocked_domains[d]})")
                now = time.monotonic()
                active = self._domain_active.get(d, 0)
                if active >= self.per_domain_conc:
                    wait_s = 0.05
                else:
                    min_gap = max(1.0 / self.per_domain_rps, float(crawl_delay or 0.0))
                    g_gap = 1.0 / self.global_rps
                    ready_at = max(
                        now,
                        self._domain_next.get(d, 0.0),
                        self._domain_backoff_until.get(d, 0.0),
                        self._global_next,
                    )
                    if ready_at <= now + 1e-4:
                        self._domain_active[d] = active + 1
                        self._domain_next[d] = now + min_gap
                        self._global_next = now + g_gap
                        return 0.0
                    wait_s = min(30.0, ready_at - now)
            time.sleep(wait_s)

    def _release_slot(self, domain: str) -> None:
        d = (domain or "").lower() or "_default"
        with self._lock:
            cur = self._domain_active.get(d, 0)
            if cur <= 1:
                self._domain_active.pop(d, None)
            else:
                self._domain_active[d] = cur - 1

    @contextmanager
    def acquire_sync(self, domain: str, *, crawl_delay: float = 0.0):
        self._reserve_slot_sync(domain, crawl_delay=crawl_delay)
        try:
            yield
        finally:
            self._release_slot(domain)

    @asynccontextmanager
    async def acquire(self, domain: str, *, crawl_delay: float = 0.0):
        d = (domain or "").lower() or "_default"
        while True:
            wait_s = 0.0
            with self._lock:
                if d in self._blocked_domains:
                    raise RuntimeError(f"域名 {d} 已被拦截 ({self._blocked_domains[d]})")
                now = time.monotonic()
                active = self._domain_active.get(d, 0)
                if active >= self.per_domain_conc:
                    wait_s = 0.05
                else:
                    min_gap = max(1.0 / self.per_domain_rps, float(crawl_delay or 0.0))
                    g_gap = 1.0 / self.global_rps
                    ready_at = max(
                        now,
                        self._domain_next.get(d, 0.0),
                        self._domain_backoff_until.get(d, 0.0),
                        self._global_next,
                    )
                    if ready_at <= now + 1e-4:
                        self._domain_active[d] = active + 1
                        self._domain_next[d] = now + min_gap
                        self._global_next = now + g_gap
                        break
                    wait_s = min(30.0, ready_at - now)
            await asyncio.sleep(wait_s)
        try:
            yield
        finally:
            self._release_slot(domain)
