"""web_fetch：抓取网页正文（SSRF 防护、策略闸门、robots.txt、限速、条件缓存、隐藏文本清洗、PDF 支持、分页）。"""
from __future__ import annotations

import asyncio
import io
import re
import time
from typing import Any
from urllib.parse import urljoin

from .. import net
from ..research.extract import extract_html, extract_response
from ..research.limiter import DomainLimiter
from ..research.policy import ResearchPolicy, detect_access_block, domain_of, safe_user_agent
from ..research.robots import RobotsCache
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx

# url -> (ts, title, text, links, note)
_CACHE: dict[str, tuple[float, str, str, list[tuple[str, str]], str]] = {}
_DEFAULT_ROBOTS = RobotsCache()
_DEFAULT_LIMITER = DomainLimiter()


def html_to_text(html: str, base_url: str) -> tuple[str, str, list[tuple[str, str]]]:
    doc = extract_html(html, base_url, do_redact_pii=False)
    return doc.title, doc.text, doc.links


def _pdf_text(data: bytes) -> str:
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(pdf.pages[:80], 1))
    except ImportError:
        from pypdf import PdfReader  # type: ignore

        r = PdfReader(io.BytesIO(data))
        return "\n".join(f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(r.pages[:80], 1))


def _fetch_page(ctx: ToolCtx, url: str) -> tuple[str, str, list[tuple[str, str]], str, bool]:
    cfg = ctx.cfg
    research = getattr(ctx, "research", None)
    policy = research.policy if research is not None else ResearchPolicy(cfg)
    ledger = research.ledger if research is not None else None
    robots = research.robots if research is not None else _DEFAULT_ROBOTS
    limiter = research.limiter if research is not None else _DEFAULT_LIMITER
    wcache = research.cache if research is not None else None
    agent_id = getattr(ctx.session, "agent_id", "main") if ctx.session else "main"
    sid = ctx.sid if ctx.session else ""

    dec = policy.evaluate_url(url, purpose="web_fetch", approved_domains=set(ctx.session.user_domains or ()))
    if not dec.allowed:
        if ledger is not None:
            ledger.record(
                url=url, method="GET", status=0, purpose="web_fetch",
                agent_id=agent_id, session_id=sid, decision="deny", reason=dec.reason,
            )
        raise net.NetError(dec.reason)

    dom = dec.domain or domain_of(url)
    blk = limiter.is_blocked(dom)
    if blk:
        if ledger is not None:
            ledger.record(
                url=url, method="GET", status=0, purpose="web_fetch",
                agent_id=agent_id, session_id=sid, decision="blocked", reason=blk,
            )
        raise net.NetError(f"站点 {dom} 此前触发访问限制 ({blk})，已跳过（不绕过反爬/验证）")

    proxy = cfg.get("search", "proxy", default="")
    ok_rob, delay, why_rob = robots.check_sync(url, cfg=cfg, proxy=proxy, respect_robots=policy.respect_robots())
    if not ok_rob:
        if ledger is not None:
            ledger.record(
                url=url, method="GET", status=0, purpose="web_fetch",
                agent_id=agent_id, session_id=sid, decision="deny", reason=why_rob,
            )
        raise net.NetError(f"robots.txt 拒绝访问: {why_rob}")

    qcap = int(cfg.get("research", "quote_max_chars", default=400))
    pii = bool(cfg.get("research", "pii_redact", default=True))

    if wcache is not None:
        hit_disk = wcache.get(url)
        if hit_disk and hit_disk.get("fresh"):
            if ledger is not None:
                ledger.record(
                    url=url, method="GET", status=hit_disk["status"], bytes_in=len(hit_disk["body"]),
                    purpose="web_fetch", agent_id=agent_id, session_id=sid, decision="allow", cached=True,
                )
            doc = extract_response(hit_disk["body"], url, headers=hit_disk.get("headers"), quote_max_chars=qcap, do_redact_pii=pii)
            no_cache = bool(doc.directives.get("noarchive") or doc.directives.get("noai") or doc.directives.get("noindex"))
            return doc.title, doc.text, doc.links, "", no_cache

    cond_hdrs = wcache.conditional_headers(url) if wcache is not None else {}
    ua = safe_user_agent(cfg)
    req_hdrs = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        **cond_hdrs,
    }

    r = None
    for attempt in range(2):
        with limiter.acquire_sync(dom, crawl_delay=delay):
            t0 = time.monotonic()
            r = net.fetch_sync(
                url,
                timeout=float(cfg.get("web", "timeout_s", default=20)),
                max_bytes=int(cfg.get("web", "max_bytes", default=3_000_000)),
                proxy=proxy,
                headers=req_hdrs,
            )
            dur = int((time.monotonic() - t0) * 1000)
        if r.status == 304 and wcache is not None:
            refreshed = wcache.touch_304(url, r.headers)
            if ledger is not None:
                ledger.record(
                    url=url, method="GET", status=304, bytes_in=0, purpose="web_fetch",
                    agent_id=agent_id, session_id=sid, decision="allow", cached=True, duration_ms=dur,
                )
            if refreshed:
                doc = extract_response(refreshed["body"], url, headers=refreshed.get("headers"), quote_max_chars=qcap, do_redact_pii=pii)
                no_cache = bool(doc.directives.get("noarchive") or doc.directives.get("noai") or doc.directives.get("noindex"))
                return doc.title, doc.text, doc.links, "", no_cache
        if r.status in (429, 503):
            wait_s = limiter.note_response(dom, r.status, r.headers.get("retry-after"))
            if ledger is not None:
                ledger.record(
                    url=url, method="GET", status=r.status, bytes_in=len(r.body), purpose="web_fetch",
                    agent_id=agent_id, session_id=sid, decision="retry" if attempt == 0 else "deny",
                    reason=f"HTTP {r.status}", duration_ms=dur,
                )
            if attempt == 0:
                time.sleep(min(wait_s, 3.0))
                continue
            raise net.NetError(f"HTTP {r.status}（站点限流）")
        limiter.note_response(dom, r.status)
        break

    assert r is not None
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    raw_txt = "" if ct == "application/pdf" else r.text()
    is_blk, blk_why = detect_access_block(r.status, r.headers, raw_txt)
    if is_blk:
        limiter.mark_blocked(dom, blk_why)
        if ledger is not None:
            ledger.record(
                url=r.url, method="GET", status=r.status, bytes_in=len(r.body), purpose="web_fetch",
                agent_id=agent_id, session_id=sid, decision="blocked", reason=blk_why,
            )
        raise net.NetError(f"HTTP {r.status}（检测到 {blk_why}，LocalAgent 不绕过访问控制，换个来源试试）")

    if r.status >= 400:
        if ledger is not None:
            ledger.record(
                url=r.url, method="GET", status=r.status, bytes_in=len(r.body), purpose="web_fetch",
                agent_id=agent_id, session_id=sid, decision="error", reason=f"HTTP {r.status}",
            )
        raise net.NetError(f"HTTP {r.status}")

    if ct.startswith("image/") or ct.startswith(("audio/", "video/")) or ct in ("application/zip", "application/octet-stream", "application/gzip"):
        raise net.NetError(f"不支持的内容类型 {ct}")

    note = f"（内容被截断到 {len(r.body) // 1024} KB）" if r.truncated else ""
    doc = extract_response(
        r.body,
        r.url,
        headers=r.headers,
        content_type=ct,
        charset_text=raw_txt if ct != "application/pdf" else None,
        quote_max_chars=qcap,
        do_redact_pii=pii,
    )
    if doc.kind == "html" and len(doc.text) < 200 and re.search(r"enable javascript|请开启.*javascript|requires javascript", raw_txt, re.I):
        note += "（页面需要 JavaScript 渲染，可能读不到正文）"

    if wcache is not None:
        wcache.put(url, status=r.status, headers=r.headers, body=r.body, directives=doc.directives)
    if ledger is not None:
        ledger.record(
            url=r.url, method="GET", status=r.status, bytes_in=len(r.body), purpose="web_fetch",
            agent_id=agent_id, session_id=sid, decision="allow",
        )
    no_cache = bool(doc.directives.get("noarchive") or doc.directives.get("noai") or doc.directives.get("noindex"))
    return doc.title, doc.text, doc.links, note, no_cache


