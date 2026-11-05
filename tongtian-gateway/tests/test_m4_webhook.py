"""M4.4 自测：审计外送 outbox、HMAC 签名、失败重试与 4xx 跳过。

用法: python tests/test_m4_webhook.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


HOOK_PORT = free_port()
HOOK_SECRET = "webhook-test-secret-0123456789"
os.environ["TT_GATEWAY_AUDIT_WEBHOOK_URL"] = f"http://127.0.0.1:{HOOK_PORT}/audit"
os.environ["TT_GATEWAY_AUDIT_WEBHOOK_SECRET"] = HOOK_SECRET
os.environ["TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS"] = "1"

from tests import support  # noqa: E402
from tests.support import admin_headers, check, http, report, start_gateway, stop_gateway  # noqa: E402

from app.state import audit_webhook, db  # noqa: E402


class Receiver(BaseHTTPRequestHandler):
    requests: list[dict] = []
    mode = "ok"
    server_version = "AuditReceiver/1.0"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        timestamp = self.headers.get("x-tt-audit-timestamp", "")
        signature = self.headers.get("x-tt-audit-signature", "")
        expected = "sha256=" + hmac.new(
            HOOK_SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        valid = bool(timestamp) and hmac.compare_digest(signature, expected)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.__class__.requests.append(
            {"valid": valid, "timestamp": timestamp, "body": body}
        )
        if self.mode == "fail":
            status = 500
        elif self.mode == "reject":
            status = 400
        else:
            status = 200
        payload = json.dumps({"ok": status == 200}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


_receiver = ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), Receiver)
threading.Thread(target=_receiver.serve_forever, daemon=True).start()


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def health() -> dict:
    status, _, body = http("GET", "/gateway/v1/admin/health", headers=admin_headers())
    return body if status == 200 else {}


def create_provider() -> None:
    status, _, body = http(
        "POST",
        "/gateway/v1/admin/providers",
        headers=admin_headers(),
        body={
            "id": "openai_compatible",
            "label": "webhook",
            "baseUrl": "http://127.0.0.1:9/v1",
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    if status != 201:
        raise RuntimeError(f"创建 provider 失败：{status} {body}")


def patch_provider() -> None:
    status, _, body = http(
        "PATCH",
        "/gateway/v1/admin/providers/openai_compatible",
        headers=admin_headers(),
        body={"label": "webhook-2"},
    )
    if status != 200:
        raise RuntimeError(f"修改 provider 失败：{status} {body}")


def create_key() -> None:
    status, _, body = http(
        "POST",
        "/gateway/v1/admin/api-keys",
        headers=admin_headers(),
        body={
            "label": "hook-key",
            "tenant": "acme",
            "dept": "dept-sales",
            "sub": "u-hook",
            "scopes": ["chat"],
        },
    )
    if status != 201:
        raise RuntimeError(f"签发 Key 失败：{status} {body}")


def main() -> int:
    check("外送组件已启用", audit_webhook.enabled)
    server = start_gateway()
    try:
        print("\n[投递与签名]")
        create_provider()
        received = wait_for(lambda: len(Receiver.requests) >= 1)
        check("审计事件被外送", received, str(len(Receiver.requests)))
        first = Receiver.requests[0]
        check("HMAC 签名可复算", first["valid"], json.dumps(first, ensure_ascii=False))
        check(
            "批次带网关标识与事件",
            first["body"].get("gatewayId") == support.GATEWAY_ID
            and any(e.get("action") == "provider.create" for e in first["body"].get("events", [])),
            json.dumps(first["body"], ensure_ascii=False)[:300],
        )
        check("投递后 outbox 清空", wait_for(lambda: health().get("auditWebhook", {}).get("pending") == 0))
        status = health().get("auditWebhook", {})
        check(
            "健康页报告投递状态",
            status.get("enabled") is True
            and status.get("consecutiveFailures") == 0
            and status.get("lastDeliveredAt"),
            json.dumps(status, ensure_ascii=False),
        )

        print("\n[失败重试]")
        Receiver.mode = "fail"
        before = len(Receiver.requests)
        patch_provider()
        failed = wait_for(lambda: len(Receiver.requests) > before)
        check("接收端 500 视为失败", failed)
        check(
            "失败后 outbox 保留并计数",
            wait_for(lambda: health().get("auditWebhook", {}).get("consecutiveFailures", 0) >= 1),
            json.dumps(health().get("auditWebhook", {}), ensure_ascii=False),
        )
        check("积压仍待投递", health().get("auditWebhook", {}).get("pending", 0) >= 1)
        Receiver.mode = "ok"
        check(
            "恢复后自动补投",
            wait_for(
                lambda: health().get("auditWebhook", {}).get("pending") == 0, timeout=25
            ),
            json.dumps(health().get("auditWebhook", {}), ensure_ascii=False),
        )

        print("\n[4xx 跳过]")
        Receiver.mode = "reject"
        before = len(Receiver.requests)
        create_key()
        rejected = wait_for(lambda: len(Receiver.requests) > before)
        check("接收端 400 收到请求", rejected)
        check(
            "4xx 记错误后跳过（不阻塞后续）",
            wait_for(
                lambda: (health().get("auditWebhook", {}).get("pending") == 0)
                and "rejected" in (health().get("auditWebhook", {}).get("lastError") or ""),
                timeout=15,
            ),
            json.dumps(health().get("auditWebhook", {}), ensure_ascii=False),
        )
        count = len(Receiver.requests)
        time.sleep(2)
        check("已跳过的事件不再重试", len(Receiver.requests) == count, str(len(Receiver.requests)))
        check("所有请求签名均可复算", all(item["valid"] for item in Receiver.requests))
        check(
            "outbox 不落明文密钥",
            "ttg_" not in json.dumps([item["body"] for item in Receiver.requests], ensure_ascii=False),
        )
    finally:
        stop_gateway(server)
        _receiver.shutdown()
        db.close()
    return report("M4 审计外送自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
