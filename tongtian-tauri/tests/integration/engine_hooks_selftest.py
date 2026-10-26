#!/usr/bin/env python3
"""
用户 Hooks 进任务生命周期自测（能力台账 P0-3，G6）

锁住「Hooks 页配置的事件真的在任务里触发、PreToolUse 真的能拦住工具」这条链，
防止再退回「12 类事件只是配置和试运行，任务跑起来一个都不触发」：
1. runtime["hooks"] 的取值口径（没带键、读表失败、段被转成字符串、没通道、没放行）
2. 「没能触发」必须变成可见说明，而不是静少；说明条数有上限，不淹没事件流
3. 中间件挂进真实 Toolkit：PreToolUse 阻断时工具**不执行**、PostToolUse 带工具结果、
   失败结果转 PostToolUseFailure、门禁没装上必须可见
4. /api/chat 端到端（假网关走真实 HTTP）：主链路事件真的上报，带 token，
   阻断时文件没被写出来但本轮仍然正常收尾；网关失联只摊说明，不把任务打成 error
   （SubagentStart/SubagentStop 由 engine_subagent_selftest.py 覆盖）

离线运行：不联网、无需 API Key；网关用本机 127.0.0.1 随机端口的假服务，
不触碰用户数据目录（WORK_DIR 换成临时目录）。
agentscope 或 fastapi 缺失时对应分组输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_hooks_selftest.py
"""

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 控制台默认 cp1250/gbk，中文说明会直接把 print 打崩（不是被测代码的问题）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import hooks_runtime as hr
    from agentscope.tool import Toolkit, ToolResponse
    from agentscope.message import TextBlock, ToolUseBlock
except Exception as exc:
    print(f"SKIP: hooks runtime selftest unavailable ({exc})")
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

FAILURES: list = []
RECORDED: list = []

GATE_URL = "http://127.0.0.1:9/hook"
GATE_TOKEN = "selftest-token"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def gate_block(**extra) -> dict:
    block = {"gateUrl": GATE_URL, "token": GATE_TOKEN, "configured": 1}
    block.update(extra)
    return block


def notes_of(block) -> list[str]:
    return hr.HookGateway(block if isinstance(block, dict) else None).notes()


def events_of(notes: list[str], needle: str) -> bool:
    return any(needle in n for n in notes)


# ── 1. 纯函数层：取值口径与可见说明 ────────────────────────────

