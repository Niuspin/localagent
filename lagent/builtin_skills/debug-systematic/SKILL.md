---
name: debug-systematic
description: "系统化根因排错：建立可证伪假设、二分缩小范围、采集实证状态并区分根因与表象。"
description_en: "Systematic root-cause debugging via falsifiable hypotheses, binary narrowing, and evidence verification."
version: "1.0.0"
pack: dev
default_enabled: false
tags: ["debugging", "root-cause", "bisect", "diagnostics"]
when_to_use: "遇到偶现 Bug、跨模块级联报错、环境诡异差异或反复修复无效时使用。"
triggers:
  keywords: ["系统排查", "定位根因", "根因分析", "偶现bug", "debug systematically", "root cause", "troubleshoot crash"]
  globs: []
  mimes: []
requires:
  tools: ["read_file", "grep", "run_command"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 系统化根因排错流程

1. **冻结事实**：精确记录失败命令、输入参数、错误原文、最近改动（`git_log`）及最近一次正常版本。
2. **列出 Top-3 可证伪假设**：按可能性排序，为每个假设设计“什么实验结果能直接推翻它”。
3. **二分缩小排查面**：
   - 区分配置/环境问题 vs 代码逻辑问题 vs 数据输入问题；
   - 通过最小化复现脚本或临时断言（排错后清理）把问题锁定到单函数/单条件。
4. **避免盲改循环**：如果连续两次尝试未验证假设就失败，立即停下重读相关源码和日志，不要随机碰运气修改。
5. **形成根因证明链**：汇报时明确说明“触发条件 → 中间传播状态 → 直接失效点 → 验证证据”。
