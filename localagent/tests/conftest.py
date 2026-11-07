"""测试夹具：临时 LAGENT_HOME + 假 llama-server + 真实启动的 LocalAgent 服务进程。"""
from __future__ import annotations

import http.client
import json
from urllib.parse import quote
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import make_gguf  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, port: int, token: str):
        self.port, self.token = port, token
        self.cookie = f"la_token={token}"

    def req(self, method: str, path: str, body=None, headers=None, cookie=True, host=None, raw: bytes | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, quote(path, safe="/?&=%:.-_~+"), body=data, headers=h)
        r = c.getresponse()
        txt = r.read()
        try:
            js = json.loads(txt)
        except ValueError:
            js = txt.decode("utf-8", "replace")
        c.close()
        return r.status, js

    def get(self, p, **k): return self.req("GET", p, **k)
    def post(self, p, b=None, **k): return self.req("POST", p, b if b is not None else {}, **k)
    def patch(self, p, b, **k): return self.req("PATCH", p, b, **k)
    def put(self, p, b, **k): return self.req("PUT", p, b, **k)
    def delete(self, p, **k): return self.req("DELETE", p, **k)

    def stream(self, rid: str, on_event=None, timeout=90):
        """读取运行 SSE 直到 run_end；on_event(event, data) 可在中途调用 API（例如审批）。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.request("GET", f"/api/runs/{rid}/events", headers={"Cookie": self.cookie, "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        assert r.status == 200, r.status
        evs, ev, data = [], None, None
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = r.readline()
            if not line:
                break
            line = line.decode().rstrip("\n")
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
            elif line == "" and ev is not None:
                evs.append((ev, data))
                if on_event:
                    on_event(ev, data)
                if ev == "run_end":
                    break
                ev, data = None, None
        c.close()
        return evs

    def wait_engine(self, status="ready", timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, st = self.get("/api/state")
            if st["engine"]["status"] == status:
                return st["engine"]
            if st["engine"]["status"] == "error" and status != "error":
                raise AssertionError(st["engine"])
            time.sleep(0.3)
        raise AssertionError("engine timeout " + status)

    def chat(self, sid, text, decisions=None, **extra):
        """发送消息并自动处理审批：decisions 是 dict(工具名→决定) 或默认 'allow'。"""
        s, r = self.post(f"/api/sessions/{sid}/messages", {"text": text, **extra})
        assert s == 200, (s, r)
        decisions = decisions or {}

        def on(ev, d):
            if ev == "approval_request":
                self.post(f"/api/approvals/{d['approval_id']}", {"decision": decisions.get(d["name"], decisions.get("*", "allow"))})
        return self.stream(r["run_id"], on)


@pytest.fixture(scope="session")
def home(tmp_path_factory):
    h = tmp_path_factory.mktemp("lahome")
    (h / "models").mkdir()
    make_gguf.make(h / "models" / "Mock-MoE-A1B-Q4_K_M.gguf")
    make_gguf.make(h / "models" / "mmproj-Mock-MoE-A1B-F16.gguf", arch="clip", experts=0, layers=2, name="mmproj")
    (h / "bin").mkdir()
    shutil.copy(ROOT / "tests" / "mock_llama_server.py", h / "bin" / "llama-server")
    os.chmod(h / "bin" / "llama-server", 0o755)
    (h / "ws").mkdir()
    return h


@pytest.fixture(scope="session")
def server(home):
    port = free_port()
    env = {**os.environ, "LAGENT_HOME": str(home), "MOCK_LOG": str(home / "mock.log"), "NO_PROXY": "127.0.0.1,localhost"}
    p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)], cwd=str(ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    token = None
    t0 = time.time()
    while time.time() - t0 < 30:
        line = p.stdout.readline()
        m = re.search(r"\?t=(\S+)", line or "")
        if m:
            token = m.group(1)
            break
        if p.poll() is not None:
            raise RuntimeError("server died: " + (line or ""))
    assert token, "no token"
    api = Api(port, token)
    for _ in range(50):
        try:
            if api.get("/api/state")[0] == 200:
                break
        except OSError:
            time.sleep(0.2)
    api.home = home
    yield api
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


@pytest.fixture(scope="session")
def ready(server, home):
    """工作区已设置、引擎已加载。"""
    s, r = server.put("/api/workspace", {"path": str(home / "ws")})
    assert s == 200, r
    s, _ = server.post("/api/engine/start", {})
    assert s == 200
    server.wait_engine("ready")
    return server


def mock_log(home: Path) -> list[dict]:
    p = home / "mock.log"
    return [json.loads(x) for x in p.read_text("utf-8").splitlines()] if p.exists() else []
