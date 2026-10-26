#!/usr/bin/env python3
"""执行面 P1/P2 电脑操控自测：runtime 档位 → 工具边界 → 截图进模型

锁住这些不能退让的约束：
1. 档位禁用 / 非 Windows / 网关缺失 / 地址非回环 / 模型无视觉能力 → 不注册工具，
   原因可见，提示词也不提；token 不出现在任何提示或错误里；
2. 截屏与操作只走本机回环网关（这里替换 `_gate_post` 模拟），引擎不直接碰系统 API；
3. 坐标以截图像素为准，按缩放比例与多显示器原点换算；越界/未截屏直接拒绝；
4. 输入正文只出现在给网关的请求里，不出现在工具返回文案里；次数有上限；
5. 工具结果里的 base64 截图被提升为用户消息图片，formatter 不再落临时文件；
6. /api/chat 端到端：系统提示带电脑操控说明，模型第二轮真的收到截图。

离线运行：替换引擎侧网关调用，不触碰真实桌面。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_computer_use_selftest.py
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
    import computer_use as cu
    from fastapi.testclient import TestClient
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.tool import Toolkit
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine computer-use selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
RECORDED = []
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/"
    "lK3P9wAAAABJRU5ErkJggg=="
)


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
        "features": {"screenshot": True, "input": True},
        "policy": "auto",
        "persisted": True,
        "gateUrl": "http://127.0.0.1:54321/computer",
        "gateToken": "selftest-token",
    }
    block.update(overrides)
    return {"computerUse": block}


def ready_policy(**overrides) -> cu.ComputerUse:
    return cu.ComputerUse.from_runtime(bundle(**overrides), vision=True, task_id="task-cu")


def fake_gate(calls: list, shot_overrides: dict | None = None, action_error: str = ""):
    shot = {
        "ok": True,
        "mime": "image/png",
        "imageBase64": PNG_B64,
        "width": 800,
        "height": 400,
        "originalWidth": 1600,
        "originalHeight": 800,
        "originX": -1920,
        "originY": 0,
        "scaled": True,
    }
    shot.update(shot_overrides or {})

    def post(url: str, token: str, payload: dict, timeout: int = 0):
        calls.append({"url": url, "token": token, "payload": dict(payload)})
        if url.endswith("/screenshot"):
            return dict(shot), ""
        if action_error:
            return None, action_error
        return {"ok": True, "action": f"模拟执行 {payload.get('action')}"}, ""

    return post


def pure_checks() -> None:
    print("--- 1. 档位/平台/网关/视觉能力判定（纯函数层） ---")
    auto = ready_policy()
    check("auto 档位就绪", auto.ready and auto.notes() and "自动执行" in auto.notes()[0],
          str(auto.notes()))
    hint = auto.system_hint()
    check("提示词列出两个工具", "screen_capture" in hint and "computer_action" in hint, hint)
    check("token 不出现在提示词", "selftest-token" not in hint, hint)

    ask = ready_policy(policy="ask_every_time")
    check("每次询问档位就绪且说明确认框", ask.ready and "确认框" in ask.system_hint())

    disabled = ready_policy(policy="disabled")
    check("禁用档位不就绪", not disabled.ready and disabled.system_hint() == "")
    check("禁用档位不制造噪声说明", disabled.notes() == [], str(disabled.notes()))

    other = ready_policy(supported=False, platform="macos")
    check("非 Windows 不就绪", not other.ready and other.notes() == [])
    check("非 Windows 原因可见", "Windows" in other.reason, other.reason)

    no_gate = cu.ComputerUse.from_runtime(
        {"computerUse": {
            "supported": True, "platform": "windows", "policy": "auto",
            "features": {}, "gateError": "网关没起来",
        }},
        vision=True,
    )
    check("网关缺失不就绪并说明", not no_gate.ready and "网关没起来" in no_gate.notes()[0],
          str(no_gate.notes()))

    remote = ready_policy(gateUrl="http://10.0.0.8:8000/computer")
    check("非回环网关地址被拒", not remote.ready and "地址无效" in remote.reason, remote.reason)

    no_vision = cu.ComputerUse.from_runtime(bundle(), vision=False)
    check("模型无视觉能力不就绪", not no_vision.ready and "视觉能力" in str(no_vision.notes()),
          str(no_vision.notes()))

    broken = cu.ComputerUse.from_runtime(
        bundle(policy="disabled", error="COMPUTER_USE_INVALID_POLICY: bad"),
        vision=True,
    )
    check("坏档位 fail-closed 且原因可见",
          not broken.ready and "读取失败" in broken.notes()[0], str(broken.notes()))

    check("所有说明都不含 token",
          all("selftest-token" not in n for n in (*auto.notes(), *no_gate.notes(), *broken.notes())))


def registration_checks() -> None:
    print("--- 2. 工具注册：未就绪一律不注册 ---")
    toolkit = Toolkit()
    ready_policy().install(toolkit)
    check("就绪时注册截屏与操作",
          "screen_capture" in toolkit.tools and "computer_action" in toolkit.tools,
          str(list(toolkit.tools)))

    empty = Toolkit()
    ready_policy(policy="disabled").install(empty)
    check("禁用时不注册任何电脑操控工具",
          "screen_capture" not in empty.tools and "computer_action" not in empty.tools,
          str(list(empty.tools)))

    no_vision = Toolkit()
    cu.ComputerUse.from_runtime(bundle(), vision=False).install(no_vision)
    check("无视觉能力时不注册", not no_vision.tools, str(list(no_vision.tools)))


def tool_checks() -> None:
    print("--- 3. 截屏/操作：坐标换算、越界、错误与次数上限 ---")
    original = cu._gate_post
    calls: list = []
    cu._gate_post = fake_gate(calls)
    try:
        computer = ready_policy(policy="ask_every_time")
        toolkit = Toolkit()
        computer.install(toolkit)
        capture = toolkit.tools["screen_capture"].original_func
        action = toolkit.tools["computer_action"].original_func

        before = tool_text(action("click", 10, 10))
        check("未截屏时坐标操作被拒", "还没有截过屏" in before, before[:160])
        check("未截屏不会请求网关", calls == [], str(calls))

        shot = capture()
        text = tool_text(shot)
        blocks = [b for b in shot.content if isinstance(b, dict)]
        images = [b for b in blocks if b.get("type") == "image"]
        check("截屏返回图片块", len(images) == 1 and images[0]["source"]["type"] == "base64",
              str(blocks)[:200])
        check("截屏说明尺寸与缩放", "800x400" in text and "1600x800" in text, text)
        check("截屏只请求 /screenshot",
              len(calls) == 1 and calls[0]["url"].endswith("/screenshot"), str(calls[:1]))
        check("请求带任务号且不出现在说明里",
              calls[0]["payload"].get("taskId") == "task-cu", str(calls[0]["payload"]))

        calls.clear()
        out = tool_text(action("click", 400, 200))
        check("坐标按缩放与负原点换算（400,200 → -1120,400）",
              calls and calls[0]["payload"].get("x") == -1120 and calls[0]["payload"].get("y") == 400,
              str(calls[:1]))
        check("操作成功文案不回显输入正文", "已执行" in out, out[:160])

        calls.clear()
        out = tool_text(action("click", 800, 10))
        check("截图范围外坐标被拒", "超出最近截图范围" in out, out[:160])
        check("越界不会请求网关", calls == [], str(calls))

        calls.clear()
        out = tool_text(action("type", text="账号 alice 密码 hunter2"))
        payload = calls[0]["payload"] if calls else {}
        check("输入动作把正文交给网关", payload.get("text", "").endswith("hunter2"), str(payload))
        check("输入动作返回文案不泄露正文",
              "hunter2" not in out and "alice" not in out, out[:160])

        calls.clear()
        out = tool_text(action("key", keys="ctrl+shift+t"))
        check("按键组合原样交给网关（危险组合由执行器拒绝）",
              calls and calls[0]["payload"].get("keys") == "ctrl+shift+t", str(calls[:1]))

        calls.clear()
        out = tool_text(action("scroll", amount=0))
        check("滚动 0 格被拒", "滚动格数" in out, out[:160])

        cu._gate_post = fake_gate(calls, action_error="COMPUTER_USE_DENIED_BY_USER: 用户拒绝了本次电脑操控")
        out = tool_text(action("click", 1, 1))
        check("确认框拒绝透传且不泄露 token",
              "用户拒绝" in out and "selftest-token" not in out, out[:200])

        cu._gate_post = fake_gate(calls)
        computer._captures = cu.MAX_CAPTURES
        out = tool_text(capture())
        check("截屏次数达到上限后拒绝", f"上限 {cu.MAX_CAPTURES}" in out, out[:160])
        computer._captures = 0
        computer._actions = cu.MAX_ACTIONS
        out = tool_text(action("type", text="x"))
        check("操作次数达到上限后拒绝", f"上限 {cu.MAX_ACTIONS}" in out, out[:160])
    finally:
        cu._gate_post = original


def formatter_checks() -> None:
    print("--- 4. 工具结果截图提升为用户消息图片 ---")

    def tool_msg(call_id: str, images: list[str]) -> Msg:
        output = [{"type": "text", "text": "done"}]
        for data in images:
            output.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            })
        return Msg(
            name="system",
            role="system",
            content=[{"type": "tool_result", "id": call_id, "name": "screen_capture", "output": output}],
        )

    old = tool_msg("old", ["OLD"])
    batch_a = tool_msg("a", ["A"])
    batch_b = tool_msg("b", ["B1", "B2"])
    msgs = [Msg(name="system", content="系统提示", role="system"), old, batch_a, batch_b]

    cleaned, images, _ = cu.lift_tool_images(msgs, limit=2)
    check("只提升最近两张截图",
          [img["source"]["data"] for img in images] == ["B1", "B2"], str(images))
    blob = json.dumps([msg.content for msg in cleaned], ensure_ascii=False)
    check("较早截图被替换为省略说明", "较早的截图已省略" in blob, blob[:300])
    check("被提升的截图不再留在工具结果里", '"data": "B1"' not in blob and '"data": "B2"' not in blob,
          blob[:300])
    check("非截图工具结果原样保留", "done" in blob)

    class CaptureFormatter:
        def __init__(self):
            self.received = None

        async def format(self, msgs, *args, **kwargs):
            self.received = msgs
            return [{"ok": True}]

    async def scenario():
        base = CaptureFormatter()
        wrapped = cu.ToolImageFormatter(base, max_images=2)
        result = await wrapped.format(msgs)
        check("formatter 包装透传返回值", result == [{"ok": True}], str(result))
        last = base.received[-1]
        check("追加一条带图片的用户消息",
              last.role == "user" and any(b.get("type") == "image" for b in last.content),
              str(last.content)[:200])
        check("用户消息说明坐标基准", "坐标以最后一张截图" in last.content[0]["text"],
              last.content[0]["text"])
        check("模型入参里的图片是 base64 data URI",
              any(b.get("source", {}).get("data") == "B2" for b in last.content),
              str(last.content)[:200])

        plain = CaptureFormatter()
        untouched = await cu.ToolImageFormatter(plain, max_images=2).format([
            Msg(name="user", content="没有工具图片", role="user"),
        ])
        check("没有工具图片时不追加消息", untouched == [{"ok": True}] and len(plain.received) == 1,
              str(plain.received))

    asyncio.run(scenario())


class ToolCallingModel(ChatModelBase):
    """第一轮截屏，第二轮作答；记录 formatter 真正发给模型的消息"""

    def __init__(self):
        super().__init__(model_name="gpt-4o", stream=False)
        self.calls = 0

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[
                {"type": "tool_use", "id": "call-shot-1", "name": "screen_capture", "input": {}},
            ])
        return ChatResponse(content=[{"type": "text", "text": "已看到桌面并完成检查。"}])


def http_checks() -> None:
    print("--- 5. /api/chat 端到端：截图真的进入模型上下文 ---")
    original_post = cu._gate_post
    original_vision = os.environ.get("TT_VISION")
    calls: list = []
    cu._gate_post = fake_gate(calls)
    os.environ["TT_VISION"] = "on"
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ToolCallingModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-computer-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    RECORDED.clear()
    try:
        resp = client.post("/api/chat", json={
            "message": "看看当前桌面，告诉我有什么",
            "task_id": "computer-e2e",
            "runtime": bundle(),
        })
        blob = "\n".join(RECORDED)
        check("执行流正常收尾", "event: done" in resp.text and "event: error" not in resp.text,
              resp.text[:200])
        check("启用说明进事件流", "电脑操控已启用" in resp.text, resp.text[:300])
        check("系统提示包含电脑操控工具",
              "screen_capture" in blob and "computer_action" in blob, blob[:300])
        check("截屏请求真实发出", any(c["url"].endswith("/screenshot") for c in calls), str(calls))
        check("模型第二轮收到截图 data URI",
              "data:image/png;base64," in blob and PNG_B64[:24] in blob, blob[-500:])
        check("模型收到坐标基准说明", "坐标以最后一张截图的像素为准" in blob, blob[-500:])
        check("最终回答产出", "已看到桌面并完成检查" in resp.text, resp.text[-300:])
        check("token 不出现在事件流", "selftest-token" not in resp.text)
    finally:
        cu._gate_post = original_post
        if original_vision is None:
            os.environ.pop("TT_VISION", None)
        else:
            os.environ["TT_VISION"] = original_vision


def main() -> int:
    pure_checks()
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
