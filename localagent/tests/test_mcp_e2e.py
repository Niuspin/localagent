"""端到端：MCP 工具经过真实 Agent 循环（审批、污染、按需加载、API 不泄露密钥）。
本机没有 bwrap，所以这里用 sandbox=off；沙箱参数结构与“不可用即拒绝启动”见 test_mcp.py。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import mock_log
from test_e2e import end_status, ev, new_session

MOCK = str(Path(__file__).with_name("mock_mcp_server.py"))


def _setup(api, **patch):
    s, r = api.put("/api/mcp/mock", {"command": [sys.executable, MOCK], "sandbox": "off", "env": {"MY_KEY": "TOPSECRET-VALUE"}, **patch})
    assert s == 200, r
    return r


def test_mcp_api_never_leaks_env_and_validates(ready, home):
    r = _setup(ready)
    assert "TOPSECRET-VALUE" not in json.dumps(r) and r["env_masked"] == {"MY_KEY": "••••••"}
    s, r = ready.get("/api/mcp")
    assert "TOPSECRET-VALUE" not in json.dumps(r) and r["servers"][0]["name"] == "mock" and "available" in r["sandbox"]
    assert ready.put("/api/mcp/Bad Name", {"command": "x"})[0] == 400
    assert ready.put("/api/mcp/mock", {"env": {"LD_PRELOAD": "x"}})[0] == 400
    assert ready.put("/api/mcp/mock", {"readonly_paths": ["/"]})[0] == 400
    assert ready.post("/api/mcp/nope/start")[0] == 404
    # 配置文件在 data/ 下：权限 0600，Agent 的文件工具看不到
    p = home / "data" / "mcp.json"
    assert oct(p.stat().st_mode & 0o777) == "0o600" and "TOPSECRET-VALUE" in p.read_text("utf-8")


def test_mcp_tool_runs_through_agent_with_approval_and_taint(ready, home):
    ready.patch("/api/config", {"agent": {"permission_mode": "ask"}})
    _setup(ready)
    s, v = ready.post("/api/mcp/mock/start")
    assert s == 200 and v["status"] == "running" and [t["tool"] for t in v["tools"]][:2] == ["echo", "add"]
    assert not any(t["tool"] == "evil" for t in v["tools"]) and v["warnings"]
    sid = new_session(ready)
    evs = ready.chat(sid, "请用 mock 做外部回声")
    ap = ev(evs, "approval_request")
    assert ap and ap[0]["name"] == "mcp__mock__echo" and ap[0]["risk"] == "high"        # 默认逐次确认
    res = [d for d in ev(evs, "tool_result") if d.get("status") != "error"]
    assert res and "echo:ping" in res[0]["content"] and end_status(evs) == "done"
    assert "mcp__mock__echo" in mock_log(home)[-1]["tool_names"]                         # 提到服务器名 → 工具组被加载
    assert ev(evs, "taint") and ready.get(f"/api/sessions/{sid}")[1]["tainted"] is True  # 结果不可信 → 会话被污染
    # 拒绝时不会执行
    evs = ready.chat(new_session(ready), "请用 mock 做外部回声", {"mcp__mock__echo": "deny"})
    assert not [d for d in ev(evs, "tool_result") if "echo:ping" in d.get("content", "")]


def test_mcp_plan_mode_denied_and_trust_results(ready, home):
    ready.patch("/api/config", {"agent": {"permission_mode": "plan"}})
    evs = ready.chat(new_session(ready), "请用 mock 做外部回声")
    assert "mcp__mock__echo" not in mock_log(home)[-1]["tool_names"]                    # 规划模式连工具都不展示
    assert not [d for d in ev(evs, "tool_result") if "echo:ping" in d.get("content", "")]
    ready.patch("/api/config", {"agent": {"permission_mode": "ask"}})
    _setup(ready, trust_results=True)
    sid = new_session(ready)
    evs = ready.chat(sid, "请用 mock 做外部回声")
    assert ev(evs, "approval_request") and not ev(evs, "taint")                          # 受信服务器：不污染


def test_mcp_disable_and_delete(ready, home):
    assert ready.put("/api/mcp/mock", {"enabled": False})[1]["status"] == "disabled"
    sid = new_session(ready)
    ready.chat(sid, "请用 mock 做外部回声")
    assert "mcp__mock__echo" not in mock_log(home)[-1]["tool_names"]
    assert ready.delete("/api/mcp/mock")[0] == 200 and ready.delete("/api/mcp/mock")[0] == 404
    assert not (home / "data" / "mcp" / "mock").exists()