def pure_checks() -> None:
    print("--- 1. runtime['hooks'] 取值口径（纯函数层） ---")

    gw = hr.from_runtime(None)
    check("没带 hooks 段：不启用也不制造说明",
          not gw.enabled and gw.notes() == [], str(gw.notes()))

    gw = hr.from_runtime({"hooks": {"configured": 0}})
    check("没配脚本：不启用（每次工具调用都问网关是白给的开销）",
          not gw.enabled and gw.notes() == [], str(gw.notes()))

    notes = notes_of({"error": "HOOK_CONFIG_QUERY_FAILED: 表被占用"})
    check("读表失败：说清「本轮不会触发任何脚本」并到此为止",
          events_of(notes, "读取失败") and "不会触发任何脚本" in notes[0] and len(notes) == 1,
          str(notes))

    notes = notes_of({"configured": 3, "gateError": "端口占用"})
    check("配了脚本但没有本机执行通道：条数与原因都摊出来",
          events_of(notes, "没有本机 Hook 执行通道") and "3 条" in notes[0]
          and "端口占用" in notes[0], str(notes))

    notes = notes_of(gate_block(ungranted=["guard.sh", "check.py", "notify.ps1", "deep.bat"]))
    check("未放行的命令逐条点名（超过 3 条折叠计数）",
          events_of(notes, "尚未放行") and "guard.sh" in notes[0] and "deep.bat" not in notes[0]
          and "等 4 条" in notes[0], str(notes))

    notes = notes_of(gate_block(mountedUnfired=["Notification", "PermissionRequest"]))
    check("引擎没有触发点的事件：明说「不会执行」而不是静默",
          events_of(notes, "没有触发点") and "Notification" in notes[-1], str(notes))

    notes = notes_of(gate_block(configured=2))
    check("通道可用且已放行：没有额外说明（不写「Hook 全部生效」这种空话）",
          notes == [], str(notes))

    strung = hr.from_runtime({"hooks": json.dumps(gate_block())})
    check("hooks 段被转成 JSON 字符串照样能用（与 mcp 段同口径）",
          strung.enabled and strung.token == GATE_TOKEN, str(vars(strung)))

    raw = hr.from_runtime({"hooks": "网关未启动"})
    check("非 JSON 字符串不当成「没配置」，而是报错",
          raw.configured == 0 and raw.error == "网关未启动"
          and events_of(raw.notes(), "读取失败"),
          f"{raw.error} / {raw.notes()}")

    bad = hr.from_runtime({"hooks": {"configured": "三"}})
    check("configured 不是数字：按 0 处理而不是抛异常",
          bad.configured == 0 and not bad.enabled, str(bad.configured))

    check("失败前缀判定只认 AgentScope 已知的几种",
          all(hr.is_failure(p + " whatever") for p in
              ("Error:", "Error occurred when calling MCP tool:", "FunctionNotFoundError",
               "FunctionInactiveError", "<system-info>"))
          and not hr.is_failure("写入成功"), "")

    resp = ToolResponse(content=[TextBlock(type="text", text="第一行")])

    class _Blockish:
        text = "第二行"

    joined = hr.response_text([resp, ToolResponse(content=[_Blockish(), {"type": "text"}])])
    check("工具结果摊平成文本（PostToolUse 的 tool_response 字段）",
          joined == "第一行\n第二行", joined)

    closed = hr.HookGateway({"configured": 2})
    calls: list = []
    original_post = hr.httpx.post
    hr.httpx.post = lambda *a, **k: calls.append(a)  # type: ignore[assignment]
    try:
        out = closed.fire("PreToolUse", "write_text_file", {})
    finally:
        hr.httpx.post = original_post  # type: ignore[assignment]
    check("网关未启用时 fire 直接返回，不发任何请求",
          calls == [] and out == {"blocked": False, "notes": [], "results": []}, str(out))


# ── 2. 中间件层：真实 Toolkit + 假网关 ─────────────────────────

class StubGateway:
    """只暴露中间件用到的两个字段，把网关往返与断言解耦"""

    def __init__(self, results=None, enabled=True):
        self.enabled = enabled
        self.calls: list = []
        self.results = results or {}

    def fire(self, event, tool=None, payload=None, task_id=""):
        self.calls.append((event, tool, payload))
        return self.results.get(event, {"blocked": False, "notes": [], "results": []})


EXECUTED: list = []


def probe_tool(path: str = "") -> ToolResponse:
    EXECUTED.append("probe")
    return ToolResponse(content=[TextBlock(type="text", text="probe:ok")])


def failing_tool(path: str = "") -> ToolResponse:
    EXECUTED.append("failing")
    return ToolResponse(content=[TextBlock(type="text", text="Error: 磁盘只读")])


def _tool_call(name: str) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=f"call-{name}", name=name,
                        input={"path": "reports/east.md"})


async def _collect(toolkit: Toolkit, name: str) -> str:
    out = []
    async for chunk in await toolkit.call_tool_function(_tool_call(name)):
        out.append(hr.response_text([chunk]))
    return "\n".join(out)


