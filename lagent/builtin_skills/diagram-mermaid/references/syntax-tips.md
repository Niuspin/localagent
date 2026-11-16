# Mermaid 常见避坑清单

1. 含括号/引号/中文符号的节点文本：`node_id["文本 (补充说明)"]` 而不是 `node_id[文本 (补充说明)]`。
2. 时序图参与者含空格/连字符时先声明别名：`participant GW as "API Gateway"`。
3. subgraph 标题也建议带引号：`subgraph sub_exec["执行层"]`。
4. 避免使用实验性或甘特图在精简渲染器里的非标扩展属性。
