"""联网调研统一策略闸门（ResearchPolicy）：
覆盖离线强制拦截、域名允许/拒绝列表、类别拦截、出站查询脱敏、PII 最小化、UA 合规校验、
反爬/付费墙/验证码检测与 X-Robots-Tag / meta robots 指令解析。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .. import __version__, security as sec

DEFAULT_USER_AGENT = f"LocalAgent/{__version__} (+https://github.com/Niuspin/localagent)"

_IMPERSONATE_UA_RE = re.compile(
    r"\b(mozilla|chrome|safari|firefox|edg|edge|opera|googlebot|bingbot|baiduspider|duckduckbot|slurp|gptbot|bytespider|claudebot)\b",
    re.I,
)

# 内置高风险/违规类别匹配（用户可在 research.deny_categories 中增减）
CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "adult": (
        "pornhub", "xvideos", "xnxx", "xhamster", "redtube", "youporn",
        "onlyfans", "chaturbate", "stripchat", "hentai", "nsfw",
    ),
    "malware": (
        "malware", "ransomware", "trojan", "keylogger", "warez-crack",
        "exploit-kit", "c2-server", "stealer",
    ),
    "piracy": (
        "thepiratebay", "1337x", "rarbg", "nyaa.si", "sci-hub",
        "libgen", "z-library", "piratebay", "kickasstorrents",
    ),
    "paywall_bypass": (
        "12ft.io", "removepaywall", "bypasspaywalls", "paywallbypass",
        "unpaywall-hack", "archive-paywall",
    ),
}

# 出站查询敏感模式：防止通过搜索词外泄密钥、本地路径或个人隐私画像
_SECRET_QUERY_PATS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "疑似 API Key (sk-*)"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "疑似 GitHub Token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS Access Key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "疑似 Google API Key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "疑似 Slack Token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "疑似私钥"),
    (re.compile(r"(?:api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9_.\-]{8,}", re.I), "疑似凭据赋值"),
]

_LOCAL_PATH_PATS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z]:\\(?:Users|Windows|Program Files|ProgramData)\\[^\s\"']+", re.I), "含本机 Windows 绝对路径"),
    (re.compile(r"(?:^|\s)/(?:home|Users|root|etc|var|private)/[^\s\"']+"), "含本机 POSIX 绝对路径"),
    (re.compile(r"(?:^|[/\\\s])(?:\.env(?:\.\w+)?|secrets\.json|id_rsa|id_ed25519|\.aws[/\\]credentials|\.git-credentials)(?:\b|$)", re.I), "含本地敏感文件名"),
]

_PROFILING_PATS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:身份证号|家庭住址|户籍|开盒|人肉搜索|查手机号|隐私信息|银行卡号|社保号)", re.I), "涉及个人画像/隐私信息检索"),
    (re.compile(r"\b(?:doxx|home address of|social security number|ssn of|phone number of|passport number)\b", re.I), "涉及个人隐私画像检索"),
    (re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"), "查询中包含疑似身份证号"),
]

# 落盘前 PII 脱敏模式
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CN_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID_RE = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# 访问控制/人机验证/付费墙识别（绝不绕过，统一标记 blocked）
_CAPTCHA_RE = re.compile(
    r"(?:captcha|g-recaptcha|h-captcha|cf-turnstile|turnstile|cf-challenge|challenge-platform|"
    r"verify you are human|are you a robot|unusual traffic|bot detection|人机验证|安全验证|请完成验证)",
    re.I,
)
_PAYWALL_RE = re.compile(
    r"(?:paywall|subscribe to (?:continue|read)|subscribers[- ]only|membership required|"
    r"unlock this article|付费阅读|订阅后查看|开通会员查看全文)",
    re.I,
)
_LOGIN_RE = re.compile(
    r"(?:sign in to continue|log in to (?:view|continue|read)|authentication required|请先登录|登录后查看)",
    re.I,
)


@dataclass
class PolicyDecision:
    allowed: bool
    action: str       # allow | ask | deny
    reason: str = ""
    category: str = ""
    domain: str = ""


def is_offline() -> bool:
    return os.environ.get("LOCALAGENT_OFFLINE") == "1"


def domain_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_loopback_host(host: str) -> bool:
    h = (host or "").lower().strip("[]").rstrip(".")
    return h in ("127.0.0.1", "::1", "localhost") or h.endswith(".localhost")


def _domain_matches(dom: str, patterns: list[str] | tuple[str, ...]) -> bool:
    d = (dom or "").lower().lstrip("*.")
    if not d:
        return False
    for raw in patterns or ():
        p = str(raw or "").strip().lower()
        if not p:
            continue
        p = p.lstrip("*.")
        if d == p or d.endswith("." + p):
            return True
    return False


def safe_user_agent(cfg: Any = None) -> str:
    if cfg is not None:
        raw = str(cfg.get("research", "user_agent", default="") or "").strip()
        if raw and not _IMPERSONATE_UA_RE.search(raw):
            return raw[:200]
    return DEFAULT_USER_AGENT


def redact_pii(text: str) -> str:
    """落盘前对邮箱、手机号、身份证号、SSN 等 PII 做脱敏。"""
    if not text:
        return ""
    s = _EMAIL_RE.sub("[已脱敏:email]", text)
    s = _CN_ID_RE.sub("[已脱敏:id]", s)
    s = _SSN_RE.sub("[已脱敏:ssn]", s)
    s = _CN_PHONE_RE.sub("[已脱敏:phone]", s)
    return s


def check_outbound_query(query: str) -> tuple[bool, str]:
    """出站搜索词/查询脱敏检查：含密钥、本机路径、敏感文件名或个人画像意图时拦截。"""
    q = str(query or "").strip()
    if not q:
        return False, "查询内容为空"
    for rx, why in _SECRET_QUERY_PATS:
        if rx.search(q):
            return False, f"出站查询拦截（{why}），防止密钥通过搜索词外泄"
    for rx, why in _LOCAL_PATH_PATS:
        if rx.search(q):
            return False, f"出站查询拦截（{why}），请勿把本机路径发送到外部搜索引擎"
    for rx, why in _PROFILING_PATS:
        if rx.search(q):
            return False, f"出站查询拦截（{why}）"
    # 复用 security.sensitive_reason 对查询词中的凭据文件名片段做二次校验
    for tok in re.split(r"[\s\"']+", q):
        if tok and ("." in tok or "/" in tok or "\\" in tok):
            sr = sec.sensitive_reason(tok)
            if sr:
                return False, f"出站查询拦截（{sr}）"
    return True, ""


def detect_access_block(status: int, headers: dict[str, str] | None = None, body_text: str = "") -> tuple[bool, str]:
    """检测登录墙、付费墙、验证码与反爬挑战——绝不绕过，统一标记 blocked。"""
    head = (body_text or "")[:12000]
    if status == 402:
        return True, "paywall"
    if status == 401:
        return True, "login_wall"
    if _CAPTCHA_RE.search(head):
        return True, "captcha"
    if _PAYWALL_RE.search(head):
        return True, "paywall"
    if status == 403:
        if _LOGIN_RE.search(head):
            return True, "login_wall"
        return True, "anti_bot_403"
    if status < 400 and len(head.strip()) < 1500 and _LOGIN_RE.search(head):
        return True, "login_wall"
    return False, ""


def parse_directives(headers: dict[str, str] | None = None, html_text: str = "") -> dict[str, Any]:
    """解析 X-Robots-Tag HTTP 头与 HTML <meta name="robots"> 指令（noindex/noarchive/noai/nosnippet）。"""
    tokens: list[str] = []
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if hdrs.get("x-robots-tag"):
        tokens.extend(re.split(r"[,;\s]+", hdrs["x-robots-tag"].lower()))
    if html_text:
        head_slice = html_text[:64000]
        for m in re.finditer(
            r"""<meta\b[^>]*\bname\s*=\s*["']?(?:robots|googlebot|bingbot|gptbot|ccbot)["']?[^>]*>""",
            head_slice,
            re.I,
        ):
            tag = m.group(0)
            cm = re.search(r"""\bcontent\s*=\s*["']([^"']+)["']""", tag, re.I)
            if cm:
                tokens.extend(re.split(r"[,;\s]+", cm.group(1).lower()))
    clean = [t.strip() for t in tokens if t.strip()]
    tok_set = set(clean)
    cc = hdrs.get("cache-control", "").lower()
    no_store = "no-store" in cc
    return {
        "noindex": "noindex" in tok_set or "none" in tok_set,
        "noarchive": "noarchive" in tok_set or "nocache" in tok_set or "none" in tok_set or no_store,
        "noai": "noai" in tok_set or "noimageai" in tok_set,
        "nosnippet": "nosnippet" in tok_set,
        "raw": sorted(tok_set),
    }


