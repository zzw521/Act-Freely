#!/usr/bin/env python3
"""执行面 P2 浏览器 AI+ 自测：档位 → 网关放行 → CDP 操作 → 审计脱敏 → 分级/录制/回放

锁住这些不能退让的约束：
1. 未收到能力块 / 未装浏览器 / 禁用 / 档位非法 / 网关非回环 / 缺 token → 不注册工具，
   原因可见，提示词也不提；token 不出现在任何提示、错误或审计里；
2. 只允许 http/https，file/javascript/data/chrome 等协议在引擎侧直接拒绝；
3. 打开/快照/点击/输入/滚动/截图都先经本机网关放行，引擎不直接接触浏览器进程；
4. 密码框拒绝输入；输入正文与页面正文不写入审计；URL 查询串与片段在审计里去掉；
5. 快照/操作次数有上限；「每次询问」被拒绝时工具如实返回，不执行；
6. 截图作为图片块返回，且能被 formatter 提升为用户消息图片（文案可区分浏览器/电脑操控）；
7. /api/chat 端到端：系统提示带浏览器工具，模型真的能调用工具并拿到快照文字；
8. （D-36）四档快照的上限全部由既有常量导出，默认档 interactive 的文案与分级前逐字一致，
   同一页面 minimal < interactive < full；非法档位不请求放行；
9. （D-36）草案只存语义步骤：没有坐标、没有输入正文、url 去查询串；敏感字段就地标记；
   落盘走 `add_draft`（status: draft），未 promote 不进技能目录，重复保存拒绝覆盖；
10.（D-36）回放每一步仍按动作过网关、复用既有工具的路径；输入位没给值就停在那一步且不写页面；
   敏感字段即使给了值也拒绝代填；按标签定位而不是按坐标；
11.（D-36）网关没在 `features` 里说支持的动作一律不注册，并在 `notes()` 里说明缺哪一半；
   引擎侧那几个动作名与 Rust `is_known_action` 白名单**互不重叠**（重叠即说明桌面半边已接，
   这条会红，提醒同步删掉 `FEATURE_ONLY_ACTIONS` 那一项）；
12.（D-36）`/tabs` 没实现、或收了 tabId 仍返回同一 targetId 时，如实报错，不假装标签页可用；
13.（D-36）`browser_press(index=0)` 发给「页面自己聚焦的元素」，所以发按键前先读一次聚焦对象：
   落在密码框就拒绝且**一个键也不派发、也不过网关**（白名单里有 Backspace/Delete/空格），
   页面没有聚焦元素时不在回话里编造一个聚焦对象。

离线运行：替换 `_gate_post` 与 CDP 客户端，不启动真实浏览器；引擎数据根整体重定向到
临时目录（`browser_record(save)` 会往 `skill_pool/drafts/` 落真文件）。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_browser_selftest.py
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

# 引擎数据根整体重定向到临时目录：`browser_record(save)` 会往
# `{home}/skill_pool/drafts/` 落**真文件**，未隔离的版本会把测试草案写进使用者的技能池。
SELFTEST_HOME = tempfile.mkdtemp(prefix="tt-browser-selftest-home-")
os.environ["TONGTIANWORK_HOME"] = SELFTEST_HOME

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

# 页面正文写长一点：分级的意义是「同一页少发多少」，两行正文比不出差别
PAGE_TEXT = "欢迎来到测试页。请输入关键词并提交。" + "这里是一段与流程无关的说明文字。" * 40
SNAPSHOT_ELEMENTS = [
    {"i": 1, "kind": "button", "label": "提交", "disabled": False,
     "x": 100, "y": 50, "w": 80, "h": 30, "inView": True},
    {"i": 2, "kind": "input[text]", "label": "搜索", "disabled": False,
     "x": 200, "y": 100, "w": 240, "h": 32, "inView": True},
    {"i": 4, "kind": "select", "label": "城市", "disabled": False,
     "x": 200, "y": 220, "w": 240, "h": 32, "inView": True},
]
OUTLINE = [
    {"tag": "h1", "text": "测试页", "top": 12, "items": 0},
    {"tag": "form", "text": "搜索", "top": 90, "items": 0},
    {"tag": "ul", "text": "", "top": 300, "items": 9},
]
LINKS = [
    {"text": "帮助中心", "href": "/help"},
    {"text": "退出登录", "href": "/logout"},
]
FIELDS = [
    {"kind": "input[text]", "label": "搜索", "password": False, "required": True, "top": 100},
    {"kind": "input[password]", "label": "密码", "password": True, "required": False, "top": 160},
    {"kind": "select", "label": "城市", "password": False, "required": False, "top": 220},
]


class FakeCDP:
    """模拟页面级 CDP：只认本次自测用到的命令与脚本。

    快照按脚本里的 `LEVEL` 与三个上限**分别**返回：分级要能被证明，靠一个固定
    payload 是测不出「interactive 比 full 省」的。
    """

    instances: list = []
    ready_state = "complete"
    label_matches = 1
    eval_result: dict | None = None
    # `browser_press(index=0)` 会先看一眼页面聚焦的是什么元素（密码框要拒），假页面用这个键给答案
    active_element: dict | None = None
    # 页面脚本给命中的元素打上 `data-tt-idx="1"`；假页面用这个键把「按标签找到谁」
    # 传给随后的 `const idx = 1` 解析，否则序号与元素在假页面里对不上
    bound: dict | None = None

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

    @staticmethod
    def _snapshot_payload(expr: str) -> dict:
        level = "interactive"
        found = re.search(r"const LEVEL = '([a-z]+)'", expr)
        if found:
            level = found.group(1)
        elem_cap = int(re.search(r"nodes\.slice\(0, (\d+)\)", expr).group(1))
        text_cap = int(re.search(r"clean\(body\)\.slice\(0, (\d+)\)", expr).group(1))
        items = [dict(e) for e in SNAPSHOT_ELEMENTS[:elem_cap]]
        if level == "full":
            for item in items:
                item.update({"href": "/go?a=1", "name": "q", "role": "button",
                             "id": "btn-1", "depth": 3})
        payload = {
            "url": PAGE_URL,
            "title": "测试页",
            "level": level,
            "text": PAGE_TEXT[:text_cap],
            "elements": items,
            "outline": [], "links": [], "fields": [],
            "truncated": {
                "elements": max(0, len(SNAPSHOT_ELEMENTS) - len(items)),
                "outline": False, "links": False,
            },
            "scrollX": 0, "scrollY": 0,
            "viewport": {"w": 1200, "h": 800}, "scrollHeight": 3000, "iframes": 1,
        }
        if level in ("structured", "full"):
            payload["outline"] = OUTLINE
            payload["fields"] = FIELDS
        if level == "full":
            payload["links"] = LINKS
        return payload

    @staticmethod
    def _label_hit(expression: str):
        match = re.search(r'const want = String\((".*?"|\'.*?\') \|\| \'\'\)', expression, re.S)
        if not match:
            return {"error": "按标签定位需要非空的 label"}
        label = json.loads(match.group(1))
        order = [str(e.get("label")) for e in SNAPSHOT_ELEMENTS]
        if label not in order:
            return {"error": f"页面上找不到文字标签「{label}」对应的可交互元素"}
        # 按快照给出的序号回查元素属性（`i` 不是列表下标：页面上只有可交互元素被编号）
        meta = ELEMENTS[int(SNAPSHOT_ELEMENTS[order.index(label)]["i"])]
        FakeCDP.bound = meta
        return {
            "x": meta["x"], "y": meta["y"], "w": meta["w"], "h": meta["h"],
            "kind": meta["kind"], "label": label, "matches": FakeCDP.label_matches,
            "disabled": meta["disabled"], "editable": meta["editable"],
            "isPassword": meta["isPassword"], "isSelect": meta["isSelect"],
        }

    async def eval_js(self, expression: str):
        self.evals.append(expression)
        if "document.readyState" in expression:
            return {"url": PAGE_URL, "title": "测试页", "ready": FakeCDP.ready_state}
        if "const idx = " in expression and "isPassword" in expression:
            idx = int(expression.split("const idx = ", 1)[1].split(";", 1)[0])
            if idx == 1 and FakeCDP.bound is not None:
                return dict(FakeCDP.bound)
            return ELEMENTS.get(idx, {"error": "元素序号已失效，请重新调用 browser_snapshot"})
        if "labelOf" in expression:
            return FakeCDP._label_hit(expression)
        if "nodes.slice" in expression:
            return FakeCDP._snapshot_payload(expression)
        if "const want = " in expression:
            return {"ok": True, "label": "北京"}
        if "JSON.stringify(value)" in expression:
            return dict(FakeCDP.eval_result or {"type": "string", "value": "42", "truncated": False})
        if "document.activeElement" in expression:
            return dict(FakeCDP.active_element) if FakeCDP.active_element else {"none": True}
        if "try { el.focus(); } catch" in expression:
            return {"ok": True, "tag": "input"}
        if "el.select()" in expression:
            return True
        if "w: innerWidth" in expression:
            return {"w": 1200, "h": 800, "x": 0, "y": 0}
        if "y: Math.round(scrollY)" in expression:
            return {"y": 900, "h": 800, "total": 3000}
        return None


def fake_gate(calls: list, deny: str = "", tabs: list | None = None,
              target: str = "PAGE", follow_tab: bool = True):
    """假网关。`tabs=None` 表示桌面半边没有实现 `/tabs`；
    `follow_tab=False` 表示收了 tabId 但返回的还是同一个页面（今天真实网关的行为）。"""

    async def post(url: str, token: str, payload: dict, timeout: int = 0):
        calls.append({"url": url, "token": token, "payload": dict(payload)})
        if url.endswith("/ensure"):
            chosen = str(payload.get("tabId") or "") if follow_tab else ""
            chosen = chosen or target
            return {
                "ok": True, "started": False, "browser": "chrome", "pid": 4321, "port": 9222,
                "pageId": chosen, "targetId": chosen, "url": PAGE_URL, "title": "测试页",
                "wsUrl": f"ws://127.0.0.1:9222/devtools/page/{chosen}",
                "profileDir": "C:/fake/browser/profile",
            }, ""
        if url.endswith("/approve"):
            if deny:
                return None, f"BROWSER_USE_DENIED_BY_USER: {deny}"
            return {
                "ok": True, "action": payload.get("action"),
                "description": f"模拟放行 {payload.get('action')}", "policy": "auto",
            }, ""
        if url.endswith("/tabs"):
            if tabs is None:
                return None, "网关拒绝了请求（HTTP 404）"
            act = str(payload.get("action") or "")
            if act == "new":
                tabs.append({"id": "TAB-NEW", "url": str(payload.get("url") or ""),
                             "title": "新标签页", "active": True})
                return {"ok": True, "tabId": "TAB-NEW", "tabs": tabs}, ""
            if act == "switch":
                want = str(payload.get("tabId") or "")
                for item in tabs:
                    item["active"] = item.get("id") == want
                return {"ok": True, "tabId": want, "tabs": tabs}, ""
            if act == "close":
                want = str(payload.get("tabId") or "")
                keep = [item for item in tabs if item.get("id") != want]
                tabs[:] = keep
                return {"ok": True, "tabs": keep}, ""
            return {"ok": True, "tabs": tabs}, ""
        return None, "unexpected endpoint"

    post.calls = calls
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


class Harness:
    """一阶段一套假网关/假 CDP：阶段之间不共享连接与预算计数，免得前面的用例把上限吃光"""

    def __init__(self, policy: str = "auto", gate=None, task_id: str = "task-bu", **overrides):
        self.task_id = task_id
        if gate is not None:
            # 外部造的网关（带 tabs / follow_tab 等场景）自带请求记录，直接接过来用
            self.calls = getattr(gate, "calls", [])
            self.gate = gate
        else:
            self.calls = []
            self.gate = fake_gate(self.calls)
        self.audits: list = []
        self.browser = ready_policy(policy=policy, **overrides)
        self.toolkit = Toolkit()
        FakeCDP.instances.clear()

    def emit(self, ev, data) -> None:
        if ev == "tool_audit":
            self.audits.append(data)

    def __enter__(self) -> "Harness":
        self._gate = bu._gate_post
        self._cdp = bu.CDP
        bu._gate_post = self.gate
        bu.CDP = FakeCDP
        FakeCDP.ready_state = "complete"
        FakeCDP.label_matches = 1
        FakeCDP.eval_result = None
        FakeCDP.active_element = None
        FakeCDP.bound = None
        self.browser.install(self.toolkit, emit=self.emit, task_id=self.task_id)
        self.fn = {name: tool.original_func for name, tool in self.toolkit.tools.items()}
        return self

    def __exit__(self, *exc) -> bool:
        bu._gate_post = self._gate
        bu.CDP = self._cdp
        FakeCDP.bound = None
        return False

    @property
    def cdp(self) -> FakeCDP:
        return FakeCDP.instances[-1]


def level_checks() -> None:
    print("--- 8. DOM 分级：四档判定、省在哪、非法档位 ---")
    got, err = bu.normalize_level("")
    check("缺省档位是 interactive（不改既有行为）", got == "interactive" and not err, f"{got}/{err}")
    for alias, want in (("min", "minimal"), ("interact", "interactive"),
                        ("structure", "structured"), ("完整", "full")):
        got, err = bu.normalize_level(alias)
        check(f"别名「{alias}」归一到 {want}", got == want and not err, f"{got}/{err}")
    got, err = bu.normalize_level("huge")
    check("非法档位不猜默认并列出四档", not got and "minimal" in err, f"{got}/{err}")
    check("四档的档案与说明齐全",
          tuple(bu.SNAPSHOT_LEVEL_PROFILES) == bu.SNAPSHOT_LEVELS
          and tuple(bu.SNAPSHOT_LEVEL_LABELS) == bu.SNAPSHOT_LEVELS)
    derived = {0, bu.MAX_ELEMENTS, bu.MAX_ELEMENTS // 4, bu.MAX_PAGE_TEXT, bu.MAX_PAGE_TEXT // 3}
    flat = {v for prof in bu.SNAPSHOT_LEVEL_PROFILES.values() for v in prof.values()
            if type(v) is int}
    check("分级上限全部由既有常量导出（不另立预算）", flat <= derived, str(sorted(flat - derived)))
    check("minimal 不返回正文，interactive 带坐标，full 带链接",
          bu.SNAPSHOT_LEVEL_PROFILES["minimal"]["pageText"] == 0
          and bu.SNAPSHOT_LEVEL_PROFILES["interactive"]["geometry"]
          and not bu.SNAPSHOT_LEVEL_PROFILES["interactive"]["links"]
          and bu.SNAPSHOT_LEVEL_PROFILES["full"]["links"]
          and bu.SNAPSHOT_LEVEL_PROFILES["structured"]["fields"])

    async def scenario():
        with Harness() as h:
            outs: dict[str, str] = {}
            for lvl in bu.SNAPSHOT_LEVELS:
                outs[lvl] = tool_text(await h.fn["browser_snapshot"](lvl))
            check("interactive 的元素行与分级前逐字一致",
                  "[1] button 「提交」 (100,50 80x30)" in outs["interactive"],
                  outs["interactive"][:400])
            check("interactive 不额外报档位（默认档文案不变）",
                  "档位：" not in outs["interactive"], outs["interactive"][:200])
            check("minimal 显式说明不含正文",
                  "本档不返回页面正文" in outs["minimal"]
                  and "这里是一段与流程无关的说明文字" not in outs["minimal"],
                  outs["minimal"][:300])
            check("minimal 不打印坐标", "(100,50 80x30)" not in outs["minimal"],
                  outs["minimal"][:300])
            check("structured 给结构与表单字段",
                  "页面结构：" in outs["structured"] and "表单字段" in outs["structured"]
                  and "帮助中心" not in outs["structured"], outs["structured"][:400])
            check("full 给链接与元素细节",
                  "页面链接：" in outs["full"] and "指向 /go?a=1" in outs["full"]
                  and "层级 3" in outs["full"], outs["full"][:600])
            check("同一页面：minimal 明显小于 interactive，interactive 明显小于 full",
                  len(outs["minimal"]) < len(outs["interactive"]) < len(outs["full"]),
                  f"{len(outs['minimal'])}/{len(outs['interactive'])}/{len(outs['full'])}")
            check("表单字段清单不含已填写的值",
                  "value" not in outs["structured"].split("表单字段")[1].split("可交互元素")[0],
                  outs["structured"][:600])
            check("密码字段在清单里被点名且不当代填", "密码框（引擎拒绝代填）" in outs["structured"],
                  outs["structured"][:600])
            last = h.audits[-1]
            check("审计写明档位与真实回传字数",
                  last["detail"].startswith("full 快照：") and "元素 3 个" in last["detail"],
                  str(last))
            check("分级快照也不把页面正文写进审计",
                  "欢迎来到测试页" not in json.dumps(h.audits, ensure_ascii=False))
            h.calls.clear()
            h.audits.clear()
            out = tool_text(await h.fn["browser_snapshot"]("nope"))
            check("非法档位就地拒绝且不请求放行",
                  "不认识" in out and not any(c["url"].endswith("/approve") for c in h.calls),
                  f"{out[:120]}/{h.calls[:1]}")
            check("非法档位写 denied 审计", h.audits and h.audits[-1]["outcome"] == "denied",
                  str(h.audits[-1:]))
            marks = [e for e in h.cdp.evals if "const LEVEL" in e]
            check("档位真的下发到页面脚本", len(marks) == 4 and
                  all(f"'{lvl}'" in m for lvl, m in zip(bu.SNAPSHOT_LEVELS, marks)),
                  str([m[:40] for m in marks]))

    asyncio.run(scenario())


def record_checks() -> None:
    print("--- 9. 录制：语义步骤、不落正文、草案落盘要人工确认 ---")
    check("像密钥的标签被认出，普通标签不是",
          bu._looks_secret("API Token") and bu._looks_secret("密码")
          and bu._looks_secret("助记词") and not bu._looks_secret("搜索"), "")

    async def scenario():
        with Harness(policy="ask_every_time") as h:
            b = h.browser
            check("没开录制时什么步骤都不记",
                  (h.fn and b._steps == [] and not b._recording), str(b._steps))
            out = tool_text(await h.fn["browser_record"]("start"))
            check("开始录制并说明三条规矩",
                  "已开始录制" in out and "占位符" in out and "不记坐标" in out, out[:200])
            await h.fn["browser_open"]("https://example.com/search?q=%E9%98%BF%E9%87%8C")
            await h.fn["browser_snapshot"]()
            await h.fn["browser_click"](1)
            await h.fn["browser_type"](2, "机密正文")
            await h.fn["browser_scroll"]("down", 2)
            steps = json.loads(json.dumps(b._steps))
            flat = json.dumps(steps, ensure_ascii=False)
            check("录到 4 步（读取类动作不进草案）", len(steps) == 4, flat[:300])
            check("草案里的动作都是可回放的",
                  all(s.get("action") in bu.REPLAY_ACTIONS for s in steps), flat[:300])
            check("草案里没有任何输入正文", "机密正文" not in flat, flat[:300])
            check("草案里 url 去掉查询串",
                  all("?" not in str(s.get("url") or "") for s in steps), flat[:300])
            check("步骤只记标签不记坐标",
                  all("x" not in s and "y" not in s for s in steps), flat[:300])
            check("输入步留占位符", any(str(s.get("input", "")).startswith("{{搜索") for s in steps),
                  flat[:300])
            b._record("type", kind="input[text]", label="验证码", value="123456")
            last = b._steps[-1]
            check("像密钥的输入步就地标记为敏感且不留正文",
                  last.get("sensitive") is True and "123456" not in json.dumps(last, ensure_ascii=False),
                  json.dumps(last, ensure_ascii=False))
            preview = tool_text(await h.fn["browser_record"]("preview"))
            check("预览不落盘且声明未写文件", "尚未写文件" in preview, preview[:160])
            check("预览含输入位清单", "需要使用者提供的输入位" in preview, preview[:400])
            check("预览含可直接抄的回放 JSON", '"action": "click"' in preview, preview[-600:])
            check("preview 阶段没有写出任何 SKILL.md",
                  list(Path(SELFTEST_HOME).rglob("SKILL.md")) == [],
                  str(list(Path(SELFTEST_HOME).rglob("SKILL.md"))))
            out = tool_text(await h.fn["browser_record"]("save"))
            check("save 不给标识就拒写", "需要 skill_id" in out, out[:160])
            out = tool_text(await h.fn["browser_record"]("save", skill_id="bad id!!"))
            check("非法标识被拒", "不合法" in out, out[:160])
            out = tool_text(await h.fn["browser_record"]("save", skill_id="search-flow"))
            check("存成草案技能并说明需人工确认",
                  "已写入" in out and "需人工确认后启用" in out, out[:300])
            md = Path(SELFTEST_HOME) / "skill_pool" / "drafts" / "search-flow" / "SKILL.md"
            check("草案落在 drafts 目录", md.is_file(), str(md))
            body = md.read_text(encoding="utf-8") if md.is_file() else ""
            check("草案正文不含输入正文与查询串",
                  "机密正文" not in body and "q=" not in body, body[:300])
            check("草案状态是 draft 且来源标注为录制",
                  "status: draft" in body and "origin: 浏览器录制" in body, body[:220])
            out = tool_text(await h.fn["browser_record"]("save", skill_id="search-flow"))
            check("重复保存不覆盖已有草案", "已存在" in out and "不覆盖" in out, out[:200])
            status = tool_text(await h.fn["browser_record"]("status"))
            check("保存后录制停止并留下痕迹",
                  "未开始" in status and "search-flow" in status.replace("\\", "/"), status[:260])
            # 「草案不会自己进执行面」是这条链最要紧的性质：直接跑一遍确认/晋升回路
            import skill_registry_runtime as srr
            home = Path(SELFTEST_HOME)
            installed = srr.installed_root(home)
            check("刚存下时技能目录里没有它（未确认不进执行面）",
                  not any(installed.rglob("SKILL.md")) if installed.is_dir() else True,
                  str(list(installed.rglob("SKILL.md"))))
            ok, err2 = srr.promote_draft(home, "search-flow", reviewer="selftest")
            check("人工确认才把草案晋升为正式技能",
                  ok is True and any(installed.rglob("SKILL.md")) if installed.is_dir() else False,
                  f"{ok}/{err2}")

    asyncio.run(scenario())

    with Harness() as h:
        b = h.browser
        b._recording = True
        for i in range(bu.MAX_RECORD_STEPS + 5):
            b._record("click", label=f"元素{i}")
        check("录制步数触顶后置截断标记", b._record_truncated and
              len(b._steps) == bu.MAX_RECORD_STEPS, f"{len(b._steps)}")
        body = b._draft_body(b._steps)
        check("草案如实声明截断", f"在第 {bu.MAX_RECORD_STEPS} 步被截断" in body, body[:200])


def replay_checks() -> None:
    print("--- 10. 回放：改一个输入就能重跑，每步仍过网关 ---")
    steps, err, warn = bu.normalize_replay_steps([])
    check("空步骤直接报错", not steps and "非空数组" in err, f"{steps}/{err}")
    steps, err, warn = bu.normalize_replay_steps([
        {"action": "hover", "label": "菜单"},
        {"action": "click"},
        {"action": "type", "label": "搜索", "value": "阿里云"},
    ])
    check("认不出的动作与缺标签的步骤被跳过并计数",
          len(steps) == 1 and steps[0]["value"] == "阿里云" and warn.count("跳过") == 2,
          f"{steps}/{warn}")
    steps, err, warn = bu.normalize_replay_steps({"steps": [{"action": "open"}]})
    check("open 缺 url 时没有可用步骤", "可用的回放步骤为 0" in err, f"{steps}/{err}")
    check("同名元素提示歧义", "3 个同名元素" in bu._ambiguity({"matches": 3}),
          bu._ambiguity({"matches": 3}))
    check("只有一个同名元素时不多话", bu._ambiguity({"matches": 1}) == ""
          and bu._ambiguity({}) == "", "")

    draft_steps = [
        {"action": "open", "url": "https://example.com/search"},
        {"action": "click", "label": "提交"},
        {"action": "type", "label": "搜索", "input": "{{搜索-3}}"},
        {"action": "select", "label": "城市", "input": "{{城市-4}}"},
        {"action": "scroll", "direction": "down", "amount": 2},
    ]

    async def scenario():
        with Harness(policy="ask_every_time") as h:
            out = tool_text(await h.fn["browser_replay"](draft_steps))
            check("不给输入位时停在需要输入的那一步",
                  "回停在第" in out and "输入位 {{搜索-3}}" in out, out[:400])
            inserts = [p for m, p in h.cdp.calls if m == "Input.insertText"]
            check("没给值的输入步不往页面写任何东西", not inserts, str(inserts))
            check("停在输入位前已完成前两步", "1. 打开" in out and "2. 点击" in out, out[:300])

            out = tool_text(await h.fn["browser_replay"](
                draft_steps, {"{{搜索-3}}": "换个关键词", "{{城市-4}}": "北京"}))
            check("补齐两个输入位后整段跑通", "回放完成：5 步全部走通" in out, out[:500])
            inserts = [p.get("text") for m, p in h.cdp.calls if m == "Input.insertText"]
            check("输入位换成新的值真的写进页面", "换个关键词" in inserts, str(inserts))
            check("回放不回显输入正文", "换个关键词" not in out, out[:400])
            h.calls.clear()
            out = tool_text(await h.fn["browser_replay"](
                draft_steps, {"{{搜索-3}}": "另一个词", "{{城市-4}}": "北京"}))
            approves = [c["payload"].get("action") for c in h.calls
                        if c["url"].endswith("/approve")]
            check("每一步仍按动作过网关",
                  approves == ["open", "click", "type", "select", "scroll"], str(approves))
            check("回放不把正文写进审计",
                  "换个关键词" not in json.dumps(h.audits, ensure_ascii=False))
            check("下拉那一步确实按 select 走而不是打字",
                  approves.count("select") == 1 and approves.count("type") == 1, str(approves))

        with Harness(policy="auto") as h:
            out = tool_text(await h.fn["browser_replay"](
                [{"action": "type", "label": "密码", "value": "hunter2"}]))
            check("敏感标签即使给了值也拒绝代填",
                  "不会代填" in out and "hunter2" not in out, out[:220])
            out = tool_text(await h.fn["browser_replay"](
                [{"action": "type", "label": "不存在的元素", "value": "x"}], {"x": "y"}))
            check("按标签找不到元素时如实停下", "找不到文字标签" in out, out[:220])
            FakeCDP.label_matches = 2
            out = tool_text(await h.fn["browser_replay"](
                [{"action": "click", "label": "提交"}]))
            check("同名元素把歧义说出来", "2 个同名元素" in out, out[:220])
            FakeCDP.label_matches = 1
            out = tool_text(await h.fn["browser_replay"](
                [{"action": "click", "label": "提交"}], {"{{搜索-3}}": "阿里云"}))
            check("回放按标签定位不靠坐标（页面脚本走的是按 label 找元素）",
                  "完成" in out and any("const want = String(" in e for e in h.cdp.evals),
                  out[:160])

    asyncio.run(scenario())


def wait_checks() -> None:
    print("--- 11. browser_wait：等页面就绪，不替模型挂住任务 ---")
    hint = ready_policy().system_hint()
    check("提示词里的等待上限与常量同源", f"{bu.MAX_WAIT_SECONDS:g} 秒" in hint, hint[-500:])
    with Harness() as h:
        doc = h.fn["browser_wait"].__doc__ or ""
        check("工具说明里的秒数与常量同源", "最多 10" in doc, doc[:120])

    async def scenario():
        with Harness() as h:
            out = tool_text(await h.fn["browser_wait"](1))
            check("已就绪时立刻返回", "页面已就绪" in out and "complete" in out, out[:160])
            FakeCDP.ready_state = "loading"
            out = tool_text(await h.fn["browser_wait"](0.4))
            check("未就绪时如实说明而不是谎报",
                  "仍是「loading」" in out and "不是失败" in out, out[:200])
            out = tool_text(await h.fn["browser_wait"](999))
            check("超过上限的等待被压到上限并说明",
                  f"超过上限，已按 {bu.MAX_WAIT_SECONDS:g} 秒" in out, out[:220])
            FakeCDP.ready_state = "complete"
            out = tool_text(await h.fn["browser_wait"]("abc"))
            check("非数字秒数被拒", "必须是数字" in out, out[:120])
            out = tool_text(await h.fn["browser_wait"](0))
            check("0 秒被拒", "必须大于 0" in out, out[:120])
            check("等待不请求网关放行也不读页面正文",
                  not any(c["url"].endswith("/approve") for c in h.calls), str(h.calls[:2]))
            detail = json.dumps(h.audits, ensure_ascii=False)
            check("等待审计只有状态没有正文", "欢迎来到测试页" not in detail, detail[:200])

    asyncio.run(scenario())


def feature_checks() -> None:
    print("--- 12. 能力位门控：网关不认的动作就不注册 ---")
    with Harness() as h:
        names = set(h.toolkit.tools)
        check("默认能力块下不注册 hover/press/evaluate/tabs",
              not {"browser_hover", "browser_press", "browser_evaluate", "browser_tab"} & names,
              str(sorted(names)))
        check("notes 说明为什么没有这些动作",
              any("桌面壳的浏览器网关还没有登记" in n and "hover" in n for n in h.browser.notes()),
              str(h.browser.notes()))
        check("提示词给出等价做法而不是空头按钮",
              "本轮没有的浏览器动作" in h.browser.system_hint()
              and "browser_type(submit=true)" in h.browser.system_hint(),
              h.browser.system_hint()[-400:])

    async def scenario():
        feats = {"snapshot": True, "click": True, "type": True, "screenshot": True,
                 "hover": True, "press": True, "evaluate": True, "tabs": True}
        gate = fake_gate([], tabs=[{"id": "PAGE", "url": PAGE_URL, "title": "测试页",
                                   "active": True}], target="PAGE")
        with Harness(policy="ask_every_time", gate=gate, features=feats) as h:
            names = set(h.toolkit.tools)
            check("网关登记后四个工具同时注册",
                  {"browser_hover", "browser_press", "browser_evaluate", "browser_tab"} <= names,
                  str(sorted(names)))
            check("提示词列出已登记的动作",
                  "本机网关已登记这些浏览器动作" in h.browser.system_hint(),
                  h.browser.system_hint()[-300:])
            h.calls.clear()
            out = tool_text(await h.fn["browser_hover"](1))
            moved = [p for m, p in h.cdp.calls if m == "Input.dispatchMouseEvent"
                     and p.get("type") == "mouseMoved"]
            check("悬停走 hover 动作并派发 mouseMoved",
                  "已悬停" in out and moved and all(p.get("button") == "none" for p in moved),
                  f"{out[:80]}/{moved[:1]}")
            out = tool_text(await h.fn["browser_press"]("tab"))
            keys = [p for m, p in h.cdp.calls if m == "Input.dispatchKeyEvent"]
            check("按键按白名单发送且不落文本",
                  "已按下 Tab" in out and [p.get("key") for p in keys[-2:]] == ["Tab", "Tab"],
                  f"{out[:120]}/{keys[-2:]}")
            out = tool_text(await h.fn["browser_press"]("f13"))
            check("白名单外的按键被拒", "不在可发送的按键里" in out, out[:160])
            # index=0 时按键落到「页面自己聚焦的元素」：不看一眼就可能把 Backspace 送进密码框
            FakeCDP.active_element = {"tag": "input", "type": "password",
                                      "isPassword": True, "label": "密码"}
            before = len([m for m, p in h.cdp.calls if m == "Input.dispatchKeyEvent"])
            approvals_before = len([c for c in h.calls if c["url"].endswith("/approve")])
            out = tool_text(await h.fn["browser_press"]("backspace"))
            keys_after = len([m for m, p in h.cdp.calls if m == "Input.dispatchKeyEvent"])
            check("当前聚焦是密码框时按键被拒（不带序号也挡）",
                  "按键未执行" in out and "密码框" in out, out[:200])
            check("被拒的按键一个也没派发", keys_after == before, f"{before}/{keys_after}")
            check("拒绝发生在过网关之前，不烧一次确认框",
                  len([c for c in h.calls if c["url"].endswith("/approve")]) == approvals_before,
                  str([c["payload"].get("action") for c in h.calls][-2:]))
            check("按键清单里确实有会改输入框的键（这条判断不是摆设）",
                  "backspace" in bu.KEY_CODES and "delete" in bu.KEY_CODES and "space" in bu.KEY_CODES,
                  str(sorted(bu.KEY_CODES)))
            FakeCDP.active_element = {"tag": "input", "type": "text",
                                      "isPassword": False, "label": "搜索"}
            out = tool_text(await h.fn["browser_press"]("tab"))
            check("聚焦在普通输入框时照常发送并说明发给谁",
                  "已按下 Tab" in out and "当前聚焦" in out and "搜索" in out, out[:200])
            FakeCDP.active_element = None
            out = tool_text(await h.fn["browser_press"]("escape"))
            check("页面没有聚焦元素时不编造聚焦对象",
                  "已按下 Escape" in out and "当前聚焦" not in out, out[:200])
            FakeCDP.eval_result = {"type": "number", "value": "42", "truncated": False}
            out = tool_text(await h.fn["browser_evaluate"]("1+1"))
            check("脚本回传类型与结果", "类型 number" in out and "42" in out, out[:160])
            expr_sent = [e for e in h.cdp.evals if "JSON.stringify(value)" in e]
            check("表达式原样交给页面脚本且过 evaluate 动作",
                  "1+1" in expr_sent[-1] if expr_sent else False, str(expr_sent[-1:][:1]))
            check("脚本内容不进审计",
                  "1+1" not in json.dumps(h.audits, ensure_ascii=False), "")
            FakeCDP.eval_result = {"type": "string", "value": "x" * 100, "truncated": True}
            out = tool_text(await h.fn["browser_evaluate"]("long()"))
            check("结果截断被如实标注", "已截断" in out, out[:120])
            h.browser._recording = True
            await h.fn["browser_hover"](1)
            check("悬停不进草案但计入跳过数",
                  h.browser._record_skipped >= 1 and h.browser._steps == [],
                  f"{h.browser._record_skipped}/{h.browser._steps}")

        tabs = [{"id": "PAGE", "url": PAGE_URL, "title": "测试页", "active": True}]
        calls2: list = []
        gate2 = fake_gate(calls2, tabs=tabs, target="PAGE")
        with Harness(policy="auto", gate=gate2, features=feats) as h:
            out = tool_text(await h.fn["browser_tab"]("list"))
            check("列出标签页", "标签页操作 list 已完成" in out and "PAGE" in out, out[:200])
            out = tool_text(await h.fn["browser_tab"]("switch"))
            check("switch/close 缺 tab_id 被拒", "需要 tab_id" in out, out[:160])
            out = tool_text(await h.fn["browser_tab"]("new", url="file:///c:/x"))
            check("新建标签页只允许 http/https", "只允许打开 http/https" in out, out[:200])
            out = tool_text(await h.fn["browser_tab"]("switch", tab_id="TAB-2"))
            check("网关换了 targetId 时如实报切换完成",
                  "switch" in out and "错误" not in out, out[:220])
            check("切换后连接按新 targetId 重建",
                  h.cdp.ws_url.endswith("/devtools/page/TAB-2"), h.cdp.ws_url)
            out = tool_text(await h.fn["browser_tab"]("close", tab_id="TAB-2"))
            check("关闭后回到剩余标签页", "close" in out, out[:200])
            ensure = [c for c in h.calls if c["url"].endswith("/ensure")]
            check("ensure 会带上要用的 tabId",
                  any(c["payload"].get("tabId") for c in ensure), str(ensure[:2]))

        gate3 = fake_gate([], tabs=None, target="PAGE")
        with Harness(policy="auto", gate=gate3, features=feats) as h:
            out = tool_text(await h.fn["browser_tab"]("list"))
            check("网关没有 /tabs 时说清是桌面半边没接",
                  "没有实现标签页接口" in out and "不会假装成功" in out, out[:260])

        gate4 = fake_gate([], tabs=[{"id": "T1"}], target="PAGE", follow_tab=False)
        with Harness(policy="auto", gate=gate4, features=feats) as h:
            out = tool_text(await h.fn["browser_tab"]("switch", tab_id="OTHER"))
            check("收了 tabId 却返回同一个页面时报错而不是假装切换",
                  "切换标签页没有生效" in out, out[:260])

        blind = {"snapshot": True, "click": True, "type": True}
        with Harness(policy="auto", features=blind) as h:
            check("只给部分能力位时只注册认识的那几件",
                  not {"browser_hover", "browser_press", "browser_evaluate", "browser_tab"}
                  & set(h.toolkit.tools) and "browser_evaluate" not in str(h.toolkit.tools),
                  str(sorted(h.toolkit.tools)))

    asyncio.run(scenario())

    root = PROJECT_ROOT / "src-tauri" / "src" / "browser" / "mod.rs"
    text = root.read_text(encoding="utf-8") if root.is_file() else ""
    block = text.split("pub fn is_known_action", 1)
    white = block[1].split("}", 1)[0] if len(block) > 1 else ""
    for action in ("open", "snapshot", "click", "type", "select", "scroll", "screenshot"):
        check(f"Rust 白名单里有 {action}（引擎常驻注册的前提）", f'"{action}"' in white, white[:200])
    for action in bu.FEATURE_ONLY_ACTIONS:
        check(f"Rust 还不认识 {action} ⇒ 引擎按能力位门控是对的；"
              "若这行变红说明桌面半边已接上，要同步删掉 FEATURE_ONLY_ACTIONS 那一项",
              f'"{action}"' not in white, white[:220])


def main() -> int:
    pure_checks()
    url_checks()
    registration_checks()
    tool_checks()
    formatter_checks()
    level_checks()
    record_checks()
    replay_checks()
    wait_checks()
    feature_checks()
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
