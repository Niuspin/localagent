"""web_fetch：抓取网页正文（SSRF 防护、限量、HTML→文本、PDF 支持、分页）。"""
from __future__ import annotations

import asyncio
import io
import re
import time
from urllib.parse import urljoin, urlsplit

from .. import net
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx

_CACHE: dict[str, tuple[float, str, str]] = {}   # url -> (ts, title, text)
_DROP = ("script", "style", "noscript", "template", "svg", "iframe", "canvas", "form", "nav", "footer", "aside", "header", "button", "select", "dialog")
_NOISE = re.compile(r"(^|[-_ ])(cookie|banner|advert|ads?|sidebar|menu|popup|modal|share|social|breadcrumb|comment|related|newsletter|subscribe)([-_ ]|$)", re.I)


def html_to_text(html: str, base_url: str) -> tuple[str, str, list[tuple[str, str]]]:
    from bs4 import BeautifulSoup, NavigableString  # type: ignore
    try:
        s = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        s = BeautifulSoup(html, "html.parser")
    title = (s.title.get_text().strip() if s.title else "")[:200]
    for t in s(_DROP):
        t.decompose()
    for t in s.find_all(attrs={"aria-hidden": "true"}):
        t.decompose()
    for t in list(s.find_all(True)):
        if t.attrs is None:
            continue
        cls = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if cls.strip() and _NOISE.search(cls) and t.name not in ("html", "body", "article", "main"):
            t.decompose()
    root = s.find("article") or s.find("main") or s.find(attrs={"role": "main"}) or s.body or s
    links: list[tuple[str, str]] = []

    out: list[str] = []

    def walk(node, depth=0):
        for ch in node.children:
            if isinstance(ch, NavigableString):
                tx = re.sub(r"\s+", " ", str(ch))
                if tx.strip():
                    out.append(tx)
                continue
            n = ch.name
            if n in ("h1", "h2", "h3", "h4", "h5", "h6"):
                out.append("\n\n" + "#" * int(n[1]) + " " + re.sub(r"\s+", " ", ch.get_text()).strip() + "\n")
            elif n in ("p", "div", "section", "tr", "dd", "dt", "figcaption", "blockquote"):
                out.append("\n")
                walk(ch, depth + 1)
                out.append("\n")
            elif n == "br":
                out.append("\n")
            elif n == "li":
                out.append("\n- ")
                walk(ch, depth + 1)
            elif n in ("td", "th"):
                walk(ch, depth + 1)
                out.append(" | ")
            elif n in ("pre", "code") and n == "pre":
                out.append("\n```\n" + ch.get_text() + "\n```\n")
            elif n == "a":
                href = ch.get("href", "")
                txt = re.sub(r"\s+", " ", ch.get_text()).strip()
                if href and txt and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    full = urljoin(base_url, href)
                    if full.startswith("http") and len(links) < 80:
                        links.append((txt[:80], full))
                out.append(txt if txt else "")
            elif n == "img":
                alt = (ch.get("alt") or "").strip()
                if alt:
                    out.append(f"[图: {alt[:80]}]")
            else:
                walk(ch, depth + 1)

    walk(root)
    text = "".join(out)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text, links


def _pdf_text(data: bytes) -> str:
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(pdf.pages[:80], 1))
    except ImportError:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(io.BytesIO(data))
        return "\n".join(f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(r.pages[:80], 1))


def _fetch_page(ctx: ToolCtx, url: str) -> tuple[str, str, list[tuple[str, str]], str]:
    cfg = ctx.cfg
    r = net.fetch_sync(url, timeout=float(cfg.get("web", "timeout_s", default=20)), max_bytes=int(cfg.get("web", "max_bytes", default=3_000_000)),
                       proxy=cfg.get("search", "proxy", default=""),
                       headers={"Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
                                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    if r.status >= 400:
        raise net.NetError(f"HTTP {r.status}" + ("（站点拒绝机器人访问，换个来源试试）" if r.status in (401, 403, 429) else ""))
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    final = r.url
    note = f"（内容被截断到 {len(r.body) // 1024} KB）" if r.truncated else ""
    if ct == "application/pdf" or final.lower().split("?")[0].endswith(".pdf"):
        return "PDF 文档", _pdf_text(r.body), [], note
    if ct.startswith("image/") or ct.startswith(("audio/", "video/")) or ct in ("application/zip", "application/octet-stream", "application/gzip"):
        raise net.NetError(f"不支持的内容类型 {ct}")
    html = r.text()
    if ct in ("text/html", "application/xhtml+xml", "") or "<html" in html[:500].lower():
        t, text, links = html_to_text(html, final)
        if len(text) < 200 and re.search(r"enable javascript|请开启.*javascript|requires javascript", html, re.I):
            note += "（页面需要 JavaScript 渲染，可能读不到正文）"
        return t, text, links, note
    return "", html, [], note


async def web_fetch(ctx: ToolCtx, a: dict) -> ToolResult:
    url = a["url"].strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    off = max(0, int(a.get("offset") or 0))
    max_chars = int(ctx.cfg.get("web", "max_chars", default=12000))
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        title, text, links, note = hit[1], hit[2], [], ""
    else:
        title, text, links, note = await asyncio.to_thread(_fetch_page, ctx, url)
        _CACHE[url] = (time.time(), title, text)
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
    return ToolResult(head + "\n" + chunk + tail + (("\n" + note) if note else ""), meta={"url": url, "chars": total},
                      taint=f"网页 {d}", untrusted_source=url)


register(Tool(
    "web_fetch", "读取一个网页（或在线 PDF）的正文文本。内容很长时用 offset 分页。不会执行 JavaScript。仅限公网地址。",
    obj({"url": P("string", "网址"), "offset": P("integer", "从第几个字符开始，默认 0"),
         "show_links": P("boolean", "在末尾附上页面中的链接列表（用于继续导航）")}, ["url"]),
    web_fetch, "net_fetch", group="web", title=lambda a: f"读取网页 {a.get('url', '')[:70]}"))
