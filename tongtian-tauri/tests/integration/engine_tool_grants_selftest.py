#!/usr/bin/env python3
"""
执行面 P1 受控命令执行/联网自测：runtime.sandbox 授权 → 真实工具边界

锁住这些不能退让的约束：
1. 未授权/白名单为空 → 工具不注册，system prompt 也不提；
2. run_command 只接受白名单裸命令名，argv 方式执行（不经 shell），
   工作目录固定、超时/输出有上限、子进程环境变量不含引擎秘密；
3. fetch_url 只允许白名单公网主机，本机/内网/链路本地拒绝，重定向逐跳复核，
   非文本与超长响应被拒/截断；
4. 成功、失败、超时、拒绝都发 tool_audit，且审计 target 不带 URL 凭据/查询串，
   命令参数不进审计；
5. /api/chat 端到端：默认关闭时无「已启用」说明，授权后说明可见。

离线运行：本地假 HTTP 服务 + 真实 Toolkit，无需外网与 API Key（仅 DNS 判定用本机回环，
测试期把 _is_public_ip 临时视为通过，避免依赖真实公网）。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_tool_grants_selftest.py
"""

import json
import os
import shutil
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
    import tool_grants as tg
    from fastapi.testclient import TestClient
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine tool grants selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
RECORDED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class RecordingModel(ChatModelBase):
    def __init__(self):
        super().__init__(model_name="fake-grants-model", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        return ChatResponse(content=[{"type": "text", "text": "已完成。"}], usage=None)


def patch_model():
    engine_server.build_model_and_formatter = lambda *a, **k: (
        RecordingModel(), DashScopeChatFormatter()
    )


def bundle(grants: dict) -> dict:
    return {"skills": [], "sandbox": grants}


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_name() -> str:
    return Path(sys.executable).stem


def grants_policy(**overrides) -> tg.ToolGrantPolicy:
    base = {
        "workspaceId": "ws-grants",
        "execEnabled": True,
        "execAllowlist": [cmd_name()],
        "networkEnabled": False,
        "networkAllowlist": [],
    }
    base.update(overrides)
    return tg.ToolGrantPolicy.from_runtime(bundle(base))


def pure_checks(tmp: Path) -> None:
    print("--- 1. 默认关闭与判定（纯函数层） ---")
    off = tg.ToolGrantPolicy.from_runtime(None)
    check("无 runtime → 执行默认关闭", not off.exec_enabled and not off.network_enabled)
    check("无授权 → 没有可注册的工具", off.notes() == [] and off.system_hint() == "")
    check("无授权 → run_command 被拒", "未授权" in (off.exec_denial("python") or ""))
    check("无授权 → fetch_url 被拒", "未授权" in (off.network_denial("https://example.com") or ""))

    empty = grants_policy(execAllowlist=[])
    check("开启但白名单为空 → 说明未注册", any("run_command 未注册" in n for n in empty.notes()), str(empty.notes()))
    check("开启但白名单为空 → 提示词不提", empty.system_hint() == "")

    policy = grants_policy()
    check("白名单内裸命令放行", policy.exec_denial(cmd_name()) is None, cmd_name())
    check("白名单外命令被拒", "不在授权白名单" in (policy.exec_denial("definitely-not-allowed") or ""))
    check("带路径命令被拒", "裸命令名" in (policy.exec_denial("/usr/bin/python") or ""))
    check("带空格/shell 元字符被拒", "裸命令名" in (policy.exec_denial("python -c print(1)") or ""))
    check("系统里不存在的命令被拒", "找不到命令" in (grants_policy(execAllowlist=["tt-no-such-cmd-xyz"]).exec_denial("tt-no-such-cmd-xyz") or ""))
    hint = policy.system_hint()
    check("授权后提示词列出白名单命令", "受控执行与联网" in hint and cmd_name() in hint, hint)

    net = grants_policy(execAllowlist=[], networkEnabled=True, networkAllowlist=["127.0.0.1"])
    check("非 http/https 被拒", "只允许 http/https" in (net.network_denial("ftp://127.0.0.1/a") or ""))
    check("带用户名密码的 URL 被拒", "用户名或密码" in (net.network_denial("http://u:p@127.0.0.1/a") or ""))
    check("白名单外主机被拒", "不在联网白名单" in (grants_policy(networkEnabled=True, networkAllowlist=["example.com"]).network_denial("http://evil.test/a") or ""))
    local = grants_policy(execAllowlist=[], networkEnabled=True, networkAllowlist=["127.0.0.1"])
    check("白名单里的本机地址仍被拒（防 SSRF）", "非公网地址" in (local.network_denial("http://127.0.0.1/a") or ""))

    corrupt = tg.ToolGrantPolicy.from_runtime(bundle({"error": "POLICY_CORRUPT: bad"}))
    check("策略损坏 → 工具判定失败关闭", "读取失败" in (corrupt.exec_denial("python") or ""))
    check("策略损坏 → 有可见说明", any("读取失败" in n for n in corrupt.notes()), str(corrupt.notes()))

    try:
        # 校验失败必须在 follow 之前抛出，避免先把请求发到新地址
        tg._CheckedRedirect(lambda url: (_ for _ in ()).throw(ValueError("blocked"))).redirect_request(
            None, None, 302, "Found", {}, "http://evil.test/x"
        )
        check("重定向逐跳校验会在跟随前拒绝", False)
    except ValueError as exc:
        check("重定向逐跳校验会在跟随前拒绝", "blocked" in str(exc))


def registration_checks(tmp: Path) -> None:
    print("--- 2. 工具注册与提示词（未授权不注册） ---")
    plain = engine_server.build_toolkit(tmp)
    check("默认 toolkit 不含 run_command/fetch_url",
          "run_command" not in plain.tools and "fetch_url" not in plain.tools, str(list(plain.tools)))

    disabled = engine_server.build_toolkit(
        tmp, grants=grants_policy(execEnabled=False, networkEnabled=False)
    )
    check("显式关闭时不注册执行/联网工具",
          "run_command" not in disabled.tools and "fetch_url" not in disabled.tools, str(list(disabled.tools)))

    empty = engine_server.build_toolkit(
        tmp, grants=grants_policy(execAllowlist=[], networkAllowlist=[], networkEnabled=True)
    )
    check("开启但白名单为空也不注册",
          "run_command" not in empty.tools and "fetch_url" not in empty.tools, str(list(empty.tools)))

    events = []
    enabled = engine_server.build_toolkit(
        tmp,
        grants=grants_policy(networkEnabled=True, networkAllowlist=["example.com"]),
        emit=lambda ev, data: events.append((ev, data)),
        task_id="task-1",
    )
    check("授权后注册 run_command", "run_command" in enabled.tools, str(list(enabled.tools)))
    check("授权后注册 fetch_url", "fetch_url" in enabled.tools, str(list(enabled.tools)))


def exec_checks(tmp: Path) -> None:
    print("--- 3. 命令真实执行：无 shell、超时、输出上限、环境变量 ---")
    name = cmd_name()
    if shutil.which(name) is None:
        print("SKIP: 当前解释器命令不在 PATH，跳过真实执行检查")
        return
    events = []
    policy = grants_policy()
    events_ref = lambda ev, data: events.append((ev, data))  # noqa: E731
    toolkit = engine_server.build_toolkit(tmp, grants=policy, emit=events_ref, task_id="task-1")
    run = toolkit.tools["run_command"].original_func

    out = tool_text(run(name, json.dumps(["-c", "print('hello-grants')"]), 30))
    check("真实命令执行成功", "hello-grants" in out and "退出码：0" in out, out[:200])
    ok_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "ok"]
    check("成功调用写审计", len(ok_audits) == 1, str(events))
    check("审计带工具/目标/工作区",
          ok_audits and ok_audits[0].get("tool") == "run_command"
          and str(ok_audits[0].get("workspaceId")) == "ws-grants"
          and "个参数" in ok_audits[0].get("target", ""), str(ok_audits[:1]))

    # 分号在 argv 里只是普通字符：如果走了 shell，echo pwned 会真的执行
    no_shell = tool_text(run(name, json.dumps(["-c", "print('a; echo pwned')"]), 30))
    check("参数不经过 shell 解析", "a; echo pwned" in no_shell and "pwned" in no_shell, no_shell[:200])

    denied = tool_text(run("tt-not-allowed-cmd", "[]", 30))
    check("白名单外命令实际被拒", "命令未执行" in denied and "不在授权白名单" in denied, denied[:200])
    denied_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "denied"]
    check("拒绝调用写审计", len(denied_audits) >= 1, str(events))

    path_denied = tool_text(run(str(Path(sys.executable)), "[]", 30))
    check("带路径的真实可执行文件也被拒", "裸命令名" in path_denied, path_denied[:200])

    events.clear()
    timeout_out = tool_text(run(name, json.dumps(["-c", "import time; time.sleep(3)"]), 1))
    check("超时会被终止并说明", "超时" in timeout_out, timeout_out[:200])
    timeout_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "timeout"]
    check("超时写审计", len(timeout_audits) == 1, str(events))

    events.clear()
    long_out = tool_text(run(name, json.dumps(["-c", "print('x' * 9000)"]), 30))
    check("输出被截断到上限", "已截断" in long_out and "原文共" in long_out, long_out[-120:])
    check("截断后长度受控", len(long_out) < tg.MAX_EXEC_OUTPUT_CHARS + 500, str(len(long_out)))

    bad_json = tool_text(run(name, "[not-json", 30))
    check("非法 args_json 被拒", "不是合法 JSON" in bad_json, bad_json[:160])
    too_many = tool_text(run(name, json.dumps([str(i) for i in range(tg.MAX_EXEC_ARGS + 1)]), 30))
    check("参数数量超限被拒", "最多" in too_many, too_many[:160])

    # 环境变量白名单：引擎进程里的秘密不得进入子进程
    os.environ["TT_SELFTEST_TOKEN"] = "should-not-leak"
    env = tg._safe_env()
    os.environ.pop("TT_SELFTEST_TOKEN", None)
    check("子进程环境不含任意 *_TOKEN", "TT_SELFTEST_TOKEN" not in env, str(sorted(env)[:8]))
    check("子进程环境保留 PATH", "PATH" in env or "Path" in env, str(sorted(env)[:8]))


