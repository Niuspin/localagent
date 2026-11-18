"""Git Worktree 隔离工作副本变更收集与安全写回主工作区逻辑。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .. import security as sec
from ..journal import Journal, atomic_write, sha256_bytes


def git_cmd(cwd: Path, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    kw: dict[str, Any] = {"creationflags": 0x08000000} if sec.IS_WIN else {}
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kw,
    )
    return r.returncode, r.stdout or "", r.stderr or ""


def apply_worktree_changes(
    wt_dir: Path,
    ws_dir: Path,
    sid: str,
    turn_id: str,
    *,
    journal: Journal | None,
    protected: list[Path] | None = None,
    hidden: list[Path] | None = None,
) -> tuple[list[str], str]:
    rc, status_out, _ = git_cmd(wt_dir, "status", "--porcelain", "-uall")
    if rc != 0 or not status_out.strip():
        return [], ""
    _, diff_out, _ = git_cmd(wt_dir, "diff", "HEAD")
    changed: list[str] = []
    guard_ws = sec.PathGuard(ws_dir, protected=protected or [], hidden=hidden or [])
    for ln in status_out.splitlines():
        if len(ln) < 4:
            continue
        st_code = ln[:2]
        rel_path = ln[3:].strip().strip('"').split(" -> ")[-1]
        src_p = wt_dir / rel_path
        try:
            dst_p = guard_ws.resolve(rel_path, write=True)
        except sec.SecurityError:
            continue
        if "D" in st_code and not src_p.exists():
            if dst_p.exists():
                if journal:
                    tid = journal.trash(dst_p, sid)
                    journal.record(
                        sid, op="delete", path=dst_p, summary=f"[worktree] 删除 {rel_path}",
                        extra={"trash_id": tid, "turn_id": turn_id},
                    )
                else:
                    dst_p.unlink()
                changed.append(f"D {rel_path}")
        elif src_p.is_file():
            data = src_p.read_bytes()
            existed = dst_p.is_file()
            snap = journal.snapshot(dst_p) if (existed and journal) else None
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(dst_p, data)
            if journal:
                journal.record(
                    sid, op="edit" if existed else "write", path=dst_p,
                    summary=f"[worktree] {'更新' if existed else '新增'} {rel_path}",
                    snap=snap, existed=existed, after=sha256_bytes(data), extra={"turn_id": turn_id},
                )
            changed.append(("M " if existed else "A ") + rel_path)
    return changed, diff_out
