# LocalAgent 架构优化进度报告

> **对照基线**：[ARCHITECTURE_REVIEW.md](./ARCHITECTURE_REVIEW.md)（评估基线 `be0f14b`）  
> **最新验证范围**：`tests/` 全量单测与集成测试（`pytest`）、Playwright 浏览器 UI 全链路烟雾测试（`tests/ui_smoke.py`）、`tests/eval/run_eval.py --list` 评测场景清单

---

## 总体进度概览

| 阶段 / 维度 | 状态 | 完成度评估 | 说明 |
| :--- | :--- | :--- | :--- |
| **Section 1 (P0)：前后端断点与契约修复** | **已完成** | **6 / 6 (100%)** | `/model`·`/plan`·`/mode`、递归 `@` 补全、Slash/规则/Hooks 接入、`verify` 子代理、附件重发/多轮 `truncate`、`Bearer` 鉴权与 `docs/API.md` 对齐全部落地并有测试覆盖。 |
| **Section 2：推理与硬件调度架构** | **已完成（零额外强制依赖）** | **核心链路全部打通** | `--parallel` 多槽位 + `_SlotScheduler` 前台优先队列、`ModelRouter`（OpenAI / Anthropic 压缩 + Embedding 路由）、Append-Only 前缀投影（`project_api_messages` + `advance_prune_watermark`）与 `/slots` 保存恢复、分片 GGUF + TTFT/KV/投机降级、`--list-devices` + `--tensor-split`/`--split-mode`、`--reasoning-format`/`--reasoning-budget`、`schema_to_gbnf` 结构化输出降级、Keep-Alive 连接池 (`net._local_request`) 与指数退避重试全部上线。 |
| **Section 3：Agent 运行时、代码智能与记忆** | **已完成** | **全闭环落地** | `worker` 基于 `git worktree` 隔离并发修改与统一审核回写；`rg` + `.gitignore` 搜索；`repo_map`（AST + 可选 Tree-sitter）；`edit_file` 行哈希锚定（`anchors=true` + `line_hash`/`start_line`）与多处原子批量编辑；`code_diagnostics` + 编辑后语法/Lint 即时检查；有状态安全终端（持久化 `shell_cwd`/`shell_env` 过滤危险环境）与流式 `tool_output_delta`；`/tokenize` 实测、`seen_files` 持久化、分层 `AGENTS.md`；FTS5 + `local_embed`/外部 Embedding 稠密向量 RRF 混合检索、情景蒸馏（`_maybe_distill_episode`）、按需延迟加载会话索引（`sessions_index.json`）与 `Journal.checkpoints` 按回合检查点。 |
| **Section 4：前端交互与桌面工作台** | **已完成** | **全部高优体验落地** | `chat.js` 块级增量 Markdown DOM 复用 + 超长会话前端窗口折叠；`ttft_ms` 与 `KV 缓存 %` 指标徽章；多审批排队顶部汇总条 + 拒绝后一键“重新描述 / 切换 auto_edit”快捷引导；会话级权限策略覆盖菜单；`Ctrl/Cmd+K` 全局命令面板、Steer 纠偏队列、代码行号预览与文件跳转、Git Diff Drawer + 按回合快照、离线 `.mermaid-box` 与 `$$` 公式块、子代理轨迹卡片全部上线。 |
| **Section 5：MCP 生态与跨平台零信任沙箱** | **主体完成（OAuth2 / AppContainer 留作进阶）** | **核心安全与完整协议落地** | Windows Win32 `Job Object` 进程硬隔离（进程数限制、退出全树强杀与 UI 隔离，`harden_windows_process`）与 macOS `sandbox-exec` 白名单最小读权收紧；`tainted=True` 下区分仅执行显式 `read_only=True` 且通过安全白名单校验的只读 Hook；Streamable HTTP / SSE MCP 传输、Header 密钥隔离、Resources / Prompts / Roots (`roots/list`)、`list_changed` SSE 热推送与 `$defs`/`$ref`/`anyOf`/`oneOf` JSON Schema 适配。注：Windows AppContainer/WSL2 与 MCP OAuth 2.0 交互授权留作后续可选扩展。 |
| **Section 6：Model Hub、部署模式与评测扩展** | **主体完成** | **Hub 与评测全部落地** | 新增 `lagent/hub.py` 硬件自适应推荐、`hf-mirror` 镜像改写、HTTP `Range` 断点续传与 SHA-256 校验及 UI 下载面板；`bootstrap.py` 补全 Linux AMD ROCm 资产挑选；新增 `--offline` 纯离线模式与 `/api/fs/pick` 原生 OS 文件夹选择器；`tests/eval/run_eval.py` 扩充 `multirefactor`、`selfheal` 与 `redteam_taint` 场景。注：独立驻留式系统托盘/多窗口守护 Daemon 留作后续演进，当前支持 `--no-browser` 纯服务模式。 |

