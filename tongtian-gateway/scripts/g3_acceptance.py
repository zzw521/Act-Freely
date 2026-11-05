#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3 验收：关掉 HS256，经网关完成对话；越权 403；换 kid 后继续服务。

三段式验收，全部走真实进程与真实 HTTP：
- 控制面 `tongtian-server`（临时数据目录，独立 venv）
- 网关 `tongtian-gateway`（RS256 + 控制面 JWKS/策略，独立 venv）
- OpenAI 兼容假上游（网关在这里真正完成一次对话）

用法: python scripts/g3_acceptance.py
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = GATEWAY_ROOT.parent
SERVER_ROOT = REPO_ROOT / "tongtian-server"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from tests.fake_upstreams import FakeUpstream  # noqa: E402

SERVICE_TOKEN = "g3-gateway-service-token"
ADMIN_TOKEN = "g3-gateway-admin-token"
ADMIN_EMAIL = "g3-admin@test.local"
ADMIN_PASSWORD = "g3-admin-secret-123"
USER_EMAIL = "g3-user@test.local"
USER_PASSWORD = "g3-user-secret-123"
MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
STARTUP_TIMEOUT = 90.0
JWKS_THROTTLE_SECONDS = 31.0

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK  {name}")
        return
    message = f"  NG  {name}" + (f" — {detail}" if detail else "")
    print(message)
    FAILURES.append(message)


