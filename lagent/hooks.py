"""生命周期钩子管理（兼容 Claude Code 风格的 hooks.json / settings.json 钩子）。

支持钩子事件：
- PreToolUse: 工具执行前运行。可根据退出码或输出决定放行或拒绝（exit_code != 0 或 JSON {"decision":"deny"} 表示拒绝）。
- PostToolUse: 工具执行完毕后运行。可做后续审计或产出物检查。
- Stop: 会话回合结束（run_end）时触发。
- UserPromptSubmit: 用户提交 Prompt 时触发。

安全规范：
- 超时保护（默认 10 秒，最大不超过 30 秒）。
- 清洗环境变量（sec.clean_env），防止泄露敏感环境信息。
- 拒绝 hard_deny 危险命令。
- 若会话已受污染（tainted）或处于不可信状态，跳过钩子并发出安全警告。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import security as sec

logger = logging.getLogger(__name__)


@dataclass
class HookDef:
    event: str          # "PreToolUse" | "PostToolUse" | "Stop" | "UserPromptSubmit"
    command: str        # 待执行命令
    matcher: str = "*"  # 工具匹配器（如 "run_command" 或 "write_file|edit_file" 或 "*"）
    timeout: int = 10   # 超时秒数


@dataclass
class HookResult:
    action: str = "allow"      # "allow" | "deny"
    reason: str = ""
    output: str = ""
    exit_code: int = 0
    skipped: bool = False


def _shell_argv(cmd: str) -> list[str]:
    if sec.IS_WIN:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", cmd]
    return [shutil.which("bash") or "/bin/sh", "-c", cmd]


class HookManager:
    """管理并安全执行项目生命周期钩子。"""

    def __init__(self, workspace: str | Path | None = None, hooks: dict[str, list[dict]] | None = None):
        self.workspace = Path(workspace) if workspace else None
        self._explicit = hooks is not None
        self._sig_val: tuple | None = None
        self.hooks: dict[str, list[HookDef]] = {
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
            "UserPromptSubmit": [],
        }
        if hooks:
            self._load_from_dict(hooks)
        elif self.workspace:
            self.reload()

    def _cand_files(self) -> list[Path]:
        if not self.workspace:
            return []
        return [
            self.workspace / ".localagent" / "hooks.json",
            self.workspace / ".claude" / "hooks.json",
            self.workspace / ".claude" / "settings.json",
        ]

    def _sig(self) -> tuple:
        out = []
        for f in self._cand_files():
            try:
                st = f.stat()
                out.append((str(f), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(f), None, None))
        return tuple(out)

    def _load_from_dict(self, d: dict) -> None:
        raw_hooks = d.get("hooks") if isinstance(d.get("hooks"), dict) else d
        for ev in ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"):
            items = raw_hooks.get(ev) or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("command"):
                        cmd = str(it["command"]).strip()
                        matcher = str(it.get("matcher") or "*").strip()
                        timeout = max(1, min(int(it.get("timeout") or 10), 30))
                        self.hooks[ev].append(HookDef(event=ev, command=cmd, matcher=matcher, timeout=timeout))

    def reload(self) -> None:
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "Stop": [], "UserPromptSubmit": []}
        self._sig_val = self._sig()
        if not self.workspace or not self.workspace.is_dir():
            return
        for f in self._cand_files():
            if f.is_file():
                try:
                    data = json.loads(f.read_text("utf-8"))
                    if isinstance(data, dict):
                        self._load_from_dict(data)
                except (OSError, ValueError):
                    pass

    def reload_if_changed(self) -> bool:
        if self._explicit or not self.workspace:
            return False
        cur = self._sig()
        if cur != self._sig_val:
            self.reload()
            return True
        return False

    def list_hooks(self) -> dict[str, list[dict]]:
        return {
            ev: [{"matcher": h.matcher, "command": h.command, "timeout": h.timeout} for h in defs]
            for ev, defs in self.hooks.items()
        }

    def _matches(self, pattern: str, tool_name: str) -> bool:
        if pattern in ("*", "", "all"):
            return True
        if "|" in pattern:
            return any(self._matches(p.strip(), tool_name) for p in pattern.split("|"))
        return pattern.lower() == tool_name.lower() or re.search(f"^{pattern}$", tool_name, re.I) is not None

    async def _execute_hook(self, hook: HookDef, env_extra: dict[str, str], ws: Path) -> HookResult:
        # 安全检查 1：硬拒绝命令
        why = sec.hard_deny_reason(hook.command)
        if why:
            logger.warning("钩子命令因硬拒绝规则被跳过: %s (%s)", hook.command, why)
            return HookResult(action="deny", reason=f"钩子命令包含危险行为被拦截: {why}", exit_code=126)

        # 环境变量清洗与字符串强制转换
        env = sec.clean_env(str(ws))
        for k, v in env_extra.items():
            env[str(k)] = str(v)
        argv = _shell_argv(hook.command)

        creationflags = 0
        if sec.IS_WIN:
            creationflags = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(ws),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=hook.timeout)
            stdout = stdout_b.decode("utf-8", "replace").strip()
            stderr = stderr_b.decode("utf-8", "replace").strip()
            code = proc.returncode or 0

            # 检查输出中是否有显式的 JSON 决策
            if stdout.startswith("{") and stdout.endswith("}"):
                try:
                    j = json.loads(stdout)
                    if isinstance(j, dict) and j.get("decision") == "deny":
                        return HookResult(action="deny", reason=j.get("reason") or "被钩子显式拒绝", output=stdout, exit_code=code)
                except ValueError:
                    pass

            if code != 0:
                reason = stderr or stdout or f"钩子退出码异常 ({code})"
                return HookResult(action="deny", reason=reason, output=stdout, exit_code=code)

            return HookResult(action="allow", output=stdout, exit_code=0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return HookResult(action="deny", reason=f"钩子执行超时（>{hook.timeout}s）", exit_code=124)
        except Exception as e:  # noqa: BLE001
            return HookResult(action="deny", reason=f"钩子执行出错: {e}", exit_code=1)

    async def run_pre_tool_use(self, tool_name: str, args: dict, ctx: Any) -> HookResult:
        if not self.workspace or not self.hooks["PreToolUse"]:
            return HookResult(action="allow")
        # 污染上下文安全跳过：防止不可信数据通过钩子触发未审查行为
        tainted = getattr(ctx.session, "tainted", False) if hasattr(ctx, "session") else False
        if tainted:
            if hasattr(ctx, "emit") and ctx.emit:
                ctx.emit("notice", {"level": "warn", "text": "当前上下文受污染，已跳过 PreToolUse 钩子。"})
            return HookResult(action="allow", skipped=True)

        for h in self.hooks["PreToolUse"]:
            if self._matches(h.matcher, tool_name):
                env = {
                    "HOOK_EVENT": "PreToolUse",
                    "TOOL_NAME": tool_name,
                    "TOOL_ARGS": json.dumps(args, ensure_ascii=False)[:4000],
                    "SESSION_ID": getattr(ctx, "sid", ""),
                    "WORKSPACE": str(self.workspace),
                }
                res = await self._execute_hook(h, env, self.workspace)
                if res.action == "deny":
                    return res
        return HookResult(action="allow")

    async def run_post_tool_use(self, tool_name: str, args: dict, result_content: str, ctx: Any) -> HookResult:
        if not self.workspace or not self.hooks["PostToolUse"]:
            return HookResult(action="allow")
        tainted = getattr(ctx.session, "tainted", False) if hasattr(ctx, "session") else False
        if tainted:
            return HookResult(action="allow", skipped=True)

        for h in self.hooks["PostToolUse"]:
            if self._matches(h.matcher, tool_name):
                env = {
                    "HOOK_EVENT": "PostToolUse",
                    "TOOL_NAME": tool_name,
                    "TOOL_ARGS": json.dumps(args, ensure_ascii=False)[:2000],
                    "TOOL_RESULT_PREVIEW": result_content[:2000],
                    "SESSION_ID": getattr(ctx, "sid", ""),
                    "WORKSPACE": str(self.workspace),
                }
                await self._execute_hook(h, env, self.workspace)
        return HookResult(action="allow")

    async def run_stop(self, ctx: Any) -> HookResult:
        if not self.workspace or not self.hooks["Stop"]:
            return HookResult(action="allow")
        tainted = getattr(ctx.session, "tainted", False) if hasattr(ctx, "session") else False
        if tainted:
            return HookResult(action="allow", skipped=True)

        for h in self.hooks["Stop"]:
            env = {
                "HOOK_EVENT": "Stop",
                "SESSION_ID": getattr(ctx, "sid", "") if hasattr(ctx, "sid") else "",
                "WORKSPACE": str(self.workspace),
            }
            await self._execute_hook(h, env, self.workspace)
        return HookResult(action="allow")

    async def run_prompt_submit(self, text: str, ctx: Any) -> HookResult:
        if not self.workspace or not self.hooks["UserPromptSubmit"]:
            return HookResult(action="allow")
        for h in self.hooks["UserPromptSubmit"]:
            env = {
                "HOOK_EVENT": "UserPromptSubmit",
                "USER_PROMPT": text[:4000],
                "SESSION_ID": getattr(ctx, "sid", "") if hasattr(ctx, "sid") else "",
                "WORKSPACE": str(self.workspace),
            }
            res = await self._execute_hook(h, env, self.workspace)
            if res.action == "deny":
                return res
        return HookResult(action="allow")
