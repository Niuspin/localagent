"""外置记忆库：SQLite 单文件（data/memory.db），与模型/会话完全解耦，可导出/导入/手工编辑。

检索：本地词法打分（拉丁词 + 中日韩 bigram，兼顾标签/置顶/新近/使用频次），不依赖向量模型，开箱即用。
安全：agent 在“受污染上下文”里写入的记忆标记为未信任(trusted=0)，需用户在记忆面板批准后才会被自动召回——
防止网页里的提示注入通过“记忆投毒”长期影响 agent。
"""
from __future__ import annotations

from array import array
import hashlib
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


def local_embed(text: str, dim: int = 128) -> list[float]:
    """零依赖确定性局部特征哈希向量（支持中英混合词元与字符 3-gram，用于离线稠密检索回退）。"""
    vec = [0.0] * dim
    feats = tokens(text)
    s = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if len(s) >= 3:
        feats = feats + [s[i:i + 3] for i in range(min(400, len(s) - 2))]
    for f in feats:
        d = hashlib.blake2b(f.encode("utf-8"), digest_size=4).digest()
        h = int.from_bytes(d, "little")
        idx = h % dim
        sign = 1.0 if ((h >> 8) & 1) else -1.0
        weight = 1.0 + 0.15 * min(len(f), 8)
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        inv = 1.0 / norm
        return [v * inv for v in vec]
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if (na > 0 and nb > 0) else 0.0


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
        if "supersedes_id" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN supersedes_id TEXT NOT NULL DEFAULT ''")
        if "superseded_by" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS memories_norm ON memories(scope, workspace, norm)")
        todo = self.db.execute("SELECT id,text FROM memories WHERE norm=''").fetchall()
        if todo:
            self.db.executemany("UPDATE memories SET norm=? WHERE id=?", [(self._norm(r["text"]), r["id"]) for r in todo])
        self.db.execute("""CREATE TABLE IF NOT EXISTS mem_vectors(
            id TEXT PRIMARY KEY, dim INTEGER NOT NULL, model TEXT NOT NULL DEFAULT 'local-hash',
            vec BLOB NOT NULL)""")
        vec_missing = self.db.execute(
            "SELECT m.id, m.text, m.tags FROM memories m LEFT JOIN mem_vectors v ON m.id=v.id WHERE v.id IS NULL"
        ).fetchall()
        if vec_missing:
            rows_v = []
            for r in vec_missing:
                blob = array("f", local_embed(r["text"] + " " + " ".join(json.loads(r["tags"])))).tobytes()
                rows_v.append((r["id"], 128, "local-hash", blob))
            self.db.executemany("INSERT OR REPLACE INTO mem_vectors(id,dim,model,vec) VALUES(?,?,?,?)", rows_v)

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

    def _vec_put(self, mid: str, text: str, tags: list[str], vec: list[float] | None = None, model: str = "local-hash") -> None:
        v = vec if vec else local_embed(text + " " + " ".join(tags))
        blob = array("f", [float(x) for x in v]).tobytes()
        self.db.execute("INSERT OR REPLACE INTO mem_vectors(id,dim,model,vec) VALUES(?,?,?,?)", (mid, len(v), model, blob))

    def set_vector(self, mid: str, vec: list[float], model: str = "embedding") -> None:
        if not vec:
            return
        with self._lock:
            blob = array("f", [float(x) for x in vec]).tobytes()
            self.db.execute("INSERT OR REPLACE INTO mem_vectors(id,dim,model,vec) VALUES(?,?,?,?)", (mid, len(vec), model, blob))
            self.db.commit()

    def _fts_drop(self, mid: str) -> None:
        if self.fts:
            self.db.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
        self.db.execute("DELETE FROM mem_vectors WHERE id=?", (mid,))

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
        keys = r.keys()
        return {"id": r["id"], "text": r["text"], "kind": r["kind"], "tags": json.loads(r["tags"]), "scope": r["scope"],
                "workspace": r["workspace"], "source": r["source"], "trusted": bool(r["trusted"]), "pinned": bool(r["pinned"]),
                "created": r["created"], "updated": r["updated"], "uses": r["uses"],
                "supersedes_id": r["supersedes_id"] if "supersedes_id" in keys else "",
                "superseded_by": r["superseded_by"] if "superseded_by" in keys else ""}

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
            supersedes = ""
            if trusted and kind in ("fact", "preference", "project"):
                qt_new = set(tokens(text))
                if len(qt_new) >= 3:
                    candidates = self.db.execute(
                        "SELECT id, text FROM memories WHERE scope=? AND workspace=? AND kind=? AND trusted=1 AND superseded_by='' ORDER BY updated DESC LIMIT 80",
                        (scope, ws, kind),
                    ).fetchall()
                    for cand in candidates:
                        qt_old = set(tokens(cand["text"]))
                        if not qt_old:
                            continue
                        inter = len(qt_new & qt_old)
                        min_sz = max(1, min(len(qt_new), len(qt_old)))
                        overlap = inter / min_sz
                        # 高主题词重叠且内容不同（覆盖更新旧偏好/事实，如「使用深色主题」替代「使用浅色主题」）
                        if overlap >= 0.55 and inter >= 2 and self._norm(cand["text"]) != n:
                            supersedes = cand["id"]
                            self.db.execute("UPDATE memories SET superseded_by=? WHERE id=?", (mid, supersedes))
                            break
            self.db.execute(
                "INSERT INTO memories(id,text,kind,tags,scope,workspace,source,trusted,pinned,created,updated,norm,supersedes_id,superseded_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, text, kind, json.dumps(tag_list, ensure_ascii=False), scope, ws, source, int(trusted), int(pinned), now, now, n, supersedes, ""),
            )
            if self.fts:
                self._fts_put(mid, text, tag_list)
            self._vec_put(mid, text, tag_list)
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
                if "text" in f or "tags" in f:
                    row = self.db.execute("SELECT text,tags FROM memories WHERE id=?", (mid,)).fetchone()
                    self._vec_put(mid, row["text"], json.loads(row["tags"]))
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
            where.append("superseded_by=''")
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

    def _vec_candidates(self, qvec: list[float], dim: int, limit: int = 100, *,
                        workspace: str = "", kind: str = "", model_filter: str = "") -> dict[str, float]:
        if not qvec:
            return {}
        with self._lock:
            sql = ("SELECT m.id, v.vec FROM mem_vectors v JOIN memories m ON v.id=m.id "
                   "WHERE m.trusted=1 AND m.superseded_by='' AND v.dim=? AND (m.scope='global' OR m.workspace=?)")
            args: list[Any] = [dim, workspace]
            if kind:
                sql += " AND m.kind=?"
                args.append(kind)
            if model_filter:
                sql += " AND v.model=?"
                args.append(model_filter)
            rows = self.db.execute(sql + " LIMIT 2000", args).fetchall()
        sims: dict[str, float] = {}
        for r in rows:
            a = array("f")
            a.frombytes(r["vec"])
            sim = _cosine(qvec, list(a))
            if sim > 0.20:
                sims[r["id"]] = sim
        if len(sims) > limit:
            top = sorted(sims.items(), key=lambda x: -x[1])[:limit]
            return dict(top)
        return sims

    def search(self, q: str, k: int = 6, workspace: str = "", kind: str = "",
               query_vec: list[float] | None = None) -> list[dict]:
        """混合检索（FTS5/BM25 词法 + 稠密/哈希余弦向量 + RRF 融合）：只返回可信记忆。"""
        qt = set(tokens(q))
        cand = self._candidates(qt)
        local_q = local_embed(q) if q.strip() else []
        vec_sims = self._vec_candidates(
            query_vec if query_vec else local_q,
            len(query_vec) if query_vec else 128,
            limit=80,
            workspace=workspace,
            kind=kind,
            model_filter="" if query_vec else "local-hash",
        )
        all_ids: list[str] = []
        if cand is not None:
            seen_c = set(cand)
            all_ids = list(cand)
            for vid, sim in vec_sims.items():
                if vid not in seen_c and sim >= (0.45 if query_vec else 0.38):
                    seen_c.add(vid)
                    all_ids.append(vid)
            rows = self._trusted_by_ids(all_ids, kind)
        else:
            rows, _ = self.list(kind=kind, limit=5000)

        lex_pairs: list[tuple[float, dict]] = []
        vec_pairs: list[tuple[float, dict]] = []
        row_by_id: dict[str, dict] = {}
        min_cos = 0.45 if query_vec else 0.38
        for m in rows:
            if m["scope"] == "workspace" and m["workspace"] != workspace:
                continue
            row_by_id[m["id"]] = m
            ls = self._score(qt, m)
            if ls > 0:
                lex_pairs.append((ls, m))
            vs = vec_sims.get(m["id"], 0.0)
            if vs >= min_cos:
                vec_pairs.append((vs, m))

        lex_pairs.sort(key=lambda x: -x[0])
        vec_pairs.sort(key=lambda x: -x[0])
        rrf: dict[str, float] = {}
        for rank, (ls, m) in enumerate(lex_pairs, 1):
            rrf[m["id"]] = rrf.get(m["id"], 0.0) + (1.0 / (60.0 + rank)) + min(ls, 5.0) * 0.005
        for rank, (vs, m) in enumerate(vec_pairs, 1):
            rrf[m["id"]] = rrf.get(m["id"], 0.0) + (0.85 / (60.0 + rank)) + vs * 0.01
        for mid, base in list(rrf.items()):
            m = row_by_id[mid]
            if m.get("pinned"):
                rrf[mid] = base * 1.25
        ranked = sorted(rrf.items(), key=lambda x: -x[1])
        return [row_by_id[mid] for mid, _ in ranked[:k]]

    def _trusted_by_ids(self, ids: list[str], kind: str = "") -> list[dict]:
        if not ids:
            return []
        with self._lock:
            ph = ",".join("?" * len(ids))
            sql = f"SELECT * FROM memories WHERE trusted=1 AND superseded_by='' AND id IN ({ph})" + (" AND kind=?" if kind else "")
            return [self._row(r) for r in self.db.execute(sql, (*ids, *([kind] if kind else [])))]

    def _pinned(self, workspace: str, limit: int = 8) -> list[dict]:
        with self._lock:
            rs = self.db.execute("SELECT * FROM memories WHERE trusted=1 AND superseded_by='' AND pinned=1 AND (scope='global' OR workspace=?) "
                                 "ORDER BY updated DESC LIMIT ?", (workspace, limit)).fetchall()
            return [self._row(r) for r in rs]

    def recall_block(self, query: str, workspace: str, k: int = 6, max_chars: int = 1800,
                     query_vec: list[float] | None = None) -> tuple[str, list[str]]:
        """生成注入系统提示的记忆块 + 命中的 id。置顶记忆总是包含。"""
        pinned = self._pinned(workspace)
        hits = self.search(query, k, workspace, query_vec=query_vec) if k > 0 else []
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
