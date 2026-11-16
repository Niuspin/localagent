---
name: write-tests
description: "编写高质量单元测试与回归测试：覆盖正常路径、边界空值、错误分支与纯本地 Mock 隔离，拒绝空壳断言。"
description_en: "Author rigorous unit and integration tests covering happy paths, edge cases, error paths, and isolated mocks."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["testing", "pytest", "unit-test", "coverage"]
when_to_use: "为新功能补齐单元测试、提升覆盖率或为历史脆弱模块建立测试网时使用。"
triggers:
  keywords: ["写测试", "补充单元测试", "补全测试", "测试用例", "write tests", "unit tests", "test coverage"]
  globs: ["test_*.py", "*_test.py", "*.test.js", "*.spec.ts"]
  mimes: []
requires:
  tools: ["read_file", "write_file", "run_command"]
  capabilities: []
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 高质量测试编写规范

1. **摸清现有测试风格**：先查看 `tests/` 下的 fixture、tmp 目录约定和命名习惯，保持一致而不修改共享底座除非必需。
2. **设计测试矩阵**：
   - **主路径（Happy path）**：典型有效输入与预期返回值/状态变化；
   - **边界输入**：空串、空列表、极限数值、中英文/特殊字符、幂等重复调用；
   - **异常与拒绝路径**：非法参数、权限越界、依赖超时或故障降级。
3. **防脆弱原则**：
   - 不依赖外网与真实系统时间，使用 `tmp_path` 与可控本地 Stub/Fake；
   - 断言精确行为而非实现私有细节；严禁只断言 `res is not None` 或 `True`。
4. **实测闭环**：编写完立刻运行目标测试与全量套件，确认 0 跳过、0 放宽断言全通过。
