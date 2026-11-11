"""工具调用容错与度量：把“本地小模型常见的调用瑕疵”集中在一处处理，纯函数、不依赖模型和网络，可单独测试。

分四块：
 1. resolve_name   工具名纠正：大小写/连字符/命名空间前缀、常见别名（bash→run_command）、拼写接近的唯一候选
 2. normalize_args 参数规范化：驼峰→蛇形、常见同义参数名（file_path→path）、被多包了一层的 {"arguments": {...}}
 3. looks_like_failed_call  识别“整段回复就是一次没解析成功的函数调用”（用于一次性的 tool_choice=required 重试）
 4. RunStats       每次运行的计数器；运行结束写入审计日志并随 run_end 事件下发，用来量化“这个模型到底哪里不稳”

原则：只在**无歧义**时才改写，拿不准就原样交还给模型（带上更好的报错），绝不替模型“猜”一个有副作用的操作。
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field, fields
from typing import Any, Iterable

from .tools.base import repair_json

# ---------------------------------------------------------------- 工具名
TOOL_ALIASES: dict[str, str] = {
    "bash": "run_command", "shell": "run_command", "sh": "run_command", "terminal": "run_command", "exec": "run_command",
    "execute": "run_command", "run": "run_command", "command": "run_command", "cmd": "run_command", "powershell": "run_command",
    "cat": "read_file", "read": "read_file", "open": "read_file", "open_file": "read_file", "view": "read_file", "view_file": "read_file",
    "ls": "list_dir", "list": "list_dir", "list_files": "list_dir", "list_directory": "list_dir", "dir": "list_dir", "tree": "list_dir",
    "find": "glob", "find_files": "glob", "glob_files": "glob", "search_files": "glob",
    "rg": "grep", "search_content": "grep", "search_in_files": "grep", "grep_search": "grep",
    "edit": "edit_file", "replace": "edit_file", "str_replace": "edit_file", "patch_file": "edit_file", "modify_file": "edit_file",
    "write": "write_file", "create_file": "write_file", "save_file": "write_file", "overwrite_file": "write_file",
    "search": "web_search", "google": "web_search", "search_web": "web_search", "internet_search": "web_search",
    "fetch": "web_fetch", "fetch_url": "web_fetch", "browse": "web_fetch", "open_url": "web_fetch", "get_url": "web_fetch", "http_get": "web_fetch",
    "todo": "todo_write", "todos": "todo_write", "update_todos": "todo_write", "plan": "todo_write",
    "ask": "ask_user", "ask_question": "ask_user", "question": "ask_user",
    "remember": "memory_save", "save_memory": "memory_save", "recall": "memory_search", "search_memory": "memory_search",
    "rm": "delete_path", "delete": "delete_path", "delete_file": "delete_path", "remove": "delete_path",
    "mv": "move_path", "move": "move_path", "rename": "move_path", "cp": "copy_path", "copy": "copy_path", "mkdir": "make_dir",
}
_PREFIX = re.compile(r"^(?:functions?|tools?|default_api|api|assistant|call|multi_tool_use)[.:/]+", re.I)


def _snake(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s.strip())
    return re.sub(r"[\s.\-/:]+", "_", s).lower().strip("_")


def canon_name(name: str) -> str:
    return _snake(_PREFIX.sub("", (name or "").strip()))


def resolve_name(name: str, known: Iterable[str]) -> tuple[str | None, str]:
    """返回 (规范的工具名或 None, 方式)。方式: ""=原样 / "canon" / "alias" / "fuzzy"。"""
    known = list(known)
    if name in known:
        return name, ""
    c = canon_name(name)
    if c in known:
        return c, "canon"
    if c in TOOL_ALIASES and TOOL_ALIASES[c] in known:
        return TOOL_ALIASES[c], "alias"
    if c and sum(1 for k in known if k.startswith(c)) >= 2:
        return None, ""                       # 被截断的名字是多个工具的公共前缀（memory_s → save/search）：绝不猜，尤其不能猜成有副作用的那个
    close = difflib.get_close_matches(c, known, n=3, cutoff=0.7)       # 候选范围比入选门槛宽，用来发现“还有另一个差不多的”
    if close:
        ratio = lambda k: difflib.SequenceMatcher(None, c, k).ratio()  # noqa: E731
        if ratio(close[0]) >= 0.84 and all(ratio(close[0]) - ratio(o) >= 0.1 for o in close[1:]):
            return close[0], "fuzzy"
    return None, ""


def suggest(name: str, known: Iterable[str], n: int = 3) -> list[str]:
    """给“未知工具”报错用：列出最像的几个，帮模型自己改对。"""
    known = list(known)
    c = canon_name(name)
    out = [TOOL_ALIASES[c]] if TOOL_ALIASES.get(c) in known else []
    out += [x for x in difflib.get_close_matches(c, known, n=n, cutoff=0.5) if x not in out]
    return out[:n]


# ---------------------------------------------------------------- 参数
ARG_ALIASES: dict[str, str] = {
    **{k: "path" for k in ("file_path", "filepath", "file", "filename", "file_name", "fname", "dir", "directory", "folder", "target", "src", "source_path")},
    **{k: "command" for k in ("cmd", "script", "shell", "bash", "shell_command")},
    **{k: "content" for k in ("text", "data", "body", "contents", "file_content", "file_text")},
    **{k: "old_string" for k in ("old", "old_str", "old_text", "search", "find", "original", "old_content")},
    **{k: "new_string" for k in ("new", "new_str", "new_text", "replace", "replacement", "replace_with", "new_content")},
    **{k: "query" for k in ("q", "keyword", "keywords", "search_query", "term", "text_query")},
    **{k: "url" for k in ("link", "href", "uri", "address", "website")},
    **{k: "pattern" for k in ("glob", "regex", "expr", "expression", "match")},
    **{k: "question" for k in ("prompt", "ask")},
    "job_id": "id", "task_id": "id", "memory_id": "id",
    "from": "src", "to": "dst", "source": "src", "destination": "dst", "dest": "dst", "limit_lines": "limit", "max_lines": "limit", "start_line": "offset",
}
_WRAP_KEYS = ("arguments", "parameters", "params", "args", "input", "kwargs")


def normalize_args(props: dict, args: dict) -> tuple[dict, list[str]]:
    """把模型给的参数名对齐到工具真正的参数名。返回 (新参数, 修改说明列表)。props 为空（无声明参数）时原样返回。"""
    if not props or not isinstance(args, dict):
        return args, []
    fixes: list[str] = []
    cur = dict(args)
    # 多包了一层：{"arguments": {...}} / {"input": {...}}（该键本身不是工具参数时才解开）
    for w in _WRAP_KEYS:
        if len(cur) == 1 and w in cur and w not in props and isinstance(cur[w], dict):
            cur = dict(cur[w])
            fixes.append(f"解开 {w}")
            break
    out: dict[str, Any] = {}
    spare: dict[str, Any] = {}
    for k, v in cur.items():
        sk = _snake(str(k))
        if k in props:
            out[k] = v
        elif sk in props:
            out[sk] = v
            fixes.append(f"{k}→{sk}")
        else:
            spare[k] = v
    for k, v in spare.items():
        target = ARG_ALIASES.get(_snake(str(k)))
        if target and target in props and target not in out:
            out[target] = v
            fixes.append(f"{k}→{target}")
        # 对不上的参数：不带进去（coerce_args 本来也会忽略），避免把无关内容当成有效参数
    return out, fixes


def parse_args(raw: str) -> tuple[dict | None, bool]:
    """解析工具参数 JSON。返回 (字典或 None, 是否经过修复)。"""
    if not isinstance(raw, str) or not raw.strip():
        return {}, False
    try:
        v = json.loads(raw)
        return (v, False) if isinstance(v, dict) else (None, False)
    except ValueError:
        pass
    v = repair_json(raw)
    return (v, True) if isinstance(v, dict) else (None, False)


# ---------------------------------------------------------------- 失败的函数调用
_CALL_SHAPE = re.compile(r'<tool_call>|\[TOOL_CALLS\]|<\|?tool_call|"name"\s*:\s*"[A-Za-z_][\w.\-]*"\s*,\s*"(?:arguments|parameters)"', re.I)
_START = ("{", "[", "<tool_call", "[TOOL_CALLS]", "<|tool_call", "```json", "```")


def looks_like_failed_call(content: str) -> bool:
    """整段回复本身就是函数调用的样子，却没有被解析成调用。只有这种情况才值得用 tool_choice=required 重试一次。
    故意保守：正常回答里引用一段 JSON 不会触发（回复必须以调用格式开头）。"""
    t = (content or "").strip()
    return bool(t) and len(t) < 20000 and t.startswith(_START) and bool(_CALL_SHAPE.search(t))


# ---------------------------------------------------------------- 度量
@dataclass
class RunStats:
    steps: int = 0
    tool_calls: int = 0
    name_fixed: int = 0          # 工具名被纠正
    args_fixed: int = 0          # 参数名被对齐
    json_repaired: int = 0       # 参数 JSON 需要修复才能解析
    json_failed: int = 0         # 参数 JSON 修不好
    text_fallback: int = 0       # 调用是从文本里解析出来的（没走结构化 tool_calls）
    forced_retries: int = 0      # 触发了 tool_choice=required 重试
    unknown_tool: int = 0
    arg_errors: int = 0          # 参数缺失/类型不对
    precheck_blocked: int = 0    # 先读再改等预检拦下
    repeat_calls: int = 0        # 连续重复同一调用
    thinking_cut: int = 0        # 思考过长被截断
    extra: dict = field(default_factory=dict)

    def bump(self, name: str, n: int = 1) -> None:
        if name in {f.name for f in fields(self)} and name != "extra":
            setattr(self, name, getattr(self, name) + n)
        else:
            self.extra[name] = self.extra.get(name, 0) + n

    def nonzero(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "extra"}
        d.update(self.extra)
        return {k: v for k, v in d.items() if v}

    @property
    def friction(self) -> int:
        """“摩擦”总量：所有需要框架替模型兜底或模型自己出错的次数。评测里拿它横向比较不同模型。"""
        return sum(getattr(self, k) for k in ("name_fixed", "args_fixed", "json_repaired", "json_failed", "text_fallback", "forced_retries",
                                               "unknown_tool", "arg_errors", "repeat_calls"))
