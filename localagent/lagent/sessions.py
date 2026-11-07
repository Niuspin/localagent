"""会话存储：每个会话一个 JSON 文件（原子写入）。history 是发给模型的原始消息，ui 是界面用的消息块。"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .journal import atomic_write

SID_RE = re.compile(r"^[a-f0-9]{12}$")


class Session:
    def __init__(self, sid: str | None = None, workspace: str = ""):
        now = time.time()
        self.id = sid or uuid.uuid4().hex[:12]
        self.title = "新对话"
        self.workspace = workspace
        self.created = self.updated = now
        self.pinned = False
        self.tainted = False
        self.summary: str | None = None
        self.todos: list[dict] = []
        self.ui: list[dict] = []
        self.history: list[dict] = []
        self.usage: dict = {"ctx": 0, "used": 0, "pct": 0.0, "summarized": False}
        self.nonce = secrets.token_hex(4)
        self.user_domains: list[str] = []
        self.calib = 1.0
        self.active_run: str | None = None   # 仅内存

    # ------------------------------------------------------------ 序列化
    def to_disk(self) -> dict:
        return {"v": 1, "id": self.id, "title": self.title, "workspace": self.workspace, "created": self.created, "updated": self.updated,
                "pinned": self.pinned, "tainted": self.tainted, "summary": self.summary, "todos": self.todos, "ui": self.ui,
                "history": self.history, "usage": self.usage, "nonce": self.nonce, "user_domains": self.user_domains, "calib": self.calib}

    @classmethod
    def from_disk(cls, d: dict) -> "Session":
        s = cls(d["id"], d.get("workspace", ""))
        for k in ("title", "created", "updated", "pinned", "tainted", "summary", "todos", "ui", "history", "usage", "nonce",
                  "user_domains", "calib"):
            if k in d:
                setattr(s, k, d[k])
        # 上次异常退出时残留的 running 消息
        for m in s.ui:
            if m.get("role") == "assistant" and m.get("status") == "running":
                m["status"] = "cancelled"
                for b in m.get("blocks", []):
                    if b.get("type") == "tool" and b.get("status") in ("running", "pending_approval"):
                        b["status"] = "error"
                        b["content"] = (b.get("content") or "") + "（会话中断）"
                        b["approval"] = None
        return s

    def meta(self) -> dict:
        return {"id": self.id, "title": self.title, "created": self.created, "updated": self.updated, "workspace": self.workspace,
                "msg_count": len(self.ui), "pinned": self.pinned}

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "workspace": self.workspace, "created": self.created, "updated": self.updated,
                "pinned": self.pinned, "tainted": self.tainted, "summary": self.summary,
                "context": {**self.usage, "summarized": bool(self.summary)}, "todos": self.todos, "active_run": self.active_run,
                "messages": self.ui}


class SessionStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Session] = {}
        self._lock = threading.RLock()
        for f in self.dir.glob("*.json"):
            if SID_RE.match(f.stem):
                try:
                    s = Session.from_disk(json.loads(f.read_text("utf-8")))
                    self._cache[s.id] = s
                except (OSError, ValueError, KeyError):
                    continue

    def create(self, workspace: str = "") -> Session:
        with self._lock:
            s = Session(workspace=workspace)
            self._cache[s.id] = s
            self.save(s)
            return s

    def get(self, sid: str) -> Session | None:
        return self._cache.get(sid) if SID_RE.match(sid or "") else None

    def list(self) -> list[dict]:
        with self._lock:
            return [s.meta() for s in sorted(self._cache.values(), key=lambda x: x.updated, reverse=True)]

    def save(self, s: Session) -> None:
        with self._lock:
            atomic_write(self.dir / f"{s.id}.json", json.dumps(s.to_disk(), ensure_ascii=False).encode("utf-8"))

    def delete(self, sid: str) -> bool:
        with self._lock:
            s = self._cache.pop(sid, None)
            if not s:
                return False
            try:
                (self.dir / f"{sid}.json").unlink()
            except OSError:
                pass
            return True
