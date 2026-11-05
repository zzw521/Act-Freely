"""M2.1–M2.2 自测：管理面 mTLS（代理模式）与请求限流。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须先于 tests.support / app.*：Settings 在导入时固化
os.environ["TT_GATEWAY_MTLS_MODE"] = "proxy"
os.environ["TT_GATEWAY_TRUSTED_PROXIES"] = "127.0.0.1,::1"
os.environ["TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE"] = "6"
os.environ["TT_GATEWAY_ADMIN_RATE_LIMIT_BURST"] = "3"

from dataclasses import replace  # noqa: E402

from starlette.requests import Request  # noqa: E402

from tests import support  # noqa: E402

from app.config import settings  # noqa: E402
from app.mtls import (  # noqa: E402
    DN_HEADER,
    VERIFY_HEADER,
    AdminPeer,
    is_trusted_proxy,
    resolve_admin_peer,
)
from app.ratelimit import AdminRateLimiter, admin_limiter  # noqa: E402
from app.secrets import Secrets  # noqa: E402
from app.startup import validate_startup  # noqa: E402

ADMIN_PATH = "/gateway/v1/admin/health"


def request_for(client: str, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": ADMIN_PATH,
        "raw_path": ADMIN_PATH.encode(),
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": (client, 43111),
        "server": ("127.0.0.1", 8001),
    }
    return Request(scope)


def mtls_headers(dn: str = "CN=control-plane") -> dict[str, str]:
    return {
        "X-TT-Admin-Token": support.ADMIN_TOKEN,
        VERIFY_HEADER: "SUCCESS",
        DN_HEADER: dn,
    }


def test_trust_boundary() -> None:
    check = support.check
    check("受信代理：回环地址被采信", is_trusted_proxy("127.0.0.1", ("127.0.0.1",)))
    check("受信代理：非白名单地址被拒", not is_trusted_proxy("10.9.9.9", ("127.0.0.1",)))
    check("受信代理：CIDR 生效", is_trusted_proxy("10.1.2.3", ("10.0.0.0/8",)))
    check(
        "受信代理：IPv4-mapped IPv6 归一化",
        is_trusted_proxy("::ffff:127.0.0.1", ("127.0.0.1",)),
    )
    check("受信代理：非法配置失败关闭", not is_trusted_proxy("127.0.0.1", ("bad-ip",)))

    verified = resolve_admin_peer(request_for("127.0.0.1", mtls_headers()))
    check(
        "对端解析：受信代理 + SUCCESS 得到证书主体",
        verified.verified and verified.subject == "CN=control-plane",
        repr(verified),
    )
    check(
        "对端解析：主体里的控制字符被清掉",
        resolve_admin_peer(
            request_for("127.0.0.1", mtls_headers("CN=a\x00b\x1fc"))
        ).subject
        == "CN=abc",
    )
    check(
        "对端解析：非受信对端携带校验头也不算数",
        not resolve_admin_peer(request_for("10.9.9.9", mtls_headers())).verified,
    )
    check(
        "对端解析：FAILED 不算通过",
        not resolve_admin_peer(
            request_for("127.0.0.1", {VERIFY_HEADER: "FAILED", DN_HEADER: "CN=x"})
        ).verified,
    )
    check(
        "对端解析：缺校验头不算通过",
        not resolve_admin_peer(request_for("127.0.0.1", {DN_HEADER: "CN=x"})).verified,
    )
    check(
        "限流键：证书身份与对端地址分开计数",
        AdminPeer("127.0.0.1", "CN=a", True).rate_key
        != AdminPeer("127.0.0.1", "", False).rate_key,
    )


def test_rate_limiter_unit() -> None:
    now = [100.0]
    limiter = AdminRateLimiter(60, 2, clock=lambda: now[0])
    support.check("令牌桶：前两个请求放行", limiter.consume("k") == 0 and limiter.consume("k") == 0)
    blocked = limiter.consume("k")
    support.check("令牌桶：耗尽后返回重试秒数", 0.9 < blocked <= 1.0, f"{blocked:.3f}")
    now[0] += 1.0
    support.check("令牌桶：按速率补充后恢复", limiter.consume("k") == 0)
    limiter.reset()
    support.check("令牌桶：reset 后恢复满桶", limiter.consume("k") == 0)
    support.check("令牌桶：0 表示关闭", AdminRateLimiter(0, 0).consume("k") == 0)


def test_startup_validation() -> None:
    def report(**overrides):
        values = {
            "environment": "production",
            "jwt_mode": "rs256",
            "jwks_url": "https://gw.example.internal/.well-known/jwks.json",
            "control_plane_url": "",
            "admin_token": "prod-admin-token",
            "allow_private_providers": False,
            "mtls_mode": "proxy",
            "trusted_proxies": ("127.0.0.1",),
            "admin_rate_limit_per_minute": 120,
            "admin_rate_limit_burst": 40,
        }
        values.update(overrides)
        base = replace(settings, **values)
        return validate_startup(base, Secrets.from_settings(base))

    baseline = report()
    support.check(
        "启动自检：生产默认 mTLS + 限流无错误无告警",
        not baseline.errors and not baseline.warnings,
        f"{baseline.errors} {baseline.warnings}",
    )
    disabled = report(mtls_mode="off", admin_rate_limit_per_minute=0)
    support.check(
        "启动自检：显式关闭 mTLS/限流给出告警",
        any("未启用 mTLS" in item for item in disabled.warnings)
        and any("未启用限流" in item for item in disabled.warnings),
        str(disabled.warnings),
    )
    invalid_mode = report(mtls_mode="required")
    support.check(
        "启动自检：非法 mTLS 模式报错",
        any("TT_GATEWAY_MTLS_MODE" in item for item in invalid_mode.errors),
        str(invalid_mode.errors),
    )
    no_proxy = report(trusted_proxies=())
    support.check(
        "启动自检：proxy 模式必须配置受信代理",
        any("TT_GATEWAY_TRUSTED_PROXIES" in item for item in no_proxy.errors),
        str(no_proxy.errors),
    )
    bad_proxy = report(trusted_proxies=("not-an-ip",))
    support.check(
        "启动自检：受信代理地址非法报错",
        any("非法地址" in item for item in bad_proxy.errors),
        str(bad_proxy.errors),
    )


def test_http_enforcement(server) -> None:
    check = support.check

    def admin(headers: dict[str, str]):
        return support.http("GET", ADMIN_PATH, headers=headers)

    status, _, body = admin({"X-TT-Admin-Token": support.ADMIN_TOKEN})
    check(
        "HTTP：缺客户端证书校验头 → 401 MTLS_REQUIRED",
        status == 401 and body.get("code") == "MTLS_REQUIRED",
        f"{status} {body}",
    )
    status, _, body = admin(
        {"X-TT-Admin-Token": support.ADMIN_TOKEN, VERIFY_HEADER: "FAILED"}
    )
    check(
        "HTTP：证书校验失败 → 401 MTLS_REQUIRED",
        status == 401 and body.get("code") == "MTLS_REQUIRED",
        f"{status} {body}",
    )
    status, _, body = admin(mtls_headers())
    check("HTTP：受信代理 + 有效证书 + 管理令牌 → 200", status == 200, f"{status} {body}")

    admin_limiter.reset()
    status, _, body = admin({**mtls_headers("CN=wrong-token"), "X-TT-Admin-Token": "nope"})
    check(
        "HTTP：mTLS 通过但管理令牌错误 → 401 AUTH_REQUIRED",
        status == 401 and body.get("code") == "AUTH_REQUIRED",
        f"{status} {body}",
    )

    admin_limiter.reset()
    allowed = [admin(mtls_headers("CN=rate-limited"))[0] for _ in range(3)]
    status, headers, body = admin(mtls_headers("CN=rate-limited"))
    check(
        "HTTP：令牌桶耗尽 → 429 RATE_LIMITED",
        allowed == [200, 200, 200] and status == 429 and body.get("code") == "RATE_LIMITED",
        f"{allowed} {status} {body}",
    )
    check(
        "HTTP：429 带 Retry-After",
        headers.get("retry-after", "").isdigit() and int(headers["retry-after"]) >= 1,
        str(headers.get("retry-after")),
    )
    status, _, body = admin(mtls_headers("CN=another-client"))
    check(
        "HTTP：限流按证书主体隔离",
        status == 200,
        f"{status} {body}",
    )
    status, _, _ = admin(mtls_headers("CN=rate-limited"))
    check("HTTP：同主体仍在限流窗口内", status == 429, str(status))

    admin_limiter.reset()
    payload = {
        "id": "openai_compatible",
        "label": "mtls-provider",
        "baseUrl": "http://127.0.0.1:9/v1",
        "timeoutSeconds": 30,
        "allowPrivate": True,
    }
    status, _, body = support.http(
        "POST",
        "/gateway/v1/admin/providers",
        headers=mtls_headers("CN=audit-subject"),
        body=payload,
    )
    check("HTTP：mTLS 身份可完成管理写操作", status == 201, f"{status} {body}")
    from app.state import db

    rows = db.query("SELECT actor FROM audit_log ORDER BY id DESC LIMIT 1")
    check(
        "审计：未带 actor 头时回落证书主体",
        bool(rows) and rows[0]["actor"] == "CN=audit-subject",
        str([dict(row) for row in rows]),
    )


def main() -> int:
    test_trust_boundary()
    test_rate_limiter_unit()
    test_startup_validation()
    server = support.start_gateway()
    try:
        test_http_enforcement(server)
    finally:
        support.stop_gateway(server)
    return support.report("M2 管理面 mTLS 与限流自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
