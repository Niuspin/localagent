"""Mission 数据模型与 MISSION.md 双向序列化。"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

MISSION_STATUSES = ("queued", "running", "paused", "blocked", "completed", "failed", "cancelled")
MILESTONE_STATUSES = ("pending", "running", "blocked", "done", "failed", "skipped")
ACTIVE_MISSION_STATUSES = ("queued", "running")


def new_id(prefix: str = "ms") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class AcceptanceCheck:
    id: str = ""
    kind: str = "command"          # command | file_exists | test | manual
    target: str = ""
    expect: str = ""
    timeout_s: float = 60.0
    required: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_id("chk")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AcceptanceCheck":
        return cls(
            id=str(raw.get("id") or new_id("chk")),
            kind=str(raw.get("kind") or "command"),
            target=str(raw.get("target") or ""),
            expect=str(raw.get("expect") or ""),
            timeout_s=float(raw.get("timeout_s") or 60.0),
            required=bool(raw.get("required", True)),
        )


@dataclass
class GateResult:
    check_id: str
    kind: str
    target: str
    ok: bool
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    awaiting_manual: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Milestone:
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceCheck] = field(default_factory=list)
    gate_results: list[dict[str, Any]] = field(default_factory=list)
    commit_sha: str | None = None
    attempts: int = 0
    max_attempts: int = 4
    error: str | None = None
    swarm_spec: dict[str, Any] | None = None
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["acceptance"] = [a.to_dict() for a in self.acceptance]
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Milestone":
        return cls(
            id=str(raw.get("id") or new_id("m")),
            title=str(raw.get("title") or "Untitled Milestone"),
            description=str(raw.get("description") or ""),
            status=str(raw.get("status") or "pending"),
            depends_on=[str(x) for x in (raw.get("depends_on") or [])],
            acceptance=[AcceptanceCheck.from_dict(x) for x in (raw.get("acceptance") or [])],
            gate_results=list(raw.get("gate_results") or []),
            commit_sha=raw.get("commit_sha"),
            attempts=int(raw.get("attempts") or 0),
            max_attempts=int(raw.get("max_attempts") or 4),
            error=raw.get("error"),
            swarm_spec=raw.get("swarm_spec"),
            completed_at=raw.get("completed_at"),
        )


@dataclass
class Handoff:
    episode_id: int
    summary: str
    completed: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    diff_summary: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Handoff":
        return cls(
            episode_id=int(raw.get("episode_id") or 0),
            summary=str(raw.get("summary") or ""),
            completed=[str(x) for x in (raw.get("completed") or [])],
            next_actions=[str(x) for x in (raw.get("next_actions") or [])],
            open_questions=[str(x) for x in (raw.get("open_questions") or [])],
            decisions=[str(x) for x in (raw.get("decisions") or [])],
            diff_summary=str(raw.get("diff_summary") or ""),
            ts=float(raw.get("ts") or time.time()),
        )


@dataclass
class Episode:
    id: int
    milestone_id: str | None = None
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    steps: int = 0
    tokens: int = 0
    tool_calls: int = 0
    run_id: str | None = None
    context_chars: int = 0
    handoff: Handoff | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["handoff"] = self.handoff.to_dict() if self.handoff else None
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Episode":
        ho = raw.get("handoff")
        return cls(
            id=int(raw.get("id") or 0),
            milestone_id=raw.get("milestone_id"),
            status=str(raw.get("status") or "running"),
            started_at=float(raw.get("started_at") or time.time()),
            ended_at=raw.get("ended_at"),
            steps=int(raw.get("steps") or 0),
            tokens=int(raw.get("tokens") or 0),
            tool_calls=int(raw.get("tool_calls") or 0),
            run_id=raw.get("run_id"),
            context_chars=int(raw.get("context_chars") or 0),
            handoff=Handoff.from_dict(ho) if isinstance(ho, dict) else None,
            error=raw.get("error"),
        )


def default_mission_policy(mode: str = "attended") -> dict[str, Any]:
    return {
        "mode": mode if mode in ("attended", "unattended") else "attended",
        "allow_paths": [],
        "allow_commands": [],
        "allow_domains": [],
        "quiet_hours": "",
        "branch": "",
        "auto_commit": True,
    }


def default_mission_budget() -> dict[str, Any]:
    return {
        "wall_s": 14400.0,
        "tokens": 2_000_000,
        "steps": 400,
        "disk_mb": 2048.0,
        "episode_max_steps": 25,
        "used_wall_s": 0.0,
        "used_tokens": 0,
        "used_steps": 0,
        "used_tool_calls": 0,
        "used_disk_mb": 0.0,
        "warned": [],
    }


def default_stall_state() -> dict[str, Any]:
    return {
        "consecutive_failures": 0,
        "no_progress_steps": 0,
        "escalation_level": 0,
        "last_fingerprint": "",
        "fingerprint_count": 0,
        "last_diff_hash": "",
        "history": [],
    }


@dataclass
class Mission:
    id: str
    title: str
    goal: str
    workspace: str = ""
    status: str = "queued"
    mode: str = "attended"
    branch: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    session_id: str | None = None
    milestones: list[Milestone] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    current_milestone_id: str | None = None
    current_episode: int = 0
    acceptance: list[AcceptanceCheck] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    user_notes: str = ""
    policy: dict[str, Any] = field(default_factory=default_mission_policy)
    budget: dict[str, Any] = field(default_factory=default_mission_budget)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    stall_state: dict[str, Any] = field(default_factory=default_stall_state)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    use_swarm: bool = False
    mission_md_sha: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    pause_reason: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "workspace": self.workspace,
            "status": self.status,
            "mode": self.mode,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "base_commit": self.base_commit,
            "session_id": self.session_id,
            "milestones": [m.to_dict() for m in self.milestones],
            "episodes": [e.to_dict() for e in self.episodes],
            "current_milestone_id": self.current_milestone_id,
            "current_episode": self.current_episode,
            "acceptance": [a.to_dict() for a in self.acceptance],
            "open_questions": list(self.open_questions),
            "decisions": list(self.decisions),
            "user_notes": self.user_notes,
            "policy": dict(self.policy),
            "budget": dict(self.budget),
            "approvals": list(self.approvals),
            "stall_state": dict(self.stall_state),
            "timeline": list(self.timeline),
            "use_swarm": bool(self.use_swarm),
            "mission_md_sha": self.mission_md_sha,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "pause_reason": self.pause_reason,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Mission":
        pol = {**default_mission_policy(str(raw.get("mode") or "attended")), **(raw.get("policy") or {})}
        bgt = {**default_mission_budget(), **(raw.get("budget") or {})}
        stl = {**default_stall_state(), **(raw.get("stall_state") or {})}
        return cls(
            id=str(raw.get("id") or new_id("ms")),
            title=str(raw.get("title") or "Untitled Mission"),
            goal=str(raw.get("goal") or ""),
            workspace=str(raw.get("workspace") or ""),
            status=str(raw.get("status") or "queued"),
            mode=str(raw.get("mode") or pol.get("mode") or "attended"),
            branch=raw.get("branch"),
            base_branch=raw.get("base_branch"),
            base_commit=raw.get("base_commit"),
            session_id=raw.get("session_id"),
            milestones=[Milestone.from_dict(x) for x in (raw.get("milestones") or [])],
            episodes=[Episode.from_dict(x) for x in (raw.get("episodes") or [])],
            current_milestone_id=raw.get("current_milestone_id"),
            current_episode=int(raw.get("current_episode") or 0),
            acceptance=[AcceptanceCheck.from_dict(x) for x in (raw.get("acceptance") or [])],
            open_questions=[str(x) for x in (raw.get("open_questions") or [])],
            decisions=[str(x) for x in (raw.get("decisions") or [])],
            user_notes=str(raw.get("user_notes") or ""),
            policy=pol,
            budget=bgt,
            approvals=list(raw.get("approvals") or []),
            stall_state=stl,
            timeline=list(raw.get("timeline") or []),
            use_swarm=bool(raw.get("use_swarm", False)),
            mission_md_sha=str(raw.get("mission_md_sha") or ""),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            started_at=raw.get("started_at"),
            ended_at=raw.get("ended_at"),
            pause_reason=raw.get("pause_reason"),
            last_error=raw.get("last_error"),
        )

    def milestone(self, mid: str | None) -> Milestone | None:
        if not mid:
            return None
        for m in self.milestones:
            if m.id == mid:
                return m
        return None

    def latest_handoff(self) -> Handoff | None:
        for ep in reversed(self.episodes):
            if ep.handoff is not None:
                return ep.handoff
        return None

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [a for a in self.approvals if a.get("status") == "pending"]

    def progress(self) -> dict[str, int]:
        total = len(self.milestones)
        done = sum(1 for m in self.milestones if m.status == "done")
        blocked = sum(1 for m in self.milestones if m.status == "blocked")
        return {"done": done, "total": total, "blocked": blocked}

    def public_summary(self) -> dict[str, Any]:
        prog = self.progress()
        b = self.budget
        ratios = [
            float(b.get("used_wall_s", 0)) / max(1.0, float(b.get("wall_s", 1))),
            float(b.get("used_tokens", 0)) / max(1.0, float(b.get("tokens", 1))),
            float(b.get("used_steps", 0)) / max(1.0, float(b.get("steps", 1))),
            float(b.get("used_disk_mb", 0)) / max(1.0, float(b.get("disk_mb", 1))),
        ]
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "workspace": self.workspace,
            "status": self.status,
            "mode": self.mode,
            "branch": self.branch,
            "session_id": self.session_id,
            "current_milestone_id": self.current_milestone_id,
            "current_episode": self.current_episode,
            "progress": prog,
            "budget_ratio": round(min(1.0, max(ratios)), 4),
            "pending_approvals": len(self.pending_approvals()),
            "pause_reason": self.pause_reason,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


# ------------------------------------------------------------------ MISSION.md 渲染与解析
_SEC_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_MS_LINE_RE = re.compile(
    r"^-\s*\[(?P<mark>[ xX\-!])\]\s*(?:`?(?P<id>m\d+|[A-Za-z0-9_-]+)`?:\s*)?(?P<title>.+?)"
    r"(?:\s*<!--\s*(?P<meta>.*?)-->)?\s*$"
)


def _split_sections(md: str) -> dict[str, str]:
    matches = list(_SEC_RE.finditer(md or ""))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[name] = md[start:end].strip()
    return out


def _bullets(block: str) -> list[str]:
    items: list[str] = []
    for line in block.splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            txt = s[2:].strip()
            if txt and txt != "(none)":
                items.append(txt)
    return items


def render_mission_md(m: Mission) -> str:
    """将 Mission 渲染为可读可手改的 MISSION.md。"""
    lines = [
        f"# Mission: {m.title}",
        "",
        f"> ID: `{m.id}` | Status: `{m.status}` | Mode: `{m.mode}` | Branch: `{m.branch or '-'}` | Episode: `{m.current_episode}`",
        "",
        "## Goal",
        m.goal.strip() or "(empty)",
        "",
        "## Milestones",
    ]
    if not m.milestones:
        lines.append("- [ ] m1: Execute goal")
    else:
        for ms in m.milestones:
            mark = "x" if ms.status == "done" else ("!" if ms.status == "blocked" else ("-" if ms.status == "skipped" else " "))
            meta_parts: list[str] = []
            if ms.depends_on:
                meta_parts.append(f"depends:{','.join(ms.depends_on)}")
            if ms.commit_sha:
                meta_parts.append(f"commit:{ms.commit_sha[:12]}")
            if ms.status in ("running", "failed"):
                meta_parts.append(f"status:{ms.status}")
            meta = f" <!-- {' '.join(meta_parts)} -->" if meta_parts else ""
            lines.append(f"- [{mark}] {ms.id}: {ms.title}{meta}")
            if ms.description:
                lines.append(f"  {ms.description.strip()}")

    lines.extend(["", "## Acceptance"])
    if not m.acceptance and all(not ms.acceptance for ms in m.milestones):
        lines.append("- (none)")
    else:
        for chk in m.acceptance:
            exp = f" (expect: {chk.expect})" if chk.expect else ""
            lines.append(f"- [{chk.kind}] `{chk.target}`{exp}")
        for ms in m.milestones:
            for chk in ms.acceptance:
                exp = f" (expect: {chk.expect})" if chk.expect else ""
                lines.append(f"- ({ms.id}) [{chk.kind}] `{chk.target}`{exp}")

    lines.extend(["", "## Open Questions"])
    lines.extend([f"- {q}" for q in m.open_questions] if m.open_questions else ["- (none)"])

    lines.extend(["", "## Decisions"])
    lines.extend([f"- {d}" for d in m.decisions] if m.decisions else ["- (none)"])

    ho = m.latest_handoff()
    lines.extend(["", "## Latest Handoff"])
    if ho is None:
        lines.append("- (no handoff yet)")
    else:
        lines.append(f"### Episode {ho.episode_id}")
        lines.append(ho.summary.strip() or "(no summary)")
        if ho.next_actions:
            lines.append("")
            lines.append("**Next Actions:**")
            lines.extend(f"- {x}" for x in ho.next_actions)
        if ho.diff_summary:
            lines.append("")
            lines.append("**Git Diff Summary:**")
            lines.append("```text")
            lines.append(ho.diff_summary.strip())
            lines.append("```")

    lines.extend(["", "## User Notes"])
    lines.append(m.user_notes.strip() if m.user_notes.strip() else "<!-- 在此添加补充说明；下次 Episode 启动前会自动读取 -->")
    lines.append("")
    return "\n".join(lines)


def apply_mission_md_edits(m: Mission, md_text: str) -> bool:
    """将用户对 MISSION.md 的修改同步回 Mission；返回是否有结构变更。"""
    secs = _split_sections(md_text)
    changed = False

    if "goal" in secs:
        new_goal = secs["goal"].strip()
        if new_goal and new_goal != "(empty)" and new_goal != m.goal:
            m.goal = new_goal
            changed = True

    if "milestones" in secs:
        existing = {ms.id: ms for ms in m.milestones}
        parsed: list[Milestone] = []
        raw_lines = secs["milestones"].splitlines()
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i].strip()
            match = _MS_LINE_RE.match(line)
            if not match:
                i += 1
                continue
            mid = match.group("id") or f"m{len(parsed) + 1}"
            title = match.group("title").strip()
            mark = match.group("mark")
            meta_raw = match.group("meta") or ""
            desc_lines: list[str] = []
            j = i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if _MS_LINE_RE.match(nxt.strip()):
                    break
                if nxt.strip():
                    desc_lines.append(nxt.strip())
                j += 1
            i = j
            desc = "\n".join(desc_lines)
            ms = existing.get(mid)
            if ms is None:
                depends: list[str] = []
                dm = re.search(r"depends:([^\s>]+)", meta_raw)
                if dm:
                    depends = [x.strip() for x in dm.group(1).split(",") if x.strip()]
                status = "done" if mark.lower() == "x" else ("skipped" if mark == "-" else "pending")
                ms = Milestone(id=mid, title=title, description=desc, status=status, depends_on=depends)
                changed = True
            else:
                if title and title != ms.title:
                    ms.title = title
                    changed = True
                if desc and desc != ms.description:
                    ms.description = desc
                    changed = True
                if mark.lower() == "x" and ms.status != "done":
                    ms.status = "done"
                    ms.completed_at = ms.completed_at or time.time()
                    changed = True
                elif mark == " " and ms.status in ("done", "skipped", "failed", "blocked"):
                    # 用户手动取消勾选，允许重新执行该里程碑
                    ms.status = "pending"
                    ms.error = None
                    changed = True
                elif mark == "-" and ms.status != "skipped":
                    ms.status = "skipped"
                    changed = True
            parsed.append(ms)
        if parsed and ([x.id for x in parsed] != [x.id for x in m.milestones] or changed):
            m.milestones = parsed
            changed = True

    if "open questions" in secs:
        qs = _bullets(secs["open questions"])
        if qs != m.open_questions:
            m.open_questions = qs
            changed = True

    if "decisions" in secs:
        ds = _bullets(secs["decisions"])
        if ds != m.decisions:
            m.decisions = ds
            changed = True

    if "user notes" in secs:
        notes = secs["user notes"].strip()
        placeholder = "<!-- 在此添加补充说明；下次 Episode 启动前会自动读取 -->"
        if notes == placeholder:
            notes = ""
        if notes != m.user_notes:
            m.user_notes = notes
            changed = True

    return changed


def default_milestones_from_goal(goal: str) -> list[Milestone]:
    """若创建时未显式提供 milestones，根据目标文本拆分出合理的默认里程碑。"""
    raw = (goal or "").strip()
    if not raw:
        return [Milestone(id="m1", title="Execute mission goal", description="")]
    lines = [
        re.sub(r"^(?:\d+[.)]\s*|[-*]\s+|\[[ xX]?\]\s*)", "", ln).strip()
        for ln in raw.splitlines()
        if ln.strip()
    ]
    # 仅在明显多条清单时拆分为多阶段里程碑；否则创建单阶段里程碑 + 验证阶段
    bullet_like = sum(1 for ln in raw.splitlines() if re.match(r"^\s*(?:\d+[.)]\s+|[-*]\s+)", ln))
    if len(lines) >= 2 and bullet_like >= 2:
        out: list[Milestone] = []
        for idx, line in enumerate(lines[:12], start=1):
            deps = [f"m{idx - 1}"] if idx > 1 else []
            out.append(Milestone(id=f"m{idx}", title=line[:120], description=line, depends_on=deps))
        return out
    return [Milestone(id="m1", title=raw.splitlines()[0][:120], description=raw)]
