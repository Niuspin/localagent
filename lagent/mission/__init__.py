"""LocalAgent Mission 运行时包：分段自主执行、验收门控、停滞侦测、预算与电源守卫。"""
from __future__ import annotations

from .budget import BudgetStatus, MissionBudgetTracker
from .gates import format_gate_failures, run_acceptance_checks, run_single_check
from .model import (
    MISSION_STATUSES,
    MILESTONE_STATUSES,
    AcceptanceCheck,
    Episode,
    GateResult,
    Handoff,
    Milestone,
    Mission,
    apply_mission_md_edits,
    default_milestones_from_goal,
    render_mission_md,
)
from .policy import MissionPolicy, PolicyVerdict, queue_approval
from .power import SleepInhibitor, in_quiet_hours, notify_mission
from .runner import EpisodeOutcome, MissionManager, build_episode_prompt
from .store import MissionStore
from .watchdog import StallSignal, StallWatchdog

__all__ = [
    "AcceptanceCheck",
    "BudgetStatus",
    "Episode",
    "EpisodeOutcome",
    "GateResult",
    "Handoff",
    "MILESTONE_STATUSES",
    "MISSION_STATUSES",
    "Milestone",
    "Mission",
    "MissionBudgetTracker",
    "MissionManager",
    "MissionPolicy",
    "MissionStore",
    "PolicyVerdict",
    "SleepInhibitor",
    "StallSignal",
    "StallWatchdog",
    "apply_mission_md_edits",
    "build_episode_prompt",
    "default_milestones_from_goal",
    "format_gate_failures",
    "in_quiet_hours",
    "notify_mission",
    "queue_approval",
    "render_mission_md",
    "run_acceptance_checks",
    "run_single_check",
]
