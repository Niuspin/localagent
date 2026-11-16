---
name: code-explore
description: "只读探索陌生代码库：定位入口、追踪调用链、识别模块边界与技术债（严格只读，不改动文件）。"
description_en: "Strictly read-only codebase exploration: find entry points, trace call chains, and map architecture."
version: "1.0.0"
pack: dev
default_enabled: true
tags: ["codebase", "architecture", "search", "readonly"]
when_to_use: "接手新项目、理解模块实现原理或解答“某功能在哪实现”时使用。"
triggers:
  keywords: ["梳理代码", "代码架构", "在哪里实现", "调用链", "摸清代码库", "explore codebase", "trace call chain", "where is implemented"]
  globs: []
  mimes: []
requires:
  tools: ["list_dir", "grep", "read_file"]
  capabilities: []
  binaries: []
  python: []
allowed_tools: ["list_dir", "find_files", "glob", "grep", "read_file", "git_status", "git_diff", "git_log", "use_skill", "read_skill_file"]
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 只读代码库探索

> 权限约束：本技能仅使用只读检索工具，不修改工作区文件。

1. **先看全局骨架**：`list_dir` 查看根目录、`README.md` 与 `pyproject.toml`/`package.json`，确认入口文件、依赖与运行方式。
2. **定位而非通读**：先用 `grep` / `find_files` 找到目标符号、路由或配置项，再按精确行区间 `read_file`，避免整文件通读浪费上下文。
3. **沿着调用链追踪**：入口 → 路由/控制器 → 核心服务/状态机 → 数据存储/外部边界；记下每段关键路径与行号。
4. **输出架构图谱**：用列表或 Mermaid 说明核心模块职责、数据流向、配置入口与扩展点，所有结论必须附带具体文件路径与行号。
