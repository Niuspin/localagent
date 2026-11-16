"""Swarm 集群编排工具：非阻塞派生（spawn_agent / swarm_plan）、等待汇合（await_agents）与控制。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .. import security as sec
from ..swarm.roles import ROLE_SPECS, get_role
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx


def err(msg: str) -> ToolResult:
    return ToolResult(f"错误: {msg}", error=True)


def _get_manager(ctx: ToolCtx):
    mgr = getattr(ctx, "swarms", None)
    if mgr is None and getattr(ctx, "agent", None) is not None:
        mgr = getattr(ctx.agent, "swarms", None)
    return mgr


def _active_swarm_id(ctx: ToolCtx, explicit_id: str = "") -> str:
    if explicit_id:
        return explicit_id.strip()
    return str(getattr(ctx.session, "swarm_id", "") or "")


def _workspace_path(ctx: ToolCtx) -> str:
    g = getattr(ctx, "guard", None)
    if g is None:
        return ""
    ws = getattr(g, "ws", None)
    return str(ws) if isinstance(ws, Path) and ws.exists() else ""


def _parent_mode(ctx: ToolCtx) -> str:
    agent = getattr(ctx, "agent", None)
    run = getattr(ctx, "run", None)
    if agent and run and hasattr(agent, "store"):
        s = agent.store.get(ctx.sid)
        if s is not None:
            return agent._session_mode(s)
    return str(ctx.cfg.get("agent", "permission_mode", default="ask"))


async def spawn_agent_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    """非阻塞派生一个 Swarm 代理；若显式传 blocking=true 或未启用 Swarm 则回退为同步 delegate_task。"""
    mgr = _get_manager(ctx)
    blocking = a.get("blocking")
    has_swarm_shape = bool(a.get("role") or a.get("goal") or a.get("deps") or a.get("swarm_id") or blocking is False)

    if mgr is None or blocking is True or (not has_swarm_shape and a.get("prompt")):
        from .agentic import delegate_task
        return await delegate_task(ctx, a)

    depth = int(getattr(ctx, "depth", getattr(ctx.session, "depth", 0)) or 0)
    max_depth = int(ctx.cfg.get("swarm", "max_depth", default=2) or 2)
    if depth >= max_depth:
        return err(f"子代理递归深度已达 swarm.max_depth={max_depth}")

    role = str(a.get("role") or a.get("mode") or "researcher").strip().lower()
    goal = str(a.get("goal") or a.get("prompt") or "").strip()
    if not goal:
        return err("缺少 goal 或 prompt 参数")

    node_spec: dict[str, Any] = {
        "id": str(a.get("id") or f"{get_role(role).name}_{uuid.uuid4().hex[:6]}"),
        "role": role,
        "goal": goal,
        "deps": [str(x) for x in (a.get("deps") or [])],
        "budget": dict(a.get("budget") or {}),
        "apply": bool(a.get("apply", True)),
        "parent": getattr(ctx.session, "agent_id", "main") or "main",
    }
    if a.get("max_steps"):
        node_spec["budget"]["steps"] = max(1, min(60, int(a["max_steps"])))

    ws = _workspace_path(ctx)
    sid = _active_swarm_id(ctx, str(a.get("swarm_id") or ""))
    try:
        if sid and sid in mgr.swarms:
            node = mgr.spawn_node(sid, node_spec, parent_depth=depth)
            sw_id = sid
        else:
            sw = mgr.create_swarm(
                session_id=ctx.sid,
                goal=goal[:120],
                nodes=[node_spec],
                workspace=ws,
                parent_mode=_parent_mode(ctx),
                explicit_auto_worker=bool(a.get("explicit_auto_worker", False)),
            )
            sw_id = sw.id
            ctx.session.swarm_id = sw_id  # type: ignore[attr-defined]
            node = next(iter(sw.graph.nodes.values()))
    except (ValueError, KeyError) as e:
        return err(str(e))

    return ToolResult(json.dumps({
        "swarm_id": sw_id,
        "agent_id": node.id,
        "role": node.role,
        "status": node.status,
    }, ensure_ascii=False))


async def swarm_plan_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    mgr = _get_manager(ctx)
    if mgr is None:
        return err("Swarm 调度器未初始化")
    nodes = a.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return err("nodes 必须是非空任务节点列表")

    goal = str(a.get("goal") or "多代理协作任务").strip()
    ws = _workspace_path(ctx)
    try:
        sw = mgr.create_swarm(
            session_id=ctx.sid,
            goal=goal,
            nodes=nodes,
            workspace=ws,
            parent_mode=_parent_mode(ctx),
            explicit_auto_worker=bool(a.get("explicit_auto_worker", False)),
            budget=dict(a.get("budget") or {}) or None,
        )
        ctx.session.swarm_id = sw.id  # type: ignore[attr-defined]
    except (ValueError, KeyError) as e:
        return err(str(e))

    return ToolResult(json.dumps({
        "swarm_id": sw.id,
        "nodes": [{"id": n.id, "role": n.role, "deps": n.deps, "status": n.status} for n in sw.graph.nodes.values()],
    }, ensure_ascii=False))


async def await_agents_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    mgr = _get_manager(ctx)
    if mgr is None:
        return err("Swarm 调度器未初始化")
    sw_id = _active_swarm_id(ctx, str(a.get("swarm_id") or ""))
    if not sw_id:
        return err("未指定 swarm_id，且当前会话无活动 Swarm")
    ids = [str(x) for x in (a.get("ids") or [])] or None
    timeout_s = max(1.0, min(1800.0, float(a.get("timeout_s") or 120.0)))
    res = await mgr.await_agents(sw_id, ids, timeout_s=timeout_s)
    if res.get("tainted"):
        ctx.session.tainted = True
        nonce = getattr(ctx.session, "nonce", "") or "swarm"
        for ag in (res.get("agents") or {}).values():
            if ag.get("tainted") and ag.get("result") and "<untrusted " not in ag["result"]:
                ag["result"] = sec.wrap_untrusted(ag["result"], "swarm:agent", nonce)
    return ToolResult(
        json.dumps(res, ensure_ascii=False, indent=2),
        taint="Swarm 子代理读取过不可信内容" if res.get("tainted") else None,
    )


async def agent_status_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    mgr = _get_manager(ctx)
    if mgr is None:
        return err("Swarm 调度器未初始化")
    sw_id = _active_swarm_id(ctx, str(a.get("swarm_id") or ""))
    if not sw_id:
        return ToolResult(json.dumps({"swarms": mgr.list_swarms(session_id=ctx.sid)}, ensure_ascii=False))
    snap = mgr.snapshot(sw_id)
    if not snap:
        return err(f"未找到 Swarm: {sw_id}")
    aid = str(a.get("id") or "").strip()
    if aid:
        nodes = {n["id"]: n for n in snap.get("graph", {}).get("nodes", [])}
        if aid not in nodes:
            return err(f"未找到代理: {aid}")
        return ToolResult(json.dumps({"swarm_id": sw_id, "agent": nodes[aid]}, ensure_ascii=False))
    return ToolResult(json.dumps(snap, ensure_ascii=False))


async def cancel_agent_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    mgr = _get_manager(ctx)
    if mgr is None:
        return err("Swarm 调度器未初始化")
    sw_id = _active_swarm_id(ctx, str(a.get("swarm_id") or ""))
    aid = str(a.get("id") or "").strip()
    if not sw_id or not aid:
        return err("需要提供 swarm_id（或当前会话活动 Swarm）与 id")
    affected = await mgr.cancel_agent(sw_id, aid)
    return ToolResult(json.dumps({"swarm_id": sw_id, "cancelled": affected}, ensure_ascii=False))


async def send_message_tool(ctx: ToolCtx, a: dict[str, Any]) -> ToolResult:
    mgr = _get_manager(ctx)
    if mgr is None:
        return err("Swarm 调度器未初始化")
    sw_id = _active_swarm_id(ctx, str(a.get("swarm_id") or ""))
    to = str(a.get("to") or "").strip()
    text = str(a.get("text") or "").strip()
    kind = str(a.get("kind") or "handoff").strip()
    if not sw_id or not to or not text:
        return err("需要提供 swarm_id、to 与 text")
    msg = mgr.send_message(sw_id, to, text, from_agent=getattr(ctx.session, "agent_id", "main") or "main", kind=kind)
    return ToolResult(json.dumps(msg, ensure_ascii=False))


_ROLE_NAMES = list(ROLE_SPECS.keys()) + ["worker", "explore"]

register(Tool(
    "spawn_agent",
    "非阻塞派生独立子代理并立即返回 swarm_id 与 agent_id；若传 blocking=true 或兼容旧 mode/prompt 调用则同步等待返回。",
    obj({
        "role": P("string", "角色：planner / researcher / coder / reviewer / verifier / summarizer / reader", enum=_ROLE_NAMES),
        "goal": P("string", "子代理需要完成的明确目标"),
        "prompt": P("string", "兼容参数（等同 goal）"),
        "mode": P("string", "兼容参数：explore / plan / verify / worker", enum=["explore", "plan", "verify", "worker"]),
        "deps": {"type": "array", "items": {"type": "string"}, "description": "前置依赖 agent_id 列表"},
        "swarm_id": P("string", "可选：追加到已有 Swarm"),
        "max_steps": P("integer", "步数上限"),
        "apply": P("boolean", "coder/worker 结束后是否通过 MergeQueue 合回主工作区"),
        "blocking": P("boolean", "是否阻塞等待完成（默认 false 非阻塞；旧式仅传 prompt+mode 时同步）"),
    }, []),
    spawn_agent_tool,
    "read",
    group="swarm",
    title=lambda a: f"派生代理({a.get('role') or a.get('mode') or 'researcher'}): {(a.get('goal') or a.get('prompt') or '')[:36]}",
))

register(Tool(
    "swarm_plan",
    "一次性提交多代理 DAG 协同计划（含角色、目标与 deps 依赖边），调度器按硬件预算并发执行。",
    obj({
        "goal": P("string", "Swarm 总目标"),
        "nodes": {
            "type": "array",
            "description": "任务节点数组，每项包含 id/role/goal/deps",
            "items": {
                "type": "object",
                "properties": {
                    "id": P("string", "可选节点 ID"),
                    "role": P("string", "角色", enum=_ROLE_NAMES),
                    "goal": P("string", "该节点具体目标"),
                    "deps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["role", "goal"],
            },
        },
        "budget": {"type": "object", "description": "可选总预算 {steps, wall_s, tokens}"},
    }, ["nodes"]),
    swarm_plan_tool,
    "read",
    group="swarm",
    title=lambda a: f"Swarm DAG 编排 ({len(a.get('nodes') or [])} 节点)",
))

register(Tool(
    "await_agents",
    "等待指定（或当前 Swarm 全部）子代理完成并返回摘要、产物与 taint 状态。",
    obj({
        "swarm_id": P("string", "Swarm ID（省略时取当前会话关联的 Swarm）"),
        "ids": {"type": "array", "items": {"type": "string"}, "description": "等待的 agent_id 列表（省略则等全部）"},
        "timeout_s": P("number", "最长等待秒数（默认 120）"),
    }, []),
    await_agents_tool,
    "read",
    group="swarm",
    title=lambda a: f"等待 Swarm 汇合 ({len(a.get('ids') or []) or 'all'})",
))

register(Tool(
    "agent_status",
    "查看 Swarm 或单个子代理的最新执行状态、进度与预算使用情况。",
    obj({
        "swarm_id": P("string", "Swarm ID（可选）"),
        "id": P("string", "单个 agent_id（可选）"),
    }, []),
    agent_status_tool,
    "read",
    group="swarm",
    title=lambda a: f"查询 Swarm 状态 {a.get('id') or a.get('swarm_id') or ''}".strip(),
))

register(Tool(
    "cancel_agent",
    "取消指定子代理及其级联依赖下游节点。",
    obj({
        "swarm_id": P("string", "Swarm ID（省略时取当前会话关联的 Swarm）"),
        "id": P("string", "要取消的 agent_id"),
    }, ["id"]),
    cancel_agent_tool,
    "read",
    group="swarm",
    title=lambda a: f"取消子代理 {a.get('id', '')}",
))

register(Tool(
    "send_message",
    "向 Swarm 中指定代理（或 '*' 广播）发送类型化邮箱消息。",
    obj({
        "swarm_id": P("string", "Swarm ID（省略时取当前会话关联的 Swarm）"),
        "to": P("string", "接收者 agent_id 或 '*'"),
        "text": P("string", "消息正文"),
        "kind": P("string", "消息类型", enum=["handoff", "question", "result", "alert", "cancel"]),
    }, ["to", "text"]),
    send_message_tool,
    "read",
    group="swarm",
    title=lambda a: f"发送消息给 {a.get('to', '')}",
))
