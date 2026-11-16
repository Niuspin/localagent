"""Swarm 分层预算账本（Swarm → Agent）：跟踪步数、墙钟时间、Token 与工具调用配额。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentBudget:
    max_steps: int = 30
    max_wall_s: float = 1800.0
    max_tokens: int = 200_000
    max_tool_calls: int = 120
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0
    started_at: float | None = None
    warned_80: bool = False

    def wall_elapsed(self, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        cur = now if now is not None else time.monotonic()
        return max(0.0, cur - self.started_at)

    def usage_ratios(self, now: float | None = None) -> dict[str, float]:
        return {
            "steps": self.steps / max(1, self.max_steps),
            "wall_s": self.wall_elapsed(now) / max(1.0, float(self.max_wall_s)),
            "tokens": self.tokens / max(1, self.max_tokens),
            "tool_calls": self.tool_calls / max(1, self.max_tool_calls),
        }

    def max_ratio(self, now: float | None = None) -> float:
        return max(self.usage_ratios(now).values())

    def check_exceeded(self, now: float | None = None) -> str | None:
        if self.steps >= self.max_steps:
            return f"步数已达上限 ({self.steps}/{self.max_steps})"
        if self.wall_elapsed(now) >= self.max_wall_s:
            return f"耗时已达上限 ({self.wall_elapsed(now):.1f}s/{self.max_wall_s:.0f}s)"
        if self.tokens >= self.max_tokens:
            return f"Token 已达上限 ({self.tokens}/{self.max_tokens})"
        if self.tool_calls >= self.max_tool_calls:
            return f"工具调用已达上限 ({self.tool_calls}/{self.max_tool_calls})"
        return None


@dataclass
class BudgetLedger:
    max_steps: int = 120
    max_wall_s: float = 600.0
    max_tokens: int = 500_000
    max_tool_calls: int = 400
    started_at: float = field(default_factory=time.monotonic)
    agents: dict[str, AgentBudget] = field(default_factory=dict)
    _warned_swarm: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def from_config(cls, default_budget: dict[str, Any] | None = None) -> "BudgetLedger":
        b = default_budget or {}
        return cls(
            max_steps=max(5, int(b.get("steps", 120) or 120)),
            max_wall_s=max(1.0, float(b.get("wall_s", 600) or 600)),
            max_tokens=max(1000, int(b.get("tokens", 500_000) or 500_000)),
            max_tool_calls=max(10, int(b.get("tool_calls", 400) or 400)),
        )

    def ensure_agent(self, agent_id: str, limit: dict[str, Any] | None = None) -> AgentBudget:
        with self._lock:
            if agent_id in self.agents:
                return self.agents[agent_id]
            lim = limit or {}
            ab = AgentBudget(
                max_steps=min(self.max_steps, max(1, int(lim.get("steps", 30) or 30))),
                max_wall_s=min(self.max_wall_s, max(1.0, float(lim.get("wall_s", self.max_wall_s) or self.max_wall_s))),
                max_tokens=min(self.max_tokens, max(500, int(lim.get("tokens", 200_000) or 200_000))),
                max_tool_calls=min(self.max_tool_calls, max(1, int(lim.get("tool_calls", 120) or 120))),
            )
            self.agents[agent_id] = ab
            return ab

    def mark_started(self, agent_id: str) -> None:
        with self._lock:
            ab = self.ensure_agent(agent_id)
            if ab.started_at is None:
                ab.started_at = time.monotonic()

    def record(self, agent_id: str, *, steps: int = 0, tokens: int = 0, tool_calls: int = 0) -> dict[str, Any]:
        """记账并返回告警信息（若跨过 80% 阈值且此前未告警）。"""
        with self._lock:
            ab = self.ensure_agent(agent_id)
            if ab.started_at is None:
                ab.started_at = time.monotonic()
            ab.steps += max(0, int(steps))
            ab.tokens += max(0, int(tokens))
            ab.tool_calls += max(0, int(tool_calls))

            warn: dict[str, Any] | None = None
            ratio = ab.max_ratio()
            if ratio >= 0.8 and not ab.warned_80:
                ab.warned_80 = True
                warn = {"scope": "agent", "agent_id": agent_id, "ratio": round(ratio, 3)}
            elif self.swarm_ratio() >= 0.8 and not self._warned_swarm:
                self._warned_swarm = True
                warn = {"scope": "swarm", "agent_id": agent_id, "ratio": round(self.swarm_ratio(), 3)}
            return {"warn": warn, "exceeded": self.check(agent_id)}

    def swarm_totals(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "steps": sum(a.steps for a in self.agents.values()),
                "tokens": sum(a.tokens for a in self.agents.values()),
                "tool_calls": sum(a.tool_calls for a in self.agents.values()),
                "wall_s": round(max(0.0, now - self.started_at), 2),
                "limits": {
                    "steps": self.max_steps,
                    "tokens": self.max_tokens,
                    "tool_calls": self.max_tool_calls,
                    "wall_s": self.max_wall_s,
                },
            }

    def swarm_ratio(self) -> float:
        t = self.swarm_totals()
        return max(
            t["steps"] / max(1, self.max_steps),
            t["tokens"] / max(1, self.max_tokens),
            t["tool_calls"] / max(1, self.max_tool_calls),
            t["wall_s"] / max(1.0, self.max_wall_s),
        )

    def check(self, agent_id: str | None = None) -> str | None:
        with self._lock:
            t = self.swarm_totals()
            if t["steps"] >= self.max_steps:
                return f"Swarm 总步数达上限 ({t['steps']}/{self.max_steps})"
            if t["tokens"] >= self.max_tokens:
                return f"Swarm 总 Token 达上限 ({t['tokens']}/{self.max_tokens})"
            if t["tool_calls"] >= self.max_tool_calls:
                return f"Swarm 工具调用达上限 ({t['tool_calls']}/{self.max_tool_calls})"
            if t["wall_s"] >= self.max_wall_s:
                return f"Swarm 总耗时达上限 ({t['wall_s']:.1f}s/{self.max_wall_s:.0f}s)"
            if agent_id and agent_id in self.agents:
                return self.agents[agent_id].check_exceeded()
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "totals": self.swarm_totals(),
                "ratio": round(self.swarm_ratio(), 3),
                "agents": {
                    aid: {
                        "steps": a.steps,
                        "max_steps": a.max_steps,
                        "tokens": a.tokens,
                        "max_tokens": a.max_tokens,
                        "tool_calls": a.tool_calls,
                        "wall_s": round(a.wall_elapsed(), 2),
                    }
                    for aid, a in self.agents.items()
                },
            }
