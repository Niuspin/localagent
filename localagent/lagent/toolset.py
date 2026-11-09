"""工具分级：核心工具常驻，低频工具按需加载。

为什么：本地小模型的上下文宝贵，且工具数量越多越容易选错。25 个工具的定义每轮约占 3.8K token；
把 Office 创建/编辑、删除/移动/复制、技能与记忆管理这些低频工具放到“扩展组”，只在用到时才加入。

规则（为保护 llama.cpp 的 KV 前缀缓存，已加载的组在会话内只增不减）：
 1. 用户消息/附件里出现相关关键词时自动加载（宁可多加载，也不漏掉）；
 2. 模型直接调用了某个未列出的扩展工具时，自动加载它所在的组；
 3. 模型也可以主动调用 load_tools 加载。
"""
from __future__ import annotations

import re

EXT_GROUPS: dict[str, tuple[str, ...]] = {
    "office": ("write_document", "write_spreadsheet", "write_presentation", "edit_office"),
    "fileops": ("delete_path", "move_path", "copy_path", "make_dir"),
    "extras": ("read_skill_file", "create_skill", "memory_forget"),
    "jobs": ("job_output", "job_stop"),
}

GROUP_DESC: dict[str, str] = {
    "office": "创建/修改 Word、Excel、PowerPoint、PDF 文档",
    "fileops": "删除、移动、复制文件，新建目录",
    "extras": "读取技能附带文件、创建技能、删除记忆",
    "jobs": "查看/终止后台命令（run_command background=true 启动）",
}

_OF: dict[str, str] = {t: g for g, ts in EXT_GROUPS.items() for t in ts}

_TRIGGERS: dict[str, re.Pattern[str]] = {
    "office": re.compile(
        r"\.(?:docx?|xlsx?|xlsm|pptx?|pdf)\b|\b(?:word|excel|powerpoint|pdf|pptx?|xlsx?|docx?)\b"
        r"|文档|表格|幻灯|演示文稿|报告|简历|合同|电子表格|工作簿", re.I),
    "fileops": re.compile(
        r"删除|删掉|移动|移到|复制|拷贝|重命名|改名|整理|归档|清理|新建(?:文件夹|目录)|创建(?:文件夹|目录)"
        r"|\b(?:delete|remove|rename|move|copy|mkdir|tidy|organi[sz]e|clean ?up)\b", re.I),
    "extras": re.compile(r"技能|\bskills?\b|忘[记掉]|\bforget\b", re.I),
    "jobs": re.compile(r"后台|开发服务器|\bdev server\b|\bnpm run dev\b|\bwatch(?:ing)? mode\b|持续运行|长时间运行|\bin the background\b", re.I),
}


def group_of(tool_name: str) -> str | None:
    """扩展工具所属的组；核心工具返回 None。"""
    return _OF.get(tool_name)


def visible(tool_name: str, loaded: set[str] | frozenset[str]) -> bool:
    g = _OF.get(tool_name)
    return g is None or g in loaded


def detect(text: str, files: list[str] | None = None, skills: list[str] | None = None) -> set[str]:
    """根据用户消息、附件名、显式选择的技能，推断需要预先加载的扩展组。"""
    blob = (text or "") + "\n" + "\n".join(files or [])
    out = {g for g, rx in _TRIGGERS.items() if rx.search(blob)}
    if skills:                       # 技能的做法通常要读写办公文件、整理文件
        out |= {"office", "fileops"}
    return out


def normalize(groups: object) -> set[str]:
    """把模型传来的 groups（可能是字符串/列表，可能含 all）规整成合法组名集合。"""
    if isinstance(groups, str):
        groups = re.split(r"[,，\s]+", groups)
    items = [str(x).strip().lower() for x in (groups or []) if str(x).strip()]
    if "all" in items:
        return set(EXT_GROUPS)
    return {g for g in items if g in EXT_GROUPS}


def hint() -> str:
    """写进系统提示的一句话（内容固定，不破坏前缀缓存）。"""
    return ("- 工具列表只含常用工具。需要办公文档（Word/Excel/PPT/PDF）、删除/移动/复制文件、技能与记忆管理、"
            "后台任务管理时，先调用 load_tools（groups: office / fileops / extras / jobs）再继续。")
