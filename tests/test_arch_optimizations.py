"""P0 契约修复与 P1 架构优化的单元测试与 HTTP 集成测试。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lagent import gguf
from lagent import security as sec
from lagent.commands import CommandContext, dispatch_command
from lagent.config import Config
from lagent.inference import TelemetryTracker
from lagent.journal import Journal
from lagent.loop import Agent
from lagent.outputs import OutputStore
from lagent.profile import Profile
from lagent.sessions import Session, SessionStore
from lagent.tools.ctx import SessionState, ToolCtx
from lagent.tools.files import _post_edit_check, commit_bytes
from tests.make_gguf import make as make_gguf


def test_config_set_nested_and_validation(tmp_path: Path):
    cfg = Config(tmp_path / "config.json", tmp_path / "secrets.json")
    cfg.set("agent", "permission_mode", "auto_edit")
    assert cfg.get("agent", "permission_mode") == "auto_edit"
    reloaded = Config(tmp_path / "config.json", tmp_path / "secrets.json")
    assert reloaded.get("agent", "permission_mode") == "auto_edit"
    with pytest.raises(ValueError):
        cfg.set("only_one_arg")


def test_commands_model_and_status_with_profile_object(tmp_path: Path):
    cfg = Config(tmp_path / "config.json", tmp_path / "secrets.json")
    sess = Session("s1", workspace=str(tmp_path))
    eng_loaded = SimpleNamespace(
        status="ready",
        model={"name": "Qwen3-30B-A3B", "file": "qwen3.gguf"},
        ctx=32768,
        ctx_cap=131072,
        vision=True,
        profile=Profile("large", params_b=30.0, active_b=3.0),
        tps=42.5,
    )
    ctx = CommandContext(agent=SimpleNamespace(engine=eng_loaded), session=sess, workspace=str(tmp_path), cfg=cfg)

    r_model = dispatch_command("/model", ctx)
    assert r_model and r_model.handled and not r_model.is_prompt
    assert "Qwen3-30B-A3B" in r_model.text
    assert "large" in r_model.text
    assert "42.5" in r_model.text

    r_status = dispatch_command("/status", ctx)
    assert r_status and "Qwen3-30B-A3B" in r_status.text

    eng_empty = SimpleNamespace(status="stopped", model=None, ctx=0, ctx_cap=0, vision=False, profile=None, tps=0.0)
    r_empty = dispatch_command("/model", CommandContext(agent=SimpleNamespace(engine=eng_empty), session=sess, workspace="", cfg=cfg))
    assert r_empty and "未加载" in r_empty.text


def test_session_seen_persists_across_reload(tmp_path: Path):
    sdir = tmp_path / "sessions"
    store = SessionStore(sdir)
    s = store.create(workspace=str(tmp_path))
    s.seen = {"foo.py": "abc123"}
    store.save(s)

    reloaded = SessionStore(sdir).get(s.id)
    assert reloaded is not None
    assert reloaded.seen == {"foo.py": "abc123"}


def test_verify_subagent_policy_and_tools(tmp_path: Path):
    cfg = Config(tmp_path / "config.json", tmp_path / "secrets.json")
    agent = Agent(
        cfg=cfg,
        engine=SimpleNamespace(tuned=lambda: Profile("standard")),
        store=SessionStore(tmp_path / "sessions"),
        journal=Journal(tmp_path / "j"),
        audit=sec.AuditLog(tmp_path / "audit.jsonl"),
        memory=None,
        skills=None,
        protected=[],
        hidden=[],
        outputs=OutputStore(tmp_path / "out"),
    )
    verify_tools = {t.name for t in agent.tools_for("verify", True)}
    plan_tools = {t.name for t in agent.tools_for("plan", True)}
    assert "read_file" in verify_tools
    assert "run_command" in verify_tools
    assert "run_command" not in plan_tools
    assert "write_file" not in verify_tools
    assert "delete_path" not in verify_tools

    dec_write = sec.Policy.decide(kind="write", name="write_file", mode="verify", tainted=False, allow_keys=set())
    assert dec_write.action == "deny"
    dec_exec = sec.Policy.decide(kind="exec", name="run_command", mode="verify", tainted=False, allow_keys=set(), command="pytest -q")
    assert dec_exec.action != "deny"


def test_truncate_rewinds_only_removed_turns(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = Config(tmp_path / "config.json", tmp_path / "secrets.json")
    journal = Journal(tmp_path / "j")
    store = SessionStore(tmp_path / "sessions")
    agent = Agent(
        cfg=cfg,
        engine=SimpleNamespace(tuned=lambda: Profile("standard")),
        store=store,
        journal=journal,
        audit=sec.AuditLog(tmp_path / "audit.jsonl"),
        memory=None,
        skills=None,
        protected=[],
        hidden=[],
    )
    sess = Session("s_trunc", workspace=str(ws))
    guard = sec.PathGuard(ws)

    # Turn 1 edits f1
    f1 = ws / "a.txt"
    f1.write_text("v0", encoding="utf-8")
    st1 = SessionState(sess.id)
    st1.turn_id = "turn_1"
    ctx1 = ToolCtx(cfg=cfg, guard=guard, journal=journal, session=st1)
    commit_bytes(ctx1, f1, b"v1", "edit", "t1 edit")

    # Turn 2 edits f2
    f2 = ws / "b.txt"
    f2.write_text("b0", encoding="utf-8")
    st2 = SessionState(sess.id)
    st2.turn_id = "turn_2"
    ctx2 = ToolCtx(cfg=cfg, guard=guard, journal=journal, session=st2)
    commit_bytes(ctx2, f2, b"b1", "edit", "t2 edit")

    sess.history = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "a2"},
    ]
    sess.ui = [
        {"id": "u1", "role": "user", "hist_index": 0, "turn_id": "turn_1"},
        {"id": "a1", "role": "assistant", "hist_index": 1},
        {"id": "u2", "role": "user", "hist_index": 2, "turn_id": "turn_2"},
        {"id": "a2", "role": "assistant", "hist_index": 3},
    ]

    agent.truncate(sess, 2)
    assert len(sess.ui) == 2
    assert f2.read_text(encoding="utf-8") == "b0"
    assert f1.read_text(encoding="utf-8") == "v1"


def test_sharded_gguf_tensor_layout(tmp_path: Path):
    s1 = make_gguf(tmp_path / "Model-00001-of-00002.gguf", layers=4, experts=8)
    s2 = make_gguf(tmp_path / "Model-00002-of-00002.gguf", layers=4, experts=8)
    layout = gguf.tensor_layout(s1)
    assert layout is not None
    assert len(layout["expert_per_layer"]) == 4
    single = gguf.tensor_layout(s1.with_name("Single.gguf"))  # nonexistent
    assert single is None

    # Incomplete shard set must safely return None
    s2.unlink()
    assert gguf.tensor_layout(s1) is None


def test_telemetry_ttft_and_spec_auto_disable():
    tt = TelemetryTracker(alpha=0.5)
    for _ in range(3):
        tt.record_timings({"prompt_ms": 120.0, "predicted_per_second": 40.0, "draft_n": 20, "draft_n_accepted": 2})
    summary = tt.benchmark_summary()
    assert summary["ttft_ms"] > 0
    assert tt.should_disable_speculation() is True
    assert summary["spec_recommended"] is False


def test_post_edit_check_reports_syntax_errors(tmp_path: Path):
    py_bad = tmp_path / "bad.py"
    assert "Python 语法错误" in _post_edit_check(py_bad, b"def broken(:\n    pass\n")
    assert _post_edit_check(py_bad, b"def ok():\n    return 1\n") == ""

    js_bad = tmp_path / "bad.json"
    assert "JSON 格式错误" in _post_edit_check(js_bad, b'{"a": }')
    assert _post_edit_check(js_bad, b'{"a": 1}') == ""


def test_server_bearer_tree_recursive_tainted_and_git(server, tmp_path: Path):
    # Authorization: Bearer token support
    s, st = server.req("GET", "/api/state", cookie=False, headers={"Authorization": f"Bearer {server.token}"})
    assert s == 200 and "engine" in st
    s_bad, _ = server.req("GET", "/api/state", cookie=False, headers={"Authorization": "Bearer wrong"})
    assert s_bad == 401

    # Recursive workspace tree skipping .git / node_modules
    ws = tmp_path / "rec_ws"
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "node_modules" / "dep").mkdir(parents=True)
    (ws / "node_modules" / "dep" / "index.js").write_text("", encoding="utf-8")
    assert server.put("/api/workspace", {"path": str(ws)})[0] == 200

    s_tree, tree = server.get("/api/workspace/tree?recursive=1")
    assert s_tree == 200 and tree.get("recursive") is True
    paths = {e["path"] for e in tree["entries"]}
    assert "src/pkg/mod.py" in paths
    assert "node_modules/dep/index.js" not in paths

    # Non-git workspace gracefully returns is_repo=False
    s_git, git_info = server.get("/api/workspace/git")
    assert s_git == 200 and git_info["is_repo"] is False

    # Git repo status + diff when git binary exists
    if shutil.which("git"):
        subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=ws, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, capture_output=True)
        subprocess.run(["git", "add", "src/pkg/mod.py"], cwd=ws, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True)
        (ws / "src" / "pkg" / "mod.py").write_text("x = 2\n", encoding="utf-8")
        s_git2, g2 = server.get("/api/workspace/git?path=src/pkg/mod.py")
        assert s_git2 == 200 and g2["is_repo"] is True
        assert any(f["path"] == "src/pkg/mod.py" for f in g2["files"])
        assert "-x = 1" in g2["diff"] and "+x = 2" in g2["diff"]

    # Tainted state per session_id + PATCH /api/config
    s_c, sess = server.post("/api/sessions", {})
    assert s_c == 200
    assert server.get(f"/api/state?session_id={sess['id']}")[1]["tainted"] is False
    assert server.patch("/api/config", {"agent": {"permission_mode": "auto_edit"}})[0] == 200

    # Session search endpoint: matches content snippet even when title is generic
    assert server.patch(f"/api/sessions/{sess['id']}", {"title": "排查 WebSocket 断连重连策略与 KV 缓存命中"})[0] == 200
    sc, found = server.get("/api/sessions/search?q=断连重连")
    assert sc == 200
    assert any(x["id"] == sess["id"] and "断连重连" in x.get("snippet", "") for x in found)

    # Also test message-body full-text snippet search directly via ASGI app (no extra httpx dependency)
    import asyncio
    from lagent.server import State, make_app
    home2 = tmp_path / "search_home"
    (home2 / "data").mkdir(parents=True)
    (home2 / "models").mkdir()
    (home2 / "bin").mkdir()
    st2 = State(token="tok123", port=8000, root=home2)
    app2 = make_app(st2)
    sr = st2.store.create("")
    sr.ui.append({"id": "u1", "role": "user", "text": "优化上下文 KV 前缀缓存与投机解码命中率", "ts": 1})
    st2.store.save(sr)

    async def asgi_get(path_str: str, query: bytes):
        out_status = 0
        chunks: list[bytes] = []
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path_str, "raw_path": path_str.encode(),
            "query_string": query,
            "headers": [
                (b"host", b"127.0.0.1:8000"),
                (b"authorization", b"Bearer tok123"),
            ],
            "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000),
        }
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(msg):
            nonlocal out_status
            if msg["type"] == "http.response.start":
                out_status = msg["status"]
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
        await app2(scope, receive, send)
        return out_status, json.loads(b"".join(chunks).decode("utf-8"))

    from urllib.parse import quote
    rs_code, hits = asyncio.run(asgi_get("/api/sessions/search", f"q={quote('投机解码命中率')}".encode("ascii")))
    assert rs_code == 200 and len(hits) == 1 and hits[0]["id"] == sr.id and "投机解码命中率" in hits[0]["snippet"]

    # Permissions & hooks with session_id fallback
    pr_c, pr_d = server.get(f"/api/permissions?session_id={sess['id']}")
    hk_c, hk_d = server.get(f"/api/hooks?session_id={sess['id']}")
    assert pr_c == 200 and "rules" in pr_d
    assert hk_c == 200 and "hooks" in hk_d


def test_hw_llama_devices_fallback(monkeypatch, tmp_path):
    from lagent import hw

    fake_bin = tmp_path / ("llama-server.exe" if __import__("os").name == "nt" else "llama-server")
    fake_bin.write_bytes(b"bin")
    monkeypatch.setattr(hw, "_nvidia", lambda: [])
    monkeypatch.setattr(hw, "_amd", lambda: [])
    monkeypatch.setattr(hw.sys, "platform", "win32")
    monkeypatch.setattr(hw, "_run", lambda args, timeout=5.0: (
        "Available devices:\n"
        "  CUDA0: NVIDIA RTX 4090 (24564 MiB, 22000 MiB free)\n"
        "  Vulkan0: Intel Graphics (4096 MiB, 4096 MiB free)\n"
    ) if "--list-devices" in args else "")
    info = hw.detect(force=True, binary=fake_bin)
    assert len(info["gpus"]) == 2
    assert info["gpus"][0]["vendor"] == "nvidia"
    assert info["gpus"][0]["name"].startswith("NVIDIA RTX 4090")
    assert info["gpus"][0]["vram_gb"] == round(24564 / 1024.0, 2)
    assert info["gpus"][1]["vendor"] == "vulkan"


def test_model_router_fallback_and_remote(monkeypatch, tmp_path):
    import asyncio
    from lagent import net
    from lagent.config import Config
    from lagent.router import ModelRouter

    cfg = Config(tmp_path / "cfg.json", tmp_path / "secrets.json")

    class FakeEng:
        def __init__(self):
            self.calls = 0

        async def chat(self, body, timeout=600):
            self.calls += 1
            return {"choices": [{"message": {"content": "local-summary"}}]}

    eng = FakeEng()
    router = ModelRouter(cfg, eng)
    out1 = asyncio.run(router.complete(role="compact", messages=[{"role": "user", "content": "hi"}]))
    assert eng.calls == 1
    assert out1 == "local-summary"

    # Configure compact endpoint + mock net.fetch
    cfg.set("agent", "compact_endpoint", "http://127.0.0.1:11434/v1/chat/completions")
    cfg.set("agent", "compact_model", "qwen2.5:1.5b")

    async def fake_fetch(url, method="GET", headers=None, data=None, timeout=120, max_bytes=2_000_000, allow_private=False):
        assert allow_private is True
        body = json.loads(data.decode("utf-8"))
        assert body["model"] == "qwen2.5:1.5b"
        return net.Response(status=200, url=url, headers={}, body=b'{"choices":[{"message":{"content":"remote-summary"}}]}')

    monkeypatch.setattr("lagent.router.net.fetch", fake_fetch)
    out2 = asyncio.run(router.complete(role="compact", messages=[{"role": "user", "content": "hi"}]))
    assert eng.calls == 1
    assert out2 == "remote-summary"


def test_repomap_and_subdir_projectdoc(tmp_path):
    from lagent import projectdoc
    from lagent.repomap import build_repo_map

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("function leak() {}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "out.py").write_text("def compiled(): pass\n", encoding="utf-8")

    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "core.py").write_text(
        "class EngineCore:\n    def run(self, x):\n        return x\n\ndef helper():\n    pass\n",
        encoding="utf-8",
    )
    (pkg / "ui.ts").write_text("export async function renderView(id: string) {}\n", encoding="utf-8")
    (tmp_path / "src" / "AGENTS.md").write_text("请在 src 目录保持纯函数风格。\n", encoding="utf-8")

    rmap = build_repo_map(tmp_path)
    assert "EngineCore" in rmap and "helper" in rmap and "renderView" in rmap
    assert "node_modules" not in rmap and "compiled" not in rmap

    sdocs = projectdoc.load_for_path(tmp_path, "src/pkg/core.py")
    assert len(sdocs) == 1 and sdocs[0].usable
    assert sdocs[0].name == "src/AGENTS.md"
    assert "纯函数风格" in sdocs[0].text


def test_edit_file_atomic_multi_edit(tmp_path):
    import asyncio
    from lagent.config import Config
    from lagent.journal import Journal
    from lagent.security import AuditLog, PathGuard
    from lagent.tools.base import TOOLS
    from lagent.tools.ctx import SessionState, ToolCtx
    from lagent.tools.files import mark_seen

    f = tmp_path / "sample.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    guard = PathGuard(tmp_path)
    ctx = ToolCtx(
        cfg=Config(tmp_path / "cfg.json", tmp_path / "secrets.json"),
        guard=guard,
        journal=Journal(tmp_path / "j"),
        session=SessionState("s1"),
        audit=AuditLog(tmp_path / "a.jsonl"),
    )
    mark_seen(ctx, f, f.read_bytes())

    # Failing second edit must abort atomically without changing file
    res_fail = asyncio.run(TOOLS["edit_file"].fn(ctx, {
        "path": "sample.py",
        "edits": [
            {"old_string": "a = 1", "new_string": "a = 10"},
            {"old_string": "nonexistent", "new_string": "x = 99"},
        ],
    }))
    assert res_fail.error
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"

    res_ok = asyncio.run(TOOLS["edit_file"].fn(ctx, {
        "path": "sample.py",
        "edits": [
            {"old_string": "a = 1", "new_string": "a = 10"},
            {"old_string": "c = 3", "new_string": "c = 30"},
        ],
    }))
    assert not res_ok.error
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"


def test_managers_reload_if_changed(tmp_path):
    import json
    from lagent.hooks import HookManager
    from lagent.permissions import PermissionManager

    cfg_dir = tmp_path / ".localagent"
    cfg_dir.mkdir()
    s_file = cfg_dir / "settings.json"
    h_file = cfg_dir / "hooks.json"
    s_file.write_text(json.dumps({"permissions": {"allow": ["Read(*.py)"]}}), encoding="utf-8")
    h_file.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "run_command", "command": "echo 1"}]}}), encoding="utf-8")

    pm = PermissionManager(tmp_path)
    hm = HookManager(tmp_path)
    assert pm.reload_if_changed() is False
    assert hm.reload_if_changed() is False

    s_file.write_text(json.dumps({"permissions": {"allow": ["Read(*.py)", "Edit(docs/**)"]}}), encoding="utf-8")
    h_file.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "run_command", "command": "echo 2"}, {"matcher": "edit_file", "command": "echo 3"}]}}), encoding="utf-8")
    assert pm.reload_if_changed() is True
    assert len(pm.rules["allow"]) == 2
    assert hm.reload_if_changed() is True
    assert len(hm.hooks["PreToolUse"]) == 2


def test_engine_slots_spec_disable_and_slot_persistence(monkeypatch, tmp_path):
    import asyncio
    from lagent.config import Config
    from lagent.engine import Engine

    models = tmp_path / "models"
    models.mkdir()
    make_gguf(models / "main.gguf")
    make_gguf(models / "draft.gguf", arch="llama")
    b = tmp_path / ("llama-server.exe" if __import__("os").name == "nt" else "llama-server")
    b.write_bytes(b"")

    cfg = Config(tmp_path / "cfg.json", tmp_path / "secrets.json")
    cfg.set("engine", "model", "main.gguf")
    cfg.set("engine", "slots", 2)
    cfg.set("engine", "reasoning_format", "deepseek")
    cfg.set("engine", "draft_model", "draft.gguf")

    monkeypatch.setattr("lagent.engine.hw.detect", lambda **kw: {
        "os": "linux", "arch": "x86_64", "cpu_cores": 8, "cpu_threads": 16,
        "ram_gb": 32.0, "ram_free_gb": 24.0, "gpus": [], "unified": False,
    })
    eng = Engine(cfg, models, tmp_path / "bin")
    eng.binary = b
    eng._help_text = "--reasoning-format --slot-save-path --model-draft -md --spec-draft-n-max -np --threads --ctx-size -m"

    plan1 = eng.plan()
    args1 = eng.build_args(plan1)
    assert "-np" in args1 and args1[args1.index("-np") + 1] == "2"
    assert "--reasoning-format" in args1 and args1[args1.index("--reasoning-format") + 1] == "deepseek"
    assert "--slot-save-path" in args1
    assert plan1["spec"]["mode"] == "draft"
    assert any("draft.gguf" in str(x) and x != plan1["model_path"] for x in args1)

    # Simulate low speculative acceptance -> auto-disable speculation
    for _ in range(4):
        eng.record_timings({"draft_n": 100, "draft_n_accepted": 5, "predicted_per_second": 20.0})
    assert eng.telemetry.should_disable_speculation() is True
    plan2 = eng.plan()
    assert plan2["spec"]["mode"] == "off"
    assert any("采纳率过低" in n for n in plan2["notes"])

    # Test save_slot & restore_slot
    eng.status = "ready"
    eng.port = 12345
    eng.slots_dir().mkdir(parents=True, exist_ok=True)

    async def fake_local_json(host, port, method, path, body=None, headers=None, timeout=15):
        if "action=save" in path:
            (eng.slots_dir() / body["filename"]).write_bytes(b"slot")
            return 200, {"ok": True}
        if "action=restore" in path:
            return 200, {"ok": True}
        return 404, {}

    monkeypatch.setattr("lagent.engine.net.local_json", fake_local_json)
    assert asyncio.run(eng.save_slot(0, "test.bin")) is True
    assert asyncio.run(eng.restore_slot(0, "test.bin")) is True

