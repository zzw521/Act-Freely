"""M4.5 自测：per-key 限流、月度 token 配额与 Key 轮换。

用法: python tests/test_m4_governance.py
"""

from __future__ import annotations

import json
import sys
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

from app.api_key_limits import ApiKeyRateLimiter  # noqa: E402
from app.state import db  # noqa: E402

CHAT_PATH = "/v1/chat/completions"
BASE_KEY = {
    "label": "gov-key",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-key",
    "scopes": ["chat", "models"],
    "modelAllowlist": [],
    "expiresAt": None,
}


def create_key(**overrides) -> tuple[int, dict]:
    payload = dict(BASE_KEY)
    payload.update(overrides)
    status, _, body = http(
        "POST", "/gateway/v1/admin/api-keys", headers=admin_headers(), body=payload
    )
    return status, body


def need_key(**overrides) -> dict:
    status, body = create_key(**overrides)
    if status != 201:
        raise RuntimeError(f"创建 API Key 失败：{status} {body}")
    return body["key"]


def key_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def chat(token: str, *, max_tokens: int = 1) -> tuple[int, dict, dict]:
    return http(
        "POST",
        CHAT_PATH,
        headers=key_headers(token),
        body={
            "model": "basic",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )


def listed(key_id: str) -> dict:
    status, _, body = http(
        "GET", "/gateway/v1/admin/api-keys", headers=admin_headers()
    )
    if status != 200:
        raise RuntimeError(f"读取 Key 列表失败：{status} {body}")
    return {item["id"]: item for item in body["keys"]}[key_id]


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    if body.get("code"):
        return str(body["code"])
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


def test_validation() -> None:
    cases = [
        ("rateLimitRpm 为负", {"rateLimitRpm": -1}),
        ("rateLimitRpm 过大", {"rateLimitRpm": 10001}),
        ("rateLimitRpm 为布尔", {"rateLimitRpm": True}),
        ("rateLimitRpm 为字符串", {"rateLimitRpm": "60"}),
        ("quotaMonthlyTokens 为负", {"quotaMonthlyTokens": -1}),
        ("quotaMonthlyTokens 过大", {"quotaMonthlyTokens": 10**15 + 1}),
        ("quotaMonthlyTokens 为布尔", {"quotaMonthlyTokens": True}),
    ]
    for name, override in cases:
        status, body = create_key(**override)
        check(
            f"拒绝创建：{name}",
            status == 400 and error_code(body) == "INVALID_REQUEST",
            f"{status} {json.dumps(body, ensure_ascii=False)[:200]}",
        )


def test_rate_limit() -> None:
    key = need_key(scopes=["models"], rateLimitRpm=2, label="rpm-key")
    check(
        "创建响应带限流与配额字段",
        key.get("rateLimitRpm") == 2
        and key.get("quotaMonthlyTokens") == 0
        and key.get("rotatedAt") is None
        and key.get("usedTokens") == 0,
        str(key),
    )
    headers = key_headers(key["secret"])
    statuses = []
    for _ in range(2):
        status, _, _ = http("GET", "/v1/models", headers=headers)
        statuses.append(status)
    check("RPM 桶内两次请求放行", statuses == [200, 200], str(statuses))
    status, response_headers, body = http("GET", "/v1/models", headers=headers)
    retry_after = int(response_headers.get("retry-after") or 0)
    check(
        "超出 RPM 返回 429 与 Retry-After",
        status == 429
        and error_code(body) == "RATE_LIMITED"
        and 1 <= retry_after <= 60,
        f"{status} {response_headers} {body}",
    )

    free = need_key(scopes=["models"], label="free-key")
    status, _, _ = http("GET", "/v1/models", headers=key_headers(free["secret"]))
    check("rateLimitRpm=0 不限制", status == 200, str(status))

    now = [1000.0]
    limiter = ApiKeyRateLimiter(clock=lambda: now[0])
    check(
        "令牌桶容量与补充速率正确",
        limiter.consume("k1", 2) == 0
        and limiter.consume("k1", 2) == 0
        and limiter.consume("k1", 2) > 0,
        "rpm=2 前两次应放行",
    )
    now[0] += 30.5
    check("令牌桶按时间恢复", limiter.consume("k1", 2) == 0)
    check("不同 Key 独立计数", limiter.consume("k2", 2) == 0)


def test_monthly_quota() -> None:
    key = need_key(quotaMonthlyTokens=20, label="quota-key")
    status, _, body = chat(key["secret"])
    check("配额内首次请求成功", status == 200, f"{status} {body}")

    item = listed(key["id"])
    check(
        "列表展示当月已用与配额字段",
        item["quotaMonthlyTokens"] == 20
        and item["rateLimitRpm"] == 0
        and item["usedTokens"] == 20,
        str(item),
    )
    counter = db.query(
        "SELECT period, used_tokens FROM api_key_counters WHERE key_id=?",
        (key["id"],),
    )
    check(
        "Key 计数器已结算",
        bool(counter) and int(counter[0]["used_tokens"]) == 20,
        str(counter),
    )

    status, _, body = chat(key["secret"])
    check(
        "超出月度配额返回 429",
        status == 429 and error_code(body) == "QUOTA_TOKENS_EXCEEDED",
        f"{status} {body}",
    )

    unlimited = need_key(label="unlimited-key")
    status, _, _ = chat(unlimited["secret"])
    check("quotaMonthlyTokens=0 不限制", status == 200, str(status))
    check(
        "不限额度也记录 Key 用量",
        listed(unlimited["id"])["usedTokens"] == 20,
        str(listed(unlimited["id"])),
    )


def test_rotation() -> None:
    key = need_key(label="rotate-key")
    old_secret = key["secret"]
    status, _, _ = chat(old_secret)
    check("轮换前可用", status == 200, str(status))

    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/api-keys/{key['id']}/rotate",
        headers=admin_headers(),
    )
    rotated = body.get("key") if isinstance(body, dict) else None
    check(
        "轮换返回同一 keyId 与新明文",
        status == 200
        and isinstance(rotated, dict)
        and rotated.get("id") == key["id"]
        and rotated.get("secret", "").startswith("ttg_" + key["id"] + "_")
        and rotated["secret"] != old_secret,
        f"{status} {body}",
    )
    check(
        "轮换响应带 rotatedAt 与已用 token",
        isinstance(rotated.get("rotatedAt"), int) and rotated.get("usedTokens") == 20,
        str(rotated),
    )

    status, _, body = chat(old_secret)
    check(
        "旧 secret 立即 401",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )
    status, _, _ = chat(rotated["secret"])
    check("新 secret 可用", status == 200, str(status))

    item = listed(key["id"])
    check("列表带最近轮换时间", item["rotatedAt"] == rotated["rotatedAt"], str(item))
    audits = db.query(
        "SELECT before_json, after_json FROM audit_log "
        "WHERE action='api_key.rotate' AND object=? ORDER BY id",
        (key["id"],),
    )
    check(
        "轮换写审计且不含 secret",
        len(audits) == 1
        and json.loads(audits[0]["before_json"]) == {"rotatedAt": None}
        and json.loads(audits[0]["after_json"])["rotatedAt"] == rotated["rotatedAt"]
        and rotated["secret"] not in audits[0]["after_json"],
        str(audits),
    )

    status, _, body = http(
        "POST",
        "/gateway/v1/admin/api-keys/000000000000/rotate",
        headers=admin_headers(),
    )
    check(
        "轮换未知 Key 返回 400",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )

    http(
        "DELETE",
        f"/gateway/v1/admin/api-keys/{key['id']}",
        headers=admin_headers(),
    )
    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/api-keys/{key['id']}/rotate",
        headers=admin_headers(),
    )
    check(
        "轮换已吊销 Key 返回 400",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )


def test_role_gate() -> None:
    key = need_key(label="auditor-rotate")
    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/api-keys/{key['id']}/rotate",
        headers={**admin_headers(), "X-TT-Admin-Roles": "auditor"},
    )
    check(
        "审计员不能轮换 Key",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def main() -> int:
    server = start_gateway()
    upstreams = start_fake_upstreams(("openai",))
    try:
        configure_openai_provider(upstreams["openai"])
        test_validation()
        test_rate_limit()
        test_monthly_quota()
        test_rotation()
        test_role_gate()
    finally:
        stop_fake_upstreams(upstreams)
        stop_gateway(server)
    return report("M4 Key 治理自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
