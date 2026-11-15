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
        self.tool_groups: list[str] = []      # 已加载的扩展工具组
        self.seen: dict[str, str] = {}        # 已读/写文本文件 sha256（跨重启保留）
        self.kv_prune_upto: int = 0           # 单调递增的 Append-Only KV 前缀裁剪水位线
        self.permission_mode: str | None = None  # 会话级权限模式覆盖（None 表示跟随全局配置）
        self.shell_cwd: str = ""              # 会话级终端当前相对工作目录
        self.shell_env: dict[str, str] = {}   # 会话级安全终端环境变量覆盖
        self.active_run: str | None = None   # 仅内存

    # ------------------------------------------------------------ 序列化
    def to_disk(self) -> dict:
        return {"v": 1, "id": self.id, "title": self.title, "workspace": self.workspace, "created": self.created, "updated": self.updated,
                "pinned": self.pinned, "tainted": self.tainted, "summary": self.summary, "todos": self.todos, "ui": self.ui,
                "history": self.history, "usage": self.usage, "nonce": self.nonce, "user_domains": self.user_domains, "calib": self.calib,
                "tool_groups": self.tool_groups, "seen": self.seen, "kv_prune_upto": self.kv_prune_upto,
                "permission_mode": self.permission_mode, "shell_cwd": self.shell_cwd, "shell_env": self.shell_env}

    @classmethod
    def from_disk(cls, d: dict) -> "Session":
        s = cls(d["id"], d.get("workspace", ""))
        for k in ("title", "created", "updated", "pinned", "tainted", "summary", "todos", "ui", "history", "usage", "nonce",
                  "user_domains", "calib", "tool_groups", "seen", "kv_prune_upto", "permission_mode", "shell_cwd", "shell_env"):
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
                "permission_mode": self.permission_mode,
                "context": {**self.usage, "summarized": bool(self.summary)}, "todos": self.todos, "active_run": self.active_run,
                "messages": self.ui}


class SessionStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.dir / "sessions_index.json"
        self._cache: dict[str, Session] = {}
        self._metas: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._load_index()

    def _load_index(self) -> None:
        files = {f.stem: f for f in self.dir.glob("*.json") if SID_RE.match(f.stem)}
        if self._index_path.is_file():
            try:
                idx = json.loads(self._index_path.read_text("utf-8"))
                if isinstance(idx, dict) and set(idx.keys()) == set(files.keys()):
                    for sid, m in idx.items():
                        if isinstance(m, dict) and m.get("id") == sid:
                            self._metas[sid] = m
                    return
            except (OSError, ValueError):
                pass
        for sid, f in files.items():
            try:
                s = Session.from_disk(json.loads(f.read_text("utf-8")))
                self._cache[sid] = s
                self._metas[sid] = s.meta()
            except (OSError, ValueError, KeyError):
                continue
        if self._metas:
            self._save_index()

    def _save_index(self) -> None:
        try:
            atomic_write(self._index_path, json.dumps(self._metas, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass

    def create(self, workspace: str = "") -> Session:
        with self._lock:
            s = Session(workspace=workspace)
            self._cache[s.id] = s
            self._metas[s.id] = s.meta()
            self.save(s)
            return s

    def get(self, sid: str) -> Session | None:
        if not SID_RE.match(sid or ""):
            return None
        with self._lock:
            if sid in self._cache:
                return self._cache[sid]
            f = self.dir / f"{sid}.json"
            if not f.is_file():
                return None
            try:
                s = Session.from_disk(json.loads(f.read_text("utf-8")))
                self._cache[sid] = s
                self._metas[sid] = s.meta()
                return s
            except (OSError, ValueError, KeyError):
                return None

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in sorted(self._metas.values(), key=lambda x: x.get("updated", 0), reverse=True)]

    def save(self, s: Session) -> None:
        with self._lock:
            self._cache[s.id] = s
            self._metas[s.id] = s.meta()
            atomic_write(self.dir / f"{s.id}.json", json.dumps(s.to_disk(), ensure_ascii=False).encode("utf-8"))
            self._save_index()

    def delete(self, sid: str) -> bool:
        with self._lock:
            existed = (sid in self._cache) or (sid in self._metas) or (self.dir / f"{sid}.json").is_file()
            self._cache.pop(sid, None)
            self._metas.pop(sid, None)
            if not existed:
                return False
            try:
                (self.dir / f"{sid}.json").unlink()
            except OSError:
                pass
            self._save_index()
            return True
