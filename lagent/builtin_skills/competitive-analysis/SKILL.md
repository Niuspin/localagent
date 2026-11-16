---
name: competitive-analysis
description: "竞品分析框架：从定位受众、核心能力矩阵、定价生态、技术护城河与优劣势进行客观对比并给出差异化建议。"
description_en: "Structured competitive analysis across positioning, feature matrix, pricing, architecture, and differentiation."
version: "1.0.0"
pack: research
default_enabled: false
tags: ["competitor", "product", "benchmark", "strategy"]
when_to_use: "分析同类产品/开源方案差异、撰写产品对比评估或规划功能差异化时使用。"
triggers:
  keywords: ["竞品分析", "产品对比", "竞品调研", "对标分析", "competitive analysis", "competitor comparison", "feature matrix"]
  globs: []
  mimes: []
requires:
  tools: ["web_search", "web_fetch"]
  capabilities: []
  binaries: []
  python: []
risk: network
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 结构化竞品分析

1. **选定参照系**：明确我方目标与 3~5 个代表性竞品（覆盖头部标准产品与敏捷替代品）。
2. **统一评估维度**：
   - 目标客群与核心场景；
   - 功能矩阵（有/无/受限，标注核实日期）；
   - 定价模式、开源协议与本地/云端部署成本；
   - 性能/体验短板与社区反馈痛点。
3. **避免堆砌流水账**：每条差异点需回答“这对用户选型意味着什么”。
4. **输出产物**：执行摘要 + 对比矩阵表 + 关键差异点剖析 + 可落地的切入/防御策略建议；所有外部信息附上官方来源链接。
