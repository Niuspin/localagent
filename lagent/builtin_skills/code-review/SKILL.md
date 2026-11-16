---
name: code-review
description: "专业代码审查（只读）：从正确性、边界条件、安全隐患、并发/状态与测试覆盖五个维度评审变更并分级给出建议。"
description_en: "Read-only code/PR review across correctness, edge cases, security, concurrency, and test coverage."
version: "1.0.0"
pack: dev
default_enabled: true
tags: ["review", "pr", "diff", "quality"]
when_to_use: "用户要求 review 代码、评审 diff/PR 或核查改动是否存在隐患时调用。"
triggers:
  keywords: ["代码审查", "审查代码", "帮我review", "检查diff", "code review", "review pr", "review diff"]
  globs: []
  mimes: []
requires:
  tools: ["git_diff", "read_file"]
  capabilities: []
  binaries: []
  python: []
allowed_tools: ["list_dir", "grep", "glob", "read_file", "git_status", "git_diff", "git_log", "use_skill", "read_skill_file"]
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 代码变更审查清单

> 权限约束：本技能为只读评审，仅查看变更与周边上下文，不擅自修改源码。

1. **获取完整变更上下文**：先运行 `git_status` 与 `git_diff`，再用 `read_file` 阅读变更函数所在模块的完整逻辑及调用方。
2. **五维审查清单**：
   - **正确性与边界**：空值/空集合/超时/异常分支、状态机合法转换、off-by-one 误差。
   - **安全与权限**：路径遍历、命令注入、XSS/SSRF、敏感信息泄露、默认权限是否只收窄不放宽。
   - **并发与资源**：竞态条件、锁粒度、事件循环阻塞、文件/连接泄漏。
   - **简洁与复用**：是否存在可提取的重复逻辑或不必要的过度复杂化。
   - **测试覆盖**：新逻辑和边界分支是否有对应单测/回归测试，有无跳过或放宽断言。
3. **分级输出**：按 `Blocker（必改）` / `Major（建议改）` / `Nit（微调）` 列出具体文件、行号、风险原因与修改方向；无重大问题时也明示审查覆盖范围与残余风险。
