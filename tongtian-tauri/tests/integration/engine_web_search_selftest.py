#!/usr/bin/env python3
"""
联网搜索自测：配置 → 联网授权 → 真实检索 → 工具与审计

锁住这些不能退让的约束：
1. 未配置 / 配置残缺 / 未知搜索源 → 不注册，原因可读；
2. 配置了但用户未授权联网、或搜索主机不在白名单 → 同样不注册；
3. searxng / tavily / serper 三种请求形态与响应解析正确；
4. 失败（HTTP 错误、非 JSON、超限、重定向跳出白名单）如实返回，不静默空结果；
5. 工具结果带链接与「摘要不是原文」的边界说明；Mock 结果不得当作事实；
6. 每次调用发 tool_audit（action=network），target 不含查询正文。

离线运行：本地假 HTTP 服务 + 真实 Toolkit，无需外网与 API Key
（测试期把回环地址临时视为公网，SSRF 判定本身由 tool_grants 自测覆盖）。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_web_search_selftest.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    import tool_grants as tg
    import web_search as ws
    from fastapi.testclient import TestClient
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine web search selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
RECORDED = []
REQUESTS = []
PORT = 0

ENV_KEYS = ("TT_SEARCH_PROVIDER", "TT_SEARCH_URL", "TT_SEARCH_API_KEY", "TT_SEARCH_MOCK")


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class RecordingModel(ChatModelBase):
    def __init__(self):
        super().__init__(model_name="fake-web-search", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        return ChatResponse(content=[{"type": "text", "text": "已完成。"}], usage=None)


def patch_model() -> None:
    engine_server.build_model_and_formatter = lambda *a, **k: (
        RecordingModel(), DashScopeChatFormatter()
    )


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def grants_policy(**overrides) -> tg.ToolGrantPolicy:
    block = {
        "workspaceId": "ws-web",
        "networkEnabled": True,
        "networkAllowlist": ["127.0.0.1"],
    }
    block.update(overrides)
    return tg.ToolGrantPolicy.from_runtime({"skills": [], "sandbox": block})


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def config_checks() -> None:
    print("--- 1. 配置与授权判定（纯函数层） ---")
    clear_env()
    idle = ws.WebSearchRuntime.from_env(None)
    check("未配置 → 不可用且不打扰用户", not idle.configured and idle.notes() == [], str(idle.notes()))
    check("未配置 → 无工具提示词", idle.system_hint() == "")
    check("未配置 → 拒绝原因可读", "未配置搜索源" in (idle.denial() or ""), str(idle.denial()))

    os.environ["TT_SEARCH_PROVIDER"] = "bogus"
    bad = ws.WebSearchRuntime.from_env(grants_policy())
    check("未知搜索源 → 明确报错", "未知搜索源" in bad.error and not bad.configured, bad.error)
    check("未知搜索源 → 原因进说明", any("未知搜索源" in n for n in bad.notes()), str(bad.notes()))

    clear_env()
    os.environ["TT_SEARCH_PROVIDER"] = "searxng"
    missing_url = ws.WebSearchRuntime.from_env(grants_policy())
    check("searxng 缺 URL → 不假装可用", "TT_SEARCH_URL" in missing_url.error, missing_url.error)

    clear_env()
    os.environ["TT_SEARCH_PROVIDER"] = "tavily"
    missing_key = ws.WebSearchRuntime.from_env(grants_policy())
    check("tavily 缺 Key → 明确报错", "TT_SEARCH_API_KEY" in missing_key.error, missing_key.error)

    clear_env()
    os.environ["TT_SEARCH_URL"] = "http://127.0.0.1:1/search"
    inferred = ws.WebSearchRuntime.from_env(grants_policy())
    check("只给 URL → 按 searxng 解释", inferred.provider == "searxng" and inferred.configured, inferred.provider)

    unauthed = ws.WebSearchRuntime.from_env(None)
    check("有搜索源但没有策略 → 不可用", "没有沙箱运行时策略" in (unauthed.denial() or ""), str(unauthed.denial()))

    off = ws.WebSearchRuntime.from_env(grants_policy(networkEnabled=False, networkAllowlist=[]))
    check("未授权联网 → 不注册", "未授权联网" in (off.denial() or ""), str(off.denial()))
    check("未授权联网 → 说明可见", any("网页搜索未注册" in n for n in off.notes()), str(off.notes()))

    wrong_host = ws.WebSearchRuntime.from_env(
        grants_policy(networkAllowlist=["example.com"])
    )
    check("搜索主机不在白名单 → 不注册", "不在联网白名单" in (wrong_host.denial() or ""), str(wrong_host.denial()))
    check("拒绝说明列出白名单主机", "example.com" in (wrong_host.denial() or ""), str(wrong_host.denial()))

    corrupt = tg.ToolGrantPolicy.from_runtime({"sandbox": {"error": "POLICY_CORRUPT"}})
    broken = ws.WebSearchRuntime.from_env(corrupt)
    check("策略损坏 → 失败关闭", "策略读取失败" in (broken.denial() or ""), str(broken.denial()))

    clear_env()
    os.environ["TT_SEARCH_MOCK"] = "true"
    no_grant_mock = ws.WebSearchRuntime.from_env(grants_policy(networkEnabled=False, networkAllowlist=[]))
    check("Mock 仍要求显式授权", not no_grant_mock.ready and "未授权联网" in (no_grant_mock.denial() or ""), str(no_grant_mock.denial()))
    mock = ws.WebSearchRuntime.from_env(grants_policy(networkAllowlist=["search.mock.local"]))
    check("Mock + 授权 → 就绪", mock.ready and mock.mock, str(mock.denial()))
    check("Mock 提示词声明不是真实网页", "Mock" in mock.system_hint() and "不能把结果当作事实" in mock.system_hint(), mock.system_hint())
    check("Mock 说明可见", any("Mock 结果" in n for n in mock.notes()), str(mock.notes()))


def parser_checks() -> None:
    print("--- 2. 三种搜索源的响应解析 ---")
    tavily = ws.parse_tavily({
        "results": [
            {"title": "T1", "url": "https://example.com/a", "content": "c1"},
            {"title": "", "url": "http://example.com/b", "content": "c2"},
            {"title": "坏链接", "url": "javascript:alert(1)", "content": "x"},
            {"title": "带凭据", "url": "https://user:pw@example.com/c", "content": "x"},
        ]
    })
    check("tavily 只保留合法 http(s) 结果", len(tavily) == 2, str(tavily))
    check("tavily 空标题回落到主机名", tavily[1]["title"] == "example.com", str(tavily[1]))
    check("javascript/凭据链接被剔除", all("javascript" not in r["url"] and "user:" not in r["url"] for r in tavily), str(tavily))

    serper = ws.parse_serper({"organic": [{"title": "S1", "link": "https://s.test/1", "snippet": "snip"}]})
    check("serper organic 解析", serper and serper[0]["url"] == "https://s.test/1", str(serper))

    searx = ws.parse_searxng({"results": [{"title": "X", "url": "https://x.test", "content": "y" * 900}]})
    check("searxng results 解析", searx and searx[0]["url"] == "https://x.test", str(searx))
    check("摘要被截断并标记", searx and len(searx[0]["snippet"]) <= ws.MAX_SNIPPET_CHARS + 1 and searx[0]["snippet"].endswith("…"), str(len(searx[0]["snippet"])))
    check("非对象载荷返回空", ws.parse_searxng([]) == [] and ws.parse_tavily("x") == [])


class _Handler(BaseHTTPRequestHandler):
    def _json(self, payload, code=200, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        REQUESTS.append({"method": "GET", "path": parsed.path, "query": urllib.parse.parse_qs(parsed.query)})
        if parsed.path == "/searxng":
            self._json({"results": [
                {"title": "差旅标准", "url": "https://example.com/travel", "content": "住宿上限 600 元"},
                {"title": "报销流程", "url": "https://example.com/process", "content": "提交发票"},
            ]})
        elif parsed.path == "/empty":
            self._json({"results": []})
        elif parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{PORT}/searxng?q=redirected&format=json")
            self.end_headers()
        elif parsed.path == "/evil-redirect":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{PORT}/searxng?q=evil&format=json")
            self.end_headers()
        elif parsed.path == "/badjson":
            self._json(b"not-json", content_type="text/plain; charset=utf-8")
        elif parsed.path == "/binary":
            self._json(b"\x00\x01\x02", content_type="application/octet-stream")
        elif parsed.path == "/http500":
            self._json({"error": "boom"}, code=500)
        elif parsed.path == "/large":
            self._json(b"a" * (ws.MAX_SEARCH_BYTES + 10), content_type="application/json")
        else:
            self._json({"error": "not found"}, code=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        REQUESTS.append({
            "method": "POST",
            "path": urllib.parse.urlsplit(self.path).path,
            "body": body,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        if self.path == "/serper":
            self._json({"organic": [
                {"title": "Serper 结果", "link": "https://example.com/serper", "snippet": "S"},
            ]})
        elif self.path == "/tavily":
            self._json({"results": [
                {"title": "Tavily 结果", "url": "https://example.com/tavily", "content": "T"},
            ]})
        else:
            self._json({"error": "not found"}, code=404)

    def log_message(self, *args):  # 静音
        pass


def _client_runtime(provider: str, path: str, key: str = "") -> ws.WebSearchRuntime:
    clear_env()
    os.environ["TT_SEARCH_PROVIDER"] = provider
    os.environ["TT_SEARCH_URL"] = f"http://127.0.0.1:{PORT}{path}"
    if key:
        os.environ["TT_SEARCH_API_KEY"] = key
    return ws.WebSearchRuntime.from_env(grants_policy())


def http_checks() -> None:
    print("--- 3. 真实检索：请求形态、失败路径、重定向 ---")
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 测试期把回环视为公网，才能用本地服务验证真实 HTTP 路径
    original_is_public = tg._is_public_ip
    original_build = ws.urllib.request.build_opener
    tg._is_public_ip = lambda _ip: True
    ws.urllib.request.build_opener = lambda *handlers: original_build(
        *handlers, ws.urllib.request.ProxyHandler({})
    )
    try:
        searx = _client_runtime("searxng", "/searxng")
        result = searx.search("差旅 住宿标准", 5)
        check("searxng 检索成功", result["outcome"] == "ok" and len(result["results"]) == 2, str(result))
        check("searxng 结果带链接", result["results"][0]["url"].startswith("https://"), str(result["results"][:1]))
        get_req = [r for r in REQUESTS if r["method"] == "GET" and r["path"] == "/searxng"]
        check("searxng 用 GET + q/format 参数", get_req and get_req[-1]["query"].get("q") == ["差旅 住宿标准"] and get_req[-1]["query"].get("format") == ["json"], str(get_req[-1:]))

        REQUESTS.clear()
        clipped = searx.search("x" * (ws.MAX_QUERY_CHARS + 50), 99)
        sent = [r for r in REQUESTS if r["path"] == "/searxng"]
        check("超长检索词被截断", clipped["outcome"] == "ok" and len(sent[-1]["query"]["q"][0]) == ws.MAX_QUERY_CHARS, str(len(sent[-1]["query"]["q"][0]) if sent else "no request"))

        REQUESTS.clear()
        empty = _client_runtime("searxng", "/empty").search("无结果查询", 3)
        check("空结果仍算成功但明确说明", empty["outcome"] == "ok" and empty["results"] == [] and any("没有返回可用结果" in n for n in empty["notes"]), str(empty))

        REQUESTS.clear()
        blank = searx.search("   ", 3)
        check("空检索词不发请求", blank["outcome"] == "denied" and not REQUESTS, str(blank))

        serper = _client_runtime("serper", "/serper", key="k-123")
        REQUESTS.clear()
        serper_out = serper.search("报销", 99)
        post = [r for r in REQUESTS if r["path"] == "/serper"]
        check("serper 检索成功", serper_out["outcome"] == "ok" and serper_out["results"][0]["url"].endswith("/serper"), str(serper_out))
        check("serper 用 POST + X-API-KEY", post and post[-1]["method"] == "POST" and post[-1]["headers"].get("x-api-key") == "k-123", str(post[-1:]))
        check("serper 结果数被夹到上限", post and post[-1]["body"].get("num") == ws.MAX_HITS, str(post[-1:]))
        check("serper q 为检索词", post and post[-1]["body"].get("q") == "报销", str(post[-1:]))

        tavily = _client_runtime("tavily", "/tavily", key="tv-1")
        REQUESTS.clear()
        tavily_out = tavily.search("政策", 2)
        tpost = [r for r in REQUESTS if r["path"] == "/tavily"]
        check("tavily 检索成功", tavily_out["outcome"] == "ok" and tavily_out["results"][0]["url"].endswith("/tavily"), str(tavily_out))
        check("tavily 请求带 key/query/max_results", tpost and tpost[-1]["body"].get("api_key") == "tv-1" and tpost[-1]["body"].get("query") == "政策" and tpost[-1]["body"].get("max_results") == 2, str(tpost[-1:]))

        REQUESTS.clear()
        follow = _client_runtime("searxng", "/redirect").search("重定向", 2)
        check("白名单内重定向正常跟随", follow["outcome"] == "ok" and len(follow["results"]) == 2, str(follow))

        evil = _client_runtime("searxng", "/evil-redirect").search("越界", 2)
        check("重定向跳出白名单被拒", evil["outcome"] == "denied" and any("不在联网白名单" in n for n in evil["notes"]), str(evil))
        check("被拒时不返回伪造结果", evil["results"] == [], str(evil))

        failed = _client_runtime("searxng", "/http500").search("故障", 2)
        check("HTTP 错误如实返回", failed["outcome"] == "failed" and any("HTTP 500" in n for n in failed["notes"]), str(failed))

        badjson = _client_runtime("searxng", "/badjson").search("坏 JSON", 2)
        check("非 JSON 如实返回", badjson["outcome"] == "failed" and any("不是合法 JSON" in n for n in badjson["notes"]), str(badjson))

        binary = _client_runtime("searxng", "/binary").search("二进制", 2)
        check("非文本响应被拒", binary["outcome"] == "failed" and any("不是 JSON/文本" in n for n in binary["notes"]), str(binary))

        large = _client_runtime("searxng", "/large").search("超限", 2)
        check("超大响应被拒", large["outcome"] == "failed" and any("超过" in n for n in large["notes"]), str(large))
    finally:
        tg._is_public_ip = original_is_public
        ws.urllib.request.build_opener = original_build
        server.shutdown()
        server.server_close()


def tool_checks(tmp: Path) -> None:
    print("--- 4. 工具注册、输出边界与审计 ---")
    clear_env()
    os.environ["TT_SEARCH_MOCK"] = "true"
    blocked = ws.WebSearchRuntime.from_env(grants_policy(networkAllowlist=["example.com"]))
    toolkit = engine_server.build_toolkit(tmp)
    blocked.install(toolkit)
    check("未授权主机不注册 web_search", "web_search" not in toolkit.tools, str(list(toolkit.tools)))

    ready = ws.WebSearchRuntime.from_env(grants_policy(networkAllowlist=["search.mock.local"]))
    events = []
    toolkit = engine_server.build_toolkit(tmp)
    ready.install(toolkit, emit=lambda ev, data: events.append((ev, data)), task_id="task-web")
    check("授权后注册 web_search", "web_search" in toolkit.tools, str(list(toolkit.tools)))

    tool = toolkit.tools["web_search"].original_func
    out = tool_text(tool("差旅报销", 2))
    check("Mock 搜索结果可用", "Mock 搜索结果" in out and "example.com/mock" in out, out[:240])
    check("Mock 输出禁止当作事实", "不得作为事实引用" in out, out[-180:])
    audits = [d for e, d in events if e == "tool_audit"]
    check("每次调用写审计", len(audits) == 1 and audits[0].get("action") == "network", str(audits))
    check("审计工具与工作区正确", audits and audits[0].get("tool") == "web_search" and audits[0].get("workspaceId") == "ws-web", str(audits[:1]))
    check("审计 target 只记主机与长度", audits and "search.mock.local" in audits[0].get("target", "") and "查询 4 字" in audits[0].get("target", ""), str(audits[:1]))
    check("审计不含查询正文", audits and "差旅报销" not in json.dumps(audits, ensure_ascii=False), str(audits[:1]))
    check("审计 detail 只记源与结果数", audits and "provider=mock" in audits[0].get("detail", "") and "results=2" in audits[0].get("detail", ""), str(audits[:1]))

    events.clear()
    clipped = tool_text(tool("检索词", 99))
    check("工具侧结果数被夹到上限", clipped.count("Mock 搜索结果") == 2, clipped[:200])

    patch_model()
    agent = engine_server.build_agent(None, tmp, None, runtime={"skills": []}, web=ready)
    check("Agent 执行面注册 web_search", "web_search" in agent.toolkit.tools, str(list(agent.toolkit.tools)))
    check("提示词声明 Mock 模式", "网页搜索（Mock 开发模式）" in (agent.sys_prompt or ""), "")

    plain = engine_server.build_agent(None, tmp, None, runtime={"skills": []}, web=blocked)
    check("未就绪时不注册也不提", "web_search" not in plain.toolkit.tools and "网页搜索" not in (plain.sys_prompt or ""), "")


def http_end_to_end(tmp: Path) -> None:
    print("--- 5. /api/chat 端到端：说明可见、默认不注册 ---")
    patch_model()
    engine_server.WORK_DIR = tmp
    client = TestClient(engine_server.app)
    try:
        clear_env()
        RECORDED.clear()
        plain = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "web-off",
            "runtime": {"skills": [], "sandbox": {"workspaceId": "ws-web"}},
        })
        prompt = RECORDED[-1] if RECORDED else ""
        check("未配置任务正常完成", "event: done" in plain.text)
        check("未配置时提示词不提 web_search", "web_search" not in prompt, prompt[:400])
        check("未配置时没有搜索说明", "网页搜索" not in plain.text, plain.text[:400])

        os.environ["TT_SEARCH_MOCK"] = "true"
        RECORDED.clear()
        granted = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "web-on",
            "runtime": {
                "skills": [],
                "sandbox": {
                    "workspaceId": "ws-web",
                    "networkEnabled": True,
                    "networkAllowlist": ["search.mock.local"],
                },
            },
        })
        prompt = RECORDED[-1] if RECORDED else ""
        check("授权任务正常完成", "event: done" in granted.text)
        check("提示词包含 web_search 与边界", "web_search" in prompt and "不能把结果当作事实引用" in prompt, prompt[-600:])
        check("Mock 说明可见", "网页搜索使用 Mock 结果" in granted.text, granted.text[:600])
    finally:
        engine_server.WORK_DIR = Path(".")
        clear_env()


def main() -> None:
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    tmp = Path(tempfile.mkdtemp(prefix="tt-web-"))
    try:
        config_checks()
        parser_checks()
        http_checks()
        tool_checks(tmp)
        http_end_to_end(tmp)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine web search selftest: ALL PASS")


if __name__ == "__main__":
    main()
