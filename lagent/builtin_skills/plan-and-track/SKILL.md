---
name: plan-and-track
description: "复杂多步任务的拆解、依赖排序与 todo 进度追踪。遇到多文件修改、跨阶段项目或长链路目标时先用此技能。"
description_en: "Decompose complex multi-step tasks, order dependencies, and track execution with todo_write."
version: "1.0.0"
pack: core
default_enabled: true
tags: ["planning", "todo", "decomposition", "milestone"]
when_to_use: "任务包含 3 个以上步骤、多文件变更或需要阶段性验证时调用。"
triggers:
  keywords: ["拆解任务", "制定计划", "分步骤", "开发路线", "实施计划", "roadmap", "task breakdown", "plan and track"]
  globs: []
  mimes: []
requires:
  tools: ["todo_write"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 复杂任务拆解与进度追踪

1. **先盘点范围**：用 `list_dir` / `read_file` 快速摸清相关文件与现有约束，不要在信息空白时拍脑袋列计划。
2. **结构化拆步**：用 `todo_write` 把目标拆为 3~7 个可验证的里程碑；每条任务应包含“动作 + 产出 + 验证标准”。
3. **单线聚焦推进**：同一时刻保持 1 项为 `in_progress`，完成后立即更新为 `completed`，再切换下一项。
4. **遇到阻塞及时重排**：若新发现改变了依赖或工作量，先更新 `todo_write` 再继续，避免盲目试错。
5. **收尾核对**：在最终回复前确认 `todo_write` 中无遗留未闭环的阻塞项，并逐项汇报真实完成状态。
