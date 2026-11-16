---
name: ask-clarify
description: "当需求存在关键歧义、高风险覆盖或多种互斥技术路线时，先用最小化结构化提问澄清再执行。"
description_en: "Identify critical ambiguities or irreversible choices and ask concise clarifying questions before acting."
version: "1.0.0"
pack: core
default_enabled: true
tags: ["clarification", "requirements", "ambiguity"]
when_to_use: "指令模糊且存在破坏性改动、架构分叉或未知业务口径时调用；小细节可直接按合理默认先行。"
triggers:
  keywords: ["澄清需求", "确认口径", "需求不明确", "先问清楚", "clarify requirements", "ambiguous spec"]
  globs: []
  mimes: []
requires:
  tools: ["ask_user"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 精准澄清歧义

1. **判断是否真的需要问**：只有当缺失信息会显著改变产物架构、覆盖不可逆数据或引发高返工成本时才提问；局部命名或格式直接选最稳妥的常规默认值。
2. **先读后问**：提问前先用 `read_file` / `list_dir` 查看现有代码与文档，不要问工作区里已经写明的事实。
3. **一次问清、给出选项**：用 `ask_user` 最多提 1~3 个高杠杆问题，并为每个问题提供明确选项与推荐项（标明默认值）。
4. **记录约定**：用户确认关键偏好后，在报告中复述确认结果；属长期偏好的可用 `memory_save` 记住。
