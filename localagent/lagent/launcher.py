"""应用启动器：run.py 与打包后的 LocalAgent.exe 共用的唯一入口。

做的事：创建目录 → 单实例锁 → 起本机服务 → 打开应用窗口（Edge/Chrome 应用模式，缺失则系统默认浏览器）→
关闭窗口即退出（并停止模型进程）。无控制台的打包版把日志写进 data/logs/app.log。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__
from .paths import FROZEN, ROOT, ensure_dirs


# ------------------------------------------------------------------ 小工具
def _free(port: int) -> int:
    for p in ([port] if port else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def notify(title: str, msg: str) -> None:
    """终端里打印；无控制台的 Windows 打包版另弹一个消息框（否则用户什么也看不到）。"""
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass
    if os.name == "nt" and (FROZEN or sys.stderr is None):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _setup_stdio(root: Path) -> None:
    """windowed 打包版 sys.stdout/stderr 为 None：uvicorn/print 会崩。统一改写到日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        d = root / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / "app.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f


class SingleInstance:
    """同一数据目录只允许一个实例（两个实例会抢 models/ 与会话文件）。锁随进程退出自动释放。"""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True


def _setup_hint(root: Path) -> list[str]:
    """首次运行缺什么，写进日志/终端；界面里也会在加载模型时给出同样的提示。"""
    from . import gguf
    hints = []
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    bin_dir = root / "bin"
    has_bin = (bin_dir / exe).is_file() or any(bin_dir.glob(f"*/{exe}")) or any(bin_dir.glob(f"*/*/{exe}"))
    if not has_bin:
        hints.append(f"未找到 llama.cpp：请把发行包（含 {exe}）解压到 {bin_dir}，或确保它在 PATH 中。")
    try:
        if not gguf.scan(root / "models")[0]:
            hints.append(f"未找到模型：请把 .gguf 文件放进 {root / 'models'}。")
    except OSError:
        pass
    return hints


# ------------------------------------------------------------------ 主流程
def main(argv: list[str] | None = None) -> int:
    _setup_stdio(ROOT)          # 必须在解析参数之前：窗口版 exe 的 stdout/stderr 为 None，--help/--version 也会写它们
    ap = argparse.ArgumentParser(description="LocalAgent — 本地 gguf + llama.cpp 的安全 Agent")
    ap.add_argument("--port", type=int, default=8765, help="界面端口（默认 8765，被占用则自动换）")
    ap.add_argument("--workspace", help="启动时设置工作区目录")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开窗口/浏览器（纯服务模式）")
    ap.add_argument("--browser", action="store_true", help="用系统默认浏览器，而不是独立应用窗口")
    ap.add_argument("--load", action="store_true", help="启动后立即加载 config 里上次的模型")
    ap.add_argument("--version", action="version", version=f"LocalAgent {__version__}")
    a = ap.parse_args(argv)

    if sys.version_info < (3, 10):
        notify("LocalAgent", "需要 Python 3.10 或更高版本")
        return 1
    failed = ensure_dirs(ROOT)
    if failed:
        notify("LocalAgent", f"无法在 {ROOT} 下创建目录：{', '.join(failed)}。请检查写入权限，或用环境变量 LAGENT_HOME 指定数据目录。")
        return 1
    lock = SingleInstance(ROOT / "data" / ".lock")
    if not lock.acquire():
        notify("LocalAgent", "LocalAgent 已经在运行（同一数据目录只允许一个实例）。请切换到已打开的窗口；若找不到，请先结束旧进程再启动。")
        return 1
    try:
        import uvicorn
        from . import security as sec
        from .server import State, make_app
    except ImportError as e:
        notify("LocalAgent", f"缺少依赖: {e.name}\n请先运行: pip install -r requirements.txt")
        return 1

    port = _free(a.port)
    st = State(sec.new_token(), port)
    if a.workspace:
        st.cfg.set_workspace(str(sec.validate_workspace(a.workspace)))
    url = f"http://127.0.0.1:{port}/?t={st.token}"
    print("=" * 62)
    print(f" LocalAgent {__version__} 已启动（仅本机可访问）")
    print(f" 数据目录: {ROOT}")
    print(f" 打开: {url}")
    print(" 提示: 链接里的令牌每次启动都会变化；请勿分享该链接。")
    for h in _setup_hint(ROOT):
        print(f" ! {h}")
    print("=" * 62, flush=True)

    st.autostart = a.load
    server = uvicorn.Server(uvicorn.Config(make_app(st), host="127.0.0.1", port=port, log_level="warning", access_log=False,
                                           timeout_graceful_shutdown=3))   # SSE 长连接不能拖住退出
    st.exit_cb = lambda: setattr(server, "should_exit", True)

    if not a.no_browser:
        threading.Thread(target=_open_ui, args=(server, url, a.browser), daemon=True).start()
    server.run()
    return 0


def _open_ui(server: object, url: str, use_browser: bool) -> None:
    for _ in range(150):                       # 等服务就绪（最多 15 秒）
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    else:
        return
    if not use_browser:
        from . import appwindow
        proc = appwindow.open_window(url, ROOT / "data" / "browser")
        if proc is not None:
            t0 = time.time()
            proc.wait()
            if time.time() - t0 > 5:           # 窗口被用户关闭 → 退出应用；秒退多半是交给了已有实例，不能据此退出
                setattr(server, "should_exit", True)
            return
    webbrowser.open(url)
