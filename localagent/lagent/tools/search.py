"""联网搜索：9 个后端，可并行融合（RRF）或按序回退。

无需密钥：duckduckgo / bing / baidu / yahoo（解析公开 HTML，属“尽力而为”，页面改版或触发验证码时会报错并自动换引擎）
自建/API：searxng（强烈推荐，稳定且可聚合 Google 等）/ brave / google_cse / tavily / serper

搜索结果属于不可信内容：返回时包裹 <untrusted> 并使会话进入“污染”状态。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlsplit

from .. import net
from .base import P, Tool, ToolResult, obj, register
from .ctx import ToolCtx

Result = dict  # {title, url, snippet, engine}


def _soup(html: str):
    from bs4 import BeautifulSoup  # type: ignore
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class _Stop(Exception):
    def __init__(self, url: str):
        self.url = url


class Env:
    """一次搜索的运行环境（配置快照 + 密钥）。"""

    def __init__(self, cfg: Any):
        s = cfg.get("search")
        self.proxy: str = s.get("proxy", "")
        self.region: str = s.get("region", "")
        self.safe: str = s.get("safe", "moderate")
        self.searxng: str = s.get("searxng_url", "")
        self.timeout = float(cfg.get("web", "timeout_s", default=20))
        self.sec = cfg.secret
        self.time_range = ""

    def get(self, url: str, **kw: Any) -> net.Response:
        return net.fetch_sync(url, timeout=min(self.timeout, 20), max_bytes=1_500_000, proxy=self.proxy, **kw)


def _http_ok(r: net.Response, name: str) -> None:
    if r.status in (401, 403):
        raise net.NetError(f"{name} 拒绝访问 (HTTP {r.status})，密钥无效或被风控")
    if r.status == 429:
        raise net.NetError(f"{name} 请求过于频繁 (HTTP 429)")
    if r.status >= 400:
        raise net.NetError(f"{name} 返回 HTTP {r.status}")


def _captcha(html: str, name: str) -> None:
    if re.search(r"captcha|unusual traffic|anomaly|人机验证|安全验证|verify you are a human|robot", html[:6000], re.I):
        raise net.NetError(f"{name} 触发了人机验证，请稍后重试、配置代理或改用 searxng/API 类引擎")


# ------------------------------------------------------------------ DuckDuckGo
def ddg(q: str, env: Env, n: int) -> list[Result]:
    body = {"q": q, "b": "", "kl": env.region or "wt-wt", "kp": {"off": "-2", "moderate": "-1", "strict": "1"}[env.safe]}
    if env.time_range:
        body["df"] = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(env.time_range, "")
    r = env.get("https://html.duckduckgo.com/html/", method="POST", data=urlencode(body).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://html.duckduckgo.com/"})
    _http_ok(r, "DuckDuckGo")
    html = r.text()
    if r.status == 202:
        raise net.NetError("DuckDuckGo 返回 202（风控），请稍后重试")
    s = _soup(html)
    out = []
    for d in s.select("div.result, div.web-result"):
        a = d.select_one("a.result__a")
        if not a or "result--ad" in (d.get("class") or []):
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        elif href.startswith("//"):
            href = "https:" + href
        sn = d.select_one(".result__snippet")
        if href.startswith("http"):
            out.append({"title": _clean(a.get_text()), "url": href, "snippet": _clean(sn.get_text() if sn else ""), "engine": "duckduckgo"})
        if len(out) >= n:
            break
    if not out:
        _captcha(html, "DuckDuckGo")
    return out


# ------------------------------------------------------------------ Bing
def _bing_url(href: str) -> str:
    if "bing.com/ck/a" in href:
        u = parse_qs(urlsplit(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            b = u[2:]
            try:
                return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return href
    return href


def bing(q: str, env: Env, n: int) -> list[Result]:
    params = {"q": q, "count": str(min(n + 2, 20)), "form": "QBLH"}
    if env.region:
        params["setmkt"] = env.region if re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", env.region) else "en-US"
    params["adlt"] = {"off": "off", "moderate": "moderate", "strict": "strict"}[env.safe]
    r = env.get("https://www.bing.com/search?" + urlencode(params), headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    _http_ok(r, "Bing")
    html = r.text()
    s = _soup(html)
    out = []
    for li in s.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        sn = li.select_one(".b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, .b_snippet, p")
        href = _bing_url(a.get("href", ""))
        if href.startswith("http"):
            out.append({"title": _clean(a.get_text()), "url": href, "snippet": _clean(sn.get_text() if sn else ""), "engine": "bing"})
        if len(out) >= n:
            break
    if not out:
        _captcha(html, "Bing")
    return out


# ------------------------------------------------------------------ Yahoo
def yahoo(q: str, env: Env, n: int) -> list[Result]:
    r = env.get("https://search.yahoo.com/search?" + urlencode({"p": q, "n": str(min(n + 2, 20))}))
    _http_ok(r, "Yahoo")
    html = r.text()
    s = _soup(html)
    out = []
    for d in s.select("div.algo, div.dd.algo, div#web li div.NewsArticle"):
        a = d.select_one("h3 a, a.d-ib")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/RU=([^/]+)/R[KS]=", href)
        if m:
            href = unquote(m.group(1))
        sn = d.select_one("div.compText p, p.fc-dustygray, p")
        if href.startswith("http"):
            out.append({"title": _clean(a.get_text()), "url": href, "snippet": _clean(sn.get_text() if sn else ""), "engine": "yahoo"})
        if len(out) >= n:
            break
    if not out:
        _captcha(html, "Yahoo")
    return out


# ------------------------------------------------------------------ Baidu
def _resolve_redirect(env: Env, url: str) -> str:
    def stop(nxt: str) -> None:
        raise _Stop(nxt)
    try:
        net.fetch_sync(url, timeout=6, max_bytes=2000, proxy=env.proxy, on_redirect=stop)
    except _Stop as s:
        return s.url
    except Exception:  # noqa: BLE001
        pass
    return url


def baidu(q: str, env: Env, n: int) -> list[Result]:
    r = env.get("https://www.baidu.com/s?" + urlencode({"wd": q, "rn": str(min(n + 2, 20)), "ie": "utf-8"}),
                headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    _http_ok(r, "百度")
    html = r.text()
    s = _soup(html)
    out = []
    for d in s.select("div.result, div.c-container, div.result-op"):
        a = d.select_one("h3 a")
        if not a or not a.get("href"):
            continue
        sn = d.select_one(".c-abstract, .content-right_8Zs40, [class*=content-right], .c-span-last")
        text = _clean(sn.get_text() if sn else d.get_text())
        title = _clean(a.get_text())
        if text.startswith(title):
            text = text[len(title):].strip()
        out.append({"title": title, "url": a["href"], "snippet": text[:240], "engine": "baidu"})
        if len(out) >= n:
            break
    if not out:
        _captcha(html, "百度")
        return out
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        urls = list(ex.map(lambda x: _resolve_redirect(env, x["url"]) if "baidu.com/link" in x["url"] else x["url"], out))
    for x, u in zip(out, urls):
        x["url"] = u
    return out


# ------------------------------------------------------------------ SearXNG
def searxng(q: str, env: Env, n: int) -> list[Result]:
    base = env.searxng.rstrip("/")
    if not base:
        raise net.NetError("未配置 SearXNG 地址（设置→搜索）")
    params = {"q": q, "format": "json", "safesearch": {"off": "0", "moderate": "1", "strict": "2"}[env.safe]}
    if env.time_range:
        params["time_range"] = env.time_range
    if env.region:
        params["language"] = env.region.split("-")[0] if "-" in env.region else env.region
    local = re.match(r"https?://(localhost|127\.|\[::1\]|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", base, re.I)
    r = env.get(base + "/search?" + urlencode(params), allow_private=bool(local), headers={"Accept": "application/json"})
    _http_ok(r, "SearXNG")
    try:
        data = json.loads(r.text())
    except ValueError:
        raise net.NetError("SearXNG 未返回 JSON：请在其 settings.yml 的 search.formats 中启用 json")
    return [{"title": _clean(x.get("title")), "url": x.get("url", ""), "snippet": _clean(x.get("content")),
             "engine": "searxng" + (f"/{x['engine']}" if x.get("engine") else "")} for x in (data.get("results") or [])[:n] if x.get("url")]


# ------------------------------------------------------------------ API 类
def brave(q: str, env: Env, n: int) -> list[Result]:
    key = env.sec("brave")
    if not key:
        raise net.NetError("未设置 Brave API 密钥")
    params = {"q": q, "count": str(min(n, 20)), "safesearch": {"off": "off", "moderate": "moderate", "strict": "strict"}[env.safe]}
    if env.time_range:
        params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(env.time_range, "")
    r = env.get("https://api.search.brave.com/res/v1/web/search?" + urlencode(params),
                headers={"X-Subscription-Token": key, "Accept": "application/json"})
    _http_ok(r, "Brave")
    data = json.loads(r.text())
    return [{"title": _clean(x.get("title")), "url": x.get("url", ""), "snippet": _clean(re.sub(r"<[^>]+>", "", x.get("description", ""))),
             "engine": "brave"} for x in (data.get("web", {}).get("results") or [])[:n]]


def google_cse(q: str, env: Env, n: int) -> list[Result]:
    key, cx = env.sec("google_cse"), env.sec("google_cx")
    if not key or not cx:
        raise net.NetError("未设置 Google CSE 的 API Key 与 cx")
    params = {"key": key, "cx": cx, "q": q, "num": str(min(n, 10))}
    if env.time_range:
        params["dateRestrict"] = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}.get(env.time_range, "")
    r = env.get("https://www.googleapis.com/customsearch/v1?" + urlencode(params))
    _http_ok(r, "Google CSE")
    data = json.loads(r.text())
    return [{"title": _clean(x.get("title")), "url": x.get("link", ""), "snippet": _clean(x.get("snippet")), "engine": "google"}
            for x in (data.get("items") or [])[:n]]


def tavily(q: str, env: Env, n: int) -> list[Result]:
    key = env.sec("tavily")
    if not key:
        raise net.NetError("未设置 Tavily API 密钥")
    body = {"query": q, "max_results": min(n, 20), "search_depth": "basic"}
    if env.time_range:
        body["time_range"] = env.time_range
    r = env.get("https://api.tavily.com/search", method="POST", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    _http_ok(r, "Tavily")
    data = json.loads(r.text())
    return [{"title": _clean(x.get("title")), "url": x.get("url", ""), "snippet": _clean(x.get("content"))[:300], "engine": "tavily"}
            for x in (data.get("results") or [])[:n]]


def serper(q: str, env: Env, n: int) -> list[Result]:
    key = env.sec("serper")
    if not key:
        raise net.NetError("未设置 Serper API 密钥")
    body: dict = {"q": q, "num": min(n, 20)}
    if env.time_range:
        body["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(env.time_range, "")
    if env.region and "-" in env.region:
        body["gl"] = env.region.split("-")[-1].lower()
    r = env.get("https://google.serper.dev/search", method="POST", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-API-KEY": key})
    _http_ok(r, "Serper")
    data = json.loads(r.text())
    return [{"title": _clean(x.get("title")), "url": x.get("link", ""), "snippet": _clean(x.get("snippet")), "engine": "google"}
            for x in (data.get("organic") or [])[:n]]


PROVIDERS: dict[str, Callable[[str, Env, int], list[Result]]] = {
    "duckduckgo": ddg, "bing": bing, "baidu": baidu, "yahoo": yahoo, "searxng": searxng,
    "brave": brave, "google_cse": google_cse, "tavily": tavily, "serper": serper,
}


# ------------------------------------------------------------------ 调度与融合
def _norm_url(u: str) -> str:
    try:
        p = urlsplit(u)
    except ValueError:
        return u
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/")
    q = "&".join(sorted(x for x in p.query.split("&") if x and not x.startswith(("utm_", "fbclid", "gclid", "spm"))))
    return f"{host}{path}?{q}" if q else f"{host}{path}"


def fuse(lists: list[list[Result]], limit: int) -> list[Result]:
    """倒数排名融合：多引擎都命中的结果排前面。"""
    score: dict[str, float] = {}
    best: dict[str, Result] = {}
    for lst in lists:
        for rank, r in enumerate(lst):
            k = _norm_url(r["url"])
            score[k] = score.get(k, 0.0) + 1.0 / (60 + rank)
            if k not in best:
                best[k] = dict(r)
            else:
                if r["engine"] not in best[k]["engine"]:
                    best[k]["engine"] += "+" + r["engine"]
                if len(r.get("snippet", "")) > len(best[k].get("snippet", "")):
                    best[k]["snippet"] = r["snippet"]
    ordered = sorted(best, key=lambda k: -score[k])
    return [best[k] for k in ordered[:limit]]


async def run_search(cfg: Any, query: str, *, limit: int | None = None, providers: list[str] | None = None,
                     time_range: str = "") -> tuple[list[Result], dict[str, str]]:
    s = cfg.get("search")
    provs = providers or s.get("providers") or ["duckduckgo"]
    n = int(limit or s.get("max_results", 8))
    env = Env(cfg)
    env.time_range = time_range if time_range in ("day", "week", "month", "year") else ""
    errors: dict[str, str] = {}

    async def one(name: str) -> list[Result]:
        fn = PROVIDERS.get(name)
        if not fn:
            errors[name] = "未知引擎"
            return []
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, query, env, max(n, 5)), timeout=env.timeout + 15)
        except asyncio.TimeoutError:
            errors[name] = "超时"
        except net.NetError as e:
            errors[name] = str(e)
        except ImportError as e:
            errors[name] = f"缺少依赖 {e.name}（pip install beautifulsoup4 lxml）"
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {e}"
        return []

    if s.get("mode") == "first" and not providers:
        for name in provs:
            res = await one(name)
            if res:
                return res[:n], errors
        return [], errors
    got = await asyncio.gather(*[one(p) for p in provs])
    lists = [g for g in got if g]
    return (fuse(lists, n) if lists else []), errors


def format_results(results: list[Result]) -> str:
    return "\n\n".join(f"[{i}] {r['title']}\n    {r['url']}\n    {r['snippet'][:300]}\n    (来源: {r['engine']})"
                       for i, r in enumerate(results, 1))


async def web_search(ctx: ToolCtx, a: dict) -> ToolResult:
    q = a["query"].strip()
    if a.get("site"):
        q += f" site:{a['site'].strip()}"
    results, errors = await run_search(ctx.cfg, q, limit=a.get("max_results"), time_range=a.get("time_range", ""))
    for r in results:
        d = net.domain_of(r["url"])
        if d:
            ctx.session.user_domains.add(d)
    if not results:
        msg = "没有搜索结果。"
        if errors:
            msg += "\n引擎错误:\n" + "\n".join(f"- {k}: {v}" for k, v in errors.items())
        msg += "\n可尝试：换关键词/英文关键词；在 设置→搜索 启用更多引擎或配置代理。"
        return ToolResult(msg, error=bool(errors), meta={"results": 0})
    txt = format_results(results)
    if errors:
        txt += "\n\n(部分引擎失败: " + "; ".join(f"{k}: {v[:60]}" for k, v in errors.items()) + ")"
    txt += "\n\n需要细节时，用 web_fetch 读取上面某个网址的正文。"
    return ToolResult(txt, meta={"results": len(results)}, taint="网页搜索结果", untrusted_source=f"web_search:{q[:80]}")


register(Tool(
    "web_search", "联网搜索，返回标题/网址/摘要。会同时查询用户配置的多个搜索引擎并融合排序。查最新信息、事实核对、找资料时使用；之后用 web_fetch 读正文。",
    obj({"query": P("string", "搜索关键词（简洁，必要时用英文）"), "max_results": P("integer", "结果数，默认按设置"),
         "site": P("string", "限定站点，如 github.com"),
         "time_range": P("string", "时间范围", enum=["day", "week", "month", "year"])}, ["query"]),
    web_search, "net_search", group="web", title=lambda a: f"搜索「{a.get('query', '')}」"))
