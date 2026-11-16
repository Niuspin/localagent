---
name: report-writer
description: "把多源材料整理为结构严谨的专业报告、方案或简报，支持输出 Markdown、Word(.docx) 与 PDF。"
description_en: "Synthesize materials into structured executive reports, proposals, or briefs exported as Markdown, Word, or PDF."
version: "1.0.0"
pack: office
default_enabled: true
tags: ["report", "docx", "pdf", "markdown", "writing"]
when_to_use: "需要写研究报告、项目方案、月报总结或正式 Word/PDF 交付物时使用。"
triggers:
  keywords: ["写报告", "写一份报告", "撰写报告", "整理成文档", "做简报", "项目方案", "导出成 word", "选型报告", "report writer", "write report", "executive brief"]
  globs: ["*.docx", "*.pdf"]
  mimes: ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
requires:
  tools: ["read_file", "write_document"]
  capabilities: ["office"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 专业报告与文档撰写（v2）

可参考 `references/templates.md` 选择“研究简报 / 技术方案 / 项目周月报”版式骨架。

1. **定口径与读者**：明确读者（决策层/技术团队/客户）与篇幅；未指明时默认“半页结论摘要 + 分章节正文 + 风险与行动项”。
2. **先读齐素材**：用 `glob`/`read_file` 读全工作区背景文档与数据文件，确保引用数字、文件名准确无误。
3. **金字塔结构**：
   - **标题与元数据**（日期、范围、作者/状态）
   - **执行摘要（结论先行）**
   - **背景与现状**
   - **核心分析与要点**（小标题 + 要点列表 + 对比表格，一段只讲一件事）
   - **风险与下一步行动（负责人/优先级）**
   - **参考来源 / 附录**
4. **高质量输出**：Markdown 写 `write_file`，Word/PDF 优先调用 `write_document`（或用 `edit_office` 修改原件），文件默认放到工作区 `output/` 目录。
5. **交付回读**：生成后用 `read_file` 或检查工具核对排版、表头与数字单位无缺失或乱码。