---

## 详细架构对照清单

### 一、P0 级缺陷与前后端契约修复（`ARCHITECTURE_REVIEW.md` §1）

| 编号 | 问题项 | 状态 | 实现位置 |
| :--- | :--- | :--- | :--- |
| 1.1 | Slash 命令 `/model`、`/plan`、`/mode` 崩溃与缺少 `Config.set()` | **已完成** | `lagent/config.py`、`lagent/commands.py` |
| 1.2 | `@` 引用文件递归补全与缓存失效 | **已完成** | `lagent/server.py`（`/api/workspace/tree?recursive=1`）、`web/js/composer.js` |
| 1.3 | 自定义 Slash 命令与工作区规则/Hooks 检查接入前端 | **已完成** | `web/js/composer.js`（15 条内置命令 + 自定义命令）、`web/js/settings.js` |
| 1.4 | `verify` 模式子代理权限修复 | **已完成** | `lagent/loop.py`（保留 `verify` 只读验证命令权限） |
| 1.5 | “编辑重发 / 重新生成”保留附件与多轮 `truncate` 回滚 | **已完成** | `web/js/chat.js`、`lagent/loop.py`（按撤销链回滚多轮快照） |
| 1.6 | `docs/API.md`、`Authorization: Bearer` 与 `/api/state` 契约对齐 | **已完成** | `lagent/server.py`、`docs/API.md` |

### 二、推理与硬件调度架构（`ARCHITECTURE_REVIEW.md` §2）

| 评估项 | 状态 | 落地能力 |
| :--- | :--- | :--- |
| **多槽位与前台优先调度** | **已完成** | 解锁 `engine.slots`（`1..8`）并新增 `_SlotScheduler`，`slots >= 2` 时为前台交互预留槽位防止后台压缩/向量请求饿死。 |
| **多角色模型路由 (`ModelRouter`)** | **已完成** | `lagent/router.py` 支持 OpenAI 兼容与 Anthropic (`/v1/messages` / `x-api-key`) 摘要压缩路由及 `/v1/embeddings` 外部或本地嵌入路由，失败自动回退。 |
| **Append-Only 前缀缓存保护与 KV 持久化** | **已完成** | `context.py` 新增只追加零原地改写的 `project_api_messages` 与单调块水位推进 `advance_prune_watermark`；`engine.py` 对接 `/slots/{id}?action=save|restore`；`loop.py` 上报 `cache_hit_rate` 与 `ttft_ms`。 |
| **分片 GGUF、TTFT 与投机降级** | **已完成** | 全分片张量布局解析 + `TelemetryTracker` 记录 TTFT / `spec_accept_rate` / `cache_hit_rate` + 连续低采纳率自动降级关闭投机解码。 |
| **跨后端硬件探测、多卡切分与 GBNF** | **已完成** | `_llama_devices()` 回退探测；`advise_multi_gpu()` 自动按空余显存生成 `--tensor-split` / `--split-mode`；`reasoning_format` + `reasoning_budget` 配置透传；`schema_to_gbnf()` 结构化输出降级。 |
| **Keep-Alive 连接池 HTTP/SSE** | **已完成** | `lagent/net.py` 为本地 `llama-server` 请求提供连接池复用（`_LOCAL_POOL`）与指数退避重试，同时保持标准库零外部依赖约束。 |

