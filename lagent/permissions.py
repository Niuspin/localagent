"""细粒度权限规则管理（兼容 Claude Code 风格的 settings.json / settings.local.json 规则）。

支持规则格式：
- Bash(command_pattern)  例如 Bash(git status*)、Bash(pytest*)
- Read(path_pattern)     例如 Read(src/**)、Read(*.py)
- Edit(path_pattern)     例如 Edit(*.md)、Edit(docs/**)
- Write(path_pattern)    例如 Write(tests/**)、Write(*)
- Delete(path_pattern)   例如 Delete(tmp/**)
- Tool(tool_name)        例如 Tool(web_search)

安全保证（不可逾越的底线）：
1. hard_deny（格式化磁盘、kill 0、mkfs 等系统高危命令）绝对拒绝，任何 allow 规则都无法覆盖。
2. 敏感文件（.env, .git/, id_rsa 等）匹配到 allow 时强制降级为 ask。
3. 污染上下文（tainted=True）下的写入/删除/执行/MCP 匹配到 allow 时强制降级为 ask。
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import security as sec


_RULE_RE = re.compile(r"^([A-Za-z0-9_-]+)\((.*)\)$", re.S)


def match_glob(target: str, pattern: str) -> bool:
    """支持 ** 与 * 的跨平台路径与通配符匹配。"""
    t = target.replace("\\", "/").strip("/")
    p = pattern.replace("\\", "/").strip("/")
    if p in ("*", "**", ""):
        return True
    if fnmatch.fnmatch(t, p):
        return True
    if p.endswith("/**"):
        prefix = p[:-3]
        if t == prefix or t.startswith(prefix + "/"):
            return True
    if p.startswith("**/"):
        suffix = p[3:]
        if t == suffix or t.endswith("/" + suffix) or fnmatch.fnmatch(Path(t).name, suffix):
            return True
    if "/" not in p and fnmatch.fnmatch(Path(t).name, p):
        return True
    return False


def match_command(cmd: str, pattern: str) -> bool:
    """命令通配符匹配，如 'git status*' 匹配 'git status' 和 'git status -s'。"""
    cmd = (cmd or "").strip()
    pattern = pattern.strip()
    if pattern in ("*", "**"):
        return True
    if fnmatch.fnmatch(cmd, pattern):
        return True
    # 去除末尾通配符后前缀匹配
    if pattern.endswith("*"):
        base = pattern[:-1].strip()
        if cmd.startswith(base):
            return True
    return False


@dataclass
class PermissionRule:
    action: str      # "allow" | "ask" | "deny"
    rule_type: str   # "bash" | "read" | "edit" | "write" | "delete" | "tool" | "all"
    pattern: str     # 括号内的模式
    raw: str         # 原始规则字符串


def parse_rule(raw: str, action: str) -> PermissionRule:
    raw_str = raw.strip()
    m = _RULE_RE.match(raw_str)
    if not m:
        # 没有括号，如 "read_file" 或 "*"
        return PermissionRule(action=action, rule_type="tool", pattern=raw_str, raw=raw_str)
    typ, pat = m.group(1).lower(), m.group(2).strip()
    norm_type = {
        "bash": "bash", "command": "bash", "sh": "bash", "exec": "bash",
        "read": "read",
        "edit": "edit",
        "write": "write",
        "delete": "delete",
        "tool": "tool",
    }.get(typ, typ)
    return PermissionRule(action=action, rule_type=norm_type, pattern=pat, raw=raw_str)


class PermissionManager:
    """管理工作区细粒度权限规则。"""

    def __init__(self, workspace: str | Path | None = None, rules: dict[str, list[str]] | None = None):
        self.workspace = Path(workspace) if workspace else None
        self.rules: dict[str, list[PermissionRule]] = {"deny": [], "ask": [], "allow": []}
        if rules:
            self._load_from_dict(rules)
        elif self.workspace:
            self.reload()

    def _load_from_dict(self, d: dict) -> None:
        perms = d.get("permissions") if isinstance(d.get("permissions"), dict) else d
        for act in ("deny", "ask", "allow"):
            items = perms.get(act) or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, str) and it.strip():
                        self.rules[act].append(parse_rule(it, act))

    def reload(self) -> None:
        self.rules = {"deny": [], "ask": [], "allow": []}
        if not self.workspace or not self.workspace.is_dir():
            return
        # 寻找配置文件优先级：.localagent/settings.json -> .claude/settings.local.json -> .claude/settings.json
        cand_files = [
            self.workspace / ".localagent" / "settings.json",
            self.workspace / ".claude" / "settings.local.json",
            self.workspace / ".claude" / "settings.json",
        ]
        for f in cand_files:
            if f.is_file():
                try:
                    data = json.loads(f.read_text("utf-8"))
                    if isinstance(data, dict):
                        self._load_from_dict(data)
                except (OSError, ValueError):
                    pass

    def evaluate(
        self,
        *,
        kind: str,
        name: str,
        args: dict,
        tainted: bool,
        sensitive: str | None = None,
        command: str | None = None,
        sandboxed: bool = True,
    ) -> sec.Decision | None:
        """评估自定义规则。返回 sec.Decision 或 None（无匹配时回退到默认 Policy）。"""
        # 1. 绝对硬拒绝：不可被任何规则绕过
        if command:
            hard = sec.hard_deny_reason(command)
            if hard:
                return sec.Decision("deny", f"命令被系统安全规则硬拒绝（{hard}）", "high", "hard_deny")

        path = ""
        for k in ("path", "src", "dst"):
            if args.get(k):
                path = str(args[k])
                break

        # 检查顺序：deny -> ask -> allow
        for act in ("deny", "ask", "allow"):
            for r in self.rules[act]:
                if self._match_rule(r, kind=kind, name=name, path=path, command=command):
                    key = f"rule:{r.raw}"
                    if act == "deny":
                        return sec.Decision("deny", f"被自定义权限规则 (deny) 拒绝: {r.raw}", "high", key)
                    if act == "ask":
                        return sec.Decision("ask", f"触发自定义权限规则 (ask): {r.raw}", "medium", key)
                    if act == "allow":
                        # 底线防护：敏感文件绝不自动放行
                        if sensitive:
                            return sec.Decision("ask", f"敏感文件即使匹配允许规则也需用户确认: {sensitive}", "high", key)
                        # 底线防护：受污染环境下的危险操作绝不自动放行
                        dangerous = kind in ("write", "delete", "exec", "mcp")
                        if tainted and dangerous:
                            return sec.Decision("ask", "上下文含不可信数据，危险操作需用户确认", "high", key)
                        return sec.Decision("allow", f"匹配自定义权限规则 (allow): {r.raw}", "low", key)

        return None

    def _match_rule(self, r: PermissionRule, *, kind: str, name: str, path: str, command: str | None) -> bool:
        # Bash 规则
        if r.rule_type == "bash":
            if kind == "exec" and command:
                return match_command(command, r.pattern)
            return False

        # Read 规则
        if r.rule_type == "read":
            if kind == "read" or name in ("read_file", "read_tool_output", "list_dir", "glob", "grep"):
                return not r.pattern or match_glob(path, r.pattern)
            return False

        # Edit 规则
        if r.rule_type == "edit":
            if name in ("edit_file", "edit_office") or (kind == "write" and name.startswith("edit")):
                return not r.pattern or match_glob(path, r.pattern)
            return False

        # Write 规则
        if r.rule_type == "write":
            if kind == "write":
                return not r.pattern or match_glob(path, r.pattern)
            return False

        # Delete 规则
        if r.rule_type == "delete":
            if kind == "delete" or name == "delete_path":
                return not r.pattern or match_glob(path, r.pattern)
            return False

        # Tool 规则 (直接匹配工具名或通配符)
        if r.rule_type == "tool" or r.rule_type == name.lower():
            if fnmatch.fnmatch(name.lower(), r.pattern.lower()):
                return True
            if r.pattern in ("*", "**"):
                return True
            # 支持 Tool(read_file) 或 read_file(*)
            if fnmatch.fnmatch(name, r.pattern):
                return True

        return False

    def list_rules(self) -> dict[str, list[str]]:
        return {act: [r.raw for r in rules] for act, rules in self.rules.items()}
