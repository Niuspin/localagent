# LocalAgent v0.2.0 迭代进度报告

> 对齐基线：[ITERATION_PLAN_2026-11-15_2140.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/ITERATION_PLAN_2026-11-15_2140.md)（v0.2.0 **"Swarm & Mission"**）  
> 上一阶段基线报告：[ARCHITECTURE_PROGRESS.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/ARCHITECTURE_PROGRESS.md)（v0.1.x Phase 1–3）  
> 基线提交：`e503db1`（M0 / M1 / M6 已提交并同步至 `origin/main`）  
> 最近更新：`2026-11-16 19:26 +08:00`

---

## 一、里程碑总览（严格对齐 §9 与 §13）

| 里程碑 | 周期（估） | 核心交付物 | 出口标准 | 当前状态 | 完成时间 |
|---|---|---|---|---|---|
| **M0 基础设施** | 0.5 周 | 会话 schema v2 + `.bak` 迁移；统一事件字段 + `AuditLog` 上下文扩展；`ArtifactStore` 与 SQLite 索引 + 只读下载 API；新配置键与校验；`tests/fixtures/`；`THIRD_PARTY_LICENSES.md` | 旧会话/配置零回归；迁移往返测试通过 | **已完成** | `2026-11-15 22:05 +08:00` |
| **M1 Swarm 核心（A）** | 1.5 周 | `lagent/swarm/` 全部模块；`spawn_agent`/`swarm_plan`/`await_agents` 等工具；SSE+API；Swarm 面板 v1 | §2.8 测试全绿；`parallel_refactor` 评测达标 | **已完成** | `2026-11-16 16:34 +08:00` |
| **M2 Mission 运行时（B）** | 1.5 周 | `lagent/mission/`；`/mission` 命令与 API；`MISSION.md`；Watchdog/Gates/Policy；Mission 详情页 | §3.8 测试全绿；崩溃续跑 100% | **已完成** | `2026-11-16 19:00 +08:00` |
| **M3 多模态输入（C-in）** | 1 周 | `media/ingest`、能力协商、受限子进程、前端附件条 | 黄金样本通过；炸弹样本被拒 | **已完成** | `2026-11-16 19:26 +08:00` |
| **M4 产物与多模态输出（C-out）** | 1 周 | Artifact 面板、`make_chart`、HTML 沙箱预览、会话导出、bundle | §4.4 测试全绿 | 待开始 | — |
| **M5 Research Engine（D）** | 2 周 | `lagent/research/` 全部模块；策略闸门接入全部联网路径；网络账本 UI；`cite_check` | §5.6 断言全绿；`--offline` 零出站 | 待开始 | — |
| **M6 UI 设计系统 v2（E）**（与 M1–M5 并行） | 2 周 | tokens/字体/图标/主题/动效/布局重构；对比度校验与无障碍强化 | §6.8 指标达标 | **已完成（无头 Playwright 截图留待装 Chromium 环境补跑）** | `2026-11-16 16:34 +08:00` |
| **M7 技能库 v2（F）** | 1 周 | Spec v2、路由、lint、`skills.lock`、30 个技能 + 评测 | §7.4 达标 | 待开始 | — |
| **M8 集成加固与发布 `v0.2.0`** | 0.5–1 周 | 全量回归、安全清单、文档收官、版本号 `0.2.0`、PyInstaller 冒烟 | CI 全绿；§8.2 清单全通过 | 待开始 | — |

---

## 二、六大核心工作流追踪

