---
name: security-review
description: "应用与代码安全审计：对照 OWASP 威胁模型排查注入、越权、路径穿越、SSR/XSS、密钥暴露与反序列化漏洞。"
description_en: "Security review against OWASP risks: injection, path traversal, auth bypass, XSS/SSRF, and secret leakage."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["security", "owasp", "audit", "vulnerability"]
when_to_use: "新增网络接口、文件操作、命令执行、认证鉴权或技能/插件沙箱逻辑后进行安全把关。"
triggers:
  keywords: ["安全审计", "安全审查", "漏洞排查", "安全风险", "owasp", "security review", "vulnerability audit"]
  globs: []
  mimes: []
requires:
  tools: ["grep", "read_file", "read_skill_file"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 应用安全威胁排查

可先用 `read_skill_file` 读取 `references/owasp-checklist.md` 获取详细检查点。

1. **划定信任边界**：区分可信配置输入、用户直传输入与外部不可信数据（网页/邮件/文档/MCP 返回）。
2. **逐项穿透高危接口**：
   - **路径安全**：是否使用统一沙箱守卫校验（`realpath` 后检查包含关系与符号链接/Windows 保留名）？
   - **命令与模板注入**：是否避免了 `shell=True`、字符串拼接 SQL/HTML/Eval？
   - **鉴权与单向收窄**：角色或技能白名单是否只能收窄不能放宽？敏感端点是否有鉴权与审批门禁？
   - **凭据隔离**：日志、SSE、异常堆栈、持久化存储是否可能泄露 Token/密码？
3. **输出威胁报告**：每条发现写明“攻击面 → 触发路径（文件:行号）→ 影响等级（高/中/低）→ 最小修复方案”。
