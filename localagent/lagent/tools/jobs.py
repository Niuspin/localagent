"""后台命令：构建、监视测试、开发服务器这类“不会很快结束”的进程。

设计要点
- 启动方式与前台 run_command 完全一致（prepare_exec：同样的硬拒绝清单、环境清洗、沙箱包装），
  审批也走同一套 exec 策略——后台只是“不等它结束”，不是绕过检查。
- 输出只保留最近 400KB（环形截断）；每个会话最多 4 个、全局最多 12 个同时运行；单个任务最长存活 4 小时。
- 任务属于会话，跨多轮对话存在；删除会话、程序退出时一并终止。
- 沙箱默认断网：后台开发服务器只在沙箱内部可达（同一条命令里 curl 它可以，另开命令则看不到）。
  需要从别处访问请在 设置→安全 开启“允许命令联网”，或在没有沙箱的平台使用。
"""
from __future__ import annotations

import asyncio
import atexit
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import security as sec
from .base import P, Tool, ToolResult, clip, obj, register
from .ctx import ToolCtx
from .files import err, kill_tree, prepare_exec

MAX_PER_SESSION = 4
MAX_TOTAL = 12
MAX_BUF = 400_000
MAX_LIFETIME_S = 4 * 3600
KEEP_FINISHED_S = 1800


@dataclass
class Job:
    id: str
    sid: str
    command: str
    proc: Any
    tag: str
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    code: int | None = None
    stopped: bool = False
    dropped: int = 0
    buf: bytearray = field(default_factory=bytearray)
    tasks: list = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.ended == 0.0

    def text(self) -> str:
        t = self.buf.decode("utf-8", "replace")
        if sec.IS_WIN and "�" in t:
            t = bytes(self.buf).decode("gb18030", "replace")
        return t

    def status(self) -> str:
        age = int((self.ended or time.time()) - self.started)
        if self.running:
            return f"运行中 {age}s"
        if self.stopped:
            return f"已终止（运行了 {age}s）"
        return f"已结束 退出码 {self.code}（运行了 {age}s）"


JOBS: dict[str, Job] = {}


async def _pump(job: Job) -> None:
    try:
        assert job.proc.stdout
        while True:
            chunk = await job.proc.stdout.read(8192)
            if not chunk:
                break
            job.buf.extend(chunk)
            if len(job.buf) > MAX_BUF:
                cut = len(job.buf) - MAX_BUF
                del job.buf[:cut]
                job.dropped += cut
        await job.proc.wait()
    except asyncio.CancelledError:
        pass
    finally:
        job.code = job.proc.returncode
        job.ended = time.time()
        for t in job.tasks[1:]:         # 结束了就不必再留着“超时看门狗”
            t.cancel()


async def _watchdog(job: Job) -> None:
    try:
        await asyncio.sleep(MAX_LIFETIME_S)
    except asyncio.CancelledError:
        return
    if job.running:
        job.stopped = True
        kill_tree(job.proc)


def reap() -> None:
    """清理结束很久的任务记录。"""
    now = time.time()
    for jid in [j.id for j in JOBS.values() if not j.running and now - j.ended > KEEP_FINISHED_S]:
        JOBS.pop(jid, None)


def mine(sid: str) -> list[Job]:
    return [j for j in JOBS.values() if j.sid == sid]


def stop_session(sid: str) -> int:
    n = 0
    for j in mine(sid):
        if j.running:
            j.stopped = True
            kill_tree(j.proc)
            n += 1
    return n


def stop_all() -> None:
    for j in list(JOBS.values()):
        if j.running:
            j.stopped = True
            kill_tree(j.proc)


atexit.register(stop_all)


