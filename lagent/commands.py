"""斜杠命令引擎（Slash Command Engine）与 @ 上下文引用解析（Context Mentions）。

支持的标准命令：
- /help: 查看所有可用命令与自定义命令说明
- /status: 查看工作区、会话、引擎、污染与权限状态
- /model: 查看当前模型与引擎参数
- /cost: 查看当前会话的 Token 统计
- /context: 查看上下文窗口容量与使用情况
- /compact: 触发会话上下文手动压缩
- /clear: 清空当前会话
- /undo: 撤销上一个回合（Turn）产生的文件修改
- /doctor: 环境、配置、沙箱与依赖健康检查
- /permissions: 查看当前生效的权限规则列表
- /hooks: 查看配置的生命周期钩子
- /plan [task]: 制定只读规划方案
- /mode <ask|auto_edit|plan|auto>: 查看或切换权限模式
- /init: [Prompt 类] 审查工作区并生成/更新 AGENTS.md 指南
- /review [files]: [Prompt 类] 审查代码质量、安全性与规范
- 自定义命令: 从 .localagent/commands/*.md 与 .claude/commands/*.md 读取并进行 $ARGUMENTS 替换
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import security as sec
from .permissions import PermissionManager
from .hooks import HookManager


@dataclass
class CommandResult:
    handled: bool = False              # 是否为已处理的斜杠命令
    is_prompt: bool = False            # 是否为需要发给 LLM 循环的 Prompt 类命令
    text: str = ""                     # 结果输出（即时回答）或展开后的 Prompt 指令
    action: str = ""                   # 特殊动作指令（如 'clear', 'compact', 'set_mode', 'undo'）
    data: dict = field(default_factory=dict)


@dataclass
class CommandContext:
    agent: Any = None
    session: Any = None
    workspace: str | Path | None = None
    cfg: Any = None
    journal: Any = None
    missions: Any = None
    research: Any = None


BUILTIN_COMMANDS: list[dict[str, str]] = [
    {"name": "help", "description": "查看所有可用命令与使用方法", "kind": "builtin"},
    {"name": "init", "description": "检查工作区结构并生成/更新项目指南 AGENTS.md", "kind": "builtin"},
    {"name": "review", "description": "审查工作区代码改动或指定文件（安全/质量/规范）", "kind": "builtin"},
    {"name": "undo", "description": "撤销上一个回合产生的文件修改", "kind": "builtin"},
    {"name": "status", "description": "查看工作区、会话状态、引擎、污染与权限模式", "kind": "builtin"},
    {"name": "model", "description": "查看当前运行的模型信息与配置", "kind": "builtin"},
    {"name": "cost", "description": "查看当前会话的 Token 消耗统计", "kind": "builtin"},
    {"name": "context", "description": "查看上下文窗口占用率与容量", "kind": "builtin"},
    {"name": "compact", "description": "手动压缩当前会话历史", "kind": "builtin"},
    {"name": "clear", "description": "清空当前会话历史", "kind": "builtin"},
    {"name": "plan", "description": "制定只读规划方案（/plan <任务>）", "kind": "builtin"},
    {"name": "mode", "description": "查看或切换权限模式（/mode <ask|auto_edit|plan|auto>）", "kind": "builtin"},
    {"name": "permissions", "description": "查看细粒度权限规则列表与生效状态", "kind": "builtin"},
    {"name": "hooks", "description": "查看当前配置的生命周期钩子", "kind": "builtin"},
    {"name": "doctor", "description": "诊断工作区、引擎、沙箱与环境状态", "kind": "builtin"},
    {"name": "mission", "description": "管理超长期自主任务 (/mission start|status|pause|resume|stop|budget|log|edit)", "kind": "builtin"},
    {"name": "research", "description": "安全合规联网调研 (/research <问题> | status | stop | report)", "kind": "builtin"},
    {"name": "net", "description": "查看网络账本与拦截摘要 (/net [domain|blocked])", "kind": "builtin"},
]


def load_custom_commands(workspace: str | Path | None) -> list[dict[str, str]]:
    """扫描工作区内 .localagent/commands/*.md 与 .claude/commands/*.md 自定义命令。"""
    out: list[dict[str, str]] = []
    if not workspace:
        return out
    ws = Path(workspace)
    if not ws.is_dir():
        return out

    cand_dirs = [
        ws / ".localagent" / "commands",
        ws / ".claude" / "commands",
    ]
    seen_names = set()
    for d in cand_dirs:
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                name = f.stem.lower()
                if name in seen_names:
                    continue
                seen_names.add(name)
                desc = ""
                try:
                    content = f.read_text("utf-8")
                    # 支持 frontmatter 描述
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            for ln in parts[1].splitlines():
                                if ln.lower().startswith("description:"):
                                    desc = ln.split(":", 1)[1].strip()
                                    break
                    if not desc:
                        # 使用首行非空文字
                        for ln in content.splitlines():
                            ln = ln.strip().lstrip("#").strip()
                            if ln:
                                desc = ln[:60]
                                break
                except OSError:
                    desc = "自定义命令"
                out.append({
                    "name": name,
                    "description": desc or "自定义命令",
                    "kind": "custom",
                    "path": str(f),
                })
    return out


def list_commands(workspace: str | Path | None = None) -> list[dict[str, str]]:
    """列出全部可用命令（内置 + 工作区自定义）。"""
    return BUILTIN_COMMANDS + load_custom_commands(workspace)


def _expand_custom_command(md_path: Path, args_str: str) -> str:
    template = md_path.read_text("utf-8")
    # 去除 frontmatter
    if template.startswith("---"):
        parts = template.split("---", 2)
        if len(parts) >= 3:
            template = parts[2].strip()

    args_list = args_str.split()
    # 替换 $ARGUMENTS, $@, $1, $2, ...
    expanded = template.replace("$ARGUMENTS", args_str).replace("$@", args_str)
    for i, a in enumerate(args_list, 1):
        expanded = expanded.replace(f"${i}", a)

    if "$ARGUMENTS" not in template and "$@" not in template and args_str and not any(f"${i}" in template for i in range(1, 10)):
        expanded += f"\n\n附加参数: {args_str}"
    return expanded


def dispatch_command(text: str, ctx: CommandContext) -> CommandResult | None:
    """解析并分发斜杠命令。如果不是斜杠命令则返回 None。"""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None

    # 分离命令名与参数
    parts = raw.split(None, 1)
    cmd = parts[0][1:].lower()
    args_str = parts[1].strip() if len(parts) > 1 else ""

    ws = Path(ctx.workspace) if ctx.workspace else None

    # 1. 检查工作区自定义命令
    if ws and ws.is_dir():
        for d in (ws / ".localagent" / "commands", ws / ".claude" / "commands"):
            cf = d / f"{cmd}.md"
            if cf.is_file():
                try:
                    expanded = _expand_custom_command(cf, args_str)
                    return CommandResult(handled=True, is_prompt=True, text=expanded, data={"command": cmd, "source": str(cf)})
                except OSError as e:
                    return CommandResult(handled=True, is_prompt=False, text=f"读取自定义命令 /{cmd} 失败: {e}")

    # 2. 内置命令分发
    if cmd == "help":
        cmds = list_commands(ws)
        lines = ["### 🛠️ 可用斜杠命令 (Slash Commands)", ""]
        for c in cmds:
            tag = "内置" if c["kind"] == "builtin" else "自定义"
            lines.append(f"- `/{c['name']}` ({tag}): {c['description']}")
        lines.append("\n提示：可输入 `@path/to/file` 将工作区文件内容直接引用到当前对话。")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if cmd == "init":
        # 扫描工作区结构并指示 Agent 生成规范的 AGENTS.md
        prompt = (
            "请检查当前工作区的文件结构和技术栈，生成或更新一份清晰、实用的 `AGENTS.md` 指南。\n\n"
            "要求包含：\n"
            "1. **项目概述**：核心功能、技术栈与目录架构说明\n"
            "2. **构建与运行**：完整的本地安装、编译与启动命令\n"
            "3. **测试与验证**：单元测试、集成测试与代码检查命令（确保 Agent 修改后可自动验证）\n"
            "4. **代码规范与安全守则**：编码规范、敏感文件边界与约束\n\n"
            "请先使用 list_dir / glob 了解当前项目文件，再使用 write_file 写入 AGENTS.md。"
        )
        if args_str:
            prompt += f"\n\n用户附加要求: {args_str}"
        return CommandResult(handled=True, is_prompt=True, text=prompt)

    if cmd == "review":
        target = args_str or "最近修改的文件与当前工作区核心代码"
        prompt = (
            f"请对工作区中的目标进行细致的代码审查（Review）：\n目标: {target}\n\n"
            "审查维度：\n"
            "1. **功能正确性**：逻辑漏洞、并发问题、边界条件处理\n"
            "2. **安全风险**：命令注入、路径穿越、凭据泄露、输入未校验\n"
            "3. **性能与可靠性**：资源未释放、大文件处理、死锁与内存隐患\n"
            "4. **代码风格与可维护性**：命名、注释、模块解耦\n\n"
            "请指出具体的代码位置，并给出直接可行的修改方案。"
        )
        return CommandResult(handled=True, is_prompt=True, text=prompt)

    if cmd == "undo":
        # 撤销上一个回合的文件变更
        if ctx.journal and ctx.session:
            from .tools.files import rewind_to_turn
            res = rewind_to_turn(ctx.journal, ctx.session.id)
            if res.get("count", 0) > 0:
                files_list = "\n".join(f"- `{p}`" for p in res["reverted"])
                msg = f"✅ 已成功撤销上一个回合（Turn {res.get('turn_id', '')}）的 {res['count']} 处文件修改：\n{files_list}"
            else:
                msg = "ℹ️ 当前会话上一个回合中没有找到可撤销的文件修改记录。"
            return CommandResult(handled=True, is_prompt=False, text=msg, action="undo", data=res)
        return CommandResult(handled=True, is_prompt=False, text="撤销失败：未提供日志或会话对象。")

    if cmd == "status":
        lines = ["### 📊 LocalAgent 状态概览", ""]
        lines.append(f"- **工作区**: `{ws or '未选择'}`")
        if ctx.cfg:
            mode = ctx.cfg.get("agent", "permission_mode", default="ask")
            lines.append(f"- **权限模式**: `{mode}`")
        if ctx.session:
            lines.append(f"- **会话 ID**: `{ctx.session.id}`")
            lines.append(f"- **上下文受污染 (Tainted)**: `{'是 (不可信)' if ctx.session.tainted else '否 (安全)'}`")
            lines.append(f"- **待办事项 (Todos)**: `{len(ctx.session.todos)} 条`")
            lines.append(f"- **已加载扩展工具组**: `{', '.join(sorted(ctx.session.tool_groups)) or '仅核心'}`")
        if ctx.agent and hasattr(ctx.agent, "engine"):
            eng = ctx.agent.engine
            m = eng.model
            mname = (m.get("name") or m.get("file")) if isinstance(m, dict) else (m or "未加载")
            lines.append(f"- **引擎状态**: `{eng.status}` (模型: `{mname}`)")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if cmd == "model":
        if ctx.agent and hasattr(ctx.agent, "engine"):
            eng = ctx.agent.engine
            m = eng.model
            mname = (m.get("name") or m.get("file")) if isinstance(m, dict) else (m or "未加载")
            prof_attr = getattr(eng, "profile", None)
            prof = prof_attr() if callable(prof_attr) else prof_attr
            prof_str = (
                f"档位 `{prof.tier}`（{prof.label}），参数约 `{prof.params_b:.1f}B`"
                if prof is not None else "未加载"
            )
            lines = [
                "### 🧠 模型与引擎状态",
                f"- **当前模型**: `{mname}`",
                f"- **引擎状态**: `{eng.status}`",
                f"- **上下文容量**: `{eng.ctx}` (最大上限: `{eng.ctx_cap}`)",
                f"- **视觉多模态 (Vision)**: `{'已启用' if eng.vision else '未启用'}`",
                f"- **硬件画像**: {prof_str}",
                f"- **平均生成速度 (TPS)**: `{eng.tps:.1f}`" if eng.tps else "- **平均速度**: 暂无数据",
            ]
            return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))
        return CommandResult(handled=True, is_prompt=False, text="无法获取引擎信息。")

    if cmd == "cost":
        if ctx.session:
            u = ctx.session.usage
            lines = [
                "### 📈 会话 Token 统计",
                f"- **上下文容量**: `{u.get('ctx', '未知')}`",
                f"- **已使用 Token**: `{u.get('used', 0)}`",
                f"- **占用比例**: `{u.get('pct', 0)}%`",
            ]
            return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))
        return CommandResult(handled=True, is_prompt=False, text="无会话 Token 统计数据。")

    if cmd == "context":
        if ctx.session:
            u = ctx.session.usage
            cx = u.get("ctx", 32768)
            used = u.get("used", 0)
            pct = u.get("pct", 0)
            summ = "是" if ctx.session.summary else "否"
            lines = [
                "### 🪟 上下文窗口监控",
                f"- **窗口大小**: `{cx}` tokens",
                f"- **当前估算**: `{used}` tokens ({pct}%)",
                f"- **已压缩早期摘要**: `{summ}`",
                f"- **历史消息条数**: `{len(ctx.session.history)}` (UI 卡片: `{len(ctx.session.ui)}`)",
            ]
            return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))
        return CommandResult(handled=True, is_prompt=False, text="无法获取上下文信息。")

    if cmd == "compact":
        return CommandResult(handled=True, is_prompt=False, text="正在压缩会话历史…", action="compact")

    if cmd == "clear":
        return CommandResult(handled=True, is_prompt=False, text="会话历史已清空。", action="clear")

    if cmd == "mode":
        valid_modes = ("ask", "auto_edit", "plan", "auto")
        if args_str:
            target_mode = args_str.lower()
            if target_mode in valid_modes:
                if ctx.cfg:
                    ctx.cfg.set("agent", "permission_mode", target_mode)
                return CommandResult(handled=True, is_prompt=False, text=f"✅ 权限模式已切换为: `{target_mode}`", action="set_mode", data={"mode": target_mode})
            return CommandResult(handled=True, is_prompt=False, text=f"❌ 无效的模式 `{args_str}`。可选模式: {', '.join(valid_modes)}")
        cur = ctx.cfg.get("agent", "permission_mode", default="ask") if ctx.cfg else "ask"
        return CommandResult(handled=True, is_prompt=False, text=f"当前权限模式: `{cur}`。可用 `/mode <ask|auto_edit|plan|auto>` 切换。")

    if cmd == "plan":
        if args_str:
            prompt = (
                f"请以只读规划模式为以下任务制定具体、可落地的实施步骤，不要直接修改文件：\n\n{args_str}\n\n"
                "输出格式要求：\n"
                "1. 现状调研与受影响模块\n"
                "2. 拆解的分步实施计划（带具体待修改文件清单）\n"
                "3. 潜在风险与验收验证方式"
            )
            return CommandResult(handled=True, is_prompt=True, text=prompt)
        if ctx.cfg:
            ctx.cfg.set("agent", "permission_mode", "plan")
        return CommandResult(handled=True, is_prompt=False, text="✅ 已切换到【只读规划】模式。模型将只读取与搜索，不会修改文件或执行危险命令。", action="set_mode", data={"mode": "plan"})

    if cmd == "permissions":
        pm = PermissionManager(ws)
        rules = pm.list_rules()
        lines = ["### 🛡️ 细粒度权限规则", ""]
        cur_mode = ctx.cfg.get("agent", "permission_mode", default="ask") if ctx.cfg else "ask"
        lines.append(f"- **基础权限模式**: `{cur_mode}`")
        for act in ("deny", "ask", "allow"):
            r_list = rules.get(act) or []
            lines.append(f"- **{act.upper()} 规则** ({len(r_list)} 条):")
            if r_list:
                for r in r_list:
                    lines.append(f"  - `{r}`")
            else:
                lines.append("  - (无)")
        lines.append("\n可在 `.localagent/settings.json` 或 `.claude/settings.local.json` 中配置自定义规则。")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if cmd == "hooks":
        hm = HookManager(ws)
        hooks = hm.list_hooks()
        lines = ["### 🪝 项目生命周期钩子", ""]
        has_any = False
        for ev, defs in hooks.items():
            if defs:
                has_any = True
                lines.append(f"- **{ev}** ({len(defs)} 个):")
                for h in defs:
                    lines.append(f"  - `matcher: {h['matcher']}` → `{h['command']}` (超时: {h['timeout']}s)")
        if not has_any:
            lines.append("当前工作区未配置生命周期钩子。\n可在 `.localagent/hooks.json` 中配置 PreToolUse / PostToolUse / Stop / UserPromptSubmit。")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if cmd == "doctor":
        lines = ["### 🩺 LocalAgent 环境与健康诊断", ""]
        # 工作区检查
        if ws and ws.is_dir():
            lines.append(f"- ✅ **工作区正常**: `{ws}`")
        else:
            lines.append(f"- ⚠️ **工作区未设定或不存在**: `{ws or '未选择'}`")

        # 依赖与外部工具
        git_path = shutil.which("git")
        lines.append(f"- {'✅' if git_path else '⚠️'} **Git 命令**: `{'已安装 (' + git_path + ')' if git_path else '未找到'}`")

        # 沙箱状态
        sb = sec.sandbox_info()
        lines.append(f"- {'✅' if sb['available'] else 'ℹ️'} **命令沙箱**: `{sb['kind']}` ({sb['note']})")

        # 引擎状态
        if ctx.agent and hasattr(ctx.agent, "engine"):
            eng = ctx.agent.engine
            lines.append(f"- {'✅' if eng.status == 'ready' else '⚠️'} **引擎状态**: `{eng.status}` (模型: `{eng.model or '无'}`)")

        # 写权限检查
        if ws and ws.is_dir():
            test_file = ws / ".la-doctor-test.tmp"
            try:
                test_file.write_text("ok", "utf-8")
                test_file.unlink()
                lines.append("- ✅ **工作区写权限**: 正常")
            except OSError as e:
                lines.append(f"- ❌ **工作区写权限异常**: {e}")

        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if cmd == "mission":
        return _handle_mission_command(args_str, ctx)

    if cmd == "research":
        return _handle_research_command(args_str, ctx)

    if cmd == "net":
        return _handle_net_command(args_str, ctx)

    # 未知命令：提示用户 /help
    return CommandResult(
        handled=True,
        is_prompt=False,
        text=f"未知命令 `/{cmd}`。输入 `/help` 查看所有支持的内置与自定义命令。"
    )


def _resolve_target_mission(mgr: Any, ctx: CommandContext, explicit_id: str | None = None) -> Any:
    if explicit_id:
        m = mgr.get_mission(explicit_id)
        if m is not None:
            return m
    sid_mid = getattr(ctx.session, "mission_id", None) if ctx.session else None
    if sid_mid:
        m = mgr.get_mission(sid_mid)
        if m is not None:
            return m
    all_ms = mgr.store.list()
    return all_ms[0] if all_ms else None


def _handle_mission_command(arg: str, ctx: CommandContext) -> CommandResult:
    mgr = ctx.missions or (getattr(ctx.agent, "missions", None) if ctx.agent else None)
    if mgr is None:
        return CommandResult(handled=True, is_prompt=False, text="⚠️ Mission 运行时未初始化。")

    raw = (arg or "").strip()
    if not raw:
        sub, rest = "status", ""
    else:
        parts = raw.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "start":
        tokens = rest.split()
        mode = ""
        use_swarm = False
        goal_tokens: list[str] = []
        for tok in tokens:
            if tok == "--unattended":
                mode = "unattended"
            elif tok == "--attended":
                mode = "attended"
            elif tok == "--swarm":
                use_swarm = True
            else:
                goal_tokens.append(tok)
        goal = " ".join(goal_tokens).strip()
        if not goal:
            return CommandResult(handled=True, is_prompt=False, text="用法: `/mission start [--unattended] [--swarm] <任务目标>`")
        ws = str(ctx.workspace or (ctx.cfg.get("workspace") if ctx.cfg else "") or "")
        sid = getattr(ctx.session, "id", None) if ctx.session else None
        m = mgr.create_mission(goal=goal, workspace=ws, mode=mode, use_swarm=use_swarm, session_id=sid, auto_start=True)
        if ctx.session is not None:
            ctx.session.mission_id = m.id
        md_path = mgr.store.mission_md_path(m)
        return CommandResult(
            handled=True,
            is_prompt=False,
            text=(
                f"### 🚀 Mission 已启动 (`{m.id}`)\n\n"
                f"- **标题**: {m.title}\n"
                f"- **模式**: `{m.mode}` | **分支**: `{m.branch or '-'}` | **里程碑**: `{len(m.milestones)}`\n"
                f"- **计划文件**: `{md_path}`"
            ),
        )

    if sub == "status":
        explicit = rest.split()[0] if rest else None
        m = _resolve_target_mission(mgr, ctx, explicit)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="暂无 Mission。输入 `/mission start <目标>` 创建一个超长期自主任务。")
        prog = m.progress()
        lines = [
            f"### 🛰️ Mission `{m.id}` — {m.title}",
            f"- **状态**: `{m.status}` | **模式**: `{m.mode}` | **Episode**: `{m.current_episode}` | **进度**: `{prog['done']}/{prog['total']}`",
        ]
        if m.branch:
            lines.append(f"- **分支**: `{m.branch}`")
        if m.pause_reason:
            lines.append(f"- **暂停原因**: `{m.pause_reason}`")
        if m.last_error:
            lines.append(f"- **最近错误**: `{m.last_error[:200]}`")
        lines.append("")
        for ms in m.milestones:
            mark = "✅" if ms.status == "done" else ("⏳" if ms.status == "running" else ("⛔" if ms.status == "blocked" else ("❌" if ms.status == "failed" else "⬜")))
            sha = f" (`{ms.commit_sha[:8]}`)" if ms.commit_sha else ""
            lines.append(f"- {mark} `{ms.id}` {ms.title}{sha}")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if sub == "pause":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到可暂停的 Mission。")
        m = mgr.pause_mission(m.id, reason="slash_command")
        return CommandResult(handled=True, is_prompt=False, text=f"⏸️ Mission `{m.id}` 已暂停（status=`{m.status}`）。")

    if sub == "resume":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到可恢复的 Mission。")
        m = mgr.resume_mission(m.id)
        return CommandResult(handled=True, is_prompt=False, text=f"▶️ Mission `{m.id}` 已恢复执行。")

    if sub == "stop":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到可停止的 Mission。")
        m = mgr.cancel_mission(m.id)
        return CommandResult(handled=True, is_prompt=False, text=f"⏹️ Mission `{m.id}` 已终止（status=`{m.status}`）。")

    if sub == "budget":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到目标 Mission。")
        b = m.budget
        return CommandResult(
            handled=True,
            is_prompt=False,
            text=(
                f"### 📊 Mission `{m.id}` 预算使用\n\n"
                f"- **Wall**: `{b.get('used_wall_s', 0):.1f}s / {b.get('wall_s', 0):.0f}s`\n"
                f"- **Tokens**: `{b.get('used_tokens', 0)} / {b.get('tokens', 0)}`\n"
                f"- **Steps**: `{b.get('used_steps', 0)} / {b.get('steps', 0)}`\n"
                f"- **Disk**: `{b.get('used_disk_mb', 0):.2f} MB / {b.get('disk_mb', 0):.0f} MB`"
            ),
        )

    if sub == "log":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到目标 Mission。")
        evs = mgr.store.read_events(m.id, limit=15)
        if not evs:
            return CommandResult(handled=True, is_prompt=False, text=f"Mission `{m.id}` 暂无事件日志。")
        lines = [f"### 📜 Mission `{m.id}` 最近事件 ({len(evs)})", ""]
        for ev in evs:
            lines.append(f"- `#{ev.get('seq')}` **{ev.get('kind')}** `{str(ev.get('data') or {})[:140]}`")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    if sub == "edit":
        m = _resolve_target_mission(mgr, ctx, rest.split()[0] if rest else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到目标 Mission。")
        md_path = mgr.store.mission_md_path(m)
        txt = mgr.store.read_mission_md(m)
        return CommandResult(
            handled=True,
            is_prompt=False,
            text=f"### 📝 MISSION.md (`{md_path}`)\n\n```markdown\n{txt[:2500]}\n```",
        )

    if sub == "rollback":
        toks = rest.split()
        if not toks:
            return CommandResult(handled=True, is_prompt=False, text="用法: `/mission rollback <milestone_id|commit_sha|base> [mission_id]`")
        target = toks[0]
        m = _resolve_target_mission(mgr, ctx, toks[1] if len(toks) > 1 else None)
        if m is None:
            return CommandResult(handled=True, is_prompt=False, text="未找到目标 Mission。")
        try:
            m = mgr.rollback(m.id, target)
            return CommandResult(handled=True, is_prompt=False, text=f"⏪ Mission `{m.id}` 已回滚到 `{target}`。")
        except Exception as e:
            return CommandResult(handled=True, is_prompt=False, text=f"❌ 回滚失败: {e}")

    return CommandResult(
        handled=True,
        is_prompt=False,
        text="用法: `/mission start <目标> | status | pause | resume | stop | budget | log | edit | rollback <target>`",
    )


def _handle_research_command(arg: str, ctx: CommandContext) -> CommandResult:
    mgr = ctx.research or (getattr(ctx.agent, "research", None) if ctx.agent else None)
    if mgr is None:
        return CommandResult(handled=True, is_prompt=False, text="⚠️ Research Engine 尚未初始化。")
    raw = (arg or "").strip()
    if not raw:
        tasks = mgr.evidence.list_tasks(session_id=getattr(ctx.session, "id", None), limit=5)
        if not tasks:
            return CommandResult(handled=True, is_prompt=False, text="暂无调研任务。用法: `/research <问题>` | `/research status [id]` | `/research stop [id]` | `/research report [id]`")
        lines = ["### 🔎 近期联网调研任务", ""]
        for t in tasks:
            lines.append(f"- `{t['id']}` [{t['status']}] ({t['mode']}) {t['question'][:60]}")
        return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))

    parts = raw.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("status", "stop", "report"):
        rid = rest.split()[0] if rest else ""
        if not rid:
            tasks = mgr.evidence.list_tasks(session_id=getattr(ctx.session, "id", None), limit=1)
            rid = tasks[0]["id"] if tasks else ""
        if not rid:
            return CommandResult(handled=True, is_prompt=False, text="未找到调研任务。")
        if sub == "stop":
            ok = mgr.stop_research(rid)
            return CommandResult(handled=True, is_prompt=False, text=f"⏹️ 调研 `{rid}` {'已停止' if ok else '不存在'}。")
        snap = mgr.snapshot(rid)
        if not snap:
            return CommandResult(handled=True, is_prompt=False, text=f"调研任务 `{rid}` 不存在。")
        t = snap["task"]
        if sub == "report":
            return CommandResult(handled=True, is_prompt=False, text=t.get("report_md") or f"调研 `{rid}` 尚未生成报告（状态 `{t['status']}`）。")
        return CommandResult(
            handled=True,
            is_prompt=False,
            text=(
                f"### 🔎 调研 `{t['id']}`\n"
                f"- **状态**: `{t['status']}` | **模式**: `{t['mode']}` | **来源**: `{len(snap['sources'])}` | **论断**: `{len(snap['claims'])}`\n"
                f"- **问题**: {t['question']}\n"
                f"- **被拦截域**: `{len(t.get('blocked_domains') or {})}`"
            ),
        )

    sid = getattr(ctx.session, "id", "") if ctx.session else ""
    try:
        snap = mgr.start_research(raw, session_id=sid)
    except Exception as e:  # noqa: BLE001
        return CommandResult(handled=True, is_prompt=False, text=f"❌ 无法启动调研: {e}")
    t = snap["task"]
    return CommandResult(
        handled=True,
        is_prompt=False,
        text=(
            f"### 🔎 已启动联网调研 `{t['id']}`\n"
            f"- **问题**: {t['question']}\n"
            f"- **模式**: `{t['mode']}` | 可在右侧「调研」面板或 `/research status {t['id']}` 查看实时进展。"
        ),
    )


def _handle_net_command(arg: str, ctx: CommandContext) -> CommandResult:
    mgr = ctx.research or (getattr(ctx.agent, "research", None) if ctx.agent else None)
    if mgr is None:
        return CommandResult(handled=True, is_prompt=False, text="⚠️ 网络账本尚未初始化。")
    raw = (arg or "").strip()
    decision = "deny" if raw == "blocked" else None
    domain = raw if (raw and raw != "blocked") else None
    res = mgr.ledger.query(domain=domain, decision=decision, limit=15)
    sm = res["summary"]
    lines = [
        "### 🌐 网络账本摘要",
        f"- **总请求**: `{sm['total']}` | **放行**: `{sm['allowed']}` | **拦截**: `{sm['blocked']}` | **缓存命中**: `{sm['cached']}` | **流量**: `{sm['total_bytes']} B`",
        "",
    ]
    for e in res["entries"][:10]:
        tag = "⚡缓存" if e["cached"] else e["decision"]
        lines.append(f"- `[{tag}]` `{e['method']}` `{e['domain'] or e['url'][:40]}` ({e['status']}) {e['purpose']} {e['reason'][:60]}")
    if not res["entries"]:
        lines.append("- （暂无出站请求记录）")
    return CommandResult(handled=True, is_prompt=False, text="\n".join(lines))


# ------------------------------------------------------------------ @ 上下文文件引用解析
_AT_MENTION_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_.\-\/\\]+)")


def resolve_context_mentions(
    text: str,
    guard: sec.PathGuard,
    max_file_mb: int = 20,
) -> tuple[str, list[dict[str, Any]], bool]:
    """解析用户消息中的 @relative/path 文件与目录引用。

    安全保障：
    1. 必须经由 guard.resolve 校验：禁止越界访问、禁止读取 data/、敏感保护等。
    2. 遵循 max_file_mb 大小限制，超大文件安全截断。
    3. 二进制/Office/上传文件标记 has_taint=True；工作区内的纯文本代码文件保持 clean。
    """
    matches = list(_AT_MENTION_RE.finditer(text))
    if not matches:
        return text, [], False

    injected_blocks: list[str] = []
    mentions: list[dict[str, Any]] = []
    has_taint = False
    max_bytes = max_file_mb * 1024 * 1024

    for m in matches:
        if m.group(0).lstrip().startswith("@mcp:"):
            continue
        rel = m.group(1).replace("\\", "/").rstrip(".,;:!?，。；！？")
        if not rel or rel == "mcp":
            continue

        try:
            p = guard.resolve(rel, write=False)
        except sec.SecurityError as e:
            injected_blocks.append(f"<!-- @{rel} 安全拦截: {e} -->")
            mentions.append({"path": rel, "status": "blocked", "error": str(e)})
            continue

        if not p.exists():
            continue

        if p.is_dir():
            # 注入目录概览
            entries = []
            try:
                for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:100]:
                    entries.append(f"{item.name}/" if item.is_dir() else item.name)
            except OSError:
                pass
            tree_repr = "\n".join(f"  {e}" for e in entries) or "  (空目录)"
            injected_blocks.append(f'<directory path="{guard.rel(p)}">\n{tree_repr}\n</directory>')
            mentions.append({"path": guard.rel(p), "kind": "dir", "status": "ok"})
            continue

        # 处理文件
        size = 0
        try:
            size = p.stat().st_size
        except OSError:
            pass

        if size > max_bytes:
            injected_blocks.append(f'<!-- @{guard.rel(p)} 文件过大（{size // 1048576}MB > {max_file_mb}MB），已跳过 -->')
            mentions.append({"path": guard.rel(p), "status": "skipped", "error": "file_too_large"})
            continue

        ext = p.suffix.lower()
        rel_path = guard.rel(p)
        risky = rel_path.startswith("uploads/")

        # Office / PDF 文件解析
        from .tools import files as files_mod
        if ext in files_mod.OFFICE_EXT:
            has_taint = True
            try:
                from .tools import office
                doc_text = office.read_document(p, 1, 2000, 40000)
                injected_blocks.append(f'<file path="{rel_path}" type="document" tainted="true">\n{doc_text}\n</file>')
                mentions.append({"path": rel_path, "kind": "document", "status": "ok", "tainted": True})
            except Exception as e:  # noqa: BLE001
                injected_blocks.append(f'<!-- 读取文档 @{rel_path} 失败: {e} -->')
            continue

        # 文本读取与二进制检测
        try:
            raw = p.read_bytes()
        except OSError as e:
            injected_blocks.append(f'<!-- 读取 @{rel_path} 失败: {e} -->')
            continue

        if files_mod.looks_binary(raw):
            has_taint = True
            injected_blocks.append(f'<!-- @{rel_path} 为二进制文件（{size} 字节），无法内联文本内容 -->')
            mentions.append({"path": rel_path, "kind": "binary", "status": "binary", "tainted": True})
            continue

        file_text, enc, _ = files_mod.decode_bytes(raw)
        if len(file_text) > 40000:
            file_text = file_text[:40000] + "\n…(内容过长已截断)"

        taint_attr = ' tainted="true"' if risky else ""
        if risky:
            has_taint = True

        injected_blocks.append(f'<file path="{rel_path}" encoding="{enc}"{taint_attr}>\n{file_text}\n</file>')
        mentions.append({"path": rel_path, "kind": "text", "status": "ok", "tainted": risky})

    augmented = text
    if injected_blocks:
        augmented += "\n\n[用户通过 @ 显式引用的文件上下文]\n" + "\n\n".join(injected_blocks)

    return augmented, mentions, has_taint
