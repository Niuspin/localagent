"""一个 MCP 服务器会话的生命周期：支持本机 stdio 进程与远程/本地 Streamable HTTP(SSE) 服务器。"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import security as sec
from .launch import Launch
from .rpc import STREAM_LIMIT, RpcClosed, RpcError, StdioRpc
from .store import ServerConfig

PROTOCOL = "2025-06-18"
ACCEPTED = {"2025-06-18", "2025-03-26", "2024-11-05"}
HANDSHAKE_TIMEOUT = 120.0          # 首次 npx/uvx 可能要下载依赖
MAX_PAGES, MAX_RAW_TOOLS = 10, 200


class McpError(Exception):
    def __init__(self, msg: str, undelivered: bool = False):
        super().__init__(msg)
        self.undelivered = undelivered


class _BaseMcpSession:
    def __init__(self, name: str, *, workspace: str | None, on_tools_changed: Callable[[], None] | None = None):
        self.name, self.started_ws = name, workspace
        self.info: dict = {}
        self.last_used = time.monotonic()
        self._on_changed = on_tools_changed
        self.tools_dirty = False
        self.resources_dirty = False
        self.prompts_dirty = False
        self.proc: asyncio.subprocess.Process | None = None
        self.launch: Launch | None = None

    @property
    def alive(self) -> bool:
        return False

    def stderr_tail(self, n: int = 12) -> str:
        return ""

    def _notification(self, method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed":
            self.tools_dirty = True
            if self._on_changed:
                self._on_changed()
        elif method == "notifications/resources/list_changed":
            self.resources_dirty = True
            if self._on_changed:
                self._on_changed()
        elif method == "notifications/prompts/list_changed":
            self.prompts_dirty = True
            if self._on_changed:
                self._on_changed()

    def _roots_list(self) -> list[dict]:
        if not self.started_ws:
            return []
        try:
            p = Path(self.started_ws).resolve()
            return [{"uri": p.as_uri(), "name": p.name or str(p)}]
        except OSError:
            return []

    def _init_params(self) -> dict:
        caps = {"roots": {"listChanged": False}} if self.started_ws else {}
        return {"protocolVersion": PROTOCOL, "capabilities": caps, "clientInfo": {"name": "LocalAgent", "version": "1"}}

    async def _rpc_request(self, method: str, params: dict | None, timeout: float) -> Any:
        raise NotImplementedError

    def _why(self, what: str, e: BaseException) -> str:
        msg = "超时" if isinstance(e, asyncio.TimeoutError) else str(e)
        tail = self.stderr_tail(4)
        return f"{what}: {msg}" + (f"\n（服务器输出：{tail[-400:]}）" if tail else "")

    async def _paginate(self, method: str, key: str, what: str, *, allow_unsupported: bool = False) -> list[dict]:
        if not self.alive:
            raise McpError("服务器未运行")
        out: list[dict] = []
        cursor: str | None = None
        try:
            for _ in range(MAX_PAGES):
                res = await self._rpc_request(method, {"cursor": cursor} if cursor else {}, timeout=30.0)
                out += [t for t in (res or {}).get(key, []) if isinstance(t, dict)]
                cursor = (res or {}).get("nextCursor")
                if not cursor or len(out) >= MAX_RAW_TOOLS:
                    break
        except RpcError as e:
            if allow_unsupported and e.code in (-32601, -32602):
                return []
            raise McpError(self._why(what, e))
        except (RpcClosed, asyncio.TimeoutError, McpError) as e:
            if allow_unsupported and isinstance(e, McpError) and ("Method not found" in str(e) or "-32601" in str(e)):
                return []
            if isinstance(e, McpError):
                raise
            raise McpError(self._why(what, e))
        return out[:MAX_RAW_TOOLS]

    async def list_tools(self) -> list[dict]:
        out = await self._paginate("tools/list", "tools", "获取工具列表失败")
        self.tools_dirty = False
        return out

    async def list_resources(self) -> list[dict]:
        out = await self._paginate("resources/list", "resources", "获取资源列表失败", allow_unsupported=True)
        self.resources_dirty = False
        return out

    async def read_resource(self, uri: str, timeout: float = 30.0) -> dict:
        if not self.alive:
            raise McpError("服务器未运行")
        self.last_used = time.monotonic()
        try:
            res = await self._rpc_request("resources/read", {"uri": uri}, timeout=timeout)
        except RpcError as e:
            raise McpError(f"服务器返回错误 {e.code}: {e}")
        except asyncio.TimeoutError:
            raise McpError(f"读取资源超过 {int(timeout)} 秒未返回")
        except RpcClosed as e:
            raise McpError(self._why("连接中断", e), undelivered=e.undelivered)
        finally:
            self.last_used = time.monotonic()
        return res if isinstance(res, dict) else {}

    async def list_prompts(self) -> list[dict]:
        out = await self._paginate("prompts/list", "prompts", "获取提示词模板失败", allow_unsupported=True)
        self.prompts_dirty = False
        return out

    async def get_prompt(self, prompt_name: str, arguments: dict | None = None, timeout: float = 30.0) -> dict:
        if not self.alive:
            raise McpError("服务器未运行")
        self.last_used = time.monotonic()
        params: dict[str, Any] = {"name": prompt_name}
        if arguments:
            params["arguments"] = arguments
        try:
            res = await self._rpc_request("prompts/get", params, timeout=timeout)
        except RpcError as e:
            raise McpError(f"服务器返回错误 {e.code}: {e}")
        except asyncio.TimeoutError:
            raise McpError(f"获取提示词超过 {int(timeout)} 秒未返回")
        except RpcClosed as e:
            raise McpError(self._why("连接中断", e), undelivered=e.undelivered)
        finally:
            self.last_used = time.monotonic()
        return res if isinstance(res, dict) else {}

    async def call(self, tool: str, args: dict, timeout: float) -> dict:
        if not self.alive:
            tail = self.stderr_tail(3)[-300:]
            raise McpError("服务器已退出" + (f"：{tail}" if tail else ""))
        self.last_used = time.monotonic()
        try:
            res = await self._rpc_request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        except RpcError as e:
            raise McpError(f"服务器返回错误 {e.code}: {e}")
        except asyncio.TimeoutError:
            raise McpError(f"调用超过 {int(timeout)} 秒未返回，已取消")
        except RpcClosed as e:
            raise McpError(self._why("连接中断", e), undelivered=e.undelivered)
        finally:
            self.last_used = time.monotonic()
        return res if isinstance(res, dict) else {}


class McpSession(_BaseMcpSession):
    def __init__(self, name: str, launch: Launch, *, workspace: str | None, on_tools_changed: Callable[[], None] | None = None):
        super().__init__(name, workspace=workspace, on_tools_changed=on_tools_changed)
        self.launch = launch
        self.rpc: StdioRpc | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.rpc is not None and not self.rpc.closed

    def stderr_tail(self, n: int = 12) -> str:
        return self.rpc.stderr_tail(n) if self.rpc else ""

    async def _rpc_request(self, method: str, params: dict | None, timeout: float) -> Any:
        return await self.rpc.request(method, params, timeout=timeout)  # type: ignore[union-attr]

    async def start(self) -> None:
        L = self.launch
        assert L is not None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *L.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(L.cwd), env=L.env, limit=STREAM_LIMIT, **L.kw)
        except (OSError, ValueError) as e:
            raise McpError(f"无法启动: {e}")
        if self.proc.pid:
            sec.harden_windows_process(self.proc.pid)
        self.rpc = StdioRpc(self.proc, on_notification=self._notification, roots_fn=self._roots_list if self.started_ws else None)
        try:
            res = await self.rpc.request("initialize", self._init_params(), timeout=HANDSHAKE_TIMEOUT)
            ver = (res or {}).get("protocolVersion")
            if ver not in ACCEPTED:
                raise McpError(f"服务器使用了不支持的协议版本 {ver!r}")
            self.info = {"protocol": ver, "server": (res or {}).get("serverInfo") or {},
                         "capabilities": (res or {}).get("capabilities") or {},
                         "instructions": str((res or {}).get("instructions") or "")[:500]}
            await self.rpc.notify("notifications/initialized")
        except (RpcError, RpcClosed, asyncio.TimeoutError) as e:
            await self.stop()
            raise McpError(self._why("握手失败", e))
        except McpError:
            await self.stop()
            raise
        self.last_used = time.monotonic()

    async def stop(self) -> None:
        rpc, self.rpc = self.rpc, None
        pid = self.proc.pid if self.proc else 0
        if rpc is not None:
            await rpc.close()
        self.proc = None
        if pid:
            sec.release_windows_job(pid)


class HttpMcpSession(_BaseMcpSession):
    """MCP Streamable HTTP（application/json + text/event-stream）传输会话。"""

    def __init__(self, name: str, cfg: ServerConfig, *, workspace: str | None,
                 on_tools_changed: Callable[[], None] | None = None,
                 policy: Any = None, ledger: Any = None):
        super().__init__(name, workspace=workspace, on_tools_changed=on_tools_changed)
        self.cfg = cfg
        self.policy = policy
        self.ledger = ledger
        self.session_id: str | None = None
        self._alive = False
        self._next = 0
        self._sse_task: asyncio.Task | None = None
        self._sse_stop = threading.Event()
        self._sse_conn: http.client.HTTPConnection | None = None

    @property
    def alive(self) -> bool:
        return self._alive

    def _open_conn(self, timeout: float) -> tuple[http.client.HTTPConnection, str]:
        sp = urlsplit(self.cfg.url)
        host = sp.hostname or ""
        port = sp.port or (443 if sp.scheme == "https" else 80)
        path = (sp.path or "/") + (f"?{sp.query}" if sp.query else "")
        if sp.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context()), path
        return http.client.HTTPConnection(host, port, timeout=timeout), path

    def _parse_sse(self, resp: http.client.HTTPResponse, rid: int | None) -> Any:
        buf: list[str] = []
        result_msg: dict | None = None
        while True:
            raw = resp.readline(2_000_000)
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if buf:
                    try:
                        msg = json.loads("\n".join(buf))
                    except ValueError:
                        buf.clear()
                        continue
                    buf.clear()
                    items = msg if isinstance(msg, list) else [msg]
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        if "method" in m and "id" not in m:
                            self._notification(str(m["method"]), m.get("params") or {})
                        elif rid is not None and m.get("id") == rid:
                            result_msg = m
                            return self._unwrap(result_msg)
                continue
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
        if rid is not None:
            if result_msg is None:
                raise McpError("SSE 响应未返回目标 JSON-RPC 结果")
            return self._unwrap(result_msg)
        return None

    @staticmethod
    def _unwrap(m: dict) -> Any:
        if "error" in m and isinstance(m["error"], dict):
            e = m["error"]
            raise RpcError(int(e.get("code", -32000)), str(e.get("message", "error")), e.get("data"))
        return m.get("result")

    def _post_sync(self, obj: dict, timeout: float, rid: int | None) -> Any:
        if os.environ.get("LOCALAGENT_OFFLINE") == "1":
            h = (urlsplit(self.cfg.url).hostname or "").lower().strip("[]")
            if h not in ("127.0.0.1", "::1", "localhost"):
                raise McpError("MCP HTTP 请求被策略拦截: 离线模式（--offline）已禁用外部网络连接")
        if self.policy is not None:
            dec = self.policy.evaluate_url(self.cfg.url, purpose="mcp_http", allow_loopback=True)
            if not dec.allowed:
                if self.ledger is not None:
                    self.ledger.record(
                        url=self.cfg.url, method="POST", status=0, purpose=f"mcp:{self.name}",
                        decision="deny", reason=dec.reason,
                    )
                raise McpError(f"MCP HTTP 请求被策略拦截: {dec.reason}")
        conn, path = self._open_conn(timeout)
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.cfg.headers,
        }
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            if self.ledger is not None:
                self.ledger.record(
                    url=self.cfg.url, method="POST", status=resp.status, bytes_in=len(body),
                    purpose=f"mcp:{self.name}", decision="allow" if resp.status < 400 else "error",
                )
            sid = resp.getheader("Mcp-Session-Id") or resp.getheader("mcp-session-id")
            if sid:
                self.session_id = sid
            ct = (resp.getheader("Content-Type") or "").lower()
            if resp.status >= 400:
                err_txt = resp.read(2000).decode("utf-8", "replace")
                raise McpError(f"HTTP {resp.status}: {err_txt[:240]}")
            if resp.status in (202, 204):
                resp.read(4096)
                return None
            if "text/event-stream" in ct:
                return self._parse_sse(resp, rid)
            raw = resp.read(8 * 1024 * 1024)
            if not raw:
                return None
            msg = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(msg, list):
                for m in msg:
                    if isinstance(m, dict) and (rid is None or m.get("id") == rid):
                        return self._unwrap(m)
                raise McpError("批量响应中缺少目标 id")
            if isinstance(msg, dict):
                return self._unwrap(msg)
            raise McpError("HTTP MCP 返回了无效的 JSON-RPC 载荷")
        except (McpError, RpcError):
            raise
        except TimeoutError as e:
            raise asyncio.TimeoutError from e
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise McpError(f"HTTP MCP 请求失败: {e}", undelivered=True) from e
        finally:
            conn.close()

    async def _rpc_request(self, method: str, params: dict | None, timeout: float) -> Any:
        self._next += 1
        rid = self._next
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}
        return await asyncio.to_thread(self._post_sync, payload, timeout, rid)

    async def start(self) -> None:
        self._alive = True
        try:
            res = await self._rpc_request("initialize", self._init_params(), timeout=min(float(self.cfg.timeout_s), 30.0))
            ver = (res or {}).get("protocolVersion")
            if ver not in ACCEPTED:
                raise McpError(f"服务器使用了不支持的协议版本 {ver!r}")
            self.info = {"protocol": ver, "server": (res or {}).get("serverInfo") or {},
                         "capabilities": (res or {}).get("capabilities") or {},
                         "instructions": str((res or {}).get("instructions") or "")[:500]}
            try:
                await asyncio.to_thread(self._post_sync, {"jsonrpc": "2.0", "method": "notifications/initialized"}, 10.0, None)
            except McpError:
                pass
        except (RpcError, asyncio.TimeoutError, McpError) as e:
            await self.stop()
            raise e if isinstance(e, McpError) else McpError(self._why("HTTP 握手失败", e))
        self.last_used = time.monotonic()
        if self._sse_task is None or self._sse_task.done():
            self._sse_stop.clear()
            self._sse_task = asyncio.create_task(self._listen_sse())

    def _listen_sse_sync(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.environ.get("LOCALAGENT_OFFLINE") == "1":
            h = (urlsplit(self.cfg.url).hostname or "").lower().strip("[]")
            if h not in ("127.0.0.1", "::1", "localhost"):
                return
        if self.policy is not None:
            dec = self.policy.evaluate_url(self.cfg.url, purpose="mcp_http_sse", allow_loopback=True)
            if not dec.allowed:
                return
        conn = None
        try:
            conn, path = self._open_conn(60.0)
            self._sse_conn = conn
            hdrs = {"Accept": "text/event-stream", **self.cfg.headers}
            if self.session_id:
                hdrs["Mcp-Session-Id"] = self.session_id
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            ct = (resp.getheader("Content-Type") or "").lower()
            if resp.status != 200 or "text/event-stream" not in ct:
                return
            buf: list[str] = []
            while not self._sse_stop.is_set() and self._alive:
                raw = resp.readline(2_000_000)
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if buf:
                        try:
                            msg = json.loads("\n".join(buf))
                        except ValueError:
                            buf.clear()
                            continue
                        buf.clear()
                        for m in (msg if isinstance(msg, list) else [msg]):
                            if isinstance(m, dict) and "method" in m and "id" not in m:
                                loop.call_soon_threadsafe(self._notification, str(m["method"]), m.get("params") or {})
                    continue
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
        except Exception:  # noqa: BLE001
            return
        finally:
            self._sse_conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _listen_sse(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            await asyncio.to_thread(self._listen_sse_sync, loop)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        self._alive = False
        self._sse_stop.set()
        if self._sse_conn is not None:
            try:
                self._sse_conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self._sse_task is not None and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sse_task = None
        sid = self.session_id
        self.session_id = None
        if sid:
            def _del() -> None:
                try:
                    conn, path = self._open_conn(3.0)
                    try:
                        conn.request("DELETE", path, headers={"Mcp-Session-Id": sid, **self.cfg.headers})
                        conn.getresponse().read(256)
                    finally:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.to_thread(_del)
