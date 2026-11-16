---
name: swarm-orchestrator
description: "编排多代理 DAG 集群（Swarm）：拆分并行/依赖节点、隔离只写工作树与独立 Verifier 把关，避免冲突编辑同一文件。"
description_en: "Orchestrate multi-agent DAG swarms with clear node ownership, isolated worktrees, and independent verification."
version: "1.0.0"
pack: meta
default_enabled: false
tags: ["swarm", "multi-agent", "dag", "parallel"]
when_to_use: "任务天然可拆为多个并行独立模块（如前后端同步调研/重构+独立审查）并需调用 `swarm_*` 工具时使用。"
triggers:
  keywords: ["多代理协同", "编排swarm", "并行集群", "dag任务", "swarm orchestrator", "multi-agent swarm", "parallel agents"]
  globs: []
  mimes: []
requires:
  tools: ["swarm_create", "swarm_run", "swarm_status"]
  capabilities: ["swarm"]
  binaries: []
  python: []
risk: write
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 多代理 DAG 集群编排

1. **评估是否真需要 Swarm**：只有存在 ≥ 2 个可独立推进的子模块、或需要“作者+独立审查者”交叉把关时才开 Swarm；简单两步任务直接自己串行做更快。
2. **清晰切分 DAG 拓扑（`swarm_create`）**：
   - 每个节点明确 **输入依赖（`depends_on`）**、**独占写入目录边界** 与 **完成标准**；
   - 严禁两个并行的写节点修改同一文件；并行为写任务配置独立分支或共享隔离目录；
   - 收尾加一个只读 `verifier` 节点核查前序产物并跑全量回归测试。
3. **监控与汇总**：用 `swarm_status` 跟踪节点进度；如有节点失败或越界，读取节点产物与日志定位后只重跑受影响分支。
4. 最终由主代理综合各节点实测产出向用户汇报。