def middleware_checks() -> None:
    print("--- 2. 中间件层（真实 Toolkit + 假网关） ---")

    def build(gw: StubGateway) -> Toolkit:
        tk = Toolkit()
        tk.register_tool_function(probe_tool, func_name="probe")
        tk.register_tool_function(failing_tool, func_name="failing")
        emitted: list = []
        runner = hr.HookRunner(gw, lambda ev, data: emitted.append((ev, data)),
                               task_id="t-1", cwd="d:/tmp/tt")
        assert hr.install(tk, runner)
        return tk

    EXECUTED.clear()
    gw = StubGateway()
    tk = build(gw)
    text = asyncio.run(_collect(tk, "probe"))
    check("放行且未阻断：工具照常执行，前后各上报一次",
          EXECUTED == ["probe"] and "probe:ok" in text
          and [c[0] for c in gw.calls] == ["PreToolUse", "PostToolUse"],
          str([c[0] for c in gw.calls]))
    pre = gw.calls[0][2]
    check("PreToolUse 带工具名与入参（matcher 与脚本要用）",
          gw.calls[0][1] == "probe" and pre["toolInput"]["path"] == "reports/east.md"
          and pre["toolUseId"] == "call-probe", str(pre))
    check("事件输入补 sessionId/cwd（脚本读得到任务与目录）",
          pre["sessionId"] == "t-1" and pre["cwd"] == "d:/tmp/tt", str(pre))
    check("PostToolUse 带工具结果文本",
          "probe:ok" in gw.calls[1][2]["tool_response"], str(gw.calls[1][2]))

    EXECUTED.clear()
    blocked = StubGateway(results={"PreToolUse": {
        "blocked": True,
        "notes": ["Hook「PreToolUse」阻断了工具调用「probe」（exit 2）：只允许写 reports 目录"],
        "results": [],
    }})
    tk = build(blocked)
    text = asyncio.run(_collect(tk, "probe"))
    check("PreToolUse 阻断：工具一次都没执行", EXECUTED == [], str(EXECUTED))
    check("阻断原因回到模型上下文（不是静默返回空）",
          "被用户配置的 PreToolUse Hook 阻断" in text and "只允许写 reports 目录" in text,
          text[:200])
    check("被阻断的调用不再上报 PostToolUse",
          [c[0] for c in blocked.calls] == ["PreToolUse"], str([c[0] for c in blocked.calls]))

    EXECUTED.clear()
    gw = StubGateway()
    tk = build(gw)
    text = asyncio.run(_collect(tk, "failing"))
    check("工具失败转 PostToolUseFailure（脚本能区分成败）",
          [c[0] for c in gw.calls] == ["PreToolUse", "PostToolUseFailure"]
          and "磁盘只读" in gw.calls[1][2]["tool_response"], str([c[0] for c in gw.calls]))

    EXECUTED.clear()
    gw = StubGateway(enabled=False)
    tk = build(gw)
    text = asyncio.run(_collect(tk, "probe"))
    check("没放行/没通道：一个事件都不上报，工具不受影响",
          EXECUTED == ["probe"] and gw.calls == [] and "probe:ok" in text, str(gw.calls))

    emitted: list = []
    runner = hr.HookRunner(StubGateway(), lambda ev, data: emitted.append(data), task_id="t")
    broken = object()  # 没有 register_middleware：装不上必须可见
    ok = hr.install(broken, runner)  # type: ignore[arg-type]
    check("门禁装不上时报错而不是静默放行工具调用",
          ok is False and any("PreToolUse 门禁没有装上" in str(d.get("description", ""))
                              for d in emitted), str(emitted))

    emitted.clear()
    runner = hr.HookRunner(StubGateway(enabled=False),
                           lambda ev, data: emitted.append(data), task_id="t")
    runner.surface([f"说明{i}" for i in range(hr.MAX_HOOK_NOTES + 6)])
    check("说明条数封顶（脚本噪音不淹没事件流）",
          len(emitted) == 2 * hr.MAX_HOOK_NOTES, str(len(emitted)))


# ── 3. /api/chat 端到端（假网关走真实 HTTP）───────────────────

