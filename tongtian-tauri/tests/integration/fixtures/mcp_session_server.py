"""MCP 常驻连接自测夹具（D-40 第 5 项）

比 `mcp_echo_server.py` 多两件事，用来把「长连接真的保住了」说实：

1. 进程痕迹：启动与退出各写一行到 `TT_MCP_TRACE_FILE`（含 pid）。收尾没发生在
   进入它的那个 asyncio 任务里时，子进程会留在地上——痕迹文件里没有 exit 行就是证据。
2. 会话状态：`STATE` 是**进程内**的列表，跨轮不清零才说明保住的是一条连接而不是
   每轮重开一个干净进程。

刻意用 MCP 低层 Server API 而不是 FastMCP：FastMCP 的 Settings 会读当前工作目录的
.env，本仓库的 .env 不是 UTF-8，会让服务器在导入期崩掉，把自测失败伪装成「连接器
注册失败」。
"""

import asyncio
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

SERVER_NAME = "tongtian-session-selftest"

# 进程内状态：只有跨轮复用同一条连接时才会累积
STATE: list[str] = []

TOOLS = [
    Tool(
        name="whoami",
        description="回报服务器进程号与当前状态条数，用于证明连接是否真的复用",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="note_add",
        description="往服务器进程内存追加一条状态并回报总条数",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ),
    Tool(
        name="note_count",
        description="只读回报服务器进程内存里的状态条数",
        inputSchema={"type": "object", "properties": {}},
    ),
]

server: Server = Server(SERVER_NAME)


def _trace_path() -> str:
    return str(os.environ.get("TT_MCP_TRACE_FILE") or "").strip()


def _trace(kind: str) -> None:
    path = _trace_path()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{kind} pid={os.getpid()} notes={len(STATE)}\n")
    except OSError:
        # 痕迹写不下去只影响自测取证，不该让服务器起不来
        pass


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "whoami":
        return [TextContent(type="text", text=f"pid={os.getpid()} notes={len(STATE)}")]
    if name == "note_add":
        STATE.append(str(arguments.get("text", "")))
        return [TextContent(type="text", text=f"notes={len(STATE)}")]
    if name == "note_count":
        return [TextContent(type="text", text=f"notes={len(STATE)}")]
    raise ValueError(f"未知工具：{name}")


async def main() -> None:
    _trace("spawn")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        # stdin 关闭 → run() 返回 → 这里落一行 exit：进程真的走完了收尾
        _trace("exit")


if __name__ == "__main__":
    asyncio.run(main())
