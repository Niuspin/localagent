# LocalAgent

本地 GGUF 模型 + llama.cpp 的**安全、简洁**的 Agent 框架。浏览器里的界面参照 Claude 的交互：对话、工具卡片、审批卡片、技能、记忆、工作区面板。
全部数据留在本机；界面不加载任何外部资源（无 CDN、无字体、无统计），可完全离线运行。

## 快速开始

1. 安装 **Python 3.10+**。
2. 把 llama.cpp 发行包（含 `llama-server`）解压到 `bin/`（任意子目录均可被找到），或确保 `llama-server` 在 PATH 中。
3. 把 `.gguf` 模型放进 `models/`。多模态模型请把配套的 `mmproj-*.gguf` 放在同一目录（会自动配对）。
4. 运行：
   - Linux / macOS：`./start.sh`
   - Windows：双击 `start.bat`

   首次运行会创建 `.venv` 并安装依赖。终端会打印 `http://127.0.0.1:8765/?t=<令牌>`，浏览器自动打开；令牌每次启动都会变。
5. 在界面里 **选择工作区 → 加载模型**，开始对话。

命令行参数：`python run.py [--port 8765] [--workspace DIR] [--no-browser] [--load]`（`--load` 启动后直接加载上次的模型）。

## 目录

```
models/          你的 .gguf 与 mmproj（只读，Agent 无法修改）
bin/             llama.cpp 可执行文件（只读）
skills/          你安装/创建的技能（Agent 只能通过 create_skill 工具写，需审批）
data/            会话、记忆库(SQLite)、审计日志、回收站、密钥——对 Agent 完全不可见
config.json      设置（界面里改即可）
lagent/          后端代码      web/  前端静态文件      docs/API.md  前后端接口契约
```

## 功能

| 能力 | 说明 |
|---|---|
| **文件读写** | 在工作区内读取/创建/编辑/移动/复制/删除文件；编辑保留原文件编码与换行；每次修改前出 diff 供确认；所有变更记录在日志里，**一键撤销**；删除先进回收站 |
| **多格式** | 文本/代码直接读写；`.docx .xlsx .pptx .pdf` 可读，Word/Excel/PowerPoint/PDF 可创建，docx/xlsx/pptx 可原地修改；图片可读（多模态模型能看到内容） |
| **命令** | `run_command`：超时、输出截断、清洗环境变量、可选系统沙箱；危险命令直接拒绝，只读命令白名单免确认。`background=true` 可后台运行（开发服务器、监视测试），用 `job_output` / `job_stop` 查看与终止；后台命令走同一套沙箱、黑名单与审批 |
| **联网搜索** | DuckDuckGo、Bing、百度、Yahoo（免密钥，解析网页）；Brave、Google CSE、Tavily、Serper（API）；SearXNG（自建）。可多引擎并行并用 RRF 融合，支持 HTTP 代理 |
| **网页抓取** | `web_fetch` 提取正文；防 SSRF（拒绝内网/回环/元数据地址，DNS 固定防重绑定，重定向逐跳检查） |
| **记忆库** | SQLite 外置长期记忆（FTS5 全文索引 + 中文分词打分，数千条也是亚毫秒级；SQLite 不带 FTS5 时自动回退）；提问前自动召回；`memory_save/search/forget` 工具；界面里可增删改、置顶、导入导出 |
| **技能** | 兼容 `SKILL.md` 目录格式（frontmatter + 正文，可带脚本/资料）；模型先看到名称+简介，需要时再读全文；输入 `/` 手动指定；支持 zip/文件夹安装。自带 4 个：深度调研、报告撰写、数据分析、整理文件夹 |
| **上下文自调** | 见下 |
| **多模态** | 图片粘贴/拖入，自动缩放；mmproj 自动配对，并按显存决定是否放 GPU |
| **项目指令** | 自动读取工作区根目录的 `AGENTS.md`（没有则 `CLAUDE.md`）写进系统提示：构建/测试命令、代码风格写一次，每个会话都生效。只读普通文件、最多 12000 字符；含“忽略先前指令”“下载即执行”“隐藏字符”等可疑内容时不加载并提示；加载时有提示并记入审计日志；`agent.project_instructions=false` 可关闭 |
| **先读再改** | 改/覆盖已有文本文件前，本会话必须读过它，且读过之后没被别人改动（否则不弹审批，直接让模型重读）。`edit_file` 还能容忍缩进抄错（唯一匹配时按文件原缩进对齐）。`agent.require_read_before_edit=false` 可关闭 |
| **任务清单 / 提问** | 多步任务用 `todo_write` 展示进度；信息不足时 `ask_user` 弹出选项卡 |

