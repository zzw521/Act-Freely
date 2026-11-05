#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写侧幂等键（D-19 / #154 刀 B）自测。

跑法：`tongtian-server/.venv/Scripts/python.exe -X utf8 tests/test_idempotency.py`
判定只看 `^  ✓` 计数与末行「通过 N 项」，别只看退出码。

这里测的是「重试到底会不会做第二遍」，全部真打 HTTP：
- 不带 `Idempotency-Key` 的请求**一行都不落**（接键不能改变旧行为）；
- 带键重试 = 拿第一次的回执，状态码与顶层键与原件一字不差，且有 `X-TT-Idempotent-Replay: true`；
- 同键不同请求体 = 409 冲突，不是「后一次盖掉前一次」；
- 同键且上一次还在跑 = 409 执行中（带 Retry-After），并发不双跑；
- 业务失败不占键（预留当场释放），租约到期能被接管；
- 市场发布这条最需要重放的路径真的救回来了（不带键重试撞自己造的 409，带键就拿到原回执）；
- 签发令牌的路径**有意不接**（回执要落明文库），带键也不会有重放头。

顺带把刀 A 里唯一没真打的两条补上：`market.detail` 与 `market.download`
（本族会先造出一个真条目，所以能按清单声明的顶层键逐条比对）。
"""

import atexit
import io
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
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-idem-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "idempotency-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "幂等测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import api_contract  # noqa: E402
from app import idempotency  # noqa: E402
from app.api_contract import STABLE_OPERATIONS  # noqa: E402
from app.db import now_ms, tx  # noqa: E402
from app.idempotency import (  # noqa: E402
    DEFAULT_TTL_MS,
    IDEMPOTENCY_HEADER,
    LEASE_MS,
    MAX_STORED_RESPONSE_BYTES,
    REPLAY_HEADER,
    SUPPORTED_SCOPES,
)
from app.main import app  # noqa: E402

_passed = 0
ADMIN_PASSWORD = "admin-secret-123"
ACCOUNT_PASSWORD = "secret123"
OPS = {str(op["id"]): op for op in STABLE_OPERATIONS}


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


def send(method: str, path: str, *, data: bytes | None = None, content_type: str = "",
         token: str | None = None, headers: dict | None = None):
    out = {"Accept": "application/json"}
    if content_type:
        out["Content-Type"] = content_type
    if token:
        out["Authorization"] = f"Bearer {token}"
    if headers:
        out.update(headers)
    request = urllib.request.Request(BASE + path, data=data, headers=out, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:
        return 0, {}, str(exc).encode("utf-8")


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None,
         headers: dict | None = None):
    data = None
    ctype = ""
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        ctype = "application/json"
    return send(method, path, data=data, content_type=ctype, token=token, headers=headers)


def head(headers: dict, name: str):
    lowered = {str(key).lower(): value for key, value in headers.items()}
    return lowered.get(name.lower())


def payload(body) -> dict:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except Exception:
        return {}


def key_header(value: str) -> dict:
    return {IDEMPOTENCY_HEADER: value}


def multipart(fields: dict, *, file_field: str, file_name: str, file_bytes: bytes,
              content_type: str = "application/zip"):
    """手搓 multipart/form-data：urllib 不带，而市场发布只吃这个形状。"""
    boundary = "----w1IdempotencyBoundary7M3kQ"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
             f"{value}\r\n").encode("utf-8")
        )
    chunks.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
         f"filename=\"{file_name}\"\r\nContent-Type: {content_type}\r\n\r\n").encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def skill_zip(skill_name: str, version: str = "1.0.0") -> bytes:
    """合法技能包 = 一个 ZIP，根目录放 SKILL.md（带 name/version 的 front-matter + 正文）。"""
    text = (
        f"---\nname: {skill_name}\nversion: {version}\ncategory: 效率\ntags: 自测,幂等\n---\n\n"
        f"# {skill_name}\n\n这是幂等自测造的假技能，只用于真打发布路径。\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", text)
    return buffer.getvalue()


def login(email: str, password: str = ADMIN_PASSWORD):
    return http("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})


def must_login(email: str, password: str = ADMIN_PASSWORD) -> dict:
    status, _, body = login(email, password)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {body[:300]!r}")
    return payload(body)


def register(email: str, nickname: str):
    return http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": ACCOUNT_PASSWORD, "nickname": nickname},
    )


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(150):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def usage_row(sub: str, org: str = "", tokens: int = 120, day: str = "2026-11-04") -> dict:
    return {
        "sub": sub,
        "org": org,
        "modelName": "tt-test-model",
        "day": day,
        "promptTokens": tokens // 3,
        "completionTokens": tokens - tokens // 3,
        "tokensUsed": tokens,
        "calls": 1,
        "pricedCalls": 1,
        "unpricedCalls": 0,
        "latencySamples": 1,
        "latencyMsTotal": 900,
    }


def report_body(sub: str, tokens: int = 120, day: str = "2026-11-04") -> dict:
    return {"source": "engine", "deviceId": "idem-dev", "schemaVersion": 1,
            "rows": [usage_row(sub, "", tokens, day)]}


def db_int(sql: str, args: tuple = ()) -> int:
    with tx() as conn:
        row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def main() -> None:  # noqa: C901 - 自测按小节线性写，拆开反而丢上下文
    start_server()

    print("\n[新表就位：整表新建，不碰旧表]")
    probe = sqlite3.connect(os.path.join(_TMP, "market.db"))
    probe.row_factory = sqlite3.Row
    tables = {str(row["name"]) for row in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row["name"]) for row in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    columns = {str(row["name"]) for row in probe.execute("PRAGMA table_info(idempotency_keys)")}
    probe.close()
    check("idempotency_keys 表已随启动就位", "idempotency_keys" in tables, sorted(tables)[:8])
    check(
        "列齐（scope/actor/idem_key 是主键，state 分执行中与完成）",
        {"scope", "actor", "idem_key", "request_digest", "state", "status",
         "response_json", "created_at_ms", "expires_at_ms"} <= columns,
        sorted(columns),
    )
    check(
        "到期清理有索引可走（SCHEMA 里与建表同批，不存在索引早于补列的时序面）",
        any("idempotency" in name for name in indexes), sorted(indexes)[:10],
    )

    admin = must_login("admin@test.local")
    admin_token = admin["accessToken"]
    alice = payload(register("alice@test.local", "爱丽丝")[2])
    user_token = alice["accessToken"]
    uid = str(payload(http("GET", "/api/v1/auth/me", token=user_token)[2])["profile"]["uid"])

    print("\n[不带键 = 逐字节走原路径]")
    before = db_int("SELECT COUNT(*) FROM idempotency_keys")
    status, heads, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid),
                               token=user_token)
    check("不带键的上报照旧 200", status == 200, f"{status} {body[:160]!r}")
    check("不带键不写幂等表（接键没改变旧行为）",
          db_int("SELECT COUNT(*) FROM idempotency_keys") == before,
          f"{before} -> {db_int('SELECT COUNT(*) FROM idempotency_keys')}")
    check("不带键也没有重放头", head(heads, REPLAY_HEADER) is None, str(heads)[:160])

    print("\n[带键重试：拿第一次的回执，不跑第二遍]")
    key = "idem-usage-0001"
    first_status, first_heads, first_body = http(
        "POST", "/api/v1/usage/report", json_body=report_body(uid, 500), token=user_token,
        headers=key_header(key))
    first = payload(first_body)
    check("第一次带键执行 200 且没被标成重放",
          first_status == 200 and head(first_heads, REPLAY_HEADER) == "false",
          f"{first_status} {head(first_heads, REPLAY_HEADER)!r}")
    check("第一次的库里数字是这一份的量",
          first.get("accepted") == 1 and first.get("ok") is True, str(first)[:200])
    total_after_first = db_int(
        "SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1", (uid,))
    second_status, second_heads, second_body = http(
        "POST", "/api/v1/usage/report", json_body=report_body(uid, 500), token=user_token,
        headers=key_header(key))
    check("同键同体重试仍是 200", second_status == 200, f"{second_status} {second_body[:160]!r}")
    check("重试被如实标成重放", head(second_heads, REPLAY_HEADER) == "true", str(second_heads)[:180])
    check("重放的顶层键与状态码和原件一字不差（这是幂等的全部意义）",
          second_status == first_status and payload(second_body) == first,
          f"{payload(second_body)} != {first}")
    total_after_replay = db_int(
        "SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1", (uid,))
    check("重放没有再写一遍库（累计值没被第二次执行碰过）",
          total_after_replay == total_after_first,
          f"{total_after_first} -> {total_after_replay}")

    print("\n[累计值回归：幂等键在这里救的是数据正确性，不是方便]")
    http("POST", "/api/v1/usage/report", json_body=report_body(uid, 900), token=user_token)
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 500),
                           token=user_token, headers=key_header(key))
    check("迟到的旧 payload 重试只重放，不会把已报的 900 打回 500",
          status == 200 and db_int(
              "SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1",
              (uid,)) == 900,
          f"{status} 实际 {db_int('SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1', (uid,))}")
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 500),
                           token=user_token)
    check("对照：不带键重发同一份旧 payload 真的会打回去（这就是为什么需要键）",
          status == 200 and db_int(
              "SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1",
              (uid,)) == 500,
          f"实际 {db_int('SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1', (uid,))}")

    print("\n[同键不同请求体 = 冲突，不是覆盖]")
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 777),
                           token=user_token, headers=key_header(key))
    conflict = payload(body)
    check("换 body 复用同一把键 → 409 IDEMPOTENCY_CONFLICT",
          status == 409 and conflict.get("code") == "IDEMPOTENCY_CONFLICT",
          f"{status} {body[:200]!r}")
    check("冲突没有改数据（第二次执行根本没发生）",
          db_int("SELECT prompt_tokens + completion_tokens FROM usage_rollup WHERE sub = ?1",
                 (uid,)) == 500, "")
    check("冲突消息不回显对方的请求体（只说摘要不同）",
          "777" not in json.dumps(conflict, ensure_ascii=False)
          and "tt-test-model" not in str(conflict.get("message") or ""),
          str(conflict)[:200])

    print("\n[键形状：先挡下来，别浪费一次执行]")
    # 只用 ASCII 反例：HTTP 头本身传不了非 Latin-1 字符（`has space and 中文` 在 urllib
    # 就炸了，到不了服务端），清单里的键形状 `[A-Za-z0-9._-]` 也本来就把它排除在外。
    for bad in ("short", "has space", "semi;colon", "x" * 201):
        status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 120),
                               token=user_token, headers=key_header(bad))
        check(f"非法键「{bad[:12]}…」被 400 IDEMPOTENCY_KEY_INVALID 拒在门外",
              status == 400 and payload(body).get("code") == "IDEMPOTENCY_KEY_INVALID",
              f"{status} {body[:160]!r}")
    rows_before = db_int("SELECT COUNT(*) FROM idempotency_keys")
    http("POST", "/api/v1/usage/report", json_body=report_body(uid, 313), token=user_token,
         headers=key_header("bad key"))
    check("键不合法时预留都没建（也没执行副作用）",
          db_int("SELECT COUNT(*) FROM idempotency_keys") == rows_before, "")

    print("\n[执行中：并发撞同一把键不双跑]")
    idempotency._reserve("usage.report", uid, "idem-running-1", "some-digest")
    status, heads, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 210),
                               token=user_token, headers=key_header("idem-running-1"))
    check("上一次还在跑 → 409 IDEMPOTENCY_IN_PROGRESS",
          status == 409 and payload(body).get("code") == "IDEMPOTENCY_IN_PROGRESS",
          f"{status} {body[:200]!r}")
    check("响应带 Retry-After（客户端要的是「过一会儿再来」，不是「别再来了」）",
            bool(head(heads, "Retry-After")), str(heads)[:200])
    check("被挡住的那次没写库",
          db_int("SELECT COUNT(*) FROM usage_rollup WHERE sub = ?1 AND day = ?2", (uid, "2026-11-04"))
          == 1, "")
    with tx() as conn:
        conn.execute("DELETE FROM idempotency_keys WHERE idem_key = 'idem-running-1'")

    print("\n[租约到期能被接管（进程被杀不会永久占键）]")
    idempotency._reserve("usage.report", uid, "idem-stale-1", "digest-a")
    with tx() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET expires_at_ms = ?1 WHERE idem_key = ?2",
            (now_ms() - 1000, "idem-stale-1"),
        )
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body(uid, 210),
                           token=user_token, headers=key_header("idem-stale-1"))
    check("过租约的同键请求真的重跑了（拿到 200 而不是 409）",
          status == 200, f"{status} {body[:160]!r}")
    check("接管后这次的结果被缓存成完成态",
          db_int("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = ?1 AND state = 'complete'",
                 ("idem-stale-1",)) == 1, "")

    print("\n[业务失败不占键]")
    status, _, body = http("POST", "/api/v1/admin/orgs",
                           json_body={"org": "toolong", "name": "名" * 65}, token=admin_token,
                           headers=key_header("idem-org-fail-1"))
    check("校验失败 → 400 INVALID_ORG_NAME",
          status == 400 and payload(body).get("code") == "INVALID_ORG_NAME", f"{status} {body[:160]!r}")
    check("失败后这把键是空的（预留被释放，重试会真的重跑）",
          db_int("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = ?1",
                 ("idem-org-fail-1",)) == 0, "")
    status, _, body = http("POST", "/api/v1/admin/orgs",
                           json_body={"org": "toolong", "name": "改短了"}, token=admin_token,
                           headers=key_header("idem-org-fail-1"))
    check("同一把键修正后重试成功（键没被失败那次占住）",
          status == 201 and payload(body).get("created") is True, f"{status} {body[:200]!r}")
    status, heads, body = http("POST", "/api/v1/admin/orgs",
                               json_body={"org": "toolong", "name": "改短了"}, token=admin_token,
                               headers=key_header("idem-org-fail-1"))
    check("成功后重放：仍 201、created 仍是第一次的 true（不会翻成 false）",
          status == 201 and payload(body).get("created") is True
          and head(heads, REPLAY_HEADER) == "true", f"{status} {body[:200]!r}")

    print("\n[部分更新：显式字段集也算进摘要]")
    put_body_full = {"monthlyTokens": 100000, "concurrentTasks": 4, "memberMonthlyTokens": 20000,
                     "enforce": 0}
    status, _, body = http("PUT", "/api/v1/admin/orgs/toolong/quota", json_body=put_body_full,
                           token=admin_token, headers=key_header("idem-quota-1"))
    check("PUT 配额带键成功（201 之外的写：200）", status == 200, f"{status} {body[:200]!r}")
    status, heads, body2 = http("PUT", "/api/v1/admin/orgs/toolong/quota", json_body=put_body_full,
                                token=admin_token, headers=key_header("idem-quota-1"))
    check("同键同体重放（吊销数不会二次变化）",
          status == 200 and payload(body2) == payload(body)
          and head(heads, REPLAY_HEADER) == "true", f"{status}")
    status, _, body3 = http("PUT", "/api/v1/admin/orgs/toolong/quota", json_body={"enforce": 1},
                            token=admin_token, headers=key_header("idem-quota-1"))
    check("同键但只改一个字段 = 另一件事 → 409 冲突（部分更新不能共用键）",
          status == 409 and payload(body3).get("code") == "IDEMPOTENCY_CONFLICT",
          f"{status} {body3[:160]!r}")

    print("\n[市场发布：最需要重放的那条，也是唯一造出真条目的一条]")
    package = skill_zip("w1-idem-skill")
    form, ctype = multipart({"summary": "幂等自测", "category": "效率", "version": "1.0.0"},
                            file_field="package", file_name="w1-idem-skill.zip", file_bytes=package)
    status, heads, body = send("POST", "/api/v1/skills", data=form, content_type=ctype,
                               token=user_token, headers=key_header("idem-publish-1"))
    published = payload(body)
    check("带键发布 201 且回执顶层键与清单声明一字不差",
          status == 201 and set(published) == set(OPS["market.publishSkill"]["response"]),
          f"{status} 实际 {sorted(published)}")
    item_id = str((published.get("item") or {}).get("id") or "")
    check("发布拿到了条目 id", bool(item_id), str(published)[:200])
    form2, ctype2 = multipart({"summary": "幂等自测", "category": "效率", "version": "1.0.0"},
                              file_field="package", file_name="w1-idem-skill.zip", file_bytes=package)
    status, heads, body = send("POST", "/api/v1/skills", data=form2, content_type=ctype2,
                               token=user_token, headers=key_header("idem-publish-1"))
    check("同键同包重试仍是 201、且是重放（没造出第二个条目）",
          status == 201 and head(heads, REPLAY_HEADER) == "true" and payload(body) == published,
          f"{status} {head(heads, REPLAY_HEADER)!r} {body[:160]!r}")
    check("条目总数仍是一条",
          db_int("SELECT COUNT(*) FROM items WHERE item_type = 'skill'") == 1, "")
    form3, ctype3 = multipart({"summary": "换了说明", "category": "效率", "version": "1.0.0"},
                              file_field="package", file_name="w1-idem-skill.zip", file_bytes=package)
    status, _, body = send("POST", "/api/v1/skills", data=form3, content_type=ctype3,
                           token=user_token, headers=key_header("idem-publish-1"))
    check("同键换了表单字段 → 409 冲突（摘要含全部字段，换字段就是另一件事）",
          status == 409 and payload(body).get("code") == "IDEMPOTENCY_CONFLICT",
          f"{status} {body[:160]!r}")
    status, _, body = send("POST", "/api/v1/skills", data=form3, content_type=ctype3,
                           token=user_token)
    check("对照：不带键重发会撞上自己造的 409 ITEM_EXISTS（这就是键省掉的那一步）",
          status == 409 and payload(body).get("code") == "ITEM_EXISTS", f"{status} {body[:160]!r}")

    print("\n[刀 A 欠的两条探针在这里补上：detail 与 download]")
    status, _, body = http("GET", f"/api/v1/skills/{item_id}")
    detail = payload(body)
    check("market.detail 匿名可读且顶层键与清单声明一字不差（顶层只有 ok 与 item）",
          status == 200 and set(detail) == set(OPS["market.detail"]["response"]),
          f"{status} 实际 {sorted(detail)}")
    item_view = detail.get("item") or {}
    check("清单里 item 的键集合是实测出来的（版本在 item.versions 里，不在顶层）",
          set(item_view) == set(OPS["market.detail"]["nested"]["item"]),
          f"多 {sorted(set(item_view) - set(OPS['market.detail']['nested']['item']))} / "
          f"缺 {sorted(set(OPS['market.detail']['nested']['item']) - set(item_view))}")
    check("技能详情的 packageInfo 只有 fileCount/files（套件才会多 commands）",
          set(item_view.get("packageInfo") or {}) == {"fileCount", "files"}
          and "commands" not in item_view,
          str(item_view.get("packageInfo"))[:160])
    check("versions 里带 sha256 与 downloadUrl 所指的版本（客户端装前要校验）",
          len(item_view.get("versions") or []) == 1
          and (item_view["versions"][0].get("sha256") or ""),
          str(item_view.get("versions"))[:200])
    status, heads, blob = http("GET", f"/api/v1/skills/{item_id}/download")
    check("market.download 回的是二进制而不是 ok 信封（清单里那条例外是真的）",
          status == 200 and head(heads, "Content-Type") != "application/json"
          and blob[:2] == b"PK", f"{status} {head(heads, 'Content-Type')!r} {blob[:24]!r}")
    check("下载拿到的字节与发布上去的包一模一样", blob == package,
          f"{len(blob)} vs {len(package)}")

    print("\n[签发令牌的路径有意不接（回执要落明文库）]")
    for label, method, path, body_json, tok in (
        ("register", "POST", "/api/v1/auth/register",
         {"email": "bob@test.local", "password": ACCOUNT_PASSWORD, "nickname": "鲍勃"}, None),
        ("login", "POST", "/api/v1/auth/login",
         {"email": "admin@test.local", "password": ADMIN_PASSWORD}, None),
        ("logout", "POST", "/api/v1/auth/logout", {"refreshToken": "not-a-real-token"}, None),
    ):
        status, heads, resp = http(method, path, json_body=body_json, token=tok,
                                   headers=key_header(f"idem-token-{label}-1"))
        check(f"auth.{label} 带键也不重放（清单标 not-supported，服务端也确实没接）",
              head(heads, REPLAY_HEADER) is None, f"{status} {str(heads)[:200]}")
        check(f"auth.{label} 没往幂等表里落行",
              db_int("SELECT COUNT(*) FROM idempotency_keys WHERE scope LIKE 'auth.%'") == 0, "")

    print("\n[清单与注册表同源（漂移就是契约说谎）]")
    supported = sorted(SUPPORTED_SCOPES)
    declared = sorted(str(op["id"]) for op in STABLE_OPERATIONS
                      if str(op.get("idempotency")) == "supported")
    check("清单里标 supported 的集合 == 服务端注册表键集合", declared == supported,
          f"清单 {declared} / 注册表 {supported}")
    check("每条 supported 的 method/path 与注册表一字不差",
          all(OPS[op_id]["method"] == SUPPORTED_SCOPES[op_id]["method"]
              and OPS[op_id]["path"] == SUPPORTED_SCOPES[op_id]["path"] for op_id in supported),
          "")
    check("注册表里的 successStatus 与清单一致（重放要还同一个状态码）",
          all(int(OPS[op_id]["successStatus"]) == int(SUPPORTED_SCOPES[op_id]["successStatus"])
              for op_id in supported), "")
    check("契约端点此刻仍报一致（problems 空）",
          api_contract.find_problems(app) == [], str(api_contract.find_problems(app))[:300])

    def drift(label: str, mutated, expect: str) -> None:  # noqa: ANN401
        # 改的是 `api_contract` 里那个名字，不是 `idempotency` 的：模块是 `from ... import`
        # 拿到的对象引用，只改源模块的绑定，自检根本看不见（第一版就在这里假绿过）。
        original = api_contract.SUPPORTED_SCOPES
        api_contract.SUPPORTED_SCOPES = mutated
        try:
            problems = api_contract.find_problems(app)
        finally:
            api_contract.SUPPORTED_SCOPES = original
        check(label, any(expect in item for item in problems), str(problems)[:260])

    drift("清单说支持而注册表没接 → 被抓",
          {k: v for k, v in SUPPORTED_SCOPES.items() if k != "usage.report"},
          "清单说支持幂等键，但 idempotency.SUPPORTED_SCOPES 里没接")
    drift("接了键但清单没登记 → 被抓",
          dict(SUPPORTED_SCOPES, **{"ghost.op": {"method": "POST", "path": "/api/v1/ghost",
                                                  "successStatus": 200, "multipart": False,
                                                  "actor": "user"}}),
          "但清单里连这个 op 都没登记")
    def drift_declared(label: str, op_id: str, flag: str, expect: str) -> None:
        original = api_contract.STABLE_OPERATIONS
        api_contract.STABLE_OPERATIONS = tuple(
            dict(op, idempotency=flag) if op["id"] == op_id else op for op in original
        )
        try:
            problems = api_contract.find_problems(app)
        finally:
            api_contract.STABLE_OPERATIONS = original
        check(label, any(expect in item for item in problems), str(problems)[:260])

    drift_declared("清单标 supported 但注册表没接 → 被抓", "auth.logout", "supported",
                   "清单说支持幂等键，但 idempotency.SUPPORTED_SCOPES 里没接")
    drift_declared("接了键但清单标 not-supported → 被抓（集成方会按不重放安全写代码）",
                   "usage.report", "not-supported", "服务端接了幂等键，清单却标")
    drift("注册表里的 path 与清单不同 → 被抓",
          dict(SUPPORTED_SCOPES, **{"usage.report": dict(
              SUPPORTED_SCOPES["usage.report"], path="/api/v1/usage/ingest")}),
          "幂等注册表里 path=")

    print("\n[回执太大不缓存：宁可重跑也不装作重放]")
    big = {"ok": True, "blob": "x" * (MAX_STORED_RESPONSE_BYTES + 1024)}
    check("测试用的回执确实超过上限", len(json.dumps(big).encode("utf-8")) > MAX_STORED_RESPONSE_BYTES,
          str(len(json.dumps(big))))

    class FakeRequest:
        def __init__(self, key: str):
            self.headers = {IDEMPOTENCY_HEADER: key}

    class FakeResponse:
        def __init__(self):
            self.headers: dict[str, str] = {}

    calls = {"n": 0}

    def big_handler() -> dict:
        calls["n"] += 1
        return big

    out1 = idempotency.guarded("usage.report", FakeRequest("idem-big-1"), FakeResponse(),
                               actor="big-actor", digest_source={"n": 1}, handler=big_handler)
    out2 = idempotency.guarded("usage.report", FakeRequest("idem-big-1"), FakeResponse(),
                               actor="big-actor", digest_source={"n": 1}, handler=big_handler)
    check("超限回执没存下来 ⇒ 第二次是真的重跑（handler 被调用两次）",
          calls["n"] == 2 and out1 == out2 == big, f"calls={calls['n']}")
    check("重跑时明确标 replay=false（不装作是重放）",
          db_int("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = ?1 AND response_json = ''",
                 ("idem-big-1",)) == 1, "")
    with tx() as conn:
        conn.execute("DELETE FROM idempotency_keys WHERE idem_key = 'idem-big-1'")

    print("\n[到期清理：懒清，不起定时任务]")
    with tx() as conn:
        conn.execute(
            "INSERT INTO idempotency_keys (scope, actor, idem_key, request_digest, state, status, "
            "response_json, created_at_ms, expires_at_ms) VALUES "
            "('usage.report', 'ghost', 'idem-expired-1', 'd', 'complete', 200, '{}', ?1, ?2)",
            (now_ms() - DEFAULT_TTL_MS - 10, now_ms() - 1),
        )
    check("先塞一条已过期的行",
          db_int("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = ?1",
                 ("idem-expired-1",)) == 1, "")
    http("POST", "/api/v1/usage/report", json_body=report_body(uid, 121), token=user_token,
         headers=key_header("idem-sweep-trigger"))
    check("下一次带键请求顺手把它扫掉了（sweep 挂在预留路径上，不引入新线程）",
          db_int("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = ?1",
                 ("idem-expired-1",)) == 0, "")

    print("\n[常量口径：清单说的数就是代码里的数]")
    contract = api_contract.build_contract(app)
    check("契约里的 ttl / 租约 / 上限与模块常量同源",
          contract["idempotency"]["ttlMs"] == DEFAULT_TTL_MS
          and contract["idempotency"]["inProgressLeaseMs"] == LEASE_MS
          and contract["idempotency"]["maxStoredResponseBytes"] == MAX_STORED_RESPONSE_BYTES,
          str(contract["idempotency"])[:200])
    check("契约 status 已从 declared-not-enforced 翻成 enforced",
          contract["idempotency"]["status"] == "enforced", str(contract["idempotency"]["status"]))
    check("覆盖缺口里如实写了「哪三处有意不接」",
          any(gap["area"] == "幂等键" and gap["status"] == "partial"
              and "register" in gap["note"] for gap in api_contract.COVERAGE_GAPS),
          str([g["area"] for g in api_contract.COVERAGE_GAPS]))
    check("semantics 把「不带键逐字节走原路径」写在最前面（升级风险的口径）",
          "不带这个头的请求逐字节走原路径" in contract["idempotency"]["semantics"], "")

    print("\n[输出泄漏总扫]")
    with tx() as conn:
        rows = conn.execute("SELECT request_digest, response_json FROM idempotency_keys "
                            "WHERE response_json != '' LIMIT 40").fetchall()
    blob = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    check("库里不存明文口令", "secret123" not in blob and ACCOUNT_PASSWORD not in blob, blob[:200])
    check("库里不存 access/refresh 令牌（签发路径确实没接）",
          "eyJ" not in blob, blob[:200])
    check("存的是摘要不是请求体原文（用量行里的模型名没整份落库）",
          "tt-test-model" not in blob, blob[:200])

    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
