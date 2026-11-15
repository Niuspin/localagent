"""MCP 服务器配置：校验 + 持久化（data/mcp.json，权限 0600；data/ 对 Agent 完全不可见）。

“放宽”都是按服务器单独设置、显式写在配置里的：
  sandbox          auto（默认，沙箱不可用就拒绝启动）| off（用户明确信任该服务器）
  network          沙箱内是否允许联网（默认否）
  workspace        是否把当前工作区挂进沙箱（可写；默认否）
  readonly_paths   额外的只读目录（装在 ~/.local、/opt 下的运行时需要）
  trust_results    不把该服务器的返回内容当作不可信（默认否；开启后“本会话允许”才真正省事）
环境变量里的值（可能含密钥）只存在这个文件里，接口只回显变量名。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import security as sec
from ..journal import atomic_write

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENV_BLOCKED = re.compile(r"^(LD_|DYLD_)|^BASH_ENV$", re.I)      # 动态库注入、shell 启动脚本注入
MASK = "••••••"


@dataclass
class ServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    sandbox: str = "auto"
    network: bool = False
    workspace: bool = False
    readonly_paths: list[str] = field(default_factory=list)
    trust_results: bool = False
    timeout_s: int = 120
    idle_s: int = 600

    def public(self) -> dict:
        d = asdict(self)
        d["env_keys"] = sorted(self.env)
        d.pop("env")
        d["command_line"] = shlex.join(self.command) if not sec.IS_WIN else " ".join(self.command)
        return d


def parse_command(v: Any) -> list[str]:
    if isinstance(v, str):
        try:
            parts = shlex.split(v, posix=not sec.IS_WIN)
        except ValueError as e:
            raise ValueError(f"命令行解析失败: {e}")
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    elif isinstance(v, list) and all(isinstance(x, str) for x in v):
        parts = list(v)
    else:
        raise ValueError("command 需要是字符串或字符串数组")
    parts = [p for p in parts if p != ""]
    if not parts:
        raise ValueError("命令不能为空")
    if len(parts) > 32 or any(len(p) > 2000 or "\x00" in p for p in parts):
        raise ValueError("命令参数过多或包含非法字符")
    return parts


def _bool(name: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{name} 需要布尔值")
    return v


def _int(name: str, v: Any, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 需要整数")
    if not lo <= n <= hi:
        raise ValueError(f"{name} 必须在 {lo}~{hi} 之间")
    return n


class McpStore:
    def __init__(self, path: Path, data_dir: Path):
        self.path, self.data_dir = Path(path), Path(data_dir)
        self._lock = threading.RLock()
        self.servers: dict[str, ServerConfig] = {}
        self._load()

    # ------------------------------------------------------------ 读写
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for name, d in (raw.get("servers") or {}).items() if isinstance(raw, dict) else []:
            try:
                self.servers[name] = self.build(name, d, None)
            except (ValueError, TypeError):
                continue            # 手工改坏的条目跳过，不影响其他服务器

    def _save(self) -> None:
        body = json.dumps({"version": 1, "servers": {n: asdict(c) for n, c in self.servers.items()}}, ensure_ascii=False, indent=1).encode("utf-8")
        atomic_write(self.path, body)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> ServerConfig | None:
        return self.servers.get(name)

    # ------------------------------------------------------------ 校验
    def build(self, name: str, patch: dict, base: ServerConfig | None) -> ServerConfig:
        if not NAME_RE.match(name or ""):
            raise ValueError("服务器名称只能用小写字母、数字、连字符和下划线，最长 32 个字符，且以字母或数字开头")
        c = ServerConfig(name=name, command=list(base.command) if base else [], env=dict(base.env) if base else {},
                         enabled=base.enabled if base else True, sandbox=base.sandbox if base else "auto",
                         network=base.network if base else False, workspace=base.workspace if base else False,
                         readonly_paths=list(base.readonly_paths) if base else [], trust_results=base.trust_results if base else False,
                         timeout_s=base.timeout_s if base else 120, idle_s=base.idle_s if base else 600)
        if "command" in patch:
            c.command = parse_command(patch["command"])
        if not c.command:
            raise ValueError("需要提供启动命令 command")
        if "enabled" in patch:
            c.enabled = _bool("enabled", patch["enabled"])
        if "sandbox" in patch:
            if patch["sandbox"] not in ("auto", "off"):
                raise ValueError("sandbox 只能是 auto 或 off")
            c.sandbox = patch["sandbox"]
        for k in ("network", "workspace", "trust_results"):
            if k in patch:
                setattr(c, k, _bool(k, patch[k]))
        if "timeout_s" in patch:
            c.timeout_s = _int("timeout_s", patch["timeout_s"], 5, 3600)
        if "idle_s" in patch:
            c.idle_s = _int("idle_s", patch["idle_s"], 0, 86400)
        if "env" in patch:
            c.env = self._env(patch["env"], c.env)
        if "readonly_paths" in patch:
            c.readonly_paths = self._paths(patch["readonly_paths"])
        return c

    @staticmethod
    def _env(v: Any, old: dict[str, str]) -> dict[str, str]:
        """env 是完整替换：值为 null 表示“沿用原来的值”（界面不回显密钥），缺失的键视为删除。"""
        if not isinstance(v, dict):
            raise ValueError("env 需要是对象")
        out: dict[str, str] = {}
        for k, val in v.items():
            if not ENV_RE.match(str(k)):
                raise ValueError(f"环境变量名不合法: {k!r}")
            if ENV_BLOCKED.search(str(k)):
                raise ValueError(f"不允许设置环境变量 {k}")
            if val is None or val == MASK:
                if k in old:
                    out[k] = old[k]
                continue
            if not isinstance(val, str) or len(val) > 4000 or "\x00" in val:
                raise ValueError(f"环境变量 {k} 的值不合法")
            out[k] = val
        if len(out) > 50:
            raise ValueError("环境变量过多")
        return out

    def _paths(self, v: Any) -> list[str]:
        if not isinstance(v, list) or len(v) > 20:
            raise ValueError("readonly_paths 需要是不超过 20 项的数组")
        out: list[str] = []
        data = self.data_dir.resolve()
        for x in v:
            if not isinstance(x, str) or not x.strip():
                continue
            p = Path(x.strip()).expanduser()
            if not p.is_absolute():
                raise ValueError(f"只读目录必须是绝对路径: {x}")
            try:
                rp = p.resolve(strict=True)
            except OSError:
                raise ValueError(f"目录不存在: {x}")
            if rp == Path(rp.anchor) or rp == Path.home().resolve() or rp in Path.home().resolve().parents:
                raise ValueError(f"范围太大，不能整体开放: {x}（请指定具体的子目录）")
            if rp == data or data in rp.parents or rp in data.parents:
                raise ValueError(f"不能开放包含 LocalAgent 数据目录（会话、记忆、密钥）的路径: {x}")
            if str(rp) not in out:
                out.append(str(rp))
        return out

    # ------------------------------------------------------------ 增删改
    def upsert(self, name: str, patch: dict) -> tuple[ServerConfig, bool]:
        with self._lock:
            base = self.servers.get(name)
            c = self.build(name, patch, base)
            if len(self.servers) >= 20 and base is None:
                raise ValueError("最多配置 20 个服务器")
            self.servers[name] = c
            self._save()
            return c, base is None

    def delete(self, name: str) -> bool:
        with self._lock:
            if name not in self.servers:
                return False
            del self.servers[name]
            self._save()
            return True