### MoE 与性能

- 读取 GGUF 元数据，**精确到每层专家权重的字节数**，据此规划“注意力放显存、专家留内存”的拆分；默认交给 llama.cpp 的 `--fit`，也可手动指定 `gpu_layers` / `n_cpu_moe`。
- 启动参数按 `--help` **运行时探测**（如不支持 `--n-cpu-moe` 则回退到 `-ot` 正则），不同版本的 llama.cpp 都能用。
- 单槽位（`-np 1`）以最大化前缀缓存命中：系统提示保持稳定，记忆与技能内容放进最新一条用户消息，自动标题用规则生成（不额外调用模型，避免挤掉 KV 缓存）。
- 设置里可预估显存/内存占用并查看实际启动命令。

### 为本地小模型减负

- **工具分级**：每轮只带约 15 个常用工具（定义约 2.3K token，比全量少四成）；Office 文档、删除/移动/复制、技能与记忆管理、后台任务这些低频工具按需加载——用户消息里出现相关词、模型调用 `load_tools`、或直接调用它们时自动加入。已加载的组在会话内只增不减，保证系统提示与工具列表稳定，不破坏 llama.cpp 的前缀缓存。
- **只读并发**：同一步里连续的只读调用（`read_file` / `grep` / `web_search` / `web_fetch` …）并发执行，写入、删除、命令仍严格串行并逐个审批。`agent.parallel_readonly=false` 可关闭。
- **工具调用容错**：小模型常把工具名写成 `Read_File`、参数写成 `filePath`；这些会被自动纠正（歧义时宁可报错也不乱猜，绝不把 `memory_s` 猜成 `memory_save` 这类有副作用的工具），JSON 损坏会修复，未知工具会列出当前可用工具。整条回复明显是一次“没写对格式的工具调用”时，会用 `tool_choice: required` 强制重试一次（不污染历史；服务器不支持则自动退回）。
- **摩擦指标**：每次运行结束的 `run_end.stats` 与审计日志 `run_stats` 记录 `name_fixed / args_fixed / json_repaired / unknown_tool / arg_errors / repeat_calls / forced_retries` 等计数，用来比较不同模型、不同提示词的“省心程度”。
- **记忆提速**：规范化文本建索引，写入去重 O(1)（3000 条写入 16.1s → 1.2s）；FTS5 候选召回让大库检索约 0.2ms。
- 这些开关都在 `config.json` 的 `agent` 段里。

### 扩展：MCP 服务器（仅本机 stdio）

设置 → 扩展里添加服务器（名称 + 启动命令）。设计原则是**界面只有一行一个服务器，安全由后端兜底**：

- **默认进沙箱**：只能写自己的私有目录 `data/mcp/<名称>/`（也是它的 HOME），看不到你的主目录与密钥，默认断网；环境变量走清洗，不继承你的 API Key。沙箱不可用时**拒绝启动**，而不是悄悄裸跑。
- **按服务器放宽**：`sandbox`（auto/off）、`network`、`workspace`（把当前工作区挂进去）、`readonly_paths`（额外只读目录，不能是主目录整体或数据目录）、`trust_results`、`timeout_s`、`idle_s`，每个服务器单独设置。
- **按不可信处理**：工具描述会清洗并做注入审查（命中则丢弃该工具）；返回内容用 `<untrusted>` 包裹并污染会话（除非该服务器设为信任）。调用默认逐次确认；自动模式仅在“沙箱生效且不联网”时免确认；规划模式不可用。
- **省上下文**：工具作为 `mcp:<名称>` 组按需加载（用户消息里提到服务器名或 `load_tools`），首次连接后缓存工具清单；空闲自动回收进程。
- 配置在 `data/mcp.json`（权限 0600，Agent 不可见），环境变量的值不会回显到界面或接口。

### 上下文自调

每次请求前用服务器返回的真实 `prompt_tokens` 校准估算，不够用时依次：
1. **裁剪**旧的工具输出；
2. **自动加倍上下文长度**（重启引擎，上限 `ctx_max` 与可用内存共同限制）；
3. **压缩**较早对话为摘要；
4. 最后才硬截断。

遇到服务器报“超出上下文”也会据 `n_prompt_tokens` 校准后重试。顶栏圆环显示用量，可手动“压缩”。

## 安全模型

目标是：**模型被网页里的恶意指令骗了，也做不成大事。**

