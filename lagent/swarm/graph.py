"""Swarm 任务依赖有向无环图（DAG）：环检测、拓扑就绪推进与级联阻塞/取消。"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

NODE_STATUSES: tuple[str, ...] = (
    "pending",
    "ready",
    "running",
    "done",
    "failed",
    "cancelled",
    "blocked",
)
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "blocked"})
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


class CycleError(ValueError):
    """任务依赖存在环时抛出。"""


@dataclass
class TaskNode:
    id: str
    role: str
    goal: str
    deps: list[str] = field(default_factory=list)
    mode: str = "explore"
    budget: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    artifacts: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    tainted: bool = False
    parent: str | None = None
    depth: int = 1
    apply_changes: bool = True
    worktree: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskNode":
        valid = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in valid}
        if not kw.get("id"):
            kw["id"] = f"ag_{uuid.uuid4().hex[:8]}"
        if kw.get("status") not in NODE_STATUSES:
            kw["status"] = "pending"
        return cls(**kw)


class TaskGraph:
    """线程外部加锁使用的纯内存 DAG 状态机。"""

    def __init__(self, nodes: list[TaskNode] | None = None):
        self.nodes: dict[str, TaskNode] = {}
        if nodes:
            self.add_batch(nodes)

    @staticmethod
    def validate_id(nid: str) -> str:
        if not nid or not _NODE_ID_RE.match(nid):
            raise ValueError(f"非法的任务节点 ID: {nid!r}")
        return nid

    def _has_cycle(self, candidate: dict[str, TaskNode]) -> bool:
        # 0 = unvisited, 1 = visiting, 2 = visited
        state: dict[str, int] = {}

        def dfs(u: str) -> bool:
            st = state.get(u, 0)
            if st == 1:
                return True
            if st == 2:
                return False
            state[u] = 1
            node = candidate.get(u)
            if node:
                for dep in node.deps:
                    if dep == u:
                        return True
                    if dep in candidate and dfs(dep):
                        return True
            state[u] = 2
            return False

        return any(dfs(nid) for nid in candidate)

    def add_batch(self, nodes: list[TaskNode]) -> list[TaskNode]:
        candidate = dict(self.nodes)
        added: list[TaskNode] = []
        for raw in nodes:
            nid = self.validate_id(raw.id or f"ag_{uuid.uuid4().hex[:8]}")
            if nid in candidate:
                raise ValueError(f"重复的任务节点 ID: {nid}")
            if nid in raw.deps:
                raise CycleError(f"节点 {nid} 不能依赖自身")
            raw.id = nid
            candidate[nid] = raw
            added.append(raw)
        for nid, node in candidate.items():
            for dep in node.deps:
                if dep not in candidate:
                    raise ValueError(f"节点 {nid} 依赖了不存在的前置节点: {dep}")
        if self._has_cycle(candidate):
            raise CycleError("任务依赖图中检测到循环依赖")
        self.nodes = candidate
        self.refresh_ready()
        return added

    def add_node(self, node: TaskNode) -> TaskNode:
        return self.add_batch([node])[0]

    def refresh_ready(self) -> list[str]:
        """推进可执行节点至 ready，并在前置失败/取消/阻塞时级联标记 blocked。返回本次新变为 ready 的节点 ID。"""
        promoted: list[str] = []
        changed = True
        while changed:
            changed = False
            for nid, node in self.nodes.items():
                if node.status not in ("pending", "ready"):
                    continue
                dep_states = [self.nodes[d].status for d in node.deps if d in self.nodes]
                if any(s in ("failed", "cancelled", "blocked") for s in dep_states):
                    if node.status != "blocked":
                        node.status = "blocked"
                        node.error = node.error or "前置依赖未成功完成，已级联阻塞"
                        node.ended_at = node.ended_at or time.time()
                        changed = True
                    continue
                if all(s == "done" for s in dep_states) and node.status == "pending":
                    node.status = "ready"
                    promoted.append(nid)
                    changed = True
        return promoted

    def ready_nodes(self) -> list[TaskNode]:
        self.refresh_ready()
        return [n for n in self.nodes.values() if n.status == "ready"]

    def cascade_cancel(self, node_id: str, reason: str = "任务已取消") -> list[str]:
        """取消指定节点及其所有未完成的下游依赖节点，返回受影响节点 ID 列表。"""
        affected: list[str] = []
        if node_id not in self.nodes:
            return affected
        root = self.nodes[node_id]
        if root.status not in TERMINAL_STATUSES:
            root.status = "cancelled"
            root.error = reason
            root.ended_at = time.time()
            affected.append(node_id)

        # 传播至后续依赖者
        queue = [node_id]
        while queue:
            cur = queue.pop(0)
            for nid, node in self.nodes.items():
                if cur in node.deps and node.status not in TERMINAL_STATUSES:
                    node.status = "cancelled"
                    node.error = f"因依赖节点 {cur} 取消而级联取消"
                    node.ended_at = time.time()
                    affected.append(nid)
                    queue.append(nid)
        return affected

    def is_complete(self) -> bool:
        return bool(self.nodes) and all(n.status in TERMINAL_STATUSES for n in self.nodes.values())

    def to_dict(self) -> dict[str, Any]:
        edges = [{"from": dep, "to": n.id} for n in self.nodes.values() for dep in n.deps]
        counts: dict[str, int] = {s: 0 for s in NODE_STATUSES}
        for n in self.nodes.values():
            counts[n.status] = counts.get(n.status, 0) + 1
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": edges,
            "counts": counts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskGraph":
        g = cls()
        raw_nodes = [TaskNode.from_dict(x) for x in (d.get("nodes") or []) if isinstance(x, dict)]
        if raw_nodes:
            g.nodes = {n.id: n for n in raw_nodes}
        return g
