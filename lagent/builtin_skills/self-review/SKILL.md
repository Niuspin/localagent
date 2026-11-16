---
name: self-review
description: "任务交付前自检：核对改动文件、验证测试输出、检查边界遗漏并确保回答只陈述真实观察到的事实。"
description_en: "Perform pre-delivery self-review: inspect diffs, verify tests passed after final edits, and check edge cases."
version: "1.0.0"
pack: core
default_enabled: true
tags: ["quality", "verification", "review", "checklist"]
when_to_use: "准备宣布完成实现、重构或调研结论前调用，把好最后一道质量关。"
triggers:
  keywords: ["交付前自检", "验收检查", "自我审查", "检查遗漏", "self review", "pre-delivery check"]
  globs: []
  mimes: []
requires:
  tools: ["read_file"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 交付前自检清单

1. **回归需求边界**：对照用户原始请求的每一项约束，检查是否有半截子功能、TODO 占位或未接入的模块。
2. **回读改动**：对最后修改过的关键文件回读或检查 `git_diff`，确认无多余调试语句、死代码或硬编码路径。
3. **验证命令时效性**：若验证后又做过任何编辑，必须重新运行测试与检查；不得把旧的通过结果当成最新状态。
4. **事实一致性**：最终汇报里提到的每个文件名、符号名、行数和通过/失败计数，必须来自本轮实测工具输出；未检查的内容明确说明未检查。
