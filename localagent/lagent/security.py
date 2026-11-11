"""安全层：路径围栏、权限策略、污染追踪、命令规则与沙箱、防篡改审计、本地服务鉴权。

设计原则（纵深防御，任何一层失效都不应导致越权）：
1. 路径围栏：realpath 解析后必须落在工作区内；agent 自身目录（配置/数据/技能/模型/引擎）永不可写。
2. 权限模式 + 逐次审批；删除一律进回收站并写撤销日志。
3. 污染追踪：读入网页/文档等不可信内容后，高危操作（命令/删除/未知域名访问）强制升级为人工确认。
4. 命令执行：硬拒绝规则 + 环境变量清洗 + OS 级沙箱（bwrap / sandbox-exec，可用时）+ 超时。
5. 审计日志：SHA-256 哈希链，可校验是否被篡改。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

IS_WIN = os.name == "nt"
_CASE_FOLD = sys.platform in ("win32", "darwin")


class SecurityError(Exception):
    pass


# =============================================================== 路径围栏
def _cf(p: str) -> str:
    p = os.path.normpath(p)
    return p.lower() if _CASE_FOLD else p


def is_within(child: str | Path, parent: str | Path) -> bool:
    c, p = _cf(str(child)), _cf(str(parent))
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

_SENS_FILE = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|"
    r"[^/]*\.(pem|key|p12|pfx|kdbx|keystore|jks)|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$", re.I)
_SENS_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure")


def sensitive_reason(rel: str) -> str | None:
    """工作区内的敏感文件（密钥、凭据）——读写都需要人工确认。"""
    r = rel.replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    r = r.lstrip("/")
    if _SENS_FILE.search("/" + r) or _SENS_FILE.search(r):
        return f"疑似凭据/密钥文件: {rel}"
    for part in r.split("/")[:-1]:
        if part.lower() in _SENS_DIRS:
            return f"位于敏感目录 {part}/ : {rel}"
    return None


class PathGuard:
    def __init__(self, workspace: str | Path, ro_roots: Iterable[str | Path] = (), protected: Iterable[str | Path] = (),
                 hidden: Iterable[str | Path] = ()):
        self.ws = Path(os.path.realpath(str(workspace)))
        self.ro = [Path(os.path.realpath(str(r))) for r in ro_roots]
        self.protected = [Path(os.path.realpath(str(p))) for p in protected]   # 可读不可写
        self.hidden = [Path(os.path.realpath(str(p))) for p in hidden]         # 读写都拒绝（密钥、会话库等）

    @staticmethod
    def _win_checks(raw: str) -> None:
        if raw.startswith("\\\\") or raw.startswith("//"):
            raise SecurityError("拒绝 UNC / 设备路径")
        if ":" in raw[2:] or (len(raw) > 1 and raw[1] == ":" and not raw[0].isalpha()):
            raise SecurityError("拒绝含 ':' 的路径（NTFS 数据流）")
        for comp in re.split(r"[\\/]+", raw):
            if not comp:
                continue
            if comp != comp.rstrip(" ."):
                if comp not in (".", ".."):
                    raise SecurityError(f"路径组件以点/空格结尾: {comp!r}")
            if comp.split(".")[0].lower() in _WIN_RESERVED:
                raise SecurityError(f"保留设备名: {comp}")

    def resolve(self, p: Any, *, write: bool = False) -> Path:
        if not isinstance(p, str) or not p.strip():
            raise SecurityError("路径为空")
        raw = p.strip()
        if "\x00" in raw:
            raise SecurityError("路径含 NUL")
        if IS_WIN:
            self._win_checks(raw)
        cand = raw if os.path.isabs(raw) else os.path.join(str(self.ws), raw)
        real = Path(os.path.realpath(cand))  # 解析符号链接；对尚不存在的尾部同样有效
        for hd in self.hidden:
            if is_within(real, hd):
                raise SecurityError(f"受保护路径（agent 的数据/密钥目录），禁止访问: {raw}")
        if not is_within(real, self.ws):
            if not write and any(is_within(real, r) for r in self.ro):
                return real
            raise SecurityError(f"路径在工作区之外，已拒绝: {raw}")
        if write:
            for pr in self.protected:
                if is_within(real, pr):
                    raise SecurityError(f"受保护路径（agent 自身文件），禁止修改: {raw}")
            rel_parts = [x.lower() if _CASE_FOLD else x for x in real.relative_to(self.ws).parts] if _same_case_rel(real, self.ws) else []
            if ".git" in rel_parts:
                raise SecurityError("禁止直接修改 .git 内部文件（请用 git 命令）")
        return real

    def rel(self, p: Path | str) -> str:
        try:
            return Path(p).relative_to(self.ws).as_posix() or "."
        except ValueError:
            return str(p)

    def is_ws_root(self, p: Path) -> bool:
        return _cf(str(p)) == _cf(str(self.ws))


def _same_case_rel(real: Path, ws: Path) -> bool:
    try:
        real.relative_to(ws)
        return True
    except ValueError:
        # 大小写不同但被视为同一目录的情况（macOS/Windows）
        return is_within(real, ws) and len(str(real)) >= len(str(ws))


def validate_workspace(path: str) -> Path:
    """用户选择的工作区必须是明确的项目目录：存在、是目录、不是根/主目录/系统目录。"""
    if not path or not path.strip():
        raise SecurityError("路径为空")
    p = Path(os.path.realpath(os.path.expanduser(path.strip())))
    if not p.exists():
        raise SecurityError(f"目录不存在: {p}")
    if not p.is_dir():
        raise SecurityError(f"不是目录: {p}")
    if p.parent == p:
        raise SecurityError("不能把磁盘根目录设为工作区")
    home = Path(os.path.realpath(os.path.expanduser("~")))
    if _cf(str(p)) == _cf(str(home)):
        raise SecurityError("不能直接把用户主目录设为工作区，请选择其中的项目文件夹")
    bad = ["/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/sys", "/proc", "/dev", "/var", "/System", "/Library", "/private/etc"]
    if IS_WIN:
        for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            v = os.environ.get(env)
            if v:
                bad.append(v)
    for b in bad:
        if is_within(p, b):
            raise SecurityError(f"系统目录不能作为工作区: {b}")
    return p


# =============================================================== 不可信内容
_INVIS = re.compile("[​‌⁠﻿‪-‮⁦-⁩\U000e0000-\U000e007f]")


def sanitize_untrusted(text: str) -> str:
    """移除不可见/双向控制字符与 Unicode Tag 字符（“ASCII 走私”提示注入载体）。"""
    return _INVIS.sub("", text)


def wrap_untrusted(text: str, source: str, nonce: str) -> str:
    body = sanitize_untrusted(text).replace("</untrusted", "<\\/untrusted")
    src = source.replace('"', "'")[:300]
    return f'<untrusted source="{src}" id="{nonce}">\n{body}\n</untrusted id="{nonce}">'


# =============================================================== 命令规则
HARD_DENY: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), why) for p, why in [
    (r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(-[a-z]+\s+)*(/|~|\$home|\*|/\*|\.\.?)(\s|$)", "危险的递归删除"),
    (r"\bmkfs(\.\w+)?\b|\bdd\s+[^|;\n]*\bof=/dev/", "格式化/写裸设备"),
    (r":\(\)\s*\{.*\};\s*:", "fork 炸弹"),
    (r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+0\b", "关机/重启"),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b[^\n|;]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", "下载并直接执行"),
    (r"\b(curl|wget)\b[^\n|;]*\|\s*(python\d?|perl|ruby|node|iex|invoke-expression)\b", "下载并直接执行"),
    (r"\bbase64\s+(-d|--decode)[^\n]*\|\s*(ba|z)?sh\b", "解码后直接执行"),
    (r"(^|[\s;&|(])(sudo|su|doas|runas|pkexec)(\s|$)", "提权命令"),
    (r"(^|[\s;&|])format\s+[a-z]:", "格式化磁盘"),
    (r"\b(rd|rmdir|del|erase)\b[^\n]*\s/[sq]\b[^\n]*\b[a-z]:\\", "危险的 Windows 删除"),
    (r"remove-item[^\n]*-recurse[^\n]*(\b[a-z]:\\\s*($|[;|&])|\b[a-z]:\\\*)", "危险的 PowerShell 删除"),
    (r"\b(diskpart|schtasks|crontab|launchctl|systemctl|bcdedit)\b|\breg\s+(add|delete|import)\b|\bsc\s+(create|config)\b", "系统持久化/服务/注册表修改"),
    (r"\s-enc(odedcommand)?\s", "PowerShell 编码命令"),
    (r"/dev/(tcp|udp)/", "反弹 shell"),
    (r"\b(nc|ncat|netcat)\b[^\n]*\s-[a-z]*e\b", "反弹 shell"),
    (r"(\.ssh[/\\]|\.aws[/\\]|\.gnupg|\.kube[/\\]|\.docker[/\\]config|id_rsa|id_ed25519|/etc/shadow|\.git-credentials|\.netrc)", "访问凭据文件"),
    (r"\bchmod\s+(-r\s+)?[0-7]*777\s+/", "放开系统目录权限"),
    (r">\s*/dev/(sd|nvme|disk|hd)", "写入磁盘设备"),
]]


def hard_deny_reason(cmd: str) -> str | None:
    c = cmd.replace("\\\n", " ")
    for rx, why in HARD_DENY:
        if rx.search(c):
            return why
    return None


_RO_CMDS = {"ls", "dir", "cat", "type", "head", "tail", "wc", "pwd", "echo", "date", "whoami", "uname", "which", "where",
            "tree", "stat", "file", "du", "df", "grep", "rg", "sort", "uniq", "cut", "basename", "dirname", "hostname", "cd"}
_RO_GIT = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame", "remote", "describe", "tag"}
_META = re.compile(r"[;|&<>$`\n(){}*?!]")


def normalize_cmd(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip())


def is_readonly_command(cmd: str) -> bool:
    """极保守的“只读命令”白名单：无 shell 元字符、无绝对路径/上级路径/家目录引用。"""
    if _META.search(cmd):
        return False
    try:
        toks = shlex.split(cmd, posix=not IS_WIN)
    except ValueError:
        return False
    if not toks:
        return False
    for t in toks[1:]:
        if t.startswith(("/", "~", "\\")) or ".." in t or re.match(r"^[A-Za-z]:", t) or sensitive_reason(t):
            return False
    exe = Path(toks[0]).name.lower()
    exe = exe[:-4] if exe.endswith(".exe") else exe
    if exe == "git":
        return len(toks) > 1 and toks[1] in _RO_GIT and "--output" not in " ".join(toks) and "-o" not in toks
    if exe in ("python", "python3", "node", "pip", "pip3", "npm", "cargo", "go", "java"):
        return len(toks) == 2 and toks[1] in ("--version", "-V", "-v", "version")
    return exe in _RO_CMDS


# =============================================================== 沙箱
_bwrap_ok: bool | None = None


def _probe_bwrap() -> bool:
    global _bwrap_ok
    if _bwrap_ok is None:
        exe = shutil.which("bwrap")
        if not exe or not sys.platform.startswith("linux"):
            _bwrap_ok = False
        else:
            try:
                r = subprocess.run([exe, "--ro-bind", "/usr", "/usr", "--unshare-all", "--die-with-parent",
                                    "--proc", "/proc", "--dev", "/dev", "true"], capture_output=True, timeout=8)
                _bwrap_ok = r.returncode == 0
            except Exception:  # noqa: BLE001
                _bwrap_ok = False
    return _bwrap_ok


def sandbox_info() -> dict:
    if _probe_bwrap():
        return {"available": True, "kind": "bwrap", "note": "bubblewrap：仅工作区可写，主目录/密钥不可见，默认断网"}
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return {"available": True, "kind": "sandbox-exec", "note": "macOS sandbox-exec：仅工作区可写，默认禁止联网（该后端未经充分测试）"}
    why = "Windows 无内置轻量沙箱" if IS_WIN else "未安装 bubblewrap（apt install bubblewrap）或用户命名空间被禁用"
    return {"available": False, "kind": "none", "note": f"无 OS 级沙箱（{why}）：所有命令都需要人工审批，且不会被自动放行"}


_SAFE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")


def clean_env(home: str, tmp: str | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
    env.update({"HOME": home, "USERPROFILE": home, "TMPDIR": tmp or home, "TEMP": tmp or home, "TMP": tmp or home,
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat", "CI": "1"})
    return env


def wrap_in_sandbox(argv: list[str], workspace: Path, allow_net: bool, home: Path, *, extra_rw: Sequence[Path | str] = (),
                    extra_ro: Sequence[Path | str] = (), sandbox_home: str | None = None) -> tuple[list[str], str]:
    """把 argv 包进 OS 沙箱；返回 (新 argv, kind)。不可用时返回 (argv, 'none')。
    workspace 可写且是工作目录；extra_rw/extra_ro 是额外可写/只读目录（MCP 服务器按需放宽用）；sandbox_home 指定沙箱内的 HOME（默认 /tmp 的临时目录）。"""
    if _probe_bwrap():
        a = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        if not allow_net:
            a.append("--unshare-net")
        for d in ("usr", "bin", "sbin", "lib", "lib64", "lib32"):
            sp = "/" + d
            if os.path.islink(sp):
                a += ["--symlink", os.readlink(sp), sp]
            elif os.path.isdir(sp):
                a += ["--ro-bind", sp, sp]
        for f in ("/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf", "/etc/hosts", "/etc/passwd", "/etc/group",
                  "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime", "/etc/nsswitch.conf", "/etc/mime.types"):
            if os.path.exists(f):
                a += ["--ro-bind", f, f]
        a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home"]
        for d in extra_ro:                                  # 只读目录先挂，可写目录后挂（嵌套时可写的覆盖只读的）
            if os.path.exists(str(d)):
                a += ["--ro-bind", str(d), str(d)]
        for d in [workspace, *extra_rw]:
            if os.path.exists(str(d)):
                a += ["--bind", str(d), str(d)]
        a += ["--chdir", str(workspace), "--setenv", "HOME", sandbox_home or "/tmp", "--"]
        return a + argv, "bwrap"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        h = os.path.expanduser("~")
        deny = " ".join(f'(subpath "{h}/{d}")' for d in (".ssh", ".aws", ".gnupg", ".kube", ".config", ".docker", "Library/Keychains"))
        prof = ("(version 1)(deny default)(allow process-exec)(allow process-fork)(allow signal (target self))"
                "(allow sysctl-read)(allow mach-lookup)(allow ipc-posix-shm)(allow file-read*)"
                f"(deny file-read* {deny})"
                f'(allow file-write* (subpath "{workspace}") ' + " ".join(f'(subpath "{d}")' for d in extra_rw) +
                f' (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "{home}")'
                ' (literal "/dev/null") (literal "/dev/tty"))' + ("(allow network*)" if allow_net else "(deny network*)"))
        return ["sandbox-exec", "-p", prof] + argv, "sandbox-exec"
    return argv, "none"


# =============================================================== 可疑内容扫描（技能 / 项目指令 / MCP 工具描述共用）
INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|rules)|忽略(之前|以上|上述)的?(所有)?(指令|规则|提示)", re.I), "含“忽略先前指令”类措辞（疑似提示注入）"),
    (re.compile(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh|powershell[^\n]*-enc|base64\s+(-d|--decode)", re.I), "含下载即执行/编码执行"),
    (re.compile(r"[​‌⁠‪-‮⁦-⁩\U000e0000-\U000e007f]"), "含不可见/双向控制字符（可能隐藏指令）"),
]
RISK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\.ssh|id_rsa|\.aws/credentials|\.env\b|api[_-]?key|/etc/passwd", re.I), "涉及凭据/密钥路径"),
    (re.compile(r"\b(rm\s+-rf|mkfs|shutdown|format\s+c:)", re.I), "含破坏性命令"),
    (re.compile(r"https?://[^\s)]+\?[^\s)]*(token|key|secret|data)=", re.I), "含疑似外传数据的 URL"),
]


def suspicious(text: str, *, risk: bool = False) -> list[str]:
    """返回命中的原因列表。默认只查“提示注入类”（项目指令、MCP 描述用）；risk=True 额外查凭据/破坏性命令（技能用）。"""
    pats = INJECTION_PATTERNS + (RISK_PATTERNS if risk else [])
    return [why for rx, why in pats if rx.search(text or "")]


# =============================================================== 权限策略
@dataclass
class Decision:
    action: str          # allow | ask | deny
    reason: str = ""
    risk: str = "low"
    key: str = ""        # “本会话允许”记录的键


class Policy:
    """kind: read | meta | memory_read | memory_write | net_search | net_fetch | write | delete | exec | skill | mcp"""

    @staticmethod
    def decide(*, kind: str, name: str, mode: str, tainted: bool, allow_keys: set[str], sensitive: str | None = None,
               command: str | None = None, sandboxed: bool = False, domain_ok: bool = True) -> Decision:
        key = f"exec:{normalize_cmd(command)}" if (kind == "exec" and command) else name
        risk = {"write": "medium", "delete": "high", "exec": "high", "skill": "high", "mcp": "high", "net_fetch": "medium"}.get(kind, "low")
        if mode == "plan" and kind in ("write", "delete", "exec", "skill", "mcp"):
            return Decision("deny", "当前为「规划（只读）」模式：不能修改文件、执行命令或调用外部工具。请切换权限模式后重试。", risk, key)
        if kind in ("read", "meta", "memory_read"):
            if sensitive:
                return Decision("ask", f"读取敏感文件: {sensitive}", "medium", key)
            return Decision("allow", "", "low", key)
        if kind in ("net_search", "memory_write"):
            return Decision("allow", "", "low", key)
        if kind == "net_fetch":
            if tainted and not domain_ok:
                return Decision("ask", "上下文含不可信内容，且该域名未出现在你的消息或搜索结果中——可能是提示注入试图外泄数据，请确认。", "high", key)
            return Decision("allow", "", "medium", key)
        if kind == "mcp":      # 外部 MCP 工具：默认逐次确认；其结果会污染会话（除非该服务器被设为“信任结果”），所以“本会话允许”只对可信服务器有意义
            if key in allow_keys and not tainted:
                return Decision("allow", "", "high", key)
            if mode == "auto" and not tainted and sandboxed:
                return Decision("allow", "", "high", key)
            why = "调用外部工具（MCP 服务器）"
            if tainted:
                why += "（上下文含不可信内容，已强制确认）"
            elif mode == "auto" and not sandboxed:
                why += "（该服务器没有沙箱，全自动模式下也需确认）"
            return Decision("ask", why, "high", key)
        # ---- 写 / 删 / 执行 / 技能
        if sensitive:
            return Decision("ask", f"涉及敏感文件: {sensitive}", "high", key)
        if kind == "skill":
            return Decision("ask", "创建/修改技能会改变 agent 的行为，必须由你确认。", "high", key)
        if kind == "write":
            if key in allow_keys:
                return Decision("allow", "", risk, key)
            if mode in ("auto_edit", "auto"):
                if tainted:
                    return Decision("ask", "上下文含不可信内容（网页/文档），自动编辑已暂停，请确认这次修改。", risk, key)
                return Decision("allow", "", risk, key)
            return Decision("ask", "修改工作区文件", risk, key)
        if kind == "delete":
            if mode == "auto" and not tainted:
                return Decision("allow", "", risk, key)
            return Decision("ask", "删除（会进入回收站，可恢复）" + ("；上下文含不可信内容" if tainted else ""), risk, key)
        if kind == "exec":
            if command and not tainted and is_readonly_command(command):
                return Decision("allow", "", "low", key)
            if key in allow_keys and not tainted:
                return Decision("allow", "", risk, key)
            if mode == "auto" and not tainted and sandboxed:
                return Decision("allow", "", risk, key)
            why = "执行 shell 命令"
            if tainted:
                why += "（上下文含不可信内容，已强制确认）"
            elif mode == "auto" and not sandboxed:
                why += "（本机无 OS 沙箱，全自动模式下也需确认）"
            return Decision("ask", why, risk, key)
        return Decision("ask", "需要确认", risk, key)


# =============================================================== 审计
_REDACT_KEYS = {"content", "text", "old", "new", "body", "old_string", "new_string"}


def summarize_args(args: Any, limit: int = 200) -> Any:
    if isinstance(args, dict):
        out = {}
        for k, v in args.items():
            if k in _REDACT_KEYS and isinstance(v, str):
                out[k] = f"<{len(v)} chars sha:{hashlib.sha256(v.encode('utf-8', 'replace')).hexdigest()[:8]}>"
            else:
                out[k] = summarize_args(v, limit)
        return out
    if isinstance(args, list):
        return [summarize_args(x, limit) for x in args[:20]]
    if isinstance(args, str) and len(args) > limit:
        return args[:limit] + f"…(+{len(args) - limit})"
    return args


class AuditLog:
    """追加写、哈希链：每条记录包含上一条的哈希，任何篡改/删除都会被 verify() 发现。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last, self._seq = "0" * 64, 0
        if self.path.exists():
            try:
                with open(self.path, "rb") as f:
                    for line in f:
                        if line.strip():
                            e = json.loads(line)
                            self._last, self._seq = e.get("hash", self._last), e.get("seq", self._seq)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _digest(prev: str, body: dict) -> str:
        return hashlib.sha256((prev + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()

    def append(self, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            body = {"ts": round(time.time(), 3), "seq": self._seq, **fields}
            e = {**body, "prev": self._last}
            e["hash"] = self._digest(self._last, body)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                f.flush()
            self._last = e["hash"]

    def tail(self, n: int = 200) -> list[dict]:
        try:
            lines = self.path.read_text("utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def verify(self) -> dict:
        prev, count = "0" * 64, 0
        try:
            with open(self.path, "rb") as f:
                for i, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    e = json.loads(line)
                    h = e.pop("hash", None)
                    p = e.pop("prev", None)
                    if p != prev or h != self._digest(prev, e):
                        return {"ok": False, "count": count, "bad_line": i}
                    prev, count = h, count + 1
        except FileNotFoundError:
            return {"ok": True, "count": 0}
        except (OSError, ValueError):
            return {"ok": False, "count": count, "bad_line": -1}
        return {"ok": True, "count": count}


# =============================================================== 本地服务鉴权
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_ok(supplied: str | None, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def host_ok(host_header: str | None, port: int) -> bool:
    return (host_header or "").lower() in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def origin_ok(origin: str | None, port: int) -> bool:
    if not origin:
        return True  # 同源 GET / 非浏览器客户端不带 Origin；写操作另有 Cookie 校验
    return origin.lower() in {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
