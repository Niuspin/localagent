"""一个 MCP 服务器进程的生命周期：启动 → 握手 → 列工具 → 调用 → 停止。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .launch import Launch
from .rpc import STREAM_LIMIT, RpcClosed, RpcError, StdioRpc

PROTOCOL = "2025-06-18"
ACCEPTED = {"2025-06-18", "2025-03-26", "2024-11-05"}
HANDSHAKE_TIMEOUT = 120.0          # 首次 npx/uvx 可能要下载依赖
MAX_PAGES, MAX_RAW_TOOLS = 10, 200


class McpError(Exception):
    def __init__(self, msg: str, undelivered: bool = False):
        super().__init__(msg)
        self.undelivered = undelivered


class McpSession:
    def __init__(self, name: str, launch: Launch, *, workspace: str | None, on_tools_changed: Callable[[], None] | None = None):
        self.name, self.launch, self.started_ws = name, launch, workspace
        self.rpc: StdioRpc | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.info: dict = {}
        self.last_used = time.monotonic()
        self._on_changed = on_tools_changed
        self.tools_dirty = False

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.rpc is not None and not self.rpc.closed

    def stderr_tail(self, n: int = 12) -> str:
        return self.rpc.stderr_tail(n) if self.rpc else ""

    def _notification(self, method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed":
            self.tools_dirty = True
            if self._on_changed:
                self._on_changed()

    async def start(self) -> None:
        L = self.launch
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *L.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(L.cwd), env=L.env, limit=STREAM_LIMIT, **L.kw)
        except (OSError, ValueError) as e:
            raise McpError(f"无法启动: {e}")
        self.rpc = StdioRpc(self.proc, on_notification=self._notification)
        try:
            res = await self.rpc.request("initialize", {
                "protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "LocalAgent", "version": "1"}}, timeout=HANDSHAKE_TIMEOUT)
            ver = (res or {}).get("protocolVersion")
            if ver not in ACCEPTED:
                raise McpError(f"服务器使用了不支持的协议版本 {ver!r}")
            self.info = {"protocol": ver, "server": (res or {}).get("serverInfo") or {},
                         "instructions": str((res or {}).get("instructions") or "")[:500]}
            await self.rpc.notify("notifications/initialized")
        except (RpcError, RpcClosed, asyncio.TimeoutError) as e:
            await self.stop()
            raise McpError(self._why("握手失败", e))
        except McpError:
            await self.stop()
            raise
        self.last_used = time.monotonic()

    def _why(self, what: str, e: BaseException) -> str:
        msg = "超时" if isinstance(e, asyncio.TimeoutError) else str(e)
        tail = self.stderr_tail(4)
        return f"{what}: {msg}" + (f"\n（服务器输出：{tail[-400:]}）" if tail else "")

    async def list_tools(self) -> list[dict]:
        if not self.alive:
            raise McpError("服务器未运行")
        out: list[dict] = []
        cursor: str | None = None
        try:
            for _ in range(MAX_PAGES):
                res = await self.rpc.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=30)  # type: ignore[union-attr]
                out += [t for t in (res or {}).get("tools", []) if isinstance(t, dict)]
                cursor = (res or {}).get("nextCursor")
                if not cursor or len(out) >= MAX_RAW_TOOLS:
                    break
        except (RpcError, RpcClosed, asyncio.TimeoutError) as e:
            raise McpError(self._why("获取工具列表失败", e))
        self.tools_dirty = False
        return out[:MAX_RAW_TOOLS]

    async def call(self, tool: str, args: dict, timeout: float) -> dict:
        if not self.alive:
            raise McpError("服务器已退出" + (f"：{self.stderr_tail(3)[-300:]}" if self.rpc else ""))
        self.last_used = time.monotonic()
        try:
            res = await self.rpc.request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)  # type: ignore[union-attr]
        except RpcError as e:
            raise McpError(f"服务器返回错误 {e.code}: {e}")
        except asyncio.TimeoutError:
            raise McpError(f"调用超过 {int(timeout)} 秒未返回，已取消")
        except RpcClosed as e:
            raise McpError(self._why("连接中断", e), undelivered=e.undelivered)
        finally:
            self.last_used = time.monotonic()
        return res if isinstance(res, dict) else {}

    async def stop(self) -> None:
        rpc, self.rpc = self.rpc, None
        if rpc is not None:
            await rpc.close()
        self.proc = None
