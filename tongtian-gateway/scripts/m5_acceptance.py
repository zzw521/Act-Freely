#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G9 验收：M5 多 worker 与共享状态，并写出 docs/m5-acceptance.md。

全部走真实进程与真实 HTTP / 真实 Redis：
- 起两个 gateway 进程（同一 data 目录 / 同一份 SQLite、同一 gatewayId、各自 pid）；
- Redis 承载管理面与 per-key 令牌桶、配额在途预留与并发计数、策略失效广播；
- 先跑 M5 三个单测（多进程迁移 / 共享状态 / outbox 租约）与 M4 单 worker 回归；
- 端到端覆盖：跨 worker 限流、跨 worker 月度配额、跨 worker 并发上限、
  Redis 故障失败关闭（CLIENT PAUSE）、策略新版本经 Pub/Sub 立即传播、
  审计链两端一致、outbox 恰好一次投递、重启后共享管理面限流；
- 两个启动门槛：workers>1 缺 Redis、Redis 不可达，都必须点名拒绝启动。

Redis 来源优先级：`M5_REDIS_URL` → 本机 Docker `redis:7-alpine`（带 requirepass）
→ `redis://127.0.0.1:6391/0`（手工启动的测试实例）。

用法: python scripts/m5_acceptance.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import redis

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
SERVER_ROOT = GATEWAY_ROOT.parent / "tongtian-server"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import g3_acceptance as g3  # noqa: E402
import g4_acceptance as g4  # noqa: E402

REPORT_PATH = GATEWAY_ROOT / "docs" / "m5-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)

GATEWAY_ID = "gw-g9-01"
TENANT = g4.TENANT
DEPT = g4.DEPT
CONC_DEPT = "dept-g9conc"
WEBHOOK_SECRET = "g9-audit-webhook-secret-0123456789"
PROVIDER_SECRET = "sk-g9-fake-provider-secret"
TOKENS_PER_CALL = g4.TOKENS_PER_CALL  # 假上游固定 12 + 8
REDIS_PREFIX = f"g9accept{uuid.uuid4().hex[:6]}"
ADMIN_HEADERS = {"X-TT-Admin-Token": g4.ADMIN_TOKEN}


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str
    output: str = field(default="", repr=False)


def _tail(text: str, lines: int = 20) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        details = error.get("details") or [{}]
        return (
            error.get("tt_code")
            or (details[0].get("code") if isinstance(details[0], dict) else "")
            or error.get("code")
            or ""
        )
    return str(body.get("code") or "")


def message_text(body) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


