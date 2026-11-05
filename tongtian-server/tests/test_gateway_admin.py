#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制面网关管理代理（M1.4 T4.10）。

覆盖：
- 未登录 401、普通用户 403、管理员放行；
- 转发路径/查询串，并在服务端补 `X-TT-Admin-Token` 与管理人 uid；
- 浏览器拿不到管理令牌；
- 网关错误状态原样透传，非 JSON 响应映射为稳定错误码；
- 写操作落控制面审计且密钥脱敏，读操作不写审计；
- 路径穿越拒绝、未配置 503、连不上 502。

用法: python tests/test_gateway_admin.py
"""

import atexit
import base64
import dataclasses
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN_TOKEN = "gateway-admin-token-for-test"
SECRET = "sk-live-secret-never-log"

_TMP = tempfile.mkdtemp(prefix="tt-gw-admin-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeGateway(BaseHTTPRequestHandler):
    requests: list[dict] = []
    server_version = "FakeGateway/1.0"

    def log_message(self, *_args):  # 静默：测试输出只保留断言
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        body = self._read_body()
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parsed.query,
                "admin": self.headers.get("x-tt-admin-token"),
                "actor": self.headers.get("x-tt-admin-actor"),
                "roles": self.headers.get("x-tt-admin-roles"),
                "depts": self.headers.get("x-tt-admin-depts"),
                "tenant": self.headers.get("x-tt-admin-tenant"),
                "body": body.decode("utf-8") if body else "",
            }
        )
        if self.headers.get("x-tt-admin-token") != ADMIN_TOKEN:
            self._send(401, {"ok": False, "code": "AUTH_REQUIRED", "message": "管理令牌无效"})
            return
        if parsed.path.endswith("/html"):
            self._send(200, b"<html>oops</html>", "text/html")
            return
        if parsed.path.endswith("/ndjson") or parsed.path.endswith("/audit/export"):
            self._send(200, b'{"id":1}\n{"id":2}\n', "application/x-ndjson")
            return
        if parsed.path.endswith("/fail"):
            self._send(400, {"ok": False, "code": "INVALID_REQUEST", "message": "坏请求"})
            return
        if parsed.path.endswith("/providers/p1/keys"):
            self._send(201, {"ok": True, "key": {"id": "pk_1", "last4": "9f2c", "secret": SECRET}})
            return
        if parsed.path.endswith("/health"):
            self._send(200, {
                "ok": True,
                "gatewayId": "gw-test",
                "usageQueue": {"pending": 0},
            })
            return
        self._send(200, {"ok": True, "path": parsed.path, "query": parsed.query})

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle
    do_DELETE = _handle


GATEWAY_PORT = free_port()
_fake = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), FakeGateway)
_threading = threading.Thread(target=_fake.serve_forever, daemon=True)
_threading.start()

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "gateway-admin-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"
os.environ["TT_GATEWAY_ADMIN_URL"] = f"http://127.0.0.1:{GATEWAY_PORT}"
os.environ["TT_GATEWAY_ADMIN_TOKEN"] = ADMIN_TOKEN
os.environ["TT_GATEWAY_PROXY_TIMEOUT_SECONDS"] = "5"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import gateway_admin as ga  # noqa: E402
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


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None,
         raw: bytes | None = None, headers: dict | None = None):
    merged = dict(headers or {})
    data = raw
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    if data is not None:
        merged["Content-Type"] = "application/json"
    if token:
        merged["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=merged, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def register_user(email: str, nickname: str) -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def login(email: str, password: str = "secret123") -> str:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    data = payload(body)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {data}")
    return data["accessToken"]


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


def audits() -> list[dict]:
    conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT actor_uid, action, object, payload_json, status FROM admin_audit "
        "ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def main() -> None:
    server, _ = start_server()
    try:
        print("\n[鉴权：浏览器只带平台 JWT]")
        status, _, body = http("GET", "/api/v1/admin/gateway/health")
        check("未登录 401", status == 401 and payload(body).get("code") == "AUTH_REQUIRED")
        user_token = login("admin@test.local", "admin-secret-123")
        me = payload(http("GET", "/api/v1/auth/me", token=user_token)[2])["profile"]
        admin_uid = me["uid"]

        stamp = int(time.time() * 1000)
        ordinary_email = f"gw-admin-user-{stamp}@test.local"
        ordinary = register_user(ordinary_email, "普通用户")
        status, _, body = http(
            "GET", "/api/v1/admin/gateway/health", token=ordinary["accessToken"]
        )
        check("非管理员 403", status == 403 and payload(body).get("code") == "FORBIDDEN")

        FakeGateway.requests.clear()
        status, headers, body = http(
            "GET", "/api/v1/admin/gateway/health", token=user_token
        )
        data = payload(body)
        check("管理员放行并透传网关信封", status == 200 and data.get("gatewayId") == "gw-test")
        check("响应不回显管理令牌", ADMIN_TOKEN not in body.decode("utf-8"))
        check("响应禁止缓存（密钥只出现一次）", headers.get("Cache-Control") == "no-store")
        forwarded = FakeGateway.requests[-1]
        check(
            "服务端补管理令牌并带上操作人/角色",
            forwarded["admin"] == ADMIN_TOKEN
            and forwarded["actor"] == admin_uid
            and forwarded["roles"] == "admin",
            json.dumps(
                {
                    "admin": forwarded["admin"],
                    "actor": forwarded["actor"],
                    "roles": forwarded["roles"],
                }
            ),
        )
        check(
            "路径前缀映射到网关管理 API",
            forwarded["path"] == "/gateway/v1/admin/health",
            forwarded["path"],
        )
        check(
            "管理员不下发部门范围头",
            forwarded["depts"] is None and forwarded["tenant"] is None,
            json.dumps({"depts": forwarded["depts"], "tenant": forwarded["tenant"]}),
        )

        print("\n[转发：查询串与错误信封]")
        status, _, body = http(
            "GET",
            "/api/v1/admin/gateway/usage?tenant=acme&dept=dept-eng&period=2026-10",
            token=user_token,
        )
        data = payload(body)
        check(
            "查询串原样转发",
            status == 200
            and FakeGateway.requests[-1]["query"] == "tenant=acme&dept=dept-eng&period=2026-10"
            and data.get("query") == "tenant=acme&dept=dept-eng&period=2026-10",
            json.dumps(data, ensure_ascii=False),
        )

        status, _, body = http(
            "GET", "/api/v1/admin/gateway/policies", token=user_token
        )
        check(
            "控制面的策略路由不被通配代理抢走",
            status == 200 and "policies" in payload(body),
            json.dumps(payload(body), ensure_ascii=False),
        )

        status, _, body = http("GET", "/api/v1/admin/gateway/fail", token=user_token)
        check(
            "网关 400 错误信封原样透传",
            status == 400 and payload(body).get("code") == "INVALID_REQUEST",
            str(payload(body)),
        )

        status, _, body = http("GET", "/api/v1/admin/gateway/html", token=user_token)
        check(
            "HTML 响应不落浏览器",
            status == 502 and payload(body).get("code") == "GATEWAY_ADMIN_BAD_RESPONSE",
            str(payload(body)),
        )

        status, headers, body = http(
            "GET", "/api/v1/admin/gateway/audit/export", token=user_token
        )
        check(
            "NDJSON 审计导出原样透传",
            status == 200
            and headers.get("Content-Type", "").startswith("application/x-ndjson")
            and body == b'{"id":1}\n{"id":2}\n',
            f"{status} {headers.get('Content-Type')} {body!r}",
        )

        status, _, body = http(
            "GET", "/api/v1/admin/gateway/%2e%2e%2fusage", token=user_token
        )
        check(
            "路径穿越被拒",
            status in (400, 404) and payload(body).get("code") in
            ("GATEWAY_ADMIN_INVALID_PATH", "NOT_FOUND"),
            f"{status} {payload(body)}",
        )

        print("\n[审计：写操作留痕且密钥脱敏]")
        before = audits()
        status, _, body = http(
            "POST",
            "/api/v1/admin/gateway/providers/p1/keys",
            token=user_token,
            json_body={"label": "prod", "secret": SECRET},
        )
        data = payload(body)
        check(
            "创建密钥 201，明文只出现这一次",
            status == 201 and data["key"]["secret"] == SECRET,
            json.dumps(data, ensure_ascii=False),
        )
        check("密钥请求体确实转发到网关", SECRET in FakeGateway.requests[-1]["body"])
        after = audits()
        new_audits = after[len(before):]
        check("写操作落一条审计", len(new_audits) == 1, json.dumps(new_audits, ensure_ascii=False))
        row = new_audits[0]
        check(
            "审计记录主体/动作/对象/状态",
            row["actor_uid"] == admin_uid
            and row["action"] == "gateway.admin"
            and row["object"] == "POST /gateway/v1/admin/providers/p1/keys"
            and row["status"] == 201,
            json.dumps(row, ensure_ascii=False),
        )
        check(
            "审计里的密钥已脱敏",
            SECRET not in row["payload_json"] and "***" in row["payload_json"],
            row["payload_json"],
        )

        before = audits()
        http("GET", "/api/v1/admin/gateway/health", token=user_token)
        check("读操作不写审计", len(audits()) == len(before))

        print("\n[角色：审计员只读、运维可写、普通用户 403]")
        ordinary_uid = ordinary["profile"]["uid"]
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{ordinary_uid}",
            token=user_token,
            json_body={"role": "auditor"},
        )
        check(
            "管理员可把账号改为审计员并吊销会话",
            status == 200
            and payload(body)["profile"]["role"] == "auditor"
            and payload(body).get("sessionsRevoked", 0) >= 1,
            str(payload(body)),
        )
        status, _, _ = http("GET", "/api/v1/auth/me", token=ordinary["accessToken"])
        check("角色变更后旧令牌失效", status == 401, str(status))

        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{ordinary_uid}",
            token=user_token,
            json_body={"role": "root"},
        )
        check(
            "非法角色被拒",
            status == 400 and payload(body).get("code") == "INVALID_ROLE",
            str(payload(body)),
        )
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{admin_uid}",
            token=user_token,
            json_body={"role": "operator"},
        )
        check(
            "不能修改自己的角色",
            status == 400 and payload(body).get("code") == "SELF_ROLE_CHANGE",
            str(payload(body)),
        )

        auditor_token = login(ordinary_email)
        status, _, body = http("GET", "/api/v1/admin/gateway/health", token=auditor_token)
        check("审计员可读网关管理数据", status == 200, str(payload(body)))
        check("代理下发 auditor 角色头", FakeGateway.requests[-1]["roles"] == "auditor")
        before_count = len(FakeGateway.requests)
        status, _, body = http(
            "POST",
            "/api/v1/admin/gateway/providers/p1/keys",
            token=auditor_token,
            json_body={"label": "audit-write", "secret": "sk-auditor"},
        )
        check(
            "审计员写操作被控制面拦截",
            status == 403 and payload(body).get("code") == "FORBIDDEN",
            str(payload(body)),
        )
        check("被拦截的写没有发往网关", len(FakeGateway.requests) == before_count)

        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{ordinary_uid}",
            token=user_token,
            json_body={"role": "operator"},
        )
        check("管理员可把审计员改为运维", status == 200, str(payload(body)))
        operator_token = login(ordinary_email)
        status, _, body = http("GET", "/api/v1/admin/gateway/health", token=operator_token)
        check("运维可读网关管理数据", status == 200, str(payload(body)))
        status, _, body = http(
            "POST",
            "/api/v1/admin/gateway/providers/p1/keys",
            token=operator_token,
            json_body={"label": "ops-write", "secret": "sk-operator"},
        )
        check("运维可写网关管理数据", status == 201, str(payload(body)))
        check("代理下发 operator 角色头", FakeGateway.requests[-1]["roles"] == "operator")

        role_audits = [
            item for item in audits() if item["action"] == "user.role"
        ]
        check("角色变更落控制面审计", len(role_audits) >= 2, json.dumps(role_audits))
        last_role = role_audits[-1]
        check(
            "角色审计含前后值与操作人",
            last_role["actor_uid"] == admin_uid
            and "auditor" in last_role["payload_json"]
            and "operator" in last_role["payload_json"],
            json.dumps(last_role, ensure_ascii=False),
        )

        print("\n[配置边界]")
        original = ga.settings
        try:
            ga.settings = dataclasses.replace(original, gateway_admin_token="")
            status, _, body = http("GET", "/api/v1/admin/gateway/health", token=user_token)
            check(
                "未配置管理令牌 503",
                status == 503 and payload(body).get("code") == "GATEWAY_ADMIN_UNAVAILABLE",
                str(payload(body)),
            )
            ga.settings = dataclasses.replace(
                original, gateway_admin_url="http://127.0.0.1:1"
            )
            status, _, body = http("GET", "/api/v1/admin/gateway/health", token=user_token)
            check(
                "网关不可达 502",
                status == 502 and payload(body).get("code") == "GATEWAY_ADMIN_UNREACHABLE",
                str(payload(body)),
            )
        finally:
            ga.settings = original
        print("\n[组织与部门：增改与成员 claims]")
        stamp2 = str(int(time.time() * 1000))
        org = "acme" + stamp2[-6:]
        dept_id = "dept-ops"
        status, _, body = http(
            "POST",
            "/api/v1/admin/departments",
            token=user_token,
            json_body={"org": org, "id": dept_id, "name": "运营部"},
        )
        check("创建部门 201", status == 201, str(payload(body)))

        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/departments/{org}/{dept_id}",
            token=user_token,
            json_body={"name": "运营与增长部"},
        )
        check(
            "重命名部门",
            status == 200 and payload(body)["department"]["name"] == "运营与增长部",
            str(payload(body)),
        )
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/departments/{org}/{dept_id}",
            token=user_token,
            json_body={"name": "  "},
        )
        check(
            "空部门名被拒",
            status == 400 and payload(body).get("code") == "INVALID_DEPARTMENT",
            str(payload(body)),
        )
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/departments/{org}/dept-missing",
            token=user_token,
            json_body={"name": "不存在"},
        )
        check(
            "不存在的部门 404",
            status == 404 and payload(body).get("code") == "DEPARTMENT_NOT_FOUND",
            str(payload(body)),
        )
        status, _, body = http("GET", f"/api/v1/admin/departments?org={org}", token=user_token)
        listed = payload(body).get("departments", [])
        check(
            "部门列表显示新名称",
            status == 200 and listed and listed[0]["name"] == "运营与增长部",
            str(listed),
        )

        member = register_user(f"dept-member-{stamp}@test.local", "部门成员")
        uid = member["profile"]["uid"]
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{uid}",
            token=user_token,
            json_body={"org": org, "depts": [dept_id], "primaryDept": dept_id},
        )
        check(
            "分配部门会注销旧会话",
            status == 200 and payload(body).get("sessionsRevoked", 0) >= 1,
            str(payload(body)),
        )
        old_token = member["accessToken"]
        status, _, _ = http("GET", "/api/v1/auth/me", token=old_token)
        check("旧令牌已失效", status == 401, str(status))

        fresh = login(f"dept-member-{stamp}@test.local")
        status, _, body = http(
            "POST", "/api/v1/auth/gateway-token", token=fresh, json_body={}
        )
        gateway_token = payload(body).get("token", "")
        claims_b64 = gateway_token.split(".")[1] if gateway_token else ""
        claims = json.loads(
            base64.urlsafe_b64decode(claims_b64 + "=" * (-len(claims_b64) % 4))
        ) if claims_b64 else {}
        check(
            "重新登录后的 claims 带新部门",
            claims.get("dept") == dept_id and claims.get("depts") == [dept_id],
            json.dumps(claims, ensure_ascii=False),
        )

        print("\n[部门范围：operator 只带本部门头，审计/provider 写被控制面拦截]")
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{uid}",
            token=user_token,
            json_body={"role": "operator"},
        )
        check("管理员可把部门成员改为运维", status == 200, str(payload(body)))
        scoped_token = login(f"dept-member-{stamp}@test.local")
        status, _, body = http(
            "GET", "/api/v1/admin/gateway/health", token=scoped_token
        )
        forwarded = FakeGateway.requests[-1]
        check("部门范围运维可读健康", status == 200, str(payload(body)))
        check(
            "代理下发部门与租户范围头",
            forwarded["depts"] == dept_id and forwarded["tenant"] == org,
            json.dumps(
                {"depts": forwarded["depts"], "tenant": forwarded["tenant"]}
            ),
        )
        before_count = len(FakeGateway.requests)
        status, _, body = http(
            "GET", "/api/v1/admin/gateway/audit", token=scoped_token
        )
        check(
            "部门范围运维不能读审计",
            status == 403 and payload(body).get("code") == "FORBIDDEN",
            str(payload(body)),
        )
        status, _, body = http(
            "POST",
            "/api/v1/admin/gateway/providers",
            token=scoped_token,
            json_body={"id": "p-scope", "baseUrl": "https://api.example.com"},
        )
        check(
            "部门范围运维不能写 provider",
            status == 403 and payload(body).get("code") == "FORBIDDEN",
            str(payload(body)),
        )
        check(
            "被拦截的请求没有发往网关",
            len(FakeGateway.requests) == before_count,
            f"{before_count} -> {len(FakeGateway.requests)}",
        )

    finally:
        server.should_exit = True
        _fake.shutdown()

    print()
    print(f"gateway admin selftest: ALL PASS（{_passed} 项）")


if __name__ == "__main__":
    main()
