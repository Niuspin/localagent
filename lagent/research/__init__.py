"""M5 Research Engine：安全合法联网采集、策略闸门、robots/限速/缓存、证据库、引用校验与网络账本。"""
from __future__ import annotations

from .cache import WebCache
from .evidence import EvidenceStore
from .extract import ExtractedDoc, extract_html, extract_response
from .ledger import NetLedger
from .limiter import DomainLimiter, parse_retry_after
from .pipeline import ResearchManager, ResearchRun
from .policy import (
    CATEGORY_RULES,
    DEFAULT_USER_AGENT,
    PolicyDecision,
    ResearchPolicy,
    check_outbound_query,
    detect_access_block,
    domain_of,
    is_loopback_host,
    is_offline,
    parse_directives,
    redact_pii,
    safe_user_agent,
)
from .robots import RobotsCache, RobotsRules, parse_robots_txt
from .verify import cite_check
from .watch import PageWatchManager

__all__ = [
    "CATEGORY_RULES",
    "DEFAULT_USER_AGENT",
    "DomainLimiter",
    "EvidenceStore",
    "ExtractedDoc",
    "NetLedger",
    "PageWatchManager",
    "PolicyDecision",
    "ResearchManager",
    "ResearchPolicy",
    "ResearchRun",
    "RobotsCache",
    "RobotsRules",
    "WebCache",
    "check_outbound_query",
    "cite_check",
    "detect_access_block",
    "domain_of",
    "extract_html",
    "extract_response",
    "is_loopback_host",
    "is_offline",
    "parse_directives",
    "parse_retry_after",
    "parse_robots_txt",
    "redact_pii",
    "safe_user_agent",
]
