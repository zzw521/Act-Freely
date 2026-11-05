#!/usr/bin/env python3
"""MCP 跨轮常驻连接自测（D-40 第 5 项长连接 + D-39 退避/健康面/错误口径）

锁住五件事，缺一件就不算「长连接」：

1. 复用是真的：同一（会话, 连接器）第二轮不再拉起进程，客户端对象与**服务器进程内
   状态**都延续（夹具的 `note_add` / `whoami` 直接给出 pid 与状态条数）。
2. 收尾必须发生在进入它的那个 asyncio 任务里：每条连接由自己的 slot 任务
   connect → 等回收 → close；枢纽的调度任务与业务任务都不亲自进出作用域。
   跨任务退出 anyio 取消作用域、或在同一任务里乱序退出，都会把子进程留在地上。
3. 什么时候不该复用：配置或凭据变了就地重建、探活失败就地重开、容量与空闲超时按
   LRU 回收、引用泄漏过硬时限强制回收、不在枢纽绑定的事件循环时降级为一次性连接。
4. 降级要看得见：`mcp_runtime.attach` 借到/新建/没借到，notes 各说各的，
   注册失败会把这条常驻连接撤下而不是端着坏连接跑下一轮。
5. 连不上不许每轮再付一次握手（D-39）：按连接器记退避、鉴权类失败直接压到最长冷却并
   提示重新授权、改过配置（新指纹）立刻给一次机会、恢复后自动放行；退避台账与健康面
   只留指纹摘要和一句原话，凭据原值一处都不出现；错误类别统一取 `mcp_errors` 那张表。

离线运行：stdio 夹具是本仓库内的 MCP 服务器（`fixtures/mcp_session_server.py`），
不联网、不需要 API Key。agentscope / mcp 缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_mcp_session_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
FIXTURE = str(PROJECT_ROOT / "tests" / "integration" / "fixtures" / "mcp_session_server.py")

try:
    import mcp_runtime as mr
    import mcp_session_runtime as ms
    from agentscope.tool import Toolkit
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: mcp session selftest unavailable ({exc})")
    sys.exit(0)

FAILURES: list = []
SECRET = "super-secret-token-勿外泄"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def stdio_spec(server_id: str = "srv-session", name: str = "常驻回声", **extra) -> dict:
    spec = {
        "serverId": server_id,
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [FIXTURE],
        "env": {"TT_MCP_TRACE_FILE": TRACE_FILE},
        "timeoutSecs": 30,
    }
    spec.update(extra)
    return spec


# ── 1. 纯函数层 ────────────────────────────────────────────────

def pure_checks() -> None:
    print("--- 1. 传输归一 / 配置指纹 / 可否常驻（纯函数层） ---")
    check(
        "传输名归一：空值与别名各归各位",
        ms.normalize_transport({}) == "stdio"
        and ms.normalize_transport({"transport": "STDIO"}) == "stdio"
        and ms.normalize_transport({"transport": "HTTP"}) == "streamable_http"
        and ms.normalize_transport({"transport": "streamablehttp"}) == "streamable_http"
        and ms.normalize_transport({"transport": "sse"}) == "sse",
    )
    check(
        "websocket 这类传输不进取用面，且给得出原因",
        ms.reuse_eligible({"transport": "websocket", "url": "ws://x"})[0] is False
        and "不是常驻连接形态" in ms.reuse_eligible({"transport": "websocket", "url": "ws://x"})[1],
    )
    check("stdio 缺命令直接判不可用", ms.reuse_eligible({"transport": "stdio", "command": " "})[0] is False)
    check("http 缺地址直接判不可用", ms.reuse_eligible({"transport": "http", "url": ""})[0] is False)

    base = stdio_spec()
    fp = ms.fingerprint_of(base)
    check("指纹稳定：同配置同摘要", fp == ms.fingerprint_of(stdio_spec()) and len(fp) == 16)
    check("命令变了要重建", fp != ms.fingerprint_of(stdio_spec(command="python3")))
    check("参数顺序变了要重建", fp != ms.fingerprint_of(stdio_spec(args=["-u", FIXTURE])))
    check(
        "凭据（env 值）变了要重建：不能让长连接替用户抱着过期令牌",
        fp != ms.fingerprint_of(stdio_spec(env={"TT_MCP_TRACE_FILE": TRACE_FILE, "X": "1"})),
    )
    check(
        "headers 值变了要重建",
        fp != ms.fingerprint_of(stdio_spec(headers={"Authorization": "Bearer 旧的"})),
    )
    check(
        "超时也进指纹：改超时等于换连接方式",
        fp != ms.fingerprint_of(stdio_spec(timeoutSecs=5)),
    )
    check(
        "指纹只看连接事实，与展示名无关",
        fp == ms.fingerprint_of(stdio_spec(name="换了个中文名")),
    )


# ── 2. 枢纽语义（假客户端：策略可确定性验证）──────────────────

class FakeClient:
    """只实现枢纽用到的那几个方法；把 connect/close 落在哪个任务里如实记下来

    记的是任务对象而不是 `id()`：任务结束后对象可能被回收，新任务会复用同一个地址，
    用地址判「是不是同一条任务」会误报。
    """

    def __init__(self, spec: dict, log: list, seq: int):
        self.spec = spec
        self.name = str(spec.get("name") or "连接器")
        self.log = log
        self.seq = seq
        self.is_connected = False
        self.alive = True
        self.faulty_close = False
        self.connects = 0
        self.closes = 0
        self.list_calls = 0

    async def connect(self) -> None:
        if self.is_connected:
            raise RuntimeError("The MCP server is already connected. Call close() first.")
        self.connects += 1
        self.is_connected = True
        self.log.append(("connect", asyncio.current_task(), self.seq))

    async def close(self, ignore_errors: bool = True) -> None:
        if not self.is_connected:
            raise RuntimeError("The MCP server is not connected. Call connect() before closing.")
        self.closes += 1
        self.log.append(("close", asyncio.current_task(), self.seq))
        if self.faulty_close:
            raise RuntimeError("Attempted to exit cancel scope in a different task")
        self.is_connected = False

    async def list_tools(self) -> list:
        if not self.is_connected:
            raise RuntimeError("The connection is not established.")
        self.list_calls += 1
        if not self.alive:
            raise RuntimeError("服务器已经没有响应了")
        return []


def fake_hub(log: list, now: list, **kw):
    made: list = []

    def factory(spec):
        client = FakeClient(spec, log, len(made) + 1)
        made.append(client)
        return client

    hub = ms.McpSessionHub(
        client_factory=factory,
        clock=lambda: now[0],
        poll_seconds=kw.pop("poll_seconds", 0.05),
        **kw,
    )
    hub.made = made  # type: ignore[attr-defined]
    return hub


class RecordingStack:
    """顶替 AgentScope connect() 里那个 AsyncExitStack：记下有没有真被收过"""

    def __init__(self, sink: list):
        self.sink = sink

    async def aclose(self) -> None:
        self.sink.append("aclose")


async def hub_checks() -> None:
    print("--- 2. 枢纽策略（假客户端） ---")
    log: list = []
    now = [1_000_000]
    spec = stdio_spec()

    hub = fake_hub(log, now)
    await hub.start()
    hub_task = hub._task                     # 调度任务：不该出现在 connect/close 记录里
    caller_task = asyncio.current_task()      # 业务任务：同样不该亲自进出作用域
    got1 = await hub.acquire("task-a", spec)
    check("首轮：新建连接并占住引用",
          got1["reused"] is False and hub.spawns == 1 and hub.live_count() == 1,
          str(got1))
    check("新建的客户端确实被 connect 过", hub.made[0].connects == 1 and hub.made[0].is_connected)
    client_a = got1["client"]

    await hub.release("task-a", spec)
    got2 = await hub.acquire("task-a", spec)
    check("同会话第二轮：复用同一客户端，不再新建",
          got2["reused"] is True and got2["client"] is client_a and hub.spawns == 1,
          str(got2))
    check("复用计数进快照", hub.snapshot()["counters"]["reuses"] == 1, str(hub.snapshot()["counters"]))
    check("复用前做过探活（一轮一次 tools/list）", client_a.list_calls >= 1,
          str(client_a.list_calls))

    # 两个任务同时借同一条连接：引用要数得清，不能谁先跑完谁把连接关了
    got3 = await hub.acquire("task-a", spec)
    check("同一会话重复借用只加引用，对象不变",
          got3["client"] is client_a and hub._entries[("task-a", "srv-session")].refs == 2,
          str(hub._entries[("task-a", "srv-session")].refs))

    async def racer():
        return await hub.acquire("task-a", stdio_spec())

    r1, r2 = await asyncio.gather(asyncio.create_task(racer()), asyncio.create_task(racer()))
    check("并发借用拿回同一条连接（没有各开一个进程）",
          r1["client"] is r2["client"] is client_a and hub.spawns == 1,
          f"{r1['client'] is r2['client']} spawns={hub.spawns}")
    for _ in range(4):
        await hub.release("task-a", spec)
    check("引用归零后才回到空闲",
          hub._entries[("task-a", "srv-session")].refs == 0,
          str(hub._entries[("task-a", "srv-session")].refs))

    # 配置/凭据变更
    old = client_a
    changed = stdio_spec(env={"TT_MCP_TRACE_FILE": TRACE_FILE, "Authorization": SECRET})
    got4 = await hub.acquire("task-a", changed)
    check("凭据变了就地重建：旧连接关闭、新连接顶上",
          got4["client"] is not old and old.is_connected is False and hub.spawns == 2,
          str(hub.snapshot()["counters"]))
    check("重建计数如实记 configRebuilds", hub.config_rebuilds == 1, str(hub.config_rebuilds))
    await hub.release("task-a", changed)

    # 探活失败
    hub.made[-1].alive = False
    dead = hub.made[-1]
    got5 = await hub.acquire("task-a", changed)
    check("探活失败就地重开，不把坏连接交给业务任务",
          got5["client"] is not dead and hub.probe_failures == 1 and hub.spawns == 3,
          str(hub.snapshot()["counters"]))
    check("坏连接被关掉（is_connected 回落）", dead.is_connected is False)
    await hub.release("task-a", changed)
    await hub.shutdown()
    check("shutdown 把在途连接收干净", hub.live_count() == 0, str(hub.live_count()))
    by_seq: dict = {}
    for (ev, task, seq) in log:
        by_seq.setdefault(seq, {})[ev] = task
    check("每条连接的 close 落在进入它的那个任务里（作用域进出配对）",
          bool(by_seq) and all(
              v.get("close") is None or v.get("close") is v.get("connect") for v in by_seq.values()
          ), str({k: sorted(v) for k, v in by_seq.items()}))
    used = {t for (_e, t, _s) in log}
    check("枢纽的调度任务与业务任务都不亲自 connect/close",
          hub_task not in used and caller_task not in used,
          str(sorted(str(t) for t in used)))
    check("不同连接各占一个任务（不按全局后进先出也能收尾）",
          len(used) >= len(by_seq), f"{len(by_seq)} 条连接 / {len(used)} 个任务")

    # 容量 LRU
    log2: list = []
    hub2 = fake_hub(log2, now, max_live=2)
    await hub2.start()
    for i in range(2):
        got = await hub2.acquire("task-b", stdio_spec(server_id=f"srv-{i}"))
        await hub2.release("task-b", stdio_spec(server_id=f"srv-{i}"))
        now[0] += 1000
    check("容量内的两个连接都留着", hub2.live_count() == 2, str(hub2.live_count()))
    await hub2.acquire("task-b", stdio_spec(server_id="srv-2"))
    check("超出容量按最久未用回收",
          hub2.capacity_evictions == 1 and hub2.live_count() == 2
          and ("task-b", "srv-0") not in hub2._entries,
          str(sorted(hub2._entries)))
    for i in (1, 2):
        await hub2.release("task-b", stdio_spec(server_id=f"srv-{i}"))
    await hub2.shutdown()

    # 空闲 TTL（注入时钟推进，owner 任务巡检）
    log3: list = []
    now3 = [10_000]
    hub3 = fake_hub(log3, now3, idle_seconds=2.0)
    await hub3.start()
    await hub3.acquire("task-c", spec)
    await hub3.release("task-c", spec)
    now3[0] += 2_001
    await asyncio.sleep(0.2)
    check("空闲超过 TTL 自动回收", hub3.live_count() == 0 and hub3.idle_evictions == 1,
          str(hub3.snapshot()["counters"]))
    await hub3.shutdown()

    # 引用泄漏（业务任务崩在半路没归还）
    log4: list = []
    now4 = [10_000]
    hub4 = fake_hub(log4, now4, idle_seconds=2.0)
    await hub4.start()
    await hub4.acquire("task-d", spec)  # 故意不 release
    now4[0] += ms.HARD_HOLD_SECONDS * 1000 + 1000
    await asyncio.sleep(0.2)
    check("引用挂了过硬时限：判定泄漏并强制回收",
          hub4.live_count() == 0 and hub4.leak_forced == 1, str(hub4.snapshot()["counters"]))
    await hub4.shutdown()

    # 会话结束只清自己那一摊
    log5: list = []
    now5 = [10_000]
    hub5 = fake_hub(log5, now5)
    await hub5.start()
    await hub5.acquire("task-e", spec)
    await hub5.acquire("task-f", stdio_spec(server_id="srv-other", name="别人的连接器"))
    dropped = await hub5.drop_session("task-e")
    check("会话清理只关这个会话的连接器",
          dropped == 1 and hub5.live_count() == 1 and ("task-f", "srv-other") in hub5._entries,
          str(sorted(hub5._entries)))
    snap = hub5.snapshot()
    blob = json.dumps(snap, ensure_ascii=False)
    check("快照只给摘要：命令/env 原值不外泄",
          SECRET not in blob and "TT_MCP_TRACE_FILE" not in blob and "fixtures" not in blob,
          blob[:200])
    check("快照里能看到在用的槽位与计数",
          snap["live"] == 1 and snap["entries"][0]["serverId"] == "srv-other"
          and "spawns" in snap["counters"], blob[:200])
    await hub5.shutdown()

    # 收尾失败要留痕，不能伪装成已清理
    log6: list = []
    now6 = [10_000]
    hub6 = fake_hub(log6, now6)
    await hub6.start()
    got6 = await hub6.acquire("task-g", spec)
    got6["client"].faulty_close = True
    await hub6.release("task-g", spec)
    await hub6.drop("task-g", "srv-session")
    check("close 抛错时计一次 closeErrors 并留中文原因",
          hub6.close_errors == 1 and "收尾失败" in hub6.last_error and hub6.live_count() == 0,
          hub6.last_error)
    await hub6.shutdown()

    # 跨事件循环：宁可降级也不跨循环退出取消作用域
    log7: list = []
    now7 = [10_000]
    hub7 = fake_hub(log7, now7)
    await hub7.start()
    foreign = await asyncio.to_thread(_acquire_in_new_loop, hub7, spec)
    check("不在枢纽绑定的事件循环里：拒绝复用并说清原因",
          foreign["client"] is None and "事件循环" in foreign["reason"], str(foreign["reason"]))
    check("降级判定不算成复用", hub7.reuses == 0 and hub7.spawns == 0)
    await hub7.shutdown()

    # 握手吊死：owner 任务是串行的，一个不答话的连接器不能拖住后面所有连接器
    log8: list = []
    now8 = [10_000]
    closed_half: list = []

    class HangingClient(FakeClient):
        """模拟 connect() 里进了 ExitStack 却没走到头：子进程已起，握手无回应"""

        def __init__(self, spec, seq):
            super().__init__(spec, log8, seq)
            self.stack = RecordingStack(closed_half)

        async def connect(self) -> None:
            self.connects += 1
            await asyncio.sleep(30)  # 远超枢纽的握手时限

    made8: list = []

    def factory(spec):
        if str(spec.get("serverId")) == "srv-hang":
            client = HangingClient(spec, len(made8) + 1)
        else:
            client = FakeClient(spec, log8, len(made8) + 1)
        made8.append(client)
        return client

    hub8 = ms.McpSessionHub(
        client_factory=factory, clock=lambda: now8[0],
        poll_seconds=0.05, connect_timeout=0.3, close_timeout=0.3,
    )
    await hub8.start()
    hung = False
    hang_exc: BaseException = RuntimeError("没报错")
    try:
        await asyncio.wait_for(hub8.acquire("task-i", stdio_spec(server_id="srv-hang")), 5.0)
    except Exception as exc:  # noqa: BLE001
        hung = True
        hang_exc = exc
    check("握手超过时限：本轮如实报错，不把 owner 任务吊死",
          hung and type(hang_exc).__name__ == "TimeoutError", str(hang_exc))
    check("吊死的连接器没留槽位，且原因带中文说明",
          hub8.live_count() == 0 and hub8.open_failures == 1
          and "握手失败" in hub8.last_error,
          f"{hub8.live_count()} {hub8.open_failures} {hub8.last_error}")
    check("半开连接器的 ExitStack 被就地收掉（不留下孤儿子进程）",
          len(closed_half) == 1, str(closed_half))
    ok_after = await hub8.acquire("task-i", stdio_spec(server_id="srv-ok"))
    check("前一个连接器超时后，后面的连接器照常接上（队列没被堵住）",
          ok_after["client"] is not None and hub8.spawns == 1, str(ok_after))
    await hub8.release("task-i", stdio_spec(server_id="srv-ok"))
    await hub8.shutdown()

    # 两条连接同时常驻，先收早开的那条：空闲回收与会话清理不会讲全局后进先出，
    # anyio 的作用域栈是按任务记的，共享任务管多条连接就是在这里炸过一次
    log9: list = []
    now9 = [10_000]
    hub9 = fake_hub(log9, now9)
    await hub9.start()
    old_spec, new_spec = stdio_spec(server_id="srv-old"), stdio_spec(server_id="srv-new")
    got_old = await hub9.acquire("task-j", old_spec)
    now9[0] += 1000
    got_new = await hub9.acquire("task-k", new_spec)
    await hub9.release("task-j", old_spec)
    await hub9.release("task-k", new_spec)
    check("两条连接各占一个任务（不在同一任务里叠作用域）",
          hub9.live_count() == 2
          and len({t for (e, t, _s) in log9 if e == "connect"}) == 2,
          str([x for x in log9 if x[0] == "connect"]))
    await hub9.drop("task-j", "srv-old")  # 后开的还活着，先收早开的
    check("先收早开的连接：收尾零失败，另一条不受牵连",
          hub9.close_errors == 0 and hub9.live_count() == 1
          and got_new["client"].is_connected and ("task-k", "srv-new") in hub9._entries,
          f"{hub9.close_errors} {hub9.live_count()} {hub9.last_error}")
    check("早开那条确实被关掉且槽位让出", got_old["client"].is_connected is False)
    await hub9.drop_session("task-k")
    check("再收后开的那条也不报错（顺序无关）",
          hub9.close_errors == 0 and hub9.live_count() == 0, hub9.last_error)
    await hub9.shutdown()


def _acquire_in_new_loop(hub, spec: dict) -> dict:
    async def once():
        return await hub.acquire("task-h", spec)

    return asyncio.run(once())


# ── 3. 真 stdio 子进程：跨任务复用与进程痕迹 ──────────────────

TRACE_FILE = ""


def _trace_pids() -> tuple[set, set]:
    """痕迹文件里的 (spawn 进程号, exit 进程号)：整轮自测共用一个文件，比增量要用"""
    spawned, exited = set(), set()
    for line in _read_trace():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "spawn":
            spawned.add(parts[1])
        elif parts[0] == "exit":
            exited.add(parts[1])
    return spawned, exited


def _read_trace() -> list[str]:
    path = Path(TRACE_FILE)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def real_turns() -> None:
    print("--- 3. 真子进程跨轮复用（stdio 夹具，每轮一个新任务） ---")
    hub = ms.McpSessionHub(idle_seconds=300.0)
    await hub.start()

    async def turn(note: str) -> dict:
        # 每一轮都像执行面那样开一个新任务：这正是「谁用谁关」会炸的地方
        async def body():
            client = await hub.acquire("task-real", stdio_spec())
            return client

        got = await asyncio.create_task(body())
        check(f"{note}：拿到客户端", got.get("client") is not None, str(got.get("reason")))
        client = got["client"]
        func = await client.get_callable_function("whoami")
        out = await func()
        text = out.content[0]["text"]
        await hub.release("task-real", stdio_spec())
        return {"text": text, "reused": got["reused"], "client": client}

    first = await turn("第 1 轮")
    second = await turn("第 2 轮")
    third = await turn("第 3 轮")

    check("第 1 轮是新建，第 2、3 轮是复用",
          first["reused"] is False and second["reused"] and third["reused"],
          str([first["reused"], second["reused"], third["reused"]]))
    check("三轮共用同一个客户端对象",
          second["client"] is third["client"] is first["client"])
    pids = {t["text"].split()[0] for t in (first, second, third)}
    check("服务器进程只有一个（pid 不变）", len(pids) == 1, str(pids))
    check("枢纽只拉起过一次进程", hub.spawns == 1 and hub.reuses == 2,
          str(hub.snapshot()["counters"]))

    # 进程内状态：只有真复用才会在下一轮看见上一轮写进去的东西
    held = first["client"]
    add = await held.get_callable_function("note_add")
    await add(text="上一轮留下的状态")
    count_out = await (await held.get_callable_function("note_count"))()
    check("服务器进程内状态跨轮累积（复用的是同一条会话）",
          count_out.content[0]["text"] == "notes=1", count_out.content[0]["text"])

    spawns = _read_trace()
    check("痕迹文件只记了一次 spawn",
          sum(1 for line in spawns if line.startswith("spawn")) == 1, str(spawns))

    # 换了配置：必须重开一个新进程，旧进程要真的退出（痕迹里有它的 exit 行）
    old_pid = pids.pop().replace("pid=", "")
    await hub.drop("task-real", "srv-session")
    waited = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        lines = _read_trace()
        if any(line.startswith("exit") and f"pid={old_pid}" in line for line in lines):
            waited = True
            break
    check("断开后子进程真的退出（夹具写下 exit 行）", waited, str(_read_trace()))

    again = await turn("断开后的下一轮")
    new_pid = again["text"].split()[0].replace("pid=", "")
    check("重开是新进程，pid 与上一台不同", new_pid != old_pid, f"{new_pid} vs {old_pid}")
    check("重开计数从复用变成新建", again["reused"] is False and hub.spawns == 2,
          str(hub.snapshot()["counters"]))

    # 关停：所有连接都在 owner 任务里收掉，子进程随之退出
    await hub.shutdown()
    final = _read_trace()
    spawn_pids = [line.split()[1] for line in final if line.startswith("spawn")]
    exit_pids = [line.split()[1] for line in final if line.startswith("exit")]
    check("关停后每台服务器进程都有 exit（无残留子进程）",
          spawn_pids and set(spawn_pids) == set(exit_pids), str(final))
    check("关停后槽位清空", hub.live_count() == 0 and not hub.running())


# ── 4. attach 与常驻连接的配合 ────────────────────────────────

async def attach_integration() -> None:
    print("--- 4. attach：借到 / 新建 / 降级 / 失败撤下 ---")
    hub = ms.McpSessionHub(idle_seconds=300.0)
    await hub.start()

    toolkit = Toolkit()
    res1 = await mr.attach(toolkit, {"mcp": [stdio_spec(name="常驻一号")]},
                           hub=hub, session_key="task-attach")
    check("attach 首轮注册工具并借出常驻连接",
          {"whoami", "note_add", "note_count"} <= set(toolkit.tools.keys())
          and len(res1["held"]) == 1 and res1["clients"] == [],
          str(sorted(toolkit.tools.keys())))
    check("首轮说明里写清「本轮建立常驻连接」",
          any("本轮建立常驻连接" in n for n in res1["notes"]), str(res1["notes"]))
    await mr.release_hub(hub, "task-attach", res1["held"])

    toolkit2 = Toolkit()
    res2 = await mr.attach(toolkit2, {"mcp": [stdio_spec(name="常驻一号")]},
                           hub=hub, session_key="task-attach")
    check("第二轮工具照旧可用且是复用",
          "whoami" in toolkit2.tools.keys() and hub.reuses == 1 and hub.spawns == 1,
          str(hub.snapshot()["counters"]))
    check("复用那一轮把「未重新拉起」说给用户",
          any("复用同会话第" in n and "未重新拉起" in n for n in res2["notes"]), str(res2["notes"]))

    out = await toolkit2.tools["whoami"].original_func()
    check("复用的连接里工具真的还能调用", out.content[0]["text"].startswith("pid="),
          out.content[0]["text"])
    await mr.release_hub(hub, "task-attach", res2["held"])

    # 别的会话不共享这条连接（每个会话一套进程，互不串状态）
    toolkit3 = Toolkit()
    res3 = await mr.attach(toolkit3, {"mcp": [stdio_spec(name="常驻一号")]},
                           hub=hub, session_key="task-other")
    check("另一个会话新建自己的连接（不串会话状态）",
          hub.spawns == 2 and len(res3["held"]) == 1, str(hub.snapshot()["counters"]))
    await mr.release_hub(hub, "task-other", res3["held"])

    # 会话结束：只清这个会话，另一个仍在
    await hub.drop_session("task-attach")
    check("会话清理后仍在的只有另一会话的槽位",
          hub.live_count() == 1 and ("task-other", "srv-session") in hub._entries,
          str(sorted(hub._entries)))
    await hub.drop_session("task-other")
    check("两个会话都收完", hub.live_count() == 0)

    # 坏连接器：进程起来就退出，握手拿不到回应。注册失败要把常驻槽位撤下，
    # 不能端着同一个坏连接跑下一轮
    broken = {
        "serverId": "srv-broken",
        "name": "半路坏的",
        "transport": "stdio",
        "command": sys.executable,
        # 真实失败面：子进程秒退，initialize 永远等不到回应
        "args": ["-c", "import sys; sys.exit(1)"],
        "timeoutSecs": 30,
    }
    toolkit4 = Toolkit()
    res4 = await mr.attach(toolkit4, {"mcp": [broken]}, hub=hub, session_key="task-broken")
    entry_after = hub._entries.get(("task-broken", "srv-broken"))
    check("连不上的连接器不影响本轮其余结论，且给出中文原因",
          any("工具注册失败" in n for n in res4["notes"]), str(res4["notes"]))
    check("失败的连接器不会被留在常驻槽位里", entry_after is None, str(sorted(hub._entries)))
    check("坏连接器没把本轮的 clients 留下（不泄漏给调用方收尾）",
          res4["clients"] == [] and res4["held"] == [], str(res4))

    # 没给 hub：完全走存量口径（本轮自建、调用方关闭）
    toolkit5 = Toolkit()
    legacy = await mr.attach(toolkit5, {"mcp": [stdio_spec(name="一次性")]})
    check("不给枢纽时保持旧行为：本轮自建客户端并交回给调用方关闭",
          len(legacy["clients"]) == 1 and legacy["held"] == []
          and legacy["clients"][0].is_connected, str(legacy))
    legacy_note = "未走常驻连接" in " ".join(legacy["notes"])
    check("旧路径不编造「常驻」字样", legacy_note is False, str(legacy["notes"]))
    await mr.close_all(legacy["clients"])
    check("旧路径收尾后连接关闭", all(not c.is_connected for c in legacy["clients"]))

    # 给了 hub 但连接器本身不适合常驻：降级要看得见
    toolkit6 = Toolkit()
    bogus = {"serverId": "srv-bogus", "name": "怪传输", "transport": "websocket",
             "url": "ws://127.0.0.1:1"}
    res6 = await mr.attach(toolkit6, {"mcp": [bogus]}, hub=hub, session_key="task-bogus")
    check("不适合常驻的传输：降级并如实说明原因",
          any("未走常驻连接" in n and "不是常驻连接形态" in n for n in res6["notes"]),
          str(res6["notes"]))

    await hub.shutdown()


def wiring_checks() -> None:
    """接线面：执行进程是否真的把枢纽接上了（源码断言，不等于运行时验证）

    这里只保证「接线没被改丢」：attach 带 hub/session_key、收尾归还引用、会话淘汰与
    关停各自有回收点。真机多轮对话仍需在桌面端演练，台账里按未做处理。
    """
    print("--- 5. server.py 接线（源码断言） ---")
    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    check("模块级只有一个连接枢纽", src.count("MCP_HUB = msr.McpSessionHub()") == 1)
    check("attach 带上了枢纽与会话标识",
          "hub=MCP_HUB, session_key=session.task_id" in src.replace("\n                ", " "),
          "没找到 hub/session_key 传参")
    check("borrowed 连接在收尾时归还引用",
          "await mr.release_hub(MCP_HUB, session.task_id, mcp_held)" in src.replace(
              "\n            ", " "))
    check("一次性连接与借出连接各自初始化，不会未定义就引用",
          "mcp_clients: list = []" in src and "mcp_held: list = []" in src)
    check("会话被淘汰时投递该会话的连接回收",
          "MCP_HUB.schedule_session_drop(tid)" in src)
    check("lifespan 启动时绑定循环、关停时收摊",
          "await MCP_HUB.start()" in src and "await MCP_HUB.shutdown()" in src)
    # 借出的连接不能同时又进 close_all（那等于下一轮端着已关闭的客户端）
    check("归还与关闭走的是两条清单",
          "await mr.close_all(mcp_clients)" in src and "close_all(mcp_held)" not in src)


def eviction_checks() -> None:
    """真接线行为面：用 server.py 的那个模块级枢纽，验证会话被淘汰时连接随之收掉

    只测「淘汰 → 投递回收 → 进程退出」这条链，不启动整台引擎（那需要可用模型）。
    `_im_lifespan` 的 start/shutdown 仍由第 5 节的源码断言兜着，桌面真机演练未做。
    """
    print("--- 6. 会话淘汰联动（engine.server 的模块级枢纽） ---")
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "engine"))
        import server as engine_server
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 引擎模块导入不了，跳过淘汰联动（{type(exc).__name__}: {exc}）")
        return

    hub = engine_server.MCP_HUB

    async def scenario() -> None:
        await hub.start()
        base = _trace_pids()
        keys = ["tt-evict-1", "tt-evict-2", "tt-evict-3"]
        for tid in keys:
            got = await hub.acquire(tid, stdio_spec(server_id=f"srv-{tid}", name=tid))
            assert got["client"] is not None, got
            # 会话对象按执行面的样子摆进表里：cancelled=True 才是可淘汰候选
            session = engine_server.TaskSession(tid)
            session.cancelled = True
            engine_server._TASK_SESSIONS[tid] = session
        check("淘汰前三个会话各占一条常驻连接",
              hub.live_count() == 3, str(sorted(hub._entries)))

        original = engine_server.MAX_IN_MEMORY_SESSIONS
        engine_server.MAX_IN_MEMORY_SESSIONS = 1
        try:
            engine_server._evict_finished_sessions()  # 同步投递回收，不等结果
            for _ in range(60):
                # 回收由枢纽自己的任务执行：让出控制权等它跑完，最长 3 秒
                if hub.live_count() <= 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            engine_server.MAX_IN_MEMORY_SESSIONS = original
            for tid in keys:
                engine_server._TASK_SESSIONS.pop(tid, None)
        check("会话被淘汰后它的连接器随之收掉（进程不跟着会话数涨）",
              hub.live_count() <= 1, str(sorted(hub._entries)))
        check("回收走的是 session-ended 口径，不记成收尾失败",
              hub.close_errors == 0, hub.last_error)
        spawned_now, exited_now = _trace_pids()
        new_spawns = spawned_now - base[0]
        new_exits = exited_now - base[1]
        check("被淘汰会话的子进程真的退出（本景起 3 台、至少 2 台已退出）",
              len(new_spawns) == 3 and len(new_exits) >= 2,
              f"spawn={sorted(new_spawns)} exit={sorted(new_exits)}")
        await hub.shutdown()
        spawned_now, exited_now = _trace_pids()
        check("枢纽关停后每台起过的服务器都有 exit（无残留子进程）",
              spawned_now and spawned_now <= exited_now,
              f"未退出={sorted(spawned_now - exited_now)}")
        check("枢纽关停后被淘汰会话不再留槽位", hub.live_count() == 0)

    _run(scenario())


# ── 7. 连不上时：退避、鉴权与健康面（D-39）────────────────────

class FailingClient:
    """握手必败的客户端：失败面（鉴权 / 网络）由测试决定，用来验退避与文案"""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.is_connected = False
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        raise self.exc

    async def close(self, ignore_errors: bool = True) -> None:
        return None

    async def list_tools(self) -> list:
        raise RuntimeError("The connection is not established.")


class Named:
    def __init__(self, name: str):
        self.name = name


class NamedClient(FakeClient):
    """探活时会返回「真东西」的客户端：SDK 对象形状与 dict 形状混着给"""

    async def list_tools(self) -> list:
        # super() 负责「没连上就报错」与计数，这里只替换返回形状
        await super().list_tools()
        return [Named("read_file"), Named("write_file"), {"name": "search_nodes"}]


def backoff_pure_checks() -> None:
    print("--- 7a. 失败分类与退避时长（纯函数层） ---")
    auth_cases = [
        RuntimeError("Error POSTing to endpoint (HTTP 401): Unauthorized"),
        RuntimeError("mcp error: unauthorized"),
        RuntimeError("HTTP status 403: Forbidden"),
        RuntimeError("invalid api key provided"),
        RuntimeError("服务器返回未授权"),
        RuntimeError("访问令牌过期，请重新登录"),
        RuntimeError("Bearer token is expired"),
    ]
    for exc in auth_cases:
        check(f"这类失败是「要重新授权」：{str(exc)[:28]}", ms.needs_reauth(exc))
    plain_cases = [
        RuntimeError("connection refused on port 401"),
        RuntimeError("url=http://127.0.0.1:4013/mcp 连接被拒绝"),
        TimeoutError(),
        RuntimeError("子进程已退出"),
        RuntimeError("名字里带 401 但这不是状态码：workspace 401"),
        ValueError(""),
    ]
    for exc in plain_cases:
        check(f"这类失败不是鉴权问题：{type(exc).__name__}/{str(exc)[:24]}",
              not ms.needs_reauth(exc))

    check("失败 0 次不等（只有真失败才冷却）", ms.backoff_ms(0, 1000, 300_000) == 0)
    check("第 1 次失败等基数", ms.backoff_ms(1, 1000, 300_000) == 1000)
    check("第 2、3 次翻倍", ms.backoff_ms(2, 1000, 300_000) == 2000
          and ms.backoff_ms(3, 1000, 300_000) == 4000)
    check("封顶：不让一次宕机把重试推到几小时后",
          ms.backoff_ms(30, 1000, 300_000) == 300_000)
    check("失败次数为负也不倒着走", ms.backoff_ms(-3, 1000, 300_000) == 0)


async def backoff_checks() -> None:
    print("--- 7b. 枢纽的退避与鉴权（假客户端，时钟可控） ---")
    log: list = []
    now = [2_000_000]
    made: list = []
    fail: list = [RuntimeError("Error POSTing to endpoint (HTTP 401): Unauthorized")]

    def factory(spec):
        client = FailingClient(fail[0]) if fail[0] is not None else FakeClient(spec, log, len(made) + 1)
        made.append(client)
        return client

    hub = ms.McpSessionHub(
        client_factory=factory, clock=lambda: now[0], poll_seconds=0.05,
        backoff_seconds=1.0, connect_timeout=1.0, close_timeout=1.0,
    )
    await hub.start()
    spec = stdio_spec(server_id="srv-dead", name="总是 401",
                      env={"TT_MCP_TRACE_FILE": TRACE_FILE, "TT_SECRET": SECRET})

    err: BaseException = RuntimeError("没报错")
    try:
        await hub.acquire("task-1", spec)
    except Exception as exc:  # noqa: BLE001
        err = exc
    check("第一次连不上仍然如实抛（不静默吞掉）", isinstance(err, RuntimeError), str(err))
    rows = hub.snapshot()["backoff"]
    check("失败当场记下退避（下一轮不该再撞一次）", len(rows) == 1, str(rows))
    row = rows[0]
    check("鉴权类失败认得出来（不混在「网络坏了」里）", row["needsAuth"] is True and row["failures"] == 1, str(row))
    check("鉴权失败直接压到最长冷却：重试多少次都不会自己变好",
          row["nextRetryMs"] - now[0] == hub.max_backoff_ms, str(row["nextRetryMs"] - now[0]))
    check("退避台账只留指纹与服务器那句话（配置原值不外泄）",
          "401" in row["reason"] and SECRET not in str(rows) and row["fingerprint"] != SECRET,
          str(row))

    blocked = await hub.acquire("task-1", spec)
    check("冷却期内本轮不再重复握手", blocked.get("blocked") is True and blocked["client"] is None, str(blocked))
    check("冷却时一个客户端对象都不造（不白拉进程）", len(made) == 1, str(len(made)))
    check("原因说清是鉴权并指向重新授权",
          "未授权" in blocked["reason"] and "重新授权" in blocked["reason"], blocked["reason"])
    check("拦截文案里也不带凭据原值", SECRET not in blocked["reason"], blocked["reason"])
    counters = hub.snapshot()["counters"]
    check("冷却拦截摊进计数（authBlocks 与 backoffBlocks 分开看）",
          counters["authBlocks"] == 1 and counters["backoffBlocks"] == 1, str(counters))
    check("原因里保留服务器那句话（用户不用猜是 401 还是别的）", "401" in row["reason"], str(row))

    # 用户改配置（重新授权回来的令牌）：指纹变了就该立刻给一次机会
    now[0] += hub.max_backoff_ms - 1
    fresh = {**spec, "env": {"TT_MCP_TRACE_FILE": TRACE_FILE, "TT_TOKEN": "新令牌"}}
    fail[0] = None
    revived = await hub.acquire("task-1", fresh)
    check("改过配置（新令牌）不许被旧失败次数拦着", revived["client"] is not None, str(revived))
    check("连上之后退避记录清空", hub.snapshot()["backoff"] == [], str(hub.snapshot()["backoff"]))
    check("重新授权成功走的是同一条指纹重建路径（不是复用旧连接）",
          revived["reused"] is False and hub.spawns == 1, str(hub.snapshot()["counters"]))
    await hub.release("task-1", fresh)

    # 网络类失败：指数退避，且不能误判成鉴权
    fail[0] = RuntimeError("connection refused on port 401")
    dead2 = {**spec, "serverId": "srv-net", "name": "端口没人听", "env": {"TT_MCP_TRACE_FILE": TRACE_FILE}}
    for expect_wait, attempt in ((1000, 1), (2000, 2), (4000, 3)):
        try:
            await hub.acquire("task-1", dead2)
        except Exception:  # noqa: BLE001
            pass
        snap = hub.snapshot()["backoff"]
        net_row = next((r for r in snap if r["serverId"] == "srv-net"), {})
        check(f"第 {attempt} 次网络失败：等 {expect_wait} 毫秒",
              net_row.get("failures") == attempt
              and net_row.get("nextRetryMs", 0) - now[0] == expect_wait, str(net_row))
        check(f"第 {attempt} 次网络失败不算鉴权问题", net_row.get("needsAuth") is False, str(net_row))
        # 第 3 次顺手验一下「还没到冷却点就该拦住」——放在推时钟之前，否则拦不住
        if attempt == 3:
            spawns_before = hub.spawns
            blocked2 = await hub.acquire("task-1", dead2)
            check("网络退避的文案说「连续失败 + 何时重试」而不是「未授权」",
                  blocked2.get("blocked") is True and "连续" in blocked2["reason"]
                  and "未授权" not in blocked2["reason"], blocked2["reason"])
            check("网络退避也带类别那句（端口没人听，而不是只说失败）",
                  "端口没人听" in blocked2["reason"], blocked2["reason"])
            check("网络退避期间不再起新进程（跟鉴权拦截一样省一次握手）",
                  hub.spawns == spawns_before and blocked2["client"] is None,
                  f"spawns={hub.spawns}")
        now[0] += expect_wait + 1
    now[0] += 60_000
    fail[0] = None
    recovered = await hub.acquire("task-1", dead2)
    check("服务恢复后下一轮立刻放行（退避不是永久黑名单）",
          recovered["client"] is not None and hub.snapshot()["backoff"] == [], str(recovered))

    # 健康面：live 与 blocked 同屏
    fail[0] = RuntimeError("unauthorized")
    third = {**spec, "serverId": "srv-half", "name": "刚坏的那个"}
    try:
        await hub.acquire("task-1", third)
    except Exception:  # noqa: BLE001
        pass
    health = {r["serverId"]: r for r in hub.health()}
    check("健康面给每个连接器一行（常驻的、退避的都在）",
          {"srv-dead", "srv-net", "srv-half"} <= set(health), str(sorted(health)))
    check("连着的标 live，退避的标 blocked",
          health["srv-net"]["state"] == "live" and health["srv-half"]["state"] == "blocked",
          str({k: v["state"] for k, v in health.items()}))
    check("blocked 行带上失败次数、是否要重新授权与下次可试时间",
          health["srv-half"]["failures"] == 1 and health["srv-half"]["needsAuth"] is True
          and health["srv-half"]["nextRetryMs"] > now[0], str(health["srv-half"]))
    check("退避的行排在最前（用户先看见坏掉的）", hub.health()[0]["state"] == "blocked",
          str([r["state"] for r in hub.health()]))
    # 类别与中文文案由 mcp_errors 统一给：界面只渲染，不再自己拼「连接失败」
    check("退避行带机读类别与中文下一步（鉴权的那种要指向重新授权）",
          health["srv-half"]["kind"] == "auth" and "重新授权" in health["srv-half"]["hint"],
          str(health["srv-half"]))
    check("常驻行也带一句现状（连着、探到几个工具）",
          health["srv-net"]["kind"] == "live" and bool(health["srv-net"]["hint"]),
          str(health["srv-net"]))
    check("健康面的中文提示里没有凭据原值",
          SECRET not in str(hub.health()), str(hub.health())[:200])
    await hub.shutdown()
    check("枢纽关停连退避台账一起清掉（重启不把上次的失败算给用户）",
          hub.snapshot()["backoff"] == [] and hub.live_count() == 0, str(hub.snapshot()))

    # 探活顺手拿到的工具清单：白名单展开用它，不再多打一次往返
    nlog: list = []
    nmade: list = []

    def named_factory(spec):
        client = NamedClient(spec, nlog, len(nmade) + 1)
        nmade.append(client)
        return client

    hub_t = ms.McpSessionHub(client_factory=named_factory, clock=lambda: now[0], poll_seconds=0.05)
    await hub_t.start()
    tools_spec = stdio_spec(server_id="srv-tools", name="带清单的")
    first = await hub_t.acquire("task-t", tools_spec)
    check("首轮（新建）不假装知道工具清单", first.get("tools") is None, str(first))
    await hub_t.release("task-t", tools_spec)
    second = await hub_t.acquire("task-t", tools_spec)
    check("复用轮的探活顺手把清单交给调用方",
          second["reused"] is True and second["tools"] == ["read_file", "write_file", "search_nodes"],
          str(second))
    check("拿清单用的是同一套取名口径（SDK 对象与 dict 混着也给得出名字）",
          nmade[0].list_calls == 1, str(nmade[0].list_calls))
    entry_row = hub_t.snapshot()["entries"][0]
    check("工具数进快照（逐连接器健康面看得见它到底注册了几个）",
          entry_row["toolCount"] == 3 and entry_row["lastProbeMs"] == now[0], str(entry_row))
    await hub_t.shutdown()

    # attach 层：冷却中的连接器不该被拉起，也不该假装接入成功
    toolkit = Toolkit()
    bmade: list = []

    def boom_factory(spec):
        client = FailingClient(RuntimeError("unauthorized"))
        bmade.append(client)
        return client

    boom = ms.McpSessionHub(
        client_factory=boom_factory, clock=lambda: now[0], poll_seconds=0.05, backoff_seconds=1.0,
    )
    await boom.start()
    one = stdio_spec(server_id="srv-attach-dead", name="连不上的")
    res1 = await mr.attach(toolkit, {"mcp": [one]}, hub=boom, session_key="task-x")
    check("第一次仍然按「注册失败」如实报（不把退避粉饰成成功）",
          any("注册失败" in n for n in res1["notes"]), str(res1["notes"]))
    res = await mr.attach(toolkit, {"mcp": [one]}, hub=boom, session_key="task-x")
    check("退避中的连接器在注册链路上被跳过（不是又一条 traceback）",
          any("本轮不接入" in n for n in res["notes"]) and not any("注册失败" in n for n in res["notes"]),
          str(res["notes"]))
    check("跳过时一个工具都没注册、也没留下待收尾的客户端",
          toolkit.tools == {} and res["clients"] == [] and res["held"] == [], str(res))
    check("退避期间没有再拉起新连接（第一轮一次、第二轮零次）", len(bmade) == 1, str(len(bmade)))
    await boom.shutdown()


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        check("联动场景整体跑通", False, f"{type(exc).__name__}: {exc}")
        raise


def main() -> int:
    global TRACE_FILE
    print("=== MCP 跨轮常驻连接自测（D-40 第 5 项 + D-39 退避） ===")
    tmp = tempfile.mkdtemp(prefix="tt-mcp-session-")
    TRACE_FILE = os.path.join(tmp, "trace.log")
    pure_checks()
    asyncio.run(hub_checks())
    asyncio.run(real_turns())
    asyncio.run(attach_integration())
    wiring_checks()
    eviction_checks()
    backoff_pure_checks()
    asyncio.run(backoff_checks())
    print("----------------------------------------------------")

    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} 项未过")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("SELFTEST PASSED: MCP 常驻连接契约全部通过")
    return 0


if __name__ == "__main__":
    # 依赖缺失在上面的导入块就 SKIP 退出了；能走到这里再炸，只可能是自测自己写坏了。
    # 旧的宽 `except` 把 UnboundLocalError 报成「依赖或环境不满足」并退 0，等于把失败
    # 藏成绿灯——本轮真被它骗过一次，所以这里改成如实报红。
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 自测自身出错（依赖已在导入处校验过，这不是 SKIP）："
              f"{type(exc).__name__}: {exc}", flush=True)
        print(f"SELFTEST FAILED: 自测崩溃")
        sys.exit(1)
