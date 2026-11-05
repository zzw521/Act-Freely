#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1.3 身份与策略端到端测试（标准库实现，无 pytest/httpx 依赖）。

覆盖：
- JWKS 形状（RS256 公钥，无私钥材料）与 Cache-Control；
- gateway-token 换票：部门选择、主部门兜底、无组织/无部门拒绝、
  过期/非 access/会话吊销拒绝、响应头 no-store、短期票 claims；
- RS256 轮换：新票用新 kid，旧票仍能用 JWKS 验签；
- 组织/部门管理：部门创建、用户部门分配、主部门兜底、跨组织同 id 隔离；
- 网关策略：服务令牌鉴权、PUT 版本戳、ETag/304、非法文档拒绝、租户隔离。

用法: python tests/test_identity.py
"""

import atexit
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-identity-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "identity-test-secret-please-change"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"
os.environ["TT_GATEWAY_SERVICE_TOKEN"] = "gw-service-token-e2e"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from app import security  # noqa: E402
from app.config import settings  # noqa: E402
from app.keys import key_store  # noqa: E402
from app.main import app  # noqa: E402

_passed = 0
SERVICE_HEADERS = {"Authorization": "Bearer gw-service-token-e2e"}


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def http(method, path, *, json_body=None, token=None, extra_headers=None):
    headers = dict(extra_headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=30)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def split_jwt(token: str) -> tuple[dict, dict, bytes]:
    header_raw, claims_raw, sig_raw = token.split(".")
    return (
        json.loads(b64url_decode(header_raw)),
        json.loads(b64url_decode(claims_raw)),
        b64url_decode(sig_raw),
    )


def verify_with_jwks(token: str, jwks: dict) -> tuple[dict, dict]:
    header, claims, signature = split_jwt(token)
    jwk = next((item for item in jwks.get("keys", []) if item.get("kid") == header.get("kid")), None)
    if jwk is None:
        raise AssertionError(f"JWKS 中没有 kid={header.get('kid')}")
    public = rsa.RSAPublicNumbers(
        int.from_bytes(b64url_decode(jwk["e"]), "big"),
        int.from_bytes(b64url_decode(jwk["n"]), "big"),
    ).public_key()
    header_raw, claims_raw, _ = token.split(".")
    public.verify(
        signature,
        f"{header_raw}.{claims_raw}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return header, claims


def register(email: str, nickname: str = "成员") -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def login(email: str, password: str = "secret123") -> dict:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    data = payload(body)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {data}")
    return data


def exchange(access_token: str, body: dict | None = None):
    return http(
        "POST",
        "/api/v1/auth/gateway-token",
        json_body=body,
        token=access_token,
    )


def mint_access(uid: str, sid: str, *, exp_offset: int = 600, type_: str = "access", audience: str | None = None) -> str:
    now = int(time.time())
    return security.jwt_encode(
        {
            "sub": uid,
            "sid": sid,
            "org": "acme",
            "role": "user",
            "type": type_,
            "iss": settings.jwt_issuer,
            "aud": audience or settings.jwt_audience,
            "iat": now,
            "exp": now + exp_offset,
        }
    )


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            status, _, _ = http("GET", "/health")
            if status == 200:
                return server, thread
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def test_jwks():
    print("\n[JWKS]")
    status, headers, body = http("GET", "/.well-known/jwks.json")
    data = payload(body)
    keys = data.get("keys", [])
    check("JWKS 200 且包含公钥", status == 200 and len(keys) >= 1, str(data))
    check("JWKS 可缓存 300 秒", "max-age=300" in headers.get("Cache-Control", ""), headers.get("Cache-Control", ""))
    public = keys[0]
    check(
        "公钥字段齐全且只含公开参数",
        public.get("kty") == "RSA"
        and public.get("alg") == "RS256"
        and public.get("use") == "sig"
        and public.get("kid")
        and public.get("n")
        and public.get("e")
        and not {"d", "p", "q", "dp", "dq", "qi"} & set(public),
        str(public),
    )
    check("当前 kid 与密钥库一致", public.get("kid") == key_store().current_kid)


def test_exchange_basics(ctx):
    print("\n[换票：基本路径]")
    access = ctx["access"]
    status, headers, body = exchange(access)
    data = payload(body)
    check("换票 200", status == 200 and data.get("ok") is True, str(data))
    check("换票响应禁止缓存", headers.get("Cache-Control") == "no-store", headers.get("Cache-Control", ""))
    check(
        "换票返回租户与默认部门",
        data.get("tenant") == "acme" and data.get("dept") == "dept-default",
        str(data),
    )
    check(
        "有效期不超过 15 分钟且与 expiresAt 自洽",
        0 < int(data.get("expiresIn") or 0) <= 900
        and abs(int(data["expiresAt"]) - int(time.time()) - int(data["expiresIn"])) <= 2,
        str({k: data.get(k) for k in ("expiresIn", "expiresAt")}),
    )
    access_claims = split_jwt(access)[1]
    check("网关票不会超过 access token 的有效期", data["expiresAt"] <= access_claims["exp"])

    status, _, body = http("GET", "/.well-known/jwks.json")
    jwks = payload(body)
    header, claims = verify_with_jwks(data["token"], jwks)
    check("网关票用 RS256 且 kid 在 JWKS 中", header.get("alg") == "RS256" and header.get("kid") == jwks["keys"][0]["kid"], str(header))
    check(
        "网关票 claims：受众/类型/主体/会话/部门",
        claims.get("aud") == settings.gateway_audience
        and claims.get("type") == "access"
        and claims.get("sub") == ctx["uid"]
        and claims.get("sid") == access_claims.get("sid")
        and claims.get("tenant") == "acme"
        and claims.get("org") == "acme"
        and claims.get("dept") == "dept-default"
        and claims.get("depts") == ["dept-default"]
        and claims.get("roles") == ["user"]
        and claims.get("iss") == settings.jwt_issuer,
        str(claims),
    )
    check(
        "网关票 exp−iat ≤ 900",
        0 < int(claims["exp"]) - int(claims["iat"]) <= 900,
        str((claims.get("iat"), claims.get("exp"))),
    )
    check("网关票带唯一 jti", str(claims.get("jti") or "").startswith("gt-"), str(claims.get("jti")))

    status, _, body = exchange(access, {"dept": "dept-default"})
    check("显式选择默认部门可用", status == 200 and payload(body).get("dept") == "dept-default")

    status, _, body = exchange(access, {"dept": "dept-unknown"})
    check(
        "不在成员部门内 → 403",
        status == 403 and payload(body).get("code") == "FORBIDDEN",
        str(payload(body)),
    )
    status, _, body = exchange(access, {"dept": 42})
    check("dept 非字符串 → 400", status == 400 and payload(body).get("code") == "INVALID_REQUEST", str(payload(body)))

    status, _, body = http(
        "POST", "/api/v1/auth/gateway-token", token=access,
        extra_headers={"Content-Type": "application/json"},
    )
    check("空请求体按默认部门处理", status == 200)


def test_exchange_rejections(ctx):
    print("\n[换票：拒绝路径]")
    uid = ctx["uid"]
    sid = split_jwt(ctx["access"])[1]["sid"]

    status, _, body = exchange(mint_access(uid, sid, exp_offset=-10))
    check("过期 access token → 401 TOKEN_EXPIRED", status == 401 and payload(body).get("code") == "TOKEN_EXPIRED", str(payload(body)))

    status, _, body = exchange(mint_access(uid, sid, type_="refresh"))
    check("非 access token → 401 AUTH_REQUIRED", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))

    status, _, body = exchange(mint_access(uid, sid, audience="someone-else"))
    check("受众不符 → 401 AUTH_REQUIRED", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))

    status, _, body = exchange("not-a-jwt")
    check("畸形令牌 → 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))

    unknown = register(f"ghost-{int(time.time())}@test.local", "无会话")
    forged = mint_access(unknown["profile"]["uid"], "sess-doesnotexist")
    status, _, body = exchange(forged)
    check("会话不存在 → 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))

    status, _, body = http("POST", "/api/v1/auth/gateway-token")
    check("缺少 Authorization → 401", status == 401, str(payload(body)))

    admin = ctx["admin"]
    status, _, _ = http("POST", f"/api/v1/admin/users/{uid}/revoke-sessions", token=admin)
    check("管理员强制下线成功", status == 200)
    status, _, body = exchange(ctx["access"])
    check("会话吊销后换票 → 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))
    # 恢复登录，后续用例继续用新会话
    ctx["access"] = login(ctx["email"])["accessToken"]


def test_personal_and_no_department(ctx):
    print("\n[换票：个人账号与无部门]")
    personal = register(f"personal-{int(time.time())}@test.local", "个人用户")
    status, _, body = exchange(personal["accessToken"])
    check(
        "个人账号（无组织）→ 403",
        status == 403 and payload(body).get("code") == "FORBIDDEN" and "组织" in payload(body).get("message", ""),
        str(payload(body)),
    )

    user = register(f"nodept-{int(time.time())}@test.local", "无部门用户")
    uid = user["profile"]["uid"]
    status, _, body = http("PATCH", f"/api/v1/admin/users/{uid}", json_body={"org": "acme"}, token=ctx["admin"])
    check("分配组织后默认部门成员", status == 200 and payload(body)["profile"]["depts"][0]["id"] == "dept-default", str(payload(body)))
    access = login(user["profile"]["email"])["accessToken"]
    status, _, body = http("PATCH", f"/api/v1/admin/users/{uid}", json_body={"depts": None}, token=ctx["admin"])
    check("清空部门后成员为空", status == 200 and payload(body)["profile"]["depts"] == [], str(payload(body)))
    access = login(user["profile"]["email"])["accessToken"]
    status, _, body = exchange(access)
    check(
        "组织内但无部门 → 403",
        status == 403 and payload(body).get("code") == "FORBIDDEN" and "部门" in payload(body).get("message", ""),
        str(payload(body)),
    )


def test_departments_and_membership(ctx):
    print("\n[组织与部门]")
    admin = ctx["admin"]
    status, _, body = http("POST", "/api/v1/admin/departments", json_body={"org": "acme", "id": "dept-sales", "name": "销售部"}, token=admin)
    check("创建部门 201", status == 201 and payload(body)["department"]["id"] == "dept-sales", str(payload(body)))
    status, _, body = http("POST", "/api/v1/admin/departments", json_body={"org": "acme", "id": "Bad Dept"}, token=admin)
    check("非法部门标识 → 400", status == 400 and payload(body).get("code") == "INVALID_DEPARTMENT", str(payload(body)))
    status, _, body = http("POST", "/api/v1/admin/departments", json_body={"org": "", "id": "dept-x"}, token=admin)
    check("缺组织标识 → 400", status == 400 and payload(body).get("code") == "INVALID_ORG", str(payload(body)))
    status, _, body = http("GET", "/api/v1/admin/departments?org=acme", token=ctx["access"])
    check("非管理员不能列部门", status == 403, str(payload(body)))

    status, _, body = http("GET", "/api/v1/admin/departments?org=acme", token=admin)
    rows = {row["id"]: row for row in payload(body).get("departments", [])}
    check(
        "部门列表带成员数",
        status == 200 and "dept-default" in rows and rows["dept-sales"]["name"] == "销售部"
        and rows["dept-sales"]["members"] == 0,
        str(rows),
    )

    uid = ctx["uid"]
    status, _, body = http(
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        json_body={"depts": ["dept-default", "dept-sales"], "primaryDept": "dept-sales"},
        token=admin,
    )
    profile = payload(body).get("profile", {})
    depts = {item["id"]: item for item in profile.get("depts", [])}
    check(
        "分配两个部门并指定主部门",
        status == 200 and set(depts) == {"dept-default", "dept-sales"} and depts["dept-sales"]["primary"] is True,
        str(profile.get("depts")),
    )
    check("改部门会吊销旧会话", payload(body).get("sessionsRevoked", 0) >= 1, str(payload(body)))

    status, _, body = http("PATCH", f"/api/v1/admin/users/{uid}", json_body={"depts": ["dept-ghost"]}, token=admin)
    check("分配不存在的部门 → 400", status == 400 and payload(body).get("code") == "UNKNOWN_DEPARTMENT", str(payload(body)))
    status, _, body = http(
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        json_body={"depts": ["dept-default"], "primaryDept": "dept-sales"},
        token=admin,
    )
    check("主部门不在成员内 → 400", status == 400 and payload(body).get("code") == "INVALID_DEPARTMENT", str(payload(body)))

    ctx["access"] = login(ctx["email"])["accessToken"]
    status, _, body = exchange(ctx["access"])
    check("未指定部门时选主部门", status == 200 and payload(body).get("dept") == "dept-sales", str(payload(body)))
    check("网关票 depts 含全部成员部门", split_jwt(payload(body)["token"])[1].get("depts") == ["dept-sales", "dept-default"], str(split_jwt(payload(body)["token"])[1].get("depts")))
    status, _, body = exchange(ctx["access"], {"dept": "dept-default"})
    check("可显式换到非主部门", status == 200 and payload(body).get("dept") == "dept-default")

    status, _, body = http("GET", "/api/v1/admin/users?q=" + urllib.parse.quote(ctx["email"]), token=admin)
    card = next((row for row in payload(body).get("users", []) if row["uid"] == uid), {})
    check(
        "用户列表带部门与主部门",
        card.get("primaryDept") == "dept-sales" and {d["id"] for d in card.get("depts", [])} == {"dept-default", "dept-sales"},
        str(card),
    )

    # 同一部门 id 在不同组织下互不影响
    other = register(f"beta-{int(time.time())}@test.local", "Beta 用户")
    http("PATCH", f"/api/v1/admin/users/{other['profile']['uid']}", json_body={"org": "beta"}, token=admin)
    status, _, body = http(
        "POST", "/api/v1/admin/departments",
        json_body={"org": "beta", "id": "dept-sales", "name": "Beta Sales"},
        token=admin,
    )
    check("跨组织可复用部门 id", status == 201, str(payload(body)))
    status, _, body = http("GET", "/api/v1/admin/departments?org=beta", token=admin)
    beta_rows = {row["id"]: row for row in payload(body).get("departments", [])}
    check("不同组织的同名部门各自独立", beta_rows["dept-sales"]["name"] == "Beta Sales", str(beta_rows))
    status, _, body = http("GET", "/api/v1/admin/departments?org=acme", token=admin)
    acme_rows = {row["id"]: row for row in payload(body).get("departments", [])}
    check("原组织部门名称未被覆盖", acme_rows["dept-sales"]["name"] == "销售部", str(acme_rows))


def test_key_rotation(ctx):
    print("\n[密钥轮换]")
    status, _, body = exchange(ctx["access"])
    old_token = payload(body)["token"]
    old_kid = split_jwt(old_token)[0]["kid"]

    new_kid = key_store().rotate()
    status, _, body = http("GET", "/.well-known/jwks.json")
    jwks = payload(body)
    kids = [item["kid"] for item in jwks.get("keys", [])]
    check(
        "轮换后 JWKS 同时保留新旧公钥",
        new_kid != old_kid and set(kids) == {new_kid, old_kid},
        str(kids),
    )
    verify_with_jwks(old_token, jwks)  # 旧票仍可验证
    check("轮换前的网关票仍能验签", True)

    status, _, body = exchange(ctx["access"])
    new_token = payload(body)["token"]
    header, _ = verify_with_jwks(new_token, jwks)
    check("轮换后的新票使用新 kid", header["kid"] == new_kid, header["kid"])
    ctx["gateway_token"] = new_token


def policy_document(tenant: str = "acme") -> dict:
    return {
        "contractVersion": "gateway-contract.v1",
        "tenant": tenant,
        "gateway": {"url": "http://127.0.0.1:8001"},
        "noLog": True,
        "byok": {"allowed": True, "hardQuota": False},
        "models": {
            "auto": {
                "provider": "azure_openai",
                "upstreamModel": "gpt-4o",
                "capabilities": ["chat", "tools"],
                "contextWindow": 128000,
                "priceVersion": "2026-10-01",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "2.50",
                    "outputPer1M": "10.00",
                },
            }
        },
        "routes": {"auto": {"chain": ["auto"], "fallbackAllowed": False}},
        "allowedModels": ["auto"],
        "quotaDefaults": {"monthlyTokens": 2000000, "concurrency": 4},
        "departments": {"dept-default": {"allowedModels": ["auto"]}},
    }


def test_policy(ctx):
    print("\n[网关策略]")
    admin = ctx["admin"]
    status, _, body = http("GET", "/api/v1/gateway/policy?tenant=acme", extra_headers=SERVICE_HEADERS)
    check("未配置策略 → 404", status == 404 and payload(body).get("code") == "POLICY_NOT_FOUND", str(payload(body)))
    status, _, body = http("GET", "/api/v1/gateway/policy?tenant=acme")
    check("缺少服务令牌 → 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED", str(payload(body)))
    status, _, body = http(
        "GET", "/api/v1/gateway/policy?tenant=acme",
        extra_headers={"Authorization": "Bearer wrong-token"},
    )
    check("服务令牌错误 → 401", status == 401, str(payload(body)))
    status, _, body = http("GET", "/api/v1/gateway/policy?tenant=Bad%20Tenant", extra_headers=SERVICE_HEADERS)
    check("非法租户 → 400", status == 400 and payload(body).get("code") == "INVALID_TENANT", str(payload(body)))

    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/acme", json_body=policy_document(), token=admin)
    first = payload(body)
    check("保存策略 200", status == 200 and first.get("ok") is True, str(first))
    version1 = first.get("policyVersion", "")
    check(
        "服务端重算版本并保持一致",
        version1.endswith("#1") and first["document"]["policyVersion"] == version1 and first["document"]["version"] == version1,
        str(version1),
    )
    check("保存时保留签发时间", bool(first["document"].get("issuedAt")), str(first["document"].get("issuedAt")))

    status, headers, body = http("GET", "/api/v1/gateway/policy?tenant=acme", extra_headers=SERVICE_HEADERS)
    document = payload(body)
    check("网关拉取策略 200", status == 200 and document.get("policyVersion") == version1, str(document.get("policyVersion")))
    check("ETag 等于策略版本", headers.get("ETag") == f'"{version1}"', headers.get("ETag", ""))
    check("策略响应禁止缓存", headers.get("Cache-Control") == "no-store")
    status, headers, body = http(
        "GET",
        "/api/v1/gateway/policy?tenant=acme",
        extra_headers={**SERVICE_HEADERS, "If-None-Match": headers.get("ETag", "")},
    )
    check("条件请求 304 且无正文", status == 304 and body == b"", f"{status} {body!r}")

    # 客户端带来的 policyVersion 会被丢弃并由服务端重算
    payload_doc = policy_document()
    payload_doc["policyVersion"] = "client-forged#99"
    payload_doc["version"] = "client-forged#99"
    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/ACME", json_body=payload_doc, token=admin)
    second = payload(body)
    version2 = second.get("policyVersion", "")
    check(
        "重复保存版本递增且忽略客户端版本",
        status == 200 and version2.endswith("#2") and "client-forged" not in json.dumps(second, ensure_ascii=False),
        str(version2),
    )
    status, headers, _ = http("GET", "/api/v1/gateway/policy?tenant=acme", extra_headers=SERVICE_HEADERS)
    check("租户大小写归一后 ETag 更新", headers.get("ETag") == f'"{version2}"', headers.get("ETag", ""))

    bad = policy_document()
    bad.pop("routes")
    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/acme", json_body=bad, token=admin)
    check("缺 routes → 400 POLICY_INVALID", status == 400 and payload(body).get("code") == "POLICY_INVALID", str(payload(body)))
    bad = policy_document()
    bad["models"]["auto"]["pricing"]["inputPer1M"] = "01"
    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/acme", json_body=bad, token=admin)
    check("非法单价 → 400 POLICY_INVALID", status == 400 and payload(body).get("code") == "POLICY_INVALID", str(payload(body)))

    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/acme", json_body=policy_document(), token=ctx["access"])
    check("普通用户不能写策略", status == 403, str(payload(body)))

    other = policy_document("beta")
    other["models"]["auto"]["upstreamModel"] = "qwen-max"
    status, _, body = http("PUT", "/api/v1/admin/gateway/policy/beta", json_body=other, token=admin)
    check("第二个租户保存成功", status == 200, str(payload(body)))
    status, _, body = http("GET", "/api/v1/gateway/policy?tenant=beta", extra_headers=SERVICE_HEADERS)
    check("租户策略相互隔离", payload(body)["models"]["auto"]["upstreamModel"] == "qwen-max", str(payload(body)))

    status, _, body = http("GET", "/api/v1/admin/gateway/policies", token=admin)
    tenants = {row["tenant"] for row in payload(body).get("policies", [])}
    check("管理员策略列表", status == 200 and {"acme", "beta"} <= tenants, str(tenants))
    status, _, body = http("GET", "/api/v1/admin/gateway/policy/acme", token=admin)
    detail = payload(body)
    check(
        "管理员读取策略详情",
        status == 200 and detail.get("policyVersion") == version2 and detail["document"]["tenant"] == "acme",
        str(detail.get("policyVersion")),
    )


def test_service_token_missing():
    print("\n[服务令牌未配置]")
    # 模拟控制面未下发服务令牌：网关侧应收到 503 而不是放行
    import dataclasses

    import app.gateway_policy as gateway_policy

    original = gateway_policy.settings
    gateway_policy.settings = dataclasses.replace(original, gateway_service_token="")
    try:
        status, _, body = http("GET", "/api/v1/gateway/policy?tenant=acme", extra_headers=SERVICE_HEADERS)
        check(
            "控制面未配置服务令牌 → 503 AUTH_UNAVAILABLE",
            status == 503 and payload(body).get("code") == "AUTH_UNAVAILABLE",
            str(payload(body)),
        )
    finally:
        gateway_policy.settings = original


def run():
    server, thread = start_server()
    try:
        status, _, body = http(
            "POST", "/api/v1/auth/login",
            json_body={"email": "admin@test.local", "password": "admin-secret-123"},
        )
        admin = payload(body)["accessToken"]
        check("管理员登录", status == 200)

        user = register(f"worker-{int(time.time())}@test.local", "部门成员")
        uid = user["profile"]["uid"]
        status, _, body = http("PATCH", f"/api/v1/admin/users/{uid}", json_body={"org": "acme"}, token=admin)
        check("管理员分配组织", status == 200 and payload(body)["profile"]["org"] == "acme", str(payload(body)))
        access = login(user["profile"]["email"])["accessToken"]
        ctx = {
            "admin": admin,
            "uid": uid,
            "email": user["profile"]["email"],
            "access": access,
        }

        test_jwks()
        test_exchange_basics(ctx)
        test_exchange_rejections(ctx)
        test_personal_and_no_department(ctx)
        test_departments_and_membership(ctx)
        test_key_rotation(ctx)
        test_policy(ctx)
        test_service_token_missing()
        print(f"\n全部通过：{_passed} 项断言")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    run()
