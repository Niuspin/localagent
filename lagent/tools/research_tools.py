"""Research Engine 工具集（M5）：research_start / research_status / cite_check。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .. import security as sec
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx


def _get_mgr(ctx: ToolCtx) -> Any:
    mgr = getattr(ctx, "research", None)
    if mgr is None:
        ag = getattr(ctx, "agent", None)
        mgr = getattr(ag, "research", None) if ag is not None else None
    return mgr


async def research_start_tool(ctx: ToolCtx, a: dict) -> ToolResult:
    mgr = _get_mgr(ctx)
    if mgr is None:
        return ToolResult("错误: Research Engine 尚未初始化。", error=True)
    question = str(a.get("question") or "").strip()
    if not question:
        return ToolResult("错误: question 不能为空。", error=True)
    depth = str(a.get("depth") or "standard").strip().lower()
    budget = a.get("budget") if isinstance(a.get("budget"), dict) else None
    urls = [str(u).strip() for u in (a.get("urls") or []) if str(u).strip()] if isinstance(a.get("urls"), list) else None
    try:
        snap = mgr.start_research(question, session_id=ctx.sid, depth=depth, budget=budget, urls=urls)
    except PermissionError as e:
        return ToolResult(f"拒绝: {e}", error=True)
    except ValueError as e:
        return ToolResult(f"错误: {e}", error=True)

    task = snap["task"]
    if a.get("wait"):
        r = mgr.runs.get(task["id"])
        if r and r.task:
            try:
                await asyncio.wait_for(asyncio.shield(r.task), timeout=max(5.0, min(300.0, float(a.get("timeout_s") or 60.0))))
            except asyncio.TimeoutError:
                pass
        snap = mgr.snapshot(task["id"]) or snap
        task = snap["task"]

    out = {
        "research_id": task["id"],
        "status": task["status"],
        "mode": task["mode"],
        "depth": task["depth"],
        "sources": len(snap.get("sources") or []),
        "claims": len(snap.get("claims") or []),
        "blocked_domains": task.get("blocked_domains") or {},
    }
    if task.get("report_md"):
        out["report_preview"] = task["report_md"][:2000]
    return ToolResult(
        json.dumps(out, ensure_ascii=False, indent=2),
        meta={"research_id": task["id"], "status": task["status"]},
        taint="联网调研外部来源" if snap.get("sources") else "",
        untrusted_source=f"research:{task['id']}" if snap.get("sources") else "",
    )


async def research_status_tool(ctx: ToolCtx, a: dict) -> ToolResult:
    mgr = _get_mgr(ctx)
    if mgr is None:
        return ToolResult("错误: Research Engine 尚未初始化。", error=True)
    rid = str(a.get("research_id") or a.get("id") or "").strip()
    if not rid:
        tasks = mgr.evidence.list_tasks(session_id=ctx.sid, limit=10)
        return ToolResult(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2))
    wait_s = max(0.0, min(60.0, float(a.get("wait_s") or 0.0)))
    if wait_s > 0:
        r = mgr.runs.get(rid)
        if r and r.task and not r.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(r.task), timeout=wait_s)
            except asyncio.TimeoutError:
                pass
    snap = mgr.snapshot(rid)
    if not snap:
        return ToolResult(f"错误: 未找到调研任务 {rid}", error=True)
    t = snap["task"]
    payload = {
        "research_id": t["id"],
        "question": t["question"],
        "status": t["status"],
        "sub_questions": t.get("sub_questions") or [],
        "sources": [
            {"ref": s["ref_num"], "domain": s["domain"], "title": s["title"], "url": s["url"], "robots_ok": s["robots_ok"], "cred_score": s["cred_score"]}
            for s in (snap.get("sources") or [])
        ],
        "claims_count": len(snap.get("claims") or []),
        "blocked_domains": t.get("blocked_domains") or {},
        "pending_domains": snap.get("pending_domains") or [],
        "cite_check": t.get("cite_check") or {},
        "report_md": t.get("report_md") or "",
    }
    return ToolResult(
        json.dumps(payload, ensure_ascii=False, indent=2),
        meta={"research_id": rid, "status": t["status"]},
        taint="联网调研外部来源" if snap.get("sources") else "",
        untrusted_source=f"research:{rid}" if snap.get("sources") else "",
    )


async def cite_check_tool(ctx: ToolCtx, a: dict) -> ToolResult:
    from ..research.verify import cite_check

    mgr = _get_mgr(ctx)
    if mgr is None:
        return ToolResult("错误: Research Engine 尚未初始化。", error=True)
    rid = str(a.get("research_id") or a.get("report_id") or "").strip()
    if not rid:
        return ToolResult("错误: 必须指定 research_id / report_id。", error=True)
    bundle = mgr.evidence.get_evidence_bundle(rid)
    if not bundle:
        return ToolResult(f"错误: 调研任务 {rid} 不存在。", error=True)
    report_text = str(a.get("report_text") or bundle["task"].get("report_md") or "").strip()
    if not report_text:
        return ToolResult("错误: 报告文本为空，无法校验引用。", error=True)
    res = cite_check(report_text, bundle["sources"], claims=bundle["claims"])
    return ToolResult(
        json.dumps(res, ensure_ascii=False, indent=2),
        error=not res.get("ok", False),
        meta={"research_id": rid, "cite_ok": res.get("ok")},
    )


register(Tool(
    "research_start",
    "启动安全合规的深度联网调研任务：自动拆题、按策略/robots/限速抓取公开网页、经无工具 reader 隔离抽取证据并交叉验证。",
    obj({
        "question": P("string", "调研问题"),
        "depth": P("string", "调研深度", enum=["quick", "standard", "deep"]),
        "budget": P("object", "可选预算覆盖，如 {max_requests, max_sources}"),
        "urls": P("array", "可选指定种子 URL 列表", items=P("string", "URL")),
        "wait": P("boolean", "是否阻塞等待完成（默认 false，立即返回 research_id）"),
    }, ["question"]),
    research_start_tool,
    "net_search",
    group="research",
    title=lambda a: f"启动调研「{str(a.get('question', ''))[:40]}」",
))

register(Tool(
    "research_status",
    "查询联网调研任务进度、子问题、来源列表、被拦截域名与已生成报告。",
    obj({
        "research_id": P("string", "调研任务 ID（留空则返回当前会话近期任务列表）"),
        "wait_s": P("integer", "可选等待完成的秒数 (0~60)"),
    }),
    research_status_tool,
    "read",
    group="research",
    title=lambda a: f"查看调研 {a.get('research_id', '')}",
))

register(Tool(
    "cite_check",
    "校验调研报告中的每一条 [n] 引文是否能在证据库原文中准确定位，拦截并报告幻觉引用。",
    obj({
        "research_id": P("string", "调研任务 ID / report_id"),
        "report_text": P("string", "待核查的报告正文（留空则检查该任务已生成的 report_md）"),
    }, ["research_id"]),
    cite_check_tool,
    "read",
    group="research",
    title=lambda a: f"引用核查 {a.get('research_id', '')}",
))
