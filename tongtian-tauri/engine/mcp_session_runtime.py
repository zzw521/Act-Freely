"""MCP 跨轮常驻连接枢纽（D-40 第 5 项：支持会话复用的传输保持长连接）

连接器过去每轮都要「重新拉起 → 握手 → 用完关掉」：stdio 连接器通常是 node/python
起的进程，一次冷启动几百毫秒到几秒，多轮对话里同一份配置反复付这笔钱；服务器侧的
会话状态（浏览器实例、数据库连接、临时目录、已授权上下文）也每轮清零。这里把连接
的生命周期从「一轮」抬到「一个会话」：同一（会话, 连接器）命中就复用，配置变了才
重建，空闲超时 / 会话结束 / 引擎关停才关。

一条硬约束决定了整个结构。AgentScope 的 stateful 客户端在 `connect()` 里进入 anyio
的 cancel scope（stdio_client / streamablehttp_client 内部是 task group），**退出必须
发生在同一个 asyncio 任务里**，否则 `close()` 抛
「Attempted to exit cancel scope in a different task」，子进程还留在地上。而且 anyio 的
cancel scope 栈是**按任务**记的：同一个任务里常驻两条连接，就必须严格按后进先出收尾，
否则收早开的那条会抛「isn't the current task's current cancel scope」（自测真踩到过）。
执行面每一轮都是新任务，空闲回收与会话清理也不讲全局顺序，所以不能靠一个共享后台任务
管所有连接。

做法：**每条连接自己占一个任务**（slot 任务），在同一个任务里 `connect()` → 等回收指令
→ `close()`，作用域进出天然配对；枢纽只当调度（借出、引用计数、探活、投递回收），不再
亲自进出任何作用域。业务任务只借客户端去注册工具与发起调用——跨任务*使用*一条已连接的
会话是允许的（响应靠事件循环投递回来），要禁止的只是跨任务进出作用域。

失败口径与 `mcp_runtime` 一致：单个连接器坏掉只影响它自己，原因由调用方摊成步骤
事件。复用前先探活（一次 `tools/list` 往返），探活失败就地重开一次，重开仍失败如实
抛错——绝不把「上一轮连着」当成「这一轮可用」。握手在 slot 任务里自带超时，收尾等不到
也照样摘槽位，并把「进程可能还在」如实记进计数——不能让整个枢纽跟着一个连接器吊死。

凭据口径：`env` / `headers` 的**值**参与配置指纹（只留 sha256 摘要，摘要进快照，
原值绝不进快照、不进日志）。令牌刷新会改变指纹，从而强制重建连接，不会让长连接
替用户把一个过期 Authorization 头一直用下去。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import mcp_connector_catalog as mcc
import mcp_errors as mex

# 空闲多久回收：一轮长任务之间用户可能离开几分钟，太短等于没复用
DEFAULT_IDLE_SECONDS = 600.0
# 引用没放掉（业务任务崩在半路）超过这个时长就判定泄漏，强制回收
HARD_HOLD_SECONDS = 1800.0
# 同时常驻的连接器进程上限：再多通常是配置出错，也不该让引擎变成进程堆
DEFAULT_MAX_LIVE = 16
# owner 任务的排队空转间隔：没有命令时用它来做空闲巡检
DEFAULT_POLL_SECONDS = 0.5
# 探活的一次往返上限：比这还慢的服务器当它已死
PROBE_TIMEOUT_SECONDS = 10.0
# 握手与收尾的兜底时限：owner 任务是串行的，一个连接器吊死会拖住所有会话的连接器
CONNECT_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 15.0
# 连续失败后的退避基数与上限：第 1 次失败等 5 秒，之后翻倍，最多 5 分钟。
# 没有这一档，服务器宕机期间每一轮都会重新拉起一个必败的子进程并白付一次握手超时。
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0

# 能被「常驻」的传输：stateful 客户端才有 connect/close 可言。
# stdio 与 streamable HTTP / SSE 的 stateful 客户端都算；AgentScope 的
# HttpStatelessClient 每次调用新建会话，缓存它没有收益，不进取用面。
_REUSABLE_TRANSPORTS = ("stdio", "streamable_http", "streamablehttp", "http", "sse")

# 一眼看出「这不是网络坏了，是没授权」的错误特征，以及每类错误的中文口径，都在
# `mcp_errors` 里有一份：注册链路、枢纽退避、桌面端健康面读的是同一张表，
# 免得「这边提示重新授权，那边只说连接失败」。
needs_reauth = mex.needs_reauth  # 机读判定：这条失败要不要用户去重新授权
classify = mex.classify          # 类别（auth / refused / timeout …），进快照给界面着色


def backoff_ms(failures: int, base_ms: int, cap_ms: int) -> int:
    """第 N 次连续失败要等多久：指数退避、封顶，失败次数为 0 时不等待"""
    if failures <= 0:
        return 0
    return min(cap_ms, base_ms * (2 ** (failures - 1)))



def _positive(raw, fallback: float) -> float:
    """时限必须是个正数：配置传歪了宁可回落到默认值，也不要变成永不超时"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value != value or value <= 0:  # NaN / 负数 / 0
        return fallback
    return value


