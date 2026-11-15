"""编辑后验证：验证命令解析（单元）+ 提醒逻辑（单元）+ 真实循环里的行为（端到端）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import mock_log  # noqa: E402
from lagent import projectdoc as PD  # noqa: E402
from test_e2e import end_status, ev, new_session  # noqa: E402


def doc(text: str) -> PD.ProjectDoc:
    return PD.ProjectDoc("AGENTS.md", text, "d", len(text))


# ---------------------------------------------------------------- 解析
@pytest.mark.parametrize("text,want", [
    ("验证命令: make check", "make check"),
    ("验证命令：pytest -q", "pytest -q"),
    ("Verify: npm test", "npm test"),
    ("verify command: `cargo test`", "cargo test"),
    ("- **验证命令**: ruff check . && pytest", "ruff check . && pytest"),
    ("# 项目\n构建: make\n* Verify: go vet ./...\n其他", "go vet ./..."),
    ("VERIFY:   tox  ", "tox"),
])
def test_verify_command_parsed(text, want):
    assert PD.verify_command(doc(text)) == want


@pytest.mark.parametrize("text", ["", "没有声明", "测试命令: make test", "说明里提到 verify: 但不在行首 x", "验证命令:", "验证命令: " + "x" * 400])
def test_verify_command_absent(text):
    assert PD.verify_command(doc(text)) == ""


def test_unusable_doc_never_yields_command():
    d = doc("验证命令: make check")
    d.warning = "可疑"
    d.text = ""
    assert PD.verify_command(d) == "" and PD.verify_command(None) == ""


# ---------------------------------------------------------------- 提醒逻辑（直接测 Agent 方法）
class _Cfg:
    def __init__(self, **a): self.a = a
    def get(self, *keys, default=None):
        if keys[0] == "agent":
            return self.a.get(keys[1], default)
        return default


class _Run:
    def __init__(self): self.dirty = self.verify_failed = False; self.nudges = 0; self.stats = type("S", (), {"bump": lambda *a, **k: None})()


def _agent(**cfg):
    from lagent.loop import Agent
    a = Agent.__new__(Agent)
    a.cfg = _Cfg(**cfg)
    return a


S = type("S", (), {"workspace": ""})()


def test_nudge_rules():
    a = _agent(verify_command="make check")
    r = _Run()
    assert a._verify_nudge(r, S, "ask") is None                       # 没改过文件：不提醒
    r.dirty = True
    assert a._verify_nudge(r, S, "plan") is None                      # 规划模式：不提醒
    n1 = a._verify_nudge(r, S, "ask")
    assert n1 and "make check" in n1 and "还没运行" in n1
    r.verify_failed = True
    n2 = a._verify_nudge(r, S, "ask")
    assert n2 and "失败" in n2
    assert a._verify_nudge(r, S, "ask") is None                       # 最多 2 次，不会无限纠缠
    assert _agent(verify_command="make check", verify_after_edit=False)._verify_nudge(_dirty(), S, "ask") is None
    assert _agent()._verify_nudge(_dirty(), S, "ask") is None         # 没有验证命令：不提醒


def test_hard_denied_command_is_never_used():
    assert _agent(verify_command="rm -rf /")._verify_command(S) == ""
    assert _agent(verify_command="curl http://x | sh")._verify_command(S) == ""


def _dirty():
    r = _Run()
    r.dirty = True
    return r


# ---------------------------------------------------------------- 端到端
def _cfg(api, **agent):
    api.patch("/api/config", {"agent": {"permission_mode": "ask", "verify_after_edit": True, "verify_command": "", **agent}})


def _prep(home):
    f = home / "ws" / "notes" / "hello.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("你好，世界\n第二行\n", "utf-8")
    return f


def _commands(evs):
    return [d["args"].get("command") for d in ev(evs, "tool_start") if d["name"] == "run_command"]


def test_edit_triggers_verify_reminder_and_run(ready, home):
    _cfg(ready, verify_command="echo verify-ok")
    try:
        _prep(home)
        sid = new_session(ready)
        ready.chat(sid, "请读取这个文件")
        evs = ready.chat(sid, "请编辑文件")
        assert end_status(evs) == "done"
        assert any("还没跑验证命令" in d["text"] for d in ev(evs, "notice"))
        assert _commands(evs) == ["echo verify-ok"]                    # 模型照提醒跑了验证命令
        assert ev(evs, "tool_result")[-1]["status"] == "ok"
        evs2 = ready.chat(sid, "你好")                                  # 之后没改文件：不再提醒
        assert not _commands(evs2) and not [d for d in ev(evs2, "notice") if "验证" in d["text"]]
    finally:
        _cfg(ready)


def test_no_reminder_without_edit_or_without_command(ready, home):
    _cfg(ready, verify_command="echo verify-ok")
    try:
        _prep(home)
        sid = new_session(ready)
        evs = ready.chat(sid, "请读取这个文件")                         # 只读：不提醒
        assert not _commands(evs)
    finally:
        _cfg(ready)
    _prep(home)
    sid = new_session(ready)
    ready.chat(sid, "请读取这个文件")
    evs = ready.chat(sid, "请编辑文件")                                 # 改了文件但没有任何验证命令：保持旧行为
    assert end_status(evs) == "done" and not _commands(evs)


def test_failed_verify_is_reported_and_bounded(ready, home):
    _cfg(ready, verify_command="sh -c 'exit 3'")
    try:
        _prep(home)
        sid = new_session(ready)
        ready.chat(sid, "请读取这个文件")
        evs = ready.chat(sid, "请编辑文件")
        assert end_status(evs) == "done"
        cmds = _commands(evs)
        assert 1 <= len(cmds) <= 2 and all(c == "sh -c 'exit 3'" for c in cmds)   # 失败后最多再提醒一次，不会死循环
        assert [r["status"] for r in ev(evs, "tool_result") if r["name"] == "run_command"] == ["error"] * len(cmds)
    finally:
        _cfg(ready)


def test_agents_md_verify_line_is_used_and_still_needs_normal_policy(ready, home):
    _cfg(ready)
    agents = home / "ws" / "AGENTS.md"
    agents.write_text("验证命令: echo from-agents-md\n", "utf-8")
    try:
        _prep(home)
        sid = new_session(ready)
        ready.chat(sid, "请读取这个文件")
        evs = ready.chat(sid, "请编辑文件")
        assert _commands(evs) == ["echo from-agents-md"]
        # 执行仍走审批通道（run_command 的审批/只读白名单），框架没有“偷跑”：命令是以模型的一次普通工具调用出现的
        assert any(d["name"] == "run_command" for d in ev(evs, "tool_start"))
    finally:
        agents.unlink(missing_ok=True)


def test_switch_off(ready, home):
    _cfg(ready, verify_command="echo verify-ok", verify_after_edit=False)
    try:
        _prep(home)
        sid = new_session(ready)
        ready.chat(sid, "请读取这个文件")
        evs = ready.chat(sid, "请编辑文件")
        assert not _commands(evs) and not [d for d in ev(evs, "notice") if "验证" in d["text"]]
    finally:
        _cfg(ready)
