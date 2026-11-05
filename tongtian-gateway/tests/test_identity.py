"""M1.3 网关身份自测：RS256 + JWKS + 控制面策略（标准库 + uvicorn）。

独立于 tests/support.py：support 在导入时固化 hs256-dev，本文件自带一套
rs256 环境、假控制面与 RSA 密钥，覆盖 T3.10–T3.13 与 G3 的网关侧行为。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

SERVICE_TOKEN = "gw-service-token-e2e"
ADMIN_TOKEN = "test-admin-token"
ISSUER = "tongtian-auth"
AUDIENCE = "tongtian-gateway"
TENANT = "acme"
KEY1_ID = "kid-2026-10-a"
KEY2_ID = "kid-2026-10-b"
KEY3_ID = "kid-2026-10-c"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
        return
    message = f"FAIL {name}" + (f" — {detail}" if detail else "")
    print(message)
    FAILURES.append(message)


def report(ok_message: str) -> int:
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(ok_message)
    return 0


# ── 假控制面 ────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_int(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def public_jwk(kid: str, private: rsa.RSAPrivateKey) -> dict:
    numbers = private.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_int(numbers.n),
        "e": _b64url_int(numbers.e),
    }


def policy_document(version: str) -> dict:
    sales_models = ["auto", "basic", "chat-a", "embedding-default", "embed"]
    if version.endswith("#2"):
        sales_models = [
            "auto",
            "advanced",
            "basic",
            "chat-a",
            "chat-b",
            "embedding-default",
            "embed",
        ]
    return {
        "contractVersion": "gateway-contract.v1",
        "policyVersion": version,
        "version": version,
        "tenant": TENANT,
        "gateway": {"url": "http://127.0.0.1:8001"},
        "noLog": True,
        "embeddingRoute": "embedding-default",
        "byok": {"allowed": True, "hardQuota": False},
        "models": {
            "chat-a": {
                "provider": "openai_compatible",
                "upstreamModel": "chat-a-upstream",
                "capabilities": ["chat", "tools", "vision"],
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                "priceVersion": "test-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "1.00",
                    "outputPer1M": "2.00",
                },
            },
            "chat-b": {
                "provider": "openai_compatible",
                "upstreamModel": "chat-b-upstream",
                "capabilities": ["chat"],
                "maxOutputTokens": 4096,
                "priceVersion": "test-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "1.00",
                    "outputPer1M": "2.00",
                },
            },
            "embed": {
                "provider": "openai_compatible",
                "upstreamModel": "embed-upstream",
                "capabilities": ["embeddings"],
                "priceVersion": "test-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "0.10",
                    "outputPer1M": "0",
                },
            },
        },
        "routes": {
            "auto": {"chain": ["chat-a"], "fallbackAllowed": True},
            "advanced": {"chain": ["chat-b"], "fallbackAllowed": False},
            "basic": {"chain": ["chat-a"], "fallbackAllowed": True},
            "embedding-default": {"chain": ["embed"], "fallbackAllowed": False},
        },
        "allowedModels": [
            "auto",
            "advanced",
            "basic",
            "embedding-default",
            "chat-a",
            "chat-b",
            "embed",
        ],
        "quotaDefaults": {
            "monthlyTokens": 1000000,
            "concurrency": 4,
            "maxOutputTokens": 4096,
        },
        "departments": {
            "dept-sales": {"allowedModels": sales_models},
            "dept-eng": {
                "allowedModels": [
                    "auto",
                    "advanced",
                    "basic",
                    "chat-a",
                    "chat-b",
                    "embedding-default",
                    "embed",
                ]
            },
        },
        "issuedAt": "2026-10-30T00:00:00Z",
        "expiresAt": "2027-10-30T00:00:00Z",
    }


class ControlPlane:
    """控制面替身：JWKS 与策略分发，可切换故障注入。"""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.policy_version = "acme#1"
        self.jwks_down = False
        self.jwks_broken = False
        self.policy_down = False
        self.policy_missing = False
        self.jwks_requests = 0
        self.policy_requests: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-control-plane", daemon=True
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.base}/.well-known/jwks.json"

    def start(self) -> "ControlPlane":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - 静默访问日志
        return

    @property
    def state(self) -> ControlPlane:
        return self.server.state  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parts = urlsplit(self.path)
        if parts.path == "/.well-known/jwks.json":
            self._serve_jwks()
            return
        if parts.path == "/api/v1/gateway/policy":
            self._serve_policy(parts.query)
            return
        self._json(404, {"error": "not found"})

    def _serve_jwks(self) -> None:
        state = self.state
        state.jwks_requests += 1
        if state.jwks_down:
            self._json(500, {"error": "jwks down"})
            return
        if state.jwks_broken:
            self._json(200, {"keys": "not-a-list"})
            return
        self._json(
            200,
            {"keys": [public_jwk(kid, key) for kid, key in state.keys.items()]},
        )

    def _serve_policy(self, query: str) -> None:
        state = self.state
        tenant = (parse_qs(query).get("tenant") or [""])[0]
        state.policy_requests.append(
            {
                "tenant": tenant,
                "if_none_match": self.headers.get("if-none-match", ""),
                "auth": self.headers.get("authorization", ""),
            }
        )
        if state.policy_down:
            self._json(503, {"error": "policy down"})
            return
        if self.headers.get("authorization", "") != f"Bearer {SERVICE_TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return
        if tenant != TENANT or state.policy_missing:
            self._json(404, {"error": "unknown tenant"})
            return
        etag = f'"{state.policy_version}"'
        if self.headers.get("if-none-match", "") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(200, policy_document(state.policy_version), {"ETag": etag})


# ── 令牌与 HTTP 工具 ───────────────────────────────────────


def _claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "sub": "u-1",
        "sid": "s-1",
        "tenant": TENANT,
        "dept": "dept-sales",
        "depts": ["dept-sales", "dept-eng"],
        "roles": ["member"],
        "type": "access",
        "iss": ISSUER,
        "aud": [AUDIENCE],
        "iat": now,
        "exp": now + 600,
    }
    base.update(overrides)
    return base


def sign(
    claims: dict,
    *,
    kid: str | None = KEY1_ID,
    private: rsa.RSAPrivateKey | None = None,
    alg: str = "RS256",
) -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    header_part = _b64url(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_part = _b64url(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    if alg == "none":
        return f"{header_part}.{payload_part}."
    assert private is not None
    signature = private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_part}.{payload_part}.{_b64url(signature)}"


def bearer(
    *,
    kid: str = KEY1_ID,
    private: rsa.RSAPrivateKey | None = None,
    **overrides,
) -> dict[str, str]:
    return {"Authorization": f"Bearer {sign(_claims(**overrides), kid=kid, private=private)}"}


def _decode(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


BASE = ""


def http(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
):
    request = urllib.request.Request(
        BASE + path, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                _decode(response.read()),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            _decode(exc.read()),
        )


def error_code(call) -> str | None:
    try:
        call()
    except GatewayError as exc:
        return exc.code
    return None


def expect(
    name: str,
    status: int,
    body,
    expected_status: int,
    expected_code: str,
    headers: dict | None = None,
) -> None:
    code = ""
    if isinstance(body, dict):
        code = str(((body.get("error") or {}).get("code") or ""))
    ok = status == expected_status and code == expected_code
    detail = f"status={status} code={code} body={body}"
    if headers is not None:
        ok = ok and bool(headers.get("x-tt-request-id"))
    check(name, ok, detail)


def model_ids(body) -> set[str]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return set()
    return {str(item.get("id")) for item in body["data"] if isinstance(item, dict)}


# ── 服务启动 ───────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_gateway():
    global BASE
    import uvicorn

    from app.main import app

    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="gateway", daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            status, _, _ = http("GET", "/health", timeout=1)
            if status == 200:
                return server
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.05)
    raise RuntimeError("网关未能在 10 秒内启动")


def stop_gateway(server) -> None:
    server.should_exit = True
    deadline = time.time() + 10
    while server.started and time.time() < deadline:
        time.sleep(0.05)


# ── 测试 ───────────────────────────────────────────────────


def test_mode() -> None:
    check("测试以 RS256 模式运行", settings.jwt_mode == "rs256", settings.jwt_mode)
    status, _, body = http("GET", "/health")
    check("健康检查", status == 200 and body.get("ok") is True, str(body))
    status, _, body = http("GET", "/gateway/v1/capabilities")
    names = sorted(item.get("name") for item in (body.get("protocols") or []))
    check(
        "能力发现包含三种协议",
        status == 200 and names == ["anthropic", "gemini", "openai"],
        str(names),
    )


def test_rs256_access() -> None:
    status, headers, body = http("GET", "/v1/models", headers=bearer(private=KEY1))
    check("RS256 令牌通过鉴权", status == 200, str(body))
    check("协议响应带请求 id", bool(headers.get("x-tt-request-id")), str(headers))
    check(
        "dept-sales 只看到本部门模型",
        model_ids(body) == {"auto", "basic", "chat-a"},
        str(sorted(model_ids(body))),
    )
    check(
        "策略拉取带服务令牌与租户",
        any(
            item["tenant"] == TENANT and item["auth"] == f"Bearer {SERVICE_TOKEN}"
            for item in CONTROL.policy_requests
        ),
        str(CONTROL.policy_requests[-3:]),
    )


def test_department_scope() -> None:
    headers = {**bearer(private=KEY1), "X-TT-Department": "dept-eng"}
    status, _, body = http("GET", "/v1/models", headers=headers)
    check(
        "切换到自己加入的部门",
        status == 200 and {"advanced", "chat-b"} <= model_ids(body),
        f"{status} {sorted(model_ids(body))}",
    )
    headers["X-TT-Department"] = "dept-ops"
    status, headers_out, body = http("GET", "/v1/models", headers=headers)
    expect("未加入的部门被拒绝", status, body, 403, "FORBIDDEN", headers_out)


def test_token_rejections() -> None:
    now = int(time.time())
    cases = [
        ("alg=none", sign(_claims(), alg="none"), 401, "AUTH_REQUIRED"),
        ("缺少 kid", sign(_claims(), kid=None, private=KEY1), 401, "AUTH_REQUIRED"),
        (
            "过期令牌",
            sign(_claims(iat=now - 1000, exp=now - 100), private=KEY1),
            401,
            "TOKEN_EXPIRED",
        ),
        (
            "有效期超过 900 秒",
            sign(_claims(exp=now + 901), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        (
            "tenant 与 org 冲突",
            sign(_claims(org="globex"), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        (
            "dept 不在 depts 内",
            sign(_claims(depts=["dept-eng"]), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        (
            "缺少 dept",
            sign(_claims(dept="", depts=[]), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        (
            "缺少 roles",
            sign(_claims(roles=[], role=""), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        (
            "类型不是 access",
            sign(_claims(type="refresh"), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
        ("受众不匹配", sign(_claims(aud=["other-api"]), private=KEY1), 401, "AUTH_REQUIRED"),
        (
            "签发方不匹配",
            sign(_claims(iss="evil-issuer"), private=KEY1),
            401,
            "AUTH_REQUIRED",
        ),
    ]
    for name, token, expected_status, expected_code in cases:
        status, _, body = http(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {token}"}
        )
        expect(f"拒绝{name}", status, body, expected_status, expected_code)

    from app.auth import encode_dev_hs256

    hs256 = encode_dev_hs256(_claims(), settings.dev_hs256_secret)
    status, _, body = http(
        "GET", "/v1/models", headers={"Authorization": f"Bearer {hs256}"}
    )
    expect("RS256 模式下拒绝 HS256", status, body, 401, "AUTH_REQUIRED")

    status, _, body = http("GET", "/v1/models")
    expect("缺少凭据", status, body, 401, "AUTH_REQUIRED")


def test_jwks_rotation() -> None:
    before = CONTROL.jwks_requests
    CONTROL.keys = {KEY1_ID: KEY1, KEY2_ID: KEY2}
    status, _, body = http(
        "GET", "/v1/models", headers=bearer(kid=KEY2_ID, private=KEY2)
    )
    check(
        "未知 kid 触发 JWKS 刷新",
        status == 200 and CONTROL.jwks_requests > before,
        f"status={status} requests={CONTROL.jwks_requests - before}",
    )

    CONTROL.jwks_down = True
    status, _, body = http("GET", "/v1/models", headers=bearer(private=KEY1))
    check("JWKS 不可达时仍可用缓存公钥", status == 200, str(body))

    status, _, body = http(
        "GET", "/v1/models", headers=bearer(kid=KEY3_ID, private=KEY3)
    )
    expect("未知 kid 且 JWKS 不可用", status, body, 503, "AUTH_UNAVAILABLE")
    CONTROL.jwks_down = False


def test_policy_lifecycle() -> None:
    time.sleep(1.1)
    status, headers, body = http("GET", "/v1/models", headers=bearer(private=KEY1))
    check(
        "策略条件请求命中 304",
        status == 200 and not headers.get("x-tt-policy-stale"),
        f"status={status} stale={headers.get('x-tt-policy-stale')}",
    )
    check(
        "按 policyVersion 发送 If-None-Match",
        any(
            item["if_none_match"] == '"acme#1"'
            for item in CONTROL.policy_requests[-4:]
        ),
        str(CONTROL.policy_requests[-4:]),
    )

    CONTROL.policy_version = "acme#2"
    time.sleep(1.1)
    status, _, body = http("GET", "/v1/models", headers=bearer(private=KEY1))
    check(
        "策略版本更新后生效",
        status == 200 and {"advanced", "chat-b"} <= model_ids(body),
        f"{status} {sorted(model_ids(body))}",
    )

    CONTROL.policy_down = True
    time.sleep(1.1)
    status, headers, body = http("GET", "/v1/models", headers=bearer(private=KEY1))
    check(
        "控制面不可达时使用宽限缓存",
        status == 200 and headers.get("x-tt-policy-stale") == "1",
        f"status={status} stale={headers.get('x-tt-policy-stale')}",
    )
    CONTROL.policy_down = False

    status, headers_out, body = http(
        "GET", "/v1/models", headers=bearer(private=KEY1, tenant="globex")
    )
    expect("未知租户被拒绝", status, body, 403, "FORBIDDEN", headers_out)


def test_jwks_client_unit() -> None:
    CONTROL.jwks_down = False
    CONTROL.jwks_broken = False
    CONTROL.keys = {KEY1_ID: KEY1}

    client = JwksClient(CONTROL.jwks_url, ttl_seconds=0, min_refresh_seconds=0)
    key = client.get_key(KEY1_ID)
    check("JWKS 首次拉取并解析 RSA 公钥", key.key_size == 2048, str(key.key_size))

    CONTROL.keys = {KEY2_ID: KEY2}
    key = client.get_key(KEY2_ID)
    check(
        "JWKS 未知 kid 立即刷新",
        key.public_numbers().n == KEY2.public_key().public_numbers().n,
    )
    check(
        "刷新成功但仍无该 kid",
        error_code(lambda: client.get_key("kid-missing")) == "AUTH_REQUIRED",
    )

    CONTROL.jwks_down = True
    check(
        "JWKS 不可用且无该 kid",
        error_code(lambda: client.get_key("kid-missing")) == "AUTH_UNAVAILABLE",
    )
    CONTROL.jwks_down = False

    CONTROL.jwks_broken = True
    broken = JwksClient(CONTROL.jwks_url, ttl_seconds=0, min_refresh_seconds=0)
    check(
        "JWKS 载荷损坏时降级为不可用",
        error_code(lambda: broken.get_key(KEY1_ID)) == "AUTH_UNAVAILABLE",
    )
    CONTROL.jwks_broken = False
    CONTROL.keys = {KEY1_ID: KEY1}


def test_policy_client_unit() -> None:
    CONTROL.policy_down = False
    unit_dir = TEST_DIR / "unit"
    unit_dir.mkdir(parents=True, exist_ok=True)

    settings_ok = replace(
        settings,
        control_plane_url=CONTROL.base,
        service_token=SERVICE_TOKEN,
        policy_ttl_seconds=1,
        policy_stale_grace_seconds=86400,
    )
    db = Database(unit_dir / "policy.db")
    try:
        client = PolicyClient(settings_ok, db)
        state = client.get(TENANT)
        check(
            "策略客户端拉取并带版本",
            state.policy_version == CONTROL.policy_version and not state.stale,
            f"{state.policy_version} stale={state.stale}",
        )
        time.sleep(1.1)
        CONTROL.policy_down = True
        state = client.get(TENANT)
        check("宽限期内返回 stale 快照", state.stale is True, str(state.stale))
        CONTROL.policy_down = False
        CONTROL.policy_missing = True
        check(
            "控制面明确无策略时 403 而不是吃旧缓存",
            error_code(lambda: client.get(TENANT)) == "FORBIDDEN",
        )
    finally:
        db.close()
        CONTROL.policy_down = False
        CONTROL.policy_missing = False

    strict = replace(settings_ok, policy_stale_grace_seconds=0)
    db2 = Database(unit_dir / "strict.db")
    CONTROL.policy_down = True
    try:
        strict_client = PolicyClient(strict, db2)
        check(
            "无缓存且控制面不可达时 POLICY_STALE",
            error_code(lambda: strict_client.get(TENANT)) == "POLICY_STALE",
        )
    finally:
        db2.close()
        CONTROL.policy_down = False


def main() -> int:
    server = start_gateway()
    try:
        test_mode()
        test_rs256_access()
        test_department_scope()
        test_token_rejections()
        test_jwks_rotation()
        test_policy_lifecycle()
        test_jwks_client_unit()
        test_policy_client_unit()
    finally:
        stop_gateway(server)
        CONTROL.stop()
    return report("全部身份与策略测试通过")


CONTROL = ControlPlane().start()
TEST_DIR = Path(tempfile.mkdtemp(prefix="tt-gateway-identity-"))

# 必须先于任何 app.* 导入：Settings 在 import 时固化。
os.environ["TT_GATEWAY_ENV"] = "development"
os.environ["TT_GATEWAY_JWT_MODE"] = "rs256"
os.environ["TT_GATEWAY_JWKS_URL"] = CONTROL.jwks_url
os.environ["TT_GATEWAY_CONTROL_PLANE_URL"] = CONTROL.base
os.environ["TT_GATEWAY_SERVICE_TOKEN"] = SERVICE_TOKEN
os.environ["TT_GATEWAY_POLICY_TTL_SECONDS"] = "1"
os.environ["TT_GATEWAY_POLICY_STALE_GRACE_SECONDS"] = "86400"
os.environ["TT_GATEWAY_DATA_DIR"] = str(TEST_DIR / "data")
os.environ["TT_GATEWAY_MASTER_KEY"] = base64.b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode()
os.environ["TT_GATEWAY_MASTER_KEY_VERSION"] = "1"
os.environ["TT_GATEWAY_ADMIN_TOKEN"] = ADMIN_TOKEN
os.environ["TT_GATEWAY_ID"] = "gw-identity-01"
os.environ.pop("TT_GATEWAY_POLICY_FILE", None)

from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import GatewayError  # noqa: E402
from app.jwks import JwksClient, client_for  # noqa: E402
from app.policy_client import PolicyClient  # noqa: E402

KEY1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY3 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CONTROL.keys = {KEY1_ID: KEY1}
client_for(settings).prime({KEY1_ID: KEY1.public_key()})


if __name__ == "__main__":
    raise SystemExit(main())
