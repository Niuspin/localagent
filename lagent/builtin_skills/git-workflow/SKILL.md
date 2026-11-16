---
name: git-workflow
description: "安全规范的 Git 协同流程：状态核查、语义化提交信息、分支合并/冲突解决与防误删保护。"
description_en: "Safe Git workflow for status checks, clean commits, branching, and conflict resolution without destructive history loss."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["git", "branch", "commit", "merge", "conflict"]
when_to_use: "整理提交、切分支、解合并冲突、排查某行历史改动责任时使用。"
triggers:
  keywords: ["git提交", "合并冲突", "提交规范", "git分支", "git workflow", "resolve conflict", "commit message"]
  globs: []
  mimes: []
requires:
  tools: ["git_status", "git_diff", "git_log"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 安全 Git 操作指南

1. **操作前先探查**：任何提交或切分支前，先检查 `git_status`、`git_diff` 与 `git_log`（近 5 条了解仓库提交风格）。
2. **禁止高危重写（除非明确要求）**：绝不擅自执行 `git reset --hard`、`git push --force`、`git clean -fd` 或跳过 pre-commit hook；绝不直接读写 `.git/` 内部文件。
3. **检查敏感信息**：`git_diff` 核对无 `.env`、API Key、临时大二进制、误改的无关文件后再暂存提交。
4. **解冲突策略**：先理解双方改动意图，手工合并保留两边有效变更，合并后立即跑关键测试，不能仅靠“采用我方/对方”简单覆盖。
5. **清晰 Commit Message**：概括“做了什么 + 为什么（Why）”，与仓库惯例（Conventional Commits 或中文前缀）一致。
