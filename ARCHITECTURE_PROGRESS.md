# LocalAgent 架构优化进度报告

> **记录时间**：2026-11-15 20:20:00 +08:00  
> **对照基线**：[ARCHITECTURE_REVIEW.md](./ARCHITECTURE_REVIEW.md)（评估基线 `be0f14b`）  
> **当前版本**：`main` (`96cbb0a`) · 关联提交 `fb732a4` → `8d45b75` → `96cbb0a`  
> **最近验证**：`.\.venv\Scripts\python.exe -m pytest -q`（**385 passed, 3 skipped**）· `.\.venv\Scripts\python.exe tests\ui_smoke.py`（**12 项全部通过**，0 外部请求、0 控制台/CSP 错误）

---

## 总体进度概览

| 阶段 / 维度 | 状态 | 完成度评估 | 说明 |
| :--- | :--- | :--- | :--- |
| **Phase 1 (P0)：前后端断点与契约修复** | **已完成** | **6 / 6 (100%)** | `/model`·`/plan`·`/mode`、递归 `@` 补全、Slash/规则/Hooks 接入、`verify` 子代理、附件重发/多轮 `truncate`、`Bearer` 鉴权与 `docs/API.md` 对齐全部落地并有测试覆盖。 |
| **Section 2 / Phase 2：推理与硬件调度架构** | **大部分完成** | **核心链路已打通** | 可配 `-np` 槽位、`ModelRouter` 摘要路由、`save_slot`/`restore_slot` + `/tokenize`、分片 GGUF 张量估算、Telemetry 负收益投机降级、`--list-devices` 兜底与 `--reasoning-format` 已落地；`httpx` 连接池迁移与 `--tensor-split`/GBNF 语法约束待后续按需扩展。 |
| **Section 3 / Phase 2：Agent 运行时、代码智能与记忆** | **部分完成** | **主体闭环已落地** | `rg` 搜索 + `.gitignore` 裁剪、轻量 AST/正则 `repo_map`、编辑后语法/编译自检（`_post_edit_check`）、`edit_file(edits=...)` 原子批量编辑、流式终端输出、`seen_files` 持久化、分层子目录 `AGENTS.md`、SQLite FTS5 已完成；Git Worktree 并发写入子代理、LSP 客户端、本地 Embedding 混合向量检索待 Phase 3 演进。 |
| **Section 4 / Phase 2：前端工作台与交互体验** | **大部分完成** | **高频体验已上线** | `Ctrl/Cmd+K` 全局命令面板、Steer/纠偏排队、代码行号高亮预览、Git Diff Drawer、文件行号点击跳转、SSE 一键重连、会话全文检索、子代理执行轨迹卡片、离线 Mermaid 源码卡片与 `$$` 公式块已上线；超长对话增量 Markdown DIFF/虚拟滚动优化待后续迭代。 |
| **Section 5：MCP 生态与跨平台零信任沙箱** | **部分完成** | **规则热重载与鉴权已完成** | `PermissionManager` / `HookManager` 配置热重载与 `Authorization: Bearer` 鉴权已完成；Windows 原生沙箱（AppContainer / Restricted Token）与 Streamable HTTP / OAuth MCP 仍处于 Phase 3 规划中。 |
| **Section 6：部署分发、守护进程与评测** | **未开始 / 保留基线** | **既有能力保持稳定** | Model & Runtime Hub、Daemon-UI 解耦与红队/LSP 扩展评测纳入下一阶段演进。 |

---

## 逐项对照清单

### 一、P0 级已发现缺陷与前后端断层（`ARCHITECTURE_REVIEW.md` §1）

