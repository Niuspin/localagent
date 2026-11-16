"""M0 基础设施单元测试与路由契约验证。

覆盖范围：
1. paths.ensure_dirs / data_dir 子目录规范
2. config.Config v0.2.0 全量新增配置键、边界校验、密钥隔离、旧 config.json 自动补全
3. Session Schema v1 -> v2 自动迁移、首次写回 .bak 备份、双向迁移往返、Content Parts 规范化与降级
4. 统一事件字段 {ts, kind, run_id, agent_id, swarm_id, mission_id} 与 AuditLog 哈希链兼容
5. ArtifactStore SQLite WAL 索引、防路径穿越、配额淘汰、会话清理与 /api/artifacts 安全下载头
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lagent import context as C  # noqa: E402
from lagent import paths  # noqa: E402
from lagent import security as sec  # noqa: E402
from lagent.artifacts import ArtifactStore  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.events import EVENT_KINDS, EVENT_SCHEMA_VERSION, enrich_event  # noqa: E402
from lagent.loop import Run  # noqa: E402
from lagent.parts import content_to_parts, parts_to_content  # noqa: E402
from lagent.server import State, make_app  # noqa: E402
from lagent.sessions import (  # noqa: E402
    SESSION_SCHEMA_VERSION,
    Session,
    SessionStore,
    downgrade_v2_to_v1,
    migrate_v1_to_v2,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


async def _call_asgi(app, method: str, path: str, *, query: str = "", port: int = 8765, token: str = "tok-m0", body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [
            (b"host", f"127.0.0.1:{port}".encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ],
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
    headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), headers, payload


# ==================================================================== 1. Paths
def test_paths_ensure_dirs_creates_v2_subdirs(tmp_path):
    root = tmp_path / "home"
    assert paths.ensure_dirs(root) == []
    for sub in paths.DATA_SUBDIRS:
        assert paths.data_dir(sub, root=root).is_dir()


# ==================================================================== 2. Config
def test_config_v2_defaults_validation_and_legacy_upgrade(tmp_path):
    cfg_path = tmp_path / "config.json"
    sec_path = tmp_path / "secrets.json"
    # 模拟仅含 v0.1 字段的旧配置
    cfg_path.write_text(json.dumps({"ui": {"theme": "dark", "font_size": 16}, "search": {"max_results": 5}}), encoding="utf-8")

    cfg = Config(cfg_path, sec_path)
    pub = cfg.public()
    assert pub["ui"] == {"theme": "dark", "font_size": 16, "motion": "auto", "density": "comfortable", "code_ligatures": False}
    assert pub["search"]["max_results"] == 5
    assert pub["search"]["html_scrapers_ack"] is False
    assert pub["research"]["mode"] == "off"
    assert pub["research"]["respect_robots"] is True
    assert pub["swarm"]["max_agents"] == "auto"
    assert pub["swarm"]["worker_api_key"] is False
    assert pub["mission"]["unattended_default"] == "attended"
    assert pub["media"]["html_scripts_default"] is False
    assert pub["skills"]["default_enabled_limit"] == 14

    # worker_api_key 应隔离写入 secrets.json
    updated = cfg.patch({"swarm": {"worker_api_key": "sk-worker-123", "role_limits": {"analyst": 3, "coder": 99}}})
    assert updated["swarm"]["worker_api_key"] is True
    assert updated["swarm"]["role_limits"] == {"analyst": 3, "coder": 8}
    assert "sk-worker-123" not in cfg_path.read_text("utf-8")
    assert cfg.secret("swarm_worker_api_key") == "sk-worker-123"

    # 非法输入保持原子回滚
    snap = json.dumps(cfg.public(), sort_keys=True)
    for bad in (
        {"research": {"mode": "unrestricted"}},
        {"swarm": {"merge_strategy": "yolo"}},
        {"swarm": {"role_limits": {"Bad Role!": 2}}},
        {"ui": {"motion": "warp"}},
        {"mission": {"unattended_default": "always"}},
        {"research": {"allow_domains": "not-a-list"}},
    ):
        with pytest.raises(ValueError):
            cfg.patch(bad)
    assert json.dumps(cfg.public(), sort_keys=True) == snap

    # 清空密钥
    assert cfg.patch({"swarm": {"worker_api_key": ""}})["swarm"]["worker_api_key"] is False


# ==================================================================== 3. Sessions & Parts
def test_v1_session_migration_roundtrip_and_bak_creation(tmp_path):
    raw_bytes = (FIXTURES / "sessions" / "v1_session.json").read_bytes()
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    v1_file = sdir / "aa11bb22cc33.json"
    v1_file.write_bytes(raw_bytes)

    store = SessionStore(sdir)
    sess = store.get("aa11bb22cc33")
    assert sess is not None
    assert sess.schema_version == SESSION_SCHEMA_VERSION
    assert sess.agent_id == "main"
    assert not (sdir / "aa11bb22cc33.json.bak").exists()  # 仅读取不写入 .bak

    # UI 保留旧字段同时补齐 parts
    u0 = sess.ui[0]
    assert u0["text"] == "请检查这张截图并读取 readme.md"
    assert [p["type"] for p in u0["parts"]] == ["text", "image", "file"]

    # 首次写回创建精确 .bak，并升级磁盘版本至 v2
    sess.title = "Migrated Title"
    store.save(sess)
    bak = sdir / "aa11bb22cc33.json.bak"
    assert bak.read_bytes() == raw_bytes
    disk_v2 = json.loads(v1_file.read_text("utf-8"))
    assert disk_v2["v"] == 2 and disk_v2["schema_version"] == 2

    # 第二次写回绝不覆盖最初的 v1 .bak 快照
    sess.title = "Second Edit"
    store.save(sess)
    assert bak.read_bytes() == raw_bytes

    # 双向迁移函数校验
    v1_raw = json.loads(raw_bytes.decode("utf-8"))
    v2_doc = migrate_v1_to_v2(v1_raw)
    v1_back = downgrade_v2_to_v1(v2_doc)
    assert v1_back["v"] == 1
    assert "schema_version" not in v1_back
    assert v1_back["ui"][0]["text"] == v1_raw["ui"][0]["text"]
    assert v1_back["ui"][0]["images"] == v1_raw["ui"][0]["images"]
    assert v1_back["history"][2]["content"] == "完整输出内容..."

    # 删除会话同时清理 .json 与 .json.bak
    assert store.delete("aa11bb22cc33") is True
    assert not v1_file.exists() and not bak.exists()


def test_content_parts_degradation_and_context_projection():
    parts = [
        {"type": "text", "text": "分析季度营收"},
        {"type": "image", "url": "data:image/png;base64,AAA=", "alt": "Q1图", "artifact_id": "art_img111"},
        {"type": "table", "title": "季度表", "row_count": 24, "summary": "同比+18%", "artifact_id": "art_tbl222"},
        {"type": "chart", "title": "趋势图", "summary": "上升趋势", "artifact_id": "art_cht333"},
        {"type": "audio", "duration_s": 12.5, "transcript": "会议结论确认发布", "artifact_id": "art_aud444"},
        {"type": "artifact_ref", "kind": "report", "title": "Q1报告.pdf", "summary": "15页", "artifact_id": "art_rep555"},
    ]
    openai_vision = parts_to_content(parts, vision=True)
    assert isinstance(openai_vision, list)
    assert openai_vision[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}}
    assert any("季度表" in x.get("text", "") for x in openai_vision if x["type"] == "text")

    no_vision = parts_to_content(parts, vision=False)
    assert isinstance(no_vision, str)
    assert "[图片" in no_vision and "季度表" in no_vision and "会议结论确认发布" in no_vision

    hist = [
        {"role": "user", "parts": parts, "content": None},
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "继续"},
        {"role": "user", "content": "第三轮"},
    ]
    assert C.est_msg(hist[0], image_tokens=700) >= 700
    projected = C.project_api_messages(hist, prune_upto=3)
    assert "parts" not in projected[0]
    assert all(p.get("type") == "text" for p in projected[0]["content"])
    summary_text = C.render_for_summary(hist, 2000)
    assert "季度表" in summary_text and "趋势图" in summary_text

    # content_to_parts 兼容旧 UI 格式
    rebuilt = content_to_parts("hello", images=["data:image/png;base64,x"], files=["a.txt"])
    assert [p["type"] for p in rebuilt] == ["text", "image", "file"]


# ==================================================================== 4. Events & Audit
def test_unified_events_and_audit_compat(tmp_path):
    assert EVENT_SCHEMA_VERSION == 1 and "agent_spawned" in EVENT_KINDS
    raw = {"text": "hi"}
    ev = enrich_event("token", raw, run_id="r1", agent_id="worker-1", swarm_id="sw1", mission_id="ms1")
    assert "kind" not in raw  # 不污染调用方原字典
    assert ev == {
        "text": "hi",
        "ts": ev["ts"],
        "kind": "token",
        "run_id": "r1",
        "agent_id": "worker-1",
        "swarm_id": "sw1",
        "mission_id": "ms1",
    }

    async def run_check():
        r = Run("aa11bb22cc33", agent_id="main", swarm_id="sw_demo", mission_id="ms_demo")
        r.emit("status", {"status": "running"})
        r.emit("token", {"text": "ok"})
        assert [e[0] for e in r.events] == [1, 2]
        assert r.events[0][2]["run_id"] == r.id
        assert r.events[1][2]["swarm_id"] == "sw_demo"

    asyncio.run(run_check())

    # 旧格式日志与带新上下文字段的日志混合校验必须通过
    alog_path = tmp_path / "audit.jsonl"
    legacy_body = {"ts": 1.0, "seq": 1, "event": "legacy"}
    prev_hash = sec.AuditLog._digest("0" * 64, legacy_body)
    alog_path.write_text(json.dumps({**legacy_body, "prev": "0" * 64, "hash": prev_hash}) + "\n", encoding="utf-8")

    log = sec.AuditLog(alog_path)
    log.append(event="tool", tool="read_file", run_id="run1", agent_id="coder-1", swarm_id="sw1", parent_id="main", mission_id="m1")
    assert log.verify() == {"ok": True, "count": 2}
    tail = log.tail(5)
    assert tail[1]["agent_id"] == "coder-1" and tail[1]["parent_id"] == "main" and tail[1]["kind"] == "tool"


# ==================================================================== 5. ArtifactStore & HTTP
def test_artifact_store_crud_isolation_and_routes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    try:
        a1 = store.save_bytes(
            "aa11bb22cc33",
            b"<svg><script>alert(1)</script></svg>",
            kind="chart",
            title="危险预览测试.svg",
            filename="evil.svg",
            created_by="coder-1",
            preview={"text": "chart"},
            meta={"width": 100},
        )
        assert a1.mime == "image/svg+xml"
        assert store.resolve_path(a1).is_file()
        assert store.read_bytes(a1.id) == b"<svg><script>alert(1)</script></svg>"
        assert len(store.list("aa11bb22cc33", kind="chart")) == 1
        assert store.list("aa11bb22cc33", kind="audio") == []

        # 防御非法 session_id / artifact_id 路径穿越
        with pytest.raises(ValueError):
            store.save_bytes("../escape", b"x")
        assert store.get("../escape") is None

        # 配额淘汰与跨会话清理
        a2 = store.save_bytes("aa11bb22cc33", b"1234567890", kind="file", filename="b.txt")
        store.save_bytes("deadbeef0000", b"orphan", kind="file", filename="old.txt")
        assert store.sweep({"aa11bb22cc33"}) >= 1
        assert store.list("deadbeef0000") == []
        assert store.prune_max_bytes(15, session_id="aa11bb22cc33") >= 1
        assert store.get(a2.id) is not None or store.get(a1.id) is not None
    finally:
        store.close()

    # 真实 HTTP ASGI 路由与删除会话级联清理验证
    st = State(token="tok-m0", port=8765, root=tmp_path / "srv")
    try:
        sess = st.store.create("")
        art = st.artifacts.save_bytes(sess.id, b"<html><script>x</script></html>", kind="html", filename="report.html", title="HTML Report")
        app = make_app(st)

        code, _hdr, body = asyncio.run(_call_asgi(app, "GET", "/api/artifacts", query=f"session_id={sess.id}"))
        assert code == 200
        listing = json.loads(body)
        assert len(listing["artifacts"]) == 1 and listing["artifacts"][0]["id"] == art.id

        code, _hdr, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{art.id}/meta"))
        assert code == 200 and json.loads(body)["download_url"] == f"/api/artifacts/{art.id}"

        code, hdr, body = asyncio.run(_call_asgi(app, "GET", f"/api/artifacts/{art.id}"))
        assert code == 200
        assert hdr["x-content-type-options"] == "nosniff"
        assert hdr["content-disposition"].startswith("attachment;")
        assert b"<script>" in body

        # DELETE /api/sessions/{sid} 必须级联清理该会话 artifacts
        code, _hdr, _body = asyncio.run(_call_asgi(app, "DELETE", f"/api/sessions/{sess.id}"))
        assert code == 200
        assert st.artifacts.get(art.id) is None
    finally:
        st.artifacts.close()
        st.memory.db.close()
