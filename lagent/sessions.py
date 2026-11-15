"""会话存储：每个会话一个 JSON 文件（原子写入）。history 是发给模型的原始消息，ui 是界面用的消息块。"""
from __future__ import annotations

import copy
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .journal import atomic_write
from .parts import PART_TYPES, content_to_parts, normalize_part, parts_to_content

SID_RE = re.compile(r"^[a-f0-9]{12}$")
SESSION_SCHEMA_VERSION = 2


def migrate_v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
    """将 v1 会话字典无损升级为 Schema v2（保留全部旧字段以维持前端与测试兼容）。"""
    out = copy.deepcopy(d)
    out["v"] = SESSION_SCHEMA_VERSION
    out["schema_version"] = SESSION_SCHEMA_VERSION
    out["agent_id"] = out.get("agent_id") or "main"
    out.setdefault("swarm_id", None)
    out.setdefault("mission_id", None)

    ui = []
    for item in out.get("ui") or []:
        if not isinstance(item, dict):
            continue
        m = dict(item)
        m.setdefault("agent_id", out["agent_id"])
        if "parts" not in m:
            m["parts"] = content_to_parts(m.get("text", ""), images=m.get("images"), files=m.get("files"))
        else:
            m["parts"] = [normalize_part(p) for p in (m.get("parts") or [])]
        ui.append(m)
    out["ui"] = ui

    hist = []
    for item in out.get("history") or []:
        if not isinstance(item, dict):
            continue
        m = dict(item)
        m.setdefault("agent_id", out["agent_id"])
        if "parts" not in m and m.get("content") is not None:
            m["parts"] = content_to_parts(m["content"])
        elif "parts" in m and isinstance(m["parts"], list):
            m["parts"] = [normalize_part(p) for p in m["parts"]]
        hist.append(m)
    out["history"] = hist
    return out


def downgrade_v2_to_v1(d: dict[str, Any]) -> dict[str, Any]:
    """将 Schema v2 会话字典降级回 v1 兼容结构（用于回滚与双向迁移校验）。"""
    out = copy.deepcopy(d)
    out["v"] = 1
    for k in ("schema_version", "agent_id", "swarm_id", "mission_id"):
        out.pop(k, None)

    ui = []
    for item in out.get("ui") or []:
        if not isinstance(item, dict):
            continue
        m = dict(item)
        parts = m.pop("parts", None)
        m.pop("agent_id", None)
        if isinstance(parts, list):
            if "text" not in m:
                text_bits = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
                m["text"] = "\n".join(x for x in text_bits if x != "") if any(x != "" for x in text_bits) else ""
            if "images" not in m:
                imgs = [p["url"] for p in parts if isinstance(p, dict) and p.get("type") == "image" and p.get("url")]
                if imgs:
                    m["images"] = imgs
            if "files" not in m:
                files_ = [p.get("path") or p.get("name") for p in parts if isinstance(p, dict) and p.get("type") == "file" and (p.get("path") or p.get("name"))]
                if files_:
                    m["files"] = files_
        ui.append(m)
    out["ui"] = ui

    hist = []
    for item in out.get("history") or []:
        if not isinstance(item, dict):
            continue
        m = dict(item)
        parts = m.pop("parts", None)
        m.pop("agent_id", None)
        if "content" not in m and isinstance(parts, list):
            m["content"] = parts_to_content(parts, vision=True)
        hist.append(m)
    out["history"] = hist
    return out


class Session:
    def __init__(self, sid: str | None = None, workspace: str = ""):
        now = time.time()
        self.id = sid or uuid.uuid4().hex[:12]
        self.schema_version: int = SESSION_SCHEMA_VERSION
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
        self.agent_id: str = "main"           # 默认归属主代理
        self.swarm_id: str | None = None
        self.mission_id: str | None = None
        self.active_run: str | None = None   # 仅内存
        self._migrated_from_v1: bool = False  # 仅内存：首次写回前备份 v1 原文件

    # ------------------------------------------------------------ 序列化
    def to_disk(self) -> dict:
        return {"v": SESSION_SCHEMA_VERSION, "schema_version": SESSION_SCHEMA_VERSION,
                "id": self.id, "title": self.title, "workspace": self.workspace, "created": self.created, "updated": self.updated,
                "pinned": self.pinned, "tainted": self.tainted, "summary": self.summary, "todos": self.todos, "ui": self.ui,
                "history": self.history, "usage": self.usage, "nonce": self.nonce, "user_domains": self.user_domains, "calib": self.calib,
                "tool_groups": self.tool_groups, "seen": self.seen, "kv_prune_upto": self.kv_prune_upto,
                "permission_mode": self.permission_mode, "shell_cwd": self.shell_cwd, "shell_env": self.shell_env,
                "agent_id": self.agent_id, "swarm_id": self.swarm_id, "mission_id": self.mission_id}

    @classmethod
    def from_disk(cls, d: dict) -> "Session":
        raw_ver = int(d.get("schema_version") or d.get("v") or 1)
        migrated = raw_ver < SESSION_SCHEMA_VERSION
        src = migrate_v1_to_v2(d) if migrated else d
        s = cls(src["id"], src.get("workspace", ""))
        for k in ("title", "created", "updated", "pinned", "tainted", "summary", "todos", "ui", "history", "usage", "nonce",
                  "user_domains", "calib", "tool_groups", "seen", "kv_prune_upto", "permission_mode", "shell_cwd", "shell_env",
                  "agent_id", "swarm_id", "mission_id"):
            if k in src:
                setattr(s, k, src[k])
        s.schema_version = SESSION_SCHEMA_VERSION
        s._migrated_from_v1 = migrated
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
                "msg_count": len(self.ui), "pinned": self.pinned, "schema_version": self.schema_version}

    def view(self) -> dict:
        return {"id": self.id, "schema_version": self.schema_version,
                "agent_id": self.agent_id, "swarm_id": self.swarm_id, "mission_id": self.mission_id,
                "title": self.title, "workspace": self.workspace, "created": self.created, "updated": self.updated,
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
            target = self.dir / f"{s.id}.json"
            if getattr(s, "_migrated_from_v1", False):
                bak = self.dir / f"{s.id}.json.bak"
                if target.is_file() and not bak.exists():
                    try:
                        atomic_write(bak, target.read_bytes())
                    except OSError:
                        pass
                s._migrated_from_v1 = False
            s.schema_version = SESSION_SCHEMA_VERSION
            self._cache[s.id] = s
            self._metas[s.id] = s.meta()
            atomic_write(target, json.dumps(s.to_disk(), ensure_ascii=False).encode("utf-8"))
            self._save_index()

    def delete(self, sid: str) -> bool:
        with self._lock:
            existed = (sid in self._cache) or (sid in self._metas) or (self.dir / f"{sid}.json").is_file()
            self._cache.pop(sid, None)
            self._metas.pop(sid, None)
            if not existed:
                return False
            for suffix in (".json", ".json.bak"):
                try:
                    (self.dir / f"{sid}{suffix}").unlink()
                except OSError:
                    pass
            self._save_index()
            return True
