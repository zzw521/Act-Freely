"""per-key 限流：默认进程内令牌桶，配置 Redis 时使用共享令牌桶（M5）。

RPM 按 Key 独立计算，桶容量 `max(1, min(rpm, 60))`；共享模式下多 worker
共用同一个桶，Redis 不可用时失败关闭。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import GatewayError
from .shared_state import SharedStateError


class ApiKeyRateLimiter:
    CLEANUP_SIZE = 4096
    IDLE_SECONDS = 600.0

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, shared=None
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self.shared = shared

    def consume(self, key_id: str, rpm: int) -> float:
        """消耗一个令牌；返回 0 表示放行，否则为建议的 Retry-After 秒数。"""
        if rpm <= 0 or not key_id:
            return 0.0
        capacity = float(max(1, min(rpm, 60)))
        if self.shared is not None and self.shared.enabled:
            try:
                return self.shared.consume(
                    "key", key_id, per_minute=rpm, burst=int(capacity)
                )
            except SharedStateError as exc:
                raise GatewayError.of(
                    "SHARED_STATE_UNAVAILABLE", "API Key 限流状态不可用，请稍后重试"
                ) from exc
        rate = rpm / 60.0
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key_id, (capacity, now))
            tokens = min(capacity, tokens + (now - updated) * rate)
            if tokens >= 1.0:
                self._buckets[key_id] = (tokens - 1.0, now)
                retry_after = 0.0
            else:
                self._buckets[key_id] = (tokens, now)
                retry_after = (1.0 - tokens) / rate
            if len(self._buckets) > self.CLEANUP_SIZE:
                self._prune(now)
            return retry_after

    def _prune(self, now: float) -> None:
        idle = now - self.IDLE_SECONDS
        self._buckets = {
            key: value for key, value in self._buckets.items() if value[1] >= idle
        }

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


api_key_limiter = ApiKeyRateLimiter()
