# 本地 Agent 与 Web/API 核心安全核查表

- **Path Traversal / Symlink**：检查相对路径是否经 `realpath` 解析并校验位于允许根目录内；Windows 校验 UNC/ADS/保留设备名。
- **Command Injection**：严禁对不可信输入使用 `shell=True`；参数列表传递并经工作区限制。
- **XSS & CSP**：前端动态插入文本优先 `textContent`，禁止拼装未转义 HTML；保持零内联脚本。
- **Prompt Injection**：外部网页/文档输出必须隔离标记且高危工具写操作需二次确认或阻断。
- **Least Privilege**：工具/技能白名单 `allowed_tools` 只允许取交集收窄，禁止逆向放宽。
