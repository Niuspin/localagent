"""工具运行上下文：把配置、围栏、日志、会话状态打包给工具使用。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import Config
from ..journal import Journal
from ..security import AuditLog, PathGuard


@dataclass
class SessionState:
    """工具能读写的会话状态（污染标记、待办、已见域名等）。"""
    sid: str
    tainted: bool = False
    nonce: str = ""
    todos: list = field(default_factory=list)
    user_domains: set = field(default_factory=set)      # 出现在用户消息 / 搜索结果中的域名（访问它们不算可疑）
    allow_keys: set = field(default_factory=set)        # “本会话允许”
    loaded_skills: set = field(default_factory=set)
    tool_groups: set = field(default_factory=set)       # 已加载的扩展工具组（见 toolset.py；会话内只增不减）
    seen: dict = field(default_factory=dict)            # 相对路径 → 内容 sha256：本会话读过/写过的文本文件（先读再改）


@dataclass
class ToolCtx:
    cfg: Config
    guard: PathGuard
    journal: Journal
    session: SessionState
    audit: AuditLog | None = None
    vision: bool = False
    memory: Any = None
    skills: Any = None
    outputs: Any = None      # OutputStore：大输出落盘（见 outputs.py）
    emit: Callable[[str, dict], None] | None = None
    ask: Callable[[str, list[str]], Awaitable[str]] | None = None
    cancel: asyncio.Event | None = None
    sandbox_kind: str = "none"

    @property
    def sid(self) -> str:
        return self.session.sid

    @property
    def max_bytes(self) -> int:
        return int(self.cfg.get("security", "max_file_mb", default=20)) * 1024 * 1024
