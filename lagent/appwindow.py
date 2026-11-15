"""桌面应用窗口：支持 pywebview 原生独立窗口（Edge WebView2 / WebKit / GTK），
未安装或环境不支持时自动降级到系统 Edge / Chrome 应用模式（--app=），
再找不到时退回到系统默认浏览器。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_active_window: Any = None


def has_native_window() -> bool:
    """检查是否安装并支持 pywebview 原生独立窗口。"""
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def close_native_window() -> None:
    """如果当前存在打开的原生窗口，将其关闭。"""
    global _active_window
    if _active_window is not None:
        try:
            _active_window.destroy()
        except Exception:
            pass
        _active_window = None


def run_native_window(
    url: str,
    profile_dir: Path,
    title: str = "LocalAgent",
    width: int = 1320,
    height: int = 880,
    min_size: tuple[int, int] = (920, 640),
    on_close: Callable[[], None] | None = None,
) -> bool:
    """在主线程中启动 pywebview 原生窗口（阻塞直至窗口关闭）。
    返回 True 表示原生窗口正常运行并关闭；返回 False 表示初始化失败需退回浏览器/应用模式。
    """
    global _active_window
    try:
        import webview
        profile_dir.mkdir(parents=True, exist_ok=True)
        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(
            title=title,
            url=url,
            width=width,
            height=height,
            min_size=min_size,
            confirm_close=False,
            text_select=True,
        )
        _active_window = window
        if on_close:
            window.events.closed += on_close
        webview.start(private_mode=False, storage_path=str(profile_dir))
        return True
    except Exception:
        return False
    finally:
        _active_window = None


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


def pick_folder(initial_dir: str = "") -> str | None:
    """调用系统原生文件夹选择对话框；无桌面或用户取消时返回 None。"""
    init = str(Path(initial_dir).expanduser()) if initial_dir and Path(initial_dir).is_dir() else str(Path.home())
    if _active_window is not None:
        try:
            import webview
            res = _active_window.create_file_dialog(webview.FOLDER_DIALOG, directory=init)
            if res and len(res) > 0:
                return str(res[0])
            return None
        except Exception:  # noqa: BLE001
            pass
    try:
        if os.name == "nt":
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if ps:
                esc = init.replace("'", "''")
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "$d.Description = '选择 LocalAgent 工作区目录';"
                    f"$d.SelectedPath = '{esc}';"
                    "$d.ShowNewFolderButton = $true;"
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
                )
                r = subprocess.run(
                    [ps, "-NoProfile", "-STA", "-Command", f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; {script}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                    creationflags=0x08000000,
                )
                out = (r.stdout or "").strip()
                return out or None
        elif sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择 LocalAgent 工作区目录")'],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            return (r.stdout or "").strip().rstrip("/") or None if r.returncode == 0 else None
        else:
            for cmd in (
                ["zenity", "--file-selection", "--directory", f"--filename={init}/"],
                ["kdialog", "--getexistingdirectory", init],
            ):
                if shutil.which(cmd[0]):
                    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                    return (r.stdout or "").strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None