| 编号 | 问题项 | 状态 | 实现提交 | 核心改动位置 |
| :--- | :--- | :--- | :--- | :--- |
| 1.1 | Slash 命令 `/model`、`/plan`、`/mode` 崩溃与缺少 `Config.set()` | **已完成** | `8d45b75` | `lagent/config.py`（补齐 `Config.set()`）、`lagent/commands.py`（修正属性访问与精简输出） |
| 1.2 | `@` 引用文件无法补全子目录及缓存失效问题 | **已完成** | `8d45b75` | `lagent/server.py`（`/api/workspace/tree?recursive=1`）、`web/js/composer.js`（递归展平与缓存失效） |
| 1.3 | 自定义 Slash 命令与工作区规则/Hooks 检查未接入前端 | **已完成** | `8d45b75`, `96cbb0a` | `web/js/composer.js`（动态拉取并缓存 `/api/commands`）、`web/js/settings.js`（安全页接入 `/api/permissions` & `/api/hooks`）、`lagent/server.py`（支持 `?session_id=` 解析） |
| 1.4 | `verify` 模式子代理被强制降级为 `plan` | **已完成** | `8d45b75` | `lagent/loop.py`（区分 `verify` 与 `explore`/`plan`，保留只读验证命令能力） |
| 1.5 | “编辑重发 / 重新生成”丢失附件，且 `truncate` 只回滚单轮 | **已完成** | `8d45b75` | `web/js/chat.js`（保留 `images`/`files`/`skills`）、`lagent/loop.py`（`truncate()` 按撤销链回滚多轮快照） |
| 1.6 | API 文档、`Authorization: Bearer` 与 `/api/state` `tainted` 漂移 | **已完成** | `8d45b75`, `96cbb0a` | `lagent/server.py`（Bearer 解析 + 会话维度 `tainted`）、`docs/API.md`（同步最新接口与 SSE 事件契约） |

---

### 二、推理与硬件调度架构（`ARCHITECTURE_REVIEW.md` §2）

| 评估项 | 状态 | 已完成部分 | 未完成 / 后续规划 |
| :--- | :--- | :--- | :--- |
| **多槽位并发调度** | **部分完成** | 已解除 `-np 1` 硬编码，新增 `engine.slots`（`1..8`）并贯穿 `build_args()` 与设置面板（`96cbb0a`）。 | 多请求并发队列调度与前后台优先级隔离（高优对话 vs 后台压缩）。 |
| **角色化多模型路由 (`ModelRouter`)** | **已完成（摘要角色）** | 新增 `lagent/router.py`，支持 `agent.compact_endpoint` / `agent.compact_model` 将摘要压缩分流至外部 OpenAI 兼容小模型并自动回退本地引擎（`96cbb0a`）。 | Embedding 专用模型角色路由、Anthropic 格式直接适配。 |
| **KV 前缀缓存保护与槽位检查点** | **部分完成** | 已打通 `/slots/{id}?action=save` / `restore` 与 `--slot-save-path`，并在上下文扩容重启前后自动保存/恢复热点槽位（`96cbb0a`）。 | `prune_old()` 彻底切换为只追加不可变消息历史以消除前缀重算。 |
| **分片 GGUF 解析与负收益投机降级** | **已完成** | `lagent/gguf.py` 补全 `-00001-of-0000N.gguf` 全分片张量布局；`TelemetryTracker` 新增 TTFT / `spec_accept_rate` 统计，`plan()` 支持 `auto_disable_bad_spec` 自动关闭低命中投机解码（`8d45b75`, `96cbb0a`）。 | — |
| **跨后端硬件探测与推理格式控制** | **部分完成** | `lagent/hw.py` 新增 `_llama_devices()` 回退探测 Vulkan/SYCL/CUDA/ROCm/Metal；新增 `engine.reasoning_format`（`auto`/`deepseek`/`none`）替代硬编码（`96cbb0a`）。 | 多卡 `--tensor-split` 比例调度、`reasoning_budget` 与小模型 GBNF/Schema 约束降级。 |
| **异步连接池 HTTP/SSE 客户端** | **未开始** | 维持标准库零第三方依赖实现（`lagent/net.py`）。 | 若引入 `httpx` 或纯 `asyncio` HTTP 客户端，再替换每请求 OS 线程模型。 |

---

### 三、Agent 运行时、代码智能与记忆架构（`ARCHITECTURE_REVIEW.md` §3）

| 评估项 | 状态 | 已完成部分 | 未完成 / 后续规划 |
| :--- | :--- | :--- | :--- |
| **子代理分层权限与隔离工作流** | **部分完成** | 解锁 `verify` 只读验证权限，后端转发 `subagent_step` 实时轨迹事件至前端工具卡片（`8d45b75`, `96cbb0a`）。 | 多层级持久化子代理树、基于 **Git Worktree** 并行修改并输出 Diff 审核合并。 |
| **代码检索、RepoMap、批量编辑与验证** | **部分完成** | `grep` 优先调用 `rg` 并感知 `.gitignore`（`8d45b75`）；`edit_file` 新增 `_post_edit_check()` 编译/语法即时检查及 `edits` 数组原子批量编辑；新增 `lagent/repomap.py` 与 `repo_map` 工具提取 Python AST 及 JS/TS/Go/Rust 符号（`96cbb0a`）。 | 接入完整 Tree-sitter 语法树与真实 Language Server (LSP) 语义诊断桥接；行哈希锚定编辑。 |
| **流式命令输出与有状态终端** | **部分完成** | `run_command` 支持实时流式 `tool_output_delta` 增量输出（`8d45b75`）。 | 持久化目录/环境变量与 PTY 交互式终端。 |
| **精准 Token 计量与嵌套规则加载** | **已完成** | `loop.py` `_fit_context()` 接入 `/tokenize` 实测校准；`seen_files` 持久化至会话（`8d45b75`）；`lagent/projectdoc.py` `load_for_path()` 按路径按需动态注入嵌套子目录 `AGENTS.md` / `CLAUDE.md`（`96cbb0a`）。 | — |
| **混合记忆检索与统一检查点** | **部分完成** | 已有 SQLite FTS5 全文记忆检索（`lagent/memory.py`）与文件快照（`lagent/journal.py`）。 | 本地 Embedding Dense Vector + RRF 混合检索、后台情景蒸馏与可分支会话树 Checkpoint。 |

