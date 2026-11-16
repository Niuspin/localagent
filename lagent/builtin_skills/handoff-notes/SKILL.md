---
name: handoff-notes
description: "整理会话或项目的交接备忘录：记录已完成事项、关键决策、剩余问题、环境命令与下一步行动。"
description_en: "Generate crisp session or project handoff notes covering decisions, state, blockers, and next steps."
version: "1.0.0"
pack: core
default_enabled: false
tags: ["handoff", "context", "session", "status"]
when_to_use: "长会话即将结束、准备交接给其他团队成员或下一次迭代继续推进时使用。"
triggers:
  keywords: ["交接文档", "会话交接", "进度备忘", "handoff notes", "context handoff", "resume guide"]
  globs: []
  mimes: []
requires:
  tools: ["read_file", "write_file"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 交接备忘录整理

1. **盘点当前状态**：检查 `git_status` / `todo_write` / 测试结果，区分“已验证完成”“代码已改未验证”“尚未开始”。
2. **编写交接卡片**（可按需写入 `HANDOFF.md` 或直接回复）：
   - **目标与范围**：本次要解决什么问题。
   - **关键已定决策**：采纳了什么方案及原因，拒绝了哪些替代方案。
   - **文件改动索引**：关键文件 + 行区间 + 一句话说明。
   - **验证实录**：跑了哪些命令，真实输出是什么。
   - **下一步行动（Next Steps）**：按顺序写出接手人立刻要执行的第 1、2、3 步。
3. 只记录当前磁盘与工具可核实的事实，不写猜测性状态。
