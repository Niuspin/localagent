"""文件变更日志 + 快照 + 回收站：所有 agent 的修改都可撤销，删除永远只是“移入回收站”。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SNAPSHOT_LIMIT = 50 * 1024 * 1024
_ID_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})\.(\d+)$")


def sha256_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + fsync + os.replace；保留已有文件权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".la-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Journal:
    def __init__(self, data_dir: Path, snapshot_limit: int = SNAPSHOT_LIMIT):
        self.dir = Path(data_dir)
        self.jdir, self.sdir, self.tdir = self.dir / "journal", self.dir / "snapshots", self.dir / "trash"
        for d in (self.jdir, self.sdir, self.tdir):
            d.mkdir(parents=True, exist_ok=True)
        self.limit = snapshot_limit
        self._lock = threading.RLock()

    # ------------------------------------------------ 快照
    def snapshot(self, path: Path) -> str | None:
        """复制文件内容作为“修改前”快照。目录/过大文件返回 None。"""
        try:
            if not path.is_file() or path.stat().st_size > self.limit:
                return None
            ref = uuid.uuid4().hex
            shutil.copy2(path, self.sdir / ref)
            return ref
        except OSError:
            return None

    # ------------------------------------------------ 回收站
    def trash(self, path: Path, session_id: str = "") -> str:
        tid = uuid.uuid4().hex[:16]
        d = self.tdir / tid
        d.mkdir(parents=True)
        is_dir = path.is_dir() and not path.is_symlink()
        try:
            size = path.stat().st_size if not is_dir else sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            size = 0
        shutil.move(str(path), str(d / "payload"))
        (d / "meta.json").write_text(json.dumps({"id": tid, "ts": time.time(), "path": str(path), "is_dir": is_dir,
                                                 "size": size, "session": session_id}, ensure_ascii=False), "utf-8")
        return tid

    def list_trash(self) -> list[dict]:
        out = []
        for d in self.tdir.iterdir():
            try:
                out.append(json.loads((d / "meta.json").read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda m: m["ts"], reverse=True)

    def restore(self, tid: str, force_rename: bool = True) -> str:
        if not re.fullmatch(r"[0-9a-f]{16}", tid):
            raise ValueError("非法回收站 ID")
        d = self.tdir / tid
        meta = json.loads((d / "meta.json").read_text("utf-8"))
        dest = Path(meta["path"])
        if os.path.lexists(dest):
            if not force_rename:
                raise FileExistsError(str(dest))
            n = 1
            while os.path.lexists(dest.with_name(f"{dest.stem}.restored-{n}{dest.suffix}")):
                n += 1
            dest = dest.with_name(f"{dest.stem}.restored-{n}{dest.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d / "payload"), str(dest))
        shutil.rmtree(d, ignore_errors=True)
        return str(dest)

    def empty_trash(self) -> int:
        n = 0
        for d in list(self.tdir.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        return n

    def purge(self, days: int) -> None:
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        for m in self.list_trash():
            if m["ts"] < cutoff:
                shutil.rmtree(self.tdir / m["id"], ignore_errors=True)
        live = set()
        for f in self.jdir.glob("*.jsonl"):
            for e in self._read(f):
                if e.get("snap"):
                    live.add(e["snap"])
        for s in self.sdir.iterdir():
            try:
                if s.name not in live or s.stat().st_mtime < cutoff:
                    s.unlink()
            except OSError:
                pass

    # ------------------------------------------------ 日志
    def _file(self, sid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            raise ValueError("非法会话 ID")
        return self.jdir / f"{sid}.jsonl"

    @staticmethod
    def _read(f: Path) -> list[dict]:
        out = []
        try:
            for ln in f.read_text("utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def _write_all(self, f: Path, entries: list[dict]) -> None:
        atomic_write(f, ("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n").encode("utf-8"))

    def record(self, sid: str, *, op: str, path: Path | str, summary: str, snap: str | None = None, existed: bool = True,
               after: str | None = None, extra: dict | None = None, no_undo: bool = False) -> str:
        with self._lock:
            f = self._file(sid)
            entries = self._read(f)
            n = (entries[-1]["n"] + 1) if entries else 1
            e = {"id": f"{sid}.{n}", "n": n, "ts": time.time(), "op": op, "path": str(path), "summary": summary,
                 "snap": snap, "existed": existed, "after": after, "extra": extra or {}, "undone": False, "no_undo": no_undo}
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            return e["id"]

    def entries(self, sid: str, ws: Path | None = None) -> list[dict]:
        out = []
        for e in reversed(self._read(self._file(sid))):
            p = e["path"]
            try:
                disp = Path(p).relative_to(ws).as_posix() if ws else p
            except ValueError:
                disp = p
            can = (not e["undone"]) and (not e.get("no_undo")) and (
                e["op"] in ("delete", "move", "copy", "mkdir") or bool(e.get("snap")) or not e["existed"])
            out.append({"id": e["id"], "ts": e["ts"], "op": e["op"], "path": disp, "summary": e["summary"],
                        "undone": e["undone"], "can_undo": can})
        return out

    def count(self, sid: str) -> int:
        return len(self._read(self._file(sid)))

    def undo(self, entry_id: str, force: bool = False) -> dict:
        m = _ID_RE.match(entry_id)
        if not m:
            raise ValueError("非法日志 ID")
        sid, n = m.group(1), int(m.group(2))
        with self._lock:
            f = self._file(sid)
            entries = self._read(f)
            e = next((x for x in entries if x["n"] == n), None)
            if not e:
                raise KeyError("日志不存在")
            if e["undone"]:
                raise ValueError("该操作已撤销")
            p = Path(e["path"])
            op, ex = e["op"], e.get("extra", {})
            cur = sha256_file(p) if p.is_file() else None
            if op in ("write", "edit", "office"):
                if not force and e.get("after") is not None and cur != e["after"]:
                    raise PermissionError("conflict: 文件在此之后又被修改过")
                if e["existed"]:
                    if not e.get("snap") or not (self.sdir / e["snap"]).exists():
                        raise ValueError("没有可用快照（文件过大或已清理），无法撤销")
                    atomic_write(p, (self.sdir / e["snap"]).read_bytes())
                elif p.exists():
                    self.trash(p, sid)
            elif op == "delete":
                tid = ex.get("trash_id")
                if not tid:
                    raise ValueError("缺少回收站记录")
                if os.path.lexists(p) and not force:
                    raise PermissionError("conflict: 原位置已有同名文件")
                self.restore(tid)
            elif op == "move":
                src, dst = Path(ex["src"]), Path(ex["dst"])
                if os.path.lexists(src) and not force:
                    raise PermissionError("conflict: 原位置已有文件")
                if not os.path.lexists(dst):
                    raise ValueError("移动后的文件已不存在")
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                if ex.get("trash_id"):
                    self.restore(ex["trash_id"])
            elif op == "copy":
                if p.exists():
                    self.trash(p, sid)
                if ex.get("trash_id"):
                    self.restore(ex["trash_id"])
            elif op == "mkdir":
                try:
                    p.rmdir()
                except OSError:
                    raise PermissionError("conflict: 目录非空，未撤销")
            else:
                raise ValueError(f"不支持撤销的操作: {op}")
            e["undone"] = True
            self._write_all(f, entries)
            return {"ok": True, "op": op, "path": str(p)}
