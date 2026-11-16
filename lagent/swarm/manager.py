"""SwarmManager：编排 DAG 执行、预算控制、Git Worktree 隔离、MergeQueue 与状态机恢复。"""
from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import security as sec
from ..events import enrich_event
from ..journal import Journal
from .budget import BudgetLedger
from .graph import TERMINAL_STATUSES, TaskGraph, TaskNode
from .locks import FileLeaseManager
from .mailbox import Mailbox
from .merge import MergeQueue, MergeResult
from .persist import SwarmStore
from .roles import effective_permission, get_role
from .scheduler import SwarmScheduler

RunnerFn = Callable[[TaskNode, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class SwarmInstance:
    id: str
    session_id: str
    goal: str
    workspace: str
    parent_mode: str
    explicit_auto_worker: bool
    graph: TaskGraph
    budget: BudgetLedger
    mailbox: Mailbox
    merges: MergeQueue
    status: str = "running"          # running | paused | done | failed | cancelled
    tainted: bool = False
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))
    created_at: float = field(default_factory=time.time)
    mission_id: str | None = None
    driver_task: asyncio.Task | None = None
    active_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    cancel_evt: asyncio.Event = field(default_factory=asyncio.Event)
    pause_evt: asyncio.Event = field(default_factory=asyncio.Event)  # set() = 未暂停，clear() = 已暂停
    done_evt: asyncio.Event = field(default_factory=asyncio.Event)
    live_subs: set[asyncio.Queue] = field(default_factory=set)
    base_sha: str = ""
    worktrees: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pause_evt.set()


