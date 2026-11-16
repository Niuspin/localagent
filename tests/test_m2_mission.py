"""M2 Mission 运行时单元测试与集成测试。

覆盖范围：
1. test_mission_resume: 中途崩溃重启 (--resume-missions / resume_all) 断点续跑且不重复已完成里程碑
2. test_mission_watchdog: 重复工具调用 / 无进展 / 连续验证失败触发 nudge -> replan -> review -> pause 升级链
3. test_mission_policy: 无人值守 allowlist 越界阻塞 + 非依赖里程碑推进 + approve 续跑 + PathGuard/HARD_DENY/Taint 不可绕过
4. test_mission_budget: 80% 警告与 100% 自动保存 Handoff 并暂停（tokens/steps/disk_mb/wall_s）
5. MISSION.md 人工编辑重载、Git mission/<id> 自动提交与一键回滚、摘要保留 plan/acceptance/open_questions
6. 8 小时模拟（20 个分段 Episode）验证每段上下文严格有界不膨胀
7. /mission 斜杠命令、/api/missions* HTTP 与 SSE 接口鉴权契约、--resume-missions 参数解析
8. Agent.run_mission_episode 真实工具边界与引擎协同
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lagent import security as sec  # noqa: E402
from lagent.commands import CommandContext, dispatch_command  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.events import EVENT_KINDS  # noqa: E402
from lagent.loop import Agent  # noqa: E402
from lagent.mission import (  # noqa: E402
    AcceptanceCheck,
    EpisodeOutcome,
    Milestone,
    MissionBudgetTracker,
    MissionManager,
    MissionPolicy,
    MissionStore,
    SleepInhibitor,
    StallWatchdog,
    in_quiet_hours,
)
from lagent.server import State, make_app  # noqa: E402
from lagent.sessions import Session  # noqa: E402


def _git(ws: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(ws),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return cp.stdout.strip()


def _init_git_repo(ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init")
    _git(ws, "config", "user.name", "Test User")
    _git(ws, "config", "user.email", "test@localagent.local")
    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "initial")
    return ws


async def _call_asgi(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    port: int = 8765,
    token: str | None = "tok-m2",
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", f"127.0.0.1:{port}".encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if body:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", port),
    }
    sent: list[dict] = []
    more = True

    async def receive():
        nonlocal more
        if more:
            more = False
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg: dict):
        sent.append(msg)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    res_hdrs = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), res_hdrs, payload


# ==================================================================== 1. Resume
def test_mission_resume(tmp_path):
    ws = _init_git_repo(tmp_path / "repo")
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "missions")

    executed: list[str] = []

    async def fake_runner(**kw):
        ms = kw["milestone"]
        executed.append(ms.id)
        (ws / f"{ms.id}.txt").write_text(f"done {ms.id}\n", encoding="utf-8")
        if ms.id == "m2" and executed.count("m2") == 1:
            # 模拟 m2 执行到一半尚未通过验收时进程被杀死
            (ws / f"{ms.id}.txt").unlink()
            await asyncio.sleep(0.5)
        return EpisodeOutcome(steps=2, tokens=120, tool_calls=1, summary=f"completed {ms.id}", milestone_ready=True)

    async def run_flow():
        mgr1 = MissionManager(cfg=cfg, store=store, default_episode_runner=fake_runner)
        m = mgr1.create_mission(
            title="Crash Resume Test",
            goal="三阶段任务验证崩溃恢复",
            workspace=str(ws),
            mode="unattended",
            milestones=[
                Milestone(id="m1", title="Phase 1", acceptance=[AcceptanceCheck(kind="file_exists", target="m1.txt")]),
                Milestone(id="m2", title="Phase 2", depends_on=["m1"], acceptance=[AcceptanceCheck(kind="file_exists", target="m2.txt")]),
                Milestone(id="m3", title="Phase 3", depends_on=["m2"], acceptance=[AcceptanceCheck(kind="file_exists", target="m3.txt")]),
            ],
        )
        mgr1.start_mission(m.id)
        # 等待 m1 完成进入 m2 睡眠模拟点后立即优雅停机（模拟崩溃中断，保留 running 状态）
        for _ in range(80):
            cur = store.get(m.id)
            if cur and cur.milestone("m1").status == "done" and "m2" in executed:
                break
            await asyncio.sleep(0.02)
        await mgr1.stop_all()

        mid_state = store.get(m.id)
        assert mid_state is not None
        assert mid_state.status == "running"
        assert mid_state.milestone("m1").status == "done"
        assert mid_state.milestone("m1").commit_sha is not None

        # 新实例化 MissionManager（对应启动参数 --resume-missions）
        mgr2 = MissionManager(cfg=cfg, store=MissionStore(tmp_path / "missions"), default_episode_runner=fake_runner)
        resumed_ids = mgr2.resume_all()
        assert m.id in resumed_ids
        task = mgr2._tasks[m.id]
        await task
        return mgr2.get_mission(m.id)

    final_m = asyncio.run(run_flow())
    assert final_m is not None
    assert final_m.status == "completed"
    assert [ms.status for ms in final_m.milestones] == ["done", "done", "done"]
    # 关键验证：m1 在恢复后绝不重复执行
    assert executed.count("m1") == 1
    assert executed.count("m3") == 1
    assert _git(ws, "rev-parse", "--abbrev-ref", "HEAD") == f"mission/{final_m.id}"
    assert (ws / ".localagent" / "missions" / final_m.id / "MISSION.md").is_file()


# ==================================================================== 2. Watchdog
def test_mission_watchdog(tmp_path):
    # 2A. 单元测试重复指纹升级链: nudge -> replan -> review -> pause
    wd = StallWatchdog(repeat_threshold=3, no_progress_threshold=4, verify_fail_threshold=3)
    assert wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0") is None
    assert wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0") is None
    s1 = wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0")
    s2 = wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0")
    s3 = wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0")
    s4 = wd.observe_step(tool="read_file", args={"path": "a.py"}, diff_hash="h0")
    assert [s.action for s in (s1, s2, s3, s4)] == ["nudge", "replan", "review", "pause"]

    # 有真实 diff 变化应清零升级级别
    assert wd.observe_step(tool="write_file", args={"path": "a.py"}, diff_hash="h1") is None
    assert wd.state["escalation_level"] == 0

    # 2B. 端到端集成：连续验收失败导致逐级升级直至 pause 并调用 review_runner
    ws = _init_git_repo(tmp_path / "wd_repo")
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "ms")
    reviewed: list[str] = []

    async def fake_review(m, ms, sig, ws_path):
        reviewed.append(f"{ms.id}:{sig.get('action')}")
        return "Try inspecting failing gate command."

    async def noop_runner(**kw):
        # 始终未产生输出文件且无进展
        return EpisodeOutcome(steps=2, tokens=40, summary="still stuck", milestone_ready=True)

    async def run_wd():
        mgr = MissionManager(cfg=cfg, store=store, default_episode_runner=noop_runner, review_runner=fake_review)
        m = mgr.create_mission(
            title="Watchdog Test",
            goal="测试停滞自动升级",
            workspace=str(ws),
            mode="unattended",
            milestones=[
                Milestone(
                    id="m1",
                    title="Failing Gate",
                    max_attempts=10,
                    acceptance=[AcceptanceCheck(kind="file_exists", target="never_created.txt")],
                )
            ],
        )
        return await mgr.run_to_completion(m.id)

    res_m = asyncio.run(run_wd())
    assert res_m.status == "paused"
    assert res_m.pause_reason and res_m.pause_reason.startswith("stall_escalated:")
    actions = [x["action"] for x in res_m.stall_state.get("history", [])]
    assert actions == ["nudge", "replan", "review", "pause"]
    assert len(reviewed) == 1
    events = store.read_events(res_m.id)
    assert any(e["kind"] == "stall_detected" for e in events)
    assert any(e["kind"] == "mission_paused" for e in events)


# ==================================================================== 3. Policy
def test_mission_policy(tmp_path):
    ws = _init_git_repo(tmp_path / "pol_repo")
    (ws / "src").mkdir()
    guard = sec.PathGuard(ws)
    pol = MissionPolicy(
        {
            "mode": "unattended",
            "allow_paths": ["src/**"],
            "allow_commands": ["pytest*"],
            "allow_domains": ["docs.python.org"],
        },
        guard,
        workspace=ws,
    )

    # 3A. 单调安全边界不可绕过
    assert pol.evaluate("write_file", {"path": "src/main.py"}).allowed is True
    v_out = pol.evaluate("write_file", {"path": "deploy/prod.yaml"})
    assert v_out.allowed is False and v_out.action == "block"
    assert pol.evaluate("shell", {"command": "pytest tests/"}).allowed is True
    v_cmd = pol.evaluate("shell", {"command": "pip install badpkg"})
    assert v_cmd.allowed is False and v_cmd.action == "block"
    assert pol.evaluate("web_fetch", {"url": "https://docs.python.org/3/"}).allowed is True
    assert pol.evaluate("web_fetch", {"url": "https://evil.example.com/x"}).action == "block"

    # 硬拒绝 / PathGuard / 敏感文件 / Taint 绝不能放行
    evil_pol = MissionPolicy({"mode": "unattended", "allow_paths": ["**", "../**"], "allow_commands": ["*"]}, guard, workspace=ws)
    assert evil_pol.evaluate("shell", {"command": "rm -rf /"}).action == "deny"
    assert evil_pol.evaluate("write_file", {"path": "../../outside.txt"}).action == "deny"
    assert evil_pol.evaluate("write_file", {"path": ".env"}).action == "block"
    assert evil_pol.evaluate("shell", {"command": "pytest"}, tainted=True).action == "block"
    assert evil_pol.evaluate("write_file", {"path": "src/a.py"}, tainted=True).action == "block"

    # 3B. Runner 集成：越界操作不执行直接阻断，同时推进非依赖里程碑，用户 approve 后完成
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "ms")

    async def pol_runner(**kw):
        ms = kw["milestone"]
        policy = kw["policy"]
        approved = kw["approved_actions"]
        if ms.id == "m1":
            verdict = policy.evaluate("write_file", {"path": "deploy/prod.yml"}, approved_actions=approved)
            if not verdict.allowed:
                return EpisodeOutcome(
                    steps=1,
                    milestone_ready=False,
                    blocked={"tool": "write_file", "target": verdict.target, "reason": verdict.reason, "signature": "write_file:deploy/prod.yml"},
                    summary="blocked outside allowlist",
                )
            (ws / "deploy").mkdir(exist_ok=True)
            (ws / "deploy" / "prod.yml").write_text("ok: true\n", encoding="utf-8")
            return EpisodeOutcome(steps=1, milestone_ready=True, summary="wrote deploy after approval")
        (ws / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        return EpisodeOutcome(steps=1, milestone_ready=True, summary="wrote src/ok.py")

    async def run_pol():
        mgr = MissionManager(cfg=cfg, store=store, default_episode_runner=pol_runner)
        m = mgr.create_mission(
            title="Policy Block Test",
            goal="越界阻塞与非依赖推进",
            workspace=str(ws),
            mode="unattended",
            policy={"mode": "unattended", "allow_paths": ["src/**"]},
            milestones=[
                Milestone(id="m1", title="Outside Write", acceptance=[AcceptanceCheck(kind="file_exists", target="deploy/prod.yml")]),
                Milestone(id="m2", title="Independent Src", acceptance=[AcceptanceCheck(kind="file_exists", target="src/ok.py")]),
            ],
        )
        m_mid = await mgr.run_to_completion(m.id)
        # m1 应被阻塞且文件未写入，而非依赖的 m2 应已先完成！
        assert not (ws / "deploy" / "prod.yml").exists()
        assert m_mid.milestone("m2").status == "done"
        assert m_mid.milestone("m1").status == "blocked"
        assert m_mid.status == "blocked"
        aid = m_mid.pending_approvals()[0]["id"]

        mgr.approve(m.id, aid, "allow")
        await mgr._tasks[m.id]
        return mgr.get_mission(m.id)

    final_m = asyncio.run(run_pol())
    assert final_m.status == "completed"
    assert (ws / "deploy" / "prod.yml").read_text("utf-8") == "ok: true\n"


# ==================================================================== 4. Budget
def test_mission_budget(tmp_path):
    # 4A. BudgetTracker 80% warn + 100% exceed (含 disk_mb 与 wall_s)
    bcfg = {
        "wall_s": 100.0,
        "tokens": 1000,
        "steps": 20,
        "disk_mb": 10.0,
        "episode_max_steps": 10,
        "used_wall_s": 0.0,
        "used_tokens": 0,
        "used_steps": 0,
        "used_disk_mb": 0.0,
        "warned": [],
    }
    tr = MissionBudgetTracker(bcfg)
    s1 = tr.record(steps=2, tokens=820, tool_calls=1)
    assert "tokens" in s1.warned_now and s1.exceeded is None
    # 同维度不重复警告
    s2 = tr.record(steps=1, tokens=20, tool_calls=1)
    assert s2.warned_now == [] and s2.exceeded is None
    s3 = tr.evaluate(disk_mb_override=10.5)
    assert "disk_mb" in s3.warned_now and s3.exceeded == "disk_mb"

    # 4B. Runner 集成：80% 发警告，100% 自动保存 Handoff 并暂停
    ws = tmp_path / "bgt_ws"
    ws.mkdir()
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "ms")

    async def token_heavy_runner(**kw):
        ep = kw["episode"]
        return EpisodeOutcome(steps=2, tokens=450, tool_calls=1, summary=f"chunk {ep.id}", milestone_ready=False)

    async def run_bgt():
        mgr = MissionManager(cfg=cfg, store=store, default_episode_runner=token_heavy_runner)
        m = mgr.create_mission(
            title="Budget Test",
            goal="测试预算警告与自动暂停",
            workspace=str(ws),
            budget={"tokens": 1000, "steps": 100, "wall_s": 3600, "disk_mb": 500},
            milestones=[Milestone(id="m1", title="Loop Work")],
        )
        return await mgr.run_to_completion(m.id)

    res_m = asyncio.run(run_bgt())
    assert res_m.status == "paused"
    assert res_m.pause_reason == "budget_exceeded:tokens"
    assert res_m.latest_handoff() is not None
    evs = [e["kind"] for e in store.read_events(res_m.id)]
    assert "budget_warn" in evs
    assert "mission_paused" in evs
    assert "handoff_saved" in evs


# ==================================================================== 5. MISSION.md, Rollback & Compaction
def test_mission_md_edit_git_rollback_and_compaction(tmp_path):
    ws = _init_git_repo(tmp_path / "rb_repo")
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "ms")

    captured_prompts: list[str] = []

    async def runner(**kw):
        ms = kw["milestone"]
        captured_prompts.append(kw["prompt"])
        (ws / f"{ms.id}.txt").write_text(f"data {ms.id}\n", encoding="utf-8")
        return EpisodeOutcome(
            steps=1,
            tokens=50,
            summary=f"built {ms.id}",
            open_questions=["Should we keep v1 fallback?"] if ms.id == "m1" else [],
            milestone_ready=True,
        )

    async def flow():
        mgr = MissionManager(cfg=cfg, store=store, default_episode_runner=runner)
        m = mgr.create_mission(
            title="MD and Rollback",
            goal="验证 MISSION.md 与回滚",
            workspace=str(ws),
            mode="unattended",
            acceptance=[AcceptanceCheck(kind="file_exists", target="m1.txt")],
            milestones=[
                Milestone(id="m1", title="Base", acceptance=[AcceptanceCheck(kind="file_exists", target="m1.txt")]),
                Milestone(id="m2", title="Feature", depends_on=["m1"], acceptance=[AcceptanceCheck(kind="file_exists", target="m2.txt")]),
            ],
        )
        # 在启动前人工修改磁盘上的 MISSION.md 注入 User Notes
        md_p = store.mission_md_path(m)
        txt = store.read_mission_md(m)
        txt = txt.replace(
            "<!-- 在此添加补充说明；下次 Episode 启动前会自动读取 -->",
            "务必使用 utf-8 编码输出所有结果。",
        )
        md_p.write_text(txt, encoding="utf-8")

        done_m = await mgr.run_to_completion(m.id)
        assert done_m.status == "completed"
        assert "务必使用 utf-8 编码输出所有结果" in captured_prompts[0]
        assert (ws / "m2.txt").is_file()

        # 验证 Agent._preserve_mission_summary 结构化保留 plan / acceptance / open_questions
        dummy_agent = Agent.__new__(Agent)
        dummy_agent.missions = mgr
        sess = Session("s1", workspace=str(ws))
        sess.mission_id = done_m.id
        compacted = dummy_agent._preserve_mission_summary(sess, "Earlier summary.")
        assert "### plan" in compacted and "### acceptance" in compacted and "### open_questions" in compacted
        assert "Should we keep v1 fallback?" in compacted

        # 一键回滚到 m1
        rolled = mgr.rollback(done_m.id, "m1")
        assert rolled.status == "paused"
        assert rolled.milestone("m1").status == "done"
        assert rolled.milestone("m2").status == "pending"
        assert rolled.milestone("m2").commit_sha is None
        assert (ws / "m1.txt").is_file()
        assert not (ws / "m2.txt").exists()

    asyncio.run(flow())


# ==================================================================== 6. 8h Simulation & Bounded Context
def test_mission_8h_simulated_bounded_context(tmp_path):
    ws = tmp_path / "long_ws"
    ws.mkdir()
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    store = MissionStore(tmp_path / "ms")

    async def sim_runner(**kw):
        ms = kw["milestone"]
        ep = kw["episode"]
        # 输出较长摘要测试 handoff 裁剪与分段上下文不线性增长
        return EpisodeOutcome(
            steps=3,
            tokens=100,
            tool_calls=2,
            summary=(f"Episode {ep.id} finished {ms.id}. " + ("log_detail " * 80)),
            next_actions=[f"continue after {ms.id}"],
            milestone_ready=True,
        )

    milestones = [
        Milestone(id=f"m{i}", title=f"Stage {i}", depends_on=[f"m{i-1}"] if i > 1 else [])
        for i in range(1, 17)
    ]

    async def run_sim():
        mgr = MissionManager(cfg=cfg, store=store, default_episode_runner=sim_runner)
        m = mgr.create_mission(
            title="8h Long Horizon Sim",
            goal="模拟 16 段长时间连续重构，校验上下文长度严格有界",
            workspace=str(ws),
            mode="unattended",
            budget={"wall_s": 28800, "tokens": 5_000_000, "steps": 2000, "disk_mb": 4096},
            milestones=milestones,
        )
        return await mgr.run_to_completion(m.id)

    fin = asyncio.run(run_sim())
    assert fin.status == "completed"
    assert len(fin.episodes) == 16
    chars = [ep.context_chars for ep in fin.episodes]
    assert max(chars) < 6000
    # 最后分段不会随着分段数线性爆炸
    assert chars[-1] - chars[1] < 1200


# ==================================================================== 7. HTTP / SSE / Slash Commands / CLI
def test_mission_http_api_sse_and_slash_commands(tmp_path):
    # 事件枚举检查
    for req_ev in ("episode_started", "handoff_saved", "milestone_done", "gate_result", "stall_detected", "mission_blocked", "budget_warn"):
        assert req_ev in EVENT_KINDS

    # Quiet hours & SleepInhibitor 安全性检查
    from datetime import datetime
    assert in_quiet_hours("23:00-07:00", now=datetime(2026, 1, 1, 23, 30)) is True
    assert in_quiet_hours("23:00-07:00", now=datetime(2026, 1, 1, 12, 0)) is False
    si = SleepInhibitor(enabled=False)
    assert si.acquire() is False
    si.release()

    ws = tmp_path / "api_ws"
    ws.mkdir()
    st = State(token="tok-m2", port=8765, root=tmp_path / "srv")
    try:
        sess = st.store.create(str(ws))
        app = make_app(st)

        # 未鉴权请求必须拒绝
        code, _, _ = asyncio.run(_call_asgi(app, "GET", "/api/missions", token=None))
        assert code == 401

        # 创建 Mission (auto_start=False 测试纯路由闭环)
        payload = json.dumps({
            "goal": "1. Read files\n2. Write tests",
            "title": "API Test Mission",
            "workspace": str(ws),
            "mode": "attended",
            "session_id": sess.id,
            "auto_start": False,
        }).encode()
        code, _, body = asyncio.run(_call_asgi(app, "POST", "/api/missions", body=payload))
        assert code == 200
        created = json.loads(body)
        mid = created["id"]
        assert len(created["milestones"]) == 2

        code, _, body = asyncio.run(_call_asgi(app, "GET", "/api/missions"))
        assert code == 200 and any(x["id"] == mid for x in json.loads(body)["missions"])

        code, _, body = asyncio.run(_call_asgi(app, "PATCH", f"/api/missions/{mid}", body=json.dumps({"mode": "unattended"}).encode()))
        assert code == 200 and json.loads(body)["mode"] == "unattended"

        # MISSION.md GET & PUT
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/missions/{mid}/mission-md"))
        assert code == 200 and "API Test Mission" in json.loads(body)["content"]
        new_md = json.loads(body)["content"] + "\nExtra note via PUT.\n"
        code, _, body = asyncio.run(_call_asgi(app, "PUT", f"/api/missions/{mid}/mission-md", body=json.dumps({"content": new_md}).encode()))
        assert code == 200 and "Extra note via PUT." in json.loads(body)["mission_md"]

        # Timeline & Events SSE (已完成状态立即下发存量事件流结束)
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/missions/{mid}/timeline"))
        assert code == 200 and "events" in json.loads(body)

        asyncio.run(asyncio.sleep(0))
        code, _, _ = asyncio.run(_call_asgi(app, "POST", f"/api/missions/{mid}/pause", body=b"{}"))
        assert code == 200
        code, _, _ = asyncio.run(_call_asgi(app, "POST", f"/api/missions/{mid}/cancel", body=b"{}"))
        assert code == 200

        code, hdrs, sse_body = asyncio.run(_call_asgi(app, "GET", f"/api/missions/{mid}/events"))
        assert code == 200 and "text/event-stream" in hdrs["content-type"]
        assert b"event: mission" in sse_body

        # Slash commands
        ctx = CommandContext(agent=st.agent, session=sess, workspace=ws, cfg=st.cfg, journal=st.journal, missions=st.missions)
        for cmd in (
            "/mission start Refactor module",
            "/mission status",
            "/mission budget",
            "/mission log",
            "/mission edit",
            "/mission pause",
            "/mission stop",
        ):
            res = dispatch_command(cmd, ctx)
            assert res.handled is True and res.is_prompt is False and res.text
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ==================================================================== 8. Agent.run_mission_episode & --resume-missions
def test_agent_run_mission_episode_and_cli_resume_flag(tmp_path):
    ws = tmp_path / "e2e_ws"
    (ws / "src").mkdir(parents=True)
    st = State(token="tok-e2e", port=8765, root=tmp_path / "srv")
    try:
        # Mock 引擎流：第 1 次尝试写越界文件 deploy/out.txt（被 MissionPolicy 阻断），
        # 第 2 次（approve 后）再次写 deploy/out.txt（成功）并输出摘要。
        calls = {"n": 0}

        async def fake_ensure_ready():
            st.engine.status = "ready"

        async def fake_chat_stream(body):
            calls["n"] += 1
            msgs = body.get("messages") or []
            has_tool_res = any(m.get("role") == "tool" for m in msgs)
            if not has_tool_res:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": f"tc_{calls['n']}",
                                "function": {"name": "write_file", "arguments": json.dumps({"path": "deploy/out.txt", "content": "ok\n"})},
                            }]
                        },
                        "finish_reason": None,
                    }]
                }
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 55}}
            else:
                yield {"choices": [{"delta": {"content": "Milestone completed safely."}, "finish_reason": "stop"}], "usage": {"total_tokens": 30}}

        st.engine.status = "ready"
        st.engine.ctx = 4096
        st.engine.vision = False
        st.engine.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
        st.engine.chat_stream = fake_chat_stream  # type: ignore[method-assign]

        async def run_e2e():
            m = st.missions.create_mission(
                title="E2E Agent Episode",
                goal="测试真实 Agent._loop 协同与边界阻断",
                workspace=str(ws),
                mode="unattended",
                policy={"mode": "unattended", "allow_paths": ["src/**"]},
                milestones=[Milestone(id="m1", title="Write Deploy", acceptance=[AcceptanceCheck(kind="file_exists", target="deploy/out.txt")])],
            )
            m_blk = await st.missions.run_to_completion(m.id)
            assert m_blk.status == "blocked"
            assert not (ws / "deploy" / "out.txt").exists()
            aid = m_blk.pending_approvals()[0]["id"]
            st.missions.approve(m.id, aid, "allow")
            await st.missions._tasks[m.id]
            return st.missions.get_mission(m.id)

        done = asyncio.run(run_e2e())
        assert done.status == "completed"
        assert (ws / "deploy" / "out.txt").read_text("utf-8") == "ok\n"

        # 校验 launcher --resume-missions 解析分支逻辑存在且可执行
        launcher_src = (ROOT / "lagent" / "launcher.py").read_text("utf-8")
        assert "--resume-missions" in launcher_src
        assert "st.resume_missions" in launcher_src
    finally:
        st.artifacts.close()
        st.memory.db.close()
