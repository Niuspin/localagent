---
name: source-evaluation
description: "信息源可信度评估：审查来源权威性、时效性、利益相关性与一手证据链，过滤营销软文和二手失真。"
description_en: "Evaluate source authority, recency, conflict of interest, and primary citation chain to filter unreliable claims."
version: "1.0.0"
pack: research
default_enabled: false
tags: ["credibility", "bias", "sources", "epistemology"]
when_to_use: "调研结果相互矛盾、需要筛选一批网址或判断某个博客/评测是否客观可信时使用。"
triggers:
  keywords: ["信源评估", "来源可信度", "评估信息源", "软文识别", "source evaluation", "evaluate credibility", "assess sources"]
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

# 信源可信度与偏见评估

对每个候选网页、报告或数据引用，按以下四维评分并解释理由：
1. **一手度（Provenance）**：是原创实验/官方文档，还是转述甚至 AI 洗稿？顺藤摸瓜找原始链接。
2. **时效性（Recency）**：核实发布/更新日期；技术文档若跨越重大版本迭代需降权标旧。
3. **客观性与利益相关（Bias/COI）**：是否存在赞助评测、自家基准测试挑数据（Cherry-picking）或匿名推广？
4. **可复现性（Verifiability）**：是否公开测试条件、样本量、配置命令与误差范围？

输出表格：`来源 | 类型(Tier 1/2/3) | 时效 | 潜在偏见 | 是否采信及使用限制`。
