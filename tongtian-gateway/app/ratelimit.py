"""管理面限流：默认进程内令牌桶，配置 Redis 时使用共享令牌桶（M5）。

单 worker 未配 Redis 的行为与 M2 完全一致；共享模式下列桶由 Redis 统一维护，
Redis 不可用时失败关闭（`503 SHARED_STATE_UNAVAILABLE`）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .config import settings
from .errors import GatewayError
from .shared_state import SharedStateError


class AdminRateLimiter:
    CLEANUP_SIZE = 4096
    IDLE_SECONDS = 600.0

    def __init__(
        self,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        shared=None,
    ) -> None:
        self.per_minute = max(0, per_minute)
        self.burst = max(0, burst)
        self._rate = self.per_minute / 60.0
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self.shared = shared

    @property
    def enabled(self) -> bool:
        return self._rate > 0 and self.burst > 0

    def consume(self, key: str) -> float:
        """消耗一个令牌；返回 0 表示放行，否则为建议的 Retry-After 秒数。"""
        if not self.enabled:
            return 0.0
        if self.shared is not None and self.shared.enabled:
            try:
                return self.shared.consume(
                    "admin", key, per_minute=self.per_minute, burst=self.burst
                )
            except SharedStateError as exc:
                raise GatewayError.of(
                    "SHARED_STATE_UNAVAILABLE", "管理面限流状态不可用，请稍后重试"
                ) from exc
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - updated) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                retry_after = 0.0
            else:
                self._buckets[key] = (tokens, now)
                retry_after = (1.0 - tokens) / self._rate
            if len(self._buckets) > self.CLEANUP_SIZE:
                self._prune(now)
            return retry_after

    def _prune(self, now: float) -> None:
        idle = now - self.IDLE_SECONDS
        self._buckets = {
            key: value
            for key, value in self._buckets.items()
            if value[1] >= idle and value[0] < self.burst
        }

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


admin_limiter = AdminRateLimiter(
    settings.admin_rate_limit_per_minute, settings.admin_rate_limit_burst
)
