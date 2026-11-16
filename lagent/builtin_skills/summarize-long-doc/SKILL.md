---
name: summarize-long-doc
description: "超长文档/大文件的分层分段摘要：先定位结构再分段提炼，保留关键数据、页码/行号与证据映射。"
description_en: "Map-reduce summarization for long documents, preserving key figures, headings, and section citations."
version: "1.0.0"
pack: core
default_enabled: false
tags: ["summary", "long-context", "reading", "map-reduce"]
when_to_use: "遇到长 PDF、长日志、大型规范文档或需要跨章节提炼要点时使用。"
triggers:
  keywords: ["长文档总结", "全文摘要", "分段总结", "万字文档", "summarize long document", "executive summary"]
  globs: ["*.pdf", "*.txt", "*.md"]
  mimes: ["application/pdf", "text/plain"]
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

# 超长文档分层摘要

1. **先摸目录骨架**：用 `read_file` 分页读取前几段目录/标题与末尾结论，确定全文结构与长度，避免一次塞爆上下文。
2. **分块映射（Map）**：按章节或固定行数区间分块阅读，每块提取 3~5 条核心论断、关键数字（带单位/口径）与对应行号/页码。
3. **归并去重（Reduce）**：合并重复论述，标出跨章节的因果链或前后矛盾点。
4. **分层输出**：
   - **三句话结论**（TL;DR）
   - **核心要点清单**（附章节/行号定位）
   - **关键数据表 / 未决风险**
5. 遇大工具输出被落盘时，用 `read_output` 精准取回片段，不盲目重读全文。