def wait_until(fn, timeout: float = 20.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def _json_text(value) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def digest(entry: dict) -> str:
    material = "\n".join(
        (
            str(entry.get("prevHash") or ""),
            str(int(entry.get("atMs") or 0)),
            str(entry.get("actor") or ""),
            str(entry.get("actorRole") or ""),
            str(entry.get("action") or ""),
            str(entry.get("object") or ""),
            _json_text(entry.get("before")),
            _json_text(entry.get("after")),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_chain(entries: list[dict]) -> tuple[bool, str]:
    previous = ""
    for entry in entries:
        if previous and str(entry.get("prevHash")) != previous:
            return False, f"id={entry.get('id')} prevHash 断链"
        if digest(entry) != str(entry.get("entryHash")):
            return False, f"id={entry.get('id')} entryHash 不可复算"
        previous = str(entry.get("entryHash"))
    return True, ""


# ── Redis ─────────────────────────────────────────────────


class RedisServer:
    """为验收提供带密码的临时 Redis；也接受外部 M5_REDIS_URL。"""

    def __init__(self) -> None:
        self.url = ""
        self.password = ""
        self._container = ""

    def start(self) -> "RedisServer":
        external = os.environ.get("M5_REDIS_URL", "").strip()
        if external:
            self.url = external
            self._require_ping("外部 M5_REDIS_URL")
            return self

        docker = shutil.which("docker")
        if docker:
            try:
                probe = subprocess.run(
                    [docker, "image", "inspect", "redis:7-alpine"],
                    capture_output=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                probe = None
            if probe is not None and probe.returncode == 0:
                port = g3.free_port()
                self.password = "g9redis" + secrets.token_hex(8)
                name = f"tt-g9-redis-{uuid.uuid4().hex[:6]}"
                run = subprocess.run(
                    [
                        docker,
                        "run",
                        "-d",
                        "--rm",
                        "--name",
                        name,
                        "-p",
                        f"127.0.0.1:{port}:6379",
                        "redis:7-alpine",
                        "redis-server",
                        "--appendonly",
                        "no",
                        "--requirepass",
                        self.password,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if run.returncode != 0:
                    raise RuntimeError(
                        "启动 Redis 容器失败：" + (run.stderr or run.stdout).strip()
                    )
                self._container = name
                self.url = f"redis://:{self.password}@127.0.0.1:{port}/0"
                if not self._wait_come_up(30):
                    self.stop()
                    raise RuntimeError("Redis 容器已启动但 PING 不通")
                return self

        fallback = "redis://127.0.0.1:6391/0"
        self.url = fallback
        self._require_ping("回退 Redis")
        return self

    def client(self):
        return redis.Redis.from_url(
            self.url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )

    def _require_ping(self, label: str) -> None:
        try:
            client = self.client()
            client.ping()
            client.close()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{label} 不可用（{type(exc).__name__}）。"
                "请设置 M5_REDIS_URL，或准备 Docker 镜像 redis:7-alpine"
            ) from exc

    def _wait_come_up(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                client = self.client()
                client.ping()
                client.close()
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        return False

    def pause(self, milliseconds: int) -> None:
        """暂停所有客户端命令，模拟共享状态不可用（只读/读写全部挂起）。"""
        client = self.client()
        client.execute_command("CLIENT", "PAUSE", milliseconds, "ALL")
        client.close()

    def clean_prefix(self) -> None:
        client = self.client()
        keys = list(client.scan_iter(match=f"{REDIS_PREFIX}*", count=200))
        if keys:
            client.delete(*keys)
        client.close()

    def stop(self) -> None:
        if self._container:
            subprocess.run(
                ["docker", "rm", "-f", self._container],
                capture_output=True,
                timeout=60,
            )
            self._container = ""


# ── Webhook 接收端 ─────────────────────────────────────────


class _HookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, _HookHandler)
        self.records: list[dict] = []
        self.lock = threading.Lock()


class _HookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        timestamp = self.headers.get("x-tt-audit-timestamp", "")
        signature = self.headers.get("x-tt-audit-signature", "")
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        valid = bool(timestamp) and hmac.compare_digest(signature, expected)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        record = {
            "valid": valid,
            "gatewayId": self.headers.get("x-tt-gateway-id", ""),
            "body": payload,
            "raw": body.decode("utf-8", "replace"),
        }
        server = self.server
        with server.lock:
            server.records.append(record)
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 验收输出里不要 HTTP 访问日志
        return


class WebhookReceiver:
    def __init__(self) -> None:
        self._server = _HookServer(("127.0.0.1", 0))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="g9-webhook", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/audit/events"

    @property
    def records(self) -> list[dict]:
        with self._server.lock:
            return list(self._server.records)

    def start(self) -> "WebhookReceiver":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ── 进程与网关 ─────────────────────────────────────────────


def gateway_env(
    control_plane: str,
    data_dir: Path,
    *,
    redis_url: str,
    policy_ttl: int,
    admin_rpm: int,
    admin_burst: int,
    webhook_url: str,
) -> dict[str, str]:
    return g3._clean_env(
        {
            "TT_GATEWAY_ENV": "development",
            "TT_GATEWAY_JWT_MODE": "rs256",
            "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
            "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
            "TT_GATEWAY_SERVICE_TOKEN": g4.SERVICE_TOKEN,
            "TT_GATEWAY_ADMIN_TOKEN": g4.ADMIN_TOKEN,
            "TT_GATEWAY_ID": GATEWAY_ID,
            "TT_GATEWAY_DATA_DIR": str(data_dir),
            "TT_GATEWAY_MASTER_KEY": g3.MASTER_KEY,
            "TT_GATEWAY_MASTER_KEY_VERSION": "1",
            "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
            "TT_GATEWAY_POLICY_TTL_SECONDS": str(policy_ttl),
            "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE": str(admin_rpm),
            "TT_GATEWAY_ADMIN_RATE_LIMIT_BURST": str(admin_burst),
            "TT_GATEWAY_AUDIT_WEBHOOK_URL": webhook_url,
            "TT_GATEWAY_AUDIT_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS": "1",
            "TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE": "50",
            "TT_GATEWAY_WORKERS": "2",
            "TT_GATEWAY_REDIS_URL": redis_url,
            "TT_GATEWAY_REDIS_PREFIX": REDIS_PREFIX,
            "TT_GATEWAY_REDIS_TIMEOUT_MS": "500",
        }
    )


def launch_gateway(
    port: int, *, env: dict[str, str], log_path: Path, wait: bool = True
) -> g3.Process:
    process = g3.Process(
        [
            g3.venv_python(GATEWAY_ROOT),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        GATEWAY_ROOT,
        env,
        log_path,
    )
    if wait:
        g3.wait_ready(f"http://127.0.0.1:{port}", "/health", "网关")
    return process


# ── 管理面与协议面 ─────────────────────────────────────────


def admin_health(base: str, tenant: str = TENANT):
    return g3.http(
        base,
        "GET",
        f"/gateway/v1/admin/health?tenant={tenant}",
        headers=ADMIN_HEADERS,
    )


def wait_shared_ready(base: str) -> dict | None:
    def probe():
        status, _, body = admin_health(base)
        state = body.get("sharedState") if isinstance(body, dict) else None
        if (
            status == 200
            and isinstance(state, dict)
            and state.get("mode") == "redis"
            and state.get("connected")
            and state.get("policySubscriber") == "listening"
        ):
            return body
        return None

    return wait_until(probe, timeout=20, interval=0.2)


def create_key(
    base: str,
    *,
    label: str,
    dept: str,
    sub: str,
    rpm: int = 0,
    quota: int = 0,
) -> dict:
    status, _, body = g3.http(
        base,
        "POST",
        "/gateway/v1/admin/api-keys",
        headers=ADMIN_HEADERS,
        body={
            "label": label,
            "tenant": TENANT,
            "dept": dept,
            "sub": sub,
            "scopes": ["chat"],
            "modelAllowlist": ["auto", "fake-chat"],
            "rateLimitRpm": rpm,
            "quotaMonthlyTokens": quota,
        },
    )
    key = body.get("key") if isinstance(body, dict) else None
    g3.check(
        f"签发 API Key（{label}）",
        status == 201 and isinstance(key, dict) and bool(key.get("id")),
        f"{status} {body}",
    )
    return key or {}


def chat(base: str, secret: str, *, max_tokens: int = 64):
    return g3.http(
        base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": max_tokens,
        },
        timeout=60,
    )


def custom_policy(gateway_base: str, price_version: str = "g9-2026-10") -> dict:
    document = g4.policy_document(gateway_base)
    document["models"]["fake-chat"]["priceVersion"] = price_version
    document["quotaDefaults"] = {
        "monthlyTokens": 1000000,
        "concurrency": 4,
        "maxOutputTokens": 4096,
    }
    document["departments"][DEPT] = {"allowedModels": ["auto", "fake-chat"]}
    document["departments"][CONC_DEPT] = {
        "allowedModels": ["auto", "fake-chat"],
        "quota": {"concurrency": 1},
    }
    return document


def put_policy(server_base: str, admin: str, document: dict) -> str:
    status, _, body = g3.http(
        server_base,
        "PUT",
        f"/api/v1/admin/gateway/policy/{TENANT}",
        headers={"Authorization": f"Bearer {admin}"},
        body=document,
    )
    version = body.get("policyVersion", "") if isinstance(body, dict) else ""
    g3.check(
        f"控制面保存策略 → {version or '?'}",
        status == 200 and bool(version),
        f"{status} {body}",
    )
    return version


# ── 验收段 ─────────────────────────────────────────────────


def check_workers_ready(base_a: str, base_b: str, policy_v2: str, redis_srv: RedisServer) -> None:
    g3.step("G9-1：两个 worker 共享状态与健康面")
    health_a = wait_shared_ready(base_a)
    health_b = wait_shared_ready(base_b)
    g3.check("worker A 共享状态就绪", isinstance(health_a, dict), str(health_a))
    g3.check("worker B 共享状态就绪", isinstance(health_b, dict), str(health_b))
    health_a = health_a or {}
    health_b = health_b or {}

    state_a = health_a.get("sharedState") or {}
    state_b = health_b.get("sharedState") or {}
    g3.check(
        "两端都报告 redis / connected / workers=2 / listening",
        state_a.get("mode") == "redis"
        and state_b.get("mode") == "redis"
        and state_a.get("connected") is True
        and state_b.get("connected") is True
        and state_a.get("workers") == 2
        and state_b.get("workers") == 2
        and state_a.get("policySubscriber") == "listening"
        and state_b.get("policySubscriber") == "listening",
        f"A={state_a} B={state_b}",
    )
    g3.check(
        "同一 gatewayId、不同 pid",
        health_a.get("gatewayId") == GATEWAY_ID
        and health_b.get("gatewayId") == GATEWAY_ID
        and health_a.get("workerPid")
        and health_b.get("workerPid")
        and health_a.get("workerPid") != health_b.get("workerPid"),
        f"A={health_a.get('workerPid')} B={health_b.get('workerPid')}",
    )
    g3.check(
        "两个 worker 已读到同一策略版本",
        health_a.get("policyVersion") == policy_v2
        and health_b.get("policyVersion") == policy_v2,
        f"A={health_a.get('policyVersion')} B={health_b.get('policyVersion')}",
    )
    blob = json.dumps([health_a, health_b], ensure_ascii=False)
    g3.check(
        "健康面不回显 Redis 连接串/密码",
        "redis://" not in blob
        and "rediss://" not in blob
        and (not redis_srv.password or redis_srv.password not in blob),
        blob[:300],
    )


def check_shared_rate(base_a: str, base_b: str, secret: str) -> None:
    g3.step("G9-2：per-key RPM 跨 worker 共享")
    statuses: list[int] = []
    bodies: list = []
    for index in range(5):
        status, _, body = chat(base_a if index % 2 == 0 else base_b, secret)
        statuses.append(status)
        bodies.append(body)
    g3.check(
        "交替打 A/B 的前 4 次通过（桶容量 4）",
        statuses[:4] == [200] * 4
        and all(message_text(body) == "你好" for body in bodies[:4]),
        f"statuses={statuses} last={bodies[-1]}",
    )
    last = bodies[-1]
    g3.check(
        "第 5 次在另一 worker 上 429 RATE_LIMITED",
        statuses[4] == 429 and error_code(last) == "RATE_LIMITED",
        f"statuses={statuses} {last}",
    )


def check_shared_quota(base_a: str, base_b: str, secret: str) -> None:
    g3.step("G9-3：per-key 月配额跨 worker 共享")
    statuses = []
    bodies = []
    for index in range(6):
        status, _, body = chat(
            base_a if index % 2 == 0 else base_b, secret, max_tokens=1
        )
        statuses.append(status)
        bodies.append(body)
    g3.check(
        f"交替打 A/B 的前 5 次通过（每次结算 {TOKENS_PER_CALL} token，上限 100）",
        statuses[:5] == [200] * 5
        and all(message_text(body) == "你好" for body in bodies[:5]),
        f"statuses={statuses} last={bodies[-1]}",
    )
    g3.check(
        "第 6 次 429 QUOTA_TOKENS_EXCEEDED（另一 worker 读到共享账本）",
        statuses[5] == 429 and error_code(bodies[5]) == "QUOTA_TOKENS_EXCEEDED",
        f"statuses={statuses} {bodies[5]}",
    )


def check_shared_concurrency(
    base_a: str, base_b: str, secret: str, upstream
) -> None:
    g3.step("G9-4：并发计数跨 worker 共享")
    first: dict = {}

    def slow_call() -> None:
        first["result"] = chat(base_a, secret)

    upstream.delay_seconds = 2.5
    thread = threading.Thread(target=slow_call, name="g9-slow-chat")
    thread.start()
    try:
        time.sleep(0.7)
        status, _, body = chat(base_b, secret)
        thread.join(timeout=15)
    finally:
        upstream.delay_seconds = 0
    first_result = first.get("result") or (0, {}, {})
    g3.check(
        "A 上的在途请求未被并发上限误伤",
        first_result[0] == 200 and message_text(first_result[2]) == "你好",
        f"{first_result[0]} {first_result[2]}",
    )
    g3.check(
        "A 在途时 B 的第二个请求 429 QUOTA_CONCURRENCY_EXCEEDED（部门并发=1）",
        status == 429 and error_code(body) == "QUOTA_CONCURRENCY_EXCEEDED",
        f"{status} {body}",
    )
    status, _, body = chat(base_b, secret)
    g3.check(
        "A 结算后 B 可再次通过（并发计数已回退）",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )


def check_fail_closed(
    base_a: str, base_b: str, redis_srv: RedisServer, fresh_secret: str, rate_secret: str
) -> None:
    g3.step("G9-5：Redis 不可用时失败关闭（D6）")
    redis_srv.pause(3000)
    status, _, body = chat(base_a, fresh_secret)
    g3.check(
        "配额路径 503 SHARED_STATE_UNAVAILABLE",
        status == 503 and error_code(body) == "SHARED_STATE_UNAVAILABLE",
        f"{status} {body}",
    )
    g3.check(
        "OpenAI 信封类型为 api_error",
        isinstance(body, dict)
        and (body.get("error") or {}).get("type") == "api_error",
        str(body),
    )
    status, _, body = chat(base_b, rate_secret)
    g3.check(
        "限流路径同样 503（不静默放开）",
        status == 503 and error_code(body) == "SHARED_STATE_UNAVAILABLE",
        f"{status} {body}",
    )
    time.sleep(2.5)
    status, _, body = chat(base_a, fresh_secret)
    g3.check(
        "Redis 恢复后无需重启即恢复服务",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )
    health = admin_health(base_a)
    state = health[2].get("sharedState") if isinstance(health[2], dict) else {}
    g3.check(
        "恢复后 health.sharedState.connected=true",
        isinstance(state, dict) and state.get("connected") is True,
        str(state),
    )


def check_policy_invalidation(
    server_base: str,
    admin: str,
    base_a: str,
    base_b: str,
    *,
    policy_v2: str,
    fresh_secret: str,
) -> str:
    g3.step("G9-6：策略新版本经 Pub/Sub 立即传播（B 的 TTL=300s）")
    policy_v3 = put_policy(server_base, admin, custom_policy(base_a, "g9-2026-11"))
    before = admin_health(base_b)[2]
    g3.check(
        "B 在 TTL 内仍报告旧版本",
        isinstance(before, dict) and before.get("policyVersion") == policy_v2,
        f"B={before.get('policyVersion') if isinstance(before, dict) else before}",
    )
    time.sleep(2.4)
    status, _, body = chat(base_a, fresh_secret)
    g3.check(
        "A 过了 TTL 后拉取新版本",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )

    began = time.time()
    seen = ""
    while time.time() - began < 5:
        current = admin_health(base_b)[2]
        seen = current.get("policyVersion", "") if isinstance(current, dict) else ""
        if seen == policy_v3:
            break
        time.sleep(0.2)
    elapsed = time.time() - began
    g3.check(
        f"B 在 {elapsed:.1f}s 内切到新版本（远小于 B 的 300s TTL）",
        seen == policy_v3 and elapsed < 5,
        f"B={seen} 期望={policy_v3} 用时={elapsed:.1f}s",
    )
    return policy_v3


def check_audit_and_outbox(
    base_a: str, base_b: str, receiver: WebhookReceiver
) -> None:
    g3.step("G9-7：审计链两端一致 + outbox 恰好一次投递")

    def entries_of(base: str) -> list[dict]:
        status, _, body = g3.http(
            base,
            "GET",
            "/gateway/v1/admin/audit?after=0&limit=1000",
            headers=ADMIN_HEADERS,
        )
        return (body.get("entries") or []) if isinstance(body, dict) else []

    verify_a = g3.http(
        base_a, "GET", "/gateway/v1/admin/audit/verify", headers=ADMIN_HEADERS
    )
    verify_b = g3.http(
        base_b, "GET", "/gateway/v1/admin/audit/verify", headers=ADMIN_HEADERS
    )
    body_a = verify_a[2] if isinstance(verify_a[2], dict) else {}
    body_b = verify_b[2] if isinstance(verify_b[2], dict) else {}
    g3.check(
        "两个 worker 验链都通过且 headHash 相同",
        verify_a[0] == 200
        and verify_b[0] == 200
        and body_a.get("ok") is True
        and body_b.get("ok") is True
        and body_a.get("brokenAt") is None
        and body_b.get("brokenAt") is None
        and bool(body_a.get("headHash"))
        and body_a.get("headHash") == body_b.get("headHash"),
        f"A={body_a} B={body_b}",
    )

    entries = entries_of(base_a)
    ok, reason = verify_chain(entries)
    g3.check("网关审计链逐条可复算（共享 SQLite 追加有序）", bool(entries) and ok, reason)
    g3.check(
        "审计条目包含两个 worker 的写操作",
        len(entries) >= 4,
        str(len(entries)),
    )

    status, headers, raw = g3.http(
        base_b,
        "GET",
        "/gateway/v1/admin/audit/export?limit=1000",
        headers=ADMIN_HEADERS,
    )
    exported = []
    for line in str(raw or "").splitlines():
        if line.strip():
            try:
                exported.append(json.loads(line))
            except ValueError:
                exported.append({})
    ok, reason = verify_chain(exported)
    g3.check(
        "B 导出的 NDJSON 逐行可复算且头哈希一致",
        status == 200
        and bool(exported)
        and ok
        and headers.get("x-tt-audit-head", "")
        == (exported[-1].get("entryHash") if exported else ""),
        f"{status} lines={len(exported)} {reason}",
    )

    ids = {int(entry["id"]) for entry in entries}
    actions = {str(entry.get("action") or "") for entry in entries}

    def delivered() -> list[int]:
        collected: list[int] = []
        for record in receiver.records:
            events = (record.get("body") or {}).get("events") or []
            collected.extend(int(event.get("id") or 0) for event in events)
        return collected

    def flushed():
        delivered_now = delivered()
        health = admin_health(base_a)[2]
        upload = health.get("auditWebhook") if isinstance(health, dict) else {}
        if (
            ids <= set(delivered_now)
            and isinstance(upload, dict)
            and upload.get("pending") == 0
            and upload.get("consecutiveFailures") == 0
        ):
            return delivered_now
        return None

    final = wait_until(flushed, timeout=30, interval=0.3)
    final_ids = final or delivered()
    g3.check(
        "所有审计条目都已外送（pending=0）",
        isinstance(final, list) and ids <= set(final_ids),
        f"审计={len(ids)} 已投递={len(set(final_ids))}",
    )
    g3.check(
        "同一事件只被一个 worker 投递（租约生效，无重复）",
        len(final_ids) == len(set(final_ids)) and len(final_ids) >= len(ids),
        f"投递 {len(final_ids)} 次 / 去重 {len(set(final_ids))} 条",
    )
    records = receiver.records
    g3.check(
        "每批 HMAC-SHA256 签名可复算且带同一 gatewayId",
        bool(records)
        and all(record["valid"] for record in records)
        and all(record["gatewayId"] == GATEWAY_ID for record in records),
        str([(record["valid"], record["gatewayId"]) for record in records][:5]),
    )
    g3.check(
        "外送覆盖 provider 与 API Key 写操作",
        {"provider.create", "api_key.create"} <= actions,
        str(sorted(actions)),
    )


def check_admin_rate_shared(
    redis_srv: RedisServer,
    port_a: int,
    port_b: int,
    env_a: dict[str, str],
    env_b: dict[str, str],
    logs: dict[str, Path],
) -> tuple[g3.Process, g3.Process]:
    g3.step("G9-8：共享管理面限流（重启为 6 RPM / burst 6）")
    client = redis_srv.client()
    keys = list(client.scan_iter(match=f"{REDIS_PREFIX}:rl:admin:*", count=200))
    if keys:
        client.delete(*keys)
    client.close()

    proc_a = launch_gateway(port_a, env=env_a, log_path=logs["a_limited"])
    proc_b = launch_gateway(port_b, env=env_b, log_path=logs["b_limited"])
    statuses: list[int] = []
    retry_after = ""
    for index in range(7):
        status, headers, _ = admin_health(f"http://127.0.0.1:{port_a if index % 2 == 0 else port_b}")
        statuses.append(status)
        if status == 429:
            retry_after = headers.get("retry-after", "")
    g3.check(
        "交替打 A/B 的前 6 次通过、第 7 次 429 RATE_LIMITED",
        statuses[:6] == [200] * 6 and statuses[6] == 429,
        f"statuses={statuses} Retry-After={retry_after!r}",
    )
    g3.check(
        "429 带 Retry-After（整数秒）",
        retry_after.isdigit() and int(retry_after) >= 1,
        retry_after,
    )
    return proc_a, proc_b


# ── 启动门槛 ───────────────────────────────────────────────


def boot_gate(name: str, overrides: dict[str, str], expected: str) -> Step:
    temp = Path(tempfile.mkdtemp(prefix="tt-g9-boot-"))
    env = g3._clean_env(
        {
            "TT_GATEWAY_ENV": "development",
            "TT_GATEWAY_DATA_DIR": str(temp / "data"),
            "TT_GATEWAY_WORKERS": "2",
        }
    )
    env.update(overrides)
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [
                g3.venv_python(GATEWAY_ROOT),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(g3.free_port()),
            ],
            cwd=str(GATEWAY_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return Step(name, False, time.perf_counter() - began, "进程未退出")
    text = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode != 0 and expected in text
    return Step(
        name,
        ok,
        time.perf_counter() - began,
        f"退出码 {proc.returncode}；{'点名' if ok else '未点名'} {expected}",
        output=_tail(text, 12),
    )


# ── 主流程 ─────────────────────────────────────────────────


def run_shared_flow(redis_srv: RedisServer) -> list[Step]:
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g9-"))
    upstream = g3.FakeUpstream("openai").start()
    receiver = WebhookReceiver().start()
    server_port = g3.free_port()
    port_a = g3.free_port()
    port_b = g3.free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    base_a = f"http://127.0.0.1:{port_a}"
    base_b = f"http://127.0.0.1:{port_b}"
    logs = {
        "server": temp / "server.log",
        "a": temp / "gateway-a.log",
        "b": temp / "gateway-b.log",
        "a_limited": temp / "gateway-a-limited.log",
        "b_limited": temp / "gateway-b-limited.log",
    }
    server = None
    gateway_a = None
    gateway_b = None
    limited_a = None
    limited_b = None
    key_secrets: list[str] = []
    try:
        began = time.perf_counter()
        server = g4.start_server(
            server_port, temp / "server", logs["server"], base_a
        )
        env_a = gateway_env(
            server_base,
            temp / "gateway",
            redis_url=redis_srv.url,
            policy_ttl=2,
            admin_rpm=600,
            admin_burst=200,
            webhook_url=receiver.url,
        )
        env_b = gateway_env(
            server_base,
            temp / "gateway",
            redis_url=redis_srv.url,
            policy_ttl=300,
            admin_rpm=600,
            admin_burst=200,
            webhook_url=receiver.url,
        )
        # 不等待 A 就启动 B：两个进程同时迁移同一份 SQLite
        gateway_a = launch_gateway(port_a, env=env_a, log_path=logs["a"], wait=False)
        gateway_b = launch_gateway(port_b, env=env_b, log_path=logs["b"], wait=False)
        g3.wait_ready(base_a, "/health", "网关 A")
        g3.wait_ready(base_b, "/health", "网关 B")
        steps.append(
            Step(
                "两个 worker 同时启动（共享 SQLite + Redis）",
                True,
                time.perf_counter() - began,
                "控制面、假上游、Webhook 与两个网关均就绪",
            )
        )

        uid, admin, access = g4.setup_control_plane(
            server_base, base_a, f"{upstream.base_url}/v1"
        )
        policy_v2 = put_policy(server_base, admin, custom_policy(base_a))

        key_rate = create_key(
            base_a, label="g9-rate", dept=DEPT, sub="g9-rate", rpm=4
        )
        key_quota = create_key(
            base_a, label="g9-quota", dept=DEPT, sub="g9-quota", rpm=600, quota=100
        )
        # Key C 在 B 上签发：审计写操作来自第二个 worker
        key_conc = create_key(
            base_b, label="g9-conc", dept=CONC_DEPT, sub="g9-conc"
        )
        key_fresh = create_key(
            base_a, label="g9-fresh", dept=DEPT, sub="g9-fresh"
        )
        key_secrets = [
            str(key.get("secret") or "")
            for key in (key_rate, key_quota, key_conc, key_fresh)
        ]

        steps.append(section("多 worker 共享健康面", check_workers_ready, base_a, base_b, policy_v2, redis_srv))
        steps.append(section("per-key RPM 跨 worker", check_shared_rate, base_a, base_b, key_rate.get("secret", "")))
        steps.append(section("per-key 月配额跨 worker", check_shared_quota, base_a, base_b, key_quota.get("secret", "")))
        steps.append(section("并发计数跨 worker", check_shared_concurrency, base_a, base_b, key_conc.get("secret", ""), upstream))
        steps.append(section("Redis 故障失败关闭", check_fail_closed, base_a, base_b, redis_srv, key_fresh.get("secret", ""), key_rate.get("secret", "")))
        steps.append(section("策略失效 Pub/Sub", check_policy_invalidation, server_base, admin, base_a, base_b, policy_v2=policy_v2, fresh_secret=key_fresh.get("secret", "")))
        steps.append(section("审计链与 outbox 恰好一次", check_audit_and_outbox, base_a, base_b, receiver))

        blocked = [
            value
            for value in (g4.ADMIN_TOKEN, g4.SERVICE_TOKEN, g3.MASTER_KEY)
            if value
        ] + key_secrets
        haystack = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (logs["server"], logs["a"], logs["b"])
            if path.exists()
        )
        leaked = [value for value in blocked if value and value in haystack]
        g3.check("进程日志不含管理/服务令牌、主密钥与 Key 明文", not leaked, str(leaked))
        g3.check(
            "进程日志不含 Redis 连接串或密码",
            "redis://" not in haystack
            and "rediss://" not in haystack
            and (not redis_srv.password or redis_srv.password not in haystack),
            "日志出现 Redis 连接信息",
        )
        steps.append(
            Step(
                "敏感值不落日志",
                not leaked
                and "redis://" not in haystack
                and "rediss://" not in haystack
                and (not redis_srv.password or redis_srv.password not in haystack),
                time.perf_counter() - began,
                "管理令牌/服务令牌/主密钥/Key 明文/Redis URL 均未出现",
            )
        )

        gateway_a.stop()
        gateway_b.stop()
        g3.wait_port_free(port_a)
        g3.wait_port_free(port_b)
        env_a_limited = gateway_env(
            server_base,
            temp / "gateway",
            redis_url=redis_srv.url,
            policy_ttl=2,
            admin_rpm=6,
            admin_burst=6,
            webhook_url=receiver.url,
        )
        env_b_limited = gateway_env(
            server_base,
            temp / "gateway",
            redis_url=redis_srv.url,
            policy_ttl=300,
            admin_rpm=6,
            admin_burst=6,
            webhook_url=receiver.url,
        )
        before_admin = len(g3.FAILURES)
        began_admin = time.perf_counter()
        try:
            limited_a, limited_b = check_admin_rate_shared(
                redis_srv,
                port_a,
                port_b,
                env_a_limited,
                env_b_limited,
                logs,
            )
        except Exception as exc:  # noqa: BLE001 - 失败也要留下两个进程供 finally 清理
            traceback.print_exc()
            g3.FAILURES.append(
                f"  NG  共享管理面限流 — {type(exc).__name__}: {exc}"
            )
        admin_failures = g3.FAILURES[before_admin:]
        steps.append(
            Step(
                "共享管理面限流（重启 6 RPM / burst 6）",
                not admin_failures,
                time.perf_counter() - began_admin,
                "；".join(admin_failures)
                if admin_failures
                else "两个 worker 共用一个管理面令牌桶",
            )
        )
    except Exception as exc:  # noqa: BLE001 - 验收要把异常落进报告
        traceback.print_exc()
        g3.FAILURES.append(f"  NG  多 worker 端到端 — {type(exc).__name__}: {exc}")
        steps.append(
            Step(
                "多 worker 端到端",
                False,
                time.perf_counter() - began,
                f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        for process in (limited_a, limited_b, gateway_a, gateway_b):
            if process is not None:
                process.stop()
        if server is not None:
            server.stop()
        receiver.stop()
        upstream.stop()
        redis_srv.clean_prefix()
    return steps


def section(name: str, fn, *args, **kwargs) -> Step:
    before = len(g3.FAILURES)
    began = time.perf_counter()
    detail = "全部断言通过"
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        g3.FAILURES.append(f"  NG  {name} — {type(exc).__name__}: {exc}")
        detail = f"{type(exc).__name__}: {exc}"
    failures = g3.FAILURES[before:]
    if failures:
        detail = "；".join(failures)
    return Step(name, not failures, time.perf_counter() - began, detail)


def unit_suite(
    name: str,
    path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 900,
) -> Step:
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [PYTHON, str(path)],
            cwd=str(GATEWAY_ROOT),
            env=g3._clean_env(env or {}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return Step(
            name,
            False,
            time.perf_counter() - began,
            f"超时（{timeout:.0f} 秒）",
            _tail(str(exc.stdout or "")),
        )
    stdout = proc.stdout or ""
    passes = len(re.findall(r"^\s*(?:OK|PASS|✓)\s", stdout, re.MULTILINE))
    fails = len(re.findall(r"^\s*(?:NG|FAIL|✗)\s", stdout, re.MULTILINE))
    return Step(
        name,
        proc.returncode == 0 and fails == 0,
        time.perf_counter() - began,
        f"退出码 {proc.returncode}；通过 {passes} 项，失败 {fails} 项",
        stdout + (proc.stderr or ""),
    )


def write_report(steps: list[Step]) -> None:
    total = len(steps)
    passed = sum(1 for step in steps if step.ok)
    verdict = "通过" if passed == total else "未通过"
    lines = [
        "# M5 验收报告（G9：多 worker 与共享状态）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / "
        "同机 2 个 worker + Redis 7 + 共享 SQLite",
        f"- 结论：**G9 {verdict}**（{passed}/{total} 个步骤通过）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|",
    ]
    for step in steps:
        mark = "通过" if step.ok else "失败"
        detail = step.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {step.name} | {mark} | {step.seconds:.1f}s | {detail} |")
    lines.extend(
        [
            "",
            "## 验收口径",
            "",
            "- 同机多 worker：两个独立 uvicorn 进程使用同一 `TT_GATEWAY_DATA_DIR`、同一份",
            "  SQLite、同一 `TT_GATEWAY_ID`，各自 pid；迁移在 `BEGIN IMMEDIATE` 后重读",
            "  schema 版本，同时启动不会重复迁移或互相阻塞。",
            "- Redis 共享热状态：管理面与 per-key 令牌桶（容量 `max(1,min(rpm,60))`）、",
            "  配额在途预留（`quota:res:*` / `quota:kres:*`）、并发计数（`quota:active:*`），",
            "  桶时间取 Redis `TIME`。已结算用量仍在 SQLite，账本可重建。",
            "- 预留语义：读 SQLite 已用量 → Lua 原子检查 `used + reserved + tokens`、",
            "  部门/Key 月配额与并发 → 写预留并 INCR 并发；结算先写 SQLite 再释放 Redis。",
            "  崩溃留下的预留按 1h TTL 保守回收，不超发。",
            "- 失败关闭：Redis 不可用（本报告用 `CLIENT PAUSE` 模拟）时，限流与配额路径",
            "  返回 503 `SHARED_STATE_UNAVAILABLE`，OpenAI/Anthropic/Gemini 分别映射为",
            "  `api_error` / `overloaded_error` / `UNAVAILABLE`，不静默放开限额。",
            "- 策略失效：`TT_GATEWAY_POLICY_TTL_SECONDS` 到期拉到新版本并写共享 SQLite",
            "  后向 `{prefix}:policy:events` 发布租户；其他 worker 清内存缓存后从共享",
            "  SQLite 读到新版本。本报告 A 的 TTL=2s、B 的 TTL=300s，B 在 5 秒内切版。",
            "- 审计 outbox：`lease_owner = gatewayId:pid:uuid`、租约至少 60s，",
            "  `BEGIN IMMEDIATE` 内认领；两个 worker 并发投递同一批审计不会重复。",
            "- 启动门槛：`TT_GATEWAY_WORKERS>1` 且缺 `TT_GATEWAY_REDIS_URL` 拒绝启动并点名",
            "  变量；配置了 Redis 但 PING 失败同样拒绝启动（不退回本机限流）。",
            "- 管理面限流：management 桶在 Redis 中按 rateKey（证书主体或对端 IP）共享。",
            "  重启为 6 RPM / burst 6 后交替打两个 worker：前 6 次 200，第 7 次 429。",
            "- 安全边界：Redis 连接串只来自环境变量，健康页只暴露模式/连通性，日志不出现",
            "  URL 或密码；管理令牌、服务令牌、主密钥、provider 密钥与 API Key 明文均不落",
            "  日志或审计外送载荷。",
            "",
            "## 说明与残余",
            "",
            "- 跨主机多副本仍需要 Postgres 或等价共享数据库；M5 只解决同机多 worker，",
            "  `gatewayId` 唯一性与审计链仍是单库语义。",
            "- Redis 假定单实例；哨兵/Cluster、凭据轮换与 TLS 客户端证书留到部署侧或后续",
            "  里程碑。`TT_GATEWAY_REDIS_PREFIX` 必须在同机多实例间区分。",
            "- 策略传播是“尽快”不是“强一致”：Pub/Sub 丢失时回到 TTL（默认 300s），",
            "  控制面不可达仍可用 stale 宽限。",
            "- `health.usageQueue` / `usageUpload` 只反映当前 worker；控制面合并视图仍是",
            "  权威。用量上报器每 worker 一个，控制面按 `(gatewayId, requestId)` 去重。",
            "- 共享 Redis 故障期间已发出的在途预留会等待 TTL 回收；恢复后无需重启。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    steps: list[Step] = []
    redis_srv = RedisServer()
    try:
        redis_srv.start()
    except Exception as exc:  # noqa: BLE001
        print(f"G9 无法开始：{exc}")
        return 2
    print(f"[Redis] {redis_srv.url.split('@')[-1]}")

    suites = [
        ("tests/test_m5_schema.py", None),
        ("tests/test_m5_shared.py", {"TT_GATEWAY_REDIS_URL": redis_srv.url}),
        ("tests/test_m5_outbox.py", None),
        ("tests/test_m4_governance.py", None),
    ]
    try:
        for index, (name, env) in enumerate(suites, start=1):
            print(f"\n[{index}/{len(suites) + 1}] {name}")
            step = unit_suite(name, GATEWAY_ROOT / name, env=env)
            steps.append(step)
            print(_tail(step.output))

        print(f"\n[{len(suites) + 1}/{len(suites) + 1}] 多 worker 端到端")
        for step in run_shared_flow(redis_srv):
            steps.append(step)
            print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

        for name, overrides, expected in (
            ("workers>1 缺 Redis 拒绝启动", {}, "TT_GATEWAY_REDIS_URL"),
            (
                "workers>1 且 Redis 不可达拒绝启动",
                {"TT_GATEWAY_REDIS_URL": f"redis://127.0.0.1:{g3.free_port()}/0"},
                "无法连接 Redis",
            ),
        ):
            step = boot_gate(name, overrides, expected)
            steps.append(step)
            print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")
    finally:
        redis_srv.stop()

    write_report(steps)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG9 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = _tail(step.output)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG9 验收通过：多 worker 共享限流/配额/并发、Redis 失败关闭、策略广播、outbox 恰好一次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
