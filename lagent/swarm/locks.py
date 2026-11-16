"""主工作区文件协作租约（读共享 / 写独占，带 TTL 防死锁）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Lease:
    readers: dict[str, float] = field(default_factory=dict)
    writer: str | None = None
    writer_exp: float = 0.0


class FileLeaseManager:
    def __init__(self, default_ttl_s: float = 120.0):
        self.default_ttl_s = float(default_ttl_s)
        self._leases: dict[str, _Lease] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _norm(path: str) -> str:
        return str(path or "").replace("\\", "/").strip("/")

    def _gc(self, lease: _Lease, now: float) -> None:
        if lease.writer and now >= lease.writer_exp:
            lease.writer = None
            lease.writer_exp = 0.0
        for owner in [k for k, exp in lease.readers.items() if now >= exp]:
            lease.readers.pop(owner, None)

    def acquire_read(self, path: str, owner: str, *, ttl_s: float | None = None) -> bool:
        key = self._norm(path)
        now = time.monotonic()
        exp = now + (ttl_s if ttl_s is not None else self.default_ttl_s)
        with self._lock:
            lease = self._leases.setdefault(key, _Lease())
            self._gc(lease, now)
            if lease.writer and lease.writer != owner:
                return False
            lease.readers[owner] = exp
            return True

    def acquire_write(self, path: str, owner: str, *, ttl_s: float | None = None) -> bool:
        key = self._norm(path)
        now = time.monotonic()
        exp = now + (ttl_s if ttl_s is not None else self.default_ttl_s)
        with self._lock:
            lease = self._leases.setdefault(key, _Lease())
            self._gc(lease, now)
            other_readers = [r for r in lease.readers if r != owner]
            if other_readers or (lease.writer and lease.writer != owner):
                return False
            lease.writer = owner
            lease.writer_exp = exp
            return True

    def release(self, path: str, owner: str) -> None:
        key = self._norm(path)
        with self._lock:
            lease = self._leases.get(key)
            if not lease:
                return
            lease.readers.pop(owner, None)
            if lease.writer == owner:
                lease.writer = None
                lease.writer_exp = 0.0
            if not lease.readers and lease.writer is None:
                self._leases.pop(key, None)

    def release_owner(self, owner: str) -> int:
        released = 0
        with self._lock:
            for key in list(self._leases):
                lease = self._leases[key]
                had = (owner in lease.readers) or (lease.writer == owner)
                if had:
                    released += 1
                    self.release(key, owner)
        return released

    def holder(self, path: str) -> dict:
        key = self._norm(path)
        now = time.monotonic()
        with self._lock:
            lease = self._leases.get(key)
            if not lease:
                return {"readers": [], "writer": None}
            self._gc(lease, now)
            return {"readers": sorted(lease.readers), "writer": lease.writer}
