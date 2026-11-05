"""测试脚手架：环境引导、HTTP/SSE 工具、假上游与网关线程生命周期。"""

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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SECRET = "test-secret"
ADMIN_TOKEN = "test-admin-token"
GATEWAY_ID = "gw-test-01"
MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
TEST_DIR = Path(tempfile.mkdtemp(prefix="tt-gateway-test-"))
POLICY_FILE = TEST_DIR / "policy.json"

TEST_POLICY: dict = {
    "contractVersion": "gateway-contract.v1",
    "policyVersion": "dev#1",
    "version": "dev#1",
    "tenant": "acme",
    "gateway": {"url": "http://127.0.0.1:8001"},
    "noLog": True,
    "embeddingRoute": "embedding-default",
    "byok": {"allowed": True, "hardQuota": False},
    "models": {
        "fake-chat": {
            "provider": "openai_compatible",
            "upstreamModel": "fake-openai-model",
            "capabilities": ["chat", "tools", "vision"],
            "contextWindow": 32768,
            "maxOutputTokens": 4096,
            "priceVersion": "test-2026-10",
            "pricing": {
                "currency": "USD",
                "inputPer1M": "1.00",
                "outputPer1M": "2.00",
                "cachedInputPer1M": "0.50",
                "reasoningAsOutput": True,
            },
        },
        "fake-embed": {
            "provider": "openai_compatible",
            "upstreamModel": "fake-embedding",
            "capabilities": ["embeddings"],
            "contextWindow": 8191,
            "priceVersion": "test-2026-10",
            "pricing": {
                "currency": "USD",
                "inputPer1M": "0.10",
                "outputPer1M": "0",
                "reasoningAsOutput": False,
            },
        },
        "missing-model": {
            "provider": "bailian",
            "upstreamModel": "missing-model",
            "capabilities": ["chat"],
            "maxOutputTokens": 4096,
        },
    },
    "routes": {
        "auto": {"chain": ["missing-model", "fake-chat"], "fallbackAllowed": True},
        "advanced": {"chain": ["fake-chat"], "fallbackAllowed": False},
        "basic": {"chain": ["fake-chat"], "fallbackAllowed": True},
        "economy": {"chain": ["fake-chat"], "fallbackAllowed": False},
        "embedding-default": {"chain": ["fake-embed"], "fallbackAllowed": False},
    },
    "allowedModels": [
        "auto",
        "advanced",
        "basic",
        "economy",
        "embedding-default",
        "fake-chat",
        "fake-embed",
        "missing-model",
    ],
    "quotaDefaults": {
        "monthlyTokens": 1000000,
        "concurrency": 4,
        "maxOutputTokens": 4096,
    },
    "departments": {
        "dept-sales": {
            "allowedModels": [
                "auto",
                "advanced",
                "basic",
                "economy",
                "embedding-default",
                "fake-chat",
                "fake-embed",
                "missing-model",
            ],
            "quota": {
                "monthlyTokens": 500000,
                "concurrency": 4,
                "maxOutputTokens": 1024,
            },
        },
        "dept-eng": {
            "allowedModels": [
                "auto",
                "advanced",
                "basic",
                "economy",
                "embedding-default",
                "fake-chat",
                "fake-embed",
                "missing-model",
            ],
            "quota": {"monthlyTokens": 5000000, "concurrency": 4},
        },
    },
    "issuedAt": "2026-10-30T00:00:00Z",
    "expiresAt": "2027-10-30T00:00:00Z",
}
POLICY_FILE.write_text(
    json.dumps(TEST_POLICY, ensure_ascii=False, indent=2), encoding="utf-8"
)

# 必须先于任何 app.* 导入：Settings 在 import 时固化。
os.environ["TT_GATEWAY_ENV"] = "development"
os.environ["TT_GATEWAY_JWT_MODE"] = "hs256-dev"
os.environ["TT_GATEWAY_DEV_HS256_SECRET"] = SECRET
os.environ["TT_GATEWAY_ADMIN_TOKEN"] = ADMIN_TOKEN
os.environ["TT_GATEWAY_ID"] = GATEWAY_ID
os.environ["TT_GATEWAY_DATA_DIR"] = str(TEST_DIR / "data")
os.environ["TT_GATEWAY_MASTER_KEY"] = MASTER_KEY
os.environ["TT_GATEWAY_MASTER_KEY_VERSION"] = "1"
os.environ["TT_GATEWAY_POLICY_FILE"] = str(POLICY_FILE)
os.environ["TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS"] = "1"

from app.auth import Principal, encode_dev_hs256  # noqa: E402
from app.errors import GatewayError  # noqa: E402
from tests.fake_upstreams import FakeUpstream  # noqa: E402

BASE = ""
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


def _decode(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http(
    method: str,
    path: str,
    *,
    base: str | None = None,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 10,
):
    url = (base or BASE) + path
    data = None
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=merged, method=method)
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "u-1",
        "sid": "s-1",
        "tenant": "acme",
        "dept": "dept-sales",
        "depts": ["dept-sales", "dept-eng"],
        "roles": ["member"],
        "type": "access",
        "iss": "tongtian-auth",
        "aud": ["tongtian-gateway"],
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return encode_dev_hs256(claims, SECRET)


def bearer(**overrides) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(**overrides)}"}


def error_code(call) -> str | None:
    try:
        call()
    except GatewayError as exc:
        return exc.code
    return None


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = {"event": None, "data": ""}
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event["event"] = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        event["data"] = "\n".join(data_lines)
        events.append(event)
    return events


def principal(dept: str = "dept-sales") -> Principal:
    return Principal(
        subject="u-1",
        session_id="s-1",
        tenant="acme",
        department=dept,
        departments=(dept,),
        roles=("member",),
        auth_kind="jwt",
    )


def start_gateway() -> Any:
    """在后台线程启动 app.main:app，并把默认请求地址写入 BASE。"""
    global BASE
    import uvicorn

    from app.main import app

    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
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


def stop_gateway(server: Any) -> None:
    server.should_exit = True
    deadline = time.time() + 10
    while server.started and time.time() < deadline:
        time.sleep(0.05)


def start_fake_upstreams(
    kinds: tuple[str, ...] = ("openai", "anthropic", "gemini"),
) -> dict[str, FakeUpstream]:
    return {kind: FakeUpstream(kind).start() for kind in kinds}


def stop_fake_upstreams(upstreams: dict[str, FakeUpstream]) -> None:
    for upstream in upstreams.values():
        upstream.stop()


def admin_headers() -> dict[str, str]:
    return {"X-TT-Admin-Token": ADMIN_TOKEN}


def create_provider(
    base_url: str,
    *,
    provider_id: str = "openai_compatible",
    allow_private: bool = True,
    api_version: str | None = None,
    secret: str = "sk-test-provider-secret-1234",
) -> dict:
    payload = {
        "id": provider_id,
        "label": f"test-{provider_id}",
        "baseUrl": base_url,
        "timeoutSeconds": 30,
        "allowPrivate": allow_private,
    }
    if api_version:
        payload["apiVersion"] = api_version
    status, _, body = http(
        "POST", "/gateway/v1/admin/providers", headers=admin_headers(), body=payload
    )
    if status not in (200, 201):
        raise RuntimeError(f"创建 provider 失败：{status} {body}")
    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/providers/{provider_id}/keys",
        headers=admin_headers(),
        body={"label": "test-key", "secret": secret},
    )
    if status != 201:
        raise RuntimeError(f"创建 provider 密钥失败：{status} {body}")
    return body


def configure_openai_provider(upstream: FakeUpstream) -> dict:
    return create_provider(f"{upstream.base_url}/v1")
