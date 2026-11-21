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
    shell_cwd: str = ""                                 # 会话级终端当前相对工作目录
    shell_env: dict = field(default_factory=dict)       # 会话级安全终端环境变量覆盖
    swarm_id: str | None = None                         # 当前会话关联的活跃 Swarm ID
    agent_id: str = "main"                              # 当前执行主体 ID
    allowed_tools: set[str] | None = None               # Swarm 角色白名单（None 表示不限制）
    skill_allowed_tools: set[str] | None = None         # 当前技能工具白名单（只收窄，不放宽）
    model_tier: str = "standard"                        # 当前激活模型档位：compact / standard / large
    web_auth: str | None = None                         # 会话生效联网授权档位（off/ask/allowlist/open）
    approved_domains: set = field(default_factory=set)  # 用户显式信任的域名集合
    once_approved_urls: set = field(default_factory=set)  # 单次审批通过的 URL（不落盘）


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
    artifacts: Any = None    # ArtifactStore：会话多媒体/结构化产物仓储（见 artifacts.py）
    swarms: Any = None       # SwarmManager：多代理 DAG 集群管理器（见 swarm/）
    research: Any = None     # ResearchManager：M5 联网调研与策略/账本管理器（见 research/）
    emit: Callable[[str, dict], None] | None = None
    ask: Callable[[str, list[str]], Awaitable[str]] | None = None
    cancel: asyncio.Event | None = None
    sandbox_kind: str = "none"
    tool_call_id: str = ""

    @property
    def sid(self) -> str:
        return self.session.sid

    @property
    def max_bytes(self) -> int:
        return int(self.cfg.get("security", "max_file_mb", default=20)) * 1024 * 1024
