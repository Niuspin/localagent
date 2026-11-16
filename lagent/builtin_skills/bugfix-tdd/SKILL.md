---
name: bugfix-tdd
description: "缺陷修复闭环（TDD）：先写能稳定复现问题的失败测试，再做最小根因修复，最后全量回归验证。"
description_en: "Test-driven bugfix workflow: reproduce with a failing test, apply minimal root-cause fix, verify full suite."
version: "1.0.0"
pack: dev
default_enabled: true
tags: ["bugfix", "tdd", "regression", "testing"]
when_to_use: "修复明确的 Bug、异常报错、逻辑回归或边界崩溃时优先使用。"
triggers:
  keywords: ["修复bug", "修bug", "复现问题", "失败测试", "测试驱动修复", "fix bug", "bugfix", "tdd fix", "reproduce failure"]
  globs: []
  mimes: []
requires:
  tools: ["read_file", "edit_file", "run_command"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 测试驱动缺陷修复（TDD）

1. **收集现场**：先阅读报错堆栈、相关源码与已有测试，定位可疑函数及调用方；切忌靠猜直接改代码。
2. **先写失败测试（Red）**：在对应测试文件中补充最小复现测试，运行它并亲眼确认按预期失败（说明测试真的覆盖到了缺陷）。
3. **最小根因修复（Green）**：用 `edit_file` 针对根本原因做最小、干净的修改，禁止用宽泛 `try/except` 吞掉异常敷衍了事。
4. **验证与回归**：
   - 先跑刚才的定向测试确认转绿；
   - 再跑相关模块与全量测试套件，确保无回归副作用；
   - 有 lint/typecheck 时一并跑过。
5. 汇报：说明根因、改动位置、新增的回归测试用例和最新实测输出。
