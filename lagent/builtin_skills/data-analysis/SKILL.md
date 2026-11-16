---
name: data-analysis
description: "分析 CSV/Excel/JSON 表格数据：检查脏数据、用 Python 精确计算指标并产出汇总表与关键业务洞察。"
description_en: "Perform reproducible exploratory analysis, cleaning, aggregation, and reporting on CSV/Excel/JSON datasets."
version: "1.0.0"
pack: office
default_enabled: true
tags: ["csv", "excel", "statistics", "pandas", "eda"]
when_to_use: "用户提供或工作区存在 `.csv` / `.xlsx` / 数据表并要求清洗、统计、透视或找规律时使用。"
triggers:
  keywords: ["数据分析", "分析数据", "数据清洗", "数据透视", "统计规律", "csv分析", "data analysis", "analyze dataset", "exploratory analysis"]
  globs: ["*.csv", "*.tsv", "*.xlsx", "*.xls", "*.parquet"]
  mimes: ["text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
requires:
  tools: ["read_file", "run_command", "write_spreadsheet"]
  capabilities: ["office"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 可复现数据分析（v2）

可读取 `references/checklist.md` 核对数据质量检查项。

1. **先探查再算数**：用 `read_file` 看样本行与字段名，检查编码、日期格式、缺失值、重复主键及极端异常值，并先向用户提示明显数据瑕疵。
2. **禁止心算**：所有求和、均值、中位数、同比/环比、分组透视和相关性一律在工作区 `scratch/` 编写干净 Python 脚本通过 `run_command` 运行（优先用 `pandas`/`numpy` 或标准库 `csv`/`statistics`），保证完全可复现。
3. **只读原始文件**：绝不覆盖原始 CSV/Excel；清洗表与透视结果输出到 `output/`（CSV 或用 `write_spreadsheet` 生成多 Sheet Excel）。
4. **透明口径与汇报**：
   - 先列 3~5 条可操作核心发现（带具体数字、样本量与单位）；
   - 写明数据清洗规则（剔除了多少空行/异常行、歧义口径采用了什么假设）；
   - 附上输出产物与分析脚本路径。
