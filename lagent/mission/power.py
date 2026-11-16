"""Mission 电源防休眠守卫、安静时间段与系统通知。"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class SleepInhibitor:
    """跨平台进程运行期防系统休眠（Windows/macOS/Linux），非运行期自动释放。"""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._refs = 0
        self._proc: subprocess.Popen[Any] | None = None
        self.active = False

    def acquire(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            self._refs += 1
            if self._refs == 1:
                self.active = self._start_os_inhibit()
            return self.active

    def release(self, *, force: bool = False) -> None:
        with self._lock:
            if force:
                self._refs = 0
            else:
                self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self.active:
                self._stop_os_inhibit()
                self.active = False

    def _start_os_inhibit(self) -> bool:
        try:
            if os.name == "nt":
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                return True
            if sys.platform == "darwin" and shutil.which("caffeinate"):
                self._proc = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
                self._proc = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=sleep",
                        "--who=LocalAgent",
                        "--why=Mission running",
                        "--mode=block",
                        "cat",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
        except Exception:
            return False
        return False

    def _stop_os_inhibit(self) -> None:
        try:
            if os.name == "nt":
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
        except Exception:
            self._proc = None


def in_quiet_hours(spec: str | dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """判断当前时间是否处于安静时段（如 '23:00-07:00'）。"""
    if not spec:
        return False
    if isinstance(spec, dict):
        start_s = str(spec.get("start") or "").strip()
        end_s = str(spec.get("end") or "").strip()
    else:
        raw = str(spec).strip()
        if "-" not in raw:
            return False
        start_s, end_s = [x.strip() for x in raw.split("-", 1)]
    try:
        sh, sm = [int(x) for x in start_s.split(":", 1)]
        eh, em = [int(x) for x in end_s.split(":", 1)]
    except Exception:
        return False
    cur = now or datetime.now()
    cur_m = cur.hour * 60 + cur.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def notify_mission(title: str, message: str, *, quiet_hours: Any = None, now: datetime | None = None) -> bool:
    if in_quiet_hours(quiet_hours, now=now):
        return False
    try:
        from ..launcher import notify as os_notify
        os_notify(title, message)
        return True
    except Exception:
        return False
