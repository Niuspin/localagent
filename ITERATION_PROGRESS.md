# LocalAgent v0.2.0 迭代进度报告

> 对齐基线：[ITERATION_PLAN_2026-11-15_2140.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/ITERATION_PLAN_2026-11-15_2140.md)（v0.2.0 **"Swarm & Mission"**）  
> 上一阶段基线报告：[ARCHITECTURE_PROGRESS.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/ARCHITECTURE_PROGRESS.md)（v0.1.x Phase 1–3）  
> 基线提交：`09faa74`（`ITERATION_PLAN_2026-11-15_2140.md` 已提交并同步至 `origin/main`）  
> 最近更新：`2026-11-15 22:05 +08:00`

---

## 一、里程碑总览（严格对齐 §9 与 §13）

| 里程碑 | 周期（估） | 核心交付物 | 出口标准 | 当前状态 | 完成时间 |
|---|---|---|---|---|---|
| **M0 基础设施** | 0.5 周 | 会话 schema v2 + `.bak` 迁移；统一事件字段 + `AuditLog` 上下文扩展；`ArtifactStore` 与 SQLite 索引 + 只读下载 API；新配置键与校验；`tests/fixtures/`；`THIRD_PARTY_LICENSES.md` | 旧会话/配置零回归；迁移往返测试通过 | **已完成** | `2026-11-15 22:05 +08:00` |
| **M1 Swarm 核心（A）** | 1.5 周 | `lagent/swarm/` 全部模块；`spawn_agent`/`await_agents` 等工具；SSE+API；Swarm 面板 v1 | §2.8 测试全绿；`parallel_refactor` 评测达标 | 待开始 | — |
| **M2 Mission 运行时（B）** | 1.5 周 | `lagent/mission/`；`/mission` 命令与 API；`MISSION.md`；Watchdog/Gates/Policy；Mission 详情页 | §3.8 测试全绿；崩溃续跑 100% | 待开始 | — |
| **M3 多模态输入（C-in）** | 1 周 | `media/ingest`、能力协商、受限子进程、前端附件条 | 黄金样本通过；炸弹样本被拒 | 待开始 | — |
| **M4 产物与多模态输出（C-out）** | 1 周 | Artifact 面板、`make_chart`、HTML 沙箱预览、会话导出、bundle | §4.4 测试全绿 | 待开始 | — |
| **M5 Research Engine（D）** | 2 周 | `lagent/research/` 全部模块；策略闸门接入全部联网路径；网络账本 UI；`cite_check` | §5.6 断言全绿；`--offline` 零出站 | 待开始 | — |
| **M6 UI 设计系统 v2（E）**（与 M1–M5 并行） | 2 周 | tokens/字体/图标/主题/动效/布局重构；虚拟化；对比度与视觉回归 | §6.8 指标达标 | 待开始 | — |
| **M7 技能库 v2（F）** | 1 周 | Spec v2、路由、lint、`skills.lock`、30 个技能 + 评测 | §7.4 达标 | 待开始 | — |
| **M8 集成加固与发布 `v0.2.0`** | 0.5–1 周 | 全量回归、安全清单、文档收官、版本号 `0.2.0`、PyInstaller 冒烟 | CI 全绿；§8.2 清单全通过 | 待开始 | — |

---

## 二、六大核心工作流追踪

| 工作流 | 对应规划章节 | 所属里程碑 | 当前进度 | M0 已落实底座与约束 |
|---|---|---|---|---|
| **A. Agent 集群并发处理（Swarm）** | §2 | M1 | 已完成 M0 前置契约 | 已完成 `swarm` 配置校验、`worker_api_key` 密钥隔离、`data/swarms/` 目录规范、统一事件与审计字段 `{run_id, agent_id, swarm_id, parent_id, mission_id}` |
| **B. 超长期编程智能体（Mission）** | §3 | M2 | 已完成 M0 前置契约 | 已完成 `mission` 配置与预算限额校验、`data/missions/` 目录规范、Session v2 `mission_id` 关联与审计上下文 |
| **C. 多模态多格式 I/O 与 Artifact** | §4 | M3 / M4 | 已完成 M0 存储与 Schema v2 | 已完成 `lagent/parts.py`（7 类 part 与降级）、Session v2 迁移与 `.bak` 保护、`ArtifactStore` + SQLite WAL 索引 + `/api/artifacts` 安全下载 |
| **D. 安全合法自动联网调研** | §5 | M5 | 已完成 M0 配置/目录预留 | 已完成 `research` 默认安全配置（`mode="off"`、`respect_robots=True`、`pii_redact=True`、`reader_isolation=True`）、`search.html_scrapers_ack`、`data/webcache/` |
| **E. UI 界面深度美化** | §6 | M6 | 已完成 M0 配置与合规登记 | 已完成 `ui.motion` / `ui.density` / `ui.code_ligatures` 校验，以及 `THIRD_PARTY_LICENSES.md` 中 M2/M6 字体与图标离线资源清单元 |
| **F. 系统默认技能库（Skills v2）** | §7 | M7 | 已完成 M0 预算配置 | 已完成 `skills.default_enabled_limit=14`（1..50）与 `skills.auto_route=True` 配置模型与旧配置自动补全 |

