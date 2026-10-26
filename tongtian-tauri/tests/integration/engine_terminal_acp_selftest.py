#!/usr/bin/env python3
"""终端 AI+ 与 ACP 双向自测。

锁住这些不能退让的约束：
1. 终端端点只认桌面壳令牌：无令牌/错令牌一律 403，且不返回会话信息；
2. 真实 shell 会话能写入/读回/按 offset 增量读取，关闭后读回 404；
3. AI 只产出建议、永不执行；回答不是约定 JSON 时不给可执行命令（不编造）；
4. ACP 服务端：initialize/session/new/prompt 能驱动本机引擎 SSE，
   非 text 内容与 session/load 明确报不支持，取消返回 cancelled；
5. ACP 客户端：握手、流式消息、fs 读写限制在工作目录、权限请求按设置放行/拒绝、
   超时杀进程、审计不含文件正文；
6. 委派工具只在用户显式启用且命令可解析时注册；/api/chat 端到端能真实调用。

用法: python tests/integration/engine_terminal_acp_selftest.py
"""

import asyncio
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    import terminal_runtime as trm
    import acp_client as acpm
    from fastapi.testclient import TestClient
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.tool import Toolkit
except Exception as exc:
    print(f"SKIP: engine terminal/ACP selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []

TERM_HEADERS = {"x-tt-terminal-token": "selftest-terminal-token"}
ACP_SERVER = PROJECT_ROOT / "engine" / "acp_server.py"


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


# ── 终端 ─────────────────────────────────────────────────────

class JsonSuggestionModel(ChatModelBase):
    def __init__(self, raw: str) -> None:
        super().__init__(model_name="terminal-suggest", stream=False)
        self.raw = raw
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        self.calls += 1
        return ChatResponse(content=[{"type": "text", "text": self.raw}])


def _read_until(client, sid: str, needle: str, timeout: float = 10.0,
                start: int = 0) -> tuple[str, int]:
    offset = start
    blob = ""
    end = time.time() + timeout
    while time.time() < end:
        resp = client.get(
            "/api/terminal/read", params={"session": sid, "offset": offset}, headers=TERM_HEADERS
        )
        data = resp.json()
        blob += str(data.get("chunk") or "")
        offset = int(data.get("offset") or offset)
        if needle in blob:
            break
        time.sleep(0.1)
    return blob, offset


def terminal_checks() -> None:
    print("--- 1. 终端会话：令牌、真实 shell、增量读取、AI 只建议 ---")
    os.environ["TT_TERMINAL_TOKEN"] = "selftest-terminal-token"
    work_dir = Path(tempfile.mkdtemp(prefix="tt-terminal-"))
    client = TestClient(engine_server.app)

    no_token = client.post("/api/terminal/status")
    wrong = client.post("/api/terminal/status", headers={"x-tt-terminal-token": "wrong"})
    good = client.post("/api/terminal/status", headers=TERM_HEADERS)
    check("无令牌访问终端被拒且不泄露会话信息",
          no_token.status_code == 403 and no_token.json().get("code") == "TERMINAL_DISABLED",
          no_token.text[:200])
    check("错误令牌同样被拒", wrong.status_code == 403, wrong.text[:200])
    check("桌面壳令牌通过", good.status_code == 200 and good.json().get("pipe") is True,
          good.text[:200])

    opened = client.post("/api/terminal/open", json={"cwd": str(work_dir)}, headers=TERM_HEADERS)
    data = opened.json()
    sid = str(data.get("sessionId") or "")
    check("打开真实 shell 会话", opened.status_code == 200 and sid.startswith("term-"), opened.text[:300])
    check("会话返回真实 cwd", Path(str(data.get("cwd") or "")).resolve() == work_dir.resolve(),
          str(data.get("cwd")))
    check("显示 shell 名称", bool(data.get("shell")), opened.text[:200])
    check("管道模式说明", "不支持全屏交互程序" in str(data.get("note") or ""), opened.text[:300])

    bad_cwd = client.post("/api/terminal/open", json={"cwd": str(work_dir / "missing")},
                          headers=TERM_HEADERS)
    check("不存在的工作目录给出可读错误", bad_cwd.status_code == 400 and bad_cwd.json().get("code"),
          bad_cwd.text[:200])

    client.post("/api/terminal/input",
                json={"sessionId": sid, "data": "echo __TT_TERMINAL_MARK__\r\n"}, headers=TERM_HEADERS)
    blob, offset = _read_until(client, sid, "__TT_TERMINAL_MARK__", timeout=15)
    check("真实命令输出可读回", "__TT_TERMINAL_MARK__" in blob, blob[-500:])
    think = client.post("/api/terminal/input",
                        json={"sessionId": sid, "data": f"echo __NEXT__ && cd\r\n"}, headers=TERM_HEADERS)
    check("第二次写入成功", think.status_code == 200, think.text[:200])
    second = _read_until(client, sid, "__NEXT__", timeout=10, start=offset)[0]
    check("offset 增量读取不重复旧内容",
          "__NEXT__" in second and "__TT_TERMINAL_MARK__" not in second, second[:300])

    huge = client.post("/api/terminal/input",
                       json={"sessionId": sid, "data": "x" * 70000}, headers=TERM_HEADERS)
    check("超长输入被拒绝", huge.status_code == 400 and huge.json().get("code") == "TERMINAL_INPUT_TOO_LARGE",
          huge.text[:200])

    empty_question = client.post("/api/terminal/ai", json={"sessionId": sid, "question": " "},
                                 headers=TERM_HEADERS)
    check("AI 建议必须有问题描述", empty_question.status_code == 400
          and empty_question.json().get("code") == "TERMINAL_QUESTION_REQUIRED", empty_question.text[:200])
    bad_pii = client.post("/api/terminal/ai",
                          json={"sessionId": sid, "question": "清理临时文件", "piiOutboundMode": "wat"},
                          headers=TERM_HEADERS)
    check("AI 建议拒绝非法脱敏档位", bad_pii.status_code == 400
          and bad_pii.json().get("code") == "PII_MODE_INVALID", bad_pii.text[:200])

    raw = json.dumps({
        "summary": "先列出目录再删除",
        "commands": [
            {"command": "dir", "explanation": "列出当前目录"},
            {"command": "del /q *.tmp", "explanation": "删除临时文件"},
            {"command": "line1\nline2", "explanation": "多行命令应被丢弃"},
        ],
        "risk": "high",
    }, ensure_ascii=False)
    fake = JsonSuggestionModel(raw)
    original_build = engine_server.build_model_and_formatter
    engine_server.build_model_and_formatter = lambda *a, **k: (fake, DashScopeChatFormatter())
    try:
        ai = client.post("/api/terminal/ai",
                         json={"sessionId": sid, "question": "怎么清理临时文件？"},
                         headers=TERM_HEADERS)
        body = ai.json()
        cmds = [c["command"] for c in (body.get("suggestion") or {}).get("commands") or []]
        check("AI 建议返回命令与风险", ai.status_code == 200 and body.get("suggestion", {}).get("risk") == "high",
              ai.text[:400])
        check("多行命令被丢弃", cmds == ["dir", "del /q *.tmp"], str(cmds))
        check("AI 明确不执行", body.get("executed") is False, ai.text[:300])
        check("AI 调用后终端没有多出 dir 的执行痕迹",
              "驱动器" not in _read_until(client, sid, "__NEVER__", timeout=0.6)[0])
    finally:
        engine_server.build_model_and_formatter = original_build

    broken = trm.parse_suggestion("我觉得你可以用 dir 看看")
    check("非 JSON 输出不编造命令",
          broken.get("commands") == [] and broken.get("parseError"), str(broken))
    fenced = trm.parse_suggestion("好的：\n```json\n{\"summary\":\"看\",\"commands\":[{\"command\":\"ls\",\"explanation\":\"列目录\"}],\"risk\":\"low\"}\n```")
    check("代码围栏里的 JSON 能解析", [c["command"] for c in fenced["commands"]] == ["ls"], str(fenced))

    closed = client.post("/api/terminal/close", json={"sessionId": sid}, headers=TERM_HEADERS)
    check("关闭会话", closed.status_code == 200 and closed.json().get("ok") is True, closed.text[:200])
    gone = client.get("/api/terminal/read", params={"session": sid, "offset": 0}, headers=TERM_HEADERS)
    check("关闭后读回 404", gone.status_code == 404 and gone.json().get("code") == "TERMINAL_NOT_FOUND",
          gone.text[:200])

    os.environ.pop("TT_TERMINAL_TOKEN", None)
    shutil.rmtree(work_dir, ignore_errors=True)


# ── 假 ACP Agent ─────────────────────────────────────────────

FAKE_AGENT = r'''
import json, sys, time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
next_id = [100]

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def notify(session_id, kind, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id, "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}})

def request(method, params):
    rid = next_id[0]; next_id[0] += 1
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(0)
        msg = json.loads(line)
        if msg.get("id") == rid and "method" not in msg:
            return msg

def log(text):
    sys.stderr.write(text + "\n"); sys.stderr.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "fake-session-1"}})
    elif method == "session/prompt":
        sid = params.get("sessionId")
        if mode == "timeout":
            log("fake agent: entering timeout sleep")
            time.sleep(30)
            continue
        if mode == "path":
            resp = request("fs/read_text_file", {"sessionId": sid, "path": "../outside.txt"})
            code = (resp.get("error") or {}).get("data") or "NO_ERROR"
            notify(sid, "agent_message_chunk", "PATH_RESULT=" + str(code))
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
            continue
        notify(sid, "agent_thought_chunk", "先读取项目说明")
        notify(sid, "agent_message_chunk", "开始处理。")
        read = request("fs/read_text_file", {"sessionId": sid, "path": "hello.txt"})
        content = ((read.get("result") or {}).get("content") or "").strip()
        perm = request("session/request_permission", {
            "sessionId": sid,
            "toolCall": {"toolCallId": "tc-1", "title": "修改 hello.txt"},
            "options": [
                {"optionId": "allow", "kind": "allow_once", "name": "允许"},
                {"optionId": "reject", "kind": "reject_once", "name": "拒绝"},
            ],
        })
        outcome = ((perm.get("result") or {}).get("outcome") or {})
        decision = outcome.get("optionId") or outcome.get("outcome") or "none"
        notify(sid, "agent_message_chunk", "READ=" + content + "; PERM=" + decision)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        pass
'''


def _write_fake_agent(root: Path) -> Path:
    path = root / "fake_acp_agent.py"
    path.write_text(FAKE_AGENT, encoding="utf-8")
    return path


def _agent_command(fake: Path, mode: str) -> str:
    import subprocess as sp
    return sp.list2cmdline([sys.executable, str(fake), mode])


def acp_client_checks() -> None:
    print("--- 2. ACP 客户端：握手、流式、fs 边界、权限、超时、审计 ---")
    root = Path(tempfile.mkdtemp(prefix="tt-acp-client-"))
    work = root / "work"
    work.mkdir()
    (work / "hello.txt").write_text("hello from acp", encoding="utf-8")
    (root / "outside.txt").write_text("secret outside", encoding="utf-8")
    fake = _write_fake_agent(root)

    audits: list = []
    client = acpm.AcpClient(
        acpm._split_command(_agent_command(fake, "normal")), str(work),
        permission="deny", timeout=60, audit=lambda ev, fields: audits.append((ev, fields)),
    )
    result = client.run_prompt("修复 hello.txt 的拼写并跑测试")
    check("ACP 会话正常结束", result["ok"] and result["stopReason"] == "end_turn", str(result)[:400])
    check("流式正文被汇总", "READ=hello from acp" in result["output"], result["output"][:300])
    check("权限请求默认拒绝", "PERM=reject" in result["output"], result["output"][:300])
    check("审计含读取与权限决定",
          any(e == "fs_read" for e, _ in audits) and any(e == "permission" and f.get("decision") == "deny"
                                                        for e, f in audits), str(audits)[:400])
    check("审计不含文件正文", all("hello from acp" not in json.dumps(f, ensure_ascii=False) for _, f in audits))

    allow_audits: list = []
    allow = acpm.AcpClient(
        acpm._split_command(_agent_command(fake, "normal")), str(work),
        permission="allow", timeout=60, audit=lambda ev, fields: allow_audits.append((ev, fields)),
    )
    allowed = allow.run_prompt("允许外部 Agent 修改文件")
    check("自动放行档位选择 allow 选项", "PERM=allow" in allowed["output"], allowed["output"][:300])
    check("放行也写审计", any(e == "permission" and f.get("decision") == "allow" for e, f in allow_audits),
          str(allow_audits)[:300])

    escape = acpm.AcpClient(
        acpm._split_command(_agent_command(fake, "path")), str(work), permission="deny", timeout=60,
    )
    escaped = escape.run_prompt("读取工作目录外的文件")
    check("工作目录外的读取被拒绝", "PATH_RESULT=ACP_PATH_DENIED" in escaped["output"], escaped["output"][:300])

    timed = acpm.AcpClient(
        acpm._split_command(_agent_command(fake, "timeout")), str(work), permission="deny", timeout=2,
    )
    started = time.monotonic()
    try:
        timed.run_prompt("这个任务会超时")
        timed_code = "NO_ERROR"
    except acpm.AcpError as exc:
        timed_code = exc.code
    elapsed = time.monotonic() - started
    check("超时被终止且可读", timed_code == "ACP_TIMEOUT" and elapsed < 15,
          f"{timed_code} elapsed={elapsed:.1f}s")
    check("超时后进程已回收", timed.proc is None or timed.proc.poll() is not None)

    bad = acpm.AcpClient(["definitely-not-an-acp-command-xyz"], str(work), timeout=30)
    try:
        bad.start()
        missing_code = "NO_ERROR"
    except acpm.AcpError as exc:
        missing_code = exc.code
    check("命令不存在给出可读错误", missing_code == "ACP_COMMAND_NOT_FOUND", missing_code)

    shutil.rmtree(root, ignore_errors=True)


# ── ACP 服务端 ───────────────────────────────────────────────

class FakeEngine:
    mode = "normal"
    cancel = threading.Event()
    cancel_hits = 0
    requests: list = []


class FakeEngineHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: N802
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json({"status": "ok", "version": "fake"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if self.path == "/api/chat/cancel":
            FakeEngine.cancel_hits += 1
            FakeEngine.cancel.set()
            self._json({"ok": True})
            return
        if self.path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        FakeEngine.requests.append(body)
        FakeEngine.cancel.clear()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: str, payload: dict) -> None:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        try:
            emit("plan", {"steps": [{"description": "阅读需求"}, {"description": "修改文件"}]})
            if FakeEngine.mode == "slow":
                for i in range(100):
                    if FakeEngine.cancel.is_set():
                        emit("status_change", {"status": "cancelled"})
                        return
                    emit("step_start", {"step_id": f"s{i}", "description": f"慢步骤 {i}"})
                    time.sleep(0.1)
                return
            emit("step_start", {"step_id": "s1", "description": "阅读需求"})
            emit("step_done", {"step_id": "s1", "output": "已确认改动点"})
            emit("deliver", {"final_text": "已修复 hello.txt，测试通过。", "artifacts": ["D:/tmp/hello.txt"]})
            emit("done", {})
        except (BrokenPipeError, ConnectionResetError):
            return


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AcpProc:
    def __init__(self, engine_url: str) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.proc = __import__("subprocess").Popen(
            [sys.executable, str(ACP_SERVER), "--engine-url", engine_url],
            stdin=__import__("subprocess").PIPE,
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").PIPE,
            env=env,
        )
        self.messages: list = []
        self.stderr: list = []
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._reader_err, daemon=True).start()

    def _reader_err(self) -> None:
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", errors="replace").strip())

    def _reader(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except Exception:
                pass

    def send(self, payload: dict) -> None:
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_for(self, pred, timeout: float = 20.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                message = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            self.messages.append(message)
            if pred(message):
                return message
        raise TimeoutError(f"ACP 服务端在 {timeout}s 内没有返回期望消息；已收到 {self.messages[-3:]}")

    def request(self, rid: int, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.wait_for(lambda m: m.get("id") == rid, timeout)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def acp_server_checks() -> None:
    print("--- 3. ACP 服务端：外部客户端驱动本机引擎 ---")
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeEngineHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    acp = AcpProc(f"http://127.0.0.1:{port}")
    work = Path(tempfile.mkdtemp(prefix="tt-acp-server-"))
    try:
        init = acp.request(1, "initialize", {"protocolVersion": 1})
        result = init.get("result") or {}
        check("ACP initialize 返回协议版本", result.get("protocolVersion") == 1, str(init)[:300])
        check("能力声明不夸大", result.get("agentCapabilities", {}).get("loadSession") is False
              and result.get("agentCapabilities", {}).get("promptCapabilities", {}).get("image") is False,
              str(result)[:300])
        check("无认证方式", result.get("authMethods") == [], str(result)[:200])

        new = acp.request(2, "session/new", {"cwd": str(work)})
        sid = str((new.get("result") or {}).get("sessionId") or "")
        check("session/new 返回会话", bool(sid), str(new)[:300])
        bad_cwd = acp.request(3, "session/new", {"cwd": str(work / "missing")})
        check("不存在 cwd 被拒绝", (bad_cwd.get("error") or {}).get("code") == -32602, str(bad_cwd)[:300])

        acp.send({"jsonrpc": "2.0", "id": 4, "method": "session/prompt", "params": {
            "sessionId": sid, "prompt": [{"type": "text", "text": "修复项目里的构建错误"}],
        }})
        done = acp.wait_for(lambda m: m.get("id") == 4, 20)
        updates = [
            m["params"]["update"] for m in acp.messages
            if m.get("method") == "session/update" and m["params"].get("sessionId") == sid
        ]
        kinds = [u.get("sessionUpdate") for u in updates]
        texts = "".join(u.get("content", {}).get("text", "") for u in updates)
        check("prompt 正常收尾", (done.get("result") or {}).get("stopReason") == "end_turn", str(done)[:300])
        check("计划以思考块下发", "agent_thought_chunk" in kinds and "阅读需求" in texts, str(kinds))
        check("最终正文以消息块下发", "agent_message_chunk" in kinds and "已修复 hello.txt" in texts, texts[:300])
        check("交付物提示可见", "hello.txt" in texts, texts[:300])

        captured = FakeEngine.requests[-1] if FakeEngine.requests else {}
        check("引擎收到工作目录", Path(str(captured.get("workDir") or "")).resolve() == work.resolve(),
              str(captured)[:300])
        check("引擎收到任务号", str(captured.get("task_id") or "").startswith("acp-"), str(captured)[:200])

        image = acp.request(5, "session/prompt", {"sessionId": sid, "prompt": [{"type": "image", "data": "x"}]})
        err = image.get("error") or {}
        check("非 text 内容明确不支持", err.get("code") == -32602 and "ACP_UNSUPPORTED_CONTENT" in err.get("message", ""),
              str(image)[:300])

        load = acp.request(6, "session/load", {"sessionId": sid})
        check("session/load 明确不支持", (load.get("error") or {}).get("code") == -32601, str(load)[:300])
        unknown = acp.request(7, "nope/nothing", {})
        check("未知方法返回 -32601", (unknown.get("error") or {}).get("code") == -32601, str(unknown)[:300])

        FakeEngine.mode = "slow"
        FakeEngine.cancel_hits = 0
        acp.send({"jsonrpc": "2.0", "id": 8, "method": "session/prompt", "params": {
            "sessionId": sid, "prompt": [{"type": "text", "text": "慢任务"}],
        }})
        acp.wait_for(
            lambda m: m.get("method") == "session/update"
            and "慢步骤" in m["params"]["update"].get("content", {}).get("text", ""), 10,
        )
        acp.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        cancelled = acp.wait_for(lambda m: m.get("id") == 8, 10)
        check("取消返回 cancelled", (cancelled.get("result") or {}).get("stopReason") == "cancelled",
              str(cancelled)[:300])
        deadline = time.time() + 5
        while time.time() < deadline and FakeEngine.cancel_hits == 0:
            time.sleep(0.05)
        check("取消转发到引擎", FakeEngine.cancel_hits > 0,
              f"hits={FakeEngine.cancel_hits} set={FakeEngine.cancel.is_set()} stderr={acp.stderr[-5:]}")
    finally:
        FakeEngine.mode = "normal"
        acp.close()
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


# ── 委派工具注册与端到端 ─────────────────────────────────────

def acp_runtime_checks() -> None:
    print("--- 4. 委派工具：显式启用才注册，端到端可调用 ---")
    root = Path(tempfile.mkdtemp(prefix="tt-acp-runtime-"))
    work = root / "work"
    work.mkdir()
    (work / "hello.txt").write_text("hello", encoding="utf-8")
    fake = _write_fake_agent(root)
    audit_path = root / "audit" / "acp.jsonl"

    disabled = acpm.AcpRuntime.from_runtime({"acp": {
        "enabled": False, "command": _agent_command(fake, "normal"), "workDir": str(work),
    }}, work_dir=str(work))
    check("未启用时不可用", not disabled.ready and "未启用" in disabled.reason, disabled.reason)
    check("未启用时提示词为空", disabled.system_hint() == "")

    empty_cmd = acpm.AcpRuntime.from_runtime({"acp": {"enabled": True, "command": ""}},
                                             work_dir=str(work))
    check("启用但缺命令时不可用且说明", not empty_cmd.ready and "启动命令" in empty_cmd.reason, empty_cmd.reason)

    bad_perm = acpm.AcpRuntime.from_runtime({"acp": {
        "enabled": True, "command": _agent_command(fake, "normal"), "permission": "wat",
    }}, work_dir=str(work))
    check("非法审批策略 fail-closed 且不可用", not bad_perm.ready
          and bad_perm.permission == "deny" and "非法" in bad_perm.reason, bad_perm.reason)

    runtime = acpm.AcpRuntime.from_runtime({"acp": {
        "enabled": True,
        "command": _agent_command(fake, "normal"),
        "permission": "allow",
        "timeoutSeconds": 120,
        "workDir": str(work),
        "auditPath": str(audit_path),
    }}, work_dir=str(work))
    check("显式启用后可用", runtime.ready, runtime.reason)
    check("提示词包含委派工具与权限口径",
          "delegate_coding_task" in runtime.system_hint() and "自动放行" in runtime.system_hint())

    toolkit = Toolkit()
    events: list = []
    runtime.install(toolkit, emit=lambda ev, data: events.append((ev, data)), task_id="acp-task-1")
    check("工具注册进 Toolkit", "delegate_coding_task" in toolkit.tools, str(sorted(toolkit.tools)))
    fn = toolkit.tools["delegate_coding_task"].original_func
    resp = fn(task="修复 hello.txt 的拼写错误，并确认测试通过", cwd="")
    text = tool_text(resp)
    check("委派工具返回外部 Agent 结果", "外部编码 Agent 执行完成" in text and "READ=hello" in text, text[:500])
    check("委派过程有进度与审计事件",
          any(ev == "step_progress" for ev, _ in events)
          and any(ev == "tool_audit" and d.get("event") == "delegate_done" for ev, d in events),
          str(events)[:500])
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []
    check("审计文件写入开始与完成", len(audit_lines) >= 2
          and "delegate_start" in audit_lines[0] and "delegate_done" in audit_lines[-1],
          "\n".join(audit_lines[-2:])[:400])
    check("审计不含任务正文", "拼写错误" not in "\n".join(audit_lines))

    outside = fn(task="越界尝试读取别的目录里的内容", cwd=str(root))
    check("委派 cwd 不能越出工作目录", "不在本任务工作目录内" in tool_text(outside), tool_text(outside)[:300])

    shutil.rmtree(root, ignore_errors=True)


class DelegateModel(ChatModelBase):
    def __init__(self) -> None:
        super().__init__(model_name="acp-e2e", stream=False)
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        engine_server.RECORDED_PROMPTS.append(json.dumps(messages, ensure_ascii=False, default=str))
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[{
                "type": "tool_use", "id": "call-acp-1", "name": "delegate_coding_task",
                "input": {"task": "修复 hello.txt 的拼写错误并确认测试通过"},
            }])
        return ChatResponse(content=[{"type": "text", "text": "已委派外部 Agent 并核对结果。"}])


def chat_e2e_checks() -> None:
    print("--- 5. /api/chat 端到端：委派工具真实可达 ---")
    if not hasattr(engine_server, "RECORDED_PROMPTS"):
        engine_server.RECORDED_PROMPTS = []
    root = Path(tempfile.mkdtemp(prefix="tt-acp-e2e-"))
    work = root / "work"
    work.mkdir()
    (work / "hello.txt").write_text("hello", encoding="utf-8")
    fake = _write_fake_agent(root)
    original_build = engine_server.build_model_and_formatter
    original_work = engine_server.WORK_DIR
    engine_server.build_model_and_formatter = lambda *a, **k: (DelegateModel(), DashScopeChatFormatter())
    engine_server.WORK_DIR = work
    engine_server.RECORDED_PROMPTS.clear()
    client = TestClient(engine_server.app)
    runtime = {"acp": {
        "enabled": True,
        "command": _agent_command(fake, "normal"),
        "permission": "deny",
        "timeoutSeconds": 120,
        "workDir": str(work),
        "auditPath": str(root / "audit.jsonl"),
    }}
    try:
        resp = client.post("/api/chat", json={
            "message": "把 hello 文件的拼写修一下", "task_id": "acp-e2e", "runtime": runtime,
        })
        blob = "\n".join(engine_server.RECORDED_PROMPTS)
        check("端到端执行正常收尾", "event: done" in resp.text and "event: error" not in resp.text,
              resp.text[:400])
        check("启用说明进事件流", "ACP 委派已启用" in resp.text, resp.text[:400])
        check("系统提示包含委派工具", "delegate_coding_task" in blob, blob[:400])
        check("模型拿到外部 Agent 结果", "外部编码 Agent 执行完成" in blob and "READ=hello" in blob,
              blob[-800:])
        check("最终回答产出", "已委派外部 Agent 并核对结果" in resp.text, resp.text[-300:])
    finally:
        engine_server.build_model_and_formatter = original_build
        engine_server.WORK_DIR = original_work
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    terminal_checks()
    acp_client_checks()
    acp_server_checks()
    acp_runtime_checks()
    chat_e2e_checks()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + " | ".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
