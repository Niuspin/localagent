"""测试 Claude Code 标准特性：
1. 斜杠命令引擎（内置命令、自定义命令、参数替换）
2. @file / @dir 上下文引用安全解析与 PathGuard 围栏防护
3. 细粒度权限规则（allow/ask/deny 匹配与敏感/受污染不可逾越防线）
4. 项目生命周期钩子（PreToolUse、PostToolUse、Stop、安全防线）
5. 子代理（delegate_task / spawn_agent 工具与递归深度保护）
6. 检查点与回退（Turn 标签与 rewind_to_turn 回滚）
"""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from lagent.commands import (
    CommandContext,
    dispatch_command,
    list_commands,
    load_custom_commands,
    resolve_context_mentions,
)
from lagent.hooks import HookDef, HookManager, HookResult
from lagent.journal import Journal
from lagent.permissions import PermissionManager, match_command, match_glob, parse_rule
from lagent.security import PathGuard
from lagent.tools import agentic, files  # noqa: F401
from lagent.tools.base import TOOLS
from lagent.tools.ctx import SessionState, ToolCtx
from lagent.tools.files import commit_bytes, rewind_to_turn


# =====================================================================
# 1. 斜杠命令测试
# =====================================================================
def test_builtin_commands_list():
    cmds = list_commands(None)
    names = {c["name"] for c in cmds}
    for expected in ("help", "init", "review", "undo", "status", "model", "cost", "context", "plan", "doctor", "permissions", "hooks", "mode"):
        assert expected in names, f"缺少内置命令: {expected}"


def test_custom_commands_loading(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cmd_dir = ws / ".localagent" / "commands"
    cmd_dir.mkdir(parents=True)
    
    (cmd_dir / "deploy.md").write_text("---\ndescription: 部署到生产环境\n---\n执行部署脚本: ./deploy.sh $ARGUMENTS", "utf-8")
    (cmd_dir / "test_module.md").write_text("# 测试模块\n运行针对特定模块的单元测试: pytest tests/$1 -k $2", "utf-8")

    customs = load_custom_commands(ws)
    names = {c["name"]: c["description"] for c in customs}
    assert "deploy" in names
    assert names["deploy"] == "部署到生产环境"
    assert "test_module" in names


def test_custom_command_arg_substitution(tmp_path: Path):
    ws = tmp_path / "workspace"
    cmd_dir = ws / ".localagent" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "bench.md").write_text("运行测试基准: run_bench --target=$1 --iters=$2", "utf-8")

    ctx = CommandContext(workspace=ws)
    res = dispatch_command("/bench api 100", ctx)
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is True
    assert "run_bench --target=api --iters=100" in res.text


def test_dispatch_builtin_help():
    res = dispatch_command("/help", CommandContext())
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is False
    assert "/init" in res.text
    assert "/review" in res.text
    assert "/undo" in res.text


def test_dispatch_builtin_init():
    res = dispatch_command("/init", CommandContext())
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is True
    assert "AGENTS.md" in res.text
    assert "构建与运行" in res.text
    assert "测试与验证" in res.text


def test_dispatch_builtin_review():
    res = dispatch_command("/review src/server.py", CommandContext())
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is True
    assert "src/server.py" in res.text
    assert "功能正确性" in res.text
    assert "安全风险" in res.text


def test_dispatch_builtin_plan():
    res = dispatch_command("/plan 重构数据库层", CommandContext())
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is True
    assert "只读规划" in res.text
    assert "重构数据库层" in res.text


def test_dispatch_builtin_doctor(tmp_path: Path):
    res = dispatch_command("/doctor", CommandContext(workspace=tmp_path))
    assert res is not None
    assert res.handled is True
    assert res.is_prompt is False
    assert "LocalAgent 环境与健康诊断" in res.text
    assert "工作区" in res.text


