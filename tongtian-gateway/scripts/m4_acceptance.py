#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G8 验收：M4 治理与审计，并写出 docs/m4-acceptance.md。

全部走真实进程与真实 HTTP：
- 先以子进程重跑两端 5 个 M4 用例（角色 / 审计 / 外送 / Key 治理 + 控制面审计）；
- 控制面 + 网关按生产配置启动（RS256 + JWKS + 控制面策略 + 审计外送）；
- 管理面只走控制面代理：角色来自控制面 JWT 快照，代理下发 `X-TT-Admin-Roles`，
  浏览器与管理令牌不接触；
- 覆盖角色只读/可写、改角色吊销会话、两端审计链验链与 NDJSON 导出、
  Webhook HMAC 外送、per-key RPM / 月配额 / 轮换。

说明：G8 把网关管理面设为 `TT_GATEWAY_MTLS_MODE=off`，因为控制面代理不转发
`X-TT-Client-Verify` / `X-TT-Client-DN`；mTLS 本身由 G6/G7 直连覆盖，生产部署
仍应使用 proxy 模式。

用法: python scripts/m4_acceptance.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
SERVER_ROOT = GATEWAY_ROOT.parent / "tongtian-server"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import g3_acceptance as g3  # noqa: E402
import g4_acceptance as g4  # noqa: E402
import m2_acceptance as m2  # noqa: E402

REPORT_PATH = GATEWAY_ROOT / "docs" / "m4-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)
SERVER_PYTHON = g3.venv_python(SERVER_ROOT)

GATEWAY_ID = "gw-g8-prod"
WEBHOOK_SECRET = "g8-audit-webhook-secret-0123456789"
PROVIDER_SECRET = "sk-g8-fake-provider-secret"
OPERATOR_EMAIL = "g8-operator@test.local"
AUDITOR_EMAIL = "g8-auditor@test.local"
USER_EMAIL = "g8-user@test.local"
USER_PASSWORD = "g8-user-secret-123"
TENANT = "acme"
DEPT = "dept-default"
ALL_SCOPES = ["chat", "models"]
SECRET_PATTERN = re.compile(r"ttg_[0-9a-f]{12}_[A-Za-z0-9_-]{43}")


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


def _json_text(value) -> str:
    # 与落库时的 canonical 序列化保持一致；导出保留原始键序，sort_keys 不能开
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
    """逐条复算 entryHash 并校验 prevHash 与前一条相连。"""
    previous = ""
    for entry in entries:
        if previous and str(entry.get("prevHash")) != previous:
            return False, f"id={entry.get('id')} prevHash 断链"
        if digest(entry) != str(entry.get("entryHash")):
            return False, f"id={entry.get('id')} entryHash 不可复算"
        previous = str(entry.get("entryHash"))
    return True, ""


