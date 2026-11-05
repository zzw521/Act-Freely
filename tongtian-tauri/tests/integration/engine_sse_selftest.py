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

    # ── 7. 模型用量采集（离线：假模型驱动真实 ReActAgent 循环） ──
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

    # 台账是落盘的（追加式增量日志，重启后重放）。自测若共用真实数据目录，
    # 第二遍跑就会在上一遍的数字上继续累加，「calls == 1」这类断言必然假失败。
    # 这里把台账换成临时文件，跑完再换回去。
    import os
    import tempfile

    original_ledger = engine_server.USAGE_LEDGER
    original_persist = os.environ.get("TT_ENGINE_USAGE_PERSIST")
    os.environ["TT_ENGINE_USAGE_PERSIST"] = "0"
    temp_log = engine_server.us.UsageDeltaLog()
    temp_log.path = lambda: Path(tempfile.gettempdir()) / "tt-selftest-usage.jsonl"
    engine_server.USAGE_LEDGER = engine_server.UsageLedger(log=temp_log)
    try:
        _run_usage_checks(client)
    finally:
        engine_server.USAGE_LEDGER = original_ledger
        if original_persist is None:
            os.environ.pop("TT_ENGINE_USAGE_PERSIST", None)
        else:
            os.environ["TT_ENGINE_USAGE_PERSIST"] = original_persist
        Path(tempfile.gettempdir(), "tt-selftest-usage.jsonl").unlink(missing_ok=True)


def _run_usage_checks(client) -> None:
    """真正的用量断言体（在隔离过的临时台账文件里跑，重复执行结果一致）"""
    import asyncio

    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.model._model_usage import ChatUsage

    class FakeModel(ChatModelBase):
        """固定返回一段文本 + 真实形状 usage 的假模型（一次调用即收尾）"""

        def __init__(self, name: str = "fake-qwen", stream: bool = False):            super().__init__(model_name=name, stream=stream)

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
            # 引擎当前回调签名带 latency/cost/currency/priced，测试只关心前三个
            on_usage=lambda p, c, m, *rest: captured.append((p, c, m)),
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
    stream_seen: list = []

    async def drain_stream():
        m = engine_server.UsageTrackingModel(
            FakeModel("fake-stream", stream=True),
            on_usage=lambda *args: stream_seen.append(args),
        )
        return [c async for c in await m([])]

    chunks = asyncio.run(drain_stream())
    srow = next((r for r in engine_server.USAGE_LEDGER.rows.values() if r["modelName"] == "fake-stream"), None)
    check("流式仅按末尾 chunk 记一次用量",
          len(chunks) == 3 and srow is not None and srow["calls"] == 1
          and srow["promptTokens"] == 5 and srow["completionTokens"] == 3,
          f"chunks={len(chunks)} row={srow}")
    # 首包（D-40 第 6 项）：回调第 8 个参数 = 第一个有内容增量的耗时。
    # 基准要拿它与 Rust harness 的首包对比，缺这一列就只能报整包。
    first_call = stream_seen[0] if stream_seen else ()
    check("流式回调带得出首包耗时（第 8 个参数）",
          len(first_call) == 8 and isinstance(first_call[7], int)
          and first_call[3] is not None and first_call[7] <= first_call[3],
          str(stream_seen)[:200])
    # 只有 usage、没有正文的收尾包不许算成首包
    quiet: list = []

    async def drain_usage_only():
        class LateContent(FakeModel):
            async def __call__(self, *args, **kwargs):
                async def gen():
                    yield ChatResponse(content=[], usage=ChatUsage(
                        input_tokens=4, output_tokens=1, time=0.1))
                    yield ChatResponse(content=[{"type": "text", "text": "字"}], usage=None)
                return gen()

        m = engine_server.UsageTrackingModel(
            LateContent("fake-quiet", stream=True),
            on_usage=lambda *args: quiet.append(args),
        )
        return [c async for c in await m([])]

    asyncio.run(drain_usage_only())
    check("空内容的 usage 包不算首包（首包 ≤ 整包且取自真有正文的那一条）",
          bool(quiet) and len(quiet[0]) == 8 and quiet[0][7] is not None
          and quiet[0][7] <= quiet[0][3], str(quiet)[:200])

    # 压缩摘要走结构化输出：模型没回 metadata 时 AgentScope 只留一条日志就继续跑，
    # 包装层必须把「这轮其实没压成」记下来，界面才敢说，也才不会每轮白烧一次摘要调用
    class StructuredModel(FakeModel):
        def __init__(self, name: str, metadata):
            super().__init__(name, stream=True)
            self._metadata = metadata

        async def __call__(self, *args, **kwargs):
            async def gen():
                yield ChatResponse(
                    content=[{"type": "text", "text": "摘要"}],
                    metadata=self._metadata,
                    usage=ChatUsage(input_tokens=9, output_tokens=2, time=0.1),
                )
            return gen()

    async def drain_structured(model):
        m = engine_server.UsageTrackingModel(model)
        # 压缩调用的形状：带 structured_model、不带 tools；生成器消费完才定性
        chunks = [c async for c in await m([], structured_model=object())]
        return m.last_structured_ok, len(chunks)

    ok_meta = asyncio.run(drain_structured(StructuredModel("fake-struct-ok", {"task_overview": "基准"})))
    bad_meta = asyncio.run(drain_structured(StructuredModel("fake-struct-bad", None)))
    check("结构化输出拿到摘要时记 True", ok_meta[0] is True, str(ok_meta))
    check("结构化输出空返回时记 False（压缩静默失败要能被上层看见）",
          bad_meta[0] is False, str(bad_meta))

    async def drain_plain():
        m = engine_server.UsageTrackingModel(FakeModel("fake-plain", stream=True))
        [c async for c in await m([])]           # 不带 structured_model 的普通调用
        return m.last_structured_ok

    check("普通调用不改写结构化结论（保持 None，不误判成压缩失败）",
          asyncio.run(drain_plain()) is None, "")
    trackers = engine_server._usage_trackers(
        engine_server.RoutingChatModel([
            engine_server.UsageTrackingModel(FakeModel("fake-inner"))
        ]))
    check("能从路由/包装层里找到用量包装层", len(trackers) == 1, str(trackers))

    # 只读端点：daily 只含今日台账；非法 period 降级 total 不报错
    body = client.get("/api/usage?period=daily").json()
    check("/api/usage daily 返回台账行",
          body.get("ok") is True and any(r["modelName"] == "fake-qwen" for r in body.get("rows", [])),
          str(body)[:200])
    bad = client.get("/api/usage?period=hourly").json()
    check("/api/usage 非法 period 降级 total", bad.get("period") == "total", str(bad)[:120])

    # 自测产生的台账行只活在临时文件里；这里再清一次内存态，避免同进程后续检查读到
    engine_server.USAGE_LEDGER.rows.clear()


if __name__ == "__main__":
    main()