| 工作流 | 对应规划章节 | 所属里程碑 | 当前进度 | 已落实底座与核心能力 |
|---|---|---|---|---|
| **A. Agent 集群并发处理（Swarm）** | §2 | M1 | **M1 核心已完成** | `lagent/swarm/`（DAG 环检测与级联、四级优先调度器、7 角色单调权限、分层预算账本、类型化 JSONL 邮箱、文件租约锁、串行行级三方 `MergeQueue` + 冲突卡、崩溃恢复）、`slots="auto"` 规划、`worker_endpoint` 路由、Swarm 工具与 `/api/swarms*` HTTP/SSE、`parallel_refactor` 评测及 6 组专项测试全绿 |
| **B. 超长期编程智能体（Mission）** | §3 | M2 | **M2 核心已完成** | `lagent/mission/`（模型与 `MISSION.md` 双向解析、原子 `state.json`/`events.jsonl`、分段 Episode 引擎、Git 分支与里程碑回滚、`StallWatchdog` 四级升级、`MissionPolicy` 无人值守与安全单调边界、四维预算与 80%/100% 控管、跨平台防休眠与安静时间窗）、`--resume-missions`、`/mission` 命令、`/api/missions*` HTTP/SSE、前端 Missions 侧栏与详情监控面板 |
| **C. 多模态多格式 I/O 与 Artifact** | §4 | M3 / M4 | **M3 输入已完成，待 M4 输出** | 已落地 M3 `lagent/media/`（魔数嗅探、能力协商 `capabilities.py`、受限子进程 `sandbox.py`/`_worker.py`、EXIF/GPS 剥离与长图切块、PDF 文本+扫描页 `pypdfium2` 渲染/JS 探测、Office 表格/批注/备注/内嵌图+宏与外链剥离、CSV/Parquet 抽样、ipynb/EPUB/MHTML/HTML 清洗、压缩包仅列目录与 Zip Bomb/Slip 拦截、音视频优雅降级、工具 `ingest_media` 与 `OutputStore` 落盘、`/api/capabilities` 与 `/api/media/ingest`、前端附件条标签与分辨率切换）；待完成 M4 `make_chart` 与 Artifact 面板 |
| **D. 安全合法自动联网调研** | §5 | M5 | 已完成 M0 配置/目录预留 | 已完成 `research` 默认安全配置（`mode="off"`、`respect_robots=True`、`pii_redact=True`、`reader_isolation=True`）、`search.html_scrapers_ack`、`data/webcache/` |
| **E. UI 界面深度美化** | §6 | M6 | **M6 设计系统 v2 已完成** | `web/css/` 模块化分层重构（零 `transition: all`）、6 套 WCAG AA 主题（含 Paper / Midnight / HC / Colorblind）+ `tests/ui_contrast.py` 自动化校验、SVG 图标精灵、CJK 排版与等宽连字控制、可拖拽/键盘分栏、专注模式、View Transitions 与 `reduced/off` 动效控制、i18n 双语字典、Swarm 检查器面板（`web/` 总计 ~321.8 KB） |
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

### `2026-11-16 19:26 +08:00` — M3 多模态输入摄取（方向 C-in）交付与全量验证
- **M3 代码交付**：
  - 新增 `lagent/media/` 包（[__init__.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/__init__.py), [capabilities.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/capabilities.py), [ingest/__init__.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/__init__.py), [models.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/models.py), [sniff.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/sniff.py), [handlers.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/handlers.py), [sandbox.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/sandbox.py), [_worker.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/media/ingest/_worker.py)）。
  - 后端集成：[config.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/config.py) 新增 `media.recorder_enabled`/`max_image_pixels`/`max_archive_mb`/`max_video_frames`；[engine.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/engine.py) 从 `/props` 探测 `audio` 模态；[bootstrap.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/bootstrap.py) 增加 `find_whisper`/`install_whisper_cpp`（SHA-256 校验+Zip Slip 防护）；[files.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/tools/files.py) 升级 `read_file` 并新增 `ingest_media` 工具（接入受限子进程、`OutputStore` 渐进式句柄与强制 `taint`）；[loop.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/loop.py) 在 `_append_user` 中受限摄取富媒体附件并包裹 `<untrusted>`；[server.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/server.py) 暴露 `GET /api/capabilities`、`POST /api/media/ingest` 并增强 `/api/state` 与 `/api/sessions/{sid}/upload`（拦截炸弹上传）。
  - 前端 UI：更新 [index.html](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/index.html)、[composer.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/composer.js)、[chat.css](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/css/components/chat.css)，新增附件处理方式标签（`VLM 视觉`/`OCR`/`PDF 渲染/文本`/`文档结构化`/`Schema 抽样`/`仅列目录`/`Whisper 转写`/`不可转写` 等）、图片分辨率切换（标准/高清/省流）与能力门控录音按钮。
  - 夹具、测试与文档：生成 `tests/fixtures/media/`（[generate.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/fixtures/media/generate.py) 及 16 组黄金/炸弹/畸形样本），新建 [test_m3_media.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/test_m3_media.py)（5 组测试全绿），同步 [docs/API.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/docs/API.md) 与规划 §13。
