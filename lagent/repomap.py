"""工作区代码结构索引（RepoMap）：用零外部依赖的 AST + 多语言正则提取关键符号大纲，帮助小上下文模型快速定位模块。"""
from __future__ import annotations

import ast
import fnmatch
import os
import re
from pathlib import Path

from . import security as sec
from .tools.files import SKIP_DIRS

_TS_CACHE: dict[str, object] = {}


def _extract_treesitter(text: str, ext: str) -> list[str] | None:
    """可选使用 tree-sitter 解析多语言符号大纲；未安装对应语言包时返回 None 回退。"""
    lang_map = {
        ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".java": "java", ".cs": "c_sharp",
    }
    lang = lang_map.get(ext)
    if not lang:
        return None
    try:
        parser = _TS_CACHE.get(lang)
        if parser is False:
            return None
        if parser is None:
            try:
                from tree_sitter_language_pack import get_parser  # type: ignore
            except ImportError:
                from tree_sitter_languages import get_parser  # type: ignore
            parser = get_parser(lang)
            _TS_CACHE[lang] = parser
    except Exception:  # noqa: BLE001
        _TS_CACHE[lang] = False
        return None
    try:
        tree = parser.parse(text.encode("utf-8", "replace"))  # type: ignore[attr-defined]
        wanted = {
            "function_definition", "async_function_definition", "class_definition",
            "function_declaration", "method_definition", "class_declaration",
            "type_declaration", "struct_item", "enum_item", "trait_item", "impl_item",
            "function_item", "interface_declaration",
        }
        out: list[str] = []

        def walk(node, depth: int = 0) -> None:
            if len(out) >= 24 or depth > 3:
                return
            if node.type in wanted:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    nm = name_node.text.decode("utf-8", "replace")
                    line = node.start_point[0] + 1
                    kind = node.type.split("_")[0]
                    out.append(f"L{line} {kind} {nm}")
            for ch in node.children:
                walk(ch, depth + 1)

        walk(tree.root_node)
        return out if out else None
    except Exception:  # noqa: BLE001
        return None

_EXT_ORDER = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".java", ".cs"}
_JS_RX = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*)|(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)"
)
_GO_RX = re.compile(r"^\s*(?:func\s+(?:\([^)]+\)\s*)?([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*)\s+(?:struct|interface))\b")
_RS_RX = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)")


def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file() or gi.is_symlink():
        return []
    pats: list[str] = []
    try:
        for ln in gi.read_text("utf-8", "replace").splitlines()[:300]:
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith("!"):
                continue
            pats.append(s.rstrip("/"))
    except OSError:
        return []
    return pats


def _ignored(rel_posix: str, name: str, gi_pats: list[str]) -> bool:
    for p in gi_pats:
        if "/" in p:
            if fnmatch.fnmatch(rel_posix, p) or rel_posix.startswith(p + "/"):
                return True
        elif fnmatch.fnmatch(name, p):
            return True
    return False


