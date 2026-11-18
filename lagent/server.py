"""HTTP 服务（Starlette）：只监听本机；Cookie 令牌鉴权 + Host/Origin 校验 + 严格 CSP。接口契约见 docs/API.md。"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, appwindow, gguf, hub as hub_mod, hw, metrics as metrics_mod
from .paths import FROZEN
from . import security as sec
from .artifacts import ArtifactStore
from .commands import list_commands
from .config import BIN_DIR, CODE_DIR, DATA_DIR, MODELS_DIR, ROOT, SKILLS_DIR, WEB_DIR, Config
from .engine import Engine, EngineError
from .events import enrich_event
from .hooks import HookManager
from .journal import Journal, atomic_write
from .loop import Agent
from .mcp import McpManager
from .mcp.launch import LaunchError
from .mcp.session import McpError
from .memory import Memory
from .outputs import OutputStore
from .permissions import PermissionManager
from .sessions import SessionStore
from .skills import Skills
from .swarm import SwarmManager, SwarmStore
from .mission import MissionManager, MissionStore
from .research import ResearchManager
from .media import (
    build_artifact_preview,
    create_artifact_bundle,
    detect_capabilities,
    explain_ingest_plan,
    export_session,
    html_preview_csp,
    ingest_file_async,
    sanitize_svg_for_preview,
    sniff_magic,
)
from .tools import search as search_mod
from .tools.files import SKIP_DIRS, rewind_to_turn

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
COOKIE = "la_token"
MAX_JSON = 20 * 1024 * 1024


def J(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def bad(msg: str, status: int = 400) -> JSONResponse:
    return J({"detail": msg}, status)


class State:
    """进程内的全部服务对象。"""

    def __init__(self, token: str, port: int, root: Path = ROOT):
        self.token, self.port = token, port
        self.cfg = Config(root / "config.json", root / "data" / "secrets.json")
        data = root / "data"
        self.cfg_dir = data
        self.subs: set[asyncio.Queue] = set()
        self.engine = Engine(self.cfg, root / "models", root / "bin", self.broadcast)
        self.store = SessionStore(data / "sessions")
        self.journal = Journal(data)
        self.outputs = OutputStore(data / "outputs")
        self.outputs.sweep({m["id"] for m in self.store.list()})      # 清掉已删除会话遗留的输出
        self.artifacts = ArtifactStore(data / "artifacts")
        self.artifacts.sweep({m["id"] for m in self.store.list()})
        self.audit = sec.AuditLog(data / "audit.jsonl")
        self.memory = Memory(data / "memory.db")
        self.skills = Skills(
            root / "skills",
            CODE_DIR / "lagent" / "builtin_skills",
            data / "skills_state.json",
            default_limit=self.cfg.get("skills", "default_enabled_limit", default=14),
        )
        protected = [root / "config.json", root / "skills", root / "models", root / "bin", CODE_DIR / "lagent", CODE_DIR / "web", root / "run.py"]
        if FROZEN:      # 打包运行：程序本体（exe 与其资源目录）同样只读
            protected += [Path(sys.executable), CODE_DIR]
        hidden = [data]
        self._loop: asyncio.AbstractEventLoop | None = None
        self.swarms = SwarmManager(cfg=self.cfg, store=SwarmStore(data / "swarms"), journal=self.journal, audit=self.audit, notify=self.broadcast)
        self.missions = MissionManager(cfg=self.cfg, store=MissionStore(data / "missions"), journal=self.journal, audit=self.audit,
                                       memory=self.memory, swarms=self.swarms, notify=self.broadcast)
        self.research = ResearchManager(cfg=self.cfg, data_dir=data, audit=self.audit, artifacts=self.artifacts,
                                        swarms=self.swarms, notify=self.broadcast)
        self.mcp = McpManager(data, self.audit, on_change=lambda _n, v: self.broadcast("mcp", {"server": v}), research=self.research)
        self.hub = hub_mod.DownloadManager(root / "models", on_event=self.broadcast_threadsafe)
        self.agent = Agent(cfg=self.cfg, engine=self.engine, store=self.store, journal=self.journal, audit=self.audit, memory=self.memory,
                           skills=self.skills, protected=protected, hidden=hidden, notify=self.broadcast, mcp=self.mcp,
                           outputs=self.outputs, artifacts=self.artifacts, swarms=self.swarms, missions=self.missions, research=self.research)
        self.research.router = self.agent.router
        self.swarms.default_runner = self.agent.run_swarm_node
        self.missions.default_episode_runner = self.agent.run_mission_episode
        self.root = root
        self.autostart = False
        self.resume_missions = False
        self.exit_cb: Any = None        # 由启动器设置：请求整个应用退出（测试/纯服务模式下为 None）

    def broadcast(self, event: str, data: dict) -> None:
        payload = enrich_event(event, data)
        for q in list(self.subs):
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

    def broadcast_threadsafe(self, event: str, data: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self.broadcast(event, data)
        else:
            self._loop.call_soon_threadsafe(self.broadcast, event, data)

    def workspace_of(self, sid: str | None) -> str:
        if sid:
            s = self.store.get(sid)
            if s and s.workspace:
                return s.workspace
        return self.cfg.get("workspace", default="")

    def guard(self, sid: str | None) -> sec.PathGuard:
        ws = self.workspace_of(sid)
        if not ws:
            raise ValueError("尚未选择工作区")
        return sec.PathGuard(ws, protected=self.agent.protected, hidden=self.agent.hidden)


# ====================================================================== 中间件
class Guard:
    def __init__(self, app: Any, st: State):
        self.app, self.st = app, st

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        st = self.st
        hdr = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        path, method = scope["path"], scope["method"]

        async def reply(resp: Response) -> None:
            await resp(scope, receive, send)

        if not sec.host_ok(hdr.get("host"), st.port):
            return await reply(PlainTextResponse("Forbidden host", 403))
        if not sec.origin_ok(hdr.get("origin"), st.port):
            return await reply(PlainTextResponse("Forbidden origin", 403))
        cookies = {}
        for part in hdr.get("cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        auth_hdr = hdr.get("authorization", "")
        bearer = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else ""
        authed = sec.token_ok(cookies.get(COOKIE), st.token) or sec.token_ok(bearer, st.token)
        if path == "/" and method == "GET":
            t = parse_qs(scope.get("query_string", b"").decode()).get("t", [""])[0]
            if t and sec.token_ok(t, st.token):
                r = RedirectResponse("/", 302)
                r.set_cookie(COOKIE, st.token, httponly=True, samesite="strict", path="/", max_age=60 * 60 * 24 * 30)
                return await reply(r)
        if not authed:
            if path.startswith("/api/"):
                return await reply(JSONResponse({"detail": "unauthorized"}, 401))
            return await reply(PlainTextResponse("会话已失效，请从终端打印的链接重新打开 LocalAgent。\nSession expired – reopen the link printed in the terminal.", 401))

        async def send2(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers", []))
                have = {k.lower() for k, _ in h}
                extra = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                         (b"cross-origin-resource-policy", b"same-origin"), (b"x-frame-options", b"DENY")]
                h += [(k, v) for k, v in extra if k not in have]
                msg = {**msg, "headers": h}
            await send(msg)

        await self.app(scope, receive, send2)


async def jbody(req: Request) -> dict:
    raw = await req.body()
    if len(raw) > MAX_JSON:
        raise ValueError("请求体过大")
    if not raw:
        return {}
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("需要 JSON 对象")
    return d


def make_app(st: State) -> Starlette:
    R: list[Route] = []

    def route(path: str, methods: list[str]):
        def deco(fn: Any) -> Any:
            async def handler(req: Request) -> Response:
                try:
                    return await fn(req)
                except (ValueError, sec.SecurityError, json.JSONDecodeError, McpError, LaunchError) as e:
                    return bad(str(e))
                except KeyError as e:
                    return bad(str(e.args[0]) if e.args else "不存在", 404)
                except (PermissionError, FileExistsError, EngineError) as e:
                    return bad(str(e), 409)
            R.append(Route(path, handler, methods=methods))
            return fn
        return deco

    # ------------------------------------------------------------ 状态/配置
    def models_view() -> tuple[list[dict], list[str]]:
        models, mm = st.engine.scan()
        keys = ("file", "size_gb", "arch", "name", "moe", "experts", "experts_used", "ctx_train", "quant",
                "mmproj", "vision", "is_draft", "draft_model", "compatible_drafts", "error")
        return [{k: m.get(k) for k in keys} for m in models], [x["file"] for x in mm]

    @route("/api/state", ["GET"])
    async def state(req: Request) -> Response:
        if st.engine.binary is None and st.engine.status == "stopped":
            await asyncio.to_thread(st.engine.probe)
        models, mms = await asyncio.to_thread(models_view)
        sid = req.query_params.get("session_id")
        sess = st.store.get(sid) if sid else None
        global_mode = st.cfg.get("agent", "permission_mode")
        eff_mode = (getattr(sess, "permission_mode", "") if sess else "") or global_mode
        return J({"version": __version__, "workspace": st.workspace_of(sid), "engine": st.engine.state(), "models": models,
                  "mmprojs": mms, "hw": await asyncio.to_thread(hw.detect), "sandbox": sec.sandbox_info(),
                  "capabilities": detect_capabilities(st.engine, st.cfg, st.engine.bin_dir),
                  "permission_mode": eff_mode, "global_permission_mode": global_mode,
                  "offline": os.environ.get("LOCALAGENT_OFFLINE") == "1",
                  "tainted": bool(sess.tainted) if sess else False})

    @route("/api/capabilities", ["GET"])
    async def capabilities(req: Request) -> Response:
        caps = detect_capabilities(st.engine, st.cfg, st.engine.bin_dir)
        out: dict[str, Any] = dict(caps)
        fn = (req.query_params.get("filename") or "").strip()
        mime = (req.query_params.get("mime") or "").strip()
        if fn or mime:
            out["plan"] = explain_ingest_plan(fn, mime, caps)
        return J(out)

    @route("/api/config", ["GET", "PATCH"])
    async def config(req: Request) -> Response:
        if req.method == "PATCH":
            d = await jbody(req)
            if "workspace" in d:
                raise ValueError("工作区请通过 PUT /api/workspace 设置")
            out = st.cfg.patch(d)
            st.swarms.sync_scheduler_from_cfg()
            st.research.sync_from_cfg()
            st.skills.set_default_limit(st.cfg.get("skills", "default_enabled_limit", default=14))
            st.audit.append(event="config", keys=sorted(d.keys()))
            st.broadcast("config", {"config": out})
            return J({"config": out})
        return J({"config": st.cfg.public()})

    # ------------------------------------------------------------ 引擎
    @route("/api/engine/start", ["POST"])
    async def e_start(req: Request) -> Response:
        d = await jbody(req)
        if st.engine.status in ("starting", "ready", "stopping"):
            pass
        asyncio.create_task(_safe(st.engine.start(d.get("model"), d.get("mmproj"), d.get("ctx"))))
        await asyncio.sleep(0.05)
        return J(st.engine.state())

    async def _safe(coro: Any) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            pass

    @route("/api/engine/stop", ["POST"])
    async def e_stop(req: Request) -> Response:
        await st.engine.stop()
        return J(st.engine.state())

    @route("/api/engine/restart", ["POST"])
    async def e_restart(req: Request) -> Response:
        d = await jbody(req)
        asyncio.create_task(_safe(st.engine.restart(reset_ctx=True, model=d.get("model"), mmproj=d.get("mmproj"), ctx=d.get("ctx"))))
        await asyncio.sleep(0.05)
        return J(st.engine.state())

    @route("/api/engine/plan", ["GET", "POST"])
    async def e_plan(req: Request) -> Response:
        d = dict(req.query_params) if req.method == "GET" else await jbody(req)
        await asyncio.to_thread(st.engine.probe)
        plan = await asyncio.to_thread(st.engine.plan, d.get("model"), d.get("mmproj"), d.get("ctx"))
        cmd = ""
        if st.engine.binary:
            st.engine.port, st.engine.api_key = 0, "x"
            cmd = st.engine.display_command(st.engine.build_args(plan)).replace("--port 0", "--port <随机>")
        else:
            plan["notes"].append("尚未找到 llama-server，无法生成启动命令。")
        plan = {k: v for k, v in plan.items() if k not in ("model_path", "mmproj_path")}
        return J({"plan": plan, "command": cmd})

    @route("/api/engine/logs", ["GET"])
    async def e_logs(req: Request) -> Response:
        n = min(int(req.query_params.get("n", 200)), 600)
        return J({"lines": list(st.engine.log)[-n:]})

    @route("/api/events", ["GET"])
    async def events(req: Request) -> Response:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        st.subs.add(q)

        async def gen():
            try:
                yield f"event: engine\ndata: {json.dumps(st.engine.state(), ensure_ascii=False)}\n\n"
                while True:
                    try:
                        ev, data = await asyncio.wait_for(q.get(), 15)
                        yield f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                st.subs.discard(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ 会话
    @route("/api/sessions", ["GET", "POST"])
    async def sessions(req: Request) -> Response:
        if req.method == "POST":
            d = await jbody(req)
            ws = d.get("workspace") or ""
            if ws:
                ws = str(sec.validate_workspace(ws))
            s = st.store.create(ws or st.cfg.get("workspace", default=""))
            return J(s.view())
        return J(st.store.list())

    @route("/api/sessions/search", ["GET"])
    async def sessions_search(req: Request) -> Response:
        q = (req.query_params.get("q") or "").strip().lower()
        items = st.store.list()
        if not q:
            return J(items)
        out: list[dict] = []
        for meta in items:
            s = st.store.get(meta["id"])
            if s is None:
                continue
            snippet = ""
            if q in (s.title or "").lower():
                snippet = s.title
            if not snippet:
                for u in s.ui:
                    txt = str(u.get("text") or "")
                    idx = txt.lower().find(q)
                    if idx >= 0:
                        lo = max(0, idx - 24)
                        snippet = ("…" if lo > 0 else "") + txt[lo:idx + len(q) + 56].replace("\n", " ")
                        break
                    for b in u.get("blocks") or []:
                        if b.get("type") in ("text", "tool"):
                            bt = str(b.get("text") or b.get("title") or "")
                            idx = bt.lower().find(q)
                            if idx >= 0:
                                lo = max(0, idx - 24)
                                snippet = ("…" if lo > 0 else "") + bt[lo:idx + len(q) + 56].replace("\n", " ")
                                break
                    if snippet:
                        break
            if not snippet and s.summary:
                idx = s.summary.lower().find(q)
                if idx >= 0:
                    lo = max(0, idx - 24)
                    snippet = ("…" if lo > 0 else "") + s.summary[lo:idx + len(q) + 56].replace("\n", " ")
            if snippet:
                out.append({**meta, "snippet": snippet[:120]})
        return J(out)

    def sess_or_404(req: Request):
        s = st.store.get(req.path_params["sid"])
        if not s:
            raise KeyError("会话不存在")
        return s

    @route("/api/sessions/{sid}", ["GET", "PATCH", "DELETE"])
    async def session_one(req: Request) -> Response:
        s = sess_or_404(req)
        if req.method == "DELETE":
            if s.active_run:
                st.agent.cancel(s.active_run)
            st.store.delete(s.id)
            from .tools import jobs as _jobs
            from . import shell_session as _sh_sess
            _jobs.stop_session(s.id)           # 会话没了，它启动的后台任务与持久终端也一并终止
            await _sh_sess.close_session_shell(s.id)
            st.agent.seen_files.pop(s.id, None)
            st.outputs.drop(s.id)
            st.artifacts.drop_session(s.id)
            return J({"ok": True})
        if req.method == "PATCH":
            d = await jbody(req)
            if "title" in d:
                s.title = str(d["title"]).strip()[:120] or s.title
            if "pinned" in d:
                s.pinned = bool(d["pinned"])
            if "workspace" in d:
                s.workspace = str(sec.validate_workspace(d["workspace"])) if d["workspace"] else ""
            if "permission_mode" in d:
                pm = str(d["permission_mode"] or "").strip()
                if pm and pm not in ("ask", "auto_edit", "plan", "auto"):
                    raise ValueError("permission_mode 只能是 ask / auto_edit / plan / auto 或空字符串")
                s.permission_mode = pm or None
            st.store.save(s)
            st.broadcast("state", {"session_id": s.id, "permission_mode": s.permission_mode or st.cfg.get("agent", "permission_mode")})
        return J(s.view())

    @route("/api/sessions/{sid}/messages", ["POST"])
    async def send_message(req: Request) -> Response:
        s = sess_or_404(req)
        if s.active_run and not (st.agent.get_run(s.active_run) and st.agent.get_run(s.active_run).finished):  # type: ignore[union-attr]
            return bad("该会话正在运行，请先停止或等待完成", 409)
        d = await jbody(req)
        text = str(d.get("text") or "").strip()
        images = [u for u in (d.get("images") or []) if isinstance(u, str)][:6]
        for u in images:
            if not re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=]+$", u) or len(u) > 12_000_000:
                raise ValueError("图片格式无效或过大（仅支持 png/jpeg/webp/gif 的 data URL）")
        fl = []
        if d.get("files"):
            g = st.guard(s.id)
            for f in d["files"][:10]:
                p = g.resolve(str(f))
                if not p.is_file():
                    raise ValueError(f"附件不存在: {f}")
                fl.append(g.rel(p))
        sk = []
        for n in (d.get("skills") or [])[:5]:
            if not st.skills.get(str(n), with_body=False):
                raise ValueError(f"技能不存在: {n}")
            sk.append(str(n))
        if not (text or images or fl):
            raise ValueError("消息为空")
        if st.engine.status in ("stopped", "error") and not st.engine._last:
            return bad("模型未就绪：请先在 设置→模型与引擎 中加载模型。", 409)
        if not s.workspace:
            s.workspace = st.cfg.get("workspace", default="")
        run = st.agent.start_run(s, text, images, fl, sk)
        return J({"run_id": run.id})

    @route("/api/sessions/{sid}/upload", ["POST"])
    async def upload(req: Request) -> Response:
        s = sess_or_404(req)
        g = st.guard(s.id)
        form = await req.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise ValueError("缺少 file 字段")
        data = await f.read()
        if len(data) > int(st.cfg.get("security", "max_file_mb", default=20)) * 1048576:
            raise ValueError("文件超过大小上限")
        name = re.sub(r"[^\w.\-一-鿿 ]", "_", os.path.basename(f.filename or "file"))[:100].strip(" .") or "file"
        dest = g.resolve(f"uploads/{name}", write=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{uuid.uuid4().hex[:4]}{dest.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dest, data)
        st.journal.record(s.id, op="write", path=dest, summary=f"上传 {g.rel(dest)}", existed=False)
        caps = detect_capabilities(st.engine, st.cfg, st.engine.bin_dir)
        kind, mime = sniff_magic(data[:16384], name, path=dest)
        plan = explain_ingest_plan(name, mime, caps, kind=kind)
        res = await ingest_file_async(dest, cfg=st.cfg, engine=st.engine, caps=caps) if kind not in ("text", "md") else None
        out: dict[str, Any] = {
            "path": g.rel(dest),
            "size": len(data),
            "kind": res.kind if res else kind,
            "mime": res.mime if res else mime,
            "plan": plan,
        }
        if res is not None:
            if not res.ok and res.error and any(k in res.error for k in ("炸弹", "穿越", "符号链接", "上限")):
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ValueError(res.error)
            out["ok"] = res.ok
            out["warnings"] = res.warnings
            out["meta"] = res.meta
            out["images"] = res.images[:4]
            out["summary"] = res.render_summary(max_chars=2500)
            if not res.ok and res.error:
                out["error"] = res.error
        return J(out)

    @route("/api/media/ingest", ["POST"])
    async def media_ingest(req: Request) -> Response:
        d = await jbody(req)
        sid = str(d.get("session_id") or "")
        raw_path = str(d.get("path") or "").strip()
        if not raw_path:
            raise ValueError("path 不能为空")
        g = st.guard(sid) if sid else sec.PathGuard(sec.validate_workspace(st.cfg.get("workspace") or ""))
        p = g.resolve(raw_path, write=False)
        res = await ingest_file_async(
            p,
            cfg=st.cfg,
            engine=st.engine,
            options={
                "resolution": str(d.get("resolution") or "auto"),
                "select_member": str(d.get("select_member") or ""),
                "render_scanned": bool(d.get("render_scanned", True)),
            },
        )
        sess = st.store.get(sid) if sid else None
        if sess is not None and not sess.tainted:
            sess.tainted = True
            st.store.save(sess)
        payload = res.to_dict()
        payload["summary"] = res.render_summary(max_chars=8000)
        return J(payload)

    @route("/api/sessions/{sid}/compact", ["POST"])
    async def compact(req: Request) -> Response:
        s = sess_or_404(req)
        if s.active_run:
            return bad("运行中无法压缩", 409)
        return J({"summary": await st.agent.manual_compact(s)})

    @route("/api/sessions/{sid}/truncate", ["POST"])
    async def truncate(req: Request) -> Response:
        s = sess_or_404(req)
        if s.active_run:
            return bad("运行中无法回退", 409)
        d = await jbody(req)
        st.agent.truncate(s, int(d["from_index"]))
        return J(s.view())

    @route("/api/sessions/{sid}/untaint", ["POST"])
    async def untaint(req: Request) -> Response:
        s = sess_or_404(req)
        s.tainted = False
        st.store.save(s)
        st.audit.append(event="untaint", session=s.id)
        return J({"tainted": False})

    @route("/api/sessions/{sid}/undo", ["POST"])
    async def sess_undo(req: Request) -> Response:
        s = sess_or_404(req)
        if s.active_run:
            return bad("运行中无法撤销", 409)
        d = await jbody(req)
        res = await asyncio.to_thread(rewind_to_turn, st.journal, s.id, d.get("turn_id"), bool(d.get("force", False)))
        if res.get("count"):
            st.audit.append(event="undo_turn", session=s.id, turn_id=res.get("turn_id"), count=res["count"])
        return J(res)

    @route("/api/sessions/{sid}/export", ["GET", "POST"])
    async def export(req: Request) -> Response:
        s = sess_or_404(req)
        d = await jbody(req) if req.method == "POST" else {}
        fmt = str(req.query_params.get("format") or d.get("format") or "md").strip().lower()
        q_think = req.query_params.get("thinking")
        q_tools = req.query_params.get("tools")
        inc_think = (q_think not in ("0", "false", "no")) if q_think is not None else bool(d.get("include_thinking", True))
        inc_tools = (q_tools not in ("0", "false", "no")) if q_tools is not None else bool(d.get("include_tools", True))
        save_art = bool(d.get("save_artifact", False)) or req.query_params.get("save") in ("1", "true")
        arts = st.artifacts.list(s.id, limit=200)
        try:
            payload, mime, fname = await asyncio.to_thread(
                export_session, s, fmt, include_thinking=inc_think, include_tools=inc_tools, artifacts=arts
            )
        except RuntimeError as e:
            return bad(str(e), 409)
        if save_art:
            art = st.artifacts.save_bytes(
                s.id,
                payload,
                kind="export",
                title=fname,
                filename=fname,
                mime=mime.split(";")[0].strip(),
                created_by="user",
                meta={"format": fmt, "include_thinking": inc_think, "include_tools": inc_tools},
            )
            st.broadcast("artifact_created", art.to_dict())
            return J(art.to_dict())
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname) or f"session-{s.id}.{fmt}"
        disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname, safe='')}"
        return Response(
            payload,
            media_type=mime,
            headers={
                "Content-Disposition": disp,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------ 运行
    @route("/api/runs/{rid}/events", ["GET"])
    async def run_events(req: Request) -> Response:
        run = st.agent.get_run(req.path_params["rid"])
        if not run:
            return bad("run 不存在或已过期", 404)
        after = int(req.headers.get("last-event-id") or req.query_params.get("after") or 0)

        async def gen():
            i = max(0, after)
            while True:
                while i < len(run.events):
                    eid, ev, data = run.events[i]
                    i += 1
                    yield f"id: {eid}\nevent: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if run.finished and i >= len(run.events):
                    return
                try:
                    async with run.cond:
                        if i >= len(run.events) and not run.finished:
                            await asyncio.wait_for(run.cond.wait(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @route("/api/runs/{rid}/cancel", ["POST"])
    async def run_cancel(req: Request) -> Response:
        return J({"ok": st.agent.cancel(req.path_params["rid"])})

    @route("/api/approvals/{aid}", ["POST"])
    async def approval(req: Request) -> Response:
        d = await jbody(req)
        if d.get("decision") not in ("allow", "allow_session", "deny"):
            raise ValueError("decision 必须是 allow / allow_session / deny")
        return J({"ok": st.agent.answer_approval(req.path_params["aid"], d["decision"])})

    @route("/api/questions/{qid}", ["POST"])
    async def question(req: Request) -> Response:
        d = await jbody(req)
        return J({"ok": st.agent.answer_question(req.path_params["qid"], str(d.get("answer", ""))[:4000])})

    # ------------------------------------------------------------ 工作区
    @route("/api/workspace", ["GET", "PUT"])
    async def workspace(req: Request) -> Response:
        if req.method == "PUT":
            d = await jbody(req)
            p = sec.validate_workspace(str(d.get("path", "")))
            st.cfg.set_workspace(str(p))
            if d.get("session_id"):
                s = st.store.get(d["session_id"])
                if s:
                    s.workspace = str(p)
                    st.store.save(s)
            st.audit.append(event="workspace", path=str(p))
        sid = req.query_params.get("session_id")
        ws = st.workspace_of(sid)
        return J({"path": ws, "exists": bool(ws) and Path(ws).is_dir(), "journal_count": st.journal.count(sid) if sid else 0,
                  "trash_count": len(st.journal.list_trash())})

    @route("/api/fs/pick", ["POST"])
    async def fs_pick(req: Request) -> Response:
        d = await jbody(req)
        init = str(d.get("initial_dir") or st.cfg.get("workspace", default="") or "")
        picked = await asyncio.to_thread(appwindow.pick_folder, init)
        if not picked:
            return J({"path": None, "cancelled": True})
        return J({"path": str(sec.validate_workspace(picked)), "cancelled": False})

    @route("/api/fs/browse", ["GET"])
    async def browse(req: Request) -> Response:
        raw = req.query_params.get("path", "")
        if not raw:
            roots = [str(Path.home())]
            if os.name == "nt":
                roots += [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
            else:
                roots.append("/")
            return J({"path": "", "parent": None, "dirs": roots, "sep": os.sep, "roots": True})
        p = Path(os.path.realpath(os.path.expanduser(raw)))
        if not p.is_dir():
            raise ValueError("不是目录")
        dirs = []
        try:
            for e in sorted(os.scandir(p), key=lambda x: x.name.lower()):
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        dirs.append(e.name)
                except OSError:
                    continue
        except PermissionError:
            raise ValueError("没有权限读取该目录")
        return J({"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs[:1000], "sep": os.sep})

    @route("/api/workspace/tree", ["GET"])
    async def tree(req: Request) -> Response:
        g = st.guard(req.query_params.get("session_id"))
        p = g.resolve(req.query_params.get("path") or ".")
        if not p.is_dir():
            raise ValueError("不是目录")
        rec = req.query_params.get("recursive") in ("1", "true", "yes")
        if rec:
            def _scan_rec() -> list[dict]:
                items: list[dict] = []
                for root_s, dirs, fns in os.walk(p, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(root_s, d)))
                    rp = Path(root_s)
                    for fn in sorted(fns):
                        fp = rp / fn
                        if fp.is_symlink():
                            continue
                        try:
                            rel = g.rel(fp)
                        except sec.SecurityError:
                            continue
                        items.append({"name": fn, "path": rel, "type": "file"})
                        if len(items) >= 2500:
                            return items
                return items
            ents = await asyncio.to_thread(_scan_rec)
            return J({"path": g.rel(p), "recursive": True, "entries": ents})
        ents = []
        for e in os.scandir(p):
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                stt = e.stat(follow_symlinks=False)
                ents.append({"name": e.name, "type": "dir" if is_dir else "file", "size": 0 if is_dir else stt.st_size, "mtime": stt.st_mtime})
            except OSError:
                continue
        ents.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return J({"path": g.rel(p), "entries": ents[:2000]})

    @route("/api/workspace/git", ["GET"])
    async def wgit(req: Request) -> Response:
        g = st.guard(req.query_params.get("session_id"))
        diff_rel = ""
        if req.query_params.get("path"):
            dp = g.resolve(req.query_params["path"])
            diff_rel = g.rel(dp)

        def _git_info() -> dict:
            if not shutil.which("git"):
                return {"is_repo": False, "reason": "git not installed"}
            kw = {"cwd": str(g.ws), "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 4}
            try:
                chk = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], **kw)
            except (OSError, subprocess.SubprocessError):
                return {"is_repo": False}
            if chk.returncode != 0 or chk.stdout.strip() != "true":
                return {"is_repo": False}
            st_r = subprocess.run(["git", "-c", "core.quotepath=false", "status", "--porcelain=v1", "-b"], **kw)
            branch = ""
            files = []
            for ln in st_r.stdout.splitlines():
                if ln.startswith("## "):
                    branch = ln[3:].strip()
                    continue
                if len(ln) >= 4:
                    xy = ln[:2]
                    fp = ln[3:].strip()
                    if " -> " in fp:
                        fp = fp.split(" -> ", 1)[1]
                    files.append({"status": xy.strip() or xy, "code": xy, "path": fp})
            diff_text = ""
            if diff_rel:
                dr = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--no-color", "--", diff_rel], **kw)
                diff_text = dr.stdout or ""
                if not diff_text:
                    dr2 = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--cached", "--no-color", "--", diff_rel], **kw)
                    diff_text = dr2.stdout or ""
                if len(diff_text) > 32000:
                    diff_text = diff_text[:32000] + "\n…(diff 已截断)"
            return {"is_repo": True, "branch": branch, "files": files[:300], "diff": diff_text}

        return J(await asyncio.to_thread(_git_info))

    @route("/api/workspace/file", ["GET"])
    async def wfile(req: Request) -> Response:
        g = st.guard(req.query_params.get("session_id"))
        p = g.resolve(req.query_params.get("path", ""))
        if not p.is_file():
            raise ValueError("不是文件")
        size = p.stat().st_size
        ext = p.suffix.lower()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp") and size < 6_000_000:
            return J({"kind": "image", "data_url": f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode(), "truncated": False, "size": size, "mime": mime})
        if ext in (".docx", ".xlsx", ".xlsm", ".pptx", ".pdf"):
            from .tools import office
            txt = await asyncio.to_thread(office.read_document, p, 1, None, 60000)
            return J({"kind": "office", "content": txt, "truncated": False, "size": size, "mime": mime})
        raw = p.read_bytes()[:200_000]
        if b"\x00" in raw[:8192]:
            return J({"kind": "binary", "truncated": False, "size": size, "mime": mime})
        from .tools.files import decode_bytes
        return J({"kind": "text", "content": decode_bytes(raw)[0], "truncated": size > 200_000, "size": size, "mime": mime})

    # ------------------------------------------------------------ 会话产物（ArtifactStore）
    @route("/api/artifacts", ["GET"])
    async def artifacts_list(req: Request) -> Response:
        sid = req.query_params.get("session_id") or None
        kind = req.query_params.get("kind") or None
        limit = int(req.query_params.get("limit") or 200)
        return J({"artifacts": [a.to_dict() for a in st.artifacts.list(sid, kind=kind, limit=limit)]})

    @route("/api/artifacts/bundle", ["POST"])
    async def artifacts_bundle(req: Request) -> Response:
        d = await jbody(req)
        sid = str(d.get("session_id") or "").strip()
        if not sid or not st.store.get(sid):
            raise KeyError("会话不存在")
        art_ids = [str(x).strip() for x in (d.get("artifact_ids") or []) if str(x).strip()] if d.get("artifact_ids") is not None else None
        title = str(d.get("title") or "Artifact Bundle").strip()
        art, _ = await asyncio.to_thread(create_artifact_bundle, st.artifacts, sid, art_ids, title=title)
        max_mb = int(st.cfg.get("media", "artifact_max_mb", default=512) or 512)
        st.artifacts.prune_max_bytes(max_mb * 1024 * 1024, session_id=sid)
        st.audit.append(event="artifact_bundle", session=sid, artifact_id=art.id, count=art.meta.get("bundle_count", 0))
        ad = art.to_dict()
        st.broadcast("artifact_created", ad)
        return J(ad)

    @route("/api/artifacts/{aid}/meta", ["GET"])
    async def artifact_meta(req: Request) -> Response:
        art = st.artifacts.get(req.path_params["aid"])
        if not art:
            return bad("artifact 不存在", 404)
        return J(art.to_dict())

    @route("/api/artifacts/{aid}/preview", ["GET"])
    async def artifact_preview(req: Request) -> Response:
        art = st.artifacts.get(req.path_params["aid"])
        if not art:
            return bad("artifact 不存在", 404)
        p = st.artifacts.resolve_path(art)
        if not p or not p.is_file():
            return bad("artifact 文件缺失", 404)
        raw_mode = req.query_params.get("raw") in ("1", "true")
        if raw_mode:
            ext = p.suffix.lower()
            mime = (art.mime or "").lower()
            if mime == "text/html" or ext in (".html", ".htm") or art.kind == "html":
                scripts_ok = bool((art.meta or {}).get("scripts_authorized", False))
                return Response(
                    p.read_bytes(),
                    media_type="text/html; charset=utf-8",
                    headers={
                        "Content-Security-Policy": html_preview_csp(scripts_ok),
                        "X-Frame-Options": "SAMEORIGIN",
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
            if mime == "image/svg+xml" or ext == ".svg" or art.kind == "chart":
                clean_svg = sanitize_svg_for_preview(p.read_text("utf-8", errors="replace"))
                return Response(
                    clean_svg.encode("utf-8"),
                    media_type="image/svg+xml; charset=utf-8",
                    headers={
                        "Content-Security-Policy": html_preview_csp(False),
                        "X-Frame-Options": "SAMEORIGIN",
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
            if mime.startswith("audio/"):
                return FileResponse(p, media_type=art.mime or "application/octet-stream",
                                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        page = int(req.query_params.get("page") or 1)
        sheet = int(req.query_params.get("sheet") or 0)
        offset = int(req.query_params.get("offset") or 0)
        limit = int(req.query_params.get("limit") or 100)
        return J(await asyncio.to_thread(build_artifact_preview, st.artifacts, art, page=page, sheet=sheet, offset=offset, limit=limit))

    @route("/api/artifacts/{aid}/authorize-scripts", ["POST"])
    async def artifact_authorize_scripts(req: Request) -> Response:
        art = st.artifacts.get(req.path_params["aid"])
        if not art:
            return bad("artifact 不存在", 404)
        d = await jbody(req)
        enabled = bool(d.get("enabled", False))
        updated = st.artifacts.update_meta(art.id, {"scripts_authorized": enabled})
        st.audit.append(event="artifact_script_auth", session=art.session_id, artifact_id=art.id, enabled=enabled)
        return J(updated.to_dict() if updated else art.to_dict())

    @route("/api/artifacts/{aid}/open", ["POST"])
    async def artifact_open(req: Request) -> Response:
        art = st.artifacts.get(req.path_params["aid"])
        if not art:
            return bad("artifact 不存在", 404)
        p = st.artifacts.resolve_path(art)
        if not p or not p.is_file():
            return bad("artifact 文件缺失", 404)
        await asyncio.to_thread(appwindow.open_local_file, p)
        st.audit.append(event="artifact_open", session=art.session_id, artifact_id=art.id)
        return J({"ok": True, "id": art.id})

    @route("/api/artifacts/{aid}", ["GET", "DELETE"])
    async def artifact_download(req: Request) -> Response:
        art = st.artifacts.get(req.path_params["aid"])
        if not art:
            return bad("artifact 不存在", 404)
        if req.method == "DELETE":
            ok = st.artifacts.delete(art.id)
            return J({"ok": ok, "id": art.id})
        p = st.artifacts.resolve_path(art)
        if not p or not p.is_file():
            return bad("artifact 文件缺失", 404)
        fname = p.name
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname) or f"{art.id}.bin"
        disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname, safe='')}"
        return FileResponse(
            p,
            media_type=art.mime or "application/octet-stream",
            headers={
                "Content-Disposition": disp,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------ Swarm 多代理集群
    @route("/api/swarms", ["GET", "POST"])
    async def swarms_root(req: Request) -> Response:
        if req.method == "POST":
            d = await jbody(req)
            sid = str(d.get("session_id") or "")
            sess = st.store.get(sid) if sid else None
            ws = str(d.get("workspace") or (sess.workspace if sess else st.cfg.get("workspace", default="")) or "")
            if ws:
                ws = str(sec.validate_workspace(ws))
            nodes = d.get("nodes")
            if not isinstance(nodes, list) or not nodes:
                raise ValueError("nodes 必须是非空节点列表")
            parent_mode = st.agent._session_mode(sess) if sess else str(st.cfg.get("agent", "permission_mode", default="ask"))
            sw = st.swarms.create_swarm(
                session_id=sid or "global",
                goal=str(d.get("goal") or "Swarm 并行任务"),
                nodes=nodes,
                workspace=ws,
                parent_mode=parent_mode,
                explicit_auto_worker=bool(d.get("explicit_auto_worker", False)),
                budget=dict(d.get("budget") or {}) or None,
                mission_id=d.get("mission_id"),
            )
            if sess:
                sess.swarm_id = sw.id
                st.store.save(sess)
            return J(st.swarms.snapshot(sw.id))
        sid = req.query_params.get("session_id") or None
        return J({"swarms": st.swarms.list_swarms(session_id=sid)})

    @route("/api/swarms/{swid}", ["GET"])
    async def swarm_one(req: Request) -> Response:
        swid = req.path_params["swid"]
        snap = st.swarms.snapshot(swid)
        if not snap:
            return bad("swarm 不存在", 404)
        sw = st.swarms.swarms.get(swid)
        if sw:
            snap["mailbox"] = sw.mailbox.all(100)
        return J(snap)

    @route("/api/swarms/{swid}/events", ["GET"])
    async def swarm_events(req: Request) -> Response:
        swid = req.path_params["swid"]
        snap = st.swarms.snapshot(swid)
        if not snap:
            return bad("swarm 不存在", 404)
        after = int(req.headers.get("last-event-id") or req.query_params.get("after") or 0)
        sw = st.swarms.swarms.get(swid)
        q: asyncio.Queue | None = asyncio.Queue(maxsize=200) if (sw and not sw.done_evt.is_set()) else None
        if sw and q is not None:
            sw.live_subs.add(q)

        async def gen():
            last_seq = after
            try:
                for ev in st.swarms.store.read_events(swid, after_seq=after):
                    last_seq = max(last_seq, int(ev.get("seq", 0)))
                    yield f"id: {ev.get('seq', 0)}\nevent: {ev.get('event', 'swarm')}\ndata: {json.dumps(ev.get('data') or {}, ensure_ascii=False)}\n\n"
                if q is None:
                    return
                while True:
                    try:
                        seq, kind, payload = await asyncio.wait_for(q.get(), 15)
                        if seq <= last_seq:
                            continue
                        last_seq = seq
                        yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        if kind == "swarm_done" or (sw and sw.done_evt.is_set() and q.empty()):
                            return
                    except asyncio.TimeoutError:
                        if sw and sw.done_evt.is_set():
                            return
                        yield ": ping\n\n"
            finally:
                if sw and q is not None:
                    sw.live_subs.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @route("/api/swarms/{swid}/cancel", ["POST"])
    async def swarm_cancel(req: Request) -> Response:
        return J({"ok": await st.swarms.cancel(req.path_params["swid"])})

    @route("/api/swarms/{swid}/pause", ["POST"])
    async def swarm_pause(req: Request) -> Response:
        return J({"ok": st.swarms.pause(req.path_params["swid"])})

    @route("/api/swarms/{swid}/resume", ["POST"])
    async def swarm_resume(req: Request) -> Response:
        sw = st.swarms.resume(req.path_params["swid"])
        return J({"ok": sw is not None})

    @route("/api/swarms/{swid}/agents/{aid}/message", ["POST"])
    async def swarm_agent_message(req: Request) -> Response:
        d = await jbody(req)
        text = str(d.get("text") or "").strip()
        if not text:
            raise ValueError("消息正文不能为空")
        msg = st.swarms.send_message(
            req.path_params["swid"],
            req.path_params["aid"],
            text,
            from_agent=str(d.get("from") or "user"),
            kind=str(d.get("kind") or "handoff"),
        )
        return J({"ok": True, "message": msg})

    @route("/api/swarms/{swid}/agents/{aid}/cancel", ["POST"])
    async def swarm_agent_cancel(req: Request) -> Response:
        affected = await st.swarms.cancel_agent(req.path_params["swid"], req.path_params["aid"])
        return J({"ok": bool(affected), "cancelled": affected})

    @route("/api/swarms/{swid}/conflicts/{cid}/resolve", ["POST"])
    async def swarm_conflict_resolve(req: Request) -> Response:
        d = await jbody(req)
        decision = str(d.get("decision") or "dismissed")
        swid = req.path_params["swid"]
        sw = st.swarms.swarms.get(swid) or st.swarms.resume(swid)
        card = (
            sw.merges.resolve_conflict(
                req.path_params["cid"],
                decision,
                workspace=sw.workspace,
                session_id=sw.session_id,
            )
            if sw
            else None
        )
        if not card:
            return bad("冲突记录不存在", 404)
        st.swarms._persist(sw)
        return J({"ok": True, "conflict": card.to_dict()})

    # ------------------------------------------------------------ Missions (M2)
    @route("/api/missions", ["GET", "POST"])
    async def missions_root(req: Request) -> Response:
        if req.method == "POST":
            d = await jbody(req)
            goal = str(d.get("goal") or "").strip()
            if not goal:
                raise ValueError("mission goal 不能为空")
            sid = str(d.get("session_id") or "")
            sess = st.store.get(sid) if sid else None
            ws = str(d.get("workspace") or (sess.workspace if sess else st.cfg.get("workspace", default="")) or "")
            if ws:
                ws = str(sec.validate_workspace(ws))
            m = st.missions.create_mission(
                goal=goal,
                title=str(d.get("title") or ""),
                workspace=ws,
                mode=str(d.get("mode") or ""),
                milestones=d.get("milestones"),
                acceptance=d.get("acceptance"),
                policy=d.get("policy"),
                budget=d.get("budget"),
                use_swarm=bool(d.get("use_swarm", False)),
                session_id=sid or None,
                auto_start=bool(d.get("auto_start", True)),
            )
            if sess:
                sess.mission_id = m.id
                st.store.save(sess)
            return J(st.missions.mission_snapshot(m.id))
        return J({"missions": st.missions.list_missions()})

    @route("/api/missions/{mid}", ["GET", "PATCH", "DELETE"])
    async def mission_one(req: Request) -> Response:
        mid = req.path_params["mid"]
        if req.method == "DELETE":
            m = st.missions.get_mission(mid)
            if m is None:
                return bad("mission 不存在", 404)
            st.missions.cancel_mission(mid)
            return J({"ok": st.missions.store.delete(mid)})
        if req.method == "PATCH":
            d = await jbody(req)
            try:
                st.missions.update_mission(mid, d)
            except KeyError:
                return bad("mission 不存在", 404)
            return J(st.missions.mission_snapshot(mid))
        snap = st.missions.mission_snapshot(mid)
        if not snap:
            return bad("mission 不存在", 404)
        return J(snap)

    @route("/api/missions/{mid}/start", ["POST"])
    async def mission_start(req: Request) -> Response:
        mid = req.path_params["mid"]
        try:
            st.missions.start_mission(mid)
        except KeyError:
            return bad("mission 不存在", 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/pause", ["POST"])
    async def mission_pause(req: Request) -> Response:
        mid = req.path_params["mid"]
        d = await jbody(req) if req.headers.get("content-length") else {}
        try:
            st.missions.pause_mission(mid, reason=str(d.get("reason") or "user"))
        except KeyError:
            return bad("mission 不存在", 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/resume", ["POST"])
    async def mission_resume(req: Request) -> Response:
        mid = req.path_params["mid"]
        try:
            st.missions.resume_mission(mid)
        except KeyError:
            return bad("mission 不存在", 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/cancel", ["POST"])
    async def mission_cancel(req: Request) -> Response:
        mid = req.path_params["mid"]
        try:
            st.missions.cancel_mission(mid)
        except KeyError:
            return bad("mission 不存在", 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/rollback", ["POST"])
    async def mission_rollback(req: Request) -> Response:
        mid = req.path_params["mid"]
        d = await jbody(req)
        target = str(d.get("target") or "").strip()
        if not target:
            raise ValueError("必须指定回滚目标 (milestone_id / commit_sha / base)")
        try:
            st.missions.rollback(mid, target)
        except KeyError:
            return bad("mission 不存在", 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/approve/{aid}", ["POST"])
    async def mission_approve(req: Request) -> Response:
        mid = req.path_params["mid"]
        aid = req.path_params["aid"]
        d = await jbody(req) if req.headers.get("content-length") else {}
        decision = str(d.get("decision") or "allow")
        try:
            st.missions.approve(mid, aid, decision=decision)
        except KeyError as e:
            return bad(str(e), 404)
        return J(st.missions.mission_snapshot(mid))

    @route("/api/missions/{mid}/timeline", ["GET"])
    async def mission_timeline(req: Request) -> Response:
        mid = req.path_params["mid"]
        m = st.missions.get_mission(mid)
        if m is None:
            return bad("mission 不存在", 404)
        after = int(req.query_params.get("after") or 0)
        return J({
            "mission_id": mid,
            "timeline": m.timeline,
            "episodes": [e.to_dict() for e in m.episodes],
            "events": st.missions.store.read_events(mid, after_seq=after, limit=300),
        })

    @route("/api/missions/{mid}/mission-md", ["GET", "PUT"])
    async def mission_md_rw(req: Request) -> Response:
        mid = req.path_params["mid"]
        m = st.missions.get_mission(mid)
        if m is None:
            return bad("mission 不存在", 404)
        if req.method == "PUT":
            d = await jbody(req)
            content = str(d.get("content") or "")
            if not content.strip():
                raise ValueError("MISSION.md 内容不能为空")
            st.missions.store.write_mission_md(m, content)
            return J(st.missions.mission_snapshot(mid))
        return J({
            "mission_id": mid,
            "path": str(st.missions.store.mission_md_path(m)),
            "content": st.missions.store.read_mission_md(m),
        })

    @route("/api/missions/{mid}/events", ["GET"])
    async def mission_events(req: Request) -> Response:
        mid = req.path_params["mid"]
        m = st.missions.get_mission(mid)
        if m is None:
            return bad("mission 不存在", 404)
        after = int(req.headers.get("last-event-id") or req.query_params.get("after") or 0)
        terminal = m.status in ("completed", "failed", "cancelled")
        q = None if terminal else st.missions.subscribe(mid)

        async def gen():
            last_seq = after
            try:
                for ev in st.missions.store.read_events(mid, after_seq=after, limit=500):
                    seq = int(ev.get("seq") or 0)
                    last_seq = max(last_seq, seq)
                    yield f"id: {seq}\nevent: {ev.get('kind', 'mission')}\ndata: {json.dumps(ev.get('data') or {}, ensure_ascii=False)}\n\n"
                if q is None:
                    return
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        seq = int(ev.get("seq") or 0)
                        if seq <= last_seq:
                            continue
                        last_seq = seq
                        kind = str(ev.get("kind") or "mission")
                        yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(ev.get('data') or {}, ensure_ascii=False)}\n\n"
                        cur = st.missions.get_mission(mid)
                        if kind == "mission_done" or (cur and cur.status in ("completed", "failed", "cancelled") and q.empty()):
                            return
                    except asyncio.TimeoutError:
                        cur = st.missions.get_mission(mid)
                        if cur and cur.status in ("completed", "failed", "cancelled"):
                            return
                        yield ": ping\n\n"
            finally:
                if q is not None:
                    st.missions.unsubscribe(mid, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ Research Engine & 网络账本 (M5)
    @route("/api/research", ["GET", "POST"])
    async def research_root(req: Request) -> Response:
        if req.method == "POST":
            d = await jbody(req)
            question = str(d.get("question") or "").strip()
            if not question:
                raise ValueError("question 不能为空")
            sid = str(d.get("session_id") or "")
            depth = str(d.get("depth") or "standard").strip().lower()
            budget = d.get("budget") if isinstance(d.get("budget"), dict) else None
            urls = d.get("urls") if isinstance(d.get("urls"), list) else None
            snap = st.research.start_research(question, session_id=sid, depth=depth, budget=budget, urls=urls)
            return J(snap)
        sid = req.query_params.get("session_id") or None
        limit = int(req.query_params.get("limit") or 30)
        return J({"tasks": st.research.evidence.list_tasks(session_id=sid, limit=limit)})

    @route("/api/research/policy", ["GET", "PUT"])
    async def research_policy(req: Request) -> Response:
        if req.method == "PUT":
            d = await jbody(req)
            patch_obj: dict[str, Any] = {}
            if "research" in d and isinstance(d["research"], dict):
                patch_obj["research"] = d["research"]
            else:
                r_keys = {k: v for k, v in d.items() if k != "html_scrapers_ack"}
                if r_keys:
                    patch_obj["research"] = r_keys
            if "html_scrapers_ack" in d:
                patch_obj.setdefault("search", {})["html_scrapers_ack"] = bool(d["html_scrapers_ack"])
            if patch_obj:
                out = st.cfg.patch(patch_obj)
                st.research.sync_from_cfg()
                st.audit.append(event="research_policy", keys=sorted(d.keys()))
                st.broadcast("config", {"config": out})
        return J({
            "research": st.cfg.get("research"),
            "html_scrapers_ack": bool(st.cfg.get("search", "html_scrapers_ack", default=False)),
            "offline": os.environ.get("LOCALAGENT_OFFLINE") == "1",
        })

    @route("/api/research/{rid}", ["GET"])
    async def research_one(req: Request) -> Response:
        rid = req.path_params["rid"]
        want_sse = req.query_params.get("stream") in ("1", "true") or "text/event-stream" in (req.headers.get("accept") or "").lower()
        if want_sse:
            return await research_events(req)
        snap = st.research.snapshot(rid)
        if not snap:
            return bad("research 任务不存在", 404)
        return J(snap)

    @route("/api/research/{rid}/events", ["GET"])
    async def research_events(req: Request) -> Response:
        rid = req.path_params["rid"]
        snap = st.research.snapshot(rid)
        if not snap:
            return bad("research 任务不存在", 404)
        after = int(req.headers.get("last-event-id") or req.query_params.get("after") or 0)
        run = st.research.runs.get(rid)
        done = snap["task"]["status"] in ("completed", "failed", "stopped")
        q = None if (done or run is None) else st.research.subscribe(rid)

        async def gen():
            last_seq = after
            try:
                if run is not None:
                    for seq, kind, payload in list(run.events):
                        if seq <= after:
                            continue
                        last_seq = max(last_seq, seq)
                        yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if q is None:
                    yield f"event: research_done\ndata: {json.dumps({'research_id': rid, 'status': snap['task']['status']}, ensure_ascii=False)}\n\n"
                    return
                while True:
                    try:
                        seq, kind, payload = await asyncio.wait_for(q.get(), 15)
                        if seq <= last_seq:
                            continue
                        last_seq = seq
                        yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        if kind == "research_done":
                            return
                    except asyncio.TimeoutError:
                        cur = st.research.evidence.get_task(rid)
                        if cur and cur["status"] in ("completed", "failed", "stopped"):
                            return
                        yield ": ping\n\n"
            finally:
                if q is not None:
                    st.research.unsubscribe(rid, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @route("/api/research/{rid}/stop", ["POST"])
    async def research_stop(req: Request) -> Response:
        ok = st.research.stop_research(req.path_params["rid"])
        return J({"ok": ok})

    @route("/api/research/{rid}/approve-domain", ["POST"])
    async def research_approve_domain(req: Request) -> Response:
        d = await jbody(req)
        dom = str(d.get("domain") or "").strip()
        snap = st.research.approve_domain(req.path_params["rid"], dom)
        if not snap:
            return bad("research 任务不存在", 404)
        return J(snap)

    @route("/api/research/{rid}/evidence", ["GET"])
    async def research_evidence(req: Request) -> Response:
        bundle = st.research.evidence.get_evidence_bundle(req.path_params["rid"])
        if not bundle:
            return bad("research 任务不存在", 404)
        return J(bundle)

    @route("/api/net/ledger", ["GET"])
    async def net_ledger(req: Request) -> Response:
        qp = req.query_params
        filt = {
            "domain": qp.get("domain") or None,
            "decision": qp.get("decision") or None,
            "agent_id": qp.get("agent_id") or None,
            "research_id": qp.get("research_id") or None,
            "q": qp.get("q") or None,
            "limit": int(qp.get("limit") or 200),
        }
        if (qp.get("format") or "").lower() == "csv":
            csv_txt = st.research.ledger.export_csv(**filt)
            return Response(
                csv_txt,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="net-ledger.csv"',
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        return J(st.research.ledger.query(**filt))

    @route("/api/journal", ["GET"])
    async def journal(req: Request) -> Response:
        sid = req.query_params.get("session_id") or ""
        if not sid:
            return J([])
        ws = st.workspace_of(sid)
        return J(st.journal.entries(sid, Path(os.path.realpath(ws)) if ws else None))

    @route("/api/sessions/{sid}/checkpoints", ["GET"])
    async def checkpoints(req: Request) -> Response:
        s = sess_or_404(req)
        ws = st.workspace_of(s.id)
        return J(st.journal.checkpoints(s.id, Path(os.path.realpath(ws)) if ws else None))

    @route("/api/journal/{jid}/undo", ["POST"])
    async def undo(req: Request) -> Response:
        d = await jbody(req)
        try:
            r = st.journal.undo(req.path_params["jid"], force=bool(d.get("force")))
        except FileExistsError as e:
            raise PermissionError(f"conflict: {e}")
        st.audit.append(event="undo", id=req.path_params["jid"])
        return J(r)

    @route("/api/trash", ["GET", "DELETE"])
    async def trash(req: Request) -> Response:
        if req.method == "DELETE":
            n = st.journal.empty_trash()
            st.audit.append(event="empty_trash", count=n)
            return J({"ok": True, "count": n})
        return J([{"id": m["id"], "ts": m["ts"], "path": m["path"], "size": m["size"], "is_dir": m["is_dir"]} for m in st.journal.list_trash()])

    @route("/api/trash/{tid}/restore", ["POST"])
    async def trash_restore(req: Request) -> Response:
        try:
            return J({"ok": True, "path": st.journal.restore(req.path_params["tid"])})
        except FileNotFoundError:
            raise KeyError("回收站条目不存在")

    # ------------------------------------------------------------ 技能
    @route("/api/skills", ["GET"])
    async def skills_list(req: Request) -> Response:
        return J(st.skills.list())

    @route("/api/skills/packs", ["GET"])
    async def skills_packs(req: Request) -> Response:
        return J({"packs": st.skills.packs(), "lock": st.skills.verify_lock()})

    @route("/api/skills/pack", ["POST"])
    async def skills_pack_toggle(req: Request) -> Response:
        d = await jbody(req)
        pack = str(d.get("pack", "")).strip()
        enabled = bool(d.get("enabled", True))
        updated = st.skills.set_pack_enabled(pack, enabled)
        st.audit.append(event="skill_pack", pack=pack, enabled=enabled, count=len(updated))
        return J({"pack": pack, "enabled": enabled, "skills": updated, "packs": st.skills.packs()})

    @route("/api/skills/lint", ["GET"])
    async def skills_lint(req: Request) -> Response:
        return J(st.skills.lint())

    @route("/api/skills/route", ["POST"])
    async def skills_route(req: Request) -> Response:
        d = await jbody(req)
        return J({
            "matches": st.skills.route(
                str(d.get("text", "")),
                files=d.get("files") or [],
                mimes=d.get("mimes") or [],
                top_k=int(d.get("top_k") or 3),
                include_disabled=bool(d.get("include_disabled", False)),
            )
        })

    @route("/api/skills/install", ["POST"])
    async def skills_install(req: Request) -> Response:
        ct = req.headers.get("content-type", "")
        if ct.startswith("multipart/"):
            f = (await req.form()).get("file")
            if f is None or not hasattr(f, "read"):
                raise ValueError("缺少 file")
            s = await asyncio.to_thread(st.skills.install_zip, await f.read())
        else:
            d = await jbody(req)
            s = await asyncio.to_thread(st.skills.install_dir, str(d.get("path", "")))
        st.audit.append(event="skill_install", name=s["name"])
        return J(s)

    @route("/api/skills/create", ["POST"])
    async def skills_create(req: Request) -> Response:
        d = await jbody(req)
        s = st.skills.create(str(d.get("name", "")).strip(), str(d.get("description", "")), str(d.get("body", "")))
        st.audit.append(event="skill_create", name=s["name"])
        return J(s)

    @route("/api/skills/{name}", ["GET", "PUT", "DELETE"])
    async def skills_one(req: Request) -> Response:
        n = req.path_params["name"]
        if req.method == "DELETE":
            st.skills.delete(n)
            return J({"ok": True})
        if req.method == "PUT":
            d = await jbody(req)
            return J(st.skills.update(n, d.get("description"), d.get("body")))
        s = st.skills.get(n)
        if not s:
            raise KeyError("技能不存在")
        return J(s)

    @route("/api/skills/{name}/enable", ["POST"])
    async def skills_enable(req: Request) -> Response:
        d = await jbody(req)
        st.skills.set_enabled(req.path_params["name"], bool(d.get("enabled")))
        st.audit.append(event="skill_enable", name=req.path_params["name"], enabled=bool(d.get("enabled")))
        return J(st.skills.get(req.path_params["name"], with_body=False))

    # ------------------------------------------------------------ 扩展（MCP 服务器）
    @route("/api/mcp", ["GET"])
    async def mcp_list(req: Request) -> Response:
        return J(st.mcp.list_view())

    @route("/api/mcp/{name}", ["PUT", "DELETE"])
    async def mcp_item(req: Request) -> Response:
        name = req.path_params["name"]
        if req.method == "DELETE":
            if not await st.mcp.remove(name):
                raise KeyError("服务器不存在")
            return J({"ok": True})
        return J(await st.mcp.update(name, await jbody(req)))

    @route("/api/mcp/{name}/start", ["POST"])
    async def mcp_start(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        return J(await st.mcp.start_user(name))

    @route("/api/mcp/{name}/stop", ["POST"])
    async def mcp_stop(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        await st.mcp.stop(name)
        return J(st.mcp.view(name))

    @route("/api/mcp/{name}/resources", ["GET"])
    async def mcp_resources(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        if req.query_params.get("refresh") in ("1", "true"):
            await st.mcp.refresh_tools(name, st.workspace_of(req.query_params.get("session_id")))
        return J({"resources": st.mcp.view(name).get("resources", [])})

    @route("/api/mcp/{name}/resources/read", ["POST"])
    async def mcp_resource_read(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        d = await jbody(req)
        uri = str(d.get("uri") or "").strip()
        if not uri:
            raise ValueError("缺少 uri")
        return J(await st.mcp.read_resource(name, uri, st.workspace_of(d.get("session_id"))))

    @route("/api/mcp/{name}/prompts", ["GET"])
    async def mcp_prompts(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        if req.query_params.get("refresh") in ("1", "true"):
            await st.mcp.refresh_tools(name, st.workspace_of(req.query_params.get("session_id")))
        return J({"prompts": st.mcp.view(name).get("prompts", [])})

    @route("/api/mcp/{name}/prompts/get", ["POST"])
    async def mcp_prompt_get(req: Request) -> Response:
        name = req.path_params["name"]
        if st.mcp.store.get(name) is None:
            raise KeyError("服务器不存在")
        d = await jbody(req)
        pname = str(d.get("name") or "").strip()
        if not pname:
            raise ValueError("缺少提示词名称 name")
        args = d.get("arguments") if isinstance(d.get("arguments"), dict) else None
        return J(await st.mcp.get_prompt(name, pname, args, st.workspace_of(d.get("session_id"))))

    # ------------------------------------------------------------ 模型 Hub
    @route("/api/hub/recommend", ["GET"])
    async def hub_recommend(req: Request) -> Response:
        hw_info = await asyncio.to_thread(hw.detect)
        rec = await asyncio.to_thread(hub_mod.recommend_models, hw_info, st.root / "models")
        return J({**rec, "downloads": st.hub.status(), "offline": os.environ.get("LOCALAGENT_OFFLINE") == "1"})

    @route("/api/hub/downloads", ["GET"])
    async def hub_downloads(req: Request) -> Response:
        return J({"downloads": st.hub.status()})

    @route("/api/hub/download", ["POST"])
    async def hub_download(req: Request) -> Response:
        d = await jbody(req)
        url = str(d.get("url") or "").strip()
        filename = str(d.get("filename") or "").strip()
        sha = str(d.get("sha256") or "").strip()
        if d.get("id"):
            entry = next((x for x in hub_mod.CATALOG if x["id"] == d["id"]), None)
            if not entry:
                raise KeyError("未知的推荐模型 ID")
            url = url or entry["url"]
            filename = filename or entry["file"]
        if not url or not filename:
            raise ValueError("需要提供 url 和 filename（或推荐模型 id）")
        mirror = str(d.get("mirror") or ("hf-mirror" if st.cfg.get("search", "safe", default="") == "" else "official"))
        if d.get("mirror") in ("official", "hf-mirror"):
            mirror = d["mirror"]
        proxy = str(st.cfg.get("engine", "proxy", default="") or d.get("proxy") or "").strip()
        job = st.hub.start_download(url, filename, mirror=mirror, expected_sha256=sha, proxy=proxy)
        st.audit.append(event="hub_download", file=job["file"], url=job["url"])
        return J(job)

    @route("/api/hub/downloads/{tid}/cancel", ["POST"])
    async def hub_cancel(req: Request) -> Response:
        return J({"ok": st.hub.cancel(req.path_params["tid"])})

    # ------------------------------------------------------------ 记忆
    @route("/api/memory", ["GET", "POST"])
    async def memory(req: Request) -> Response:
        if req.method == "POST":
            d = await jbody(req)
            return J(st.memory.add(str(d.get("text", "")), kind=d.get("kind", "fact"), tags=d.get("tags"), pinned=bool(d.get("pinned")),
                                   scope=d.get("scope", "global"), workspace=st.cfg.get("workspace", default=""), source="user"))
        q = req.query_params
        items, total = st.memory.list(q=q.get("q", ""), kind=q.get("kind", ""), scope=q.get("scope", ""), pending=q.get("pending") == "1")
        return J({"items": items, "total": total})

    @route("/api/memory/export", ["GET"])
    async def mem_export(req: Request) -> Response:
        return Response(json.dumps(st.memory.export(), ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=memory-export.json"})

    @route("/api/memory/import", ["POST"])
    async def mem_import(req: Request) -> Response:
        d = await jbody(req)
        return J({"imported": st.memory.import_items(d.get("items") or [])})

    @route("/api/memory/{mid}", ["PATCH", "DELETE"])
    async def mem_one(req: Request) -> Response:
        mid = req.path_params["mid"]
        if req.method == "DELETE":
            return J({"ok": st.memory.delete(mid)})
        return J(st.memory.update(mid, **await jbody(req)))

    @route("/api/memory/{mid}/approve", ["POST"])
    async def mem_approve(req: Request) -> Response:
        return J(st.memory.approve(req.path_params["mid"]))

    # ------------------------------------------------------------ 其他
    @route("/api/search/test", ["POST"])
    async def search_test(req: Request) -> Response:
        d = await jbody(req)
        q = str(d.get("query", "")).strip()
        if not q:
            raise ValueError("请输入关键词")
        prov = [d["provider"]] if d.get("provider") else None
        res, errs = await search_mod.run_search(st.cfg, q, providers=prov, research=st.research)
        return J({"results": res, "errors": errs})

    @route("/api/app/quit", ["POST"])
    async def app_quit(req: Request) -> Response:
        if st.exit_cb is None:
            return J({"ok": False, "detail": "当前不是由启动器运行，请在终端里按 Ctrl+C 退出。"}, 409)
        asyncio.get_running_loop().call_later(0.3, st.exit_cb)       # 先把响应发出去
        return J({"ok": True})

    @route("/api/audit", ["GET"])
    async def audit(req: Request) -> Response:
        return J({"entries": st.audit.tail(min(int(req.query_params.get("n", 200)), 2000))})

    @route("/api/audit/verify", ["GET"])
    async def audit_verify(req: Request) -> Response:
        return J(st.audit.verify())

    @route("/api/metrics", ["GET"])
    async def api_metrics(req: Request) -> Response:
        hrs = float(req.query_params.get("hours") or 24.0)
        return J(metrics_mod.collect_runtime_metrics(st, window_hours=hrs))

    @route("/api/commands", ["GET"])
    async def api_commands(req: Request) -> Response:
        ws = req.query_params.get("workspace") or st.cfg.get("workspace", default="")
        if not ws and req.query_params.get("session_id"):
            s = st.store.get(req.query_params["session_id"])
            if s:
                ws = s.workspace
        return J({"commands": list_commands(ws or None)})

    @route("/api/permissions", ["GET"])
    async def api_permissions(req: Request) -> Response:
        ws = req.query_params.get("workspace") or ""
        if not ws and req.query_params.get("session_id"):
            s = st.store.get(req.query_params["session_id"])
            if s:
                ws = s.workspace
        ws = ws or st.cfg.get("workspace", default="")
        return J({"rules": PermissionManager(ws or None).list_rules()})

    @route("/api/hooks", ["GET"])
    async def api_hooks(req: Request) -> Response:
        ws = req.query_params.get("workspace") or ""
        if not ws and req.query_params.get("session_id"):
            s = st.store.get(req.query_params["session_id"])
            if s:
                ws = s.workspace
        ws = ws or st.cfg.get("workspace", default="")
        return J({"hooks": HookManager(ws or None).list_hooks()})

    from .routes.ops import register_ops_routes
    register_ops_routes(R, st, sess_or_404=sess_or_404, jbody=jbody, json_res=J, bad=bad)

    # ------------------------------------------------------------ 静态文件
    async def static(req: Request) -> Response:
        rel = req.path_params.get("p", "") or "index.html"
        p = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in p.parents or not p.is_file():
            return PlainTextResponse("Not found", 404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    R.append(Route("/", static, methods=["GET"]))
    R.append(Route("/{p:path}", static, methods=["GET"]))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        st._loop = asyncio.get_running_loop()
        await asyncio.to_thread(st.engine.probe)
        await asyncio.to_thread(st.journal.purge, int(st.cfg.get("security", "trash_retention_days", default=14)))
        await asyncio.to_thread(st.swarms.gc_worktrees, float(st.cfg.get("swarm", "worktree_gc_hours", default=24)))
        if st.autostart:
            asyncio.create_task(_safe(st.engine.start()))
        if st.resume_missions:
            st.missions.resume_all()
        st.mcp.start_reaper()
        try:
            yield
        finally:
            await st.research.stop_all()
            await st.missions.stop_all()
            await st.swarms.stop_all()
            from . import shell_session as _sh_sess
            await _sh_sess.close_all_shells()
            st.hub.cancel_all()
            await st.mcp.stop_all()
            await st.engine.stop()
            st.research.close()

    app = Starlette(routes=R, lifespan=lifespan)
    atexit.register(st.engine.shutdown_sync)
    atexit.register(st.mcp.shutdown_sync)
    return Guard(app, st)  # type: ignore[return-value]