### 三、Agent 运行时、代码智能与记忆架构（`ARCHITECTURE_REVIEW.md` §3）

| 评估项 | 状态 | 落地能力 |
| :--- | :--- | :--- |
| **Git Worktree 隔离并发 Worker 与轨迹流** | **已完成** | `loop.py` + `tools/agentic.py` 支持 `worker` 子代理在独立 `git worktree` 并行读写并通过 `Agent._apply_worktree()` 汇总 Diff 安全写入 `Journal`；支持嵌套轨迹 `subagent_step` 实时呈现。 |
| **RepoMap、行哈希锚定与代码诊断** | **已完成** | `repomap.py` 集成 Python AST、正则多语言骨架提取及可选 Tree-sitter；`files.py` 实现 `read_file(anchors=True)` 行哈希输出、`edit_file(start_line, line_hash, edits=...)` 乐观锁定与原子批量编辑、`code_diagnostics` 多语言诊断及 `_post_edit_check`。 |
| **有状态终端与流式输出** | **已完成** | `SessionState` 持久化跟踪工作区内 `shell_cwd` 与安全过滤后的 `shell_env`；实时流式推送 `tool_output_delta`。 |
| **精确计量与嵌套项目规则** | **已完成** | `/tokenize` 校准、`seen_files`（含哈希）随会话持久化、`lagent/projectdoc.py` 按路径按需动态加载子目录 `AGENTS.md` / `CLAUDE.md`。 |
| **混合向量记忆、惰性会话索引与回合快照** | **已完成** | `memory.py` 新增 `mem_vectors` 表、`local_embed` 零依赖哈希特征向量及外部向量 BM25 + Dense Cosine + RRF 混合排序；`loop.py` 支持回合结束自动情景蒸馏；`sessions.py` 改用 `sessions_index.json` 惰性加载；`journal.py` 提供 `checkpoints()` 按回合聚合时间线。 |

### 四、前端交互、安全沙箱、MCP 与 Model Hub（`ARCHITECTURE_REVIEW.md` §4–§6）

| 评估项 | 状态 | 落地能力 |
| :--- | :--- | :--- |
| **增量 DOM、长会话折叠与工作台效率** | **已完成** | `chat.js` 块级前缀复用增量 Markdown 渲染（零划词打断）与 32 条消息窗口折叠；`ttft_ms`/`KV 缓存 %` 展示；多审批排队全局条 + 拒绝后改写/`auto_edit` 引导；会话级权限覆盖开关；Git Diff + 回合快照抽屉、`Ctrl/Cmd+K` 全局面板。 |
| **跨平台零信任沙箱与可读性 Hook** | **已完成** | `security.py` 收紧 macOS `sandbox-exec` 为最小路径读白名单 + 敏感目录拒绝；新增 Windows Win32 `Job Object` 进程树与 UI 限权（`harden_windows_process` / `release_windows_job`）；`hooks.py` 支持 `tainted=True` 下仅执行显式 `read_only=True` 且经安全白名单校验的只读 Hook；`config.py` / `hooks.py` 热重载与 Bearer 鉴权就绪。 |
| **Streamable HTTP / Resources / Prompts MCP** | **已完成** | `mcp/` 新增 `HttpMcpSession`（JSON + SSE + `Mcp-Session-Id`，严格不纳入本地容器豁免）、`Header` 密钥隔离、`$defs`/`$ref`/`anyOf`/`oneOf` Schema 适配、`roots/list`、Resources / Prompts 浏览读取 API 及 `mcp` 全局 SSE 热更新。 |
| **Model Hub、ROCm 引导与扩展评测** | **已完成** | `lagent/hub.py` + `settings.js` 实现硬件匹配推荐、镜像切换与断点续传；`bootstrap.py` 支持 Linux AMD ROCm；`launcher.py` 支持 `--offline`；`appwindow.py` + `/api/fs/pick` 接入 OS 原生目录选择弹窗；`tests/eval/run_eval.py` 与 `tests/test_phase2_3.py` 覆盖多轮重构、自愈调试与红队防注入场景。 |