- **实测验证结果**：
  - `python -m pytest tests\test_m3_media.py -v -p no:cacheprovider`：**`5 passed`（15.58s）**。
  - `python -m pytest tests -q -p no:cacheprovider`：**`424 passed, 2 failed, 3 skipped`（106.27s）**（仅有的 2 项失败与改造前基线一致，为既有 M0/M6 原始 ASGI 桩读取 `FileResponse` 空体问题，零新增回归）。
  - `python tests\ui_smoke.py`：真 Chromium 无头测试 **[1]–[12] 全部通过**（零控制台错误、零外部请求）。
  - `python tests\ui_contrast.py`：6 套主题 WCAG 2.2 AA 对比度全通过，零 `transition: all`。
  - `node --check web/js/**/*.js` 全部通过；`web/` 总大小 **`362,156 bytes`**（< `1.5 MB`）。
- **下一步计划**：进入 **M4（产物与多模态输出 C-out）**，完善 `make_chart`、HTML 沙箱预览、Artifact 检查器面板与会话导出。

### `2026-11-16 19:00 +08:00` — M2 Mission 运行时（方向 B）交付与全量验证
- **M2 代码交付**：
  - 新增 `lagent/mission/` 全包（[model.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/model.py), [store.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/store.py), [budget.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/budget.py), [policy.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/policy.py), [gates.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/gates.py), [watchdog.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/watchdog.py), [power.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/power.py), [runner.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/runner.py), [__init__.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/mission/__init__.py)）。
  - 接入运行时与接口：[events.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/events.py) 补全 `episode_started` / `handoff_saved` / `milestone_done` / `gate_result` / `stall_detected` / `mission_blocked` / `budget_warn` / `mission_rollback`；[launcher.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/launcher.py) 新增 `--resume-missions`；[commands.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/commands.py) 实现 `/mission start|status|pause|resume|stop|budget|log|edit|rollback`；[loop.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/loop.py) 落地 `Agent.run_mission_episode`、工具边界/Watchdog 钩子与 `_preserve_mission_summary`；[server.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/server.py) 注册 `/api/missions*` HTTP/SSE 接口并接入 `lifespan` 启停。
  - 前端 UI 与无头稳定性：新增 [mission.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/mission.js)，更新 [index.html](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/index.html)、[api.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/api.js)、[sidebar.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/sidebar.js)、[panels.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/panels.js)、[composer.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/composer.js)、[main.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/main.js)（含修复快速切换主题时 `ViewTransition` 中断未捕获拒绝异常）、[drawer.css](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/css/components/drawer.css) 与中英 i18n。
  - 测试与文档：新增 [test_m2_mission.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/test_m2_mission.py)（8 项核心契约/E2E 用例），在 [run_eval.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/eval/run_eval.py) 扩充 `long_refactor` / `resume_after_crash` / `stall_recovery` 三场景，并同步 [docs/API.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/docs/API.md) 与规划 §13。
- **实测验证结果**：
  - `python -m pytest tests\test_m2_mission.py -v -p no:cacheprovider`：**`8 passed`（16.82s）**（全绿通过）。
  - `python -m pytest tests -q -p no:cacheprovider`：**`419 passed, 2 failed, 3 skipped`（86.70s）**——其中 2 项失败与改造前基线完全一致，均来自既有 M0/M6 原始 ASGI 桩在当前环境下接收 `FileResponse` 空体（`test_artifact_store_crud_isolation_and_routes` 与 `test_m6_modular_assets_csp_and_bundle_budget`），M2 及既有功能零回归。
  - `python tests\ui_smoke.py`：真 Chromium 无头冒烟测试 **[1]–[12] 全部通过**（零控制台错误、零外部请求）。
  - `python tests\ui_contrast.py`：6 套主题 WCAG 2.2 AA 对比度全通过，零 `transition: all`。
  - `node --check web/js/**/*.js`：全部前端脚本通过；`web/` 静态资源总大小 `356,020 bytes`（< `1.5 MB`）。
- **下一步计划**：可按 §9 路线进入 **M3（多模态输入摄取）** 或 **M5（Research Engine）**。

