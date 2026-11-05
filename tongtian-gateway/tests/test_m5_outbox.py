"""M5.5：多 worker 审计外送租约——同一批事件不会被两个 worker 同时投递。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit_outbox import AuditWebhook, enqueue  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Database, now_ms  # noqa: E402
from tests.support import check, report  # noqa: E402


class Receiver:
    def __init__(self, delay: float = 0.3) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                time.sleep(delay)
                with receiver.lock:
                    receiver.events.extend(payload.get("events", []))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:  # noqa: D401
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/audit"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _seed(db: Database, ids: list[int]) -> None:
    with db.tx() as conn:
        for value in ids:
            enqueue(
                conn,
                {
                    "id": value,
                    "atMs": now_ms(),
                    "actor": "tester",
                    "actorRole": "admin",
                    "action": "test",
                    "object": str(value),
                    "before": {},
                    "after": {},
                },
            )


def _settings(url: str, batch_size: int = 20):
    return replace(
        settings,
        audit_webhook_url=url,
        audit_webhook_secret="outbox-test-secret-123456",
        audit_webhook_batch_size=batch_size,
        audit_webhook_interval_seconds=300,
        audit_webhook_timeout_seconds=10,
    )


def test_single_delivery(tmp: Path) -> None:
    receiver = Receiver(delay=0.4)
    try:
        path = tmp / "outbox-race.db"
        db_a = Database(path).ensure()
        db_b = Database(path).ensure()
        _seed(db_a, list(range(1, 13)))
        config = _settings(receiver.url, batch_size=12)
        worker_a = AuditWebhook(config, db_a)
        worker_b = AuditWebhook(config, db_b)
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def run(worker: AuditWebhook) -> None:
            barrier.wait()
            results.append(worker.flush())

        threads = [
            threading.Thread(target=run, args=(worker_a,)),
            threading.Thread(target=run, args=(worker_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        with receiver.lock:
            received = list(receiver.events)
        check(
            "并发 flush 只投递一次",
            len(received) == 12 and len({event["id"] for event in received}) == 12,
            f"received={len(received)} ids={sorted(e['id'] for e in received)}",
        )
        only_one_active = sum(1 for value in results if value) <= 1
        check("同一批只被一个 worker 抢到", only_one_active, str(results))
        delivered = db_a.query(
            "SELECT COUNT(*) AS n FROM audit_outbox WHERE delivered_at_ms > 0"
        )[0]["n"]
        check("outbox 全部标记已投递", int(delivered) == 12, str(delivered))
        leases = db_a.query(
            "SELECT COUNT(*) AS n FROM audit_outbox WHERE lease_owner <> ''"
        )[0]["n"]
        check("投递后清空租约", int(leases) == 0, str(leases))
        db_b.close()
        db_a.close()
    finally:
        receiver.stop()


def test_lease_expiry(tmp: Path) -> None:
    receiver = Receiver(delay=0.0)
    try:
        path = tmp / "outbox-lease.db"
        db = Database(path).ensure()
        _seed(db, [101])
        config = _settings(receiver.url, batch_size=10)
        worker = AuditWebhook(config, db)

        with db.tx() as conn:
            conn.execute(
                "UPDATE audit_outbox SET lease_owner='dead-worker', leased_until_ms=? "
                "WHERE audit_id=101",
                (now_ms() + 60_000,),
            )
        check("有效租约内不重复投递", worker.flush() is False)
        with receiver.lock:
            check("有效租约内没有外送", len(receiver.events) == 0)

        with db.tx() as conn:
            conn.execute(
                "UPDATE audit_outbox SET leased_until_ms=? WHERE audit_id=101",
                (now_ms() - 1,),
            )
        worker.flush()
        deadline = time.time() + 5
        while time.time() < deadline:
            with receiver.lock:
                if receiver.events:
                    break
            time.sleep(0.05)
        with receiver.lock:
            check("过期租约后完成投递", [e["id"] for e in receiver.events] == [101])
        db.close()
    finally:
        receiver.stop()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tt-m5-outbox-"))
    test_single_delivery(tmp)
    test_lease_expiry(tmp)
    return report("M5 审计 outbox 租约自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