def sandbox_checks(tmp: Path) -> None:
    print("--- 3b. 系统资源沙箱：内存、进程数、进程树 ---")
    name = cmd_name()
    if shutil.which(name) is None:
        print("SKIP: 当前解释器命令不在 PATH，跳过资源沙箱检查")
        return
    events = []
    toolkit = engine_server.build_toolkit(
        tmp, grants=grants_policy(), emit=lambda e, d: events.append((e, d)), task_id="task-sandbox"
    )
    run = toolkit.tools["run_command"].original_func

    events.clear()
    out = tool_text(run(name, json.dumps(["-c", "print('sandbox-ok')"]), 30))
    check("正常执行不带降级说明", "sandbox-ok" in out and "资源限制" not in out, out[:200])
    ok_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "ok"]
    check("审计标记资源限制已生效",
          ok_audits and "limits=on" in str(ok_audits[0].get("detail", "")), str(ok_audits[:1]))

    # 超时必须终止整棵进程树：孙进程写入 PID 后长睡，父命令被杀后它也必须消失
    pid_file = tmp / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pid_file.as_posix()}', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    events.clear()
    timeout_out = tool_text(run(name, json.dumps(["-c", script]), 2))
    check("超时提示终止进程树", "整棵进程树已终止" in timeout_out, timeout_out[:240])
    deadline = time.time() + 5
    while time.time() < deadline and not pid_file.exists():
        time.sleep(0.1)
    child_pid = int(pid_file.read_text().strip()) if pid_file.exists() else 0
    check("孙进程 PID 已落盘", child_pid > 0, str(pid_file))
    deadline = time.time() + 5
    while time.time() < deadline and pid_alive(child_pid):
        time.sleep(0.1)
    check("超时后孙进程也被终止", not pid_alive(child_pid), f"pid={child_pid}")
    timeout_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "timeout"]
    check("超时审计仍为一条", len(timeout_audits) == 1, str(events))

    # 内存上限：把上限临时降到 64MB，子进程分配 256MB 必须失败
    original_mem = tg.EXEC_MEMORY_LIMIT_MB
    tg.EXEC_MEMORY_LIMIT_MB = 64
    try:
        mem_out = tool_text(run(name, json.dumps(["-c", (
            "try:\n"
            "    b = bytearray(256 * 1024 * 1024)\n"
            "    print('ALLOCATED', len(b))\n"
            "except MemoryError:\n"
            "    print('MEM_LIMIT_HIT')\n"
        )]), 30))
    finally:
        tg.EXEC_MEMORY_LIMIT_MB = original_mem
    check("超过内存上限时分配失败", "MEM_LIMIT_HIT" in mem_out, mem_out[:300])
    check("内存限制未触发时不会误报", "ALLOCATED" not in mem_out, mem_out[:300])

    if os.name == "nt":
        # 进程数上限：上限 3（含命令自身）时派生 5 个必须被挡住
        original_proc = tg.EXEC_PROCESS_LIMIT
        tg.EXEC_PROCESS_LIMIT = 3
        try:
            proc_out = tool_text(run(name, json.dumps(["-c", (
                "import subprocess, sys\n"
                "n = 0\n"
                "for _ in range(5):\n"
                "    try:\n"
                "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'])\n"
                "        n += 1\n"
                "    except OSError:\n"
                "        pass\n"
                "print('SPAWNED', n)\n"
            )]), 30))
        finally:
            tg.EXEC_PROCESS_LIMIT = original_proc
        spawned_lines = [ln for ln in proc_out.splitlines() if ln.startswith("SPAWNED")]
        spawned = int(spawned_lines[0].split()[1]) if spawned_lines else -1
        check("进程数上限阻止无限制派生", 0 <= spawned < 5, proc_out[:300])

        # Job 创建失败时如实降级，并在审计里可见
        original_job = tg._win_job
        tg._win_job = lambda: (None, "模拟资源限制不可用")
        try:
            events.clear()
            degraded_out = tool_text(run(name, json.dumps(["-c", "print('degraded')"]), 30))
        finally:
            tg._win_job = original_job
        check("资源限制不可用时如实说明",
              "系统资源限制未生效" in degraded_out and "模拟资源限制不可用" in degraded_out,
              degraded_out[:300])
        degraded_audits = [d for e, d in events if e == "tool_audit"]
        check("降级执行在审计中可见",
              degraded_audits and "limits=degraded" in str(degraded_audits[-1].get("detail", "")),
              str(degraded_audits[-1:]))
    else:
        print("SKIP: 进程数上限与 Job 降级说明只在 Windows 验证")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            body = b"hello-fetch-grants"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/evil-redirect":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{PORT}/ok")
            self.end_headers()
        elif self.path == "/binary":
            body = b"\x00\x01\x02"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
        elif self.path == "/large":
            # 不给 Content-Length：验证「边读边截断」的兜底路径
            body = b"a" * (tg.MAX_FETCH_BYTES + 10)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
        elif self.path == "/too-large":
            body = b"a" * 16
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(tg.MAX_FETCH_BYTES + 1))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 静音
        pass


