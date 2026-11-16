---
name: presentation-builder
description: "规划演示文稿逻辑并生成 PPTX：一页一个核心观点、行动式标题（Action Title）、克制字数与配图提示。"
description_en: "Structure slide narratives with action titles, concise bullets, visual layout hints, and export via write_presentation."
version: "1.0.0"
pack: office
default_enabled: false
tags: ["pptx", "slides", "presentation", "deck"]
when_to_use: "需要将报告、项目总结或技术分享做成幻灯片（PPT / PPTX）时使用。"
triggers:
  keywords: ["做ppt", "做 ppt", "做幻灯片", "制作演示文稿", "演示文稿", "写ppt大纲", "presentation", "slide deck", "build slides", "pptx"]
  globs: ["*.pptx"]
  mimes: ["application/vnd.openxmlformats-officedocument.presentationml.presentation"]
requires:
  tools: ["write_presentation"]
  capabilities: ["office"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 演示文稿设计与制作

1. **先写 Storyline 大纲**：把全套幻灯片控制在合适页数（汇报型 8~15 页）：封面 → 执行结论 → 背景冲突 → 方案分解 → 关键数据/路径 → 风险与行动号召。
2. **单页排版铁律**：
   - **观点型标题**：不用“系统架构”这种词，改用结论句“三层解耦架构将推理延迟降低 35%”；
   - **密度克制**：每页不超过 3~5 条要点、每条不超过 15~20 字；能用对比表或数据图表达的绝不堆满大段文本；
   - **视觉节拍**：正文页在文字列表、左右对比栏、数据表格之间交替切换。
3. **生成与验收**：调用 `write_presentation` 输出到 `output/`；写好演讲者备注（Speaker Notes）补充细节；完成后检查页数和每页文字是否溢出。
