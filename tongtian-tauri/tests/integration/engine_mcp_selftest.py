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

离线运行：夹具是本仓库内的 MCP stdio 服务器，不联网、无需 API Key。
agentscope 或 mcp 缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_mcp_selftest.py
"""

import asyncio
import json
import sys
import tempfile
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
        check("空清单不产生噪声说明", empty == {"notes": [], "clients": []}, str(empty))

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

        # 按需载入模式（设置页默认）：未搜索前工具不在 schema，search_tools 命中后可用
        RECORDED.clear()
        engine_server.build_model_and_formatter = (
            lambda *a, **k: (LazyToolCallingModel(), DashScopeChatFormatter())
        )
        lazy = client.post("/api/chat", json={
            "message": "用回声工具确认华东这行数据",
            "task_id": "mcp-lazy-e2e",
            "runtime": {"mcp": [stdio_spec()]},
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


def main() -> None:
    pure_checks()
    attach_checks()
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
