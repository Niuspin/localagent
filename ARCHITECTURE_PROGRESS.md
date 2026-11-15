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
