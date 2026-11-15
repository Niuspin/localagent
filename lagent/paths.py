"""运行位置：源码运行与 PyInstaller 打包运行共用一套规则，其余模块只从这里取路径。

- CODE_DIR：只读资源（web/、内置技能）所在目录。源码运行时是项目目录；打包后是 PyInstaller 的资源目录（_internal / _MEIPASS）。
- ROOT：可写的数据根（models/ bin/ skills/ data/ config.json）。优先级：环境变量 LAGENT_HOME > 打包后 exe 所在目录（便携模式）
  > exe 目录不可写时用户数据目录（%LOCALAPPDATA%\\LocalAgent 等）> 源码目录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
APP_NAME = "LocalAgent"


def code_dir() -> Path:
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent).resolve()
    return Path(__file__).resolve().parent.parent


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_root() -> Path:
    env = os.environ.get("LAGENT_HOME")
    if env:
        return Path(env).resolve()
    if not FROZEN:
        return code_dir()
    exe_dir = Path(sys.executable).resolve().parent
    if _writable(exe_dir):
        return exe_dir
    return user_data_dir()


CODE_DIR = code_dir()
ROOT = data_root()


def ensure_dirs(root: Path = ROOT) -> list[str]:
    """创建运行所需目录；返回无法创建的目录名（通常为空）。"""
    failed = []
    for name in ("models", "bin", "skills", "data"):
        try:
            (root / name).mkdir(parents=True, exist_ok=True)
        except OSError:
            failed.append(name)
    return failed
