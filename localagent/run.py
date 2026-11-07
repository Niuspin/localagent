#!/usr/bin/env python3
"""LocalAgent 启动入口：python run.py [--port N] [--workspace DIR] [--no-browser] [--load]"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10 或更高版本")


def _free(port: int) -> int:
    for p in ([port] if port else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def main() -> None:
    ap = argparse.ArgumentParser(description="LocalAgent — 本地 gguf + llama.cpp 的安全 Agent")
    ap.add_argument("--port", type=int, default=8765, help="界面端口（默认 8765，被占用则自动换）")
    ap.add_argument("--workspace", help="启动时设置工作区目录")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--load", action="store_true", help="启动后立即加载 config 里上次的模型")
    a = ap.parse_args()
    try:
        import uvicorn
        from lagent import security as sec
        from lagent.server import State, make_app
    except ImportError as e:
        sys.exit(f"缺少依赖: {e.name}\n请先运行: pip install -r requirements.txt")
    port = _free(a.port)
    st = State(sec.new_token(), port)
    if a.workspace:
        st.cfg.set_workspace(str(sec.validate_workspace(a.workspace)))
    url = f"http://127.0.0.1:{port}/?t={st.token}"
    print("=" * 62)
    print(" LocalAgent 已启动（仅本机可访问）")
    print(f" 打开: {url}")
    print(" 提示: 链接里的令牌每次启动都会变化；请勿分享该链接。")
    print("=" * 62, flush=True)
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    st.autostart = a.load
    app = make_app(st)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