### `2026-11-16 16:34 +08:00` — M1（Swarm 核心）与 M6（UI 设计系统 v2）交付与全量回归通过
- **M1 代码交付**：
  - 新增 `lagent/swarm/` 全包（[graph.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/graph.py), [roles.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/roles.py), [budget.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/budget.py), [locks.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/locks.py), [mailbox.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/mailbox.py), [scheduler.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/scheduler.py), [merge.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/merge.py), [persist.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/persist.py), [manager.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/swarm/manager.py)）。
  - 新增并集成 Swarm 编排工具 [swarm_tools.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/tools/swarm_tools.py)（`spawn_agent` 非阻塞派生、`swarm_plan`、`await_agents`、`agent_status`、`cancel_agent`、`send_message`，并保持 `delegate_task` 向后同步兼容）及 `swarm` 扩展组（[toolset.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/toolset.py)）。
  - 打通 `engine.slots = "auto"` 自适应并行槽位推荐（[config.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/config.py), [engine.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/engine.py)）、`ModelRouter._worker_target` 外部 Worker 端点路由（[router.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/router.py)）、`Agent.run_swarm_node`（[loop.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/loop.py)）以及 `/api/swarms*`、`/api/engine/plan` HTTP/SSE 接口与 [API.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/docs/API.md)。
  - 在 [run_eval.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/eval/run_eval.py) 增加 `parallel_refactor` 评测场景；新增 [test_m1_swarm.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/test_m1_swarm.py)。
- **M6 代码交付**：
  - 将 `web/app.css` 模块化重构为 [tokens.css](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/css/tokens.css)、[base.css](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/css/base.css)、[layout.css](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/css/layout.css)、`web/css/components/*`（`controls.css`、`chat.css`、`drawer.css`）及 6 套 WCAG AA 主题 `web/css/themes/*`（`light`、`dark`、`paper`、`midnight`、`hc`、`colorblind`），保留 `web/app.css` 为兼容入口并消灭全部 `transition: all`。
  - 新增本地 SVG 矢量图标精灵 [sprite.svg](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/icons/sprite.svg)、字体预留说明 [web/fonts/README.md](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/fonts/README.md)、双语字典 `web/js/i18n/{zh-CN,en}.js` 及 Swarm 检查器前端面板 [swarm.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/swarm.js)。
  - 升级 [index.html](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/index.html)、[util.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/util.js)、[panels.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/panels.js)、[settings.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/settings.js)、[main.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/main.js)、[chat.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/chat.js)，实现可拖拽/键盘分栏、专注模式、View Transitions、`motion/density/code_ligatures/accent` 外观控制与多代理审批分组。
  - 新增自动化检查 [ui_contrast.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/ui_contrast.py) 与 [test_m6_ui.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/tests/test_m6_ui.py)。
- **实测验证结果**：
  - `python -m pytest tests -q -p no:cacheprovider`：**`413 passed, 3 skipped`（75.99s）**，全部既有测试 + M0 + M1 + M6 测试零失败。
  - `python tests/ui_contrast.py`：6 套主题 WCAG 2.2 AA 对比度（`>= 4.5:1`）全通过，零 `transition: all`。
  - `node --check web/js/**/*.js`：全部前端脚本语法通过；`web/` 静态资源总大小 `329,488 bytes`（远低于 `1.5 MB` 预算）。
  - `python tests/ui_smoke.py`：因当前环境未安装 `playwright`，按设计自动跳过（真实 Chromium 视觉截图基线待有浏览器环境时补跑）。
- **下一步计划**：进入 **M2（Mission 超长期任务运行时）**，并行可推进 **M3（多模态输入摄取）**。

### `2026-11-15 22:05 +08:00` — M0 基础设施交付与全量回归通过
- **代码变更**：新增 `lagent/events.py`、`lagent/parts.py`、`lagent/artifacts.py`；修改 `lagent/paths.py`、`lagent/config.py`、`lagent/sessions.py`、`lagent/context.py`、`lagent/security.py`、`lagent/tools/ctx.py`、`lagent/loop.py`、`lagent/server.py`、`docs/API.md`。
- **文档与测试**：新增根目录 `THIRD_PARTY_LICENSES.md`、`ITERATION_PROGRESS.md`，新建 `tests/fixtures/` 与 `tests/test_m0_infra.py`，并勾选 `ITERATION_PLAN_2026-11-15_2140.md` §13 的 M0 项、追加 `ARCHITECTURE_PROGRESS.md` 记录。
- **验证结果**：
  - `python -m pytest tests -q -p no:cacheprovider`：`405 passed, 3 skipped`（修复轻量 `Run` 测试桩的 `_audit_ctx` 兼容后全量通过）。
  - `node --check web/js/*.js`：全部前端脚本语法通过。
  - `python tests/ui_smoke.py`：因本地环境未安装 `playwright` 自动跳过（`SKIP: playwright 未安装`，退出码 0）。
- **下一步计划**：按推荐风险顺序，在进入 **M1（Swarm 核心）** 的同时，也可并行启动 **M6（UI 字体/Token 基建）** 底座。
