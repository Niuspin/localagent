"""JSON-RPC 2.0 over stdio 传输。MCP 的 stdio 规范：每行一条 JSON 消息（消息内不含换行），服务器日志走 stderr。

只负责传输：请求/响应配对、服务器发来的请求（ping 回 {}，其余回“方法不存在”，免得服务器干等）、通知回调、
超时与取消（会向服务器发 notifications/cancelled）、stderr 滚动缓冲（出错时给用户看原因）、优雅关闭。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from typing import Any, Callable

STREAM_LIMIT = 16 * 1024 * 1024        # 单条消息上限（大结果/图片可能很大）


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class RpcClosed(Exception):
    """连接已关闭（进程退出/管道断开）。undelivered=True 表示请求根本没发出去，可以安全地重启后重试。"""
    def __init__(self, msg: str = "", undelivered: bool = False):
        super().__init__(msg)
        self.undelivered = undelivered


class StdioRpc:
    def __init__(self, proc: asyncio.subprocess.Process, *, on_notification: Callable[[str, dict], None] | None = None):
        self.proc = proc
        self._on_note = on_notification
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._wlock = asyncio.Lock()
        self._stderr: deque[str] = deque(maxlen=200)
        self.closed = False
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._drain_stderr())]

    # ------------------------------------------------------------ 状态
    def stderr_tail(self, n: int = 12) -> str:
        return "\n".join(list(self._stderr)[-n:])

    def _note(self, line: str) -> None:
        self._stderr.append(line[:500])

    # ------------------------------------------------------------ 收
    async def _drain_stderr(self) -> None:
        try:
            assert self.proc.stderr
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                self._note(line.decode("utf-8", "replace").rstrip())
        except (asyncio.CancelledError, ValueError):
            return

    async def _read_loop(self) -> None:
        try:
            assert self.proc.stdout
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self._note("(单条消息超过 16MB，连接已关闭)")
                    return
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    self._note("(stdout 里出现非 JSON 内容) " + text[:200])     # 有些服务器会往 stdout 打印横幅，忽略即可
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    if isinstance(m, dict):
                        await self._dispatch(m)
        except asyncio.CancelledError:
            raise
        finally:
            self._fail_all()

    async def _dispatch(self, m: dict) -> None:
        if "method" in m and "id" in m:                       # 服务器发来的请求：我们没有声明任何客户端能力
            method = m["method"]
            if method == "ping":
                await self._send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
            else:
                await self._send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": f"Method not found: {method}"}})
        elif "method" in m:
            if self._on_note:
                try:
                    self._on_note(str(m["method"]), m.get("params") or {})
                except Exception:  # noqa: BLE001
                    pass
        elif "id" in m:
            fut = self._pending.pop(m["id"], None) if isinstance(m["id"], int) else None
            if fut is None or fut.done():
                return
            if "error" in m and isinstance(m["error"], dict):
                e = m["error"]
                fut.set_exception(RpcError(int(e.get("code", -32000)), str(e.get("message", "error")), e.get("data")))
            else:
                fut.set_result(m.get("result"))

    def _fail_all(self) -> None:
        self.closed = True
        tail = self.stderr_tail(6)
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RpcClosed("服务器进程已退出" + (f"。最后的日志:\n{tail}" if tail else "")))
        self._pending.clear()

    # ------------------------------------------------------------ 发
    async def _send(self, obj: dict) -> None:
        if self.closed:
            raise RpcClosed("连接已关闭", undelivered=True)
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._wlock:
            try:
                assert self.proc.stdin
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                self.closed = True
                raise RpcClosed(f"写入失败: {e}", undelivered=True) from e

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    async def request(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> Any:
        self._next += 1
        rid = self._next
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._pending.pop(rid, None)
            if not self.closed:                              # 告诉服务器别再做了（尽力而为，不等待）
                asyncio.ensure_future(self._cancel_quietly(rid))
            raise
        except BaseException:
            self._pending.pop(rid, None)
            raise

    async def _cancel_quietly(self, rid: int) -> None:
        with contextlib.suppress(Exception):
            await self.notify("notifications/cancelled", {"requestId": rid, "reason": "client timeout or cancel"})

    # ------------------------------------------------------------ 关
    async def close(self, grace: float = 3.0) -> None:
        """MCP stdio 规范的关闭顺序：关 stdin → 等一会 → SIGTERM → 杀进程树。"""
        from ..tools.files import kill_tree
        self.closed = True
        with contextlib.suppress(Exception):
            assert self.proc.stdin
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), grace)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2.0)
            except asyncio.TimeoutError:
                kill_tree(self.proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.proc.wait(), 3.0)
        for t in self._tasks:
            t.cancel()
        self._fail_all()
