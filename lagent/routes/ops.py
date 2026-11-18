"""扩展运行时路由：会话分支（Fork）、运行中实时转向（Mid-Turn Steer）、MCP Resources/Prompts 查询。"""
from __future__ import annotations

from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


def register_ops_routes(
    routes: list[Route],
    st: Any,
    *,
    sess_or_404: Callable[[Request], Any],
    jbody: Callable[[Request], Any],
    json_res: Callable[..., Response],
    bad: Callable[..., Response],
) -> None:
    def add(path: str, methods: list[str], fn: Callable[[Request], Any]) -> None:
        routes.append(Route(path, fn, methods=methods))

    async def fork_session(req: Request) -> Response:
        src = sess_or_404(req)
        d = await jbody(req) if req.headers.get("content-length") else {}
        at_idx = d.get("from_index")
        forked = st.store.fork(
            src.id,
            at_ui_index=int(at_idx) if at_idx is not None else None,
            title=d.get("title"),
        )
        if forked is None:
            return bad("源会话不存在", 404)
        st.audit.append(event="session_fork", session=forked.id, parent=src.id, from_index=at_idx)
        return json_res(forked.view(), 201)

    async def steer_run(req: Request) -> Response:
        d = await jbody(req)
        txt = str(d.get("text") or "").strip()
        if not txt:
            return bad("转向内容 text 不能为空", 400)
        rid = req.path_params["rid"]
        ok = st.agent.steer_run(rid, txt)
        if not ok:
            return bad("目标运行不存在或已结束", 409)
        st.audit.append(event="run_steer", run_id=rid, preview=txt[:120])
        return json_res({"ok": True, "run_id": rid})

    async def mcp_catalog(req: Request) -> Response:
        return json_res({
            "resources": st.mcp.list_all_resources() if hasattr(st.mcp, "list_all_resources") else [],
            "prompts": st.mcp.list_all_prompts() if hasattr(st.mcp, "list_all_prompts") else [],
        })

    add("/api/sessions/{sid}/fork", ["POST"], fork_session)
    add("/api/runs/{rid}/steer", ["POST"], steer_run)
    add("/api/mcp/catalog", ["GET"], mcp_catalog)
