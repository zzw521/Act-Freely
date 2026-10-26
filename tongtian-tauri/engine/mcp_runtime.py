"""MCP 连接器 → Agent Toolkit 工具（能力台账 P0-1）

Rust 侧管配置与握手事实（`mcp_servers` 表 + 「连接」验证），这里负责把**已连接**
连接器的工具真的注册进本轮 Toolkit。过去连接器只到「列表页能看到工具」为止，
模型一个都调不到，属于台账里的「只落库不生效」。

生命周期约束：stdio 子进程的退出必须发生在进入它的那个 asyncio 任务里，否则 anyio
的 cancel scope 直接报错。所以 `attach`/`close_all` 都由 `_run_execution` 的
`run_agent()` 同任务包住调用，跑完即关，不驻留后台进程（与 Rust 侧探测同口径）。

失败口径：单个连接器失败只影响它自己的工具，不拖垮整轮任务；每条没能注册的原因
都由调用方作为步骤事件摊出来。
"""

from __future__ import annotations

import os

import tool_search as ts

# 一轮最多接入的连接器数量：再多通常是配置出错，不该让提交卡在串行握手上
MAX_MCP_SERVERS = 8

DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 600.0

_HTTP_TRANSPORTS = ("streamable_http", "streamablehttp", "http")


def display_name(spec: dict) -> str:
    return str(spec.get("name") or spec.get("serverId") or "连接器").strip() or "连接器"


def _brief(exc: BaseException) -> str:
    """异常摘要压到一行：MCP 的报错常常是整段 traceback，事件里塞不下也没法读"""
    text = " ".join(str(exc).split())
    return text[:240] + "…" if len(text) > 240 else text


def _timeout(raw) -> float:
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if secs != secs:  # NaN
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, secs))


def normalize_servers(runtime: dict | None) -> tuple[list[dict], list[str]]:
    """runtime["mcp"] → (待接入服务器, 说明)。缺键表示客户端没带这一项，不制造噪声。"""
    block = (runtime or {}).get("mcp")
    if block is None:
        return [], []
    if isinstance(block, dict):
        err = str(block.get("error") or "").strip()
        if err:
            return [], [f"连接器清单读取失败，本轮没有接入任何工具：{err}"]
        return [], []
    if not isinstance(block, list):
        return [], []
    servers = [
        s
        for s in block
        # serverId 是 SQLite 里的连接器主键：没有它就无法把工具归属到某个连接器
        if isinstance(s, dict) and str(s.get("serverId") or "").strip()
    ]
    notes: list[str] = []
    if len(servers) > MAX_MCP_SERVERS:
        notes.append(
            f"本轮只接入前 {MAX_MCP_SERVERS} 个连接器，"
            f"其余 {len(servers) - MAX_MCP_SERVERS} 个未注册工具"
        )
        servers = servers[:MAX_MCP_SERVERS]
    return servers, notes


def build_client(spec: dict):
    """按传输类型构造 AgentScope 的 MCP 客户端；配置不全就抛中文说明"""
    from agentscope.mcp import HttpStatelessClient, StdIOStatefulClient

    name = display_name(spec)
    transport = str(spec.get("transport") or "stdio").strip().lower()
    if transport in ("", "stdio"):
        command = str(spec.get("command") or "").strip()
        if not command:
            raise ValueError("stdio 传输缺少启动命令")
        args = [str(a) for a in (spec.get("args") or []) if str(a).strip()]
        env = {
            str(k): str(v)
            for k, v in (spec.get("env") if isinstance(spec.get("env"), dict) else {}).items()
            if str(k).strip()
        }
        return StdIOStatefulClient(
            name=name,
            command=command,
            args=args,
            # 给了 env 就整份替换子进程环境，必须带上继承的 PATH，
            # 否则 node/python 这类靠 PATH 解析的命令根本起不来
            env={**os.environ, **env} if env else None,
        )
    url = str(spec.get("url") or "").strip()
    kind = "streamable_http" if transport in _HTTP_TRANSPORTS else "sse"
    if not url:
        raise ValueError(f"{transport} 传输缺少服务地址")
    headers = {
        str(k): str(v)
        for k, v in (spec.get("headers") if isinstance(spec.get("headers"), dict) else {}).items()
        if str(k).strip()
    }
    return HttpStatelessClient(
        name=name,
        transport=kind,
        url=url,
        headers=headers or None,
        timeout=_timeout(spec.get("timeoutSecs")),
    )


