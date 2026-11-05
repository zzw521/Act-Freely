#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.3 控制面审计哈希链：分页、验链、NDJSON 导出与角色权限。

用法: python tests/test_m4_audit.py
"""

from __future__ import annotations

import atexit
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
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-audit-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "audit-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

_passed = 0
CHAIN_LEN = 64


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def register(email: str, nickname: str) -> dict:
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
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def main() -> None:
    server = start_server()
    try:
        admin_token = login("admin@test.local", "admin-secret-123")
        me = payload(http("GET", "/api/v1/auth/me", token=admin_token)[2])["profile"]
        admin_uid = me["uid"]

        stamp = int(time.time() * 1000)
        auditor = register(f"auditor-{stamp}@test.local", "审计员")
        operator = register(f"operator-{stamp}@test.local", "运维")

        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{auditor['profile']['uid']}",
            json_body={"role": "auditor"},
            token=admin_token,
        )
        check("提升为审计员", status == 200, f"{status} {body}")
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{operator['profile']['uid']}",
            json_body={"role": "operator"},
            token=admin_token,
        )
        check("提升为运维", status == 200, f"{status} {body}")

        auditor_token = login(f"auditor-{stamp}@test.local")
        ordinary = register(f"plain-{stamp}@test.local", "普通用户")

        print("\n[分页与链字段]")
        status, _, body = http("GET", "/api/v1/admin/audit?after=0&limit=100", token=admin_token)
        data = payload(body)
        entries = data.get("entries") or []
        check("管理员可读", status == 200 and data.get("ok") is True, f"{status} {body[:200]}")
        check("角色变更已入链", any(e["action"] == "user.role" for e in entries), str(len(entries)))
        check(
            "链字段完整",
            all(
                len(str(e.get("entryHash") or "")) == CHAIN_LEN
                and len(str(e.get("prevHash") or "")) == CHAIN_LEN
                for e in entries
            ),
            json.dumps(entries[:1], ensure_ascii=False),
        )
        check(
            "prevHash 逐条串联",
            all(
                entries[i]["prevHash"] == entries[i - 1]["entryHash"]
                for i in range(1, len(entries))
            ),
        )
        role_entry = next(e for e in entries if e["action"] == "user.role")
        check(
            "角色审计记录前后值",
            role_entry["before"].get("role") in ("user", "auditor")
            and role_entry["after"].get("role") in ("auditor", "operator")
            and role_entry["actor"] == admin_uid
            and role_entry["actorRole"] == "admin",
            json.dumps(role_entry, ensure_ascii=False),
        )
        check("链头与末条一致且无旧行", data.get("headHash") == entries[-1]["entryHash"] and data.get("legacyRows") == 0)

        status, _, body = http("GET", "/api/v1/admin/audit?limit=0", token=admin_token)
        check("limit 越界 400", status == 400 and payload(body).get("code") == "INVALID_REQUEST", str(body))

        status, _, body = http(
            "GET", f"/api/v1/admin/audit?after={entries[0]['id']}&limit=1", token=admin_token
        )
        page = payload(body)
        check(
            "after 游标",
            status == 200
            and len(page.get("entries") or []) == 1
            and page["entries"][0]["id"] > entries[0]["id"],
            str(body)[:200],
        )

        print("\n[验链]")
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=admin_token)
        result = payload(body)
        check(
            "正常链 ok",
            status == 200 and result.get("ok") is True and result.get("brokenAt") is None,
            json.dumps(result),
        )

        print("\n[导出]")
        status, headers, body = http("GET", "/api/v1/admin/audit/export?limit=1000", token=admin_token)
        text = body.decode("utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        check(
            "NDJSON 导出",
            status == 200
            and headers.get("Content-Type", "").startswith("application/x-ndjson")
            and len(lines) == len(entries),
            f"{status} {headers.get('Content-Type')} {len(lines)}",
        )
        check(
            "导出头带链头",
            headers.get("X-TT-Audit-Head") == result.get("headHash"),
            str(dict(headers)),
        )
        exported = [json.loads(line) for line in lines]
        check(
            "导出含前后值与角色",
            all("before" in item and "after" in item and item.get("actorRole") for item in exported),
        )
        check("导出不外泄登录令牌", admin_token not in text and auditor_token not in text)

        print("\n[角色]")
        status, _, body = http("GET", "/api/v1/admin/audit", token=auditor_token)
        check("审计员可读控制面审计", status == 200 and payload(body).get("ok") is True, f"{status}")
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=auditor_token)
        check("审计员可验链", status == 200, f"{status}")
        status, headers, body = http("GET", "/api/v1/admin/audit/export", token=auditor_token)
        check(
            "审计员可导出",
            status == 200 and headers.get("Content-Type", "").startswith("application/x-ndjson"),
            f"{status}",
        )
        status, _, body = http("GET", "/api/v1/admin/audit", token=ordinary["accessToken"])
        check("普通用户 403", status == 403 and payload(body).get("code") == "FORBIDDEN", f"{status}")
        status, _, body = http("GET", "/api/v1/admin/audit")
        check("未登录 401", status == 401, f"{status}")

        print("\n[篡改定位]")
        first_id = entries[0]["id"]
        conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
        conn.execute("UPDATE admin_audit SET action='login' WHERE id=?", (first_id,))
        conn.commit()
        conn.close()
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=admin_token)
        tampered = payload(body)
        check(
            "篡改后 200 + ok=false",
            status == 200 and tampered.get("ok") is False,
            f"{status} {body}",
        )
        check(
            "brokenAt 指向被篡改行",
            tampered.get("brokenAt") == first_id,
            json.dumps(tampered),
        )
    finally:
        server.should_exit = True
    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
