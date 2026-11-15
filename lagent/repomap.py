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


def _extract_python(text: str) -> list[str]:
    out: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))][:8]
            mstr = f" ({', '.join(methods)})" if methods else ""
            out.append(f"L{node.lineno} class {node.name}{mstr}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"L{node.lineno} def {node.name}")
    return out[:24]


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
) -> str:
    """生成工作区结构摘要（按文件列出关键类/函数行号），受字符上限控制。"""
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
            if len(files) >= max_files * 2:
                break
        if len(files) >= max_files * 2:
            break

    lines: list[str] = []
    used = 0
    count = 0
    for fp in files:
        if count >= max_files:
            break
        try:
            if fp.stat().st_size > 512_000:
                continue
            txt = fp.read_text("utf-8", "replace")
            rel = g.rel(fp)
        except (OSError, sec.SecurityError):
            continue
        ext = fp.suffix.lower()
        syms = _extract_treesitter(txt, ext)
        if syms is None:
            syms = _extract_python(txt) if ext == ".py" else _extract_regex(txt, ext)
        entry = f"{rel}" + (f": {' | '.join(syms)}" if syms else f" ({txt.count(chr(10)) + 1}L)")
        if used + len(entry) + 1 > max_chars:
            lines.append("…[已达到符号地图字符上限]")
            break
        lines.append(entry)
        used += len(entry) + 1
        count += 1
    return "\n".join(lines) if lines else "（当前目录没有可索引的源码文件）"