def _extract_python_defs_and_refs(text: str) -> tuple[list[str], set[str], set[str]]:
    """提取 Python 符号行标签、定义符号名集合、以及外部引用标识符集合。"""
    labels: list[str] = []
    defs: set[str] = set()
    refs: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return labels, defs, refs
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defs.add(node.name)
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))][:8]
            for m in methods:
                defs.add(m)
            mstr = f" ({', '.join(methods)})" if methods else ""
            labels.append(f"L{node.lineno} class {node.name}{mstr}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.add(node.name)
            labels.append(f"L{node.lineno} def {node.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and len(node.id) >= 3:
            refs.add(node.id)
        elif isinstance(node, ast.Attribute) and len(node.attr) >= 3:
            refs.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for part in node.module.split("."):
                    if len(part) >= 3:
                        refs.add(part)
            for al in node.names:
                if al.name != "*" and len(al.name) >= 3:
                    refs.add(al.name)
    return labels[:24], defs, refs


def _extract_python(text: str) -> list[str]:
    return _extract_python_defs_and_refs(text)[0]


def _extract_sym_names_from_labels(labels: list[str]) -> set[str]:
    out: set[str] = set()
    for lb in labels:
        parts = lb.split(" ", 2)
        if len(parts) >= 2:
            raw = parts[-1].split("(")[0].strip()
            if len(raw) >= 3:
                out.add(raw)
    return out


_IDENT_RX = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def _rank_files_pagerank(
    items: list[dict[str, Any]],
    *,
    focus_files: set[str],
    iters: int = 12,
    damping: float = 0.85,
) -> list[dict[str, Any]]:
    """基于跨文件符号引用图运行轻量 PageRank，优先排序核心依赖节点与当前会话相关文件。"""
    n = len(items)
    if n <= 1:
        return items

    sym_owners: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        for s in it["defs"]:
            sym_owners.setdefault(s, []).append(idx)

    out_edges: list[dict[int, float]] = [{} for _ in range(n)]
    for i, it in enumerate(items):
        for r in it["refs"]:
            owners = sym_owners.get(r)
            if not owners or len(owners) > 6:  # 常见通用方法名（如 init/get/run）过多时不作为强耦合边
                continue
            w = 1.0 / len(owners)
            for j in owners:
                if j != i:
                    out_edges[i][j] = out_edges[i].get(j, 0.0) + w

    pers = [1.0 / n for _ in range(n)]
    if focus_files:
        boosted = [1.0 if it["rel"] in focus_files else 0.15 for it in items]
        tot_b = sum(boosted)
        pers = [b / tot_b for b in boosted]

    ranks = list(pers)
    for _ in range(iters):
        nxt = [(1.0 - damping) * pers[i] for i in range(n)]
        dangling = 0.0
        for i in range(n):
            tot_w = sum(out_edges[i].values())
            if tot_w <= 0:
                dangling += ranks[i]
            else:
                share = damping * ranks[i] / tot_w
                for j, w in out_edges[i].items():
                    nxt[j] += share * w
        if dangling > 0:
            for i in range(n):
                nxt[i] += damping * dangling * pers[i]
        ranks = nxt

    for i, it in enumerate(items):
        it["_rank"] = ranks[i]
    return sorted(items, key=lambda x: (-x["_rank"], x["rel"]))


def _extract_regex(text: str, ext: str) -> list[str]:
    rx = _GO_RX if ext == ".go" else _RS_RX if ext == ".rs" else _JS_RX
    out: list[str] = []
    for i, ln in enumerate(text.splitlines()[:600], 1):
        m = rx.match(ln)
        if not m:
            continue
        sym = next((g for g in m.groups() if g), None)
        if sym:
            out.append(f"L{i} {sym}")
            if len(out) >= 24:
                break
    return out


def build_repo_map(
    ws: Path,
    *,
    subdir: str = ".",
    max_files: int = 80,
    max_chars: int = 8000,
    guard: sec.PathGuard | None = None,
    focus_files: list[str] | set[str] | None = None,
) -> str:
    """生成工作区结构摘要（按跨文件引用图 PageRank 与关注文件排序，列出关键类/函数行号）。"""
    g = guard or sec.PathGuard(ws)
    start = g.resolve(subdir or ".", write=False)
    if not start.is_dir():
        return f"不是目录: {subdir}"
    gi_pats = _load_gitignore(g.ws)
    files: list[Path] = []
    for root_s, dirs, fns in os.walk(start, followlinks=False):
        rp = Path(root_s)
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            if d in SKIP_DIRS or d.startswith("."):
                continue
            dp = rp / d
            if dp.is_symlink():
                continue
            try:
                rel_d = g.rel(dp)
            except sec.SecurityError:
                continue
            if _ignored(rel_d, d, gi_pats):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for fn in sorted(fns):
            fp = rp / fn
            if fp.suffix.lower() not in _EXT_ORDER or fp.is_symlink():
                continue
            try:
                rel_f = g.rel(fp)
            except sec.SecurityError:
                continue
            if _ignored(rel_f, fn, gi_pats):
                continue
            files.append(fp)
            if len(files) >= max(max_files * 3, 240):
                break
        if len(files) >= max(max_files * 3, 240):
            break

    parsed_items: list[dict[str, Any]] = []
    for fp in files:
        try:
            if fp.stat().st_size > 512_000:
                continue
            txt = fp.read_text("utf-8", "replace")
            rel = g.rel(fp)
        except (OSError, sec.SecurityError):
            continue
        ext = fp.suffix.lower()
        syms = _extract_treesitter(txt, ext)
        defs: set[str] = set()
        refs: set[str] = set()
        if ext == ".py":
            py_syms, defs, refs = _extract_python_defs_and_refs(txt)
            if syms is None:
                syms = py_syms
            else:
                defs |= _extract_sym_names_from_labels(syms)
        else:
            if syms is None:
                syms = _extract_regex(txt, ext)
            defs = _extract_sym_names_from_labels(syms)
            refs = set(_IDENT_RX.findall(txt[:40_000]))
        parsed_items.append({
            "rel": rel,
            "syms": syms,
            "lines": txt.count("\n") + 1,
            "defs": defs,
            "refs": refs - defs,
        })

    focus_set = {str(x).replace("\\", "/").strip("./") for x in (focus_files or ()) if str(x).strip()}
    ranked = _rank_files_pagerank(parsed_items, focus_files=focus_set)

    lines: list[str] = []
    used = 0
    count = 0
    for it in ranked:
        if count >= max_files:
            break
        syms = it["syms"]
        entry = f"{it['rel']}" + (f": {' | '.join(syms)}" if syms else f" ({it['lines']}L)")
        if used + len(entry) + 1 > max_chars:
            lines.append("…[已达到符号地图字符上限]")
            break
        lines.append(entry)
        used += len(entry) + 1
        count += 1
    return "\n".join(lines) if lines else "（当前目录没有可索引的源码文件）"
