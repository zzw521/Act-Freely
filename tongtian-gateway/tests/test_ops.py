"""M1.4 网关侧自测：用量上报器（T4.1–T4.2）、管理用量聚合（T4.3）、
配额调整（T4.4）与 G4 的 429 重置时间头。

标准库 + uvicorn；复用 tests/support.py 的临时数据目录、假策略与请求工具。
直接往 `usage_events` 写事件，是为了让聚合结果可以和手算逐项对账；
真实链路的落库口径由 test_protocols.py / test_core.py 覆盖。

用法: python tests/test_ops.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import support  # noqa: E402,F401 - 导入即固化测试环境变量
from tests.support import GATEWAY_ID, admin_headers, check, http, report  # noqa: E402

from app.auth import Principal  # noqa: E402
from app.config import settings  # noqa: E402
from app.contract import UsageEvent  # noqa: E402
from app.errors import GatewayError  # noqa: E402
from app.state import db, service, usage_store  # noqa: E402
from app.usage_uploader import UsageUploader  # noqa: E402

STAMP = str(int(time.time() * 1000))


# ── 工具 ───────────────────────────────────────────────────

def make_event(
    request_id: str,
    *,
    tenant: str = "acme",
    dept: str = "dept-sales",
    sub: str = "u-1",
    model: str = "fake-chat",
    provider: str = "openai_compatible",
    prompt: int = 10,
    completion: int = 5,
    cost: str = "0",
    currency: str = "USD",
    latency: int = 100,
    finished: int | None = None,
    status: str = "ok",
) -> UsageEvent:
    end = finished if finished is not None else int(time.time() * 1000)
    return UsageEvent(
        gatewayId=GATEWAY_ID,
        requestId=request_id,
        tenantId=tenant,
        departmentId=dept,
        userId=sub,
        protocol="openai",
        endpoint="/v1/chat/completions",
        modelAlias=model,
        model=model,
        provider=provider,
        promptTokens=prompt,
        completionTokens=completion,
        cost=cost,
        currency=currency,
        latencyMs=latency,
        status=status,
        startedAt=end - latency,
        finishedAt=end,
    )


def micros(amount: str) -> int:
    return int((Decimal(amount) * 1_000_000).to_integral_value())


def admin_usage(**params) -> tuple[int, dict, dict]:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    path = "/gateway/v1/admin/usage" + (f"?{query}" if query else "")
    return http("GET", path, headers=admin_headers())


def patch_quota(principal: str, body: dict, *, actor: str | None = None):
    headers = admin_headers()
    if actor:
        headers["X-TT-Admin-Actor"] = actor
    return http("PATCH", f"/gateway/v1/admin/quotas/{principal}", headers=headers, body=body)


def sqlite_rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def quota_error(subject: str = "u-1", prompt_estimate: int = 50, max_tokens: int = 1024) -> str | None:
    try:
        execution = service.begin_chat(
            Principal(
                subject=subject,
                session_id="s-1",
                tenant="acme",
                department="dept-sales",
                departments=("dept-sales",),
                roles=("member",),
                auth_kind="jwt",
            ),
            "fake-chat",
            max_tokens=max_tokens,
            prompt_estimate=prompt_estimate,
            request_id=f"quota-probe-{subject}-{STAMP}",
            endpoint="/v1/chat/completions",
            stream=False,
        )
    except GatewayError as exc:
        return exc.code
    service._quota.release(execution.reservation)
    return None


# ── T4.1–T4.2 上报器 ──────────────────────────────────────


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200
        self.body = '{"ok":true,"accepted":0,"duplicates":0}'
        self.error: Exception | None = None

    def __call__(self, payload: dict, endpoint: str, token: str) -> tuple[int, str]:
        self.calls.append({"payload": payload, "endpoint": endpoint, "token": token})
        if self.error is not None:
            raise self.error
        return self.status, self.body

    @property
    def last(self) -> dict:
        return self.calls[-1]


def test_uploader() -> UsageUploader:
    print("\n[上报器：批量、鉴权与队列]")
    recorder = Recorder()
    upload_settings = replace(
        settings,
        control_plane_url="http://control.test",
        service_token="svc-token-e2e",
        usage_batch_size=200,
        usage_flush_seconds=5,
    )
    uploader = UsageUploader(upload_settings, usage_store, transport=recorder)
    check("配置齐全时上报器启用", uploader.enabled is True)
    check("初始队列为空", uploader.status()["pending"] == 0)

    base_ms = int(time.time() * 1000)
    first_ids = [f"{STAMP}-ok-{index}" for index in range(3)]
    for index, request_id in enumerate(first_ids):
        usage_store.record(make_event(request_id, finished=base_ms + index))
    check("事件先落本地账本", usage_store.count_pending() == 3)

    check("首批上报 3 条", uploader.flush_once() == 3)
    payload = recorder.last["payload"]
    events = payload["events"]
    check("上报端点指向控制面", recorder.last["endpoint"] == "http://control.test/api/v1/gateway/usage")
    check("上报用服务令牌", recorder.last["token"] == "svc-token-e2e")
    check("负载带契约版本", payload["contractVersion"] == "gateway-contract.v1")
    check("负载带网关标识", payload["gatewayId"] == GATEWAY_ID)
    check("批次标识稳定可追踪", isinstance(payload["batchId"], str) and payload["batchId"].startswith("batch_"))
    check("事件完整上报", len(events) == 3 and {item["requestId"] for item in events} == set(first_ids))
    check("事件标 origin=gateway", all(item["origin"] == "gateway" for item in events))
    check(
        "不携带提示词与响应正文",
        {"prompt", "response", "messages", "content"}.isdisjoint(events[0].keys()),
    )
    status = uploader.status()
    check("成功后队列清空", status["pending"] == 0)
    check("成功时间已记录", isinstance(status["lastSuccessAtMs"], int))
    check("成功后无退避", status["consecutiveFailures"] == 0 and status["backoffSeconds"] == 0.0)
    check("空队列不会重复上报", uploader.flush_once() == 0 and len(recorder.calls) == 1)

    print("\n[上报器：失败保留、退避与批标识复用]")
    recorder.status = 503
    recorder.body = '{"ok":false,"code":"SERVICE_UNAVAILABLE"}'
    retry_ids = [f"{STAMP}-retry-{index}" for index in range(2)]
    for index, request_id in enumerate(retry_ids):
        usage_store.record(make_event(request_id, finished=base_ms + 10 + index))
    check("控制面不可达时不上报成功", uploader.flush_once() == 0)
    first_batch = recorder.last["payload"]["batchId"]
    state = uploader.status()
    check("事件留在本地队列", state["pending"] == 2)
    check("错误已记录", "503" in state["lastError"])
    check("指数退避第 1 次为 1s", state["consecutiveFailures"] == 1 and state["backoffSeconds"] == 1.0)
    check("重试仍失败不丢数据", uploader.flush_once() == 0)
    check("同一批重试复用 batchId", recorder.last["payload"]["batchId"] == first_batch)
    state = uploader.status()
    check("指数退避第 2 次为 2s", state["consecutiveFailures"] == 2 and state["backoffSeconds"] == 2.0)

    recorder.status = 200
    recorder.body = '{"ok":true,"accepted":2,"duplicates":0}'
    check("控制面恢复后补发", uploader.flush_once() == 2)
    state = uploader.status()
    check("补发后队列清空", state["pending"] == 0 and state["consecutiveFailures"] == 0)
    check("成功后清除错误", state["lastError"] == "")

    print("\n[上报器：网络异常按可重试处理]")
    recorder.error = OSError("connection refused")
    net_id = f"{STAMP}-net-1"
    usage_store.record(make_event(net_id, finished=base_ms + 20))
    check("网络异常不上报成功", uploader.flush_once() == 0)
    state = uploader.status()
    check("网络异常不丢事件", state["pending"] == 1 and state["lastError"].startswith("网络错误"))
    recorder.error = None
    check("网络恢复后补发", uploader.flush_once() == 1 and uploader.status()["pending"] == 0)

    print("\n[上报器：确定性拒绝不堵队列]")
    recorder.status = 400
    recorder.body = '{"ok":false,"code":"GATEWAY_USAGE_INVALID"}'
    poison_id = f"{STAMP}-poison-1"
    usage_store.record(make_event(poison_id, finished=base_ms + 30))
    check("坏批次被跳过", uploader.flush_once() == 1)
    state = uploader.status()
    check("跳过后队列可继续前进", state["pending"] == 0)
    check("拒绝原因留在健康信息", "HTTP 400" in state["lastError"])
    check("账本仍可按 requestId 追溯", usage_store.get(poison_id) is not None)

    print("\n[上报器：未配置时只记本地账]")
    disabled = UsageUploader(
        replace(settings, control_plane_url="", service_token=""),
        usage_store,
        transport=recorder,
    )
    check("未配置上报器不启用", disabled.enabled is False)
    disabled.start()
    check("未启用不启动线程", disabled._thread is None)
    check("未启用状态可见", disabled.status()["enabled"] is False)

    recorder.status = 200
    return uploader


# ── T4.3 管理用量聚合 ─────────────────────────────────────


def seed_usage() -> list[dict]:
    base_ms = int(time.time() * 1000)
    samples = [
        {"dept": "dept-sales", "sub": "u-1", "model": "fake-chat", "prompt": 100,
         "completion": 50, "cost": "0.001500", "latency": 100, "finished": base_ms + 100},
        {"dept": "dept-sales", "sub": "u-1", "model": "fake-chat", "prompt": 200,
         "completion": 100, "cost": "0.003000", "latency": 300, "finished": base_ms + 101},
        {"dept": "dept-eng", "sub": "u-2", "model": "fake-embed", "prompt": 1000,
         "completion": 0, "cost": "0.000100", "latency": 50, "finished": base_ms + 102},
    ]
    for index, sample in enumerate(samples):
        usage_store.record(
            make_event(
                f"{STAMP}-ops-{index}",
                tenant="ops-acme",
                dept=sample["dept"],
                sub=sample["sub"],
                model=sample["model"],
                prompt=sample["prompt"],
                completion=sample["completion"],
                cost=sample["cost"],
                latency=sample["latency"],
                finished=sample["finished"],
            )
        )
    return samples


def test_admin_usage() -> None:
    print("\n[管理用量：鉴权与参数]")
    status, _, body = http("GET", "/gateway/v1/admin/usage")
    check("用量查询需要管理令牌", status == 401 and body.get("code") == "AUTH_REQUIRED")
    status, _, body = admin_usage(period="weekly")
    check("非法周期被拒", status == 400 and body.get("code") == "INVALID_REQUEST")

    samples = seed_usage()
    print("\n[管理用量：与手算对账]")
    status, _, body = admin_usage(tenant="ops-acme", period="total")
    check("用量查询成功", status == 200 and body.get("ok") is True, str(body)[:200])
    check("生成时间戳存在", isinstance(body.get("generatedAtMs"), int))

    expected_calls = len(samples)
    expected_prompt = sum(item["prompt"] for item in samples)
    expected_completion = sum(item["completion"] for item in samples)
    expected_total = expected_prompt + expected_completion
    expected_micros = sum(micros(item["cost"]) for item in samples)
    expected_latency = sum(item["latency"] for item in samples)
    totals = body["totals"]
    check("调用次数与手算一致", totals["calls"] == expected_calls, str(totals))
    check("prompt token 与手算一致", totals["promptTokens"] == expected_prompt, str(totals))
    check("completion token 与手算一致", totals["completionTokens"] == expected_completion, str(totals))
    check("total token 与手算一致", totals["totalTokens"] == expected_total, str(totals))
    check("成本与手算一致", totals["costs"] == [{"currency": "USD", "micros": expected_micros}], str(totals))
    check(
        "平均延迟按总延迟整除",
        totals["latencyMsTotal"] == expected_latency
        and totals["avgLatencyMs"] == expected_latency // expected_calls,
        str(totals),
    )
    check("按 用户/模型/日/币种 分组", body.get("rowCount") == 2, str(body["rows"]))

    rows = {(row["userId"], row["model"]): row for row in body["rows"]}
    sales = rows.get(("u-1", "fake-chat"))
    eng = rows.get(("u-2", "fake-embed"))
    check("分组行内容正确", sales is not None and eng is not None, str(list(rows)))
    if sales and eng:
        check(
            "销售部门行 token 正确",
            sales["totalTokens"] == 450
            and sales["calls"] == 2
            and sales["costMicros"] == 4500
            and sales["avgLatencyMs"] == 200
            and sales["departmentId"] == "dept-sales"
            and sales["tenantId"] == "ops-acme"
            and sales["currency"] == "USD",
            str(sales),
        )
        today = time.strftime("%Y-%m-%d", time.gmtime())
        check("分组日期为 UTC 自然日", sales["day"] == today, sales["day"])

    db_row = sqlite_rows(
        "SELECT COUNT(*) AS calls, SUM(prompt_tokens) AS prompt, "
        "SUM(completion_tokens) AS completion, SUM(total_tokens) AS total, "
        "SUM(cost_micros) AS micros FROM usage_events WHERE tenant='ops-acme'"
    )[0]
    check(
        "与 usage_events 原始表一致",
        (db_row["calls"], db_row["prompt"], db_row["completion"], db_row["total"], db_row["micros"])
        == (expected_calls, expected_prompt, expected_completion, expected_total, expected_micros),
        str(dict(db_row)),
    )

    status, _, body = admin_usage(tenant="ops-acme", dept="dept-sales", period="monthly")
    check("部门过滤", status == 200 and body["totals"]["calls"] == 2 and body["totals"]["totalTokens"] == 450)
    status, _, body = admin_usage(tenant="ops-acme", sub="u-2", period="monthly")
    check("用户过滤", status == 200 and body["totals"]["calls"] == 1 and body["totals"]["totalTokens"] == 1000)
    status, _, body = admin_usage(tenant="ops-acme", model="fake-embed")
    check("模型过滤", status == 200 and body["totals"]["calls"] == 1)
    status, _, body = admin_usage(tenant="ops-none")
    check("无数据时为空", status == 200 and body["rowCount"] == 0 and body["totals"]["calls"] == 0)

    status, _, body = http("GET", "/gateway/v1/admin/health", headers=admin_headers())
    check("健康页暴露上报队列", status == 200 and body.get("usageQueue") == usage_store.count_pending())
    upload = body.get("usageUpload") or {}
    check(
        "健康页暴露上报状态",
        {"enabled", "pending", "lastSuccessAtMs", "lastError", "consecutiveFailures", "backoffSeconds"}
        <= set(upload),
        str(upload),
    )


# ── T4.4 配额调整与 G4 超限 ───────────────────────────────


def test_quota_overrides() -> None:
    print("\n[配额调整：鉴权与参数]")
    status, _, body = http("PATCH", "/gateway/v1/admin/quotas/acme:dept-sales:*", body={"monthlyTokens": 1})
    check("配额调整需要管理令牌", status == 401 and body.get("code") == "AUTH_REQUIRED")
    status, _, body = patch_quota("acme", {"monthlyTokens": 1000})
    check("主体格式错误被拒", status == 400 and body.get("code") == "INVALID_REQUEST", str(body))
    status, _, body = patch_quota("acme:dept-sales:*", {})
    check("缺少 monthlyTokens 被拒", status == 400 and body.get("code") == "INVALID_REQUEST")
    status, _, body = patch_quota("acme:dept-sales:*", {"monthlyTokens": True})
    check("布尔不被当作整数", status == 400 and body.get("code") == "INVALID_REQUEST")
    status, _, body = patch_quota("acme:dept-sales:*", {"monthlyTokens": 1000, "period": "2026-13"})
    check("非法周期被拒", status == 400 and body.get("code") == "INVALID_REQUEST")

    # 先结算 700 token，验证 used/remaining 口径
    execution = service.begin_chat(
        Principal(
            subject="u-1",
            session_id="s-1",
            tenant="acme",
            department="dept-sales",
            departments=("dept-sales",),
            roles=("member",),
            auth_kind="jwt",
        ),
        "fake-chat",
        max_tokens=1024,
        prompt_estimate=50,
        request_id=f"quota-settle-{STAMP}",
        endpoint="/v1/chat/completions",
        stream=False,
    )
    service._quota.settle(execution.reservation, 700)

    print("\n[配额调整：当月覆盖立即生效]")
    status, _, body = patch_quota("acme:dept-sales:*", {"monthlyTokens": 10000}, actor="ops-tester")
    current_month = time.strftime("%Y-%m", time.gmtime())
    check("部门级额度调整成功", status == 200 and body.get("ok") is True, str(body))
    check(
        "返回生效额度",
        body.get("principal") == "acme:dept-sales:*"
        and body.get("effectiveMonthlyTokens") == 10000
        and body.get("previousMonthlyTokens") is None
        and body.get("period") == current_month,
        str(body),
    )
    check("返回已用与剩余", body.get("usedTokens") == 700 and body.get("remainingTokens") == 9300, str(body))
    check("重置时间带时区", str(body.get("resetAt", "")).endswith("Z"))
    check("操作人已记录", body.get("updatedBy") == "ops-tester")

    status, _, body = http("GET", "/gateway/v1/admin/quotas", headers=admin_headers())
    overrides = {item["principal"]: item for item in body.get("overrides") or []}
    check("配额快照包含覆盖", status == 200 and overrides.get("acme:dept-sales:*", {}).get("monthlyTokens") == 10000, str(overrides))
    check("配额快照带当月计数", any(row["principal"] == "acme:dept-sales:u-1" and row["committedTokens"] == 700 for row in body.get("quotas") or []))

    audit = sqlite_rows(
        "SELECT actor, before_json, after_json FROM audit_log "
        "WHERE action='quota.update' AND object='acme:dept-sales:*' ORDER BY id DESC LIMIT 1"
    )
    check("配额调整写审计", len(audit) == 1 and audit[0]["actor"] == "ops-tester", str([dict(r) for r in audit]))
    if audit:
        check("审计含前后值", json.loads(audit[0]["after_json"]) == {"monthlyTokens": 10000}
              and json.loads(audit[0]["before_json"]) == {"monthlyTokens": None}, str(dict(audit[0])))

    check("上调后请求可通过（立即生效）", quota_error() is None)
    status, _, body = patch_quota("acme:dept-sales:*", {"monthlyTokens": 100})
    check("下调额度成功", status == 200 and body.get("effectiveMonthlyTokens") == 100)
    check("下调后请求被拒（立即生效）", quota_error() == "QUOTA_TOKENS_EXCEEDED")

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=support.bearer(),
        body={"model": "fake-chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024},
    )
    error = (body or {}).get("error") or {}
    check("超限返回 429", status == 429, f"status={status} body={body}")
    check("错误码为 QUOTA_TOKENS_EXCEEDED", error.get("code") == "QUOTA_TOKENS_EXCEEDED", str(error))
    check("429 带重置时间头", str(headers.get("x-tt-quota-reset", "")).endswith("Z"), str(headers))

    print("\n[配额调整：个人覆盖优先于部门级]")
    status, _, body = patch_quota("acme:dept-sales:u-1", {"monthlyTokens": 500000}, actor="ops-tester")
    check("个人额度调整成功", status == 200 and body.get("effectiveMonthlyTokens") == 500000, str(body))
    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=support.bearer(),
        body={"model": "fake-chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024},
    )
    check(
        "个人覆盖后不再超限（进入上游解析）",
        status == 503 and ((body or {}).get("error") or {}).get("code") == "PROVIDER_UNAVAILABLE",
        f"status={status} body={body}",
    )
    check(
        "响应带剩余额度头",
        str(headers.get("x-tt-quota-remaining", "")).isdigit(),
        str(headers),
    )
    check("未设个人覆盖的成员仍受部门级限制", quota_error("u-2") == "QUOTA_TOKENS_EXCEEDED")

    status, _, body = patch_quota("acme:dept-sales:u-1", {"monthlyTokens": None})
    check(
        "清除个人覆盖回落到部门级",
        status == 200 and body.get("effectiveMonthlyTokens") == 100,
        str(body),
    )
    status, _, body = patch_quota("acme:dept-sales:*", {"monthlyTokens": None})
    check("清除部门覆盖成功", status == 200 and body.get("previousMonthlyTokens") == 100)
    status, _, body = http("GET", "/gateway/v1/admin/quotas", headers=admin_headers())
    remaining = {item["principal"] for item in body.get("overrides") or []}
    check("清除后覆盖表为空", "acme:dept-sales:*" not in remaining and "acme:dept-sales:u-1" not in remaining, str(remaining))
    check("恢复策略值后请求可通过", quota_error() is None)


# ── 主流程 ────────────────────────────────────────────────


def main() -> int:
    test_uploader()
    server = support.start_gateway()
    try:
        test_admin_usage()
        test_quota_overrides()
    finally:
        support.stop_gateway(server)
    return report("M1.4 网关侧自测通过")


if __name__ == "__main__":
    sys.exit(main())
