"""网关用量接收与部门报表（M1.4 T4.5–T4.8）。

网关是模型调用的事实来源：每条事件在网关落 `usage_events`，再批量推到控制面。
控制面不反查网关库，只按 `(gatewayId, requestId)` 判重后把增量并入 `usage_rollup`
（`source=gateway`、`origin=gateway`）。判重键在 `gateway_usage_events` 保留 30 天，
所以同一批重放、批次重叠、网关重启后补报都不会重复计数。

整批校验：任一行不合法就整批 400，不写入半批——否则网关会以为这批没成功而整体重试，
缺行与半行会让两边账目长期对不上。响应只回 `{ok, accepted, duplicates}`，
与 CONTRACT §6.2 一致。
"""

from __future__ import annotations

import json
import threading
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .auth import require_admin
from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .gateway_policy import CONTRACT_VERSION, require_service
from .usage import (
    MAX_ID_LEN,
    MAX_LATENCY_MS,
    MAX_TOKENS,
    ORIGIN_DESKTOP,
    ORIGIN_GATEWAY,
    SOURCE_ENGINE,
    SOURCE_GATEWAY,
    _empty_bucket,
    _finish,
    _merge_into,
    _normalize_period,
)

router = APIRouter()

MAX_EVENTS_PER_BATCH = 1000
CLEANUP_INTERVAL_MS = 3600 * 1000
MICROS = Decimal(1_000_000)
COST_RE = r"^(0|[1-9]\d*)(\.\d{1,8})?$"
MAX_COST_MICROS = 10**15

_cleanup_lock = threading.Lock()
_last_cleanup_ms = 0


class GatewayUsageEventIn(BaseModel):
    """CONTRACT §6.1 事件：只收网关发出的 `origin=gateway` 事件。"""

    model_config = ConfigDict(extra="forbid")

    # 事件级契约版本/网关 id 与批级重复出现是契约允许的；出现时必须与批一致
    contractVersion: str | None = None
    gatewayId: str | None = None
    requestId: str
    tenantId: str
    departmentId: str | None = None
    userId: str | None = None
    apiKeyId: str | None = None
    projectId: str | None = None
    origin: str = ORIGIN_GATEWAY
    protocol: Literal["openai", "anthropic", "gemini"]
    endpoint: str = ""
    modelAlias: str | None = None
    model: str
    provider: str = ""
    promptTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    completionTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    cachedTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    reasoningTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    totalTokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    cost: str = Field(default="0", pattern=COST_RE)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    priceVersion: str | None = None
    latencyMs: int = Field(default=0, ge=0, le=MAX_LATENCY_MS)
    status: Literal["ok", "error", "cancelled"]
    errorCode: str | None = None
    stream: bool = False
    estimated: bool = False
    startedAt: int = Field(ge=0, le=10**15)
    finishedAt: int = Field(ge=0, le=10**15)

    @field_validator("origin")
    @classmethod
    def _gateway_only(cls, origin: str) -> str:
        if (origin or "").strip().lower() != ORIGIN_GATEWAY:
            raise ValueError("网关事件 origin 必须是 gateway")
        return ORIGIN_GATEWAY

    @model_validator(mode="after")
    def _consistent(self) -> "GatewayUsageEventIn":
        if self.finishedAt < self.startedAt:
            raise ValueError("finishedAt 不能早于 startedAt")
        total = self.promptTokens + self.completionTokens
        if self.totalTokens is not None and self.totalTokens != total:
            raise ValueError(
                f"totalTokens({self.totalTokens}) 必须等于 prompt+completion({total})"
            )
        return self


