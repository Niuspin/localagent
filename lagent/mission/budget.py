"""Mission 预算控制器：wall / tokens / steps / disk_mb（80% 警告 + 100% 暂停）。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..swarm.budget import BudgetLedger


@dataclass
class BudgetStatus:
    warned_now: list[str]
    exceeded: str | None
    ratios: dict[str, float]
    snapshot: dict[str, Any]


def dir_size_bytes(path: Path, *, max_entries: int = 20_000) -> int:
    """安全估算目录磁盘占用，跳过 .git/venv/node_modules 等无关缓存避免卡顿。"""
    if not path or not path.exists():
        return 0
    total = 0
    seen = 0
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            seen += 1
            if seen > max_entries:
                return total
            fp = Path(root) / fn
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


class MissionBudgetTracker:
    """复用 Swarm BudgetLedger 并补充磁盘增长与 80%/100% 持久化统计。"""

    def __init__(self, budget_dict: dict[str, Any], *, workspace: str | Path | None = None, mission_dir: Path | None = None):
        self.cfg = budget_dict
        self.workspace = Path(workspace) if workspace else None
        self.mission_dir = Path(mission_dir) if mission_dir else None
        self.wall_limit = max(1.0, float(budget_dict.get("wall_s") or 14400.0))
        self.token_limit = max(1, int(budget_dict.get("tokens") or 2_000_000))
        self.step_limit = max(1, int(budget_dict.get("steps") or 400))
        self.disk_limit_mb = max(1.0, float(budget_dict.get("disk_mb") or 2048.0))

        self.base_wall_s = float(budget_dict.get("used_wall_s") or 0.0)
        self.warned: set[str] = set(str(x) for x in (budget_dict.get("warned") or []))
        self._baseline_bytes = self._measure_raw_bytes()
        self._run_t0 = time.monotonic()

        self.ledger = BudgetLedger(
            max_steps=self.step_limit,
            max_wall_s=max(1.0, self.wall_limit - self.base_wall_s),
            max_tokens=self.token_limit,
            max_tool_calls=max(self.step_limit * 4, 400),
        )
        # 恢复历史步数与 token 用量
        prev_steps = int(budget_dict.get("used_steps") or 0)
        prev_tokens = int(budget_dict.get("used_tokens") or 0)
        prev_calls = int(budget_dict.get("used_tool_calls") or 0)
        if prev_steps or prev_tokens or prev_calls:
            self.ledger.record("mission", steps=prev_steps, tokens=prev_tokens, tool_calls=prev_calls)

    def _measure_raw_bytes(self) -> int:
        # 若测试或运行时希望从 0 计起，默认记录启动时工作区+mission_dir基准；如果 mission_dir 已经有文件则计其增量
        ws_b = dir_size_bytes(self.workspace) if self.workspace and self.workspace.is_dir() else 0
        md_b = dir_size_bytes(self.mission_dir) if self.mission_dir and self.mission_dir.is_dir() else 0
        return ws_b + md_b

    def measure_disk_mb(self) -> float:
        cur = self._measure_raw_bytes()
        delta = max(0, cur - self._baseline_bytes)
        prev_mb = float(self.cfg.get("used_disk_mb") or 0.0)
        return max(prev_mb, round(delta / (1024 * 1024), 4))

    def record(self, *, steps: int = 0, tokens: int = 0, tool_calls: int = 0, agent_id: str = "mission") -> BudgetStatus:
        self.ledger.record(agent_id, steps=steps, tokens=tokens, tool_calls=tool_calls)
        return self.evaluate()

    def evaluate(self, *, disk_mb_override: float | None = None, wall_s_override: float | None = None) -> BudgetStatus:
        totals = self.ledger.swarm_totals()
        used_wall = wall_s_override if wall_s_override is not None else (self.base_wall_s + (time.monotonic() - self._run_t0))
        used_tokens = int(totals["tokens"])
        used_steps = int(totals["steps"])
        used_calls = int(totals["tool_calls"])
        used_disk = float(disk_mb_override) if disk_mb_override is not None else self.measure_disk_mb()

        ratios = {
            "wall_s": used_wall / self.wall_limit,
            "tokens": used_tokens / self.token_limit,
            "steps": used_steps / self.step_limit,
            "disk_mb": used_disk / self.disk_limit_mb,
        }

        warned_now: list[str] = []
        exceeded: str | None = None
        for dim, ratio in ratios.items():
            if ratio >= 0.8 and dim not in self.warned:
                self.warned.add(dim)
                warned_now.append(dim)
            if ratio >= 1.0 and exceeded is None:
                exceeded = dim

        self.cfg["used_wall_s"] = round(used_wall, 3)
        self.cfg["used_tokens"] = used_tokens
        self.cfg["used_steps"] = used_steps
        self.cfg["used_tool_calls"] = used_calls
        self.cfg["used_disk_mb"] = round(used_disk, 4)
        self.cfg["warned"] = sorted(self.warned)

        snap = {
            "wall_s": self.wall_limit,
            "tokens": self.token_limit,
            "steps": self.step_limit,
            "disk_mb": self.disk_limit_mb,
            "used_wall_s": self.cfg["used_wall_s"],
            "used_tokens": used_tokens,
            "used_steps": used_steps,
            "used_tool_calls": used_calls,
            "used_disk_mb": self.cfg["used_disk_mb"],
            "ratios": {k: round(v, 4) for k, v in ratios.items()},
            "warned": list(self.cfg["warned"]),
        }
        return BudgetStatus(warned_now=warned_now, exceeded=exceeded, ratios=ratios, snapshot=snap)
