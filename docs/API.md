# LocalAgent HTTP & SSE API 契约

本文档记录 `web/js/*.js` 与 `lagent/server.py` 之间的 HTTP 与 SSE 通信接口。  
修改任何字段前请同步更新本文件、`lagent/server.py` 与对应的 `web/js/*.js` 及测试。

---

## 认证与通用头

- 除 `GET /` 外，所有 `/api/*` 均需通过 Cookie `la_token=<token>` 或 `Authorization: Bearer <token>` 认证。
- 启动器打印的地址 `http://127.0.0.1:<port>/?t=<token>` 访问根路径后会下发 `HttpOnly`、`SameSite=Strict` 的会话 Cookie 并重定向到 `/`。
- 响应统一带安全头：`Content-Security-Policy`（严禁外链与内联脚本）、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。

---

## 全局状态与模型 / 引擎

### `GET /api/state`
返回应用实时快照：
- `version` (str)
- `workspace` (str | null)
- `recent` (list[str])
- `hardware`: `HwInfo`（`cpu_cores`、`cpu_threads`、`ram_gb`、`ram_free_gb`、`gpus`、`gpu_backend` 等）
- `engine`:
  - `status`: `"stopped" | "starting" | "ready" | "stopping" | "error"`
  - `model` (str | null), `mmproj` (str | null), `ctx` (int), `ctx_max` (int), `vision` (bool)
  - `binary` (str | null), `binary_version` (str | null), `error` (str | null)
  - `load_log_tail` (list[str])
  - `tps` (float)
  - `perf`: `{"prompt_tps": float, "eval_tps": float, "tps": float, "spec_accept_rate": float | null, "cache_hit_rate": float, "total_prompt_tokens": int, "total_eval_tokens": int, "requests": int}`
  - `plan` (dict | null): 当前已应用的推理规划（含 `summary`、`command`、`spec`、`kv_budget`、`moe_cache_mib`、`threads_batch`、`cache_reuse`、`mlock`、`notes`）
  - `profile` (dict | null): 当前模型自动调优档案概要
- `config` (dict): 完整配置字典（`workspace`、`engine`、`agent`、`permissions`、`search`、`web`、`mcp`、`ui`）
- `mcp` (list): MCP 服务器连接与工具数列表
- `has_model` (bool), `has_binary` (bool)

### `GET /api/models`
扫描 `models/` 下的 `.gguf` 文件并返回：
- `models`: `ModelInfo[]`，关键字段含 `file`, `name`, `arch`, `quant`, `size_gb`, `moe`, `experts`, `experts_used`, `n_layers`, `embd`, `is_draft`, `draft_model`, `compatible_drafts`, `mmproj`, `vision`, `has_chat_template`, `error`
- `mmprojs`: 独立多模态投影权重列表

### `POST /api/models/rescan`
刷新 GGUF 扫描缓存，返回同 `GET /api/models`。

### `POST /api/engine/plan`
预览某模型与参数组合将生成的启动规划（不启动进程）。  
请求体（可选字段）：`{"model": str, "mmproj": str | null, "ctx": int | "auto", ...}`  
返回：`{"plan": dict, "command": str}`。

### `POST /api/engine/start` / `POST /api/engine/restart` / `POST /api/engine/stop`
控制 `llama-server` 进程生命周期；参数同 `/api/engine/plan`。

---

## 配置、工作区、命令、权限与钩子

### `GET /api/config` / `PATCH /api/config`
读取或局部更新 `config.json`（含类型与范围校验、`RANGES` 与 `SPECIAL` 枚举校验）。  
`PATCH` 返回 `{"config": dict, "restart_needed": bool}`。

### `GET /api/commands?workspace=...&session_id=...`
返回当前生效的斜杠命令列表（内置 15 个 Claude Code 标准命令 + 工作区 `.localagent/commands/*.md` 与 `.claude/commands/*.md` 自定义命令）：  
`{"commands": [{"name": str, "description": str, "kind": "builtin" | "custom", ...}]}`

### `GET /api/permissions?workspace=...`
返回当前生效的细粒度权限规则清单：  
`{"rules": [{"action": "allow" | "ask" | "deny", "tool": str, "pattern": str, ...}]}`

### `GET /api/hooks?workspace=...`
返回已配置的生命周期钩子（`PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit`）：  
`{"hooks": [...]}`

### `POST /api/workspace` / `GET /api/browse` / `GET /api/files` / `GET /api/file`
- `POST /api/workspace`: 绑定工作区路径（校验 `PathGuard` 与目录存在性）
- `GET /api/browse`: 本地目录浏览（用于工作区选择器）
- `GET /api/files`: 递归列出当前工作区安全文件（用于抽屉与 `@` 补全）
- `GET /api/file`: 读取工作区内受限文本文件预览

---

## 会话、运行、撤销与实时事件

### `GET /api/sessions` / `POST /api/sessions` / `GET /api/sessions/{sid}` / `DELETE /api/sessions/{sid}`
管理持久化的对话会话与历史消息、工作区绑定及审计变更记录（`changes`）。

### `POST /api/sessions/{sid}/undo`
回退上一回合（Turn）在工作区中产生的文件新建、修改与删除（基于 `Journal` 原子快照）。  
请求体（可选）：`{"steps": int}`（默认 1 个回合）。  
若会话正在运行（`active_run`），返回 `409 Conflict`。

### `POST /api/sessions/{sid}/messages`
提交用户消息或斜杠命令并启动一个 Agent Turn：  
请求体：`{"text": str, "attachments": list, "skills": list}`  
返回：`{"run_id": str}`（或命令立即结果）。

### `POST /api/sessions/{sid}/compact`
手动触发当前会话上下文压缩。

### `POST /api/runs/{rid}/cancel`
取消正在运行的推理回合并向客户端发送终止状态。

### `GET /api/runs/{rid}/events`（SSE）
单次运行实时事件流，常见事件类型：
- `thinking_delta` / `thinking_end`
- `text_delta`
- `tool_start` / `tool_end`
- `approval_request`（需前端回传 `POST /api/approvals/{aid}`）
- `usage`: `{"prompt_tokens": int, "completion_tokens": int, "ctx": int, "used": int, "pct": float, "tps": float}`
- `run_end`: `{"status": "done" | "cancelled" | "error", "stats": dict, ...}`

### `POST /api/approvals/{aid}`
提交敏感工具/命令审批结果：`{"decision": "allow" | "allow_turn" | "deny"}`。

### `GET /api/events`（全局 SSE）
推送跨页面的状态更新事件：`engine`、`config`、`mcp`、`workspace`、`notice`、`sessions`。

---

## 技能、记忆、MCP 与应用控制

- **技能 (`/api/skills`, `/api/skills/{name}`)**: 列出、创建、启停、删除本地技能包（`skills/` 及内置技能）。
- **记忆 (`/api/memory`, `/api/memory/{mid}`)**: 检索、新增与删除 SQLite 持久化长期记忆。
- **MCP (`/api/mcp`, `/api/mcp/test`)**: 增删改查、启停并测试 stdio / HTTP MCP 服务器。
- **应用退出 (`POST /api/app/shutdown`)**: 安全停止 `llama-server` 并退出本地服务进程。
