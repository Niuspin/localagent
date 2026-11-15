"""所有 MCP 服务器的统一入口。

职责：
 - 懒启动（第一次用到才拉起进程）、空闲回收、每服务器一把锁（避免并发重复启动）；
 - 工具清单缓存（data/mcp/<名>/tools.json）：不启动进程也能把工具名挂进“按需加载”的工具组；
 - 把 MCP 工具桥接进现有的全局工具注册表（kind="mcp"，走同一套审批/污染/审计），并同步到 toolset 的动态组；
 - 对外给 server.py 提供 view/update/remove/start/stop，对 Agent 提供 contained()/call_tool()。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .. import security as sec
from .. import toolset
from ..journal import atomic_write
from ..tools.base import TOOLS, P, Tool, ToolResult, obj
from . import convert
from .launch import LaunchError, build_launch
from .session import McpError, McpSession
from .store import MASK, McpStore, ServerConfig


class McpManager:
    def __init__(self, data_dir: Path, audit: sec.AuditLog | None = None):
        self.data_dir = Path(data_dir)
        self.audit = audit
        self.store = McpStore(self.data_dir / "mcp.json", self.data_dir)
        self.sessions: dict[str, McpSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tools: dict[str, list[dict]] = {}          # 服务器 → 已转换的工具 [{name, tool, description, parameters}]
        self._warns: dict[str, list[str]] = {}
        self._errors: dict[str, str] = {}
        self._registered: set[str] = set()
        self._reaper: asyncio.Task | None = None
        for name, cfg in self.store.servers.items():
            self._load_cache(cfg)
        self.sync_registry()

    # ------------------------------------------------------------ 工具缓存
    def _sig(self, cfg: ServerConfig) -> str:
        return hashlib.sha256(json.dumps([cfg.command, sorted(cfg.env)]).encode()).hexdigest()[:16]

    def _cache_path(self, name: str) -> Path:
        return self.data_dir / "mcp" / name / "tools.json"

    def _load_cache(self, cfg: ServerConfig) -> None:
        try:
            d = json.loads(self._cache_path(cfg.name).read_text("utf-8"))
            if d.get("sig") == self._sig(cfg) and isinstance(d.get("tools"), list):
                self._tools[cfg.name] = d["tools"]
                self._warns[cfg.name] = list(d.get("warnings") or [])
        except (OSError, ValueError):
            pass

    def _save_cache(self, cfg: ServerConfig) -> None:
        p = self._cache_path(cfg.name)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(p, json.dumps({"sig": self._sig(cfg), "tools": self._tools.get(cfg.name, []),
                                        "warnings": self._warns.get(cfg.name, [])}, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass

    # ------------------------------------------------------------ 注册表桥接
    def sync_registry(self) -> None:
        for n in self._registered:
            TOOLS.pop(n, None)
        self._registered = set()
        groups: dict[str, tuple[tuple[str, ...], str]] = {}
        for name, cfg in self.store.servers.items():
            if not cfg.enabled:
                continue
            names: list[str] = []
            for t in self._tools.get(name, []):
                tool = Tool(t["name"], f"[外部工具·{name}] {t['description']}", t["parameters"], self._make_fn(name, t["tool"]), "mcp",
                            group="mcp", title=self._title(name, t["tool"]), extra={"server": name, "tool": t["tool"]})
                TOOLS[tool.name] = tool
                self._registered.add(tool.name)
                names.append(tool.name)
            if names:
                groups[f"mcp:{name}"] = (tuple(names), f"{name} 服务器的 {len(names)} 个工具")
        toolset.set_dynamic(groups)

    @staticmethod
    def _title(server: str, tool: str):
        def title(a: dict) -> str:
            brief = ", ".join(f"{k}={str(v)[:24]}" for k, v in list((a or {}).items())[:2])
            return f"{server}/{tool}" + (f"（{brief}）" if brief else "")
        return title

    def _make_fn(self, server: str, tool: str):
        async def fn(ctx: Any, a: dict) -> ToolResult:
            cfg = self.store.get(server)
            if cfg is None or not cfg.enabled:
                return ToolResult("该服务器已被停用或删除。", error=True)
            ws = str(ctx.guard.ws) if getattr(ctx, "guard", None) is not None else None
            if ws and ws.endswith(".la-no-workspace-sentinel"):
                ws = None
            try:
                try:
                    sess = await self.ensure(server, ws)
                    res = await sess.call(tool, a, float(cfg.timeout_s))
                except McpError as e:
                    if not e.undelivered:               # 只在“请求根本没发出去”时重试一次，避免重复执行有副作用的调用
                        raise
                    sess = await self.ensure(server, ws)
                    res = await sess.call(tool, a, float(cfg.timeout_s))
            except (McpError, LaunchError) as e:
                return ToolResult(f"调用失败: {e}", error=True)
            if sess.tools_dirty:
                asyncio.ensure_future(self._refresh_quiet(server))
            out = convert.result_to_toolresult(res, server=server, tool=tool, trusted=cfg.trust_results, vision=bool(getattr(ctx, "vision", False)))
            return out
        return fn

    # ------------------------------------------------------------ 生命周期
    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def effective_sandbox(self, name: str) -> str:
        cfg = self.store.get(name)
        if cfg is None or cfg.sandbox == "off":
            return "none"
        info = sec.sandbox_info()
        return info.get("kind") or "none" if info["available"] else "unavailable"

    def contained(self, server: str) -> bool:
        """沙箱生效且不能联网：此时自动模式下可以免审批。"""
        cfg = self.store.get(server)
        if cfg is None or cfg.sandbox == "off" or cfg.network:
            return False
        s = self.sessions.get(server)
        if s is not None and s.alive:
            return s.launch.sandboxed and not s.launch.network
        return bool(sec.sandbox_info()["available"])

    async def ensure(self, name: str, workspace: str | None = None) -> McpSession:
        cfg = self.store.get(name)
        if cfg is None:
            raise McpError("未知的服务器")
        async with self._lock(name):
            s = self.sessions.get(name)
            if s is not None and s.alive and not (cfg.workspace and workspace and s.started_ws != workspace):
                return s
            if s is not None:
                await s.stop()
                self.sessions.pop(name, None)
            try:
                launch = build_launch(cfg, data_dir=self.data_dir, workspace=workspace if cfg.workspace else None)
                s = McpSession(name, launch, workspace=workspace if cfg.workspace else None,
                               on_tools_changed=None)
                await s.start()
            except (LaunchError, McpError) as e:
                self._errors[name] = str(e)
                self._audit("mcp_start_failed", name, error=str(e)[:200])
                raise
            self._errors.pop(name, None)
            self.sessions[name] = s
            self._audit("mcp_start", name, sandbox=launch.sandbox, network=launch.network, command=cfg.command[0])
            return s

    async def refresh_tools(self, name: str, workspace: str | None = None) -> list[dict]:
        cfg = self.store.get(name)
        if cfg is None:
            raise McpError("未知的服务器")
        s = await self.ensure(name, workspace)
        raw = await s.list_tools()
        ok, warns = convert.vet_tools(raw)
        self._tools[name] = [{"name": convert.tool_name(name, t["name"]), "tool": t["name"],
                              "description": t["description"], "parameters": t["inputSchema"]} for t in ok]
        self._warns[name] = warns
        self._save_cache(cfg)
        self.sync_registry()
        return self._tools[name]

    async def _refresh_quiet(self, name: str) -> None:
        try:
            s = self.sessions.get(name)
            await self.refresh_tools(name, s.started_ws if s else None)
        except Exception:  # noqa: BLE001
            pass

    async def start_user(self, name: str, workspace: str | None = None) -> dict:
        """用户在界面上点“启动/测试”：拉起进程、读取并缓存工具。"""
        try:
            await self.refresh_tools(name, workspace)
        except (McpError, LaunchError) as e:
            self._errors[name] = str(e)
        return self.view(name)

    async def stop(self, name: str) -> None:
        async with self._lock(name):
            s = self.sessions.pop(name, None)
            if s:
                await s.stop()
                self._audit("mcp_stop", name)

    async def stop_all(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        for n in list(self.sessions):
            await self.stop(n)

    def shutdown_sync(self) -> None:
        """进程退出时的兜底：杀掉所有仍在运行的服务器进程树。"""
        from ..tools.files import kill_tree
        for s in list(self.sessions.values()):
            if s.proc is not None and s.proc.returncode is None:
                kill_tree(s.proc)

    def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for n, s in list(self.sessions.items()):
                cfg = self.store.get(n)
                if not s.alive:
                    self.sessions.pop(n, None)
                elif cfg and cfg.idle_s and now - s.last_used > cfg.idle_s:
                    await self.stop(n)

    # ------------------------------------------------------------ 配置变更
    async def update(self, name: str, patch: dict) -> dict:
        old = self.store.get(name)
        cfg, created = self.store.upsert(name, patch)
        restart = created or old is None or any(getattr(old, k) != getattr(cfg, k) for k in
                                                ("command", "env", "sandbox", "network", "workspace", "readonly_paths"))
        if restart:
            await self.stop(name)
        if old is not None and old.command != cfg.command or old is not None and sorted(old.env) != sorted(cfg.env):
            self._tools.pop(name, None)
            self._warns.pop(name, None)
        self._audit("mcp_config", name, created=created, sandbox=cfg.sandbox, network=cfg.network, workspace=cfg.workspace,
                    trust_results=cfg.trust_results, enabled=cfg.enabled, readonly_paths=len(cfg.readonly_paths))
        if not cfg.enabled:
            await self.stop(name)
        self.sync_registry()
        return self.view(name)

    async def remove(self, name: str) -> bool:
        await self.stop(name)
        ok = self.store.delete(name)
        if ok:
            self._tools.pop(name, None)
            self._warns.pop(name, None)
            self._errors.pop(name, None)
            base = (self.data_dir / "mcp").resolve()
            d = (base / name).resolve()
            if d.parent == base and d.is_dir():           # 只删自己的私有目录
                shutil.rmtree(d, ignore_errors=True)
            self._audit("mcp_remove", name)
            self.sync_registry()
        return ok

    # ------------------------------------------------------------ 视图
    def view(self, name: str) -> dict:
        cfg = self.store.get(name)
        if cfg is None:
            return {}
        s = self.sessions.get(name)
        d = cfg.public()
        d["env_masked"] = {k: MASK for k in cfg.env}
        d["status"] = ("running" if s is not None and s.alive else "error" if name in self._errors else "idle") if cfg.enabled else "disabled"
        d["error"] = self._errors.get(name, "")
        d["tools"] = [{"name": t["name"], "tool": t["tool"], "description": t["description"]} for t in self._tools.get(name, [])]
        d["warnings"] = self._warns.get(name, [])
        d["sandbox_effective"] = s.launch.sandbox if s is not None and s.alive else self.effective_sandbox(name)
        d["contained"] = self.contained(name)
        d["stderr"] = s.stderr_tail(20) if s is not None else ""
        d["server_info"] = s.info.get("server", {}) if s is not None else {}
        return d

    def list_view(self) -> dict:
        info = sec.sandbox_info()
        return {"servers": [self.view(n) for n in sorted(self.store.servers)],
                "sandbox": {"available": bool(info["available"]), "kind": info.get("kind", "none"), "note": info.get("note", "")}}

    def _audit(self, event: str, name: str, **kw: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.append(event=event, server=name, **kw)
            except Exception:  # noqa: BLE001
                pass