PORT = 0


def fetch_checks(tmp: Path) -> None:
    print("--- 4. 联网获取：白名单、重定向、文本/体积边界、审计 ---")
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    PORT = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # 测试期把回环视为公网，才能用本地服务验证真实 HTTP 路径；SSRF 判定本身在上面单独验证
    original_is_public = tg._is_public_ip
    original_build = tg.urllib.request.build_opener
    tg._is_public_ip = lambda _ip: True
    tg.urllib.request.build_opener = lambda *handlers: original_build(
        *handlers, tg.urllib.request.ProxyHandler({})
    )
    events = []
    try:
        policy = grants_policy(execAllowlist=[], networkEnabled=True, networkAllowlist=["127.0.0.1"])
        toolkit = engine_server.build_toolkit(tmp, grants=policy, emit=lambda e, d: events.append((e, d)), task_id="task-net")
        fetch = toolkit.tools["fetch_url"].original_func
        base = f"http://127.0.0.1:{PORT}"

        out = tool_text(fetch(f"{base}/ok"))
        check("白名单主机获取成功", "hello-fetch-grants" in out and "状态：200" in out, out[:200])
        ok_audits = [d for e, d in events if e == "tool_audit" and d.get("outcome") == "ok"]
        check("成功获取写审计", len(ok_audits) == 1, str(events))
        check("审计 target 不带查询串/凭据",
              ok_audits and "?" not in ok_audits[0].get("target", ""), str(ok_audits[:1]))

        events.clear()
        redirected = tool_text(fetch(f"{base}/redirect"))
        check("白名单内重定向正常跟随", "hello-fetch-grants" in redirected, redirected[:200])
        check("跟随重定向后仍记成功审计",
              any(e == "tool_audit" and d.get("outcome") == "ok" for e, d in events), str(events))

        events.clear()
        evil = tool_text(fetch(f"{base}/evil-redirect"))
        check("重定向跳出白名单被拒", "不在联网白名单" in evil, evil[:240])
        check("被拒的重定向写审计（denied）",
              any(e == "tool_audit" and d.get("outcome") == "denied" for e, d in events), str(events))

        binary = tool_text(fetch(f"{base}/binary"))
        check("非文本响应被拒", "不是文本" in binary, binary[:200])

        events.clear()
        large = tool_text(fetch(f"{base}/large"))
        check("超大响应被截断并说明", "已截断" in large and str(tg.MAX_FETCH_BYTES) in large, large[:220])
        check("截断后正文不超过字符上限", len(large) <= tg.MAX_FETCH_CHARS + 300, str(len(large)))

        too_large = tool_text(fetch(f"{base}/too-large"))
        check("声明超限 Content-Length 时直接拒绝",
              "超过" in too_large and str(tg.MAX_FETCH_BYTES) in too_large, too_large[:200])

        denied = tool_text(fetch("http://localhost:1/ok"))
        check("白名单外主机实际被拒", "请求未发出" in denied and "不在联网白名单" in denied, denied[:200])

        # 凭据与查询串不进审计 target
        events.clear()
        with_user = tool_text(fetch(f"http://user:secret@{'127.0.0.1'}:{PORT}/ok?token=leak"))
        check("带凭据 URL 被拒且不回显密码", "用户名或密码" in with_user and "secret" not in with_user, with_user[:200])
        check("拒绝审计 target 不含密码/查询串",
              all(
                  "secret" not in str(d.get("target", "")) and "token=leak" not in str(d.get("target", ""))
                  for e, d in events if e == "tool_audit"
              ), str(events))
    finally:
        tg._is_public_ip = original_is_public
        tg.urllib.request.build_opener = original_build
        server.shutdown()
        server.server_close()


