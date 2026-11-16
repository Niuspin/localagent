---
name: docs-writer
description: "编写与同步工程文档（README、API 手册、CHANGELOG、配置说明），确保参数、示例与当前源码严格一致。"
description_en: "Author and sync README, API docs, and changelogs verified strictly against actual source code."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["documentation", "readme", "api-docs", "changelog"]
when_to_use: "新增接口/配置后同步文档，或需要为项目梳理清晰入门指南与参考手册时使用。"
triggers:
  keywords: ["写文档", "更新readme", "接口文档", "api文档", "更新changelog", "docs writer", "update readme", "api documentation"]
  globs: ["README*.md", "docs/*.md", "CHANGELOG.md"]
  mimes: []
requires:
  tools: ["read_file", "edit_file", "write_file"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 工程文档编写与同步

1. **源码优先**：编写文档前先用 `read_file` / `grep` 核实真实路由路径、HTTP 方法、请求/响应字段、CLI 参数与默认值——绝不凭记忆编造接口字段。
2. **结构面向读者**：
   - README：一句话定位 → 核心特性 → 5 分钟快速启动 → 配置与安全说明；
   - API 文档：Endpoint + 方法 + 参数表 + 示例 Payload + 返回字段说明与错误码。
3. **最小精确更新**：保留现有非相关注释和段落结构，用 `edit_file` 局部补充或修订；新示例命令要在本地核对可运行语义。
4. **收尾复核**：检查 Markdown 链接路径是否有效、表格列对齐、版本号与代码一致。
