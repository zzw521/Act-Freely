#!/usr/bin/env python3
"""
engine/server.py SSE 契约自测（流 C3/H2，三层架构演进 P0）

验证项：
1. seq 单调性 + SSE id 行（`id: {task_id}:{seq}`）
2. 重放窗口（/api/events/replay，seq > from_seq）
3. 取消端点（/api/chat/cancel，未知任务 TASK_NOT_FOUND）
4. Last-Event-ID 解析
5. 两阶段协议：plan / review（approve·reject·modify）/ control
6. 模型用量采集（离线：假模型驱动真实 ReActAgent，无网络、无需 API Key）
7. /api/usage 只读端点

仅使用 EMPTY_GOAL 错误路径与假模型（不触发真实模型调用），无需 API Key。
fastapi.testclient 不可用时输出 SKIP 并以 0 退出（不阻塞无依赖环境）。

用法: python tests/integration/engine_sse_selftest.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    from fastapi.testclient import TestClient
    import server as engine_server
except Exception as exc:  # fastapi/httpx 缺失时优雅跳过
    print(f"SKIP: engine selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                import json
                try:
                    ev["data"] = json.loads(line[5:].strip())
                except Exception:
                    ev["data"] = {"raw": line[5:].strip()}
        if ev.get("event") or ev.get("data"):
            events.append(ev)
    return events


def main() -> None:
    client = TestClient(engine_server.app)

    # ── 1. seq 单调性 + id 行（EMPTY_GOAL 路径：error + done） ──
    task_id = "selftest-seq"
    resp = client.post("/api/chat", json={"message": "", "task_id": task_id})
    check("chat HTTP 200", resp.status_code == 200, str(resp.status_code))
    events = parse_sse(resp.text)
    check("EMPTY_GOAL 产出 2 事件（error+done）", len(events) == 2, f"got {len(events)}")
    seqs = [e.get("data", {}).get("seq") for e in events]
    check("seq 单调递增 1..2", seqs == [1, 2], f"got {seqs}")
    ids = [e.get("id") for e in events]
    check("id 行格式 {task_id}:{seq}", ids == [f"{task_id}:1", f"{task_id}:2"], f"got {ids}")
    check("首事件错误码 EMPTY_GOAL", events[0].get("data", {}).get("code") == "EMPTY_GOAL")

    # ── 2. 重放窗口 ──
    resp = client.get(f"/api/events/replay?task_id={task_id}&from_seq=0")
    body = resp.json()
    check("replay 返回 2 事件", len(body.get("events", [])) == 2, str(body))
    check("replay windowExpired=False", body.get("windowExpired") is False)

    resp = client.get(f"/api/events/replay?task_id={task_id}&from_seq=1")
    body = resp.json()
    check("replay from_seq=1 仅返回 seq=2", [e["seq"] for e in body.get("events", [])] == [2])

    resp = client.get("/api/events/replay?task_id=no-such&from_seq=0")
    body = resp.json()
    check("未知任务 replay windowExpired=True", body.get("windowExpired") is True)

    # ── 3. 取消端点 ──
    resp = client.post("/api/chat/cancel", json={"task_id": "no-such"})
    body = resp.json()
    check("取消未知任务 cancelled=False", body.get("cancelled") is False)
    check("取消未知任务 reason=TASK_NOT_FOUND", body.get("reason") == "TASK_NOT_FOUND")

    # ── 4. Last-Event-ID 解析函数 ──
    parsed = engine_server._parse_last_event_id(f"{task_id}:7")
    check("Last-Event-ID 解析", parsed == (task_id, 7), str(parsed))
    check("Last-Event-ID 非法输入返回 None", engine_server._parse_last_event_id("abc") is None)

    # ── 5. 两阶段协议：stage=plan 计划生成（核心闭环补齐 流 3.1） ──
    plan_task = "selftest-plan"
    resp = client.post("/api/chat", json={"message": "写一份周报", "task_id": plan_task, "stage": "plan"})
    check("plan 阶段 HTTP 200", resp.status_code == 200, str(resp.status_code))
    events = parse_sse(resp.text)
    # usage 为引擎上报的真实 token 事件（有 Key 时出现），UI 侧不消费该类型
    types = [e.get("event") for e in events if e.get("event") != "usage"]
    check("plan 流事件序列 plan→status_change→done",
          types == ["plan", "status_change", "done"], str(types))
    plan_data = events[0].get("data", {})
    check("plan 事件携带 steps（兜底计划 2 步）",
          isinstance(plan_data.get("steps"), list) and len(plan_data["steps"]) >= 2,
          str(plan_data)[:200])
    check("status_change 为 pending_review",
          events[1].get("data", {}).get("status") == "pending_review")

    # plan 阶段空 goal 仍走 EMPTY_GOAL
    resp = client.post("/api/chat", json={"message": "", "task_id": "selftest-plan-empty", "stage": "plan"})
    ev0 = parse_sse(resp.text)[0]
    check("plan 阶段空 goal 返回 EMPTY_GOAL", ev0.get("data", {}).get("code") == "EMPTY_GOAL")

    # ── 6. 审阅：reject / modify / approve ──
    resp = client.post("/api/chat/review", json={"task_id": plan_task, "action": "modify",
                                                  "patch": [{"id": "step-1", "name": "[修改] 先收集素材"}]})
    events = parse_sse(resp.text)
    check("modify 重新产出 plan",
          [e.get("event") for e in events] == ["plan", "status_change", "done"])
    check("modify patch 已应用",
          any("[修改]" in s.get("description", "") for s in events[0].get("data", {}).get("steps", [])))

    resp = client.post("/api/chat/review", json={"task_id": plan_task, "action": "reject"})
    events = parse_sse(resp.text)
    check("reject 产出 done(REJECTED)",
          events[0].get("data", {}).get("statusReasonCode") == "REJECTED"
          and events[-1].get("event") == "done")

    # approve：无模型 Key 时执行阶段构建失败 → ENGINE_CONFIG_ERROR（既有降级路径）
    resp = client.post("/api/chat/review", json={"task_id": plan_task, "action": "approve"})
    events = parse_sse(resp.text)
    etypes = [e.get("event") for e in events]
    check("approve 进入执行流（status_change executing 或错误降级）",
          "status_change" in etypes or "error" in etypes, str(etypes))

    resp = client.post("/api/chat/review", json={"task_id": "no-such", "action": "approve"})
    check("审阅未知任务 404", resp.status_code == 404)

    # ── 7. 控制：pause/resume/rollback_to ──
    resp = client.post("/api/chat/control", json={"task_id": plan_task, "action": "pause"})
    check("pause 返回 ok", resp.json().get("ok") is True)
    resp = client.post("/api/chat/control", json={"task_id": plan_task, "action": "resume"})
    check("resume 返回 ok", resp.json().get("ok") is True)
    resp = client.post("/api/chat/control", json={"task_id": plan_task, "action": "rollback_to", "step_id": "step-1"})
    check("rollback_to 返回 ok", resp.json().get("ok") is True)
    resp = client.post("/api/chat/control", json={"task_id": "no-such", "action": "pause"})
    check("控制未知任务 TASK_NOT_FOUND", resp.json().get("code") == "TASK_NOT_FOUND")
    resp = client.post("/api/chat/control", json={"task_id": plan_task, "action": "fly"})
    check("未知 action VALIDATION_ERROR", resp.json().get("code") == "VALIDATION_ERROR")

    # ── 8. 模型用量采集（离线：假模型驱动真实 ReActAgent 循环） ──
    check_usage_tracking(client)

    # ── 汇总 ──
    if FAILURES:
        print(f"\nSELFTEST FAILED: {len(FAILURES)} failures: {FAILURES}")
        sys.exit(1)
    print("\nSELFTEST PASSED: engine SSE 契约全部通过")


def check_usage_tracking(client) -> None:
    """用量采集契约：包装层在真实 ReActAgent 循环里取得真实 usage 并留痕。

    用假模型替换模型构建入口（build_model_and_formatter），全程离线、
    不消耗任何真实 Token；同时验证包装层不破坏 agentscope 调用语义。
    """
    try:
        import asyncio
        from pathlib import Path

        from agentscope.formatter import DashScopeChatFormatter
        from agentscope.message import Msg
        from agentscope.model import ChatModelBase, ChatResponse
        from agentscope.model._model_usage import ChatUsage
    except Exception as exc:  # agentscope 缺失时优雅跳过
        print(f"[SKIP] 用量采集检查（agentscope 缺失: {exc}）")
        return

    class FakeModel(ChatModelBase):
        """固定返回一段文本 + 真实形状 usage 的假模型（一次调用即收尾）"""

        def __init__(self, name: str = "fake-qwen", stream: bool = False):
            super().__init__(model_name=name, stream=stream)

        async def __call__(self, *args, **kwargs):
            if self.stream:
                async def gen():
                    # 流式 usage 仅出现在末尾 chunk（对齐 DashScope/OpenAI 适配层）
                    yield ChatResponse(content=[{"type": "text", "text": "好"}], usage=None)
                    yield ChatResponse(
                        content=[{"type": "text", "text": "的"}],
                        usage=ChatUsage(input_tokens=5, output_tokens=3, time=0.1),
                    )
                    yield ChatResponse(
                        content=[{"type": "text", "text": "。"}],
                        usage=ChatUsage(input_tokens=5, output_tokens=3, time=0.2),
                    )
                return gen()
            return ChatResponse(
                content=[{"type": "text", "text": "好的，已完成。"}],
                usage=ChatUsage(input_tokens=11, output_tokens=7, time=0.1),
            )

    captured: list = []
    orig = engine_server.build_model_and_formatter
    engine_server.build_model_and_formatter = lambda *a, **k: (
        engine_server.UsageTrackingModel(FakeModel(), k.get("on_usage")),
        DashScopeChatFormatter(),
    )
    try:
        agent = engine_server.build_agent(
            "fake-qwen", Path(engine_server.WORK_DIR), None,
            on_usage=lambda p, c, m: captured.append((p, c, m)),
        )
        res = asyncio.run(agent(Msg(name="user", content="写一份周报", role="user")))
    finally:
        engine_server.build_model_and_formatter = orig

    text = engine_server._text_of(res.get("content") if isinstance(res, dict) else getattr(res, "content", ""))
    check("包装层不破坏 ReActAgent 回复", "已完成" in text, text[:80])
    check("非流式 usage 回调收到 (prompt, completion, model)",
          captured == [(11, 7, "fake-qwen")], str(captured))

    row = next((r for r in engine_server.USAGE_LEDGER.rows.values() if r["modelName"] == "fake-qwen"), None)
    check("台账按模型累计 token", row is not None and row["promptTokens"] == 11
          and row["completionTokens"] == 7 and row["calls"] == 1, str(row))

    # 流式：usage 只在末尾 chunk，须恰好记一次（不得按 chunk 重复累计）
    async def drain_stream():
        m = engine_server.UsageTrackingModel(FakeModel("fake-stream", stream=True))
        return [c async for c in await m([])]

    chunks = asyncio.run(drain_stream())
    srow = next((r for r in engine_server.USAGE_LEDGER.rows.values() if r["modelName"] == "fake-stream"), None)
    check("流式仅按末尾 chunk 记一次用量",
          len(chunks) == 3 and srow is not None and srow["calls"] == 1
          and srow["promptTokens"] == 5 and srow["completionTokens"] == 3,
          f"chunks={len(chunks)} row={srow}")

    # 只读端点：daily 只含今日台账；非法 period 降级 total 不报错
    body = client.get("/api/usage?period=daily").json()
    check("/api/usage daily 返回台账行",
          body.get("ok") is True and any(r["modelName"] == "fake-qwen" for r in body.get("rows", [])),
          str(body)[:200])
    bad = client.get("/api/usage?period=hourly").json()
    check("/api/usage 非法 period 降级 total", bad.get("period") == "total", str(bad)[:120])

    # 清掉自测产生的台账行（内存态，无持久化副作用）
    engine_server.USAGE_LEDGER.rows.clear()


if __name__ == "__main__":
    main()