---

## 三、M0 里程碑详细验收（已完成）

| 编号 | 子任务 | 实际改动/新增文件 | 状态 | 验收结果 |
|---|---|---|---|---|
| **M0.1** | 运行数据目录与路径规范 | [paths.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/paths.py) | **已完成** | 新增 `DATA_SUBDIRS`（含 `sessions`, `outputs`, `swarms`, `missions`, `artifacts`, `webcache`）与 `data_dir()`、`ensure_dirs()` 自动建目录 |
| **M0.2** | v0.2.0 全量配置 Schema 与校验 | [config.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/config.py) | **已完成** | 补全 `swarm`、`mission`、`media`、`research`、`skills`、`ui` 扩展字段及 `search.html_scrapers_ack`；新增 `_role_limits` 与全量 `SPECIAL`/`RANGES` 校验；`swarm.worker_api_key` 隔离至 `secrets.json` |
| **M0.3** | Session Schema v1 → v2 迁移与 Part 降级 | [parts.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/parts.py), [sessions.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/sessions.py), [context.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/context.py) | **已完成** | `Session.schema_version=2`、7 类 Part 规范化与无损/降级投影、v1 首次写回前自动备份 `.json.bak`（二次写回不覆盖）、提供 `migrate_v1_to_v2` / `downgrade_v2_to_v1` |
| **M0.4** | 统一事件流与审计上下文字段 | [events.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/events.py), [loop.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/loop.py), [server.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/server.py), [security.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/security.py) | **已完成** | `Run.emit` 与 `State.broadcast` 统一注入 `{ts, kind, run_id, agent_id, swarm_id, mission_id}`（保持 `Last-Event-ID` 序号不变）；`AuditLog.append` 与 `Agent._audit_ctx` 写入完整上下文且兼容历史审计条目与 `verify()` |
| **M0.5** | `ArtifactStore` 与 SQLite 索引 | [artifacts.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/artifacts.py), [ctx.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/tools/ctx.py), [server.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/server.py), [API.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/docs/API.md) | **已完成** | `data/artifacts/index.db`（WAL）+ 会话分目录安全读写、路径穿越校验、配额最旧淘汰、会话删除级联清理、新增 `GET /api/artifacts` / `/meta` / `{aid}`（含 `Content-Disposition: attachment` 与 `X-Content-Type-Options: nosniff`） |
| **M0.6** | 合规声明与测试 Fixtures | [THIRD_PARTY_LICENSES.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/THIRD_PARTY_LICENSES.md), `tests/fixtures/`, [test_m0_infra.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/test_m0_infra.py) | **已完成** | 登记既有 Python 依赖许可证与 M2/M6 预留离线字体/图标；新增 `tests/fixtures/sessions/v1_session.json` 等夹具与 6 组 M0 核心测试 |
| **M0.7** | 全量回归验证与文档同步 | — | **已完成** | `python -m pytest tests -q -p no:cacheprovider` -> `405 passed, 3 skipped`（含全部既有 399 项测试与 6 项新增 M0 测试）；`web/js/*.js` `node --check` 全部通过；同步勾选规划清单并更新双报告 |

---

## 四、变更与验证日志

### `2026-11-15 22:05 +08:00` — M0 基础设施交付与全量回归通过
- **代码变更**：新增 `lagent/events.py`、`lagent/parts.py`、`lagent/artifacts.py`；修改 `lagent/paths.py`、`lagent/config.py`、`lagent/sessions.py`、`lagent/context.py`、`lagent/security.py`、`lagent/tools/ctx.py`、`lagent/loop.py`、`lagent/server.py`、`docs/API.md`。
- **文档与测试**：新增根目录 `THIRD_PARTY_LICENSES.md`、`ITERATION_PROGRESS.md`，新建 `tests/fixtures/` 与 `tests/test_m0_infra.py`，并勾选 `ITERATION_PLAN_2026-11-15_2140.md` §13 的 M0 项、追加 `ARCHITECTURE_PROGRESS.md` 记录。
- **验证结果**：
  - `python -m pytest tests -q -p no:cacheprovider`：`405 passed, 3 skipped`（修复轻量 `Run` 测试桩的 `_audit_ctx` 兼容后全量通过）。
  - `node --check web/js/*.js`：全部前端脚本语法通过。
  - `python tests/ui_smoke.py`：因本地环境未安装 `playwright` 自动跳过（`SKIP: playwright 未安装`，退出码 0）。
- **下一步计划**：按推荐风险顺序，在进入 **M1（Swarm 核心）** 的同时，也可并行启动 **M6（UI 字体/Token 基建）** 底座。
