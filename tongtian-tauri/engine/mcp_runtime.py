"""MCP 连接器 → Agent Toolkit 工具（能力台账 P0-1）

Rust 侧管配置与握手事实（`mcp_servers` 表 + 「连接」验证），这里负责把**已连接**
连接器的工具真的注册进本轮 Toolkit。过去连接器只到「列表页能看到工具」为止，
模型一个都调不到，属于台账里的「只落库不生效」。

生命周期约束：stdio 子进程的退出必须发生在进入它的那个 asyncio 任务里，否则 anyio
的 cancel scope 直接报错。所以 `attach`/`close_all` 都由 `_run_execution` 的
`run_agent()` 同任务包住调用；**不给 hub** 时跑完即关，不驻留后台进程。**给了 `hub`**
（D-40 第 5 项）时连接由 `mcp_session_runtime.McpSessionHub` 常驻管着，本轮只借还引用。

失败口径：单个连接器失败只影响它自己的工具，不拖垮整轮任务；每条没能注册的原因
都由调用方作为步骤事件摊出来，并先经 `mcp_errors` 翻成一句中文（D-39 中文错误映射），
服务方原话仍然跟在后面。
"""

from __future__ import annotations

import os

import sandbox_launcher as sandbox
import tool_search as ts
import mcp_connector_catalog as mcc
import mcp_errors as mex

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
    return mex.brief(exc, 240)


def _timeout(raw) -> float:
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if secs != secs:  # NaN
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, secs))


def normalize_servers(runtime: dict | None) -> tuple[list[dict], list[str]]:
    """runtime["mcp"] → (待接入服务器, 说明)。缺键表示客户端没带这一项，不制造噪声。

    两道闸在这里把关（D-39 验收项「停用连接器后工具不可调用」）：

    - `enabled=False`：桌面把连接器停用了，工具就不能再进 Toolkit——过去这里只看
      有没有 `serverId`，停用的连接器照样被接入，等于「界面关了、模型还能调」；
    - `authExpiresAtMs` 已过：拿着过期令牌去握手只会得到一句 401，还白付一次握手
      超时；直接说明「请重新授权」比让服务器替我们报错诚实。
    """
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
    notes: list[str] = []
    now_ms = _now_ms()
    servers: list[dict] = []
    for item in block:
        # serverId 是 SQLite 里的连接器主键：没有它就无法把工具归属到某个连接器
        if not isinstance(item, dict) or not str(item.get("serverId") or "").strip():
            continue
        name = display_name(item)
        if item.get("enabled") is False:
            notes.append(f"连接器「{name}」已停用，本轮不接入它的工具")
            continue
        expired = _expired_at(item, now_ms)
        if expired:
            notes.append(expired)
            continue
        servers.append(item)
    if len(servers) > MAX_MCP_SERVERS:
        notes.append(
            f"本轮只接入前 {MAX_MCP_SERVERS} 个连接器，"
            f"其余 {len(servers) - MAX_MCP_SERVERS} 个未注册工具"
        )
        servers = servers[:MAX_MCP_SERVERS]
    return servers, notes


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _expired_at(spec: dict, now_ms: int) -> str:
    """令牌过期判定：只有明确给了 `authExpiresAtMs` 且已经过去才拦下来"""
    raw = spec.get("authExpiresAtMs")
    if raw is None:
        return ""
    try:
        expires = int(raw)
    except (TypeError, ValueError):
        return ""
    if expires <= 0 or expires > now_ms:
        return ""
    return (
        f"连接器「{display_name(spec)}」的访问令牌已过期，本轮不接入；"
        "请在桌面端重新授权（一键授权或刷新令牌）后继续"
    )


def build_client(spec: dict, stateful: bool = False):
    """按传输类型构造 AgentScope 的 MCP 客户端；配置不全就抛中文说明。

    `stateful=True` 时远程传输也用带会话的客户端（`HttpStatefulClient`）：
    连接与 MCP 会话跨调用保持，由 `mcp_session_runtime` 那样持有生命周期的一方
    负责关闭。默认 False 保持存量口径（无状态客户端，每次调用自建会话），
    不影响只做一次探测/独立调用的调用方。
    """
    from agentscope.mcp import (
        HttpStatefulClient,
        HttpStatelessClient,
        StdIOStatefulClient,
    )

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
        # 系统隔离开启时，MCP 服务进程也要进 AppContainer；用户为连接器显式配置的
        # 变量（如 GITHUB_TOKEN）由启动器保留，其余敏感变量仍被过滤。
        try:
            wrapped = sandbox.wrap(command, args, keep_env=tuple(env))
        except sandbox.SandboxUnavailable as exc:
            raise ValueError(
                f"系统隔离已开启，连接器「{name}」无法在沙箱中启动：{exc}"
            ) from None
        if wrapped:
            command, args = wrapped
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
    Http = HttpStatefulClient if stateful else HttpStatelessClient
    return Http(
        name=name,
        transport=kind,
        url=url,
        headers=headers or None,
        timeout=_timeout(spec.get("timeoutSecs")),
    )


