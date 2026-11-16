---
name: refactor-safe
description: "安全渐进式重构：先确认测试守卫与调用方清单，再小步抽取公共逻辑或拆解大函数，步步验证行为等价。"
description_en: "Safe incremental refactoring with baseline test guards, caller audits, and step-by-step verification."
version: "1.0.0"
pack: dev
default_enabled: true
tags: ["refactor", "clean-code", "behavior-preserving"]
when_to_use: "清理重复代码、拆分过长函数、改善模块耦合且必须保证外部契约不变时使用。"
triggers:
  keywords: ["重构代码", "安全重构", "抽取公共函数", "拆解大函数", "消除重复代码", "refactor", "extract helper", "deduplicate code"]
  globs: []
  mimes: []
requires:
  tools: ["grep", "read_file", "edit_file", "run_command"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 安全渐进式重构

1. **建立基线守卫**：重构前先运行现有测试；若核心逻辑缺乏测试覆盖，先补特征测试（Characterization Tests）锁定当前行为。
2. **盘点所有调用方**：用 `grep` 找出被改函数/类的全部引用位置（含跨模块、测试与配置）。
3. **小步等价变换**：
   - 一次只做一类改造（例如先提取共享 helper，再统一配置分支，最后删旧代码）；
   - 重构期间不要顺便夹带功能行为变更；
   - 保留原有公开 API 签名与错误语义，除非用户明确同意变更。
4. **步步跑测**：每完成一小步即运行受影响测试；收尾前跑完整个相关测试集与语法/类型检查。
