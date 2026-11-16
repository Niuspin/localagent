"""M6 UI 设计系统 v2 与合规验证：模块化 CSS、零外链/零 transition:all、WCAG AA 对比度、静态资源与体积预算。"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lagent.config import Config  # noqa: E402
from lagent.server import CSP, State, make_app  # noqa: E402
import ui_contrast  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _get(app, path: str, *, port: int = 8765, token: str = "tok-m6") -> tuple[int, dict[str, str], bytes]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"host", f"127.0.0.1:{port}".encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", port),
    }
    sent: list[dict] = []
    done = asyncio.Event()

    async def receive():
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict):
        sent.append(msg)

    try:
        await app(scope, receive, send)
    finally:
        done.set()
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), headers, payload


def test_m6_css_contrast_and_zero_transition_all():
    errs = ui_contrast.verify_all()
    assert errs == [], f"对比度或 CSS 规范失败: {errs}"


def test_m6_modular_assets_csp_and_bundle_budget(tmp_path):
    # 1. 模块化 CSS/图标/JS 文件齐备且无外部 CDN 请求
    html = (WEB_DIR / "index.html").read_text("utf-8")
    urls = [u for u in re.findall(r"https?://[^\s'\"<>]+", html) if not u.startswith("http://www.w3.org/")]
    assert not urls, f"index.html 不得包含外部 URL: {urls}"
    util_js = (WEB_DIR / "js" / "util.js").read_text("utf-8")
    for rel in (
        "css/tokens.css",
        "css/base.css",
        "css/layout.css",
        "css/components/controls.css",
        "css/components/chat.css",
        "css/components/drawer.css",
        "css/themes/light.css",
        "css/themes/dark.css",
        "css/themes/paper.css",
        "css/themes/midnight.css",
        "css/themes/hc.css",
        "css/themes/colorblind.css",
        "icons/sprite.svg",
        "js/swarm.js",
        "js/i18n/zh-CN.js",
        "js/i18n/en.js",
    ):
        assert (WEB_DIR / rel).is_file(), f"缺少静态资源 {rel}"
        if rel == "icons/sprite.svg":
            assert f"/{rel}" in util_js
        else:
            assert f"/{rel}" in html

    # 2. 首屏静态资源总大小 < 1.5 MB（§8.3 性能预算）
    total_bytes = sum(p.stat().st_size for p in WEB_DIR.rglob("*") if p.is_file())
    assert total_bytes < 1_500_000, f"web/ 资源超预算: {total_bytes} bytes"

    # 3. 支持全部 v2 主题配置
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    for theme in ("auto", "light", "dark", "paper", "midnight", "hc", "colorblind"):
        assert cfg.patch({"ui": {"theme": theme}})["ui"]["theme"] == theme

    # 4. 服务器正确提供模块化 CSS、SVG 精灵图与 JS，且维持严格 CSP 与 MIME 头
    st = State(token="tok-m6", port=8765, root=tmp_path / "srv")
    try:
        app = make_app(st)
        for path, mime_sub in (
            ("/css/tokens.css", "text/css"),
            ("/css/themes/paper.css", "text/css"),
            ("/icons/sprite.svg", "image/svg+xml"),
            ("/js/swarm.js", "javascript"),
            ("/js/i18n/en.js", "javascript"),
        ):
            code, hdr, body = asyncio.run(_get(app, path))
            assert code == 200 and len(body) > 10
            assert mime_sub in hdr.get("content-type", "").lower()
            assert hdr.get("content-security-policy") == CSP
            assert "font-src 'self'" in CSP
            assert hdr.get("x-content-type-options") == "nosniff"
    finally:
        st.artifacts.close()
        st.memory.db.close()
