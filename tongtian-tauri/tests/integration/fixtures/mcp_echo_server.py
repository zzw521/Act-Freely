"""本地 MCP echo 服务器（自测夹具）

只用于验证「连接器的工具真的能注册进 Toolkit 并被调用」，不联网、不需要外部进程。

刻意使用 MCP 低层 Server API 而不是 FastMCP：FastMCP 的 Settings 会读取**当前工作
目录**的 .env，本仓库的 .env 不是 UTF-8，会让服务器在导入期就崩掉，把自测失败
原因伪装成「连接器的工具注册失败」。
"""

import asyncio
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

SERVER_NAME = "tongtian-selftest"

TOOLS = [
    Tool(
        name="echo",
        description="把输入原样返回，用于确认工具调用真的往返了一次",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ),
    Tool(
        name="now_word",
        description="无入参工具，用于确认空 schema 也能注册与调用",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="env_probe",
        description="回读服务器进程的环境变量，用于确认下发的 env 真的注入子进程",
        inputSchema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    ),
]

server: Server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "echo":
        return [TextContent(type="text", text=f"echo:{arguments.get('text', '')}")]
    if name == "now_word":
        return [TextContent(type="text", text="已执行")]
    if name == "env_probe":
        key = str(arguments.get("key", ""))
        return [TextContent(type="text", text=f"{key}={os.environ.get(key, '缺失')}")]
    raise ValueError(f"未知工具：{name}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
