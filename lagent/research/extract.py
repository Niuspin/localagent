"""网页与在线文档安全抽取（extract.py）：
- 优先使用 lxml 解析（缺失时回退 html.parser）；
- 彻底剔除隐藏提示注入节点（display:none / visibility:hidden / opacity:0 / font-size:0 / aria-hidden / HTML 注释）与不可见字符；
- 抽取 canonical、标题、作者、发布日期、版权/协议、表格、正文与链接；
- 支持 PDF 文本抽提。
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from .. import security as sec
from .policy import parse_directives, redact_pii

_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "iframe",
    "canvas", "form", "nav", "footer", "aside", "header", "button", "select", "dialog",
)
_NOISE_CLS_RE = re.compile(
    r"(^|[-_ ])(cookie|banner|advert|ads?|sidebar|menu|popup|modal|share|social|breadcrumb|comment|related|newsletter|subscribe)([-_ ]|$)",
    re.I,
)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|font-size\s*:\s*0(?:px|em|rem|pt)?\s*(?:;|$)|text-indent\s*:\s*-\d{4,}px)",
    re.I,
)


@dataclass
class ExtractedDoc:
    url: str
    canonical_url: str
    title: str
    text: str
    excerpt: str
    author: str = ""
    published_at: str = ""
    license: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    directives: dict[str, Any] = field(default_factory=dict)
    hidden_removed: int = 0
    injection_warnings: list[str] = field(default_factory=list)
    sha256: str = ""
    kind: str = "html"


def _make_soup(html: str):
    from bs4 import BeautifulSoup  # type: ignore

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def _meta_content(soup: Any, *names: str) -> str:
    want = {n.lower() for n in names}
    for m in soup.find_all("meta"):
        key = str(m.get("name") or m.get("property") or m.get("itemprop") or "").strip().lower()
        if key in want:
            val = str(m.get("content") or "").strip()
            if val:
                return val[:200]
    return ""


def _extract_pdf_text(data: bytes) -> str:
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(
                f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}"
                for i, p in enumerate(pdf.pages[:80], 1)
            ).strip()
    except ImportError:
        from pypdf import PdfReader  # type: ignore

        r = PdfReader(io.BytesIO(data))
        return "\n".join(
            f"--- 第 {i} 页 ---\n{(p.extract_text() or '').strip()}"
            for i, p in enumerate(r.pages[:80], 1)
        ).strip()


def extract_html(
    html: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    quote_max_chars: int = 400,
    do_redact_pii: bool = True,
) -> ExtractedDoc:
    from bs4 import Comment, NavigableString  # type: ignore

    directives = parse_directives(headers, html)
    soup = _make_soup(html)

    # 先收集注入预警（在剔除隐藏元素前，但隐藏元素绝不进入最终文本）
    hidden_removed = 0
    hidden_snippets: list[str] = []

    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        cs = str(c).strip()
        if cs:
            hidden_snippets.append(cs[:400])
        c.extract()
        hidden_removed += 1

    for t in soup(_DROP_TAGS):
        t.decompose()

    for t in list(soup.find_all(True)):
        if t.attrs is None:
            continue
        style = str(t.get("style") or "")
        is_hidden = (
            t.get("hidden") is not None
            or str(t.get("aria-hidden") or "").lower() == "true"
            or bool(_HIDDEN_STYLE_RE.search(style))
        )
        if is_hidden:
            txt = t.get_text(" ", strip=True)
            if txt:
                hidden_snippets.append(txt[:400])
            t.decompose()
            hidden_removed += 1
            continue
        cls = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if cls.strip() and _NOISE_CLS_RE.search(cls) and t.name not in ("html", "body", "article", "main"):
            t.decompose()

    title = sec.sanitize_untrusted(soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    if not title:
        title = sec.sanitize_untrusted(_meta_content(soup, "og:title", "twitter:title"))

    canonical = url
    link_can = soup.find("link", rel=lambda v: v and ("canonical" in ([x.lower() for x in v] if isinstance(v, list) else str(v).lower())))
    if link_can and link_can.get("href"):
        canonical = urljoin(url, str(link_can["href"]).strip())

    author = sec.sanitize_untrusted(_meta_content(soup, "author", "article:author", "dc.creator"))
    published_at = sec.sanitize_untrusted(_meta_content(soup, "article:published_time", "pubdate", "publishdate", "date", "dc.date"))
    lic = sec.sanitize_untrusted(_meta_content(soup, "license", "dc.rights", "copyright"))

    root = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    links: list[tuple[str, str]] = []
    tables: list[list[list[str]]] = []

    for tbl in root.find_all("table")[:8]:
        rows: list[list[str]] = []
        for tr in tbl.find_all("tr")[:40]:
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True))[:120] for td in tr.find_all(["th", "td"])[:12]]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)

    out: list[str] = []

    def walk(node: Any) -> None:
        for ch in node.children:
            if isinstance(ch, NavigableString):
                tx = re.sub(r"\s+", " ", str(ch))
                if tx.strip():
                    out.append(tx)
                continue
            n = getattr(ch, "name", "")
            if n in ("h1", "h2", "h3", "h4", "h5", "h6"):
                out.append("\n\n" + "#" * int(n[1]) + " " + re.sub(r"\s+", " ", ch.get_text()).strip() + "\n")
            elif n in ("p", "div", "section", "tr", "dd", "dt", "figcaption", "blockquote"):
                out.append("\n")
                walk(ch)
                out.append("\n")
            elif n == "br":
                out.append("\n")
            elif n == "li":
                out.append("\n- ")
                walk(ch)
            elif n in ("td", "th"):
                walk(ch)
                out.append(" | ")
            elif n == "pre":
                out.append("\n```\n" + ch.get_text() + "\n```\n")
            elif n == "a":
                href = str(ch.get("href") or "")
                txt = re.sub(r"\s+", " ", ch.get_text()).strip()
                if href and txt and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    full = urljoin(canonical or url, href)
                    if full.startswith(("http://", "https://")) and len(links) < 80:
                        links.append((txt[:80], full))
                if txt:
                    out.append(txt)
            elif n == "img":
                alt = str(ch.get("alt") or "").strip()
                if alt:
                    out.append(f"[图: {alt[:80]}]")
            else:
                walk(ch)

    walk(root)
    text = "".join(out)
    text = sec.sanitize_untrusted(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if do_redact_pii:
        text = redact_pii(text)
        title = redact_pii(title)

    warnings = sec.suspicious("\n".join(hidden_snippets) + "\n" + text[:8000], risk=False)
    if hidden_snippets and any(sec.suspicious(s) for s in hidden_snippets):
        if "隐藏节点中发现可疑指令" not in warnings:
            warnings.append("隐藏节点中发现可疑指令（已自动剔除）")

    qcap = max(100, min(2000, int(quote_max_chars or 400)))
    excerpt = "" if (directives.get("noarchive") or directives.get("noai") or directives.get("nosnippet")) else text[:qcap]
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return ExtractedDoc(
        url=url,
        canonical_url=canonical,
        title=title,
        text=text,
        excerpt=excerpt,
        author=author,
        published_at=published_at,
        license=lic,
        links=links,
        tables=tables,
        directives=directives,
        hidden_removed=hidden_removed,
        injection_warnings=warnings,
        sha256=digest,
        kind="html",
    )


def extract_response(
    body: bytes,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content_type: str = "",
    charset_text: str | None = None,
    quote_max_chars: int = 400,
    do_redact_pii: bool = True,
) -> ExtractedDoc:
    ct = (content_type or (headers or {}).get("content-type", "")).split(";")[0].strip().lower()
    directives = parse_directives(headers, "")
    if ct == "application/pdf" or url.lower().split("?")[0].endswith(".pdf"):
        raw_text = _extract_pdf_text(body)
        clean = sec.sanitize_untrusted(raw_text)
        if do_redact_pii:
            clean = redact_pii(clean)
        qcap = max(100, min(2000, int(quote_max_chars or 400)))
        excerpt = "" if (directives.get("noarchive") or directives.get("noai") or directives.get("nosnippet")) else clean[:qcap]
        return ExtractedDoc(
            url=url,
            canonical_url=url,
            title="PDF 文档",
            text=clean,
            excerpt=excerpt,
            directives=directives,
            injection_warnings=sec.suspicious(clean[:8000]),
            sha256=hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest(),
            kind="pdf",
        )

    txt = charset_text if charset_text is not None else body.decode("utf-8", "replace")
    if ct in ("text/html", "application/xhtml+xml", "") or "<html" in txt[:600].lower():
        return extract_html(txt, url, headers=headers, quote_max_chars=quote_max_chars, do_redact_pii=do_redact_pii)

    clean = sec.sanitize_untrusted(txt)
    if do_redact_pii:
        clean = redact_pii(clean)
    qcap = max(100, min(2000, int(quote_max_chars or 400)))
    excerpt = "" if (directives.get("noarchive") or directives.get("noai") or directives.get("nosnippet")) else clean[:qcap]
    return ExtractedDoc(
        url=url,
        canonical_url=url,
        title="",
        text=clean,
        excerpt=excerpt,
        directives=directives,
        injection_warnings=sec.suspicious(clean[:8000]),
        sha256=hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest(),
        kind="text",
    )
