"""Swarm 持久化存储（data/swarms/<swarm_id>/state.json + events.jsonl）：支持崩溃后恢复。"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..journal import atomic_write
from .graph import TaskGraph

SWARM_ID_OK = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class SwarmStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _sdir(self, swarm_id: str) -> Path:
        if not swarm_id or not SWARM_ID_OK.match(swarm_id):
            raise ValueError(f"非法的 swarm_id: {swarm_id!r}")
        p = (self.dir / swarm_id).resolve()
        p.relative_to(self.dir.resolve())
        return p

    def mailbox_path(self, swarm_id: str) -> Path:
        sdir = self._sdir(swarm_id)
        sdir.mkdir(parents=True, exist_ok=True)
        return sdir / "mailbox.jsonl"

    def save_state(self, swarm_id: str, state: dict[str, Any]) -> None:
        sdir = self._sdir(swarm_id)
        sdir.mkdir(parents=True, exist_ok=True)
        payload = {**state, "updated_at": round(time.time(), 3)}
        with self._lock:
            atomic_write(sdir / "state.json", json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def load_state(self, swarm_id: str) -> dict[str, Any] | None:
        try:
            p = self._sdir(swarm_id) / "state.json"
            if not p.is_file():
                return None
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def append_event(self, swarm_id: str, event: dict[str, Any]) -> int:
        sdir = self._sdir(swarm_id)
        sdir.mkdir(parents=True, exist_ok=True)
        ev_file = sdir / "events.jsonl"
        with self._lock:
            seq = 1
            if ev_file.is_file():
                try:
                    seq = sum(1 for ln in ev_file.read_text("utf-8").splitlines() if ln.strip()) + 1
                except OSError:
                    seq = 1
            entry = {"seq": seq, **event}
            with open(ev_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
            return seq

    def read_events(self, swarm_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        try:
            ev_file = self._sdir(swarm_id) / "events.jsonl"
            if not ev_file.is_file():
                return []
            out: list[dict[str, Any]] = []
            for ln in ev_file.read_text("utf-8").splitlines():
                if not ln.strip():
                    continue
                d = json.loads(ln)
                if int(d.get("seq", 0)) > after_seq:
                    out.append(d)
            return out
        except (OSError, ValueError):
            return []

    def list_states(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sub in self.dir.iterdir():
            if not sub.is_dir() or not SWARM_ID_OK.match(sub.name):
                continue
            st = self.load_state(sub.name)
            if not st:
                continue
            if session_id and st.get("session_id") != session_id:
                continue
            out.append(st)
        out.sort(key=lambda x: float(x.get("updated_at") or x.get("created_at") or 0.0), reverse=True)
        return out

    @staticmethod
    def prepare_resume_graph(graph_dict: dict[str, Any]) -> TaskGraph:
        """崩溃恢复时将未完成的 running 节点安全重置为 ready，保留已完成节点与产物。"""
        g = TaskGraph.from_dict(graph_dict)
        for node in g.nodes.values():
            if node.status == "running":
                node.status = "ready"
                node.started_at = None
        g.refresh_ready()
        return g