def http_checks(tmp: Path) -> None:
    print("--- 5. /api/chat 端到端：说明可见与默认关闭 ---")
    patch_model()
    engine_server.WORK_DIR = tmp
    client = TestClient(engine_server.app)
    try:
        RECORDED.clear()
        plain = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "grants-off",
            "runtime": bundle({"workspaceId": "ws-grants"}),
        })
        check("未授权任务正常完成", "event: done" in plain.text)
        check("未授权时没有「已启用」说明",
              "受控命令执行已启用" not in plain.text and "联网获取已启用" not in plain.text,
              plain.text[:400])

        RECORDED.clear()
        granted = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "grants-on",
            "runtime": bundle({
                "workspaceId": "ws-grants",
                "execEnabled": True,
                "execAllowlist": [cmd_name()],
                "networkEnabled": True,
                "networkAllowlist": ["example.com"],
            }),
        })
        check("授权后任务正常完成", "event: done" in granted.text)
        check("授权说明可见",
              "受控命令执行已启用" in granted.text and "联网获取已启用" in granted.text,
              granted.text[:500])
    finally:
        engine_server.WORK_DIR = Path(".")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-grants-"))
    try:
        pure_checks(tmp)
        registration_checks(tmp)
        exec_checks(tmp)
        sandbox_checks(tmp)
        fetch_checks(tmp)
        http_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine tool grants selftest: ALL PASS")


if __name__ == "__main__":
    main()