| 层 | 机制 |
|---|---|
| 网络面 | 仅监听 `127.0.0.1`；随机令牌 → HttpOnly + SameSite=Strict Cookie；校验 `Host` / `Origin`（防 DNS 重绑定与跨站请求）；严格 CSP（`script-src 'self'`，禁止内联脚本）与其它安全头；llama-server 使用随机端口 + 随机 API Key，且 `--host` 等参数不允许被覆盖 |
| 路径 | 所有文件操作经 `PathGuard`：realpath、符号链接、`..`、Windows 保留名/备用数据流检查；工作区外一律拒绝；`models/ bin/ skills/ config.json` 及程序代码只读；`data/` 完全隐藏；`.env`、密钥、`.git` 等敏感文件强制确认 |
| 权限模式 | `逐项确认`（默认）/ `自动编辑` / `只读规划` / `全自动`。**任何模式下**敏感文件与不可信上下文都不会被静默放行 |
| 污染追踪 | 读过网页、搜索结果、Office/PDF、上传文件后，本会话被标记为“不可信上下文”：写入、删除、命令、非白名单抓取都强制确认；此时 Agent 写入的记忆需你批准才生效。读到的内容用带随机 nonce 的 `<untrusted>` 包裹并剔除不可见字符 |
| 命令 | 硬拒绝清单（`rm -rf /`、磁盘/引导操作、`curl … \| sh` 等）；联网命令默认关闭；环境变量清洗（不会把你的 API Key 暴露给子进程）；Linux 有 bwrap、macOS 有 sandbox-exec 时自动套沙箱 |
| 可恢复 | 修改前快照、删除进回收站、日志可撤销（检测到之后被改过会提示冲突） |
| 审计 | `data/audit.jsonl` 为 SHA-256 哈希链，界面里可一键校验是否被篡改 |
| 技能 | zip 解压防穿越与符号链接；新安装的技能**默认不启用**；扫描可疑内容并提示 |
| 密钥 | 单独存放 `data/secrets.json`（0600），接口不回显 |

> **请注意这些边界**：
> - 没有可用沙箱时（如 Windows，或 Linux 未装 bubblewrap），命令只靠“确认 + 黑名单”保护。请保持“逐项确认”，不要放行看不懂的命令。
> - “全自动”模式意味着你信任模型和它读到的一切，仅建议在一次性目录里用。
> - 提示注入没有银弹。污染追踪与确认机制能显著降低风险，但最后一道防线是你看审批卡片。

## 搜索引擎说明

免密钥的 DuckDuckGo / Bing / 百度 / Yahoo 是**解析搜索结果网页**实现的，对方改版或触发风控时会失效或返回验证码页。
需要稳定性时，推荐配 **SearXNG（自建）** 或 **Brave / Tavily / Serper** 等 API。访问 Google/DuckDuckGo 需要代理时，在 设置→搜索 里填 HTTP 代理。

## 技能格式

```
skills/my-skill/
  SKILL.md        ← frontmatter: name、description；正文是做法
  scripts/ …      ← 可选，Agent 要运行时仍需你逐次审批
```

可在界面“技能”里安装 zip / 文件夹，或新建；也可直接让 Agent 用 `create_skill` 帮你起草（需审批）。

## 测试

```
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 单元 + 端到端（假 llama-server、mock MCP 服务器）
python tests/ui_smoke.py [截图目录]   # 可选：Chromium 驱动完整界面（需 playwright install chromium）
python tests/eval/run_eval.py --repeat 3   # 真实模型评测：通过率 + 摩擦指标（没有模型时自动跳过）
```

GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.10 / 3.12 上跑上面前两项。

## 已知限制（如实说明）

- 开发环境里**没有真实的 llama.cpp 与 GPU**：引擎路径用与 llama-server 行为对齐的假服务器测试。参数探测、`--fit`、MoE 拆分、mmproj 等在真实环境首次运行请观察“设置→日志”，必要时用“额外参数”微调。
- **Windows / macOS** 的进程管理（Job Object）、沙箱（sandbox-exec）与路径规则按文档实现，但未在真机验证。
- 免密钥搜索引擎的 HTML 解析仅用样本页面测试过，见上文。
- **MCP 沙箱**：开发环境没有 bubblewrap，沙箱参数（只读/可写挂载、断网、HOME）只做了结构验证，“不可用即拒绝启动”已实测；请在装有 bubblewrap 的机器上实际跑一次。只支持本机 stdio 服务器（无 HTTP/OAuth），不支持服务器发起的采样/请求。
- 单用户、单槽位设计；同一时刻一个会话在跑。不做多用户权限。
- 本地小模型的工具调用可靠性不如云端大模型：已做 JSON 修复、`<tool_call>` 文本回退、重复调用检测，但复杂任务仍请留意审批卡片与结果。

## 许可证

[MIT](../LICENSE) © 2026 Niuspin
