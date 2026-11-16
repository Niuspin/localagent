"""M8 v0.2.0 集成加固、§8.2 安全回归清单、§8.3 性能预算、§8.4 本地运行指标与打包冒烟验证。"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from lagent import __version__, config, net
from lagent.artifacts import ArtifactStore
from lagent.hub import DownloadManager
from lagent.journal import Journal
from lagent.mcp.session import HttpMcpSession, McpError
from lagent.mcp.store import ServerConfig
from lagent.mission.policy import MissionPolicy
from lagent.research import ResearchManager, ResearchPolicy
from lagent.security import AuditLog, PathGuard, Policy, SecurityError, sensitive_reason
from lagent.skills import Skills
from lagent.swarm.roles import effective_permission, get_role
from lagent.tools import files as _files_tools, search as search_mod, web as web_mod  # noqa: F401
from lagent.tools.base import TOOLS
from lagent.tools.ctx import SessionState, ToolCtx

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def test_version_020_and_packaging_spec(server):
    assert __version__ == "0.2.0"

    # CLI --version
    res = subprocess.run([sys.executable, str(ROOT / "run.py"), "--version"], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0
    assert "LocalAgent 0.2.0" in (res.stdout + res.stderr)

    # HTTP /api/state 版本一致
    s, st = server.get("/api/state")
    assert s == 200 and st["version"] == "0.2.0"

    # PyInstaller spec & build.py 语法与资源完整性（含 web/ 与 builtin_skills/skills.lock）
    for rel in ("packaging/localagent.spec", "packaging/build.py"):
        src = (ROOT / rel).read_text("utf-8")
        compile(src, rel, "exec")
    spec_src = (ROOT / "packaging/localagent.spec").read_text("utf-8")
    for mod in ("lagent.bootstrap", "lagent.swarm", "lagent.mission", "lagent.media", "lagent.research", "lagent.skills", "lagent.metrics"):
        assert mod in spec_src
        importlib.import_module(mod)
    assert (ROOT / "web" / "index.html").is_file()
    assert (ROOT / "lagent" / "builtin_skills" / "skills.lock").is_file()


def test_m8_security_checklist_paths_blacklist_and_monotonic_perms(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET_OUTSIDE", "utf-8")
    (ws / ".env").write_text("API_KEY=secret-value-123\n", "utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n", "utf-8")

    # §8.2.1 PathGuard 越界与 Windows 保留/ADS 拦截
    guard = PathGuard(ws)
    with pytest.raises(SecurityError):
        guard.resolve("../outside.txt", write=False)
    with pytest.raises(SecurityError):
        guard.resolve("normal.txt:ads_stream", write=False)
    with pytest.raises(SecurityError):
        guard.resolve("\\\\evil\\share\\x.txt", write=False)
    with pytest.raises(SecurityError):
        guard.resolve(".git/config", write=True)

    # §8.2.6 新旧工具不绕过敏感文件黑名单
    cfg = config.Config(tmp_path / "config.json", tmp_path / "secrets.json")
    st = SessionState("s-sec")
    ctx = ToolCtx(cfg=cfg, guard=guard, journal=Journal(tmp_path / "data"), session=st)

    sens = sensitive_reason(".env")
    assert sens is not None
    dec = Policy.decide(kind="read", name="read_file", mode="auto", tainted=False, allow_keys=set(), sensitive=sens)
    assert dec.action == "ask"
    dec_ingest = Policy.decide(kind="read", name="ingest_media", mode="auto", tainted=False, allow_keys=set(), sensitive=sensitive_reason(".ssh/id_rsa"))
    assert dec_ingest.action == "ask"

    # ingest_media 与 read_file 对越界路径严格拦截（read_file 抛出 SecurityError，ingest_media 返回 error 或 SecurityError）
    with pytest.raises(SecurityError):
        _run(TOOLS["read_file"].fn(ctx, {"path": "../outside.txt"}))
    try:
        r_oob = _run(TOOLS["ingest_media"].fn(ctx, {"path": "../outside.txt"}))
        assert r_oob.error is True
    except SecurityError:
        pass

    # §8.2.2 子代理 / Swarm / Mission / Skill 权限单调收窄（绝不放大）
    exp_role = get_role("explore")
    assert exp_role.write_capable is False
    assert "read_file" in exp_role.allowed_tools and "write_file" not in exp_role.allowed_tools
    assert get_role("verifier").write_capable is False
    assert effective_permission("plan", "coder") == "plan"
    assert effective_permission("auto", "verifier") == "verify"

    mp = MissionPolicy({"mode": "unattended", "allow_paths": ["**"]}, guard=guard, workspace=ws)
    # 即使 allow_paths 为 **，工作区外仍被 PathGuard 阻断
    v_oob = mp.evaluate("write_file", {"path": "../escape.txt", "content": "x"}, tainted=False)
    assert v_oob.allowed is False and v_oob.action == "deny" and v_oob.hard is True
    # 受污染下不能无审批写操作
    v_taint = mp.evaluate("write_file", {"path": "ok.txt", "content": "x"}, tainted=True)
    assert v_taint.allowed is False and v_taint.action == "block"


def test_m8_offline_zero_outbound_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # §8.2.3 --offline 模式任何组件不得发起 socket 出站
    monkeypatch.setenv("LOCALAGENT_OFFLINE", "1")

    def _boom_url(*args, **kwargs):
        raise AssertionError("OUTBOUND SOCKET ATTEMPTED IN OFFLINE MODE")

    orig_connect = socket.socket.connect

    def _guard_connect(self_sock, addr):
        # 放行 Python asyncio Windows ProactorEventLoop 的内部 127.0.0.1 socketpair 管道，严格拦截外部连接
        host = addr[0] if isinstance(addr, tuple) and addr else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"OUTBOUND SOCKET ATTEMPTED IN OFFLINE MODE: {addr}")
        return orig_connect(self_sock, addr)

    monkeypatch.setattr(socket, "create_connection", _boom_url)
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(urllib.request, "urlopen", _boom_url)

    # 1. net.fetch
    with pytest.raises(net.NetError):
        _run(net.fetch("https://example.com"))

    # 2. web_search / web_fetch tools
    cfg = config.Config(tmp_path / "cfg.json", tmp_path / "sec.json")
    cfg.set("research", "mode", "open")
    ctx = ToolCtx(cfg=cfg, guard=PathGuard(tmp_path), journal=Journal(tmp_path / "j"), session=SessionState("off"))
    r_ws = _run(TOOLS["web_search"].fn(ctx, {"query": "python release"}))
    assert r_ws.error is True and "离线" in r_ws.content
    with pytest.raises(net.NetError):
        _run(TOOLS["web_fetch"].fn(ctx, {"url": "https://example.com"}))

    # 3. ResearchManager
    rm = ResearchManager(cfg=cfg, data_dir=tmp_path / "rdata")
    with pytest.raises(PermissionError):
        rm.start_research("offline test question")
    rm.close()

    # 4. DownloadManager (Model Hub)
    dm = DownloadManager(tmp_path / "models")
    with pytest.raises(PermissionError):
        dm.start_download("https://huggingface.co/test.gguf", "test.gguf")

    # 5. HttpMcpSession (含显式 policy 与默认无 policy 兜底均拦截)
    hs = HttpMcpSession(
        "remote",
        ServerConfig(name="remote", transport="http", url="https://mcp.example.com/rpc"),
        workspace=None,
        policy=ResearchPolicy(cfg),
    )
    with pytest.raises(McpError):
        hs._post_sync({"jsonrpc": "2.0", "method": "initialize", "id": 1}, 5.0, 1)

    hs_nop = HttpMcpSession("remote2", ServerConfig(name="remote2", transport="http", url="https://mcp.example.com/rpc"), workspace=None)
    with pytest.raises(McpError):
        hs_nop._post_sync({"jsonrpc": "2.0", "method": "initialize", "id": 1}, 5.0, 1)


def test_m8_guard_auth_artifacts_and_licenses(server):
    # §8.2.5 所有新增 v0.2.0 路由均受 Guard 保护（缺 Token/Cookie -> 401，错误 Host/Origin -> 403）
    v2_paths = [
        ("GET", "/api/metrics"),
        ("GET", "/api/swarms"),
        ("GET", "/api/missions"),
        ("GET", "/api/artifacts"),
        ("GET", "/api/research"),
        ("GET", "/api/net/ledger"),
        ("GET", "/api/skills/packs"),
        ("GET", "/api/skills/lint"),
        ("POST", "/api/skills/route"),
        ("GET", "/api/capabilities"),
    ]
    for method, p in v2_paths:
        s401, _ = server.req(method, p, body={} if method == "POST" else None, cookie=False)
        assert s401 == 401, f"Missing auth was not blocked on {method} {p}: got {s401}"

        s_host, _ = server.req(method, p, body={} if method == "POST" else None, host="evil.example:80")
        assert s_host == 403, f"Bad Host was not blocked on {method} {p}: got {s_host}"

        s_orig, _ = server.req(method, p, body={} if method == "POST" else None, headers={"Origin": "https://evil.example"})
        assert s_orig == 403, f"Bad Origin was not blocked on {method} {p}: got {s_orig}"

    # §8.2.4 产物下载头与 SVG/HTML 沙箱 XSS 防护（直接经会话/产物仓库测试并验服务端响应头）
    s_sess, sess = server.post("/api/sessions", {"workspace": str(server.home)})
    assert s_sess == 200
    sid = sess["id"]
    ws = server.home / "ws_art"
    ws.mkdir(exist_ok=True)
    svg_f = ws / "xss.svg"
    svg_f.write_text('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script><circle cx="5" cy="5" r="4"/></svg>', "utf-8")
    html_f = ws / "page.html"
    html_f.write_text("<html><body><script>fetch('https://evil.example')</script><h1>Hi</h1></body></html>", "utf-8")

    # 通过 server.home/data/artifacts 创建测试产物，并通过真实 HTTP 验证下载与预览防护
    store = ArtifactStore(server.home / "data" / "artifacts")
    a_svg = store.save_bytes(sid, svg_f.read_bytes(), kind="image", title="xss.svg", filename="xss.svg", mime="image/svg+xml")
    a_html = store.save_bytes(sid, html_f.read_bytes(), kind="html", title="page.html", filename="page.html", mime="text/html")

    st_prev_svg, prev_svg = server.get(f"/api/artifacts/{a_svg.id}/preview")
    assert st_prev_svg == 200
    assert "<script" not in prev_svg.get("svg", "").lower() and "onload" not in prev_svg.get("svg", "").lower()

    st_raw, raw_html = server.get(f"/api/artifacts/{a_html.id}/preview?raw=1")
    assert st_raw == 200
    # 校验下载头防嗅探与强制附件
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    conn.request("GET", f"/api/artifacts/{a_html.id}", headers={"Cookie": server.cookie, "Host": f"127.0.0.1:{server.port}"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 200
    assert "attachment" in (resp.getheader("Content-Disposition") or "").lower()
    assert (resp.getheader("X-Content-Type-Options") or "").lower() == "nosniff"

    # §8.2.7 第三方许可证清单完整性（requirements*.txt 内所有包名均已登记）
    lic_txt = (ROOT / "THIRD_PARTY_LICENSES.md").read_text("utf-8").lower()
    for req_name in ("requirements.txt", "requirements-dev.txt", "requirements-build.txt"):
        for raw_ln in (ROOT / req_name).read_text("utf-8").splitlines():
            ln = raw_ln.split("#", 1)[0].strip()
            if not ln or ln.startswith("-r"):
                continue
            pkg = re.split(r"[<>=!~\[]", ln, maxsplit=1)[0].strip().lower()
            assert pkg in lic_txt, f"{pkg} from {req_name} missing in THIRD_PARTY_LICENSES.md"


def test_m8_performance_budgets_and_local_metrics(server):
    # §8.3 静态资源预算 < 1.5 MB；技能提示硬上限 <= 14
    web_bytes = sum(p.stat().st_size for p in (ROOT / "web").rglob("*") if p.is_file())
    assert web_bytes < 1_500_000, f"web bundle too large: {web_bytes} bytes"

    sk = Skills(server.home / "skills", ROOT / "lagent" / "builtin_skills", server.home / "data" / "skills_state.json")
    idx_lines = [ln for ln in sk.index_text(limit=14).splitlines() if ln.startswith("- ")]
    assert len(idx_lines) <= 14

    # §8.4 本地运行指标 API (/api/metrics) 字段完整且纯本机汇聚
    audit = AuditLog(server.home / "data" / "audit.jsonl")
    audit.append(event="tool", tool="run_command", status="ok", session="s1")
    audit.append(event="denied", tool="write_file", reason="skill_allowed_tools", session="s1")
    audit.append(event="run_stats", session="s1", status="done", friction=0, prompt_tokens=120, completion_tokens=45)

    s, m = server.get("/api/metrics?hours=24")
    assert s == 200
    assert m["version"] == "0.2.0"
    assert m["local_only"] is True
    assert m["tokens"]["prompt"] >= 120 and m["tokens"]["completion"] >= 45
    assert m["denied_requests"] >= 1
    assert m["verify_pass_rate"] is not None
    assert "merge_conflict_rate" in m
    assert "stall_count" in m
    assert "steps_per_hour" in m
    assert m["inventory"]["skills_lock_ok"] is True
