"""工作区文件工具：读 / 写 / 精确编辑 / 列目录 / glob / grep / 删除(回收站) / 移动 / 复制 / 建目录 / 命令 / 待办 / 提问。"""
from __future__ import annotations

import asyncio
import base64
import codecs
import difflib
import fnmatch
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import security as sec
from ..journal import atomic_write, sha256_bytes, sha256_file
from .base import P, Tool, ToolResult, clip, obj, register
from .ctx import ToolCtx

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "dist", "build", ".next", "target"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
OFFICE_EXT = {".docx", ".xlsx", ".xlsm", ".pptx", ".pdf"}
MAX_READ_CHARS = 40000


def err(msg: str) -> ToolResult:
    return ToolResult(msg, error=True)


# ------------------------------------------------------------------ 文本编码
def decode_bytes(b: bytes) -> tuple[str, str, str]:
    """返回 (文本, 编码, 换行符)。编码用于原样写回，避免把 GBK/UTF-16/BOM 文件悄悄改成 UTF-8。"""
    if b.startswith(codecs.BOM_UTF8):
        enc = "utf-8-sig"
    elif b.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        enc = "utf-16"
    else:
        enc = "utf-8"
    try:
        s = b.decode(enc)
    except UnicodeDecodeError:
        try:
            s, enc = b.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            s, enc = b.decode("utf-8", "replace"), "utf-8"
    nl = "\r\n" if s.count("\r\n") > s.count("\n") / 2 and "\r\n" in s else "\n"
    return s.replace("\r\n", "\n"), enc, nl


def encode_text(s: str, enc: str, nl: str) -> bytes:
    if nl == "\r\n":
        s = s.replace("\n", "\r\n")
    return s.encode(enc, "replace" if enc != "utf-8" else "strict")


def looks_binary(b: bytes) -> bool:
    head = b[:8192]
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    return b"\x00" in head


def make_diff(old: str, new: str, name: str, limit: int = 6000) -> str:
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{name}", f"b/{name}", n=2))
    return clip(d, limit) if d else "(无变化)"


# ------------------------------------------------------------------ 写入 + 日志
def commit_bytes(ctx: ToolCtx, p: Path, data: bytes, op: str, summary: str) -> dict:
    if len(data) > ctx.max_bytes:
        raise ValueError(f"内容超过大小上限 {ctx.max_bytes // 1048576} MB")
    existed = p.exists()
    if existed and not p.is_file():
        raise ValueError("目标不是普通文件")
    snap = ctx.journal.snapshot(p) if existed else None
    atomic_write(p, data)
    jid = ctx.journal.record(ctx.sid, op=op, path=p, summary=summary, snap=snap, existed=existed, after=sha256_bytes(data))
    return {"path": ctx.guard.rel(p), "undo_id": jid, "created": not existed}


# ------------------------------------------------------------------ 先读再改
def mark_seen(ctx: ToolCtx, p: Path, data: bytes) -> None:
    """记录“本会话里模型见过/写过这份内容”。内容之后若被用户或命令改动，哈希会对不上。"""
    ctx.session.seen[ctx.guard.rel(p)] = sha256_bytes(data)


def _stale_reason(ctx: ToolCtx, p: Path) -> str | None:
    if not ctx.cfg.get("agent", "require_read_before_edit", default=True) or not p.is_file():
        return None
    h = ctx.session.seen.get(ctx.guard.rel(p))
    if h is None:
        return f"请先用 read_file 读取 {ctx.guard.rel(p)} 再修改它（本会话还没有读过这个文件，直接改很容易写错）。"
    try:
        if sha256_file(p) != h:
            return f"{ctx.guard.rel(p)} 在你上次读取/写入之后被改动过（可能是用户或某条命令改的）。请重新 read_file 再修改。"
    except OSError:
        return None
    return None


def _edit_precheck(ctx: ToolCtx, a: dict) -> str | None:
    try:
        p = ctx.guard.resolve(a.get("path", ""), write=True)
    except Exception:  # noqa: BLE001  路径问题交给后面的统一安全检查去报告
        return None
    if p.suffix.lower() in OFFICE_EXT:
        return None
    return _stale_reason(ctx, p)


def _ensure_parent(ctx: ToolCtx, p: Path) -> list[Path]:
    """为新文件创建缺失的父目录，并为每个新建目录写 mkdir 日志（可撤销）。"""
    made: list[Path] = []
    cur = p.parent
    while not cur.exists() and not ctx.guard.is_ws_root(cur):
        made.append(cur)
        cur = cur.parent
    for d in reversed(made):
        d.mkdir(exist_ok=True)
        ctx.journal.record(ctx.sid, op="mkdir", path=d, summary=f"创建目录 {ctx.guard.rel(d)}")
    return made


