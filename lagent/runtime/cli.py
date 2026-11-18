"""无头 CLI 模式与 ACP (Agent Client Protocol) JSON-RPC stdio 桥接器。"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


async def run_oneshot_prompt(st: Any, prompt: str, *, output_format: str = "text") -> int:
    """运行单次无头对话并输出完整回复或 JSON 事件流，结束后优雅释放引擎与外部 MCP。"""
    try:
        sess = st.store.create(st.cfg.get("workspace", default="") or "")
        run = st.agent.start_run(sess, prompt, [], [], [])
        idx = 0
        out_chunks: list[str] = []
        status = "done"
        err: str | None = None
        while True:
            while idx < len(run.events):
                _eid, ev, data = run.events[idx]
                idx += 1
                if output_format == "json":
                    print(json.dumps({"event": ev, "data": data}, ensure_ascii=False), flush=True)
                elif ev == "text_delta":
                    txt = str(data.get("text") or "")
                    out_chunks.append(txt)
                    print(txt, end="", flush=True)
                elif ev == "run_end":
                    status = str(data.get("status") or "done")
                    err = data.get("error")
            if run.finished and idx >= len(run.events):
                break
            async with run.cond:
                if idx >= len(run.events) and not run.finished:
                    try:
                        await asyncio.wait_for(run.cond.wait(), 5.0)
                    except asyncio.TimeoutError:
                        pass
        if run.task is not None and not run.task.done():
            await run.task
        if output_format != "json":
            print("", flush=True)
            if err:
                print(f"[Error] {err}", file=sys.stderr, flush=True)
        return 0 if (status == "done" and not err) else 1
    finally:
        try:
            await st.mcp.stop_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            await st.engine.stop()
        except Exception:  # noqa: BLE001
            pass


async def run_acp_stdio(st: Any) -> int:
    """轻量 ACP JSON-RPC 2.0 stdio 模式（支持 initialize / session/create / prompt / steer / cancel / shutdown）。"""
    active_sessions: dict[str, Any] = {}
    write_lock = asyncio.Lock()

    async def send_msg(obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        async with write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    async def pump_run(run: Any, sid: str) -> None:
        idx = 0
        while True:
            while idx < len(run.events):
                _eid, ev, data = run.events[idx]
                idx += 1
                await send_msg({"jsonrpc": "2.0", "method": f"run/{ev}", "params": {"session_id": sid, **data}})
            if run.finished and idx >= len(run.events):
                break
            async with run.cond:
                if idx >= len(run.events) and not run.finished:
                    try:
                        await asyncio.wait_for(run.cond.wait(), 5.0)
                    except asyncio.TimeoutError:
                        pass

    try:
        while True:
            raw_line = await asyncio.to_thread(sys.stdin.readline)
            if not raw_line:
                break
            s = raw_line.strip()
            if not s:
                continue
            try:
                req = json.loads(s)
            except ValueError:
                await send_msg({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            rid = req.get("id")
            method = str(req.get("method") or "")
            params = req.get("params") or {}
            try:
                if method == "initialize":
                    await send_msg({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "name": "LocalAgent",
                            "protocol": "acp-jsonrpc-1",
                            "capabilities": {"prompt": True, "steer": True, "cancel": True, "streaming": True},
                        },
                    })
                elif method == "session/create":
                    ws = str(params.get("workspace") or st.cfg.get("workspace", default="") or "")
                    sess = st.store.create(ws)
                    active_sessions[sess.id] = sess
                    await send_msg({"jsonrpc": "2.0", "id": rid, "result": {"session_id": sess.id, "workspace": sess.workspace}})
                elif method == "prompt":
                    sid = str(params.get("session_id") or "")
                    sess = active_sessions.get(sid) or st.store.get(sid)
                    if sess is None:
                        sess = st.store.create(st.cfg.get("workspace", default="") or "")
                        active_sessions[sess.id] = sess
                    run = st.agent.start_run(
                        sess,
                        str(params.get("text") or ""),
                        list(params.get("images") or []),
                        list(params.get("files") or []),
                        list(params.get("skills") or []),
                    )
                    asyncio.create_task(pump_run(run, sess.id))
                    await send_msg({"jsonrpc": "2.0", "id": rid, "result": {"run_id": run.id, "session_id": sess.id}})
                elif method == "steer":
                    ok = st.agent.steer_run(str(params.get("run_id") or ""), str(params.get("text") or ""))
                    await send_msg({"jsonrpc": "2.0", "id": rid, "result": {"ok": ok}})
                elif method == "cancel":
                    ok = st.agent.cancel(str(params.get("run_id") or ""))
                    await send_msg({"jsonrpc": "2.0", "id": rid, "result": {"ok": ok}})
                elif method == "shutdown":
                    await send_msg({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
                    break
                else:
                    await send_msg({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            except Exception as e:  # noqa: BLE001
                await send_msg({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}})
        return 0
    finally:
        await st.mcp.stop_all()
        await st.engine.stop()
