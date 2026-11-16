# shell-safe-ops (Compact)

1. 优先内置文件工具，非必要不走 `run_command`。
2. 区分 Windows PowerShell（用 `;` 非 `&&`）与 Bash 引号规范。
3. 批处理先 `--dry-run`/`-WhatIf` 预览列表，严禁拼接未知输入执行。
