#!/usr/bin/env python3
"""
执行面 P1-9 沙箱策略自测：runtime.sandbox → 真实工具/文件边界

锁住「策略保存了却没人执行」这条假承诺：
1. 工具白名单外的工具不执行；工具黑名单优先于白名单
2. L0 只读拒绝写文件；L2 放开目录限制但黑名单仍生效
3. 文件白名单扩展可访问目录，文件黑名单优先于工作目录
4. 策略损坏 → fail-closed（全部工具被拒）且说明可见
5. 未配置策略时保持原有工作目录兜底（不因新代码放宽边界）
6. 拒绝事件 sandbox_denied 带 tool/reason/source，Rust 据此落审计

离线运行：假模型 + 真实 ReActAgent / Toolkit，无网络、无需 API Key。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_sandbox_selftest.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    import sandbox_runtime as sr
    from fastapi.testclient import TestClient
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine sandbox selftest unavailable ({exc})")
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
        super().__init__(model_name="fake-sandbox-model", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        return ChatResponse(content=[{"type": "text", "text": "已完成。"}], usage=None)


def patch_model():
    engine_server.build_model_and_formatter = lambda *a, **k: (
        RecordingModel(), DashScopeChatFormatter()
    )


def bundle(policy: dict) -> dict:
    return {"skills": [], "sandbox": policy}


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def pure_checks(tmp: Path) -> None:
    print("--- 1. 策略解析与判定（纯函数层） ---")
    assert sr.SandboxPolicy.from_runtime(None).configured is False
    check("未下发 sandbox 段 → 未配置", sr.SandboxPolicy.from_runtime(None).tool_denial("x") is None)

    allow = sr.SandboxPolicy.from_runtime(bundle({
        "workspaceId": "ws-a", "configured": True, "executionLevel": "L1",
        "toolAllowlist": ["view_text_file"], "toolBlocklist": [], 
        "fileAllowlist": [], "fileBlocklist": [],
    }), work_dir=tmp)
    check("白名单内的工具放行", allow.tool_denial("view_text_file") is None)
    check("白名单外的工具被拒", (allow.tool_denial("write_text_file") or ("", "", ""))[1] == "tool_allowlist")

    both = sr.SandboxPolicy.from_runtime(bundle({
        "configured": True, "executionLevel": "L1",
        "toolAllowlist": ["view_text_file", "write_text_file"],
        "toolBlocklist": ["write_text_file"],
    }), work_dir=tmp)
    check("黑名单优先于白名单", (both.tool_denial("write_text_file") or ("", "", ""))[1] == "tool_blocklist")

    l0 = sr.SandboxPolicy.from_runtime(bundle({
        "configured": True, "executionLevel": "L0",
        "toolAllowlist": ["view_text_file", "write_text_file"],
    }), work_dir=tmp)
    check("L0 拒绝写工具", (l0.tool_denial("write_text_file") or ("", "", ""))[1] == "execution_level")
    check("L0 仍允许读工具", l0.tool_denial("view_text_file") is None)

    broken = sr.SandboxPolicy.from_runtime({"sandbox": {"error": "POLICY_CORRUPT: bad"}}, work_dir=tmp)
    check("策略损坏 fail-closed（读工具也拒）",
          (broken.tool_denial("view_text_file") or ("", "", ""))[1] == "policy_error", str(broken))
    check("策略损坏有可见说明", any("fail-closed" in n or "拒绝" in n for n in broken.notes()), str(broken.notes()))

    outside = tmp.parent / (tmp.name + "-outside")
    outside.mkdir(exist_ok=True)
    files = sr.SandboxPolicy.from_runtime(bundle({
        "configured": True, "executionLevel": "L1",
        "fileAllowlist": [str(outside)], "fileBlocklist": [str(outside / "secret.txt")],
    }), work_dir=tmp)
    check("工作目录内放行", files.file_denial(tmp / "a.txt") is None)
    check("文件白名单扩展目录放行", files.file_denial(outside / "ok.txt") is None)
    check("文件黑名单优先级最高",
          (files.file_denial(outside / "secret.txt") or ("", "", ""))[1] == "file_blocklist")
    other = tmp.parent / (tmp.name + "-other")
    other.mkdir(exist_ok=True)
    check("白名单外目录被拒", (files.file_denial(other / "x.txt") or ("", "", ""))[1] == "file_allowlist")

    l2 = sr.SandboxPolicy.from_runtime(bundle({
        "configured": True, "executionLevel": "L2",
        "fileBlocklist": [str(outside / "secret.txt")],
    }), work_dir=tmp)
    check("L2 放开目录限制", l2.file_denial(other / "x.txt") is None)
    check("L2 黑名单仍生效", (l2.file_denial(outside / "secret.txt") or ("", "", ""))[1] == "file_blocklist")


def toolkit_checks(tmp: Path) -> None:
    print("--- 2. 内置文件工具真实边界 ---")
    patch_model()
    outside = tmp.parent / (tmp.name + "-ext")
    outside.mkdir(exist_ok=True)
    policy = sr.SandboxPolicy.from_runtime(bundle({
        "workspaceId": "ws-a", "configured": True, "executionLevel": "L1",
        "fileAllowlist": [str(outside)], "fileBlocklist": [str(outside / "secret.txt")],
    }), work_dir=tmp)
    agent = engine_server.build_agent(None, tmp, None, runtime=bundle({}), sandbox=policy)
    write = agent.toolkit.tools["write_text_file"].original_func
    view = agent.toolkit.tools["view_text_file"].original_func

    ok = tool_text(write(str(outside / "ok.txt"), "hi"))
    check("白名单目录可写", "已成功写入" in ok, ok[:120])
    bad = tool_text(write(str(tmp.parent / "nope.txt"), "x"))
    check("白名单外写入被拒", "沙箱拒绝" in bad, bad[:160])
    secret = tool_text(view(str(outside / "secret.txt")))
    check("黑名单文件读取被拒", "沙箱拒绝" in secret, secret[:160])

    l0 = sr.SandboxPolicy.from_runtime(bundle({
        "configured": True, "executionLevel": "L0",
    }), work_dir=tmp)
    l0_agent = engine_server.build_agent(None, tmp, None, runtime=bundle({}), sandbox=l0)
    l0_write = l0_agent.toolkit.tools["write_text_file"].original_func
    denied = tool_text(l0_write(str(tmp / "x.txt"), "x"))
    check("L0 工具层直接调用也拒写", "沙箱拒绝" in denied and "L0" in denied, denied[:160])

    plain = engine_server.build_agent(None, tmp, None, runtime=bundle({}))
    plain_write = plain.toolkit.tools["write_text_file"].original_func
    escaped = tool_text(plain_write(str(tmp.parent / "escape.txt"), "x"))
    check("未配置策略仍限定工作目录", "超出工作目录" in escaped, escaped[:160])


async def _run_middleware(mw, kwargs, calls):
    async def next_handler(**kw):
        calls.append(kw.get("tool_call"))

        async def agen():
            yield ToolResponse(content=[TextBlock(type="text", text="handler-ran")])

        return agen()

    out = []
    async for resp in mw(kwargs, next_handler):
        out.append(resp)
    return out


def middleware_checks(tmp: Path) -> None:
    print("--- 3. 工具门禁中间件与拒绝事件 ---")
    policy = sr.SandboxPolicy.from_runtime(bundle({
        "workspaceId": "ws-a", "configured": True, "executionLevel": "L1",
        "toolAllowlist": ["view_text_file"], "toolBlocklist": ["danger_tool"],
        "fileBlocklist": [str(tmp / "secret.txt")],
    }), work_dir=tmp)
    events = []
    mw = sr.tool_middleware(policy, lambda ev, data: events.append((ev, data)), "task-1")

    calls = []
    out = asyncio.run(_run_middleware(
        mw, {"tool_call": {"name": "danger_tool", "input": {}}}, calls
    ))
    check("黑名单工具不执行", not calls and "沙箱策略拒绝" in tool_text(out[0]), str(calls))
    denied = [d for e, d in events if e == "sandbox_denied"]
    check("发出 sandbox_denied 事件", len(denied) == 1, str(events))
    check("事件带工具/来源/原因",
          denied and denied[0].get("tool") == "danger_tool"
          and denied[0].get("source") == "tool_blocklist"
          and "黑名单" in denied[0].get("reason", ""), str(denied))
    check("拒绝同时摊成步骤事件",
          any(e == "step_start" and d.get("stepType") == "sandbox" for e, d in events), str(events))

    events.clear()
    calls.clear()
    out = asyncio.run(_run_middleware(
        mw, {"tool_call": {"name": "view_text_file", "input": {"file_path": str(tmp / "secret.txt")}}},
        calls,
    ))
    check("路径黑名单在中间件层拦截", not calls and "黑名单" in tool_text(out[0]), tool_text(out[0]))
    check("文件拒绝事件来源正确",
          any(e == "sandbox_denied" and d.get("source") == "file_blocklist" for e, d in events),
          str(events))

    events.clear()
    calls.clear()
    out = asyncio.run(_run_middleware(
        mw, {"tool_call": {"name": "view_text_file", "input": {"file_path": str(tmp / "ok.txt")}}},
        calls,
    ))
    check("白名单内工具与路径正常放行", len(calls) == 1 and "handler-ran" in tool_text(out[0]))
    check("放行不产生拒绝事件", events == [], str(events))


def http_checks(tmp: Path) -> None:
    print("--- 4. /api/chat 端到端：说明可见 + 损坏策略 fail-closed ---")
    patch_model()
    engine_server.WORK_DIR = tmp
    client = TestClient(engine_server.app)
    try:
        RECORDED.clear()
        resp = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "sb-notes",
            "runtime": bundle({
                "workspaceId": "ws-a", "configured": True, "executionLevel": "L0",
                "toolAllowlist": ["view_text_file"],
            }),
        })
        check("配置了沙箱仍能完成任务", "event: done" in resp.text)
        check("沙箱约束进可见说明",
              "工具白名单已生效" in resp.text and "L0 只读" in resp.text, resp.text[:300])

        RECORDED.clear()
        broken = client.post("/api/chat", json={
            "message": "随便聊聊",
            "task_id": "sb-broken",
            "runtime": bundle({"error": "POLICY_CORRUPT: sandbox 策略解析失败"}),
        })
        check("策略损坏时任务不崩", "event: done" in broken.text)
        check("损坏策略说明全部拒绝且带原因",
              "所有工具与文件访问会被拒绝" in broken.text and "POLICY_CORRUPT" in broken.text,
              broken.text[:300])
    finally:
        engine_server.WORK_DIR = Path(".")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-sandbox-"))
    pure_checks(tmp)
    toolkit_checks(tmp)
    middleware_checks(tmp)
    http_checks(tmp)
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine sandbox selftest: ALL PASS")


if __name__ == "__main__":
    main()
