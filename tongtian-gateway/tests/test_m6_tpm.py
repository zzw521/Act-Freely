"""M6.4 TPM 限流自测：主体/Key 两级桶、进程内与 Redis 共享、结算退回。

用法: python tests/test_m6_tpm.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import support  # noqa: E402
from tests.support import (  # noqa: E402
    admin_headers,
    check,
    configure_openai_provider,
    http,
    make_token,
    report,
    start_fake_upstreams,
    start_gateway,
    stop_fake_upstreams,
    stop_gateway,
)

from app.tpm import TpmBucket, TpmLimiter, TpmReservation  # noqa: E402

REDIS_URL = os.environ.get("TT_GATEWAY_REDIS_URL", "redis://127.0.0.1:6391/0")
CHAT_PATH = "/v1/chat/completions"
BASE_KEY = {
    "label": "tpm-key",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-tpm-key",
    "scopes": ["chat", "models"],
    "modelAllowlist": [],
    "expiresAt": None,
}


def configure_policy() -> None:
    """主体桶：dept-sales 400 TPM；其他部门沿用默认（不限）。"""
    document = json.loads(json.dumps(support.TEST_POLICY))
    document["quotaDefaults"]["tpm"] = 0
    document["departments"]["dept-sales"]["quota"]["tpm"] = 400
    support.POLICY_FILE.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def chat(
    headers: dict[str, str], *, max_tokens: int = 200, timeout: float = 10
) -> tuple[int, dict, dict]:
    return http(
        "POST",
        CHAT_PATH,
        headers=headers,
        body={
            "model": "basic",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=timeout,
    )


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    if body.get("code"):
        return str(body["code"])
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


def chat_in_thread(headers: dict[str, str], result: dict) -> threading.Thread:
    def run() -> None:
        result["response"] = chat(headers)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def retry_after(headers: dict) -> int:
    try:
        return int(headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        return 0


def test_validation() -> None:
    cases = [
        ("rateLimitTpm 为负", {"rateLimitTpm": -1}),
        ("rateLimitTpm 过大", {"rateLimitTpm": 10**9 + 1}),
        ("rateLimitTpm 为布尔", {"rateLimitTpm": True}),
        ("rateLimitTpm 为字符串", {"rateLimitTpm": "300"}),
    ]
    for name, override in cases:
        status, body = create_key(**override)
        check(
            f"拒绝创建：{name}",
            status == 400 and error_code(body) == "INVALID_REQUEST",
            f"{status} {json.dumps(body, ensure_ascii=False)[:200]}",
        )

    key = need_key(rateLimitTpm=300, label="tpm-fields")
    check(
        "创建响应带 rateLimitTpm",
        key.get("rateLimitTpm") == 300,
        str(key),
    )
    status, _, body = http(
        "GET", "/gateway/v1/admin/api-keys", headers=admin_headers()
    )
    listed = {
        item["id"]: item for item in (body.get("keys") or [])
    }.get(key["id"], {})
    check(
        "列表带 rateLimitTpm",
        status == 200 and listed.get("rateLimitTpm") == 300,
        str(listed),
    )


def test_local_bucket() -> None:
    now = [1000.0]
    limiter = TpmLimiter(clock=lambda: now[0])
    bucket = TpmBucket("principal", "acme:dept-eng:u-local", 6000)
    reservation = limiter.reserve([bucket], 1000)
    check("进程内桶首次放行", reservation.cost == 1000 and len(reservation.buckets) == 1)

    try:
        limiter.reserve([bucket], 5500)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "")
        wait = int((getattr(exc, "headers", None) or {}).get("Retry-After", 0))
        check(
            "进程内桶不足返回 429 与 Retry-After",
            code == "RATE_LIMITED" and 5 <= wait <= 6,
            f"{code} retry={wait}",
        )
    else:
        check("进程内桶不足返回 429 与 Retry-After", False, "未抛错")

    limiter.refund(reservation, 100)
    check("结算退回未用部分", limiter.reserve([bucket], 5900).cost == 5900)

    fresh = TpmBucket("principal", "acme:dept-eng:u-fresh", 1000)
    limiter.reserve([fresh], 100)
    limiter.refund(TpmReservation(buckets=(fresh,), cost=5000), 0)
    check(
        "退回不超过桶容量",
        limiter.reserve([fresh], 1000).cost == 1000,
        "超额退回应被容量截断",
    )

    small = TpmBucket("key", "ttg-key-small", 1)
    limiter.reserve([small], 1)
    try:
        limiter.reserve([small], 1)
    except Exception as exc:  # noqa: BLE001
        wait = int((getattr(exc, "headers", None) or {}).get("Retry-After", 0))
        check("tpm=1 时下一次需等约 60 秒", wait == 60, f"retry={wait}")
    else:
        check("tpm=1 时下一次需等约 60 秒", False, "未抛错")

    huge = TpmBucket("principal", "acme:dept-eng:u-huge", 100_000)
    try:
        limiter.reserve([huge], 60_001)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "")
        wait = int((getattr(exc, "headers", None) or {}).get("Retry-After", 0))
        check(
            "单请求成本超过容量直接拒绝",
            code == "RATE_LIMITED" and wait == 60,
            f"{code} retry={wait}",
        )
    else:
        check("单请求成本超过容量直接拒绝", False, "未抛错")

    empty = limiter.reserve([TpmBucket("key", "", 0)], 10_000)
    check("tpm=0 不限制", not empty.buckets and empty.cost == 0)


def test_key_bucket(upstream) -> None:
    key = need_key(label="tpm-key-limited", rateLimitTpm=300, sub="u-tpm-key")
    headers = key_headers(key["secret"])
    upstream.delay_seconds = 0.6
    first: dict = {}
    thread = chat_in_thread(headers, first)
    time.sleep(0.25)
    status, response_headers, body = chat(headers)
    check(
        "Key 桶打满后并发请求 429 并带 Retry-After",
        status == 429
        and error_code(body) == "RATE_LIMITED"
        and retry_after(response_headers) >= 1,
        f"{status} {response_headers} {body}",
    )
    thread.join(timeout=10)
    upstream.delay_seconds = 0.0
    first_status, _, first_body = first.get("response", (0, {}, {}))
    check("并发中的首个请求成功", first_status == 200, f"{first_status} {first_body}")

    status, _, body = chat(headers)
    check(
        "结算退回后桶恢复可用",
        status == 200,
        f"{status} {body}",
    )

    other = need_key(
        label="tpm-key-other",
        rateLimitTpm=300,
        sub="u-tpm-key-2",
    )
    status, _, body = chat(key_headers(other["secret"]))
    check("不同 Key 的桶互相独立", status == 200, f"{status} {body}")

    unlimited = need_key(label="tpm-key-free", rateLimitTpm=0, sub="u-tpm-key-3")
    statuses = []
    for _ in range(2):
        status, _, _ = chat(key_headers(unlimited["secret"]))
        statuses.append(status)
    check("rateLimitTpm=0 不限制", statuses == [200, 200], str(statuses))


def test_subject_bucket(upstream) -> None:
    from app.state import db

    headers = {"Authorization": f"Bearer {make_token(sub='u-tpm-jwt')}"}
    upstream.delay_seconds = 0.6
    first: dict = {}
    thread = chat_in_thread(headers, first)
    time.sleep(0.25)
    status, response_headers, body = chat(headers, max_tokens=300)
    thread.join(timeout=10)
    upstream.delay_seconds = 0.0
    check(
        "主体桶（JWT）按策略 tpm 生效",
        status == 429
        and error_code(body) == "RATE_LIMITED"
        and retry_after(response_headers) >= 1,
        f"{status} {response_headers} {body}",
    )
    first_status, _, first_body = first.get("response", (0, {}, {}))
    check("主体桶首个请求成功", first_status == 200, f"{first_status} {first_body}")

    rows = db.query(
        "SELECT used_tokens FROM quota_counters WHERE sub=?", ("u-tpm-jwt",)
    )
    used = int(rows[0]["used_tokens"]) if rows else 0
    check(
        "被 TPM 拒绝的请求不消耗月度配额",
        used == 20,
        f"used={used} rows={rows}",
    )

    status, _, body = chat(headers, max_tokens=300)
    check("主体桶退回后恢复", status == 200, f"{status} {body}")


def test_shared_bucket() -> None:
    try:
        import redis
    except ImportError:  # pragma: no cover
        print("SKIP 缺少 redis 依赖")
        return
    try:
        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=1, socket_timeout=1
        )
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001
        print(f"SKIP Redis 不可用（{REDIS_URL}）")
        return

    from app.shared_state import SharedState

    prefix = f"m6tpm{uuid.uuid4().hex[:8]}"
    shared = SharedState(REDIS_URL, prefix=prefix, timeout_ms=500)
    first = TpmLimiter(shared=shared)
    second = TpmLimiter(shared=shared)
    bucket = TpmBucket("principal", "acme:dept-eng:u-shared", 1200)
    try:
        shared.ping()
        reservation = first.reserve([bucket], 700)
        retry = shared.tpm_consume(
            [("principal", "acme:dept-eng:u-shared", 1200, 20.0)], 600
        )
        check(
            "Redis 共享桶跨实例可见",
            4.5 <= retry <= 5.5,
            f"retry={retry}",
        )
        try:
            second.reserve([bucket], 600)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "")
            check("另一实例立即 429", code == "RATE_LIMITED", str(code))
        else:
            check("另一实例立即 429", False, "未抛错")

        first.refund(reservation, 20)
        second_reservation = second.reserve([bucket], 600)
        check(
            "共享桶结算退回跨实例生效",
            second_reservation.cost == 600,
        )
        second.refund(second_reservation, 0)
        check(
            "共享桶退回后跨实例可用",
            shared.tpm_consume(
                [("principal", "acme:dept-eng:u-shared", 1200, 20.0)], 1180
            )
            == 0.0,
            "退回后应能取满 1180",
        )
    finally:
        shared.stop()
        cleanup = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        keys = list(cleanup.scan_iter(match=f"{prefix}*", count=200))
        if keys:
            cleanup.delete(*keys)
        cleanup.close()

    dead = TpmLimiter(
        shared=SharedState(
            "redis://127.0.0.1:6399/0", prefix=prefix, timeout_ms=200
        )
    )
    try:
        dead.reserve([TpmBucket("key", "k-1", 1000)], 100)
    except Exception as exc:  # noqa: BLE001
        check(
            "Redis 故障时 TPM 失败关闭",
            getattr(exc, "code", "") == "SHARED_STATE_UNAVAILABLE",
            str(exc),
        )
    else:
        check("Redis 故障时 TPM 失败关闭", False, "未抛错")


def main() -> int:
    configure_policy()
    server = start_gateway()
    upstreams = start_fake_upstreams(("openai",))
    try:
        configure_openai_provider(upstreams["openai"])
        test_validation()
        test_local_bucket()
        test_key_bucket(upstreams["openai"])
        test_subject_bucket(upstreams["openai"])
        test_shared_bucket()
    finally:
        stop_fake_upstreams(upstreams)
        stop_gateway(server)
    return report("M6 TPM 限流自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
