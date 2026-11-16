"""Swarm 集群运行时：DAG 调度、角色权限、类型化邮箱、租约锁、串行 MergeQueue、预算账本与崩溃恢复。"""
from __future__ import annotations

from .budget import AgentBudget, BudgetLedger
from .graph import CycleError, NODE_STATUSES, TaskGraph, TaskNode
from .locks import FileLeaseManager
from .mailbox import MSG_KINDS, MailMessage, Mailbox
from .manager import SwarmManager
from .merge import ConflictCard, MergeQueue, MergeResult
from .persist import SwarmStore
from .roles import ROLE_SPECS, RoleSpec, effective_permission, get_role
from .scheduler import SwarmScheduler

__all__ = [
    "AgentBudget",
    "BudgetLedger",
    "ConflictCard",
    "CycleError",
    "FileLeaseManager",
    "MailMessage",
    "Mailbox",
    "MergeQueue",
    "MergeResult",
    "MSG_KINDS",
    "NODE_STATUSES",
    "ROLE_SPECS",
    "RoleSpec",
    "SwarmManager",
    "SwarmScheduler",
    "SwarmStore",
    "TaskGraph",
    "TaskNode",
    "effective_permission",
    "get_role",
]
