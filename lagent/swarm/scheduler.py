"""Swarm 调度器：槽位预算估算、四级优先级队列、角色并发上限、饥饿保护与协作式让渡。"""
from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class _WaitItem:
    score: float
    seq: int
    role: str = field(compare=False)
    fut: asyncio.Future = field(compare=False)
    enqueued_at: float = field(compare=False)


class SwarmScheduler:
    """并发槽位与角色并发控制器。"""

    def __init__(
        self,
        *,
        slots: int = 1,
        max_agents: int | str = "auto",
        role_limits: dict[str, int] | None = None,
        aging_per_s: float = 0.25,
    ):
        self.slots = max(1, int(slots))
        self.max_agents_cfg = max_agents
        self.role_limits: dict[str, int] = dict(role_limits or {"reviewer": 2, "researcher": 4, "coder": 4, "verifier": 2})
        self.aging_per_s = float(aging_per_s)
        self._seq = 0
        self._active_total = 0
        self._active_role: dict[str, int] = {}
        self._waiters: list[_WaitItem] = []
        self._lock = asyncio.Lock()

    @property
    def global_limit(self) -> int:
        if isinstance(self.max_agents_cfg, int):
            return max(1, min(self.slots, self.max_agents_cfg))
        if isinstance(self.max_agents_cfg, str) and self.max_agents_cfg != "auto":
            try:
                return max(1, min(self.slots, int(self.max_agents_cfg)))
            except ValueError:
                pass
        return self.slots

    def reconfigure(self, *, slots: int | None = None, max_agents: int | str | None = None, role_limits: dict[str, int] | None = None) -> None:
        if slots is not None:
            self.slots = max(1, int(slots))
        if max_agents is not None:
            self.max_agents_cfg = max_agents
        if role_limits is not None:
            self.role_limits = dict(role_limits)

    def _can_run_unlocked(self, role: str, priority: int) -> bool:
        limit = self.global_limit
        if self._active_total >= limit:
            return False
        # 多槽时为前台高优先级（priority == 0）保留 1 个槽位防饥饿，除非仅 1 槽
        if priority > 0 and limit >= 2 and self._active_total >= limit - 1:
            fg_waiting = any(not w.fut.done() and w.score < 1.0 for w in self._waiters)
            if fg_waiting or (self._active_total >= limit - 1 and priority >= 3):
                return False
        rcap = int(self.role_limits.get(role, limit))
        if self._active_role.get(role, 0) >= max(1, rcap):
            return False
        return True

    def _dispatch_unlocked(self) -> None:
        if not self._waiters:
            return
        now = time.monotonic()
        # 重新计算含老化加权的排序（优先保证前台，同时避免低优长期饥饿）
        alive: list[_WaitItem] = []
        for w in self._waiters:
            if w.fut.done():
                continue
            wait_s = max(0.0, now - w.enqueued_at)
            base_prio = int(w.score // 100) if w.score >= 100 else w.score
            # 前台（prio 0）始终最小分；后台随等待时间递减分数以防永久饥饿
            effective_score = 0.0 if base_prio <= 0 else max(0.5, float(base_prio) - wait_s * self.aging_per_s)
            w.score = effective_score
            alive.append(w)
        heapq.heapify(alive)

        deferred: list[_WaitItem] = []
        while alive:
            w = heapq.heappop(alive)
            prio_est = 0 if w.score < 0.25 else (1 if w.score < 1.5 else (2 if w.score < 2.5 else 3))
            if self._can_run_unlocked(w.role, prio_est):
                self._active_total += 1
                self._active_role[w.role] = self._active_role.get(w.role, 0) + 1
                if not w.fut.done():
                    w.fut.set_result(True)
            else:
                deferred.append(w)
        heapq.heapify(deferred)
        self._waiters = deferred

    async def acquire(self, *, role: str = "researcher", priority: int = 2, timeout: float | None = None) -> bool:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._can_run_unlocked(role, priority):
                self._active_total += 1
                self._active_role[role] = self._active_role.get(role, 0) + 1
                return True
            self._seq += 1
            fut: asyncio.Future = loop.create_future()
            item = _WaitItem(score=float(priority), seq=self._seq, role=role, fut=fut, enqueued_at=time.monotonic())
            heapq.heappush(self._waiters, item)

        try:
            if timeout is not None:
                await asyncio.wait_for(fut, timeout=timeout)
            else:
                await fut
            return True
        except asyncio.TimeoutError:
            async with self._lock:
                if not fut.done():
                    fut.cancel()
                    return False
                # 若在超时边界正好已被授予槽位，必须归还避免泄露
                self.release_unlocked(role)
                return False
        except asyncio.CancelledError:
            async with self._lock:
                if fut.done() and not fut.cancelled():
                    self.release_unlocked(role)
                elif not fut.done():
                    fut.cancel()
            raise

    def release_unlocked(self, role: str) -> None:
        self._active_total = max(0, self._active_total - 1)
        cur = self._active_role.get(role, 0)
        if cur <= 1:
            self._active_role.pop(role, None)
        else:
            self._active_role[role] = cur - 1
        self._dispatch_unlocked()

    async def release(self, role: str = "researcher") -> None:
        async with self._lock:
            self.release_unlocked(role)

    async def yield_slot(self, *, role: str = "researcher", priority: int = 2) -> None:
        """步边界协作式让渡：若有更高优先级请求等待则让出并重新排队。"""
        should_yield = False
        async with self._lock:
            if any(not w.fut.done() and w.score < float(priority) for w in self._waiters):
                should_yield = True
        if should_yield:
            await self.release(role)
            await asyncio.sleep(0)
            await self.acquire(role=role, priority=priority)

    def stats(self) -> dict[str, Any]:
        return {
            "slots": self.slots,
            "global_limit": self.global_limit,
            "active_total": self._active_total,
            "active_by_role": dict(self._active_role),
            "waiting": sum(1 for w in self._waiters if not w.fut.done()),
        }
