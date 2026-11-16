---
name: chart-maker
description: "高质量数据可视化与图表导出：依问题选对图型（趋势/对比/分布/占比），编写 Python 绘图脚本输出高分辨率图片。"
description_en: "Select optimal chart types and generate publication-ready PNG/SVG plots cleanly with Python."
version: "1.0.0"
pack: media
default_enabled: false
tags: ["visualization", "charts", "matplotlib", "plotting"]
when_to_use: "需要将统计数据、测试基准或业务趋势渲染为直观可保存的图表文件时使用。"
triggers:
  keywords: ["画图", "生成图表", "折线图", "柱状图", "散点图", "数据可视化", "make chart", "plot graph", "matplotlib chart"]
  globs: []
  mimes: []
requires:
  tools: ["run_command", "write_file"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 数据可视化与图表制作

1. **按信息关系选图型**：
   - 时间趋势 → 折线图 / 面积图；
   - 类别对比 → 水平或垂直柱状图（大类从大到小排序）；
   - 分布与离散度 → 直方图 / 箱线图；
   - 相关性 → 散点图；
   - 占比构成 → 堆叠柱状图（少于 5 类才考虑饼图）。
2. **编写高可读性脚本**：在 `scratch/` 写脚本（如 `matplotlib`/`svg` 纯脚本绘制），确保：
   - 处理中文字体或安全回退到清晰字体/英文标签避免方块乱码（先测字体可用性）；
   - 必须有**结论性标题 + 带单位的轴标签 + 清晰图例 + 数据来源脚标**；
   - Y 轴柱状图从 0 开始，禁止截断误导比例；
   - 输出高分辨率文件到 `output/`（>=150 DPI）。
3. **产后检查**：如支持视觉，可用 `read_file` 或预览检验文字未重叠遮挡，并汇报文件路径与图片核心洞察。
