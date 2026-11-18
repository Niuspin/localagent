"""全链路架构优化（P0/P1/P2）回归与集成测试套件。"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lagent import memory as mem_mod  # noqa: E402
from lagent import permissions as perm_mod  # noqa: E402
from lagent import repomap as rm_mod  # noqa: E402
from lagent import security as sec  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.engine import Engine  # noqa: E402
from lagent.hooks import HookDef, HookManager  # noqa: E402
from lagent.inference import schema_to_gbnf  # noqa: E402
from lagent.journal import Journal  # noqa: E402
from lagent.loop import Agent, Run, ThinkSplitter  # noqa: E402
from lagent.mission import AcceptanceCheck, Milestone, MissionManager, MissionPolicy, MissionStore, shadow_git  # noqa: E402
from lagent.providers import UnifiedProvider  # noqa: E402
from lagent.research.pipeline import ResearchManager  # noqa: E402
from lagent.runtime.cli import run_acp_stdio, run_oneshot_prompt  # noqa: E402
from lagent.server import State, make_app  # noqa: E402
from lagent.sessions import Session, SessionStore  # noqa: E402
from lagent.swarm import SwarmManager, SwarmStore  # noqa: E402
from lagent.swarm.locks import FileLeaseManager  # noqa: E402
from lagent.swarm.merge import MergeQueue  # noqa: E402
from lagent.tools import files as files_tool  # noqa: E402
from lagent.tools.ctx import SessionState, ToolCtx  # noqa: E402


def _git(ws: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True, encoding="utf-8", check=True)
    return cp.stdout.strip()


def _init_git(ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init")
    _git(ws, "config", "user.name", "Tester")
    _git(ws, "config", "user.email", "test@localagent.local")
    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")
    return ws


async def _call_asgi(app, method: str, path: str, *, port: int = 8765, token: str = "tok-opt", body: bytes = b"") -> tuple[int, bytes]:
    hdrs = [(b"host", f"127.0.0.1:{port}".encode()), (b"authorization", f"Bearer {token}".encode())]
    if body:
        hdrs += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": hdrs, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", port),
    }
    sent: list[dict] = []
    more = True

    async def recv():
        nonlocal more
        if more:
            more = False
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    await app(scope, recv, send)
    st = next(m for m in sent if m["type"] == "http.response.start")["status"]
    pl = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(st), pl


# ===================================================================== P0: Security
def test_p0_command_chaining_and_mode_invariants(tmp_path):
    # 1. Compound shell injection MUST NOT match narrow allowlist patterns
    assert perm_mod.match_command("pytest -q", "pytest*") is True
    assert perm_mod.match_command("pytest -q && pytest tests/unit", "pytest*") is True
    assert perm_mod.match_command("pytest && rm -rf /", "pytest*") is False
    assert perm_mod.match_command("pytest; curl https://evil.example", "pytest*") is False
    assert perm_mod.match_command("pytest $(whoami)", "pytest*") is False
    assert perm_mod.match_command("pytest > leak.py", "pytest*") is False

    # 2. Multi-pattern MissionPolicy allows valid compound chains ONLY if every segment matches
    ws = tmp_path / "ws"
    ws.mkdir()
    mp = MissionPolicy({"mode": "unattended", "allow_commands": ["pytest*", "git status*"]}, sec.PathGuard(ws), workspace=ws)
    assert mp.evaluate("run_command", {"command": "git status && pytest -q"}).allowed is True
    assert mp.evaluate("run_command", {"command": "git status && python bad.py"}).action == "block"

    # 3. PermissionManager cannot bypass read-only plan or verify mode
    pm = perm_mod.PermissionManager(rules={"allow": ["Write(*)", "Bash(*)"]})
    dec_plan = pm.evaluate(kind="write", name="write_file", args={"path": "a.py"}, tainted=False, mode="plan")
    assert dec_plan is not None and dec_plan.action == "deny"
    dec_plan_cmd = pm.evaluate(kind="exec", name="run_command", args={"command": "echo ok"}, command="echo ok", tainted=False, mode="plan")
    assert dec_plan_cmd is not None and dec_plan_cmd.action == "deny"
    dec_verify = pm.evaluate(kind="write", name="write_file", args={"path": "a.py"}, tainted=False, mode="verify")
    assert dec_verify is not None and dec_verify.action == "deny"

    # 4. Delegated write kind detection for spawn_agent / swarm_plan
    assert perm_mod.infer_delegated_kind("spawn_agent", {"role": "coder"}, "meta") == "write"
    assert perm_mod.infer_delegated_kind("spawn_agent", {"role": "researcher"}, "meta") == "meta"
    assert perm_mod.infer_delegated_kind("swarm_plan", {"nodes": [{"role": "coder", "goal": "x"}]}, "meta") == "write"


def test_p0_mission_use_swarm_enforces_mission_policy(tmp_path):
    ws = _init_git(tmp_path / "swm_repo")
    (ws / "src").mkdir()
    st = State(token="tok-swp", port=8768, root=tmp_path / "srv")
    try:
        async def fake_ensure_ready():
            st.engine.status = "ready"

        async def fake_chat_stream(body):
            msgs = body.get("messages") or []
            tmsgs = [m for m in msgs if m.get("role") == "tool"]
            if not tmsgs:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "tc_block_sw",
                                "function": {"name": "write_file", "arguments": json.dumps({"path": "deploy/secret.yml", "content": "x"})},
                            }]
                        },
                        "finish_reason": None,
                    }]
                }
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 12}}
            else:
                yield {"choices": [{"delta": {"content": "blocked"}, "finish_reason": "stop"}], "usage": {"total_tokens": 8}}

        st.engine.status = "ready"
        st.engine.ctx = 4096
        st.engine.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
        st.engine.chat_stream = fake_chat_stream  # type: ignore[method-assign]

        async def run_flow():
            m = st.missions.create_mission(
                title="Swarm Policy",
                goal="Test Mission Swarm boundary",
                workspace=str(ws),
                mode="unattended",
                use_swarm=True,
                policy={"mode": "unattended", "allow_paths": ["src/**"]},
                milestones=[Milestone(id="ms1", title="Swarm Write", swarm_spec={"roles": ["coder"]})],
            )
            return await st.missions.run_to_completion(m.id)

        done_m = asyncio.run(run_flow())
        assert done_m.status == "blocked"
        assert done_m.pending_approvals()
        assert not (ws / "deploy" / "secret.yml").exists()
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ===================================================================== P0: Swarm & MergeQueue
def test_p0_swarm_node_approval_bridge_and_merge_resolve(tmp_path):
    ws = _init_git(tmp_path / "sw_repo")
    (ws / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "add app.py")

    # 1. MergeQueue.resolve_conflict("resolved_theirs") actually applies theirs with Journal
    (ws / "app.py").write_text("line1\nours2\nline3\n", encoding="utf-8")
    j = Journal(tmp_path / "journal")
    mq = MergeQueue(j)
    res = asyncio.run(mq.enqueue_changes(
        workspace=ws,
        file_ops={
            "app.py": (b"line1\nline2\nline3\n", b"line1\nours2\nline3\n", b"line1\ntheirs2\nline3\n"),
            "extra.txt": (None, None, b"non-conflicting\n"),
        },
        agent_id="coder_1",
        session_id="sess1",
    ))
    assert not res.ok and res.conflict is not None
    card = mq.resolve_conflict(res.conflict.id, "resolved_theirs", workspace=ws, session_id="sess1")
    assert card is not None and card.status == "resolved_theirs"
    assert (ws / "app.py").read_text("utf-8") == "line1\ntheirs2\nline3\n"
    assert (ws / "extra.txt").read_text("utf-8") == "non-conflicting\n"

    # 2. Swarm node interactive ask approval does not deadlock
    st = State(token="tok-swask", port=8769, root=tmp_path / "srv_ask")
    try:
        seen_ap: list[dict] = []
        st.agent.notify = lambda ev, d: seen_ap.append(d) if ev == "approval_request" else None

        async def fake_ensure_ready():
            st.engine.status = "ready"

        async def fake_chat_stream(body):
            msgs = body.get("messages") or []
            tmsgs = [m for m in msgs if m.get("role") == "tool"]
            if not tmsgs:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "tc_ask1",
                                "function": {"name": "write_file", "arguments": json.dumps({"path": "out.txt", "content": "hi"})},
                            }]
                        },
                        "finish_reason": None,
                    }]
                }
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
            else:
                yield {"choices": [{"delta": {"content": "wrote out.txt"}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

        st.engine.status = "ready"
        st.engine.ctx = 4096
        st.engine.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
        st.engine.chat_stream = fake_chat_stream  # type: ignore[method-assign]

        async def run_sw_ask():
            sw = st.swarms.create_swarm(
                session_id="s_main",
                goal="test ask bridge",
                nodes=[{"id": "n1", "role": "coder", "goal": "write out.txt", "apply": False}],
                workspace=str(ws),
                parent_mode="ask",
                explicit_auto_worker=False,
            )
            for _ in range(100):
                if seen_ap:
                    st.agent.answer_approval(seen_ap[0]["approval_id"], "allow")
                    break
                await asyncio.sleep(0.02)
            res_w = await st.swarms.await_agents(sw.id, timeout_s=5.0)
            return res_w

        out = asyncio.run(run_sw_ask())
        assert seen_ap, "Swarm node should emit approval_request without hanging"
        assert out["agents"]["n1"]["status"] == "done"
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ===================================================================== P0: KV Cache, GBNF, ThinkSplitter & Research
def test_p0_kv_prefix_slots_think_gbnf_and_research(monkeypatch, tmp_path):
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    eng = Engine(cfg, tmp_path / "models", tmp_path / "bin")
    eng.binary = tmp_path / "llama-server"
    eng._help_text = "--reasoning-format --threads --ctx-size -np -m"
    eng.plan_info = {"slots": 4, "model": "gemma-4-27b.gguf"}
    assert eng.slots_count() == 4
    assert eng.slot_for_session("main_sess", is_worker=False) == 0
    assert eng.slot_for_session("worker_a", is_worker=True) in (1, 2, 3)

    # Non-thinking Gemma model under auto shouldn't force deepseek format; Qwen3 should
    args_gemma = eng.build_args({"model": "gemma-4-27b.gguf", "model_path": str(tmp_path / "m.gguf"), "ctx": 4096, "threads": 4, "batch": 512, "ubatch": 512, "slots": 1, "ngl": 0, "moe": False, "n_cpu_moe": 0, "flash_attn": "off", "kv_type": "f16", "mmproj_offload": True, "spec": {"mode": "off", "flags": []}})
    assert "--reasoning-format" not in args_gemma
    args_qwen = eng.build_args({"model": "qwen3-coder.gguf", "model_path": str(tmp_path / "q.gguf"), "ctx": 4096, "threads": 4, "batch": 512, "ubatch": 512, "slots": 1, "ngl": 0, "moe": False, "n_cpu_moe": 0, "flash_attn": "off", "kv_type": "f16", "mmproj_offload": True, "spec": {"mode": "off", "flags": []}})
    assert "--reasoning-format" in args_qwen and args_qwen[args_qwen.index("--reasoning-format") + 1] == "deepseek"

    # ThinkSplitter supports both <think> and <thought>
    ts = ThinkSplitter()
    segs = ts.feed("pre<thought>inner reasoning</thought>final") + ts.flush()
    assert ("think", "inner reasoning") in segs
    assert ("text", "pre") in segs and ("text", "final") in segs

    # GBNF with optional field has no invalid trailing comma
    g = schema_to_gbnf({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]})
    assert "kv-" in g

    # System prompt prefix stays stable across dates; date goes to user prompt
    st = State(token="tok-kv", port=8770, root=tmp_path / "srv_kv")
    try:
        sess = st.store.create(str(tmp_path))
        p1 = st.agent.system_prompt(sess, "ask", str(tmp_path))
        assert dt.date.today().isoformat() not in p1

        # Research _synthesize_report triggers LLM + cite_check reject-and-rewrite loop
        calls: list[str] = []

        class FakeRouter:
            class _FakeProv:
                def resolve(self, _r):
                    return SimpleNamespace(kind="local")

            provider = _FakeProv()
            engine = SimpleNamespace(status="ready")

            async def complete(self, *, role, messages, **kw):
                calls.append(messages[-1]["content"])
                if len(calls) == 1:
                    # First draft makes unsupported citation [99] -> should be rejected by cite_check!
                    return "# 调研报告：Q\n\n## 核心发现与证据\n- Bogus claim [99]\n\n## 参考文献\n[1] Example"
                return "# 调研报告：Q\n\n## 核心发现与证据\n- Python supports asyncio cleanly [1]\n\n## 参考文献\n[1] Example"

        rm = ResearchManager(cfg=cfg, data_dir=tmp_path / "res_d", router=FakeRouter())
        try:
            rep, chk = asyncio.run(rm._synthesize_report(
                "Q", ["sub1"],
                sources=[{"id": "s1", "ref_num": 1, "url": "https://example.com", "domain": "example.com", "title": "Example", "fetched_at": 1000.0, "robots_ok": True, "excerpt": "Python supports asyncio"}],
                claims=[{"source_id": "s1", "claim_text": "Python supports asyncio cleanly", "quote": "Python supports asyncio"}],
                blocked={},
                archived_texts={"s1": "Python supports asyncio cleanly in stdlib."},
            ))
            assert len(calls) >= 2
            assert "[99]" not in rep and "[1]" in rep and chk["ok"] is True
        finally:
            rm.close()
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ===================================================================== P1: Providers, Shadow Git, RepoMap, Memory, Shell
def test_p1_providers_shadow_git_repomap_memory_and_shell(tmp_path):
    # 1. Shadow Git on non-git folder preserves user untracked files and supports rollback
    ws_ng = tmp_path / "nongit_ws"
    ws_ng.mkdir()
    (ws_ng / "user_existing.txt").write_text("keep me\n", encoding="utf-8")
    sh_dir = ws_ng / ".localagent" / "missions" / "shadow.git"
    s1 = shadow_git.snapshot(ws_ng, sh_dir, "step 1")
    assert s1 is not None
    (ws_ng / "m2_added.txt").write_text("new file\n", encoding="utf-8")
    (ws_ng / "user_existing.txt").write_text("modified temporarily\n", encoding="utf-8")
    s2 = shadow_git.snapshot(ws_ng, sh_dir, "step 2")
    assert s2 is not None and s2 != s1
    assert shadow_git.rollback(ws_ng, sh_dir, s1) is True
    assert (ws_ng / "user_existing.txt").read_text("utf-8") == "keep me\n"
    assert not (ws_ng / "m2_added.txt").exists()

    # 2. UnifiedProvider resolves main/worker endpoints cleanly & translates Anthropic payload
    cfg = Config(tmp_path / "c2.json", tmp_path / "s2.json")
    cfg.set("agent", "main_endpoint", "https://api.openai.com/v1")
    cfg.set("swarm", "worker_endpoint", "http://127.0.0.1:9999")
    prov = UnifiedProvider(cfg, SimpleNamespace(slots_count=lambda: 2))
    assert prov.resolve("main").kind == "openai"
    assert prov.resolve("coder").url.endswith("/v1/chat/completions")
    prep = prov._prepare_body(prov.resolve("main"), {"id_slot": 1, "cache_prompt": True, "messages": [{"role": "user", "content": "hi"}]}, stream=True)
    assert "id_slot" not in prep and "cache_prompt" not in prep

    # 3. RepoMap reference graph + PageRank places heavily-referenced module before unreferenced one
    rm_ws = tmp_path / "rm_ws"
    rm_ws.mkdir()
    (rm_ws / "zzz_core.py").write_text("def core_helper():\n    return 1\n", encoding="utf-8")
    (rm_ws / "a_user.py").write_text("from zzz_core import core_helper\ndef f1():\n    return core_helper()\n", encoding="utf-8")
    (rm_ws / "b_user.py").write_text("from zzz_core import core_helper\ndef f2():\n    return core_helper()\n", encoding="utf-8")
    (rm_ws / "00_leaf.py").write_text("def lonely():\n    return 0\n", encoding="utf-8")
    rmap = rm_mod.build_repo_map(rm_ws, max_files=10)
    lines = rmap.splitlines()
    idx_core = next(i for i, l in enumerate(lines) if "zzz_core.py" in l)
    idx_leaf = next(i for i, l in enumerate(lines) if "00_leaf.py" in l)
    assert idx_core < idx_leaf

    # 4. Memory semantic conflict resolution supersedes older fact
    mdb = mem_mod.Memory(tmp_path / "mem.db")
    try:
        old_m = mdb.add("User editor preference: use dark theme always", kind="preference")
        new_m = mdb.add("User editor preference: use light theme always", kind="preference")
        assert new_m["supersedes_id"] == old_m["id"]
        hits = mdb.search("editor preference theme", k=5)
        hit_ids = [h["id"] for h in hits]
        assert new_m["id"] in hit_ids and old_m["id"] not in hit_ids
    finally:
        mdb.db.close()

    # 5. Persistent run_command retains env/cwd cleanly and handles stdin
    from lagent import shell_session
    ss = SessionState("s_sh", False, "nonce", [], set(), set())
    j = Journal(tmp_path / "j_sh")
    ctx = ToolCtx(cfg=cfg, guard=sec.PathGuard(ws_ng), journal=j, session=ss)
    stdin_cmd = (
        f'& "{sys.executable}" -c "import sys; print(sys.stdin.read().strip())"'
        if sec.IS_WIN else
        f'"{sys.executable}" -c "import sys; print(sys.stdin.read().strip())"'
    )

    async def _run_sh():
        r_stdin = await files_tool.run_command(ctx, {
            "command": stdin_cmd,
            "stdin": "hello_from_stdin",
        })
        assert not r_stdin.error and "hello_from_stdin" in r_stdin.content
        r_p1 = await files_tool.run_command(ctx, {
            "command": "$env:LAGENT_PERSIST_VAR='val42'" if sec.IS_WIN else "export LAGENT_PERSIST_VAR=val42",
            "persistent": True,
        })
        assert not r_p1.error
        r_p2 = await files_tool.run_command(ctx, {
            "command": "echo $env:LAGENT_PERSIST_VAR" if sec.IS_WIN else "echo $LAGENT_PERSIST_VAR",
            "persistent": True,
        })
        assert not r_p2.error and "val42" in r_p2.content
        await shell_session.close_all_shells()

    asyncio.run(_run_sh())


# ===================================================================== P2: JSONL, Fork, Steer & CLI
def test_p2_jsonl_session_fork_midturn_steer_and_cli(monkeypatch, tmp_path):
    # 1. Append-only JSONL step persistence & recovery
    store = SessionStore(tmp_path / "sess")
    s = store.create(str(tmp_path))
    s.history.append({"role": "user", "content": "step 1"})
    s.ui.append({"id": "u1", "role": "user", "text": "step 1", "hist_index": 0})
    store.save_step(s)
    assert (tmp_path / "sess" / f"{s.id}.events.jsonl").is_file()
    store._cache.clear()
    recovered = store.get(s.id)
    assert recovered is not None and len(recovered.history) == 1 and recovered.history[0]["content"] == "step 1"

    # 2. Fork & Mid-Turn Steer HTTP endpoints + Run loop execution
    st = State(token="tok-p2", port=8771, root=tmp_path / "srv_p2")
    try:
        app = make_app(st)
        sess = st.store.create(str(tmp_path))
        sess.history.extend([
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ])
        sess.ui.extend([
            {"id": "u1", "role": "user", "text": "q1", "hist_index": 0},
            {"id": "a1", "role": "assistant", "blocks": [{"type": "text", "text": "a1"}], "hist_index": 1},
            {"id": "u2", "role": "user", "text": "q2", "hist_index": 2},
            {"id": "a2", "role": "assistant", "blocks": [{"type": "text", "text": "a2"}], "hist_index": 3},
        ])
        st.store.save(sess)

        rc_f, raw_f = asyncio.run(_call_asgi(
            app, "POST", f"/api/sessions/{sess.id}/fork", port=8771, token="tok-p2",
            body=json.dumps({"from_index": 1}).encode(),
        ))
        assert rc_f == 201
        fork_d = json.loads(raw_f)
        assert fork_d["parent_session_id"] == sess.id
        assert len(fork_d["messages"]) == 2

        # Mid-turn steer injects user direction cleanly before run finishes
        step_cnt = {"n": 0}
        saw_steer = {"ok": False}

        async def fake_ensure_ready():
            st.engine.status = "ready"

        async def fake_chat_stream(body):
            step_cnt["n"] += 1
            msgs = body.get("messages") or []
            if any("switch to rust" in str(m.get("content") or "") for m in msgs):
                saw_steer["ok"] = True
                yield {"choices": [{"delta": {"content": "switched to rust!"}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}
                return
            if step_cnt["n"] == 1:
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tc_s1", "function": {"name": "list_dir", "arguments": "{}"}}]}, "finish_reason": None}]}
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
            else:
                yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}}

        st.engine.status = "ready"
        st.engine.ctx = 4096
        st.engine.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
        st.engine.chat_stream = fake_chat_stream  # type: ignore[method-assign]

        async def run_steer_flow():
            run = st.agent.start_run(sess, "start task", [], [], [])
            # Inject steer immediately on step 1
            await asyncio.sleep(0.01)
            rc_s, _ = await _call_asgi(app, "POST", f"/api/runs/{run.id}/steer", port=8771, token="tok-p2", body=json.dumps({"text": "switch to rust"}).encode())
            assert rc_s == 200
            if run.task:
                await run.task

        asyncio.run(run_steer_flow())
        assert saw_steer["ok"] is True

        # 3. Headless -p CLI runner completes cleanly
        async def fast_oneshot_stream(body):
            yield {"choices": [{"delta": {"content": "CLI_OK"}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}}

        st.engine.chat_stream = fast_oneshot_stream  # type: ignore[method-assign]
        rc_cli = asyncio.run(run_oneshot_prompt(st, "hello cli"))
        assert rc_cli == 0
    finally:
        st.artifacts.close()
        st.memory.db.close()


def test_p1_p2_extra_coverage(monkeypatch, tmp_path):
    from lagent.mission import EpisodeOutcome
    from lagent.runtime.handoff import extract_episode_handoff

    # 1. Mission git rollback preserves pre-existing untracked user files while removing mission m2 files
    ws = _init_git(tmp_path / "rb_preserve")
    (ws / "user_untracked_pre.txt").write_text("user precious note\n", encoding="utf-8")
    cfg = Config(tmp_path / "c3.json", tmp_path / "s3.json")

    async def rb_runner(**kw):
        ms = kw["milestone"]
        (ws / f"{ms.id}.txt").write_text(f"built {ms.id}\n", encoding="utf-8")
        return EpisodeOutcome(steps=1, tokens=10, summary="done", milestone_ready=True)

    mgr = MissionManager(cfg=cfg, store=MissionStore(tmp_path / "ms3"), default_episode_runner=rb_runner)
    m = mgr.create_mission(
        title="Preserve Untracked",
        goal="preserve user untracked on rollback",
        workspace=str(ws),
        mode="unattended",
        milestones=[
            Milestone(id="m1", title="M1", acceptance=[AcceptanceCheck(kind="file_exists", target="m1.txt")]),
            Milestone(id="m2", title="M2", depends_on=["m1"], acceptance=[AcceptanceCheck(kind="file_exists", target="m2.txt")]),
        ],
    )
    done_m = asyncio.run(mgr.run_to_completion(m.id))
    assert done_m.status == "completed"
    # Simulate an added untracked file created during m2 vs pre-existing untracked
    (ws / "temp_after.txt").write_text("temp", encoding="utf-8")
    mgr.rollback(done_m.id, "m1")
    assert (ws / "user_untracked_pre.txt").read_text("utf-8") == "user precious note\n"
    assert not (ws / "m2.txt").exists()
    assert not (ws / "temp_after.txt").exists()

    # 2. Structured Handoff parser & max_steps gating
    ho = extract_episode_handoff(
        'Summary\n<handoff>{"milestone_ready":false,"decisions":["Use SQLite"],"open_questions":["Confirm SLA"],"next_actions":["Add tests"]}</handoff>',
        "done",
        has_acceptance=False,
        tool_calls=2,
    )
    assert ho["milestone_ready"] is False
    assert ho["decisions"] == ["Use SQLite"]
    assert ho["open_questions"] == ["Confirm SLA"]
    assert ho["next_actions"] == ["Add tests"]
    ho_ms = extract_episode_handoff("stopped", "max_steps", has_acceptance=True, tool_calls=3)
    assert ho_ms["milestone_ready"] is False and ho_ms["next_actions"]

    # 3. FileLeaseManager wired into Agent._exec_tool rejects conflicting concurrent write
    st = State(token="tok-ext", port=8772, root=tmp_path / "srv_ext")
    try:
        sess = st.store.create(str(ws))
        sst = SessionState(sess.id, False, "nonce", [], set(), set(), agent_id="node_a")
        sst.sub_mode = "auto_edit"
        sst.depth = 1
        async def _check_leases_and_user():
            run = Run(sess.id, agent_id="node_a")
            ctx = st.agent._make_ctx(run, sess, sst, str(ws))
            leases = FileLeaseManager()
            assert leases.acquire_write("m1.txt", "other_worker") is True
            files_tool.mark_seen(ctx, ws / "m1.txt", (ws / "m1.txt").read_bytes())
            ctx.leases = leases  # type: ignore[attr-defined]
            ui = {"blocks": []}
            res_txt, _ = await st.agent._exec_tool(
                run, sess, sst, ctx, ui,
                {"id": "tc_l1", "name": "write_file", "arguments": json.dumps({"path": "m1.txt", "content": "conflict"})},
                [],
            )
            assert "文件租约冲突" in res_txt
            leases.release_owner("other_worker")

            # 4. Agent._append_user injects date into user turn while system_prompt remains date-free
            await st.agent._append_user(run, sess, ctx, "hello", [], [], [])
            assert dt.date.today().isoformat() in str(sess.history[-1]["content"])

            # 5. code_diagnostics with mocked LSP result
            (ws / "ok.py").write_text("x = 1\n", encoding="utf-8")
            monkeypatch.setattr("lagent.lsp.query_lsp_diagnostics", lambda *_a, **_kw: ["ok.py:1:1: [warning] test_lsp"])
            diag_res = await files_tool.code_diagnostics(ctx, {"path": "ok.py"})
            assert "test_lsp" in diag_res.content

        asyncio.run(_check_leases_and_user())

        # 6. ACP JSON-RPC stdio initialize -> session/create -> shutdown
        lines_in = iter([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/create", "params": {"workspace": str(ws)}}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}}) + "\n",
        ])
        out_buf = io.StringIO()
        monkeypatch.setattr(sys.stdin, "readline", lambda: next(lines_in, ""))
        monkeypatch.setattr(sys, "stdout", out_buf)
        rc_acp = asyncio.run(run_acp_stdio(st))
        assert rc_acp == 0
        acp_msgs = [json.loads(ln) for ln in out_buf.getvalue().splitlines() if ln.strip()]
        assert len(acp_msgs) == 3
        assert acp_msgs[0]["result"]["capabilities"]["steer"] is True
        assert acp_msgs[1]["result"]["session_id"]
        assert acp_msgs[2]["result"]["ok"] is True
    finally:
        st.artifacts.close()
        st.memory.db.close()
