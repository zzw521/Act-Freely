#!/usr/bin/env python3
"""
引擎台账导出 → 服务端入库 → 按用户查询合并视图 的端到端自测（缺陷 D-43 验收）

为什么单独立一个脚本：`tongtian-server/tests/test_server.py` 只能证明「服务端按上报行
算得对」，而 D-43 的验收要求是**同一批调用的两处口径一致且不重复计数**。这里让引擎真实
台账（`UsageLedger.daily_rows`，即 `/api/usage/export` 的返回体）直接喂给真实服务端
（uvicorn + 自己的 SQLite），再按前端 `usage-local.totalsOf` 的算法独立算一遍对照，
三方对齐才算数。

覆盖：
1. 引擎导出的行原样上报即可入库（字段名不需要同名转换）；
2. 桌面行与引擎 desktop 行是同一批调用：合并视图只保留桌面的，引擎那份排除并披露数量；
3. 桌面没记的 im/web 等来源原样补入，不静默丢失；
4. 无来源标记的调用计入总量并单列 unattributedTokens；
5. 引擎重启（重放落盘台账）后重复上报，服务端总量不变（幂等）；
6. 平均耗时三处同口径：一律向下取整（1000/3 = 333）；
7. 服务端只写自己的库，数据目录里没有引擎台账文件。

不发起任何网络请求：模型调用由台账假数据代替，服务端只监听 127.0.0.1 随机端口。

用法: python tests/integration/engine_usage_report_selftest.py
"""

import atexit
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = PROJECT_ROOT.parent / "tongtian-server"
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ENGINE_HOME = tempfile.mkdtemp(prefix="tt-usage-report-engine-")
_SERVER_HOME = tempfile.mkdtemp(prefix="tt-usage-report-server-")
atexit.register(shutil.rmtree, _ENGINE_HOME, ignore_errors=True)
atexit.register(shutil.rmtree, _SERVER_HOME, ignore_errors=True)
os.environ["TONGTIANWORK_HOME"] = _ENGINE_HOME
os.environ["TT_SERVER_DATA_DIR"] = _SERVER_HOME
os.environ["TT_SERVER_JWT_SECRET"] = "usage-report-selftest-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"

FAILURES = []

try:
    import server as engine_server
    import usage_store as us
except Exception as exc:  # 引擎依赖缺失时优雅跳过
    print(f"SKIP: engine usage report selftest unavailable ({exc})")
    sys.exit(0)

try:
    sys.path.insert(0, str(SERVER_ROOT))
    import uvicorn
    from app.main import app as server_app
except Exception as exc:  # 服务端依赖（fastapi 等）缺失时同样跳过
    print(f"SKIP: server side unavailable for usage report selftest ({exc})")
    sys.exit(0)


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


BASE = ""


def http(method: str, path: str, body: dict | None = None, token: str = ""):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def start_server() -> tuple[str, object, threading.Thread]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(server_app, host="127.0.0.1", port=port,
                            log_level="error", access_log=False)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    global BASE
    BASE = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if http("GET", "/health")[0] == 200:
                return BASE, instance, thread
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def model_level_rows(rows: list[dict]) -> list[dict]:
    """导出行 → 模型行：客户端/Rust 的台账本来就是「一模型一行」，先归并再算汇总"""
    out: dict[str, dict] = {}
    for row in rows:
        cur = out.setdefault(row["modelName"], {
            "modelName": row["modelName"],
            "promptTokens": 0,
            "completionTokens": 0,
            "calls": 0,
            "lastUsedAtMs": 0,
            "latencyMsTotal": 0,
            "latencySamples": 0,
            "pricedCalls": 0,
            "unpricedCalls": 0,
            "costs": {},
        })
        cur["promptTokens"] += int(row["promptTokens"])
        cur["completionTokens"] += int(row["completionTokens"])
        cur["calls"] += int(row["calls"])
        cur["lastUsedAtMs"] = max(cur["lastUsedAtMs"], int(row.get("lastUsedAtMs") or 0))
        cur["latencyMsTotal"] += int(row.get("latencyMsTotal") or 0)
        cur["latencySamples"] += int(row.get("latencySamples") or 0)
        cur["pricedCalls"] += int(row.get("pricedCalls") or 0)
        cur["unpricedCalls"] += int(row.get("unpricedCalls") or 0)
        for item in row.get("costs") or []:
            cur["costs"][item["currency"]] = cur["costs"].get(item["currency"], 0) + int(item["micros"])
    model_rows = []
    for cur in out.values():
        samples = cur.pop("latencySamples")
        total = cur.pop("latencyMsTotal")
        cur["latencySamples"] = samples
        cur["avgLatencyMs"] = total // samples if samples else None
        cur["costs"] = [
            {"currency": code, "micros": cur["costs"][code]} for code in sorted(cur["costs"])
        ]
        model_rows.append(cur)
    return model_rows


