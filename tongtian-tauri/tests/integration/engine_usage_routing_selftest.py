#!/usr/bin/env python3
"""
engine/server.py 成本/延迟台账 + 模型故障转移自测

锁定四类事实：
1. 成本只在配置了输入/输出单价时计算（tokens × 每 1M 单价 → micros），
   未配置单价的调用计入 unpricedCalls，不能当 0 元；
2. 延迟只对真实样本取平均，旧数据/无样本返回 null；
3. 故障转移只在「尚未吐出首个 chunk」前发生；已输出的流式响应绝不拼接
   另一个模型的内容；全部候选失败时保留每个候选的错误；
4. 转移链按候选各自的价格记账，压缩窗口取链上最小上下文窗口。

不发起任何网络请求：模型用假实现替换，台账使用进程内实例。

用法: python tests/integration/engine_usage_routing_selftest.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_TMP_HOME = tempfile.mkdtemp(prefix="tt-usage-routing-")
os.environ["TONGTIANWORK_HOME"] = _TMP_HOME

try:
    import context_budget as cb
    import server as engine_server
    from agentscope.model import ChatModelBase
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine usage/routing selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeChatModel(ChatModelBase):
    """可编排行为的假模型：异常 / 非流式响应 / 流式响应 / 首块后失败"""

    def __init__(self, name: str, behavior, stream: bool = False):
        super().__init__(model_name=name, stream=stream)
        self.behavior = behavior
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        behavior = self.behavior
        if isinstance(behavior, BaseException):
            raise behavior
        if behavior == "empty_stream":
            async def empty():
                if False:  # pragma: no cover
                    yield None
            return empty()
        if behavior == "raise_before_first_chunk":
            async def broken():
                if False:  # pragma: no cover
                    yield None
                raise RuntimeError(f"{self.model_name} 连接失败")
            return broken()
        if isinstance(behavior, list) and behavior and behavior[0] == "chunks_then_fail":
            async def partial():
                yield {"text": "半截内容", "usage": {"input_tokens": 5, "output_tokens": 1}}
                raise RuntimeError(f"{self.model_name} 流中断")
            return partial()
        if isinstance(behavior, list):
            async def chunks():
                for item in behavior:
                    yield item
            return chunks()
        return behavior


def reset_ledger() -> None:
    engine_server.USAGE_LEDGER.rows.clear()


def run_async(coro):
    return asyncio.run(coro)


def test_price_helpers() -> None:
    reset_ledger()
    prices = engine_server._prices_of(
        {"prices": {"inputPerM": 0.8, "outputPerM": 2, "currency": "cny"}}
    )
    check(
        "单价解析：完整配置转成大写币种",
        prices == {"inputPerM": 0.8, "outputPerM": 2.0, "currency": "CNY"},
        str(prices),
    )
    check(
        "单价解析：缺一侧/负数/NaN 一律视为未配置",
        engine_server._prices_of({"prices": {"inputPerM": 1}}) is None
        and engine_server._prices_of(
            {"prices": {"inputPerM": -1, "outputPerM": 2}}
        ) is None
        and engine_server._prices_of(
            {"prices": {"inputPerM": float("nan"), "outputPerM": 2}}
        ) is None,
        "负价/半套价格未被拦截",
    )
    cost, currency = engine_server.cost_micros_of(prices, 600, 300)
    check(
        "成本换算：600 输入 + 300 输出按每 1M 单价 → 1080 micros",
        cost == 1080 and currency == "CNY",
        f"{cost} {currency}",
    )
    check(
        "成本换算：未配置单价返回 None（不按 0 元）",
        engine_server.cost_micros_of(None, 600, 300) == (None, None),
    )


def test_ledger_latency_cost() -> None:
    reset_ledger()
    ledger = engine_server.USAGE_LEDGER
    ledger.add("m1", 100, 50, latency_ms=120, cost_micros=1234, currency="cny", priced=True)
    ledger.add("m1", 10, 5, latency_ms=80, cost_micros=100, currency="USD", priced=True)
    ledger.add("m2", 7, 3)  # 旧式/未配置单价的调用：无耗时、无成本

    rows = ledger.aggregate("total")
    m1 = next(r for r in rows if r["modelName"] == "m1")
    m2 = next(r for r in rows if r["modelName"] == "m2")
    check(
        "台账：m1 延迟按两次样本平均（120/80 → 100ms）",
        m1["avgLatencyMs"] == 100 and m1["latencySamples"] == 2,
        str(m1),
    )
    check(
        "台账：多币种成本分开累计并按币种排序",
        m1["costs"] == [{"currency": "CNY", "micros": 1234}, {"currency": "USD", "micros": 100}],
        str(m1.get("costs")),
    )
    check(
        "台账：m1 两次调用均已计价",
        m1["pricedCalls"] == 2 and m1["unpricedCalls"] == 0,
        str(m1),
    )
    check(
        "台账：m2 无耗时样本 → null；未计价 1 次",
        m2["avgLatencyMs"] is None
        and m2["latencySamples"] == 0
        and m2["pricedCalls"] == 0
        and m2["unpricedCalls"] == 1
        and m2["costs"] == [],
        str(m2),
    )

    totals = ledger.totals("total", rows)
    check(
        "汇总：延迟按样本加权（100ms）",
        totals["avgLatencyMs"] == 100 and totals["latencySamples"] == 2,
        str(totals),
    )
    check(
        "汇总：成本合并两币种，调用数 3（计价 2 / 未计价 1）",
        totals["costs"] == [{"currency": "CNY", "micros": 1234}, {"currency": "USD", "micros": 100}]
        and totals["calls"] == 3
        and totals["pricedCalls"] == 2
        and totals["unpricedCalls"] == 1,
        str(totals),
    )

    days = ledger.days("total")
    check("趋势：同一天只有一桶且含两个模型", len(days) == 1 and days[0]["models"] == 2, str(days))
    check(
        "趋势：日均耗时与成本口径与明细一致",
        days[0]["avgLatencyMs"] == 100
        and days[0]["costs"] == [{"currency": "CNY", "micros": 1234}, {"currency": "USD", "micros": 100}]
        and days[0]["pricedCalls"] == 2
        and days[0]["unpricedCalls"] == 1,
        str(days[0]),
    )


def test_usage_tracking_model() -> None:
    reset_ledger()
    seen: list[tuple] = []
    prices = engine_server._prices_of(
        {"prices": {"inputPerM": 0.8, "outputPerM": 2, "currency": "CNY"}}
    )
    inner = FakeChatModel(
        "priced-model",
        {"usage": {"input_tokens": 600, "output_tokens": 300}, "content": "ok"},
    )
    tracked = engine_server.UsageTrackingModel(
        inner, on_usage=lambda *args: seen.append(args), prices=prices
    )

    async def exercise():
        return await tracked()

    result = run_async(exercise())
    rows = engine_server.USAGE_LEDGER.aggregate("total")
    check("采集：非流式响应原样返回", result.get("content") == "ok", str(result))
    check(
        "采集：回调带 token / 模型 / 延迟 / 成本 / 币种 / 计价标记",
        len(seen) == 1
        and seen[0][0] == 600
        and seen[0][1] == 300
        and seen[0][2] == "priced-model"
        and seen[0][3] >= 0
        and seen[0][4] == 1080
        and seen[0][5] == "CNY"
        and seen[0][6] is True,
        str(seen),
    )
    check(
        "采集：台账同步记入成本与耗时",
        len(rows) == 1
        and rows[0]["costs"] == [{"currency": "CNY", "micros": 1080}]
        and rows[0]["latencySamples"] == 1,
        str(rows),
    )

    reset_ledger()
    stream_inner = FakeChatModel(
        "stream-priced",
        [
            {"text": "a"},
            {"text": "b", "usage": {"input_tokens": 100, "output_tokens": 20}},
            {"text": "c"},
        ],
        stream=True,
    )
    stream_seen: list[tuple] = []
    stream_tracked = engine_server.UsageTrackingModel(
        stream_inner,
        on_usage=lambda *args: stream_seen.append(args),
        prices=engine_server._prices_of(
            {"prices": {"inputPerM": 1, "outputPerM": 2, "currency": "CNY"}}
        ),
    )

    async def consume():
        agen = await stream_tracked()
        return [chunk["text"] async for chunk in agen]

    texts = run_async(consume())
    stream_rows = engine_server.USAGE_LEDGER.aggregate("total")
    check("采集：流式内容完整转发", texts == ["a", "b", "c"], str(texts))
    check(
        "采集：流式只按末次 usage 记一次（100/20 → 140 micros）",
        len(stream_seen) == 1
        and stream_seen[0][0] == 100
        and stream_seen[0][1] == 20
        and stream_seen[0][4] == 140
        and len(stream_rows) == 1
        and stream_rows[0]["calls"] == 1,
        f"seen={stream_seen} rows={stream_rows}",
    )


def test_routing() -> None:
    switches: list[dict] = []

    async def non_stream():
        primary = FakeChatModel("primary", RuntimeError("主模型 500"))
        backup = FakeChatModel("backup", {"usage": {"input_tokens": 1, "output_tokens": 1}, "content": "备用"})
        routed = engine_server.RoutingChatModel([primary, backup], on_switch=switches.append)
        result = await routed()
        return result, primary, backup

    result, primary, backup = run_async(non_stream())
    check(
        "路由：非流式主模型失败 → 切到备用并返回备用结果",
        result.get("content") == "备用" and primary.calls == 1 and backup.calls == 1,
        str(result),
    )
    check(
        "路由：切换回调带出失败模型、目标模型与原因",
        len(switches) == 1
        and switches[0]["from"] == "primary"
        and switches[0]["to"] == "backup"
        and "500" in switches[0]["reason"],
        str(switches),
    )

    switches.clear()

    async def stream_before_first_chunk():
        primary = FakeChatModel("primary", "raise_before_first_chunk", stream=True)
        backup = FakeChatModel("backup", [{"text": "b1"}, {"text": "b2"}], stream=True)
        routed = engine_server.RoutingChatModel([primary, backup], on_switch=switches.append)
        agen = await routed()
        return [chunk["text"] async for chunk in agen], primary, backup

    texts, primary, backup = run_async(stream_before_first_chunk())
    check(
        "路由：流式首块前失败 → 只用备用模型的内容",
        texts == ["b1", "b2"] and backup.calls == 1,
        str(texts),
    )
    check("路由：首块前失败会记录一次切换", len(switches) == 1, str(switches))

    switches.clear()

    async def stream_after_first_chunk():
        primary = FakeChatModel("primary", ["chunks_then_fail"], stream=True)
        backup = FakeChatModel("backup", [{"text": "不应出现"}], stream=True)
        routed = engine_server.RoutingChatModel([primary, backup], on_switch=switches.append)
        agen = await routed()
        first = await agen.__anext__()
        error = None
        try:
            await agen.__anext__()
        except Exception as exc:  # noqa: BLE001
            error = exc
        return first["text"], error, backup

    first_text, error, backup = run_async(stream_after_first_chunk())
    check(
        "路由：已输出首块后失败 → 抛出原错误，不换模型拼接",
        first_text == "半截内容"
        and isinstance(error, RuntimeError)
        and "流中断" in str(error)
        and backup.calls == 0
        and switches == [],
        f"first={first_text} error={error} backupCalls={backup.calls} switches={switches}",
    )

    errors = []

    async def all_fail():
        a = FakeChatModel("a", RuntimeError("a 失败"))
        b = FakeChatModel("b", RuntimeError("b 失败"))
        routed = engine_server.RoutingChatModel([a, b], on_switch=switches.append)
        try:
            await routed()
        except engine_server.ModelRouteError as exc:
            errors.append(exc)

    run_async(all_fail())
    check(
        "路由：全部候选失败 → ModelRouteError 保留每个原因",
        len(errors) == 1
        and [f["model"] for f in errors[0].failures] == ["a", "b"]
        and "a 失败" in str(errors[0])
        and "b 失败" in str(errors[0]),
        str(errors),
    )

    async def empty():
        primary = FakeChatModel("primary", "empty_stream", stream=True)
        backup = FakeChatModel("backup", [{"text": "backup"}], stream=True)
        routed = engine_server.RoutingChatModel([primary, backup], on_switch=switches.append)
        agen = await routed()
        return [chunk async for chunk in agen], backup

    chunks, backup = run_async(empty())
    check(
        "路由：空响应视为正常空结果，不切备用",
        chunks == [] and backup.calls == 0,
        f"chunks={chunks} backupCalls={backup.calls}",
    )

    async def stream_flag():
        a = FakeChatModel("a", {"usage": {}})
        b = FakeChatModel("b", {"usage": {}})
        routed = engine_server.RoutingChatModel([a, b])
        routed.stream = False
        return a.stream, b.stream, routed.stream

    flags = run_async(stream_flag())
    check("路由：stream 改写传播到所有候选", flags == (False, False, False), str(flags))


def test_build_wiring() -> None:
    """build_model_and_formatter 真实装配：每个候选各用自己的价格记账"""
    reset_ledger()
    built: list[FakeChatModel] = []
    original = engine_server._build_chat_model

    def fake_build(name, api_key, base_url, kind, stream):
        behavior = RuntimeError("主模型不可用") if name == "primary" else {
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "content": "备用结果",
        }
        model = FakeChatModel(name, behavior, stream=stream)
        built.append(model)
        return model, object()

    switches: list[dict] = []
    usages: list[tuple] = []
    config = {
        "baseUrl": "https://models.example/v1",
        "apiKey": "sk-test",
        "modelName": "primary",
        "displayName": "主模型",
        "prices": {"inputPerM": 1, "outputPerM": 2, "currency": "CNY"},
        "fallbacks": [
            {
                "modelName": "backup",
                "baseUrl": "https://models.example/v1",
                "apiKey": "sk-test-2",
                "displayName": "备用模型",
                "prices": {"inputPerM": 3, "outputPerM": 4, "currency": "CNY"},
            }
        ],
    }

    async def exercise():
        model, _formatter = engine_server.build_model_and_formatter(
            model_config=config,
            stream=False,
            on_usage=lambda *args: usages.append(args),
            on_route_switch=switches.append,
            pii_mode="off",
        )
        return await model()

    engine_server._build_chat_model = fake_build
    try:
        result = run_async(exercise())
    finally:
        engine_server._build_chat_model = original

    rows = engine_server.USAGE_LEDGER.aggregate("total")
    check(
        "装配：主模型失败后真实走到备用模型",
        result.get("content") == "备用结果" and [m.model_name for m in built] == ["primary", "backup"],
        f"result={result} built={[m.model_name for m in built]}",
    )
    check("装配：切到备用时回调一次", len(switches) == 1 and switches[0]["to"] == "backup", str(switches))
    check(
        "装配：按备用模型自己的单价记账（1000×3 + 500×4 = 5000 micros）",
        len(usages) == 1
        and usages[0][2] == "backup"
        and usages[0][4] == 5000
        and len(rows) == 1
        and rows[0]["modelName"] == "backup"
        and rows[0]["costs"] == [{"currency": "CNY", "micros": 5000}],
        f"usages={usages} rows={rows}",
    )


def test_fallback_helpers() -> None:
    usable = engine_server._usable_fallbacks(
        {
            "fallbacks": [
                {"modelName": "ok", "baseUrl": "https://x/v1"},
                {"modelName": "no-base"},
                {"modelName": "foreign", "baseUrl": "https://x/v1", "kind": "other"},
                {"modelName": ""},
            ]
        },
        "custom",
    )
    check(
        "转移候选：自定义接入只保留同形态且带 baseUrl 的目标",
        [item["modelName"] for item in usable] == ["ok", "foreign"],
        str(usable),
    )
    usable_dash = engine_server._usable_fallbacks(
        {
            "fallbacks": [
                {"modelName": "same-provider"},
                {"modelName": "custom", "baseUrl": "https://x/v1"},
            ]
        },
        "dashscope",
    )
    check(
        "转移候选：环境接入不收自定义端点（formatter 不同）",
        [item["modelName"] for item in usable_dash] == ["same-provider"],
        str(usable_dash),
    )

    summary = engine_server._fallback_summary(
        {
            "displayName": "主模型",
            "fallbacks": [{"displayName": "备用 A"}, {"modelName": "backup-b"}],
        }
    )
    check(
        "转移链摘要：包含完整链路与不拼接承诺",
        "主模型 → 备用 A → backup-b" in summary and "不会拼接" in summary,
        summary,
    )
    check(
        "转移链摘要：没有可用目标时返回空串",
        engine_server._fallback_summary({"displayName": "主模型", "fallbacks": []}) == "",
    )
    notes = engine_server._fallback_notes(
        {"fallbackNotes": ["备用模型缺少密钥，已跳过", "  ", "目标接入方式不同"]}
    )
    check("转移说明：过滤空项并保留真实原因", notes == ["备用模型缺少密钥，已跳过", "目标接入方式不同"], str(notes))


def test_context_window() -> None:
    check(
        "压缩窗口：取转移链最小上下文窗口",
        cb.fallback_window(
            {"fallbacks": [{"contextWindow": 131072}, {"contextWindow": 32768}]}
        ) == 32768,
    )
    check(
        "压缩窗口：非法/过小窗口被忽略",
        cb.fallback_window(
            {"fallbacks": [{"contextWindow": 10}, {"contextWindow": "x"}, {"contextWindow": 65536}]}
        ) == 65536,
    )
    check(
        "压缩窗口：没有可用目标时返回 None",
        cb.fallback_window({"fallbacks": [{"contextWindow": 10}]}) is None
        and cb.fallback_window({"fallbacks": []}) is None
        and cb.fallback_window(None) is None,
    )


def main() -> None:
    try:
        test_price_helpers()
        test_ledger_latency_cost()
        test_usage_tracking_model()
        test_routing()
        test_build_wiring()
        test_fallback_helpers()
        test_context_window()
    finally:
        reset_ledger()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine usage/routing selftest: ALL PASS")


if __name__ == "__main__":
    main()
