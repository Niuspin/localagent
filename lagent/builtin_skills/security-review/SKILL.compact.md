# security-review (Compact)

1. 划定可信/不可信输入边界（详见 `references/owasp-checklist.md`）。
2. 重点核查路径校验、命令/XSS 注入、权限单向收窄、凭据脱敏。
3. 报告按高/中/低风险列出位置与修复方案。