def _tool_names(toolkit) -> set:
    tools = getattr(toolkit, "tools", None)
    return set(tools.keys()) if isinstance(tools, dict) else set()


async def _declared_tool_names(client) -> tuple:
    """向服务器要一次工具名清单，返回 `(names 或 None, 原因)`。

    白名单里的通配规则（`read_*`、网关的 `上游/*`）必须按真实工具名展开：AgentScope 的
    `enable_funcs` 只做精确名匹配。拿不到清单时返回 `(None, 原因)`——调用方据此**收紧**
    （只放行写了完整名字的规则），绝不因为"列不出来"就退回全量注册：那等于把用户已经
    做出的权限决策放宽。
    """
    try:
        raw = await client.list_tools()
    except Exception as exc:  # noqa: BLE001
        return None, _brief(exc)
    return mcc.tool_names_from(raw), ""


async def attach(toolkit, runtime: dict | None, lazy: bool = False, hub=None,
                 session_key: str = "", expert_ref: str = "") -> dict:
    """把已连接连接器的工具注册进 toolkit。

    `lazy=True` 时每个连接器进一个未激活工具组，模型先用 search_tools 搜索启用；
    默认 False 保持旧的「全部直接可见」行为，避免影响不经设置页的调用方。

    给了 `hub`（`mcp_session_runtime.McpSessionHub`）就走跨轮常驻：同一
    （`session_key`, 连接器）命中就复用已有客户端，本轮结束只归还引用、不关连接。
    枢纽给不出槽位时（传输不适合常驻、本轮不在枢纽绑定的事件循环里、配置不可用）
    如实降级成本轮自建的一次性连接，原因写进 notes——不复用不等于坏，但必须让用户
    看得见为什么这轮又拉起了一次进程。枢纽明确说「本轮别试」（连续握手失败的退避
    冷却、服务器判定需要重新授权）时不降级：那种情况下再拉一次进程只会再付一次失败。

    工具白名单（D-39「按专家放行工具」）在注册那一步生效：`spec.allowedTools`
    是连接器级配置，`expertToolAllowlists[当前专家]` 优先于它（同一连接器给不同专家
    不同权限）。规则支持精确名、`前缀*` 与网关的 `上游/*`，后两种要靠一份工具清单
    展开成真实工具名，再交给 AgentScope 的 `enable_funcs` 精确匹配。清单依次尝试：
    枢纽探活缓存、Rust 上次握手存的 `tools` 列、本轮现打一次 `tools/list`；三样都拿不到
    就**收紧**（只放行写了完整名字的规则），不退回全量注册。

    返回 `{"notes": [...], "clients": [...], "held": [...]}`：
    clients 是本轮自建、必须由调用方在同一个任务里关闭的 stateful 客户端；
    held 是本轮从枢纽借出的连接器 spec，收尾时交给 `release_hub`（只归还引用）。
    """
    servers, notes = normalize_servers(runtime)
    if not servers:
        return {"notes": notes, "clients": [], "held": []}
    try:
        import mcp  # noqa: F401  依赖缺失要在这一轮就说清楚
    except Exception as exc:
        return {
            "notes": [*notes, f"运行时无 MCP 客户端依赖，连接器工具未注册：{_brief(exc)}"],
            "clients": [],
            "held": [],
        }
    expert_ref = str(expert_ref or (runtime or {}).get("expertId") or "").strip()

    clients: list = []
    held: list = []
    for index, spec in enumerate(servers):
        name = display_name(spec)
        before = _tool_names(toolkit)
        client = None
        hub_owned = False
        reused = False
        turns = 0
        group_name = "basic"
        patterns, allow_reason = mcc.allowlist_for(spec, expert_ref)
        if allow_reason:
            notes.append(f"连接器「{name}」{allow_reason}")
        if patterns is not None and not patterns:
            # 空列表是用户显式决定「一条都不放行」：连握手都不必付
            notes.append(
                f"连接器「{name}」的工具白名单是空的，本轮一条都不放行，已跳过连接"
                "（要放开请在桌面端改白名单）"
            )
            continue
        if lazy:
            try:
                group_name = ts.ensure_group(toolkit, spec, index)
            except Exception as exc:  # noqa: BLE001
                # 分组建不起来时退回直接注册：工具可用优先，原因必须可见
                group_name = "basic"
                notes.append(
                    f"连接器「{name}」无法建立按需工具组，已直接注册全部工具：{_brief(exc)}"
                )
        hub_tools: list | None = None
        try:
            if hub is not None:
                got = await hub.acquire(session_key, spec)
                client = got.get("client")
                hub_owned = client is not None
                reused = bool(got.get("reused"))
                turns = int(got.get("turns") or 0)
                tools = got.get("tools")
                hub_tools = list(tools) if isinstance(tools, (list, tuple)) else None
                if not hub_owned:
                    reason = str(got.get("reason") or "连接枢纽未给出常驻槽位")
                    if got.get("blocked"):
                        notes.append(f"连接器「{name}」本轮不接入：{reason}")
                        continue
                    # 降级不等注册结果就先说：这一轮为什么要再拉一次进程，
                    # 用户要在过程里看得见，而不是事后猜
                    notes.append(f"连接器「{name}」未走常驻连接：{reason}")
            if client is None:
                client = build_client(spec)
            if hasattr(client, "connect") and not getattr(client, "is_connected", False):
                # 枢纽里的客户端已经连着；再 connect 会被 AgentScope 当场拒掉
                await client.connect()
                if not hub_owned:
                    clients.append(client)
            if hub_owned:
                held.append(spec)
            enable_funcs = None
            allow_note = ""
            if patterns is not None:
                # 工具清单的来源按便宜到贵排：枢纽探活顺手缓存的那份 > Rust 上次握手存的
                # `tools` 列 > 本轮现打一次 `tools/list`。缓存只可能少放行（缓存里没有的
                # 名字匹配不上），不可能多放行，所以拿它展开通配是安全的。
                declared = hub_tools
                source = "本轮探活"
                if declared is None:
                    cached = mcc.tool_names_from(spec.get("tools"))
                    if cached:
                        declared = cached
                        source = "上次探测的缓存清单"
                if declared is None:
                    declared, why = await _declared_tool_names(client)
                    source = "本轮现问"
                    if declared is None:
                        notes.append(
                            f"连接器「{name}」本轮取不到工具清单（{why}），"
                            "白名单里的通配规则无法展开，只放行写了完整工具名的规则"
                        )
                if declared is not None:
                    enable_funcs = [t for t in declared if mcc.tool_allowed(patterns, t)]
                    allow_note = (
                        mcc.describe_allowlist(patterns, len(declared), len(enable_funcs))
                        + f"，清单取自{source}"
                    )
                else:
                    enable_funcs = [p for p in patterns if not p.endswith("*")]
                    allow_note = (
                        f"工具清单本轮取不到，白名单只按 {len(enable_funcs)} 条完整工具名放行，"
                        "通配规则未展开（宁可少给，不冒险全给）"
                    )
            # 与内置文件工具重名时跳过内置工具不动，注册结果以 toolkit 实际新增为准
            await toolkit.register_mcp_client(
                client, group_name=group_name, namesake_strategy="skip",
                enable_funcs=enable_funcs,
            )
        except Exception as exc:
            if hub_owned:
                # 注册都失败了，这条常驻连接多半已经坏掉：撤下槽位，下一轮重开，
                # 而不是让后面每一轮都端着同一个坏连接
                held = [s for s in held if s is not spec]
                await hub.drop(session_key, str(spec.get("serverId") or "").strip())
            else:
                clients = [c for c in clients if c is not client]
                if client is not None:
                    await close_all([client])
            notes.append(f"连接器「{name}」工具注册失败，本轮用不到它：{mex.explain(exc)}")
            continue
        suffix = ""
        if hub_owned and reused:
            suffix = f"（复用同会话第 {turns} 轮的常驻连接，未重新拉起）"
        elif hub_owned:
            suffix = "（本轮建立常驻连接，同会话后续轮沿用）"
        if allow_note:
            suffix += "；" + allow_note
        added = sorted(_tool_names(toolkit) - before)
        if not added:
            notes.append(
                f"连接器「{name}」本轮没有新增工具：服务器未声明工具，或工具名与已注册的重复{suffix}"
            )
        elif lazy and group_name != "basic":
            listed = "、".join(added[:8])
            if len(added) > 8:
                listed += f" 等 {len(added)} 个"
            notes.append(
                f"连接器「{name}」已接入 {len(added)} 个工具（按需载入）：{listed}；"
                f"调用前先用 search_tools 搜索启用{suffix}"
            )
        else:
            notes.append(f"连接器「{name}」已接入 {len(added)} 个工具：{'、'.join(added)}{suffix}")
    return {"notes": notes, "clients": clients, "held": held}


async def release_hub(hub, session_key: str, specs: list) -> None:
    """把本轮借出的常驻连接归还（只放引用，不关连接）；没有枢纽就什么都不做"""
    if hub is None or not specs:
        return
    for spec in specs:
        try:
            await hub.release(session_key, spec)
        except Exception as exc:  # noqa: BLE001
            print(f"[engine] 常驻连接归还失败（忽略）: {exc}", flush=True)


async def close_all(clients: list) -> None:
    """关闭 stateful 客户端（子进程随之退出）；收尾失败只记日志，不影响已完成的回答

    connect() 半途失败（子进程已起来但 initialize 抛错）的客户端也要进来，
    否则 stdio 子进程就泄漏了；SDK 对「未连接就 close」会抛错，那属于正常收尾，
    不当成异常播报。
    """
    for c in clients:
        if not hasattr(c, "close"):
            continue  # 无状态客户端没有会话要退出（降级路径可能把它交进来）
        try:
            await c.close()
        except Exception as exc:
            if "not connected" in str(exc).lower():
                continue
            print(f"[engine] MCP 客户端关闭失败（忽略）: {exc}", flush=True)
