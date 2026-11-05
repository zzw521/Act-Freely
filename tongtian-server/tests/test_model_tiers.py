#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 D-44 服务端半边：模型档位下发的存储、签名、版本单调与回退口径。

覆盖：
- 无表时如实 404（客户端应回退本地 models.json），有表时 ETag/304；
- 文档校验：未知档位、空链、链引用未登记模型、缺省 auto 却无选档依据、契约版本不符、多余字段；
- revision 单调：内容不变不升版、变了则 +1、回滚是「重新发布成新版本」且旧内容可比对；
- 作用域优先级：组织表覆盖全局表，撤销组织表后回落全局；个人账号按 personal-<uid> 下发；
- 签名：验签巡检全绿；改一个字节即不通过并给出中文原因；
- 角色边界与审计入链，审计链在档位写入后仍可验。

用法: python tests/test_model_tiers.py
"""

from __future__ import annotations

import atexit
import base64
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

_TMP = tempfile.mkdtemp(prefix="tt-tiers-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "tiers-test-secret"
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
from app.model_tiers import (  # noqa: E402
    TIER_AUDIENCE,
    TIER_CONTRACT_VERSION,
    canonical_bytes,
    document_digest,
)

_passed = 0


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


def http(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    token: str | None = None,
    headers: dict | None = None,
):
    send = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        send["Content-Type"] = "application/json"
    if token:
        send["Authorization"] = f"Bearer {token}"
    send.update(headers or {})
    request = urllib.request.Request(BASE + path, data=data, headers=send, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, _headers(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _headers(exc.headers), exc.read()


def _headers(message) -> dict:
    return {str(key).lower(): value for key, value in message.items()}


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def login(email: str, password: str) -> str:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {body[:200]}")
    return payload(body)["accessToken"]


def register(email: str, nickname: str) -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {body[:200]}")
    return payload(body)


def start_server():
    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


DOCUMENT = {
    "contractVersion": TIER_CONTRACT_VERSION,
    "defaultTier": "auto",
    "byokAllowed": True,
    "models": [
        {
            "id": "small-fast",
            "provider": "demo",
            "upstreamModel": "demo-small",
            "displayName": "经济小模型",
            "contextWindow": 32000,
            "maxOutputTokens": 4000,
        },
        {
            "id": "mid-main",
            "provider": "demo",
            "upstreamModel": "demo-mid",
            "displayName": "基础主力",
            "contextWindow": 128000,
            "maxOutputTokens": 8000,
        },
        {
            "id": "big-long",
            "provider": "demo",
            "upstreamModel": "demo-big",
            "displayName": "高级长上下文",
            "contextWindow": 256000,
            "maxOutputTokens": 16000,
        },
    ],
    "tiers": {
        "economy": {"label": "经济", "chain": ["small-fast"], "byokAllowed": False, "note": ""},
        "basic": {"label": "基础", "chain": ["mid-main", "small-fast"], "note": ""},
        "advanced": {
            "label": "高级",
            "chain": ["big-long", "mid-main"],
            "byokAllowed": True,
            "note": "",
        },
    },
    "autoPolicy": {
        "rules": [
            {"taskKind": "chat", "maxContextTokens": 8000, "tier": "economy"},
            {"taskKind": "report", "maxContextTokens": None, "tier": "advanced"},
        ],
        "fallbackTier": "basic",
    },
}


def main() -> None:
    server = start_server()
    try:
        admin_token = login("admin@test.local", "admin-secret-123")
        stamp = int(time.time())
        member = register(f"member-{stamp}@test.local", "组织成员")
        other = register(f"other-{stamp}@test.local", "别组织成员")
        member_uid = member["profile"]["uid"]
        other_uid = other["profile"]["uid"]

        print("\n[无表时的回退口径]")
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=member["accessToken"]
        )
        data = payload(body)
        check(
            "未下发档位表返回 404 且说明回退本地配置",
            status == 404
            and data.get("code") == "TIER_TABLE_NOT_FOUND"
            and "models.json" in str(data.get("message")),
            f"{status} {body[:200]}",
        )

        print("\n[文档校验]")
        bad_cases = [
            (
                "未知档位名",
                {**DOCUMENT, "tiers": {**DOCUMENT["tiers"], "turbo": {"chain": ["mid-main"]}}},
            ),
            ("空模型链", {**DOCUMENT, "tiers": {**DOCUMENT["tiers"], "basic": {"chain": []}}}),
            (
                "链引用未登记模型",
                {
                    **DOCUMENT,
                    "tiers": {
                        **DOCUMENT["tiers"],
                        "basic": {"chain": ["ghost-model"]},
                    },
                },
            ),
            (
                "缺省 auto 但没有选档依据",
                {**DOCUMENT, "autoPolicy": None},
            ),
            (
                "契约版本不受支持",
                {**DOCUMENT, "contractVersion": "model-tiers.v99"},
            ),
            (
                "auto 规则指向未声明档位",
                {
                    **DOCUMENT,
                    "autoPolicy": {"rules": [{"taskKind": "", "tier": "turbo"}], "fallbackTier": "basic"},
                },
            ),
            (
                "多余字段",
                {**DOCUMENT, "masterKey": "nope"},
            ),
        ]
        for label, body_doc in bad_cases:
            status, _, body = http(
                "PUT",
                "/api/v1/admin/model-tiers/global/-",
                json_body=body_doc,
                token=admin_token,
            )
            data = payload(body)
            check(
                f"拒绝：{label}",
                status == 400 and data.get("code") == "TIER_INVALID",
                f"{status} {body[:160]}",
            )

        print("\n[发布全局档位表]")
        status, _, body = http(
            "PUT",
            "/api/v1/admin/model-tiers/global/-",
            json_body=DOCUMENT,
            token=admin_token,
        )
        data = payload(body)
        check("首次发布 revision=1", status == 200 and data.get("revision") == 1, str(body[:200]))
        check("changed 为真", data.get("changed") is True)
        revision_1_digest = data["digest"]
        check(
            "客户端可用收到的文档自行复算摘要",
            document_digest(data["document"]) == revision_1_digest
            and canonical_bytes(data["document"]).startswith(b"{"),
            str(data.get("digest")),
        )
        check(
            "落库的是补全默认值后的规范文档",
            data["document"]["tiers"]["basic"].get("byokAllowed") is None
            and "autoPolicy" in data["document"],
            json.dumps(data["document"]["tiers"]["basic"], ensure_ascii=False),
        )
        check(
            "带 RS256 签名与 kid",
            len(str(data.get("signature") or "").split(".")) == 3
            and str(data.get("signingKid") or "").startswith("k-"),
            str(data.get("signingKid")),
        )
        check("档位中文名可解释", any(
            t["label"] == "经济" for t in payload(
                http("GET", "/api/v1/admin/model-tiers", token=admin_token)[2]
            ).get("tiers", [])
        ))

        print("\n[客户端拉取与 304]")
        status, headers, body = http(
            "GET", "/api/v1/model-tiers/effective", token=member["accessToken"]
        )
        data = payload(body)
        check(
            "已登录可拉全局表",
            status == 200
            and data.get("source") == "global"
            and data.get("localFallbackSuggested") is False,
            f"{status} {body[:200]}",
        )
        check(
            "响应里的文档摘要与签名声明一致",
            document_digest(data["document"]) == data["digest"],
        )
        etag = headers.get("etag") or ""
        check(
            "带 ETag 与版本号头",
            etag.startswith('"tiers-global-') and headers.get("x-tt-tier-revision") == "1",
            str(etag),
        )
        status, _, _ = http(
            "GET",
            "/api/v1/model-tiers/effective",
            token=member["accessToken"],
            headers={"If-None-Match": etag},
        )
        check("同版本命中 304", status == 304, str(status))
        status, _, body = http("GET", "/api/v1/model-tiers/effective")
        check("未登录 401", status == 401, str(status))

        print("\n[内容不变不升版 / 变了才升版]")
        status, _, body = http(
            "PUT",
            "/api/v1/admin/model-tiers/global/-",
            json_body=DOCUMENT,
            token=admin_token,
        )
        data = payload(body)
        check(
            "重复发布同样内容不升版本",
            status == 200 and data.get("changed") is False and data.get("revision") == 1,
            str(body[:200]),
        )
        changed_doc = json.loads(json.dumps(DOCUMENT))
        changed_doc["tiers"]["economy"]["chain"] = ["small-fast", "mid-main"]
        status, _, body = http(
            "PUT",
            "/api/v1/admin/model-tiers/global/-",
            json_body=changed_doc,
            token=admin_token,
        )
        data = payload(body)
        check(
            "改动后 revision 递增",
            status == 200 and data.get("changed") is True and data.get("revision") == 2,
            str(body[:200]),
        )
        revision_2_digest = data["digest"]

        print("\n[回滚是新发布，不是版本号倒退]")
        status, _, body = http(
            "POST",
            "/api/v1/admin/model-tiers/global/-/revert",
            json_body={"revision": 1},
            token=admin_token,
        )
        data = payload(body)
        check(
            "回到第 1 版内容并记为第 3 版",
            status == 200
            and data.get("revision") == 3
            and data.get("revertToRevision") == 1
            and document_digest(data["document"]) == revision_1_digest,
            str(body[:220]),
        )
        status, _, body = http(
            "POST",
            "/api/v1/admin/model-tiers/global/-/revert",
            json_body={"revision": 3},
            token=admin_token,
        )
        check(
            "不能回滚到当前或更新的版本",
            status == 400 and payload(body).get("code") == "TIER_NOT_AHEAD",
            str(body[:160]),
        )
        status, _, body = http(
            "GET", f"/api/v1/model-tiers/effective?x={stamp}", token=member["accessToken"]
        )
        check(
            "客户端只会看到递增版本",
            status == 200 and payload(body).get("revision") == 3,
            str(body[:160]),
        )

        print("\n[按历史版本回看]")
        status, _, body = http(
            "GET", "/api/v1/model-tiers/at?revision=2", token=member["accessToken"]
        )
        data = payload(body)
        check(
            "任务元数据里的 revision 可取回当时那一版",
            status == 200
            and data.get("revision") == 2
            and data.get("digest") == revision_2_digest,
            str(body[:200]),
        )
        status, _, body = http(
            "GET", "/api/v1/model-tiers/at?revision=99", token=member["accessToken"]
        )
        check(
            "不存在的版本 404",
            status == 404 and payload(body).get("code") == "TIER_REVISION_NOT_FOUND",
            str(body[:160]),
        )

        print("\n[作用域优先级]")
        org_id = f"acme{stamp}"
        status, _, body = http(
            "PATCH",
            f"/api/v1/admin/users/{member_uid}",
            json_body={"org": org_id},
            token=admin_token,
        )
        check("成员划入组织", status == 200, f"{status} {body[:160]}")
        member_token = login(f"member-{stamp}@test.local", "secret123")
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=member_token
        )
        check(
            "组织尚无表时回落全局",
            status == 200 and payload(body).get("source") == "global",
            str(body[:160]),
        )
        org_doc = json.loads(json.dumps(DOCUMENT))
        org_doc["tiers"]["advanced"]["chain"] = ["mid-main"]
        status, _, body = http(
            "PUT",
            f"/api/v1/admin/model-tiers/org/{org_id}",
            json_body=org_doc,
            token=admin_token,
        )
        check(
            "发布组织表",
            status == 200 and payload(body).get("scope") == {"kind": "org", "id": org_id},
            str(body[:200]),
        )
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=member_token
        )
        data = payload(body)
        check(
            "组织成员拿到组织表",
            status == 200 and data.get("source") == "org" and data["scope"]["id"] == org_id,
            str(body[:200]),
        )
        other_token = login(f"other-{stamp}@test.local", "secret123")
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=other_token
        )
        check(
            "别的账号仍拿全局表",
            status == 200 and payload(body).get("source") == "global",
            str(body[:200]),
        )
        personal_doc = json.loads(json.dumps(DOCUMENT))
        personal_doc["defaultTier"] = "economy"
        personal_doc["autoPolicy"] = None
        status, _, body = http(
            "PUT",
            f"/api/v1/admin/model-tiers/org/personal-{other_uid}",
            json_body=personal_doc,
            token=admin_token,
        )
        check(
            "个人账号可按 personal-<uid> 下发",
            status == 200 and payload(body).get("revision") == 1,
            str(body[:200]),
        )
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=other_token
        )
        data = payload(body)
        check(
            "个人表优先于全局表",
            status == 200
            and data.get("source") == "org"
            and data["document"]["defaultTier"] == "economy",
            str(body[:220]),
        )
        status, _, body = http(
            "DELETE", f"/api/v1/admin/model-tiers/org/personal-{other_uid}", token=admin_token
        )
        check(
            "撤销个人表",
            status == 200 and payload(body).get("ok") is True,
            str(body[:160]),
        )
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=other_token
        )
        check(
            "撤销后回落全局",
            status == 200 and payload(body).get("source") == "global",
            str(body[:160]),
        )

        print("\n[角色与参数边界]")
        status, _, body = http(
            "GET", "/api/v1/admin/model-tiers", token=other_token
        )
        check(
            "普通用户不可进管理面",
            status == 403 and payload(body).get("code") == "FORBIDDEN",
            str(body[:160]),
        )
        status, _, body = http(
            "PUT",
            "/api/v1/admin/model-tiers/org/Bad%20Org",
            json_body=DOCUMENT,
            token=admin_token,
        )
        check(
            "组织标识不合法被拒",
            status == 400 and payload(body).get("code") == "INVALID_SCOPE",
            str(body[:160]),
        )
        status, _, body = http(
            "PUT",
            "/api/v1/admin/model-tiers/global/acme",
            json_body=DOCUMENT,
            token=admin_token,
        )
        check("全局作用域不接受组织标识", status == 400, str(body[:160]))

        print("\n[签名巡检与篡改]")
        status, _, body = http(
            "GET", "/api/v1/admin/model-tiers/verify", token=admin_token
        )
        data = payload(body)
        check(
            "落库的档位表逐张可信",
            status == 200
            and data.get("tables", 0) >= 2
            and data.get("untrusted") == 0
            and all(item["trusted"] for item in data.get("items", [])),
            str(body[:220]),
        )
        conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
        live_doc = json.loads(
            conn.execute(
                "SELECT document_json FROM model_tier_tables "
                "WHERE scope_kind='global' AND scope_id=''"
            ).fetchone()[0]
        )
        live_doc["tiers"]["advanced"]["chain"] = ["small-fast"]
        conn.execute(
            "UPDATE model_tier_tables SET document_json = ? "
            "WHERE scope_kind='global' AND scope_id=''",
            (json.dumps(live_doc, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()
        status, _, body = http(
            "GET", "/api/v1/admin/model-tiers/verify", token=admin_token
        )
        data = payload(body)
        tampered = [item for item in data.get("items", []) if not item["trusted"]]
        check(
            "改一个字节即验签不通过并点名",
            data.get("untrusted") == 1
            and tampered[0]["scopeKind"] == "global"
            and "摘要" in tampered[0]["reason"],
            str(body[:260]),
        )
        status, _, body = http(
            "GET", "/api/v1/model-tiers/effective", token=other_token
        )
        data = payload(body)
        check(
            "被篡改的那张表正是该账号的生效作用域",
            data.get("source") == "global",
            str(body[:160]),
        )
        check(
            "篡改后响应里的摘要与文档不再自洽（客户端据此拒绝）",
            document_digest(data["document"]) != data["digest"],
        )

        print("\n[审计入链且链仍可验]")
        status, _, body = http("GET", "/api/v1/admin/audit?limit=200", token=admin_token)
        entries = payload(body).get("entries") or []
        actions = {entry["action"] for entry in entries}
        check(
            "发布/回滚/撤销都留痕",
            {"model_tier.publish", "model_tier.revert", "model_tier.withdraw"} <= actions,
            str(sorted(a for a in actions if a.startswith("model_tier"))),
        )
        tier_entry = next(e for e in entries if e["action"] == "model_tier.publish")
        check(
            "审计里记的是版本与摘要",
            isinstance(tier_entry["after"].get("revision"), int)
            and len(str(tier_entry["after"].get("digest") or "")) == 64,
            json.dumps(tier_entry["after"], ensure_ascii=False),
        )
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=admin_token)
        check(
            "档位写入后审计链仍可验",
            status == 200 and payload(body).get("ok") is True,
            str(body[:200]),
        )

        print("\n[受众隔离]")
        claims = json.loads(
            base64.urlsafe_b64decode(data["signature"].split(".")[1] + "===").decode("utf-8")
        )
        check(
            "档位签名用独立受众与作用域声明",
            claims.get("aud") == TIER_AUDIENCE
            and claims.get("typ") == "model-tiers"
            and claims.get("rev") == data["revision"]
            and claims.get("digest") == data["digest"],
            json.dumps(claims, ensure_ascii=False),
        )
        status, _, body = http("GET", "/api/v1/auth/me", token=data["signature"])
        check(
            "档位签名不能当访问令牌用",
            status == 401,
            f"{status} {body[:120]}",
        )
    finally:
        server.should_exit = True
    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
