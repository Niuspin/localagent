"""Mission 无人值守策略评估器：复用 PathGuard / HARD_DENY / Taint 单调不降级安全边界。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import security as sec
from ..permissions import match_command, match_glob

READONLY_TOOLS = {
    "read_file",
    "list_dir",
    "find_files",
    "grep",
    "glob",
    "repo_map",
    "memory_search",
    "memory_get",
    "todo_write",
    "verify",
    "web_search",
}

WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "delete_file", "mkdir", "move_file"}
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
        sig = f"{tname}:{args.get('command') or args.get('path') or args.get('url') or ''}"

        # 1. 硬拒绝与路径越界永远优先（不可覆盖）
        if tname in ("shell", "run_command", "bash"):
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
            # unattended 模式严格核对 command allowlist
            if self.allow_commands and any(match_command(cmd, pat) for pat in self.allow_commands):
                return PolicyVerdict(True, "allow", "allowlisted_command", target=cmd)
            return PolicyVerdict(False, "block", "command_outside_allowlist", target=cmd)

        if tname in WRITE_TOOLS or "path" in args or "file_path" in args:
            raw_p = str(args.get("path") or args.get("file_path") or "").strip()
            if raw_p:
                try:
                    resolved = self.guard.resolve(raw_p, write=(tname in WRITE_TOOLS))
                except sec.SecurityError as e:
                    return PolicyVerdict(False, "deny", f"path_guard: {e}", target=raw_p, hard=True)
                rel = self._rel_posix(resolved)
                norm_sig = f"{tname}:{rel}"
                if tname in WRITE_TOOLS:
                    sens = sec.sensitive_reason(rel)
                    if sens:
                        return PolicyVerdict(False, "block", f"sensitive_path:{sens}", target=rel)
                    if tainted:
                        return PolicyVerdict(False, "block", "tainted_write_requires_approval", target=rel)
                    if approved_actions and (sig in approved_actions or norm_sig in approved_actions):
                        return PolicyVerdict(True, "allow", "explicitly_approved", target=rel)
                    # Mission 自身状态目录允许在内部更新，或匹配 allow_paths
                    if rel.startswith(".localagent/missions/"):
                        return PolicyVerdict(True, "allow", "mission_metadata", target=rel)
                    if self.mode != "unattended":
                        return PolicyVerdict(False, "block", "attended_write_requires_approval", target=rel)
                    if self.allow_paths and any(match_glob(rel, pat) for pat in self.allow_paths):
                        return PolicyVerdict(True, "allow", "allowlisted_path", target=rel)
                    return PolicyVerdict(False, "block", "path_outside_allowlist", target=rel)
                return PolicyVerdict(True, "allow", "safe_read", target=rel)

        if tname in NET_TOOLS or "url" in args:
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

        if tname in READONLY_TOOLS:
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