# ------------------------------------------------------------------ read_file
async def read_file(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"])
    if not p.exists():
        return err(f"文件不存在: {a['path']}（可用 list_dir / glob 查找）")
    if p.is_dir():
        return err(f"这是目录，请用 list_dir: {a['path']}")
    ext = p.suffix.lower()
    size = p.stat().st_size
    rel = ctx.guard.rel(p)
    risky = rel.startswith("uploads/")
    if ext in IMG_EXT:
        return await asyncio.to_thread(_read_image, ctx, p, size)
    if ext in OFFICE_EXT:
        from . import office
        text = await asyncio.to_thread(office.read_document, p, a.get("offset", 1), a.get("limit"), MAX_READ_CHARS)
        return ToolResult(text, taint=f"读取文档 {rel}（文档内容不可信）")
    if size > ctx.max_bytes:
        return err(f"文件过大（{size / 1048576:.1f} MB），请用 grep 或指定 offset/limit 的方式分段读取以外的手段处理")
    raw = await asyncio.to_thread(p.read_bytes)
    if looks_binary(raw):
        return err(f"二进制文件（{size} 字节，类型 {ext or '未知'}），无法按文本读取")
    text, enc, _nl = decode_bytes(raw)
    mark_seen(ctx, p, raw)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    off = max(1, int(a.get("offset") or 1))
    lim = int(a.get("limit") or 2000)
    chunk = lines[off - 1: off - 1 + lim]
    out, used = [], 0
    for i, ln in enumerate(chunk, off):
        if len(ln) > 2000:
            ln = ln[:2000] + "…(行过长已截断)"
        s = f"{i:>6}\t{ln}"
        used += len(s) + 1
        if used > MAX_READ_CHARS:
            out.append(f"…[输出过长，已截止到第 {i - 1} 行；用 offset={i} 继续读取]")
            break
        out.append(s)
    head = f"[{rel} · 共 {total} 行 · 编码 {enc}]"
    if off - 1 + lim < total and not (out and out[-1].startswith("…")):
        out.append(f"…[还有 {total - off + 1 - len(chunk)} 行；用 offset={off + len(chunk)} 继续]")
    if not text:
        out = ["(空文件)"]
    return ToolResult(head + "\n" + "\n".join(out), taint=f"读取上传文件 {rel}" if risky else "")


def _read_image(ctx: ToolCtx, p: Path, size: int) -> ToolResult:
    try:
        from PIL import Image  # type: ignore
        im = Image.open(p)
        w, h = im.size
        info = f"[图片 {ctx.guard.rel(p)} · {w}×{h} · {size // 1024} KB]"
        if not ctx.vision:
            return ToolResult(info + "\n当前模型未加载视觉投影(mmproj)，无法查看图片内容。", meta={"path": ctx.guard.rel(p)})
        im = im.convert("RGB")
        m = 1568
        if max(w, h) > m:
            r = m / max(w, h)
            im = im.resize((max(1, int(w * r)), max(1, int(h * r))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        return ToolResult(info + "\n图片已附在下一条消息中。", images=[url])
    except ImportError:
        return err("读取图片需要 Pillow（pip install pillow）")
    except Exception as e:  # noqa: BLE001
        return err(f"无法打开图片: {e}")


register(Tool(
    "read_file", "读取工作区内的文件。文本带行号返回，支持 offset/limit 分段；也能读 docx/xlsx/pptx/pdf（提取文本）和图片（需多模态模型）。",
    obj({"path": P("string", "相对工作区的路径"), "offset": P("integer", "起始行号(从1开始)"),
         "limit": P("integer", "最多读取行数，默认 2000")}, ["path"]),
    read_file, "read", title=lambda a: f"读取 {a.get('path', '')}", path_args=("path",)))


# ------------------------------------------------------------------ write_file / edit_file
async def write_file(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if p.is_dir():
        return err("目标是目录")
    if p.suffix.lower() in OFFICE_EXT:
        return err(f"{p.suffix} 是二进制办公格式，不能用 write_file 写入。请改用 write_document / write_spreadsheet / write_presentation。")
    content = a["content"]
    if len(content.encode("utf-8")) > ctx.max_bytes:
        return err("内容过大")
    enc, nl, old = "utf-8", "\n", None
    if p.exists():
        raw = p.read_bytes()
        if looks_binary(raw):
            return err("目标是二进制文件，拒绝覆盖")
        old, enc, nl = decode_bytes(raw)
    else:
        _ensure_parent(ctx, p)
    data = encode_text(content, enc, nl)
    meta = commit_bytes(ctx, p, data, "write", f"{'覆盖' if old is not None else '创建'} {ctx.guard.rel(p)}")
    mark_seen(ctx, p, data)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return ToolResult(f"已{'覆盖' if old is not None else '创建'} {meta['path']}（{n} 行，{len(content)} 字符）", meta=meta)


async def write_preview(ctx: ToolCtx, a: dict) -> dict | None:
    try:
        p = ctx.guard.resolve(a.get("path", ""), write=True)
    except sec.SecurityError as e:
        return {"kind": "text", "text": str(e)}
    content = a.get("content", "")
    if p.is_file():
        raw = p.read_bytes()
        if looks_binary(raw):
            return {"kind": "text", "text": "(目标是二进制文件)"}
        old = decode_bytes(raw)[0]
        return {"kind": "diff", "text": make_diff(old, content, ctx.guard.rel(p))}
    return {"kind": "diff", "text": make_diff("", content, ctx.guard.rel(p) + "（新文件）")}


register(Tool(
    "write_file", "创建新文本文件，或用 content 完整覆盖已有文件。修改已有文件的局部内容请优先用 edit_file。会自动创建缺失的目录。",
    obj({"path": P("string", "相对工作区的路径"), "content": P("string", "完整文件内容")}, ["path", "content"]),
    write_file, "write", title=lambda a: f"写入 {a.get('path', '')}", preview=write_preview, path_args=("path",),
    precheck=_edit_precheck))


def _lead(s: str) -> str:
    return s[: len(s) - len(s.lstrip(" \t"))]


def _reindent_edit(text: str, old: str, new: str) -> str | None:
    """容错：old_string 的缩进（空格/Tab/层级）与文件不一致，但去掉首尾空白后逐行一致，且只有唯一一处匹配时，
    按文件里的真实缩进替换，并把 new_string 的缩进整体平移对齐。本地小模型最常见的就是缩进抄错。"""
    t_lines, o_lines, n_lines = text.split("\n"), old.split("\n"), new.split("\n")
    trimmed = False
    while o_lines and not o_lines[0].strip():
        o_lines.pop(0)
        trimmed = True
    while o_lines and not o_lines[-1].strip():
        o_lines.pop()
        trimmed = True
    if not o_lines:
        return None
    key = [x.strip() for x in o_lines]
    hits = [i for i in range(len(t_lines) - len(key) + 1) if all(t_lines[i + j].strip() == key[j] for j in range(len(key)))]
    if len(hits) != 1:
        return None
    if trimmed:
        while n_lines and not n_lines[0].strip():
            n_lines.pop(0)
        while n_lines and not n_lines[-1].strip():
            n_lines.pop()
    i = hits[0]
    win_ind, old_ind = _lead(t_lines[i]), _lead(o_lines[0])
    if win_ind != old_ind:
        shifted = []
        for ln in n_lines:
            if not ln.strip():
                shifted.append(ln)
            elif ln.startswith(old_ind):
                shifted.append(win_ind + ln[len(old_ind):])
            else:
                return None          # new_string 的缩进体系和 old_string 对不上，无法安全推断，交还给模型重读
        n_lines = shifted
    t_lines[i:i + len(key)] = n_lines
    return "\n".join(t_lines)


def _apply_edit(text: str, old: str, new: str, replace_all: bool) -> tuple[str | None, str]:
    if old == new:
        return None, "old_string 与 new_string 相同，无需修改"
    cnt = text.count(old)
    if cnt == 0:
        # 容错：忽略行尾空白差异
        norm = lambda s: "\n".join(x.rstrip() for x in s.split("\n"))  # noqa: E731
        if old and norm(old) in norm(text) and norm(text).count(norm(old)) == 1:
            t_lines, o_lines = text.split("\n"), old.split("\n")
            for i in range(len(t_lines) - len(o_lines) + 1):
                if all(t_lines[i + j].rstrip() == o_lines[j].rstrip() for j in range(len(o_lines))):
                    t_lines[i:i + len(o_lines)] = new.split("\n")
                    return "\n".join(t_lines), "ok(忽略行尾空白)"
        # 容错：忽略缩进差异（唯一匹配时按文件原缩进对齐）
        if old.strip() and not replace_all:
            fixed = _reindent_edit(text, old, new)
            if fixed is not None:
                return fixed, "ok(缩进已按文件原样对齐)"
        first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
        near = difflib.get_close_matches(first, [ln.strip() for ln in text.split("\n")], n=3, cutoff=0.6) if first else []
        hint = ("；文件中最相似的行: " + " | ".join(repr(x[:80]) for x in near)) if near else ""
        return None, f"在文件中找不到 old_string（需逐字符匹配，含缩进和换行）。请先 read_file 确认原文{hint}"
    if cnt > 1 and not replace_all:
        return None, f"old_string 出现了 {cnt} 次，不唯一。请包含更多上下文使其唯一，或设置 replace_all=true"
    return text.replace(old, new) if replace_all else text.replace(old, new, 1), "ok"


async def edit_file(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if not p.is_file():
        return err(f"文件不存在: {a['path']}（新建请用 write_file）")
    if p.suffix.lower() in OFFICE_EXT:
        return err("办公格式文件请用 edit_office")
    raw = p.read_bytes()
    if looks_binary(raw):
        return err("二进制文件，不能按文本编辑")
    text, enc, nl = decode_bytes(raw)
    new_text, why = _apply_edit(text, a["old_string"], a["new_string"], bool(a.get("replace_all")))
    if new_text is None:
        return err(why)
    data = encode_text(new_text, enc, nl)
    meta = commit_bytes(ctx, p, data, "edit", f"编辑 {ctx.guard.rel(p)}")
    mark_seen(ctx, p, data)
    return ToolResult(f"已编辑 {meta['path']}\n" + make_diff(text, new_text, meta["path"], 3000), meta=meta)


async def edit_preview(ctx: ToolCtx, a: dict) -> dict | None:
    try:
        p = ctx.guard.resolve(a.get("path", ""), write=True)
        if not p.is_file():
            return {"kind": "text", "text": "文件不存在"}
        text = decode_bytes(p.read_bytes())[0]
        new_text, why = _apply_edit(text, a.get("old_string", ""), a.get("new_string", ""), bool(a.get("replace_all")))
        if new_text is None:
            return {"kind": "text", "text": why}
        return {"kind": "diff", "text": make_diff(text, new_text, ctx.guard.rel(p))}
    except Exception as e:  # noqa: BLE001
        return {"kind": "text", "text": str(e)}


register(Tool(
    "edit_file", "对已有文本文件做精确字符串替换。old_string 必须在文件中唯一（否则加上下文或设 replace_all）。修改前先 read_file。",
    obj({"path": P("string", "文件路径"), "old_string": P("string", "要被替换的原文（逐字符，含缩进）"),
         "new_string": P("string", "替换后的文本"), "replace_all": P("boolean", "替换所有出现处，默认 false")},
        ["path", "old_string", "new_string"]),
    edit_file, "write", title=lambda a: f"编辑 {a.get('path', '')}", preview=edit_preview, path_args=("path",),
    precheck=_edit_precheck))


# ------------------------------------------------------------------ list_dir / glob / grep
def _walk(root: Path, max_depth: int):
    base = len(root.parts)
    for dp, dns, fns in os.walk(root, followlinks=False):
        d = Path(dp)
        depth = len(d.parts) - base
        dns[:] = sorted(x for x in dns if x not in SKIP_DIRS and not (d / x).is_symlink())
        if depth >= max_depth:
            dns[:] = []
        yield d, dns, sorted(fns)


def _list_dir_sync(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a.get("path") or ".")
    if not p.is_dir():
        return err(f"不是目录: {a.get('path')}")
    depth = max(1, min(4, int(a.get("depth") or 1)))
    lines, n = [], 0
    for d, dns, fns in _walk(p, depth):
        ind = "  " * (len(d.parts) - len(p.parts))
        if d != p:
            lines.append(f"{ind[:-2]}{d.name}/")
        for f in fns:
            try:
                sz = (d / f).stat().st_size
            except OSError:
                sz = 0
            lines.append(f"{ind}{f}  ({_hsize(sz)})")
            n += 1
            if n >= 400:
                lines.append("…[条目过多，已截断；请指定子目录]")
                return ToolResult(f"{ctx.guard.rel(p)}/\n" + "\n".join(lines))
        if depth == 1:
            lines = [f"{x}/" for x in dns] + lines
    return ToolResult(f"{ctx.guard.rel(p)}/\n" + ("\n".join(lines) or "(空目录)"))


async def list_dir(ctx: ToolCtx, a: dict) -> ToolResult:
    return await asyncio.to_thread(_list_dir_sync, ctx, a)       # 遍历目录放进线程：不卡事件循环，也能和其他只读调用并发


def _hsize(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return ""


register(Tool("list_dir", "列出目录内容（默认当前工作区根）。depth 最大 4。",
              obj({"path": P("string", "目录，默认 '.'"), "depth": P("integer", "递归深度 1-4，默认 1")}),
              list_dir, "read", title=lambda a: f"列出 {a.get('path') or '.'}", path_args=("path",)))


def _glob_sync(ctx: ToolCtx, a: dict) -> ToolResult:
    root = ctx.guard.resolve(a.get("path") or ".")
    pat = a["pattern"].replace("\\", "/")
    rx = re.compile(fnmatch.translate(pat if "/" in pat else "**/" + pat).replace(r"(?s:", "(?s:"), re.I)
    # fnmatch 的 * 会跨 /，足以满足“**/*.py”语义
    res = []
    for d, _dns, fns in _walk(root, 12):
        for f in fns:
            rel = (d / f).relative_to(ctx.guard.ws).as_posix()
            if rx.match(rel) or rx.match("/" + rel) or fnmatch.fnmatch(f, pat):
                res.append(rel)
                if len(res) >= 500:
                    return ToolResult("\n".join(res) + "\n…[已达 500 条上限]")
    return ToolResult("\n".join(res) if res else "没有匹配的文件")


async def glob_files(ctx: ToolCtx, a: dict) -> ToolResult:
    return await asyncio.to_thread(_glob_sync, ctx, a)


register(Tool("glob", "按通配符查找文件，如 '**/*.py'、'*.docx'、'src/**/test_*.js'。",
              obj({"pattern": P("string", "通配符"), "path": P("string", "起始目录，默认工作区根")}, ["pattern"]),
              glob_files, "read", title=lambda a: f"查找 {a.get('pattern', '')}", path_args=("path",)))


def _grep_sync(ctx: ToolCtx, a: dict) -> ToolResult:
    root = ctx.guard.resolve(a.get("path") or ".")
    flags = re.I if a.get("ignore_case") else 0
    try:
        rx = re.compile(a["pattern"], flags)
    except re.error as e:
        return err(f"正则表达式错误: {e}")
    gl = a.get("glob")
    files = [root] if root.is_file() else [d / f for d, _x, fns in _walk(root, 12) for f in fns]
    out, nfiles = [], 0
    t0 = time.time()
    for f in files:
        if gl and not fnmatch.fnmatch(f.name, gl):
            continue
        try:
            if f.stat().st_size > 2_000_000 or f.suffix.lower() in OFFICE_EXT | IMG_EXT:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if looks_binary(raw):
            continue
        text = decode_bytes(raw)[0]
        hit = False
        for i, ln in enumerate(text.split("\n"), 1):
            if rx.search(ln):
                out.append(f"{ctx.guard.rel(f)}:{i}: {ln.strip()[:200]}")
                hit = True
                if len(out) >= 200:
                    return ToolResult("\n".join(out) + "\n…[已达 200 条上限]")
        nfiles += hit
        if time.time() - t0 > 15:
            out.append("…[搜索超时，结果不完整]")
            break
    return ToolResult("\n".join(out) if out else "没有匹配")


async def grep(ctx: ToolCtx, a: dict) -> ToolResult:
    return await asyncio.to_thread(_grep_sync, ctx, a)


register(Tool("grep", "在工作区文件内容中用正则搜索，返回 路径:行号: 内容。",
              obj({"pattern": P("string", "正则表达式"), "path": P("string", "文件或目录，默认工作区根"),
                   "glob": P("string", "仅搜索匹配该通配符的文件名，如 '*.py'"), "ignore_case": P("boolean", "忽略大小写")}, ["pattern"]),
              grep, "read", title=lambda a: f"搜索 {a.get('pattern', '')}", path_args=("path",)))


# ------------------------------------------------------------------ delete / move / copy / mkdir
async def delete_path(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if ctx.guard.is_ws_root(p):
        return err("不能删除工作区根目录")
    if not os.path.lexists(p):
        return err(f"路径不存在: {a['path']}")
    is_dir = p.is_dir() and not p.is_symlink()
    if is_dir and any(p.iterdir()) and not a.get("recursive"):
        return err("目录非空；确认要删除整个目录请设置 recursive=true")
    tid = ctx.journal.trash(p, ctx.sid)
    jid = ctx.journal.record(ctx.sid, op="delete", path=p, summary=f"删除 {ctx.guard.rel(p)}", extra={"trash_id": tid})
    return ToolResult(f"已删除 {ctx.guard.rel(p)}（移入回收站，可恢复）", meta={"path": ctx.guard.rel(p), "undo_id": jid})


register(Tool("delete_path", "删除文件或目录。实际是移入回收站，用户可随时恢复。非空目录需 recursive=true。",
              obj({"path": P("string", "要删除的路径"), "recursive": P("boolean", "允许删除非空目录")}, ["path"]),
              delete_path, "delete", title=lambda a: f"删除 {a.get('path', '')}", path_args=("path",)))


async def move_path(ctx: ToolCtx, a: dict) -> ToolResult:
    src = ctx.guard.resolve(a["src"], write=True)
    dst = ctx.guard.resolve(a["dst"], write=True)
    if not os.path.lexists(src):
        return err(f"源路径不存在: {a['src']}")
    if ctx.guard.is_ws_root(src):
        return err("不能移动工作区根目录")
    if dst.is_dir() and not src.samefile(dst):
        dst = dst / src.name
        dst = ctx.guard.resolve(str(dst), write=True)
    if sec.is_within(dst, src) and src.is_dir():
        return err("不能把目录移动到它自己内部")
    tid = None
    if os.path.lexists(dst):
        if not a.get("overwrite"):
            return err(f"目标已存在: {ctx.guard.rel(dst)}（设置 overwrite=true 将把旧文件送入回收站）")
        tid = ctx.journal.trash(dst, ctx.sid)
    _ensure_parent(ctx, dst)
    shutil.move(str(src), str(dst))
    jid = ctx.journal.record(ctx.sid, op="move", path=dst, summary=f"移动 {ctx.guard.rel(src)} → {ctx.guard.rel(dst)}",
                             extra={"src": str(src), "dst": str(dst), "trash_id": tid})
    return ToolResult(f"已移动 {ctx.guard.rel(src)} → {ctx.guard.rel(dst)}", meta={"path": ctx.guard.rel(dst), "undo_id": jid})


register(Tool("move_path", "移动或重命名文件/目录。",
              obj({"src": P("string", "源路径"), "dst": P("string", "目标路径"), "overwrite": P("boolean", "目标存在时覆盖（旧文件进回收站）")}, ["src", "dst"]),
              move_path, "write", title=lambda a: f"移动 {a.get('src', '')} → {a.get('dst', '')}", path_args=("src", "dst")))


async def copy_path(ctx: ToolCtx, a: dict) -> ToolResult:
    src = ctx.guard.resolve(a["src"])
    dst = ctx.guard.resolve(a["dst"], write=True)
    if not src.exists():
        return err(f"源路径不存在: {a['src']}")
    if dst.is_dir():
        dst = ctx.guard.resolve(str(dst / src.name), write=True)
    tid = None
    if dst.exists():
        if not a.get("overwrite"):
            return err(f"目标已存在: {ctx.guard.rel(dst)}（设置 overwrite=true 将把旧文件送入回收站）")
        tid = ctx.journal.trash(dst, ctx.sid)
    _ensure_parent(ctx, dst)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    jid = ctx.journal.record(ctx.sid, op="copy", path=dst, summary=f"复制 {ctx.guard.rel(src)} → {ctx.guard.rel(dst)}",
                             extra={"trash_id": tid})
    return ToolResult(f"已复制到 {ctx.guard.rel(dst)}", meta={"path": ctx.guard.rel(dst), "undo_id": jid})


register(Tool("copy_path", "复制文件或目录。",
              obj({"src": P("string", "源路径"), "dst": P("string", "目标路径"), "overwrite": P("boolean", "目标存在时覆盖（旧文件进回收站）")}, ["src", "dst"]),
              copy_path, "write", title=lambda a: f"复制 {a.get('src', '')} → {a.get('dst', '')}", path_args=("src", "dst")))


async def make_dir(ctx: ToolCtx, a: dict) -> ToolResult:
    p = ctx.guard.resolve(a["path"], write=True)
    if p.exists():
        return ToolResult(f"已存在: {ctx.guard.rel(p)}")
    _ensure_parent(ctx, p)
    p.mkdir()
    jid = ctx.journal.record(ctx.sid, op="mkdir", path=p, summary=f"创建目录 {ctx.guard.rel(p)}")
    return ToolResult(f"已创建目录 {ctx.guard.rel(p)}", meta={"path": ctx.guard.rel(p), "undo_id": jid})


register(Tool("make_dir", "创建目录（含父目录）。", obj({"path": P("string", "目录路径")}, ["path"]),
              make_dir, "write", title=lambda a: f"新建目录 {a.get('path', '')}", path_args=("path",)))


# ------------------------------------------------------------------ run_command
def _shell_argv(cmd: str) -> list[str]:
    if sec.IS_WIN:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", cmd]
    return [shutil.which("bash") or "/bin/sh", "-c", cmd]


@dataclass
class Prepared:
    """一条命令真正启动所需的全部东西（沙箱包装、清洗后的环境、工作目录），前台与后台命令共用，保证安全策略一致。"""
    argv: list[str]
    env: dict[str, str]
    kw: dict[str, Any]
    cwd: Path
    mode: str            # 设置里的 command_sandbox
    kind: str            # 实际使用的沙箱：bwrap / sandbox-exec / none
    allow_net: bool

    @property
    def tag(self) -> str:
        return {"bwrap": "沙箱·bwrap", "sandbox-exec": "沙箱·seatbelt", "none": "无沙箱"}[self.kind if self.mode == "auto" else "none"]


def prepare_exec(ctx: ToolCtx, cmd: str, cwd_arg: str | None = None) -> Prepared:
    allow_net = bool(ctx.cfg.get("security", "allow_network_commands", default=False))
    cwd = ctx.guard.ws
    if cwd_arg:
        cwd = ctx.guard.resolve(cwd_arg)
        if not cwd.is_dir():
            raise ValueError("cwd 不是目录")
    home = ctx.journal.dir / "cmdhome"
    home.mkdir(parents=True, exist_ok=True)
    mode = ctx.cfg.get("security", "command_sandbox", default="auto")
    argv = _shell_argv(cmd)
    kind = "none"
    if mode == "auto":
        argv, kind = sec.wrap_in_sandbox(argv, ctx.guard.ws, allow_net, home)
    env = sec.clean_env(str(home))
    kw: dict[str, Any] = {}
    if sec.IS_WIN:
        kw["creationflags"] = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return Prepared(argv, env, kw, cwd, mode, kind, allow_net)


def kill_tree(proc: Any) -> None:
    """杀掉进程及其子进程（Windows 用 taskkill /T，其他系统杀进程组）。"""
    try:
        if sec.IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def run_command(ctx: ToolCtx, a: dict) -> ToolResult:
    cmd = a["command"]
    why = sec.hard_deny_reason(cmd)
    if why:
        return err(f"命令被安全规则拒绝（{why}）。")
    if a.get("background"):
        from . import jobs
        return await jobs.start_job(ctx, cmd, a.get("cwd"))
    timeout = max(1, min(int(a.get("timeout") or ctx.cfg.get("security", "command_timeout_s", default=120)), 3600))
    try:
        pr = prepare_exec(ctx, cmd, a.get("cwd"))
    except ValueError as e:
        return err(str(e))
    allow_net, mode, kind = pr.allow_net, pr.mode, pr.kind
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(*pr.argv, cwd=str(pr.cwd), env=pr.env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **pr.kw)
    except OSError as e:
        return err(f"无法启动命令: {e}")
    buf = bytearray()
    cap = 400_000

    async def pump() -> None:
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                return
            if len(buf) < cap:
                buf.extend(chunk[: cap - len(buf)])

    def kill() -> None:
        kill_tree(proc)

    task = asyncio.create_task(pump())
    timed_out = cancelled = False
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if done:
                break
            if time.time() - t0 > timeout:
                timed_out = True
                break
            if ctx.cancel is not None and ctx.cancel.is_set():
                cancelled = True
                break
        if timed_out or cancelled:
            kill()
            task.cancel()
        else:
            await proc.wait()
    finally:
        if proc.returncode is None:
            kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        if not task.done():
            task.cancel()
    text = buf.decode("utf-8", "replace") if buf else ""
    if sec.IS_WIN and "�" in text:
        text = bytes(buf).decode("gb18030", "replace")
    # 落盘开启时不在这里预截断：完整输出交给循环落盘（上限见 outputs.MAX_ITEM_CHARS），模型看到的头尾由循环统一裁剪；
    # 关闭时保持旧行为。命令本身的输出缓冲另有上限，不会无限增长。
    text = text.strip()
    if ctx.outputs is None or not ctx.cfg.get("agent", "offload_outputs", default=True):
        text = clip(text, 20000, "输出过长")
    dur = time.time() - t0
    tag = {"bwrap": "沙箱·bwrap", "sandbox-exec": "沙箱·seatbelt", "none": "无沙箱"}[kind if mode == "auto" else "none"]
    if timed_out:
        return ToolResult(f"{text}\n[命令超时（{timeout}s）已终止]", error=True, meta={"sandbox": tag})
    if cancelled:
        return ToolResult(f"{text}\n[已取消]", error=True, meta={"sandbox": tag})
    code = proc.returncode
    body = (text or "(无输出)") + (f"\n[退出码 {code}]" if code else "")
    if kind == "bwrap" and not allow_net and code and re.search(r"resolve|network|connect|unreachable", text, re.I):
        body += "\n[提示: 沙箱默认断网。如确需联网命令，请用户在 设置→安全 中开启“允许命令联网”]"
    return ToolResult(body, error=bool(code), meta={"sandbox": tag, "duration_s": round(dur, 1)})


async def command_preview(ctx: ToolCtx, a: dict) -> dict | None:
    info = sec.sandbox_info()
    net = ctx.cfg.get("security", "allow_network_commands", default=False)
    mode = ctx.cfg.get("security", "command_sandbox", default="auto")
    sb = f"沙箱: {info['kind']}（{'可联网' if net else '断网'}）" if info["available"] and mode == "auto" else "⚠ 无沙箱：命令将以你的用户权限直接运行"
    bg = "\n方式: 后台运行（不等待结束，可用 job_output 查看、job_stop 终止）" if a.get("background") else ""
    return {"kind": "command", "text": f"{a.get('command', '')}\n\n工作目录: {a.get('cwd') or '.'}{bg}\n{sb}"}


register(Tool(
    "run_command", "在工作区内执行 shell 命令（Windows 用 PowerShell，其他系统用 bash）。用于运行测试、构建、git、脚本等。"
                   "有超时与输出截断；不会继承你的环境变量/密钥。读文件、改文件请用专用工具而不是 cat/sed。"
                   "长时间运行的进程（监视测试、开发服务器、大型构建）设 background=true：立即返回任务 id，之后用 job_output 看输出。",
    obj({"command": P("string", "要执行的命令"), "cwd": P("string", "工作目录（相对工作区），默认工作区根"),
         "timeout": P("integer", "超时秒数（前台命令）"), "background": P("boolean", "后台运行，立即返回任务 id")}, ["command"]),
    run_command, "exec", title=lambda a: "$ " + (a.get("command", "")[:80]), preview=command_preview, path_args=("cwd",)))


# ------------------------------------------------------------------ todo_write / ask_user
async def todo_write(ctx: ToolCtx, a: dict) -> ToolResult:
    items = []
    for it in a["items"]:
        if isinstance(it, str):
            it = {"text": it, "status": "pending"}
        if not isinstance(it, dict):
            continue
        st = it.get("status", "pending")
        items.append({"text": str(it.get("text", ""))[:200], "status": st if st in ("pending", "in_progress", "done") else "pending"})
    ctx.session.todos = items[:30]
    if ctx.emit:
        ctx.emit("todo_update", {"items": ctx.session.todos})
    done = sum(1 for i in items if i["status"] == "done")
    return ToolResult(f"任务清单已更新（{done}/{len(items)} 完成）")


register(Tool("todo_write", "维护多步骤任务的清单。开始复杂任务前先列出步骤，推进时更新状态（pending/in_progress/done）。每次传入完整清单。",
              obj({"items": {"type": "array", "description": "完整任务清单",
                             "items": obj({"text": P("string", "任务描述"), "status": P("string", "状态", enum=["pending", "in_progress", "done"])}, ["text"])}}, ["items"]),
              todo_write, "meta", title=lambda a: "更新任务清单"))


async def ask_user(ctx: ToolCtx, a: dict) -> ToolResult:
    if ctx.ask is None:
        return err("当前无法向用户提问")
    opts = [str(o) for o in (a.get("options") or [])][:6]
    ans = await ctx.ask(a["question"], opts)
    return ToolResult(f"用户回答: {ans}")


register(Tool("ask_user", "遇到无法自行判断的关键歧义时，向用户提问（可附 2-6 个选项）。不要为琐事提问。",
              obj({"question": P("string", "问题"), "options": {"type": "array", "items": {"type": "string"}, "description": "可选答案"}}, ["question"]),
              ask_user, "meta", title=lambda a: "向你提问"))
