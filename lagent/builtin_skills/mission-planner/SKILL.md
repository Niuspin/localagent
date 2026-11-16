---
name: mission-planner
description: "规划长时间多轮自主任务（Mission）：划分里程碑、配置预算护栏、审批停顿点与 git 断点回滚策略。"
description_en: "Plan long-running autonomous Missions with clear milestones, time/token budgets, approval gates, and rollback targets."
version: "1.0.0"
pack: meta
default_enabled: false
tags: ["mission", "autonomous", "milestones", "budget"]
when_to_use: "任务周期跨越多次运行会话、需要定时推进、断点续跑与预算控制时使用 `mission_*` 工具。"
triggers:
  keywords: ["长期任务", "自主任务规划", "mission计划", "断点续跑", "预算护栏", "mission planner", "autonomous mission", "milestone budget"]
  globs: []
  mimes: []
requires:
  tools: ["mission_create", "mission_start", "mission_status"]
  capabilities: ["mission"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 长程自主任务（Mission）规划

1. **明确终态与验收红线**：用可机检条件定义成功标准（如特定测试全过、产物文件结构通过检查），避免模糊目标导致空转消耗预算。
2. **拆分高内聚里程碑**：将大目标拆为 3~6 个阶段里程碑，每个里程碑应能产生一个可提交或可验证的中间状态（Checkpoint）。
3. **设置安全与预算参数**：
   - 合理设定单集最大步数与累计时间/预算阈值；
   - 对涉及外部发布、全量数据清洗或删改核心配置的关键节点设置“需人工审批暂停（Approval Gate）”。
4. **可回滚原则**：每完成一个关键里程碑记录回滚锚点（Commit / Milestone ID），一旦后续阶段卡死或偏航可干净回滚。
