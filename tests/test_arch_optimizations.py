"""P0 契约修复与 P1 架构优化的单元测试与 HTTP 集成测试。"""
from __future__ import annotations

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
