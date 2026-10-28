#!/usr/bin/env python3
"""服务化面自测：静态站点托管 + 引擎令牌守卫 + 非回环绑定准入

锁住这些约束：
1. TT_WEB_DIR/--web-dir 指向真实构建目录时，根路径与静态资源可访问，API 路由不被接管；
2. 目录缺失/缺少 index.html 时明确失败，而不是静默只起 API；
3. 设置 TT_ENGINE_TOKEN 后 /api/* 必须携带 Bearer 或 x-tt-engine-token，/health 与静态页仍开放；
4. 绑定非回环地址必须同时显式确认与设置令牌，二者缺一即拒绝；
5. 桌面模式（不设令牌、回环地址）行为不变。

依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_service_cli_selftest.py
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    from fastapi.testclient import TestClient
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine service selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(secret: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "u-1",
        "sid": "sess-1",
        "role": "user",
        "org": "acme",
        "quota": {"monthlyTokens": 100000, "concurrentTasks": 2},
        "iss": "tongtian-auth",
        "aud": "tongtian-engine",
        "type": "access",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _auth_env_clear() -> None:
    for name in (
        "TT_ENGINE_TOKEN",
        "TT_ENGINE_JWT_SECRET",
        "TT_AUTH_JWT_SECRET",
        "TT_ENGINE_JWT_ISSUER",
        "TT_ENGINE_JWT_AUDIENCE",
        "TT_ENGINE_ALLOWED_USERS",
        "TT_ENGINE_AUDIT_LOG",
    ):
        os.environ.pop(name, None)


def check_jwt_and_audit(client) -> None:
    print("\n[引擎 JWT 校验、审计与用量归属]")
    secret = "engine-jwt-selftest-secret"
    _auth_env_clear()
    os.environ["TT_ENGINE_JWT_SECRET"] = secret

    good = make_jwt(secret)
    ok = client.get("/api/models", headers=_bearer(good))
    check("合法 JWT 可通过", ok.status_code == 200, f"{ok.status_code} {ok.text[:120]}")

    bad_tokens = [
        ("签名错误", make_jwt("wrong-secret")),
        ("已过期", make_jwt(secret, exp=int(time.time()) - 1)),
        ("aud 不匹配", make_jwt(secret, aud="other-engine")),
        ("iss 不匹配", make_jwt(secret, iss="other-auth")),
        ("refresh 令牌", make_jwt(secret, type="refresh")),
        ("缺少 sid", make_jwt(secret, sid="")),
        ("缺少 sub", make_jwt(secret, sub="")),
        ("畸形令牌", "not-a-jwt"),
    ]
    for name, token in bad_tokens:
        res = client.get("/api/models", headers=_bearer(token))
        check(
            f"JWT {name} → 401 ENGINE_UNAUTHORIZED",
            res.status_code == 401 and res.json().get("code") == "ENGINE_UNAUTHORIZED",
            f"{res.status_code} {res.text[:120]}",
        )

    os.environ["TT_ENGINE_ALLOWED_USERS"] = "u-1"
    denied = client.get("/api/models", headers=_bearer(make_jwt(secret, sub="u-2", sid="sess-2")))
    check(
        "白名单外用户 → 403 ENGINE_USER_NOT_ALLOWED",
        denied.status_code == 403 and denied.json().get("code") == "ENGINE_USER_NOT_ALLOWED",
        f"{denied.status_code} {denied.text[:120]}",
    )
    check(
        "白名单内用户放行",
        client.get("/api/models", headers=_bearer(good)).status_code == 200,
    )
    os.environ.pop("TT_ENGINE_ALLOWED_USERS", None)

    os.environ["TT_ENGINE_TOKEN"] = "static-selftest-token"
    check(
        "静态令牌与 JWT 并存（静态）",
        client.get("/api/models", headers=_bearer("static-selftest-token")).status_code == 200,
    )
    check(
        "静态令牌与 JWT 并存（JWT）",
        client.get("/api/models", headers=_bearer(good)).status_code == 200,
    )
    os.environ.pop("TT_ENGINE_TOKEN", None)

    u1 = make_jwt(secret)
    u2 = make_jwt(secret, sub="u-2", sid="sess-2")
    admin = make_jwt(secret, sub="admin-1", sid="sess-admin", role="admin")
    client.get("/api/models", headers=_bearer(u1))
    client.get("/api/tools", headers=_bearer(u2))

    own = client.get("/api/audit?limit=200", headers=_bearer(u1)).json()
    own_entries = own.get("entries") or []
    check("审计记录调用主体", any(e.get("sub") == "u-1" and e.get("path") == "/api/models" for e in own_entries))
    check(
        "普通用户审计只见自己",
        bool(own_entries) and all(e.get("sub") == "u-1" for e in own_entries),
    )
    forced = client.get("/api/audit?sub=u-2", headers=_bearer(u1)).json().get("entries") or []
    check(
        "普通用户无法指定他人审计",
        bool(forced) and all(e.get("sub") == "u-1" for e in forced),
    )

    all_entries = client.get("/api/audit?limit=500", headers=_bearer(admin)).json().get("entries") or []
    subjects = {e.get("sub") for e in all_entries}
    check("管理员审计可见多个用户", {"u-1", "u-2"} <= subjects, str(subjects))
    by_user = client.get("/api/audit?sub=u-2", headers=_bearer(admin)).json().get("entries") or []
    check("管理员可按用户过滤审计", bool(by_user) and all(e.get("sub") == "u-2" for e in by_user))
    failed = [e for e in all_entries if e.get("reason")]
    check("失败请求也进入审计", any(e.get("reason") == "expired" for e in failed), str(failed[:2]))

    with tempfile.TemporaryDirectory() as audit_dir:
        log_path = Path(audit_dir) / "audit.jsonl"
        os.environ["TT_ENGINE_AUDIT_LOG"] = str(log_path)
        client.get("/api/models", headers=_bearer(u1))
        lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        persisted = json.loads(lines[-1]) if lines else {}
        check("审计可追加落盘", persisted.get("sub") == "u-1" and persisted.get("path") == "/api/models")
        os.environ.pop("TT_ENGINE_AUDIT_LOG", None)

    engine_server.USAGE_LEDGER.add("audit-model", 10, 5, sub="u-1", org="acme")
    engine_server.USAGE_LEDGER.add("audit-model", 20, 7, sub="u-2", org="acme")
    all_rows = engine_server.USAGE_LEDGER.aggregate("total")
    merged = next((r for r in all_rows if r["modelName"] == "audit-model"), {})
    check("用量默认汇总全部用户", merged.get("tokensUsed") == 42, str(merged))
    u1_rows = engine_server.USAGE_LEDGER.aggregate("total", "u-1")
    u1_merged = next((r for r in u1_rows if r["modelName"] == "audit-model"), {})
    check("用量可按用户过滤", u1_merged.get("tokensUsed") == 15, str(u1_merged))
    users = engine_server.USAGE_LEDGER.users("total")
    order = [row["sub"] for row in users]
    check("用户汇总按 token 降序", order[:2] == ["u-2", "u-1"], str(users))
    check("用户汇总带组织", next(r for r in users if r["sub"] == "u-1").get("org") == "acme")

    u1_usage = client.get("/api/usage?period=total&sub=u-2", headers=_bearer(u1)).json()
    u1_row = next((r for r in u1_usage.get("rows") or [] if r["modelName"] == "audit-model"), {})
    check(
        "普通用户查询用量被强制收窄到自己",
        u1_usage.get("subject") == "u-1" and u1_row.get("tokensUsed") == 15,
        str(u1_usage)[:200],
    )
    admin_usage = client.get("/api/usage?period=total&sub=u-2", headers=_bearer(admin)).json()
    admin_row = next((r for r in admin_usage.get("rows") or [] if r["modelName"] == "audit-model"), {})
    check(
        "管理员可按用户查询用量",
        admin_usage.get("subject") == "u-2" and admin_row.get("tokensUsed") == 27,
        str(admin_usage)[:200],
    )
    report = client.get("/api/usage/report?period=total", headers=_bearer(admin)).json()
    subjects = {row.get("sub") for row in report.get("users") or []}
    check("用量报表含用户汇总", {"u-1", "u-2"} <= subjects, str(subjects))

    _auth_env_clear()
    check("清理凭据后回到无鉴权桌面模式", client.get("/api/models").status_code == 200)


def main() -> int:
    _auth_env_clear()
    os.environ.pop("TT_ALLOW_REMOTE_BIND", None)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "index.html").write_text("<!doctype html><title>tt-web</title>", encoding="utf-8")
        (root / "assets").mkdir()
        (root / "assets" / "app.js").write_text("console.log('tt')", encoding="utf-8")

        mounted = engine_server.mount_web(engine_server.app, td)
        check("mount_web 返回解析后的绝对目录", mounted == root.resolve(), str(mounted))

        client = TestClient(engine_server.app)
        home = client.get("/")
        check("根路径返回 index.html", home.status_code == 200 and "tt-web" in home.text)
        asset = client.get("/assets/app.js")
        check(
            "静态资源可访问且 MIME 不被改写",
            asset.status_code == 200 and "tt" in asset.text,
            f"{asset.status_code} {asset.headers.get('content-type')}",
        )
        health = client.get("/health")
        check("健康检查仍走 API", health.status_code == 200 and health.json()["status"] == "ok")

        # 令牌守卫：只保护 /api/*
        os.environ["TT_ENGINE_TOKEN"] = "sekret-service-token"
        protected = client.get("/api/models")
        check("无令牌的 /api 请求被拒", protected.status_code == 401, str(protected.status_code))
        check(
            "拒绝文案不回显令牌",
            protected.status_code == 401 and "sekret-service-token" not in protected.text,
        )
        bearer = client.get(
            "/api/models", headers={"Authorization": "Bearer sekret-service-token"}
        )
        check("Bearer 令牌可通过", bearer.status_code == 200, f"{bearer.status_code} {bearer.text[:120]}")
        header = client.get(
            "/api/models", headers={"x-tt-engine-token": "sekret-service-token"}
        )
        check("x-tt-engine-token 可通过", header.status_code == 200, str(header.status_code))
        check("健康检查不受令牌守卫影响", client.get("/health").status_code == 200)
        check("静态页不受令牌守卫影响", client.get("/").status_code == 200)
        os.environ.pop("TT_ENGINE_TOKEN", None)

        check_jwt_and_audit(client)

        # 目录不可用必须失败，不能静默退化成只起 API
        try:
            engine_server.mount_web(engine_server.app, str(root / "missing"))
            check("缺失目录应抛错", False)
        except RuntimeError as exc:
            check("缺失目录明确报错并指向构建产物", "index.html" in str(exc))

    # 绑定准入：纯函数覆盖各象限
    check("回环地址无需令牌", engine_server.service_bind_error("127.0.0.1", "", "") == "")
    check(
        "非回环但未确认 → 拒绝",
        "TT_ALLOW_REMOTE_BIND" in engine_server.service_bind_error("0.0.0.0", "", "tok"),
    )
    check(
        "非回环已确认但缺令牌 → 拒绝",
        "TT_ENGINE_TOKEN" in engine_server.service_bind_error("0.0.0.0", "1", ""),
    )
    check(
        "非回环 + 确认 + 令牌 → 允许",
        engine_server.service_bind_error("0.0.0.0", "1", "tok") == "",
    )
    check(
        "非回环 + 确认 + 仅 JWT 密钥 → 允许",
        engine_server.service_bind_error("0.0.0.0", "1", "", "jwt-secret") == "",
    )
    check(
        "非回环 + 确认 + 无任何凭据 → 拒绝",
        "TT_ENGINE_JWT_SECRET" in engine_server.service_bind_error("0.0.0.0", "1", "", ""),
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("OK: 服务化面自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