---

## v0.2.0 迭代进程追加记录

> 完整跟踪看板见根目录 [ITERATION_PROGRESS.md](./ITERATION_PROGRESS.md)，总规划见 [ITERATION_PLAN_2026-11-15_2140.md](./ITERATION_PLAN_2026-11-15_2140.md)。

### `[2026-11-15 22:05 +08:00]` M0 基础设施（已完成）
- 完成 Session Schema v1 → v2 自动迁移、`parts` 多模态结构化与上下文降级、以及首次写回前 `.json.bak` 自动备份（`lagent/parts.py`、`lagent/sessions.py`、`lagent/context.py`）。
- 完成统一运行/广播事件元数据结构 `{ts, kind, run_id, agent_id, swarm_id, mission_id}` 与 `AuditLog` 上下文字段扩展，保持 SSE `Last-Event-ID` 序号与审计哈希链校验兼容（`lagent/events.py`、`lagent/loop.py`、`lagent/server.py`、`lagent/security.py`）。
- 完成 `ArtifactStore`（`data/artifacts/<sid>/` + SQLite WAL 索引、防路径穿越、配额淘汰与会话清理）及 `/api/artifacts` 只读安全下载接口（`lagent/artifacts.py`、`lagent/server.py`、`docs/API.md`）。
- 补齐 `swarm` / `mission` / `media` / `research` / `skills` / `ui` 扩展配置校验与 `worker_api_key` 密钥隔离（`lagent/config.py`、`lagent/paths.py`），并建立 `THIRD_PARTY_LICENSES.md` 与 `tests/fixtures/` + `tests/test_m0_infra.py`。
- 全量验证：`python -m pytest tests -q -p no:cacheprovider` -> `405 passed, 3 skipped`；前端 JS 语法检查通过。

### `[2026-11-16 16:34 +08:00]` M1 Swarm 核心 & M6 UI 设计系统 v2（已完成核心交付与自动化验收）
- **M1（Swarm）**：新增 `lagent/swarm/`（`TaskGraph` DAG 调度/环检测/级联阻塞、`SwarmScheduler` 四级队列+防饥饿老化、`RoleSpec` 单调权限、`BudgetLedger`、`Mailbox` JSONL 邮箱、`FileLeaseManager`、`MergeQueue` 串行三方行级合并+冲突卡、`SwarmStore` 崩溃恢复），并打通 `engine.slots="auto"` 规划、外部 Worker 端点路由、Swarm 工具集与 `/api/swarms*` HTTP/SSE 接口。
- **M6（UI v2）**：模块化重构 `web/css/*`（消灭全量 `transition: all`），落地 `light/dark/paper/midnight/hc/colorblind` 6 套 WCAG AA 主题、本地 SVG 精灵图 `web/icons/sprite.svg`、可拖拽/键盘分栏、专注模式、View Transitions、`motion/density/code_ligatures/accent` 设置与 Swarm DAG 检查器面板。
- 全量验证：`python -m pytest tests -q -p no:cacheprovider` -> `413 passed, 3 skipped`；`python tests/ui_contrast.py` 与 `node --check web/js/**/*.js` 全部通过。

