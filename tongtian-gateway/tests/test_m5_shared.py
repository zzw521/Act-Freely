"""M5.3：Redis 共享状态单元测试（令牌桶、预留/释放、策略通知、失败关闭）。

需要可用的 Redis；默认连 `redis://127.0.0.1:6391/0`（G9 启动的临时容器），
也可用 `TT_GATEWAY_REDIS_URL` 覆盖。连不上时整体跳过。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import redis  # noqa: E402

from app.errors import GatewayError  # noqa: E402
from app.shared_state import SharedState, SharedStateError  # noqa: E402
from tests.support import check, report  # noqa: E402

REDIS_URL = os.environ.get("TT_GATEWAY_REDIS_URL", "redis://127.0.0.1:6391/0")
PREFIX = f"m5test{uuid.uuid4().hex[:8]}"


def _redis_available() -> bool:
    try:
        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=1, socket_timeout=1
        )
        client.ping()
        client.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _cleanup() -> None:
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    keys = list(client.scan_iter(match=f"{PREFIX}*", count=200))
    if keys:
        client.delete(*keys)
    client.close()


def test_token_bucket(state: SharedState) -> None:
    state.consume("key", "bucket-a", per_minute=60, burst=2)
    state.consume("key", "bucket-a", per_minute=60, burst=2)
    retry = state.consume("key", "bucket-a", per_minute=60, burst=2)
    check("令牌桶耗尽后返回 Retry-After", 0.5 <= retry <= 1.1, f"retry={retry}")
    time.sleep(1.1)
    check(
        "令牌桶按时间恢复",
        state.consume("key", "bucket-a", per_minute=60, burst=2) == 0.0,
    )
    check("不同 key 独立", state.consume("key", "bucket-b", per_minute=60, burst=2) == 0.0)


def test_reserve_release(state: SharedState) -> None:
    base = dict(
        principal_key="acme:dept-eng:u-9",
        tenant="acme",
        dept="dept-eng",
        sub="u-9",
        period="2026-10",
        used=100,
        monthly=200,
        concurrency=1,
        api_key_id="",
        key_used=0,
        key_monthly=0,
    )
    first = uuid.uuid4().hex
    state.reserve(reservation_id=first, tokens=80, **base)
    check("预留后 reserved=80", state.reserved("acme", "dept-eng", "u-9", "2026-10") == 80)
    check("预留后 active=1", state.active("acme:dept-eng:u-9") == 1)

    try:
        state.reserve(reservation_id=uuid.uuid4().hex, tokens=30, **base)
    except GatewayError as exc:
        check("超出月度 token 返回配额错误", exc.code == "QUOTA_TOKENS_EXCEEDED")
    else:
        check("超出月度 token 返回配额错误", False, "未抛错")

    concurrency = dict(base)
    concurrency["monthly"] = 0
    try:
        state.reserve(reservation_id=uuid.uuid4().hex, tokens=1, **concurrency)
    except GatewayError as exc:
        check("并发超限", exc.code == "QUOTA_CONCURRENCY_EXCEEDED")
    else:
        check("并发超限", False, "未抛错")

    state.release(
        reservation_id=first,
        principal_key="acme:dept-eng:u-9",
        tenant="acme",
        dept="dept-eng",
        sub="u-9",
        period="2026-10",
        tokens=80,
    )
    check("释放后 reserved=0", state.reserved("acme", "dept-eng", "u-9", "2026-10") == 0)
    check("释放后 active=0", state.active("acme:dept-eng:u-9") == 0)

    state.reserve(reservation_id=uuid.uuid4().hex, tokens=30, **base)
    check("释放后可再次预留", state.reserved("acme", "dept-eng", "u-9", "2026-10") == 30)


def test_key_quota(state: SharedState) -> None:
    kwargs = dict(
        principal_key="acme:dept-sales:u-2",
        tenant="acme",
        dept="dept-sales",
        sub="u-2",
        period="2026-10",
        tokens=60,
        used=0,
        monthly=1000,
        concurrency=4,
        api_key_id="key-1",
        key_used=100,
        key_monthly=150,
    )
    try:
        state.reserve(reservation_id=uuid.uuid4().hex, **kwargs)
    except GatewayError as exc:
        check("Key 月度配额不足", exc.code == "QUOTA_TOKENS_EXCEEDED")
    else:
        check("Key 月度配额不足", False, "未抛错")

    kwargs["key_monthly"] = 0
    state.reserve(reservation_id=uuid.uuid4().hex, **kwargs)
    check("Key 不限额度时放行", state.reserved("acme", "dept-sales", "u-2", "2026-10") == 60)


def test_policy_events(state: SharedState) -> None:
    received: list[str] = []
    event = threading.Event()

    def callback(tenant: str) -> None:
        received.append(tenant)
        event.set()

    state.start_policy_listener(callback)
    check("策略订阅已启动", state.status()["policySubscriber"] in ("starting", "listening"))
    check("发布策略失效", state.publish_policy("acme"))
    check("收到策略失效事件", event.wait(5) and "acme" in received, str(received))
    state.stop()


def test_fail_closed() -> None:
    dead = SharedState(
        "redis://127.0.0.1:6399/0", prefix=PREFIX, timeout_ms=200, workers=2
    )
    try:
        dead.ping()
    except SharedStateError:
        check("Redis 不可达时 ping 失败", True)
    else:
        check("Redis 不可达时 ping 失败", False, "未抛错")
    try:
        dead.consume("admin", "x", per_minute=60, burst=5)
    except SharedStateError:
        check("Redis 不可达时限流失败关闭", True)
    else:
        check("Redis 不可达时限流失败关闭", False, "未抛错")
    check("失败后 status.connected=false", dead.status()["connected"] is False)


def main() -> int:
    if not _redis_available():
        print(f"SKIP Redis 不可用（{REDIS_URL}），跳过 M5 共享状态单测")
        return 0
    _cleanup()
    state = SharedState(
        REDIS_URL, prefix=PREFIX, timeout_ms=500, workers=2, client_factory=None
    )
    try:
        state.ping()
        check("Redis PING 成功", state.status()["mode"] == "redis")
        test_token_bucket(state)
        test_reserve_release(state)
        test_key_quota(state)
        test_policy_events(state)
        test_fail_closed()
    finally:
        state.stop()
        _cleanup()
    return report("M5 共享状态自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
