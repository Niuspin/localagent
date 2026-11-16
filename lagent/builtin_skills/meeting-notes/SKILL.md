---
name: meeting-notes
description: "整理结构化会议纪要：区分已拍板决策、争议未决项、待办 Action Items（含负责人与截止时间）与背景摘要。"
description_en: "Distill messy meeting transcripts into structured minutes: decisions, open questions, and owner-dated action items."
version: "1.0.0"
pack: office
default_enabled: false
tags: ["meeting", "minutes", "action-items", "decisions"]
when_to_use: "收到会议录音转写文本、零散讨论记录或聊天记录需要提炼纪要时使用。"
triggers:
  keywords: ["会议纪要", "整理会议记录", "会议总结", "待办事项提取", "meeting notes", "meeting minutes", "action items"]
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

# 结构化会议纪要整理

1. **去噪提纯**：跳过寒暄和重复拉扯，聚焦议题结论、分歧原因与承诺的行动。
2. **标准输出模板**：
   - **会议信息**：主题、日期、参会方（仅写材料出现的人名/角色）。
   - **核心结论（Decisions）**：已达成一致的事项（附原因简述）。
   - **待办行动项（Action Items）**：表格列 `事项 | 负责人 | 截止日期 | 状态/依赖`；原记录未写明负责人的标注 `[待认领]`，严禁瞎猜人名。
   - **悬而未决 / 需后续跟进的问题**。
   - **议题讨论摘要**（简明扼要保留关键论据）。
3. 若输入含多语言缩写或专业术语，保留原文并在首次出现处加简短括号说明。
