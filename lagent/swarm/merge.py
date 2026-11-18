"""串行三方合并队列（MergeQueue）：worker 产物合回主工作区前的原子预检、行级合并与冲突卡生成。"""
from __future__ import annotations

import asyncio
import base64
import difflib
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..journal import Journal, atomic_write, sha256_bytes
from ..tools.files import line_digest


@dataclass
class ConflictCard:
    id: str
    agent_id: str
    files: list[str]
    details: list[dict[str, Any]]
    status: str = "pending"          # pending | resolved_ours | resolved_theirs | dismissed
    created_at: float = field(default_factory=time.time)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # snapshots 仅作磁盘恢复使用，摘要视图省略大 blob 可读更清晰，但保留 has_snapshots 标志
        snaps = d.pop("snapshots", {}) or {}
        if snaps:
            d["has_snapshots"] = True
        return d

    def to_persist_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MergeResult:
    ok: bool
    merged_files: list[str] = field(default_factory=list)
    diff_summary: str = ""
    conflict: ConflictCard | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "merged_files": self.merged_files,
            "diff_summary": self.diff_summary,
            "conflict": self.conflict.to_dict() if self.conflict else None,
            "error": self.error,
        }


def _collect_hunks(base: list[str], target: list[str]) -> list[tuple[int, int, list[str]]]:
    """返回相对 base 的变更块列表：(base_start, base_end, replacement_lines)。"""
    sm = difflib.SequenceMatcher(None, base, target, autojunk=False)
    out: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.append((i1, i2, target[j1:j2]))
    return out


def _hunks_overlap(a: tuple[int, int, list[str]], b: tuple[int, int, list[str]]) -> bool:
    ai1, ai2, _ = a
    bi1, bi2, _ = b
    # 纯插入都在同一空行位置
    if ai1 == ai2 and bi1 == bi2:
        return ai1 == bi1
    # 一方为纯插入，若严格落入另一方被修改区间的内部则冲突（边界不冲突）
    if ai1 == ai2:
        return bi1 < ai1 < bi2
    if bi1 == bi2:
        return ai1 < bi1 < ai2
    return ai1 < bi2 and bi1 < ai2


