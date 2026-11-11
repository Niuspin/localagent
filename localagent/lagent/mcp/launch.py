"""把服务器配置变成真正的启动命令。安全上的两条硬规矩：
 1. 默认放进 OS 沙箱：只有它自己的私有目录（data/mcp/<名称>/）可写，主目录/密钥不可见，默认断网；
 2. 沙箱不可用时**拒绝启动**（fail closed），而不是悄悄不带沙箱运行。信任某个服务器的用户可以单独把它的 sandbox 设为 off。
环境变量走 clean_env（不继承你的 API Key 等），再叠加该服务器配置里显式给出的变量。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import security as sec
from .store import ServerConfig

_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc")


class LaunchError(Exception):
    pass


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    kw: dict[str, Any]
    cwd: Path
    sandbox: str          # bwrap | sandbox-exec | none
    network: bool

    @property
    def sandboxed(self) -> bool:
        return self.sandbox != "none"


def resolve_exe(cmd0: str) -> str:
    if os.sep in cmd0 or (sec.IS_WIN and "/" in cmd0):
        p = Path(cmd0).expanduser()
        if p.is_file():
            return str(p)
        raise LaunchError(f"找不到可执行文件: {cmd0}")
    found = shutil.which(cmd0)
    if not found:
        raise LaunchError(f"找不到命令 {cmd0}（请确认已安装，并且在 PATH 里）")
    return found


def exe_prefixes(exe: str, data_dir: Path) -> list[Path]:
    """可执行文件所在的安装前缀（如 /opt/node22），沙箱里要只读挂进去才跑得起来。
    同时考虑符号链接前后两个位置（npx → .../lib/node_modules/npm/bin/npx-cli.js，而 node 在原路径的同级 bin 里）。"""
    home = Path.home().resolve()
    data = data_dir.resolve()
    out: list[Path] = []
    for raw in (Path(os.path.abspath(exe)), Path(exe).resolve()):
        if str(raw).startswith(_SYSTEM):
            continue
        par = raw.parent
        prefix = par.parent if par.name in ("bin", "sbin", "Scripts") else par
        if prefix == Path(prefix.anchor) or prefix == home or prefix in home.parents:
            prefix = par                        # 前缀太大（整个主目录）就只开放可执行文件所在目录
        if prefix == data or data in prefix.parents or prefix in data.parents:
            continue
        if prefix not in out:
            out.append(prefix)
    return out


def build_launch(cfg: ServerConfig, *, data_dir: Path, workspace: str | None) -> Launch:
    private = Path(data_dir) / "mcp" / cfg.name
    home = private / "home"
    home.mkdir(parents=True, exist_ok=True)
    exe = resolve_exe(cfg.command[0])
    argv = [exe, *cfg.command[1:]]
    env = sec.clean_env(str(home))
    env.update(cfg.env)
    kw: dict[str, Any] = {}
    if sec.IS_WIN:
        kw["creationflags"] = 0x08000000 | 0x00000200      # NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    if cfg.sandbox == "off":
        return Launch(argv, env, kw, private, "none", True)
    info = sec.sandbox_info()
    if not info["available"]:
        raise LaunchError("这台设备没有可用的沙箱（" + info["note"].split("：")[0] + "），出于安全考虑已拒绝启动。"
                          "如果你信任这个服务器，可以在它的设置里关闭“沙箱”后再启动。")
    extra_rw = [Path(workspace)] if cfg.workspace and workspace else []
    extra_ro = [*exe_prefixes(exe, Path(data_dir)), *[Path(p) for p in cfg.readonly_paths]]
    wrapped, kind = sec.wrap_in_sandbox(argv, private, cfg.network, home, extra_rw=extra_rw, extra_ro=extra_ro, sandbox_home=str(home))
    if kind == "none":
        raise LaunchError("沙箱包装失败，已拒绝启动。")
    return Launch(wrapped, env, kw, private, kind, cfg.network)
