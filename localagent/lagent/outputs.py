"""工具输出落盘：大输出完整保存在 data/outputs/<会话>/，上下文里只留头尾 + 句柄，需要时再按句柄取回。

为什么：
- 旧做法是把超长输出截成“头 70% + 尾 20%”，中间永久丢失；上下文紧张时旧输出被替换成占位，只能让模型“重新调用工具”
  ——对 web_fetch / run_command / 搜索这类不幂等或很贵的调用，重跑既慢，结果还可能不同。
- 落盘后，占位里写明句柄，模型用 read_tool_output 分段读取或按关键词查找，不必重跑原工具。

安全：
- data/ 对 Agent 的文件工具完全不可见，这里只通过“会话 ID + 句柄”取回，且句柄只能读自己会话的输出；
  句柄格式严格校验，不接受路径。
- 原输出的“不可信”标记随内容一起保存，取回时由循环重新用 <untrusted> 包裹，不会因为“落盘再读”而洗白污染。
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from pathlib import Path

from .journal import atomic_write

SID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OID_OK = re.compile(r"^o[a-f0-9]{8}$")

MIN_CHARS = 1500                    # 短于此的输出不落盘（直接留在上下文里就好）
MAX_ITEM_CHARS = 8 * 1024 * 1024    # 单条上限；更长的只存前面这么多
MAX_SESSION_BYTES = 64 * 1024 * 1024  # 单会话总量上限；超出时淘汰最旧的
READ_DEFAULT = 8000                 # 单次取回的默认/最大字符数
READ_MAX = 30000
MAX_HITS = 40                       # contains 查找最多返回的命中行数
HIT_CHARS = 300


class OutputStore:
    def __init__(self, directory: Path, max_session_bytes: int = MAX_SESSION_BYTES):
        self.dir = Path(directory)
        self.max_session_bytes = max_session_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 内部
    def _sdir(self, sid: str) -> Path | None:
        return self.dir / sid if SID_OK.match(sid or "") else None

    # ------------------------------------------------------------ 写
    def save(self, sid: str, text: str, *, tool: str = "", source: str = "", taint: str = "") -> str | None:
        """保存一条输出，返回句柄；会话 ID 非法或写盘失败时返回 None（调用方退回到旧的截断做法）。"""
        d = self._sdir(sid)
        if d is None:
            return None
        total = len(text)
        body = text[:MAX_ITEM_CHARS]
        with self._lock:
            try:
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
                oid = "o" + secrets.token_hex(4)
                while (d / f"{oid}.json").exists():
                    oid = "o" + secrets.token_hex(4)
                rec = {"tool": tool, "source": source, "taint": taint, "chars": total, "stored": len(body), "text": body}
                atomic_write(d / f"{oid}.json", json.dumps(rec, ensure_ascii=False).encode("utf-8"))
                self._evict(d, keep=oid)
                return oid
            except OSError:
                return None

    def _evict(self, d: Path, keep: str) -> None:
        files = sorted((p for p in d.glob("o*.json")), key=lambda p: p.stat().st_mtime)
        size = sum(p.stat().st_size for p in files)
        for p in files:
            if size <= self.max_session_bytes:
                break
            if p.stem == keep:
                continue
            try:
                n = p.stat().st_size
                p.unlink()
                size -= n
            except OSError:
                continue

    # ------------------------------------------------------------ 读
    def load(self, sid: str, oid: str) -> dict | None:
        d = self._sdir(sid)
        if d is None or not OID_OK.match(oid or ""):
            return None
        try:
            return json.loads((d / f"{oid}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def read(self, sid: str, oid: str, offset: int = 0, limit: int = READ_DEFAULT, contains: str = "") -> dict | None:
        """返回 {text, meta, total, start, end}；找不到返回 None。contains 非空时按行做不区分大小写的子串查找（不用正则，避免 ReDoS）。"""
        rec = self.load(sid, oid)
        if rec is None:
            return None
        text: str = rec.get("text", "")
        meta = {"tool": rec.get("tool", ""), "source": rec.get("source", ""), "taint": rec.get("taint", ""),
                "chars": rec.get("chars", len(text)), "stored": len(text)}
        if contains:
            needle = contains.lower()
            hits: list[str] = []
            for i, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{i}: {line[:HIT_CHARS]}")
                    if len(hits) >= MAX_HITS:
                        break
            return {"text": "\n".join(hits), "meta": meta, "total": len(text), "hits": len(hits), "capped": len(hits) >= MAX_HITS}
        offset = max(0, int(offset or 0))
        limit = max(200, min(int(limit or READ_DEFAULT), READ_MAX))
        chunk = text[offset:offset + limit]
        return {"text": chunk, "meta": meta, "total": len(text), "start": offset, "end": offset + len(chunk)}

    # ------------------------------------------------------------ 清理
    def drop(self, sid: str) -> None:
        d = self._sdir(sid)
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)

    def sweep(self, live_sids: set[str]) -> int:
        """删除已不存在的会话遗留的输出目录（启动时调用）。返回删除的目录数。"""
        n = 0
        if not self.dir.is_dir():
            return 0
        for d in self.dir.iterdir():
            if d.is_dir() and d.name not in live_sids:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n
