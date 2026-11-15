"""WP1–WP3 全链路能力补充单测与集成测试（零外网依赖）。"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lagent import bootstrap, config, context, hub, inference, launcher, memory, net, repomap, router, security as sec
from lagent.engine import Engine, _SlotScheduler
from lagent.hooks import HookDef, HookManager
from lagent.journal import Journal
from lagent.mcp import convert, session as mcp_session
from lagent.mcp.manager import McpManager
from lagent.sessions import Session, SessionStore
from lagent.tools import agentic, files
from lagent.tools.ctx import SessionState, ToolCtx


def run(coro):
    return asyncio.run(coro)


def _ctx(ws: Path, *, cfg: config.Config | None = None, sess_state: SessionState | None = None, mem: memory.Memory | None = None) -> ToolCtx:
    guard = sec.PathGuard(ws)
    ss = sess_state or SessionState(sid="s-test")
    return ToolCtx(
        cfg=cfg or config.Config(ws / ".cfg.json", ws / ".sec.json"),
        guard=guard,
        journal=Journal(ws / ".la_j"),
        session=ss,
        memory=mem,
    )


# ------------------------------------------------------------------ WP1: Launcher / Config / Net Pool / Router / Inference / Engine / Context
def test_launcher_offline_flag(monkeypatch):
    monkeypatch.delenv("LOCALAGENT_OFFLINE", raising=False)
    ap = launcher.build_parser() if hasattr(launcher, "build_parser") else None
    if ap is not None:
        args = ap.parse_args(["--offline"])
        assert args.offline is True
    # 直接测试 hub 在离线环境变量下的保护
    monkeypatch.setenv("LOCALAGENT_OFFLINE", "1")
    with pytest.raises(PermissionError, match="离线模式"):
        hub.DownloadManager(Path.cwd() / ".tmp_nonexist_check").start_download("https://huggingface.co/a/b.gguf", "b.gguf")


def test_config_secrets_and_new_fields(tmp_path: Path):
    cfg = config.Config(tmp_path / "config.json", tmp_path / "secrets.json")
    pub = cfg.patch({
        "engine": {"tensor_split": "24, 16", "split_mode": "row"},
        "generation": {"reasoning_budget": 1024},
        "agent": {
            "compact_provider": "anthropic",
            "compact_api_key": "sk-ant-secret-1",
            "embed_endpoint": "http://127.0.0.1:8080/v1/embeddings",
            "embed_model": "bge-small",
            "embed_api_key": "sk-emb-secret-2",
        },
    })
    assert pub["engine"]["tensor_split"] == "24,16"
    assert pub["engine"]["split_mode"] == "row"
    assert pub["generation"]["reasoning_budget"] == 1024
    assert pub["agent"]["compact_api_key"] is True
    assert pub["agent"]["embed_api_key"] is True
    assert cfg.secret("compact_api_key") == "sk-ant-secret-1"
    assert cfg.secret("embed_api_key") == "sk-emb-secret-2"
    raw_cfg = json.loads((tmp_path / "config.json").read_text("utf-8"))
    assert "sk-ant-secret-1" not in json.dumps(raw_cfg)
    assert "sk-emb-secret-2" not in json.dumps(raw_cfg)
    with pytest.raises(ValueError):
        cfg.set("engine", "tensor_split", "-1,8")


def test_net_local_keepalive_pool_and_retry():
    calls = 0

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal calls
            calls += 1
            n = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(n)
            body = json.dumps({"n": calls}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st1, d1 = net._local_request("127.0.0.1", port, "POST", "/v1/test", {"a": 1}, {}, 5.0)
        st2, d2 = net._local_request("127.0.0.1", port, "POST", "/v1/test", {"a": 2}, {}, 5.0)
        assert st1 == 200 and st2 == 200 and d1["n"] == 1 and d2["n"] == 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_router_anthropic_adapter_and_embed():
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["x_api_key"] = self.headers.get("x-api-key")
            seen["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            if self.path.endswith("/messages"):
                out = json.dumps({"content": [{"type": "text", "text": "摘要结果"}]}).encode("utf-8")
            else:
                out = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tmp_cfg = config.Config(Path(os.path.abspath(".tmp_r_cfg.json")), Path(os.path.abspath(".tmp_r_sec.json")))
        tmp_cfg.patch({
            "agent": {
                "compact_provider": "anthropic",
                "compact_endpoint": f"http://127.0.0.1:{port}/v1/messages",
                "compact_model": "claude-haiku-test",
                "compact_api_key": "ant-key-123",
                "embed_endpoint": f"http://127.0.0.1:{port}/v1/embeddings",
                "embed_model": "emb-test",
                "embed_api_key": "emb-key-456",
            }
        })
        eng = MagicMock()
        r = router.ModelRouter(tmp_cfg, eng)
        txt = run(r.complete(role="compact", messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]))
        assert txt == "摘要结果"
        assert seen["x_api_key"] == "ant-key-123"
        assert seen["body"]["system"] == "sys"
        vecs = run(r.embed(["hello"]))
        assert vecs == [[0.1, 0.2, 0.3]]
    finally:
        srv.shutdown()
        srv.server_close()
        for fn in (".tmp_r_cfg.json", ".tmp_r_sec.json"):
            Path(fn).unlink(missing_ok=True)


def test_inference_multi_gpu_gbnf_and_slot_scheduler(tmp_path: Path):
    advisor = inference.InferenceAdvisor(
        {"gpus": [{"vram_gb": 24.0, "free_gb": 24.0}, {"vram_gb": 12.0, "free_gb": 12.0}]},
        {"size_bytes": 1000},
        engine_cfg={"tensor_split": "auto", "split_mode": "layer"},
    )
    adv = advisor.advise_multi_gpu()
    assert adv["tensor_split"] == "67,33" and adv["split_mode"] == "layer"

    gbnf = inference.schema_to_gbnf({
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ok", "fail"]}, "count": {"type": "integer"}},
        "required": ["status", "count"],
    })
    assert "root ::=" in gbnf and '\\"ok\\"' in gbnf and '\\"fail\\"' in gbnf

    async def _check_sched():
        sched = _SlotScheduler(slots=2)
        order = []

        async def bg():
            async with sched.acquire(priority=1):
                order.append("bg-start")
                await asyncio.sleep(0.04)
                order.append("bg-end")

        async def fg():
            await asyncio.sleep(0.01)
            async with sched.acquire(priority=0):
                order.append("fg")

        await asyncio.gather(bg(), fg())
        assert "fg" in order and "bg-end" in order

    run(_check_sched())

    cfg = config.Config(tmp_path / "c.json", tmp_path / "s.json")
    cfg.patch({"engine": {"tensor_split": "16,8", "split_mode": "row"}, "generation": {"reasoning_budget": 512}})
    eng = Engine(cfg, tmp_path / "m", tmp_path / "b")
    eng.binary = tmp_path / "b" / "llama-server"
    eng.has = lambda _flag: True
    eng.gpus = [{"vram_gb": 16}, {"vram_gb": 8}]
    eng_adv = inference.InferenceAdvisor(
        {"gpus": [{"vram_gb": 16.0, "free_gb": 16.0}, {"vram_gb": 8.0, "free_gb": 8.0}]},
        {"size_bytes": 1000},
        engine_cfg={"tensor_split": "16,8", "split_mode": "row"},
    ).advise_multi_gpu()
    plan = {
        "model_path": str(tmp_path / "m" / "x.gguf"), "ctx": 8192, "ngl": 32, "moe": False,
        "threads": 4, "threads_batch": 8, "batch": 512, "ubatch": 256,
        "flash_attn": "on", "kv_type": "q8_0", "slots": 2, "spec": {}, "multi_gpu": eng_adv,
    }
    eng.port, eng.api_key = 9999, "tok"
    argv = eng.build_args(plan)
    assert "--tensor-split" in argv and "16,8" in argv
    assert "--split-mode" in argv and "row" in argv
    assert "--reasoning-budget" in argv and "512" in argv


def test_append_only_kv_prefix_and_lazy_session_index(tmp_path: Path):
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "L" * 5000},
        {"role": "user", "content": "u2"},
    ]
    p1 = context.project_api_messages(msgs, prune_upto=0)
    assert len(p1[3]["content"]) == 5000
    wm, saved = context.advance_prune_watermark(msgs, prev_upto=0, keep_recent_tools=0)
    assert wm > 0 and saved > 0
    # 原始消息列表必须完全不可变（append-only）
    assert len(msgs[3]["content"]) == 5000
    p2 = context.project_api_messages(msgs, prune_upto=wm)
    assert len(p2[3]["content"]) < 500 and "已省略" in p2[3]["content"]

    store = SessionStore(tmp_path / "sessions")
    s1 = store.create(str(tmp_path))
    s1.title = "First"
    s1.permission_mode = "auto_edit"
    store.save(s1)
    store2 = SessionStore(tmp_path / "sessions")
    assert (tmp_path / "sessions" / "sessions_index.json").is_file()
    items = store2.list()
    assert len(items) == 1 and items[0]["title"] == "First"
    assert s1.id not in store2._cache  # 懒加载尚未反序列化完整消息体
    loaded = store2.get(s1.id)
    assert loaded is not None and loaded.permission_mode == "auto_edit"


# ------------------------------------------------------------------ WP2: RepoMap / Hash Edits / Diagnostics / Shell / Memory / Journal / Worker / ROCm / Hub
def test_anchored_read_hash_edit_diagnostics_and_stateful_shell(tmp_path: Path):
    f = tmp_path / "sample.py"
    f.write_text("alpha = 1\nbeta = 2\n", "utf-8")
    ss = SessionState(sid="s-test")
    ctx = _ctx(tmp_path, sess_state=ss)

    r = run(files.read_file(ctx, {"path": "sample.py", "anchors": True}))
    assert not r.error
    d_beta = files.line_digest("beta = 2")
    assert f"#{d_beta}\tbeta = 2" in r.content

    # 错误的 line_hash 应触发乐观校验拒绝
    bad = run(files.edit_file(ctx, {"path": "sample.py", "start_line": 2, "line_hash": "deadbeef",
                                    "old_string": "beta = 2", "new_string": "beta = 20"}))
    assert bad.error and "哈希" in bad.content

    # 正确的 start_line + line_hash 锚定修改
    ok = run(files.edit_file(ctx, {"path": "sample.py", "start_line": 2, "line_hash": d_beta,
                                   "old_string": "beta = 2", "new_string": "beta = 20"}))
    assert not ok.error and "beta = 20" in f.read_text("utf-8")

    # 语法/代码静态诊断
    bad_py = tmp_path / "broken.py"
    bad_py.write_text("def oops(:\n  pass\n", "utf-8")
    diag = run(files.code_diagnostics(ctx, {"path": "broken.py"}))
    assert not diag.error and "SyntaxError" in diag.content

    # 有状态工作目录与环境变量保持（过滤 LD_ 危险变量）
    sub = tmp_path / "sub"
    sub.mkdir()
    if os.name == "nt":
        cmd1 = 'cd sub; $env:LA_TEST_ENV="hello_la"; $env:LD_PRELOAD="evil"'
        cmd2 = '(Get-Location).Path; $env:LA_TEST_ENV'
    else:
        cmd1 = "cd sub && export LA_TEST_ENV=hello_la && export LD_PRELOAD=/tmp/evil.so"
        cmd2 = "pwd && echo $LA_TEST_ENV"
    res1 = run(files.run_command(ctx, {"command": cmd1}))
    assert not res1.error and ss.shell_cwd == "sub"
    assert (tmp_path / ss.shell_cwd).resolve() == sub.resolve()
    assert ss.shell_env.get("LA_TEST_ENV") == "hello_la"
    assert "LD_PRELOAD" not in ss.shell_env
    res2 = run(files.run_command(ctx, {"command": cmd2}))
    assert not res2.error and "hello_la" in res2.content


def test_memory_hybrid_vector_rrf_and_journal_checkpoints(tmp_path: Path):
    mem = memory.Memory(tmp_path / "mem.db")
    v1 = memory.local_embed("FastAPI server authentication middleware")
    v2 = memory.local_embed("Banana bread baking oven temperature")
    e1 = mem.add("FastAPI uses Starlette Guard token checks", kind="fact", source="user")
    e2 = mem.add("Bake banana bread at 180C for 45 minutes", kind="fact", source="user")
    mem.set_vector(e1["id"], v1, model="local-hash-v1")
    mem.set_vector(e2["id"], v2, model="local-hash-v1")
    hits = mem.search("Starlette Guard token", k=2, query_vec=v1)
    assert hits and hits[0]["id"] == e1["id"]

    jdir = tmp_path / "j"
    j = Journal(jdir)
    tfile = tmp_path / "a.txt"
    tfile.write_text("v1", "utf-8")
    snap = j.snapshot(tfile)
    j.record("s1", op="edit", path=tfile, summary="edit a", snap=snap, extra={"turn_id": 1})
    j.record("s1", op="write", path=tmp_path / "b.txt", summary="write b", snap=None, existed=False, extra={"turn_id": 2})
    cps = j.checkpoints("s1", tmp_path)
    assert len(cps) == 2 and cps[0]["turn_id"] == "2" and cps[1]["turn_id"] == "1"


def test_worker_git_worktree_execution(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local.dev"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=ws, check=True, capture_output=True)
    (ws / "hello.txt").write_text("base\n", "utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    # 通过真实 Journal + Agent._apply_worktree 验证 worktree 生成与安全回写工作区
    from lagent.loop import Agent
    from lagent.outputs import OutputStore
    from lagent.skills import Skills
    cfg = config.Config(tmp_path / "c.json", tmp_path / "s.json")
    ag = Agent(cfg=cfg, engine=MagicMock(), store=SessionStore(tmp_path / "sess"), journal=Journal(tmp_path / "data"),
               audit=sec.AuditLog(tmp_path / "audit.jsonl"), memory=memory.Memory(tmp_path / "m.db"),
               skills=Skills(tmp_path / "sk", tmp_path / "sk_b", tmp_path / "sk.json"),
               protected=[], hidden=[], notify=lambda *a: None, outputs=OutputStore(tmp_path / "out"))

    wt = tmp_path / "wt1"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt)], cwd=ws, check=True, capture_output=True)
    try:
        (wt / "hello.txt").write_text("modified-in-wt\n", "utf-8")
        (wt / "new_file.txt").write_text("created-in-wt\n", "utf-8")
        changed, _diff = ag._apply_worktree(wt, ws, "s-wt", "3")
        assert any("new_file.txt" in c for c in changed) and any("hello.txt" in c for c in changed)
        assert (ws / "hello.txt").read_text("utf-8") == "modified-in-wt\n"
        assert (ws / "new_file.txt").read_text("utf-8") == "created-in-wt\n"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ws, capture_output=True)


def test_bootstrap_rocm_and_hub_resumable_download(tmp_path: Path, monkeypatch):
    # ROCm asset selection on Linux AMD
    rel = {
        "tag_name": "b5000",
        "assets": [
            {"name": "llama-b5000-bin-ubuntu-x64.tar.gz", "browser_download_url": "https://x/cpu.tar.gz", "size": 100},
            {"name": "llama-b5000-bin-ubuntu-rocm-6.2-x64.tar.gz", "browser_download_url": "https://x/rocm.tar.gz", "size": 200},
        ],
    }
    picked = bootstrap.select_release_assets([rel], {"os": "linux", "arch": "x64", "has_nvidia": False, "has_amd": True, "has_vulkan": False})
    assert picked and picked[1][0]["name"].endswith("rocm-6.2-x64.tar.gz")

    # Hub recommendations
    rec = hub.recommend_models({"gpus": [{"vram_gb": 16.0}], "ram_gb": 32.0}, tmp_path / "models")
    assert rec["models"] and rec["models"][0]["fit"] == "best"
    assert hub.rewrite_mirror("https://huggingface.co/a/b.gguf", "hf-mirror") == "https://hf-mirror.com/a/b.gguf"

    # Range resumable download + SHA-256 verification
    payload = b"GGUF" + b"0123456789ABCDEF" * 64
    expected_sha = hashlib.sha256(payload).hexdigest()
    range_seen = {}

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            rng = self.headers.get("Range", "")
            range_seen["hdr"] = rng
            start = 0
            if rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0] or 0)
            part = payload[start:]
            self.send_response(206 if start > 0 else 200)
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            self.wfile.write(part)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LOCALAGENT_OFFLINE", "0")
    monkeypatch.setattr(net, "check_hostname_literal", lambda *a: None)
    monkeypatch.setattr(net, "resolve_public", lambda *a: ["127.0.0.1"])
    try:
        mdir = tmp_path / "models"
        mdir.mkdir()
        (mdir / "test-model.gguf.part").write_bytes(payload[:128])
        dm = hub.DownloadManager(mdir)
        job = dm.start_download(f"http://127.0.0.1:{port}/test-model.gguf", "test-model.gguf", expected_sha256=expected_sha)
        for _ in range(50):
            st = dm.status()[0]
            if st["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        st = dm.status()[0]
        assert st["status"] == "done" and st["id"] == job["id"]
        assert range_seen["hdr"] == "bytes=128-"
        assert (mdir / "test-model.gguf").read_bytes() == payload
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ WP3: Sandbox Hardening / Hooks / Full MCP Stack
def test_sandbox_darwin_whitelist_and_windows_job(tmp_path: Path, monkeypatch):
    # Test macOS seatbelt profile generation removes wildcard read
    monkeypatch.setattr(sec, "_probe_bwrap", lambda: False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/sandbox-exec" if c == "sandbox-exec" else None)
    argv, kind = sec.wrap_in_sandbox(["echo", "hi"], tmp_path, allow_net=False, home=tmp_path / "h")
    assert kind == "sandbox-exec"
    prof = argv[2]
    assert "(allow file-read* (literal" in prof
    assert "(allow file-read*)" not in prof
    assert "(deny network*)" in prof

    if sec.IS_WIN:
        p = subprocess.Popen([sys.executable, "-c", "print('ok')"], stdout=subprocess.PIPE)
        assert sec.harden_windows_process(p.pid) is True
        p.communicate(timeout=5)
        sec.release_windows_job(p.pid)


def test_hooks_tainted_readonly_runs_safely(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    hm = HookManager(tmp_path)
    hm.hooks["PreToolUse"] = [
        HookDef(event="PreToolUse", matcher="*", command="python -c \"import sys; sys.exit(2)\"", read_only=False),
        HookDef(event="PreToolUse", matcher="*", command="git status", read_only=True),
    ]
    ctx = MagicMock()
    ctx.session.tainted = True
    # Only the read_only verified hook runs; exit 0 allows, non-read_only is skipped
    res = run(hm.run_pre_tool_use("read_file", {"path": "x"}, ctx))
    assert res.action == "allow" and res.skipped is False


def test_mcp_schema_union_ref_and_http_sse_resources_prompts(tmp_path: Path):
    schema = {
        "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
        "properties": {
            "target": {"anyOf": [{"$ref": "#/$defs/Item"}, {"type": "string"}]},
            "opt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["target"],
    }
    out = convert.convert_schema(schema)
    assert "anyOf" in out["properties"]["target"]
    assert out["properties"]["opt"]["type"] == "string"

    class McpHttpHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            rid, meth = req.get("id"), req.get("method")
            if meth == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if meth == "initialize":
                res = {"protocolVersion": mcp_session.PROTOCOL, "capabilities": {"resources": {}, "prompts": {}}, "serverInfo": {"name": "http-mock"}}
            elif meth == "tools/list":
                res = {"tools": [{"name": "ping", "description": "pong", "inputSchema": {"type": "object", "properties": {}}}]}
            elif meth == "resources/list":
                res = {"resources": [{"uri": "mem://doc1", "name": "Doc1", "description": "demo", "mimeType": "text/plain"}]}
            elif meth == "resources/read":
                res = {"contents": [{"uri": req["params"]["uri"], "text": "resource-body"}]}
            elif meth == "prompts/list":
                res = {"prompts": [{"name": "summarize", "description": "sum prompt", "arguments": [{"name": "topic", "required": True}]}]}
            elif meth == "prompts/get":
                res = {"messages": [{"role": "user", "content": {"type": "text", "text": "Hello prompt"}}]}
            elif meth == "tools/call":
                # Test SSE response format too
                sse = f"data: {json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': 'sse-ok'}]}})}\n\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", "sess-http-1")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
                return
            else:
                res = {}
            raw = json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-http-1")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), McpHttpHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mgr = McpManager(tmp_path / "mcp_data", None)
        v = run(mgr.update("remote", {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"Authorization": "Bearer secret-tok"}}))
        assert v["transport"] == "http" and "Authorization" in v["header_keys"]
        assert mgr.contained("remote") is False  # HTTP 服务器不视为本地隔离沙箱
        v2 = run(mgr.start_user("remote"))
        assert v2["status"] == "running"
        assert len(v2["resources"]) == 1 and v2["resources"][0]["uri"] == "mem://doc1"
        assert len(v2["prompts"]) == 1 and v2["prompts"][0]["name"] == "summarize"
        r_call = run(mcp_session.HttpMcpSession.call(mgr.sessions["remote"], "ping", {}, 10.0))
        assert r_call["content"][0]["text"] == "sse-ok"
        res_body = run(mgr.read_resource("remote", "mem://doc1"))
        assert res_body["contents"][0]["text"] == "resource-body"
        prm_body = run(mgr.get_prompt("remote", "summarize", {"topic": "ai"}))
        assert prm_body["messages"][0]["content"]["text"] == "Hello prompt"
        run(mgr.stop_all())
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_new_wp3_routes(tmp_path: Path, monkeypatch):
    import http.client
    import socket
    import uvicorn
    from lagent import appwindow, server

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    home = tmp_path / "la_home"
    (home / "ws").mkdir(parents=True)
    st = server.State("tok123", port, root=home)
    app = server.make_app(st)
    hdr = {"Authorization": "Bearer tok123", "Host": f"127.0.0.1:{port}"}
    monkeypatch.setattr(appwindow, "pick_folder", lambda init="": str(home / "ws"))

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = dict(hdr)
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c.request(method, path, body=raw, headers=h)
        resp = c.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        c.close()
        return resp.status, data

    try:
        for _ in range(50):
            if srv.started:
                break
            time.sleep(0.05)
        assert srv.started

        rc, r_pick = call("POST", "/api/fs/pick", {})
        assert rc == 200 and r_pick["path"] == str(home / "ws")

        rc, s = call("POST", "/api/sessions", {"workspace": str(home / "ws")})
        assert rc == 200
        sid = s["id"]

        rc, r_patch = call("PATCH", f"/api/sessions/{sid}", {"permission_mode": "auto_edit"})
        assert rc == 200 and r_patch["permission_mode"] == "auto_edit"

        rc, r_state = call("GET", f"/api/state?session_id={sid}")
        assert rc == 200 and r_state["permission_mode"] == "auto_edit"
        assert r_state["global_permission_mode"] == "ask"

        rc, r_cp = call("GET", f"/api/sessions/{sid}/checkpoints")
        assert rc == 200 and r_cp == []

        rc, r_hub = call("GET", "/api/hub/recommend")
        assert rc == 200 and len(r_hub["models"]) >= 3
    finally:
        srv.should_exit = True
        th.join(timeout=5)