---

### 四、前端交互与桌面工作台（`ARCHITECTURE_REVIEW.md` §4）

| 评估项 | 状态 | 已完成部分 | 未完成 / 后续规划 |
| :--- | :--- | :--- | :--- |
| **代码高亮预览、文件引用、离线图表/公式** | **部分完成** | 右侧预览支持 `LA.highlight()` 与行号显示；对话中识别文件行号并可跳转预览（`8d45b75`）；`md.js` 新增零外链 CSP 安全的 `.mermaid-box` 与 `$$` 公式块（`96cbb0a`）。 | `chat.js` 增量块级 Markdown DOM 复用、长会话虚拟列表滚动及离线静态矢量图表/公式包选配。 |
| **Steer 队列、子代理轨迹与终端输出流** | **已完成** | 运行期开放输入进入 Steer 待发队列；支持流式终端输出块、SSE 断线一键恢复（`8d45b75`）与 `delegate_task` 子代理轨迹折叠展示（`96cbb0a`）。 | 顶部全局待审批队列条与拒绝对话后一键改写引导（按需补充）。 |
| **Git 工作流、命令面板与会话全文检索** | **已完成** | 新增工作区 Git Status / Diff 面板、`Ctrl/Cmd+K` 全局命令面板（`8d45b75`）及 `GET /api/sessions/search?q=...` 联调侧栏全文搜索摘要预览（`96cbb0a`）。 | 会话级（非全局覆盖）权限策略覆盖（按需补充）。 |

---

### 五、MCP 生态、零信任沙箱与部署分发（`ARCHITECTURE_REVIEW.md` §5–§6）

| 评估项 | 状态 | 已完成部分 | 未完成 / 后续规划 |
| :--- | :--- | :--- | :--- |
| **规则/Hooks 热重载与 Bearer 鉴权** | **已完成** | `PermissionManager` & `HookManager` 实现基于 `(mtime_ns, size)` 签名的自动热重载（`96cbb0a`）；服务层完整支持 `Authorization: Bearer` 鉴权（`8d45b75`）。 | `tainted=True` 下区分可读/安全只读类 Hook 策略精细化。 |
| **Windows / macOS 真实沙箱** | **未开始** | Linux 支持 `bwrap`，Windows/macOS 保持现有限制环境与路径守卫。 | Windows AppContainer / Restricted Token + Job Object 或 WSL2 隔离；macOS 精细化 `sandbox-exec` profile。 |
| **完整 MCP 协议栈** | **未开始** | 维持 stdio MCP 与安全超时控制（`lagent/mcp/`）。 | Streamable HTTP / SSE 传输、OAuth 2.0、Resources/Prompts/Roots/Sampling 与 `list_changed` 热推流。 |
| **Model Hub、Daemon 解耦与评测扩充** | **未开始** | 保持既有引导下载（`bootstrap.py`）、`pywebview` 窗体与 `tests/eval/run_eval.py`。 | 硬件自适应模型推荐与断点续传下载、Daemon 后台常驻解耦、多轮重构与红队防注入评测集。 |

---

## 阶段汇总与 Git 提交记录

- **`fb732a4`** (`2026-11-15 19:42:06 +0800`): `docs: add frontier architecture review and evolution roadmap`
- **`8d45b75`** (`2026-11-15 19:53:41 +0800`): `feat: implement P0 contract fixes and P1 frontier agent optimizations`
- **`96cbb0a`** (`2026-11-15 20:12:39 +0800`): `feat: inference router, repomap, atomic multi-edit, live rules/hooks & UI trace`