class SwarmManager:
    """管理全部 Swarm 实例的生命周期、调度与持久化。"""

    def __init__(
        self,
        *,
        cfg: Any,
        store: SwarmStore,
        journal: Journal | None = None,
        audit: sec.AuditLog | None = None,
        notify: Callable[[str, dict[str, Any]], None] | None = None,
        worktree_root: Path | None = None,
        default_runner: RunnerFn | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.journal = journal
        self.audit = audit
        self.notify = notify or (lambda _e, _d: None)
        self.worktree_root = Path(worktree_root) if worktree_root else (self.store.dir.parent / "worktrees" / "swarm")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.default_runner = default_runner
        self.leases = FileLeaseManager()
        self.scheduler = SwarmScheduler(
            slots=int(cfg.get("engine", "slots", default=1) or 1) if cfg.get("engine", "slots", default=1) != "auto" else 2,
            max_agents=cfg.get("swarm", "max_agents", default="auto"),
            role_limits=cfg.get("swarm", "role_limits", default={}),
        )
        self.swarms: dict[str, SwarmInstance] = {}
        self._node_runner: dict[str, RunnerFn] = {}

    def sync_scheduler_from_cfg(self, slots_override: int | None = None) -> None:
        raw_slots = slots_override if slots_override is not None else self.cfg.get("engine", "slots", default=1)
        slots = 2 if raw_slots == "auto" else max(1, int(raw_slots or 1))
        self.scheduler.reconfigure(
            slots=slots,
            max_agents=self.cfg.get("swarm", "max_agents", default="auto"),
            role_limits=self.cfg.get("swarm", "role_limits", default={}),
        )

    def _emit(self, sw: SwarmInstance, kind: str, data: dict[str, Any], *, agent_id: str | None = None) -> None:
        payload = enrich_event(
            kind,
            data,
            agent_id=agent_id,
            swarm_id=sw.id,
            mission_id=sw.mission_id,
        )
        seq = self.store.append_event(sw.id, {"event": kind, "data": payload})
        for q in list(sw.live_subs):
            try:
                q.put_nowait((seq, kind, payload))
            except asyncio.QueueFull:
                pass
        self.notify(kind, payload)

    def _persist(self, sw: SwarmInstance) -> None:
        self.store.save_state(
            sw.id,
            {
                "id": sw.id,
                "session_id": sw.session_id,
                "goal": sw.goal,
                "workspace": sw.workspace,
                "parent_mode": sw.parent_mode,
                "explicit_auto_worker": sw.explicit_auto_worker,
                "status": sw.status,
                "tainted": sw.tainted,
                "created_at": sw.created_at,
                "mission_id": sw.mission_id,
                "base_sha": sw.base_sha,
                "graph": sw.graph.to_dict(),
                "budget": sw.budget.snapshot(),
                "merges": sw.merges.history,
                "conflicts": [c.to_dict() for c in sw.merges.conflicts.values()],
            },
        )

    def snapshot(self, swarm_id: str) -> dict[str, Any] | None:
        sw = self.swarms.get(swarm_id)
        if sw:
            self._persist(sw)
            return self.store.load_state(swarm_id)
        return self.store.load_state(swarm_id)

    def list_swarms(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        for sw in list(self.swarms.values()):
            self._persist(sw)
        return self.store.list_states(session_id=session_id)

    @staticmethod
    def _git_cmd(cwd: Path, *args: str, timeout: int = 15) -> tuple[int, str, str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return r.returncode, r.stdout or "", r.stderr or ""
        except Exception as e:  # noqa: BLE001
            return 1, "", str(e)

    def _create_worktree(self, sw: SwarmInstance, node: TaskNode) -> tuple[str, str]:
        ws = Path(sw.workspace)
        if not sw.workspace or not ws.is_dir():
            return "", "worker 需要有效的本地 Git 工作区"
        rc, head, _ = self._git_cmd(ws, "rev-parse", "HEAD")
        if rc != 0 or not head.strip():
            return "", "worker 隔离模式要求工作区是已初始化提交的 Git 仓库"
        if not sw.base_sha:
            sw.base_sha = head.strip()
        wt_dir = self.worktree_root / sw.id / node.id
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        if wt_dir.exists():
            self._remove_worktree(sw, node.id)
        rc_w, _o, err = self._git_cmd(ws, "worktree", "add", "--detach", str(wt_dir), sw.base_sha)
        if rc_w != 0:
            return "", f"创建 worktree 失败: {err.strip()}"
        sw.worktrees[node.id] = wt_dir
        node.worktree = str(wt_dir)
        return str(wt_dir), ""

    def _remove_worktree(self, sw: SwarmInstance, node_id: str) -> None:
        wt_dir = sw.worktrees.pop(node_id, None) or (self.worktree_root / sw.id / node_id)
        ws = Path(sw.workspace) if sw.workspace else None
        if ws and ws.is_dir():
            self._git_cmd(ws, "worktree", "remove", "--force", str(wt_dir))
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)

    def create_swarm(
        self,
        *,
        session_id: str,
        goal: str,
        nodes: list[dict[str, Any]],
        workspace: str = "",
        parent_mode: str = "ask",
        explicit_auto_worker: bool = False,
        budget: dict[str, Any] | None = None,
        mission_id: str | None = None,
        runner: RunnerFn | None = None,
        start: bool = True,
    ) -> SwarmInstance:
        max_children = int(self.cfg.get("swarm", "max_children", default=4) or 4)
        max_depth = int(self.cfg.get("swarm", "max_depth", default=2) or 2)
        if len(nodes) > max_children * 4:
            raise ValueError(f"单次 DAG 节点数超出安全限制 ({max_children * 4})")

        task_nodes: list[TaskNode] = []
        for idx, spec_d in enumerate(nodes):
            role_spec = get_role(str(spec_d.get("role") or spec_d.get("mode") or "researcher"))
            depth = int(spec_d.get("depth", 1) or 1)
            if depth > max_depth:
                raise ValueError(f"代理深度 {depth} 超过 swarm.max_depth={max_depth}")
            nid = str(spec_d.get("id") or f"{role_spec.name}_{idx + 1}_{uuid.uuid4().hex[:6]}")
            nd = TaskNode(
                id=nid,
                role=role_spec.name,
                goal=str(spec_d.get("goal") or spec_d.get("prompt") or "").strip(),
                deps=[str(x) for x in (spec_d.get("deps") or [])],
                mode=role_spec.mode,
                budget=dict(spec_d.get("budget") or {}),
                parent=spec_d.get("parent") or "main",
                depth=depth,
                apply_changes=bool(spec_d.get("apply", True)),
            )
            if not nd.goal:
                raise ValueError(f"节点 {nid} 缺少 goal")
            task_nodes.append(nd)

        graph = TaskGraph(task_nodes)
        sw_id = f"sw_{uuid.uuid4().hex[:10]}"
        default_b = self.cfg.get("swarm", "default_budget", default={}) or {}
        merged_b = {**default_b, **(budget or {})}
        ledger = BudgetLedger.from_config(merged_b)
        for nd in graph.nodes.values():
            role_spec = get_role(nd.role)
            nd_b = {"steps": role_spec.default_steps, "tokens": role_spec.default_tokens, **nd.budget}
            ledger.ensure_agent(nd.id, nd_b)

        sw = SwarmInstance(
            id=sw_id,
            session_id=session_id,
            goal=goal,
            workspace=workspace,
            parent_mode=parent_mode,
            explicit_auto_worker=explicit_auto_worker,
            graph=graph,
            budget=ledger,
            mailbox=Mailbox(self.store.mailbox_path(sw_id)),
            merges=MergeQueue(self.journal),
            mission_id=mission_id,
        )
        if sw.workspace and Path(sw.workspace).is_dir():
            rc, sha, _ = self._git_cmd(Path(sw.workspace), "rev-parse", "HEAD")
            if rc == 0 and sha.strip():
                sw.base_sha = sha.strip()

        self.swarms[sw.id] = sw
        if runner:
            self._node_runner[sw.id] = runner
        self._persist(sw)
        self._emit(sw, "swarm_started", {"goal": goal, "node_count": len(graph.nodes)})
        if self.audit:
            self.audit.append(event="swarm_started", session=session_id, swarm_id=sw.id, mission_id=mission_id, nodes=len(graph.nodes))
        if start:
            sw.driver_task = asyncio.create_task(self._drive(sw))
        return sw

    def spawn_node(self, swarm_id: str, spec_d: dict[str, Any], *, parent_depth: int = 0) -> TaskNode:
        sw = self.swarms[swarm_id]
        max_depth = int(self.cfg.get("swarm", "max_depth", default=2) or 2)
        max_children = int(self.cfg.get("swarm", "max_children", default=4) or 4)
        parent_id = str(spec_d.get("parent") or "main")
        sibs = sum(1 for n in sw.graph.nodes.values() if n.parent == parent_id)
        if sibs >= max_children:
            raise ValueError(f"父节点 {parent_id} 的子代理数量已达上限 ({max_children})")
        depth = parent_depth + 1
        if depth > max_depth:
            raise ValueError(f"子代理递归深度已达 swarm.max_depth={max_depth}")

        role_spec = get_role(str(spec_d.get("role") or spec_d.get("mode") or "researcher"))
        nid = str(spec_d.get("id") or f"{role_spec.name}_{uuid.uuid4().hex[:6]}")
        nd = TaskNode(
            id=nid,
            role=role_spec.name,
            goal=str(spec_d.get("goal") or spec_d.get("prompt") or "").strip(),
            deps=[str(x) for x in (spec_d.get("deps") or [])],
            mode=role_spec.mode,
            budget=dict(spec_d.get("budget") or {}),
            parent=parent_id,
            depth=depth,
            apply_changes=bool(spec_d.get("apply", True)),
        )
        sw.graph.add_node(nd)
        sw.budget.ensure_agent(nd.id, {"steps": role_spec.default_steps, "tokens": role_spec.default_tokens, **nd.budget})
        self._persist(sw)
        if sw.driver_task is None or sw.driver_task.done():
            sw.status = "running"
            sw.done_evt.clear()
            sw.driver_task = asyncio.create_task(self._drive(sw))
        return nd

    async def _run_node(self, sw: SwarmInstance, node: TaskNode) -> None:
        role_spec = get_role(node.role)
        eff_mode = effective_permission(sw.parent_mode, node.role, explicit_auto_worker=sw.explicit_auto_worker)
        if role_spec.write_capable and eff_mode == "plan":
            node.status = "failed"
            node.error = "父会话处于只读 plan 模式，禁止执行写类型 worker"
            node.ended_at = time.time()
            self._emit(sw, "agent_blocked", {"reason": node.error}, agent_id=node.id)
            return

        acquired = await self.scheduler.acquire(role=role_spec.name, priority=role_spec.priority)
        if not acquired:
            return
        eff_ws = sw.workspace
        try:
            if sw.cancel_evt.is_set():
                node.status = "cancelled"
                node.ended_at = time.time()
                return
            await sw.pause_evt.wait()

            if role_spec.write_capable:
                wt_ws, err = await asyncio.to_thread(self._create_worktree, sw, node)
                if err:
                    node.status = "failed"
                    node.error = err
                    node.ended_at = time.time()
                    self._emit(sw, "agent_blocked", {"reason": err}, agent_id=node.id)
                    return
                eff_ws = wt_ws

            node.status = "running"
            node.started_at = time.time()
            sw.budget.mark_started(node.id)
            self._emit(sw, "agent_spawned", {"role": role_spec.name, "goal": node.goal[:200], "permission": eff_mode, "worktree": bool(node.worktree)}, agent_id=node.id)
            if self.audit:
                self.audit.append(event="agent_spawned", session=sw.session_id, swarm_id=sw.id, agent_id=node.id, parent_id=node.parent, mission_id=sw.mission_id, role=role_spec.name, mode=eff_mode)

            runner = self._node_runner.get(sw.id) or self.default_runner
            if runner is None:
                res_d = {"status": "done", "result": f"[{role_spec.name}] 已就绪并完成占位执行: {node.goal}", "steps": 1, "tokens": 0, "tainted": False}
            else:
                ctx_info = {
                    "swarm": sw,
                    "workspace": eff_ws,
                    "main_workspace": sw.workspace,
                    "permission_mode": eff_mode,
                    "role": role_spec,
                    "mailbox": sw.mailbox,
                    "budget": sw.budget,
                    "leases": self.leases,
                    "emit_step": lambda st_d: self._emit(sw, "agent_step", st_d, agent_id=node.id),
                }
                res_d = await runner(node, ctx_info)

            steps = int(res_d.get("steps", 1) or 0)
            tokens = int(res_d.get("tokens", 0) or 0)
            tcalls = int(res_d.get("tool_calls", 0) or 0)
            bcheck = sw.budget.record(node.id, steps=steps, tokens=tokens, tool_calls=tcalls)
            if bcheck.get("warn"):
                self._emit(sw, "budget_warn", bcheck["warn"], agent_id=node.id)

            raw_res = str(res_d.get("result") or "")
            tainted = bool(res_d.get("tainted", False))
            if tainted:
                sw.tainted = True
                raw_res = sec.wrap_untrusted(raw_res, f"swarm:{node.id}", sw.nonce)

            # worktree 串行合并队列
            merge_note = ""
            if role_spec.write_capable and node.worktree and node.apply_changes and sw.workspace:
                strategy = self.cfg.get("swarm", "merge_strategy", default="queue")
                if strategy == "queue":
                    self._emit(sw, "merge_started", {"worktree": node.worktree}, agent_id=node.id)
                    mr: MergeResult = await sw.merges.enqueue_worktree(
                        workspace=Path(sw.workspace),
                        worktree=Path(node.worktree),
                        base_sha=sw.base_sha,
                        agent_id=node.id,
                        session_id=sw.session_id,
                    )
                    if not mr.ok:
                        node.status = "failed"
                        node.error = mr.error
                        node.ended_at = time.time()
                        self._emit(sw, "merge_conflict", mr.to_dict(), agent_id=node.id)
                        return
                    if mr.merged_files:
                        merge_note = f"\n[MergeQueue 已合并文件: {', '.join(mr.merged_files)}]"

            node.result = raw_res + merge_note
            node.artifacts = list(res_d.get("artifacts") or [])
            node.tainted = tainted
            node.status = "done" if res_d.get("status", "done") == "done" else "failed"
            if node.status != "done":
                node.error = str(res_d.get("error") or "执行失败")
            node.ended_at = time.time()

            sw.mailbox.post(
                node.id,
                node.parent or "main",
                "result",
                node.result[:4000],
                artifacts=node.artifacts,
                tainted=node.tainted,
            )
            self._emit(sw, "agent_done", {"status": node.status, "tainted": node.tainted, "artifacts": node.artifacts, "error": node.error}, agent_id=node.id)
            if self.audit:
                self.audit.append(event="agent_done", session=sw.session_id, swarm_id=sw.id, agent_id=node.id, parent_id=node.parent, mission_id=sw.mission_id, status=node.status, tainted=node.tainted)
        except asyncio.CancelledError:
            node.status = "cancelled"
            node.error = "任务已取消"
            node.ended_at = time.time()
            self._emit(sw, "agent_done", {"status": "cancelled"}, agent_id=node.id)
            raise
        except Exception as e:  # noqa: BLE001
            node.status = "failed"
            node.error = str(e)
            node.ended_at = time.time()
            self._emit(sw, "agent_done", {"status": "failed", "error": str(e)}, agent_id=node.id)
        finally:
            self.leases.release_owner(node.id)
            if node.worktree:
                await asyncio.to_thread(self._remove_worktree, sw, node.id)
            await self.scheduler.release(role_spec.name)
            self._persist(sw)

    async def _drive(self, sw: SwarmInstance) -> None:
        try:
            while not sw.cancel_evt.is_set():
                await sw.pause_evt.wait()
                # 超出 Swarm 全局预算则阻止继续调度新节点
                b_err = sw.budget.check()
                if b_err:
                    for nd in sw.graph.nodes.values():
                        if nd.status in ("pending", "ready"):
                            nd.status = "blocked"
                            nd.error = b_err
                            nd.ended_at = time.time()
                    self._emit(sw, "budget_warn", {"scope": "swarm", "reason": b_err, "ratio": 1.0})
                    break

                ready = sw.graph.ready_nodes()
                for nd in ready:
                    if nd.id in sw.active_tasks and not sw.active_tasks[nd.id].done():
                        continue
                    t = asyncio.create_task(self._run_node(sw, nd))
                    sw.active_tasks[nd.id] = t

                # 清理已结束子任务引用
                for nid in [k for k, t in sw.active_tasks.items() if t.done()]:
                    sw.active_tasks.pop(nid, None)

                sw.graph.refresh_ready()
                if sw.graph.is_complete() and not sw.active_tasks:
                    break
                if not sw.active_tasks and not sw.graph.ready_nodes():
                    break
                if sw.active_tasks:
                    await asyncio.wait(list(sw.active_tasks.values()), timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0.02)

            if sw.cancel_evt.is_set():
                sw.status = "cancelled"
            elif any(n.status == "failed" for n in sw.graph.nodes.values()):
                sw.status = "failed"
            else:
                sw.status = "done"
        finally:
            sw.done_evt.set()
            self._persist(sw)
            self._emit(sw, "swarm_done", {"status": sw.status, "tainted": sw.tainted, "counts": sw.graph.to_dict()["counts"]})

    async def await_agents(self, swarm_id: str, agent_ids: list[str] | None = None, *, timeout_s: float = 120.0) -> dict[str, Any]:
        sw = self.swarms.get(swarm_id)
        if not sw:
            st = self.store.load_state(swarm_id)
            return st or {"error": "swarm 不存在"}
        targets = set(agent_ids or list(sw.graph.nodes))
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            if all(sw.graph.nodes[aid].status in TERMINAL_STATUSES for aid in targets if aid in sw.graph.nodes):
                break
            await asyncio.sleep(0.03)
        results = {}
        for aid in targets:
            nd = sw.graph.nodes.get(aid)
            if nd:
                results[aid] = {
                    "status": nd.status,
                    "role": nd.role,
                    "result": nd.result,
                    "error": nd.error,
                    "artifacts": nd.artifacts,
                    "tainted": nd.tainted,
                }
        return {"swarm_id": swarm_id, "tainted": sw.tainted, "agents": results, "timed_out": any(r["status"] not in TERMINAL_STATUSES for r in results.values())}

    def pause(self, swarm_id: str) -> bool:
        sw = self.swarms.get(swarm_id)
        if not sw or sw.status in ("done", "cancelled", "failed"):
            return False
        sw.pause_evt.clear()
        sw.status = "paused"
        self._persist(sw)
        self._emit(sw, "swarm", {"status": "paused"})
        return True

    def resume(self, swarm_id: str, *, runner: RunnerFn | None = None) -> SwarmInstance | None:
        sw = self.swarms.get(swarm_id)
        if sw:
            if sw.status == "paused":
                sw.status = "running"
                sw.pause_evt.set()
                self._persist(sw)
                self._emit(sw, "swarm", {"status": "running"})
            return sw
        # 崩溃后从磁盘恢复
        st = self.store.load_state(swarm_id)
        if not st:
            return None
        graph = SwarmStore.prepare_resume_graph(st.get("graph") or {})
        ledger = BudgetLedger.from_config((st.get("budget") or {}).get("totals", {}).get("limits"))
        sw = SwarmInstance(
            id=swarm_id,
            session_id=st.get("session_id", ""),
            goal=st.get("goal", ""),
            workspace=st.get("workspace", ""),
            parent_mode=st.get("parent_mode", "ask"),
            explicit_auto_worker=bool(st.get("explicit_auto_worker", False)),
            graph=graph,
            budget=ledger,
            mailbox=Mailbox(self.store.mailbox_path(swarm_id)),
            merges=MergeQueue(self.journal),
            status="running",
            tainted=bool(st.get("tainted", False)),
            created_at=float(st.get("created_at") or time.time()),
            mission_id=st.get("mission_id"),
            base_sha=st.get("base_sha", ""),
        )
        if runner:
            self._node_runner[sw.id] = runner
        self.swarms[sw.id] = sw
        if not graph.is_complete():
            sw.driver_task = asyncio.create_task(self._drive(sw))
        else:
            sw.status = "done" if not any(n.status == "failed" for n in graph.nodes.values()) else "failed"
            sw.done_evt.set()
        self._persist(sw)
        return sw

    async def cancel(self, swarm_id: str) -> bool:
        sw = self.swarms.get(swarm_id)
        if not sw:
            return False
        sw.cancel_evt.set()
        sw.pause_evt.set()
        cur_loop = asyncio.get_running_loop()
        for aid, t in list(sw.active_tasks.items()):
            if t.get_loop() is cur_loop and not cur_loop.is_closed():
                t.cancel()
        for aid in list(sw.graph.nodes):
            sw.graph.cascade_cancel(aid, "Swarm 已取消")
        same_loop_tasks = [t for t in sw.active_tasks.values() if t.get_loop() is cur_loop and not t.get_loop().is_closed()]
        if same_loop_tasks:
            await asyncio.gather(*same_loop_tasks, return_exceptions=True)
        if sw.driver_task and not sw.driver_task.done() and sw.driver_task.get_loop() is cur_loop and not cur_loop.is_closed():
            try:
                await asyncio.wait_for(sw.driver_task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sw.driver_task.cancel()
        for aid in list(sw.worktrees):
            self._remove_worktree(sw, aid)
        sw.status = "cancelled"
        sw.done_evt.set()
        self._persist(sw)
        return True

    async def cancel_agent(self, swarm_id: str, agent_id: str) -> list[str]:
        sw = self.swarms.get(swarm_id)
        if not sw:
            return []
        affected = sw.graph.cascade_cancel(agent_id)
        for aid in affected:
            t = sw.active_tasks.get(aid)
            if t and not t.done():
                t.cancel()
        sw.mailbox.post("main", agent_id, "cancel", "操作者已请求取消该子代理")
        self._persist(sw)
        return affected

    def send_message(self, swarm_id: str, to_agent: str, body: str, *, from_agent: str = "main", kind: str = "handoff") -> dict[str, Any] | None:
        sw = self.swarms.get(swarm_id)
        if sw:
            msg = sw.mailbox.post(from_agent, to_agent, kind, body)
        else:
            msg = Mailbox(self.store.mailbox_path(swarm_id)).post(from_agent, to_agent, kind, body)
        if sw:
            self._emit(sw, "agent_message", msg.to_dict(), agent_id=to_agent)
        return msg.to_dict()

    def gc_worktrees(self, max_age_hours: float = 24.0) -> int:
        removed = 0
        cutoff = time.time() - max(1.0, float(max_age_hours)) * 3600
        if not self.worktree_root.is_dir():
            return 0
        for sub in list(self.worktree_root.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name in self.swarms and not self.swarms[sub.name].done_evt.is_set():
                continue
            try:
                if sub.stat().st_mtime < cutoff:
                    shutil.rmtree(sub, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        return removed

    async def stop_all(self) -> None:
        for sid in list(self.swarms):
            await self.cancel(sid)
