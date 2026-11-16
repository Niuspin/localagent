"""M3 多模态输入摄取、能力协商与受限子进程隔离测试（§4.4）。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lagent import security as sec  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.journal import Journal  # noqa: E402
from lagent.media import (  # noqa: E402
    detect_capabilities,
    explain_ingest_plan,
    ingest_file,
    run_in_restricted_subprocess,
    sniff_magic,
)
from lagent.outputs import OutputStore  # noqa: E402
from lagent.server import State, make_app  # noqa: E402
from lagent.sessions import Session  # noqa: E402
from lagent.tools.base import TOOLS  # noqa: E402
from lagent.tools.ctx import SessionState, ToolCtx  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "media"


async def _call_asgi(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    port: int = 8765,
    token: str | None = "tok-m3",
    body: bytes = b"",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", f"127.0.0.1:{port}".encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if extra_headers:
        headers.extend(extra_headers)
    elif body:
        headers.append((b"content-type", b"application/json"))
    if body:
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

    async def receive():
        nonlocal more
        if more:
            more = False
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg: dict):
        sent.append(msg)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    res_hdrs = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), res_hdrs, payload


def _multipart(filename: str, content: bytes, content_type: str = "application/octet-stream") -> tuple[bytes, str]:
    boundary = "----LaM3TestBoundary9876"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


# ==================================================================== 1. 魔数嗅探与伪装防御
def test_magic_sniffing_and_spoof_resistance(tmp_path):
    # PNG 改名为 .txt 依然识别为 image/png
    png_fake = tmp_path / "disguised.txt"
    png_fake.write_bytes((FIX / "tall_screenshot.png").read_bytes()[:4096])
    k, m = sniff_magic(png_fake.read_bytes(), png_fake.name, path=png_fake)
    assert (k, m) == ("image", "image/png")

    # HTML 改名为 .png 识别为 html，绝不交给图片库解析
    html_raw = (FIX / "spoofed_html.png").read_bytes()
    assert sniff_magic(html_raw, "spoofed_html.png", path=FIX / "spoofed_html.png") == ("html", "text/html")

    # docx 改名为 .zip 仍嗅探内部特征为 docx
    docx_zip = tmp_path / "disguised.zip"
    docx_zip.write_bytes((FIX / "sample_notes.docx").read_bytes())
    assert sniff_magic(docx_zip.read_bytes()[:2048], docx_zip.name, path=docx_zip)[0] == "docx"

    # 未知随机二进制假装 .png -> binary
    bad = tmp_path / "random.png"
    bad.write_bytes(b"\x00\x01\x02\x03\x99\xaa\xbb\xcc")
    assert sniff_magic(bad.read_bytes(), bad.name, path=bad) == ("binary", "application/octet-stream")


# ==================================================================== 2. 受限摄取黄金样本 & 炸弹防御
def test_media_ingest_golden_and_bombs(tmp_path):
    caps_vis = {"vision": True, "ocr": "vlm", "pdf_render": "pypdfium2", "pillow": True, "pdf_text": True}
    caps_no_vis = {"vision": False, "ocr": "none", "pdf_render": "pypdfium2", "pillow": True, "pdf_text": True}

    # 1) 图片 EXIF/GPS 剥离 & 长截图分块
    r_exif = ingest_file(FIX / "exif_gps_sample.jpg", caps=caps_vis)
    assert r_exif.ok and r_exif.tainted
    assert r_exif.meta["exif_stripped"] is True and r_exif.meta["gps_present"] is True
    assert "exif_gps_stripped" in r_exif.warnings
    b64 = r_exif.images[0]["data_url"].split(",", 1)[1]
    clean_im = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert len(clean_im.getexif()) == 0

    r_tall = ingest_file(FIX / "tall_screenshot.png", caps=caps_vis, options={"resolution": "low"})
    assert r_tall.ok and 2 <= len(r_tall.images) <= 6

    # 2) 像素炸弹拒收且主进程存活
    r_pb = ingest_file(FIX / "pixel_bomb.png", caps=caps_vis)
    assert not r_pb.ok and ("像素" in (r_pb.error or "") or "pixel" in (r_pb.error or "").lower())

    # 3) PDF：JS 剥离提示 + 扫描页 pypdfium2 渲染 + 畸形 PDF 隔离
    r_js = ingest_file(FIX / "js_active.pdf", caps=caps_vis)
    assert r_js.ok and r_js.meta["js_stripped"] is True
    assert "pdf_active_content_stripped" in r_js.warnings
    assert "Normal PDF Text" in r_js.render_summary()

    r_scan = ingest_file(FIX / "scanned_empty.pdf", caps=caps_vis)
    assert r_scan.ok and len(r_scan.images) == 1

    r_bad_pdf = ingest_file(FIX / "malformed.pdf", caps=caps_vis)
    assert not r_bad_pdf.ok

    # 4) Office：docx 表格/批注/内嵌图、xlsm 宏与外链剥离、pptx 演讲备注、畸形 docx
    r_docx = ingest_file(FIX / "sample_notes.docx", caps=caps_vis)
    assert r_docx.ok and len(r_docx.tables) == 1 and len(r_docx.images) == 1
    summ_docx = r_docx.render_summary()
    assert "批注" in summ_docx and "剥离外部链接" in summ_docx

    r_xlsm = ingest_file(FIX / "macro_links.xlsm", caps=caps_vis)
    assert r_xlsm.ok and r_xlsm.meta["macro_stripped"] is True and r_xlsm.meta["external_links_stripped"] is True
    assert "office_macros_stripped" in r_xlsm.warnings
    assert "media-worker" in r_xlsm.render_summary()

    r_pptx = ingest_file(FIX / "notes_slides.pptx", caps=caps_vis)
    assert r_pptx.ok and "isolated worker subprocess" in r_pptx.render_summary()

    r_bad_docx = ingest_file(FIX / "malformed.docx", caps=caps_vis)
    assert not r_bad_docx.ok

    # 5) ipynb 剥离大二进制 & csv schema 推断
    r_nb = ingest_file(FIX / "sample.ipynb", caps=caps_vis)
    assert r_nb.ok and "stripped_1_binary_outputs" in r_nb.warnings
    assert "AAAA" * 20 not in r_nb.render_full_text()

    r_csv = ingest_file(FIX / "sample.csv", caps=caps_vis)
    assert r_csv.ok and "int" in r_csv.render_summary() and "float" in r_csv.render_summary() and "bool" in r_csv.render_summary()

    # 6) Zip Bomb / Zip Slip 拒收；正常压缩包仅列目录且支持按需选读成员
    r_zb = ingest_file(FIX / "zip_bomb.zip", cfg={"max_archive_mb": 16})
    assert not r_zb.ok

    r_zs = ingest_file(FIX / "zip_slip.zip")
    assert not r_zs.ok

    good_zip = tmp_path / "bundle.zip"
    with zipfile.ZipFile(good_zip, "w") as z:
        z.writestr("docs/readme.txt", "safe archive note")
        z.writestr("src/app.py", "print(1)")
    r_zip = ingest_file(good_zip, options={"select_member": "docs/readme.txt"})
    assert r_zip.ok and "archive_list_only_no_auto_extract" in r_zip.warnings
    assert "safe archive note" in r_zip.render_summary()
    assert not (tmp_path / "docs" / "readme.txt").exists()  # 绝不自动解压

    # 7) 音视频 & 缺省可选依赖优雅降级
    r_wav = ingest_file(FIX / "sample_tone.wav", caps=caps_no_vis)
    assert r_wav.ok and r_wav.meta["duration_s"] == pytest.approx(0.2, abs=0.05)
    assert "audio_transcription_unavailable" in r_wav.warnings

    r_vid = ingest_file(FIX / "corrupt_video.mp4", caps=caps_no_vis)
    assert r_vid.ok and "ffmpeg_unavailable" in r_vid.warnings


# ==================================================================== 3. 受限子进程超时回收与崩溃隔离
def test_subprocess_timeout_and_crash_isolation(tmp_path):
    # 生成一个阻塞休眠的临时测试模块与一个立即崩溃的模块
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "hang_worker.py").write_text("import time; time.sleep(10)", encoding="utf-8")
    (mod_dir / "crash_worker.py").write_text("import os; os._exit(7)", encoding="utf-8")
    target = FIX / "sample.csv"

    r_to = run_in_restricted_subprocess(target, timeout_s=0.4, worker_module="hang_worker", extra_pythonpath=str(mod_dir))
    assert not r_to.ok and "超时" in (r_to.error or "")

    r_cr = run_in_restricted_subprocess(target, timeout_s=3.0, worker_module="crash_worker", extra_pythonpath=str(mod_dir))
    assert not r_cr.ok and "code=7" in (r_cr.error or "")


# ==================================================================== 4. 能力协商与处理计划说明
def test_capabilities_and_plan_negotiation(tmp_path):
    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    eng_vis = SimpleNamespace(vision=True, audio=False, bin_dir=None)
    c1 = detect_capabilities(eng_vis, cfg)
    assert c1["vision"] is True and c1["ocr"] == "vlm"
    assert explain_ingest_plan("shot.png", "image/png", c1)["handler"] == "vlm"
    assert explain_ingest_plan("voice.wav", "audio/wav", c1)["supported"] is False

    eng_aud = SimpleNamespace(vision=False, audio=True, bin_dir=None)
    c2 = detect_capabilities(eng_aud, cfg)
    assert c2["audio"] is True
    assert explain_ingest_plan("voice.wav", "audio/wav", c2)["handler"] == "audio"
    assert explain_ingest_plan("pic.jpg", "image/jpeg", c2)["supported"] is False


# ==================================================================== 5. 工具集成 / taint / OutputStore / HTTP 接口
def test_tools_taint_offload_and_http_routes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # 复制黄金 docx 与一个超大 txt-like ipynb 到工作区
    (ws / "notes.docx").write_bytes((FIX / "sample_notes.docx").read_bytes())
    huge_nb = ws / "big.ipynb"
    huge_nb.write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 5,
        "cells": [{"cell_type": "code", "source": ["x = '" + ("A" * 2000) + "'"], "outputs": []} for _ in range(12)],
    }), encoding="utf-8")

    cfg = Config(tmp_path / "c.json", tmp_path / "s.json")
    st_mem = SessionState("s1", tainted=False, nonce="n1", todos=[], user_domains=set(), allow_keys=set())
    out_store = OutputStore(tmp_path / "outputs")
    ctx = ToolCtx(
        cfg=cfg,
        guard=sec.PathGuard(ws),
        journal=Journal(tmp_path / "journal"),
        session=st_mem,
        audit=sec.AuditLog(tmp_path / "audit.jsonl"),
        vision=True,
        memory=None,
        skills=None,
        outputs=out_store,
    )

    tr_docx = asyncio.run(TOOLS["read_file"].fn(ctx, {"path": "notes.docx"}))
    assert not tr_docx.error and tr_docx.taint and "批注" in tr_docx.content
    assert len(tr_docx.images) == 1

    tr_nb = asyncio.run(TOOLS["ingest_media"].fn(ctx, {"path": "big.ipynb"}))
    assert not tr_nb.error and tr_nb.taint
    assert "句柄" in tr_nb.content  # 超长提取已自动落盘到 OutputStore

    # HTTP 路由 + Guard 鉴权 + 上传安全
    st = State(token="tok-m3", port=8765, root=tmp_path / "srv")
    try:
        sess = st.store.create(str(ws))
        app = make_app(st)

        # 未鉴权拒绝
        code, _, _ = asyncio.run(_call_asgi(app, "GET", "/api/capabilities", token=None))
        assert code == 401

        code, _, body = asyncio.run(_call_asgi(app, "GET", "/api/capabilities", query="filename=report.pdf"))
        caps = json.loads(body)
        assert code == 200 and caps["plan"]["handler"] in ("pdf_render", "pdf_text")

        code, _, body = asyncio.run(_call_asgi(app, "GET", "/api/state", query=f"session_id={sess.id}"))
        assert code == 200 and "capabilities" in json.loads(body)

        # 上传正常 docx 触发 magic sniff 与预览
        mp_body, mp_ct = _multipart("report.docx", (FIX / "sample_notes.docx").read_bytes())
        code, _, body = asyncio.run(_call_asgi(
            app, "POST", f"/api/sessions/{sess.id}/upload",
            body=mp_body, extra_headers=[(b"content-type", mp_ct.encode())],
        ))
        assert code == 200
        up_json = json.loads(body)
        assert up_json["kind"] == "docx" and up_json["plan"]["handler"] == "office"

        # 上传 zip bomb 必须拦截且不残留文件
        zb_body, zb_ct = _multipart("evil.zip", (FIX / "zip_bomb.zip").read_bytes())
        code, _, _ = asyncio.run(_call_asgi(
            app, "POST", f"/api/sessions/{sess.id}/upload",
            body=zb_body, extra_headers=[(b"content-type", zb_ct.encode())],
        ))
        assert code == 400
        assert not (ws / "uploads" / "evil.zip").exists()

        # POST /api/media/ingest 应打上 taint 并阻止路径穿越
        code, _, body = asyncio.run(_call_asgi(
            app, "POST", "/api/media/ingest",
            body=json.dumps({"session_id": sess.id, "path": "notes.docx"}).encode(),
        ))
        assert code == 200 and json.loads(body)["tainted"] is True
        assert st.store.get(sess.id).tainted is True

        code, _, _ = asyncio.run(_call_asgi(
            app, "POST", "/api/media/ingest",
            body=json.dumps({"session_id": sess.id, "path": "../../etc/passwd"}).encode(),
        ))
        assert code >= 400

        # Agent._append_user 自动摄取媒体附件 + 标记 tainted + 传递内嵌图片
        from lagent.loop import Run
        st.engine.vision = True

        async def _check_append():
            run_obj = Run(sess.id)
            st_user = SessionState(sess.id, tainted=False, nonce="n9", todos=[], user_domains=set(), allow_keys=set())
            ctx_user = st.agent._make_ctx(run_obj, sess, st_user, str(ws))
            await st.agent._append_user(run_obj, sess, ctx_user, "分析此文档", [], ["notes.docx"], [])
            assert st_user.tainted is True and sess.tainted is True

        asyncio.run(_check_append())
        last_msg = sess.history[-1]["content"]
        assert isinstance(last_msg, list)
        assert any(p.get("type") == "text" and "<untrusted" in p.get("text", "") and "批注" in p.get("text", "") for p in last_msg)
        assert any(p.get("type") == "image_url" for p in last_msg)
    finally:
        st.artifacts.close()
        st.memory.db.close()