def _clean_id(raw: Any, label: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ApiError(400, "GATEWAY_USAGE_INVALID", f"{label}不能为空")
    if len(value) > MAX_ID_LEN:
        raise ApiError(400, "GATEWAY_USAGE_INVALID", f"{label}超过 {MAX_ID_LEN} 个字符")
    return value


def _optional(raw: Any, limit: int = MAX_ID_LEN) -> str:
    value = str(raw or "").strip()
    return value[:limit]


def _cost_micros(cost: str) -> int:
    try:
        value = Decimal(cost)
    except (InvalidOperation, ValueError):
        raise ApiError(400, "GATEWAY_USAGE_INVALID", f"cost 不是合法十进制：{cost}") from None
    micros = int((value * MICROS).to_integral_value(rounding=ROUND_HALF_UP))
    if micros < 0 or micros > MAX_COST_MICROS:
        raise ApiError(400, "GATEWAY_USAGE_INVALID", f"cost 超出允许范围：{cost}")
    return micros


def _day_of(finished_at_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(finished_at_ms / 1000))


def _cleanup_expired(conn, now: int) -> int:
    """清理 30 天前的判重键；每小时最多一次。返回删除行数。"""
    global _last_cleanup_ms
    with _cleanup_lock:
        if now - _last_cleanup_ms < CLEANUP_INTERVAL_MS:
            return 0
        _last_cleanup_ms = now
    retention_days = max(1, settings.gateway_usage_retention_days)
    cutoff = now - retention_days * 86400 * 1000
    cursor = conn.execute(
        "DELETE FROM gateway_usage_events WHERE received_at_ms < ?1", (cutoff,)
    )
    return cursor.rowcount


@router.post("/api/v1/gateway/usage")
def api_gateway_usage(
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict:
    """网关批量上报（服务令牌）：按 `(gatewayId, requestId)` 幂等并入按日台账。"""
    require_service(request)
    contract = str(payload.get("contractVersion") or "").strip()
    if contract != CONTRACT_VERSION:
        raise ApiError(
            400,
            "GATEWAY_USAGE_CONTRACT",
            f"不支持的契约版本：{contract or '(缺失)'}",
        )
    gateway = _clean_id(payload.get("gatewayId"), "gatewayId")
    batch = _clean_id(payload.get("batchId"), "batchId")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ApiError(400, "GATEWAY_USAGE_INVALID", "events 必须是数组")
    if len(raw_events) > MAX_EVENTS_PER_BATCH:
        raise ApiError(
            400,
            "GATEWAY_USAGE_TOO_MANY_EVENTS",
            f"单批最多 {MAX_EVENTS_PER_BATCH} 条事件",
        )

    events: list[GatewayUsageEventIn] = []
    for index, item in enumerate(raw_events):
        try:
            events.append(GatewayUsageEventIn.model_validate(item))
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            location = ".".join(str(part) for part in first.get("loc", ()))
            detail = f"{location} {first.get('msg', '')}".strip()
            raise ApiError(
                400, "GATEWAY_USAGE_INVALID", f"第 {index + 1} 条事件不合法：{detail}"
            ) from None

    for index, event in enumerate(events):
        if event.contractVersion is not None and event.contractVersion != contract:
            raise ApiError(
                400,
                "GATEWAY_USAGE_CONTRACT",
                f"第 {index + 1} 条事件的 contractVersion 与批次不一致",
            )
        if event.gatewayId is not None and event.gatewayId.strip() != gateway:
            raise ApiError(
                400,
                "GATEWAY_USAGE_INVALID",
                f"第 {index + 1} 条事件的 gatewayId 与批次不一致",
            )

    prepared = [
        {
            "request_id": _clean_id(event.requestId, "requestId"),
            "tenant": _clean_id(event.tenantId, "tenantId"),
            "sub": _optional(event.userId) or _optional(event.apiKeyId),
            "dept": _optional(event.departmentId),
            "model": _clean_id(event.model, "model"),
            "day": _day_of(event.finishedAt),
            "micros": _cost_micros(event.cost),
            "event": event,
        }
        for event in events
    ]

    accepted = 0
    duplicates = 0
    stamp = now_ms()
    with tx() as conn:
        _cleanup_expired(conn, stamp)
        for item in prepared:
            event = item["event"]
            tenant = item["tenant"]
            sub = item["sub"]
            dept = item["dept"]
            model = item["model"]
            day = item["day"]
            micros = item["micros"]
            inserted = conn.execute(
                """
                INSERT INTO gateway_usage_events (
                    gateway_id, request_id, batch_id, tenant_id, dept_id, sub, model, day,
                    status, prompt_tokens, completion_tokens, cost_micros, currency,
                    latency_ms, received_at_ms
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)
                ON CONFLICT(gateway_id, request_id) DO NOTHING
                """,
                (
                    gateway,
                    item["request_id"],
                    batch,
                    tenant,
                    dept,
                    sub,
                    model,
                    day,
                    event.status,
                    event.promptTokens,
                    event.completionTokens,
                    micros,
                    event.currency,
                    event.latencyMs,
                    stamp,
                ),
            ).rowcount == 1
            if not inserted:
                duplicates += 1
                continue

            priced = 1 if (event.priceVersion or "").strip() else 0
            existing = conn.execute(
                "SELECT costs_json FROM usage_rollup WHERE source = ?1 AND device_id = ?2 "
                "AND sub = ?3 AND model_name = ?4 AND day = ?5 AND origin = ?6",
                (SOURCE_GATEWAY, gateway, sub, model, day, ORIGIN_GATEWAY),
            ).fetchone()
            costs = json.loads(existing["costs_json"] or "{}") if existing else {}
            costs[event.currency] = int(costs.get(event.currency, 0)) + micros
            costs_json = json.dumps(costs, separators=(",", ":"), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO usage_rollup (
                    source, device_id, sub, org_id, dept_id, model_name, day, origin,
                    prompt_tokens, completion_tokens, calls, priced_calls, unpriced_calls,
                    latency_samples, latency_ms_total, costs_json, last_used_at_ms, reported_at_ms
                ) VALUES (
                    ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8,
                    ?9, ?10, 1, ?11, ?12, 1, ?13, ?14, ?15, ?16
                )
                ON CONFLICT(source, device_id, sub, model_name, day, origin) DO UPDATE SET
                    org_id = excluded.org_id,
                    dept_id = excluded.dept_id,
                    prompt_tokens = usage_rollup.prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = usage_rollup.completion_tokens + excluded.completion_tokens,
                    calls = usage_rollup.calls + 1,
                    priced_calls = usage_rollup.priced_calls + excluded.priced_calls,
                    unpriced_calls = usage_rollup.unpriced_calls + excluded.unpriced_calls,
                    latency_samples = usage_rollup.latency_samples + 1,
                    latency_ms_total = usage_rollup.latency_ms_total + excluded.latency_ms_total,
                    costs_json = excluded.costs_json,
                    last_used_at_ms = MAX(usage_rollup.last_used_at_ms, excluded.last_used_at_ms),
                    reported_at_ms = excluded.reported_at_ms
                """,
                (
                    SOURCE_GATEWAY,
                    gateway,
                    sub,
                    tenant,
                    dept,
                    model,
                    day,
                    ORIGIN_GATEWAY,
                    event.promptTokens,
                    event.completionTokens,
                    priced,
                    0 if priced else 1,
                    event.latencyMs,
                    costs_json,
                    event.finishedAt,
                    stamp,
                ),
            )
            accepted += 1

    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


def _is_deduped(source: str, origin: str) -> bool:
    """客户端行与网关账本重叠的部分：engine 重复桌面已计的，以及经网关调用的。"""
    if source == SOURCE_ENGINE and origin == ORIGIN_DESKTOP:
        return True
    return source != SOURCE_GATEWAY and origin == ORIGIN_GATEWAY


@router.get("/api/v1/admin/usage/departments")
def api_admin_usage_departments(
    period: str = Query("monthly"),
    org: str = Query(""),
    _admin: dict = Depends(require_admin),
) -> dict:
    """部门用量汇总：与个人明细同一套行过滤，逐部门相加等于总量。"""
    p = _normalize_period(period)
    org_filter = (org or "").strip().lower()
    now = now_ms()
    today = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    month = today[:7]

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    users: dict[tuple[str, str], set[str]] = {}
    models_seen: dict[tuple[str, str], set[str]] = {}
    totals = _empty_bucket()

    with tx() as conn:
        if org_filter:
            rows = conn.execute(
                "SELECT * FROM usage_rollup WHERE org_id = ?1", (org_filter,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM usage_rollup").fetchall()
        names = {
            (row["org_id"], row["id"]): row["name"]
            for row in conn.execute("SELECT org_id, id, name FROM departments")
        }

    for raw in rows:
        day = raw["day"]
        if p == "daily" and day != today:
            continue
        if p == "monthly" and not day.startswith(month):
            continue
        if _is_deduped(raw["source"], raw["origin"]):
            continue
        key = (raw["org_id"], raw["dept_id"])
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _empty_bucket()
            buckets[key] = bucket
            users[key] = set()
            models_seen[key] = set()
        row = {
            "promptTokens": raw["prompt_tokens"],
            "completionTokens": raw["completion_tokens"],
            "tokensUsed": raw["prompt_tokens"] + raw["completion_tokens"],
            "calls": raw["calls"],
            "lastUsedAtMs": raw["last_used_at_ms"],
            "latencyMsTotal": raw["latency_ms_total"],
            "latencySamples": raw["latency_samples"],
            "pricedCalls": raw["priced_calls"],
            "unpricedCalls": raw["unpriced_calls"],
            "costs": json.loads(raw["costs_json"] or "{}"),
        }
        _merge_into(bucket, row)
        _merge_into(totals, row)
        users[key].add(raw["sub"])
        models_seen[key].add(raw["model_name"])

    departments = []
    weighted_latency = 0
    for key in sorted(buckets, key=lambda item: (-buckets[item]["tokensUsed"], item)):
        item = _finish(buckets[key])
        item["org"] = key[0]
        item["dept"] = key[1]
        item["name"] = names.get(key) or ("未分配" if not key[1] else key[1])
        item["users"] = len(users[key])
        item["models"] = len(models_seen[key])
        departments.append(item)
        weighted_latency += int(item["avgLatencyMs"] or 0) * int(item["latencySamples"] or 0)

    totals["latencyMsTotal"] = weighted_latency
    totals_out = _finish(totals)
    totals_out["departments"] = len(departments)
    return {
        "ok": True,
        "period": p,
        "org": org_filter,
        "departments": departments,
        "totals": totals_out,
        "generatedAtMs": now,
    }
