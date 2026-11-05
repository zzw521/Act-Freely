"""用量事件缓冲：本地去重，批量交给控制面。"""

from __future__ import annotations

import threading
from collections import deque

from .contract import UsageEvent


class UsageBuffer:
    def __init__(self, batch_size: int = 200, max_events: int = 100000) -> None:
        self._batch_size = max(1, batch_size)
        self._max_events = max(self._batch_size, max_events)
        self._events: deque[UsageEvent] = deque()
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _key(event: UsageEvent) -> tuple[str, str]:
        return (event.gatewayId, event.requestId)

    def append(self, event: UsageEvent) -> bool:
        key = self._key(event)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._events.append(event)
            while len(self._events) > self._max_events:
                dropped = self._events.popleft()
                self._seen.discard(self._key(dropped))
            return True

    def ready(self) -> bool:
        with self._lock:
            return len(self._events) >= self._batch_size

    def flush(self) -> list[dict]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
            for event in events:
                self._seen.discard(self._key(event))
            return [event.model_dump() for event in events]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
