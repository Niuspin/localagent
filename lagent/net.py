"""标准库 HTTP 客户端（零第三方依赖）：SSRF 防护（DNS 固定）、HTTP 代理、gzip 限量解压、流式 SSE。"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import zlib
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable
from urllib.parse import urljoin, urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 LocalAgent/0.1")


class NetError(Exception):
    pass


class HTTPStatusError(NetError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body

    def json(self) -> dict:
        try:
            v = json.loads(self.body)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    def text(self) -> str:
        ct = self.headers.get("content-type", "")
        m = re.search(r"charset=([\w-]+)", ct, re.I) or re.search(rb"charset=[\"']?([\w-]+)", self.body[:4096], re.I)
        enc = None
        if m:
            g = m.group(1)
            enc = g.decode("ascii", "ignore") if isinstance(g, bytes) else g
        for e in ([enc] if enc else []) + ["utf-8", "gb18030"]:
            try:
                return self.body.decode(e)  # type: ignore[arg-type]
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")


# ---------------------------------------------------------------- SSRF
def is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return bool(a.is_global and not a.is_multicast)


def check_hostname_literal(host: str) -> None:
    h = host.lower().rstrip(".")
    if not h:
        raise NetError("URL 缺少主机名")
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        if not is_public_ip(str(ip)):
            raise NetError(f"拒绝访问内网/保留地址: {host}")
        return
    except ValueError:
        pass
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")) or "." not in h:
        raise NetError(f"拒绝访问内部主机名: {host}")


def resolve_public(host: str, port: int) -> list[str]:
    check_hostname_literal(host)
    try:
        infos = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise NetError(f"DNS 解析失败: {host}")
    ips: list[str] = []
    for _fam, _t, _p, _c, sa in infos:
        ip = sa[0]
        if not is_public_ip(ip):
            raise NetError(f"拒绝访问内网/保留地址: {host} → {ip}")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise NetError(f"DNS 无结果: {host}")
    return ips


def domain_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


# ---------------------------------------------------------------- 连接类
def _dial(ips: list[str], port: int, timeout: float) -> socket.socket:
    last: Exception | None = None
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as e:
            last = e
    raise NetError(f"连接失败: {last}")


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ips = ips

    def connect(self) -> None:
        self.sock = _dial(self._ips, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float, ctx: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._ips, self._ctx = ips, ctx

    def connect(self) -> None:
        s = _dial(self._ips, self.port, self.timeout)
        self.sock = self._ctx.wrap_socket(s, server_hostname=self.host)


class _TunnelHTTPS(http.client.HTTPConnection):
    """经 HTTP 代理 CONNECT 隧道，再做 TLS（证书按目标主机校验）。"""

    def __init__(self, phost: str, pport: int, host: str, port: int, timeout: float, ctx: ssl.SSLContext, hdrs: dict):
        super().__init__(phost, pport, timeout=timeout)
        self.set_tunnel(host, port, hdrs)
        self._ctx, self._thost = ctx, host

    def connect(self) -> None:
        super().connect()
        self.sock = self._ctx.wrap_socket(self.sock, server_hostname=self._thost)


def _parse_proxy(proxy: str) -> tuple[str, int, dict]:
    p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if p.scheme not in ("http", "https"):
        raise NetError("暂只支持 HTTP 代理（如 http://127.0.0.1:7890）；socks 请改用代理软件的 HTTP 端口")
    hdrs: dict[str, str] = {}
    if p.username:
        import base64
        tok = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
        hdrs["Proxy-Authorization"] = f"Basic {tok}"
    return p.hostname or "127.0.0.1", p.port or 80, hdrs


# ---------------------------------------------------------------- fetch
def fetch_sync(url: str, *, method: str = "GET", headers: dict | None = None, data: bytes | None = None,
               timeout: float = 20.0, max_bytes: int = 3_000_000, proxy: str = "", allow_private: bool = False,
               max_redirects: int = 5, on_redirect: Callable[[str], None] | None = None) -> Response:
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate", "Connection": "close"}
    hdrs.update(headers or {})
    ctx = ssl.create_default_context()
    chain: list[str] = []
    for _hop in range(max_redirects + 1):
        sp = urlsplit(url)
        if sp.scheme not in ("http", "https"):
            raise NetError(f"只支持 http/https: {url[:80]}")
        if sp.username or sp.password:
            raise NetError("拒绝带用户名密码的 URL")
        host = sp.hostname or ""
        port = sp.port or (443 if sp.scheme == "https" else 80)
        path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
        conn: http.client.HTTPConnection
        req_target = path
        if proxy:
            if not allow_private:
                check_hostname_literal(host)  # 代理场景下 DNS 由代理完成（兼容 fake-ip）
            ph, pp, phdrs = _parse_proxy(proxy)
            if sp.scheme == "https":
                conn = _TunnelHTTPS(ph, pp, host, port, timeout, ctx, phdrs)
            else:
                conn = http.client.HTTPConnection(ph, pp, timeout=timeout)
                req_target = url
                hdrs.update(phdrs)
        elif allow_private:
            conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx) if sp.scheme == "https"
                    else http.client.HTTPConnection(host, port, timeout=timeout))
        else:
            ips = resolve_public(host, port)
            conn = (_PinnedHTTPS(host, port, ips, timeout, ctx) if sp.scheme == "https"
                    else _PinnedHTTP(host, port, ips, timeout))
        try:
            h = dict(hdrs)
            h["Host"] = host if sp.port is None else f"{host}:{sp.port}"
            try:
                conn.request(method, req_target, body=data, headers=h)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException, ssl.SSLError) as e:
                raise NetError(f"请求失败: {e}")
            status = resp.status
            if status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                nxt = urljoin(url, resp.getheader("Location"))
                resp.read(0)
                if on_redirect:
                    on_redirect(nxt)
                if domain_of(nxt) != domain_of(url):
                    hdrs.pop("Authorization", None)
                    hdrs.pop("Cookie", None)
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, data = "GET", None
                chain.append(nxt)
                url = nxt
                continue
            enc = (resp.getheader("Content-Encoding") or "").lower()
            dec = zlib.decompressobj(47) if enc in ("gzip", "deflate", "x-gzip") else None
            out = bytearray()
            truncated = False
            try:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    if dec:
                        buf = chunk
                        while buf and len(out) <= max_bytes:
                            out += dec.decompress(buf, max_bytes + 1 - len(out))
                            buf = dec.unconsumed_tail
                    else:
                        out += chunk
                    if len(out) > max_bytes:
                        truncated = True
                        del out[max_bytes:]
                        break
            except (OSError, http.client.HTTPException, zlib.error) as e:
                if not out:
                    raise NetError(f"读取响应失败: {e}")
                truncated = True
            return Response(status, {k.lower(): v for k, v in resp.getheaders()}, bytes(out), url, truncated, chain)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    raise NetError("重定向次数过多")


async def fetch(url: str, **kw: Any) -> Response:
    return await asyncio.to_thread(fetch_sync, url, **kw)


# ---------------------------------------------------------------- 本地 llama-server 通信
def _local_request(host: str, port: int, method: str, path: str, body: Any, headers: dict, timeout: float) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        h = {"Accept": "application/json", **headers}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read(20_000_000)
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")
    finally:
        conn.close()


async def local_json(host: str, port: int, method: str, path: str, body: Any = None,
                     headers: dict | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    try:
        return await asyncio.to_thread(_local_request, host, port, method, path, body, headers or {}, timeout)
    except (OSError, http.client.HTTPException) as e:
        raise NetError(str(e))


async def sse_post(host: str, port: int, path: str, body: dict, headers: dict | None = None,
                   read_timeout: float = 1800.0) -> AsyncIterator[dict]:
    """POST 并把 `data: {...}` 行解析成 dict 逐个产出。消费者退出/取消时会立刻断开连接（让 llama-server 停止生成）。"""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    holder: dict[str, Any] = {}

    def put(item: tuple) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:
            pass

    def worker() -> None:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=15)
            holder["conn"] = conn
            data = json.dumps(body).encode()
            conn.request("POST", path, body=data, headers={"Content-Type": "application/json",
                                                           "Accept": "text/event-stream", **(headers or {})})
            if conn.sock:
                conn.sock.settimeout(read_timeout)
            resp = conn.getresponse()
            if resp.status != 200:
                put(("http_error", resp.status, resp.read(8000).decode("utf-8", "replace")))
                return
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    put(("data", json.loads(payload)))
                except ValueError:
                    continue
        except Exception as e:  # noqa: BLE001
            if not stop.is_set():
                put(("exc", e))
        finally:
            put(("end",))

    t = threading.Thread(target=worker, daemon=True, name="sse-post")
    t.start()
    try:
        while True:
            item = await q.get()
            k = item[0]
            if k == "data":
                yield item[1]
            elif k == "http_error":
                raise HTTPStatusError(item[1], item[2])
            elif k == "exc":
                raise NetError(f"与引擎通信失败: {item[1]}")
            else:
                break
    finally:
        stop.set()
        conn = holder.get("conn")
        if conn is not None:
            try:
                if conn.sock:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
