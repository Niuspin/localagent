#!/usr/bin/env python3
"""一键构建 LocalAgent.exe（需在目标系统上运行：Windows 出 exe，Linux/macOS 出对应可执行文件）。

用法：python packaging/build.py          → dist/LocalAgent/（含 LocalAgent.exe 与 models/ bin/ skills/ 空目录、使用说明）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "LocalAgent"

README = """LocalAgent 使用说明
====================
1. 把 llama.cpp 发行包（含 llama-server）解压到 bin\\ 目录；
2. 把 .gguf 模型放进 models\\（多模态模型请把 mmproj-*.gguf 放在同一目录）；
3. 双击 LocalAgent.exe。会打开独立应用窗口；关闭窗口即退出并停止模型。

数据（会话、记忆、审计日志）都在本目录的 data\\ 下，整个文件夹可直接拷走（便携）。
目录不可写时（如装在 Program Files）会改用 %LOCALAPPDATA%\\LocalAgent。
日志：data\\logs\\app.log。已有实例在运行时再次双击会提示，不会重复启动。
"""


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 1
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "packaging" / "localagent.spec")],
                          cwd=ROOT)
    for d in ("models", "bin", "skills"):
        (DIST / d).mkdir(parents=True, exist_ok=True)
        (DIST / d / ".gitkeep").write_text("")
    (DIST / "使用说明.txt").write_text(README, encoding="utf-8")
    print(f"\n构建完成：{DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
