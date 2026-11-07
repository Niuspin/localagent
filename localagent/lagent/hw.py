"""硬件探测：内存 / NVIDIA·AMD 显存 / Apple 统一内存。全部尽力而为，失败不抛异常。"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any

_cache: tuple[float, dict] | None = None


def _run(argv: list[str], timeout: float = 5.0) -> str:
    try:
        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _ram() -> tuple[float, float]:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return vm.total / 2**30, vm.available / 2**30
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform.startswith("linux"):
            d = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    d[k] = int(v.split()[0]) * 1024
            return d["MemTotal"] / 2**30, d.get("MemAvailable", d["MemFree"]) / 2**30
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong),
                            ("av", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                            ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return ms.tot / 2**30, ms.av / 2**30
    except Exception:  # noqa: BLE001
        pass
    return 0.0, 0.0


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 2),
                             "free_gb": round(float(parts[2]) / 1024, 2), "vendor": "nvidia"})
            except ValueError:
                pass
    return gpus


def _amd() -> list[dict]:
    exe = shutil.which("rocm-smi")
    if not exe:
        return []
    import json
    out = _run([exe, "--showmeminfo", "vram", "--showproductname", "--json"])
    gpus = []
    try:
        data = json.loads(out)
        for card, d in data.items():
            tot = float(d.get("VRAM Total Memory (B)", 0))
            used = float(d.get("VRAM Total Used Memory (B)", 0))
            if tot:
                gpus.append({"name": d.get("Card series") or d.get("Card Series") or card,
                             "vram_gb": round(tot / 2**30, 2), "free_gb": round((tot - used) / 2**30, 2), "vendor": "amd"})
    except Exception:  # noqa: BLE001
        pass
    return gpus


def detect(force: bool = False) -> dict:
    global _cache
    if _cache and not force and time.time() - _cache[0] < 5:
        return _cache[1]
    ram, free = _ram()
    gpus = _nvidia() or _amd()
    unified = False
    if not gpus and sys.platform == "darwin" and platform.machine() == "arm64":
        unified = True
        gpus = [{"name": "Apple Silicon (统一内存)", "vram_gb": round(ram * 0.70, 2),
                 "free_gb": round(max(0.0, free - ram * 0.05), 2), "vendor": "apple"}]
    info = {
        "os": {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform),
        "arch": platform.machine(), "cpu_threads": os.cpu_count() or 1,
        "ram_gb": round(ram, 1), "ram_free_gb": round(free, 1), "gpus": gpus, "unified": unified,
    }
    _cache = (time.time(), info)
    return info