class ResearchPolicy:
    """联网策略统一闸门：所有搜索、抓取、调研流水线及外部 HTTP MCP 请求均须经过此闸门。"""

    def __init__(self, cfg: Any):
        self.cfg = cfg

    def _rcfg(self) -> dict[str, Any]:
        val = self.cfg.get("research", default={}) if self.cfg else {}
        return val if isinstance(val, dict) else {}

    def mode(self, mode_override: str | None = None) -> str:
        if is_offline():
            return "off"
        if mode_override:
            ov = str(mode_override).strip().lower()
            if ov in ("off", "ask", "allowlist", "open"):
                return ov
        return str(self._rcfg().get("mode") or "off").lower()

    def respect_robots(self, mode_override: str | None = None) -> bool:
        rc = self._rcfg()
        m = self.mode(mode_override)
        val = bool(rc.get("respect_robots", True))
        if not val and m != "allowlist":
            return True
        return val

    def category_for(self, url_or_domain: str) -> str:
        s = (url_or_domain or "").lower()
        deny_cats = [str(c).lower() for c in (self._rcfg().get("deny_categories") or ["adult", "malware", "paywall_bypass"])]
        for cat in deny_cats:
            pats = CATEGORY_RULES.get(cat, ())
            if any(p in s for p in pats):
                return cat
        return ""

    def evaluate_query(self, query: str, *, mode_override: str | None = None) -> PolicyDecision:
        if is_offline():
            return PolicyDecision(False, "deny", "离线模式（--offline）已禁用联网搜索")
        if mode_override and str(mode_override).strip().lower() == "off":
            return PolicyDecision(
                False,
                "deny",
                "当前会话未开启联网授权（联网：关闭），请点击输入框下方「联网授权」开启后再试",
                category="web_auth_off",
            )
        ok, why = check_outbound_query(query)
        if not ok:
            return PolicyDecision(False, "deny", why, category="query_sanitization")
        return PolicyDecision(True, "allow", "")

    def evaluate_url(
        self,
        url: str,
        *,
        purpose: str = "web_fetch",
        approved_domains: set[str] | None = None,
        allow_loopback: bool = False,
        mode_override: str | None = None,
        once_approved_urls: set[str] | None = None,
    ) -> PolicyDecision:
        try:
            sp = urlsplit(url)
            host = (sp.hostname or "").lower()
        except ValueError:
            return PolicyDecision(False, "deny", f"非法 URL: {url[:80]}")
        dom = domain_of(url) or host

        if is_offline():
            if allow_loopback and is_loopback_host(host):
                return PolicyDecision(True, "allow", "loopback_allowed", domain=dom)
            return PolicyDecision(False, "deny", "离线模式（--offline）已禁用外部网络请求", domain=dom)

        if sp.scheme not in ("http", "https"):
            return PolicyDecision(False, "deny", f"仅允许 http/https 协议: {sp.scheme}", domain=dom)

        rc = self._rcfg()
        deny_domains = list(rc.get("deny_domains") or [])
        allow_domains = list(rc.get("allow_domains") or [])
        if _domain_matches(dom, deny_domains) or _domain_matches(host, deny_domains):
            return PolicyDecision(False, "deny", f"域名 {dom} 在拒绝列表 (deny_domains) 中", category="deny_domain", domain=dom)

        cat = self.category_for(url)
        if cat:
            return PolicyDecision(False, "deny", f"URL 命中受限内容类别 ({cat}): {dom}", category=cat, domain=dom)

        m = self.mode(mode_override)
        if purpose.startswith("research"):
            if m == "off":
                return PolicyDecision(False, "deny", "自主联网调研默认关闭（research.mode=off），请在 设置→搜索 中开启后使用", domain=dom)
            if m == "allowlist":
                if allow_loopback and is_loopback_host(host):
                    return PolicyDecision(True, "allow", "loopback_allowed", domain=dom)
                if not (_domain_matches(dom, allow_domains) or _domain_matches(host, allow_domains)):
                    return PolicyDecision(False, "deny", f"域名 {dom} 不在 allow_domains 白名单中", category="allowlist", domain=dom)
                return PolicyDecision(True, "allow", "allowlist", domain=dom)
            if m == "ask":
                appr = approved_domains or set()
                if _domain_matches(dom, allow_domains) or _domain_matches(dom, list(appr)) or (allow_loopback and is_loopback_host(host)):
                    return PolicyDecision(True, "allow", "approved", domain=dom)
                return PolicyDecision(False, "ask", f"research.mode=ask：首次访问域名 {dom} 需要用户确认", domain=dom)
            return PolicyDecision(True, "allow", "open", domain=dom)

        if mode_override is not None and m == "off" and purpose == "web_fetch" and not (allow_loopback and is_loopback_host(host)):
            return PolicyDecision(False, "deny", "当前会话未开启联网授权（联网：关闭），请点击输入框下方「联网授权」开启", category="web_auth_off", domain=dom)

        if once_approved_urls and url in once_approved_urls:
            return PolicyDecision(True, "allow", "once_approved", domain=dom)

        # 非 automated research 路径（web_fetch / web_search / mcp_http）：
        if m == "allowlist" and not (allow_loopback and is_loopback_host(host)):
            appr = approved_domains or set()
            if not (_domain_matches(dom, allow_domains) or _domain_matches(host, allow_domains) or _domain_matches(dom, list(appr))):
                return PolicyDecision(False, "deny", f"当前处于白名单模式 (allowlist)，域名 {dom} 未在允许列表内", category="allowlist", domain=dom)
        if m == "ask" and purpose in ("web_fetch", "mcp_http") and not (allow_loopback and is_loopback_host(host)):
            appr = approved_domains or set()
            if not (_domain_matches(dom, allow_domains) or _domain_matches(dom, list(appr))):
                return PolicyDecision(False, "ask", f"联网策略为逐域确认 (ask)：访问新域名 {dom} 需用户确认", domain=dom)
        return PolicyDecision(True, "allow", "ok", domain=dom)
