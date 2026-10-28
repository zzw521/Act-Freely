#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通天办公服务端全链路冒烟测试（标准库实现，无 pytest/httpx 依赖）。

覆盖：
- 健康检查、分类、错误格式；
- v1 注册/登录/刷新轮换/退出、loopback 网页注册与授权码交换；
- 技能/套件发布、版本发布、检索、分类、下载与 SHA-256 校验；
- MCP 配置发布与校验（拒绝密钥值字段、非法传输方式）；
- 超大请求体拒绝、包文件损坏检测；
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
):
    url = BASE + path
    headers: dict[str, str] = {}
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
    check("演示技能 +2", after_first["skills"] == before["skills"] + 2)
    check("演示套件 +2", after_first["suites"] == before["suites"] + 2)
    check("演示 MCP +3", after_first["mcp"] == before["mcp"] + 3)
    check("重复执行不新增", after_second == after_first)
    check("演示数据总量", after_first == {"skills": 3, "suites": 3, "mcp": 5})

    status, _, body = http(
        "POST", "/api/v1/auth/login",
        json_body={"email": seed.DEMO_EMAIL, "password": os.environ["TT_DEMO_PASSWORD"]},
    )
    check("演示账号可登录", status == 200 and payload(body)["profile"]["nickname"] == seed.DEMO_NICKNAME)
    demo_access = payload(body)["accessToken"]
    status, _, body = http("GET", "/api/v1/me/items?pageSize=100", token=demo_access)
    check("演示账号拥有 7 个条目", status == 200 and payload(body).get("total") == 7)

    status, _, body = http("GET", "/api/v1/skills?pageSize=2&page=1&sort=name")
    first_page = payload(body)
    check("分页第一页", status == 200 and len(first_page["items"]) == 2 and first_page["hasMore"] is True)
    status, _, body = http("GET", "/api/v1/skills?pageSize=2&page=2&sort=name")
    second_page = payload(body)
    check("分页第二页", status == 200 and len(second_page["items"]) == 1 and second_page["hasMore"] is False)


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
        test_skill_market(ctx)
        ctx["skill_id"] = "weekly-digest"
        ctx.update(test_suite_market(ctx))
        ctx.update(test_mcp_market(ctx))
        test_interactions(ctx)
        test_my_items(ctx)
        test_projects(ctx)
        test_project_events(ctx)
        test_seed(ctx)
        test_market_revoke(ctx)
        test_rate_limit()
        print(f"\n全部通过：{_passed} 项断言")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    run()