def client_side_totals(rows: list[dict]) -> dict:
    """`usage-local.totalsOf` 的等价实现（输入为模型行）：独立算一遍用来对照服务端视图

    与客户端一致的三点：平均耗时按样本数加权后向下取整；成本按币种合并；
    没有单价的调用只计数，不按 0 元计。
    """
    prompt = completion = calls = samples = priced = unpriced = 0
    weighted = 0
    costs: dict[str, int] = {}
    last_used = 0
    names: set[str] = set()
    for row in rows:
        prompt += int(row["promptTokens"])
        completion += int(row["completionTokens"])
        calls += int(row["calls"])
        last_used = max(last_used, int(row.get("lastUsedAtMs") or 0))
        row_samples = int(row.get("latencySamples") or 0)
        samples += row_samples
        weighted += int(row.get("avgLatencyMs") or 0) * row_samples
        priced += int(row.get("pricedCalls") or 0)
        unpriced += int(row.get("unpricedCalls") or 0)
        names.add(row["modelName"])
        for item in row.get("costs") or []:
            costs[item["currency"]] = costs.get(item["currency"], 0) + int(item["micros"])
    return {
        "promptTokens": prompt,
        "completionTokens": completion,
        "tokensUsed": prompt + completion,
        "calls": calls,
        "activeModels": len(names),
        "lastUsedAtMs": last_used,
        "avgLatencyMs": weighted // samples if samples else None,
        "latencySamples": samples,
        "costs": [{"currency": code, "micros": costs[code]} for code in sorted(costs)],
        "pricedCalls": priced,
        "unpricedCalls": unpriced,
    }


