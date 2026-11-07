# LocalAgent HTTP API（前后端契约）

所有接口均在 `http://127.0.0.1:<port>`，JSON 编码（除特别说明）。
前端为纯静态文件（`web/`），**不得引用任何外部 CDN/字体/图片**，必须可完全离线运行。

## 鉴权与加固
- 启动时终端打印 `http://127.0.0.1:PORT/?t=<TOKEN>`。首次访问 `/?t=TOKEN` 会设置 HttpOnly+SameSite=Strict 的 `la_token` Cookie 并 302 到 `/`。
- 之后所有 `/api/*` 通过该 Cookie 鉴权（前端无需自己处理 token）。401 → 前端应显示“会话已失效，请从终端打印的链接重新打开”。
- 服务端校验 `Host` 与 `Origin`（防 DNS rebinding / CSRF），并下发严格 CSP：`script-src 'self'`，**禁止内联脚本**（`<script>` 必须是外部文件；允许 `style-src 'unsafe-inline'`）；`img-src 'self' data: blob:`。

## 状态

### GET /api/state
```json
{
  "version": "0.1.0",
  "workspace": "D:/work/proj" ,
  "engine": {
    "status": "stopped|starting|ready|error|stopping",
    "model": "Qwen3-30B-A3B-Q4_K_M.gguf", "mmproj": "mmproj-F16.gguf|null",
    "ctx": 32768, "ctx_max": 131072, "vision": true, "error": null,
    "binary": "/path/llama-server|null", "binary_version": "b6xxx|null",
    "load_log_tail": ["..."], "plan": {"summary":"…","n_cpu_moe":12,"ngl":"all","kv_mb":1800,"notes":["…"]},
    "tps": 42.1
  },
  "models": [{"file":"x.gguf","size_gb":17.3,"arch":"qwen3moe","name":"…","moe":true,"experts":128,"experts_used":8,
              "ctx_train":262144,"quant":"Q4_K_M","mmproj":"mmproj-x.gguf|null","vision":true}],
  "mmprojs": ["mmproj-x.gguf"],
  "hw": {"os":"windows","cpu_threads":16,"ram_gb":64,"ram_free_gb":40,"gpus":[{"name":"RTX 4090","vram_gb":24,"free_gb":22.5}]},
  "sandbox": {"available": true, "kind": "bwrap|sandbox-exec|none", "note": "…"},
  "permission_mode": "ask|auto_edit|plan|auto",
  "tainted": false
}
```
`tainted`：当前上下文是否读入过不可信内容（见安全设计）。会话相关的 `tainted` 也在 `GET /api/sessions/{id}` 中。

### GET /api/config  →  `{"config": {...}}`；PATCH /api/config `{...部分配置(深合并)}` → `{"config": {...}}`
密钥不回显：`search.keys` 返回 `{"brave": true|false, ...}`（是否已设置）；PATCH 时 `search.keys.brave="xxx"` 设置，`""` 清除，缺省不变。
```json
{
 "workspace": "",
 "engine": {"binary":"","model":"","mmproj":"auto","ctx":"auto","ctx_start":32768,"ctx_max":131072,"ctx_auto_grow":true,
            "gpu_layers":"auto","n_cpu_moe":"auto","mmproj_offload":"auto","flash_attn":"auto","kv_cache_type":"auto",
            "threads":0,"batch":2048,"ubatch":512,"image_max_tokens":1024,"cache_ram_mb":4096,"vram_reserve_mb":1024,"extra_args":[]},
 "generation": {"thinking":"auto","temperature":0.6,"top_p":0.95,"top_k":20,"min_p":0.0,"max_tokens":8192,"presence_penalty":0.0},
 "agent": {"max_steps":40,"permission_mode":"ask","system_prompt_extra":"","auto_title":true,"memory_auto_recall":true,"memory_recall_k":6},
 "security": {"allow_network_commands":false,"command_sandbox":"auto","command_timeout_s":120,"max_file_mb":20,
              "trash_retention_days":14,"block_remote_images":true,"taint_tracking":true},
 "search": {"providers":["duckduckgo","bing"],"mode":"merge|first","max_results":8,"proxy":"","searxng_url":"","region":"","safe":"moderate",
            "keys":{"brave":false,"google_cse":false,"google_cx":false,"tavily":false,"serper":false}},
 "web": {"timeout_s":20,"max_bytes":3000000,"max_chars":12000},
 "ui": {"theme":"auto|light|dark","font_size":15}
}
```
枚举说明：`ctx` = `"auto"` 或整数；`gpu_layers`/`n_cpu_moe`/`mmproj_offload` = `"auto"` 或整数/布尔；`flash_attn` = `auto|on|off`；`kv_cache_type` = `auto|f16|q8_0|q4_0`；
`thinking` = `auto|on|off`；`command_sandbox` = `auto|off`；可用搜索引擎 id：`duckduckgo, bing, baidu, yahoo, brave, google_cse, searxng, tavily, serper`（brave/google_cse/tavily/serper 需密钥，searxng 需 `searxng_url`）。

