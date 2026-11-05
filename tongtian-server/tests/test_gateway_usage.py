#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关用量接收、合并视图与部门报表（M1.4 T4.5–T4.8）。

覆盖：
- 服务令牌鉴权（缺失/错误 401，未配置 503）；
- 批量上报按 (gatewayId, requestId) 幂等，重放不重复计数；
- 非法行整批拒绝，不写入半批；
- 合并视图排除客户端 origin=gateway 的重复行，计入网关账本行；
- 部门报表与用户明细同口径，逐部门相加等于总量；
- 判重键按 30 天保留，保留期内清理后重放仍判重。

用法: python tests/test_gateway_usage.py
"""

import atexit
import dataclasses
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-gw-usage-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "gateway-usage-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"
os.environ["TT_GATEWAY_SERVICE_TOKEN"] = "test-service-token"
os.environ["TT_GATEWAY_USAGE_RETENTION_DAYS"] = "30"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import gateway_policy  # noqa: E402
from app import gateway_usage as gu  # noqa: E402
from app.main import app  # noqa: E402

_passed = 0
SERVICE = "test-service-token"
GATEWAY = "gw-test-01"
CONTRACT = "gateway-contract.v1"


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None,
         headers: dict | None = None):
    merged = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    if token:
        merged["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=merged, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            status, _, _ = http("GET", "/health")
            if status == 200:
                return server, thread
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def register_user(email: str, nickname: str) -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def login(email: str, password: str = "secret123") -> str:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    data = payload(body)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {data}")
    return data["accessToken"]


def event(request_id: str, sub: str, *, prompt: int, completion: int, latency: int,
          cost: str = "0", currency: str = "USD", price_version: str | None = None,
          dept: str = "dept-sales", tenant: str = "acme", model: str = "qwen-plus",
          origin: str = "gateway", status: str = "ok", finished: int | None = None) -> dict:
    finished_at = finished if finished is not None else int(time.time() * 1000)
    return {
        "contractVersion": CONTRACT,
        "gatewayId": GATEWAY,
        "requestId": request_id,
        "tenantId": tenant,
        "departmentId": dept,
        "userId": sub,
        "apiKeyId": None,
        "projectId": None,
        "origin": origin,
        "protocol": "openai",
        "endpoint": "/v1/chat/completions",
        "modelAlias": "auto",
        "model": model,
        "provider": "openai_compatible",
        "promptTokens": prompt,
        "completionTokens": completion,
        "cachedTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": prompt + completion,
        "cost": cost,
        "currency": currency,
        "priceVersion": price_version,
        "latencyMs": latency,
        "status": status,
        "errorCode": None,
        "stream": False,
        "estimated": False,
        "startedAt": finished_at - latency,
        "finishedAt": finished_at,
    }


def batch(batch_id: str, events: list[dict], gateway: str = GATEWAY) -> dict:
    return {
        "contractVersion": CONTRACT,
        "gatewayId": gateway,
        "batchId": batch_id,
        "events": events,
    }


def send(body: dict, token: str | None = SERVICE):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http("POST", "/api/v1/gateway/usage", json_body=body, headers=headers)


def me(token: str, period: str = "total") -> dict:
    status, _, body = http("GET", f"/api/v1/usage/me?period={period}", token=token)
    data = payload(body)
    if status != 200:
        raise AssertionError(f"查询用量失败 {status}: {data}")
    return data


def test_auth():
    print("\n[服务令牌鉴权]")
    body = batch("b-auth", [event("req-auth", "u-x", prompt=1, completion=1, latency=1)])
    status, _, raw = http("POST", "/api/v1/gateway/usage", json_body=body)
    check("缺少令牌 401", status == 401 and payload(raw).get("code") == "AUTH_REQUIRED")
    status, _, raw = send(body, token="wrong-token")
    check("错误令牌 401", status == 401 and payload(raw).get("code") == "AUTH_REQUIRED")

    original = gateway_policy.settings
    gateway_policy.settings = dataclasses.replace(original, gateway_service_token="")
    try:
        status, _, raw = send(body)
        check(
            "控制面未配置服务令牌 503",
            status == 503 and payload(raw).get("code") == "AUTH_UNAVAILABLE",
        )
    finally:
        gateway_policy.settings = original

    status, _, raw = send(batch("b-empty", []))
    check(
        "空批合法",
        status == 200 and payload(raw).get("accepted") == 0
        and payload(raw).get("duplicates") == 0,
        str(payload(raw)),
    )
    wrong = batch("b-contract", [])
    wrong["contractVersion"] = "gateway-contract.v2"
    status, _, raw = send(wrong)
    check(
        "未知契约版本整批拒绝",
        status == 400 and payload(raw).get("code") == "GATEWAY_USAGE_CONTRACT",
    )


def test_idempotent_upload(uid: str, token: str):
    print("\n[批量上报与幂等]")
    events = [
        event("req-1", uid, prompt=100, completion=50, latency=100,
              cost="0.001000", price_version="test-2026-10"),
        event("req-2", uid, prompt=200, completion=100, latency=200,
              cost="0.002500", price_version="test-2026-10"),
        event("req-3", uid, prompt=10, completion=5, latency=300),
    ]
    status, _, raw = send(batch("b-1", events))
    data = payload(raw)
    check(
        "首批入库 accepted=3",
        status == 200 and data.get("accepted") == 3 and data.get("duplicates") == 0,
        str(data),
    )
    view = me(token)
    check(
        "合并视图计入网关行",
        view["totals"]["tokensUsed"] == 465 and view["totals"]["calls"] == 3
        and view["sources"]["gatewayRows"] == 1,
        json.dumps(view["sources"], ensure_ascii=False),
    )
    check(
        "网关行成本与耗时按币种累计",
        view["totals"]["costs"] == [{"currency": "USD", "micros": 3500}]
        and view["totals"]["avgLatencyMs"] == 200
        and view["totals"]["pricedCalls"] == 2
        and view["totals"]["unpricedCalls"] == 1,
        json.dumps(view["totals"], ensure_ascii=False),
    )

    for attempt in (1, 2):
        status, _, raw = send(batch("b-1", events))
        data = payload(raw)
        check(
            f"第 {attempt} 次重放 duplicates=3",
            status == 200 and data.get("accepted") == 0 and data.get("duplicates") == 3,
            str(data),
        )
    again = me(token)
    check(
        "重放三次总量不变",
        again["totals"] == view["totals"] and again["sources"] == view["sources"],
        json.dumps(again["totals"], ensure_ascii=False),
    )

    status, _, raw = send(batch("b-overlap", events[:2] + [
        event("req-2", uid, prompt=200, completion=100, latency=200),
    ]))
    data = payload(raw)
    check(
        "批内重复 requestId 只计一次",
        status == 200 and data.get("accepted") == 0 and data.get("duplicates") == 3,
        str(data),
    )


def test_reject_whole_batch(uid: str, token: str):
    print("\n[非法行整批拒绝]")
    before = me(token)["totals"]
    good = event("req-new-good", uid, prompt=7, completion=3, latency=10)
    bad = event("req-new-bad", uid, prompt=7, completion=3, latency=10)
    bad["totalTokens"] = 999
    status, _, raw = send(batch("b-invalid-token", [good, bad]))
    check(
        "tokensUsed 口径不一致整批 400",
        status == 400 and payload(raw).get("code") == "GATEWAY_USAGE_INVALID",
        str(payload(raw)),
    )
    check("非法批不写入半批", me(token)["totals"] == before)

    wrong_origin = event("req-origin", uid, prompt=1, completion=1, latency=1, origin="desktop")
    status, _, raw = send(batch("b-origin", [wrong_origin]))
    check(
        "网关事件 origin 必须是 gateway",
        status == 400 and payload(raw).get("code") == "GATEWAY_USAGE_INVALID",
        str(payload(raw)),
    )

    future = event("req-time", uid, prompt=1, completion=1, latency=1, finished=1)
    future["startedAt"] = 5
    status, _, raw = send(batch("b-time", [future]))
    check(
        "finishedAt 早于 startedAt 被拒",
        status == 400 and payload(raw).get("code") == "GATEWAY_USAGE_INVALID",
    )


def test_merge_dedupe(uid: str, token: str):
    print("\n[合并视图去重]")
    base = me(token)
    status, _, raw = http(
        "POST",
        "/api/v1/usage/report",
        token=token,
        json_body={
            "source": "engine",
            "deviceId": "pc-1",
            "rows": [
                # 与网关 req-1 同一次调用：引擎侧也记了一份
                {"sub": uid, "modelName": "qwen-plus", "day": time.strftime("%Y-%m-%d"),
                 "origin": "gateway", "promptTokens": 100, "completionTokens": 50, "calls": 1,
                 "latencyMsTotal": 100, "latencySamples": 1, "costs": {"USD": 1000}},
                {"sub": uid, "modelName": "qwen-plus", "day": time.strftime("%Y-%m-%d"),
                 "origin": "im", "promptTokens": 40, "completionTokens": 10, "calls": 1,
                 "latencyMsTotal": 50, "latencySamples": 1},
                {"sub": uid, "modelName": "qwen-plus", "day": time.strftime("%Y-%m-%d"),
                 "origin": "desktop", "promptTokens": 20, "completionTokens": 5, "calls": 1},
            ],
        },
    )
    check("引擎上报成功", status == 200, str(payload(raw)))
    view = me(token)
    check(
        "客户端 origin=gateway 行被排除且披露",
        view["totals"]["tokensUsed"] == base["totals"]["tokensUsed"] + 50
        and view["sources"]["gatewayDedupedRows"] == 1
        and view["sources"]["gatewayDedupedTokens"] == 150,
        json.dumps(view["sources"], ensure_ascii=False),
    )
    check(
        "引擎 desktop 行仍按原规则去重",
        view["sources"]["engineDedupedRows"] == 1 and view["sources"]["engineRows"] == 1,
        json.dumps(view["sources"], ensure_ascii=False),
    )
    check(
        "网关行数量不受客户端上报影响",
        view["sources"]["gatewayRows"] == 1,
        json.dumps(view["sources"], ensure_ascii=False),
    )


def test_retention():
    print("\n[判重键保留与清理]")
    db_path = os.path.join(_TMP, "market.db")
    old_ms = int(time.time() * 1000) - 31 * 86400 * 1000
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO gateway_usage_events (gateway_id, request_id, batch_id, tenant_id, "
        "dept_id, sub, model, day, status, prompt_tokens, completion_tokens, cost_micros, "
        "currency, latency_ms, received_at_ms) "
        "VALUES (?1, 'req-ancient', 'b-old', 'acme', 'dept-sales', 'u-old', 'qwen-plus', "
        "'2026-01-01', 'ok', 1, 1, 0, 'USD', 1, ?2)",
        (GATEWAY, old_ms),
    )
    conn.commit()
    conn.close()

    gu._last_cleanup_ms = 0
    status, _, raw = send(batch("b-cleanup", []))
    check("空批也可触发清理", status == 200 and payload(raw).get("accepted") == 0)

    conn = sqlite3.connect(db_path)
    ancient = conn.execute(
        "SELECT COUNT(*) FROM gateway_usage_events WHERE request_id = 'req-ancient'"
    ).fetchone()[0]
    fresh = conn.execute(
        "SELECT COUNT(*) FROM gateway_usage_events WHERE request_id LIKE 'req-%' "
        "AND request_id != 'req-ancient'"
    ).fetchone()[0]
    conn.close()
    check("超过保留期的判重键被清理", ancient == 0)
    check("保留期内的判重键保留", fresh > 0)

    gu._last_cleanup_ms = 0
    status, _, raw = send(batch("b-1-after-cleanup", [
        event("req-1", "u-any", prompt=100, completion=50, latency=100),
    ]))
    check(
        "清理后重放旧请求仍判重",
        status == 200 and payload(raw).get("duplicates") == 1
        and payload(raw).get("accepted") == 0,
        str(payload(raw)),
    )


def run():
    server, _thread = start_server()
    try:
        stamp = int(time.time() * 1000)
        email = f"gw-user-{stamp}@test.local"
        user = register_user(email, "网关用户")
        uid = user["profile"]["uid"]
        token = user["accessToken"]

        test_auth()
        test_idempotent_upload(uid, token)
        test_reject_whole_batch(uid, token)
        test_merge_dedupe(uid, token)

        admin = login("admin@test.local", "admin-secret-123")
        status, _, raw = http(
            "POST", "/api/v1/admin/departments", token=admin,
            json_body={"org": "acme", "id": "dept-sales", "name": "销售部"},
        )
        check("创建部门", status == 201, str(payload(raw)))
        status, _, raw = http(
            "PATCH", f"/api/v1/admin/users/{uid}", token=admin,
            json_body={"org": "acme", "depts": ["dept-sales"], "primaryDept": "dept-sales"},
        )
        check("分配部门", status == 200, str(payload(raw)))
        # 分配部门会吊销旧会话，必须重新登录拿到新 claims
        token = login(email)
        # 部门报表用独立用户与独立部门：该用户全部用量都归属 dept-eng，
        # 才能逐字段对照「部门汇总 == 用户明细」
        status, _, raw = http(
            "POST", "/api/v1/admin/departments", token=admin,
            json_body={"org": "acme", "id": "dept-eng", "name": "研发部"},
        )
        check("创建第二个部门", status == 201, str(payload(raw)))
        eng_email = f"gw-eng-{stamp}@test.local"
        eng = register_user(eng_email, "研发用户")
        eng_uid = eng["profile"]["uid"]
        status, _, raw = http(
            "PATCH", f"/api/v1/admin/users/{eng_uid}", token=admin,
            json_body={"org": "acme", "depts": ["dept-eng"], "primaryDept": "dept-eng"},
        )
        check("分配研发部", status == 200, str(payload(raw)))
        eng_token = login(eng_email)

        status, _, raw = send(batch("b-eng", [
            event("req-eng-1", eng_uid, prompt=50, completion=25, latency=80,
                  cost="0.000500", price_version="test-2026-10", dept="dept-eng"),
        ]))
        check("研发部网关事件入库", status == 200, str(payload(raw)))
        status, _, raw = http(
            "POST", "/api/v1/usage/report", token=eng_token,
            json_body={
                "source": "engine", "deviceId": "pc-eng",
                "rows": [{"sub": eng_uid, "modelName": "qwen-plus",
                          "day": time.strftime("%Y-%m-%d"), "origin": "im",
                          "promptTokens": 30, "completionTokens": 10, "calls": 1,
                          "latencyMsTotal": 60, "latencySamples": 1}],
            },
        )
        check("研发用户上报客户端用量", status == 200, str(payload(raw)))

        status, _, raw = http("GET", "/api/v1/admin/usage/departments?period=total")
        check("未登录不能查部门报表", status == 401)
        status, _, raw = http(
            "GET", "/api/v1/admin/usage/departments?period=total", token=eng_token
        )
        check("非管理员 403", status == 403)

        status, _, raw = http(
            "GET", "/api/v1/admin/usage/departments?period=total", token=admin
        )
        report = payload(raw)
        check("管理员查询部门报表", status == 200 and report.get("ok") is True)
        eng_row = next(
            (row for row in report.get("departments", [])
             if row["org"] == "acme" and row["dept"] == "dept-eng"),
            None,
        )
        detail = me(eng_token)
        check(
            "部门汇总与用户明细同口径",
            eng_row is not None
            and eng_row["tokensUsed"] == detail["totals"]["tokensUsed"]
            and eng_row["calls"] == detail["totals"]["calls"]
            and eng_row["users"] == 1
            and eng_row["models"] == 1,
            json.dumps(eng_row or {}, ensure_ascii=False),
        )
        user_total = (
            me(token)["totals"]["tokensUsed"] + me(eng_token)["totals"]["tokensUsed"]
        )
        check(
            "部门相加等于用户明细之和",
            report["totals"]["tokensUsed"]
            == sum(row["tokensUsed"] for row in report["departments"])
            == user_total,
            json.dumps(report["totals"], ensure_ascii=False),
        )

        test_retention()
    finally:
        server.should_exit = True

    print()
    print(f"gateway usage selftest: ALL PASS（{_passed} 项）")


if __name__ == "__main__":
    run()
