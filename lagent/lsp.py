"""轻量级零依赖 LSP 诊断客户端（stdio JSON-RPC），自动探测 PATH 中已安装的语言服务器。"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import security as sec

_EXT_LANG = {
    ".py": ("python", (("basedpyright-langserver", "--stdio"), ("pyright-langserver", "--stdio"), ("pylsp",))),
    ".ts": ("typescript", (("typescript-language-server", "--stdio"),)),
    ".tsx": ("typescriptreact", (("typescript-language-server", "--stdio"),)),
    ".js": ("javascript", (("typescript-language-server", "--stdio"),)),
    ".jsx": ("javascriptreact", (("typescript-language-server", "--stdio"),)),
    ".rs": ("rust", (("rust-analyzer",),)),
    ".go": ("go", (("gopls",),)),
}

_SEV_LABEL = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def available_servers() -> dict[str, list[str]]:
    """探测本机 PATH 中可用的语言服务器命令。"""
    found: dict[str, list[str]] = {}
    for ext, (lang, cands) in _EXT_LANG.items():
        if lang in found:
            continue
        for cmd in cands:
            exe = shutil.which(cmd[0])
            if exe:
                found[lang] = [exe, *cmd[1:]]
                break
    return found


def _encode_rpc(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    hdr = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return hdr + body


def _read_rpc(stream: Any, deadline: float) -> dict[str, Any] | None:
    hdrs: dict[str, str] = {}
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return None
        s = line.decode("ascii", "replace").strip()
        if not s:
            break
        if ":" in s:
            k, v = s.split(":", 1)
            hdrs[k.lower().strip()] = v.strip()
    clen = int(hdrs.get("content-length") or 0)
    if clen <= 0 or clen > 4_000_000:
        return None
    raw = stream.read(clen)
    if not raw or len(raw) < clen:
        return None
    return json.loads(raw.decode("utf-8", "replace"))


def query_lsp_diagnostics(
    workspace: Path,
    files: list[Path],
    *,
    timeout_s: float = 4.0,
    max_per_lang: int = 8,
) -> list[str]:
    """向可用 LSP 服务器发送 initialize + didOpen 并在短超时内回收 publishDiagnostics。"""
    servers = available_servers()
    if not servers or not files:
        return []

    by_lang: dict[str, list[Path]] = {}
    for fp in files:
        info = _EXT_LANG.get(fp.suffix.lower())
        if not info:
            continue
        lang = info[0]
        if lang in servers:
            by_lang.setdefault(lang, []).append(fp)

    out: list[str] = []
    ws_resolved = Path(workspace).resolve()
    kw: dict[str, Any] = {"creationflags": 0x08000000} if sec.IS_WIN else {}

    for lang, fps in by_lang.items():
        cmd = servers[lang]
        batch = fps[:max_per_lang]
        uri_to_rel: dict[str, str] = {}
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ws_resolved),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **kw,
            )
            assert proc.stdin and proc.stdout
            deadline = time.monotonic() + timeout_s
            proc.stdin.write(_encode_rpc({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": None,
                    "rootUri": ws_resolved.as_uri(),
                    "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}}},
                },
            }))
            proc.stdin.flush()

            # 等待 initialize 响应
            init_ok = False
            while time.monotonic() < deadline:
                msg = _read_rpc(proc.stdout, deadline)
                if msg is None:
                    break
                if msg.get("id") == 1:
                    init_ok = "result" in msg
                    break
            if not init_ok:
                continue

            proc.stdin.write(_encode_rpc({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
            for fp in batch:
                try:
                    txt = fp.read_text("utf-8", "replace")
                    rel = fp.resolve().relative_to(ws_resolved).as_posix()
                except Exception:  # noqa: BLE001
                    continue
                uri = fp.resolve().as_uri()
                uri_to_rel[uri] = rel
                proc.stdin.write(_encode_rpc({
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {"textDocument": {"uri": uri, "languageId": lang, "version": 1, "text": txt}},
                }))
            proc.stdin.flush()

            seen_uris: set[str] = set()
            while time.monotonic() < deadline and len(seen_uris) < len(uri_to_rel):
                msg = _read_rpc(proc.stdout, deadline)
                if msg is None:
                    break
                if msg.get("method") == "textDocument/publishDiagnostics":
                    p = msg.get("params") or {}
                    uri = str(p.get("uri") or "")
                    seen_uris.add(uri)
                    rel = uri_to_rel.get(uri) or uri
                    for diag in (p.get("diagnostics") or [])[:10]:
                        sev = int(diag.get("severity") or 1)
                        if sev > 2:  # 仅返回 error / warning 减少噪音
                            continue
                        pos = ((diag.get("range") or {}).get("start") or {})
                        ln = int(pos.get("line") or 0) + 1
                        col = int(pos.get("character") or 0) + 1
                        msg_txt = str(diag.get("message") or "").splitlines()[0][:180]
                        out.append(f"{rel}:{ln}:{col}: [{_SEV_LABEL.get(sev, 'error')}] {msg_txt}")

            try:
                proc.stdin.write(_encode_rpc({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None}))
                proc.stdin.write(_encode_rpc({"jsonrpc": "2.0", "method": "exit"}))
                proc.stdin.flush()
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            continue
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    return out