def _brief(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:200] + "…" if len(text) > 200 else text


def normalize_transport(spec: dict) -> str:
    transport = str((spec or {}).get("transport") or "stdio").strip().lower()
    if transport in ("", "stdio"):
        return "stdio"
    if transport in ("http", "streamablehttp", "streamable_http"):
        return "streamable_http"
    if transport == "sse":
        return "sse"
    return transport


def fingerprint_of(spec: dict) -> str:
    """连接配置的指纹：变了就必须重开（含凭据值，令牌刷新不等下一轮）"""
    normalized = {
        "transport": normalize_transport(spec),
        "command": str(spec.get("command") or "").strip(),
        "args": [str(a) for a in (spec.get("args") or [])],
        "url": str(spec.get("url") or "").strip(),
        "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items() if str(k).strip()},
        "headers": {
            str(k): str(v) for k, v in (spec.get("headers") or {}).items() if str(k).strip()
        },
        "timeoutSecs": spec.get("timeoutSecs"),
    }
    blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def reuse_eligible(spec: dict) -> tuple[bool, str]:
    """这个连接器能不能常驻；不能时说清为什么（文案直接进过程事件）"""
    transport = normalize_transport(spec)
    if transport not in _REUSABLE_TRANSPORTS:
        return False, f"传输「{transport}」不是常驻连接形态，本轮按一次性连接处理"
    if transport == "stdio" and not str(spec.get("command") or "").strip():
        return False, "stdio 传输缺少启动命令，本轮无法接入"
    if transport != "stdio" and not str(spec.get("url") or "").strip():
        return False, f"{transport} 传输缺少服务地址，本轮无法接入"
    return True, ""


@dataclass
class _Entry:
    """一个（会话, 连接器）的常驻槽位：连接由它自己的 slot 任务进出作用域"""

    session_key: str
    server_id: str
    name: str
    fingerprint: str
    client: Any
    transport: str
    opened_ms: int = 0
    last_used_ms: int = 0
    refs: int = 0
    turns: int = 0
    spawns: int = 1
    task: Optional[asyncio.Task] = None          # 唯一进出这条连接作用域的任务
    closing: Optional[asyncio.Event] = None      # 枢纽投递的回收指令
    retired: str = ""                            # 回收原因，供收尾时留痕
    tool_names: Optional[list[str]] = None       # 探活顺手拿到的工具名，白名单展开用它
    last_probe_ms: int = 0                       # 最后一次确认「它还活着」的时刻

    def idle_for(self, now_ms: int) -> int:
        return max(0, now_ms - self.last_used_ms)


@dataclass
class _Backoff:
    """连不上的连接器：记下失败次数与下次可试时间，别每轮都重新付一次握手超时"""

    failures: int = 0
    next_retry_ms: int = 0
    fingerprint: str = ""
    needs_auth: bool = False
    reason: str = ""
    kind: str = mex.UNKNOWN_KIND
    last_fail_ms: int = 0


