"""用量事件落库、幂等与成本计算。

`usage_events` 是网关账本：每个进入上游的请求写一条（成功/失败/取消）。
成本用 `Decimal` 按策略定价计算，保留 6 位小数，不做浮点累加。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Mapping

from .adapters.base import ChatMessage, ImagePart, TextPart, Usage
from .contract import UsageEvent
from .db import Database

MICROS = Decimal(1_000_000)
SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class CostResult:
    amount: str
    micros: int
    currency: str


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"金额不是合法十进制：{value!r}") from exc


def compute_cost(
    pricing: Mapping | None, usage: Usage, *, default_currency: str = "USD"
) -> CostResult:
    if not pricing:
        return CostResult("0", 0, default_currency)
    currency = str(pricing.get("currency") or default_currency)
    input_price = _decimal(pricing.get("inputPer1M", "0"))
    output_price = _decimal(pricing.get("outputPer1M", "0"))
    cached_price = (
        _decimal(pricing["cachedInputPer1M"])
        if pricing.get("cachedInputPer1M") is not None
        else input_price
    )
    reasoning_as_output = bool(pricing.get("reasoningAsOutput", True))

    cached = min(max(0, usage.cached_tokens), usage.prompt_tokens)
    reasoning = min(max(0, usage.reasoning_tokens), usage.completion_tokens)
    billable_input = usage.prompt_tokens - cached
    billable_output = usage.completion_tokens - (0 if reasoning_as_output else reasoning)

    total = (
        Decimal(billable_input) * input_price
        + Decimal(cached) * cached_price
        + Decimal(billable_output) * output_price
    ) / MICROS
    amount = total.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    return CostResult(str(amount), int(amount * MICROS), currency)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_chars = len(text) - non_ascii
    # 中文按接近 1 字 1 token 的保守估计，英文按 4 字符 1 token
    return max(1, non_ascii + (ascii_chars + 3) // 4)


def estimate_prompt_tokens(
    messages: tuple[ChatMessage, ...], system: str | None = None
) -> int:
    total = estimate_text_tokens(system or "")
    for message in messages:
        total += 4
        content = message.content
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, tuple):
            for part in content:
                if isinstance(part, TextPart):
                    total += estimate_text_tokens(part.text)
                elif isinstance(part, ImagePart):
                    total += 85
        for call in message.tool_calls:
            total += estimate_text_tokens(call.name) + estimate_text_tokens(call.arguments)
    return max(1, total)


def estimate_completion_tokens(text: str) -> int:
    return estimate_text_tokens(text)


def _period_bounds(period: str) -> tuple[int, int] | None:
    """UTC 自然日/自然月的 [起, 止) 毫秒边界，避免依赖后端日期函数。"""
    if period not in ("daily", "monthly"):
        return None
    now = datetime.now(timezone.utc)
    if period == "daily":
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    else:
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        end = datetime(
            now.year + (1 if now.month == 12 else 0),
            now.month % 12 + 1,
            1,
            tzinfo=timezone.utc,
        )
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


class UsageStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, event: UsageEvent) -> bool:
        try:
            micros = int(
                (_decimal(event.cost) * MICROS).to_integral_value(rounding=ROUND_HALF_UP)
            )
        except ValueError:
            micros = 0
        with self._db.tx() as conn:
            cursor = conn.execute(
                "INSERT INTO usage_events ("
                "gateway_id, request_id, tenant, dept, sub, project_id, api_key_id, "
                "protocol, endpoint, model_alias, model, provider, prompt_tokens, "
                "completion_tokens, cached_tokens, reasoning_tokens, total_tokens, "
                "cost_micros, currency, price_version, latency_ms, status, error_code, "
                "stream, estimated, started_at_ms, finished_at_ms, uploaded_at_ms"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(gateway_id, request_id) DO NOTHING",
                (
                    event.gatewayId,
                    event.requestId,
                    event.tenantId,
                    event.departmentId or "",
                    event.userId or "",
                    event.projectId or "",
                    event.apiKeyId or "",
                    event.protocol,
                    event.endpoint,
                    event.modelAlias or "",
                    event.model,
                    event.provider,
                    event.promptTokens,
                    event.completionTokens,
                    event.cachedTokens,
                    event.reasoningTokens,
                    event.totalTokens,
                    micros,
                    event.currency,
                    event.priceVersion or "",
                    event.latencyMs,
                    event.status,
                    event.errorCode or "",
                    1 if event.stream else 0,
                    1 if event.estimated else 0,
                    event.startedAt,
                    event.finishedAt,
                ),
            )
            return cursor.rowcount == 1

    def count(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM usage_events")
        return int(rows[0]["n"]) if rows else 0

    def count_pending(self) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM usage_events WHERE uploaded_at_ms = 0"
        )
        return int(rows[0]["n"]) if rows else 0

    def get(self, request_id: str) -> UsageEvent | None:
        rows = self._db.query(
            "SELECT * FROM usage_events WHERE request_id=? ORDER BY finished_at_ms DESC "
            "LIMIT 1",
            (request_id,),
        )
        return _to_event(rows[0]) if rows else None

    def recent(self, limit: int = 20) -> list[UsageEvent]:
        rows = self._db.query(
            "SELECT * FROM usage_events ORDER BY finished_at_ms DESC LIMIT ?",
            (max(1, limit),),
        )
        return [_to_event(row) for row in rows]

    def pending(self, limit: int = 200) -> list[UsageEvent]:
        rows = self._db.query(
            "SELECT * FROM usage_events WHERE uploaded_at_ms = 0 "
            "ORDER BY finished_at_ms LIMIT ?",
            (max(1, limit),),
        )
        return [_to_event(row) for row in rows]

    def mark_uploaded(self, request_ids: list[str], at_ms: int) -> int:
        if not request_ids:
            return 0
        placeholders = ",".join("?" for _ in request_ids)
        with self._db.tx() as conn:
            cursor = conn.execute(
                f"UPDATE usage_events SET uploaded_at_ms=? "
                f"WHERE request_id IN ({placeholders}) AND uploaded_at_ms = 0",
                (at_ms, *request_ids),
            )
            return cursor.rowcount

    def aggregate(
        self,
        *,
        tenant: str = "",
        dept: str = "",
        sub: str = "",
        model: str = "",
        period: str = "monthly",
        limit: int = 500,
    ) -> dict:
        """按租户/部门/用户/模型/日/币种聚合，与 `usage_events` 手算一致。"""
        clauses: list[str] = []
        params: list = []
        for column, value in (
            ("tenant", tenant),
            ("dept", dept),
            ("sub", sub),
            ("model", model),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if period == "daily" or period == "monthly":
            bounds = _period_bounds(period)
            if bounds is not None:
                clauses.append("finished_at_ms >= ? AND finished_at_ms < ?")
                params.extend(bounds)
        day_sql = self._db.day_expression()
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = [
            dict(row)
            for row in self._db.query(
                "SELECT tenant, dept, sub, model, provider, currency, "
                f"{day_sql} AS day, "
                "COUNT(*) AS calls, "
                "SUM(prompt_tokens) AS prompt_tokens, "
                "SUM(completion_tokens) AS completion_tokens, "
                "SUM(total_tokens) AS total_tokens, "
                "SUM(cost_micros) AS cost_micros, "
                "SUM(latency_ms) AS latency_ms_total "
                f"FROM usage_events {where} "
                "GROUP BY tenant, dept, sub, model, provider, day, currency "
                "ORDER BY day DESC, total_tokens DESC, model ASC LIMIT ?",
                (*params, max(1, min(2000, limit))),
            )
        ]
        totals_rows = self._db.query(
            "SELECT currency, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            "COALESCE(SUM(cost_micros), 0) AS cost_micros, "
            "COALESCE(SUM(latency_ms), 0) AS latency_ms_total "
            f"FROM usage_events {where} GROUP BY currency ORDER BY currency",
            tuple(params),
        )
        totals = {
            "calls": 0,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "latencyMsTotal": 0,
            "costs": [],
        }
        for row in totals_rows:
            totals["calls"] += int(row["calls"])
            totals["promptTokens"] += int(row["prompt_tokens"])
            totals["completionTokens"] += int(row["completion_tokens"])
            totals["totalTokens"] += int(row["total_tokens"])
            totals["latencyMsTotal"] += int(row["latency_ms_total"])
            totals["costs"].append(
                {"currency": row["currency"], "micros": int(row["cost_micros"])}
            )
        totals["avgLatencyMs"] = (
            totals["latencyMsTotal"] // totals["calls"] if totals["calls"] else None
        )
        return {"rows": rows, "totals": totals, "rowCount": len(rows)}

    def aggregate_by_model(self) -> list[dict]:
        return [
            dict(row)
            for row in self._db.query(
                "SELECT model, provider, COUNT(*) AS requests, "
                "SUM(prompt_tokens) AS promptTokens, "
                "SUM(completion_tokens) AS completionTokens, "
                "SUM(total_tokens) AS totalTokens, "
                "SUM(cost_micros) AS costMicros "
                "FROM usage_events GROUP BY model, provider ORDER BY model"
            )
        ]


def _to_event(row) -> UsageEvent:
    micros = int(row["cost_micros"])
    cost = "0" if micros == 0 else str((Decimal(micros) / MICROS).quantize(SIX_PLACES))
    return UsageEvent(
        gatewayId=row["gateway_id"],
        requestId=row["request_id"],
        tenantId=row["tenant"],
        departmentId=row["dept"] or None,
        userId=row["sub"] or None,
        apiKeyId=row["api_key_id"] or None,
        projectId=row["project_id"] or None,
        origin="gateway",
        protocol=row["protocol"],
        endpoint=row["endpoint"],
        modelAlias=row["model_alias"] or None,
        model=row["model"],
        provider=row["provider"],
        promptTokens=int(row["prompt_tokens"]),
        completionTokens=int(row["completion_tokens"]),
        cachedTokens=int(row["cached_tokens"]),
        reasoningTokens=int(row["reasoning_tokens"]),
        totalTokens=int(row["total_tokens"]),
        cost=cost,
        currency=row["currency"] or "USD",
        priceVersion=row["price_version"] or None,
        latencyMs=int(row["latency_ms"]),
        status=row["status"],
        errorCode=row["error_code"] or None,
        stream=bool(row["stream"]),
        estimated=bool(row["estimated"]),
        startedAt=int(row["started_at_ms"]),
        finishedAt=int(row["finished_at_ms"]),
    )
