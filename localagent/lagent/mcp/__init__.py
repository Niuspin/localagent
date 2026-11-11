"""MCP（Model Context Protocol）客户端：只支持本机 stdio 服务器，默认放进 OS 沙箱运行。

分层（自下而上，每层只依赖下一层）：
  rpc.py      JSON-RPC 2.0 over stdio：按行收发、请求/响应配对、超时与取消、stderr 滚动缓冲。不懂 MCP 语义。
  convert.py  MCP ⇄ 本项目格式：工具名、JSON Schema 精简、调用结果 → ToolResult、描述清洗与投毒审查。纯函数。
  store.py    服务器配置的校验与持久化（data/mcp.json，0600，Agent 不可见）。
  launch.py   把配置变成真正的启动命令：解析可执行文件、环境清洗、沙箱包装，沙箱不可用时拒绝启动（fail closed）。
  session.py  一个服务器进程的生命周期：握手、列工具、调用、通知、停止。
  manager.py  所有服务器的统一入口：按需启动/空闲回收、工具缓存、把工具桥接进现有的工具注册表与权限体系。
"""
from .manager import McpManager

__all__ = ["McpManager"]
