# LocalAgent 架构评估与演进建议

> 评估基线：`main` (`be0f14b`)  
> 参考维度：前沿 Agent 交互工作台（Claude Code / Cursor / Codex / Antigravity）、本地推理与 KV 缓存架构（`llama.cpp` / Router / Hybrid Memory）、零信任沙箱与多端部署工程。

---

## 0. 现有架构优势基线

当前代码库已具备清晰的工程底座：
- **离线优先与零外部前端依赖**：纯原生 JS/CSS 桌面 UI，严格本地 CSP 策略，Markdown 渲染拦截外部追踪图片与不安全协议链接（`web/js/md.js`）。
- **轻量自举与 GGUF 硬件感知**：SHA-256 校验引导下载 `llama.cpp`（`lagent/bootstrap.py`），二进制级 GGUF 元数据探测与 MoE/投机解码参数顾问（`lagent/gguf.py`、`lagent/inference.py`）。
- **纵深防御意识**：`PathGuard` 路径边界与敏感文件拦截、命令只读分类、外部内容 `taint` 追踪、防篡改哈希链审计日志（`lagent/security.py`）、修改前文件快照与回收站（`lagent/journal.py`）。
- **可量化调优基础**：内置摩擦统计（`RunStats`）与真实模型回归评测框架（`tests/eval/run_eval.py`）。

---

## 1. P0 级已发现缺陷与前后端断层

在代码遍历过程中直接定位到以下可复现的功能断点与契约不一致问题，应作为第一批次修复目标：

1. **Slash 命令 `/model`、`/plan`、`/mode` 运行时崩溃**
   - `lagent/commands.py` 第 240 行调用 `eng.profile()`，但 `lagent/engine.py` 第 120 行的 `self.profile` 是 `Profile | None` 属性而非方法；且第 234、243 行直接打印 `eng.model` 整包扫描字典。
   - `lagent/commands.py` 第 294、311 行与 `lagent/loop.py` 第 342 行调用 `cfg.set("agent", "permission_mode", ...)`，但 `lagent/config.py` 中 `Config` 仅定义了 `get` / `patch` / `set_workspace` / `secret` / `public`，缺少 `.set()` 方法。
2. **`@` 引用文件无法补全子目录路径**
   - 前端 `web/js/composer.js` 的 `fetchFileTree()` 只请求根目录 `/api/workspace/tree?path=` 并递归期待 `e.children`；
   - 后端 `lagent/server.py`（第 510–521 行）仅通过 `os.scandir(p)` 返回单层目录，无 `children` 字段，且前端 `fileTreeCache` 在切换工作区或 `fschanged` 时未失效。
3. **自定义 Slash 命令及规则检查接口未接入前端**
   - 后端 `lagent/server.py`（第 711–728 行）已提供 `GET /api/commands`（含 `.localagent/commands/*.md`、`.claude/commands/*.md`）、`/api/permissions`、`/api/hooks`；
   - 前端 `web/js/composer.js`（第 142–156 行）硬编码了 13 条 `BUILTIN_SLASH`（漏掉 `/mode`、`/unplan` 及工作区自定义命令），设置页也缺少规则与 Hook 状态检查界面。
4. **`verify` 模式子代理被强制降级导致无法执行验证命令**
   - `lagent/tools/agentic.py` 暴露 `delegate_task(..., mode="verify")`，但 `lagent/loop.py` 第 504 行对 `depth > 0` 一律强制 `mode = "plan"`，过滤掉了 `run_command` 等验证必需工具。
5. **“编辑重发 / 重新生成”丢失附件，且 `truncate` 只回滚最后一轮文件快照**
   - `web/js/chat.js`（第 215–229 行）的 `editFrom` 与 `regenerate` 仅保留 `m.text`，丢弃 `images`、`files`、`skills`；
   - `lagent/loop.py`（第 1157–1173 行）的 `truncate()` 无论向前截断多少轮，都只调用一次 `rewind_to_turn`。
6. **API 文档与后端鉴权/状态字段漂移**
   - `docs/API.md` 记载的 `Authorization: Bearer <token>`、`GET /api/models`、`POST /api/workspace`、`GET /api/browse`、`/api/files`、`/api/app/shutdown`、`allow_turn`、`stdio / HTTP MCP` 与 `lagent/server.py` 实际契约（Cookie-only 鉴权、`/api/fs/browse`、`PUT /api/workspace`、`/api/workspace/tree`、`/api/app/quit`、`allow_session`、仅 stdio）不一致；
   - `lagent/server.py` 第 199 行在 `/api/state` 中硬编码 `"tainted": False`。

---

