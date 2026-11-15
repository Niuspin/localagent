# LocalAgent HTTP & SSE API 契约

本文档记录 `web/js/*.js` 与 `lagent/server.py` 之间的 HTTP 与 SSE 通信接口。  
修改任何字段前请同步更新本文件、`lagent/server.py` 与对应的 `web/js/*.js` 及测试。

---

## 认证与通用头

- 除 `GET /` 外，所有 `/api/*` 均需通过 Cookie `la_token=<token>` 或 `Authorization: Bearer <token>` 认证。
- 启动器打印的地址 `http://127.0.0.1:<port>/?t=<token>` 访问根路径后会下发 `HttpOnly`、`SameSite=Strict` 的会话 Cookie 并重定向到 `/`。
- 响应统一带安全头：`Content-Security-Policy`（严禁外链与内联脚本）、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`；并严格校验本机 `Host` / `Origin`。

---

## 全局状态、配置与引擎

### `GET /api/state?session_id=...`
返回应用实时快照（可选传 `session_id` 解析当前会话工作区、会话级权限覆盖与 `tainted` 状态）：
- `version` (str)
- `workspace` (str | null)
- `permission_mode` (str)：当前会话生效权限策略（优先使用会话覆盖值，否则回退全局默认）
- `global_permission_mode` (str)：全局默认权限策略
- `offline` (bool)：是否处于 `--offline` / `LOCALAGENT_OFFLINE=1` 离线模式
- `tainted` (bool)
- `hw`: `HwInfo`（`cpu_cores`、`cpu_threads`、`ram_gb`、`ram_free_gb`、`gpus`、`gpu_backend` 等）
- `sandbox`: `{"available": bool, "kind": str, "hardening"?: "win32-job", ...}`
- `engine`:
  - `status`: `"stopped" | "starting" | "ready" | "stopping" | "error"`
  - `model` (dict | null), `mmproj` (str | null), `ctx` (int), `ctx_cap` (int), `vision` (bool)
  - `binary` (str | null), `error` (str | null)
  - `tps` (float), `perf` (`TelemetryTracker` summary: `prompt_tps`, `eval_tps`, `ttft_ms`, `spec_accept_rate`, `spec_recommended`, `cache_hit_rate`, ...)
- `models`: `ModelInfo[]`（含 `file`, `size_gb`, `arch`, `name`, `moe`, `experts`, `experts_used`, `ctx_train`, `quant`, `mmproj`, `vision`, `is_draft`, `draft_model`, `compatible_drafts`, `error`）
- `mmprojs`: `list[str]`

### `POST /api/engine/plan` / `start` / `restart` / `stop` 及 `GET /api/engine/logs`
- `POST /api/engine/plan`: 预览模型参数规划与命令行（不启动进程），参数 `{"model"?, "mmproj"?, "ctx"?}` -> `{"plan": dict, "command": str}`。
- `POST /api/engine/start` / `restart` / `stop`: 控制 `llama-server` 生命周期并返回 `engine` 状态。
- `GET /api/engine/logs?n=200`: 返回最近引擎日志行（最多 600 行）。

### `GET /api/config` / `PATCH /api/config`
读取或局部更新 `config.json`（含范围 `RANGES`、枚举 `SPECIAL` 校验，并将 `compact_api_key`、`embed_api_key` 及检索 Key 写入隔离存储 `secrets.json`，对外脱敏返回布尔标记）。  
返回：`{"config": dict}`。

### `GET /api/commands` / `GET /api/permissions` / `GET /api/hooks`
均支持 `?workspace=...&session_id=...` 解析目标工作区：
- `GET /api/commands`: 返回内置 15 条 Slash 命令 + `.localagent/commands/*.md` / `.claude/commands/*.md` 自定义命令列表。
- `GET /api/permissions`: 返回细粒度权限规则 `{"rules": {"deny": [...], "ask": [...], "allow": [...]}}`。
- `GET /api/hooks`: 返回已加载生命周期钩子 `{"hooks": {...}}`（含 `read_only` 标识）。

---

## 工作区、目录选择与检查点

### `GET|PUT /api/workspace` / `POST /api/fs/pick` / `GET /api/fs/browse`
- `GET|PUT /api/workspace`: 读取或绑定当前工作区目录（校验 `PathGuard`，可选传入 `session_id`）。
- `POST /api/fs/pick`: 调用操作系统原生目录选择对话框（请求体可选 `{"initial_dir": str}`），返回 `{"path": str | null, "cancelled": bool}`。
- `GET /api/fs/browse?path=...`: 本机目录列表浏览（弹窗路径输入与回退选择器）。

### `GET /api/workspace/tree` / `GET /api/workspace/file` / `GET /api/workspace/git`
- `GET /api/workspace/tree?recursive=1`: 列出工作区目录项；`recursive=1` 返回递归相对路径集合（供 `@` 补全与 `Ctrl/Cmd+K` 命令面板检索）。
- `GET /api/workspace/file?path=...`: 读取工作区内文本、图片或 Office/PDF 安全预览。
- `GET /api/workspace/git?path=...`: 读取 Git 分支、变更文件清单与单文件 Diff（非 Git 仓库返回 `{"is_repo": false}`）。

### `GET /api/journal` / `GET /api/sessions/{sid}/checkpoints` / `POST /api/journal/{jid}/undo` / `POST /api/sessions/{sid}/undo` / `/api/trash`
- `GET /api/sessions/{sid}/checkpoints`: 按回合（`turn_id`）聚合 `Journal` 文件快照，用于“按回合时光机”检查与回退。
- `POST /api/sessions/{sid}/undo`: 请求体可选 `{"turn_id"?: str, "force"?: bool}`，回退指定回合或最新回合的文件修改；运行中返回 `409 Conflict`。
- `GET /api/trash` / `DELETE /api/trash` / `POST /api/trash/{tid}/restore`: 查看、清空与恢复工作区回收站文件。

---

## 会话、运行、审批与实时事件

### `GET|POST /api/sessions` / `GET /api/sessions/search?q=...` / `GET|PATCH|DELETE /api/sessions/{sid}`
- `GET /api/sessions` / `POST /api/sessions`: 列出摘要索引或新建会话。
- `GET /api/sessions/search?q=...`: 跨会话标题、正文、工具卡片及摘要全文检索并返回高亮片段 `snippet`。
- `PATCH /api/sessions/{sid}`: 更新会话字段 `{"title"?, "pinned"?, "workspace"?, "permission_mode"?}`（支持会话级 `"ask" | "auto_edit" | "plan" | "auto" | ""` 权限策略覆盖）。

### `POST /api/sessions/{sid}/messages` / `upload` / `compact` / `truncate` / `untaint` / `export`
- `POST /api/sessions/{sid}/messages`: 提交用户消息与附件：`{"text": str, "images"?: list[str], "files"?: list[str], "skills"?: list[str]}` -> `{"run_id": str}`。
- `POST /api/sessions/{sid}/upload`: 上传附件到工作区 `uploads/`。
- `POST /api/sessions/{sid}/compact`: 手动触发上下文摘要压缩（优先通过 `ModelRouter` 调用配置的压缩端点）。
- `POST /api/sessions/{sid}/truncate`: 请求体 `{"from_index": int}`，按 UI 消息索引回退历史对话并联动回滚多轮文件快照。
- `POST /api/sessions/{sid}/untaint`: 手动解除当前会话的 `tainted` 污染标记。
- `GET /api/sessions/{sid}/export`: 导出 Markdown 格式对话记录。

### `GET /api/runs/{rid}/events`（单次运行 SSE）与 `POST /api/runs/{rid}/cancel`
推送实时运行事件（M0 起所有事件负载统一注入元数据 `{ts, kind, run_id, agent_id, swarm_id, mission_id}`）：
- `thinking_delta` / `thinking_end`
- `text_delta` / `assistant_done`
- `tool_start` / `tool_output_delta` / `subagent_step` / `tool_result`
- `approval_request`（前端通过 `POST /api/approvals/{aid}` 提交 `{"decision": "allow" | "allow_session" | "deny"}`）
- `question_request`（前端通过 `POST /api/questions/{qid}` 提交 `{"answer": str}`）
- `usage`: `{"prompt_tokens": int, "completion_tokens": int, "ctx": int, "used": int, "pct": float, "tps": float, "cache_hit_rate": float, "ttft_ms": int}`
- `notice` / `run_end`: `{"status": "done" | "cancelled" | "error", "stats": dict, ...}`

### `GET /api/events`（全局 SSE）
实时推送跨页面状态事件：`engine`、`config`、`mcp`、`hub`、`state`、`notice`（统一注入 `{ts, kind, run_id, agent_id, swarm_id, mission_id}`）。

### `GET /api/artifacts` / `GET /api/artifacts/{aid}/meta` / `GET /api/artifacts/{aid}`
- `GET /api/artifacts?session_id=...&kind=...&limit=200`: 列出 SQLite 索引的会话产物摘要（返回 `{"artifacts": [...]}`，不包含宿主绝对路径）。
- `GET /api/artifacts/{aid}/meta`: 返回单个产物元数据与安全下载链接 `download_url`。
- `GET /api/artifacts/{aid}`: 安全下载原始产物文件（强制附带 `Content-Disposition: attachment` 与 `X-Content-Type-Options: nosniff`）。

---

## MCP 生态、Model Hub、记忆、技能与系统控制

- **MCP (`/api/mcp`, `/api/mcp/{name}`, `/api/mcp/{name}/start|stop`)**: 支持 `"stdio"`（`sandbox` 受控启动）与 `"http"`（Streamable HTTP / SSE，Header 密钥隔离脱敏）传输；支持复杂 JSON Schema (`$ref`/`$defs`/`anyOf`/`oneOf`) 与 `roots/list`、`list_changed` 热推送。
- **MCP Resources / Prompts**: `GET /api/mcp/{name}/resources`、`POST /api/mcp/{name}/resources/read`、`GET /api/mcp/{name}/prompts`、`POST /api/mcp/{name}/prompts/get`。
- **Model Hub (`/api/hub/recommend`, `/api/hub/downloads`, `/api/hub/download`, `/api/hub/downloads/{tid}/cancel`)**: 硬件显存/内存自适应推荐、官方/国内镜像（`hf-mirror`）切换、HTTP `Range` 断点续传与 SHA-256 校验（离线模式严格拦截）。
- **记忆 (`/api/memory`, `/api/memory/export`, `/api/memory/import`, `/api/memory/{mid}`, `/api/memory/{mid}/approve`)**: SQLite FTS5 + `mem_vectors` 混合向量 RRF 检索与污染记忆人工审批。
- **技能 (`/api/skills`, `/api/skills/install`, `/api/skills/create`, `/api/skills/{name}`, `/api/skills/{name}/enable`)**: 本地及内置技能包管理。
- **审计与检索自测 (`GET /api/audit`, `GET /api/audit/verify`, `POST /api/search/test`)**: 哈希链防篡改日志校验及联网搜索引擎连通性测试。
- **应用退出 (`POST /api/app/quit`)**: 安全停止 `llama-server` 并退出本地桌面应用或服务进程。
