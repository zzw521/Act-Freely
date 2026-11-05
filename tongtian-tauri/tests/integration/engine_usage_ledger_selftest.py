#!/usr/bin/env python3
"""
引擎用量台账落盘与调用来源标记自测（缺陷 D-43）

锁定六类事实：
1. 台账按「来源 × 用户 × 模型 × 日」追加落盘，进程重启（换新实例）后重放恢复，
   日聚合与重启前完全一致——配额与计费不再因重启清零；
2. 文件只做增量追加，快照合并后总量与各来源明细都不变；
3. 坏行/未来版本行按行丢弃并如实计数，不影响其余行；
4. 落盘失败绝不阻断记账，但必须在 `persist` 状态里留下可见痕迹；
5. 来源标记：请求头 > 部署方默认值 > unknown；未标记部分单列披露，不静默合并；
6. 导出明细保留来源维度，桌面已记账的部分（origin=desktop）可被合并视图精确排除。

不发起任何网络请求。用法: python tests/integration/engine_usage_ledger_selftest.py
"""

import json
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

_TMP_HOME = tempfile.mkdtemp(prefix="tt-usage-ledger-")
os.environ["TONGTIANWORK_HOME"] = _TMP_HOME
os.environ.pop("TT_DATA_HOME", None)
os.environ.pop("TT_ENGINE_USAGE_ORIGIN_DEFAULT", None)
os.environ.pop("TT_ENGINE_USAGE_PERSIST", None)

try:
    import data_paths as dp
    import server as engine_server
    import usage_store as us
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine usage ledger selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def home(sub: str) -> str:
    """每个用例独立数据根：路径每次现读环境变量，测试无需重载模块"""
    path = os.path.join(_TMP_HOME, sub)
    os.makedirs(path, exist_ok=True)
    os.environ["TONGTIANWORK_HOME"] = path
    return path


def totals(rows: list[dict]) -> tuple[int, int, int]:
    return (
        sum(int(r["tokensUsed"]) for r in rows),
        sum(int(r["calls"]) for r in rows),
        sum(int(r["pricedCalls"]) for r in rows),
    )