## 引擎
- `POST /api/engine/start`  `{model?: "x.gguf", mmproj?: "auto"|"none"|"file.gguf", ctx?: "auto"|int}` → state.engine（立即返回，status=starting，轮询 `/api/state`；或订阅 `/api/events`）
- `POST /api/engine/stop` ；`POST /api/engine/restart`（同 start 的可选覆盖）
- `POST /api/engine/plan` `{model, mmproj?, ctx?}` → `{"plan": {...}, "command": "llama-server -m … (不含密钥)"}`（不启动，仅预估显存/内存与启动参数）
- `GET /api/engine/logs?n=200` → `{"lines": ["…"]}`
- `GET /api/events`：SSE（全局事件，无需 id）。事件：`engine`（payload=state.engine）、`notice`。

## 会话
- `GET /api/sessions` → `[{id,title,created,updated,workspace,msg_count,pinned}]`（按 updated 降序）
- `POST /api/sessions` `{workspace?}` → session
- `GET /api/sessions/{id}` →
```json
{"id":"…","title":"新对话","workspace":"/abs","created":1730000000,"updated":1730000100,"pinned":false,"tainted":false,
 "summary": null,
 "context": {"ctx":32768,"used":5120,"pct":15.6,"summarized":false},
 "todos": [{"text":"…","status":"pending|in_progress|done"}],
 "active_run": null,
 "messages": [
  {"id":"m1","role":"user","ts":1730000000,"text":"…","images":["data:image/jpeg;base64,…(缩略图)"],"files":["uploads/a.pdf"],"skills":["deep-research"]},
  {"id":"m2","role":"assistant","ts":1730000001,"status":"done|cancelled|error","error":null,
   "usage":{"prompt_tokens":1200,"completion_tokens":300,"tps":38.5},
   "blocks":[
     {"type":"thinking","text":"…"},
     {"type":"text","text":"markdown"},
     {"type":"tool","id":"call_1","name":"read_file","title":"读取 src/main.py","args":{"path":"src/main.py"},
      "status":"running|pending_approval|ok|error|denied","content":"结果预览(≤4000字符)","meta":{"undo_id":"j123","path":"…"},
      "duration_ms":120,"approval":null}
   ]}
 ]}
```
  `active_run` 非空（run_id 字符串）表示该会话仍在运行，前端应重新订阅其事件流。
  同一用户回合内的多轮 assistant/tool 消息合并为一条 UI assistant 消息（多个 blocks）。
- `PATCH /api/sessions/{id}` `{title?, workspace?, pinned?}`；`DELETE /api/sessions/{id}`
- `POST /api/sessions/{id}/messages` `{text, images?: ["data:image/png;base64,…"], files?: ["uploads/a.pdf"], skills?: ["name"]}` → `{"run_id": "…"}`（409：该会话已有运行中的 run）
- `POST /api/sessions/{id}/upload`（multipart, 字段 `file`）→ `{"path":"uploads/a.pdf","size":123}`（保存到工作区 `uploads/`）
- `POST /api/sessions/{id}/compact` → `{"summary":"…"}` 手动压缩上下文
- `POST /api/sessions/{id}/truncate` `{from_index: int}` 删除第 from_index 条及之后的 UI 消息（用于“编辑重发/重新生成”）→ session
- `POST /api/sessions/{id}/untaint` 用户确认信任当前上下文 → `{"tainted":false}`

## 运行事件流
- `GET /api/runs/{run_id}/events` — SSE。每条：`id: <递增整数>`、`event: <type>`、`data: <json>`。支持 `Last-Event-ID` 头与 `?after=<id>` 重放。run 结束后发送 `run_end` 并关闭。
- `POST /api/runs/{run_id}/cancel` → `{"ok":true}`
- `POST /api/approvals/{approval_id}` `{"decision":"allow|allow_session|deny","note?":"…"}` → `{"ok":true}`
- `POST /api/questions/{question_id}` `{"answer":"…"}` → `{"ok":true}`

