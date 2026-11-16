"""Mission 分段执行引擎、Git 分支自动提交/回滚、心跳与崩溃恢复调度。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import security as sec
from .budget import MissionBudgetTracker
from .gates import format_gate_failures, run_acceptance_checks
from .model import (
    AcceptanceCheck,
    Episode,
    Handoff,
    Milestone,
    Mission,
    apply_mission_md_edits,
    default_milestones_from_goal,
    default_mission_budget,
    default_mission_policy,
    new_id,
)
from .policy import MissionPolicy, queue_approval
from .power import SleepInhibitor, in_quiet_hours, notify_mission
from .store import MissionStore
from .watchdog import StallWatchdog


@dataclass
class EpisodeOutcome:
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0
    summary: str = ""
    milestone_ready: bool = True
    next_actions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    stall_signal: dict[str, Any] | None = None
    run_id: str | None = None
    error: str | None = None
    context_chars: int = 0


def _run_git(ws: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(ws),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _is_git_repo(ws: Path) -> bool:
    if not ws or not ws.is_dir():
        return False
    try:
        cp = _run_git(ws, "rev-parse", "--is-inside-work-tree")
        return cp.returncode == 0 and cp.stdout.strip().lower() == "true"
    except OSError:
        return False


def _workspace_diff_fingerprint(ws: Path) -> tuple[str, str]:
    """返回 (可读 diff 摘要, 确定性变更哈希)，兼容 Git 与普通目录。"""
    if _is_git_repo(ws):
        st = _run_git(ws, "status", "--porcelain")
        df = _run_git(ws, "diff", "--stat")
        stat_txt = (df.stdout or "").strip()
        short_txt = (st.stdout or "").strip()
        summary = "\n".join(x for x in (short_txt[:800], stat_txt[:800]) if x)
        digest = hashlib.sha256((short_txt + "\n" + stat_txt).encode("utf-8")).hexdigest()[:16]
        return summary or "(clean)", digest

    # 非 git 目录退化为工作区文件元信息快照
    parts: list[str] = []
    if ws.is_dir():
        for p in sorted(ws.rglob("*")):
            if any(k in p.parts for k in (".git", ".localagent", "__pycache__", ".venv", "node_modules")):
                continue
            if p.is_file():
                try:
                    stt = p.stat()
                    parts.append(f"{p.relative_to(ws).as_posix()}:{stt.st_size}:{int(stt.st_mtime_ns)}")
                except OSError:
                    continue
            if len(parts) >= 500:
                break
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"files_tracked={len(parts)}", digest


def _setup_mission_branch(ws: Path, branch_name: str) -> tuple[str | None, str | None, str | None]:
    if not _is_git_repo(ws):
        return None, None, None
    cur_br = _run_git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    head = _run_git(ws, "rev-parse", "HEAD")
    base_branch = cur_br.stdout.strip() if cur_br.returncode == 0 else None
    base_commit = head.stdout.strip() if head.returncode == 0 else None
    if not branch_name:
        return base_branch, base_commit, base_branch
    chk = _run_git(ws, "show-ref", "--verify", f"refs/heads/{branch_name}")
    if chk.returncode == 0:
        sw = _run_git(ws, "checkout", branch_name)
        active = branch_name if sw.returncode == 0 else base_branch
    else:
        cr = _run_git(ws, "checkout", "-b", branch_name)
        active = branch_name if cr.returncode == 0 else base_branch
    return base_branch, base_commit, active


def _git_commit_milestone(ws: Path, mission_id: str, ms: Milestone) -> str | None:
    if not _is_git_repo(ws):
        return None
    _run_git(ws, "add", "-A")
    diff_chk = _run_git(ws, "diff", "--cached", "--quiet")
    if diff_chk.returncode == 0:
        head = _run_git(ws, "rev-parse", "HEAD")
        return head.stdout.strip() if head.returncode == 0 else None
    msg = f"mission({mission_id}): {ms.id} - {ms.title}"
    cp = _run_git(
        ws,
        "-c", "user.name=LocalAgent Mission",
        "-c", "user.email=mission@localagent.local",
        "commit",
        "-m",
        msg,
    )
    if cp.returncode != 0:
        return None
    head = _run_git(ws, "rev-parse", "HEAD")
    return head.stdout.strip() if head.returncode == 0 else None


def _git_rollback(ws: Path, commit_sha: str) -> bool:
    if not _is_git_repo(ws) or not commit_sha:
        return False
    res = _run_git(ws, "reset", "--hard", commit_sha)
    if res.returncode != 0:
        return False
    _run_git(ws, "clean", "-fd", "-e", ".localagent/")
    return True


def build_episode_prompt(
    mission: Mission,
    milestone: Milestone,
    episode_id: int,
    *,
    diff_summary: str = "",
    watchdog_hint: str = "",
    memory_snippet: str = "",
) -> str:
    """构造前缀缓存友好、严格有界的 Episode 启动提示（不携带历史全文对话）。"""
    done_ms = [f"- {m.id}: {m.title}" for m in mission.milestones if m.status == "done"]
    pending_ms = [f"- {m.id}: {m.title} ({m.status})" for m in mission.milestones if m.status not in ("done", "skipped")]
    ho = mission.latest_handoff()

    # 固定头部保持 KV Cache 稳定
    lines = [
        f"[MISSION] id={mission.id} | title={mission.title} | mode={mission.mode} | branch={mission.branch or '-'}",
        f"Goal: {mission.goal.strip()[:1200]}",
        "",
        f"Current Episode: #{episode_id}",
        f"Active Milestone: {milestone.id} — {milestone.title}",
    ]
    if milestone.description:
        lines.append(f"Milestone Detail: {milestone.description.strip()[:1200]}")
    if milestone.acceptance:
        lines.append("Milestone Acceptance Checks:")
        for chk in milestone.acceptance[:10]:
            lines.append(f"  - [{chk.kind}] {chk.target}" + (f" (expect: {chk.expect})" if chk.expect else ""))
    if mission.acceptance:
        lines.append("Global Acceptance Checks:")
        for chk in mission.acceptance[:8]:
            lines.append(f"  - [{chk.kind}] {chk.target}" + (f" (expect: {chk.expect})" if chk.expect else ""))

    if done_ms:
        lines.extend(["", "Completed Milestones:", *done_ms[-8:]])
    if pending_ms:
        lines.extend(["", "Remaining Milestones:", *pending_ms[:10]])

    if ho is not None:
        lines.extend(["", f"Previous Handoff (Episode #{ho.episode_id}):", ho.summary.strip()[:1200]])
        if ho.next_actions:
            lines.append("Next Actions:")
            lines.extend(f"- {x[:240]}" for x in ho.next_actions[:8])

    if mission.open_questions:
        lines.extend(["", "Open Questions:", *[f"- {q[:200]}" for q in mission.open_questions[-6:]]])
    if mission.decisions:
        lines.extend(["", "Decisions:", *[f"- {d[:200]}" for d in mission.decisions[-6:]]])
    if milestone.error:
        lines.extend(["", f"Previous Failure / Gate Feedback:\n{milestone.error[:800]}"])
    if mission.user_notes.strip():
        lines.extend(["", f"User Notes from MISSION.md:\n{mission.user_notes.strip()[:800]}"])
    if diff_summary:
        lines.extend(["", f"Workspace Git Diff Summary:\n{diff_summary[:800]}"])
    if memory_snippet:
        lines.extend(["", f"Relevant Memory:\n{memory_snippet[:600]}"])
    if watchdog_hint:
        lines.extend(["", f"[WATCHDOG INTERVENTION] {watchdog_hint[:400]}"])

    return "\n".join(lines)


class MissionManager:
    """管理超长期 Mission 生命周期：创建、分段 Episode 调度、心跳、停滞升级、暂停/恢复与回滚。"""

    def __init__(
        self,
        *,
        cfg: Any,
        store: MissionStore,
        journal: Any = None,
        audit: Any = None,
        memory: Any = None,
        swarms: Any = None,
        notify: Callable[[str, dict[str, Any]], Any] | None = None,
        default_episode_runner: Callable[..., Awaitable[EpisodeOutcome | dict[str, Any]]] | None = None,
        review_runner: Callable[..., Awaitable[str]] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.journal = journal
        self.audit = audit
        self.memory = memory
        self.swarms = swarms
        self.notify = notify
        self.default_episode_runner = default_episode_runner
        self.review_runner = review_runner

        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._pause_flags: dict[str, str] = {}
        self._live_subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._shutdown_requested = False

        prevent_sleep = bool(cfg.get("mission", "prevent_sleep", default=True)) if cfg else True
        self._inhibitor = SleepInhibitor(enabled=prevent_sleep)

    # ------------------------------------------------------------------ 查询与 CRUD
    def list_missions(self) -> list[dict[str, Any]]:
        return [m.public_summary() for m in self.store.list()]

    def get_mission(self, mission_id: str) -> Mission | None:
        return self.store.get(mission_id)

    def mission_snapshot(self, mission_id: str) -> dict[str, Any] | None:
        m = self.store.get(mission_id)
        if m is None:
            return None
        d = m.to_dict()
        d["progress"] = m.progress()
        d["summary"] = m.public_summary()
        d["mission_md"] = self.store.read_mission_md(m)
        d["mission_md_path"] = str(self.store.mission_md_path(m))
        d["is_running_task"] = bool(mission_id in self._tasks and not self._tasks[mission_id].done())
        return d

    def subscribe(self, mission_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=250)
        self._live_subs.setdefault(mission_id, set()).add(q)
        return q

    def unsubscribe(self, mission_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._live_subs.get(mission_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._live_subs.pop(mission_id, None)

    def create_mission(
        self,
        *,
        goal: str,
        title: str = "",
        workspace: str = "",
        mode: str = "",
        milestones: list[dict[str, Any] | Milestone] | None = None,
        acceptance: list[dict[str, Any] | AcceptanceCheck] | None = None,
        policy: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        use_swarm: bool = False,
        session_id: str | None = None,
        auto_start: bool = False,
    ) -> Mission:
        ws = str(workspace or (self.cfg.get("workspace") if self.cfg else "") or "").strip()
        default_mode = str(self.cfg.get("mission", "unattended_default", default="attended")) if self.cfg else "attended"
        chosen_mode = str(mode or (policy or {}).get("mode") or default_mode)
        if chosen_mode not in ("attended", "unattended"):
            chosen_mode = "attended"

        mid = new_id("ms")
        m_title = (title or "").strip() or (goal.strip().splitlines()[0][:64] if goal.strip() else mid)

        ms_objs: list[Milestone] = []
        if milestones:
            for idx, item in enumerate(milestones, start=1):
                if isinstance(item, Milestone):
                    ms_objs.append(item)
                else:
                    raw = dict(item)
                    if not raw.get("id"):
                        raw["id"] = f"m{idx}"
                    ms_objs.append(Milestone.from_dict(raw))
        else:
            ms_objs = default_milestones_from_goal(goal)

        acc_objs: list[AcceptanceCheck] = []
        for item in (acceptance or []):
            acc_objs.append(item if isinstance(item, AcceptanceCheck) else AcceptanceCheck.from_dict(item))

        base_pol = default_mission_policy(chosen_mode)
        if policy:
            base_pol.update({k: v for k, v in policy.items() if v is not None})
        base_pol["mode"] = chosen_mode

        base_bgt = default_mission_budget()
        if self.cfg:
            cfg_b = self.cfg.get("mission", "budget", default={}) or {}
            base_bgt.update({k: v for k, v in cfg_b.items() if v is not None})
            ep_max = self.cfg.get("mission", "episode_max_steps", default=25)
            base_bgt["episode_max_steps"] = max(5, min(40, int(ep_max)))
        if budget:
            for k, v in budget.items():
                if v is not None:
                    base_bgt[k] = v

        branch = str(base_pol.get("branch") or f"mission/{mid}")
        m = Mission(
            id=mid,
            title=m_title,
            goal=goal.strip(),
            workspace=ws,
            status="queued",
            mode=chosen_mode,
            branch=branch,
            session_id=session_id,
            milestones=ms_objs,
            acceptance=acc_objs,
            policy=base_pol,
            budget=base_bgt,
            use_swarm=bool(use_swarm),
        )
        self.store.save(m)
        self._emit(m, "mission", {"action": "created", "mission": m.public_summary()})
        if auto_start:
            self.start_mission(m.id)
        return self.store.get(m.id) or m

    def update_mission(self, mission_id: str, patch: dict[str, Any]) -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        if "title" in patch and patch["title"]:
            m.title = str(patch["title"]).strip()
        if "goal" in patch and patch["goal"]:
            m.goal = str(patch["goal"]).strip()
        if "mode" in patch and patch["mode"] in ("attended", "unattended"):
            m.mode = patch["mode"]
            m.policy["mode"] = patch["mode"]
        if "use_swarm" in patch:
            m.use_swarm = bool(patch["use_swarm"])
        if isinstance(patch.get("policy"), dict):
            m.policy.update(patch["policy"])
            if patch["policy"].get("mode") in ("attended", "unattended"):
                m.mode = patch["policy"]["mode"]
        if isinstance(patch.get("budget"), dict):
            for k in ("wall_s", "tokens", "steps", "disk_mb", "episode_max_steps"):
                if k in patch["budget"] and patch["budget"][k] is not None:
                    m.budget[k] = patch["budget"][k]
        if isinstance(patch.get("milestones"), list):
            m.milestones = [Milestone.from_dict(x) for x in patch["milestones"]]
        self.store.save(m)
        self._emit(m, "mission", {"action": "updated", "mission": m.public_summary()})
        return m

    # ------------------------------------------------------------------ 生命周期控制
    def start_mission(self, mission_id: str) -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        if m.id in self._tasks and not self._tasks[m.id].done():
            return m
        if m.status == "cancelled":
            raise ValueError("cancelled mission cannot restart directly")
        m.status = "running"
        m.pause_reason = None
        m.last_error = None
        if not m.started_at:
            m.started_at = time.time()
        self._pause_flags.pop(m.id, None)
        ev = self._cancel_flags.setdefault(m.id, asyncio.Event())
        ev.clear()
        self.store.save(m)
        self._emit(m, "mission_started", {"mission": m.public_summary()})
        try:
            loop = asyncio.get_running_loop()
            self._tasks[m.id] = loop.create_task(self._run_loop(m.id))
        except RuntimeError:
            pass
        return m

    async def run_to_completion(self, mission_id: str) -> Mission:
        """同步等待当前运行任务结束（供 CLI / 测试使用）。"""
        t = self._tasks.get(mission_id)
        if t is None or t.done():
            self.start_mission(mission_id)
            t = self._tasks.get(mission_id)
        if t is not None:
            await t
        return self.store.get(mission_id)  # type: ignore[return-value]

    def pause_mission(self, mission_id: str, reason: str = "user") -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        self._pause_flags[mission_id] = reason
        if mission_id in self._cancel_flags:
            self._cancel_flags[mission_id].set()
        if m.status in ("running", "queued"):
            m.status = "paused"
            m.pause_reason = reason
            self.store.save(m)
            self._emit(m, "mission_paused", {"reason": reason, "mission": m.public_summary()})
        return m

    def resume_mission(self, mission_id: str) -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        if m.status == "completed":
            return m
        self._pause_flags.pop(mission_id, None)
        if m.id in self._cancel_flags:
            self._cancel_flags[m.id].clear()
        m.status = "running"
        m.pause_reason = None
        self.store.save(m)
        self._emit(m, "mission_resumed", {"mission": m.public_summary()})
        if m.id not in self._tasks or self._tasks[m.id].done():
            try:
                loop = asyncio.get_running_loop()
                self._tasks[m.id] = loop.create_task(self._run_loop(m.id))
            except RuntimeError:
                pass
        return m

    def cancel_mission(self, mission_id: str) -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        self._pause_flags[mission_id] = "__cancel__"
        if mission_id in self._cancel_flags:
            self._cancel_flags[mission_id].set()
        t = self._tasks.get(mission_id)
        if t and not t.done():
            t.cancel()
        m.status = "cancelled"
        m.ended_at = time.time()
        self.store.save(m)
        self._emit(m, "mission", {"action": "cancelled", "mission": m.public_summary()})
        return m

    def resume_all(self) -> list[str]:
        """启动恢复所有崩溃前处于 running 状态的 Mission（跳过已完成里程碑）。"""
        resumed: list[str] = []
        for m in self.store.list():
            if m.status == "running":
                # 将崩溃前卡在 running 的未完成里程碑恢复为可续跑状态
                for ms in m.milestones:
                    if ms.status == "running":
                        ms.status = "pending"
                self.store.save(m)
                self.resume_mission(m.id)
                resumed.append(m.id)
        return resumed

    async def stop_all(self) -> None:
        """优雅关闭所有运行中 Mission，保留 running 状态与手写 Handoff 供下次 --resume-missions 恢复。"""
        self._shutdown_requested = True
        for mid, ev in list(self._cancel_flags.items()):
            ev.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inhibitor.release(force=True)

    # ------------------------------------------------------------------ 审批与回滚
    def approve(self, mission_id: str, approval_id: str, decision: str = "allow") -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        target_rec = None
        for rec in m.approvals:
            if rec.get("id") == approval_id:
                target_rec = rec
                break
        if target_rec is None:
            raise KeyError(f"approval not found: {approval_id}")

        allow = decision in ("allow", "approve", "always", "once")
        target_rec["status"] = "approved" if allow else "denied"
        target_rec["resolved_at"] = time.time()
        mid = target_rec.get("milestone_id")
        ms = m.milestone(mid)
        if allow:
            if ms and ms.status == "blocked":
                ms.status = "pending"
                ms.error = None
        else:
            if ms and ms.status == "blocked":
                ms.status = "failed"
                ms.error = f"approval denied: {target_rec.get('target') or target_rec.get('tool')}"

        self._record_timeline(m, "approval", {
            "approval_id": approval_id,
            "decision": target_rec["status"],
            "milestone_id": mid,
            "target": target_rec.get("target"),
        })
        self.store.save(m)
        self._emit(m, "mission", {"action": "approval_resolved", "approval": target_rec})

        if allow and m.status == "blocked":
            return self.resume_mission(m.id)
        return m

    def rollback(self, mission_id: str, target: str) -> Mission:
        m = self.store.get(mission_id)
        if m is None:
            raise KeyError(f"mission not found: {mission_id}")
        ws = Path(m.workspace) if m.workspace else None
        if ws is None or not _is_git_repo(ws):
            raise ValueError("当前工作区不是 Git 仓库，无法执行提交点回滚")

        target_sha: str | None = None
        target_idx: int | None = None
        if target == "base" or (m.base_commit and target == m.base_commit):
            target_sha = m.base_commit
            target_idx = -1
        else:
            for idx, ms in enumerate(m.milestones):
                if ms.id == target or (ms.commit_sha and ms.commit_sha.startswith(target)):
                    target_sha = ms.commit_sha
                    target_idx = idx
                    break
        if not target_sha:
            raise ValueError(f"未找到目标回滚提交点: {target}")

        # 若正在运行则先暂停
        if m.id in self._tasks and not self._tasks[m.id].done():
            self.pause_mission(m.id, reason="rollback")

        if not _git_rollback(ws, target_sha):
            raise RuntimeError(f"git reset 回滚失败: {target_sha}")

        if target_idx is not None:
            reset_ids = set()
            for idx, ms in enumerate(m.milestones):
                if idx > target_idx:
                    reset_ids.add(ms.id)
                    ms.status = "pending"
                    ms.commit_sha = None
                    ms.gate_results = []
                    ms.error = None
                    ms.completed_at = None
            if reset_ids:
                for rec in m.approvals:
                    if rec.get("status") == "pending" and rec.get("milestone_id") in reset_ids:
                        rec["status"] = "cancelled"

        m.status = "paused"
        m.pause_reason = f"rolled_back:{target}"
        self._record_timeline(m, "rollback", {"target": target, "commit": target_sha})
        self.store.save(m, prefer_disk=False)
        self._emit(m, "mission_rollback", {"target": target, "commit": target_sha, "mission": m.public_summary()})
        return m

    # ------------------------------------------------------------------ 核心分段调度循环
    async def _run_loop(self, mission_id: str) -> None:
        m = self.store.get(mission_id)
        if m is None:
            return
        ws = Path(m.workspace).resolve() if m.workspace else Path.cwd()
        guard = self._build_guard(ws)
        tracker = MissionBudgetTracker(m.budget, workspace=ws, mission_dir=self.store.mission_dir(m.id))
        watchdog = StallWatchdog(m.stall_state)

        def _persist(cur_m: Mission, *, prefer_disk: bool = True) -> Mission:
            cur_m.stall_state = dict(watchdog.state)
            cur_m.budget = dict(tracker.cfg)
            return self.store.save(cur_m, prefer_disk=prefer_disk)

        # 建立隔离 git 分支（若工作区为 Git 仓库）
        if not m.base_commit and _is_git_repo(ws):
            base_br, base_sha, active_br = await asyncio.to_thread(
                _setup_mission_branch, ws, str(m.branch or f"mission/{m.id}")
            )
            m.base_branch = m.base_branch or base_br
            m.base_commit = m.base_commit or base_sha
            if active_br:
                m.branch = active_br
            self.store.save(m)

        self._inhibitor.acquire()
        hb_task = asyncio.create_task(self._heartbeat_loop(m.id, tracker))
        try:
            while True:
                m = self.store.get(mission_id)
                if m is None or m.status in ("cancelled", "completed"):
                    break

                # 检查外部 pause / cancel 请求
                if mission_id in self._pause_flags:
                    pr = self._pause_flags.pop(mission_id)
                    if pr == "__cancel__":
                        m.status = "cancelled"
                        m.ended_at = time.time()
                    else:
                        m.status = "paused"
                        m.pause_reason = pr
                    self.store.save(m)
                    break

                # Episode 前重读用户在 MISSION.md 上的手工编辑
                if self.store.sync_mission_md(m):
                    self._record_timeline(m, "mission_md_reloaded", {})

                # 预算评估（80% 警告 / 100% 暂停）
                bstat = tracker.evaluate()
                self._emit_budget_events(m, bstat)
                if bstat.exceeded:
                    m.status = "paused"
                    m.pause_reason = f"budget_exceeded:{bstat.exceeded}"
                    self._save_handoff_only(m, f"触发预算上限 {bstat.exceeded}，已安全保存进度并暂停。")
                    self.store.save(m)
                    self._emit(m, "mission_paused", {"reason": m.pause_reason, "budget": bstat.snapshot})
                    notify_mission(
                        f"Mission 已暂停: {m.title}",
                        f"达到预算上限 ({bstat.exceeded})，已保存 Handoff。",
                        quiet_hours=m.policy.get("quiet_hours"),
                    )
                    break

                # 挑选就绪里程碑（或检查整体完成）
                ready = self._ready_milestones(m)
                if not ready:
                    blocked_ms = [ms for ms in m.milestones if ms.status == "blocked"]
                    failed_ms = [ms for ms in m.milestones if ms.status == "failed"]
                    incomplete = [ms for ms in m.milestones if ms.status not in ("done", "skipped")]
                    if blocked_ms:
                        m.status = "blocked"
                        m.pause_reason = "awaiting_approval"
                        self.store.save(m)
                        self._emit(m, "mission_blocked", {"blocked": [x.id for x in blocked_ms]})
                        notify_mission(
                            f"Mission 需要审批: {m.title}",
                            f"{len(blocked_ms)} 个里程碑等待人工确认。",
                            quiet_hours=m.policy.get("quiet_hours"),
                        )
                        break
                    if incomplete or failed_ms:
                        m.status = "failed"
                        m.last_error = failed_ms[0].error if failed_ms else "dependency_deadlock"
                        m.ended_at = time.time()
                        self.store.save(m)
                        self._emit(m, "mission_done", {"status": "failed", "error": m.last_error})
                        break

                    # 所有里程碑完成 -> 运行 Mission 全局验收门控
                    manual_ok = self._approved_set(m)
                    if m.acceptance:
                        all_ok, gres, needs_manual = await run_acceptance_checks(
                            m.acceptance, ws, guard, cfg=self.cfg, manual_approvals=manual_ok
                        )
                        gdicts = [g.to_dict() for g in gres]
                        self._record_timeline(m, "global_gates", {"ok": all_ok, "results": gdicts})
                        self._emit(m, "gate_result", {"milestone_id": None, "ok": all_ok, "results": gdicts})
                        if needs_manual and not all_ok:
                            m.status = "blocked"
                            for g in gres:
                                if g.awaiting_manual and not g.ok:
                                    queue_approval(
                                        m,
                                        milestone_id=None,
                                        tool="manual_gate",
                                        target=g.target or g.check_id,
                                        reason="manual_acceptance_required",
                                        sig=g.check_id,
                                    )
                            self.store.save(m)
                            self._emit(m, "mission_blocked", {"reason": "manual_acceptance_required"})
                            break
                        if not all_ok:
                            m.status = "failed"
                            m.last_error = format_gate_failures(gres)
                            m.ended_at = time.time()
                            self.store.save(m)
                            self._emit(m, "mission_done", {"status": "failed", "error": m.last_error})
                            break

                    m.status = "completed"
                    m.ended_at = time.time()
                    self.store.save(m)
                    self._emit(m, "mission_done", {"status": "completed", "mission": m.public_summary()})
                    notify_mission(
                        f"Mission 完成: {m.title}",
                        f"全部 {len(m.milestones)} 个里程碑已通过验收。",
                        quiet_hours=m.policy.get("quiet_hours"),
                    )
                    break

                # 选择并执行单个 Episode（若支持多里程碑独立分支/Swarm 可在内部协作）
                ms = ready[0]
                m.current_milestone_id = ms.id
                ms.status = "running"
                m.current_episode += 1
                ep_num = m.current_episode
                ep = Episode(id=ep_num, milestone_id=ms.id, status="running")
                m.episodes.append(ep)
                self.store.save(m)
                self._emit(m, "episode_started", {"episode": ep_num, "milestone_id": ms.id})

                diff_summary, pre_diff_hash = await asyncio.to_thread(_workspace_diff_fingerprint, ws)
                watchdog_hint = self._watchdog_hint_for(m)
                mem_snip = self._recall_memory(f"{m.goal}\n{ms.title}", workspace=str(ws))
                prompt = build_episode_prompt(
                    m,
                    ms,
                    ep_num,
                    diff_summary=diff_summary,
                    watchdog_hint=watchdog_hint,
                    memory_snippet=mem_snip,
                )
                ep.context_chars = len(prompt)

                policy_eval = MissionPolicy(m.policy, guard, workspace=ws)
                approved = self._approved_set(m)
                cancel_ev = self._cancel_flags.setdefault(m.id, asyncio.Event())

                ep_outcome = await self._execute_episode(
                    m,
                    ms,
                    ep,
                    prompt,
                    ws=ws,
                    policy=policy_eval,
                    approved_actions=approved,
                    watchdog=watchdog,
                    tracker=tracker,
                    cancel_event=cancel_ev,
                )

                # 记录步骤与 token 消耗
                bstat = tracker.record(
                    steps=ep_outcome.steps,
                    tokens=ep_outcome.tokens,
                    tool_calls=ep_outcome.tool_calls,
                    agent_id=f"ep_{ep_num}",
                )
                self._emit_budget_events(m, bstat)

                ep.steps = ep_outcome.steps
                ep.tokens = ep_outcome.tokens
                ep.tool_calls = ep_outcome.tool_calls
                ep.run_id = ep_outcome.run_id
                ep.ended_at = time.time()

                # 更新结构化决策/开放问题
                for q in ep_outcome.open_questions:
                    if q and q not in m.open_questions:
                        m.open_questions.append(q)
                for d in ep_outcome.decisions:
                    if d and d not in m.decisions:
                        m.decisions.append(d)

                post_diff, post_diff_hash = await asyncio.to_thread(_workspace_diff_fingerprint, ws)
                ho = Handoff(
                    episode_id=ep_num,
                    summary=ep_outcome.summary or f"Episode #{ep_num} on {ms.id}",
                    completed=[x.id for x in m.milestones if x.status == "done"],
                    next_actions=list(ep_outcome.next_actions),
                    open_questions=list(m.open_questions[-5:]),
                    decisions=list(m.decisions[-5:]),
                    diff_summary=post_diff,
                )
                ep.handoff = ho

                # 处理策略越界阻塞
                if ep_outcome.blocked:
                    blk = ep_outcome.blocked
                    rec = queue_approval(
                        m,
                        milestone_id=ms.id,
                        tool=str(blk.get("tool") or "tool"),
                        target=str(blk.get("target") or ""),
                        reason=str(blk.get("reason") or "outside_allowlist"),
                        sig=str(blk.get("signature") or ""),
                    )
                    ms.status = "blocked"
                    ms.error = rec["reason"]
                    ep.status = "blocked"
                    self._record_timeline(m, "blocked", {"milestone_id": ms.id, "approval": rec})
                    self.store.save(m)
                    self._emit(m, "mission_blocked", {"milestone_id": ms.id, "approval": rec})
                    self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                    continue

                # 若因 pause/cancel/shutdown 提前结束本 Episode
                if self._shutdown_requested or m.id in self._pause_flags:
                    ep.status = "paused"
                    if ms.status == "running":
                        ms.status = "pending"
                    self.store.save(m)
                    self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                    continue

                # 检查 Watchdog 信号（执行中或跨 Episode 无进展）
                stall_sig_dict = ep_outcome.stall_signal
                if stall_sig_dict is None and post_diff_hash == pre_diff_hash and not ep_outcome.milestone_ready:
                    sig = watchdog.observe_step(diff_hash=post_diff_hash, progress_marker=f"{ms.id}:{ms.status}")
                    if sig is not None:
                        stall_sig_dict = sig.to_dict()

                if stall_sig_dict:
                    await self._handle_stall(m, ms, stall_sig_dict, ws=ws, ho=ho)
                    if m.status == "paused":
                        ep.status = "paused"
                        _persist(m, prefer_disk=False)
                        self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                        break

                # 若预算已满 100%，立即保存并暂停
                if bstat.exceeded:
                    ep.status = "paused"
                    if ms.status == "running":
                        ms.status = "pending"
                    m.status = "paused"
                    m.pause_reason = f"budget_exceeded:{bstat.exceeded}"
                    _persist(m, prefer_disk=False)
                    self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                    self._emit(m, "mission_paused", {"reason": m.pause_reason, "budget": bstat.snapshot})
                    break

                # Episode 失败错误处理
                if ep_outcome.error:
                    ms.attempts += 1
                    ms.error = ep_outcome.error
                    ep.status = "failed"
                    ep.error = ep_outcome.error
                    sig = watchdog.observe_verify(False, summary=ep_outcome.error)
                    if sig is not None:
                        await self._handle_stall(m, ms, sig.to_dict(), ws=ws, ho=ho)
                    if m.status == "paused":
                        _persist(m, prefer_disk=False)
                        break
                    if ms.attempts >= ms.max_attempts:
                        ms.status = "failed"
                    else:
                        ms.status = "pending"
                    _persist(m, prefer_disk=False)
                    self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                    continue

                # 执行里程碑验收门控
                if ep_outcome.milestone_ready:
                    manual_ok = self._approved_set(m)
                    if ms.acceptance:
                        all_ok, gres, needs_manual = await run_acceptance_checks(
                            ms.acceptance, ws, guard, cfg=self.cfg, manual_approvals=manual_ok
                        )
                    else:
                        all_ok, gres, needs_manual = True, [], False

                    ms.gate_results = [g.to_dict() for g in gres]
                    self._emit(m, "gate_result", {"milestone_id": ms.id, "ok": all_ok, "results": ms.gate_results})

                    if needs_manual and not all_ok:
                        for g in gres:
                            if g.awaiting_manual and not g.ok:
                                queue_approval(
                                    m,
                                    milestone_id=ms.id,
                                    tool="manual_gate",
                                    target=g.target or g.check_id,
                                    reason="manual_acceptance_required",
                                    sig=g.check_id,
                                )
                        ms.status = "blocked"
                        ep.status = "blocked"
                        _persist(m, prefer_disk=False)
                        self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                        continue

                    sig = watchdog.observe_verify(all_ok, summary=format_gate_failures(gres) if not all_ok else "")
                    if all_ok:
                        watchdog.reset_level()
                        ms.status = "done"
                        ms.error = None
                        ms.completed_at = time.time()
                        ep.status = "completed"
                        if m.policy.get("auto_commit", True) and _is_git_repo(ws):
                            _persist(m, prefer_disk=False)
                            sha = await asyncio.to_thread(_git_commit_milestone, ws, m.id, ms)
                            if sha:
                                ms.commit_sha = sha
                        self._record_timeline(m, "milestone_done", {
                            "milestone_id": ms.id,
                            "title": ms.title,
                            "commit": ms.commit_sha,
                        })
                        self._emit(m, "milestone_done", {
                            "milestone_id": ms.id,
                            "title": ms.title,
                            "commit_sha": ms.commit_sha,
                        })
                    else:
                        ms.attempts += 1
                        ms.error = format_gate_failures(gres) or "acceptance_gate_failed"
                        ep.status = "completed"
                        if sig is not None:
                            await self._handle_stall(m, ms, sig.to_dict(), ws=ws, ho=ho)
                        if m.status == "paused":
                            _persist(m, prefer_disk=False)
                            self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})
                            break
                        ms.status = "failed" if ms.attempts >= ms.max_attempts else "pending"
                else:
                    ep.status = "completed"
                    ms.status = "pending"

                _persist(m, prefer_disk=False)
                self._emit(m, "handoff_saved", {"episode": ep_num, "handoff": ho.to_dict()})

        except asyncio.CancelledError:
            cur = self.store.get(mission_id)
            if cur is not None:
                if self._pause_flags.get(mission_id) == "__cancel__":
                    cur.status = "cancelled"
                    cur.ended_at = time.time()
                elif not self._shutdown_requested and cur.status == "running":
                    cur.status = "paused"
                    cur.pause_reason = self._pause_flags.pop(mission_id, "cancelled")
                # 崩溃/关闭场景：保留 running 以便 --resume-missions 自动续跑
                self.store.save(cur)
        except Exception as e:
            cur = self.store.get(mission_id)
            if cur is not None:
                cur.status = "failed"
                cur.last_error = str(e)
                cur.ended_at = time.time()
                self.store.save(cur)
                self._emit(cur, "mission_done", {"status": "failed", "error": str(e)})
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            self._inhibitor.release()

    @staticmethod
    def _build_swarm_nodes(spec: dict[str, Any], default_goal: str, *, default_role: str) -> list[dict[str, Any]]:
        raw_nodes = spec.get("nodes")
        if isinstance(raw_nodes, list) and raw_nodes:
            nodes: list[dict[str, Any]] = []
            for idx, item in enumerate(raw_nodes):
                if not isinstance(item, dict):
                    continue
                nd = dict(item)
                nd.setdefault("role", default_role)
                if not str(nd.get("goal") or nd.get("prompt") or "").strip():
                    nd["goal"] = default_goal
                nd.setdefault("id", f"m_{idx + 1}_{nd['role']}")
                nodes.append(nd)
            if nodes:
                return nodes
        roles = [str(r).strip() for r in (spec.get("roles") or ()) if str(r).strip()]
        if not roles:
            roles = [default_role]
        return [{"id": f"m_{idx + 1}_{role}", "role": role, "goal": default_goal} for idx, role in enumerate(roles)]

    async def _run_mission_swarm(
        self,
        mission: Mission,
        *,
        goal: str,
        ws: Path,
        spec: dict[str, Any],
        default_role: str,
        cancel_event: asyncio.Event | None = None,
        timeout_s: float = 600.0,
    ) -> Any:
        nodes = self._build_swarm_nodes(spec, goal, default_role=default_role)
        parent_mode = "auto" if mission.mode == "unattended" else "ask"
        sw = self.swarms.create_swarm(
            session_id=mission.session_id or f"mission_{mission.id}",
            goal=goal,
            nodes=nodes,
            workspace=str(ws),
            parent_mode=parent_mode,
            explicit_auto_worker=(mission.mode == "unattended"),
            budget=spec.get("budget") if isinstance(spec.get("budget"), dict) else None,
            mission_id=mission.id,
            start=True,
        )
        wait_done = asyncio.create_task(sw.done_evt.wait())
        waiters: set[asyncio.Task[Any]] = {wait_done}
        if cancel_event is not None:
            waiters.add(asyncio.create_task(cancel_event.wait()))
        try:
            done_tasks, pending = await asyncio.wait(waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not sw.done_evt.is_set() and (not done_tasks or (cancel_event is not None and cancel_event.is_set())):
                await self.swarms.cancel(sw.id)
        except asyncio.CancelledError:
            for t in waiters:
                t.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            await self.swarms.cancel(sw.id)
            raise
        await self.swarms.await_agents(sw.id, timeout_s=0.5)
        return self.swarms.swarms.get(sw.id) or sw

    async def _execute_episode(
        self,
        mission: Mission,
        milestone: Milestone,
        episode: Episode,
        prompt: str,
        *,
        ws: Path,
        policy: MissionPolicy,
        approved_actions: set[str],
        watchdog: StallWatchdog,
        tracker: MissionBudgetTracker,
        cancel_event: asyncio.Event,
    ) -> EpisodeOutcome:
        if (mission.use_swarm or milestone.swarm_spec) and self.swarms is not None:
            if not milestone.swarm_spec and not _is_git_repo(ws) and self.default_episode_runner is not None:
                pass
            else:
                spec = milestone.swarm_spec or {}
                default_role = "coder" if _is_git_repo(ws) else "researcher"
                goal = str(spec.get("goal") or f"{milestone.title}\n{milestone.description}".strip() or prompt)
                try:
                    sw = await self._run_mission_swarm(
                        mission,
                        goal=goal,
                        ws=ws,
                        spec=spec,
                        default_role=default_role,
                        cancel_event=cancel_event,
                    )
                    nodes = list(sw.graph.nodes.values())
                    done_cnt = sum(1 for n in nodes if n.status == "done")
                    totals = sw.budget.swarm_totals() if hasattr(sw.budget, "swarm_totals") else {}
                    tokens = int(totals.get("tokens") or 0)
                    tool_calls = int(totals.get("tool_calls") or 0)
                    steps = max(1, int(totals.get("steps") or 0), len(nodes))
                    ok = sw.status == "done" and bool(nodes) and done_cnt == len(nodes)
                    node_notes = [f"[{n.role}:{n.status}] {(n.result or n.error or '')[:200]}" for n in nodes[:6]]
                    summary = f"Swarm {sw.id}: {sw.status} ({done_cnt}/{len(nodes)})"
                    if node_notes:
                        summary += "\n" + "\n".join(node_notes)
                    return EpisodeOutcome(
                        steps=steps,
                        tokens=tokens,
                        tool_calls=tool_calls,
                        summary=summary,
                        milestone_ready=ok,
                        error=None if ok else f"swarm_{sw.status}",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    return EpisodeOutcome(steps=1, summary=f"Swarm error: {e}", milestone_ready=False, error=str(e))

        if self.default_episode_runner is None:
            return EpisodeOutcome(
                steps=0,
                summary="未配置 Episode 执行器（no episode runner）",
                milestone_ready=False,
                error="no_episode_runner",
            )

        res = await self.default_episode_runner(
            mission=mission,
            milestone=milestone,
            episode=episode,
            prompt=prompt,
            workspace=ws,
            policy=policy,
            approved_actions=approved_actions,
            watchdog=watchdog,
            tracker=tracker,
            cancel_event=cancel_event,
        )
        if isinstance(res, EpisodeOutcome):
            return res
        if isinstance(res, dict):
            return EpisodeOutcome(**{k: v for k, v in res.items() if k in EpisodeOutcome.__dataclass_fields__})
        return EpisodeOutcome(summary=str(res), milestone_ready=True)

    async def _handle_stall(self, m: Mission, ms: Milestone, sig: dict[str, Any], *, ws: Path, ho: Handoff) -> None:
        self._record_timeline(m, "stall_detected", {"milestone_id": ms.id, **sig})
        self._emit(m, "stall_detected", {"milestone_id": ms.id, **sig})
        action = sig.get("action")
        if action == "nudge":
            ho.next_actions.insert(0, f"自省检查：{sig.get('detail')} — 请验证假设并尝试替代方案")
        elif action == "replan":
            note = f"Replan triggered at {ms.id}: {sig.get('detail')}"
            if note not in m.decisions:
                m.decisions.append(note)
            ho.next_actions.insert(0, "强制重规划：拆解当前阻塞问题并更换实现策略")
        elif action == "review":
            advice = await self._run_reviewer(m, ms, sig, ws=ws)
            ho.next_actions.insert(0, f"Reviewer 建议：{advice[:300]}")
        elif action == "pause":
            m.status = "paused"
            m.pause_reason = f"stall_escalated:{sig.get('reason')}"
            if ms.status == "running":
                ms.status = "pending"
            self._emit(m, "mission_paused", {"reason": m.pause_reason, "stall": sig})
            notify_mission(
                f"Mission 停滞暂停: {m.title}",
                f"已尝试自省/重规划/评审仍无进展，请介入处理 ({sig.get('detail')})。",
                quiet_hours=m.policy.get("quiet_hours"),
            )

    async def _run_reviewer(self, m: Mission, ms: Milestone, sig: dict[str, Any], *, ws: Path) -> str:
        if self.review_runner is not None:
            try:
                return str(await self.review_runner(m, ms, sig, ws))
            except Exception as e:
                return f"reviewer_failed: {e}"
        if self.swarms is not None and getattr(self.swarms, "default_runner", None) is not None:
            try:
                sw = await self._run_mission_swarm(
                    m,
                    goal=f"Review stall on milestone {ms.id} ({ms.title}): {sig.get('detail')}",
                    ws=ws,
                    spec={"roles": ["reviewer"]},
                    default_role="reviewer",
                    timeout_s=120.0,
                )
                if sw.graph.nodes:
                    first = next(iter(sw.graph.nodes.values()))
                    return str(first.result or first.error or "reviewer finished")
            except Exception as e:
                return f"swarm_review_failed: {e}"
        return "独立检查建议：请先运行最小复现用例确认根因后再编辑代码。"

    async def _heartbeat_loop(self, mission_id: str, tracker: MissionBudgetTracker) -> None:
        hb_s = float(self.cfg.get("mission", "heartbeat_s", default=10.0)) if self.cfg else 10.0
        hb_s = max(1.0, hb_s)
        while True:
            await asyncio.sleep(hb_s)
            m = self.store.get(mission_id)
            if m is None or m.status != "running":
                break
            bstat = tracker.evaluate()
            m.budget.update(bstat.snapshot)
            self._emit(m, "mission", {"action": "heartbeat", "budget": bstat.snapshot, "mission": m.public_summary()})
            self._emit_budget_events(m, bstat)
            if bstat.exceeded:
                self._pause_flags[mission_id] = f"budget_exceeded:{bstat.exceeded}"
                if mission_id in self._cancel_flags:
                    self._cancel_flags[mission_id].set()
                break

    # ------------------------------------------------------------------ 辅助工具
    def _ready_milestones(self, m: Mission) -> list[Milestone]:
        done_ids = {ms.id for ms in m.milestones if ms.status in ("done", "skipped")}
        blocked_targets = {a.get("milestone_id") for a in m.approvals if a.get("status") == "pending"}
        out: list[Milestone] = []
        for ms in m.milestones:
            if ms.status not in ("pending", "running"):
                continue
            if ms.id in blocked_targets:
                continue
            if all(dep in done_ids for dep in (ms.depends_on or [])):
                out.append(ms)
        return out

    def _approved_set(self, m: Mission) -> set[str]:
        out: set[str] = set()
        for a in m.approvals:
            if a.get("status") == "approved":
                if a.get("signature"):
                    out.add(str(a["signature"]))
                if a.get("target"):
                    out.add(str(a["target"]))
        return out

    def _watchdog_hint_for(self, m: Mission) -> str:
        hist = m.stall_state.get("history") or []
        if not hist:
            return ""
        last = hist[-1]
        return f"action={last.get('action')} reason={last.get('reason')} detail={last.get('detail')}"

    def _recall_memory(self, query: str, workspace: str = "") -> str:
        if self.memory is None or not hasattr(self.memory, "search"):
            return ""
        try:
            hits = self.memory.search(query, k=3, workspace=workspace) or []
            lines: list[str] = []
            for h in hits:
                if not isinstance(h, dict):
                    continue
                txt = str(h.get("text") or h.get("content") or "").strip()
                if txt:
                    lines.append(f"- {txt[:160]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _save_handoff_only(self, m: Mission, summary: str) -> None:
        m.current_episode += 1
        ho = Handoff(
            episode_id=m.current_episode,
            summary=summary,
            completed=[x.id for x in m.milestones if x.status == "done"],
        )
        m.episodes.append(Episode(id=m.current_episode, milestone_id=m.current_milestone_id, status="paused", ended_at=time.time(), handoff=ho))

    def _emit_budget_events(self, m: Mission, bstat: Any) -> None:
        self._emit(m, "mission_budget", {"budget": bstat.snapshot})
        for dim in bstat.warned_now:
            self._record_timeline(m, "budget_warn", {"dimension": dim, "ratio": round(bstat.ratios.get(dim, 0), 3)})
            self._emit(m, "budget_warn", {"dimension": dim, "budget": bstat.snapshot})

    def _record_timeline(self, m: Mission, kind: str, data: dict[str, Any]) -> None:
        m.timeline.append({"ts": time.time(), "kind": kind, **data})
        if len(m.timeline) > 300:
            m.timeline = m.timeline[-300:]

    def _build_guard(self, ws: Path) -> sec.PathGuard:
        prot = [Path(p) for p in (self.cfg.get("security", "protected_paths", default=[]) or [])] if self.cfg else []
        hid = [Path(p) for p in (self.cfg.get("security", "hidden_paths", default=[]) or [])] if self.cfg else []
        return sec.PathGuard(ws, protected=prot, hidden=hid)

    def _emit(self, m: Mission, kind: str, data: dict[str, Any] | None = None) -> None:
        ev = self.store.append_event(m.id, kind, data)
        for q in list(self._live_subs.get(m.id) or ()):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        if self.audit is not None:
            try:
                self.audit.append(event=f"mission:{kind}", mission_id=m.id, **(data or {}))
            except Exception:
                pass
        if self.notify is not None:
            try:
                payload = {"mission_id": m.id, "seq": ev["seq"], **(data or {})}
                self.notify(kind, payload)
            except Exception:
                pass
