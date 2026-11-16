---
name: literature-review
description: "多篇论文/技术文献综述：建立统一对比矩阵（问题、方法、实验基线、局限），提炼演进脉络与研究空白。"
description_en: "Synthesize multiple papers or technical docs into a comparison matrix of methods, baselines, results, and gaps."
version: "1.0.0"
pack: research
default_enabled: false
tags: ["papers", "academic", "synthesis", "comparison"]
when_to_use: "需要阅读整理多篇 PDF 论文、技术白皮书或做开题/技术预研综述时使用。"
triggers:
  keywords: ["文献综述", "论文对比", "读论文", "综述多篇", "literature review", "paper survey", "compare papers"]
  globs: ["*.pdf"]
  mimes: []
requires:
  tools: ["read_file", "write_file"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 多篇文献横向综述

1. **明确综述轴线**：确定要回答的核心技术问题（例如“不同长上下文压缩算法的精度/延迟权衡”）。
2. **结构化拆解单篇**：对每篇文献提取统一卡片：年份/作者 → 核心假设 → 方法机制 → 数据集与主要指标 → 作者承认的局限。
3. **构建横向对比矩阵**：用 Markdown 表格按维度并列对比，标注不同实验设置不可直接横向比分的口径差异。
4. **综合洞察**：
   - 技术演进路径与共识；
   - 存在分歧的实验结论；
   - 当前尚未解决的空白（Research Gaps）及工程可落地方向。
