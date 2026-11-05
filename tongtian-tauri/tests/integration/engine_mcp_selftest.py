#!/usr/bin/env python3
"""
MCP 连接器进执行面自测（能力台账 P0-1，G4）

锁住「扩展页连接成功 → 模型真的能调用它的工具」这条链，防止再退回
「列表页能看到工具、模型一个都调不到」：
1. runtime["mcp"] 的取值口径（缺键静默、读表失败留痕、条目数上限）
2. 按传输类型构造客户端，配置不全给中文说明而不是吊死
3. attach 到真实 Toolkit：stdio 子进程被拉起、tools/list 注册、tools/call 往返、
   下发的 env 注入子进程、失败连接器只影响它自己
4. 跑完必须关闭（is_connected 回落），不驻留后台进程
5. /api/chat 端到端：假模型发起 tool_use，工具结果真的回到模型上下文
6. 停用 / 过期令牌两道闸（D-39）：连接器停了或令牌过期就不接入本轮，给中文原因，
   而且不占单轮连接名额
7. 工具白名单（D-39 按专家放行）：精确名、前缀规则、显式全关、格式非法四条路径都走
   真实注册链路——少放的工具要说清是谁挡的，全关时不许白拉一个进程
8. 中文错误映射（D-39）：十来种坏法各有一句中文事实与下一步，鉴权判定与类别口径一致，
   数字串里的形似状态码不误判，原话永远附在后面，真实失败的说明仍然是一行
9. connect 车道的五个端点（D-39 界面读路径）：目录/表单到 spec/白名单预览/对账/健康面
   全部**只算不连**，业务性拒绝回 200 + 中文原因，经手凭据形状的那一条只对桌面壳开放

离线运行：夹具是本仓库内的 MCP stdio 服务器，不联网、无需 API Key。
agentscope 或 mcp 缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_mcp_selftest.py
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
FIXTURE = str(PROJECT_ROOT / "tests" / "integration" / "fixtures" / "mcp_echo_server.py")

try:
    import mcp_runtime as mr
    from agentscope.tool import Toolkit
except Exception as exc:
    print(f"SKIP: mcp runtime selftest unavailable ({exc})")
    sys.exit(0)

try:
    from fastapi.testclient import TestClient
    import server as engine_server
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
except Exception as exc:
    print(f"SKIP: 端到端依赖缺失（{exc}）")
    engine_server = None
    ChatModelBase = ChatResponse = DashScopeChatFormatter = None

FAILURES = []
RECORDED: list = []

ENV_KEY = "TT_MCP_SELFTEST"
ENV_VALUE = "seen-by-child"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def stdio_spec(name: str = "本地回声", command: str = sys.executable) -> dict:
    return {
        "serverId": "srv-echo",
        "name": name,
        "transport": "stdio",
        "command": command,
        "args": [FIXTURE],
        "env": {ENV_KEY: ENV_VALUE},
        "timeoutSecs": 30,
    }


# ── 1. 纯函数层 ────────────────────────────────────────────────

def pure_checks() -> None:
    print("--- 1. runtime['mcp'] 取值口径（纯函数层） ---")
    servers, notes = mr.normalize_servers(None)
    check("没带 mcp 键：不接入也不制造噪声", servers == [] and notes == [], str(notes))

    servers, notes = mr.normalize_servers({"mcp": {"error": "MCP_QUERY_FAILED: 表被占用"}})
    check("Rust 读表失败 → 说明里带原因",
          servers == [] and any("连接器清单读取失败" in n and "MCP_QUERY_FAILED" in n for n in notes),
          str(notes))

    servers, notes = mr.normalize_servers({"mcp": [
        stdio_spec("可用"), "不是对象", {"name": "缺标识"}, {"command": "python"},
    ]})
    check("非条目与缺标识的连接器被丢弃", len(servers) == 1 and servers[0]["name"] == "可用", str(servers))

    many = {"mcp": [stdio_spec(f"srv-{i}") for i in range(mr.MAX_MCP_SERVERS + 3)]}
    servers, notes = mr.normalize_servers(many)
    check("超出单轮上限只接前 N 个并说明",
          len(servers) == mr.MAX_MCP_SERVERS and any(f"前 {mr.MAX_MCP_SERVERS} 个" in n for n in notes),
          str(notes))

    # 两道闸（D-39 验收项「停用连接器后工具不可调用」+ 过期令牌不再白付一次握手）
    mixed = {"mcp": [stdio_spec("可用"), {**stdio_spec("停用了"), "enabled": False}]}
    servers, notes = mr.normalize_servers(mixed)
    check("停用的连接器不进本轮清单并说明原因",
          len(servers) == 1 and servers[0]["name"] == "可用"
          and any("已停用" in n and "停用了" in n for n in notes), str(notes))
    check("没标 enabled 的存量行按启用处理（不因缺字段被误关）",
          len(mr.normalize_servers({"mcp": [stdio_spec()]})[0]) == 1)
    check("enabled 为真值时照常接入",
          len(mr.normalize_servers({"mcp": [{**stdio_spec(), "enabled": True}]})[0]) == 1)

    expired = {**stdio_spec("过期的"), "authExpiresAtMs": 1_000}
    servers, notes = mr.normalize_servers({"mcp": [expired]})
    check("令牌已过期的连接器本轮不接入", servers == [], str(servers))
    check("过期说明指向重新授权（不是让用户猜为什么没工具）",
          any("已过期" in n and "重新授权" in n for n in notes), str(notes))
    live = {**stdio_spec(), "authExpiresAtMs": int(__import__("time").time() * 1000) + 3_600_000}
    check("还在有效期内的照常接入（给了字段不等于疑罪从有）",
          len(mr.normalize_servers({"mcp": [live]})[0]) == 1)
    junk = {**stdio_spec(), "authExpiresAtMs": "不是数"}
    check("有效期读不懂时不拦（读不懂 ≠ 过期）", len(mr.normalize_servers({"mcp": [junk]})[0]) == 1)
    zero = {**stdio_spec(), "authExpiresAtMs": 0}
    check("有效期为 0 视为「没配」而不是「早就过期了」",
          len(mr.normalize_servers({"mcp": [zero]})[0]) == 1)
    crowded = {"mcp": [
        {**stdio_spec(f"闸掉的{i}"), "enabled": False} for i in range(mr.MAX_MCP_SERVERS + 3)
    ] + [stdio_spec("唯一可用")]}
    servers, notes = mr.normalize_servers(crowded)
    check("被闸掉的连接器不占单轮名额（否则停用得越多越接不进来）",
          len(servers) == 1 and not any("前 " in n for n in notes), str((len(servers), notes)))

    check("缺启动命令 → 中文说明而不是吊死",
          _raises(mr.build_client, {"name": "x", "transport": "stdio", "command": " "}))
    check("http 缺地址 → 中文说明",
          _raises(mr.build_client, {"name": "x", "transport": "streamable_http", "url": ""}))

    http = mr.build_client({"name": "h", "transport": "STREAMABLE_HTTP", "url": "http://127.0.0.1:1/mcp"})
    check("传输名归一后走 streamable_http", getattr(http, "transport", None) == "streamable_http",
          str(getattr(http, "transport", None)))
    sse = mr.build_client({"name": "s", "transport": "sse", "url": "http://127.0.0.1:1/sse"})
    check("sse 传输独立识别", getattr(sse, "transport", None) == "sse")

    check("超时钳制在 1..600 秒内",
          mr._timeout(0) == 1.0 and mr._timeout(9999) == 600.0
          and mr._timeout("abc") == 30.0 and mr._timeout(None) == 30.0)
    check("display_name 回落顺序：name → serverId → 连接器",
          mr.display_name({"name": "甲"}) == "甲" and mr.display_name({"serverId": "id-1"}) == "id-1"
          and mr.display_name({}) == "连接器")


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    except Exception:
        return True
    return False


# ── 2. attach 到真实 Toolkit（stdio 子进程往返）────────────────

def attach_checks() -> None:
    print("--- 2. 工具注册与真实调用（stdio 子进程） ---")

    async def scenario() -> None:
        toolkit = Toolkit()
        res = await mr.attach(toolkit, {"mcp": [stdio_spec()]})
        check("stdio 服务器被拉起并注册工具",
              {"echo", "now_word", "env_probe"} <= set(toolkit.tools.keys()),
              str(sorted(toolkit.tools.keys())))
        check("接入结果作为说明摊出来（含工具名）",
              any("已接入 3 个工具" in n and "echo" in n for n in res["notes"]), str(res["notes"]))

        reg = toolkit.tools["echo"]
        check("工具带连接器来源标记（内置工具清单不受污染）",
              getattr(reg, "mcp_name", None) == "本地回声", str(getattr(reg, "mcp_name", None)))

        out = await reg.original_func(text="华东,1200")
        text = out.content[0]["text"]
        check("tools/call 真的往返了一次", text == "echo:华东,1200", text[:120])

        out2 = await toolkit.tools["now_word"].original_func()
        check("无入参工具同样可用", out2.content[0]["text"] == "已执行", str(out2.content))

        out3 = await toolkit.tools["env_probe"].original_func(key=ENV_KEY)
        check("下发的 env 注入子进程", f"{ENV_KEY}={ENV_VALUE}" in out3.content[0]["text"],
              out3.content[0]["text"][:120])

        clients = res["clients"]
        check("stdio 客户端记为需要收尾", len(clients) == 1 and clients[0].is_connected,
              str([getattr(c, "is_connected", None) for c in clients]))
        await mr.close_all(clients)
        check("收尾后子进程随之退出（is_connected 回落）",
              all(not c.is_connected for c in clients))

        # 重名连接器：第二次没有新增工具，说明里讲清楚而不是假报成功
        again = await mr.attach(toolkit, {"mcp": [stdio_spec()]})
        check("同名工具重复接入不覆盖内置/已有工具，且如实说明",
              any("没有新增工具" in n for n in again["notes"]), str(again["notes"]))
        await mr.close_all(again["clients"])

        bad = await mr.attach(toolkit, {"mcp": [
            {"serverId": "b", "name": "不存在的命令", "transport": "stdio",
             "command": "no-such-binary-zzz", "args": []},
        ]})
        check("连不上的连接器只影响它自己",
              any("不存在的命令" in n and "注册失败" in n for n in bad["notes"]), str(bad["notes"]))
        check("失败的连接器不留下待收尾客户端", bad["clients"] == [], str(bad["clients"]))
        check("失败说明压成一行（不塞整段 traceback）",
              all("\n" not in n and len(n) < 300 for n in bad["notes"]),
              str([len(n) for n in bad["notes"]]))

        empty = await mr.attach(toolkit, {"mcp": []})
        # 返回值多了 held（本轮从枢纽借出的常驻连接器），旧的两键口径同步更新
        check("空清单不产生噪声说明", empty == {"notes": [], "clients": [], "held": []}, str(empty))

    _run(scenario())


# ── 3. /api/chat 端到端（模型真的调用连接器工具）───────────────

class ToolCallingModel(ChatModelBase):
    """第一次强制发起 echo 工具调用，第二次收尾；用于验证 tools/call 的往返真的回到模型上下文"""

    def __init__(self):
        super().__init__(model_name="fake-mcp-model", stream=False)
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        try:
            RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        except Exception as exc:
            RECORDED.append(f"<unserializable:{exc}>")
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[
                {"type": "tool_use", "id": "call-echo-1", "name": "echo", "input": {"text": "华东"}},
            ])
        return ChatResponse(content=[{"type": "text", "text": "已用连接器工具核对过数据。"}])


class LazyToolCallingModel(ChatModelBase):
    """按「搜索 → 启用后调用」的真实顺序：先 search_tools，再 echo，最后收尾"""

    def __init__(self):
        super().__init__(model_name="fake-mcp-lazy-model", stream=False)
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        try:
            RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        except Exception as exc:
            RECORDED.append(f"<unserializable:{exc}>")
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[
                {"type": "tool_use", "id": "call-search-1", "name": "search_tools",
                 "input": {"query": "echo"}},
            ])
        if self.calls == 2:
            return ChatResponse(content=[
                {"type": "tool_use", "id": "call-echo-1", "name": "echo", "input": {"text": "华东"}},
            ])
        return ChatResponse(content=[{"type": "text", "text": "已先搜索再调用连接器工具。"}])


def http_checks() -> None:
    print("--- 3. /api/chat 端到端 ---")
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ToolCallingModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-mcp-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    RECORDED.clear()
    try:
        resp = client.post("/api/chat", json={
            "message": "用回声工具确认华东这行数据",
            "task_id": "mcp-e2e",
            "runtime": {"mcp": [stdio_spec()]},
            # 直接可用模式：锁住旧的「全部工具立即进 schema」路径
            "agentPolicy": {"lazyMcpTools": False},
        })
        blob = "\n".join(RECORDED)
        check("执行流正常收尾", "event: done" in resp.text, resp.text[:160])
        check("模型收到连接器工具（echo 出现在入参里）", '"echo"' in blob, blob[:200])
        check("模型发起的 tool_use 得到真实结果", "echo:华东" in blob, blob[-400:])
        check("接入说明进事件流（不静默）",
              "已接入 3 个工具" in resp.text, resp.text[:200])
        check("回答文本产出", "已用连接器工具核对过数据" in resp.text, resp.text[-300:])

        # 按需载入模式（设置页默认）：未搜索前工具不在 schema，search_tools 命中后可用。
        # 探测缓存里的工具数要过阈值（D-40 第 5 项）才会真的走按需——3 个工具直接载入更省，
        # 所以这里按「服务器声明了 9 个工具」下发（实际 fixture 只暴露 3 个，不影响调用）。
        lazy_spec = {**stdio_spec(), "tools": [{"name": f"t{i}"} for i in range(9)]}
        RECORDED.clear()
        engine_server.build_model_and_formatter = (
            lambda *a, **k: (LazyToolCallingModel(), DashScopeChatFormatter())
        )
        lazy = client.post("/api/chat", json={
            "message": "用回声工具确认华东这行数据",
            "task_id": "mcp-lazy-e2e",
            "runtime": {"mcp": [lazy_spec]},
            "agentPolicy": {"lazyMcpTools": True},
        })
        lazy_blob = "\n".join(RECORDED)
        check("按需载入：执行流正常收尾", "event: done" in lazy.text, lazy.text[:200])
        check("按需载入：事件里说明（按需载入）",
              "（按需载入）" in lazy.text, lazy.text[:300])
        check("按需载入：搜索命中后工具可调用", "echo:华东" in lazy_blob, lazy_blob[-600:])
        check("按需载入：回答文本产出", "已先搜索再调用连接器工具" in lazy.text, lazy.text[-300:])

        RECORDED.clear()
        broken = client.post("/api/chat", json={
            "message": "随便回答",
            "task_id": "mcp-broken",
            "runtime": {"mcp": [{"serverId": "b", "name": "断线的连接器", "transport": "stdio",
                                 "command": "no-such-binary-zzz", "args": []}]},
        })
        check("连接器故障不影响回答", "event: done" in broken.text and "event: error" not in broken.text,
              broken.text[:200])
        check("连接器故障如实摊出来",
              "断线的连接器" in broken.text and "注册失败" in broken.text, broken.text[:300])
    finally:
        engine_server.WORK_DIR = Path(".")


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        FAILURES.append(f"协程执行异常：{exc}")


# ── 4. 工具白名单：按连接器 / 按专家放行（D-39）────────────────

def allowlist_checks() -> None:
    print("--- 4. 工具白名单在真实注册链路上生效 ---")

    async def scenario() -> None:
        # 精确名：只放行 echo，其余两个工具模型根本看不见
        toolkit = Toolkit()
        res = await mr.attach(toolkit, {"mcp": [{**stdio_spec(), "allowedTools": ["echo"]}]})
        check("白名单精确名只放行命中的工具",
              set(toolkit.tools.keys()) == {"echo"}, str(sorted(toolkit.tools.keys())))
        check("放行比例摊进过程事件（少掉的工具要说清是谁挡的）",
              any("按白名单放行 1/3" in n for n in res["notes"]), str(res["notes"]))
        check("为了展开规则要的那次 tools/list 不影响收尾（客户端仍交回调用方）",
              len(res["clients"]) == 1, str(len(res["clients"])))
        await mr.close_all(res["clients"])

        # 前缀规则：env_* 命中 env_probe
        toolkit2 = Toolkit()
        res2 = await mr.attach(toolkit2, {"mcp": [{**stdio_spec(), "allowedTools": ["env_*"]}]})
        check("前缀规则按真实工具名展开（不是把 env_* 当工具名塞进去）",
              set(toolkit2.tools.keys()) == {"env_probe"}, str(sorted(toolkit2.tools.keys())))
        await mr.close_all(res2["clients"])

        # 一条都不放行：连握手都不必付，不留待收尾的进程
        toolkit3 = Toolkit()
        res3 = await mr.attach(toolkit3, {"mcp": [{**stdio_spec("全关的"), "allowedTools": []}]})
        check("空白名单＝该连接器一个工具都不给", toolkit3.tools == {}, str(sorted(toolkit3.tools)))
        check("空白名单时说明是显式决定而不是故障",
              any("白名单是空的" in n for n in res3["notes"]), str(res3["notes"]))
        check("空白名单不拉起进程、也不留下待收尾的客户端",
              res3["clients"] == [] and res3["held"] == [], str(res3))

        # 规则一条都没命中（网关的 上游/* 遇到不带前缀的服务器）
        toolkit4 = Toolkit()
        res4 = await mr.attach(
            toolkit4, {"mcp": [{**stdio_spec(), "allowedTools": ["crm/*"]}]}
        )
        check("一条都没命中时如实报 0/3 并说「不是坏了」",
              toolkit4.tools == {} and any("0/3" in n and "不是坏了" in n for n in res4["notes"]),
              str(res4["notes"]))
        await mr.close_all(res4["clients"])

        # 按专家：同一连接器给两个专家不同权限
        expert_spec = {
            **stdio_spec("按专家的"),
            "allowedTools": ["echo", "now_word"],
            "expertToolAllowlists": {"法务专家": ["echo"]},
        }
        toolkit5 = Toolkit()
        res5 = await mr.attach(toolkit5, {"mcp": [expert_spec]}, expert_ref="法务专家")
        check("按专家配置的白名单优先于连接器级",
              set(toolkit5.tools.keys()) == {"echo"}, str(sorted(toolkit5.tools.keys())))
        await mr.close_all(res5["clients"])

        toolkit6 = Toolkit()
        res6 = await mr.attach(toolkit6, {"mcp": [expert_spec]}, expert_ref="财务专家")
        check("没有单独配置的专家落回连接器级（不是全放也不是全关）",
              set(toolkit6.tools.keys()) == {"echo", "now_word"}, str(sorted(toolkit6.tools)))
        await mr.close_all(res6["clients"])

        toolkit7 = Toolkit()
        res7 = await mr.attach(toolkit7, {"mcp": [expert_spec], "expertId": "法务专家"})
        check("专家也可以整轮下发（runtime.expertId 与逐次传参同口径）",
              set(toolkit7.tools.keys()) == {"echo"}, str(sorted(toolkit7.tools.keys())))
        await mr.close_all(res7["clients"])

        # 全关的专家不该把别人的进程也拉起来
        intern_spec = {
            **stdio_spec("实习生用的"),
            "allowedTools": ["echo"],
            "expertToolAllowlists": {"实习生": []},
        }
        toolkit8 = Toolkit()
        res8 = await mr.attach(toolkit8, {"mcp": [intern_spec]}, expert_ref="实习生")
        check("对某个专家全关时不连接、不注册（权限决策优先于可用性）",
              toolkit8.tools == {} and res8["clients"] == [], str(res8))

        # 格式非法：按不限制处理并说明，绝不静默变成「全关」
        toolkit9 = Toolkit()
        res9 = await mr.attach(toolkit9, {"mcp": [{**stdio_spec("配歪的"), "allowedTools": {"a": 1}}]})
        check("白名单格式非法时按不限制处理并说明",
              {"echo", "now_word", "env_probe"} <= set(toolkit9.tools.keys())
              and any("格式非法" in n for n in res9["notes"]), str(res9["notes"]))
        await mr.close_all(res9["clients"])

        # 重名工具 + 白名单：注册结果仍以 toolkit 实际新增为准
        toolkit10 = Toolkit()
        first = await mr.attach(toolkit10, {"mcp": [{**stdio_spec(), "allowedTools": ["echo"]}]})
        await mr.close_all(first["clients"])
        again = await mr.attach(
            toolkit10, {"mcp": [{**stdio_spec("第二个"), "allowedTools": ["echo"]}]}
        )
        check("白名单命中但工具重名时不谎报接入数量",
              any("没有新增工具" in n for n in again["notes"]), str(again["notes"]))
        await mr.close_all(again["clients"])

        # 清单来源（D-39 与 Rust 的 tools 列同源）：优先用缓存，缓存只会少放行
        cached_spec = {
            **stdio_spec("带缓存清单的"),
            "allowedTools": ["env_*"],
            "tools": [{"name": "echo", "description": "", "input_schema": {}},
                      {"name": "now_word", "description": "", "input_schema": {}}],
        }
        toolkit11 = Toolkit()
        res11 = await mr.attach(toolkit11, {"mcp": [cached_spec]})
        check("通配展开优先用上次探测缓存的清单（不再多问一次）",
              toolkit11.tools == {}
              and any("清单取自上次探测的缓存清单" in n for n in res11["notes"]),
              str(res11["notes"]))
        check("缓存里没有的名字不会被凭空放行（少放行而不是多放行）",
              set(toolkit11.tools.keys()) == set(), str(sorted(toolkit11.tools)))
        await mr.close_all(res11["clients"])

        ghost_spec = {
            **stdio_spec("缓存过期的"),
            "allowedTools": ["echo", "ghost_tool"],
            "tools": [{"name": "echo"}, {"name": "ghost_tool"}],
        }
        toolkit12 = Toolkit()
        res12 = await mr.attach(toolkit12, {"mcp": [ghost_spec]})
        check("缓存里多写了服务器已经没有的工具时不凭空造工具（以实际注册结果为准）",
              set(toolkit12.tools.keys()) == {"echo"}
              and any("已接入 1 个工具：echo" in n and "放行 2/2" in n for n in res12["notes"]),
              str(res12["notes"]))
        await mr.close_all(res12["clients"])

        # 缓存为空或形状不对：回落到本轮现问一次，而不是当成「一个工具都没有」
        junk_spec = {**stdio_spec("缓存是空的"), "allowedTools": ["env_*"], "tools": []}
        toolkit13 = Toolkit()
        res13 = await mr.attach(toolkit13, {"mcp": [junk_spec]})
        check("没有缓存时回落到本轮现问清单（空缓存不等于零工具）",
              set(toolkit13.tools.keys()) == {"env_probe"}
              and any("清单取自本轮现问" in n for n in res13["notes"]), str(res13["notes"]))
        await mr.close_all(res13["clients"])

    _run(scenario())


def error_mapping_checks() -> None:
    print("--- 5. 错误翻译成中文（D-39 中文错误映射，纯函数 + 真实链路） ---")
    try:
        import mcp_errors as mex
    except Exception as exc:  # noqa: BLE001
        check("中文错误映射模块可导入", False, f"{type(exc).__name__}: {exc}")
        return

    # 每一类都得有「事实」和「下一步」两句，界面不用自己补文案
    for kind in mex.ALL_KINDS:
        check(f"类别「{kind}」有中文事实与下一步",
              bool(mex.sentence(kind).strip()) and bool(mex.advice(kind).strip())
              and mex.describe(kind) == f"{mex.sentence(kind)}（{mex.advice(kind)}）",
              mex.describe(kind))

    cases = [
        ("Error POSTing to endpoint (HTTP 401): Unauthorized", "auth"),
        ("HTTP status 403: Forbidden", "forbidden"),
        ("insufficient scope for this tool", "forbidden"),
        ("访问令牌过期，请重新登录", "auth"),
        ("too many requests, retry after 30s", "rate_limited"),
        ("请求过于频繁（限流）", "rate_limited"),
        ("[WinError 2] 系统找不到指定的文件。", "not_found"),
        ("node: program not found", "not_found"),
        ("npx 无法启动：@modelcontextprotocol/server-filesystem", "not_found"),
        ("[Errno 111] connection refused", "refused"),
        ("url=http://127.0.0.1:4013/mcp 连接被拒绝", "refused"),
        ("[Errno 11001] getaddrinfo failed", "dns"),
        ("Failed to resolve mcp.example.com: no such host", "dns"),
        ("HTTPSConnectionPool: Certificate verify failed: unable to get local issuer", "tls"),
        ("peer closed connection without response", "crashed"),
        ("子进程已退出（exit code 1）", "crashed"),
        ("JSON parse error at line 1", "protocol"),
        ("method not found: tools/list", "protocol"),
        ("something the vendor invented", "unknown"),
    ]
    for text, want in cases:
        got = mex.classify(text)
        check(f"这类报错判为「{want}」：{text[:38]}", got == want, f"实判 {got}：{mex.sentence(got)}")

    # 数字最容易误判：端口号 / workspace id 里的 401、429 不许当成鉴权或限流
    for text in (
        "connection refused on port 401",
        "名字里带 401 但这不是状态码：workspace 401",
        "listen on 4291 failed",
    ):
        check(f"数字串里的状态码形似但不算：{text[:34]}",
              mex.classify(text) not in mex.REAUTH_KINDS
              and mex.classify(text) != "rate_limited", mex.classify(text))

    check("空文本的 TimeoutError 靠类型也认得出来",
          mex.classify(TimeoutError()) == "timeout", mex.sentence(mex.classify(TimeoutError())))
    check("asyncio 超时同样判 timeout（文本也是空的）",
          mex.classify(asyncio.TimeoutError()) == "timeout")
    check("None / 空串不许炸，落到「未知」",
          mex.classify(None) == mex.UNKNOWN_KIND and mex.classify("") == mex.UNKNOWN_KIND)

    # 鉴权判定与分类不许各说各话：一条错误只能有一个方向
    for text, _want in cases:
        check(f"「要不要重新授权」与类别口径一致：{text[:26]}",
              mex.needs_reauth(RuntimeError(text)) == (mex.classify(text) in mex.REAUTH_KINDS),
              f"needs_reauth={mex.needs_reauth(RuntimeError(text))} kind={mex.classify(text)}")

    # 原话必须留在文案里：翻译可能不准，用户和服务商对话靠的是那句原文
    note = mex.explain(RuntimeError("Error POSTing to endpoint (HTTP 401): Unauthorized"))
    check("中文之后仍附服务方原话（不吞证据）",
          "401" in note and "Unauthorized" in note and "重新授权" in note, note)
    check("下一步直接指向动作（不写「未知错误」糊弄）",
          "桌面端" in mex.advice("auth") or "令牌" in mex.advice("auth"), mex.advice("auth"))
    check("认不出来时明说「原因见服务方原话」，不硬套类别",
          "原因见服务方原话" in mex.explain(RuntimeError("something the vendor invented")),
          mex.explain(RuntimeError("something the vendor invented")))
    check("没有原话可附时不硬留一个空尾巴",
          mex.explain(TimeoutError()).endswith("）") and not mex.explain(TimeoutError()).endswith("："),
          mex.explain(TimeoutError()))
    check("超长原话压到一行且带省略号",
          "\n" not in mex.explain(RuntimeError(" boom " * 300))
          and mex.explain(RuntimeError(" boom " * 300)).endswith("…"),
          str(len(mex.explain(RuntimeError(" boom " * 300)))))
    check("映射表里没有配置原值的位置（只吃异常文本）",
          mex.explain(RuntimeError("upstream said no"))
          == f"{mex.describe(mex.classify('upstream said no'))}｜服务方原话：upstream said no",
          mex.explain(RuntimeError("upstream said no")))

    # 真实链路：连不上的连接器摊出来的那句要带中文类别，而不是一串英文堆栈
    async def scenario() -> None:
        toolkit = Toolkit()
        bad = await mr.attach(toolkit, {"mcp": [
            {"serverId": "err-map", "name": "命令不存在", "transport": "stdio",
             "command": "no-such-binary-zzz", "args": []},
        ]})
        hit = next((n for n in bad["notes"] if "命令不存在" in n), "")
        check("真实失败连上中文类别那句话（不是只给用户一句英文）",
              bool(hit) and ("启动命令找不到" in hit or "原因见服务方原话" in hit), hit[:220])
        check("失败说明仍然是一行（翻译没把它撑成一段）",
              all("\n" not in n and len(n) < 300 for n in bad["notes"]),
              str([len(n) for n in bad["notes"]]))
        check("翻译过的失败仍然没留下待收尾客户端", bad["clients"] == [], str(bad["clients"]))

    _run(scenario())
    check("批量分类只走纯函数（不碰任何连接）",
          mex.kinds_matching(["429 too many requests", "connection refused"]) ==
          {"429 too many requests": "rate_limited", "connection refused": "refused"},
          str(mex.kinds_matching(["429 too many requests", "connection refused"])))


def lane_checks() -> None:
    print("--- 6. connect 车道的五个只算不连端点（D-39 界面读路径） ---")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routes_connect as rc
        import routes_lanes as rl
    except Exception as exc:  # noqa: BLE001
        check("车道端点可导入（FastAPI 缺失时这一族不跑）", False, f"{type(exc).__name__}: {exc}")
        return

    class _StubHub:
        """只替引擎回答「此刻有什么连接」，本身不连任何东西"""

        def health(self):
            return [
                {"serverId": "a", "name": "连着的那个", "state": "live", "kind": "live",
                 "hint": "常驻中", "toolCount": 3, "failures": 0},
                {"serverId": "b", "name": "退避的那个", "state": "blocked", "kind": "refused",
                 "hint": "端口没人听", "failures": 2, "retryInMs": 4000, "reason": "connection refused"},
            ]

        def snapshot(self):
            return {"counters": {"acquires": 7, "backoffBlocks": 3}, "lastError": ""}

    class _DesktopEngine:
        MCP_HUB = _StubHub()

        @staticmethod
        def _is_desktop_local(request):
            return True

    class _RemoteEngine:
        """没有本机令牌、却带着远程身份的调用方：经手凭据形状的一族必须 403"""

        MCP_HUB = _StubHub()

        @staticmethod
        def _is_desktop_local(request):
            return False

        @staticmethod
        def current_principal():
            return {"sub": "someone-else"}

    app = FastAPI()
    app.include_router(rc.router)
    rl.bind(_DesktopEngine())
    client = TestClient(app, base_url="http://127.0.0.1")

    check("车道路径与登记同源（自测不抄第二份字符串）",
          set(rc.CONNECT_ENDPOINT_PATHS) == {r.path for r in rc.router.routes},
          str(sorted({r.path for r in rc.router.routes})))
    check("前缀不越界（routes_lanes 的登记表认这一族）",
          all(any(r.path.startswith(p) for p in rl.LANE_PREFIXES["connect"]) for r in rc.router.routes))

    src = (PROJECT_ROOT / "engine" / "routes_connect.py").read_text(encoding="utf-8")
    check("这一族「只算不连」：源码里没有任何建立连接的动作",
          "build_client" not in src and ".connect()" not in src and "McpSessionHub(" not in src,
          "出现 build_client / connect() 就是拿端点冒充连接事实")

    listing = client.get("/api/mcp/connectors").json()
    check("目录至少 8 条且覆盖办公/知识/协作（D-39 方案第 1 条）",
          listing.get("ok") is True and listing.get("total") >= 8
          and {"办公", "知识", "协作"} <= {r["category"] for r in listing["rows"]},
          str(listing.get("total")))
    check("每条都带文档链接与中文说明（用户不用猜这条是从哪抄的）",
          all(r["docs"] and r["summary"] and r["verified"] in ("doc", "unverified")
              for r in listing["rows"]),
          str([r["id"] for r in listing["rows"] if not r["docs"] or not r["summary"]]))
    check("绝大多数条目带建议白名单（少放比多放安全）",
          sum(1 for r in listing["rows"] if r["suggestedAllowlist"]) >= 8,
          str([r["id"] for r in listing["rows"] if not r["suggestedAllowlist"]]))
    gateway = next((r for r in listing["rows"] if r["mode"] == "gateway"), {})
    check("gateway 那条的白名单按上游给（目录不给具体建议，但必须带 gatewayNote）",
          gateway.get("suggestedAllowlist") == [] and bool(gateway.get("gatewayNote")), str(gateway)[:200])
    check("零字段的条目是真的免配置（不是漏了表单：目录自测已钉住占位符与字段一一对应）",
          all(r["fields"] or r["auth"] in ("none", "oauth_pkce") for r in listing["rows"]),
          str([r["id"] for r in listing["rows"] if not r["fields"] and r["auth"] not in ("none", "oauth_pkce")]))
    check("核验口径只有 doc / unverified 两种，且未核验的摊出来",
          all(r["verified"] in ("doc", "unverified") for r in listing["rows"])
          and bool(listing["unverifiedIds"]) and "未联网核验" in listing["unverifiedNote"],
          str(listing["unverifiedIds"]))
    check("目录不下发命令/URL 模板（半截 {占位符} 会被看成坏配置）",
          all(k not in r for r in listing["rows"] for k in ("command", "args", "url", "headers")),
          str(sorted(listing["rows"][0])))
    only_office = client.get("/api/mcp/connectors", params={"category": "办公"}).json()
    check("按分类过滤只给该分类（未知分类如实拒而不是假装全给）",
          only_office.get("ok") is True and only_office["rows"]
          and all(r["category"] == "办公" for r in only_office["rows"]), str(only_office)[:160])
    bogus = client.get("/api/mcp/connectors", params={"category": "不存在"}).json()
    check("瞎写的分类给 200 + ok=false + 可选分类",
          bogus.get("ok") is False and bogus.get("code") == "MCP_CATALOG_EMPTY_CATEGORY"
          and bogus.get("categories"), str(bogus)[:160])

    missing = client.post("/api/mcp/connectors/spec", json={"connectorId": "slack", "values": {}}).json()
    check("缺必填字段回 200 + 中文缺什么（表单就地提示，不是 500）",
          missing.get("ok") is False and missing.get("code") == "MCP_CONNECTOR_MISSING_FIELD"
          and "还缺" in missing.get("message", ""), str(missing)[:200])
    unknown = client.post("/api/mcp/connectors/spec", json={"connectorId": "不存在的连接器"}).json()
    check("目录里没有的 id 是调用方传错，回 400 并指回 GET",
          unknown.get("ok") is False and unknown.get("code") == "MCP_CONNECTOR_UNKNOWN", str(unknown)[:160])
    built = client.post(
        "/api/mcp/connectors/spec",
        json={"connectorId": "context7", "values": {"apiKey": "k-123"}, "serverId": "ctx"},
    ).json()
    check("填完字段就出可用 spec（含注入的 env 与 serverId）",
          built.get("ok") is True and built["spec"]["env"].get("CONTEXT7_API_KEY") == "k-123"
          and built["spec"]["serverId"] == "ctx", str(built)[:200])
    check("spec 里带上 catalogId（对账与界面都认这一个标识）",
          built.get("ok") is True and built["spec"].get("catalogId") == "context7", str(built)[:160])
    no_id = client.post("/api/mcp/connectors/spec", json={"values": {}}).json()
    check("没带 connectorId 时不猜默认值",
          no_id.get("code") == "MCP_CONNECTOR_ID_MISSING", str(no_id)[:120])

    preview = client.post(
        "/api/mcp/allowlist/preview",
        json={"allowedTools": ["env_*"], "tools": ["echo", "now_word", "env_probe"]},
    ).json()
    check("预览把通配展开成真实工具名并给中文数量",
          preview.get("kept") == ["env_probe"] and "放行 1/3" in preview.get("note", ""), str(preview)[:200])
    all_off = client.post("/api/mcp/allowlist/preview", json={"allowedTools": []}).json()
    check("显式全关在预览里就说一条都不放行",
          all_off.get("unrestricted") is False and all_off.get("kept") == []
          and "一条都没放行" in all_off.get("note", ""), str(all_off)[:200])
    loose = client.post("/api/mcp/allowlist/preview", json={}).json()
    check("没设白名单时标 unrestricted（不伪装成「全关」）",
          loose.get("unrestricted") is True and "全量可见" in loose.get("note", ""), str(loose)[:200])
    no_tools = client.post(
        "/api/mcp/allowlist/preview", json={"allowedTools": ["crm/*"]}
    ).json()
    check("没有工具清单时如实说展不开（不报 0 个假装算过）",
          "没给工具清单" in no_tools.get("note", "") and no_tools.get("kept") == [], str(no_tools)[:200])

    recon = client.post(
        "/api/mcp/reconcile",
        json={"servers": [built["spec"], {"serverId": "hand", "command": "my-own-server"}]},
    ).json()
    check("对账把目录命中的与手工配置分开报（手工不是坏东西）",
          recon.get("ok") is True and any(r.get("matched") for r in recon["rows"])
          and any(r.get("source") == "manual" for r in recon["rows"]) and recon["manual"] >= 1,
          str(recon)[:200])
    empty_recon = client.post("/api/mcp/reconcile", json={}).json()
    check("没带清单时给 200 + 中文说明要什么",
          empty_recon.get("ok") is False and empty_recon.get("code") == "MCP_RECONCILE_NO_ROWS",
          str(empty_recon)[:160])

    health = client.get("/api/mcp/health").json()
    check("健康面每连接器一行并带计数（顺序由枢纽定，这一族不改）",
          health.get("available") is True and health.get("blocked") == 1
          and {r["state"] for r in health["rows"]} == {"live", "blocked"}
          and health.get("counters", {}).get("acquires") == 7, str(health)[:200])
    check("端点把枢纽给的行原样转发行序（不自己重排，退避优先由枢纽负责）",
          [r["state"] for r in health["rows"]] == ["live", "blocked"], str(health)[:160])
    check("健康面把错误类别枚举一起给（界面图标不用自己抄表）",
          "refused" in health.get("errorKinds", []) and "auth" in health.get("errorKinds", []),
          str(health.get("errorKinds")))
    rl.bind(type("NoHub", (), {})())
    no_hub = client.get("/api/mcp/health").json()
    check("本进程没有常驻枢纽时如实说「不是连接器坏了」",
          no_hub.get("available") is False and "不是连接器坏了" in no_hub.get("note", ""),
          str(no_hub)[:200])

    rl.bind(_RemoteEngine())
    remote_spec = client.post("/api/mcp/connectors/spec", json={"connectorId": "context7"})
    check("带远程身份的调用方不能生成 spec（403，凭据形状只给桌面壳）",
          remote_spec.status_code == 403
          and remote_spec.json().get("code") == "MCP_DESKTOP_ONLY", str(remote_spec.text)[:200])
    check("远程身份仍可只读目录（不含任何凭据，没有理由挡）",
          client.get("/api/mcp/connectors").status_code == 200)
    check("远程身份也仍能读健康面（读数不是写动作）",
          client.get("/api/mcp/health").status_code == 200)
    # 车道绑定是进程级全局：这一族借它验鉴权，收尾必须交回真引擎模块，
    # 否则后面的端到端分组会拿到一个假引擎（其它车道的读路径跟着变形）
    if engine_server is not None:
        rl.bind(engine_server)


def main() -> None:
    pure_checks()
    attach_checks()
    allowlist_checks()
    error_mapping_checks()
    lane_checks()
    if engine_server is not None:
        http_checks()
    else:
        print("SKIP: 端到端分组未运行（依赖缺失）")
    print("-" * 52)
    if FAILURES:
        print(f"engine mcp selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine mcp selftest: ALL PASS")


if __name__ == "__main__":
    main()
