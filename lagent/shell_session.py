"""持久交互式 Shell 会话管理：维护跨命令的工作目录/环境变量/函数与可选 stdin/PTY 支持。"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any

from . import security as sec

_SESSIONS: dict[str, "PersistentShellSession"] = {}
_LOCK = threading.Lock()


class PersistentShellSession:
    """单会话级持久 Bash/PowerShell 进程包装，支持定界命令捕获、工作区边界检验与标准输入传递。"""

    def __init__(self, sid: str, workspace: Path, *, allow_net: bool = False, home: Path | None = None, sandbox_mode: str = "auto"):
        self.sid = sid
        self.workspace = Path(workspace).resolve()
        self.allow_net = allow_net
        self.home = home or (self.workspace / ".localagent" / "cmdhome")
        self.sandbox_mode = sandbox_mode
        self.proc: asyncio.subprocess.Process | None = None
        self.kind = "none"
        self._shell_kind = "sh"
        self._loop: asyncio.AbstractEventLoop | None = None
        self.cwd_rel = ""
        self._io_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        try:
            cur_loop = asyncio.get_running_loop()
            if self._loop is not None and self._loop is not cur_loop:
                return False
        except RuntimeError:
            pass
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.alive:
            return
        if self.proc is not None:
            await self.close()
        self._loop = asyncio.get_running_loop()
        self.home.mkdir(parents=True, exist_ok=True)
        if sec.IS_WIN:
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if ps:
                self._shell_kind = "powershell"
                argv = [ps, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command", "-"]
            else:
                self._shell_kind = "cmd"
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/Q"]
        else:
            self._shell_kind = "sh"
            argv = [shutil.which("bash") or "/bin/sh", "--noprofile", "--norc", "-i"] if shutil.which("bash") else ["/bin/sh"]
        if self.sandbox_mode == "auto":
            argv, self.kind = sec.wrap_in_sandbox(argv, self.workspace, self.allow_net, self.home)
        env = sec.clean_env(str(self.home))
        env["PS1"] = ""
        kw: dict[str, Any] = {}
        if sec.IS_WIN:
            kw["creationflags"] = 0x08000000 | 0x00000200
        else:
            kw["start_new_session"] = True
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.workspace),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kw,
        )
        if self.proc.pid:
            sec.harden_windows_process(self.proc.pid)
        if self._shell_kind == "powershell" and self.proc.stdin:
            self.proc.stdin.write(b"[Console]::InputEncoding=[Text.Encoding]::UTF8; [Console]::OutputEncoding=[Text.Encoding]::UTF8\n")
            await self.proc.stdin.drain()

    async def run(self, command: str, *, stdin_data: str = "", timeout: float = 120.0) -> tuple[int, str, str]:
        """在持久会话内执行命令并等待定界符返回 `(exit_code, output, cur_cwd_rel)`。"""
        async with self._io_lock:
            if not self.alive:
                await self.start()
            assert self.proc and self.proc.stdin and self.proc.stdout
            marker = f"__LAGENT_END_{secrets.token_hex(6)}__"
            if self._shell_kind == "powershell":
                payload = (
                    f"{command}\n"
                    "$__rc = if ($?) { 0 } elseif ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 1 }\n"
                    f'Write-Output ("{marker}:" + $__rc + ":" + (Get-Location).Path)\n'
                )
            elif self._shell_kind == "cmd":
                payload = f"{command}\r\necho {marker}:%ERRORLEVEL%:%CD%\r\n"
            else:
                payload = f"{command}\n__rc=$?\nprintf '%s:%s:%s\\n' '{marker}' \"$__rc\" \"$PWD\"\n"
            if stdin_data:
                payload = stdin_data + ("" if stdin_data.endswith("\n") else "\n") + payload
            self.proc.stdin.write(payload.encode("utf-8"))
            await self.proc.stdin.drain()

            lines: list[str] = []
            code = 0
            cur_cwd = self.cwd_rel
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                rem = deadline - asyncio.get_running_loop().time()
                if rem <= 0:
                    await self.close()
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=rem)
                if not raw:
                    code = self.proc.returncode or 1
                    break
                txt = raw.decode("utf-8", "replace").rstrip("\r\n")
                mpos = txt.find(marker + ":")
                if mpos >= 0:
                    head_txt = txt[:mpos].strip()
                    if head_txt:
                        lines.append(head_txt)
                    parts = txt[mpos:].split(":", 2)
                    try:
                        code = int(parts[1])
                    except ValueError:
                        code = 0
                    if len(parts) >= 3 and parts[2].strip():
                        try:
                            p_resolved = Path(parts[2].strip()).resolve()
                            rel = p_resolved.relative_to(self.workspace).as_posix()
                            cur_cwd = "" if rel == "." else rel
                            self.cwd_rel = cur_cwd
                        except Exception:  # noqa: BLE001
                            pass
                    break
                lines.append(txt)
            return code, "\n".join(lines).strip(), cur_cwd

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            pid = proc.pid or 0
            try:
                if proc.returncode is None:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            if pid:
                sec.release_windows_job(pid)


def get_or_create_shell(sid: str, workspace: Path, *, allow_net: bool, home: Path, sandbox_mode: str) -> PersistentShellSession:
    with _LOCK:
        sh = _SESSIONS.get(sid)
        if sh is None or sh.workspace != Path(workspace).resolve() or sh.allow_net != allow_net:
            sh = PersistentShellSession(sid, workspace, allow_net=allow_net, home=home, sandbox_mode=sandbox_mode)
            _SESSIONS[sid] = sh
        return sh


async def close_session_shell(sid: str) -> None:
    with _LOCK:
        sh = _SESSIONS.pop(sid, None)
    if sh is not None:
        await sh.close()


async def close_all_shells() -> None:
    with _LOCK:
        items = list(_SESSIONS.values())
        _SESSIONS.clear()
    for sh in items:
        await sh.close()
