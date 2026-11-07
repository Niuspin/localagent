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
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, gguf, hw
from . import security as sec
from .config import BIN_DIR, CODE_DIR, DATA_DIR, MODELS_DIR, ROOT, SKILLS_DIR, WEB_DIR, Config
from .engine import Engine, EngineError
from .journal import Journal, atomic_write
from .loop import Agent
from .memory import Memory
from .sessions import SessionStore
from .skills import Skills
from .tools import search as search_mod

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
        self.audit = sec.AuditLog(data / "audit.jsonl")
        self.memory = Memory(data / "memory.db")
        self.skills = Skills(root / "skills", CODE_DIR / "lagent" / "builtin_skills", data / "skills_state.json")
        protected = [root / "config.json", root / "skills", root / "models", root / "bin", CODE_DIR / "lagent", CODE_DIR / "web", root / "run.py"]
        hidden = [data]
        self.agent = Agent(cfg=self.cfg, engine=self.engine, store=self.store, journal=self.journal, audit=self.audit, memory=self.memory,
                           skills=self.skills, protected=protected, hidden=hidden, notify=self.broadcast)
        self.root = root
        self.autostart = False

    def broadcast(self, event: str, data: dict) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                pass

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
        authed = sec.token_ok(cookies.get(COOKIE), st.token)
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
                h += [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                      (b"cross-origin-resource-policy", b"same-origin"), (b"x-frame-options", b"DENY")]
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
                except (ValueError, sec.SecurityError, json.JSONDecodeError) as e:
                    return bad(str(e))
                except KeyError as e:
                    return bad(str(e.args[0]) if e.args else "不存在", 404)
                except PermissionError as e:
                    return bad(str(e), 409)
                except EngineError as e:
                    return bad(str(e), 409)
            R.append(Route(path, handler, methods=methods))
            return fn
        return deco

    # ------------------------------------------------------------ 状态/配置
    def models_view() -> tuple[list[dict], list[str]]:
        models, mm = st.engine.scan()
        keys = ("file", "size_gb", "arch", "name", "moe", "experts", "experts_used", "ctx_train", "quant", "mmproj", "vision", "error")
        return [{k: m.get(k) for k in keys} for m in models], [x["file"] for x in mm]

    @route("/api/state", ["GET"])
    async def state(req: Request) -> Response:
        if st.engine.binary is None and st.engine.status == "stopped":
            await asyncio.to_thread(st.engine.probe)
        models, mms = await asyncio.to_thread(models_view)
        return J({"version": __version__, "workspace": st.cfg.get("workspace", default=""), "engine": st.engine.state(), "models": models,
                  "mmprojs": mms, "hw": await asyncio.to_thread(hw.detect), "sandbox": sec.sandbox_info(),
                  "permission_mode": st.cfg.get("agent", "permission_mode"), "tainted": False})

    @route("/api/config", ["GET", "PATCH"])
    async def config(req: Request) -> Response:
        if req.method == "PATCH":
            d = await jbody(req)
            if "workspace" in d:
                raise ValueError("工作区请通过 PUT /api/workspace 设置")
            out = st.cfg.patch(d)
            st.audit.append(event="config", keys=sorted(d.keys()))
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

    @route("/api/engine/plan", ["POST"])
    async def e_plan(req: Request) -> Response:
        d = await jbody(req)
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
            return J({"ok": True})
        if req.method == "PATCH":
            d = await jbody(req)
            if "title" in d:
                s.title = str(d["title"]).strip()[:120] or s.title
            if "pinned" in d:
                s.pinned = bool(d["pinned"])
            if "workspace" in d:
                s.workspace = str(sec.validate_workspace(d["workspace"])) if d["workspace"] else ""
            st.store.save(s)
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
        return J({"path": g.rel(dest), "size": len(data)})

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

    @route("/api/sessions/{sid}/export", ["GET"])
    async def export(req: Request) -> Response:
        s = sess_or_404(req)
        out = [f"# {s.title}\n"]
        for m in s.ui:
            if m["role"] == "user":
                out.append(f"## 🧑 用户\n\n{m.get('text', '')}\n")
            else:
                for b in m.get("blocks", []):
                    if b["type"] == "text":
                        out.append(f"## 🤖 助手\n\n{b['text']}\n")
                    elif b["type"] == "tool":
                        out.append(f"> 🔧 {b.get('title')} — {b.get('status')}\n")
        return Response("\n".join(out), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=session-{s.id}.md"})

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

    @route("/api/journal", ["GET"])
    async def journal(req: Request) -> Response:
        sid = req.query_params.get("session_id") or ""
        if not sid:
            return J([])
        ws = st.workspace_of(sid)
        return J(st.journal.entries(sid, Path(os.path.realpath(ws)) if ws else None))

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
        res, errs = await search_mod.run_search(st.cfg, q, providers=prov)
        return J({"results": res, "errors": errs})

    @route("/api/audit", ["GET"])
    async def audit(req: Request) -> Response:
        return J({"entries": st.audit.tail(min(int(req.query_params.get("n", 200)), 2000))})

    @route("/api/audit/verify", ["GET"])
    async def audit_verify(req: Request) -> Response:
        return J(st.audit.verify())

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
        await asyncio.to_thread(st.engine.probe)
        await asyncio.to_thread(st.journal.purge, int(st.cfg.get("security", "trash_retention_days", default=14)))
        if st.autostart:
            asyncio.create_task(_safe(st.engine.start()))
        try:
            yield
        finally:
            await st.engine.stop()

    app = Starlette(routes=R, lifespan=lifespan)
    atexit.register(st.engine.shutdown_sync)
    return Guard(app, st)  # type: ignore[return-value]