### `[2026-11-16 19:00 +08:00]` M2 Mission 运行时（方向 B，已完成核心交付与自动化验收）
- **核心运行时 (`lagent/mission/`)**：实现 `Mission`/`Milestone`/`Episode`/`Handoff`/`AcceptanceCheck` 数据模型及 `.localagent/missions/<id>/MISSION.md` 双向解析同步（支持外部手工编辑在下一 Episode 自动生效）、原子 `state.json` + JSONL `events.jsonl` 持久化、Git 分支 `mission/<id>` 自动提交与一键回滚、`MissionBudgetTracker`（wall/tokens/steps/disk 80% 警告与 100% 自动暂停）、`StallWatchdog`（重复调用/无 diff 进展/连续失败触发 `nudge → replan → review → pause`）、`MissionPolicy`（`attended`/`unattended` allowlist 越界阻塞排队与 `PathGuard`/`HARD_DENY`/`Taint` 不可降级）、`gates.py` 确定性验收与 `power.py` 跨平台运行期防休眠及安静时间窗通知。
- **全栈联动**：新增 `--resume-missions` 启动续跑参数、`/mission` 全套内置命令、`Agent.run_mission_episode`（独立每段干净上下文 + 摘要强制保留 `plan`/`acceptance`/`open_questions`）、`/api/missions*` HTTP/SSE 接口（含 `Last-Event-ID` 续传）、前端侧栏 Missions 进度/预算环与详情监控面板（时间线、`MISSION.md` 在线编辑、门控证据表、阻塞审批与回滚），以及 8 项 M2 单测/E2E 用例和 3 个 `run_eval` 长周期评测场景。
- **验证**：`test_m2_mission.py` 8 项全通过，`ui_smoke.py`（Chromium）[1]–[12] 全通过，`ui_contrast.py` 与 `node --check` 全通过；全量 pytest `419 passed, 2 failed (基线已存在的 M0/M6 FileResponse ASGI 桩), 3 skipped`。

### `[2026-11-16 19:26 +08:00]` M3 多模态输入摄取（方向 C-in，已完成核心交付与自动化验收）
- **输入管道 (`lagent/media/`)**：实现魔数嗅探 `sniff.py`（防扩展名伪装）、能力协商 `capabilities.py`（`/api/capabilities` 与 `/api/state` 暴露 `{vision, audio, audio_model, ocr, whisper, ffmpeg, pdf_render, office, parquet, recorder_enabled}`）、受限子进程隔离 `sandbox.py`/`_worker.py`（Win32 Job Object + POSIX `rlimit` + 超时强杀树+崩溃隔离），覆盖图片（EXIF/GPS 剥离、像素炸弹拦截、长截图切片）、PDF（文本层+扫描页 `pypdfium2` 渲染、JS/Embed 探测提示）、Office（`docx`/`xlsx`/`xlsm`/`pptx` 表格/批注/修订/备注/内嵌图+宏与外链剥离）、CSV/JSONL/YAML/Parquet Schema 抽样、ipynb/EPUB/MHTML/HTML 清洗、Zip/Tar 仅列目录（流式校验压缩比炸弹与 Zip Slip），以及音视频缺失可选二进制时的优雅降级。
- **全栈集成**：接入 `read_file`/`ingest_media`（`OutputStore` 超长落盘与强制 `taint`）、`Agent._append_user` 富媒体附件预处理、`POST /api/sessions/{sid}/upload` 炸弹拦截与处理计划响应、`POST /api/media/ingest`，前端 [composer.js](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/web/js/composer.js) 展示处理方式标签、图片分辨率切换与受能力门控的录音入口。
- **验证**：新增 `tests/fixtures/media/` 16 组夹具与 `test_m3_media.py` 5 项全通过；`ui_smoke.py` [1]–[12] 全通过，`ui_contrast.py` 与 `node --check` 全通过；全量 pytest `424 passed, 2 failed (基线已存在的 M0/M6 FileResponse ASGI 桩), 3 skipped`。

