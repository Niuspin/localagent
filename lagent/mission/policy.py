"""Mission 无人值守策略评估器：复用 PathGuard / HARD_DENY / Taint 单调不降级安全边界。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import security as sec
from ..permissions import infer_delegated_kind, match_commands, match_glob
from ..tools.base import TOOLS

READONLY_TOOLS = {
    "read_file",
    "ingest_media",
    "list_dir",
    "find_files",
    "grep",
    "glob",
    "repo_map",
    "code_diagnostics",
    "read_tool_output",
    "memory_search",
    "memory_get",
    "todo_write",
    "verify",
    "web_search",
    "agent_status",
    "await_agents",
}

WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "patch_file",
    "delete_file",
    "delete_path",
    "mkdir",
    "make_dir",
    "move_file",
    "move_path",
    "copy_path",
    "write_document",
    "write_spreadsheet",
    "write_presentation",
    "edit_office",
}
NET_TOOLS = {"web_fetch", "fetch_url", "http_request"}


@dataclass
class PolicyVerdict:
    allowed: bool
    action: str          # allow | block | deny
    reason: str
    target: str = ""
    hard: bool = False


class MissionPolicy:
    """Mission 级权限与无人值守边界检查。

    安全单调性保证：
    1. PathGuard（工作区外 / protected / hidden）与 hard_deny_reason 永远不可被 allowlist 绕过；
    2. taint 状态下的敏感/执行操作永远要求人工审批（block），绝不在无人值守下自动放行；
    3. 敏感文件（.env / 密钥等）不可在无人值守下自动放行；
    4. 超出 allowlist 的操作绝不直接执行，而是标记 blocked 并排队等待用户审批。
    """

    def __init__(self, policy: dict[str, Any], guard: sec.PathGuard, *, workspace: Path):
        self.policy = dict(policy or {})
        self.mode = str(self.policy.get("mode") or "attended")
        self.allow_paths = [str(p) for p in (self.policy.get("allow_paths") or []) if str(p).strip()]
        self.allow_commands = [str(c) for c in (self.policy.get("allow_commands") or []) if str(c).strip()]
        self.allow_domains = [str(d).lower().strip() for d in (self.policy.get("allow_domains") or []) if str(d).strip()]
        self.guard = guard
        self.workspace = Path(workspace).resolve()

    def evaluate(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        tainted: bool = False,
        approved_actions: set[str] | None = None,
    ) -> PolicyVerdict:
        args = dict(args or {})
        tname = str(tool or "")
        tool_obj = TOOLS.get(tname)
        raw_kind = tool_obj.kind if tool_obj is not None else ""
        eff_raw = infer_delegated_kind(tname, args, raw_kind)
        if tname in ("shell", "run_command", "bash") or eff_raw == "exec":
            eff_kind = "exec"
        elif tname in WRITE_TOOLS or eff_raw in ("write", "delete"):
            eff_kind = "write"
        elif tname in NET_TOOLS or eff_raw == "net_fetch":
            eff_kind = "net_fetch"
        elif tname in READONLY_TOOLS or eff_raw in ("read", "meta", "memory_read", "net_search"):
            eff_kind = "read"
        else:
            eff_kind = "other"

        primary_target = args.get("command") or args.get("cmd") or args.get("dst") or args.get("path") or args.get("file_path") or args.get("src") or args.get("url") or ""
        sig = f"{tname}:{primary_target}"

        # 1. 硬拒绝与路径越界永远优先（不可覆盖）
        if eff_kind == "exec":
            cwd_raw = str(args.get("cwd") or "").strip()
            if cwd_raw:
                try:
                    cwd_resolved = self.guard.resolve(cwd_raw, write=False)
                except sec.SecurityError as e:
                    return PolicyVerdict(False, "deny", f"path_guard: {e}", target=cwd_raw, hard=True)
                cwd_rel = self._rel_posix(cwd_resolved)
                cwd_sens = sec.sensitive_reason(cwd_rel)
                if cwd_sens:
                    return PolicyVerdict(False, "block", f"sensitive_path:{cwd_sens}", target=cwd_rel)
            cmd = str(args.get("command") or args.get("cmd") or "").strip()
            hd = sec.hard_deny_reason(cmd)
            if hd:
                return PolicyVerdict(False, "deny", f"hard_deny: {hd}", target=cmd, hard=True)
            if tainted:
                return PolicyVerdict(False, "block", "tainted_command_requires_approval", target=cmd)
            if approved_actions and sig in approved_actions:
                return PolicyVerdict(True, "allow", "explicitly_approved", target=cmd)
            if self.mode != "unattended":
                if sec.is_readonly_command(cmd) and not self.allow_commands:
                    return PolicyVerdict(True, "allow", "readonly_command", target=cmd)
                return PolicyVerdict(False, "block", "attended_approval_required", target=cmd)
            # unattended 模式严格核对 command allowlist（拆分复合段防 && 绕过）
            if self.allow_commands and match_commands(cmd, self.allow_commands):
                return PolicyVerdict(True, "allow", "allowlisted_command", target=cmd)
            return PolicyVerdict(False, "block", "command_outside_allowlist", target=cmd)

        path_keys: list[str] = list(tool_obj.path_args) if (tool_obj and tool_obj.path_args) else []
        for fallback_k in ("path", "file_path", "src", "dst"):
            if fallback_k not in path_keys:
                path_keys.append(fallback_k)
        path_pairs: list[tuple[str, str]] = []
        for k in path_keys:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                path_pairs.append((k, v.strip()))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        path_pairs.append((k, item.strip()))

        if eff_kind == "write" or path_pairs:
            rels: list[str] = []
            write_rels: list[str] = []
            for k, raw_p in path_pairs:
                # copy_path 的 src 与 bundle_artifacts 的 files 仅读，其余写工具路径（含 move_path 源/目标）按写边界校验
                need_write = eff_kind == "write" and not ((tname == "copy_path" and k == "src") or (tname == "bundle_artifacts" and k == "files"))
                try:
                    resolved = self.guard.resolve(raw_p, write=need_write)
                except sec.SecurityError as e:
                    return PolicyVerdict(False, "deny", f"path_guard: {e}", target=raw_p, hard=True)
                rel_p = self._rel_posix(resolved)
                rels.append(rel_p)
                if need_write:
                    write_rels.append(rel_p)

            target_rel = (write_rels[-1] if write_rels else rels[-1]) if rels else tname
            target_sigs = {sig, f"{tname}:{target_rel}"}
            for rel in rels:
                sens = sec.sensitive_reason(rel)
                if sens:
                    rel_sig = f"{tname}:{rel}"
                    if not tainted and approved_actions and (rel_sig in approved_actions or (len(rels) == 1 and sig in approved_actions)):
                        continue
                    return PolicyVerdict(False, "block", f"sensitive_path:{sens}", target=rel)

            if eff_kind == "write":
                if tainted:
                    return PolicyVerdict(False, "block", "tainted_write_requires_approval", target=target_rel)
                if approved_actions and (target_sigs & approved_actions):
                    return PolicyVerdict(True, "allow", "explicitly_approved", target=target_rel)
                if not write_rels:
                    if tname in ("delegate_task", "spawn_agent", "swarm_plan"):
                        if self.mode == "unattended" and self.allow_paths:
                            return PolicyVerdict(True, "allow", "delegated_with_policy", target=target_rel)
                        if self.mode != "unattended":
                            return PolicyVerdict(False, "block", "attended_write_requires_approval", target=target_rel)
                    return PolicyVerdict(False, "block", "path_outside_allowlist", target=target_rel)
                # Mission 自身状态目录允许在内部更新，或全部写路径匹配 allow_paths
                if all(r.startswith(".localagent/missions/") for r in write_rels):
                    return PolicyVerdict(True, "allow", "mission_metadata", target=target_rel)
                if self.mode != "unattended":
                    return PolicyVerdict(False, "block", "attended_write_requires_approval", target=target_rel)
                if self.allow_paths and all(any(match_glob(r, pat) for pat in self.allow_paths) for r in write_rels):
                    return PolicyVerdict(True, "allow", "allowlisted_path", target=target_rel)
                return PolicyVerdict(False, "block", "path_outside_allowlist", target=target_rel)
            return PolicyVerdict(True, "allow", "safe_read", target=rels[-1] if rels else "")

        if eff_kind == "net_fetch" or "url" in args:
            url = str(args.get("url") or "").strip()
            host = (urlparse(url).hostname or "").lower()
            if tainted:
                return PolicyVerdict(False, "block", "tainted_network_requires_approval", target=url)
            if approved_actions and sig in approved_actions:
                return PolicyVerdict(True, "allow", "explicitly_approved", target=url)
            if self.mode == "unattended" and self.allow_domains:
                if any(host == d or host.endswith("." + d) for d in self.allow_domains):
                    return PolicyVerdict(True, "allow", "allowlisted_domain", target=url)
            return PolicyVerdict(False, "block", "domain_outside_allowlist", target=url)

        if eff_kind == "read":
            return PolicyVerdict(True, "allow", "readonly_tool")

        # 未知/高风险工具默认拦截
        if tainted:
            return PolicyVerdict(False, "block", "tainted_tool_requires_approval", target=tname)
        if approved_actions and sig in approved_actions:
            return PolicyVerdict(True, "allow", "explicitly_approved", target=tname)
        return PolicyVerdict(False, "block", "tool_requires_approval", target=tname)

    def _rel_posix(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return p.as_posix()


def queue_approval(mission: Any, *, milestone_id: str | None, tool: str, target: str, reason: str, sig: str = "") -> dict[str, Any]:
    """为越界操作排队生成一条审批项（去重同目标 pending 项）。"""
    for item in mission.approvals:
        if item.get("status") == "pending" and item.get("tool") == tool and item.get("target") == target:
            return item
    import time
    aid = f"apr_{uuid.uuid4().hex[:8]}"
    rec = {
        "id": aid,
        "milestone_id": milestone_id,
        "tool": tool,
        "target": target,
        "reason": reason,
        "signature": sig or f"{tool}:{target}",
        "status": "pending",
        "created_at": time.time(),
        "resolved_at": None,
    }
    mission.approvals.append(rec)
    return rec
