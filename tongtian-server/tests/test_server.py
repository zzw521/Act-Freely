#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通天办公服务端全链路冒烟测试（标准库实现，无 pytest/httpx 依赖）。

覆盖：
- 健康检查、分类、错误格式；
- v1 注册/登录/刷新轮换/退出、loopback 网页注册与授权码交换；
- 技能/套件发布、版本发布、检索、分类、下载与 SHA-256 校验；
- MCP 配置发布与校验（拒绝密钥值字段、非法传输方式）；
- 超大请求体拒绝、包文件损坏检测；
- 用量上报幂等入库与「桌面为主、引擎按来源补入」的合并视图（缺陷 D-43）；
- 收藏、管理员推荐、我的发布、分页；
- 演示数据幂等写入；
- 登录限流。

用法: python tests/test_server.py
"""

import atexit
import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-e2e-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "e2e-test-secret-please-change"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"
os.environ["TT_DEMO_PASSWORD"] = "demo-secret-123"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import seed, security  # noqa: E402
from app.main import app  # noqa: E402

_passed = 0


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


def encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----tt" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for key, (filename, content, content_type) in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def http(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    form: dict | None = None,
    fields: dict | None = None,
    files: dict | None = None,
    token: str | None = None,
    raw: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict | None = None,
):
    url = BASE + path
    headers: dict[str, str] = {}
    if extra_headers:
        headers.update(extra_headers)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif files is not None:
        data, headers["Content-Type"] = encode_multipart(fields or {}, files)
    elif raw is not None:
        data = raw
        if content_type:
            headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=30)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def make_zip(files: dict[str, str | bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def skill_zip(name: str, version: str = "1.0.0", description: str = "把零散进展整理成周报") -> bytes:
    return make_zip(
        {
            "SKILL.md": (
                "---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"version: {version}\n"
                "category: 效率\n"
                "tags: [周报, 总结]\n"
                "---\n\n"
                f"# {name}\n\n## 使用方式\n\n提供本周进展，输出结构化周报。\n"
            ),
            "references/checklist.md": "# 检查清单\n\n- 结果\n- 数据\n- 风险\n",
        }
    )


def publish_skill(token: str, name: str, data: bytes, **forms):
    return http(
        "POST",
        "/api/v1/skills",
        fields=forms,
        files={"package": (f"{name}.zip", data, "application/zip")},
        token=token,
    )


def publish_skill_version(token: str, item_id: str, data: bytes, **forms):
    return http(
        "POST",
        f"/api/v1/skills/{item_id}/versions",
        fields=forms,
        files={"package": (f"{item_id}.zip", data, "application/zip")},
        token=token,
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


def test_public():
    print("\n[健康检查与分类]")
    status, headers, body = http("GET", "/health")
    data = payload(body)
    check("health 200", status == 200 and data.get("ok") is True)
    check("安全响应头", headers.get("X-Content-Type-Options") == "nosniff")
    status, _, body = http("GET", "/")
    check("根路径返回文档入口", status == 200 and payload(body).get("docs") == "/docs")
    status, _, _ = http("GET", "/docs")
    check("Swagger UI 可访问", status == 200)
    status, headers, body = http("GET", "/ui/")
    check("Web 控制台可访问", status == 200 and "通天办公" in body.decode("utf-8"))
    check(
        "控制台 CSP",
        "default-src 'self'" in headers.get("Content-Security-Policy", ""),
    )

    for item_type, expected in (("skill", "效率"), ("suite", "律师"), ("mcp", "开发")):
        status, _, body = http("GET", f"/api/v1/categories?type={item_type}")
        names = [row["name"] for row in payload(body).get("categories", [])]
        check(f"{item_type} 分类齐全", status == 200 and expected in names)
    status, _, body = http("GET", "/api/v1/categories?type=unknown")
    check("非法分类类型被拒", status == 400 and payload(body).get("code") == "INVALID_TYPE")

    status, _, body = http("GET", "/api/v1/unknown")
    check("未知市场类型 404", status == 404 and payload(body).get("code") == "MARKET_NOT_FOUND")
    status, _, body = http("GET", "/api/v1/skills/bad$id")
    check("非法条目标识 400", status == 400 and payload(body).get("code") == "INVALID_ITEM_ID")

    oversized = b'{"email":"a@test.local","password":"' + b"x" * (2 * 1024 * 1024) + b'"}'
    status, _, body = http(
        "POST", "/api/v1/auth/login", raw=oversized, content_type="application/json"
    )
    check("超大 JSON 请求体被拒", status == 413 and payload(body).get("code") == "PAYLOAD_TOO_LARGE")


def test_auth():
    print("\n[账号与登录]")
    stamp = int(time.time())
    email_a = f"publisher{stamp}@test.local"
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email_a, "password": "secret123", "nickname": "发布者A"},
    )
    data = payload(body)
    check("注册成功 201", status == 201 and data.get("ok") is True)
    check("返回三件套", all(data.get("profile", {}).get(k) for k in ("uid", "nickname", "email")))
    check("默认普通用户", data["profile"].get("role") == "user" and data["profile"].get("mock") is False)
    access_a, refresh_a = data["accessToken"], data["refreshToken"]

    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email_a, "password": "secret123", "nickname": "重复"},
    )
    check("重复邮箱 409", status == 409 and payload(body).get("code") == "EMAIL_TAKEN")
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": f"weak{stamp}@test.local", "password": "123"},
    )
    check("弱密码被拒", status == 400 and payload(body).get("code") == "WEAK_PASSWORD")
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": f"extra{stamp}@test.local", "password": "secret123", "role": "admin"},
    )
    check("多余字段被拒 422", status == 422 and payload(body).get("code") == "VALIDATION_ERROR")

    status, _, body = http(
        "GET", "/api/v1/auth/me", token=access_a
    )
    check("me 返回账号资料", status == 200 and payload(body)["profile"]["email"] == email_a)
    status, _, body = http("GET", "/api/v1/auth/me")
    check("未登录访问 me 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")

    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email_a, "password": "wrong-pass"}
    )
    check("错误密码 401", status == 401 and payload(body).get("code") == "INVALID_CREDENTIALS")
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email_a, "password": "secret123"}
    )
    data = payload(body)
    check("正确密码登录", status == 200 and data.get("ok") is True)
    access_a2 = data["accessToken"]

    status, _, body = http(
        "POST", "/api/v1/auth/refresh", json_body={"refreshToken": refresh_a}
    )
    rotated = payload(body)
    check("refresh 轮换成功", status == 200 and rotated.get("refreshToken") != refresh_a)
    status, _, body = http(
        "POST", "/api/v1/auth/refresh", json_body={"refreshToken": refresh_a}
    )
    check("旧 refresh 已吊销", status == 401 and payload(body).get("code") == "INVALID_REFRESH")

    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email_a, "password": "secret123"}
    )
    logout_refresh = payload(body)["refreshToken"]
    status, _, body = http(
        "POST", "/api/v1/auth/logout", json_body={"refreshToken": logout_refresh}
    )
    check("退出登录", status == 200 and payload(body).get("revoked") is True)
    status, _, body = http(
        "POST", "/api/v1/auth/refresh", json_body={"refreshToken": logout_refresh}
    )
    check("退出后 refresh 失效", status == 401)

    status, _, body = http(
        "POST", "/api/v1/auth/login",
        json_body={"email": "admin@test.local", "password": "admin-secret-123"},
    )
    admin = payload(body)
    check("管理员登录", status == 200 and admin["profile"].get("role") == "admin")

    status, _, body = http("GET", "/api/v1/admin/users", token=admin["accessToken"])
    users = payload(body)
    check("管理员可查看用户列表", status == 200 and users.get("total", 0) >= 2)
    expected_fields = {
        "uid", "email", "nickname", "role", "plan", "org",
        "depts", "primaryDept", "createdAtMs",
    }
    check(
        "用户列表字段最小化",
        all(set(row) == expected_fields for row in users.get("users", [])),
    )
    status, _, body = http("GET", "/api/v1/admin/users", token=access_a2)
    check("普通用户无权查看用户列表", status == 403 and payload(body).get("code") == "FORBIDDEN")
    status, _, body = http(
        "GET",
        "/api/v1/admin/users?q=" + urllib.parse.quote(email_a),
        token=admin["accessToken"],
    )
    found = [row["email"] for row in payload(body).get("users", [])]
    check("用户列表支持搜索", status == 200 and found == [email_a], str(found))

    conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
    conn.row_factory = sqlite3.Row
    user_row = conn.execute("SELECT password_hash FROM users WHERE email = ?", (email_a,)).fetchone()
    plaintext_hit = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE refresh_hash = ?", (rotated["refreshToken"],)
    ).fetchone()["n"]
    conn.close()
    check("密码不以明文落库", user_row is not None and "secret123" not in user_row["password_hash"])
    check("refresh token 只存哈希", plaintext_hit == 0)

    return {
        "email_a": email_a,
        "access_a": access_a2,
        "profile_a": payload(http("GET", "/api/v1/auth/me", token=access_a2)[2])["profile"],
        "admin_access": admin["accessToken"],
    }


def test_legacy_web_login():
    print("\n[网页验证登录（loopback）]")
    stamp = int(time.time())
    email_b = f"browser{stamp}@test.local"
    password = "secret123"
    state = "state-e2e-legacy"
    callback = "http://127.0.0.1:19999/callback"

    status, headers, _ = http(
        "GET", f"/auth/login?state={state}&redirect_uri={urllib.parse.quote(callback)}"
    )
    csp = headers.get("Content-Security-Policy") or ""
    check("登录页可访问且带 CSP", status == 200 and csp != "")
    # Chrome 会把 form-action 套到表单 302 回跳上，跨端口 loopback 必须显式放行
    check("CSP 放行 loopback 回跳端口", "form-action 'self' http://127.0.0.1:19999" in csp, csp)

    evil = "https://evil.example.com/callback"
    status, headers, _ = http(
        "GET", f"/auth/login?state={state}&redirect_uri={urllib.parse.quote(evil)}"
    )
    evil_csp = headers.get("Content-Security-Policy") or ""
    check(
        "CSP 不放行非 loopback 回跳",
        status == 200 and "evil.example.com" not in evil_csp and "form-action 'self';" in evil_csp,
        evil_csp,
    )

    status, headers, body = http(
        "POST",
        "/api/auth/register",
        form={
            "nickname": "浏览器用户B",
            "email": email_b,
            "password": password,
            "confirm": password,
            "state": state,
            "redirect_uri": callback,
        },
    )
    check("网页注册 302 回跳", status == 302, f"实际 {status}")
    location = headers.get("Location", "")
    params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).items()}
    check("回跳携带 code/state", bool(params.get("code")) and params.get("state") == state)
    code = params["code"]

    status, _, body = http(
        "POST", "/api/auth/exchange", json_body={"code": code, "state": "wrong-state"}
    )
    check("state 不匹配被拒", status == 400 and payload(body).get("code") == "INVALID_CODE")
    status, _, body = http(
        "POST", "/api/auth/exchange", json_body={"code": code, "state": state}
    )
    exchanged = payload(body)
    check("授权码换 token", status == 200 and exchanged.get("ok") is True)
    check("返回桌面端 profile", exchanged["profile"].get("email") == email_b)
    status, _, body = http(
        "POST", "/api/auth/exchange", json_body={"code": code, "state": state}
    )
    check("授权码一次性", status == 400)

    status, _, body = http(
        "POST", "/api/auth/refresh", json_body={"refreshToken": exchanged["refreshToken"]}
    )
    legacy_rotated = payload(body)
    check("legacy refresh 轮换", status == 200 and legacy_rotated.get("refreshToken"))
    status, _, body = http(
        "POST", "/api/auth/refresh", json_body={"refreshToken": exchanged["refreshToken"]}
    )
    check("legacy 旧 refresh 失效", status == 401)
    status, _, body = http(
        "POST", "/api/auth/logout", json_body={"refreshToken": legacy_rotated["refreshToken"]}
    )
    check("legacy logout", status == 200 and payload(body).get("revoked") is True)

    status, headers, _ = http(
        "POST",
        "/api/auth/login",
        form={
            "email": email_b,
            "password": password,
            "state": "state-login",
            "redirect_uri": callback,
        },
    )
    check("网页登录 302", status == 302)
    location = headers.get("Location", "")
    check(
        "登录回跳带新 code",
        "code=" in location and "state=state-login" in location,
    )
    # refresh 轮换会吊销旧会话（含旧 access token），这里换刚拿到的 code 获取可用令牌
    fresh_code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code"][0]
    status, _, body = http(
        "POST", "/api/auth/exchange", json_body={"code": fresh_code, "state": "state-login"}
    )
    check("网页登录授权码可换 token", status == 200 and payload(body).get("ok") is True)
    access_b = payload(body)["accessToken"]

    status, _, body = http(
        "POST",
        "/api/auth/login",
        form={
            "email": email_b,
            "password": password,
            "state": "s",
            "redirect_uri": "https://evil.example.com/callback",
        },
    )
    check("非本机回跳被拒", status == 400 and "回调地址非法" in body.decode("utf-8"))
    status, _, body = http(
        "POST",
        "/api/auth/exchange",
        json_body={"code": "missing", "state": "s"},
    )
    check("非法授权码被拒", status == 400 and payload(body).get("code") == "INVALID_CODE")

    return {"email_b": email_b, "access_b": access_b}


def test_auth_claims(ctx):
    print("\n[令牌声明与吊销]")
    stamp = int(time.time())
    admin = ctx["admin_access"]
    email = f"claims{stamp}@test.local"
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": "声明用户"},
    )
    data = payload(body)
    uid = data["profile"]["uid"]
    access, refresh = data["accessToken"], data["refreshToken"]
    check("注册成功", status == 201)
    check("默认个人组织", data["profile"].get("org") == f"personal-{uid}")
    check(
        "默认免费配额",
        data["profile"].get("quota") == {"monthlyTokens": 100_000, "concurrentTasks": 2},
    )

    claims = security.jwt_decode(access)
    refresh_claims = security.jwt_decode(refresh)
    check(
        "access 声明齐全",
        claims.get("iss") == "tongtian-auth"
        and claims.get("aud") == "tongtian-engine"
        and claims.get("sub") == uid
        and bool(claims.get("sid"))
        and claims.get("org") == f"personal-{uid}"
        and claims.get("iat", 0) > 0
        and claims.get("exp", 0) > claims.get("iat", 0),
    )
    check("refresh 与 access 同会话", refresh_claims.get("sid") == claims.get("sid"))

    forged = security.jwt_encode(
        {
            "sub": uid,
            "sid": claims["sid"],
            "role": "user",
            "org": claims["org"],
            "quota": claims["quota"],
            "iss": "tongtian-auth",
            "aud": "someone-else",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "type": "access",
        }
    )
    status, _, body = http("GET", "/api/v1/auth/me", token=forged)
    check("aud 不匹配被拒", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")

    legacy = security.jwt_encode(
        {
            "sub": uid,
            "type": "access",
            "role": "user",
            "iss": "tongtian-auth",
            "aud": "tongtian-engine",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        }
    )
    status, _, body = http("GET", "/api/v1/auth/me", token=legacy)
    check("无 sid 旧令牌被拒", status == 401)

    status, _, body = http(
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        json_body={"org": "acme", "quota": {"monthlyTokens": 5000, "concurrentTasks": 3}},
        token=admin,
    )
    patched = payload(body)
    check("管理员改声明成功", status == 200 and patched.get("sessionsRevoked") == 1)
    check("返回新组织", patched["profile"].get("org") == "acme")
    check(
        "返回新配额",
        patched["profile"].get("quota") == {"monthlyTokens": 5000, "concurrentTasks": 3},
    )

    status, _, _ = http("GET", "/api/v1/auth/me", token=access)
    check("旧 access 立即失效", status == 401)
    status, _, body = http("POST", "/api/v1/auth/refresh", json_body={"refreshToken": refresh})
    check("旧 refresh 同步失效", status == 401 and payload(body).get("code") == "INVALID_REFRESH")

    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": "secret123"}
    )
    relogin = payload(body)
    new_access = relogin["accessToken"]
    new_claims = security.jwt_decode(new_access)
    check("重新登录带新组织", status == 200 and new_claims.get("org") == "acme")
    check("重新登录带新配额", new_claims.get("quota") == {"monthlyTokens": 5000, "concurrentTasks": 3})
    status, _, _ = http("GET", "/api/v1/auth/me", token=new_access)
    check("新令牌可正常访问", status == 200)

    status, _, body = http(
        "PATCH", f"/api/v1/admin/users/{uid}", json_body={"org": "other"}, token=new_access
    )
    check("非管理员被拒", status == 403 and payload(body).get("code") == "FORBIDDEN")
    status, _, body = http(
        "PATCH", f"/api/v1/admin/users/{uid}", json_body={"org": "Bad Org"}, token=admin
    )
    check("非法组织 400", status == 400 and payload(body).get("code") == "INVALID_ORG")
    status, _, body = http(
        "PATCH", f"/api/v1/admin/users/{uid}", json_body={"quota": {"monthlyTokens": -1}}, token=admin
    )
    check("非法配额 400", status == 400 and payload(body).get("code") == "INVALID_QUOTA")
    status, _, body = http(
        "PATCH", f"/api/v1/admin/users/{uid}", json_body={"extra": 1}, token=admin
    )
    check("声明多余字段 422", status == 422 and payload(body).get("code") == "VALIDATION_ERROR")
    status, _, body = http("PATCH", f"/api/v1/admin/users/{uid}", json_body={}, token=admin)
    check("空更新 400", status == 400 and payload(body).get("code") == "NOTHING_TO_UPDATE")
    status, _, body = http(
        "PATCH", "/api/v1/admin/users/tt-missing", json_body={"org": "x"}, token=admin
    )
    check("未知用户 404", status == 404 and payload(body).get("code") == "USER_NOT_FOUND")

    status, _, body = http(
        "POST", f"/api/v1/admin/users/{uid}/revoke-sessions", token=admin
    )
    check("管理员强制下线", status == 200 and payload(body).get("revoked") == 1)
    status, _, _ = http("GET", "/api/v1/auth/me", token=new_access)
    check("强制下线后令牌失效", status == 401)
    status, _, body = http("POST", "/api/v1/admin/users/tt-missing/revoke-sessions", token=admin)
    check("未知用户强制下线 404", status == 404)

    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": "secret123"}
    )
    last_access = payload(body)["accessToken"]
    status, _, body = http(
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        json_body={"org": None, "quota": None},
        token=admin,
    )
    cleared = payload(body)
    check("清空组织回到个人", status == 200 and cleared["profile"].get("org") == f"personal-{uid}")
    check(
        "清空配额回到默认",
        cleared["profile"].get("quota") == {"monthlyTokens": 100_000, "concurrentTasks": 2},
    )
    status, _, _ = http("GET", "/api/v1/auth/me", token=last_access)
    check("清空后旧令牌失效", status == 401)


def test_engine_introspect(ctx):
    print("\n[引擎令牌吊销内省]")
    stamp = int(time.time() * 1000) % 1_000_000
    email = f"introspect{stamp}@test.local"
    # 未启用（无共享令牌）→ 404，引擎侧对不可达按放行处理
    os.environ.pop("TT_ENGINE_INTROSPECT_TOKEN", None)
    status, _, body = http(
        "GET", "/api/v1/engine/introspect?sub=x&sid=y",
        extra_headers={"x-tt-engine-introspect": "whatever"},
    )
    check("未启用内省 → 404", status == 404 and payload(body).get("code") == "INTROSPECT_DISABLED")

    os.environ["TT_ENGINE_INTROSPECT_TOKEN"] = "it-selftest-secret"
    # 令牌错误 → 401
    status, _, body = http(
        "GET", "/api/v1/engine/introspect?sub=x&sid=y",
        extra_headers={"x-tt-engine-introspect": "wrong"},
    )
    check("内省令牌错误 → 401", status == 401 and payload(body).get("code") == "INTROSPECT_UNAUTHORIZED")
    # 缺少令牌头 → 401
    status, _, _ = http("GET", "/api/v1/engine/introspect?sub=x&sid=y")
    check("缺少内省令牌头 → 401", status == 401)
    # sub/sid 缺失 → 400
    status, _, body = http(
        "GET", "/api/v1/engine/introspect?sub=&sid=",
        extra_headers={"x-tt-engine-introspect": "it-selftest-secret"},
    )
    check("sub/sid 缺失 → 400", status == 400 and payload(body).get("code") == "INVALID_INTROSPECT_QUERY")

    hdr = {"x-tt-engine-introspect": "it-selftest-secret"}
    # 有效会话 → active=true（先注册再登录，自包含）
    status, _, body = http(
        "POST", "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123"},
    )
    check("内省测试用户注册", status == 201)
    status, _, body = http(
        "POST", "/api/v1/auth/login",
        json_body={"email": email, "password": "secret123"},
    )
    live_access = payload(body)["accessToken"]
    live = security.jwt_decode(live_access)
    status, _, body = http(
        "GET", f"/api/v1/engine/introspect?sub={live['sub']}&sid={live['sid']}",
        extra_headers=hdr,
    )
    check("有效会话内省 active=true", status == 200 and payload(body).get("active") is True)

    # 强制下线该用户全部会话 → active=false（吊销可被引擎即时内省，无需等 exp）
    status, _, _ = http(
        "POST", f"/api/v1/admin/users/{live['sub']}/revoke-sessions", token=ctx["admin_access"]
    )
    check("强制下线成功", status == 200)
    status, _, body = http(
        "GET", f"/api/v1/engine/introspect?sub={live['sub']}&sid={live['sid']}",
        extra_headers=hdr,
    )
    check(
        "已吊销会话内省 active=false",
        status == 200 and payload(body).get("active") is False
        and payload(body).get("reason") == "session_revoked",
        payload(body),
    )
    os.environ.pop("TT_ENGINE_INTROSPECT_TOKEN", None)


def test_skill_market(ctx):
    print("\n[技能市场]")
    access_a = ctx["access_a"]
    data_v1 = skill_zip("weekly-digest", "1.0.0", "把零散进展整理成周报")
    status, _, body = publish_skill(
        access_a, "weekly-digest", data_v1,
        summary="把零散进展整理成周报", changelog="首次发布",
    )
    item = payload(body).get("item", {})
    check("技能发布 201", status == 201 and payload(body).get("ok") is True, payload(body).get("message", ""))
    check("技能标识", item.get("id") == "weekly-digest")
    check("最新版本", item.get("latestVersion") == "1.0.0" and item.get("versionCount") == 1)
    check("作者信息", item.get("author", {}).get("uid") == ctx["profile_a"]["uid"])
    check("包内标签解析", item.get("tags") == ["周报", "总结"])
    check("下载地址", "version=1.0.0" in (item.get("downloadUrl") or ""))
    check("安装提示", item.get("install", {}).get("entry") == "SKILL.md")
    check("文件清单", item.get("packageInfo", {}).get("fileCount") == 2)

    status, _, body = http("GET", "/api/v1/skills/weekly-digest")
    detail = payload(body).get("item", {})
    check("技能详情", status == 200 and detail.get("name") == "weekly-digest")
    check("详情含 readme", "## 使用方式" in detail.get("readme", ""))

    status, _, body = http("GET", "/api/v1/skills?q=" + urllib.parse.quote("周报"))
    check("搜索命中", status == 200 and payload(body).get("total") == 1)
    status, _, body = http("GET", "/api/v1/skills?category=" + urllib.parse.quote("效率"))
    check("分类筛选", status == 200 and payload(body).get("total") == 1)
    status, _, body = http("GET", "/api/v1/skills?category=" + urllib.parse.quote("不存在"))
    check("非法分类被拒", status == 400 and payload(body).get("code") == "INVALID_CATEGORY")
    status, _, body = http("GET", "/api/v1/skills?sort=invalid")
    check("非法排序被拒", status == 400 and payload(body).get("code") == "INVALID_SORT")
    status, _, body = http("GET", "/api/v1/skills?featured=maybe")
    check("非法 featured 被拒", status == 400)

    status, headers, downloaded = http("GET", item["downloadUrl"])
    check("下载成功", status == 200 and downloaded == data_v1)
    check("SHA-256 响应头", headers.get("X-Checksum-Sha256") == hashlib.sha256(data_v1).hexdigest())
    check("版本响应头", headers.get("X-Item-Version") == "1.0.0")
    status, _, body = http("GET", "/api/v1/skills/weekly-digest")
    check("下载计数递增", payload(body)["item"].get("downloads") == 1)

    data_v2 = skill_zip("weekly-digest", "1.1.0", "支持自定义周报模板")
    status, _, body = publish_skill_version(
        access_a, "weekly-digest", data_v2, changelog="新增模板"
    )
    item = payload(body).get("item", {})
    check("发布新版本 201", status == 201 and item.get("latestVersion") == "1.1.0")
    check("版本列表两条", len(item.get("versions", [])) == 2)
    status, _, body = publish_skill_version(access_a, "weekly-digest", data_v2)
    check("重复版本被拒", status == 409 and payload(body).get("code") == "VERSION_EXISTS")
    status, _, body = publish_skill_version(
        ctx["access_b"], "weekly-digest", skill_zip("weekly-digest", "2.0.0")
    )
    check("他人不能发版", status == 403 and payload(body).get("code") == "FORBIDDEN")
    status, _, body = publish_skill_version(
        access_a, "weekly-digest", skill_zip("other-skill", "3.0.0")
    )
    check("包标识不一致被拒", status == 400 and payload(body).get("code") == "PACKAGE_ID_MISMATCH")
    status, headers, downloaded = http("GET", "/api/v1/skills/weekly-digest/download?version=1.0.0")
    check("旧版本仍可下载", status == 200 and downloaded == data_v1)
    status, headers, downloaded = http("GET", "/api/v1/skills/weekly-digest/download")
    check("默认下载最新版本", status == 200 and downloaded == data_v2)
    check("默认版本响应头", headers.get("X-Item-Version") == "1.1.0")
    status, _, body = http("GET", "/api/v1/skills/weekly-digest/download?version=1.0")
    check("非法版本参数被拒", status == 400 and payload(body).get("code") == "INVALID_VERSION")

    status, _, body = publish_skill(access_a, "weekly-digest", skill_zip("weekly-digest", "9.9.9"))
    check("重复条目被拒", status == 409 and payload(body).get("code") == "ITEM_EXISTS")

    print("\n[包损坏检测]")
    conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
    row = conn.execute(
        "SELECT package_path FROM item_versions WHERE item_type = 'skill' "
        "AND item_id = 'weekly-digest' AND version = '1.0.0'"
    ).fetchone()
    conn.close()
    package_file = os.path.join(_TMP, row[0].replace("/", os.sep))
    with open(package_file, "ab") as handle:
        handle.write(b"tampered")
    status, _, body = http("GET", "/api/v1/skills/weekly-digest/download?version=1.0.0")
    check("包体积异常返回 410", status == 410 and payload(body).get("code") == "PACKAGE_CORRUPTED")

    print("\n[包安全校验]")
    status, _, body = publish_skill(
        access_a, "zip-slip", make_zip({"SKILL.md": "---\nname: zip-slip\n---\nx", "../evil.txt": "x"})
    )
    check("ZIP 路径穿越被拒", status == 400 and payload(body).get("code") == "INVALID_SKILL_PACKAGE")
    status, _, body = publish_skill(access_a, "no-skill", make_zip({"README.md": "x"}))
    check("缺少 SKILL.md 被拒", status == 400 and payload(body).get("code") == "INVALID_SKILL_PACKAGE")
    status, _, body = publish_skill(
        access_a, "bad-zip", b"not a zip"
    )
    check("非 ZIP 被拒", status == 400 and payload(body).get("code") == "INVALID_SKILL_PACKAGE")

    return {"skill_zip": data_v1}


def test_suite_market(ctx):
    print("\n[专家套件市场]")
    access_a = ctx["access_a"]
    name = "合同审查专家套件"
    plugin = {
        "name": name,
        "version": "1.0.0",
        "description": "覆盖合同条款审查与风险提示的岗位工作流包。",
        "author": "@测试发布者",
        "category": "律师",
        "commands": [
            {"name": "contract-review", "description": "审查关键条款"},
            {"name": "risk-check", "description": "输出风险清单"},
        ],
        "skills": [{"name": "contract-review"}],
        "dataLinks": [{"name": "本地工作目录"}],
    }
    data = make_zip(
        {
            "contract-suite/plugin.json": json.dumps(plugin, ensure_ascii=False),
            "contract-suite/README.md": "# 合同审查专家套件\n\n演示说明",
            "contract-suite/skills/contract-review/SKILL.md": "---\nname: contract-review\n---\n正文",
        }
    )
    status, _, body = http(
        "POST",
        "/api/v1/suites",
        fields={"changelog": "首次发布"},
        files={"package": ("contract-suite.zip", data, "application/zip")},
        token=access_a,
    )
    item = payload(body).get("item", {})
    expected_id = security.suite_id_of(name)
    check("套件发布 201", status == 201, payload(body).get("message", ""))
    check("套件标识与桌面端一致", item.get("id") == expected_id)
    check("套件指令解析", [c["name"] for c in item.get("commands", [])] == ["contract-review", "risk-check"])
    check("套件技能计数", item.get("packageInfo", {}).get("skillCount") == 1)
    check("套件文件数", item.get("packageInfo", {}).get("fileCount") == 3)
    check("套件分类", item.get("category") == "律师")

    status, _, body = http("GET", "/api/v1/suites?category=" + urllib.parse.quote("律师"))
    check("套件分类筛选", status == 200 and payload(body).get("total") == 1)
    status, headers, downloaded = http("GET", item["downloadUrl"])
    check("套件下载", status == 200 and downloaded == data)

    plugin_v2 = dict(plugin, version="1.1.0", description="新增试用期风险检查。")
    data_v2 = make_zip(
        {
            "contract-suite/plugin.json": json.dumps(plugin_v2, ensure_ascii=False),
            "contract-suite/README.md": "# 合同审查专家套件\n\n第二版",
        }
    )
    status, _, body = http(
        "POST",
        f"/api/v1/suites/{expected_id}/versions",
        files={"package": ("contract-suite.zip", data_v2, "application/zip")},
        token=access_a,
    )
    check("套件新版本", status == 201 and payload(body)["item"].get("latestVersion") == "1.1.0")
    return {"suite_id": expected_id}


def test_mcp_market(ctx):
    print("\n[MCP 插件市场]")
    access_a = ctx["access_a"]
    access_b = ctx["access_b"]
    payload_v1 = {
        "name": "Repo Insights MCP",
        "version": "1.0.0",
        "summary": "读取仓库 Issue 与 PR 信息",
        "description": "演示：需要配置个人访问令牌，令牌值不会上传到市场服务端。",
        "category": "开发",
        "tags": ["GitHub", "仓库"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "envKeys": [
            {
                "key": "GITHUB_PERSONAL_ACCESS_TOKEN",
                "description": "个人访问令牌",
                "required": True,
                "sensitive": True,
            }
        ],
        "timeoutSecs": 30,
        "readme": "# Repo Insights MCP\n\n需要令牌。",
    }
    status, _, body = http("POST", "/api/v1/mcp", json_body=payload_v1, token=access_a)
    item = payload(body).get("item", {})
    check("MCP 发布 201", status == 201, payload(body).get("message", ""))
    check("MCP 标识", item.get("id") == "repo-insights-mcp")
    check("stdio 配置", item.get("mcp", {}).get("transport") == "stdio")
    check("环境变量只登记键名", item.get("mcp", {}).get("envKeys", [{}])[0].get("key")
          == "GITHUB_PERSONAL_ACCESS_TOKEN")
    check("配置标记本地执行", item.get("mcp", {}).get("requiresLocalExecution") is True)
    check("MCP 无下载地址", item.get("downloadUrl") is None)

    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/config")
    config = payload(body)
    check("配置接口可用", status == 200 and config.get("version") == "1.0.0")
    check("配置不含密钥值", "value" not in config["mcp"]["envKeys"][0])

    invalid_cases = [
        ("非法传输方式", dict(payload_v1, name="Bad Transport MCP", transport="websocket"), "INVALID_MCP_CONFIG"),
        ("stdio 不允许 url", dict(payload_v1, name="Bad Stdio MCP", url="https://example.com"), "INVALID_MCP_CONFIG"),
        ("远程不允许 command", dict(
            payload_v1, name="Bad Remote MCP", transport="http", command="npx",
            url="https://example.com/mcp", envKeys=[],
        ), "INVALID_MCP_CONFIG"),
        ("URL 内嵌凭据被拒", dict(
            payload_v1, name="Credential URL MCP", transport="http", command=None, args=[],
            url="https://user:pass@example.com/mcp", envKeys=[],
        ), "INVALID_MCP_CONFIG"),
        ("非法版本号", dict(payload_v1, name="Bad Version MCP", version="1.0"), "INVALID_VERSION"),
    ]
    for label, body_json, code in invalid_cases:
        status, _, body = http("POST", "/api/v1/mcp", json_body=body_json, token=access_a)
        check(label, status == 400 and payload(body).get("code") == code,
              f"{status}/{payload(body).get('code')}")

    status, _, body = http(
        "POST", "/api/v1/mcp",
        json_body=dict(payload_v1, name="Secret Value MCP", env={"TOKEN": "plaintext"}),
        token=access_a,
    )
    check("拒绝携带密钥值字段", status == 422 and payload(body).get("code") == "VALIDATION_ERROR")

    remote = {
        "name": "Remote Search MCP",
        "version": "1.0.0",
        "summary": "远程搜索连接器示例",
        "description": "演示远程 streamable_http 配置。",
        "category": "搜索",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "headerKeys": [
            {"key": "Authorization", "description": "Bearer <token>", "sensitive": True}
        ],
        "timeoutSecs": 20,
    }
    status, _, body = http("POST", "/api/v1/mcp", json_body=remote, token=access_a)
    remote_item = payload(body).get("item", {})
    check("远程 MCP 发布", status == 201)
    check("传输方式归一", remote_item.get("mcp", {}).get("transport") == "streamable_http")
    check("远程不要求本地执行", remote_item.get("mcp", {}).get("requiresLocalExecution") is False)
    check("请求头键名登记", remote_item.get("mcp", {}).get("headerKeys", [{}])[0].get("key") == "Authorization")

    version_base = {key: value for key, value in payload_v1.items() if key != "name"}
    status, _, body = http(
        "POST",
        "/api/v1/mcp/repo-insights-mcp/versions",
        json_body=dict(version_base, version="1.1.0", summary="新增 PR 摘要"),
        token=access_a,
    )
    check("MCP 新版本", status == 201 and payload(body)["item"].get("latestVersion") == "1.1.0")
    status, _, body = http(
        "POST",
        "/api/v1/mcp/repo-insights-mcp/versions",
        json_body=dict(version_base, version="1.1.0"),
        token=access_a,
    )
    check("MCP 重复版本被拒", status == 409 and payload(body).get("code") == "VERSION_EXISTS")
    status, _, body = http(
        "POST",
        "/api/v1/mcp/repo-insights-mcp/versions",
        json_body=dict(version_base, version="2.0.0"),
        token=access_b,
    )
    check("他人不能发 MCP 版本", status == 403)
    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/download")
    check("MCP 没有下载包", status == 404 and payload(body).get("code") == "NO_PACKAGE")
    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/config?version=1.0.0")
    check("指定版本配置", status == 200 and payload(body).get("version") == "1.0.0")
    return {"mcp_id": "repo-insights-mcp"}


def test_interactions(ctx):
    print("\n[收藏与推荐]")
    access_a, access_b = ctx["access_a"], ctx["access_b"]
    mcp_id = ctx["mcp_id"]
    status, _, body = http("POST", f"/api/v1/mcp/{mcp_id}/star", token=access_a)
    check("收藏成功", status == 200 and payload(body).get("starred") is True)
    check("收藏计数 1", payload(body).get("stars") == 1)
    status, _, body = http("GET", f"/api/v1/mcp/{mcp_id}", token=access_a)
    check("本人详情显示已收藏", payload(body)["item"].get("starred") is True)
    status, _, body = http("GET", f"/api/v1/mcp/{mcp_id}", token=access_b)
    check("他人详情未收藏", payload(body)["item"].get("starred") is False)
    check("收藏总数共享", payload(body)["item"].get("stars") == 1)
    status, _, body = http("POST", f"/api/v1/mcp/{mcp_id}/star", token=access_b)
    check("第二个用户收藏", status == 200 and payload(body).get("stars") == 2)
    status, _, body = http("POST", f"/api/v1/mcp/{mcp_id}/star", token=access_a)
    check("再次点击取消收藏", status == 200 and payload(body).get("starred") is False)
    check("取消后计数 1", payload(body).get("stars") == 1)
    status, _, body = http("POST", f"/api/v1/mcp/{mcp_id}/star")
    check("未登录收藏 401", status == 401)
    status, _, body = http("GET", "/api/v1/mcp?sort=stars")
    check("按收藏排序", status == 200 and payload(body)["items"][0]["id"] == mcp_id)

    status, _, body = http(
        "POST",
        f"/api/v1/admin/items/skill/{ctx['skill_id']}/feature",
        json_body={"featured": True},
        token=access_a,
    )
    check("普通用户不能推荐", status == 403 and payload(body).get("code") == "FORBIDDEN")
    status, _, body = http(
        "POST",
        f"/api/v1/admin/items/skill/{ctx['skill_id']}/feature",
        json_body={"featured": True},
        token=ctx["admin_access"],
    )
    check("管理员推荐", status == 200 and payload(body)["item"].get("featured") is True)
    status, _, body = http("GET", "/api/v1/skills?featured=true")
    check("推荐筛选", status == 200 and payload(body).get("total") == 1)


def test_my_items(ctx):
    print("\n[我的发布]")
    access_a, access_b = ctx["access_a"], ctx["access_b"]
    status, _, body = http("GET", "/api/v1/me/items", token=access_a)
    data = payload(body)
    check("我的发布列表", status == 200 and data.get("total") == 4, f"实际 {data.get('total')}")
    check("全部属于本人", all(row["author"]["uid"] == ctx["profile_a"]["uid"] for row in data["items"]))
    status, _, body = http("GET", "/api/v1/me/items", token=access_b)
    check("他人没有发布", status == 200 and payload(body).get("total") == 0)
    status, _, body = http("GET", "/api/v1/me/items")
    check("未登录查看我的发布 401", status == 401)


def test_seed(ctx):
    print("\n[演示数据幂等]")
    def totals():
        return {
            "skills": payload(http("GET", "/api/v1/skills?pageSize=100")[2]).get("total"),
            "suites": payload(http("GET", "/api/v1/suites?pageSize=100")[2]).get("total"),
            "mcp": payload(http("GET", "/api/v1/mcp?pageSize=100")[2]).get("total"),
        }

    before = totals()
    seed.seed_demo()
    after_first = totals()
    seed.seed_demo()
    after_second = totals()
    counts = seed.SEED_COUNTS
    check(
        "演示技能按清单写入",
        after_first["skills"] == before["skills"] + counts["skill"],
        f"期望 +{counts['skill']}，实际 +{after_first['skills'] - before['skills']}",
    )
    check(
        "演示套件按清单写入",
        after_first["suites"] == before["suites"] + counts["suite"],
        f"期望 +{counts['suite']}，实际 +{after_first['suites'] - before['suites']}",
    )
    check(
        "演示 MCP 按清单写入",
        after_first["mcp"] == before["mcp"] + counts["mcp"],
        f"期望 +{counts['mcp']}，实际 +{after_first['mcp'] - before['mcp']}",
    )
    check("重复执行不新增", after_second == after_first)
    check(
        "演示数据总量",
        after_first
        == {
            "skills": before["skills"] + counts["skill"],
            "suites": before["suites"] + counts["suite"],
            "mcp": before["mcp"] + counts["mcp"],
        },
        str(after_first),
    )

    # 演示数据的目标是「打开就有内容的市场」：每个分类都必须有条目
    for item_type in ("skill", "suite", "mcp"):
        status, _, body = http("GET", f"/api/v1/categories?type={item_type}")
        categories = payload(body).get("categories", [])
        empty = [row["name"] for row in categories if row.get("count", 0) == 0]
        check(
            f"演示数据覆盖 {item_type} 全部 {len(categories)} 个分类",
            status == 200 and categories and not empty,
            f"空分类：{empty}",
        )

    status, _, body = http(
        "POST", "/api/v1/auth/login",
        json_body={"email": seed.DEMO_EMAIL, "password": os.environ["TT_DEMO_PASSWORD"]},
    )
    check("演示账号可登录", status == 200 and payload(body)["profile"]["nickname"] == seed.DEMO_NICKNAME)
    demo_access = payload(body)["accessToken"]
    status, _, body = http("GET", "/api/v1/me/items?pageSize=100", token=demo_access)
    check(
        "演示账号拥有全部演示条目",
        status == 200 and payload(body).get("total") == sum(counts.values()),
        f"实际 {payload(body).get('total')}，期望 {sum(counts.values())}",
    )

    total_skills = after_first["skills"]
    status, _, body = http("GET", "/api/v1/skills?pageSize=2&page=1&sort=name")
    first_page = payload(body)
    check(
        "分页第一页",
        status == 200 and len(first_page["items"]) == 2 and first_page["hasMore"] is (total_skills > 2),
    )
    status, _, body = http("GET", "/api/v1/skills?pageSize=2&page=2&sort=name")
    second_page = payload(body)
    check(
        "分页第二页",
        status == 200 and len(second_page["items"]) == min(2, max(0, total_skills - 2))
        and second_page["hasMore"] is (total_skills > 4),
    )

    # 取自公开库的演示技能必须随包带着许可与出处，否则不予收录
    status, _, body = http("GET", "/api/v1/skills/copywriting")
    files = payload(body).get("item", {}).get("packageInfo", {}).get("files", [])
    check("公开库技能随包许可与出处", status == 200 and "LICENSE.txt" in files and "SOURCE.txt" in files, str(files))


def test_market_revoke(ctx):
    print("\n[市场撤销与下架]")
    access_a = ctx["access_a"]
    admin = ctx["admin_access"]

    data_v1 = skill_zip("revoke-target", "1.0.0", "撤销测试用技能")
    status, _, body = publish_skill(
        access_a, "revoke-target", data_v1, summary="撤销测试用技能"
    )
    check("撤销样本发布", status == 201, payload(body).get("message", ""))
    item = payload(body)["item"]
    check("新条目未撤销", item.get("revoked") is False)
    data_v2 = skill_zip("revoke-target", "1.1.0", "撤销测试用技能第二版")
    status, _, body = publish_skill_version(access_a, "revoke-target", data_v2)
    check("撤销样本新版本", status == 201 and payload(body)["item"].get("latestVersion") == "1.1.0")

    revoke_path = "/api/v1/admin/items/skill/revoke-target/versions/1.1.0/revoke"
    status, _, body = http("POST", revoke_path, json_body={"reason": "存在越权读取"}, token=access_a)
    check("普通用户不能撤销", status == 403 and payload(body).get("code") == "FORBIDDEN")
    status, _, body = http("POST", revoke_path, json_body={"reason": "   "}, token=admin)
    check("撤销原因不能为空", status == 400 and payload(body).get("code") == "INVALID_FIELD")
    status, _, body = http("POST", revoke_path, json_body={"reason": "x" * 501}, token=admin)
    check("撤销原因超长被拒", status == 400 and payload(body).get("code") == "INVALID_FIELD")
    status, _, body = http(
        "POST",
        "/api/v1/admin/items/skill/revoke-target/versions/9.9.9/revoke",
        json_body={"reason": "不存在"},
        token=admin,
    )
    check("撤销未知版本 404", status == 404 and payload(body).get("code") == "VERSION_NOT_FOUND")
    status, _, body = http(
        "POST",
        "/api/v1/admin/items/skill/missing-item/versions/1.0.0/revoke",
        json_body={"reason": "不存在"},
        token=admin,
    )
    check("撤销未知条目 404", status == 404 and payload(body).get("code") == "ITEM_NOT_FOUND")
    status, _, body = http(
        "POST",
        "/api/v1/admin/items/skill/revoke-target/versions/1.0/revoke",
        json_body={"reason": "坏版本"},
        token=admin,
    )
    check("撤销非法版本号 400", status == 400 and payload(body).get("code") == "INVALID_VERSION")

    status, _, body = http("POST", revoke_path, json_body={"reason": "存在越权读取"}, token=admin)
    data = payload(body)
    item = data.get("item", {})
    check("管理员撤销版本", status == 200 and data.get("changed") is True and data.get("revokedAtMs", 0) > 0)
    check("最新版本回落到未撤销版本", item.get("latestVersion") == "1.0.0")
    revoked_at = data["revokedAtMs"]
    target = next(v for v in item.get("versions", []) if v["version"] == "1.1.0")
    check("版本视图标记撤销", target.get("revoked") is True and target.get("revokedReason") == "存在越权读取")
    check("撤销版本无下载地址", target.get("downloadUrl") is None)
    status, _, body = http("GET", "/api/v1/skills/revoke-target/download?version=1.1.0")
    check("撤销版本禁止下载", status == 410 and payload(body).get("code") == "ITEM_REVOKED")
    status, headers, _ = http("GET", "/api/v1/skills/revoke-target/download")
    check("默认下载回落到未撤销版本", status == 200 and headers.get("X-Item-Version") == "1.0.0")
    status, _, body = http("POST", revoke_path, json_body={"reason": "重复撤销"}, token=admin)
    check(
        "重复撤销幂等",
        status == 200 and payload(body).get("changed") is False
        and payload(body).get("revokedAtMs") == revoked_at,
    )

    status, _, body = http("GET", "/api/v1/revoked")
    data = payload(body)
    entry = next((row for row in data.get("items", []) if row["id"] == "revoke-target"), None)
    check("下架列表返回撤销版本", status == 200 and data.get("total") == 1 and entry is not None)
    check(
        "下架条目字段",
        entry is not None and entry.get("version") == "1.1.0"
        and entry.get("reason") == "存在越权读取" and entry.get("type") == "skill"
        and entry.get("name") == "revoke-target",
    )
    status, _, body = http("GET", "/api/v1/revoked?type=mcp")
    check("下架列表按类型筛选", status == 200 and payload(body).get("total") == 0)
    status, _, body = http("GET", "/api/v1/revoked?type=unknown")
    check("下架列表类型校验", status == 400 and payload(body).get("code") == "INVALID_TYPE")

    status, _, body = http(
        "POST",
        "/api/v1/admin/items/skill/revoke-target/versions/1.0.0/revoke",
        json_body={"reason": "全部下架"},
        token=admin,
    )
    item = payload(body).get("item", {})
    check("全部版本撤销后条目下架", status == 200 and item.get("revoked") is True and item.get("latestVersion") is None)
    check("下架条目无下载地址", item.get("downloadUrl") is None)
    check("版本数仍保留历史", item.get("versionCount") == 2)
    check("下架详情不再返回 readme", item.get("readme") == "")
    status, _, body = http("GET", "/api/v1/skills/revoke-target/download")
    check("全部下架后默认下载 410", status == 410 and payload(body).get("code") == "ITEM_REVOKED")
    status, _, body = http("GET", "/api/v1/skills?q=revoke-target")
    rows = [row for row in payload(body).get("items", []) if row["id"] == "revoke-target"]
    check("列表标记下架条目", len(rows) == 1 and rows[0].get("revoked") is True)
    status, _, body = http("GET", "/api/v1/revoked")
    check("下架列表包含两个版本", payload(body).get("total") == 2)

    status, _, body = http(
        "POST",
        "/api/v1/admin/items/mcp/repo-insights-mcp/versions/1.1.0/revoke",
        json_body={"reason": "配置注入风险"},
        token=admin,
    )
    check("撤销 MCP 版本", status == 200 and payload(body)["item"].get("latestVersion") == "1.0.0")
    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/config")
    check("MCP 默认配置回落到未撤销版本", status == 200 and payload(body).get("version") == "1.0.0")
    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/config?version=1.1.0")
    check("撤销 MCP 版本配置被拒", status == 410 and payload(body).get("code") == "ITEM_REVOKED")
    status, _, body = http(
        "POST",
        "/api/v1/admin/items/mcp/repo-insights-mcp/versions/1.0.0/revoke",
        json_body={"reason": "全部下架"},
        token=admin,
    )
    check("撤销 MCP 最后一个版本", status == 200)
    prefix = "/api/v1/admin/items/mcp/repo-insights-mcp/versions/"
    status, _, body = http("GET", "/api/v1/mcp/repo-insights-mcp/config")
    check("MCP 全部撤销后配置 410", status == 410 and payload(body).get("code") == "ITEM_REVOKED")
    http("POST", prefix + "1.0.0/unrevoke", token=admin)
    http("POST", prefix + "1.1.0/unrevoke", token=admin)

    unrevoke_1 = "/api/v1/admin/items/skill/revoke-target/versions/1.1.0/unrevoke"
    status, _, body = http("POST", unrevoke_1, token=admin)
    check(
        "恢复撤销版本",
        status == 200 and payload(body).get("changed") is True
        and payload(body)["item"].get("latestVersion") == "1.1.0",
    )
    status, _, body = http("POST", unrevoke_1, token=admin)
    check("重复恢复幂等", status == 200 and payload(body).get("changed") is False)
    status, headers, _ = http("GET", "/api/v1/skills/revoke-target/download?version=1.1.0")
    check("恢复后可下载", status == 200 and headers.get("X-Item-Version") == "1.1.0")
    http(
        "POST",
        "/api/v1/admin/items/skill/revoke-target/versions/1.0.0/unrevoke",
        token=admin,
    )
    status, _, body = http("GET", "/api/v1/revoked")
    check("全部恢复后下架列表为空", status == 200 and payload(body).get("total") == 0)


def register_user(email: str, nickname: str, password: str = "secret123") -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": password, "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def test_projects(ctx):
    print("\n[项目协作]")
    stamp = int(time.time() * 1000)
    owner_access = ctx["access_a"]
    member = register_user(f"collab-member-{stamp}@test.local", "协作成员")
    viewer = register_user(f"collab-viewer-{stamp}@test.local", "只读成员")
    outsider = register_user(f"collab-outside-{stamp}@test.local", "局外人")
    member_access = member["accessToken"]
    viewer_access = viewer["accessToken"]
    outsider_access = outsider["accessToken"]

    status, _, body = http(
        "POST",
        "/api/v1/projects",
        json_body={"name": "季度复盘", "description": "跨机器整理季度数据"},
        token=owner_access,
    )
    data = payload(body)
    project_id = data.get("project", {}).get("id", "")
    check("创建项目 201", status == 201 and project_id.startswith("prj-"))
    check("创建者即所有者", data["project"]["myRole"] == "owner" and data["project"]["memberCount"] == 1)
    check("初始任务计数为 0", data["project"]["taskCounts"] == {"todo": 0, "doing": 0, "review": 0, "done": 0})
    check("创建事件写入 seq", data.get("seq", 0) >= 1)
    create_seq = data.get("seq", 0)

    status, _, body = http("GET", "/api/v1/projects", token=owner_access)
    check("所有者项目列表可见", status == 200 and any(
        row["id"] == project_id for row in payload(body).get("projects", [])
    ))
    status, _, body = http("GET", "/api/v1/projects", token=member_access)
    check("未加入成员列表为空", status == 200 and payload(body).get("projects") == [])

    status, _, body = http("GET", f"/api/v1/projects/{project_id}", token=member_access)
    check("非成员读详情 404（不泄露存在性）", status == 404 and payload(body).get("code") == "PROJECT_NOT_FOUND")
    status, _, body = http("GET", "/api/v1/projects/prj-deadbeefdeadbeef", token=owner_access)
    check("非法项目 id 404", status == 404)

    # 邀请成员：邀请码只返回一次，列表不回显
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/invites",
        json_body={"email": member["profile"]["email"], "role": "member"},
        token=owner_access,
    )
    invite = payload(body)
    member_code = invite.get("inviteCode", "")
    check("邀请成员 201", status == 201 and len(member_code) >= 20)
    check("邀请列表不回显邀请码", member_code not in json.dumps(invite.get("invite", {}), ensure_ascii=False))
    status, _, body = http("GET", f"/api/v1/projects/{project_id}/invites", token=owner_access)
    check("待接受邀请可列出", status == 200 and len(payload(body).get("invites", [])) == 1)

    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": member_code}, token=outsider_access
    )
    check("邮箱不匹配不能接受邀请", status == 403 and payload(body).get("code") == "PROJECT_INVITE_EMAIL_MISMATCH")
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": member_code}, token=member_access
    )
    joined = payload(body)
    check("成员接受邀请", status == 200 and joined["project"]["myRole"] == "member")
    check("成员数 +1", joined["project"]["memberCount"] == 2)
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": member_code}, token=member_access
    )
    check("邀请码一次性", status == 400 and payload(body).get("code") == "PROJECT_INVITE_INVALID")
    status, _, body = http("GET", "/api/v1/projects", token=member_access)
    check("成员项目列表可见", status == 200 and len(payload(body).get("projects", [])) == 1)

    # 邀请只读成员
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/invites",
        json_body={"email": viewer["profile"]["email"], "role": "viewer"},
        token=owner_access,
    )
    viewer_code = payload(body).get("inviteCode", "")
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": viewer_code}, token=viewer_access
    )
    check("只读成员加入", status == 200 and payload(body)["project"]["myRole"] == "viewer")
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "只读尝试建任务"}, token=viewer_access,
    )
    check("只读成员不能建任务", status == 403 and payload(body).get("code") == "PROJECT_FORBIDDEN")

    # 任务：创建、版本冲突、评论、结果回报
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "整理季度数据", "detail": "输出结论与图表", "assigneeUid": member["profile"]["uid"]},
        token=owner_access,
    )
    task = payload(body).get("task", {})
    task_id = task.get("id", "")
    check("创建并指派任务", status == 201 and task.get("assignee", {}).get("uid") == member["profile"]["uid"])
    check("任务初始版本 1", task.get("version") == 1 and task.get("status") == "todo")

    status, _, body = http(
        "PATCH", f"/api/v1/projects/{project_id}/tasks/{task_id}",
        json_body={"version": 1, "status": "doing"}, token=member_access,
    )
    updated = payload(body).get("task", {})
    check("负责人可改状态", status == 200 and updated.get("status") == "doing" and updated.get("version") == 2)
    status, _, body = http(
        "PATCH", f"/api/v1/projects/{project_id}/tasks/{task_id}",
        json_body={"version": 1, "status": "done"}, token=owner_access,
    )
    check("旧版本写入 409", status == 409 and payload(body).get("code") == "PROJECT_CONFLICT")
    check("冲突说明带当前版本", "当前版本 2" in payload(body).get("message", ""))
    status, _, body = http(
        "PATCH", f"/api/v1/projects/{project_id}/tasks/{task_id}",
        json_body={"version": 2, "title": "普通成员改标题"}, token=member_access,
    )
    check("非创建者不能改标题", status == 403 and payload(body).get("code") == "PROJECT_FORBIDDEN")

    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks/{task_id}/comments",
        json_body={"body": "数据源已确认，今晚开始跑"}, token=member_access,
    )
    check("成员可评论", status == 201 and payload(body)["comment"]["body"].startswith("数据源"))
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks/{task_id}/result",
        json_body={"version": 2, "summary": "旁观者伪造结果"}, token=viewer_access,
    )
    check("非负责人不能回报结果", status == 403 and payload(body).get("code") == "PROJECT_FORBIDDEN")
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks/{task_id}/result",
        json_body={
            "version": 2,
            "summary": "季度结论已生成",
            "artifacts": ["C:\\Users\\member\\out\\季度复盘.md", "C:\\Users\\member\\out\\chart.png"],
            "machine": "member-pc",
            "status": "review",
        },
        token=member_access,
    )
    reported = payload(body).get("task", {})
    check("负责人回报结果", status == 200 and reported.get("status") == "review")
    check("结果版本递增", reported.get("version") == 3)
    check("产物只保留文件名", reported["result"]["artifacts"] == ["季度复盘.md", "chart.png"])
    check("结果记录机器与回报人", reported["result"]["machine"] == "member-pc" and reported["result"]["by"] == member["profile"]["uid"])
    status, _, body = http(
        "PATCH", f"/api/v1/projects/{project_id}/tasks/{task_id}",
        json_body={"version": 3, "status": "done"}, token=owner_access,
    )
    check("所有者验收任务", status == 200 and payload(body)["task"]["status"] == "done")

    status, _, body = http("GET", f"/api/v1/projects/{project_id}", token=owner_access)
    snapshot = payload(body)
    check("快照含成员与任务", len(snapshot["members"]) == 3 and len(snapshot["tasks"]) == 1)
    check("快照含评论", len(snapshot["comments"]) == 1)
    check("快照任务计数", snapshot["project"]["taskCounts"]["done"] == 1)
    check("成员视图不含邮箱", all("email" not in row for row in snapshot["members"]))
    check("快照不暴露邀请码哈希", "token_hash" not in json.dumps(snapshot, ensure_ascii=False))
    check("快照 seq 随变更递增", snapshot["seq"] > create_seq)
    listed_seq = payload(http("GET", "/api/v1/projects", token=owner_access)[2])["projects"][0].get("seq")
    check("列表 seq 与快照一致", listed_seq == snapshot["seq"])

    # 角色管理与移除
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/members/{member['profile']['uid']}/role",
        json_body={"role": "viewer"}, token=owner_access,
    )
    check("所有者可调整角色", status == 200)
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "降级后尝试建任务"}, token=member_access,
    )
    check("降为只读后不能建任务", status == 403)
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/members/{member['profile']['uid']}/role",
        json_body={"role": "member"}, token=owner_access,
    )
    check("角色可恢复", status == 200)
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/members/{viewer['profile']['uid']}/remove",
        token=owner_access,
    )
    check("所有者可移除成员", status == 200)
    status, _, body = http("GET", f"/api/v1/projects/{project_id}", token=viewer_access)
    check("被移除后读详情 404", status == 404)

    # 所有权转移与退出
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/transfer",
        json_body={"uid": member["profile"]["uid"]}, token=owner_access,
    )
    check("转移所有权", status == 200)
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/transfer",
        json_body={"uid": member["profile"]["uid"]}, token=owner_access,
    )
    check("非所有者不能转移", status == 403)
    status, _, body = http("POST", f"/api/v1/projects/{project_id}/leave", token=member_access)
    check("所有者不能直接退出", status == 400 and payload(body).get("code") == "PROJECT_OWNER_IMMUTABLE")
    status, _, body = http("POST", f"/api/v1/projects/{project_id}/leave", token=owner_access)
    check("原所有者退出", status == 200)
    status, _, body = http("GET", f"/api/v1/projects/{project_id}", token=owner_access)
    check("退出后读详情 404", status == 404)

    # 重新邀请已移除成员；邮箱不匹配的旧邀请码不能跨账号使用
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/invites",
        json_body={"email": viewer["profile"]["email"], "role": "member"}, token=member_access,
    )
    reinvite_code = payload(body).get("inviteCode", "")
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": reinvite_code},
        token=outsider_access,
    )
    check("转发邀请码仍被邮箱校验拦截", status == 403)
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept", json_body={"token": reinvite_code},
        token=viewer_access,
    )
    check("被移除成员可重新加入", status == 200 and payload(body)["project"]["memberCount"] == 2)

    # 未登录一律 401
    status, _, body = http("GET", "/api/v1/projects")
    check("未登录不能访问项目", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")


def open_sse(path: str, token: str, timeout: float = 8.0):
    request = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}
    )
    return urllib.request.urlopen(request, timeout=timeout)


def read_sse_event(resp, timeout_seconds: float = 8.0):
    """读取下一条命名事件；跳过注释心跳与空行，超时返回 None。"""
    deadline = time.time() + timeout_seconds
    event = ""
    while time.time() < deadline:
        try:
            raw = resp.readline()
        except (TimeoutError, socket.timeout):
            return None
        if not raw:
            return None
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except ValueError:
                data = None
            if event:
                return event, data
    return None


def test_project_events(ctx):
    print("\n[项目事件流]")
    stamp = int(time.time() * 1000)
    owner = register_user(f"evt-owner-{stamp}@test.local", "事件所有者")
    outsider = register_user(f"evt-out-{stamp}@test.local", "事件局外人")
    owner_access = owner["accessToken"]
    outsider_access = outsider["accessToken"]

    status, _, body = http(
        "POST", "/api/v1/projects", json_body={"name": "事件测试"}, token=owner_access
    )
    project_id = payload(body).get("project", {}).get("id", "")
    check("事件：创建项目", status == 201 and project_id.startswith("prj-"))

    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events", token=owner_access
    )
    data = payload(body)
    seqs = [event["seq"] for event in data.get("events", [])]
    check("事件列表可读", status == 200 and data["events"][0]["kind"] == "project.created")
    check("事件按 seq 升序", seqs == sorted(seqs))
    check("游标等于最后一条 seq", data.get("cursor") == seqs[-1])

    first_cursor = data["cursor"]
    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "事件任务"}, token=owner_access,
    )
    check("事件：创建任务", status == 201)
    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events?since={first_cursor}",
        token=owner_access,
    )
    data = payload(body)
    check("增量只返回新事件", [event["kind"] for event in data["events"]] == ["task.created"])
    check("增量游标前进", data["cursor"] > first_cursor)
    cursor = data["cursor"]

    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events", token=outsider_access
    )
    check("非成员不能读事件", status == 404 and payload(body).get("code") == "PROJECT_NOT_FOUND")
    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events?since=-1", token=owner_access
    )
    check("非法游标被拒绝", status in (400, 422))
    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events/stream", token=outsider_access
    )
    check("非成员不能订阅事件流", status == 404)

    resp = open_sse(
        f"/api/v1/projects/{project_id}/events/stream?since={cursor}", owner_access
    )
    try:
        ready = read_sse_event(resp)
        check(
            "SSE 首包为 ready",
            ready is not None and ready[0] == "ready" and ready[1].get("cursor") == cursor,
        )
        status, _, body = http(
            "POST", f"/api/v1/projects/{project_id}/tasks",
            json_body={"title": "实时推送任务"}, token=owner_access,
        )
        check("事件：实时任务创建", status == 201)
        pushed = read_sse_event(resp)
        check(
            "SSE 推送新事件",
            pushed is not None
            and pushed[0] == "message"
            and pushed[1].get("kind") == "task.created",
        )
        check("SSE 事件游标前进", pushed is not None and pushed[1].get("seq", 0) > cursor)
    finally:
        resp.close()


def sse_cursor(project_id: str, token: str) -> int:
    """当前项目事件水位：订阅要从这个 seq 之后起步，才量得到「新写入」而不是历史补拉。"""
    status, _, body = http("GET", f"/api/v1/projects/{project_id}/events?since=0", token=token)
    if status != 200:
        raise AssertionError(f"读取事件水位失败 {status}")
    return int(payload(body).get("cursor", 0))


def drain_until(resp, kind: str, limit: int = 6):
    """读到指定类型的事件为止，返回 (事件, 之前顺带读到的类型)。顺带的通常是上一轮的补拉。"""
    seen = []
    for _ in range(limit):
        ev = read_sse_event(resp, timeout_seconds=6.0)
        if ev is None:
            return None, seen
        if ev[0] == "message":
            if ev[1].get("kind") == kind:
                return ev[1], seen
            seen.append(ev[1].get("kind"))
        elif ev[0] == "closed":
            return None, seen + ["closed:" + str(ev[1].get("reason"))]
    return None, seen


def test_project_events_realtime(ctx):
    """D-06 服务端侧验收：两人同时操作 1 秒内可见、断线期间不丢事件、被移出项目时流明确结束。

    三条都直接对应桌面端订阅桥要依赖的语义，所以在这里钉死，不留到界面层再发现。
    """
    print("\n[项目事件流·实时与找回]")
    stamp = int(time.time() * 1000)
    owner = register_user(f"rt-owner-{stamp}@test.local", "实时所有者")
    member = register_user(f"rt-member-{stamp}@test.local", "实时成员")
    owner_access, member_access = owner["accessToken"], member["accessToken"]
    member_uid = member["profile"]["uid"]

    status, _, body = http(
        "POST", "/api/v1/projects", json_body={"name": "实时演练"}, token=owner_access
    )
    project_id = payload(body).get("project", {}).get("id", "")
    check("实时：创建项目", status == 201 and project_id.startswith("prj-"))

    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/invites",
        json_body={"email": member["profile"]["email"], "role": "member"}, token=owner_access,
    )
    invite_code = payload(body).get("inviteCode", "")
    status, _, body = http(
        "POST", "/api/v1/projects/invites/accept",
        json_body={"token": invite_code}, token=member_access,
    )
    check("实时：成员加入项目", status == 200 and payload(body)["project"]["myRole"] == "member")

    status, _, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "实时任务"}, token=owner_access,
    )
    task_id = payload(body).get("task", {}).get("id", "")
    check("实时：创建任务", status == 201 and task_id)

    # ① 1 秒内同步：成员挂着流（从当前水位之后起步），所有者在本机写入，
    #    测「写入返回 → 对端收到」的墙钟延迟。用 since=0 订阅会先把历史事件补拉下来，
    #    量到的就不是这一次写入，所以必须先取水再订阅。
    baseline = sse_cursor(project_id, member_access)
    resp = open_sse(
        f"/api/v1/projects/{project_id}/events/stream?since={baseline}", member_access
    )
    delivered = baseline
    try:
        ready = read_sse_event(resp)
        check(
            "实时：成员订阅就绪且游标等于水位",
            ready is not None
            and ready[0] == "ready"
            and ready[1].get("cursor") == baseline,
            str(ready),
        )
        started = time.perf_counter()
        status, _, _ = http(
            "POST", f"/api/v1/projects/{project_id}/tasks/{task_id}/comments",
            json_body={"body": "跨机器评论"}, token=owner_access,
        )
        check("实时：所有者评论写入成功", status == 201)
        pushed, skipped = drain_until(resp, "task.commented")
        latency_ms = (time.perf_counter() - started) * 1000.0
        check(
            "实时：成员收到评论事件" + (f"（顺带 {skipped}）" if skipped else ""),
            pushed is not None,
            str(pushed),
        )
        check(
            f"实时：对端 1 秒内可见（实测 {latency_ms:.0f}ms）",
            latency_ms <= 1000.0,
            f"实测 {latency_ms:.0f}ms，超过 1 秒验收线",
        )
        delivered = int(pushed["seq"])
    finally:
        resp.close()

    # ② 断线期间不丢：关闭流后写两条，用最后已交付游标重连补齐，与增量接口逐条对齐
    status, _, _ = http(
        "PATCH", f"/api/v1/projects/{project_id}/tasks/{task_id}",
        json_body={"version": 1, "status": "doing"}, token=owner_access,
    )
    check("断线：改任务状态成功", status == 200)
    status, _, _ = http(
        "POST", f"/api/v1/projects/{project_id}/tasks/{task_id}/comments",
        json_body={"body": "断线期间的评论"}, token=member_access,
    )
    check("断线：成员评论成功", status == 201)

    resp2 = open_sse(
        f"/api/v1/projects/{project_id}/events/stream?since={delivered}", member_access
    )
    backfill = []
    try:
        ready2 = read_sse_event(resp2)
        check(
            "断线：重连 ready 回带请求游标",
            ready2 is not None and ready2[0] == "ready" and ready2[1].get("cursor") == delivered,
            str(ready2),
        )
        while len(backfill) < 2:
            ev = read_sse_event(resp2, timeout_seconds=5.0)
            if ev is None:
                break
            if ev[0] == "message":
                backfill.append(ev[1])
    finally:
        resp2.close()
    check(
        "断线：重连补齐断线期间的两条事件",
        [event["kind"] for event in backfill] == ["task.updated", "task.commented"],
        str([event["kind"] for event in backfill]),
    )
    seqs = [int(event["seq"]) for event in backfill]
    check("断线：补齐事件 seq 严格递增且晚于断线点", all(s > delivered for s in seqs) and seqs == sorted(seqs), str(seqs))
    status, _, body = http(
        "GET", f"/api/v1/projects/{project_id}/events?since={delivered}", token=member_access
    )
    rest_seqs = [int(event["seq"]) for event in payload(body).get("events", [])]
    check("断线：流补拉与增量接口一致（不丢也不重）", seqs == rest_seqs, f"流 {seqs} 接口 {rest_seqs}")

    # ③ 被移出项目：流以 closed(membership) 明确结束，客户端不该再无限重连
    cursor = seqs[-1] if seqs else delivered
    resp3 = open_sse(
        f"/api/v1/projects/{project_id}/events/stream?since={cursor}", member_access
    )
    try:
        status, _, _ = http(
            "POST",
            f"/api/v1/projects/{project_id}/members/{member_uid}/remove",
            token=owner_access,
        )
        check("移出：所有者移除成员成功", status == 200)
        # 移除本身也是一条事件：它会先于 closed 到达（服务端在同一轮扫描里读到），
        # 所以这里读到 closed 为止，而不是假定 closed 是第一条
        closed = None
        seen = []
        for _ in range(6):
            ev = read_sse_event(resp3, timeout_seconds=6.0)
            if ev is None:
                break
            if ev[0] == "message":
                seen.append(ev[1].get("kind"))
            elif ev[0] == "closed":
                closed = ev[1]
                break
        check(
            "移出：成员流收到 closed 并点名原因",
            closed is not None and closed.get("reason") == "membership",
            str(closed),
        )
        # 实测语义：_read_events 先查成员资格再读事件，所以「移除自己」这条事件
        # 不会经流下发——closed 就是对端能拿到的唯一信号，界面必须据此提示，
        # 不能等着看 member.removed 才知道自己出局了
        check("移出：移除事件本身不经流下发（closed 是唯一信号）",
              "member.removed" not in seen, str(seen))
        eof = False
        for _ in range(4):
            if resp3.readline() == b"":
                eof = True
                break
        check("移出：closed 之后连接即结束", eof)
    finally:
        resp3.close()


def create_agent_ticket(project_id: str, token: str, **extra) -> tuple[int, dict]:
    body = {
        "title": "整理本周数据并出图",
        "detail": "每周一要一张趋势图",
        "brief": "读仓库内的 csv，出本周趋势图",
        "scope": "只读仓库内文件；产物写到工单目录",
        "outOfScope": "不碰生产库；不发消息给任何人",
        "intensity": "explicit",
    }
    body.update(extra)
    status, _, resp = http(
        "POST", f"/api/v1/projects/{project_id}/tickets", json_body=body, token=token
    )
    return status, payload(resp)


def test_agent_tickets(ctx):
    """D-47 工单层验收：交回 / 待人工判定 / 停止是唯一撤权 / 跨端 1 秒一致。

    四条对应缺陷文档里的验收 ①②③④，另外钉住三条状态机纪律：
    授权正文不可变（改范围=吊销+新增一版）、续跑前重新校验授权、订阅扇出只补视野。
    """
    print("\n[工单层 D-47]")
    stamp = int(time.time() * 1000)
    owner = register_user(f"tk-owner-{stamp}@test.local", "工单所有者")
    worker = register_user(f"tk-worker-{stamp}@test.local", "工单执行体")
    outsider = register_user(f"tk-outside-{stamp}@test.local", "工单局外人")
    owner_access, worker_access = owner["accessToken"], worker["accessToken"]

    status, _, body = http(
        "POST", "/api/v1/projects", json_body={"name": "工单演练"}, token=owner_access
    )
    project_id = payload(body).get("project", {}).get("id", "")
    check("工单：创建项目", status == 201 and project_id.startswith("prj-"))
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/invites",
        json_body={"email": worker["profile"]["email"], "role": "member"},
        token=owner_access,
    )
    code = payload(body).get("inviteCode", "")
    status, _, body = http(
        "POST",
        "/api/v1/projects/invites/accept",
        json_body={"token": code},
        token=worker_access,
    )
    check("工单：执行体账号加入项目", status == 200)
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tasks",
        json_body={"title": "订阅源任务"},
        token=owner_access,
    )
    source_task_id = payload(body).get("task", {}).get("id", "")
    check("工单：准备订阅源任务", status == 201 and source_task_id)

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets",
        json_body={"title": "局外人试试", "brief": "不该开工"},
        token=outsider["accessToken"],
    )
    check(
        "工单：非成员不能建单（按不存在处理）",
        status == 404 and payload(body).get("code") == "PROJECT_NOT_FOUND",
    )

    # ① 建单：工单与首份授权一起落，输入还没交付所以不是"等人"
    status, data = create_agent_ticket(
        project_id, owner_access, subscribeTaskId=source_task_id
    )
    ticket = data.get("ticket", {})
    mandate = data.get("mandate", {})
    ticket_id = ticket.get("id", "")
    check("工单：建单 201 且 id 合规", status == 201 and ticket_id.startswith("atk-"))
    check("工单：初始 stage 是输入未交付", ticket.get("stage") == "input_undelivered")
    check("工单：输入未交付不算等人", ticket.get("needsHuman") is False)
    check("工单：首份授权生效中", mandate.get("status") == "active" and mandate.get("seq") == 1)
    check("工单：授权正文带摘要", len(str(mandate.get("digest", ""))) == 64)
    check("工单：订阅随建单建立", data.get("subscription") is not None)
    check("工单：建单写了事件游标", int(data.get("seq", 0)) >= 1)

    status, data = create_agent_ticket(project_id, owner_access, intensity="inferred")
    check(
        "工单：推断授权必须写明依据",
        status == 400 and data.get("code") == "AGENT_TICKET_INVALID_INPUT",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={"kind": "progress", "summary": "没人领取就报告"},
        token=worker_access,
    )
    check(
        "工单：没有在跑的执行体时报告无处可归",
        status == 409 and payload(body).get("code") == "AGENT_ASSIGNMENT_NOT_FOUND",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={"mandateId": mandate["id"], "expectedMandateDigest": "0" * 64},
        token=worker_access,
    )
    check(
        "工单：续跑前重校验授权正文（摘要对不上就拒）",
        status == 409 and payload(body).get("code") == "AGENT_MANDATE_CHANGED",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={
            "mandateId": mandate["id"],
            "expectedMandateDigest": mandate["digest"],
            "workerId": "worker-本周数据",
            "workspaceKind": "isolated",
            "workspaceRef": f"ticket-workers/{ticket_id}",
            "machine": "pc-a",
            "recoveryNote": "先看目录与 git 历史，再决定是否重做外部动作",
        },
        token=worker_access,
    )
    first = payload(body)
    assignment_id = first.get("assignment", {}).get("id", "")
    check("工单：执行体领取成功", status == 201 and assignment_id.startswith("asg-"))
    check("工单：领取后进入在跑", first.get("ticket", {}).get("stage") == "working")
    check("工单：恢复注记随绑定落账", "git 历史" in str(first["assignment"].get("recoveryNote")))
    check(
        "工单：一个授权不能开两个执行体",
        http(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
            json_body={"mandateId": mandate["id"]},
            token=worker_access,
        )[0]
        == 409,
    )

    # ② 待人工只由结构化字段判定：progress 不产生问题，waiting 必须带问题
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={"assignmentId": assignment_id, "kind": "progress", "summary": "读完 3 个文件"},
        token=worker_access,
    )
    progress = payload(body)
    check("工单：进展报告不产生待人工", status == 201 and progress.get("attentionId") is None)
    check("工单：进展报告留在在跑", progress.get("ticket", {}).get("stage") == "working")

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={"assignmentId": assignment_id, "kind": "waiting", "summary": "在等人"},
        token=worker_access,
    )
    check(
        "工单：等人必须写明等什么，不从正文猜",
        status == 400
        and payload(body).get("code") == "AGENT_ATTENTION_QUESTION_REQUIRED",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={
            "assignmentId": assignment_id,
            "kind": "waiting",
            "summary": "时间范围不明",
            "question": "要最近 7 天还是本周？",
            "options": ["最近 7 天", "本周一到现在"],
        },
        token=worker_access,
    )
    waiting = payload(body)
    check(
        "工单：waiting 让工单进入等你回答",
        status == 201 and waiting.get("ticket", {}).get("stage") == "awaiting_answer",
    )
    check("工单：等你回答算需要人", waiting.get("ticket", {}).get("needsHuman") is True)
    attention_id = waiting.get("attentionId", "")
    check("工单：结构化问题带候选项落账", bool(attention_id))

    status, _, body = http("GET", "/api/v1/agent/tickets", token=owner_access)
    mine = payload(body).get("tickets", [])
    listed = next((item for item in mine if item.get("id") == ticket_id), None)
    check("工单：等你的工单列表里有这一单", status == 200 and listed is not None)
    check(
        "工单：列表带出要问的那个问题",
        listed is not None
        and listed.get("attention", {}).get("question") == "要最近 7 天还是本周？",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/attentions/{attention_id}/answer",
        json_body={"answer": "本周一到现在"},
        token=owner_access,
    )
    answered = payload(body)
    check(
        "工单：人回答后继续在跑的执行体",
        status == 200 and answered.get("ticket", {}).get("stage") == "working",
    )
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/attentions/{attention_id}/answer",
        json_body={"answer": "再答一次"},
        token=owner_access,
    )
    check(
        "工单：同一问题不能答两次",
        status == 409 and payload(body).get("code") == "AGENT_ATTENTION_NOT_OPEN",
    )

    # ① 超出范围交回，而不是自己顺手做掉；一张工单只交回一次
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/handback",
        json_body={
            "assignmentId": assignment_id,
            "reason": "csv 里没有本周数据，需要连生产库",
            "requested": "允许只读生产库的 orders 表",
        },
        token=worker_access,
    )
    handed = payload(body)
    check("工单：超范围交回 201", status == 201 and handed.get("attentionId"))
    check("工单：交回后进等你回答", handed.get("ticket", {}).get("stage") == "awaiting_answer")
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/handback",
        json_body={"assignmentId": assignment_id, "reason": "还是想要生产库", "requested": "再问一次"},
        token=worker_access,
    )
    check(
        "工单：交回只问一次，不让人重复处理同一件事",
        status == 409 and payload(body).get("code") == "AGENT_HANDBACK_DONE",
    )

    # 人决定扩大范围：授权正文不被改写，吊销旧的、插新的一版
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/mandate/amend",
        json_body={
            "brief": "读仓库 csv 出本周趋势图，必要时只读生产库 orders 表",
            "scope": "只读；产物写工单目录",
            "reason": "按交回扩大范围",
        },
        token=owner_access,
    )
    amended = payload(body)
    new_mandate = amended.get("mandate", {})
    check(
        "工单：改授权新增一版而不是改写",
        status == 200 and new_mandate.get("seq") == 2,
        f"{status} {json.dumps(new_mandate, ensure_ascii=False)[:200]} {amended.get('message', '')}",
    )
    check("工单：改授权后退回排队", amended.get("ticket", {}).get("stage") == "queued")
    check(
        "工单：改授权换了正文摘要",
        new_mandate.get("digest") != mandate.get("digest"),
    )
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={"mandateId": mandate["id"]},
        token=worker_access,
    )
    check(
        "工单：按旧授权不能再开工",
        status == 409 and payload(body).get("code") == "AGENT_MANDATE_SUPERSEDED",
    )
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={"assignmentId": assignment_id, "kind": "progress", "summary": "旧执行体还想报"},
        token=worker_access,
    )
    check(
        "工单：改授权后旧执行体的报告不再改变状态",
        status == 409 and payload(body).get("code") == "AGENT_ASSIGNMENT_STALE",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/mandate/amend",
        json_body={"brief": "执行体自己扩范围"},
        token=worker_access,
    )
    check(
        "工单：改授权只能由人（创建者或管理员）做",
        status == 403 and payload(body).get("code") == "AGENT_TICKET_FORBIDDEN",
    )

    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={
            "mandateId": new_mandate["id"],
            "expectedMandateDigest": new_mandate["digest"],
            "workerId": "worker-本周数据-2",
        },
        token=worker_access,
    )
    second = payload(body)
    second_assignment = second.get("assignment", {}).get("id", "")
    check("工单：按新授权重新领取", status == 201 and second.get("ticket", {}).get("stage") == "working")
    check("工单：执行体序号往后排", second["assignment"].get("seq") == 2)

    # D-48.3 的服务端一侧：已读事实按步骤落账，重复落账不会记两次
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments/{second_assignment}"
        f"/read-step?eventSeq=4242",
        token=worker_access,
    )
    first_read = payload(body)
    check("工单：已读事实可落账", status == 200 and first_read.get("already") is False)
    check("工单：落账后 assignment 带步骤", len(first_read["assignment"].get("readSteps", [])) == 1)
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments/{second_assignment}"
        f"/read-step?eventSeq=4242",
        token=worker_access,
    )
    again_read = payload(body)
    check(
        "工单：同一号消息不重复记已读",
        status == 200 and again_read.get("already") is True
        and len(again_read["assignment"].get("readSteps", [])) == 1,
    )

    # ④ 两端 1 秒内一致：所有者挂着订阅流，看执行体那一侧的报告何时到达
    baseline = sse_cursor(project_id, owner_access)
    resp = open_sse(
        f"/api/v1/projects/{project_id}/events/stream?since={baseline}", owner_access
    )
    try:
        ready = read_sse_event(resp)
        check("工单实时：所有者订阅就绪", ready is not None and ready[0] == "ready")
        started = time.perf_counter()
        status, _, body = http(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
            json_body={
                "assignmentId": second_assignment,
                "kind": "ready",
                "summary": "图出好了，请验收",
                "artifacts": ["week-trend.png"],
            },
            token=worker_access,
        )
        check("工单实时：执行体报告 ready 成功", status == 201)
        pushed, skipped = drain_until(resp, "agent.report.created")
        latency_ms = (time.perf_counter() - started) * 1000.0
        check(
            "工单实时：另一端收到 agent.report.created"
            + (f"（顺带 {skipped}）" if skipped else ""),
            pushed is not None,
            str(pushed),
        )
        check(
            f"工单实时：1 秒内一致（实测 {latency_ms:.0f}ms）",
            latency_ms <= 1000.0,
            f"实测 {latency_ms:.0f}ms，超过 1 秒验收线",
        )
        check(
            "工单实时：推过来的事件带工单视图与 stage",
            pushed is not None
            and pushed.get("kind") == "agent.report.created"
            and pushed.get("payload", {}).get("ticket", {}).get("id") == ticket_id
            and pushed["payload"]["ticket"].get("stage") == "awaiting_review",
            str(pushed),
        )
    finally:
        resp.close()

    # 「只有 ready 让工单进入待评审」由下面的详情断言证明：stage 是 awaiting_review，
    # 且这一时刻唯一的开放问题 kind 是 review（不是 answer）
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    detail = payload(body)
    check("工单详情可读", status == 200 and detail["ticket"]["stage"] == "awaiting_review")
    open_attentions = [item for item in detail.get("attentions", []) if item["status"] == "open"]
    check(
        "工单：待评审的开放问题是 review 而不是 answer",
        len(open_attentions) == 1 and open_attentions[0]["kind"] == "review",
        str(open_attentions),
    )
    check("工单：报告里产物只留文件名", detail.get("reports", [])[-1].get("artifacts") == ["week-trend.png"])

    # 扇出：订阅建立之后新到的消息才作为上下文；游标只前进，第二次读不再重复给
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tasks/{source_task_id}/comments",
        json_body={"body": "顺便看下上季度的对比"},
        token=owner_access,
    )
    check("工单扇出：源任务新评论写入", status == 201)
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    contexts = payload(body).get("contexts", [])
    check("工单扇出：新到的消息作为上下文交给工单", len(contexts) == 1 and contexts[0]["kind"] == "task.commented")
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    check(
        "工单扇出：同一条不重复给（游标只前进）",
        payload(body).get("contexts", []) == [],
    )
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    check(
        "工单扇出：不给新授权（stage 仍是待评审）",
        payload(body).get("ticket", {}).get("stage") == "awaiting_review",
    )

    # 验收不通过：退回排队，授权范围照旧
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/accept",
        json_body={"accept": False, "note": "坐标轴看不清"},
        token=owner_access,
    )
    rejected = payload(body)
    check("工单：退回重做不吊销授权", status == 200 and rejected.get("ticket", {}).get("stage") == "queued")
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    check(
        "工单：退回时把在跑的执行体结束掉",
        payload(body).get("assignments", [{}])[0].get("status") == "dropped",
    )

    # blocked / failed 才出现在「等人处理」；dropped 只是回合结束，不算等人
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={"mandateId": new_mandate["id"]},
        token=worker_access,
    )
    third_assignment = payload(body).get("assignment", {}).get("id", "")
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={
            "assignmentId": third_assignment,
            "kind": "failed",
            "summary": "跑挂了：生产库连不上",
        },
        token=worker_access,
    )
    failed = payload(body)
    check("工单：failed 落到等人处理", status == 201 and failed.get("ticket", {}).get("stage") == "blocked")
    check("工单：failed 也算需要人", failed.get("ticket", {}).get("needsHuman") is True)
    status, _, body = http("GET", "/api/v1/agent/tickets", token=owner_access)
    listed = next(
        (item for item in payload(body).get("tickets", []) if item.get("id") == ticket_id), None
    )
    check("工单：等人处理出现在等你的工单里", listed is not None and listed.get("stage") == "blocked")
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/reports",
        json_body={
            "assignmentId": third_assignment,
            "kind": "dropped",
            "summary": "这一回合结束，后台作业还在跑",
        },
        token=worker_access,
    )
    dropped = payload(body)
    check(
        "工单：dropped 只是回合结束，不算等人",
        status == 201 and dropped.get("ticket", {}).get("stage") == "idle"
        and dropped.get("ticket", {}).get("needsHuman") is False,
    )

    # ③ 停止是唯一撤权：执行体停不掉自己的工单，人停了之后一切写入被拒
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/stop",
        json_body={"reason": "执行体自己不想干了"},
        token=worker_access,
    )
    check(
        "工单：停止只能由人做（创建者或项目管理员）",
        status == 403 and payload(body).get("code") == "AGENT_TICKET_FORBIDDEN",
    )
    status, _, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/stop",
        json_body={"reason": "人不放心这次改动"},
        token=owner_access,
    )
    stopped = payload(body)
    check("工单：人停止成功", status == 200 and stopped.get("ticket", {}).get("stage") == "closed")
    check("工单：停止写进 closed_reason", "人不放心" in str(stopped["ticket"].get("closedReason")))
    status, _, body = http(
        "GET",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
        token=owner_access,
    )
    detail = payload(body)
    check(
        "工单：停止即吊销授权（当前没有生效授权）",
        status == 200 and detail.get("mandate") is None,
    )
    check(
        "工单：停止把在跑的执行体标成 lost",
        detail.get("assignments", [{}])[0].get("status") == "lost",
    )
    check(
        "工单：停止撤回所有开放问题",
        all(item["status"] != "open" for item in detail.get("attentions", [])),
    )
    for label, path, body_json in (
        ("报告", "reports", {"assignmentId": third_assignment, "kind": "progress", "summary": "还想报"}),
        ("交回", "handback", {"assignmentId": third_assignment, "reason": "r", "requested": "q"}),
        ("改授权", "mandate/amend", {"brief": "b"}),
    ):
        status, _, resp = http(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/{path}",
            json_body=body_json,
            token=owner_access,
        )
        check(
            f"工单：停止后不再收任何输入（{label}）",
            status == 409 and payload(resp).get("code") == "AGENT_TICKET_CLOSED",
            str(payload(resp)),
        )
    status, _, resp = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments",
        json_body={"mandateId": new_mandate["id"]},
        token=worker_access,
    )
    check(
        "工单：停止后不能重新领取",
        status == 409 and payload(resp).get("code") == "AGENT_TICKET_CLOSED",
    )
    status, _, resp = http(
        "POST",
        f"/api/v1/projects/{project_id}/tickets/{ticket_id}/stop",
        json_body={"reason": "再停一次"},
        token=owner_access,
    )
    check("工单：停止不能停两次", status == 409 and payload(resp).get("code") == "AGENT_TICKET_CLOSED")

    # 已收口的工单不该再出现在「等你的工单」
    status, _, body = http("GET", "/api/v1/agent/tickets", token=owner_access)
    check(
        "工单：已收口不占等你的版面",
        status == 200
        and all(item.get("id") != ticket_id for item in payload(body).get("tickets", [])),
    )


def test_usage_report(ctx):
    print("\n[用量上报与合并视图]")
    stamp = int(time.time() * 1000)
    user = register_user(f"usage-{stamp}@test.local", "用量用户")
    access = user["accessToken"]
    uid = user["profile"]["uid"]
    outsider = register_user(f"usage-out-{stamp}@test.local", "用量局外人")
    outsider_uid = outsider["profile"]["uid"]
    admin = ctx["admin_access"]

    today = time.strftime("%Y-%m-%d", time.gmtime())
    month = today[:7]
    # 40 天前必然落在本月之外（月份最长 31 天），用它验证跨月过滤
    last_month_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400))
    now_ms_value = int(time.time() * 1000)

    def usage_row(model, day, origin, pt, ct, calls, **extra):
        row = {
            "sub": uid,
            "modelName": model,
            "day": day,
            "origin": origin,
            "promptTokens": pt,
            "completionTokens": ct,
            "tokensUsed": pt + ct,
            "calls": calls,
        }
        row.update(extra)
        return row

    desktop_rows = [
        usage_row("qwen-plus", today, "desktop", 1000, 500, 3,
                  pricedCalls=2, unpricedCalls=1, latencyMsTotal=3000, latencySamples=3,
                  costs={"CNY": 1080}, lastUsedAtMs=now_ms_value),
        usage_row("qwen-max", today, "desktop", 200, 100, 2, unpricedCalls=2),
    ]
    engine_rows = [
        # 桌面通道：同一批调用桌面已记，合并视图必须排除
        usage_row("qwen-plus", today, "desktop", 1000, 500, 3),
        usage_row("qwen-plus", today, "im", 40, 10, 2,
                  pricedCalls=2, latencyMsTotal=300, latencySamples=2, costs={"CNY": 40}),
        usage_row("qwen-plus", today, "unknown", 7, 7, 1, unpricedCalls=1),
        usage_row("qwen-max", last_month_day, "web", 10, 5, 1, unpricedCalls=1),
    ]

    status, _, body = http("POST", "/api/v1/usage/report", json_body={
        "source": "engine", "deviceId": "pc-1", "rows": engine_rows})
    check("未登录不能上报用量", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")

    status, _, body = http("POST", "/api/v1/usage/report", token=access, json_body={
        "source": "kafka", "deviceId": "pc-1", "rows": engine_rows})
    check("未知来源被拒", status == 400 and payload(body).get("code") == "USAGE_INVALID_SOURCE")

    bad = dict(engine_rows[0], tokensUsed=1)
    status, _, body = http("POST", "/api/v1/usage/report", token=access, json_body={
        "source": "engine", "deviceId": "pc-1", "rows": [bad]})
    check("tokensUsed 与 prompt+completion 不一致被拒",
          status == 400 and payload(body).get("code") == "USAGE_TOKENS_MISMATCH")
    status, _, body = http("POST", "/api/v1/usage/report", token=access, json_body={
        "source": "engine", "deviceId": "pc-1", "rows": [dict(engine_rows[0], day="2026/10/01")]})
    check("day 非 yyyy-mm-dd 被拒",
          status == 400 and payload(body).get("code") == "USAGE_INVALID_ROW")

    status, _, body = http("POST", "/api/v1/usage/report", token=access, json_body={
        "source": "engine", "deviceId": "pc-1",
        "rows": [dict(engine_rows[1], sub=outsider_uid)]})
    check("不能替他人上报",
          status == 403 and payload(body).get("code") == "USAGE_SCOPE_DENIED")

    def report(source, rows, device="pc-1", token=access):
        return http("POST", "/api/v1/usage/report", token=token, json_body={
            "source": source, "deviceId": device, "schemaVersion": 1, "rows": rows})

    status, _, body = report("engine", engine_rows)
    check("引擎台账上报入库", status == 200 and payload(body).get("accepted") == 4, str(payload(body)))
    status, _, body = report("desktop", desktop_rows)
    check("桌面台账上报入库", status == 200 and payload(body).get("accepted") == 2)

    def me(period="total", token=access):
        status, _, body = http("GET", f"/api/v1/usage/me?period={period}", token=token)
        return status, payload(body)

    status, view = me()
    check("查询本人合并视图", status == 200 and view.get("period") == "total")
    totals = view["totals"]
    check("桌面为主：引擎 desktop 行不重复计数",
          totals["promptTokens"] == 1257 and totals["completionTokens"] == 622
          and totals["tokensUsed"] == 1879 and totals["calls"] == 9,
          json.dumps(totals, ensure_ascii=False))
    check("引擎补入桌面没有的通道（im/web/unknown）",
          [item["modelName"] for item in view["models"]] == ["qwen-plus", "qwen-max"]
          and view["models"][0]["tokensUsed"] == 1564)
    check("平均耗时向下取整（与桌面/引擎同口径）",
          totals["avgLatencyMs"] == 660 and totals["latencySamples"] == 5)
    check("成本按币种合并、未计价单独计数",
          totals["costs"] == [{"currency": "CNY", "micros": 1120}]
          and totals["pricedCalls"] == 4 and totals["unpricedCalls"] == 5)
    check("来源披露：排除数、未标记数、上报端数",
          view["sources"]["engineDedupedRows"] == 1
          and view["sources"]["engineDedupedTokens"] == 1500
          and view["sources"]["desktopRows"] == 2
          and view["sources"]["engineRows"] == 3
          and view["sources"]["unattributedTokens"] == 14
          and view["sources"]["reporters"] == 2,
          json.dumps(view["sources"], ensure_ascii=False))

    report("engine", engine_rows)
    report("desktop", desktop_rows)
    _, again = me()
    check("重复上报幂等（累计值覆盖，不重复计数）",
          again["totals"] == totals and again["sources"] == view["sources"],
          json.dumps(again["totals"], ensure_ascii=False))

    _, monthly = me("monthly")
    check("本月视图排除跨月行",
          monthly["totals"]["tokensUsed"] == 1864
          and all(item["day"].startswith(month) for item in monthly["days"]))
    _, daily = me("daily")
    check("当日视图只含今日",
          daily["days"] and all(item["day"] == today for item in daily["days"]))
    check("趋势与明细与汇总三块自洽",
          sum(item["tokensUsed"] for item in monthly["days"])
          == sum(item["tokensUsed"] for item in monthly["models"])
          == monthly["totals"]["tokensUsed"])
    status, _, body = http("GET", "/api/v1/usage/me?period=hourly", token=access)
    check("非法周期拒绝而不是降级",
          status == 400 and payload(body).get("code") == "USAGE_INVALID_PERIOD")

    report("engine", [usage_row("qwen-plus", today, "im", 40, 10, 2,
                               pricedCalls=2, latencyMsTotal=300, latencySamples=2,
                               costs={"CNY": 40})], device="pc-2")
    _, second_device = me()
    check("多设备各算一份，不互相覆盖",
          second_device["totals"]["tokensUsed"] == 1879 + 50
          and second_device["sources"]["reporters"] == 3)

    status, _, body = http("GET", f"/api/v1/usage/users/{outsider_uid}", token=access)
    check("普通用户不能查他人用量", status == 403)
    status, _, body = http("GET", f"/api/v1/usage/users/{uid}?period=total", token=admin)
    admin_view = payload(body)
    check("管理员按用户查询与本人视图同口径",
          status == 200 and admin_view["totals"] == second_device["totals"])
    status, _, body = http("GET", "/api/v1/usage/me")
    check("未登录不能查询", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")

    fresh = register_user(f"usage-empty-{stamp}@test.local", "空台账用户")
    _, empty = me("monthly", token=fresh["accessToken"])
    check("无用量时返回零值而不是伪造第一名/耗时/成本",
          empty["totals"]["tokensUsed"] == 0 and empty["totals"]["activeModels"] == 0
          and empty["totals"]["avgLatencyMs"] is None and empty["totals"]["costs"] == []
          and empty["models"] == [] and empty["days"] == [])

    conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        "SELECT origin, COUNT(*) AS n FROM usage_rollup WHERE sub = ?1 GROUP BY origin",
        (uid,),
    ).fetchall()
    rows_in_db = conn.execute("SELECT COUNT(*) AS n FROM usage_rollup").fetchone()["n"]
    conn.close()
    check("桌面行的 origin 统一写成 desktop",
          {row["origin"] for row in stored if row["origin"] != ""} <=
          {"desktop", "im", "web", "unknown", "cli", "schedule"})
    check("上报行只落服务端自己的库（重复上报不新增行）", rows_in_db == 7, str(rows_in_db))


def test_rate_limit():
    print("\n[登录限流]")
    email = f"ghost{int(time.time())}@test.local"
    statuses = []
    for _ in range(10):
        status, _, _ = http(
            "POST", "/api/v1/auth/login", json_body={"email": email, "password": "wrong-pass-1"}
        )
        statuses.append(status)
    check("前 10 次返回 401", all(code == 401 for code in statuses), str(statuses))
    status, headers, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": "wrong-pass-1"}
    )
    check("第 11 次被限流", status == 429 and payload(body).get("code") == "RATE_LIMITED")
    check("携带 Retry-After", int(headers.get("Retry-After", "0")) >= 1)


def run():
    server, thread = start_server()
    try:
        test_public()
        ctx = test_auth()
        ctx.update(test_legacy_web_login())
        test_auth_claims(ctx)
        test_engine_introspect(ctx)
        test_skill_market(ctx)
        ctx["skill_id"] = "weekly-digest"
        ctx.update(test_suite_market(ctx))
        ctx.update(test_mcp_market(ctx))
        test_interactions(ctx)
        test_my_items(ctx)
        test_projects(ctx)
        test_project_events(ctx)
        test_project_events_realtime(ctx)
        test_agent_tickets(ctx)
        test_usage_report(ctx)
        test_seed(ctx)
        test_market_revoke(ctx)
        test_rate_limit()
        print(f"\n全部通过：{_passed} 项断言")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    run()