_GATE: dict = {"calls": [], "block_pre": False, "token": GATE_TOKEN}


class _FakeGateHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        token = self.headers.get("x-tt-hook-token")
        _GATE["calls"].append({"path": self.path, "token": token, "body": body})
        if token != _GATE["token"]:
            self._reply(401, {"ok": False, "error": "HOOK_GATE_UNAUTHORIZED"})
            return
        result = {"event": body.get("event"), "results": [
            {"command": "guard.sh", "executed": True, "granted": True,
             "exitCode": 2 if _GATE["block_pre"] and body.get("event") == "PreToolUse" else 0,
             "stdout": "", "stderr": "", "blocked": False, "timedOut": False},
        ], "blocked": False, "notes": []}
        if body.get("event") == "PreToolUse" and _GATE["block_pre"]:
            result["blocked"] = True
            result["notes"] = ["Hook「PreToolUse」阻断了工具调用「write_text_file」（exit 2）：禁止写盘"]
        self._reply(200, {"ok": True, "result": result})

    def _reply(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # 静音，别把自测输出搅乱
        pass


class WriteFileModel(ChatModelBase):
    """第一轮强制写文件，第二轮收尾；用于验证 PreToolUse 真的卡在工具执行前"""

    def __init__(self, target: str):
        super().__init__(model_name="fake-hook-model", stream=False)
        self.target = target
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        try:
            RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        except Exception as exc:
            RECORDED.append(f"<unserializable:{exc}>")
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[{
                "type": "tool_use", "id": "call-write-1", "name": "write_text_file",
                "input": {"file_path": self.target, "content": "华东 2026Q3"},
            }])
        return ChatResponse(content=[{"type": "text", "text": "这轮按 Hook 规则处理完了。"}])


def _start_gate() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/hook"


def _reset_gate(block_pre: bool) -> None:
    _GATE["calls"].clear()
    _GATE["block_pre"] = block_pre
    RECORDED.clear()


def _chat(client, task_id: str, hooks: dict, **extra) -> str:
    return client.post("/api/chat", json={
        "message": "把华东这行数据写进报表",
        "task_id": task_id,
        "runtime": {"hooks": hooks},
        **extra,
    }).text