事件类型与负载：
| event | data |
|---|---|
| `run_start` | `{run_id, session_id}` |
| `step_start` | `{step}` |
| `thinking_delta` | `{text}` |
| `text_delta` | `{text}` |
| `tool_start` | `{id, name, title, args, risk:"low\|medium\|high"}` |
| `approval_request` | `{approval_id, tool_id, name, title, args, risk, reason, tainted, preview:{kind:"diff\|command\|text", text}}` |
| `approval_resolved` | `{approval_id, tool_id, decision}` |
| `question` | `{question_id, question, options:["…"]}`（`options` 可为空 → 自由文本）|
| `tool_result` | `{id, name, status:"ok\|error\|denied", content, meta:{…}, duration_ms}` |
| `todo_update` | `{items:[{text,status}]}` |
| `usage` | `{prompt_tokens, completion_tokens, ctx, used, pct, tps}` |
| `context_event` | `{kind:"pruned\|compacted\|grow_start\|grow_done\|trimmed", detail}` |
| `notice` | `{level:"info\|warn", text}` |
| `taint` | `{tainted:true, reason}` |
| `assistant_done` | `{finish_reason}` |
| `run_end` | `{status:"done\|cancelled\|error\|max_steps", error?}` |

渲染要点：`thinking_delta` 追加到当前 thinking 块（默认折叠，流式时展开）；`text_delta` 追加到当前 text 块；每个 `tool_start` 新建 tool 块；`approval_request` 在对应 tool 块下方显示审批卡片（`preview.kind=diff` 用红绿 diff 渲染；`command` 用等宽框；显示 `reason`、`tainted` 警示）；`tool_result` 更新 tool 块状态；`context_event` 用小号居中提示行显示。

## 工作区
- `GET /api/workspace` → `{path, exists, journal_count, trash_count}`；`PUT /api/workspace` `{path, session_id?}`（校验：存在、是目录、不是盘符根/用户主目录根；返回 400 + `{"detail":"原因"}`）
- `GET /api/fs/browse?path=` → `{path, parent, dirs:[name…], sep}`（仅目录；用于选择工作区；path 为空时列出盘符/主目录）
- `GET /api/workspace/tree?path=rel&session_id=` → `{path, entries:[{name,type:"dir|file",size,mtime}]}`
- `GET /api/workspace/file?path=rel&session_id=` → `{kind:"text|image|office|binary", content?, data_url?, truncated, size, mime}`（只读预览，永不以 HTML 形式渲染）
- `GET /api/journal?session_id=` → `[{id,ts,op,path,summary,undone,can_undo}]`；`POST /api/journal/{id}/undo` `{force?:false}` → `{"ok":true}` 或 409 `{"detail":"conflict: 文件已被修改"}`
- `GET /api/trash` → `[{id,ts,path,size,is_dir}]`；`POST /api/trash/{id}/restore`；`DELETE /api/trash`（清空）

## 技能（Skills，兼容 SKILL.md 目录格式）
- `GET /api/skills` → `[{name,description,enabled,source:"builtin|user",warnings:["…"],files:["SKILL.md","scripts/x.py"],size}]`
- `GET /api/skills/{name}` → 上述字段 + `body`（SKILL.md 正文）
- `POST /api/skills/{name}/enable` `{enabled}`；`DELETE /api/skills/{name}`（仅 user）
- `POST /api/skills/install`：multipart `file`（zip，含单个技能目录或根目录 SKILL.md）或 JSON `{path:"/abs/dir"}` → skill
- `POST /api/skills/create` `{name, description, body}` → skill；`PUT /api/skills/{name}` `{description?, body?}`
- 聊天输入框中输入 `/` 应弹出技能选择；选中后随消息的 `skills:[name]` 发送。

## 记忆库
- `GET /api/memory?q=&kind=&scope=&pending=0|1` → `{items:[{id,text,kind:"fact|preference|project|episode",tags:["…"],scope:"global|workspace",source:"user|agent",trusted,pinned,created,updated,uses}], total}`
- `POST /api/memory` `{text,kind?,tags?,pinned?,scope?}`；`PATCH /api/memory/{id}`；`DELETE /api/memory/{id}`；`POST /api/memory/{id}/approve`（把 agent 在受污染上下文中写入的待审记忆标记为可信）
- `GET /api/memory/export` → JSON 文件；`POST /api/memory/import`（JSON body `{items:[…]}`）

## 其他
- `POST /api/search/test` `{query, provider?}` → `{results:[{title,url,snippet,engine}], errors:{provider:"msg"}}`
- `GET /api/audit?n=200` → `{entries:[…]}`；`GET /api/audit/verify` → `{"ok":true,"count":123}`（哈希链校验）