def _tool_names(toolkit) -> set:
    tools = getattr(toolkit, "tools", None)
    return set(tools.keys()) if isinstance(tools, dict) else set()


async def attach(toolkit, runtime: dict | None, lazy: bool = False) -> dict:
    """把已连接连接器的工具注册进 toolkit。

    `lazy=True` 时每个连接器进一个未激活工具组，模型先用 search_tools 搜索启用；
    默认 False 保持旧的「全部直接可见」行为，避免影响不经设置页的调用方。

    返回 `{"notes": [...], "clients": [...]}`：notes 由调用方作为步骤事件下发，
    clients 是需要（且必须同任务）关闭的 stateful 客户端。
    """
    servers, notes = normalize_servers(runtime)
    if not servers:
        return {"notes": notes, "clients": []}
    try:
        import mcp  # noqa: F401  依赖缺失要在这一轮就说清楚
    except Exception as exc:
        return {
            "notes": [*notes, f"运行时无 MCP 客户端依赖，连接器工具未注册：{_brief(exc)}"],
            "clients": [],
        }

    clients: list = []
    for index, spec in enumerate(servers):
        name = display_name(spec)
        before = _tool_names(toolkit)
        client = None
        group_name = "basic"
        if lazy:
            try:
                group_name = ts.ensure_group(toolkit, spec, index)
            except Exception as exc:  # noqa: BLE001
                # 分组建不起来时退回直接注册：工具可用优先，原因必须可见
                group_name = "basic"
                notes.append(
                    f"连接器「{name}」无法建立按需工具组，已直接注册全部工具：{_brief(exc)}"
                )
        try:
            client = build_client(spec)
            if hasattr(client, "connect"):
                await client.connect()
                clients.append(client)
            # 与内置三件套重名时跳过内置工具不动，注册结果以 toolkit 实际新增为准
            await toolkit.register_mcp_client(
                client, group_name=group_name, namesake_strategy="skip"
            )
        except Exception as exc:
            clients = [c for c in clients if c is not client]
            if client is not None:
                await close_all([client])
            notes.append(f"连接器「{name}」工具注册失败，本轮用不到它：{_brief(exc)}")
            continue
        added = sorted(_tool_names(toolkit) - before)
        if not added:
            notes.append(
                f"连接器「{name}」本轮没有新增工具：服务器未声明工具，或工具名与已注册的重复"
            )
        elif lazy and group_name != "basic":
            listed = "、".join(added[:8])
            if len(added) > 8:
                listed += f" 等 {len(added)} 个"
            notes.append(
                f"连接器「{name}」已接入 {len(added)} 个工具（按需载入）：{listed}；"
                "调用前先用 search_tools 搜索启用"
            )
        else:
            notes.append(f"连接器「{name}」已接入 {len(added)} 个工具：{'、'.join(added)}")
    return {"notes": notes, "clients": clients}


async def close_all(clients: list) -> None:
    """关闭 stateful 客户端（子进程随之退出）；收尾失败只记日志，不影响已完成的回答

    connect() 半途失败（子进程已起来但 initialize 抛错）的客户端也要进来，
    否则 stdio 子进程就泄漏了；SDK 对「未连接就 close」会抛错，那属于正常收尾，
    不当成异常播报。
    """
    for c in clients:
        try:
            await c.close()
        except Exception as exc:
            if "not connected" in str(exc).lower():
                continue
            print(f"[engine] MCP 客户端关闭失败（忽略）: {exc}", flush=True)
