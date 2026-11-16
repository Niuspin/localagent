---
name: spreadsheet-modeler
description: "设计与生成多工作表 Excel 模型：结构分离（假设/明细/汇总）、动态 Excel 公式（非死数值）与格式美化。"
description_en: "Build multi-sheet Excel workbooks with separated assumptions, dynamic Excel formulas, and clean formatting."
version: "1.0.0"
pack: office
default_enabled: false
tags: ["excel", "spreadsheet", "formulas", "financial-model"]
when_to_use: "需要制作预算表、ROI 测算表、排期表或多 Sheet 带公式联动的工作簿时使用。"
triggers:
  keywords: ["做excel", "制作表格", "财务模型", "预算表", "动态公式", "多sheet", "spreadsheet model", "excel workbook", "budget sheet"]
  globs: ["*.xlsx"]
  mimes: ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
requires:
  tools: ["write_spreadsheet", "edit_office"]
  capabilities: ["office"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 多工作表电子表格建模

1. **三层结构规划**：复杂表格拆分为 `Assumptions（关键假设参数）`、`Data（基础明细）`、`Summary（汇总看板）` 三个 Sheet，避免参数与计算混在一起。
2. **公式优先于硬编码**：合计、占比、毛利率、情景敏感性尽量写 Excel 公式（`SUM`/`SUMIFS`/`AVERAGE`/`IF`），让用户后续改一个假设参数就能整表联动重算。
3. **可读性与防御性**：
   - 首行冻结表头、明确货币/百分比/千分位格式及列名单位；
   - 重要除法用 `IFERROR(..., 0)` 防止 `#DIV/0!`；
   - 在汇总页附“模型说明与校验行（如借贷平衡检查 `=0`）”。
4. 用 `write_spreadsheet` 生成到 `output/`，改已有表用 `edit_office`；生成后回查无公式拼写或引用错位。
