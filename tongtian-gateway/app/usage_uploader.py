"""用量批量上报器（M1.4 T4.1–T4.2）。

后台线程按「200 条或 5 秒」把 `usage_events` 中未上报的事件推到控制面：
`POST {controlPlaneUrl}/api/v1/gateway/usage`，服务令牌鉴权，事件级
`(gatewayId, requestId)` 幂等。控制面不可达时事件继续留在本地账本，
指数退避后重试，队列保留 30 天（由账本自身保留，不做删除）。

失败分两类：
- 可重试（网络错误、401/403/404/405、429、5xx）：保留事件，退避重试。
  401/403 通常是服务令牌没配好，404/405 是控制面还没升级到 M1.4，
  都值得等运维修好，不能把账丢在本地当成功。
- 确定性拒绝（400/409/413/415/422）：同一份负载重试一万次也不会过，
  标记已上报让队列继续前进，并把错误留在健康页，避免一批坏数据堵死整条队列。
  原始事件仍在 `usage_events` 账本里，可按 requestId 追溯。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

from . import CONTRACT_VERSION
from .config import Settings
from .db import now_ms
from .usage_store import UsageStore

logger = logging.getLogger("tongtian.gateway.usage_uploader")

POISON_STATUS = frozenset({400, 409, 413, 415, 422})
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0

Transport = Callable[[dict, str, str], tuple[int, str]]


def urllib_transport(payload: dict, endpoint: str, token: str) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class UsageUploader:
    def __init__(
        self,
        settings: Settings,
        store: UsageStore,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._transport = transport or urllib_transport
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._batch_ids: tuple[str, ...] = ()
        self._batch_id = ""
        self._next_attempt_at = 0.0
        self.last_success_at_ms = 0
        self.last_error = ""
        self.consecutive_failures = 0
        self.uploaded_total = 0

    @property
    def enabled(self) -> bool:
        return bool(self._settings.control_plane_url and self._settings.service_token)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="usage-uploader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "pending": self._store.count_pending(),
            "lastSuccessAtMs": self.last_success_at_ms or None,
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
            "backoffSeconds": round(self._backoff(), 1),
        }

    def flush_once(self) -> int:
        """上传一批；返回本次被标记已上报的条数，异常都在内部消化。"""
        events = self._store.pending(self._settings.usage_batch_size)
        if not events:
            self._batch_ids = ()
            self._batch_id = ""
            self._reset_backoff()
            return 0

        ids = tuple(item.requestId for item in events)
        if ids != self._batch_ids:
            self._batch_ids = ids
            self._batch_id = f"batch_{uuid.uuid4().hex}"
        payload = {
            "contractVersion": CONTRACT_VERSION,
            "gatewayId": self._settings.gateway_id,
            "batchId": self._batch_id,
            "events": [item.model_dump(mode="json") for item in events],
        }
        endpoint = f"{self._settings.control_plane_url}/api/v1/gateway/usage"
        try:
            status, text = self._transport(payload, endpoint, self._settings.service_token)
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都按可重试处理
            self._fail(f"网络错误：{exc}")
            return 0

        if 200 <= status < 300:
            self._store.mark_uploaded(list(ids), now_ms())
            self.uploaded_total += len(ids)
            self.last_success_at_ms = now_ms()
            self.last_error = ""
            self._reset_backoff()
            logger.info("用量已上报：%d 条 batch=%s", len(ids), self._batch_id)
            return len(ids)

        if status in POISON_STATUS:
            # 确定性拒绝：跳过这一批，账本里仍可按 requestId 追溯
            self._store.mark_uploaded(list(ids), now_ms())
            self.last_error = f"HTTP {status}：{text[:300]}"
            logger.error(
                "用量批次被控制面拒绝，跳过 %d 条：HTTP %s %s",
                len(ids),
                status,
                text[:300],
            )
            return len(ids)

        self._fail(f"HTTP {status}：{text[:300]}")
        return 0

    def _fail(self, message: str) -> None:
        self.consecutive_failures += 1
        self.last_error = message
        delay = self._backoff()
        self._next_attempt_at = time.monotonic() + delay
        logger.warning("用量上报失败（第 %d 次，%.0fs 后重试）：%s",
                       self.consecutive_failures, delay, message)

    def _backoff(self) -> float:
        if self.consecutive_failures == 0:
            return 0.0
        exponent = min(self.consecutive_failures - 1, 16)
        return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2**exponent))

    def _reset_backoff(self) -> None:
        self.consecutive_failures = 0
        self._next_attempt_at = 0.0

    def _run(self) -> None:
        logger.info(
            "用量上报器已启动：%s（%d 条/批，%d 秒间隔）",
            self._settings.control_plane_url,
            self._settings.usage_batch_size,
            self._settings.usage_flush_seconds,
        )
        while not self._stop.is_set():
            now = time.monotonic()
            if now < self._next_attempt_at:
                self._stop.wait(min(5.0, self._next_attempt_at - now))
                continue
            self.flush_once()
            if self._stop.is_set():
                break
            self._stop.wait(max(0.2, float(self._settings.usage_flush_seconds)))
        logger.info("用量上报器已停止")
