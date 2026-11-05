"""配额：单进程内存计数器，预留/结算语义与 M1 的 SQLite 版一致。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .errors import GatewayError


@dataclass
class Reservation:
    reservation_id: str
    principal_key: str
    period: str
    tokens: int
    settled: bool = False


def _period(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")


def current_period(now: float | None = None) -> str:
    return _period(now if now is not None else time.time())


def reset_at(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    if moment.month == 12:
        target = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        target = datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)
    return target.isoformat().replace("+00:00", "Z")


class QuotaTracker:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._committed: dict[tuple[str, str], int] = {}
        self._reserved: dict[tuple[str, str], int] = {}
        self._active: dict[str, int] = {}

    def reserve(self, principal_key: str, limits: dict, tokens: int) -> Reservation:
        if tokens < 0:
            raise GatewayError.of("INVALID_REQUEST", "预留 token 不能为负数")
        with self._lock:
            now = self._clock()
            period = _period(now)
            counter_key = (principal_key, period)
            monthly = limits.get("monthlyTokens")
            used = self._committed.get(counter_key, 0) + self._reserved.get(
                counter_key, 0
            )
            if isinstance(monthly, int) and used + tokens > monthly:
                raise GatewayError.of(
                    "QUOTA_TOKENS_EXCEEDED",
                    f"当月 token 配额不足（已用 {used}，预留 {tokens}，上限 {monthly}）",
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
            self._active[principal_key] = active + 1
            return Reservation(
                reservation_id=uuid.uuid4().hex,
                principal_key=principal_key,
                period=period,
                tokens=tokens,
            )

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise GatewayError.of("INVALID_REQUEST", "结算 token 不能为负数")
        with self._lock:
            if reservation.settled:
                return
            counter_key = (reservation.principal_key, reservation.period)
            remaining = self._reserved.get(counter_key, 0) - reservation.tokens
            if remaining > 0:
                self._reserved[counter_key] = remaining
            else:
                self._reserved.pop(counter_key, None)
            self._committed[counter_key] = (
                self._committed.get(counter_key, 0) + actual_tokens
            )
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
        with self._lock:
            counter_key = (principal_key, _period(self._clock()))
            used = self._committed.get(counter_key, 0) + self._reserved.get(
                counter_key, 0
            )
            return max(0, monthly - used)

    def snapshot(self) -> list[dict]:
        with self._lock:
            keys = set(self._committed) | set(self._reserved)
            rows = []
            for principal_key, period in sorted(keys):
                rows.append(
                    {
                        "principal": principal_key,
                        "period": period,
                        "committedTokens": self._committed.get(
                            (principal_key, period), 0
                        ),
                        "reservedTokens": self._reserved.get(
                            (principal_key, period), 0
                        ),
                        "activeRequests": self._active.get(principal_key, 0),
                    }
                )
            return rows
