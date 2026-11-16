---
name: image-describe-ocr
description: "图片/截图/扫描件解析与 OCR 提取：结构化还原文字、表格布局、UI 状态与异常报错细节。"
description_en: "Inspect screenshots, diagrams, and scans to extract structured text/OCR, layout details, and UI errors."
version: "1.0.0"
pack: media
default_enabled: false
tags: ["vision", "ocr", "screenshot", "image"]
when_to_use: "用户上传截图、扫描件、图表图片并要求识字、解读画面或排查 UI 报错时使用。"
triggers:
  keywords: ["看图", "图片识别", "提取文字", "ocr", "截图分析", "图片里的内容", "describe image", "extract text from image"]
  globs: ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"]
  mimes: ["image/*"]
requires:
  tools: ["read_file"]
  capabilities: ["vision"]
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 视觉理解与图像 OCR 提取

1. **确认视觉与 OCR 状态**：若当前模型支持视觉（`vision=True`），直接读取图像输入并结合摄取摘要交叉对照；若当前为纯文本模型且无清晰 OCR 文本，如实说明受限于纯文本模式，无法核实图像视觉细节。
2. **结构化还原内容**：
   - **纯文字/文档截图**：按阅读顺序还原段落与代码块，表格还原为 Markdown 表格；
   - **软件界面/报错截图**：指出当前页面模块、关键提示文字、红字报错堆栈、高亮或按钮状态；
   - **数据图表**：读取坐标轴含义、图例与大致趋势；无法看清的精确刻度标 `[模糊/无法看清]`，绝不臆造具体小数。
3. **如实标注不确定度**：反光、裁剪或极小字体导致辨认存疑的字词用 `[?]` 标出并提示用户放大裁剪重传。
