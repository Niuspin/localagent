"""Swarm 类型化邮箱（append-only JSONL）：隔离代理间对话历史，仅通过结构化消息通信。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MSG_KINDS: tuple[str, ...] = ("handoff", "question", "result", "alert", "cancel")


@dataclass
class MailMessage:
    seq: int
    from_agent: str
    to_agent: str
    kind: str
    body: str
    artifacts: list[str] = field(default_factory=list)
    tainted: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MailMessage":
        return cls(
            seq=int(d.get("seq", 0)),
            from_agent=str(d.get("from_agent", "")),
            to_agent=str(d.get("to_agent", "")),
            kind=str(d.get("kind", "handoff")),
            body=str(d.get("body", "")),
            artifacts=list(d.get("artifacts") or []),
            tainted=bool(d.get("tainted", False)),
            ts=float(d.get("ts", 0.0)),
        )


class Mailbox:
    """进程安全 + 磁盘 append-only 的消息邮箱。"""

    def __init__(self, log_path: Path | None = None):
        self.log_path = Path(log_path) if log_path else None
        self._lock = threading.RLock()
        self._messages: list[MailMessage] = []
        self._ack: dict[str, int] = {}
        if self.log_path and self.log_path.is_file():
            self._reload()

    def _reload(self) -> None:
        if not self.log_path:
            return
        with self._lock:
            self._messages.clear()
            try:
                for line in self.log_path.read_text("utf-8").splitlines():
                    if not line.strip():
                        continue
                    self._messages.append(MailMessage.from_dict(json.loads(line)))
            except (OSError, ValueError):
                pass

    def post(
        self,
        from_agent: str,
        to_agent: str,
        kind: str,
        body: str,
        *,
        artifacts: list[str] | None = None,
        tainted: bool = False,
    ) -> MailMessage:
        k = kind if kind in MSG_KINDS else "handoff"
        with self._lock:
            seq = (self._messages[-1].seq + 1) if self._messages else 1
            msg = MailMessage(
                seq=seq,
                from_agent=str(from_agent),
                to_agent=str(to_agent),
                kind=k,
                body=str(body),
                artifacts=list(artifacts or []),
                tainted=bool(tainted),
                ts=round(time.time(), 3),
            )
            self._messages.append(msg)
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
                    f.flush()
            return msg

    def read(
        self,
        agent_id: str,
        *,
        unread_only: bool = True,
        after_seq: int | None = None,
        mark_read: bool = False,
    ) -> list[MailMessage]:
        with self._lock:
            floor = after_seq if after_seq is not None else (self._ack.get(agent_id, 0) if unread_only else 0)
            out = [
                m for m in self._messages
                if m.seq > floor and (m.to_agent == agent_id or m.to_agent == "*")
            ]
            if mark_read and out:
                self._ack[agent_id] = max(self._ack.get(agent_id, 0), out[-1].seq)
            return list(out)

    def ack(self, agent_id: str, seq: int) -> None:
        with self._lock:
            self._ack[agent_id] = max(self._ack.get(agent_id, 0), int(seq))

    def all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [m.to_dict() for m in self._messages[-limit:]]
