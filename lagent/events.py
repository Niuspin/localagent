"""统一事件与审计上下文契约（M0）。

所有运行期 SSE 事件（Run.emit / State.broadcast）与审计日志共用统一的上下文元数据字段：
`ts`, `kind`, `run_id`, `agent_id`, `swarm_id`, `mission_id`（外加可选 `parent_id`）。
既不改变 Run.emit 的事件编号（Last-Event-ID 序列严格保持一致），也不破坏旧前端或既有审计校验。
"""
from __future__ import annotations

import time
from typing import Any

EVENT_SCHEMA_VERSION = 1

EVENT_KINDS: tuple[str, ...] = (
    # ---- 既有运行期与全局事件 ----
    "token",
    "think",
    "tool_start",
    "tool_delta",
    "tool_done",
    "tool_result",
    "tool_call",
    "approval",
    "question",
    "todo",
    "todos",
    "usage",
    "status",
    "notice",
    "error",
    "run_end",
    "subagent_step",
    "compact",
    "session_title",
    "engine",
    "download",
    "mcp",
    # ---- v0.2.0 预留扩展事件 ----
    "agent_spawned",
    "agent_state",
    "agent_progress",
    "agent_message",
    "agent_merged",
    "agent_done",
    "swarm",
    "mission",
    "mission_started",
    "mission_episode",
    "episode_started",
    "handoff_saved",
    "milestone_done",
    "gate_result",
    "stall_detected",
    "mission_blocked",
    "budget_warn",
    "mission_checkpoint",
    "mission_rollback",
    "mission_paused",
    "mission_resumed",
    "mission_budget",
    "mission_done",
    "research_query",
    "research_fetch",
    "research_blocked",
    "research_plan",
    "research_done",
    "artifact_created",
)


def enrich_event(
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    agent_id: str | None = None,
    swarm_id: str | None = None,
    mission_id: str | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """返回浅拷贝后的事件负载，补充统一元数据字段（不覆盖调用方已显式提供的同名字段）。"""
    out = dict(data) if isinstance(data, dict) else {"value": data}
    out.setdefault("ts", round(ts if ts is not None else time.time(), 3))
    out.setdefault("kind", kind)
    out.setdefault("run_id", run_id)
    out.setdefault("agent_id", agent_id)
    out.setdefault("swarm_id", swarm_id)
    out.setdefault("mission_id", mission_id)
    return out
