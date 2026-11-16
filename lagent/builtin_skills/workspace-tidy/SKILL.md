---
name: workspace-tidy
description: "安全清理与归档工作区文件：只读盘点重复和临时文件、先出变更表经确认再执行、删除走可恢复回收站。"
description_en: "Safely inventory, classify, archive, and clean workspace files with confirmation and recoverable trash deletion."
version: "1.0.0"
pack: ops
default_enabled: true
tags: ["cleanup", "files", "organization", "trash"]
when_to_use: "用户要求整理、归类、重命名或清理工作区杂乱/临时文件时调用。"
triggers:
  keywords: ["整理文件夹", "清理工作区", "归档文件", "整理目录", "清理临时文件", "workspace tidy", "clean workspace", "organize files"]
  globs: []
  mimes: []
requires:
  tools: ["list_dir", "glob", "move_path", "delete_path"]
  capabilities: ["fileops"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 安全整理工作区（v2）

1. **只读全面盘点**：先用 `list_dir` 与 `glob` 统计文件类型、大小、最近修改时间，识别同名副本和临时垃圾（`~$*`、`.tmp`、`.bak`、`__pycache__`）。
2. **先出方案、获批再动**：以表格形式列出 `原路径 → 目标归档路径` 与 `拟移入回收站项`（含体积总计），通过 `ask_user` 或回复请示确认；严禁未经确认批量移动或删文件。
3. **严格用内置安全文件工具**：用 `make_dir`、`move_path` 归类，用 `delete_path` 删除（进入系统可控回收站，支持面板一键还原）；不要用 `run_command` 的 `rm`/`del`/`mv` 绕过回收站。
4. **绝对禁区**：不碰 `.git`、`node_modules`、`.venv`、配置/密钥类文件及用户未授权目录。
5. 汇报：列出已归档与删除清单，并提示用户可在“工作区面板 → 变更 / 回收站”随时撤销。
