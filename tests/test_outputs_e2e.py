"""端到端：大输出落盘 → 工具自动可见 → 按句柄取回 → 删会话清理 → 开关关闭（真实服务进程 + 假 llama-server）。"""
from __future__ import annotations

import json

from conftest import mock_log
from test_e2e import end_status, ev, new_session


def _cfg(api, **agent):
    api.patch("/api/config", {"agent": {"permission_mode": "ask", "offload_outputs": True, **agent}})


def _history_handles(home, sid):
    disk = json.loads(next((home / "data" / "sessions").glob(f"{sid}.json")).read_text("utf-8"))
    return [m["_out"] for m in disk["history"] if m.get("role") == "tool" and m.get("_out")]


def test_big_output_is_offloaded_and_retrievable(ready, home):
    _cfg(ready)
    sid = new_session(ready)
    evs = ready.chat(sid, "请产生大输出")
    assert end_status(evs) == "done"
    assert ev(evs, "tool_result")[0]["status"] == "ok"
    handles = _history_handles(home, sid)
    assert len(handles) == 1
    f = home / "data" / "outputs" / sid / f"{handles[0]}.json"
    assert f.exists() and "row 3999 " in json.loads(f.read_text("utf-8"))["text"]       # 完整内容在盘上（没被截断丢掉）

    sent = mock_log(home)[-1]
    assert "read_tool_output" in sent["tool_names"]               # 出现句柄后，取回工具对模型可见
    disk = json.loads(next((home / "data" / "sessions").glob(f"{sid}.json")).read_text("utf-8"))
    tool_text = next(m["content"] for m in disk["history"] if m.get("role") == "tool")
    assert f"完整输出已保存为 {handles[0]}" in tool_text and "已截断" in tool_text and len(tool_text) < 40000   # 模型收到的是头尾+句柄
    assert "outputs" in disk["tool_groups"]

    evs = ready.chat(sid, "请取回那一行")                          # 模型按句柄 + 关键词取回中间被截掉的那一行
    r = ev(evs, "tool_result")[0]
    assert r["status"] == "ok" and "4000: row 3999 payload-27993" in r["content"], r["content"]
    assert end_status(evs) == "done"


def test_deleting_session_removes_its_outputs(ready, home):
    _cfg(ready)
    sid = new_session(ready)
    ready.chat(sid, "请产生大输出")
    d = home / "data" / "outputs" / sid
    assert d.is_dir() and any(d.iterdir())
    assert ready.delete(f"/api/sessions/{sid}")[0] == 200
    assert not d.exists()


def test_offload_can_be_switched_off(ready, home):
    _cfg(ready, offload_outputs=False)
    try:
        sid = new_session(ready)
        evs = ready.chat(sid, "请产生大输出")
        assert end_status(evs) == "done" and not _history_handles(home, sid)
        assert not (home / "data" / "outputs" / sid).exists()
        disk = json.loads(next((home / "data" / "sessions").glob(f"{sid}.json")).read_text("utf-8"))
        assert "outputs" not in disk["tool_groups"]
        assert "输出过长" in next(m["content"] for m in disk["history"] if m.get("role") == "tool")   # 回到旧的截断做法
    finally:
        _cfg(ready, offload_outputs=True)
