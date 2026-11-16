"""M4 产物与多模态输出单元测试与集成测试（§4.4 / §8.2）。"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lagent import context as C  # noqa: E402
from lagent import security as sec  # noqa: E402
from lagent.artifacts import ArtifactStore  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.journal import Journal  # noqa: E402
from lagent.loop import Agent  # noqa: E402, F401  (触发 tools 注册)
from lagent.media.artifacts import (  # noqa: E402
    build_artifact_preview,
    detect_tts,
    export_session,
    make_chart_svg,
    sanitize_svg_for_preview,
    synthesize_tts_optional,
)
from lagent.parts import part_summary  # noqa: E402
from lagent.server import State, make_app  # noqa: E402
from lagent.tools.base import TOOLS  # noqa: E402
from lagent.tools.ctx import SessionState, ToolCtx  # noqa: E402


async def _call_asgi(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    port: int = 8765,
    token: str | None = "tok-m4",
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", f"127.0.0.1:{port}".encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if body:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", port),
    }
    sent: list[dict] = []
    more = True
    done = asyncio.Event()

    async def receive():
        nonlocal more
        if more:
            more = False
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict):
        sent.append(msg)

    try:
        await app(scope, receive, send)
    finally:
        done.set()
    start = next(m for m in sent if m["type"] == "http.response.start")
    res_hdrs = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), res_hdrs, payload


# ==================================================================== 1. test_chart
def test_chart_svg_xml_cjk_and_dark_theme(tmp_path):
    # 1. 五类图表均产出合法 XML SVG 且保留 CJK 中文字符与主题 CSS 变量
    specs = [
        (
            "bar",
            {
                "title": "季度营收对比（& 研发投入）",
                "subtitle": "华东 / 华南大区",
                "labels": ["一季度", "二季度", "三季度", "四季度"],
                "series": [{"name": "华东", "values": [120, 150, 180, 210]}, {"name": "华南", "values": [95, 110, 145, 175]}],
                "x_label": "季度",
                "y_label": "万元",
                "theme": "auto",
            },
        ),
        (
            "line",
            {
                "title": "延迟 P99 趋势",
                "labels": ["T1", "T2", "T3", "T4"],
                "values": [42.5, 38.0, 29.3, 21.8],
                "theme": "dark",
            },
        ),
        (
            "pie",
            {
                "title": "语言占比分布",
                "labels": ["Python", "JavaScript", "其他"],
                "values": [60, 30, 10],
                "theme": "light",
            },
        ),
        (
            "scatter",
            {
                "title": "响应耗时散点",
                "points": [[10, 120, "小请求"], [50, 340, "中请求"], [90, 580, "重请求"]],
                "theme": "auto",
            },
        ),
        (
            "heatmap",
            {
                "title": "模块耦合热力矩阵",
                "x_labels": ["引擎", "集群", "产物"],
                "y_labels": ["上午", "下午"],
                "matrix": [[12, 34, 8], [45, 18, 29]],
                "theme": "dark",
            },
        ),
    ]
    for kind, kw in specs:
        svg_txt, stats = make_chart_svg(kind, **kw)
        root = ET.fromstring(svg_txt)
        assert root.tag.endswith("svg")
        assert stats["chart_type"] == kind
        parsed_title = root.findtext("{http://www.w3.org/2000/svg}title")
        assert parsed_title == kw["title"]
        assert "--chart-bg" in svg_txt and "--chart-fg" in svg_txt
        if kw["theme"] == "dark":
            assert "#14171D" in svg_txt and "#E6E9EF" in svg_txt
        elif kw["theme"] == "auto":
            assert "prefers-color-scheme: dark" in svg_txt
            assert "data-theme='dark'" in svg_txt

    with pytest.raises(ValueError):
        make_chart_svg("unknown_kind", values=[1, 2])
    with pytest.raises(ValueError):
        make_chart_svg("pie", labels=["a"], values=[0])

    # 2. make_chart 工具落盘工作区 + 自动登记 ArtifactStore + 推送事件
    ws = tmp_path / "ws"
    ws.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    emitted: list[tuple[str, dict]] = []
    ctx = ToolCtx(
        cfg=Config(tmp_path / "c.json", tmp_path / "s.json"),
        guard=sec.PathGuard(ws),
        journal=Journal(tmp_path / "journal"),
        session=SessionState("s_chart1", agent_id="coder-1"),
        artifacts=store,
        emit=lambda ev, d: emitted.append((ev, d)),
    )
    try:
        res = asyncio.run(TOOLS["make_chart"].fn(ctx, {
            "chart_type": "bar",
            "title": "测试中文柱图",
            "labels": ["甲", "乙", "丙"],
            "values": [10, 25, 18],
            "theme": "dark",
            "path": "charts/demo.svg",
        }))
        assert not res.error
        assert (ws / "charts" / "demo.svg").is_file()
        assert "artifact" in res.meta
        art_d = res.meta["artifact"]
        assert art_d["kind"] == "chart" and art_d["mime"] == "image/svg+xml"
        assert any(ev == "artifact_created" and d["id"] == art_d["id"] for ev, d in emitted)
        ET.fromstring((ws / "charts" / "demo.svg").read_text("utf-8"))
    finally:
        store.close()


# ==================================================================== 2. test_artifacts (preview/bundle/tools/parts)
def test_artifacts_preview_sandbox_bundle_and_auto_registration(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("API_KEY=secret123\n", encoding="utf-8")

    st = State(token="tok-m4", port=8765, root=tmp_path / "srv")
    try:
        sess = st.store.create(str(ws))
        other_sess = st.store.create(str(ws))
        emitted: list[tuple[str, dict]] = []
        ctx = ToolCtx(
            cfg=st.cfg,
            guard=sec.PathGuard(ws),
            journal=st.journal,
            session=SessionState(sess.id),
            audit=st.audit,
            artifacts=st.artifacts,
            emit=lambda ev, d: emitted.append((ev, d)),
        )

        # A. 现有工具（write_document docx+pdf, write_spreadsheet xlsx+csv, write_presentation pptx, write_file html/md）自动登记产物
        r_pdf = asyncio.run(TOOLS["write_document"].fn(ctx, {
            "path": "reports/q1.pdf",
            "title": "Q1 财报摘要",
            "content": "# 概览\n\n第一季度收入同比增长 **25%**。\n\n## 下一步\n\n- 扩展本地能力\n- 补充图表",
        }))
        r_xlsx = asyncio.run(TOOLS["write_spreadsheet"].fn(ctx, {
            "path": "data/sales.xlsx",
            "sheets": [
                {"name": "Summary", "rows": [["区域", "销售额"], ["华东", 120], ["华南", 95], ["华北", 80]]},
                {"name": "Detail", "rows": [["日期", "数值"], ["2026-01", 40]]},
            ],
        }))
        r_pptx = asyncio.run(TOOLS["write_presentation"].fn(ctx, {
            "path": "slides/deck.pptx",
            "title": "项目汇报",
            "slides": [{"title": "总结", "bullets": ["达成里程碑", "零外部请求"], "notes": "发言重点"}],
        }))
        r_html = asyncio.run(TOOLS["write_file"].fn(ctx, {
            "path": "web_out/vis.html",
            "content": "<html><body><h1>Demo</h1><script>window.__ran=1;</script></body></html>",
        }))
        for rr in (r_pdf, r_xlsx, r_pptx, r_html):
            assert not rr.error and "artifact" in rr.meta

        # SVG 危险预览剥离 <script> / on* / javascript:
        evil_svg = st.artifacts.save_bytes(
            sess.id,
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="10" height="10" onclick="alert(2)"/><a href="javascript:alert(3)"><text>ok</text></a></svg>',
            kind="chart",
            filename="evil.svg",
        )
        clean_svg = sanitize_svg_for_preview(st.artifacts.read_bytes(evil_svg.id).decode("utf-8"))
        assert "<script" not in clean_svg.lower() and "onclick" not in clean_svg.lower() and "javascript:" not in clean_svg.lower()

        app = make_app(st)

        # B. HTML 预览默认禁脚本，仅在逐产物授权后允许 allow-scripts（但仍禁网 & 同源隔离）
        html_aid = r_html.meta["artifact"]["id"]
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{html_aid}/preview"))
        pv_html = json.loads(body)
        assert code == 200 and pv_html["preview_kind"] == "html"
        assert pv_html["scripts_authorized"] is False and pv_html["sandbox"] == ""

        code, hdrs, raw_html = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{html_aid}/preview", query="raw=1"))
        assert code == 200
        assert hdrs["x-frame-options"] == "SAMEORIGIN"
        assert hdrs["x-content-type-options"] == "nosniff"
        assert "script-src" not in hdrs["content-security-policy"]
        assert "connect-src 'none'" in hdrs["content-security-policy"]

        code, _, body = asyncio.run(_call_asgi(
            app, "POST", f"/api/artifacts/{html_aid}/authorize-scripts", body=json.dumps({"enabled": True}).encode()
        ))
        assert code == 200 and json.loads(body)["meta"]["scripts_authorized"] is True

        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{html_aid}/preview"))
        assert json.loads(body)["sandbox"] == "allow-scripts"
        code, hdrs, _ = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{html_aid}/preview", query="raw=1"))
        assert "script-src 'unsafe-inline'" in hdrs["content-security-policy"]
        assert "connect-src 'none'" in hdrs["content-security-policy"]

        # C. PDF / Excel 分页 / PPTX / SVG 安全预览路由
        pdf_aid = r_pdf.meta["artifact"]["id"]
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{pdf_aid}/preview", query="page=1"))
        pv_pdf = json.loads(body)
        assert code == 200 and pv_pdf["preview_kind"] == "pdf" and pv_pdf["page_count"] >= 1
        assert pv_pdf["data_url"].startswith("data:image/png;base64,")
        assert "25%" in pv_pdf["text"] or "Q1" in pv_pdf["text"]

        xlsx_aid = r_xlsx.meta["artifact"]["id"]
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{xlsx_aid}/preview", query="sheet=0&offset=0&limit=2"))
        pv_tbl = json.loads(body)
        assert code == 200 and pv_tbl["preview_kind"] == "table"
        assert pv_tbl["sheets"] == ["Summary", "Detail"] and len(pv_tbl["rows"]) == 2 and pv_tbl["truncated"] is True

        pptx_aid = r_pptx.meta["artifact"]["id"]
        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{pptx_aid}/preview"))
        pv_ppt = json.loads(body)
        assert code == 200 and pv_ppt["preview_kind"] == "slides" and pv_ppt["slides"][0]["notes"] == "发言重点"

        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{evil_svg.id}/preview"))
        assert "<script" not in json.loads(body)["svg"].lower()

        # D. Bundle 打包 + 跨会话隔离 + 敏感文件拒绝 + Open 通道安全检查
        other_art = st.artifacts.save_bytes(other_sess.id, b"other", kind="file", filename="other.txt")
        code, _, _ = asyncio.run(_call_asgi(
            app, "POST", "/api/artifacts/bundle",
            body=json.dumps({"session_id": sess.id, "artifact_ids": [other_art.id]}).encode(),
        ))
        assert code == 409

        r_bad_bundle = asyncio.run(TOOLS["bundle_artifacts"].fn(ctx, {"files": [".env"]}))
        assert r_bad_bundle.error and "敏感" in r_bad_bundle.content

        code, _, body = asyncio.run(_call_asgi(
            app, "POST", "/api/artifacts/bundle",
            body=json.dumps({"session_id": sess.id, "title": "Sprint Deliverables"}).encode(),
        ))
        assert code == 200
        bun = json.loads(body)
        assert bun["kind"] == "bundle" and bun["mime"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(st.artifacts.read_bytes(bun["id"])), "r") as zf:
            manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
            assert manifest["session_id"] == sess.id and manifest["count"] >= 4

        code, _, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{bun['id']}/preview"))
        pv_bun = json.loads(body)
        assert code == 200 and pv_bun["preview_kind"] == "archive" and pv_bun["manifest"]["count"] >= 4

        # Open 通道拒绝可执行脚本，安全类型放行
        ps1_art = st.artifacts.save_bytes(sess.id, b"Write-Host hi", kind="file", filename="bad.ps1")
        code, _, _ = asyncio.run(_call_asgi(app, "POST", f"/api/artifacts/{ps1_art.id}/open"))
        assert code == 400
        opened: list[Path] = []
        monkeypatch.setattr("lagent.appwindow.open_local_file", lambda p: opened.append(p) or True)
        code, _, _ = asyncio.run(_call_asgi(app, "POST", f"/api/artifacts/{pdf_aid}/open"))
        assert code == 200 and len(opened) == 1

        # E. Parts 表格句柄降级预算 & TTS 优雅降级
        tbl_part = {"type": "table", "title": "Sales", "row_count": 5000, "summary": "Top=华东", "artifact_id": xlsx_aid, "handle_id": "out_123"}
        sum_txt = part_summary(tbl_part)
        assert "handle=out_123" in sum_txt and "artifact=" + xlsx_aid in sum_txt
        assert C.est_msg({"role": "assistant", "parts": [tbl_part]}) < 200

        tts_info = detect_tts(st.cfg)
        assert tts_info["enabled"] is False
        assert synthesize_tts_optional("你好", cfg=st.cfg)["ok"] is False
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ==================================================================== 3. test_session_export (md / html / pdf)
def test_session_export_md_html_pdf_and_routes(tmp_path):
    st = State(token="tok-m4", port=8765, root=tmp_path / "srv")
    try:
        sess = st.store.create("")
        sess.title = "M4 架构与报表导出测试"
        sess.ui = [
            {"role": "user", "text": "请分析营收并警惕 <script>alert(9)</script>", "files": ["data/sales.csv"]},
            {
                "role": "assistant",
                "status": "done",
                "blocks": [
                    {"type": "thinking", "text": "先确认表格结构与季度维度"},
                    {"type": "tool", "name": "make_chart", "title": "生成图表 季度营收", "status": "ok",
                     "args": {"chart_type": "bar"}, "content": "已生成 bar SVG",
                     "meta": {"artifact": {"id": "art_demo123", "kind": "chart", "title": "季度营收.svg"}}},
                    {"type": "text", "text": "已完成分析并生成图表。"},
                ],
            },
        ]
        st.store.save(sess)
        art = st.artifacts.save_bytes(sess.id, b"<svg/>", kind="chart", filename="rev.svg", title="营收.svg")
        app = make_app(st)

        # 1. Markdown（包含 vs 排除思考/工具）
        code, hdrs, md_full = asyncio.run(_call_asgi(app, "GET", f"/api/sessions/{sess.id}/export", query="format=md&thinking=1&tools=1"))
        txt_full = md_full.decode("utf-8")
        assert code == 200 and "text/markdown" in hdrs["content-type"]
        assert "先确认表格结构" in txt_full and "生成图表 季度营收" in txt_full and "art_demo123" in txt_full and art.id in txt_full

        code, _, md_clean = asyncio.run(_call_asgi(app, "POST", f"/api/sessions/{sess.id}/export", body=json.dumps({"format": "md", "include_thinking": False, "include_tools": False}).encode()))
        txt_clean = md_clean.decode("utf-8")
        assert code == 200 and "先确认表格结构" not in txt_clean and "工具:" not in txt_clean and "已完成分析" in txt_clean

        # 2. 自包含 HTML（零外链、无真实 script 标签、转义用户输入、暗色媒体查询）
        code, hdrs, html_bytes = asyncio.run(_call_asgi(app, "POST", f"/api/sessions/{sess.id}/export", query="format=html"))
        html_txt = html_bytes.decode("utf-8")
        assert code == 200 and "text/html" in hdrs["content-type"] and hdrs["x-content-type-options"] == "nosniff"
        assert "&lt;script&gt;alert(9)&lt;/script&gt;" in html_txt
        assert "<script>" not in html_txt and "https://" not in html_txt
        assert "prefers-color-scheme: dark" in html_txt

        # 3. CJK PDF 导出 & save_artifact
        code, hdrs, pdf_bytes = asyncio.run(_call_asgi(app, "POST", f"/api/sessions/{sess.id}/export", query="format=pdf"))
        assert code == 200 and hdrs["content-type"] == "application/pdf"
        assert pdf_bytes.startswith(b"%PDF-")

        code, _, saved_json = asyncio.run(_call_asgi(
            app, "POST", f"/api/sessions/{sess.id}/export",
            body=json.dumps({"format": "html", "save_artifact": True}).encode(),
        ))
        assert code == 200 and json.loads(saved_json)["kind"] == "export"

        # export_session 未知格式拒绝
        with pytest.raises(ValueError):
            export_session(sess, "docx")
    finally:
        st.artifacts.close()
        st.memory.db.close()


# ==================================================================== 4. Agent E2E: toolset 自动识别 + make_chart + UI meta + Run SSE
def test_m4_agent_toolset_and_artifact_event_e2e(tmp_path):
    from lagent import toolset

    assert "artifacts" in toolset.detect("请给我画一个柱状图并把产物打包")

    ws = tmp_path / "e2e_ws"
    ws.mkdir()
    st = State(token="tok-m4e", port=8766, root=tmp_path / "srv")
    try:
        st.cfg.set("agent", "permission_mode", "auto_edit")
        seen_tools: list[list[str]] = []

        async def fake_ensure_ready():
            st.engine.status = "ready"

        async def fake_chat_stream(body):
            seen_tools.append([t["function"]["name"] for t in (body.get("tools") or [])])
            msgs = body.get("messages") or []
            tmsgs = [m for m in msgs if m.get("role") == "tool"]
            if not tmsgs:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "tc_chart_1",
                                "function": {
                                    "name": "make_chart",
                                    "arguments": json.dumps({
                                        "chart_type": "line",
                                        "title": "端到端延迟",
                                        "labels": ["A", "B"],
                                        "values": [12, 8],
                                        "path": "charts/e2e.svg",
                                    }),
                                },
                            }]
                        },
                        "finish_reason": None,
                    }]
                }
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 40}}
            else:
                yield {"choices": [{"delta": {"content": "已生成折线图产物。"}, "finish_reason": "stop"}], "usage": {"total_tokens": 20}}

        st.engine.status = "ready"
        st.engine.ctx = 4096
        st.engine.vision = False
        st.engine.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
        st.engine.chat_stream = fake_chat_stream  # type: ignore[method-assign]

        async def run_chat():
            sess = st.store.create(str(ws))
            run = st.agent.start_run(sess, "画一张延迟折线图", [], [], [])
            await run.task
            return sess, run

        sess, run = asyncio.run(run_chat())
        assert seen_tools and "make_chart" in seen_tools[0] and "bundle_artifacts" in seen_tools[0]
        assert (ws / "charts" / "e2e.svg").is_file()
        assert any(ev == "artifact_created" and d.get("kind") == "chart" for _, ev, d in run.events)
        tool_blks = [b for b in sess.ui[-1]["blocks"] if b.get("type") == "tool"]
        assert tool_blks and tool_blks[0]["meta"]["artifact"]["kind"] == "chart"
        arts = st.artifacts.list(sess.id)
        assert len(arts) == 1 and arts[0].title == "端到端延迟"
    finally:
        st.artifacts.close()
        st.memory.db.close()