# =====================================================================
# 2. @ 上下文文件引用测试
# =====================================================================
def test_at_file_mention_code_file(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    guard = PathGuard(ws)
    
    code_file = ws / "calc.py"
    code_file.write_text("def add(a, b):\n    return a + b\n", "utf-8")

    text = "请阅读 @calc.py 并解释功能"
    augmented, mentions, has_taint = resolve_context_mentions(text, guard)
    
    assert has_taint is False
    assert len(mentions) == 1
    assert mentions[0]["path"] == "calc.py"
    assert '<file path="calc.py"' in augmented
    assert "def add(a, b):" in augmented


def test_at_file_mention_directory(tmp_path: Path):
    ws = tmp_path / "workspace"
    src_dir = ws / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "mod_a.py").write_text("pass", "utf-8")
    (src_dir / "mod_b.py").write_text("pass", "utf-8")
    guard = PathGuard(ws)

    text = "看一下 @src 目录包含哪些文件"
    augmented, mentions, has_taint = resolve_context_mentions(text, guard)
    
    assert has_taint is False
    assert len(mentions) == 1
    assert mentions[0]["kind"] == "dir"
    assert '<directory path="src">' in augmented
    assert "mod_a.py" in augmented
    assert "mod_b.py" in augmented


def test_at_file_mention_pathguard_blocking(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret outside", "utf-8")
    guard = PathGuard(ws)

    # 尝试通过路径穿越访问工作区外部
    text = "读取 @../outside.txt 的内容"
    augmented, mentions, has_taint = resolve_context_mentions(text, guard)
    
    assert len(mentions) == 1
    assert mentions[0]["status"] == "blocked"
    assert "安全拦截" in augmented
    assert "secret outside" not in augmented


def test_at_file_mention_upload_tainted(tmp_path: Path):
    ws = tmp_path / "workspace"
    up_dir = ws / "uploads"
    up_dir.mkdir(parents=True)
    (up_dir / "doc.txt").write_text("来自外部上传的文件", "utf-8")
    guard = PathGuard(ws)

    text = "查看 @uploads/doc.txt"
    augmented, mentions, has_taint = resolve_context_mentions(text, guard)
    
    # 上传目录的文件应标记为不可信 (tainted)
    assert has_taint is True
    assert mentions[0]["tainted"] is True
    assert 'tainted="true"' in augmented


# =====================================================================
# 3. 细粒度权限规则测试
# =====================================================================
def test_permission_rule_parsing():
    r1 = parse_rule("Bash(git status*)", "allow")
    assert r1.action == "allow"
    assert r1.rule_type == "bash"
    assert r1.pattern == "git status*"

    r2 = parse_rule("Read(src/**)", "allow")
    assert r2.rule_type == "read"
    assert r2.pattern == "src/**"

    r3 = parse_rule("Edit(*.md)", "ask")
    assert r3.rule_type == "edit"
    assert r3.pattern == "*.md"


def test_permission_manager_allow_and_deny():
    rules = {
        "deny": ["Bash(curl *)", "Read(.env*)"],
        "allow": ["Bash(pytest*)", "Read(src/**)"],
    }
    pm = PermissionManager(rules=rules)

    # 1. 匹配 deny
    dec1 = pm.evaluate(kind="exec", name="run_command", args={"command": "curl https://example.com"},
                       tainted=False, command="curl https://example.com")
    assert dec1 is not None
    assert dec1.action == "deny"

    # 2. 匹配 allow
    dec2 = pm.evaluate(kind="exec", name="run_command", args={"command": "pytest tests/test_unit.py"},
                       tainted=False, command="pytest tests/test_unit.py")
    assert dec2 is not None
    assert dec2.action == "allow"

    # 3. 未匹配规则，返回 None（留待默认 Policy 决策）
    dec3 = pm.evaluate(kind="exec", name="run_command", args={"command": "npm install"},
                       tainted=False, command="npm install")
    assert dec3 is None


def test_permission_rule_hard_deny_cannot_bypass():
    # 即使配置了允许所有 Bash 操作
    pm = PermissionManager(rules={"allow": ["Bash(*)"]})
    
    # 危险的 hard_deny 系统命令
    dec = pm.evaluate(kind="exec", name="run_command", args={"command": "mkfs /dev/sda1"},
                      tainted=False, command="mkfs /dev/sda1")
    assert dec is not None
    assert dec.action == "deny"
    assert "hard_deny" in dec.key


def test_permission_rule_sensitive_downgrades_to_ask():
    # 允许所有读取
    pm = PermissionManager(rules={"allow": ["Read(*)"]})
    
    # 敏感文件（.env）
    dec = pm.evaluate(kind="read", name="read_file", args={"path": ".env"},
                      tainted=False, sensitive="敏感配置文件: .env")
    assert dec is not None
    # 必须降级为 ask，绝不能直接 allow
    assert dec.action == "ask"
    assert "敏感文件" in dec.reason


def test_permission_rule_tainted_downgrades_dangerous_to_ask():
    # 允许写入所有 md 文件
    pm = PermissionManager(rules={"allow": ["Write(*.md)"]})
    
    # 在污染上下文 (tainted=True) 下进行写文件
    dec = pm.evaluate(kind="write", name="write_file", args={"path": "notes.md"},
                      tainted=True)
    assert dec is not None
    # 必须降级为 ask，强制用户确认
    assert dec.action == "ask"
    assert "不可信数据" in dec.reason


# =====================================================================
# 4. 生命周期钩子测试
# =====================================================================
def test_hook_pre_tool_use_allow_and_deny(tmp_path: Path):
    hm = HookManager(tmp_path)
    hm.hooks["PreToolUse"] = [
        HookDef(event="PreToolUse", matcher="run_command", command="exit 0"),
        HookDef(event="PreToolUse", matcher="delete_path", command="exit 1"),
    ]

    ctx = MagicMock()
    ctx.session.tainted = False

    # 允许的工具
    res_allow = asyncio.run(hm.run_pre_tool_use("run_command", {"command": "git status"}, ctx))
    assert res_allow.action == "allow"

    # 拒绝的工具
    res_deny = asyncio.run(hm.run_pre_tool_use("delete_path", {"path": "src/old.py"}, ctx))
    assert res_deny.action == "deny"


def test_hook_hard_deny_is_skipped(tmp_path: Path):
    hm = HookManager(tmp_path)
    hm.hooks["PreToolUse"] = [HookDef(event="PreToolUse", matcher="*", command="mkfs /dev/sda1")]

    ctx = MagicMock()
    ctx.session.tainted = False
    res = asyncio.run(hm.run_pre_tool_use("read_file", {"path": "a.py"}, ctx))
    assert res.action == "deny"
    assert res.exit_code == 126


def test_hook_tainted_context_skips_hooks(tmp_path: Path):
    hm = HookManager(tmp_path)
    hm.hooks["PreToolUse"] = [HookDef(event="PreToolUse", matcher="*", command="echo test")]

    ctx = MagicMock()
    ctx.session.tainted = True  # 受污染上下文

    res = asyncio.run(hm.run_pre_tool_use("run_command", {"command": "dir"}, ctx))
    assert res.action == "allow"
    assert res.skipped is True


# =====================================================================
# 5. 子代理 (delegate_task) 测试
# =====================================================================
def test_subagent_recursion_guard(tmp_path: Path):
    tool = TOOLS.get("delegate_task")
    assert tool is not None, "未找到 delegate_task 工具"

    ctx = MagicMock()
    ctx.depth = 1
    ctx.session.depth = 1

    res = asyncio.run(tool.fn(ctx, {"prompt": "再派生一个子任务"}))
    assert res.error is True
    assert "不能再派生子代理" in res.content


def test_subagent_dispatch(tmp_path: Path):
    tool = TOOLS.get("delegate_task")
    assert tool is not None

    ctx = MagicMock()
    ctx.depth = 0
    ctx.session.depth = 0
    ctx.emit = MagicMock()

    mock_agent = MagicMock()
    mock_agent.run_subagent = AsyncMock(return_value="子代理分析完成：已验证代码无风险。")
    ctx.agent = mock_agent

    res = asyncio.run(tool.fn(ctx, {"prompt": "调研 tests 架构", "mode": "explore", "max_steps": 5}))
    assert res.error is False
    assert "子代理分析完成" in res.content
    mock_agent.run_subagent.assert_awaited_once()


# =====================================================================
# 6. 回合检查点与回退 (rewind_to_turn) 测试
# =====================================================================
def test_rewind_to_turn_reverts_turn_modifications(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    journal = Journal(data_dir)
    guard = PathGuard(ws)

    # 准备目标文件
    target = ws / "config.txt"
    target.write_text("initial v1", "utf-8")

    # 模拟回合 1：修改 config.txt
    ctx1 = MagicMock()
    ctx1.sid = "sess_001"
    ctx1.max_bytes = 10 * 1024 * 1024
    ctx1.guard = guard
    ctx1.journal = journal
    ctx1.session.turn_id = "turn_1"
    ctx1.turn_id = "turn_1"

    commit_bytes(ctx1, target, b"modified v2 in turn 1", "edit", "回合1修改")
    assert target.read_text("utf-8") == "modified v2 in turn 1"

    # 模拟回合 2：新建 other.txt
    other = ws / "other.txt"
    ctx2 = MagicMock()
    ctx2.sid = "sess_001"
    ctx2.max_bytes = 10 * 1024 * 1024
    ctx2.guard = guard
    ctx2.journal = journal
    ctx2.session.turn_id = "turn_2"
    ctx2.turn_id = "turn_2"

    commit_bytes(ctx2, other, b"created in turn 2", "write", "回合2创建")
    assert other.read_text("utf-8") == "created in turn 2"

    # 执行针对回合 2 的回退
    res2 = rewind_to_turn(journal, "sess_001", turn_id="turn_2")
    assert res2["ok"] is True
    assert res2["count"] == 1
    # 回合 2 新建的文件应被移入回收站
    assert not other.exists()
    # 回合 1 的修改不受影响
    assert target.read_text("utf-8") == "modified v2 in turn 1"

    # 执行针对回合 1 的回退
    res1 = rewind_to_turn(journal, "sess_001", turn_id="turn_1")
    assert res1["ok"] is True
    assert res1["count"] == 1
    # 回合 1 文件的内容应恢复为 initial v1
    assert target.read_text("utf-8") == "initial v1"


def test_rewind_without_turn_id_picks_latest(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    journal = Journal(tmp_path / "data")
    guard = PathGuard(ws)

    f1 = ws / "file1.txt"
    f1.write_text("orig", "utf-8")

    ctx = MagicMock()
    ctx.sid = "sess_002"
    ctx.max_bytes = 10 * 1024 * 1024
    ctx.guard = guard
    ctx.journal = journal
    ctx.session.turn_id = "turn_x"
    ctx.turn_id = "turn_x"

    commit_bytes(ctx, f1, b"updated", "edit", "修改")
    assert f1.read_text("utf-8") == "updated"

    # 不传 turn_id，自动查找最近的回合
    res = rewind_to_turn(journal, "sess_002")
    assert res["ok"] is True
    assert res["count"] == 1
    assert f1.read_text("utf-8") == "orig"


def test_dispatch_permissions_and_hooks_command(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    
    # 写入 permissions 配置文件
    settings_file = ws / ".localagent" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({
        "permissions": {
            "allow": ["Bash(pytest*)"],
            "deny": ["Bash(curl*)"]
        }
    }), "utf-8")

    ctx = CommandContext(workspace=ws)
    res_perm = dispatch_command("/permissions", ctx)
    assert res_perm is not None
    assert res_perm.handled is True
    assert "Bash(pytest*)" in res_perm.text
    assert "Bash(curl*)" in res_perm.text

    # 写入 hooks 配置文件
    hooks_file = ws / ".localagent" / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"matcher": "run_command", "command": "echo pre"}],
            "Stop": [{"command": "echo stop"}]
        }
    }), "utf-8")

    res_hooks = dispatch_command("/hooks", ctx)
    assert res_hooks is not None
    assert res_hooks.handled is True
    assert "PreToolUse" in res_hooks.text
    assert "echo pre" in res_hooks.text


def test_dispatch_compact_clear_mode():
    cfg = MagicMock()
    cfg.get.return_value = "ask"
    ctx = CommandContext(cfg=cfg)

    # /compact
    r_compact = dispatch_command("/compact", ctx)
    assert r_compact.handled is True
    assert r_compact.action == "compact"

    # /clear
    r_clear = dispatch_command("/clear", ctx)
    assert r_clear.handled is True
    assert r_clear.action == "clear"

    # /mode auto_edit
    r_mode = dispatch_command("/mode auto_edit", ctx)
    assert r_mode.handled is True
    assert r_mode.action == "set_mode"
    assert r_mode.data["mode"] == "auto_edit"
    cfg.set.assert_called_with("agent", "permission_mode", "auto_edit")


def test_at_file_large_file_skip(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    big_file = ws / "huge.log"
    # 写入 2MB 文件
    big_file.write_bytes(b"a" * (2 * 1024 * 1024))
    guard = PathGuard(ws)

    # 设定上限为 1MB
    text = "分析 @huge.log"
    augmented, mentions, has_taint = resolve_context_mentions(text, guard, max_file_mb=1)
    assert mentions[0]["status"] == "skipped"
    assert "文件过大" in augmented