async def start_job(ctx: ToolCtx, cmd: str, cwd_arg: str | None) -> ToolResult:
    reap()
    running_mine = [j for j in mine(ctx.sid) if j.running]
    if len(running_mine) >= MAX_PER_SESSION:
        return err(f"本会话已有 {len(running_mine)} 个后台任务在运行（上限 {MAX_PER_SESSION}）。先用 job_stop 结束不需要的。")
    if sum(1 for j in JOBS.values() if j.running) >= MAX_TOTAL:
        return err(f"后台任务总数已达上限 {MAX_TOTAL}。")
    try:
        pr = prepare_exec(ctx, cmd, cwd_arg)
    except ValueError as e:
        return err(str(e))
    try:
        proc = await asyncio.create_subprocess_exec(*pr.argv, cwd=str(pr.cwd), env=pr.env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **pr.kw)
    except OSError as e:
        return err(f"无法启动命令: {e}")
    job = Job(uuid.uuid4().hex[:8], ctx.sid, cmd, proc, pr.tag)
    job.tasks = [asyncio.create_task(_pump(job)), asyncio.create_task(_watchdog(job))]
    JOBS[job.id] = job
    ctx.session.tool_groups.add("jobs")           # 之后的请求里带上 job_output / job_stop
    if ctx.audit:
        ctx.audit.append(event="job_start", session=ctx.sid, job=job.id, command=cmd[:200], sandbox=pr.tag)
    note = ""
    if pr.kind != "none" and pr.mode == "auto" and not pr.allow_net:
        note = "\n[提示: 沙箱默认断网，服务只在这条命令的沙箱内部可达；需要外部访问请让用户开启“允许命令联网”]"
    return ToolResult(f"已在后台启动 任务 {job.id}（{pr.tag}）。用 job_output 查看输出，job_stop 终止。{note}",
                      meta={"job_id": job.id, "sandbox": pr.tag})


def _find(ctx: ToolCtx, jid: str) -> Job | str:
    j = JOBS.get(jid or "")
    if j is None or j.sid != ctx.sid:
        ids = ", ".join(x.id for x in mine(ctx.sid)) or "（无）"
        return f"没有这个后台任务: {jid!r}。本会话的任务: {ids}"
    return j


async def job_output(ctx: ToolCtx, a: dict) -> ToolResult:
    reap()
    jid = (a.get("id") or "").strip()
    if not jid:
        js = mine(ctx.sid)
        if not js:
            return ToolResult("本会话没有后台任务。")
        return ToolResult("\n".join(f"{j.id}  {j.status():<28} $ {j.command[:80]}" for j in js))
    j = _find(ctx, jid)
    if isinstance(j, str):
        return err(j)
    wait = max(0.0, min(float(a.get("wait_s") or 0), 30.0))
    if wait and j.running:
        n0, t0 = len(j.buf) + j.dropped, time.time()
        while j.running and time.time() - t0 < wait and len(j.buf) + j.dropped == n0:
            if ctx.cancel is not None and ctx.cancel.is_set():
                break
            await asyncio.sleep(0.25)
    lines = j.text().split("\n")
    tail = max(1, min(int(a.get("tail_lines") or 80), 400))
    shown = lines[-tail:]
    head = f"[任务 {j.id} · {j.status()} · $ {j.command[:80]}]"
    skipped = len(lines) - len(shown)
    if skipped > 0 or j.dropped:
        head += f"\n（只显示最后 {len(shown)} 行；更早的 {skipped} 行{'及已丢弃的旧输出' if j.dropped else ''}未显示，可调大 tail_lines）"
    return ToolResult(head + "\n" + clip("\n".join(shown).strip() or "(暂无输出)", 20000, "输出过长"))


async def job_stop(ctx: ToolCtx, a: dict) -> ToolResult:
    j = _find(ctx, (a.get("id") or "").strip())
    if isinstance(j, str):
        return err(j)
    if not j.running:
        return ToolResult(f"任务 {j.id} 已经结束：{j.status()}")
    j.stopped = True
    kill_tree(j.proc)
    for _ in range(20):
        if not j.running:
            break
        await asyncio.sleep(0.25)
    for t in j.tasks[1:]:
        t.cancel()
    if ctx.audit:
        ctx.audit.append(event="job_stop", session=ctx.sid, job=j.id)
    tail = "\n".join(j.text().split("\n")[-15:]).strip()
    return ToolResult(f"已终止任务 {j.id}。最后输出:\n{tail or '(无)'}")


register(Tool("job_output", "查看后台任务的输出与状态。不传 id 则列出本会话所有后台任务。wait_s>0 时最多等待这么多秒，直到出现新输出或任务结束。",
              obj({"id": P("string", "任务 id（run_command background=true 返回的）"),
                   "tail_lines": P("integer", "显示最后多少行，默认 80"),
                   "wait_s": P("integer", "等待新输出的秒数，最多 30")}),
              job_output, "meta", title=lambda a: "查看后台任务 " + str(a.get("id") or "")))

register(Tool("job_stop", "终止一个后台任务（连同它启动的子进程）。",
              obj({"id": P("string", "任务 id")}, ["id"]),
              job_stop, "meta", title=lambda a: "终止后台任务 " + str(a.get("id") or "")))
