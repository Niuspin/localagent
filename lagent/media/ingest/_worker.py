"""受限媒体摄取子进程工作入口：设置内存/CPU 硬限后执行单文件解析。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _apply_os_limits(max_mem_mb: int, timeout_s: float) -> None:
    if os.name == "nt":
        return
    try:
        import resource  # type: ignore
        mem_bytes = max(128, int(max_mem_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        cpu_s = max(5, int(timeout_s) + 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        req: dict[str, Any] = json.loads(raw.decode("utf-8"))
        _apply_os_limits(int(req.get("max_mem_mb") or 768), float(req.get("timeout_s") or 30.0))
        from .handlers import ingest_path_direct
        res = ingest_path_direct(
            Path(req["path"]),
            cfg=req.get("cfg") or {},
            caps=req.get("caps") or {},
            options=req.get("options") or {},
        )
        out = res.to_dict()
    except Exception as e:  # noqa: BLE001
        out = {"kind": "unknown", "mime": "application/octet-stream", "ok": False, "tainted": True, "error": f"worker_error: {e}"}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
