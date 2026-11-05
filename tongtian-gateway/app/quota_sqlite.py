"""SQLite 配额：按月 token 预留/结算 + 并发计数。

月度已用量是持久事实（quota_counters）。未结算的预留与并发计数：
单 worker 在进程内；配置 Redis 时（M5）放到共享状态，多 worker 共用同一份。
重启/崩溃留下的共享预留按 TTL（1 小时）保守回收，不污染账本。
`usage_events` 是事实来源，计数器可重建。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .audit import append as append_audit
from .db import Database, now_ms
from .errors import GatewayError
from .quota import _period
from .shared_state import SharedStateError

logger = logging.getLogger(__name__)


@dataclass
class Reservation:
    reservation_id: str
    principal_key: str
    tenant: str
    dept: str
    sub: str
    period: str
    tokens: int
    settled: bool = False
    api_key_id: str = ""
    api_key_tokens: int = 0


def _split_principal(principal_key: str) -> tuple[str, str, str]:
    parts = principal_key.split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(f"配额主体格式应为 tenant:dept:sub，收到 {principal_key!r}")
    return parts[0], parts[1], parts[2]


class SqliteQuota:
    def __init__(
        self,
        db: Database,
        clock: Callable[[], float] = time.time,
        shared=None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._lock = threading.RLock()
        self._reserved: dict[tuple[str, str], int] = {}
        self._key_reserved: dict[tuple[str, str], int] = {}
        self._active: dict[str, int] = {}
        self._shared = shared

    @property
    def _shared_enabled(self) -> bool:
        return self._shared is not None and self._shared.enabled

    def _used(self, conn, tenant: str, dept: str, sub: str, period: str) -> int:
        row = conn.execute(
            "SELECT used_tokens FROM quota_counters "
            "WHERE period=? AND tenant=? AND dept=? AND sub=?",
            (period, tenant, dept, sub),
        ).fetchone()
        return int(row["used_tokens"]) if row else 0

    def _key_used(self, conn, key_id: str, period: str) -> int:
        row = conn.execute(
            "SELECT used_tokens FROM api_key_counters WHERE period=? AND key_id=?",
            (period, key_id),
        ).fetchone()
        return int(row["used_tokens"]) if row else 0

    def reserve(
        self,
        principal_key: str,
        limits: dict,
        tokens: int,
        *,
        api_key_id: str | None = None,
        api_key_monthly: int = 0,
    ) -> Reservation:
        if tokens < 0:
            raise GatewayError.of("INVALID_REQUEST", "预留 token 不能为负数")
        tenant, dept, sub = _split_principal(principal_key)
        if self._shared_enabled:
            return self._reserve_shared(
                principal_key,
                tenant,
                dept,
                sub,
                limits,
                tokens,
                api_key_id=api_key_id,
                api_key_monthly=api_key_monthly,
            )
        with self._lock:
            period = _period(self._clock())
            counter_key = (principal_key, period)
            with self._db.tx() as conn:
                used = self._used(conn, tenant, dept, sub, period) + self._reserved.get(
                    counter_key, 0
                )
                monthly = limits.get("monthlyTokens")
                if isinstance(monthly, int) and used + tokens > monthly:
                    raise GatewayError.of(
                        "QUOTA_TOKENS_EXCEEDED",
                        f"当月 token 配额不足（已用 {used}，预留 {tokens}，上限 {monthly}）",
                    )
                key_counter = (api_key_id, period) if api_key_id else ""
                if key_counter and api_key_monthly > 0:
                    key_used = self._key_used(
                        conn, api_key_id, period
                    ) + self._key_reserved.get(key_counter, 0)
                    if key_used + tokens > api_key_monthly:
                        raise GatewayError.of(
                            "QUOTA_TOKENS_EXCEEDED",
                            "API Key 当月 token 配额不足"
                            f"（已用 {key_used}，预留 {tokens}，上限 {api_key_monthly}）",
                        )
                concurrency = limits.get("concurrency")
                active = self._active.get(principal_key, 0)
                if isinstance(concurrency, int) and active >= concurrency:
                    raise GatewayError.of(
                        "QUOTA_CONCURRENCY_EXCEEDED",
                        f"并发请求数超限（上限 {concurrency}）",
                    )
                self._reserved[counter_key] = (
                    self._reserved.get(counter_key, 0) + tokens
                )
                if key_counter:
                    self._key_reserved[key_counter] = (
                        self._key_reserved.get(key_counter, 0) + tokens
                    )
                self._active[principal_key] = active + 1
            return Reservation(
                reservation_id=uuid.uuid4().hex,
                principal_key=principal_key,
                tenant=tenant,
                dept=dept,
                sub=sub,
                period=period,
                tokens=tokens,
                api_key_id=api_key_id or "",
                api_key_tokens=tokens if api_key_id else 0,
            )

    def _reserve_shared(
        self,
        principal_key: str,
        tenant: str,
        dept: str,
        sub: str,
        limits: dict,
        tokens: int,
        *,
        api_key_id: str | None,
        api_key_monthly: int,
    ) -> Reservation:
        """已结算用量读 SQLite，在途预留/并发由 Redis Lua 原子判定。"""
        period = _period(self._clock())
        with self._db.read() as conn:
            used = self._used(conn, tenant, dept, sub, period)
            key_used = (
                self._key_used(conn, api_key_id, period) if api_key_id else 0
            )
        monthly = limits.get("monthlyTokens")
        concurrency = limits.get("concurrency")
        reservation = Reservation(
            reservation_id=uuid.uuid4().hex,
            principal_key=principal_key,
            tenant=tenant,
            dept=dept,
            sub=sub,
            period=period,
            tokens=tokens,
            api_key_id=api_key_id or "",
            api_key_tokens=tokens if api_key_id else 0,
        )
        try:
            self._shared.reserve(
                reservation_id=reservation.reservation_id,
                principal_key=principal_key,
                tenant=tenant,
                dept=dept,
                sub=sub,
                period=period,
                tokens=tokens,
                used=used,
                monthly=monthly if isinstance(monthly, int) else 0,
                concurrency=concurrency if isinstance(concurrency, int) else 0,
                api_key_id=api_key_id or "",
                key_used=key_used,
                key_monthly=api_key_monthly if api_key_monthly > 0 else 0,
            )
        except SharedStateError as exc:
            raise GatewayError.of(
                "SHARED_STATE_UNAVAILABLE", "共享配额状态不可用，请稍后重试"
            ) from exc
        return reservation

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise GatewayError.of("INVALID_REQUEST", "结算 token 不能为负数")
        with self._lock:
            if reservation.settled:
                return
            with self._db.tx() as conn:
                conn.execute(
                    "INSERT INTO quota_counters"
                    "(period, tenant, dept, sub, used_tokens, updated_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(period, tenant, dept, sub) DO UPDATE SET "
                    "used_tokens = quota_counters.used_tokens + excluded.used_tokens, "
                    "updated_at_ms = excluded.updated_at_ms",
                    (
                        reservation.period,
                        reservation.tenant,
                        reservation.dept,
                        reservation.sub,
                        actual_tokens,
                        now_ms(),
                    ),
                )
                if reservation.api_key_id:
                    conn.execute(
                        "INSERT INTO api_key_counters"
                        "(period, key_id, used_tokens, updated_at_ms) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(period, key_id) DO UPDATE SET "
                        "used_tokens = api_key_counters.used_tokens + excluded.used_tokens, "
                        "updated_at_ms = excluded.updated_at_ms",
                        (
                            reservation.period,
                            reservation.api_key_id,
                            actual_tokens,
                            now_ms(),
                        ),
                    )
            if self._shared_enabled:
                reservation.settled = True
                try:
                    self._shared.release(
                        reservation_id=reservation.reservation_id,
                        principal_key=reservation.principal_key,
                        tenant=reservation.tenant,
                        dept=reservation.dept,
                        sub=reservation.sub,
                        period=reservation.period,
                        tokens=reservation.tokens,
                        api_key_id=reservation.api_key_id,
                        key_tokens=reservation.api_key_tokens,
                    )
                except SharedStateError:
                    # 账本已落库；预留等 TTL 到期自动回收，不能因此丢响应。
                    logger.warning(
                        "共享配额预留释放失败，等待 TTL 回收 request=%s",
                        reservation.reservation_id,
                    )
                return
            counter_key = (reservation.principal_key, reservation.period)
            remaining = self._reserved.get(counter_key, 0) - reservation.tokens
            if remaining > 0:
                self._reserved[counter_key] = remaining
            else:
                self._reserved.pop(counter_key, None)
            if reservation.api_key_id and reservation.api_key_tokens:
                key_counter = (reservation.api_key_id, reservation.period)
                key_remaining = (
                    self._key_reserved.get(key_counter, 0)
                    - reservation.api_key_tokens
                )
                if key_remaining > 0:
                    self._key_reserved[key_counter] = key_remaining
                else:
                    self._key_reserved.pop(key_counter, None)
            active = self._active.get(reservation.principal_key, 0) - 1
            if active > 0:
                self._active[reservation.principal_key] = active
            else:
                self._active.pop(reservation.principal_key, None)
            reservation.settled = True

    def release(self, reservation: Reservation) -> None:
        self.settle(reservation, 0)

    def remaining(self, principal_key: str, limits: dict) -> int | None:
        monthly = limits.get("monthlyTokens")
        if not isinstance(monthly, int):
            return None
        tenant, dept, sub = _split_principal(principal_key)
        if self._shared_enabled:
            period = _period(self._clock())
            row = self._db.query(
                "SELECT used_tokens FROM quota_counters "
                "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                (period, tenant, dept, sub),
            )
            used = int(row[0]["used_tokens"]) if row else 0
            used += self._shared.reserved(tenant, dept, sub, period)
            return max(0, monthly - used)
        with self._lock:
            period = _period(self._clock())
            row = self._db.query(
                "SELECT used_tokens FROM quota_counters "
                "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                (period, tenant, dept, sub),
            )
            used = int(row[0]["used_tokens"]) if row else 0
            used += self._reserved.get((principal_key, period), 0)
            return max(0, monthly - used)

    # ── 按月额度覆盖（M1.4 T4.4） ────────────────────────────

    def _override_monthly(
        self, period: str, tenant: str, dept: str, sub: str
    ) -> int | None:
        def lookup(key: str) -> int | None:
            rows = self._db.query(
                "SELECT monthly_tokens FROM quota_overrides "
                "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                (period, tenant, dept, key),
            )
            return int(rows[0]["monthly_tokens"]) if rows else None

        value = lookup(sub) if sub else None
        if value is None:
            # 个人覆盖优先；没有个人覆盖就落到部门级（sub=''）
            value = lookup("")
        return value

    def effective_limits(
        self, limits: dict, tenant: str, dept: str, sub: str
    ) -> dict:
        """策略额度 + 当月覆盖；覆盖只影响当月，不改策略文档。"""
        monthly = self._override_monthly(_period(self._clock()), tenant, dept, sub)
        if monthly is None:
            return limits
        merged = dict(limits)
        merged["monthlyTokens"] = monthly
        return merged

    def set_monthly_override(
        self,
        tenant: str,
        dept: str,
        sub: str,
        monthly: int | None,
        *,
        actor: str,
        actor_role: str = "admin",
        period: str | None = None,
    ) -> dict:
        """设置/清除当月额度覆盖并写审计；`monthly=None` 表示恢复策略值。"""
        period = period or _period(self._clock())
        with self._lock:
            with self._db.tx() as conn:
                row = conn.execute(
                    "SELECT monthly_tokens FROM quota_overrides "
                    "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                    (period, tenant, dept, sub),
                ).fetchone()
                before = int(row["monthly_tokens"]) if row else None
                if monthly is None:
                    conn.execute(
                        "DELETE FROM quota_overrides "
                        "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                        (period, tenant, dept, sub),
                    )
                else:
                    conn.execute(
                        "INSERT INTO quota_overrides "
                        "(period, tenant, dept, sub, monthly_tokens, updated_at_ms, updated_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(period, tenant, dept, sub) DO UPDATE SET "
                        "monthly_tokens = excluded.monthly_tokens, "
                        "updated_at_ms = excluded.updated_at_ms, "
                        "updated_by = excluded.updated_by",
                        (period, tenant, dept, sub, monthly, now_ms(), actor),
                    )
                append_audit(
                    conn,
                    actor=actor,
                    actor_role=actor_role,
                    action="quota.update",
                    object_id=f"{tenant}:{dept}:{sub or '*'}",
                    before={"monthlyTokens": before},
                    after={"monthlyTokens": monthly},
                )
        return {"period": period, "beforeMonthlyTokens": before, "monthlyTokens": monthly}

    def overrides(self, period: str | None = None) -> list[dict]:
        target = period or _period(self._clock())
        rows = self._db.query(
            "SELECT period, tenant, dept, sub, monthly_tokens, updated_at_ms, updated_by "
            "FROM quota_overrides WHERE period=? "
            "ORDER BY tenant, dept, sub",
            (target,),
        )
        return [
            {
                "principal": f"{row['tenant']}:{row['dept']}:{row['sub'] or '*'}",
                "period": row["period"],
                "monthlyTokens": int(row["monthly_tokens"]),
                "updatedAtMs": int(row["updated_at_ms"]),
                "updatedBy": row["updated_by"],
            }
            for row in rows
        ]

    def used_tokens(
        self, tenant: str, dept: str, sub: str, period: str | None = None
    ) -> dict:
        """已结算用量；sub 为空时按部门汇总（含在途预留另行披露）。"""
        target = period or _period(self._clock())
        if sub:
            rows = self._db.query(
                "SELECT used_tokens FROM quota_counters "
                "WHERE period=? AND tenant=? AND dept=? AND sub=?",
                (target, tenant, dept, sub),
            )
            used = int(rows[0]["used_tokens"]) if rows else 0
            return {"users": 1, "committedTokens": used}
        rows = self._db.query(
            "SELECT COALESCE(SUM(used_tokens), 0) AS used, COUNT(DISTINCT sub) AS users "
            "FROM quota_counters WHERE period=? AND tenant=? AND dept=?",
            (target, tenant, dept),
        )
        row = rows[0]
        return {"users": int(row["users"]), "committedTokens": int(row["used"])}

    def snapshot(self) -> list[dict]:
        with self._lock:
            rows = []
            seen: set[tuple[str, str, str, str]] = set()
            for row in self._db.query(
                "SELECT period, tenant, dept, sub, used_tokens FROM quota_counters "
                "ORDER BY period, tenant, dept, sub"
            ):
                key = (row["period"], row["tenant"], row["dept"], row["sub"])
                seen.add(key)
                principal_key = f"{row['tenant']}:{row['dept']}:{row['sub']}"
                rows.append(
                    {
                        "principal": principal_key,
                        "period": row["period"],
                        "committedTokens": int(row["used_tokens"]),
                        "reservedTokens": self._reserved.get(
                            (principal_key, row["period"]), 0
                        ),
                        "activeRequests": self._active.get(principal_key, 0),
                    }
                )
            for (principal_key, period), reserved in self._reserved.items():
                tenant, dept, sub = _split_principal(principal_key)
                key = (period, tenant, dept, sub)
                if key in seen:
                    continue
                rows.append(
                    {
                        "principal": principal_key,
                        "period": period,
                        "committedTokens": 0,
                        "reservedTokens": reserved,
                        "activeRequests": self._active.get(principal_key, 0),
                    }
                )
            if self._shared_enabled:
                try:
                    shared_rows = self._shared.reservation_rows(
                        _period(self._clock())
                    )
                except SharedStateError:
                    shared_rows = []
                merged = {(row["principal"], row["period"]): row for row in rows}
                for item in shared_rows:
                    key = (item["principal"], item["period"])
                    existing = merged.get(key)
                    if existing is None:
                        rows.append(item)
                        merged[key] = item
                    else:
                        existing["reservedTokens"] = item["reservedTokens"]
                        existing["activeRequests"] = item["activeRequests"]
            return rows
