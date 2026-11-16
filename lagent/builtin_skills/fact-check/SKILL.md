---
name: fact-check
description: "逐声明核查文本/报道中的事实真伪：拆分原子命题、查找一手正反证据并给出置信评级。"
description_en: "Decompose text into atomic claims, verify each against primary and counter-evidence, and assign confidence ratings."
version: "1.0.0"
pack: research
default_enabled: false
tags: ["verification", "fact-check", "claims", "accuracy"]
when_to_use: "核验文章数据是否靠谱、辨别网传结论真假或审查引用是否张冠李戴时使用。"
triggers:
  keywords: ["事实核查", "核实真伪", "查证声明", "辟谣核验", "fact check", "verify claims", "is it true that"]
  globs: []
  mimes: []
requires:
  tools: ["web_search", "web_fetch"]
  capabilities: ["network"]
  binaries: []
  python: []
risk: network
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 逐声明事实核查

1. **提取原子声明**：把待核查材料拆成一条条“主语 + 可检验事实/数字/时间”的独立声明，过滤纯粹主观评价。
2. **双向检索**：针对每条声明分别搜索支持证据和反驳/勘误报道；优先追溯最早一手出处（原始论文、官网公告、财报或代码提交）。
3. **对比四要素**：人物/主体、数值与口径、时间窗、前提条件是否被断章取义或混淆。
4. **评级输出**：为每条声明标明状态：
   - `确认属实 (Verified)`
   - `部分属实 / 缺条件 (Mixed)`
   - `不实 / 误导 (False)`
   - `无法核实 (Unverified)`
   并逐条附上一手来源与核对理由。