def three_way_merge_text(base_text: str, ours_text: str, theirs_text: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """确定性行级三方合并；若任意变更区间冲突则不产生半成品冲突标记，返回详细冲突结构。"""
    norm_base = base_text.replace("\r\n", "\n")
    norm_ours = ours_text.replace("\r\n", "\n")
    norm_theirs = theirs_text.replace("\r\n", "\n")
    if norm_ours == norm_theirs:
        return True, ours_text, []
    if norm_base == norm_ours:
        return True, norm_theirs, []
    if norm_base == norm_theirs:
        return True, ours_text, []

    base = norm_base.splitlines(keepends=True)
    ours = norm_ours.splitlines(keepends=True)
    theirs = norm_theirs.splitlines(keepends=True)

    oh = _collect_hunks(base, ours)
    th = _collect_hunks(base, theirs)

    # 先将相同 hunk 消去冲突判定
    conflicts: list[dict[str, Any]] = []
    combined: list[tuple[int, int, list[str], str]] = []
    used_t: set[int] = set()

    for a in oh:
        matched_t = None
        for idx, b in enumerate(th):
            if idx in used_t:
                continue
            if _hunks_overlap(a, b):
                if a == b:
                    matched_t = idx
                else:
                    conflicts.append({
                        "start_line": a[0] + 1,
                        "end_line": max(a[1], b[1], a[0] + 1),
                        "base_hash": line_digest(base[a[0]]) if a[0] < len(base) else "",
                        "ours": "".join(a[2])[:600],
                        "theirs": "".join(b[2])[:600],
                    })
                    matched_t = -1
        if matched_t is not None and matched_t >= 0:
            used_t.add(matched_t)
        combined.append((a[0], a[1], a[2], "ours"))

    for idx, b in enumerate(th):
        if idx not in used_t and not any(_hunks_overlap(a, b) for a in oh):
            combined.append((b[0], b[1], b[2], "theirs"))

    if conflicts:
        return False, "", conflicts

    combined.sort(key=lambda x: (x[0], x[1]))
    out: list[str] = []
    pos = 0
    for i1, i2, rep, _side in combined:
        if i1 > pos:
            out.extend(base[pos:i1])
        out.extend(rep)
        pos = max(pos, i2)
    if pos < len(base):
        out.extend(base[pos:])
    return True, "".join(out), []


class MergeQueue:
    """严格串行的工作区合并队列：永不破坏主工作区一致性。"""

    def __init__(self, journal: Journal | None = None):
        self.journal = journal
        self._lock = asyncio.Lock()
        self.history: list[dict[str, Any]] = []
        self.conflicts: dict[str, ConflictCard] = {}
        self._payloads: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _git_show(repo: Path, rev_path: str) -> tuple[bool, bytes]:
        r = subprocess.run(
            ["git", "-C", str(repo), "show", rev_path],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0, r.stdout

    @staticmethod
    def _worktree_changed_files(worktree: Path, base_sha: str) -> list[str]:
        # 包含已修改、已新增与未跟踪文件
        r1 = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", base_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        r2 = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        files: list[str] = []
        for raw in (r1.stdout or "").splitlines() + (r2.stdout or "").splitlines():
            p = raw.strip().replace("\\", "/")
            if p and p not in files:
                files.append(p)
        return files

    async def enqueue_worktree(
        self,
        *,
        workspace: Path,
        worktree: Path,
        base_sha: str,
        agent_id: str,
        session_id: str = "",
        turn_id: str = "",
    ) -> MergeResult:
        """从隔离 worktree 提取所有变更并与当前主工作区做串行三方合并。"""
        ws = Path(workspace).resolve()
        wt = Path(worktree).resolve()
        async with self._lock:
            rel_files = await asyncio.to_thread(self._worktree_changed_files, wt, base_sha)
            if not rel_files:
                return MergeResult(ok=True, merged_files=[], diff_summary="（无文件变更）")

            file_ops: dict[str, tuple[bytes | None, bytes | None, bytes | None]] = {}
            for rel in rel_files:
                ok_base, base_b = await asyncio.to_thread(self._git_show, ws, f"{base_sha}:{rel}")
                base_val = base_b if ok_base else None
                ws_file = (ws / rel).resolve()
                wt_file = (wt / rel).resolve()
                # 安全边界防御：绝不写出工作区
                ws_file.relative_to(ws)
                wt_file.relative_to(wt)
                ours_val = ws_file.read_bytes() if ws_file.is_file() else None
                theirs_val = wt_file.read_bytes() if wt_file.is_file() else None
                file_ops[rel] = (base_val, ours_val, theirs_val)

            return self._apply_three_way_batch(
                workspace=ws,
                file_ops=file_ops,
                agent_id=agent_id,
                session_id=session_id,
                turn_id=turn_id,
            )

    async def enqueue_changes(
        self,
        *,
        workspace: Path,
        file_ops: dict[str, tuple[bytes | None, bytes | None, bytes | None]],
        agent_id: str,
        session_id: str = "",
        turn_id: str = "",
    ) -> MergeResult:
        """直接传入 {rel_path: (base_bytes, ours_bytes, theirs_bytes)} 执行原子三方合并（也便于纯单测）。"""
        ws = Path(workspace).resolve()
        async with self._lock:
            return self._apply_three_way_batch(
                workspace=ws,
                file_ops=file_ops,
                agent_id=agent_id,
                session_id=session_id,
                turn_id=turn_id,
            )

    def _apply_three_way_batch(
        self,
        *,
        workspace: Path,
        file_ops: dict[str, tuple[bytes | None, bytes | None, bytes | None]],
        agent_id: str,
        session_id: str,
        turn_id: str,
    ) -> MergeResult:
        staged: list[tuple[Path, str, bytes | None, bytes | None]] = []
        conflict_files: list[str] = []
        conflict_details: list[dict[str, Any]] = []

        for rel, (base_b, ours_b, theirs_b) in file_ops.items():
            dst = (workspace / rel).resolve()
            dst.relative_to(workspace)
            ours_live = dst.read_bytes() if dst.is_file() else ours_b

            # 情况 1：两边产物完全一致
            if ours_live == theirs_b:
                continue
            # 情况 2：主工作区未改过（含 EOL 标准化比较），直接采用 worker 结果
            if base_b is not None and ours_live is not None and theirs_b is not None:
                if ours_live.replace(b"\r\n", b"\n") == base_b.replace(b"\r\n", b"\n"):
                    staged.append((dst, rel, ours_live, theirs_b))
                    continue
                if theirs_b.replace(b"\r\n", b"\n") == base_b.replace(b"\r\n", b"\n"):
                    continue
            elif ours_live == base_b:
                staged.append((dst, rel, ours_live, theirs_b))
                continue
            elif theirs_b == base_b:
                continue
            # 情况 4：一方或两方为空（新增/删除冲突）
            if base_b is None or ours_live is None or theirs_b is None:
                conflict_files.append(rel)
                conflict_details.append({
                    "file": rel,
                    "reason": "新增或删除与主工作区存在互斥修改",
                    "ours_sha256": sha256_bytes(ours_live) if ours_live is not None else None,
                    "theirs_sha256": sha256_bytes(theirs_b) if theirs_b is not None else None,
                })
                continue
            # 情况 5：二进制文件双边均已修改
            if b"\x00" in base_b[:8192] or b"\x00" in ours_live[:8192] or b"\x00" in theirs_b[:8192]:
                conflict_files.append(rel)
                conflict_details.append({"file": rel, "reason": "二进制文件存在并发修改"})
                continue

            # 情况 6：纯文本三方行级合并
            ok, merged_txt, hunks = three_way_merge_text(
                base_b.decode("utf-8", "replace"),
                ours_live.decode("utf-8", "replace"),
                theirs_b.decode("utf-8", "replace"),
            )
            if not ok:
                conflict_files.append(rel)
                for h in hunks:
                    conflict_details.append({"file": rel, **h})
            else:
                staged.append((dst, rel, ours_live, merged_txt.encode("utf-8")))

        if conflict_files:
            snaps: dict[str, dict[str, Any]] = {}
            payload_items: list[dict[str, Any]] = []
            for rel in conflict_files:
                _b, _o, theirs_val = file_ops[rel]
                dst = (workspace / rel).resolve()
                dst.relative_to(workspace)
                ours_live = dst.read_bytes() if dst.is_file() else _o
                payload_items.append({"rel": rel, "conflict": True, "before": ours_live, "theirs": theirs_val})
                if (len(ours_live or b"") + len(theirs_val or b"")) <= 512_000:
                    snaps[rel] = {
                        "conflict": True,
                        "before_b64": base64.b64encode(ours_live).decode("ascii") if ours_live is not None else None,
                        "theirs_b64": base64.b64encode(theirs_val).decode("ascii") if theirs_val is not None else None,
                    }
            for dst, rel, before_b, after_b in staged:
                payload_items.append({"rel": rel, "conflict": False, "before": before_b, "after": after_b})
                if (len(before_b or b"") + len(after_b or b"")) <= 512_000:
                    snaps[rel] = {
                        "conflict": False,
                        "before_b64": base64.b64encode(before_b).decode("ascii") if before_b is not None else None,
                        "after_b64": base64.b64encode(after_b).decode("ascii") if after_b is not None else None,
                    }
            card = ConflictCard(
                id=f"cnf_{uuid.uuid4().hex[:10]}",
                agent_id=agent_id,
                files=conflict_files,
                details=conflict_details,
                snapshots=snaps,
            )
            self._payloads[card.id] = {
                "workspace": str(workspace),
                "session_id": session_id,
                "turn_id": turn_id or agent_id,
                "items": payload_items,
            }
            self.conflicts[card.id] = card
            return MergeResult(ok=False, conflict=card, error=f"检测到 {len(conflict_files)} 个合并冲突，主工作区保持未修改")

        merged_rels = self._commit_staged(staged, agent_id=agent_id, session_id=session_id, turn_id=turn_id)
        rec = {
            "ts": round(time.time(), 3),
            "agent_id": agent_id,
            "files": merged_rels,
            "status": "merged",
        }
        self.history.append(rec)
        return MergeResult(ok=True, merged_files=merged_rels, diff_summary=f"已安全合并 {len(merged_rels)} 个文件")

    def _commit_staged(
        self,
        staged: list[tuple[Path, str, bytes | None, bytes | None]],
        *,
        agent_id: str,
        session_id: str = "",
        turn_id: str = "",
    ) -> list[str]:
        merged_rels: list[str] = []
        for dst, rel, before_b, after_b in staged:
            existed = before_b is not None and dst.is_file()
            if after_b is None:
                tid = ""
                if self.journal and session_id and existed:
                    try:
                        tid = self.journal.trash(dst, session_id)
                    except OSError:
                        tid = ""
                if dst.exists():
                    try:
                        dst.unlink()
                    except FileNotFoundError:
                        pass
                if self.journal and session_id:
                    try:
                        self.journal.record(
                            session_id,
                            op="delete",
                            path=dst,
                            summary=f"[swarm:{agent_id}] 删除 {rel}",
                            existed=existed,
                            extra={"trash_id": tid, "turn_id": turn_id or agent_id},
                        )
                    except OSError:
                        pass
            else:
                snap = self.journal.snapshot(dst) if (self.journal and session_id and existed) else None
                dst.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(dst, after_b)
                if self.journal and session_id:
                    try:
                        self.journal.record(
                            session_id,
                            op="edit" if existed else "write",
                            path=dst,
                            summary=f"[swarm:{agent_id}] {'更新' if existed else '新增'} {rel}",
                            snap=snap,
                            existed=existed,
                            after=sha256_bytes(after_b),
                            extra={"turn_id": turn_id or agent_id},
                        )
                    except OSError:
                        pass
            merged_rels.append(rel)
        return merged_rels

    def load_conflicts(self, raw_conflicts: list[dict[str, Any]] | None) -> None:
        for rc in raw_conflicts or []:
            if not isinstance(rc, dict) or not rc.get("id"):
                continue
            cid = str(rc["id"])
            card = ConflictCard(
                id=cid,
                agent_id=str(rc.get("agent_id") or ""),
                files=list(rc.get("files") or []),
                details=list(rc.get("details") or []),
                status=str(rc.get("status") or "pending"),
                created_at=float(rc.get("created_at") or time.time()),
                snapshots=dict(rc.get("snapshots") or {}),
            )
            self.conflicts[cid] = card

    def resolve_conflict(
        self,
        conflict_id: str,
        decision: str,
        *,
        workspace: str | Path | None = None,
        session_id: str = "",
        turn_id: str = "",
    ) -> ConflictCard | None:
        card = self.conflicts.get(conflict_id)
        if not card:
            return None
        if decision not in ("resolved_ours", "resolved_theirs", "dismissed"):
            return card

        if decision in ("resolved_ours", "resolved_theirs"):
            pay = self._payloads.get(conflict_id)
            ws_raw = workspace or (pay.get("workspace") if pay else None)
            if ws_raw:
                ws = Path(ws_raw).resolve()
                sid = session_id or (pay.get("session_id") if pay else "") or ""
                tid = turn_id or (pay.get("turn_id") if pay else "") or card.agent_id
                staged: list[tuple[Path, str, bytes | None, bytes | None]] = []
                if pay:
                    for it in pay.get("items") or []:
                        rel = str(it["rel"])
                        dst = (ws / rel).resolve()
                        dst.relative_to(ws)
                        before_b = dst.read_bytes() if dst.is_file() else it.get("before")
                        if not it.get("conflict"):
                            staged.append((dst, rel, before_b, it.get("after")))
                        elif decision == "resolved_theirs":
                            staged.append((dst, rel, before_b, it.get("theirs")))
                elif card.snapshots:
                    for rel, sn in card.snapshots.items():
                        dst = (ws / rel).resolve()
                        dst.relative_to(ws)
                        b_b64 = sn.get("before_b64")
                        before_b = dst.read_bytes() if dst.is_file() else (base64.b64decode(b_b64) if b_b64 else None)
                        if not sn.get("conflict"):
                            a_b64 = sn.get("after_b64")
                            staged.append((dst, rel, before_b, base64.b64decode(a_b64) if a_b64 is not None else None))
                        elif decision == "resolved_theirs":
                            t_b64 = sn.get("theirs_b64")
                            staged.append((dst, rel, before_b, base64.b64decode(t_b64) if t_b64 is not None else None))
                if staged:
                    merged_rels = self._commit_staged(staged, agent_id=card.agent_id, session_id=sid, turn_id=tid)
                    self.history.append({
                        "ts": round(time.time(), 3),
                        "agent_id": card.agent_id,
                        "files": merged_rels,
                        "status": decision,
                        "conflict_id": conflict_id,
                    })
                elif decision == "resolved_ours":
                    self.history.append({
                        "ts": round(time.time(), 3),
                        "agent_id": card.agent_id,
                        "files": list(card.files),
                        "status": decision,
                        "conflict_id": conflict_id,
                    })

        self._payloads.pop(conflict_id, None)
        card.status = decision
        return card
