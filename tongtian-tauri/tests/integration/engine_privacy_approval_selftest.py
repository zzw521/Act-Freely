#!/usr/bin/env python3
"""PII 出站脱敏 + 逐工具审批自测

锁住这些不能退让的约束：
1. 脱敏规则与 Rust classify 同源：身份证、手机号、银行卡、邮箱、sk- 密钥命中即掩码，
   普通文本不动；block 档命中直接中止模型调用，不能回退兜底计划假装成功；
2. 真正处理 AgentScope 的 Msg 对象（只换 content/metadata 的副本，不改原消息）；
   图片块原样保留；命中计数能回到事件流；
3. 逐工具审批档位：off 不装中间件；sensitive 跳过只读工具与已有逐操作确认的
   浏览器/电脑操控；未登记工具（含 MCP）按敏感处理；all 除自有确认外全问；
4. 用户拒绝 → 工具不执行并把拒绝原因回给模型；一次拒绝不会毒化后续调用
   （曾经把 403 当网关故障，之后所有调用都被自动拒绝）；
5. 网关连不上/非回环/缺 token/档位非法 → fail-closed，原因可读，不静默放行；
6. 摘要只带路径与长度，命令/URL 里的手机号与密钥在弹框前脱敏；
7. /api/chat 对未知 piiOutboundMode 返回 400，不静默按关闭处理。

离线运行：假网关走 127.0.0.1 随机端口，不联网、不弹真实确认框。
fastapi/agentscope 缺失时 SKIP 并以 0 退出。

用法: python tests/integration/engine_privacy_approval_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 引擎模块导入前隔离数据目录，避免自测触碰用户会话/设置
_TMP_HOME = tempfile.mkdtemp(prefix="tt-pii-approval-")
os.environ["TONGTIANWORK_HOME"] = _TMP_HOME
os.environ["WORK_DIR"] = str(Path(_TMP_HOME) / "work")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import privacy_guard as pg
    import tool_approval as tap
    from agentscope.message import Msg
    from agentscope.tool import ToolResponse
    from agentscope.message import TextBlock
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: engine privacy/approval selftest unavailable ({exc})")
    sys.exit(0)

FAILURES: list[str] = []
GATE_TOKEN = "selftest-approval-token"
PHONE = "13800138000"
ID_CARD = "11010119900307123X"
BANK = "6222020200112233445"
EMAIL = "alice@example.com"
SECRET = "sk-abcdefghijklmn"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


# ── 1. 脱敏规则 ──────────────────────────────────────────────────

def test_redaction_rules() -> None:
    raw = f"身份证 {ID_CARD} 手机 {PHONE} 卡号 {BANK} 邮箱 {EMAIL} 密钥 {SECRET}"
    clean, counts = pg.redact_with_counts(raw)
    check("五类 PII/密钥全部命中并计数",
          all(counts.get(k) == 1 for k in ("idCard", "phone", "bankCard", "email", "secret")),
          f"counts={counts}")
    for value in (ID_CARD, PHONE, BANK, EMAIL, SECRET):
        check(f"原文不残留：{value[:6]}…", value not in clean, clean)
    check("掩码保留首尾可辨认片段",
          "身份证 110" in clean and "3X" in clean and "138" in clean and clean.count("*") >= 20,
          clean)

    plain = "2026 年 10 月 22 日，版本 1.2.3，任务号 42"
    check("普通文本不被误伤", pg.redact(plain) == plain, pg.redact(plain))

    spaced = "卡号 6222 0202 0011 2233 445"
    check("空格分组的银行卡仍能识别", "bankCard" in pg.redact_with_counts(spaced)[1])

    check("非法档位归一为 off", pg.normalize_mode("evil") == "off")
    check("runtime.privacy.mode 优先于提交档位",
          pg.from_runtime({"privacy": {"mode": "block"}}, "mask") == "block")
    check("无 runtime 时使用提交档位", pg.from_runtime(None, "mask") == "mask")
    check("runtime 错误可读",
          pg.runtime_error({"privacy": {"mode": "off", "error": "PII_MODE_INVALID: 非法"}})
          == "PII_MODE_INVALID: 非法")


class RecordingModel:
    """记录真正发给模型的入参；不实现真实调用。"""

    def __init__(self) -> None:
        self.stream = False
        self.seen: list = []
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        self.seen.append((args, kwargs))
        return {"ok": True}

    @property
    def model_name(self) -> str:
        return "recording-model"


def _seen_text(model: RecordingModel) -> str:
    return json.dumps(model.seen, ensure_ascii=False, default=str)


def test_redacting_model() -> None:
    async def run() -> None:
        inner = RecordingModel()
        hits: list[dict] = []
        wrapped = pg.wrap_model(inner, "mask", on_redaction=hits.append)
        msg = Msg(
            name="user",
            content=[TextBlock(type="text", text=f"帮我转给 {PHONE}，密钥 {SECRET}")],
            role="user",
        )
        before = json.dumps(msg.content, ensure_ascii=False)
        await wrapped([msg], tools=[{"type": "function", "function": {"name": "x"}}])
        seen = _seen_text(inner)
        check("Msg 内容真的被脱敏（不是只处理 dict）",
              PHONE not in seen and SECRET not in seen, seen)
        check("原 Msg 不被改写（检查点/历史仍拿原文）",
              json.dumps(msg.content, ensure_ascii=False) == before)
        check("命中计数通过回调上报", bool(hits) and hits[0].get("phone") == 1,
              f"hits={hits}")

        image = Msg(
            name="user",
            content=[{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": PHONE},
            }],
            role="user",
        )
        inner2 = RecordingModel()
        await pg.wrap_model(inner2, "mask")([image])
        check("图片 base64 原样保留（无法检测，不假装脱敏）",
              PHONE in _seen_text(inner2))

        inner3 = RecordingModel()
        blocked = None
        try:
            await pg.wrap_model(inner3, "block")([msg])
        except pg.PrivacyBlocked as exc:
            blocked = exc
        check("block 档命中即中止且模型未被调用",
              blocked is not None and inner3.calls == 0
              and "PRIVACY_OUTBOUND_BLOCKED" in str(blocked), str(blocked))

        model = RecordingModel()
        check("off 档不包装模型（保持既有行为）", pg.wrap_model(model, "off") is model)

    asyncio.run(run())


# ── 2. 审批档位与摘要 ────────────────────────────────────────────

def test_approval_policy() -> None:
    check("off 不问", not tap.needs_approval("run_command", "off"))
    for name in ("write_text_file", "run_command", "fetch_url", "spawn_subagents", "mcp_unknown"):
        check(f"sensitive 询问副作用工具 {name}", tap.needs_approval(name, "sensitive"))
    for name in ("view_text_file", "use_skill", "search_knowledge", "read_knowledge_document"):
        check(f"sensitive 跳过只读工具 {name}", not tap.needs_approval(name, "sensitive"))
    for name in ("browser_click", "browser_snapshot", "screen_capture", "computer_action"):
        check(f"自有逐操作确认的工具不重复弹窗 {name}", not tap.needs_approval(name, "all"))
    check("all 连只读工具也问", tap.needs_approval("view_text_file", "all"))

    summary = tap.summarize_input("write_text_file", {
        "file_path": "notes/a.md",
        "content": PHONE * 3,
    })
    check("写文件摘要只带路径与长度，不带正文",
          "notes/a.md" in summary and PHONE not in summary and "正文" in summary, summary)
    masked = pg.redact(tap.summarize_input("run_command", {
        "command": f"curl -H 'Authorization: Bearer {SECRET}' http://x/{PHONE}",
    }))
    check("命令摘要里的密钥与手机号在弹框前脱敏",
          SECRET not in masked and PHONE not in masked and "curl" in masked, masked)

    runtime = {"toolApproval": {
        "mode": "sensitive",
        "gateUrl": "http://127.0.0.1:54321/tool",
        "gateToken": "t",
    }}
    ta = tap.ToolApproval.from_runtime(runtime)
    check("合法 runtime 块 → enabled/ready", ta.enabled and ta.ready, ta.error)

    bad_url = tap.ToolApproval.from_runtime({"toolApproval": {
        "mode": "all", "gateUrl": "http://10.0.0.8/tool", "gateToken": "t",
    }})
    check("非回环网关 fail-closed 且原因可读",
          bad_url.enabled and not bad_url.ready and "网关" in bad_url.error, bad_url.error)

    no_token = tap.ToolApproval.from_runtime({"toolApproval": {
        "mode": "all", "gateUrl": "http://127.0.0.1:54321/tool",
    }})
    check("缺 token fail-closed", not no_token.ready)

    gate_error = tap.ToolApproval.from_runtime({"toolApproval": {
        "mode": "sensitive", "gateError": "本机工具审批网关未启动",
    }})
    check("Rust 报网关不可用时 fail-closed 且原因透传",
          not gate_error.ready and "未启动" in gate_error.error, gate_error.error)

    invalid = tap.ToolApproval.from_runtime({"toolApproval": {"mode": "everything"}})
    check("非法档位按关闭处理但保留错误说明",
          not invalid.enabled and "非法" in invalid.error, invalid.error)

    missing = tap.ToolApproval.from_runtime({})
    check("没有 runtime 块（浏览器直连）不启用审批", not missing.enabled and not missing.error)


# ── 3. 中间件 + 假网关 ──────────────────────────────────────────

_GATE = {"calls": [], "decision": "allow"}


def _make_gate_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            token = self.headers.get("x-tt-tool-token")
            _GATE["calls"].append({"token": token, "body": body})
            if token != GATE_TOKEN:
                self._reply(401, {"ok": False, "error": "TOOL_APPROVAL_UNAUTHORIZED"})
                return
            if _GATE["decision"] == "deny":
                # 拒绝同样是合法应答：引擎不能把它当成网关故障
                self._reply(403, {"ok": False, "error": "用户拒绝了本次工具调用"})
                return
            self._reply(200, {"ok": True, "tool": body.get("tool"), "mode": "sensitive"})

        def _reply(self, status: int, obj: dict) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    return Handler


def _start_gate() -> tuple:
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_gate_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/tool"


async def _run_middleware(ta: tap.ToolApproval, name: str = "run_command",
                          payload: dict | None = None) -> tuple[str, list]:
    executed: list = []

    # toolkit 传入的 next_handler 是可调用对象：调用后返回协程，协程结果是异步迭代器
    async def _run(**kwargs):
        executed.append(kwargs)
        inner = ToolResponse(content=[TextBlock(type="text", text="EXECUTED")])

        async def gen():
            yield inner

        return gen()

    def next_handler(**kwargs):
        return _run(**kwargs)

    chunks = []
    async for chunk in ta.middleware()(
        {"tool_call": {"name": name, "input": payload or {"command": "echo ok"}}},
        next_handler,
    ):
        chunks.append(tool_text(chunk))
    return "\n".join(chunks), executed


def test_approval_middleware(gate_url: str) -> None:
    async def run() -> None:
        _GATE["calls"].clear()
        _GATE["decision"] = "allow"
        ta = tap.ToolApproval(mode="sensitive", gate_url=gate_url, gate_token=GATE_TOKEN,
                              runtime_present=True)
        text, executed = await _run_middleware(ta, payload={"command": f"echo {PHONE}"})
        check("放行后工具真的执行", bool(executed) and "EXECUTED" in text, text)
        check("网关收到工具名与任务摘要",
              _GATE["calls"] and _GATE["calls"][-1]["body"]["tool"] == "run_command")
        sent = json.dumps(_GATE["calls"][-1], ensure_ascii=False)
        check("发给网关的摘要已脱敏，不带手机号", PHONE not in sent, sent)

        _GATE["calls"].clear()
        ta_sensitive = tap.ToolApproval(mode="sensitive", gate_url=gate_url,
                                        gate_token=GATE_TOKEN, runtime_present=True)
        text, executed = await _run_middleware(ta_sensitive, name="view_text_file",
                                               payload={"file_path": "a.md"})
        check("只读工具不弹窗直接执行",
              bool(executed) and "EXECUTED" in text and not _GATE["calls"], text)

        _GATE["decision"] = "deny"
        _GATE["calls"].clear()
        ta_deny = tap.ToolApproval(mode="all", gate_url=gate_url,
                                   gate_token=GATE_TOKEN, runtime_present=True)
        text, executed = await _run_middleware(ta_deny)
        check("用户拒绝 → 工具不执行并回传拒绝原因",
              not executed and "未执行" in text and "拒绝" in text, text)

        # 回归：曾经把 403 当网关故障，一次拒绝之后所有调用都被自动拒绝
        _GATE["decision"] = "allow"
        text2, executed2 = await _run_middleware(ta_deny)
        check("一次拒绝不会毒化后续调用（第二次放行可执行）",
              bool(executed2) and "EXECUTED" in text2, text2)

        # 网关连不上：fail-closed，并缓存故障避免每次再等超时
        down = tap.ToolApproval(mode="all", gate_url="http://127.0.0.1:1/tool",
                                gate_token="t", runtime_present=True)
        text3, executed3 = await _run_middleware(down)
        check("网关不可达 → 工具不执行且原因可读",
              not executed3 and "无法连接" in text3, text3)
        text4, executed4 = await _run_middleware(down)
        check("网关故障被记住，后续调用直接拒绝", not executed4 and "无法连接" in text4)

    asyncio.run(run())


# ── 4. /api/chat 档位校验（不触发模型调用）──────────────────────

def test_api_validation() -> None:
    try:
        from fastapi.testclient import TestClient
        import server as engine_server
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: /api/chat 校验（依赖缺失：{exc}）")
        return
    client = TestClient(engine_server.app)
    resp = client.post("/api/chat", json={
        "message": "你好", "task_id": "task-pii-bad", "piiOutboundMode": "everything",
    })
    body = resp.json()
    check("未知 piiOutboundMode → 400 而不是静默关闭",
          resp.status_code == 400 and body.get("code") == "PII_MODE_INVALID", resp.text)


def main() -> int:
    srv, gate_url = _start_gate()
    try:
        test_redaction_rules()
        test_redacting_model()
        test_approval_policy()
        test_approval_middleware(gate_url)
        test_api_validation()
    finally:
        srv.shutdown()
    print()
    if FAILURES:
        print(f"engine privacy/approval selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f" - {name}")
        return 1
    print("engine privacy/approval selftest: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
