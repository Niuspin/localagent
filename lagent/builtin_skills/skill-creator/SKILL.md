---
name: skill-creator
description: "把稳定可复用的工作流沉淀为 Spec v2 标准技能目录：精确设定触发关键词、紧凑精简版、工具权限收敛与评估用例。"
description_en: "Author high-signal Spec v2 skills with tight trigger metadata, compact profile, least-privilege allowed_tools, and eval cases."
version: "1.0.0"
pack: meta
default_enabled: true
tags: ["skills", "meta", "authoring", "spec-v2"]
when_to_use: "用户要求“新建技能 / 沉淀成 skill / 优化现有技能描述或路由触发率”时调用。"
triggers:
  keywords: ["创建技能", "沉淀为技能", "新技能", "编写skill", "新建技能", "create skill", "skill creator", "write a skill"]
  globs: ["**/SKILL.md"]
  mimes: []
requires:
  tools: ["create_skill", "read_skill_file"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 高质量技能沉淀指南（Spec v2）

可读取 `references/spec-v2.md` 核对完整 Spec v2 字段规范。

1. **先确认边界与高频痛点**：确保该技能针对一类高频可复用问题，而不是一次性的流水账命令；确定归属技能包（`core|dev|research|office|media|ops|meta`）。
2. **写好触发契约**：
   - `description` 与 `description_en`：一句讲清“做什么 + 何时用”；
   - `triggers.keywords`：选 4~8 个有区分度的中英文短语，避免与其它技能抢占过宽词汇；
   - 如只需只读分析，设置 `allowed_tools` 收窄权限（最小权限原则）。
3. **写精炼可执行的正文**：`SKILL.md` 控制在 1500 tokens 内，按编号步骤写清“先做什么、红线是什么、怎么验证”；同时准备 ≤ 500 tokens 的 `SKILL.compact.md` 适配小参数本地模型。
4. **配套 ≥ 2 条测试 Case**：在 `evals/cases.json` 编写正向触发与易混淆负向用例，最后通过 `create_skill` 或保存到 `skills/<name>/` 并用 `lint` 检查。
