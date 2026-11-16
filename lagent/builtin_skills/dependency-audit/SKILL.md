---
name: dependency-audit
description: "依赖清单与供应链风险审计：梳理依赖树、版本锁文件、冗余/废弃包、许可证与升级兼容性。"
description_en: "Audit dependency manifests and lockfiles for unused packages, version conflicts, licenses, and upgrade risks."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["dependencies", "supply-chain", "packages", "audit"]
when_to_use: "审查 `requirements*.txt` / `pyproject.toml` / `package.json`、准备升级依赖或引入新三方库前使用。"
triggers:
  keywords: ["依赖审计", "依赖冲突", "升级依赖", "许可证检查", "dependency audit", "package lock", "check dependencies"]
  globs: ["requirements*.txt", "pyproject.toml", "package.json", "package-lock.json", "poetry.lock"]
  mimes: []
requires:
  tools: ["read_file", "grep"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 依赖与供应链审计

1. **全面清点清单**：读取 `requirements*.txt`、`pyproject.toml`、`package.json` 及 lock 文件，对照代码实际 `import`/`require` 使用情况。
2. **四项核查维度**：
   - **未锁定/宽泛版本**：标出无版本上限的直接依赖；
   - **未使用或重复造轮子**：标出已被标准库或现有库覆盖的依赖；
   - **兼容性与破坏性升级**：评估跨主版本差异与 Python/Node 版本约束；
   - **许可与二进制体积**：检查闭源/传染性许可证及桌面打包体积影响。
3. **联网核验（如用户允许联网）**：对高风险包核查官方最新安全公告，标明核查日期与来源。
4. **输出改造建议表**：按“立即移除 / 锁定版本 / 谨慎升级 / 保持现状”给出可操作方案。
