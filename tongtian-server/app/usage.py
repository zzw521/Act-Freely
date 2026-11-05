"""模型用量上报与合并视图（缺陷 D-43 的服务端部分）。

归属：本模块只写服务端自己的库，不读取桌面 SQLite、也不读取引擎台账文件——
跨库访问由 `tests/unit/storage/ownership-contract.test.ts` 拦截。客户端要做的是
「把自己已经累计好的台账按行推上来」，不是把库文件交给服务端解析。

为什么要「来源（origin）」这一维：桌面 `model_usage` 与引擎台账会记到同一批调用
（桌面经引擎通道发出的请求两边都有），合并时必须只排除引擎侧明确标记为 `desktop`
的那部分；`web`/`im`/`cli`/`schedule` 的调用桌面根本没记，必须原样补入，否则这部分
用量会在合并视图里静默消失。没有来源标记的调用归入 `unknown`，照常计入总量并单列
披露（`unattributedTokens`），既不假装是桌面的，也不当作零。

上报是幂等的：来的是「设备 × 来源 × 用户 × 模型 × 日」的累计值，按主键覆盖，
同一批数据重复上报不会重复计数；`tokensUsed` 由服务端按 prompt+completion 重算，
与来值不一致直接拒绝（口径漂移要在入口就拦住，不留到报表里对不上）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import require_admin, require_user
from .db import now_ms, tx
from .errors import ApiError
from .idempotency import guarded
from .orgs import primary_department

router = APIRouter()

SOURCE_DESKTOP = "desktop"
SOURCE_ENGINE = "engine"
SOURCE_GATEWAY = "gateway"
SOURCES = (SOURCE_DESKTOP, SOURCE_ENGINE)

# 与引擎 `usage_store.USAGE_ORIGINS` 同名同义（跨项目不能互相 import，
# 由 tests/unit/storage/usage-merge-contract.test.ts 守住这两份清单不漂移）
ORIGIN_DESKTOP = "desktop"
ORIGIN_GATEWAY = "gateway"
ORIGIN_UNKNOWN = "unknown"
ORIGINS = ("desktop", "web", "im", "cli", "schedule", "gateway", "unknown")
ORIGIN_ALIASES = {"tauri": ORIGIN_DESKTOP, "browser": "web", "desktop-app": ORIGIN_DESKTOP}

PERIODS = ("daily", "monthly", "total")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_ROWS_PER_REPORT = 2000
MAX_ID_LEN = 120
MAX_TOKENS = 10**15
MAX_LATENCY_MS = 10**9


class UsageCostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str
    micros: int = Field(default=0, ge=0, le=MAX_TOKENS)


class UsageRowIn(BaseModel):
    """上报行：字段名与引擎 `/api/usage/export` 的导出行、桌面契约同名同义。"""

    model_config = ConfigDict(extra="forbid")
    sub: str
    modelName: str
    day: str
    origin: str = ""
    org: str = ""
    promptTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    completionTokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    tokensUsed: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    calls: int = Field(default=0, ge=0, le=MAX_TOKENS)
    pricedCalls: int = Field(default=0, ge=0, le=MAX_TOKENS)
    unpricedCalls: int = Field(default=0, ge=0, le=MAX_TOKENS)
    latencyMsTotal: int = Field(default=0, ge=0, le=MAX_LATENCY_MS)
    latencySamples: int = Field(default=0, ge=0, le=MAX_TOKENS)
    costs: dict[str, int] | list[UsageCostIn] | None = None
    lastUsedAtMs: int = Field(default=0, ge=0, le=10**15)

    @field_validator("costs")
    @classmethod
    def _costs_non_negative(cls, costs: Any) -> Any:
        if isinstance(costs, dict):
            for code, micros in costs.items():
                if int(micros) < 0 or not str(code).strip():
                    raise ValueError("costs 的币种不能为空且金额非负")
        return costs


class UsageReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    deviceId: str = ""
    schemaVersion: int = Field(default=1, ge=1)
    rows: list[UsageRowIn] = Field(default_factory=list)


def _normalize_period(raw: str) -> str:
    period = (raw or "").strip().lower()
    if period not in PERIODS:
        raise ApiError(400, "USAGE_INVALID_PERIOD", f"周期只支持 {'/'.join(PERIODS)}：{raw}")
    return period


def _normalize_source(raw: str) -> str:
    source = (raw or "").strip().lower()
    if source not in SOURCES:
        raise ApiError(400, "USAGE_INVALID_SOURCE", f"来源只支持 {'/'.join(SOURCES)}：{raw}")
    return source


def _normalize_origin(raw: str, source: str) -> str:
    """桌面行统一写 desktop：合并规则只认「引擎行里 origin==desktop 的那部分是重复的」"""
    if source == SOURCE_DESKTOP:
        return ORIGIN_DESKTOP
    key = (raw or "").strip().lower()
    if not key:
        return ORIGIN_UNKNOWN
    return ORIGIN_ALIASES.get(key, key if key in ORIGINS else ORIGIN_UNKNOWN)


def _clean_id(raw: str, label: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ApiError(400, "USAGE_INVALID_ROW", f"{label}不能为空")
    if len(value) > MAX_ID_LEN:
        raise ApiError(400, "USAGE_INVALID_ROW", f"{label}超过 {MAX_ID_LEN} 个字符")
    return value


def _costs_to_dict(costs: dict[str, int] | list[UsageCostIn] | None) -> dict[str, int]:
    if not costs:
        return {}
    items: list[tuple[str, int]]
    if isinstance(costs, dict):
        items = [(str(code), int(micros)) for code, micros in costs.items()]
    else:
        items = [(str(item.currency), int(item.micros)) for item in costs]
    out: dict[str, int] = {}
    for code, micros in items:
        key = (code or "").strip().upper() or "CNY"
        out[key] = out.get(key, 0) + max(0, micros)
    return out


def _assert_scope(user: dict, sub: str) -> None:
    if user.get("role") == "admin":
        return
    if sub != user.get("uid"):
        raise ApiError(403, "USAGE_SCOPE_DENIED", "只能上报或查询本人用量")


@router.post("/api/v1/usage/report")
def api_usage_report(
    payload: UsageReportIn,
    request: Request,
    response: Response,
    user: dict = Depends(require_user),
) -> dict:
    """接收客户端累计好的用量行；非管理员只能上报本人（JWT sub）的用量。

    带 `Idempotency-Key` 时走重放保护（缺陷 D-19 / 卡 #154 刀 B）。这条尤其值得接：
    上报的是**累计值**且 `ON CONFLICT DO UPDATE` 直接覆盖，旧 payload 在网络抖动后重试
    会把已经报上去的新值打回去；同一把键 + 同一份 body 的重试只拿第一次的回执，不再写库。
    """
    return guarded(
        "usage.report",
        request,
        response,
        actor=str(user["uid"]),
        digest_source=payload.model_dump(mode="json"),
        handler=lambda: _report_usage(user, payload),
    )


def _report_usage(user: dict, payload: UsageReportIn) -> dict:
    """真正的落库。没带键时 `guarded` 直接调这里，行为与接键之前逐字节一致。"""
    source = _normalize_source(payload.source)
    device = (payload.deviceId or "").strip()
    if len(device) > MAX_ID_LEN:
        raise ApiError(400, "USAGE_INVALID_ROW", f"deviceId 超过 {MAX_ID_LEN} 个字符")
    if len(payload.rows) > MAX_ROWS_PER_REPORT:
        raise ApiError(400, "USAGE_TOO_MANY_ROWS", f"单次上报不超过 {MAX_ROWS_PER_REPORT} 行")
    if payload.schemaVersion != 1:
        raise ApiError(
            400, "USAGE_SCHEMA_UNSUPPORTED", f"服务端不支持台账版本 {payload.schemaVersion}"
        )

    stamp = now_ms()
    accepted = 0
    org_cache: dict[str, str] = {}
    dept_cache: dict[tuple[str, str], str] = {}
    with tx() as conn:
        for index, row in enumerate(payload.rows):
            sub = _clean_id(row.sub, "sub")
            model = _clean_id(row.modelName, "modelName")
            day = (row.day or "").strip()
            if not DAY_RE.match(day):
                raise ApiError(400, "USAGE_INVALID_ROW", f"第 {index + 1} 行的 day 不是 yyyy-mm-dd")
            _assert_scope(user, sub)
            org = (row.org or "").strip()
            if not org:
                if sub not in org_cache:
                    owner = conn.execute(
                        "SELECT org_id FROM users WHERE uid = ?1", (sub,)
                    ).fetchone()
                    org_cache[sub] = str(owner["org_id"] or "") if owner else ""
                org = org_cache[sub]
            dept = ""
            if org:
                if (sub, org) not in dept_cache:
                    dept_cache[(sub, org)] = primary_department(conn, sub, org)
                dept = dept_cache[(sub, org)]
            tokens = row.promptTokens + row.completionTokens
            if row.tokensUsed is not None and row.tokensUsed != tokens:
                raise ApiError(
                    400,
                    "USAGE_TOKENS_MISMATCH",
                    f"第 {index + 1} 行 tokensUsed({row.tokensUsed}) != prompt+completion({tokens})",
                )
            origin = _normalize_origin(row.origin, source)
            conn.execute(
                """
                INSERT INTO usage_rollup (
                    source, device_id, sub, org_id, dept_id, model_name, day, origin,
                    prompt_tokens, completion_tokens, calls, priced_calls, unpriced_calls,
                    latency_samples, latency_ms_total, costs_json, last_used_at_ms, reported_at_ms
                ) VALUES (
                    :source, :device, :sub, :org, :dept, :model, :day, :origin,
                    :prompt, :completion, :calls, :priced, :unpriced,
                    :samples, :latency, :costs, :lastUsed, :reported
                )
                ON CONFLICT(source, device_id, sub, model_name, day, origin) DO UPDATE SET
                    org_id = excluded.org_id,
                    dept_id = excluded.dept_id,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    calls = excluded.calls,
                    priced_calls = excluded.priced_calls,
                    unpriced_calls = excluded.unpriced_calls,
                    latency_samples = excluded.latency_samples,
                    latency_ms_total = excluded.latency_ms_total,
                    costs_json = excluded.costs_json,
                    last_used_at_ms = excluded.last_used_at_ms,
                    reported_at_ms = excluded.reported_at_ms
                """,
                {
                    "source": source,
                    "device": device,
                    "sub": sub,
                    "org": org,
                    "dept": dept,
                    "model": model,
                    "day": day,
                    "origin": origin,
                    "prompt": row.promptTokens,
                    "completion": row.completionTokens,
                    "calls": row.calls,
                    "priced": row.pricedCalls,
                    "unpriced": row.unpricedCalls,
                    "samples": row.latencySamples,
                    "latency": row.latencyMsTotal,
                    "costs": json.dumps(
                        _costs_to_dict(row.costs), separators=(",", ":"), ensure_ascii=False
                    ),
                    "lastUsed": row.lastUsedAtMs,
                    "reported": stamp,
                },
            )
            accepted += 1
    return {"ok": True, "source": source, "accepted": accepted, "generatedAtMs": stamp}


def _empty_bucket() -> dict[str, Any]:
    return {
        "promptTokens": 0,
        "completionTokens": 0,
        "tokensUsed": 0,
        "calls": 0,
        "lastUsedAtMs": 0,
        "latencyMsTotal": 0,
        "latencySamples": 0,
        "costs": {},
        "pricedCalls": 0,
        "unpricedCalls": 0,
    }


def _merge_into(target: dict[str, Any], source_row: dict[str, Any]) -> None:
    target["promptTokens"] += int(source_row.get("promptTokens") or 0)
    target["completionTokens"] += int(source_row.get("completionTokens") or 0)
    target["tokensUsed"] += int(source_row.get("tokensUsed") or 0)
    target["calls"] += int(source_row.get("calls") or 0)
    target["lastUsedAtMs"] = max(
        target["lastUsedAtMs"], int(source_row.get("lastUsedAtMs") or 0)
    )
    target["latencyMsTotal"] += int(source_row.get("latencyMsTotal") or 0)
    target["latencySamples"] += int(source_row.get("latencySamples") or 0)
    target["pricedCalls"] += int(source_row.get("pricedCalls") or 0)
    target["unpricedCalls"] += int(source_row.get("unpricedCalls") or 0)
    costs = target["costs"]
    for code, micros in (source_row.get("costs") or {}).items():
        costs[code] = costs.get(code, 0) + int(micros)


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    """收口为对外字段：平均延迟一律向下取整，且不带内部累计用的 latencyMsTotal"""
    samples = int(bucket.get("latencySamples") or 0)
    total = int(bucket.get("latencyMsTotal") or 0)
    costs = bucket.get("costs") or {}
    out = {key: value for key, value in bucket.items() if key != "latencyMsTotal"}
    out["avgLatencyMs"] = total // samples if samples else None
    out["costs"] = [{"currency": code, "micros": int(costs[code])} for code in sorted(costs)]
    return out


def merged_view(conn, sub: str, period: str, now: int) -> dict:
    """桌面为主、引擎补入的唯一合并视图（三处口径中的服务端那一处）。"""
    today = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    month = today[:7]
    models: dict[str, dict[str, Any]] = {}
    days: dict[str, dict[str, Any]] = {}
    names_by_day: dict[str, set[str]] = {}
    stats = {
        "desktopRows": 0,
        "engineRows": 0,
        "engineDedupedRows": 0,
        "engineDedupedTokens": 0,
        "gatewayRows": 0,
        "gatewayDedupedRows": 0,
        "gatewayDedupedTokens": 0,
        "unattributedTokens": 0,
        "unattributedCalls": 0,
        "devices": set(),
    }
    for raw in conn.execute(
        "SELECT * FROM usage_rollup WHERE sub = ?1", (sub,)
    ).fetchall():
        day = raw["day"]
        if period == "daily" and day != today:
            continue
        if period == "monthly" and not day.startswith(month):
            continue
        source = raw["source"]
        origin = raw["origin"]
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
        if source == SOURCE_ENGINE and origin == ORIGIN_DESKTOP:
            # 桌面自己已经记过这批调用，计入合并视图就是重复计数
            stats["engineDedupedRows"] += 1
            stats["engineDedupedTokens"] += row["tokensUsed"]
            continue
        if source != SOURCE_GATEWAY and origin == ORIGIN_GATEWAY:
            # 经网关的调用以网关账本为准；客户端这份只是同一请求的副本
            stats["gatewayDedupedRows"] += 1
            stats["gatewayDedupedTokens"] += row["tokensUsed"]
            continue
        if source == SOURCE_GATEWAY:
            stats["gatewayRows"] += 1
        elif source == SOURCE_ENGINE:
            stats["engineRows"] += 1
        else:
            stats["desktopRows"] += 1
        stats["devices"].add(f"{source}/{raw['device_id']}")
        if source == SOURCE_ENGINE and origin == ORIGIN_UNKNOWN:
            stats["unattributedTokens"] += row["tokensUsed"]
            stats["unattributedCalls"] += row["calls"]

        model = models.get(raw["model_name"])
        if model is None:
            model = _empty_bucket()
            models[raw["model_name"]] = model
        _merge_into(model, row)

        bucket = days.get(day)
        if bucket is None:
            bucket = _empty_bucket()
            days[day] = bucket
            names_by_day[day] = set()
        _merge_into(bucket, row)
        names_by_day[day].add(raw["model_name"])

    ordered = sorted(models, key=lambda key: (-models[key]["tokensUsed"], key))
    named_models: list[dict[str, Any]] = []
    totals = _empty_bucket()
    weighted_latency = 0
    for name in ordered:
        bucket = models[name]
        item = _finish(bucket)
        item["modelName"] = name
        named_models.append(item)
        _merge_into(totals, bucket)
        # 汇总的耗时按「已取整的模型均值 × 样本数」加权：与 Rust totals_of、
        # 引擎 totals()、前端 totalsOf 三处同一算法，否则同数据会差 1ms
        weighted_latency += int(item["avgLatencyMs"] or 0) * int(item["latencySamples"] or 0)
    totals["latencyMsTotal"] = weighted_latency
    totals_out = _finish(totals)
    totals_out["activeModels"] = len(named_models)

    day_rows = []
    for day in sorted(days):
        item = _finish(days[day])
        item["day"] = day
        item["models"] = len(names_by_day[day])
        day_rows.append(item)

    return {
        "ok": True,
        "period": period,
        "sub": sub,
        "totals": totals_out,
        "models": named_models,
        "days": day_rows,
        "sources": {
            "desktopRows": stats["desktopRows"],
            "engineRows": stats["engineRows"],
            "engineDedupedRows": stats["engineDedupedRows"],
            "engineDedupedTokens": stats["engineDedupedTokens"],
            "gatewayRows": stats["gatewayRows"],
            "gatewayDedupedRows": stats["gatewayDedupedRows"],
            "gatewayDedupedTokens": stats["gatewayDedupedTokens"],
            "unattributedTokens": stats["unattributedTokens"],
            "unattributedCalls": stats["unattributedCalls"],
            "reporters": len(stats["devices"]),
        },
        "generatedAtMs": now,
    }


@router.get("/api/v1/usage/me")
def api_usage_me(period: str = Query("monthly"), user: dict = Depends(require_user)) -> dict:
    """按 JWT 到人查询合并用量：桌面为主，引擎按来源补入，未标记的单列披露。"""
    p = _normalize_period(period)
    sub = _clean_id(str(user.get("uid") or ""), "sub")
    with tx() as conn:
        return merged_view(conn, sub, p, now_ms())


@router.get("/api/v1/usage/users/{sub}")
def api_usage_user(
    sub: str, period: str = Query("monthly"), _admin: dict = Depends(require_admin)
) -> dict:
    """管理员按用户查询同一份合并视图（口径与 /me 完全一致，不是另一套算法）。"""
    p = _normalize_period(period)
    target = _clean_id(sub, "sub")
    with tx() as conn:
        return merged_view(conn, target, p, now_ms())