def step(title: str) -> None:
    print(f"\n[{title}]")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def venv_python(project: Path) -> str:
    candidate = project / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _decode(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http(
    base: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
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


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def split_jwt(token: str) -> tuple[dict, dict]:
    header_part, claims_part, _ = token.split(".")
    return (
        json.loads(b64url_decode(header_part)),
        json.loads(b64url_decode(claims_part)),
    )


def verify_with_jwks(token: str, jwks: dict) -> tuple[dict, dict]:
    header, claims = split_jwt(token)
    jwk = next(
        (item for item in jwks.get("keys", []) if item.get("kid") == header.get("kid")),
        None,
    )
    if jwk is None:
        raise AssertionError(f"JWKS 中没有 kid={header.get('kid')}")
    public = rsa.RSAPublicNumbers(
        int.from_bytes(b64url_decode(jwk["e"]), "big"),
        int.from_bytes(b64url_decode(jwk["n"]), "big"),
    ).public_key()
    header_part, claims_part, sig_part = token.split(".")
    public.verify(
        b64url_decode(sig_part),
        f"{header_part}.{claims_part}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return header, claims


def tamper(token: str, **overrides) -> str:
    header_part, claims_part, sig_part = token.split(".")
    claims = json.loads(b64url_decode(claims_part))
    claims.update(overrides)
    forged = b64url_encode(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return f"{header_part}.{forged}.{sig_part}"


def wait_ready(base: str, probe: str, label: str) -> None:
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            status, _, _ = http(base, "GET", probe, timeout=2)
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.15)
    raise RuntimeError(f"{label} 未能在 {STARTUP_TIMEOUT:.0f} 秒内就绪")


def wait_port_free(port: int) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 未被释放")


def _clean_env(overrides: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TT_")}
    env.update(overrides)
    return env


class Process:
    def __init__(self, cmd: list[str], cwd: Path, env: dict[str, str], log_path: Path):
        self.log_path = log_path
        self._stream = log_path.open("wb")
        self._proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdout=self._stream, stderr=subprocess.STDOUT
        )

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
        self._stream.close()

    def text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def start_server(port: int, data_dir: Path, log_path: Path) -> Process:
    env = _clean_env(
        {
            "TT_SERVER_DATA_DIR": str(data_dir),
            "TT_SERVER_JWT_SECRET": "g3-control-plane-secret",
            "TT_SERVER_ENV": "development",
            "TT_ALLOW_REGISTRATION": "true",
            "TT_SEED_DEMO": "false",
            "TT_ADMIN_EMAIL": ADMIN_EMAIL,
            "TT_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "TT_ADMIN_NICKNAME": "G3 管理员",
            "TT_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN,
            "TT_SERVER_JWT_KEY_MAX_AGE_SECONDS": "1",
        }
    )
    process = Process(
        [
            venv_python(SERVER_ROOT),
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
        SERVER_ROOT,
        env,
        log_path,
    )
    wait_ready(f"http://127.0.0.1:{port}", "/health", "控制面")
    return process


def start_gateway(
    port: int,
    *,
    control_plane: str,
    data_dir: Path,
    log_path: Path,
) -> Process:
    env = _clean_env(
        {
            "TT_GATEWAY_ENV": "development",
            "TT_GATEWAY_JWT_MODE": "rs256",
            "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
            "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
            "TT_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN,
            "TT_GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
            "TT_GATEWAY_ID": "gw-g3-01",
            "TT_GATEWAY_DATA_DIR": str(data_dir),
            "TT_GATEWAY_MASTER_KEY": MASTER_KEY,
            "TT_GATEWAY_MASTER_KEY_VERSION": "1",
            "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
        }
    )
    process = Process(
        [
            venv_python(GATEWAY_ROOT),
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
    wait_ready(f"http://127.0.0.1:{port}", "/health", "网关")
    return process


def policy_document(gateway_base: str) -> dict:
    return {
        "contractVersion": "gateway-contract.v1",
        "tenant": "acme",
        "gateway": {"url": gateway_base},
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
                "priceVersion": "g3-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "1.00",
                    "outputPer1M": "2.00",
                },
            },
            "fake-embed": {
                "provider": "openai_compatible",
                "upstreamModel": "fake-embedding",
                "capabilities": ["embeddings"],
                "priceVersion": "g3-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "0.10",
                    "outputPer1M": "0",
                },
            },
        },
        "routes": {
            "auto": {"chain": ["fake-chat"], "fallbackAllowed": False},
            "embedding-default": {"chain": ["fake-embed"], "fallbackAllowed": False},
        },
        "allowedModels": [
            "auto",
            "embedding-default",
            "fake-chat",
            "fake-embed",
        ],
        "quotaDefaults": {
            "monthlyTokens": 1000000,
            "concurrency": 4,
            "maxOutputTokens": 4096,
        },
        "departments": {
            "dept-default": {"allowedModels": ["auto", "fake-chat"]},
        },
    }


def run_acceptance() -> None:
    upstream = FakeUpstream("openai").start()
    server_port = free_port()
    gateway_port = free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    logs = Path(tempfile.mkdtemp(prefix="tt-g3-"))
    data = logs / "data"
    server_proc = None
    gateway_proc = None
    try:
        server_proc = start_server(server_port, data / "server", logs / "server.log")
        gateway_proc = start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=data / "gateway",
            log_path=logs / "gateway.log",
        )

        step("控制面：注册用户、分配组织、签发策略")
        status, _, body = http(
            server_base,
            "POST",
            "/api/v1/auth/register",
            body={"email": USER_EMAIL, "password": USER_PASSWORD, "nickname": "G3 用户"},
        )
        registered = body if isinstance(body, dict) else {}
        uid = ((registered.get("profile") or {}).get("uid")) or ""
        check("注册用户", status == 201 and bool(uid), f"{status} {body}")

        status, _, body = http(
            server_base,
            "POST",
            "/api/v1/auth/login",
            body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        admin = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
        check("管理员登录", status == 200 and bool(admin), f"{status} {body}")

        status, _, body = http(
            server_base,
            "PATCH",
            f"/api/v1/admin/users/{uid}",
            headers={"Authorization": f"Bearer {admin}"},
            body={"org": "acme"},
        )
        check("把用户分配到组织 acme", status == 200, f"{status} {body}")

        status, _, body = http(
            server_base,
            "POST",
            "/api/v1/auth/login",
            body={"email": USER_EMAIL, "password": USER_PASSWORD},
        )
        access = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
        check("登录拿到 access token", status == 200 and bool(access), f"{status} {body}")

        status, _, body = http(
            server_base,
            "PUT",
            "/api/v1/admin/gateway/policy/acme",
            headers={"Authorization": f"Bearer {admin}"},
            body=policy_document(gateway_base),
        )
        check("保存租户策略", status == 200, f"{status} {body}")

        status, _, body = http(
            f"http://127.0.0.1:{server_port}",
            "GET",
            "/.well-known/jwks.json",
        )
        check("JWKS 公开可读", status == 200 and bool(body.get("keys")), str(body))

        step("网关：登记假上游 provider")
        status, _, body = http(
            gateway_base,
            "POST",
            "/gateway/v1/admin/providers",
            headers={"X-TT-Admin-Token": ADMIN_TOKEN},
            body={
                "id": "openai_compatible",
                "label": "g3-fake",
                "baseUrl": f"{upstream.base_url}/v1",
                "timeoutSeconds": 30,
                "allowPrivate": True,
            },
        )
        check("创建 provider", status in (200, 201), f"{status} {body}")
        status, _, body = http(
            gateway_base,
            "POST",
            "/gateway/v1/admin/providers/openai_compatible/keys",
            headers={"X-TT-Admin-Token": ADMIN_TOKEN},
            body={"label": "g3-key", "secret": "sk-g3-fake-provider-secret"},
        )
        check("写入 provider 密钥", status == 201, f"{status} {body}")

        step("换票 → RS256 网关票")
        status, headers, body = http(
            server_base,
            "POST",
            "/api/v1/auth/gateway-token",
            headers={"Authorization": f"Bearer {access}"},
            body={},
        )
        data_body = body if isinstance(body, dict) else {}
        ticket = data_body.get("token", "")
        check("换票 200", status == 200 and bool(ticket), f"{status} {body}")
        check(
            "换票响应禁止缓存",
            status == 200 and headers.get("cache-control") == "no-store",
            headers.get("cache-control", ""),
        )

        status, _, jwks = http(server_base, "GET", "/.well-known/jwks.json")
        header: dict = {}
        claims: dict = {}
        if ticket:
            try:
                header, claims = verify_with_jwks(ticket, jwks)
            except Exception as exc:  # noqa: BLE001 - 验签失败即验收失败
                check("网关票验签", False, f"{type(exc).__name__}: {exc}")
        check(
            "网关票由当前 JWKS 验签通过",
            header.get("alg") == "RS256"
            and header.get("kid") == jwks["keys"][0]["kid"],
            str(header),
        )
        check(
            "租户与部门来自控制面",
            claims.get("tenant") == "acme" and claims.get("dept") == "dept-default",
            str({k: claims.get(k) for k in ("tenant", "dept", "depts")}),
        )
        first_kid = header.get("kid", "")

        step("经网关完成一次对话")
        auth = {"Authorization": f"Bearer {ticket}"}
        status, _, body = http(
            gateway_base,
            "POST",
            "/v1/chat/completions",
            headers=auth,
            body={
                "model": "auto",
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 64,
            },
        )
        content = ""
        if isinstance(body, dict):
            choices = body.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
        check("非流式对话 200 且有回复", status == 200 and content == "你好", f"{status} {body}")
        check(
            "上游确实收到请求",
            any(
                item["path"].startswith("/v1/chat/completions")
                for item in upstream.records
            ),
            str(upstream.records[-1:]),
        )

        status, _, raw = http(
            gateway_base,
            "POST",
            "/v1/chat/completions",
            headers=auth,
            body={
                "model": "auto",
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
            },
        )
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        check(
            "流式对话完整收尾",
            status == 200 and "data: [DONE]" in text and "你好" in text,
            f"{status} {text[:200]}",
        )

        step("关掉 HS256：控制面 access token 不能直连网关")
        status, _, body = http(
            gateway_base,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {access}"},
        )
        code = ((body or {}).get("error") or {}).get("code") if isinstance(body, dict) else ""
        check("HS256 令牌被拒", status == 401 and code == "AUTH_REQUIRED", f"{status} {body}")

        step("越权：篡改 claim 与部门头")
        forged = tamper(ticket, tenant="beta")
        status, _, body = http(
            gateway_base,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {forged}"},
        )
        code = ((body or {}).get("error") or {}).get("code") if isinstance(body, dict) else ""
        check("篡改 tenant 后签名校验失败", status == 401 and code == "AUTH_REQUIRED", f"{status} {body}")

        status, _, body = http(
            gateway_base,
            "GET",
            "/v1/models",
            headers={**auth, "X-TT-Department": "dept-sales"},
        )
        code = ((body or {}).get("error") or {}).get("code") if isinstance(body, dict) else ""
        check("切到未加入的部门 → 403", status == 403 and code == "FORBIDDEN", f"{status} {body}")

        status, _, body = http(
            gateway_base,
            "GET",
            "/v1/models",
            headers={**auth, "X-TT-Department": "dept-default"},
        )
        check("切到已加入的部门 → 200", status == 200, f"{status} {body}")

        step(f"控制面重启换 kid（先等 {JWKS_THROTTLE_SECONDS:.0f}s 过 JWKS 刷新节流）")
        time.sleep(JWKS_THROTTLE_SECONDS)
        server_proc.stop()
        wait_port_free(server_port)
        server_proc = start_server(server_port, data / "server", logs / "server-restart.log")

        status, _, jwks = http(server_base, "GET", "/.well-known/jwks.json")
        status, _, body = http(
            server_base,
            "POST",
            "/api/v1/auth/gateway-token",
            headers={"Authorization": f"Bearer {access}"},
            body={},
        )
        rotated = (body or {}).get("token", "") if isinstance(body, dict) else ""
        rotated_kid = split_jwt(rotated)[0].get("kid", "") if rotated else ""
        check(
            "控制面重启后换了 kid",
            bool(rotated_kid) and bool(first_kid) and rotated_kid != first_kid,
            f"{first_kid} → {rotated_kid}",
        )
        check(
            "JWKS 同时保留新旧公钥",
            {item["kid"] for item in jwks.get("keys", [])} >= {first_kid, rotated_kid},
            str([item.get("kid") for item in jwks.get("keys", [])]),
        )

        status, _, body = http(
            gateway_base,
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {rotated}"},
            body={
                "model": "auto",
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 64,
            },
        )
        content = ""
        if isinstance(body, dict):
            choices = body.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
        check(
            "网关刷新 JWKS 后新票可用",
            status == 200 and content == "你好",
            f"{status} {body}",
        )

        status, _, body = http(
            gateway_base,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {ticket}"},
        )
        check("轮换宽限期内旧票仍可用", status == 200, f"{status} {body}")

        step("日志不落令牌")
        gateway_log = gateway_proc.text()
        check("网关日志无 JWT 明文", "eyJ" not in gateway_log, gateway_log[:400])
        check("控制面日志无 JWT 明文", "eyJ" not in server_proc.text())
    finally:
        if gateway_proc is not None:
            gateway_proc.stop()
        if server_proc is not None:
            server_proc.stop()
        upstream.stop()


def main() -> int:
    run_acceptance()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("G3 验收通过：无 HS256 完成对话、越权被拒、换 kid 后继续服务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