_server = None
_thread = None
try:
    _, _server, _thread = start_server()

    # ── 1. 先有账号：台账主体必须是 JWT 里的 uid，服务端才认这个人的用量 ──
    stamp = int(time.time() * 1000)
    email = f"usage-report-{stamp}@selftest.local"
    status, registered = http("POST", "/api/v1/auth/register", {
        "email": email, "password": "selftest-pass-1", "nickname": "上报自测用户",
    })
    check("自测账号就绪", status == 201, str(registered))
    token = registered["accessToken"]
    sub = registered["profile"]["uid"]

    # ── 2. 引擎台账按来源记账：桌面通道的调用两边都记 ────────────
    ledger = engine_server.UsageLedger()

    def sample(origin: str, model: str, pt: int, ct: int, **kw) -> None:
        mark = us.set_usage_origin(origin)
        try:
            ledger.add(model, pt, ct, sub=sub, **kw)
        finally:
            us.reset_usage_origin(mark)

    sample(us.ORIGIN_DESKTOP, "qwen-plus", 1000, 500,
           latency_ms=3000, cost_micros=1080, currency="CNY", priced=True)
    sample(us.ORIGIN_DESKTOP, "qwen-plus", 1000, 500)  # 桌面也记了这笔：引擎侧是重复来源
    sample(us.ORIGIN_IM, "qwen-plus", 40, 10, latency_ms=300)
    sample(us.ORIGIN_IM, "qwen-max", 30, 20)
    sample(us.ORIGIN_UNKNOWN, "qwen-plus", 7, 7)

    exported = ledger.daily_rows("total", sub)
    check("导出包含全部来源分桶",
          {row["origin"] for row in exported} == {"desktop", "im", "unknown"},
          str(sorted({row["origin"] for row in exported})))
    check("导出行携带耗时与成本（否则合并视图会比客户端少一截）",
          all({"latencyMsTotal", "latencySamples", "costs"} <= set(row) for row in exported))
    check("导出只给整数累计，不在引擎里取平均",
          all("avgLatencyMs" not in row for row in exported))

    # ── 3. 桌面台账（同一批 desktop 调用）上报为 source=desktop ──
    desktop_rows = [dict(row) for row in exported if row["origin"] == us.ORIGIN_DESKTOP]
    status, body = http("POST", "/api/v1/usage/report", {
        "source": "desktop", "deviceId": "desktop-1",
        "schemaVersion": us.LEDGER_SCHEMA_VERSION, "rows": desktop_rows,
    }, token)
    check("桌面台账上报入库",
          status == 200 and body.get("accepted") == len(desktop_rows), str(body))

    # ── 4. 引擎导出原样上报 ─────────────────────────────────────
    status, body = http("POST", "/api/v1/usage/report", {
        "source": "engine", "deviceId": "engine-1",
        "schemaVersion": us.LEDGER_SCHEMA_VERSION, "rows": exported,
    }, token)
    check("引擎导出行可直接作为上报报文（字段名无需转换）",
          status == 200 and body.get("accepted") == len(exported), str(body))

    status, view = http("GET", "/api/v1/usage/me?period=total", token=token)
    check("按用户查询合并视图（JWT 到人）", status == 200 and view.get("sub") == sub, str(view)[:160])

    # ── 5. 两处口径一致：桌面为主 + 引擎补入，不重复计数 ─────────
    merged_expect = [row for row in exported if row["origin"] != us.ORIGIN_DESKTOP] + desktop_rows
    expected = client_side_totals(model_level_rows(merged_expect))
    check("服务端总量 = 客户端算法独立算出的总量",
          view["totals"] == expected, f"{view['totals']} != {expected}")
    deduped = sum(row["promptTokens"] + row["completionTokens"]
                  for row in exported if row["origin"] == us.ORIGIN_DESKTOP)
    check("引擎 desktop 行被排除且数量如实披露",
          view["sources"]["engineDedupedTokens"] == deduped, str(view["sources"]))
    check("桌面没记的通道原样补入",
          view["totals"]["tokensUsed"]
          == sum(row["promptTokens"] + row["completionTokens"] for row in merged_expect))
    unattributed = sum(row["promptTokens"] + row["completionTokens"]
                       for row in exported if row["origin"] == us.ORIGIN_UNKNOWN)
    check("无来源标记照常计入并单列披露",
          view["sources"]["unattributedTokens"] == unattributed, str(view["sources"]))
    check("合并视图三块自洽（趋势合计 = 明细合计 = 汇总）",
          sum(item["tokensUsed"] for item in view["days"])
          == sum(item["tokensUsed"] for item in view["models"])
          == view["totals"]["tokensUsed"], str(view["totals"]["tokensUsed"]))
    check("未计价调用不按 0 元计",
          view["totals"]["pricedCalls"] == 1
          and view["totals"]["unpricedCalls"] > 0
          and view["totals"]["costs"] == [{"currency": "CNY", "micros": 1080}],
          str(view["totals"]))

    # ── 6. 引擎重启后重放落盘台账，重复上报幂等 ──────────────────
    restarted = engine_server.UsageLedger()
    after_export = restarted.daily_rows("total", sub)
    check("重启后导出台账与重启前一致（用量不落空）", after_export == exported,
          f"{len(after_export)} vs {len(exported)}")
    status, body = http("POST", "/api/v1/usage/report", {
        "source": "engine", "deviceId": "engine-1", "rows": after_export,
    }, token)
    check("重启后重复上报被接受", status == 200 and body.get("accepted") == len(after_export))
    _, again = http("GET", "/api/v1/usage/me?period=total", token=token)
    check("重复上报不重复计数（汇总不变）", again["totals"] == view["totals"], str(again["totals"]))
    check("重复上报不改变来源披露（没有新增行）",
          again["sources"] == view["sources"], str(again["sources"]))

    # ── 7. 延迟口径：向下取整 ───────────────────────────────────
    ledger._apply_delta(sub, "qwen-odd", engine_server._utc_day(), {
        "pt": 10, "ct": 5, "calls": 3, "lat": 1000, "origin": us.ORIGIN_WEB, "priced": 0,
    })
    odd_rows = [row for row in ledger.daily_rows("total", sub) if row["modelName"] == "qwen-odd"]
    status, body = http("POST", "/api/v1/usage/report", {
        "source": "engine", "deviceId": "engine-1", "rows": odd_rows,
    }, token)
    check("整数耗时累计上报", status == 200 and body.get("accepted") == len(odd_rows), str(body))
    _, odd_view = http("GET", "/api/v1/usage/me?period=total", token=token)
    odd_model = next((row for row in odd_view["models"] if row["modelName"] == "qwen-odd"), {})
    check("平均耗时向下取整（1000/3 = 333，与 Rust 整除、前端 Math.floor 同口径）",
          odd_model.get("avgLatencyMs") == 333 and odd_model.get("latencySamples") == 3,
          str(odd_model))
    check("服务端视图仍与客户端算法一致（加入新行后重算）",
          odd_view["totals"] == client_side_totals(
              model_level_rows(
                  merged_expect + [row for row in odd_rows if row["origin"] != us.ORIGIN_DESKTOP]
              )
          ), str(odd_view["totals"]))

    # ── 8. 归属：服务端只写自己的库 ─────────────────────────────
    server_db = Path(_SERVER_HOME) / "market.db"
    check("服务端只建自己的库", server_db.exists())
    check("服务端数据目录里没有引擎台账（不读客户端库文件）",
          not (Path(_SERVER_HOME) / "usage").exists() and not (Path(_SERVER_HOME) / "tongtian.db").exists())
    conn = sqlite3.connect(str(server_db))
    conn.row_factory = sqlite3.Row
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = {row["source"] for row in conn.execute("SELECT DISTINCT source FROM usage_rollup")}
    devices = {row["device_id"] for row in conn.execute("SELECT DISTINCT device_id FROM usage_rollup")}
    row_count = conn.execute("SELECT COUNT(*) AS n FROM usage_rollup").fetchone()["n"]
    conn.close()
    check("用量落在 usage_rollup", "usage_rollup" in tables, str(sorted(tables)))
    check("桌面与引擎两类来源分开存，不混写", sources == {"desktop", "engine"}, str(sources))
    check("每台上报设备各一份，互不覆盖", devices == {"desktop-1", "engine-1"}, str(devices))
    check("重报幂等：库里行数等于去重后的键数", row_count == len(exported) + len(desktop_rows) + len(odd_rows),
          f"{row_count} vs {len(exported) + len(desktop_rows) + len(odd_rows)}")
finally:
    if _server is not None:
        _server.should_exit = True
        _thread.join(timeout=5)

print()
if FAILURES:
    print(f"FAIL: {len(FAILURES)} 项未通过")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("ALL PASS: 引擎导出 → 服务端入库 → 合并视图 与客户端口径一致")
