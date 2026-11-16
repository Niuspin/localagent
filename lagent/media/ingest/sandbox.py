"""受限子进程隔离调度：超时强杀、Windows Job Object / POSIX rlimit 与崩溃隔离。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ... import security as sec
from .models import IngestResult

_PKG_ROOT = str(Path(__file__).resolve().parents[3])


def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            sec.release_windows_job(proc.pid)
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def run_in_restricted_subprocess(
    path: Path,
    *,
    cfg: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
    max_mem_mb: int = 768,
    worker_module: str = "lagent.media.ingest._worker",
    extra_pythonpath: str | None = None,
) -> IngestResult:
    """在独立受限子进程中解析文件，崩溃或超时绝不波及主服务。"""
    p = Path(path)
    req = {
        "path": str(p),
        "cfg": dict(cfg or {}),
        "caps": dict(caps or {}),
        "options": dict(options or {}),
        "timeout_s": float(timeout_s),
        "max_mem_mb": int(max_mem_mb),
    }
    work_dir = p.parent if p.parent.is_dir() else Path(_PKG_ROOT)
    env = sec.clean_env(str(work_dir))
    env["PYTHONPATH"] = f"{_PKG_ROOT}{os.pathsep}{extra_pythonpath}" if extra_pythonpath else _PKG_ROOT

    kw: dict[str, Any] = {}
    if sec.IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True

    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", worker_module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(p.parent if p.parent.is_dir() else Path(_PKG_ROOT)),
            env=env,
            **kw,
        )
        if sec.IS_WIN:
            sec.harden_windows_process(proc.pid)
        stdout_b, stderr_b = proc.communicate(
            input=json.dumps(req, ensure_ascii=False).encode("utf-8"),
            timeout=max(0.2, float(timeout_s)),
        )
        if proc.returncode != 0:
            err_tail = (stderr_b or b"").decode("utf-8", "replace").strip()[:300]
            return IngestResult("unknown", "application/octet-stream", str(p), p.name, ok=False, tainted=True, error=f"解析子进程异常退出 (code={proc.returncode}): {err_tail}")
        data = json.loads((stdout_b or b"{}").decode("utf-8", "replace"))
        if not isinstance(data, dict):
            return IngestResult("unknown", "application/octet-stream", str(p), p.name, ok=False, tainted=True, error="子进程返回格式无效")
        res = IngestResult.from_dict(data)
        res.tainted = True
        return res
    except subprocess.TimeoutExpired:
        if proc is not None:
            _kill_proc(proc)
        return IngestResult("unknown", "application/octet-stream", str(p), p.name, ok=False, tainted=True, error=f"媒体解析超时（> {timeout_s}s），子进程已回收")
    except Exception as e:  # noqa: BLE001
        if proc is not None:
            _kill_proc(proc)
        return IngestResult("unknown", "application/octet-stream", str(p), p.name, ok=False, tainted=True, error=f"受限解析调度失败: {e}")
    finally:
        if proc is not None and sec.IS_WIN:
            sec.release_windows_job(proc.pid)


async def run_in_restricted_subprocess_async(
    path: Path,
    *,
    cfg: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
    max_mem_mb: int = 768,
) -> IngestResult:
    return await asyncio.to_thread(
        run_in_restricted_subprocess,
        path,
        cfg=cfg,
        caps=caps,
        options=options,
        timeout_s=timeout_s,
        max_mem_mb=max_mem_mb,
    )
