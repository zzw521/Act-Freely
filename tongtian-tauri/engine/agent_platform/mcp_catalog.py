"""MCP 层：Catalog 注册表 + 客户端池 + 工具发现/代理调用。

只负责连接与调用，不处理对话。stdio / HTTP 两种传输统一在 Catalog 里声明，
客户端构造复用 `engine/mcp_runtime.build_client`（沙箱包装、超时、env 口径与存量一致）。

Catalog 文件（`{platform home}/mcp_catalog.yaml`，缺省用内置示例）：

    servers:
      - id: filesystem
        name: 本地文件系统
        transport: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        env: {}
        enabled: true
      - id: remote-search
        name: 远程搜索
        transport: http
        url: http://127.0.0.1:9000/mcp
        headers: {}
        timeoutSecs: 30

生命周期：stdio 子进程必须在创建它的 asyncio 任务里关闭，所以池按一次调度
（`async with McpClientPool(...)`）使用；CLI 的独立命令也遵循同一约定。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, PlatformConfig, bundled_dir, load_structured

TRANSPORTS = ("stdio", "http", "sse", "streamable_http")
HTTP_TRANSPORTS = ("http", "streamable_http", "sse")
_HTTP_ALIASES = ("streamable_http", "streamablehttp", "http")


class McpCatalogError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class McpServer:
    id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    timeout_secs: float = 30.0
    enabled: bool = True
    source: str = ""

    def to_runtime_spec(self) -> dict:
        """转成 `mcp_runtime.normalize_servers` 认得的形状。"""
        return {
            "serverId": self.id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "url": self.url,
            "headers": dict(self.headers),
            "timeoutSecs": self.timeout_secs,
        }

    def view(self) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "source": self.source,
        }
        if self.transport == "stdio":
            data["command"] = self.command
            data["args"] = list(self.args)
        else:
            data["url"] = self.url
        return data


def parse_server(raw: dict, source: str) -> McpServer:
    if not isinstance(raw, dict):
        raise McpCatalogError("MCP_INVALID", f"{source}：服务器定义必须是对象")
    sid = _s(raw.get("id") or raw.get("serverId"))
    where = f"{source}（{sid or '无 id'}）"
    if not sid:
        raise McpCatalogError("MCP_INVALID", f"{source}：缺少 id")
    transport = (_s(raw.get("transport")) or "stdio").lower()
    if transport not in TRANSPORTS:
        raise McpCatalogError(
            "MCP_INVALID", f"{where}：transport 必须是 {'/'.join(TRANSPORTS)} 之一"
        )
    command = _s(raw.get("command"))
    url = _s(raw.get("url"))
    if transport == "stdio" and not command:
        raise McpCatalogError("MCP_INVALID", f"{where}：stdio 传输缺少 command")
    if transport in HTTP_TRANSPORTS and not url:
        raise McpCatalogError("MCP_INVALID", f"{where}：{transport} 传输缺少 url")
    args_raw = raw.get("args")
    if args_raw is not None and not isinstance(args_raw, list):
        raise McpCatalogError("MCP_INVALID", f"{where}：args 必须是数组")
    args = tuple(_s(a) for a in (args_raw or []) if _s(a))
    env = raw.get("env")
    headers = raw.get("headers")
    if env is not None and not isinstance(env, dict):
        raise McpCatalogError("MCP_INVALID", f"{where}：env 必须是对象")
    if headers is not None and not isinstance(headers, dict):
        raise McpCatalogError("MCP_INVALID", f"{where}：headers 必须是对象")
    try:
        timeout = float(raw.get("timeoutSecs") or 30)
    except (TypeError, ValueError):
        raise McpCatalogError("MCP_INVALID", f"{where}：timeoutSecs 必须是数字") from None
    return McpServer(
        id=sid,
        name=_s(raw.get("name")) or sid,
        transport=transport,
        command=command,
        args=args,
        env={str(k): str(v) for k, v in (env or {}).items()},
        url=url,
        headers={str(k): str(v) for k, v in (headers or {}).items()},
        timeout_secs=max(1.0, min(600.0, timeout)),
        enabled=raw.get("enabled") is not False,
        source=source,
    )


class McpCatalog:
    def __init__(self, servers: dict[str, McpServer], errors: list[str], source: str = ""):
        self._servers = servers
        self.errors = errors
        self.source = source

    @classmethod
    def load(cls, config: PlatformConfig | None = None) -> "McpCatalog":
        config = config or PlatformConfig()
        servers: dict[str, McpServer] = {}
        errors: list[str] = []
        sources: list[str] = []
        for path, bundled in (
            (bundled_dir() / "mcp_catalog.yaml", True),
            (config.mcp_catalog_path, False),
            (config.mcp_catalog_path.with_suffix(".json"), False),
        ):
            if not path.is_file():
                continue
            sources.append(str(path))
            try:
                raw = load_structured(path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name} 读取失败：{exc}")
                continue
            items = raw.get("servers") if isinstance(raw, dict) else raw
            if items is None:
                continue
            if not isinstance(items, list):
                errors.append(f"{path.name}：servers 必须是数组")
                continue
            seen: set[str] = set()
            for item in items:
                try:
                    server = parse_server(item, str(path))
                except McpCatalogError as exc:
                    errors.append(str(exc))
                    continue
                if server.id in seen:
                    errors.append(f"{path.name} 内服务器 id 重复：{server.id}（保留第一个）")
                    continue
                seen.add(server.id)
                if bundled and server.id in servers and not servers[server.id].source.startswith(
                    str(bundled_dir())
                ):
                    continue
                servers[server.id] = server
        return cls(servers, errors, "、".join(sources))

    def get(self, server_id: str) -> McpServer | None:
        return self._servers.get(_s(server_id))

    def all(self) -> list[McpServer]:
        return sorted(self._servers.values(), key=lambda s: s.id)

    def servers_for(self, skill: Any) -> tuple[list[McpServer], list[str]]:
        """技能绑定的服务器；`*` 表示全部启用项，空表示不接 MCP（最小权限）。"""
        wanted = tuple(getattr(skill, "mcp_servers", ()) or ())
        if not wanted:
            return [], []
        chosen: list[McpServer] = []
        notes: list[str] = []
        for ref in wanted:
            if ref == "*":
                for server in self.all():
                    if not server.enabled:
                        notes.append(f"MCP 服务器「{server.id}」在 Catalog 中已停用，已跳过")
                    elif server not in chosen:
                        chosen.append(server)
                continue
            server = self.get(ref)
            if server is None:
                notes.append(f"技能绑定的 MCP 服务器「{ref}」不在 Catalog 中，已跳过")
                continue
            if not server.enabled:
                notes.append(f"MCP 服务器「{ref}」在 Catalog 中已停用，已跳过")
                continue
            if server not in chosen:
                chosen.append(server)
        return chosen, notes

    def to_runtime_mcp(self, servers: list[McpServer]) -> list[dict]:
        """给存量 `mcp_runtime.normalize_servers` 的输入；空列表表示不接。"""
        return [s.to_runtime_spec() for s in servers]

    def view(self) -> dict:
        return {
            "servers": [s.view() for s in self.all()],
            "errors": list(self.errors),
            "source": self.source,
        }


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", block)))
        text = "\n".join(p for p in parts if p)
        if text:
            return text
    return str(result)


class McpClientPool:
    """连接池：按 server id 复用客户端，工具清单缓存；close 后子进程随之退出。"""

    def __init__(self, catalog: McpCatalog):
        self.catalog = catalog
        self._clients: dict[str, Any] = {}
        self._tools: dict[str, list[dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._acquire_lock = asyncio.Lock()
        self._closed = False

    def _server(self, server_id: str) -> McpServer:
        server = self.catalog.get(server_id)
        if server is None:
            raise McpCatalogError("MCP_UNKNOWN_SERVER", f"Catalog 中没有服务器「{server_id}」")
        return server

    def _lock(self, server_id: str) -> asyncio.Lock:
        lock = self._locks.get(server_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server_id] = lock
        return lock

    async def acquire(self, server_id: str):
        if self._closed:
            raise McpCatalogError("MCP_POOL_CLOSED", "连接池已关闭")
        server = self._server(server_id)
        async with self._acquire_lock:
            client = self._clients.get(server_id)
            if client is not None:
                return client
            import mcp_runtime as mr

            try:
                client = mr.build_client(server.to_runtime_spec())
                if hasattr(client, "connect"):
                    await client.connect()
            except Exception as exc:  # noqa: BLE001
                if client is not None and hasattr(client, "close"):
                    try:
                        await client.close()
                    except Exception:
                        pass
                raise McpCatalogError(
                    "MCP_CONNECT_FAILED", f"连接器「{server.name}」连接失败：{exc}"
                ) from None
            self._clients[server_id] = client
            return client

    async def discover(self, server_id: str) -> list[dict]:
        if server_id in self._tools:
            return self._tools[server_id]
        client = await self.acquire(server_id)
        async with self._lock(server_id):
            try:
                tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001
                raise McpCatalogError(
                    "MCP_DISCOVERY_FAILED", f"连接器「{server_id}」工具发现失败：{exc}"
                ) from None
            items = []
            for tool in tools or []:
                name = _s(getattr(tool, "name", ""))
                if not name:
                    continue
                schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
                items.append({
                    "name": name,
                    "description": _s(getattr(tool, "description", "")),
                    "schema": schema if isinstance(schema, dict) else {},
                })
            self._tools[server_id] = items
            return items

    async def call(self, server_id: str, tool_name: str, arguments: dict | None = None) -> str:
        client = await self.acquire(server_id)
        tools = await self.discover(server_id)
        if tool_name not in {t["name"] for t in tools}:
            names = "、".join(t["name"] for t in tools) or "（无）"
            raise McpCatalogError(
                "MCP_UNKNOWN_TOOL",
                f"连接器「{server_id}」没有工具「{tool_name}」，可用：{names}",
            )
        async with self._lock(server_id):
            try:
                func = await client.get_callable_function(tool_name)
                result = await func(**(arguments or {}))
            except Exception as exc:  # noqa: BLE001
                raise McpCatalogError(
                    "MCP_CALL_FAILED", f"调用「{server_id}/{tool_name}」失败：{exc}"
                ) from None
        return _result_text(result)

    async def close(self) -> None:
        self._closed = True
        clients = [c for c in self._clients.values() if hasattr(c, "close")]
        self._clients.clear()
        self._tools.clear()
        if clients:
            import mcp_runtime as mr

            await mr.close_all(clients)

    async def __aenter__(self) -> "McpClientPool":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()
