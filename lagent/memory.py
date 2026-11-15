"""外置记忆库：SQLite 单文件（data/memory.db），与模型/会话完全解耦，可导出/导入/手工编辑。

检索：本地词法打分（拉丁词 + 中日韩 bigram，兼顾标签/置顶/新近/使用频次），不依赖向量模型，开箱即用。
安全：agent 在“受污染上下文”里写入的记忆标记为未信任(trusted=0)，需用户在记忆面板批准后才会被自动召回——
防止网页里的提示注入通过“记忆投毒”长期影响 agent。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

KINDS = ("fact", "preference", "project", "episode")
_CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


def tokens(text: str) -> list[str]:
    t = text.lower()
    out = _WORD.findall(t)
    run: list[str] = []
    for ch in t + " ":
        if _CJK.match(ch):
            run.append(ch)
        else:
            if len(run) == 1:
                out.append(run[0])
            elif len(run) >= 2:
                out += ["".join(run[i:i + 2]) for i in range(len(run) - 1)]
            run = []
    return out


class Memory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS memories(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', tags TEXT NOT NULL DEFAULT '[]',
                scope TEXT NOT NULL DEFAULT 'global', workspace TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'user',
                trusted INTEGER NOT NULL DEFAULT 1, pinned INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0)""")
            self.db.execute("PRAGMA synchronous=NORMAL")       # WAL 模式下仍可抵御应用崩溃；省掉每次提交的整盘 fsync
            self._migrate()
            self.db.commit()
            self.fts = self._init_fts()

    def _migrate(self) -> None:
        """norm = 规范化文本（去重用）。老库自动补列并回填；之后 add() 的去重是一次索引查询，而不是全表扫描。"""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
        if "norm" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN norm TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS memories_norm ON memories(scope, workspace, norm)")
        todo = self.db.execute("SELECT id,text FROM memories WHERE norm=''").fetchall()
        if todo:
            self.db.executemany("UPDATE memories SET norm=? WHERE id=?", [(self._norm(r["text"]), r["id"]) for r in todo])

    # ------------------------------------------------------------ 全文索引（FTS5，可缺省）
    # 存的是 tokens() 的结果（拉丁词 + 中日韩 bigram，空格分隔），所以中文双字词也能命中；
    # 它只负责“快速圈出候选”，最终排序仍由 _score 决定。SQLite 不带 FTS5 时 self.fts=False，自动走全表路径。
    def _init_fts(self) -> bool:
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(id UNINDEXED, toks, tokenize=\"unicode61 tokenchars '_'\")")
            n_idx = self.db.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0]
            n_all = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if n_idx != n_all:                       # 首次升级、或手工改过数据库：整体重建
                self.db.execute("DELETE FROM mem_fts")
                for r in self.db.execute("SELECT id,text,tags FROM memories").fetchall():
                    self._fts_put(r["id"], r["text"], json.loads(r["tags"]))
            self.db.commit()
            return True
        except sqlite3.Error:
            return False

    def _fts_put(self, mid: str, text: str, tags: list[str]) -> None:
        """写入/刷新一条的索引（调用方负责确认 self.fts 为真）。"""
        self.db.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
        self.db.execute("INSERT INTO mem_fts(id,toks) VALUES(?,?)", (mid, " ".join(tokens(text + " " + " ".join(tags)))))

    def _fts_drop(self, mid: str) -> None:
        if self.fts:
            self.db.execute("DELETE FROM mem_fts WHERE id=?", (mid,))

    def _candidates(self, qt: set[str], limit: int = 300) -> list[str] | None:
        """返回按 bm25 排序的候选 id；索引不可用或查询无有效词时返回 None（调用方回退全表）。"""
        if not self.fts or not qt:
            return None
        q = " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(qt)[:64])
        try:
            with self._lock:
                return [r[0] for r in self.db.execute("SELECT id FROM mem_fts WHERE mem_fts MATCH ? ORDER BY bm25(mem_fts) LIMIT ?", (q, limit))]
        except sqlite3.Error:
            return None

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "text": r["text"], "kind": r["kind"], "tags": json.loads(r["tags"]), "scope": r["scope"],
                "workspace": r["workspace"], "source": r["source"], "trusted": bool(r["trusted"]), "pinned": bool(r["pinned"]),
                "created": r["created"], "updated": r["updated"], "uses": r["uses"]}

    @staticmethod
    def _clean_tags(tags: Any) -> list[str]:
        if isinstance(tags, str):
            tags = re.split(r"[,，\s]+", tags)
        return [str(t).strip()[:30] for t in (tags or []) if str(t).strip()][:10]

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\W+", "", s.lower())

    # ------------------------------------------------------------ 写
    def add(self, text: str, *, kind: str = "fact", tags: Any = None, scope: str = "global", workspace: str = "",
            source: str = "user", trusted: bool = True, pinned: bool = False, _commit: bool = True) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("记忆内容为空")
        if len(text) > 2000:
            raise ValueError("单条记忆最多 2000 字符，请拆分或精简")
        kind = kind if kind in KINDS else "fact"
        scope = scope if scope in ("global", "workspace") else "global"
        ws = workspace if scope == "workspace" else ""
        now = time.time()
        with self._lock:
            n = self._norm(text)
            dup = self.db.execute("SELECT id FROM memories WHERE scope=? AND workspace=? AND norm=? LIMIT 1", (scope, ws, n)).fetchone() if n else None
            if dup:                                    # 完全重复：只刷新
                self.db.execute("UPDATE memories SET updated=?, trusted=MAX(trusted,?), pinned=MAX(pinned,?) WHERE id=?",
                                (now, int(trusted), int(pinned), dup["id"]))
                if _commit:
                    self.db.commit()
                return self.get(dup["id"])  # type: ignore[return-value]
            mid = uuid.uuid4().hex[:12]
            tag_list = self._clean_tags(tags)
            self.db.execute("INSERT INTO memories(id,text,kind,tags,scope,workspace,source,trusted,pinned,created,updated,norm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (mid, text, kind, json.dumps(tag_list, ensure_ascii=False), scope, ws, source, int(trusted), int(pinned), now, now, n))
            if self.fts:
                self._fts_put(mid, text, tag_list)
            if _commit:
                self.db.commit()
            return self.get(mid)  # type: ignore[return-value]

    def update(self, mid: str, **f: Any) -> dict:
        with self._lock:
            cur = self.get(mid)
            if not cur:
                raise KeyError("记忆不存在")
            sets, vals = [], []
            if "text" in f:
                t = str(f["text"]).strip()
                if not t or len(t) > 2000:
                    raise ValueError("记忆内容为空或超过 2000 字符")
                sets.append("text=?"), vals.append(t)
                sets.append("norm=?"), vals.append(self._norm(t))
            if "kind" in f and f["kind"] in KINDS:
                sets.append("kind=?"), vals.append(f["kind"])
            if "tags" in f:
                sets.append("tags=?"), vals.append(json.dumps(self._clean_tags(f["tags"]), ensure_ascii=False))
            if "pinned" in f:
                sets.append("pinned=?"), vals.append(int(bool(f["pinned"])))
            if "trusted" in f:
                sets.append("trusted=?"), vals.append(int(bool(f["trusted"])))
            if "scope" in f and f["scope"] in ("global", "workspace"):
                sets.append("scope=?"), vals.append(f["scope"])
                if f["scope"] == "global":
                    sets.append("workspace=''")
                elif f.get("workspace"):
                    sets.append("workspace=?"), vals.append(f["workspace"])
            if sets:
                sets.append("updated=?"), vals.append(time.time())
                self.db.execute(f"UPDATE memories SET {','.join(sets)} WHERE id=?", (*vals, mid))
                if self.fts and ("text" in f or "tags" in f):
                    row = self.db.execute("SELECT text,tags FROM memories WHERE id=?", (mid,)).fetchone()
                    self._fts_put(mid, row["text"], json.loads(row["tags"]))
                self.db.commit()
            return self.get(mid)  # type: ignore[return-value]

    def delete(self, mid: str) -> bool:
        with self._lock:
            c = self.db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount
            self._fts_drop(mid)
            self.db.commit()
            return bool(c)

    def approve(self, mid: str) -> dict:
        return self.update(mid, trusted=True)

    def touch(self, ids: list[str]) -> None:
        with self._lock:
            now = time.time()
            self.db.executemany("UPDATE memories SET uses=uses+1,last_used=? WHERE id=?", [(now, i) for i in ids])
            self.db.commit()

    # ------------------------------------------------------------ 读
    def get(self, mid: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            return self._row(r) if r else None

    def list(self, *, q: str = "", kind: str = "", scope: str = "", pending: bool = False, workspace: str = "",
             limit: int = 500) -> tuple[list[dict], int]:
        with self._lock:
            where, args = ["1=1"], []
            if kind:
                where.append("kind=?"), args.append(kind)
            if scope:
                where.append("scope=?"), args.append(scope)
            where.append("trusted=0" if pending else "trusted=1")
            rows = [self._row(r) for r in self.db.execute(f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY pinned DESC, updated DESC", args)]
        if q.strip():
            qt = set(tokens(q))
            scored = []
            for m in rows:
                s = self._score(qt, m)
                if s > 0 or q.lower() in m["text"].lower():
                    scored.append((s, m))
            scored.sort(key=lambda x: -x[0])
            rows = [m for _s, m in scored]
        return rows[:limit], len(rows)

    @staticmethod
    def _score(qt: set[str], m: dict) -> float:
        if not qt:
            return 0.0
        doc = tokens(m["text"] + " " + " ".join(m["tags"]) * 2)
        if not doc:
            return 0.0
        ds = set(doc)
        hit = qt & ds
        if not hit:
            return 0.0
        # 近似 BM25：命中词越稀有（越长）权重越高，文档越长略有惩罚
        s = sum(1.0 + 0.15 * min(len(t), 8) for t in hit) / math.sqrt(1 + len(doc) / 40)
        s *= 1 + 0.6 * len(hit) / len(qt)
        age_days = (time.time() - m["updated"]) / 86400
        s *= 1 + 0.25 * math.exp(-age_days / 60)
        s *= 1 + 0.05 * min(m["uses"], 10)
        if m["pinned"]:
            s *= 1.5
        return s

    def search(self, q: str, k: int = 6, workspace: str = "", kind: str = "") -> list[dict]:
        """只返回可信记忆；全局 + 当前工作区。先用 FTS5 圈出候选再打分；无索引时回退全表。"""
        qt = set(tokens(q))
        cand = self._candidates(qt)
        if cand is not None:
            rows = self._trusted_by_ids(cand, kind)
        else:
            rows, _ = self.list(kind=kind, limit=5000)
        sc = []
        for m in rows:
            if m["scope"] == "workspace" and m["workspace"] != workspace:
                continue
            s = self._score(qt, m)
            if s > 0:
                sc.append((s, m))
        sc.sort(key=lambda x: -x[0])
        return [m for _s, m in sc[:k]]

    def _trusted_by_ids(self, ids: list[str], kind: str = "") -> list[dict]:
        if not ids:
            return []
        with self._lock:
            ph = ",".join("?" * len(ids))
            sql = f"SELECT * FROM memories WHERE trusted=1 AND id IN ({ph})" + (" AND kind=?" if kind else "")
            return [self._row(r) for r in self.db.execute(sql, (*ids, *([kind] if kind else [])))]

    def _pinned(self, workspace: str, limit: int = 8) -> list[dict]:
        with self._lock:
            rs = self.db.execute("SELECT * FROM memories WHERE trusted=1 AND pinned=1 AND (scope='global' OR workspace=?) "
                                 "ORDER BY updated DESC LIMIT ?", (workspace, limit)).fetchall()
            return [self._row(r) for r in rs]

    def recall_block(self, query: str, workspace: str, k: int = 6, max_chars: int = 1800) -> tuple[str, list[str]]:
        """生成注入系统提示的记忆块 + 命中的 id。置顶记忆总是包含。"""
        pinned = self._pinned(workspace)
        hits = self.search(query, k, workspace) if k > 0 else []
        seen, picked = set(), []
        for m in pinned + hits:
            if m["id"] not in seen:
                seen.add(m["id"])
                picked.append(m)
        lines, used, ids = [], 0, []
        for m in picked:
            ln = f"- ({m['kind']}) {m['text']}"
            if used + len(ln) > max_chars:
                break
            lines.append(ln)
            used += len(ln)
            ids.append(m["id"])
        if not lines:
            return "", []
        return "\n".join(lines), ids

    # ------------------------------------------------------------ 导入导出
    def export(self) -> dict:
        with self._lock:
            rows = [self._row(r) for r in self.db.execute("SELECT * FROM memories ORDER BY created")]
        return {"version": 1, "exported": time.time(), "items": rows}

    def import_items(self, items: list[dict]) -> int:
        """批量导入：整批一个事务（几千条也是秒级）。任何一条非法就跳过那一条，不影响其余。"""
        n = 0
        with self._lock:
            try:
                for it in items[:5000]:
                    try:
                        self.add(it["text"], kind=it.get("kind", "fact"), tags=it.get("tags"), scope=it.get("scope", "global"),
                                 workspace=it.get("workspace", ""), source="user", trusted=False if it.get("source") == "agent" else True,
                                 pinned=bool(it.get("pinned")), _commit=False)
                        n += 1
                    except (ValueError, KeyError, TypeError):
                        continue
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return n

    def count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