## 2. 推理与硬件调度架构（Inference & Engine Architecture）

| 现状瓶颈 | 源码位置 | 架构改进方向 |
| :--- | :--- | :--- |
| **单槽位串联阻塞** | `lagent/engine.py`（`-np 1` 硬编码，`--parallel` / `--models-dir` 列入 `_BLOCKED_FLAGS`） | 解锁多槽位并发与调度队列，区分高优交互推理与后台压缩/子代理/向量任务。 |
| **无角色化多模型路由** | `lagent/loop.py`（第 636、1068 行硬编码 `model: "local"`） | 引入 `ModelRouter` 抽象：支持“主推理模型 + 轻量摘要/路由小模型 + 本地 Embedding 模型”协同，并兼容 OpenAI/Anthropic/Ollama/vLLM 外部端点回退。 |
| **KV 前缀缓存击穿与扩容重加载** | `lagent/context.py` `prune_old()` 原地修改历史 `tool` 消息；`lagent/loop.py` 第 1047–1053 行 `ctx_auto_grow` 通过 `eng.restart` 重启整进程 | 1. 历史消息严格 **Append-Only**（大输出入模前即置为引用摘要），固定缓存断点，保护 `--cache-reuse` 命中率。<br>2. 对接 `llama-server` `/slots/{id}?action=save` / `restore` 持久化热点会话 KV 检查点。 |
| **分片 GGUF 与 `--fit` 规划割裂** | `lagent/gguf.py` 第 94–99 行对分片 GGUF 返回 `None`；`lagent/engine.py` 启用 `--fit on` 时跳过 MoE 拆分与 `--moe-cache-mib` | 补全 `-00001-of-0000N.gguf` 全分片张量解析；打通 `TelemetryTracker` 记录 TTFT 与 `spec_accept_rate`，低于阈值时自动关闭负收益投机解码。 |
| **硬件识别与推理格式硬编码** | `lagent/hw.py` 第 107 行仅探测 `nvidia-smi`/`rocm-smi`，忽略 Windows Vulkan/Intel GPU；`lagent/engine.py` 第 393 行硬编码 `--reasoning-format deepseek` | 通过 `llama-server --list-devices` 统一探测 Vulkan/CUDA/ROCm/Metal 显存并支持多卡 `--tensor-split`；按架构/模板自适应思考标签及 `reasoning_budget`；为小模型增加 JSON Schema / GBNF 语法约束降级。 |
| **阻塞线程流式客户端** | `lagent/net.py` 第 289–362 行每次 `sse_post` 新建 OS 线程跑 `http.client` | 迁移至异步连接池客户端（如 `httpx.AsyncClient`），支持连接复用与精细背压取消。 |

---

## 3. Agent 核心运行时、代码智能与记忆架构

1. **一等公民子代理与隔离工作流**
   - 将子代理从单层只读阻塞调用升级为可持久化、可并发、可恢复的 Subagent 树；按角色分级授权（`explore` 只读、`verify` 白名单测试命令、`worker` 基于 **Git Worktree** 并行隔离修改并输出 Diff 审核合并）。
2. **代码库结构索引与即时验证闭环**
   - 替换 `lagent/tools/files.py` 中纯 Python `os.walk` + 正则扫描：优先集成 `ripgrep` (`rg`) 与 `.gitignore` 感知遍历；
   - 引入 **Tree-sitter 符号大纲（RepoMap）** 节省小上下文成本，接入轻量 **LSP Diagnostics 桥接**（编辑文件后即时返回语法/类型诊断）；
   - 在单处子串 `edit_file` 基础上补充多处原子批量编辑与行号/内容哈希锚定编辑。
3. **有状态交互终端与流式输出**
   - 将无状态、缓冲完成才返回的 `run_command` 升级为持久化环境目录/PTY + 实时增量 `tool_stdout` / `tool_stderr` 事件推送（含后台任务事件流）。
4. **精准 Token 计量与分层项目规则**
   - `lagent/context.py` 优先通过 `llama-server /tokenize` 精确估算；`seen_files`（含哈希）与结构化工作状态随会话持久化；`lagent/projectdoc.py` 支持按路径按需加载嵌套子目录 `AGENTS.md` / glob 规则文件。
5. **混合记忆检索与统一检查点**
   - `lagent/memory.py` 在 SQLite FTS5 之上叠加本地 Embedding **BM25 + Dense Vector + RRF 混合检索** 与后台情景蒸馏合并；
   - 将 `lagent/sessions.py` 从启动全量加载 JSON 改为按需索引 + 增量事件日志，并将 `Journal` 文件快照与对话轮次绑定为统一可分支 Checkpoint。

