"""审计外送：outbox 落库 + 后台批量推送（HMAC-SHA256 签名）。

审计写入与 outbox 入队在同一事务内完成，网关崩溃不会丢事件；投递失败按指数退避
重试，4xx（除 408/429）视为接收端确定性拒绝，记错误后跳过，避免毒批次堵队列。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import Settings
from .db import Connection, Database, Row, now_ms

logger = logging.getLogger("tongtian.gateway.audit")

MAX_BACKOFF_SECONDS = 300
MAX_ERROR_CHARS = 300
RETRYABLE_STATUS = frozenset({408, 429})


def enqueue(conn: Connection, entry: dict) -> None:
    """把已落库的审计条目放进 outbox；由审计写入事务调用。"""
    payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    when = now_ms()
    conn.execute(
        "INSERT INTO audit_outbox"
        "(audit_id, payload_json, created_at_ms, next_attempt_at_ms) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(audit_id) DO NOTHING",
        (int(entry["id"]), payload, when, when),
    )


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    material = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


class AuditWebhook:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._db = db
        self._url = settings.audit_webhook_url
        self._secret = settings.audit_webhook_secret
        self._gateway_id = settings.gateway_id
        self._interval = settings.audit_webhook_interval_seconds
        self._batch_size = settings.audit_webhook_batch_size
        self._timeout = settings.audit_webhook_timeout_seconds
        self._enabled = bool(self._url)
        self._owner = f"{self._gateway_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        # 租约要覆盖一次 HTTP 超时；默认 10s 超时对应 60s 租约。
        self._lease_ms = max(60_000, self._timeout * 2000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_error = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="audit-webhook", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        rows = self._db.query(
            "SELECT COUNT(*) AS n, MAX(delivered_at_ms) AS last FROM audit_outbox "
            "WHERE delivered_at_ms = 0"
        )
        pending = int(rows[0]["n"]) if rows else 0
        delivered = self._db.query(
            "SELECT MAX(delivered_at_ms) AS last FROM audit_outbox "
            "WHERE delivered_at_ms > 0"
        )
        last_delivered = int(delivered[0]["last"] or 0) if delivered else 0
        with self._lock:
            failures = self._consecutive_failures
            error = self._last_error
        return {
            "enabled": self._enabled,
            "pending": pending,
            "consecutiveFailures": failures,
            "lastError": error,
            "lastDeliveredAt": last_delivered or None,
        }

    # ---- 推送 ----

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                while self.flush():
                    if self._stop.is_set():
                        break
            except Exception as exc:  # noqa: BLE001 - 后台线程不能因单次异常退出
                logger.warning("审计外送循环异常：%s", exc)

    def _claim(self) -> list[Row]:
        """抢占到期且未租出的行；崩溃后租约自动过期。

        Postgres 用 `FOR UPDATE SKIP LOCKED` 让多主机各取各的批次；SQLite 由
        `BEGIN IMMEDIATE` 串行化。两种后端都保留租约列，防止慢投递期间被重复抢占。
        """
        due = now_ms()
        lease_until = due + self._lease_ms
        skip_locked = " FOR UPDATE SKIP LOCKED" if self._db.dialect == "postgres" else ""
        with self._db.tx() as conn:
            candidates = conn.execute(
                "SELECT id FROM audit_outbox WHERE delivered_at_ms = 0 "
                "AND next_attempt_at_ms <= ? "
                "AND (lease_owner = '' OR leased_until_ms < ?) "
                "ORDER BY id ASC LIMIT ?" + skip_locked,
                (due, due, self._batch_size),
            ).fetchall()
            if not candidates:
                return []
            ids = [int(row["id"]) for row in candidates]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE audit_outbox SET lease_owner=?, leased_until_ms=? "
                f"WHERE id IN ({placeholders})",
                (self._owner, lease_until, *ids),
            )
            rows = conn.execute(
                f"SELECT * FROM audit_outbox WHERE id IN ({placeholders}) "
                "AND lease_owner=? ORDER BY id ASC",
                (*ids, self._owner),
            ).fetchall()
            return list(rows)

    def flush(self) -> bool:
        """抢占一批到期事件并推送；返回 True 表示可能还有积压。"""
        if not self._enabled:
            return False
        rows = self._claim()
        if not rows:
            return False
        events = [json.loads(row["payload_json"]) for row in rows]
        body = json.dumps(
            {
                "gatewayId": self._gateway_id,
                "sentAtMs": now_ms(),
                "events": events,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"tongtian-gateway/{self._gateway_id}",
                "X-TT-Gateway-Id": self._gateway_id,
                "X-TT-Audit-Timestamp": timestamp,
                "X-TT-Audit-Signature": f"sha256={_sign(self._secret, timestamp, body)}",
            },
        )
        ids = [int(row["id"]) for row in rows]
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            detail = f"HTTP {status}"
        except (urllib.error.URLError, OSError) as exc:
            status = 0
            detail = str(getattr(exc, "reason", exc))
        else:
            detail = ""

        if 200 <= status < 300 or (400 <= status < 500 and status not in RETRYABLE_STATUS):
            rejected = not (200 <= status < 300)
            error = f"rejected: HTTP {status}" if rejected else ""
            self._finish(ids, status, error)
            with self._lock:
                self._consecutive_failures = 0
                self._last_error = error
            if rejected:
                logger.warning("审计外送被接收端拒绝（HTTP %s），已跳过该批 %s 条", status, len(ids))
        else:
            self._retry(rows, status, detail)
        return len(rows) == self._batch_size

    def _finish(self, ids: list[int], status: int, error: str) -> None:
        with self._db.tx() as conn:
            conn.executemany(
                "UPDATE audit_outbox SET delivered_at_ms=?, attempts=attempts+1, "
                "last_status=?, last_error=?, next_attempt_at_ms=0, "
                "lease_owner='', leased_until_ms=0 WHERE id=?",
                [(now_ms(), status, error[:MAX_ERROR_CHARS], row_id) for row_id in ids],
            )

    def _retry(self, rows: list[Row], status: int, detail: str) -> None:
        when = now_ms()
        updates = []
        for row in rows:
            attempts = int(row["attempts"]) + 1
            delay = min(MAX_BACKOFF_SECONDS, 2 ** min(attempts, 8))
            updates.append(
                (
                    attempts,
                    when + delay * 1000,
                    status,
                    detail[:MAX_ERROR_CHARS],
                    int(row["id"]),
                )
            )
        with self._db.tx() as conn:
            conn.executemany(
                "UPDATE audit_outbox SET attempts=?, next_attempt_at_ms=?, "
                "last_status=?, last_error=?, lease_owner='', leased_until_ms=0 "
                "WHERE id=?",
                updates,
            )
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = (detail or f"HTTP {status}")[:MAX_ERROR_CHARS]
        logger.warning(
            "审计外送失败（HTTP %s）：%s，%s 秒后重试",
            status,
            self._last_error,
            updates[0][1] // 1000 - when // 1000,
        )