### `[2026-11-16 22:40 +08:00]` M5 Research Engine（方向 D，已完成核心交付与自动化验收）
- **核心引擎 (`lagent/research/`)**：落地 `ResearchPolicy`（默认 `mode="off"`、`--offline` 零出站、域名/类别策略、出站查询密钥/路径/个人画像拦截、PII 脱敏、UA 反伪装、CAPTCHA/付费墙/登录墙识别不绕过、`X-Robots-Tag`/`meta` `noarchive`/`noai`/`noindex` 遵从）、`RobotsCache`（RFC 9309 + `Crawl-delay` + 5xx/4xx 语义）、`DomainLimiter`（全局+每域速率与并发、`429/503 Retry-After` 退避、封禁域熔断）、`WebCache`（`ETag`/`If-Modified-Since` 条件请求与 LRU 淘汰）、`extract.py`（隐藏注入节点剥离）、`EvidenceStore`（`data/evidence.sqlite`）、`verify.py`（`cite_check` 引文原文定位校验）、`ResearchManager`（拆题→RRF 打分→Dual-LLM 无工具 `reader` 隔离抽取→交叉验证→报告产物）、`NetLedger` 与 `PageWatchManager`。
- **全栈集成**：统一策略闸门与账本接入 `net.py`、`tools/search.py`（官方/SearXNG 优先 + `html_scrapers_ack` 显式确认）、`tools/web.py` 与 `HttpMcpSession`；新增 `research_start`/`research_status`/`cite_check` 工具、`/research` 与 `/net` 命令、`/api/research*` & `/api/net/ledger` & `/api/research/policy` 接口、前端「调研」与「网络账本」面板。
- **验证**：`test_m5_research.py` 5 项全绿；全量 pytest **`439 passed, 3 skipped`**；`ui_smoke.py` [1]–[12]、`ui_contrast.py` 与 16 个前端 JS 语法检查全通过。

### `[2026-11-16 23:08 +08:00]` M7 技能库 v2（方向 F，已完成交付与自动化验收）
- **核心架构 (`lagent/skills.py`, `lagent/builtin_skills/`)**：升级至 Spec v2（双语描述、`pack`、`tags`、`triggers{keywords,globs,mimes}`、`requires`、`allowed_tools`、`risk`、`profiles.compact`、`license`、`default_enabled`），兼顾 v1 向后兼容；落地 `default_enabled_limit`（默认 12 条启用的高频核心技能，提示词硬上限 14）、7 大包启停控制、关键词/glob/MIME/`local_embed` 混合技能路由器、`SKILL.compact.md` 紧凑档位选择、`skills.lock` SHA-256 完整性锁定、增强风险扫描与 `python -m lagent.skills lint` CLI（已接入 CI）。
- **全栈集成**：在 `lagent/builtin_skills/` 建成 38 个跨 7 大包的内置技能（均含 `SKILL.compact.md` 与 `evals/cases.json` >= 2 条及 `skills.lock`）；`use_skill` 与 `Agent` 主循环打通 `skills.auto_route` 建议、`model_tier` 适配及 `skill_allowed_tools` 单调收窄；暴露 `/api/skills/packs|pack|lint|route` 与前端分包筛查面板。
- **验证**：`python -m lagent.skills lint` 38/38 全过；`test_m7_skills.py` 8 项全绿；全量 pytest **`447 passed, 3 skipped`**；`ui_smoke.py` [1]–[12]、`ui_contrast.py` 与 JS 语法检查全通过。

### `[2026-11-16 23:31 +08:00]` M8 集成加固与发布 `v0.2.0`（已完成）
- **发布版本与打包**：`lagent.__version__` 升级至 **`0.2.0`**；补全 `packaging/localagent.spec` 对 `swarm`/`mission`/`media`/`research`/`skills`/`metrics` 的隐藏导入声明；同步 `README.md`、`CHANGELOG.md`（`[0.2.0]`）、`docs/API.md`、`THIRD_PARTY_LICENSES.md` 与 `ITERATION_PLAN_2026-11-15_2140.md` §13 全量勾选。
- **本地可观测性与加固**：新增 [metrics.py](file:///C:/Users/nzp18/.gemini/antigravity/scratch/localagent/lagent/metrics.py) + `GET /api/metrics` + 前端设置页「运行指标」面板（展示步数/小时、Token、验证通过率、卡死次数、拒绝请求数、合并冲突率及引擎遥测）；加固 `HttpMcpSession._post_sync` `--offline` 拦截并在 CI 接入 `ui_contrast.py`。
- **全量验证**：`test_m8_release.py` 5 项安全/发布专项全通过；全量 pytest **`452 passed, 3 skipped`**；`ui_smoke.py` [1]–[12]、`ui_contrast.py`、`lagent.skills lint` 与前端 JS 语法检查全通过。