---

## 4. 前端交互与桌面工作台（Frontend UI/UX）

1. **增量 Markdown 渲染与长会话虚拟化**：改进 `web/js/chat.js`（第 336–339 行）每帧全量 `d.innerHTML = LA.renderMarkdown(...)` 导致的 DOM 销毁、划词选区打断与 $O(N^2)$ 重绘问题；补充离线 KaTeX 数学公式、Mermaid 架构图及可点击跳转的文件行号链接；右侧文件预览（`web/js/panels.js` 第 126 行）复用 `LA.highlight()` 并显示行号。
2. **运行中干预（Steer / Follow-up Queue）与并发可观测性**：打破 `LA.live` 期间完全锁定输入的限制，允许在 Agent 执行过程中补充/排队纠偏指令；增加嵌套子代理轨迹卡片、流式终端输出区、顶部/输入框上方待审批队列条与拒绝对话引导；将底部权限切换（`web/js/composer.js` 第 352 行）从覆盖全局配置调整为支持会话级策略。
3. **Git 工作流整合与效率入口**：在右侧“变更”抽屉加入 Git Status / Unstaged Diff 汇总视图；加入全局命令面板（`Ctrl/Cmd + K`）、会话全文检索、SSE 断线一键恢复与后台运行态跨会话标识。

---

## 5. MCP 生态与跨平台零信任沙箱

1. **补齐 Windows 与 macOS 真沙箱**：消除 `lagent/security.py` 第 376 行 Windows `kind="none"` 造成的隔离真空（MCP 默认因 fail-closed 无法启动或关闭沙箱后暴露全宿主权限），探索 **AppContainer / Restricted Token + Job Object** 或 **WSL2 `bwrap` / 微容器** 后端；收紧 macOS 过宽的 `(allow file-read*)` 规则。
2. **完整 MCP 规范栈**：在 `StdioRpc` 之外支持 **Streamable HTTP / SSE 与 OAuth 2.0**；对接 **Resources / Prompts / Roots / Sampling**；保留复杂 JSON Schema（`anyOf`/`oneOf`/`$ref`）并实现 `list_changed` 主动推流热更新。
3. **确定性安全护栏与来源感知污染**：避免 `tainted=True` 时在 `lagent/hooks.py` 跳过安全类 Hook；监听 `.localagent/settings.json` 与 `hooks.json` 变更自动热重载；支持 `Authorization: Bearer` 请求头与精细化 Provenance 污染衰减。

---

## 6. 部署分发、守护进程解耦与评测可观测

1. **Model & Runtime Hub**：依据本机 `hw.py` 硬件扫描自动推荐并支持断点续传/镜像拉取 GGUF、配套 `mmproj` 与 `draft` 模型，补齐 `llama-server` 增量升级与 Linux ROCm 资产匹配。
2. **Daemon 与 UI 解耦**：将桌面窗（`pywebview`）与后台常驻运行时解耦，关闭窗口不强杀热加载模型与后台长任务，支持系统托盘、多窗口唤醒与原生 OS 文件夹选择器。
3. **可观测性与评测扩张**：扩充 `tests/eval/run_eval.py` 至多轮重构、LSP 自愈与红队防注入场景，并提供 Step 级 KV 命中与耗时 Trace 视图。

---

## 7. 演进路线图（Roadmap）

| 阶段 | 核心目标 | 关键改造项 |
| :--- | :--- | :--- |
| **Phase 1 (P0)** | 消除现有功能断点 | 修复 `/model`/`/plan`/`/mode` 崩溃；打通 `@` 全工作区递归文件检索与缓存失效；前端接入 `/api/commands` 与权限/钩子视图；修复 `editFrom`/`regenerate` 丢附件；解锁 `verify` 子代理命令权限；修复 `truncate` 多轮快照回滚与 `/api/state` 状态字段；对齐 `docs/API.md`。 |
| **Phase 2 (P1)** | 推理提效与 Coding 闭环 | Append-Only 前缀缓存保护 + `/tokenize` 精准计量 + 槽位 KV 保存恢复；多槽位/多角色模型路由 + 分片 GGUF 扫描；`ripgrep`、Tree-sitter RepoMap、LSP 诊断回传、流式命令输出与前端增量渲染 / Git Diff 面板。 |
| **Phase 3 (P2)** | 并发协作、真沙箱与分发 | Git Worktree 隔离并发子代理 + 前端 Steer 队列与 `Ctrl+K`；Windows 真沙箱 + Streamable HTTP/Resources/Prompts MCP；内置 Model Hub、Daemon-UI 解耦与 CLI/Gateway 入口。 |
