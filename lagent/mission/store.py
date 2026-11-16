"""Mission 持久化存储：原子 state.json、events.jsonl 与工作区 MISSION.md 同步。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..journal import atomic_write, sha256_bytes
from .model import (
    Mission,
    apply_mission_md_edits,
    render_mission_md,
)


def _safe_id(mission_id: str) -> str:
    s = (mission_id or "").strip()
    if not s or "/" in s or "\\" in s or ".." in s:
        raise ValueError(f"非法的 mission_id: {mission_id!r}")
    return s


class MissionStore:
    """管理所有 Mission 的磁盘状态与事件流。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._seq_cache: dict[str, int] = {}

    def mission_dir(self, mission_id: str) -> Path:
        return self.root / _safe_id(mission_id)

    def state_path(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "state.json"

    def events_path(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "events.jsonl"

    def mission_md_path(self, mission: Mission) -> Path:
        ws = (mission.workspace or "").strip()
        if ws:
            base = Path(ws) / ".localagent" / "missions" / _safe_id(mission.id)
        else:
            base = self.mission_dir(mission.id)
        return base / "MISSION.md"

    def list(self) -> list[Mission]:
        with self._lock:
            out: list[Mission] = []
            if not self.root.is_dir():
                return out
            for child in sorted(self.root.iterdir(), key=lambda p: p.name):
                if not child.is_dir():
                    continue
                sp = child / "state.json"
                if not sp.is_file():
                    continue
                m = self.get(child.name)
                if m is not None:
                    out.append(m)
            out.sort(key=lambda m: (m.updated_at or 0.0, m.created_at or 0.0), reverse=True)
            return out

    def get(self, mission_id: str) -> Mission | None:
        with self._lock:
            try:
                sp = self.state_path(mission_id)
            except ValueError:
                return None
            if not sp.is_file():
                return None
            try:
                raw = json.loads(sp.read_text("utf-8"))
                if not isinstance(raw, dict):
                    return None
                return Mission.from_dict(raw)
            except Exception:
                return None

    def save(self, mission: Mission, *, sync_md: bool = True, prefer_disk: bool = True) -> Mission:
        with self._lock:
            mission.updated_at = time.time()
            mdir = self.mission_dir(mission.id)
            mdir.mkdir(parents=True, exist_ok=True)
            if sync_md:
                self._sync_md_unlocked(mission, prefer_disk=prefer_disk)
            payload = json.dumps(mission.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
            atomic_write(self.state_path(mission.id), payload)
            return mission

    def sync_mission_md(self, mission: Mission) -> bool:
        """在 Episode 前后同步：若用户外部修改过 MISSION.md，先解析回 Mission，再写回规范视图。"""
        with self._lock:
            changed = self._sync_md_unlocked(mission, prefer_disk=True)
            if changed:
                mission.updated_at = time.time()
                payload = json.dumps(mission.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
                atomic_write(self.state_path(mission.id), payload)
            return changed

    def read_mission_md(self, mission: Mission) -> str:
        with self._lock:
            p = self.mission_md_path(mission)
            if p.is_file():
                return p.read_text("utf-8")
            txt = render_mission_md(mission)
            atomic_write(p, txt.encode("utf-8"))
            mission.mission_md_sha = sha256_bytes(txt.encode("utf-8"))
            return txt

    def write_mission_md(self, mission: Mission, content: str) -> Mission:
        """显式通过 API / /mission edit 写入新的 MISSION.md 并同步模型状态。"""
        with self._lock:
            apply_mission_md_edits(mission, content)
            p = self.mission_md_path(mission)
            rendered = render_mission_md(mission)
            raw = rendered.encode("utf-8")
            atomic_write(p, raw)
            mission.mission_md_sha = sha256_bytes(raw)
            self.save(mission, sync_md=False)
            return mission

    def _sync_md_unlocked(self, mission: Mission, *, prefer_disk: bool) -> bool:
        p = self.mission_md_path(mission)
        p.parent.mkdir(parents=True, exist_ok=True)
        changed = False
        if p.is_file():
            disk_bytes = p.read_bytes()
            disk_sha = sha256_bytes(disk_bytes)
            if prefer_disk and mission.mission_md_sha and disk_sha != mission.mission_md_sha:
                disk_text = disk_bytes.decode("utf-8", "replace")
                changed = apply_mission_md_edits(mission, disk_text) or True
        rendered = render_mission_md(mission)
        raw = rendered.encode("utf-8")
        new_sha = sha256_bytes(raw)
        if not p.is_file() or new_sha != mission.mission_md_sha:
            atomic_write(p, raw)
            mission.mission_md_sha = new_sha
        return changed

    def append_event(self, mission_id: str, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            mdir = self.mission_dir(mission_id)
            mdir.mkdir(parents=True, exist_ok=True)
            ep = self.events_path(mission_id)
            seq = self._next_seq_unlocked(mission_id, ep)
            ev = {
                "seq": seq,
                "ts": round(time.time(), 4),
                "kind": kind,
                "mission_id": mission_id,
                "data": dict(data or {}),
            }
            line = json.dumps(ev, ensure_ascii=False) + "\n"
            with ep.open("a", encoding="utf-8") as f:
                f.write(line)
            return ev

    def read_events(self, mission_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            try:
                ep = self.events_path(mission_id)
            except ValueError:
                return []
            if not ep.is_file():
                return []
            out: list[dict[str, Any]] = []
            for ln in ep.read_text("utf-8", "replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                    if int(ev.get("seq") or 0) > after_seq:
                        out.append(ev)
                except Exception:
                    continue
            return out[-limit:] if limit and len(out) > limit else out

    def _next_seq_unlocked(self, mission_id: str, ep: Path) -> int:
        if mission_id not in self._seq_cache:
            last = 0
            if ep.is_file():
                for ln in ep.read_text("utf-8", "replace").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        last = max(last, int(json.loads(ln).get("seq") or 0))
                    except Exception:
                        continue
            self._seq_cache[mission_id] = last
        self._seq_cache[mission_id] += 1
        return self._seq_cache[mission_id]

    def delete(self, mission_id: str) -> bool:
        import shutil
        with self._lock:
            try:
                mdir = self.mission_dir(mission_id)
            except ValueError:
                return False
            self._seq_cache.pop(mission_id, None)
            if not mdir.exists():
                return False
            shutil.rmtree(mdir, ignore_errors=True)
            return True