def http_checks() -> None:
    print("--- 3. /api/chat 端到端（假网关真实 HTTP 往返） ---")
    srv, url = _start_gate()
    work_dir = Path(tempfile.mkdtemp(prefix="tt-hooks-http-"))
    target = work_dir / "east.md"
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (WriteFileModel(str(target)), DashScopeChatFormatter())
    )
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    try:
        _reset_gate(False)
        resp = _chat(client, "hooks-run", {"gateUrl": url, "token": GATE_TOKEN, "configured": 2})
        got = [c["body"].get("event") for c in _GATE["calls"]]
        check("执行流正常收尾", "event: done" in resp and "event: error" not in resp,
              resp[:200])
        check("有触发点的事件真的上报（会话/提交/前后置/停止/结束）",
              got[:1] == ["SessionStart"] and "UserPromptSubmit" in got
              and "PreToolUse" in got and "PostToolUse" in got
              and "Stop" in got and got[-1] == "SessionEnd", str(got))
        check("引擎每个事件都带网关 token（否则网关该 401）",
              all(c["token"] == GATE_TOKEN for c in _GATE["calls"]) and _GATE["calls"] != [],
              str([c["token"] for c in _GATE["calls"]][:3]))
        pre = next(c["body"] for c in _GATE["calls"] if c["body"]["event"] == "PreToolUse")
        check("PreToolUse 带工具名与 file_path 入参（matcher 用得上）",
              pre["tool"] == "write_text_file"
              and pre["payload"]["toolInput"]["file_path"].endswith("east.md"), str(pre))
        post = next(c["body"] for c in _GATE["calls"] if c["body"]["event"] == "PostToolUse")
        check("PostToolUse 带真实工具结果", "east.md" in post["payload"]["tool_response"],
              str(post["payload"]))
        check("未被阻断时文件真的写出来", target.exists(), str(target))
        check("网关没话讲时不往事件里塞 Hook 噪音",
              '"stepType": "hook"' not in resp, resp[:300])
        check("SessionEnd 说明本轮怎么结束的",
              _GATE["calls"][-1]["body"]["payload"]["reason"] == "completed",
              str(_GATE["calls"][-1]["body"]["payload"]))
        check("历史没被截断时不报 PreCompact", "PreCompact" not in got, str(got))

        _reset_gate(True)
        target.unlink()
        resp = _chat(client, "hooks-block", {"gateUrl": url, "token": GATE_TOKEN, "configured": 2})
        got = [c["body"].get("event") for c in _GATE["calls"]]
        check("阻断：PreToolUse 之后没有 PostToolUse",
              "PreToolUse" in got and "PostToolUse" not in got, str(got))
        check("阻断：脚本 exit 2 之后文件没被写出来", not target.exists(), str(target))
        check("阻断：本轮仍然正常收尾（阻断是业务规则，不是故障）",
              "event: done" in resp and "event: error" not in resp, resp[-300:])
        blob = "\n".join(RECORDED)
        check("阻断原因交回模型上下文", "PreToolUse Hook 阻断" in blob and "禁止写盘" in blob,
              blob[-400:])
        check("Hook 的说明进事件流（可见，不只写日志）",
              '"stepType": "hook"' in resp and "禁止写盘" in resp, resp[-400:])

        _reset_gate(False)
        resp = _chat(client, "hooks-dead",
                     {"gateUrl": "http://127.0.0.1:9/hook", "token": GATE_TOKEN, "configured": 2})
        check("网关失联：只摊说明，不把任务打成 error",
              "event: done" in resp and "event: error" not in resp
              and "本机网关未响应" in resp, resp[:300])
        check("网关失联不影响工具执行（文件写出来了）", target.exists(), str(target))

        _reset_gate(False)
        resp = _chat(client, "hooks-ungranted",
                     {"gateUrl": url, "token": GATE_TOKEN, "configured": 1,
                      "ungranted": ["guard.sh"]})
        got = [c["body"].get("event") for c in _GATE["calls"]]
        check("还没放行的命令：开跑前就点名（事件照样上报，由网关回答「没执行」）",
              "尚未放行" in resp and "guard.sh" in resp and got != [], str(_GATE["calls"][:1]))

        _reset_gate(False)
        resp = _chat(client, "hooks-401",
                     {"gateUrl": url, "token": "wrong-token", "configured": 2})
        check("网关拒绝（token 不匹配）：摊成失败说明，任务照常收尾",
              "event: done" in resp and "event: error" not in resp
              and "执行失败" in resp, resp[:300])

        _reset_gate(False)
        long_history = [{"role": "user" if i % 2 else "assistant", "content": f"第 {i} 轮"}
                        for i in range(1, 26)]
        _chat(client, "hooks-compact",
              {"gateUrl": url, "token": GATE_TOKEN, "configured": 2},
              history=long_history)
        compact = [c["body"] for c in _GATE["calls"] if c["body"].get("event") == "PreCompact"]
        check("上下文真的被截断：PreCompact 上报并带上保留条数",
              len(compact) == 1 and compact[0]["payload"]["trigger"] == "auto"
              and compact[0]["payload"]["kept"] == 20, str(compact))
    finally:
        srv.shutdown()
        engine_server.WORK_DIR = Path(".")


def main() -> None:
    pure_checks()
    middleware_checks()
    if engine_server is not None:
        http_checks()
    else:
        print("SKIP: 端到端分组未运行（依赖缺失）")
    print("-" * 52)
    if FAILURES:
        print(f"engine hooks selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine hooks selftest: ALL PASS")


if __name__ == "__main__":
    main()
