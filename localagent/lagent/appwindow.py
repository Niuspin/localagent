"""桌面应用窗口：用系统里已有的 Edge / Chrome（“应用模式”）打开界面——独立窗口、无地址栏，不引入任何额外依赖。

找不到这类浏览器时返回 None，由启动器退回到系统默认浏览器。窗口使用独立的浏览器配置目录（data/browser/），
和用户日常浏览器的登录、扩展、缓存完全隔离；关闭这个窗口就等于退出应用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _candidates() -> list[str]:
    out: list[str] = []
    if os.name == "nt":
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                out += [str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                        str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    elif sys.platform == "darwin":
        out += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        for name in ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"):
            w = shutil.which(name)
            if w:
                out.append(w)
    return out


def find_browser() -> str | None:
    for c in _candidates():
        if Path(c).is_file():
            return c
    return None


def window_args(exe: str, url: str, profile_dir: Path) -> list[str]:
    return [exe, f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,860"]


def open_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    exe = find_browser()
    if not exe:
        return None
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        kw: dict = {"creationflags": 0x00000008} if os.name == "nt" else {"start_new_session": True}   # 脱离控制台，关闭它不牵连启动器
        return subprocess.Popen(window_args(exe, url, profile_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
    except OSError:
        return None