async def web_fetch(ctx: ToolCtx, a: dict) -> ToolResult:
    url = a["url"].strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    off = max(0, int(a.get("offset") or 0))
    max_chars = int(ctx.cfg.get("web", "max_chars", default=12000))
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        title, text, links, note = hit[1], hit[2], hit[3], hit[4]
    else:
        title, text, links, note, no_cache = await asyncio.to_thread(_fetch_page, ctx, url)
        if not no_cache:
            _CACHE[url] = (time.time(), title, text, links, note)
            if len(_CACHE) > 40:
                _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
    d = net.domain_of(url)
    if d:
        ctx.session.user_domains.add(d)
    total = len(text)
    chunk = text[off: off + max_chars]
    head = f"[{title or url}]\n{url}\n"
    tail = ""
    if off + max_chars < total:
        tail = f"\n…[还有 {total - off - max_chars} 字符；用 offset={off + max_chars} 继续阅读]"
    if a.get("show_links") and links:
        tail += "\n\n页面链接:\n" + "\n".join(f"- {t} → {u}" for t, u in links[:30])
    if not chunk.strip():
        return ToolResult(head + "（未提取到正文）" + note, meta={"url": url})
    return ToolResult(
        head + "\n" + chunk + tail + (("\n" + note) if note else ""),
        meta={"url": url, "chars": total},
        taint=f"网页 {d}",
        untrusted_source=url,
    )


register(Tool(
    "web_fetch", "读取一个网页（或在线 PDF）的正文文本。内容很长时用 offset 分页。不会执行 JavaScript。仅限公网地址。",
    obj({"url": P("string", "网址"), "offset": P("integer", "从第几个字符开始，默认 0"),
         "show_links": P("boolean", "在末尾附上页面中的链接列表（用于继续导航）")}, ["url"]),
    web_fetch, "net_fetch", group="web", title=lambda a: f"读取网页 {a.get('url', '')[:70]}"))
