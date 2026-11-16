---
name: diagram-mermaid
description: "生成语法严谨、可读性强的 Mermaid 图表：架构图、流程图、时序图、状态机图与 ER 图（规避解析踩坑）。"
description_en: "Craft clean, syntax-safe Mermaid diagrams (flowchart, sequenceDiagram, stateDiagram-v2, classDiagram, erDiagram)."
version: "1.0.0"
pack: media
default_enabled: false
tags: ["mermaid", "diagram", "flowchart", "architecture"]
when_to_use: "用户要求画系统架构图、业务流程图、时序图、状态机或数据库 ER 图时使用。"
triggers:
  keywords: ["架构图", "流程图", "时序图", "状态图", "mermaid", "er图", "architecture diagram", "sequence diagram", "flowchart"]
  globs: ["*.mmd"]
  mimes: []
requires:
  tools: ["read_skill_file"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 规范 Mermaid 图表生成

可先读取 `references/syntax-tips.md` 规避常见 Mermaid 解析报错。

1. **选对图类型**（仅使用受支持稳定语法）：
   - 模块与数据流：`flowchart LR` / `flowchart TD`
   - 跨组件调用时机：`sequenceDiagram`
   - 生命周期与流转：`stateDiagram-v2`
   - 表结构与关系：`erDiagram`
   - 类与继承：`classDiagram`
2. **语法安全硬规则**：
   - 节点文案含括号、斜杠、冒号或中文标点时，**必须用双引号包裹**，如 `A["引擎管理器 (Engine)"]`；
   - 节点 ID 使用简洁英文/数字/下划线，不要把长中文句子直接作 ID；
   - 禁用内联 HTML 标签（如 `<br>` 改用引号内换行或简短标签）；
   - 单张图节点尽量控制在 18 个以内，过多时按子域 `subgraph` 分组或拆为两张图。
3. 图表下方配 2~3 句核心链路文字解说，指出最关键路径与边界。
