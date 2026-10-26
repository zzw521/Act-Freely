#!/usr/bin/env python3
"""执行面 P2 浏览器 AI+ 自测：档位 → 网关放行 → CDP 操作 → 审计脱敏

锁住这些不能退让的约束：
1. 未收到能力块 / 未装浏览器 / 禁用 / 档位非法 / 网关非回环 / 缺 token → 不注册工具，
   原因可见，提示词也不提；token 不出现在任何提示、错误或审计里；
2. 只允许 http/https，file/javascript/data/chrome 等协议在引擎侧直接拒绝；
3. 打开/快照/点击/输入/滚动/截图都先经本机网关放行，引擎不直接接触浏览器进程；
4. 密码框拒绝输入；输入正文与页面正文不写入审计；URL 查询串与片段在审计里去掉；
5. 快照/操作次数有上限；「每次询问」被拒绝时工具如实返回，不执行；
6. 截图作为图片块返回，且能被 formatter 提升为用户消息图片（文案可区分浏览器/电脑操控）；
7. /api/chat 端到端：系统提示带浏览器工具，模型真的能调用工具并拿到快照文字。

离线运行：替换 `_gate_post` 与 CDP 客户端，不启动真实浏览器。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_browser_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    import browser_use as bu
    import computer_use as cu
    from fastapi.testclient import TestClient
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.tool import Toolkit
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine browser selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
RECORDED = []
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/"
    "lK3P9wAAAABJRU5ErkJggg=="
)
PAGE_URL = "https://example.com/page?x=1#frag"
PAGE_HOST = "https://example.com/page"


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


def bundle(**overrides) -> dict:
    block = {
        "supported": True,
        "platform": "windows",
        "browser": "Google Chrome",
        "features": {"snapshot": True, "click": True, "type": True, "screenshot": True},
        "policy": "auto",
        "persisted": True,
        "running": False,
        "profileDir": "C:/fake/browser/profile",
        "gateUrl": "http://127.0.0.1:54322/browser",
        "gateToken": "selftest-browser-token",
    }
    block.update(overrides)
    return {"browserUse": block}


def ready_policy(**overrides) -> bu.BrowserUse:
    return bu.BrowserUse.from_runtime(bundle(**overrides), vision=True, task_id="task-bu")


ELEMENTS = {
    1: {"x": 100.0, "y": 50.0, "w": 80.0, "h": 30.0, "kind": "button", "label": "提交",
        "disabled": False, "editable": False, "isPassword": False, "isSelect": False},
    2: {"x": 200.0, "y": 100.0, "w": 240.0, "h": 32.0, "kind": "input[text]", "label": "搜索",
        "disabled": False, "editable": True, "isPassword": False, "isSelect": False},
    3: {"x": 200.0, "y": 160.0, "w": 240.0, "h": 32.0, "kind": "input[password]", "label": "密码",
        "disabled": False, "editable": True, "isPassword": True, "isSelect": False},
    4: {"x": 200.0, "y": 220.0, "w": 240.0, "h": 32.0, "kind": "select", "label": "城市",
        "disabled": False, "editable": False, "isPassword": False, "isSelect": True},
}


class FakeCDP:
    """模拟页面级 CDP：只认本次自测用到的命令与脚本。"""

    instances: list = []

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.calls: list = []
        self.evals: list = []
        self.connected = True
        FakeCDP.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def call(self, method: str, params: dict | None = None, timeout: float = 0) -> dict:
        self.calls.append((method, dict(params or {})))
        if method == "Page.captureScreenshot":
            return {"data": PNG_B64}
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": {"clientWidth": 1200, "clientHeight": 800}}
        return {}

    async def eval_js(self, expression: str):
        self.evals.append(expression)
        if "document.readyState" in expression:
            return {"url": PAGE_URL, "title": "测试页", "ready": "complete"}
        if "const idx = " in expression and "isPassword" in expression:
            idx = int(expression.split("const idx = ", 1)[1].split(";", 1)[0])
            return ELEMENTS.get(idx, {"error": "元素序号已失效，请重新调用 browser_snapshot"})
        if "nodes.slice" in expression:
            return {
                "url": PAGE_URL,
                "title": "测试页",
                "text": "欢迎来到测试页。请输入关键词并提交。",
                "elements": [
                    {"i": 1, "kind": "button", "label": "提交", "disabled": False,
                     "x": 100, "y": 50, "w": 80, "h": 30, "inView": True},
                    {"i": 2, "kind": "input[text]", "label": "搜索", "disabled": False,
                     "x": 200, "y": 100, "w": 240, "h": 32, "inView": True},
                    {"i": 4, "kind": "select", "label": "城市", "disabled": False,
                     "x": 200, "y": 220, "w": 240, "h": 32, "inView": True},
                ],
                "scrollX": 0, "scrollY": 0,
                "viewport": {"w": 1200, "h": 800}, "scrollHeight": 3000, "iframes": 1,
            }
        if "const want = " in expression:
            return {"ok": True, "label": "北京"}
        if "el.select()" in expression:
            return True
        if "w: innerWidth" in expression:
            return {"w": 1200, "h": 800, "x": 0, "y": 0}
        if "y: Math.round(scrollY)" in expression:
            return {"y": 900, "h": 800, "total": 3000}
        return None


def fake_gate(calls: list, deny: str = ""):
    async def post(url: str, token: str, payload: dict, timeout: int = 0):
        calls.append({"url": url, "token": token, "payload": dict(payload)})
        if url.endswith("/ensure"):
            return {
                "ok": True, "started": False, "browser": "chrome", "pid": 4321, "port": 9222,
                "pageId": "PAGE", "url": PAGE_URL, "title": "测试页",
                "wsUrl": "ws://127.0.0.1:9222/devtools/page/PAGE",
                "profileDir": "C:/fake/browser/profile",
            }, ""
        if url.endswith("/approve"):
            if deny:
                return None, f"BROWSER_USE_DENIED_BY_USER: {deny}"
            return {
                "ok": True, "action": payload.get("action"),
                "description": f"模拟放行 {payload.get('action')}", "policy": "auto",
            }, ""
        return None, "unexpected endpoint"

    return post


def pure_checks() -> None:
    print("--- 1. 档位/浏览器/网关/回环判定（纯函数层） ---")
    auto = ready_policy()
    check("auto 档位就绪", auto.ready and auto.notes() and "自动执行" in auto.notes()[0],
          str(auto.notes()))
    hint = auto.system_hint()
    for tool in ("browser_open", "browser_snapshot", "browser_click", "browser_type",
                 "browser_scroll", "browser_screenshot"):
        check(f"提示词列出 {tool}", tool in hint, hint)
    check("token 不出现在提示词", "selftest-browser-token" not in hint, hint)
    check("提示词说明独立配置目录", "独立配置目录" in hint, hint)

    no_vision = bu.BrowserUse.from_runtime(bundle(), vision=False, task_id="t")
    check("无视觉能力仍就绪（DOM 快照不依赖截图）", no_vision.ready, no_vision.reason)
    check("无视觉能力时不提供截图", "不能截图" in no_vision.system_hint()
          and "browser_screenshot：" not in no_vision.system_hint(), no_vision.system_hint())
    check("无视觉能力时有可见说明", any("DOM" in n for n in no_vision.notes()), str(no_vision.notes()))

    disabled = ready_policy(policy="disabled")
    check("禁用档位不就绪", not disabled.ready and disabled.system_hint() == "")
    check("禁用档位不制造噪声说明", disabled.notes() == [], str(disabled.notes()))

    missing = bu.BrowserUse.from_runtime({}, vision=True)
    check("缺少能力块不就绪且说明壳过旧", not missing.ready and "桌面壳可能过旧" in missing.reason,
          missing.reason)

    unsupported = ready_policy(supported=False)
    check("未安装浏览器不就绪", not unsupported.ready and "没有找到" in unsupported.reason,
          unsupported.reason)

    bad_policy = ready_policy(policy="always")
    check("非法档位就地拒绝", not bad_policy.ready and "非法" in bad_policy.reason,
          bad_policy.reason)

    remote = ready_policy(gateUrl="http://10.0.0.8:54322/browser")
    check("非回环网关地址被拒", not remote.ready and "地址无效" in remote.reason, remote.reason)

    no_token = ready_policy(gateToken="")
    check("缺少 token 被拒", not no_token.ready and "地址无效" in no_token.reason, no_token.reason)

    gate_err = ready_policy(gateError="网关没起来")
    check("网关启动失败原因透传", not gate_err.ready and "网关没起来" in gate_err.notes()[0],
          str(gate_err.notes()))

    broken = ready_policy(error="BROWSER_USE_INVALID_POLICY: bad")
    check("配置损坏 fail-closed 且原因可见",
          not broken.ready and "读取失败" in broken.notes()[0], str(broken.notes()))

    check("所有说明都不含 token",
          all("selftest-browser-token" not in n
              for n in (*auto.notes(), *missing.notes(), *broken.notes(), hint)))
    check("提示词不把 Win 键等电脑操控说明混进来", "screen_capture" not in hint, hint[:120])


def url_checks() -> None:
    print("--- 2. URL 白名单 / 回环 WS / 审计脱敏 ---")
    for raw in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,hi",
                "chrome://settings", "devtools://devtools/bundled/inspector.html",
                "about:blank", "ftp://example.com/a"):
        url, err = bu.normalize_url(raw)
        check(f"拒绝 {raw.split(':')[0]} 协议", not url and bool(err), f"{url} {err}")
    for raw in ("http://example.com", "https://example.com/a?q=1"):
        url, err = bu.normalize_url(raw)
        check(f"允许 {raw}", url == raw and not err, err)
    check("拒绝空白与控制字符", not bu.normalize_url("https://exa mple.com")[0])
    check("拒绝超长 URL",
          not bu.normalize_url("https://example.com/" + "a" * bu.MAX_URL_CHARS)[0])

    check("审计 URL 去掉查询串", bu._audit_url(PAGE_URL) == PAGE_HOST, bu._audit_url(PAGE_URL))
    check("审计 URL 去掉片段", bu._audit_url("https://a.com/p#secret") == "https://a.com/p")
    check("审计 URL 截断超长",
          len(bu._audit_url("https://a.com/" + "b" * 400)) <= 201)

    check("回环 devtools 页面地址可接受",
          bu._loopback_ws("ws://127.0.0.1:9222/devtools/page/ABC"))
    check("localhost devtools 页面地址可接受",
          bu._loopback_ws("ws://localhost:9222/devtools/page/ABC"))
    for bad in ("ws://10.0.0.8:9222/devtools/page/ABC",
                "ws://127.0.0.1:9222/devtools/browser/ABC",
                "wss://127.0.0.1:9222/devtools/page/ABC",
                "http://127.0.0.1:9222/devtools/page/ABC"):
        check(f"拒绝 {bad}", not bu._loopback_ws(bad))


def registration_checks() -> None:
    print("--- 3. 工具注册：未就绪一律不注册 ---")
    vision = Toolkit()
    ready_policy().install(vision)
    expected = {"browser_open", "browser_snapshot", "browser_click", "browser_type",
                "browser_scroll", "browser_screenshot"}
    check("有视觉能力时注册六个工具", expected <= set(vision.tools), str(list(vision.tools)))

    blind = Toolkit()
    bu.BrowserUse.from_runtime(bundle(), vision=False, task_id="t").install(blind)
    check("无视觉能力时不注册截图",
          "browser_screenshot" not in blind.tools and "browser_snapshot" in blind.tools,
          str(list(blind.tools)))

    empty = Toolkit()
    ready_policy(policy="disabled").install(empty)
    check("禁用时不注册任何浏览器工具", not empty.tools, str(list(empty.tools)))

    unsupported = Toolkit()
    ready_policy(supported=False).install(unsupported)
    check("未安装浏览器时不注册", not unsupported.tools, str(list(unsupported.tools)))


def tool_checks() -> None:
    print("--- 4. 工具执行：放行、快照、点击、输入、滚动、审计 ---")
    original_gate = bu._gate_post
    original_cdp = bu.CDP
    FakeCDP.instances.clear()
    bu.CDP = FakeCDP
    calls: list = []
    bu._gate_post = fake_gate(calls)
    audits: list = []

    def emit(ev, data):
        if ev == "tool_audit":
            audits.append(data)

    async def scenario():
        browser = ready_policy(policy="ask_every_time")
        toolkit = Toolkit()
        browser.install(toolkit, emit=emit, task_id="task-bu")
        fn = {name: toolkit.tools[name].original_func for name in toolkit.tools}
        cdp = None

        calls.clear()
        out = tool_text(await fn["browser_open"]("file:///etc/passwd"))
        check("打开非 http/https 页面被拒", "只允许打开 http/https" in out, out[:160])
        check("被拒页面不请求网关", calls == [], str(calls))
        check("被拒页面写入审计", audits and audits[-1]["outcome"] == "denied",
              str(audits[-1:]))

        calls.clear()
        audits.clear()
        out = tool_text(await fn["browser_open"]("https://example.com/search?q=secret"))
        check("打开页面成功", "已打开" in out and PAGE_HOST in out, out[:200])
        approve = [c for c in calls if c["url"].endswith("/approve")]
        ensure = [c for c in calls if c["url"].endswith("/ensure")]
        check("打开页面先请求放行", approve and approve[0]["payload"].get("action") == "open",
              str(approve[:1]))
        check("打开页面按需建立会话", bool(ensure) and ensure[0]["payload"].get("taskId") == "task-bu",
              str(ensure[:1]))
        check("请求带浏览器网关 token",
              all(c["token"] == "selftest-browser-token" for c in calls), str(calls[:1]))
        check("打开后写入 ok 审计",
              audits and audits[-1]["outcome"] == "ok" and audits[-1]["action"] == "browser",
              str(audits[-1:]))
        check("审计 URL 去掉查询串与片段", audits[-1]["target"] == "https://example.com/search",
              audits[-1]["target"])
        cdp = FakeCDP.instances[-1]
        check("CDP 在本地回环页面地址上连接", cdp.ws_url.startswith("ws://127.0.0.1:9222/"),
              cdp.ws_url)
        check("连接后启用 Page/Runtime 并拒绝下载",
              [m for m, _ in cdp.calls[:3]] == ["Page.enable", "Runtime.enable",
                                                "Page.setDownloadBehavior"],
              str(cdp.calls[:3]))
        check("导航真的发出", any(m == "Page.navigate" and p.get("url") ==
                                   "https://example.com/search?q=secret"
                                   for m, p in cdp.calls), str(cdp.calls))

        calls.clear()
        audits.clear()
        out = tool_text(await fn["browser_snapshot"]())
        check("快照返回页面文字", "欢迎来到测试页" in out, out[:200])
        check("快照返回元素序号与标签",
              "[1] button 「提交」" in out and "[2] input[text] 「搜索」" in out, out[:400])
        check("快照提示 iframe 未覆盖", "1 个 iframe" in out, out[:400])
        check("快照写入审计（元素与文本字数）",
              audits and audits[-1]["outcome"] == "ok" and "元素 3 个" in audits[-1]["detail"],
              str(audits[-1:]))
        check("快照审计不含页面正文", "欢迎来到测试页" not in json.dumps(audits, ensure_ascii=False))

        calls.clear()
        audits.clear()
        out = tool_text(await fn["browser_click"](1))
        check("点击成功并提示重新快照", "已点击 [1]" in out and "重新 browser_snapshot" in out,
              out[:200])
        mouse = [p for m, p in cdp.calls if m == "Input.dispatchMouseEvent"]
        check("点击派发真实鼠标事件（move/press/release）",
              [p.get("type") for p in mouse[-3:]] == ["mouseMoved", "mousePressed", "mouseReleased"],
              str(mouse[-3:]))
        approve = [c for c in calls if c["url"].endswith("/approve")]
        check("点击按动作放行且带元素描述",
              approve and approve[0]["payload"].get("action") == "click"
              and "提交" in approve[0]["payload"].get("detail", ""), str(approve[:1]))
        check("点击审计不含 token", "selftest-browser-token" not in json.dumps(audits))

        calls.clear()
        audits.clear()
        out = tool_text(await fn["browser_type"](2, "阿里云"))
        inserts = [p for m, p in cdp.calls if m == "Input.insertText"]
        check("输入正文进入 CDP", inserts and inserts[-1].get("text") == "阿里云", str(inserts))
        check("输入返回文案不回显正文", "阿里云" not in out, out[:200])
        check("输入审计只记字符数",
              audits and audits[-1]["outcome"] == "ok" and "输入 3 个字符" in audits[-1]["detail"]
              and "阿里云" not in json.dumps(audits, ensure_ascii=False), str(audits[-1:]))

        calls.clear()
        out = tool_text(await fn["browser_type"](3, "hunter2"))
        check("密码框拒绝输入", "密码框" in out, out[:200])
        check("密码框不请求放行", not any(c["url"].endswith("/approve") for c in calls), str(calls))
        check("密码内容不回显", "hunter2" not in out, out[:200])

        calls.clear()
        out = tool_text(await fn["browser_type"](4, "北京"))
        approve = [c for c in calls if c["url"].endswith("/approve")]
        check("下拉框按选择动作放行", approve and approve[0]["payload"].get("action") == "select",
              str(approve[:1]))
        check("下拉框返回选中项", "已选择 [4] 选项「北京」" in out, out[:200])

        calls.clear()
        scroll_before = len(cdp.calls)
        out = tool_text(await fn["browser_scroll"]("down", 2))
        wheel = [p for m, p in cdp.calls[scroll_before:] if m == "Input.dispatchMouseEvent"
                 and p.get("type") == "mouseWheel"]
        check("滚动派发滚轮事件", wheel and wheel[-1].get("deltaY") == 1440, str(wheel))
        check("滚动返回新位置", "y=900" in out, out[:200])

        shot = await fn["browser_screenshot"]()
        images = [b for b in shot.content if isinstance(b, dict) and b.get("type") == "image"]
        check("截图返回图片块", len(images) == 1 and images[0]["source"]["data"] == PNG_B64,
              str(shot.content)[:200])
        check("截图尺寸来自布局指标", "1200x800" in tool_text(shot), tool_text(shot)[:160])

        browser._actions = bu.MAX_ACTIONS
        calls.clear()
        out = tool_text(await fn["browser_click"](1))
        check("操作次数达到上限后拒绝", f"上限 {bu.MAX_ACTIONS}" in out, out[:160])
        check("超限不请求放行", not calls, str(calls))
        browser._actions = 0
        browser._snapshots = bu.MAX_SNAPSHOTS
        out = tool_text(await fn["browser_snapshot"]())
        check("快照次数达到上限后拒绝", f"上限 {bu.MAX_SNAPSHOTS}" in out, out[:160])

        bu._gate_post = fake_gate(calls, deny="用户拒绝了本次浏览器操作")
        audits.clear()
        presses_before = len([1 for m, p in cdp.calls if m == "Input.dispatchMouseEvent"
                              and p.get("type") == "mousePressed"])
        out = tool_text(await fn["browser_click"](1))
        check("用户拒绝时动作不执行并如实返回",
              "拒绝" in out and "selftest-browser-token" not in out, out[:200])
        presses_after = len([1 for m, p in cdp.calls if m == "Input.dispatchMouseEvent"
                             and p.get("type") == "mousePressed"])
        check("拒绝后没有新增点击", presses_after == presses_before,
              f"{presses_before} → {presses_after}")
        check("拒绝写入 denied 审计",
              audits and audits[-1]["outcome"] == "denied", str(audits[-1:]))

    asyncio.run(scenario())
    bu._gate_post = original_gate
    bu.CDP = original_cdp


def formatter_checks() -> None:
    print("--- 5. 截图提升为用户消息图片（可区分来源） ---")

    def tool_msg(images: list[str]) -> Msg:
        output = [{"type": "text", "text": "done"}]
        for data in images:
            output.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            })
        return Msg(
            name="system",
            role="system",
            content=[{"type": "tool_result", "id": "call-1", "name": "browser_screenshot",
                      "output": output}],
        )

    class CaptureFormatter:
        def __init__(self):
            self.received = None

        async def format(self, msgs, *args, **kwargs):
            self.received = msgs
            return [{"ok": True}]

    async def scenario():
        base = CaptureFormatter()
        wrapped = cu.ToolImageFormatter(
            base, lead="以下是浏览器刚刚截取的画面（按时间从旧到新）："
        )
        await wrapped.format([Msg(name="user", content="看看页面", role="user"), tool_msg(["B1"])])
        last = base.received[-1]
        check("追加用户消息并带图片",
              last.role == "user" and any(b.get("type") == "image" for b in last.content),
              str(last.content)[:200])
        check("使用浏览器来源文案", "浏览器刚刚截取" in last.content[0]["text"]
              and "坐标" not in last.content[0]["text"], last.content[0]["text"])

        default = CaptureFormatter()
        await cu.ToolImageFormatter(default).format([tool_msg(["B2"])])
        check("未传 lead 时回落到电脑操控文案",
              "电脑操控" in default.received[-1].content[0]["text"],
              default.received[-1].content[0]["text"])

    asyncio.run(scenario())


class ToolCallingModel(ChatModelBase):
    """第一轮打开页面，第二轮快照，第三轮作答；记录 formatter 真正发给模型的消息"""

    def __init__(self):
        super().__init__(model_name="gpt-4o", stream=False)
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[{
                "type": "tool_use", "id": "call-open-1", "name": "browser_open",
                "input": {"url": "https://example.com/search?q=1"},
            }])
        if self.calls == 2:
            return ChatResponse(content=[{
                "type": "tool_use", "id": "call-snapshot-1", "name": "browser_snapshot",
                "input": {},
            }])
        return ChatResponse(content=[{"type": "text", "text": "已读取页面并完成检查。"}])


def http_checks() -> None:
    print("--- 6. /api/chat 端到端：系统提示与工具真实可达 ---")
    original_gate = bu._gate_post
    original_cdp = bu.CDP
    original_vision = os.environ.get("TT_VISION")
    original_build = engine_server.build_model_and_formatter
    calls: list = []
    bu.CDP = FakeCDP
    bu._gate_post = fake_gate(calls)
    os.environ["TT_VISION"] = "on"
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ToolCallingModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-browser-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    RECORDED.clear()
    try:
        resp = client.post("/api/chat", json={
            "message": "打开 example.com 并告诉我页面内容",
            "task_id": "browser-e2e",
            "runtime": bundle(),
        })
        blob = "\n".join(RECORDED)
        check("执行流正常收尾", "event: done" in resp.text and "event: error" not in resp.text,
              resp.text[:300])
        check("启用说明进事件流", "浏览器 AI+ 已启用" in resp.text, resp.text[:400])
        check("系统提示包含浏览器工具",
              all(t in blob for t in ("browser_open", "browser_snapshot", "browser_click")),
              blob[:400])
        check("模型拿到打开页面的工具结果", "已打开：https://example.com" in blob, blob[-600:])
        check("模型拿到快照文字", "欢迎来到测试页" in blob, blob[-600:])
        check("放行请求真实发出",
              any(c["url"].endswith("/approve") and c["payload"].get("action") == "open"
                  for c in calls), str(calls[:2]))
        check("最终回答产出", "已读取页面并完成检查" in resp.text, resp.text[-300:])
        check("token 不出现在事件流", "selftest-browser-token" not in resp.text)
    finally:
        bu._gate_post = original_gate
        bu.CDP = original_cdp
        engine_server.build_model_and_formatter = original_build
        if original_vision is None:
            os.environ.pop("TT_VISION", None)
        else:
            os.environ["TT_VISION"] = original_vision


def main() -> int:
    pure_checks()
    url_checks()
    registration_checks()
    tool_checks()
    formatter_checks()
    http_checks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
