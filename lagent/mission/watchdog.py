"""Mission 停滞侦测（Watchdog）与升级链：nudge -> replan -> review -> pause。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

ESCALATION_ACTIONS = {
    1: "nudge",    # 自省提示：提醒检查假设与更换路径
    2: "replan",   # 强制重规划：保存 handoff 并重组里程碑/策略
    3: "review",   # 触发 reviewer 独立审查（Swarm / 子代理或自检）
    4: "pause",    # 暂停并通知用户接管
}


@dataclass
class StallSignal:
    level: int
    action: str       # nudge | replan | review | pause
    reason: str       # repeated_tool_call | no_progress | consecutive_verify_failures
    detail: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "ts": self.ts,
        }


def tool_fingerprint(tool: str, args: dict[str, Any] | None = None) -> str:
    payload = json.dumps({"tool": tool or "", "args": args or {}}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class StallWatchdog:
    """侦测重复调用、无进展与连续验证失败，并逐级升级恢复。"""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        repeat_threshold: int = 3,
        no_progress_threshold: int = 6,
        verify_fail_threshold: int = 3,
    ):
        self.state = state if state is not None else {
            "consecutive_failures": 0,
            "no_progress_steps": 0,
            "escalation_level": 0,
            "last_fingerprint": "",
            "fingerprint_count": 0,
            "last_diff_hash": "",
            "last_progress_marker": "",
            "history": [],
        }
        self.repeat_threshold = max(2, int(repeat_threshold))
        self.no_progress_threshold = max(2, int(no_progress_threshold))
        self.verify_fail_threshold = max(2, int(verify_fail_threshold))

    def observe_step(
        self,
        *,
        tool: str = "",
        args: dict[str, Any] | None = None,
        diff_hash: str = "",
        progress_marker: str = "",
    ) -> StallSignal | None:
        progressed = False
        if diff_hash and diff_hash != self.state.get("last_diff_hash", ""):
            self.state["last_diff_hash"] = diff_hash
            progressed = True
        if progress_marker and progress_marker != self.state.get("last_progress_marker", ""):
            self.state["last_progress_marker"] = progress_marker
            progressed = True

        if progressed:
            self.state["no_progress_steps"] = 0
            # 有实质进展时清零升级计数
            self.state["escalation_level"] = 0
        else:
            self.state["no_progress_steps"] = int(self.state.get("no_progress_steps") or 0) + 1

        if tool:
            fp = tool_fingerprint(tool, args)
            if fp == self.state.get("last_fingerprint"):
                self.state["fingerprint_count"] = int(self.state.get("fingerprint_count") or 0) + 1
            else:
                self.state["last_fingerprint"] = fp
                self.state["fingerprint_count"] = 1

            cnt = int(self.state.get("fingerprint_count") or 0)
            if cnt >= self.repeat_threshold:
                return self._escalate(
                    "repeated_tool_call",
                    f"工具 {tool} 连续 {cnt} 次相同调用无变化 (fingerprint={fp})",
                )

        no_prog = int(self.state.get("no_progress_steps") or 0)
        if no_prog >= self.no_progress_threshold:
            # 触发后重置步数计数以便下一步观察升级而非瞬间跳级
            self.state["no_progress_steps"] = 0
            return self._escalate(
                "no_progress",
                f"连续 {no_prog} 步无 git diff 或里程碑进度变化",
            )
        return None

    def observe_verify(self, passed: bool, *, summary: str = "") -> StallSignal | None:
        if passed:
            self.state["consecutive_failures"] = 0
            self.state["escalation_level"] = 0
            return None
        fails = int(self.state.get("consecutive_failures") or 0) + 1
        self.state["consecutive_failures"] = fails
        if fails >= self.verify_fail_threshold:
            return self._escalate(
                "consecutive_verify_failures",
                f"连续 {fails} 次验收失败" + (f": {summary[:160]}" if summary else ""),
            )
        return None

    def _escalate(self, reason: str, detail: str) -> StallSignal:
        level = min(4, int(self.state.get("escalation_level") or 0) + 1)
        self.state["escalation_level"] = level
        action = ESCALATION_ACTIONS[level]
        sig = StallSignal(level=level, action=action, reason=reason, detail=detail, ts=time.time())
        hist = list(self.state.get("history") or [])
        hist.append(sig.to_dict())
        self.state["history"] = hist[-20:]
        return sig

    def reset_level(self) -> None:
        self.state["escalation_level"] = 0
        self.state["fingerprint_count"] = 0
        self.state["no_progress_steps"] = 0
        self.state["consecutive_failures"] = 0