class McpSessionHub:
    """按会话持有 MCP 客户端；每条连接的 connect/close 都在它自己的 slot 任务里成对发生。

    注入点（测试用）：
      - `client_factory(spec)`：造客户端，默认复用 `mcp_runtime.build_client(stateful=True)`
      - `clock()`：单调毫秒时钟
    """

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[dict], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_live: int = DEFAULT_MAX_LIVE,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        probe: bool = True,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        close_timeout: float = CLOSE_TIMEOUT_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._factory = client_factory
        self._clock_ms = clock or (lambda: int(time.monotonic() * 1000))
        self.idle_ms = max(1000, int(idle_seconds * 1000))
        self.hard_hold_ms = int(HARD_HOLD_SECONDS * 1000)
        self.poll_seconds = poll_seconds
        self.probe = probe
        self.connect_timeout = _positive(connect_timeout, CONNECT_TIMEOUT_SECONDS)
        self.close_timeout = _positive(close_timeout, CLOSE_TIMEOUT_SECONDS)
        self.backoff_base_ms = int(_positive(backoff_seconds, DEFAULT_BACKOFF_BASE_SECONDS) * 1000)
        self.max_backoff_ms = int(MAX_BACKOFF_SECONDS * 1000)
        self._max_live = max(1, int(max_live))

        self._entries: dict[tuple[str, str], _Entry] = {}
        self._backoff: dict[str, _Backoff] = {}  # 按连接器（不按会话）：宕机时别每个新会话再撞一次
        self._slot_tasks: set = set()  # 每条连接（含握手半途的）自己的任务
        self._queue: "asyncio.Queue[tuple[str, dict, Optional[asyncio.Future]]] | None" = None
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

        # 计数全部对外可查（快照里摊出来），用来证明「真的复用了」而不是嘴上说说
        self.spawns = 0
        self.reuses = 0
        self.probe_failures = 0
        self.open_failures = 0
        self.config_rebuilds = 0
        self.idle_evictions = 0
        self.capacity_evictions = 0
        self.session_drops = 0
        self.leak_forced = 0
        self.close_errors = 0
        self.backoff_blocks = 0
        self.auth_blocks = 0
        self.last_error = ""

    # ── 生命周期 ────────────────────────────────────────────────

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def on_foreign_loop(self) -> bool:
        """当前调用方所在的事件循环不是枢纽绑定的那个：不能跨循环复用连接"""
        if not self.running():
            return False
        try:
            return asyncio.get_running_loop() is not self._loop
        except RuntimeError:
            return True

    async def start(self) -> None:
        if self.running():
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._closed = False
        self._task = self._loop.create_task(self._owner_loop(), name="mcp-session-hub")

    async def shutdown(self) -> None:
        """收摊：先把所有槽位关在 owner 任务里，再取消 owner；幂等，失败不抛"""
        self._closed = True
        if not self.running():
            self._entries.clear()
            self._backoff.clear()
            return
        try:
            await self._submit("shutdown", {})
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"枢纽关停异常：{exc}"
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._entries.clear()
        # 退避台账一起清：引擎重启（或用户改了配置）不该把上一次的失败算到这一轮头上
        self._backoff.clear()

    async def _submit(self, op: str, kwargs: dict) -> Any:
        if self._queue is None or self._loop is None:
            raise RuntimeError("MCP_HUB_NOT_STARTED: 连接枢纽尚未启动")
        fut = self._loop.create_future()
        self._queue.put_nowait((op, kwargs, fut))
        return await fut

    async def _owner_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                op, kwargs, fut = await asyncio.wait_for(
                    self._queue.get(), timeout=self.poll_seconds
                )
            except asyncio.TimeoutError:
                await self._sweep()
                continue
            except asyncio.CancelledError:
                await self._close_all("cancel")
                raise
            try:
                result = await self._handle(op, kwargs)
            except Exception as exc:  # noqa: BLE001
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
                continue
            if fut is not None and not fut.done():
                fut.set_result(result)

    async def _handle(self, op: str, kwargs: dict) -> Any:
        if op == "acquire":
            return await self._op_acquire(**kwargs)
        if op == "release":
            return self._op_release(**kwargs)
        if op == "drop":
            return await self._op_drop(**kwargs)
        if op == "drop_session":
            return await self._op_drop_session(**kwargs)
        if op == "shutdown":
            await self._close_all("shutdown")
            return {"closed": True}
        raise RuntimeError(f"MCP_HUB_UNKNOWN_OP: 未知枢纽命令「{op}」")

    # ── 对外接口 ────────────────────────────────────────────────

    async def acquire(self, session_key: str, spec: dict) -> dict:
        """拿到一个可用客户端；返回 `{"client", "reused", "reason", "turns", "tools"}`。

        命中复用时**不重新拉起进程**；配置指纹变了先关旧的再开新的；探活失败就地
        重开一次。所有 connect/close 都发生在 owner 任务里。

        `tools` 是这条连接当前已知的工具名清单（复用时来自探活的那次往返，新建时为
        `None`）——白名单要把通配规则展开成真实工具名，有这份现成的就不必再打一次电话。

        拿不到客户端有两种性质完全不同的情况，调用方必须能分清：
        `blocked=True` 是「本轮别试」（连续失败的退避冷却、服务器判为未授权），再拉一次
        进程只会再付一次失败；没有这个键（或 False）是「这轮不适合常驻」，降级成一次性
        连接照常工作。
        """
        await self.start()
        eligible, why = reuse_eligible(spec)
        if not eligible:
            return {"client": None, "reused": False, "reason": why or "不可常驻"}
        if self.on_foreign_loop():
            return {
                "client": None,
                "reused": False,
                "reason": "本轮不在枢纽绑定的事件循环里，跨循环复用会踩 anyio 取消作用域，按一次性连接处理",
            }
        return await self._submit(
            "acquire",
            {"session_key": session_key, "spec": spec},
        )

    async def release(self, session_key: str, spec: dict) -> None:
        """一轮用完只放引用，不关连接——关是枢纽的事"""
        if not self.running() or self.on_foreign_loop():
            return
        server_id = str((spec or {}).get("serverId") or "").strip()
        if not server_id:
            return
        try:
            await self._submit("release", {"session_key": session_key, "server_id": server_id})
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"引用归还失败：{exc}"

    async def drop(self, session_key: str, server_id: str) -> None:
        if not self.running() or self.on_foreign_loop():
            return
        try:
            await self._submit("drop", {"session_key": session_key, "server_id": server_id})
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"连接断开失败：{exc}"

    def schedule_session_drop(self, session_key: str) -> bool:
        """会话被淘汰 / 删除时投递回收；不等结果（调用点是同步函数）"""
        if not self.running() or self._queue is None or not session_key:
            return False
        self._queue.put_nowait(("drop_session", {"session_key": session_key}, None))
        return True

    async def drop_session(self, session_key: str) -> int:
        """关掉某个会话的全部常驻连接，返回收掉的槽位数"""
        if not self.running() or self.on_foreign_loop():
            return 0
        try:
            return int(await self._submit("drop_session", {"session_key": session_key}))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"会话连接清理失败：{exc}"
            return 0

    def live_count(self) -> int:
        return len(self._entries)

    def snapshot(self) -> dict:
        """只给摘要与计数：连接配置里的 env/headers 原值绝不外泄"""
        return {
            "running": self.running(),
            "live": self.live_count(),
            "maxLive": self.max_live_capacity(),
            "idleMs": self.idle_ms,
            "counters": {
                "spawns": self.spawns,
                "reuses": self.reuses,
                "probeFailures": self.probe_failures,
                "openFailures": self.open_failures,
                "configRebuilds": self.config_rebuilds,
                "idleEvictions": self.idle_evictions,
                "capacityEvictions": self.capacity_evictions,
                "sessionDrops": self.session_drops,
                "leakForced": self.leak_forced,
                "closeErrors": self.close_errors,
                "backoffBlocks": self.backoff_blocks,
                "authBlocks": self.auth_blocks,
            },
            "entries": [
                {
                    "sessionKey": e.session_key,
                    "serverId": e.server_id,
                    "name": e.name,
                    "transport": e.transport,
                    "fingerprint": e.fingerprint,
                    "refs": e.refs,
                    "turns": e.turns,
                    "spawns": e.spawns,
                    "idleMs": e.idle_for(self._clock_ms()),
                    "toolCount": len(e.tool_names) if e.tool_names is not None else None,
                    "lastProbeMs": e.last_probe_ms,
                }
                for e in sorted(
                    self._entries.values(), key=lambda e: (e.session_key, e.server_id)
                )
            ],
            "backoff": [
                {
                    "serverId": server_id,
                    "failures": b.failures,
                    "needsAuth": b.needs_auth,
                    "kind": b.kind,
                    "nextRetryMs": b.next_retry_ms,
                    "lastFailMs": b.last_fail_ms,
                    "reason": b.reason,
                    "fingerprint": b.fingerprint,
                }
                for server_id, b in sorted(self._backoff.items())
            ],
            "lastError": self.last_error,
        }

    def health(self) -> list[dict]:
        """每个连接器一行健康事实：常驻中的、退避中的、要重新授权的，都摊在一处。

        状态只有三种口径，且互相不重叠：`live` 是有常驻槽位；`blocked` 是失败退避里；
        其余（没连过、空闲回收了、这轮没用到）算 `idle`——不是坏，是「此刻没有连接」。
        这里只报事实，不做重连动作：桌面端拿它渲染徽标，重连永远由真实的一轮任务触发。
        """
        now = self._clock_ms()
        rows: dict[str, dict] = {}
        for entry in self._entries.values():
            row = rows.setdefault(
                entry.server_id,
                {"serverId": entry.server_id, "name": entry.name, "state": "live",
                 "transports": set(), "turns": 0, "sessions": 0, "toolCount": None,
                 "idleMs": 0, "failures": 0, "needsAuth": False, "nextRetryMs": 0,
                 "retryInMs": 0, "reason": "", "kind": "", "hint": ""},
            )
            row["state"] = "live"
            row["name"] = row["name"] or entry.name
            row["transports"].add(entry.transport)
            row["turns"] += entry.turns
            row["sessions"] += 1
            if entry.tool_names is not None:
                row["toolCount"] = len(entry.tool_names)
            row["idleMs"] = max(row["idleMs"], entry.idle_for(now))
            # 常驻行也给一句中文：界面不用自己拼「连着」和「连了几个工具」
            row["kind"] = "live"
            row["hint"] = (
                f"常驻中，探活到 {len(entry.tool_names)} 个工具"
                if entry.tool_names is not None
                else "常驻中（本轮新建，尚未探活）"
            )
        for server_id, back in self._backoff.items():
            row = rows.setdefault(
                server_id,
                {"serverId": server_id, "name": server_id, "state": "idle",
                 "transports": set(), "turns": 0, "sessions": 0, "toolCount": None,
                 "idleMs": 0, "failures": 0, "needsAuth": False, "nextRetryMs": 0,
                 "retryInMs": 0, "reason": "", "kind": "", "hint": ""},
            )
            # 冷却过了就不再标 blocked：那是「等着被重试」，不是「还在被拦着」
            row["state"] = "blocked" if self._waiting(back, now) > 0 else "idle"
            row["failures"] = back.failures
            row["needsAuth"] = back.needs_auth
            row["nextRetryMs"] = back.next_retry_ms
            row["retryInMs"] = self._waiting(back, now)
            row["reason"] = back.reason
            row["kind"] = back.kind
            row["hint"] = mex.describe(back.kind)
        out: list[dict] = []
        for row in rows.values():
            out.append(
                {
                    "serverId": row["serverId"],
                    "name": row["name"],
                    "state": row["state"],
                    "transport": "、".join(sorted(t for t in row["transports"] if t)),
                    "turns": row["turns"],
                    "sessions": row["sessions"],
                    "toolCount": row["toolCount"],
                    "idleMs": row["idleMs"],
                    "failures": row["failures"],
                    "needsAuth": row["needsAuth"],
                    "nextRetryMs": row["nextRetryMs"],
                    "retryInMs": row["retryInMs"],
                    "reason": row["reason"],
                    "kind": row["kind"],
                    "hint": row["hint"],
                }
            )
        out.sort(key=lambda r: (r["state"] != "blocked", r["state"] != "live", r["serverId"]))
        return out

    def max_live_capacity(self) -> int:
        return self._max_live

    # ── 调度内部（枢纽自己不进出任何 cancel scope）──────────────

    def _key(self, session_key: str, spec: dict) -> tuple[str, str]:
        return (session_key, str(spec.get("serverId") or "").strip())

    def _build_sync(self, spec: dict):
        """只造客户端对象，真正的握手在 slot 任务的 connect() 里"""
        if self._factory is not None:
            return self._factory(spec)
        import mcp_runtime as mr

        return mr.build_client(spec, stateful=True)

    async def _spawn(self, session_key: str, spec: dict) -> _Entry:
        """开一个 slot 任务去握手；只有真的连上的才占槽位"""
        entry = _Entry(
            session_key=session_key,
            server_id=str(spec.get("serverId") or "").strip(),
            name=str(spec.get("name") or spec.get("serverId") or "连接器"),
            fingerprint=fingerprint_of(spec),
            client=None,
            transport=normalize_transport(spec),
            opened_ms=self._clock_ms(),
            last_used_ms=self._clock_ms(),
            refs=0,
            closing=asyncio.Event(),
        )
        ready = asyncio.get_running_loop().create_future()
        entry.task = asyncio.create_task(
            self._slot_loop(entry, spec, ready),
            name=f"mcp-slot-{session_key}-{entry.server_id}",
        )
        self._slot_tasks.add(entry.task)
        entry.task.add_done_callback(self._slot_tasks.discard)
        try:
            client = await ready  # 握手失败/超时在这里抛给调用方
        except asyncio.CancelledError:
            # 握手半途被取消：slot 任务自己会退出，这里再 cancel 一次容易把它的收尾打断
            if not entry.task.done():
                entry.task.cancel()
            raise
        except BaseException:
            # 握手失败：槽位没占上，但收尾仍归那个任务（_slot_loop 里已安排）
            if not entry.task.done():
                await asyncio.shield(entry.task)
            raise
        entry.client = client
        now = self._clock_ms()
        entry.opened_ms = now
        entry.last_used_ms = now
        self.spawns += 1
        return entry

    async def _slot_loop(self, entry: _Entry, spec: dict, ready) -> None:
        """这条连接唯一进出作用域的任务：connect → 等回收令 → close，全在这里配对"""
        client = None
        try:
            client = self._build_sync(spec)
        except BaseException as exc:  # noqa: BLE001
            self._fail_ready(ready, exc)
            return
        try:
            if hasattr(client, "connect"):
                async with asyncio.timeout(self.connect_timeout):
                    await client.connect()
        except asyncio.CancelledError:
            await self._cleanup_half_open(client, entry)
            self._fail_ready(ready, asyncio.CancelledError())
            raise
        except BaseException as exc:  # noqa: BLE001  含 TimeoutError
            self.open_failures += 1
            self.last_error = f"连接器「{entry.name}」握手失败：{_brief(exc)}"
            await self._cleanup_half_open(client, entry)
            self._fail_ready(ready, exc)
            return
        if not ready.done():
            ready.set_result(client)
        try:
            if entry.closing is not None:
                await entry.closing.wait()
        except asyncio.CancelledError:
            pass  # 关停抢先到了：仍要在本任务里收尾，不能把子进程留在地上
        await self._close_client(client, entry)

    @staticmethod
    def _fail_ready(ready, exc: BaseException) -> None:
        if ready.done():
            return
        if isinstance(exc, asyncio.CancelledError):
            ready.cancel()
        else:
            ready.set_exception(exc)

    async def _probe_alive(self, entry: _Entry) -> tuple[bool, Optional[list[str]]]:
        """探活：一次 tools/list 往返（跨任务使用已连接的会话是允许的）

        不新鲜不如重来，但别把已经死掉的连接交给业务任务。往返拿到的工具名顺手存进
        槽位——白名单要把 `前缀*`、`上游/*` 展开成真实工具名，用这份现成的清单就不用
        再付一次往返。返回 `(是否可用, 工具名清单或 None)`。
        """
        if not self.probe:
            return True, entry.tool_names
        if not getattr(entry.client, "is_connected", True):
            return False, None
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                raw = await entry.client.list_tools()
            names = mcc.tool_names_from(raw)
            entry.tool_names = names
            entry.last_probe_ms = self._clock_ms()
            return True, names
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"连接器「{entry.name}」探活失败：{_brief(exc)}"
            return False, None

    async def _cleanup_half_open(self, client, entry: _Entry) -> None:
        """握手半途失败（超时/报错）：在同一个任务里把半开的栈收掉

        AgentScope 的 `connect()` 只在 `except Exception` 里清 ExitStack；被超时取消走
        的是 CancelledError，栈还开着、子进程可能已经起来。这里补一刀——放着不管就是
        每次握手失败泄漏一个孤儿进程。
        """
        if client is None:
            return
        stack = getattr(client, "stack", None)
        try:
            if getattr(client, "is_connected", False) and hasattr(client, "close"):
                async with asyncio.timeout(self.close_timeout):
                    await client.close()
            elif stack is not None:
                async with asyncio.timeout(self.close_timeout):
                    await stack.aclose()
        except BaseException as exc:  # noqa: BLE001
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」半成品收尾失败：{_brief(exc)}"

    async def _close_client(self, client, entry: _Entry) -> None:
        """收尾只可能来自三种坏法：SDK 抛错、超时、被中断——每种都留下事实"""
        reason = entry.retired or "unknown"
        if not hasattr(client, "close"):
            return
        if not getattr(client, "is_connected", False):
            # 无状态客户端（或没连上的半成品）：没有 cancel scope 要退出
            return
        try:
            async with asyncio.timeout(self.close_timeout):
                await client.close()
        except asyncio.CancelledError:
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」收尾被中断（{reason}）：子进程可能还在"
            raise
        except TimeoutError:
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」收尾超时（{reason}）：子进程可能还在"
        except Exception as exc:  # noqa: BLE001
            if "not connected" in str(exc).lower():
                return
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」收尾失败（{reason}）：{_brief(exc)}"

    async def _retire(self, entry: _Entry, reason: str) -> None:
        """摘槽位 + 投递回收令；收尾等不到也先把槽位让出来（不伪装成已清理）"""
        self._entries.pop((entry.session_key, entry.server_id), None)
        entry.retired = reason
        if entry.closing is not None:
            entry.closing.set()
        task = entry.task
        if task is None or task.done():
            return
        try:
            # 只等不催：主动取消正在退出作用域的任务更容易把子进程留在地上
            async with asyncio.timeout(self.connect_timeout + self.close_timeout + 1.0):
                await asyncio.shield(task)
        except TimeoutError:
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」收尾没等到（{reason}）：子进程可能还在"
        except Exception as exc:  # noqa: BLE001
            self.close_errors += 1
            self.last_error = f"连接器「{entry.name}」收尾失败（{reason}）：{_brief(exc)}"

    async def _close_all(self, reason: str) -> None:
        """每条连接在自己的任务里收尾，顺序不再敏感；没连上的半成品任务一并取消"""
        entries = sorted(self._entries.values(), key=lambda e: e.opened_ms, reverse=True)
        for entry in entries:
            await self._retire(entry, reason)
        self._entries.clear()
        for task in list(self._slot_tasks):
            if not task.done():
                task.cancel()

    async def _make_room(self, keep_out: Optional[_Entry] = None) -> None:
        """容量兜底：先清空闲引用为 0 的最旧槽位"""
        if len(self._entries) < self._max_live:
            return
        idle = sorted(
            (e for e in self._entries.values() if e.refs == 0 and e is not keep_out),
            key=lambda e: e.last_used_ms,
        )
        while len(self._entries) >= self._max_live and idle:
            victim = idle.pop(0)
            await self._retire(victim, "capacity")
            self.capacity_evictions += 1

    async def _op_acquire(self, session_key: str, spec: dict) -> dict:
        key = self._key(session_key, spec)
        server_id = key[1]
        fingerprint = fingerprint_of(spec)
        cooldown = self._cooldown_ms(server_id, fingerprint)
        if cooldown > 0:
            blocked = self._backoff.get(server_id)
            self.backoff_blocks += 1
            if blocked is not None and blocked.needs_auth:
                self.auth_blocks += 1
            return {
                "client": None,
                "reused": False,
                "turns": 0,
                "tools": None,
                "blocked": True,  # 对外口径：本轮别再试（退避冷却 / 待重新授权）
                "reason": self._blocked_text(blocked, cooldown),
            }
        entry = self._entries.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                # 凭据/命令变了：旧连接继续用等于替用户拿着过期配置，就地重建
                self.config_rebuilds += 1
                await self._retire(entry, "config-changed")
                entry = None
            else:
                alive, _tools = await self._probe_alive(entry)
                if not alive:
                    self.probe_failures += 1
                    await self._retire(entry, "probe-failed")
                    entry = None
        if entry is not None:
            entry.refs += 1
            entry.turns += 1
            entry.last_used_ms = self._clock_ms()
            self.reuses += 1
            self._backoff.pop(server_id, None)
            return {
                "client": entry.client,
                "reused": True,
                "reason": "",
                "turns": entry.turns,
                "tools": entry.tool_names,
            }

        await self._make_room()
        try:
            entry = await self._spawn(session_key, spec)
        except BaseException as exc:  # noqa: BLE001  含握手超时
            if not isinstance(exc, asyncio.CancelledError):
                self._record_failure(server_id, fingerprint, spec, exc)
            raise
        self._backoff.pop(server_id, None)
        entry.refs = 1
        entry.turns = 1
        self._entries[key] = entry
        return {
            "client": entry.client,
            "reused": False,
            "reason": "",
            "turns": 1,
            "tools": entry.tool_names,
        }

    # ── 退避与鉴权 ──────────────────────────────────────────────

    def _cooldown_ms(self, server_id: str, fingerprint: str) -> int:
        """还在冷却里就返回还要等多久（毫秒）；配置变了视为「用户动过手」，直接放行重试"""
        back = self._backoff.get(server_id)
        if back is None:
            return 0
        if back.fingerprint != fingerprint:
            # 令牌刷新 / 命令改了：再拿旧的失败次数拦用户不公平
            self._backoff.pop(server_id, None)
            return 0
        return self._waiting(back, self._clock_ms())

    @staticmethod
    def _waiting(back: _Backoff, now_ms: int) -> int:
        """这条退避记录现在还要等多久（不碰配置，只算时间；健康面与闸口共用）"""
        return max(0, back.next_retry_ms - now_ms)

    def _record_failure(self, server_id: str, fingerprint: str, spec: dict, exc: BaseException) -> None:
        """记一次连接失败：次数累计、下次可试时间按指数退避推到将来"""
        now = self._clock_ms()
        back = self._backoff.get(server_id)
        if back is None or back.fingerprint != fingerprint:
            back = _Backoff(fingerprint=fingerprint)
            self._backoff[server_id] = back
        back.failures += 1
        back.last_fail_ms = now
        back.needs_auth = needs_reauth(exc)
        back.kind = classify(exc)
        back.reason = _brief(exc)
        wait = backoff_ms(back.failures, self.backoff_base_ms, self.max_backoff_ms)
        if back.needs_auth:
            # 鉴权失败重试多少次都不会自己变好：直接压到最长冷却，等用户去重新授权
            wait = self.max_backoff_ms
        back.next_retry_ms = now + wait

    def _blocked_text(self, back: Optional[_Backoff], cooldown_ms: int) -> str:
        """为什么这一轮不接这个连接器（中文，不带连接器名——名字由调用方拼在前面）

        类别文案来自 `mcp_errors`：退避时用户看到的不再是「连接失败」这四个字，
        而是「端口没人听 / 未授权 / 被限流」这一句和对应的下一步。
        """
        secs = max(1, int(round(cooldown_ms / 1000)))
        kind = back.kind if back is not None else mex.UNKNOWN_KIND
        if back is not None and back.needs_auth:
            return (
                f"判为需要重新授权——{mex.sentence(kind)}；{mex.advice(kind)}。"
                f"本轮不再重复握手（{secs} 秒内不重试）；"
                f"服务方原话：{back.reason or '（没有回话）'}"
            )
        failures = (back.failures if back is not None else 0)
        what = mex.sentence(kind) if kind != mex.UNKNOWN_KIND else "连续连接失败"
        return (
            f"已连续 {failures} 次连接失败（{what}），退避中（约 {secs} 秒后重试），本轮不重新拉起进程；"
            f"{mex.advice(kind)}；服务恢复或改好配置后会立刻放行"
        )

    def _op_release(self, session_key: str, server_id: str) -> bool:
        entry = self._entries.get((session_key, server_id))
        if entry is None:
            return False
        entry.refs = max(0, entry.refs - 1)
        entry.last_used_ms = self._clock_ms()
        return True

    async def _op_drop(self, session_key: str, server_id: str) -> bool:
        entry = self._entries.get((session_key, server_id))
        if entry is None:
            return False
        await self._retire(entry, "manual")
        return True

    async def _op_drop_session(self, session_key: str) -> int:
        victims = [e for e in self._entries.values() if e.session_key == session_key]
        victims.sort(key=lambda e: e.opened_ms, reverse=True)
        for entry in victims:
            await self._retire(entry, "session-ended")
        self.session_drops += 1
        return len(victims)

    async def _sweep(self) -> None:
        now = self._clock_ms()
        expired = [
            e
            for e in self._entries.values()
            if e.refs == 0 and e.idle_for(now) > self.idle_ms
        ]
        expired.sort(key=lambda e: e.last_used_ms)
        for entry in expired:
            await self._retire(entry, "idle")
            self.idle_evictions += 1
        # 引用一直没放掉（业务任务半途崩了）：超过硬时限强制回收，否则槽位永久占着
        stuck = [
            e for e in self._entries.values() if e.refs > 0 and e.idle_for(now) > self.hard_hold_ms
        ]
        for entry in stuck:
            await self._retire(entry, "leaked-ref")
            self.leak_forced += 1


def make_client_factory(stateful: bool = True):
    """给 `mcp_runtime` / 测试用的默认工厂：造带会话的客户端"""

    def factory(spec: dict):
        import mcp_runtime as mr

        return mr.build_client(spec, stateful=stateful)

    return factory
