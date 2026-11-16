---
name: log-analysis
description: "日志聚类与故障时间线分析：统计错误分布、还原首发异常时刻（First Error）、过滤级联噪音并定位根因模块。"
description_en: "Analyze server/build logs by clustering errors, identifying the first triggering failure, and reconstructing timelines."
version: "1.0.0"
pack: ops
default_enabled: false
tags: ["logs", "errors", "timeline", "diagnostics"]
when_to_use: "面对数百上千行崩溃日志、CI 构建失败输出、访问日志或 JSONL 审计日志需要排查时使用。"
triggers:
  keywords: ["分析日志", "日志排查", "报错日志", "错误聚类", "崩溃日志", "log analysis", "analyze logs", "parse stacktrace"]
  globs: ["*.log", "*.jsonl"]
  mimes: []
requires:
  tools: ["grep", "read_file", "run_command"]
  capabilities: []
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 大日志聚类与时间线分析

1. **避免通读刷屏**：大日志文件绝不整篇 `read_file` 塞进上下文；先用 `grep` 搜 `ERROR`/`WARN`/`Exception`/`Traceback` 或用短 Python 单行统计命令做频次聚合。
2. **寻找“第一块倒下的多米诺骨牌”**：90% 的后续连接超时报错只是表象；务必按时间戳往前追，找出紧接在健康状态结束后的**首发异常（First Failure）**及其请求 ID/上下文参数。
3. **聚合统计维度**：按异常类型 + 模块 + HTTP 状态码统计频次、首次出现时间与最后出现时间。
4. **交付诊断卡**：
   - **故障窗口与影响面**（起止时间、报错总数占比）；
   - **首发异常与因果链路**（附原文关键 10~20 行片段与行号）；
   - **关联级联报错归类**与建议排查方向。