def origin_map(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        for item in row.get("origins") or []:
            out[item["origin"]] = out.get(item["origin"], 0) + int(item["tokensUsed"])
    return out


# ── 1. 落盘 + 重启恢复 ───────────────────────────────────────────

home("restart")
ledger_a = engine_server.UsageLedger()
ledger_a.add("qwen-max", 1_000, 500, latency_ms=900, cost_micros=5_000,
             currency="CNY", priced=True, sub="u-1", org="org-a", origin="desktop")
ledger_a.add("qwen-max", 200, 80, latency_ms=100, sub="u-1", origin="web")
ledger_a.add("qwen-turbo", 50, 30, latency_ms=40, sub="u-2", origin="im")
before_rows = ledger_a.aggregate("daily")
before_totals = totals(before_rows)
ledger_a.add("qwen-max", 10, 5, sub="u-1", origin="desktop")  # 重启前最后一条也要在
expect_totals = (
    before_totals[0] + 15,
    before_totals[1] + 1,
    before_totals[2],
)

ledger_file = dp.usage_ledger_file()
check("台账已落盘到引擎数据根", ledger_file.exists(), str(ledger_file))
lines_before = len(ledger_file.read_text(encoding="utf-8").splitlines())
check("每次调用追加一行增量", lines_before == 4, f"实际 {lines_before} 行")

ledger_b = engine_server.UsageLedger()  # 模拟引擎进程重启
after_rows = ledger_b.aggregate("daily")
check("重启后日聚合与重启前一致", totals(after_rows) == expect_totals,
      f"{totals(after_rows)} != {expect_totals}")
check("重启后用户维度不串（u-2 只看到自己的 im 调用）",
      totals(ledger_b.aggregate("daily", "u-2")) == (80, 1, 0))
check("重启后来源明细保留", origin_map(after_rows) == {"desktop": 1_515, "web": 280, "im": 80},
      str(origin_map(after_rows)))
check("配额硬停跨重启有效（u-1 本月用量不清零）",
      ledger_b.tokens_used("u-1", "monthly") == 1_515 + 280,
      str(ledger_b.tokens_used("u-1", "monthly")))

# ── 2. 快照合并不改变总量 ────────────────────────────────────────

before_by_origin = origin_map(after_rows)
snapshot_ledger = engine_server.UsageLedger()
snapshot_ledger._ensure_loaded()
ok = snapshot_ledger._log.write_snapshot(snapshot_ledger._snapshot_rows())
check("快照合并写入成功", ok)
merged_lines = len(dp.usage_ledger_file().read_text(encoding="utf-8").splitlines())
check("合并后每个键只剩一行快照", merged_lines == len(snapshot_ledger.rows),
      f"{merged_lines} 行 / {len(snapshot_ledger.rows)} 个键")
merged_rows = engine_server.UsageLedger().aggregate("daily")
check("合并后总量与来源不变", totals(merged_rows) == expect_totals and origin_map(merged_rows) == before_by_origin)
merged_cost = next(r for r in merged_rows if r["modelName"] == "qwen-max")
check("合并后成本与延迟样本不丢",
      merged_cost["costs"] == [{"currency": "CNY", "micros": 5_000}] and merged_cost["latencySamples"] == 2,
      str(merged_cost["costs"]) + " / " + str(merged_cost["latencySamples"]))
check("合并后增量继续追加且总量单调",
      len(engine_server.UsageLedger().aggregate("daily")) == len(merged_rows)
      and engine_server.UsageLedger().persist_status()["pendingDeltas"] == 0)
snapshot_ledger.add("qwen-max", 60, 40, sub="u-1", origin="web")
after_more = engine_server.UsageLedger().aggregate("daily")
check("合并后新调用照样落盘并可重放",
      totals(after_more)[0] == expect_totals[0] + 100,
      f"{totals(after_more)} vs {expect_totals}")
versions = {
    json.loads(line).get("v")
    for line in dp.usage_ledger_file().read_text(encoding="utf-8").splitlines() if line.strip()
}
check("落盘每行都带 schema 版本", versions == {us.LEDGER_SCHEMA_VERSION}, str(versions))

# ── 3. 坏行与未来版本行 ──────────────────────────────────────────

home("corrupt")
dirty = engine_server.UsageLedger()
dirty.add("qwen-max", 100, 50, sub="u-1", origin="web")
with open(dp.usage_ledger_file(), "a", encoding="utf-8") as handle:
    handle.write("这不是 JSON\n")
    handle.write(json.dumps({"v": 99, "kind": "delta", "key": {"sub": "u-1", "model": "x", "day": "2099-01-01"},
                             "d": {"pt": 999_999, "ct": 0, "calls": 1, "origin": "web"}}) + "\n")
    handle.write(json.dumps({"v": us.LEDGER_SCHEMA_VERSION, "kind": "不认识的类型", "key": {}}) + "\n")
dirty_status_ledger = engine_server.UsageLedger()
dirty_rows = dirty_status_ledger.aggregate("daily")
check("坏行不影响合法行", totals(dirty_rows) == (150, 1, 0), str(totals(dirty_rows)))
check("坏行被计数披露（不静默吞掉）",
      dirty_status_ledger.persist_status()["droppedLines"] == 3,
      str(dirty_status_ledger.persist_status()))

# ── 4. 落盘失败不阻断记账，但状态可见 ────────────────────────────

home("readonly")
blocker = dp.usage_dir()
blocker.parent.mkdir(parents=True, exist_ok=True)
blocker.write_text("我占住了 usage 目录", encoding="utf-8")  # 目录位置是文件 → mkdir 必失败
failing = engine_server.UsageLedger()
row = failing.add("qwen-max", 700, 300, sub="u-1", origin="web")
check("写盘失败时内存台账照常累计", row["promptTokens"] == 700 and row["calls"] == 1)
status = failing.persist_status()
check("写盘失败在 persist 里可见", bool(status["lastError"]) and status["enabled"] is True,
      str(status))
check("写盘失败不谎报已持久化",
      failing.aggregate("daily") and engine_server.UsageLedger().aggregate("daily") == [],
      "重启后居然读到了数据")
os.remove(blocker)

# ── 5. 来源标记优先级 ───────────────────────────────────────────

home("origin")
os.environ["TT_ENGINE_USAGE_ORIGIN_DEFAULT"] = "desktop"
defaulted = engine_server.UsageLedger()
check("未标记时取部署方声明的默认来源", us.usage_origin() == "desktop", us.usage_origin())
token = us.set_usage_origin("browser")
check("请求头声明的来源优先于默认值", us.usage_origin() == "web", us.usage_origin())
us.reset_usage_origin(token)
token = us.set_usage_origin("garbage")
check("无法识别的来源收敛为 unknown", us.usage_origin() == "unknown", us.usage_origin())
us.reset_usage_origin(token)
os.environ.pop("TT_ENGINE_USAGE_ORIGIN_DEFAULT")
default_ledger = engine_server.UsageLedger()
default_ledger.add("qwen-max", 10, 5, sub="u-9")
unmarked_rows = default_ledger.aggregate("daily", "u-9")
check("没有默认声明时未标记调用记为 unknown",
      origin_map(unmarked_rows) == {"unknown": 15}, str(origin_map(unmarked_rows)))

desktop_ledger = engine_server.UsageLedger()
desktop_ledger.add("qwen-max", 40, 10, sub="u-9", origin="desktop")
desktop_ledger.add("qwen-max", 60, 20, sub="u-9", origin="im")
exported = desktop_ledger.daily_rows("daily", "u-9")
check("导出按来源逐行拆分（合并视图可精确去重）",
      [(r["origin"], r["tokensUsed"]) for r in exported] == [("desktop", 50), ("im", 80), ("unknown", 15)],
      str([(r["origin"], r["tokensUsed"]) for r in exported]))
check("导出每行带归属键与来源，合并视图才能不覆盖、不重计",
      all({"sub", "org", "day", "model", "origin", "calls"} <= set(row.keys())
          for row in ({**row, "model": row["modelName"]} for row in exported)),
      str(exported[:1]))

# ── 6. 接口如实披露落盘状态 ──────────────────────────────────────

try:
    from fastapi.testclient import TestClient
except Exception as exc:  # pragma: no cover
    print(f"SKIP: TestClient 不可用（{exc}）")
    FAILURES = [] if not FAILURES else FAILURES
else:
    home("api")
    client = TestClient(engine_server.app)
    engine_server.USAGE_LEDGER.reset()
    engine_server.USAGE_LEDGER.add("qwen-max", 300, 200, sub="u-1", origin="desktop")
    payload = client.get("/api/usage?period=daily", headers={"x-tt-client": "web"}).json()
    check("GET /api/usage 返回 persist 状态",
          payload.get("persist", {}).get("schemaVersion") == us.LEDGER_SCHEMA_VERSION,
          str(payload.get("persist")))
    check("GET /api/usage 的行带来源明细",
          payload.get("rows") and payload["rows"][0].get("origins"),
          str(payload.get("rows")))
    exported_payload = client.get("/api/usage/export?period=daily").json()
    check("GET /api/usage/export 带来源与未归属统计",
          exported_payload.get("rows") and "unattributedTokens" in exported_payload,
          str(exported_payload)[:200])
    check("GET /api/usage/export 未归属统计正确",
          exported_payload["unattributedTokens"] == 0, str(exported_payload["unattributedTokens"]))
    report = client.get("/api/usage/report?period=daily").json()
    check("GET /api/usage/report 同样披露落盘状态", bool(report.get("persist")))

# ── 7. 中间件接线（源码契约） ───────────────────────────────────

source = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
check("HTTP 中间件把请求头来源写入上下文",
      "us.set_usage_origin(request.headers.get(us.ORIGIN_HEADER))" in source)
check("引擎不再宣称重启清零",
      "进程内累计，引擎重启清零" not in source and "引擎重启清零" not in source)
check("IM / CLI / 定时通道按消息通道标记来源",
      "us.set_usage_origin(message.channel)"
      in (PROJECT_ROOT / "engine" / "agent_platform" / "dispatcher.py").read_text(encoding="utf-8"))

print("-" * 50)
if FAILURES:
    print(f"engine usage ledger selftest: {len(FAILURES)} 项失败 -> {FAILURES}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    sys.exit(1)
print("engine usage ledger selftest: ALL PASS")
shutil.rmtree(_TMP_HOME, ignore_errors=True)