def wait_until(fn, timeout: float = 20.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


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
            "timestamp": timestamp,
            "signature": signature,
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
            target=self._server.serve_forever, name="g8-webhook", daemon=True
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


# ── 控制面与网关 ───────────────────────────────────────────


def register(server_base: str, email: str, nickname: str) -> str:
    status, _, body = g3.http(
        server_base,
        "POST",
        "/api/v1/auth/register",
        body={"email": email, "password": USER_PASSWORD, "nickname": nickname},
    )
    payload = body if isinstance(body, dict) else {}
    uid = str((payload.get("profile") or {}).get("uid") or "")
    g3.check(f"注册 {nickname}", status == 201 and bool(uid), f"{status} {body}")
    return uid


def login(server_base: str, email: str, password: str, label: str) -> tuple[str, dict]:
    status, _, body = g3.http(
        server_base,
        "POST",
        "/api/v1/auth/login",
        body={"email": email, "password": password},
    )
    payload = body if isinstance(body, dict) else {}
    token = str(payload.get("accessToken") or "")
    profile = payload.get("profile") or {}
    g3.check(label, status == 200 and bool(token), f"{status} {body}")
    return token, profile


def grant_role(
    server_base: str, admin_token: str, uid: str, role: str, label: str
) -> dict:
    status, _, body = g3.http(
        server_base,
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        headers={"Authorization": f"Bearer {admin_token}"},
        body={"org": TENANT, "role": role},
    )
    payload = body if isinstance(body, dict) else {}
    profile = payload.get("profile") or {}
    g3.check(
        label,
        status == 200 and profile.get("role") == role,
        f"{status} {body}",
    )
    return payload


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def server_api(server_base: str, method: str, path: str, token: str, body=None):
    return g3.http(server_base, method, path, headers=bearer(token), body=body)


def proxy(server_base: str, token: str, method: str, path: str, body=None):
    return g3.http(
        server_base,
        method,
        "/api/v1/admin/gateway" + path,
        headers=bearer(token),
        body=body,
    )


def create_key(
    server_base: str,
    token: str,
    *,
    label: str,
    scopes: list[str],
    allowlist: list[str] | None = None,
    rpm: int = 0,
    quota: int = 0,
    sub: str = "g8-key-sub",
) -> dict:
    payload = {
        "label": label,
        "tenant": TENANT,
        "dept": DEPT,
        "sub": sub,
        "scopes": scopes,
        "modelAllowlist": allowlist if allowlist is not None else [],
        "rateLimitRpm": rpm,
        "quotaMonthlyTokens": quota,
    }
    status, _, body = proxy(server_base, token, "POST", "/api-keys", payload)
    key = body.get("key") if isinstance(body, dict) else None
    g3.check(
        f"经代理签发 Key（{label}）",
        status == 201
        and isinstance(key, dict)
        and SECRET_PATTERN.fullmatch(str(key.get("secret") or "")) is not None,
        f"{status} {body}",
    )
    return key or {}


def chat(
    gateway_base: str, secret: str, model: str = "auto", max_tokens: int = 64
):
    return g3.http(
        gateway_base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
        body={
            "model": model,
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": max_tokens,
        },
    )


def message_text(body) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


def setup_control_plane(server_base: str, gateway_base: str) -> dict:
    admin_token, admin_profile = login(
        server_base, g4.ADMIN_EMAIL, g4.ADMIN_PASSWORD, "管理员登录"
    )
    admin_uid = str(admin_profile.get("uid") or "")
    g3.check("登录响应带管理员 uid", bool(admin_uid), str(admin_profile))

    operator_uid = register(server_base, OPERATOR_EMAIL, "G8 运维")
    auditor_uid = register(server_base, AUDITOR_EMAIL, "G8 审计员")
    user_uid = register(server_base, USER_EMAIL, "G8 普通用户")

    grant_role(server_base, admin_token, operator_uid, "operator", "授予 operator 角色")
    grant_role(server_base, admin_token, auditor_uid, "auditor", "授予 auditor 角色")
    grant_role(server_base, admin_token, user_uid, "user", "普通用户留在 user 角色")

    operator_token, _ = login(server_base, OPERATOR_EMAIL, USER_PASSWORD, "operator 登录")
    auditor_token, _ = login(server_base, AUDITOR_EMAIL, USER_PASSWORD, "auditor 登录")
    user_token, _ = login(server_base, USER_EMAIL, USER_PASSWORD, "普通用户登录")

    policy = g3.policy_document(gateway_base)
    policy["departments"][DEPT]["allowedModels"] = ["auto", "fake-chat"]
    status, _, body = server_api(
        server_base,
        "PUT",
        f"/api/v1/admin/gateway/policy/{TENANT}",
        admin_token,
        policy,
    )
    g3.check("保存租户策略", status == 200, f"{status} {body}")

    return {
        "admin": admin_token,
        "admin_uid": admin_uid,
        "operator": operator_token,
        "operator_uid": operator_uid,
        "auditor": auditor_token,
        "auditor_uid": auditor_uid,
        "user": user_token,
        "user_uid": user_uid,
    }


def start_gateway(
    port: int, *, control_plane: str, data_dir: Path, log_path: Path, webhook_url: str
) -> g3.Process:
    env = m2.production_env(
        control_plane,
        data_dir,
        TT_GATEWAY_ID=GATEWAY_ID,
        # 控制面代理使用的管理令牌必须与网关一致（g4.start_server 注入的是 g4 token）
        TT_GATEWAY_ADMIN_TOKEN=g4.ADMIN_TOKEN,
        TT_GATEWAY_MTLS_MODE="off",
        TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE="600",
        TT_GATEWAY_ADMIN_RATE_LIMIT_BURST="200",
        TT_GATEWAY_AUDIT_WEBHOOK_URL=webhook_url,
        TT_GATEWAY_AUDIT_WEBHOOK_SECRET=WEBHOOK_SECRET,
        TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS="1",
        TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE="50",
    )
    return m2.start_gateway(
        port,
        control_plane=control_plane,
        data_dir=data_dir,
        log_path=log_path,
        env=env,
    )


def register_provider(server_base: str, admin_token: str, upstream_url: str) -> None:
    status, _, body = proxy(
        server_base,
        admin_token,
        "POST",
        "/providers",
        {
            "id": "openai_compatible",
            "label": "g8-fake",
            "baseUrl": upstream_url,
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    g3.check("经代理创建 provider", status in (200, 201), f"{status} {body}")

    status, _, body = proxy(
        server_base,
        admin_token,
        "POST",
        "/providers/openai_compatible/keys",
        {"label": "g8-provider-key", "secret": PROVIDER_SECRET},
    )
    key = body.get("key") if isinstance(body, dict) else None
    g3.check(
        "provider 密钥经代理写入且明文只出现一次",
        status == 201
        and isinstance(key, dict)
        and key.get("secret") == PROVIDER_SECRET
        and str(key.get("last4")) == PROVIDER_SECRET[-4:],
        f"{status} {body}",
    )

    status, _, body = proxy(server_base, admin_token, "GET", "/providers")
    g3.check(
        "provider 列表不含密钥明文",
        status == 200 and PROVIDER_SECRET not in json.dumps(body, ensure_ascii=False),
        f"{status} {str(body)[:300]}",
    )


# ── 验收段 ─────────────────────────────────────────────────


def check_role_matrix(world: dict) -> None:
    server_base = world["server_base"]
    for role in ("operator", "auditor", "admin"):
        status, _, body = proxy(server_base, world[role], "GET", "/health")
        g3.check(
            f"{role} 可读网关健康",
            status == 200
            and isinstance(body, dict)
            and body.get("gatewayId") == GATEWAY_ID,
            f"{status} {str(body)[:300]}",
        )

    status, _, body = proxy(server_base, world["user"], "GET", "/health")
    g3.check(
        "普通用户读网关管理 → 403 FORBIDDEN",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = proxy(
        server_base,
        world["auditor"],
        "POST",
        "/providers",
        {
            "id": "auditor-forbidden",
            "label": "auditor-forbidden",
            "baseUrl": "http://127.0.0.1:9/v1",
            "timeoutSeconds": 5,
            "allowPrivate": True,
        },
    )
    g3.check(
        "审计员写网关 → 403 FORBIDDEN（控制面拦截）",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = proxy(server_base, world["operator"], "GET", "/audit?after=0&limit=10")
    g3.check(
        "部门范围运维读审计 → 403 FORBIDDEN（M6 收窄）",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {str(body)[:300]}",
    )

    status, _, body = proxy(server_base, world["auditor"], "GET", "/audit?after=0&limit=10")
    g3.check(
        "审计员可读审计",
        status == 200 and isinstance(body, dict) and bool(body.get("entries")),
        f"{status} {str(body)[:300]}",
    )


def check_role_change(world: dict) -> None:
    server_base = world["server_base"]
    admin = world["admin"]

    status, _, body = server_api(
        server_base,
        "PATCH",
        f"/api/v1/admin/users/{world['admin_uid']}",
        admin,
        {"role": "user"},
    )
    g3.check(
        "管理员不能改自己的角色 → 400 SELF_ROLE_CHANGE",
        status == 400 and error_code(body) == "SELF_ROLE_CHANGE",
        f"{status} {body}",
    )

    status, _, body = server_api(
        server_base,
        "PATCH",
        f"/api/v1/admin/users/{world['operator_uid']}",
        world["user"],
        {"role": "admin"},
    )
    g3.check(
        "普通用户不能改角色 → 403 FORBIDDEN",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    payload = grant_role(
        server_base,
        admin,
        world["auditor_uid"],
        "operator",
        "管理员把审计员提为运维",
    )
    g3.check(
        "角色变更吊销旧会话",
        int(payload.get("sessionsRevoked") or 0) >= 1,
        str(payload),
    )
    status, _, _ = proxy(server_base, world["auditor"], "GET", "/health")
    g3.check("旧 auditor 令牌立即 401", status == 401, str(status))

    operator_token, _ = login(
        server_base, AUDITOR_EMAIL, USER_PASSWORD, "提升后重新登录（operator）"
    )
    status, _, body = proxy(server_base, operator_token, "GET", "/health")
    g3.check("新 operator 令牌可读", status == 200, f"{status} {body}")
    role_key = create_key(
        server_base,
        operator_token,
        label="g8-role-change",
        scopes=["chat"],
        allowlist=["auto", "fake-chat"],
        sub="g8-operator",
    )
    world["role_key"] = role_key
    # 被提升的是 auditor 账号，写操作审计应记这个 uid
    world["promoted_uid"] = world["auditor_uid"]

    grant_role(
        server_base,
        admin,
        world["auditor_uid"],
        "auditor",
        "恢复 auditor 角色",
    )
    status, _, _ = proxy(server_base, operator_token, "GET", "/health")
    g3.check("降级后旧 operator 令牌立即 401", status == 401, str(status))

    auditor_token, _ = login(
        server_base, AUDITOR_EMAIL, USER_PASSWORD, "恢复后重新登录（auditor）"
    )
    world["auditor"] = auditor_token
    status, _, body = proxy(server_base, auditor_token, "GET", "/health")
    g3.check("恢复后 auditor 可读", status == 200, f"{status} {body}")
    status, _, body = proxy(
        server_base, auditor_token, "POST", "/api-keys", {"label": "auditor-write"}
    )
    g3.check(
        "恢复后 auditor 写 → 403 FORBIDDEN",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def check_gateway_audit(world: dict) -> None:
    server_base = world["server_base"]
    auditor = world["auditor"]

    status, headers, body = proxy(
        server_base, auditor, "GET", "/audit?after=0&limit=1000"
    )
    entries = body.get("entries") if isinstance(body, dict) else None
    g3.check(
        "审计员分页读网关审计",
        status == 200 and isinstance(entries, list) and len(entries) >= 4,
        f"{status} {len(entries or [])}",
    )
    entries = entries or []
    ok, reason = verify_chain(entries)
    g3.check("网关审计链逐条可复算", ok, reason)
    g3.check(
        "headHash 等于最后一条",
        bool(entries) and body.get("headHash") == entries[-1].get("entryHash"),
        str(body.get("headHash")),
    )

    role_entry = next(
        (
            item
            for item in entries
            if item.get("action") == "api_key.create"
            and item.get("actor") == world["promoted_uid"]
        ),
        None,
    )
    g3.check(
        "角色变更后的写操作记 operator 本人",
        role_entry is not None and role_entry.get("actorRole") == "operator",
        str(role_entry),
    )
    provider_entry = next(
        (item for item in entries if item.get("action") == "provider.create"), None
    )
    g3.check(
        "provider 审计记管理员 uid / admin",
        provider_entry is not None
        and provider_entry.get("actor") == world["admin_uid"]
        and provider_entry.get("actorRole") == "admin",
        str(provider_entry),
    )

    status, _, body = proxy(server_base, auditor, "GET", "/audit/verify")
    g3.check(
        "网关验链 brokenAt 为空",
        status == 200
        and isinstance(body, dict)
        and body.get("ok") is True
        and body.get("brokenAt") is None,
        f"{status} {body}",
    )

    status, headers, body = proxy(server_base, auditor, "GET", "/audit/export?limit=1000")
    lines = [line for line in str(body or "").splitlines() if line.strip()]
    exported = []
    for line in lines:
        try:
            exported.append(json.loads(line))
        except ValueError:
            exported.append({})
    ok, reason = verify_chain(exported)
    g3.check(
        "代理导出 NDJSON 且逐行可复算",
        status == 200
        and headers.get("content-type", "").startswith("application/x-ndjson")
        and bool(lines)
        and ok,
        f"{status} lines={len(lines)} {reason}",
    )
    world["gateway_entries"] = entries

    status, headers, body = g3.http(
        world["gateway_base"],
        "GET",
        "/gateway/v1/admin/audit/export?limit=1000",
        headers={"X-TT-Admin-Token": g4.ADMIN_TOKEN},
    )
    direct = [
        json.loads(line) for line in str(body or "").splitlines() if line.strip()
    ]
    ok, reason = verify_chain(direct)
    g3.check(
        "直连导出带头哈希且链可复算",
        status == 200
        and headers.get("x-tt-audit-head", "") == (direct[-1].get("entryHash") if direct else "")
        and headers.get("x-tt-audit-next", "").isdigit()
        and ok,
        f"{status} head={headers.get('x-tt-audit-head', '')[:12]} {reason}",
    )


def check_control_plane_audit(world: dict) -> None:
    server_base = world["server_base"]
    status, _, body = server_api(
        server_base, "GET", "/api/v1/admin/audit?after=0&limit=1000", world["admin"]
    )
    entries = body.get("entries") if isinstance(body, dict) else None
    g3.check(
        "控制面审计分页",
        status == 200 and isinstance(entries, list) and len(entries) >= 3,
        f"{status} {len(entries or [])}",
    )
    entries = entries or []
    ok, reason = verify_chain(entries)
    g3.check("控制面审计链逐条可复算", ok, reason)

    role_entry = next(
        (item for item in entries if item.get("action") == "user.role"), None
    )
    g3.check(
        "角色变更入链（actor=admin）",
        role_entry is not None
        and role_entry.get("actor") == world["admin_uid"]
        and role_entry.get("actorRole") == "admin",
        str(role_entry),
    )
    proxy_entry = next(
        (
            item
            for item in entries
            if item.get("action") == "gateway.admin"
            and item.get("actor") == world["promoted_uid"]
        ),
        None,
    )
    g3.check(
        "代理写操作入链并记 operator 角色",
        proxy_entry is not None and proxy_entry.get("actorRole") == "operator",
        str(proxy_entry),
    )

    status, _, body = server_api(
        server_base, "GET", "/api/v1/admin/audit/verify", world["admin"]
    )
    g3.check(
        "控制面验链 brokenAt 为空",
        status == 200
        and isinstance(body, dict)
        and body.get("ok") is True
        and body.get("brokenAt") is None,
        f"{status} {body}",
    )

    status, headers, body = server_api(
        server_base,
        "GET",
        "/api/v1/admin/audit/export?limit=1000",
        world["admin"],
    )
    lines = [line for line in str(body or "").splitlines() if line.strip()]
    exported = []
    for line in lines:
        try:
            exported.append(json.loads(line))
        except ValueError:
            exported.append({})
    ok, reason = verify_chain(exported)
    g3.check(
        "控制面导出 NDJSON 且带头哈希",
        status == 200
        and headers.get("content-type", "").startswith("application/x-ndjson")
        and headers.get("x-tt-audit-head", "")
        == (exported[-1].get("entryHash") if exported else "")
        and ok,
        f"{status} lines={len(lines)} {reason}",
    )

    status, _, body = server_api(
        server_base, "GET", "/api/v1/admin/audit", world["auditor"]
    )
    g3.check("审计员可读控制面审计", status == 200 and bool(body.get("entries")), f"{status}")
    status, _, body = server_api(
        server_base, "GET", "/api/v1/admin/audit", world["user"]
    )
    g3.check(
        "普通用户读控制面审计 → 403",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def check_key_governance(world: dict) -> None:
    server_base = world["server_base"]
    gateway_base = world["gateway_base"]
    admin = world["admin"]
    world["key_secrets"] = [world["role_key"]["secret"]]

    base = create_key(
        server_base,
        admin,
        label="g8-governance",
        scopes=ALL_SCOPES,
        allowlist=["auto", "fake-chat"],
        sub="g8-governance",
    )
    world["key_secrets"].append(base.get("secret", ""))

    rate_key = create_key(
        server_base,
        admin,
        label="g8-rate",
        scopes=["chat"],
        allowlist=["auto", "fake-chat"],
        rpm=1,
        sub="g8-rate",
    )
    world["key_secrets"].append(rate_key.get("secret", ""))
    status, _, body = chat(gateway_base, rate_key["secret"])
    g3.check("RPM 桶内首次请求放行", status == 200 and message_text(body) == "你好", f"{status} {body}")
    status, headers, body = chat(gateway_base, rate_key["secret"])
    g3.check(
        "超出 per-key RPM → 429 RATE_LIMITED 且带 Retry-After",
        status == 429
        and error_code(body) == "RATE_LIMITED"
        and headers.get("retry-after", "").isdigit(),
        f"{status} {headers.get('retry-after')} {body}",
    )

    quota_key = create_key(
        server_base,
        admin,
        label="g8-quota",
        scopes=["chat"],
        allowlist=["auto", "fake-chat"],
        quota=20,
        sub="g8-quota",
    )
    world["key_secrets"].append(quota_key.get("secret", ""))
    status, _, body = chat(gateway_base, quota_key["secret"], max_tokens=1)
    g3.check("月配额内请求成功", status == 200 and message_text(body) == "你好", f"{status} {body}")
    status, _, body = chat(gateway_base, quota_key["secret"], max_tokens=1)
    g3.check(
        "超出 per-key 月配额 → 429 QUOTA_TOKENS_EXCEEDED",
        status == 429 and error_code(body) == "QUOTA_TOKENS_EXCEEDED",
        f"{status} {body}",
    )

    status, _, body = proxy(server_base, admin, "GET", "/api-keys")
    keys = body.get("keys") if isinstance(body, dict) else None
    quota_row = next(
        (item for item in keys or [] if item.get("id") == quota_key.get("id")), None
    )
    rate_row = next(
        (item for item in keys or [] if item.get("id") == rate_key.get("id")), None
    )
    g3.check(
        "列表展示配额/限流/已用且无明文与哈希",
        status == 200
        and quota_row is not None
        and rate_row is not None
        and quota_row.get("quotaMonthlyTokens") == 20
        and quota_row.get("usedTokens") == 20
        and rate_row.get("rateLimitRpm") == 1
        and "secret" not in quota_row
        and "secretHash" not in quota_row
        and "secret_hash" not in quota_row,
        f"{status} quota={quota_row} rate={rate_row}",
    )

    rotate_key = create_key(
        server_base,
        admin,
        label="g8-rotate",
        scopes=["chat"],
        allowlist=["auto", "fake-chat"],
        sub="g8-rotate",
    )
    world["key_secrets"].append(rotate_key.get("secret", ""))
    old_secret = rotate_key["secret"]
    status, _, _ = chat(gateway_base, old_secret)
    g3.check("轮换前 Key 可用", status == 200, str(status))
    status, _, body = proxy(
        server_base,
        world["operator"],
        "POST",
        f"/api-keys/{rotate_key['id']}/rotate",
    )
    rotated = body.get("key") if isinstance(body, dict) else None
    g3.check(
        "轮换返回同 keyId 与新明文",
        status == 200
        and isinstance(rotated, dict)
        and rotated.get("id") == rotate_key["id"]
        and rotated.get("secret") != old_secret
        and SECRET_PATTERN.fullmatch(str(rotated.get("secret") or "")) is not None
        and int(rotated.get("rotatedAt") or 0) > 0,
        f"{status} {body}",
    )
    if isinstance(rotated, dict) and rotated.get("secret"):
        world["key_secrets"].append(rotated["secret"])
    status, _, body = chat(gateway_base, old_secret)
    g3.check(
        "旧 secret 立即 401 AUTH_REQUIRED",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )
    status, _, body = chat(gateway_base, (rotated or {}).get("secret", ""))
    g3.check("新 secret 可用", status == 200 and message_text(body) == "你好", f"{status} {body}")

    status, _, body = proxy(
        server_base, world["auditor"], "POST", f"/api-keys/{rotate_key['id']}/rotate"
    )
    g3.check(
        "审计员不能轮换 Key → 403",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = proxy(
        server_base,
        admin,
        "POST",
        "/api-keys",
        {
            "label": "g8-invalid",
            "tenant": TENANT,
            "dept": DEPT,
            "sub": "g8-invalid",
            "scopes": ["chat"],
            "rateLimitRpm": 10001,
        },
    )
    g3.check(
        "RPM 越界 → 400 INVALID_REQUEST",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )


def check_webhook(world: dict, receiver: WebhookReceiver) -> None:
    server_base = world["server_base"]
    required_actions = {"provider.create", "api_key.create", "api_key.rotate"}

    def collected_events() -> list[dict]:
        events: list[dict] = []
        for item in receiver.records:
            events.extend((item["body"] or {}).get("events") or [])
        events.sort(key=lambda entry: int(entry.get("id") or 0))
        return events

    waited = wait_until(
        lambda: (
            collected_events()
            if len(collected_events()) >= 5
            and required_actions <= {entry.get("action") for entry in collected_events()}
            else None
        ),
        timeout=30,
    )
    g3.check("审计事件被外送", bool(waited), str(len(receiver.records)))

    records = receiver.records
    g3.check(
        "每批 HMAC-SHA256 签名可复算",
        bool(records) and all(item["valid"] for item in records),
        str([item["valid"] for item in records]),
    )
    g3.check(
        "外送头携带网关 ID",
        bool(records) and all(item["gatewayId"] == GATEWAY_ID for item in records),
        str([item["gatewayId"] for item in records]),
    )
    g3.check(
        "外送体 gatewayId 与事件列表",
        bool(records)
        and all(
            isinstance(item["body"], dict)
            and item["body"].get("gatewayId") == GATEWAY_ID
            and isinstance(item["body"].get("events"), list)
            and item["body"]["events"]
            for item in records
        ),
        str([(item["body"] or {}).get("gatewayId") for item in records]),
    )

    events: list[dict] = []
    for item in records:
        events.extend((item["body"] or {}).get("events") or [])
    events.sort(key=lambda entry: int(entry.get("id") or 0))
    g3.check("外送事件按审计 id 递增", bool(events), str(len(events)))
    ok = True
    broken = ""
    previous = ""
    for entry in events:
        if previous and entry.get("prevHash") != previous:
            ok = False
            broken = f"id={entry.get('id')} 断链"
            break
        if digest(entry) != entry.get("entryHash"):
            ok = False
            broken = f"id={entry.get('id')} 摘要不符"
            break
        previous = entry.get("entryHash")
    g3.check("外送事件逐条可复算且链连续", ok, broken)
    actions = {entry.get("action") for entry in events}
    g3.check(
        "外送覆盖 provider / Key / 轮换动作",
        {"provider.create", "api_key.create", "api_key.rotate"} <= actions,
        str(sorted(actions)),
    )

    def flushed() -> dict | None:
        status, _, body = proxy(server_base, world["admin"], "GET", "/health")
        if status != 200 or not isinstance(body, dict):
            return None
        health = body.get("auditWebhook") or {}
        if health.get("pending") == 0 and health.get("lastDeliveredAt"):
            return health
        return None

    health = wait_until(flushed, timeout=25)
    g3.check(
        "健康面显示外送已清空且无连续失败",
        isinstance(health, dict)
        and health.get("enabled") is True
        and health.get("pending") == 0
        and health.get("consecutiveFailures") == 0,
        str(health),
    )
    world["webhook_events"] = events


def check_secrets_absent(world: dict, receiver: WebhookReceiver) -> None:
    logs = ""
    for path in (world["server_log"], world["gateway_log"]):
        try:
            logs += Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            logs += ""
    outbox = "\n".join(item["raw"] for item in receiver.records)
    haystack = logs + "\n" + outbox
    secrets = [
        g4.ADMIN_TOKEN,
        g4.SERVICE_TOKEN,
        g3.MASTER_KEY,
        WEBHOOK_SECRET,
        PROVIDER_SECRET,
    ] + [value for value in world.get("key_secrets", []) if value]
    leaked = [value for value in secrets if value and value in haystack]
    g3.check(
        "日志与外送不含管理令牌、Webhook 密钥、provider 密钥与 Key 明文",
        not leaked,
        str(leaked),
    )

    g3.check(
        "管理令牌与 Webhook 密钥不在页面源码里",
        _page_clean(world["server_base"]),
        "页面静态资源含管理令牌",
    )


def _page_clean(server_base: str) -> bool:
    status, _, body = g3.http(server_base, "GET", "/ui/app.js")
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return status == 200 and g4.ADMIN_TOKEN not in text and WEBHOOK_SECRET not in text


def section(name: str, fn, *args) -> Step:
    before = len(g3.FAILURES)
    began = time.perf_counter()
    try:
        fn(*args)
        detail = "全部断言通过"
    except Exception as exc:  # noqa: BLE001 - 验收要把异常落进报告
        traceback.print_exc()
        g3.FAILURES.append(f"  NG  {name} — {type(exc).__name__}: {exc}")
        detail = f"{type(exc).__name__}: {exc}"
    new_failures = g3.FAILURES[before:]
    if new_failures:
        detail = "；".join(new_failures)
    return Step(name, not new_failures, time.perf_counter() - began, detail)


def unit_suite(name: str, path: Path, python: str, cwd: Path, timeout: float = 900) -> Step:
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [python, str(path)],
            cwd=str(cwd),
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


def run_production_flow() -> list[Step]:
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g8-"))
    upstream = g3.FakeUpstream("openai").start()
    receiver = WebhookReceiver().start()
    server_port = g3.free_port()
    gateway_port = g3.free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    server = None
    gateway = None
    try:
        began = time.perf_counter()
        server = g4.start_server(
            server_port, temp / "server", temp / "server.log", gateway_base
        )
        world = {
            "server_base": server_base,
            "gateway_base": gateway_base,
            "server_log": temp / "server.log",
            "gateway_log": temp / "gateway.log",
        }
        world.update(setup_control_plane(server_base, gateway_base))
        gateway = start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=temp / "gateway",
            log_path=temp / "gateway.log",
            webhook_url=receiver.url,
        )
        register_provider(server_base, world["admin"], f"{upstream.base_url}/v1")
        create_key(
            server_base,
            world["admin"],
            label="g8-admin-key",
            scopes=ALL_SCOPES,
            allowlist=["auto", "fake-chat"],
            sub="g8-admin",
        )
        steps.append(
            Step(
                "生产配置启动（RS256 + 控制面策略 + 审计外送）",
                True,
                time.perf_counter() - began,
                "控制面、策略、网关、假上游与 Webhook 接收端均就绪",
            )
        )

        steps.append(section("角色权限（控制面代理）", check_role_matrix, world))
        steps.append(section("角色变更吊销会话", check_role_change, world))
        steps.append(section("网关审计链与导出", check_gateway_audit, world))
        steps.append(section("控制面审计链与导出", check_control_plane_audit, world))
        steps.append(section("per-key 限流、配额与轮换", check_key_governance, world))
        steps.append(section("审计外送 HMAC", check_webhook, world, receiver))
        steps.append(section("敏感值不落日志与外送", check_secrets_absent, world, receiver))
    except Exception as exc:  # noqa: BLE001 - 启动或准备失败也要出报告
        traceback.print_exc()
        g3.FAILURES.append(f"  NG  生产模式端到端 — {type(exc).__name__}: {exc}")
        steps.append(
            Step(
                "生产模式端到端",
                False,
                time.perf_counter() - began,
                f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        if gateway is not None:
            gateway.stop()
        if server is not None:
            server.stop()
        receiver.stop()
        upstream.stop()
    return steps


def write_report(steps: list[Step]) -> None:
    total = len(steps)
    passed = sum(1 for step in steps if step.ok)
    verdict = "通过" if passed == total else "未通过"
    lines = [
        "# M4 验收报告（G8：治理与审计）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / 单副本单 worker",
        f"- 结论：**G8 {verdict}**（{passed}/{total} 个步骤通过）",
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
            "- 管理角色：控制面 `user.role` 快照进 JWT；代理下发 `X-TT-Admin-Roles`，",
            "  读 = admin/operator/auditor，写 = admin/operator；审计员与普通用户的写/读越权",
            "  均在控制面被拦截。M6 起带部门范围的 operator 另受 `X-TT-Admin-Depts` 收窄，",
            "  不能读取全局审计（`403`），审计读仍由全局 auditor 覆盖。",
            "  改角色必须由管理员操作、不能改自己，并吊销全部旧会话。",
            "- 审计哈希链：`entry_hash = sha256(prev_hash\\n at_ms\\n actor\\n actor_role\\n",
            "  action\\n object\\n before_json\\n after_json)`，首条 `prev_hash` 为 64 个 0；",
            "  两端验链接口返回 `brokenAt=null`，NDJSON 导出逐行可复算，网关直连导出带",
            "  `X-TT-Audit-Head` / `X-TT-Audit-Next`。",
            "- 审计外送：审计写入与 outbox 入队同事务；后台按批推送，HMAC-SHA256 覆盖",
            "  `timestamp.body`，头带 `X-TT-Audit-Signature` / `X-TT-Audit-Timestamp` /",
            "  `X-TT-Gateway-Id`；2xx 记为已投递，4xx（除 408/429）跳过，5xx/429/网络错误",
            "  指数退避（上限 300 秒）——重试与跳过细节由 `tests/test_m4_webhook.py` 覆盖。",
            "  健康面 `auditWebhook` 显示 pending / consecutiveFailures / lastDeliveredAt。",
            "- Key 治理：`rateLimitRpm` 令牌桶（0 不限，超限 429 `RATE_LIMITED` +",
            "  `Retry-After`）；`quotaMonthlyTokens` 月配额（超限 429",
            "  `QUOTA_TOKENS_EXCEEDED`）；轮换保持同一 keyId、换新 secret，旧 secret 下一次",
            "  请求立即 401，审计记 `api_key.rotate` 且不含明文；列表带 `rotatedAt` /",
            "  `usedTokens`。",
            "- 安全边界：管理令牌只存在控制面进程环境，浏览器只经代理；provider 密钥一次性",
            "  返回，日志、审计与外送载荷均不含管理令牌、Webhook 密钥、provider 密钥与 Key",
            "  明文；`/ui/` 静态资源不含任何服务端密钥。",
            "",
            "## 说明与残余",
            "",
            "- G8 网关管理面使用 `TT_GATEWAY_MTLS_MODE=off`：控制面代理不转发",
            "  `X-TT-Client-Verify` / `X-TT-Client-DN`。mTLS 本身由 G6（代理模式）与 G7",
            "  （直连三协议面）覆盖，生产部署仍应启用 `proxy` 模式并把网关管理端口限制在",
            "  控制面网段。",
            "- 单副本 + SQLite；`audit_outbox` 为至少一次投递，接收端应按 `gatewayId + id`",
            "  去重。多副本需要把 outbox 与限流/配额计数迁到共享存储（Redis 接口已预留）。",
            "- 角色变更即时生效依赖每请求校验会话；跨节点部署需共享会话吊销表。",
            "- Key 轮换不会自动通知调用方，旧 secret 立即失效，需调用方先换后用。",
            "- Webhook 签名不包含重放窗口校验，接收端可用 `X-TT-Audit-Timestamp` 自行判重。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    steps: list[Step] = []
    suites = [
        ("tests/test_m4_roles.py", GATEWAY_ROOT, PYTHON),
        ("tests/test_m4_audit.py", GATEWAY_ROOT, PYTHON),
        ("tests/test_m4_webhook.py", GATEWAY_ROOT, PYTHON),
        ("tests/test_m4_governance.py", GATEWAY_ROOT, PYTHON),
        ("tests/test_m4_audit.py（控制面）", SERVER_ROOT, SERVER_PYTHON),
    ]
    for index, (name, cwd, python) in enumerate(suites, start=1):
        print(f"[{index}/{len(suites) + 1}] {name}")
        step = unit_suite(name, cwd / name.split("（")[0], python, cwd)
        steps.append(step)
        print(_tail(step.output))

    print(f"\n[{len(suites) + 1}/{len(suites) + 1}] 生产模式端到端")
    for step in run_production_flow():
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

    write_report(steps)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG8 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = _tail(step.output)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG8 验收通过：角色、审计链、外送、Key 治理与两端用例全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
