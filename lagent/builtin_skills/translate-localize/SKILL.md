---
name: translate-localize
description: "高信噪比技术/文档翻译与本地化：维护专有术语表一致、保留代码块/格式标记且符合目标语言母语表达习惯。"
description_en: "Technical translation and localization preserving code/formatting, enforcing consistent terminology, and natural idiom."
version: "1.0.0"
pack: office
default_enabled: false
tags: ["translation", "localization", "bilingual", "terminology"]
when_to_use: "需要把 README、界面文案、产品手册或报告精准地在中英文之间互译与润色时使用。"
triggers:
  keywords: ["翻译成", "本地化润色", "中英互译", "双语对照", "术语表翻译", "translate to", "localize text", "bilingual translation"]
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

# 技术翻译与本地化

1. **先抽术语表**：通读开头，识别专有名词、产品名、命令名与缩写，确定统一译法（不该译的标识符如 API 名、路径、变量名保留原文）。
2. **格式无损**：完整保留 Markdown 标题、表格、列表缩进、行内代码 `` `code` ``、代码块与链接 URL，不篡改格式符号。
3. **地道的本地化表达（信达雅）**：
   - 译为中文时去掉生硬的英式长定语和多余被动语态，使用流畅的中文技术行文；
   - 译为英文时使用精准动宾结构与地道工程用词，避免逐字硬译；
   - 中文混排中英文/数字时保持清晰一致的空格规范。
4. 重要翻译交付后附上 **本篇关键术语对照表**，便于团队后续复用；长文件建议对照原段校对无漏译。
