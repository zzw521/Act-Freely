"""M3 网关 API Key：签发、鉴权、scopes、模型白名单、吊销与用量归属。"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import support  # noqa: E402
from tests.support import (  # noqa: E402
    admin_headers,
    check,
    configure_openai_provider,
    http,
    report,
    start_fake_upstreams,
    start_gateway,
    stop_fake_upstreams,
    stop_gateway,
)

from app.state import db  # noqa: E402

TOKEN_RE = re.compile(r"^ttg_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
VERSION = {"anthropic-version": "2023-06-01"}
CHAT_PATH = "/v1/chat/completions"
BASE_KEY = {
    "label": "test-key",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-key",
    "scopes": ["chat", "models"],
    "modelAllowlist": [],
    "expiresAt": None,
}


def create_key(**overrides) -> dict:
    payload = dict(BASE_KEY)
    payload.update(overrides)
    status, _, body = http(
        "POST", "/gateway/v1/admin/api-keys", headers=admin_headers(), body=payload
    )
    if status != 201:
        raise RuntimeError(f"创建 API Key 失败：{status} {body}")
    return body["key"]


def key_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def chat(token: str, model: str = "basic") -> tuple[int, dict, dict]:
    return http(
        "POST",
        CHAT_PATH,
        headers=key_headers(token),
        body={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def usage_row(key_id: str):
    rows = db.query(
        "SELECT tenant, dept, sub, api_key_id, protocol, status FROM usage_events "
        "WHERE api_key_id=? ORDER BY started_at_ms DESC LIMIT 1",
        (key_id,),
    )
    return rows[0] if rows else None


def audit_rows(action: str, object_id: str) -> list:
    return db.query(
        "SELECT actor, action, before_json, after_json FROM audit_log "
        "WHERE action=? AND object=? ORDER BY id",
        (action, object_id),
    )


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    if body.get("code"):
        return str(body["code"])
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    if error.get("tt_code"):
        return str(error["tt_code"])
    details = error.get("details")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        if details[0].get("code"):
            return str(details[0]["code"])
    return str(error.get("code") or "")


def test_admin_surface() -> None:
    invalid = [
        ("缺少 scopes", {"scopes": None}),
        ("未知 scope", {"scopes": ["admin"]}),
        ("scopes 为空", {"scopes": []}),
        ("tenant 含冒号", {"tenant": "acme:dept"}),
        ("sub 含星号", {"sub": "*"}),
        ("dept 空白", {"dept": "dept sales"}),
        ("过期时间已过", {"expiresAt": int(time.time() * 1000) - 1000}),
        ("白名单不是数组", {"modelAllowlist": "auto"}),
    ]
    for name, override in invalid:
        payload = dict(BASE_KEY)
        payload.update(override)
        status, _, body = http(
            "POST", "/gateway/v1/admin/api-keys", headers=admin_headers(), body=payload
        )
        check(
            f"拒绝创建：{name}",
            status == 400 and error_code(body) == "INVALID_REQUEST",
            f"{status} {body}",
        )

    key = create_key(label="claude-code-laptop")
    match = TOKEN_RE.match(key.get("secret", ""))
    check("创建返回 ttg_ 明文", match is not None, key.get("secret", "")[:16])
    key_id = key["id"]
    check("keyId 为 12 位十六进制", match is not None and match.group(1) == key_id)
    check(
        "创建响应含绑定与 scopes",
        key["tenant"] == "acme"
        and key["dept"] == "dept-sales"
        and key["sub"] == "u-key"
        and key["scopes"] == ["chat", "models"]
        and key["modelAllowlist"] == []
        and key["revokedAt"] is None,
        str(key),
    )
    check(
        "创建响应不含哈希字段",
        "secret_hash" not in key and "secretHash" not in key,
        str(sorted(key)),
    )

    row = db.query("SELECT secret_hash FROM api_keys WHERE key_id=?", (key_id,))[0]
    check(
        "库中只存 sha256（64 位十六进制）",
        len(row["secret_hash"]) == 64
        and row["secret_hash"] != key["secret"]
        and row["secret_hash"] not in key["secret"],
        row["secret_hash"],
    )

    status, _, body = http("GET", "/gateway/v1/admin/api-keys", headers=admin_headers())
    listed = {item["id"]: item for item in (body.get("keys") or [])}
    check(
        "列表返回 Key 且不含明文",
        status == 200
        and key_id in listed
        and "secret" not in listed[key_id]
        and listed[key_id]["label"] == "claude-code-laptop",
        str(body)[:200],
    )

    audits = audit_rows("api_key.create", key_id)
    check(
        "创建写审计且不含明文/哈希",
        len(audits) == 1
        and "secret" not in audits[0]["after_json"]
        and row["secret_hash"] not in audits[0]["after_json"],
        str(audits),
    )

    status, _, body = http(
        "DELETE", f"/gateway/v1/admin/api-keys/{key_id}", headers=admin_headers()
    )
    revoked_at = body.get("revokedAt")
    check(
        "吊销返回时间",
        status == 200 and body.get("keyId") == key_id and isinstance(revoked_at, int),
        str(body),
    )
    status, _, body = http(
        "DELETE", f"/gateway/v1/admin/api-keys/{key_id}", headers=admin_headers()
    )
    check(
        "重复吊销幂等",
        status == 200 and body.get("revokedAt") == revoked_at,
        str(body),
    )
    status, _, body = http(
        "DELETE", "/gateway/v1/admin/api-keys/000000000000", headers=admin_headers()
    )
    check(
        "吊销未知 Key 返回 400",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )
    check(
        "吊销写审计",
        len(audit_rows("api_key.revoke", key_id)) == 1,
        str(audit_rows("api_key.revoke", key_id)),
    )


def test_rejections() -> None:
    key = create_key(scopes=["chat"])
    token = key["secret"]
    key_id, secret = TOKEN_RE.match(token).groups()
    bad_cases = [
        ("缺少 secret", f"ttg_{key_id}"),
        ("未知 keyId", f"ttg_000000000000_{secret}"),
        ("secret 错误", f"ttg_{key_id}_{'A' * 43}"),
        ("secret 长度不对", f"ttg_{key_id}_{secret[:-1]}"),
        ("非十六进制 keyId", f"ttg_zzzzzzzzzzzz_{secret}"),
    ]
    for name, bad in bad_cases:
        status, headers, body = chat(bad)
        check(
            f"拒绝 {name}",
            status == 401 and error_code(body) == "AUTH_REQUIRED",
            f"{status} {body}",
        )

    status, _, body = http(
        "GET", "/gateway/v1/admin/providers", headers={"X-TT-Admin-Token": token}
    )
    check(
        "ttg_ 不能当管理令牌",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )

    scoped = create_key(scopes=["chat"])
    status, _, body = http(
        "GET", "/v1/models", headers=key_headers(scoped["secret"])
    )
    check(
        "缺少 models scope 被拒绝",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    cross = create_key(scopes=["chat"], dept="dept-sales")
    status, _, body = http(
        "POST",
        CHAT_PATH,
        headers={**key_headers(cross["secret"]), "X-TT-Department": "dept-eng"},
        body={"model": "basic", "messages": [{"role": "user", "content": "hi"}]},
    )
    check(
        "Key 不能切换到未绑定部门",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def test_scopes_and_models() -> None:
    chat_key = create_key(scopes=["chat"], modelAllowlist=["basic"])
    status, _, body = chat(chat_key["secret"], "basic")
    check("chat scope + 白名单内模型可用", status == 200, f"{status} {body}")
    status, _, body = chat(chat_key["secret"], "advanced")
    check(
        "白名单外模型被拒绝",
        status == 403 and error_code(body) == "MODEL_NOT_ALLOWED",
        f"{status} {body}",
    )

    status, _, body = http(
        "GET", "/v1/models", headers=key_headers(chat_key["secret"])
    )
    check(
        "缺少 models scope 的列表请求被拒绝",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    models_key = create_key(scopes=["models"], modelAllowlist=["basic"])
    status, _, body = http("GET", "/v1/models", headers=key_headers(models_key["secret"]))
    names = {item.get("id") for item in (body.get("data") or [])}
    check(
        "模型列表按白名单过滤",
        status == 200 and names == {"basic"},
        f"{status} {sorted(names)}",
    )

    auto_key = create_key(scopes=["chat"], modelAllowlist=["auto"])
    status, _, body = chat(auto_key["secret"], "auto")
    check("白名单可以写别名", status == 200, f"{status} {body}")

    status, _, body = http(
        "POST",
        CHAT_PATH,
        headers={"x-api-key": chat_key["secret"]},
        body={"model": "basic", "messages": [{"role": "user", "content": "hi"}]},
    )
    check("x-api-key 头可用", status == 200, f"{status} {body}")

    row = usage_row(chat_key["id"])
    check(
        "用量按 Key 绑定主体记账并带 apiKeyId",
        row is not None
        and row["tenant"] == "acme"
        and row["dept"] == "dept-sales"
        and row["sub"] == "u-key"
        and row["api_key_id"] == chat_key["id"]
        and row["status"] == "ok",
        str(row),
    )
    quota = db.query(
        "SELECT used_tokens FROM quota_counters WHERE tenant='acme' "
        "AND dept='dept-sales' AND sub='u-key'"
    )
    check(
        "配额记在绑定主体上",
        bool(quota) and int(quota[0]["used_tokens"]) > 0,
        str(quota),
    )


def test_other_protocols() -> None:
    key = create_key(scopes=["messages", "gemini"], modelAllowlist=[])
    token = key["secret"]

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers={**VERSION, "x-api-key": token},
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check("Anthropic messages scope 可用", status == 200, f"{status} {body}")

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers={"x-goog-api-key": token},
        body={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 64},
        },
    )
    check("Gemini generateContent 可用", status == 200, f"{status} {body}")

    status, _, body = http(
        "GET", "/v1beta/models", headers={"x-goog-api-key": token}
    )
    check("Gemini 模型列表可用", status == 200, f"{status} {body}")

    chat_only = create_key(scopes=["chat"])
    status, _, body = http(
        "POST",
        "/v1/messages",
        headers={**VERSION, "x-api-key": chat_only["secret"]},
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check(
        "Anthropic 缺少 messages scope 被拒绝",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers={"x-goog-api-key": chat_only["secret"]},
        body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    check(
        "Gemini 缺少 gemini scope 被拒绝",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def test_expiry_and_revocation() -> None:
    expiring = create_key(
        scopes=["chat"], expiresAt=int(time.time() * 1000) + 1500
    )
    status, _, body = chat(expiring["secret"])
    check("过期前可用", status == 200, f"{status} {body}")
    time.sleep(1.8)
    status, _, body = chat(expiring["secret"])
    check(
        "过期后立即失效",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )

    revocable = create_key(scopes=["chat"])
    status, _, body = chat(revocable["secret"])
    check("吊销前可用", status == 200, f"{status} {body}")
    status, _, body = http(
        "DELETE",
        f"/gateway/v1/admin/api-keys/{revocable['id']}",
        headers=admin_headers(),
    )
    check("吊销请求成功", status == 200, f"{status} {body}")
    status, _, body = chat(revocable["secret"])
    check(
        "吊销后下一次请求即失效（无缓存）",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )


def main() -> int:
    server = start_gateway()
    upstreams = start_fake_upstreams(("openai",))
    try:
        configure_openai_provider(upstreams["openai"])
        test_admin_surface()
        test_rejections()
        test_scopes_and_models()
        test_other_protocols()
        test_expiry_and_revocation()
    finally:
        stop_fake_upstreams(upstreams)
        stop_gateway(server)
    return report("全部 API Key 测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
