"""TPM 限流：按分钟补充的 token 桶（主体级 + API Key 级）。

成本 = 输入估算 + 本次输出上限；结算后把未用部分退回桶。单 worker 无 Redis
时进程内计数；多 worker/跨主机走 Redis 共享桶，Redis 故障 fail-closed。
0/缺失表示不限；单请求成本超过桶容量直接拒绝（该请求本身超出分钟上限）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .errors import GatewayError
from .shared_state import SharedStateError

logger = logging.getLogger("tongtian.gateway.tpm")

MAX_BUCKET_TOKENS = 60_000


def bucket_capacity(tpm: int) -> int:
    return max(1, min(int(tpm), MAX_BUCKET_TOKENS))


def bucket_rate(tpm: int) -> float:
    return max(0.0, int(tpm) / 60.0)


@dataclass(frozen=True)
class TpmBucket:
    scope: str
    key: str
    tpm: int

    @property
    def name(self) -> str:
        return f"{self.scope}:{self.key}"

    @property
    def capacity(self) -> int:
        return bucket_capacity(self.tpm)

    @property
    def rate(self) -> float:
        return bucket_rate(self.tpm)


@dataclass
class TpmReservation:
    buckets: tuple[TpmBucket, ...] = ()
    cost: int = 0
    settled: bool = False


class TpmLimiter:
    CLEANUP_SIZE = 4096
    IDLE_SECONDS = 600.0

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        shared=None,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self.shared = shared

    @property
    def _shared_enabled(self) -> bool:
        return self.shared is not None and self.shared.enabled

    def reserve(self, buckets, cost: int) -> TpmReservation:
        """从所有生效的桶扣 `cost`；任一不足抛 429（附 Retry-After）。"""
        active = tuple(bucket for bucket in buckets if bucket.tpm > 0 and bucket.key)
        cost = max(0, int(cost))
        if not active or cost <= 0:
            return TpmReservation()
        for bucket in active:
            if cost > bucket.capacity:
                raise GatewayError.of(
                    "RATE_LIMITED",
                    f"单次请求需要 {cost} tokens，超过每分钟 {bucket.tpm} TPM "
                    f"对应的桶容量 {bucket.capacity}",
                    headers={"Retry-After": "60"},
                )
        if self._shared_enabled:
            try:
                retry_after = self.shared.tpm_consume(
                    [
                        (bucket.scope, bucket.key, bucket.capacity, bucket.rate)
                        for bucket in active
                    ],
                    cost,
                )
            except SharedStateError as exc:
                raise GatewayError.of(
                    "SHARED_STATE_UNAVAILABLE", "共享 TPM 状态不可用，请稍后重试"
                ) from exc
        else:
            retry_after = self._consume_local(active, cost)
        if retry_after > 0:
            cap = min(bucket.tpm for bucket in active)
            raise GatewayError.of(
                "RATE_LIMITED",
                f"每分钟 token 限额不足（本次预留 {cost}，上限 {cap} TPM）",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        return TpmReservation(buckets=active, cost=cost)

    def refund(self, reservation: TpmReservation | None, actual_tokens: int) -> None:
        """结算后把 `cost - actual` 退回桶；实际超出预留只记日志。"""
        if reservation is None or reservation.settled or not reservation.buckets:
            return
        reservation.settled = True
        actual = max(0, int(actual_tokens))
        if actual >= reservation.cost:
            if actual > reservation.cost:
                logger.info(
                    "实际用量超过 TPM 预留 cost=%s actual=%s",
                    reservation.cost,
                    actual,
                )
            return
        amount = reservation.cost - actual
        if self._shared_enabled:
            try:
                self.shared.tpm_refund(
                    [
                        (bucket.scope, bucket.key, bucket.capacity, bucket.rate)
                        for bucket in reservation.buckets
                    ],
                    amount,
                )
            except SharedStateError:
                # 分钟窗口会自然补充，退回失败不影响已确定的响应。
                logger.warning("共享 TPM 退回失败，等待分钟窗口补充")
            return
        self._refund_local(reservation.buckets, amount)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    # ---- 进程内 ----

    def _consume_local(self, buckets: tuple[TpmBucket, ...], cost: int) -> float:
        now = self._clock()
        with self._lock:
            states: dict[str, tuple[float, TpmBucket]] = {}
            retry_after = 0.0
            for bucket in buckets:
                tokens, updated = self._buckets.get(
                    bucket.name, (float(bucket.capacity), now)
                )
                tokens = min(
                    float(bucket.capacity),
                    tokens + (now - updated) * bucket.rate,
                )
                states[bucket.name] = (tokens, bucket)
                if tokens < cost:
                    retry_after = max(
                        retry_after, (cost - tokens) / bucket.rate
                    )
            for name, (tokens, _) in states.items():
                remaining = tokens if retry_after > 0 else max(0.0, tokens - cost)
                self._buckets[name] = (remaining, now)
            if len(self._buckets) > self.CLEANUP_SIZE:
                self._prune(now)
            return retry_after

    def _refund_local(self, buckets: tuple[TpmBucket, ...], amount: int) -> None:
        now = self._clock()
        with self._lock:
            for bucket in buckets:
                entry = self._buckets.get(bucket.name)
                if entry is None:
                    continue
                tokens, updated = entry
                tokens = min(
                    float(bucket.capacity),
                    tokens + (now - updated) * bucket.rate,
                )
                tokens = min(float(bucket.capacity), tokens + amount)
                self._buckets[bucket.name] = (tokens, now)

    def _prune(self, now: float) -> None:
        idle = now - self.IDLE_SECONDS
        self._buckets = {
            name: value for name, value in self._buckets.items() if value[1] >= idle
        }
