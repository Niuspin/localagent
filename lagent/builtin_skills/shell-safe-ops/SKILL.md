---
name: shell-safe-ops
description: "跨平台（Windows PowerShell / Linux Bash）安全命令行实践：干跑预览、避免路径空格与编码陷阱、确保操作可逆。"
description_en: "Cross-platform safe shell execution (PowerShell/Bash): dry-run previews, quoting safety, and reversible commands."
version: "1.0.0"
pack: ops
default_enabled: false
tags: ["shell", "powershell", "bash", "ops", "dry-run"]
when_to_use: "必须使用 `run_command` 进行批量环境检查、进程/构建脚本运行或跨系统脚本编写时调用。"
triggers:
  keywords: ["安全执行命令", "shell脚本", "powershell命令", "批量命令行", "干跑预览", "shell safe ops", "safe command", "powershell script"]
  globs: ["*.sh", "*.ps1"]
  mimes: []
requires:
  tools: ["run_command"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 跨平台安全命令行操作规范

1. **优先内置只读/安全工具**：能用 `list_dir` / `grep` / `read_file` / `delete_path` 完成的操作，绝不退化为 shell 命令。
2. **识别宿主环境与语法**：
   - Windows PowerShell 用 `;` 分隔命令（不用 Bash 的 `&&`）；参数包含空格时务必加引号，变量用 `$env:VAR`；
   - Linux/macOS Bash 尽量开 `set -euo pipefail`，并对 `"$var"` 全程双引号包裹。
3. **先干跑/只读预览再执行**：涉及批量重命名、打包或环境清理时，先跑 `--dry-run` / `-WhatIf` 或只打印拟处理列表确认数量无误，再请求真实执行。
4. **守住权限边界**：严禁写死硬编码凭据、禁用沙箱外访问、不用 `Invoke-Expression`/`eval` 拼接未知输入。
