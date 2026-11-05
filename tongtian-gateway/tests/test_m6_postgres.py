"""M6.3：Postgres 后端与跨连接并发语义。

需要 `TT_GATEWAY_TEST_PG_URL`（默认指向本机 55432 的测试库）；不可达时 SKIP。
每个连接代表一个副本，覆盖迁移幂等、审计链串行、outbox 认领、账本去重与配额共享。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get(
    "TT_GATEWAY_TEST_PG_URL",
    "postgresql://postgres:tt-m6-pass@127.0.0.1:55432/tongtian_m6",
)

try:
    import psycopg

    with psycopg.connect(PG_URL, connect_timeout=3):
        pass
except Exception as exc:  # noqa: BLE001 - 没有 Postgres 时跳过本套测试
    print(f"SKIP Postgres 不可达（{type(exc).__name__}），跳过 M6.3 测试")
    raise SystemExit(0)


def _reset_schema() -> None:
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


_reset_schema()

os.environ["TT_GATEWAY_DB_URL"] = PG_URL
os.environ.pop("TT_GATEWAY_REDIS_URL", None)
os.environ.pop("TT_GATEWAY_REDIS_SENTINEL_MASTER", None)

from tests import support  # noqa: E402
from app import state  # noqa: E402
from app import audit  # noqa: E402
from app.api_keys_store import ApiKeyStore  # noqa: E402
from app.audit_outbox import AuditWebhook, enqueue  # noqa: E402
from app.config import settings  # noqa: E402
from app.contract import UsageEvent  # noqa: E402
from app.db import Database  # noqa: E402
from app.quota_sqlite import SqliteQuota  # noqa: E402
from app.usage_store import UsageStore  # noqa: E402

check = support.check
PG_PATH = ROOT / "storage" / "unused-gateway.db"


def _db() -> Database:
    return Database(PG_PATH, url=PG_URL).ensure()


def _usage_event(request_id: str) -> UsageEvent:
    end = int(time.time() * 1000)
    return UsageEvent(
        gatewayId=support.GATEWAY_ID,
        requestId=request_id,
        tenantId="acme",
        departmentId="dept-sales",
        userId="u-1",
        protocol="openai",
        endpoint="/v1/chat/completions",
        modelAlias="fake-chat",
        model="fake-chat",
        provider="openai_compatible",
        promptTokens=10,
        completionTokens=5,
        cost="0",
        currency="USD",
        latencyMs=100,
        status="ok",
        startedAt=end - 100,
        finishedAt=end,
    )


def run_schema_tests() -> None:
    # 版本高于程序时必须拒绝启动
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version(version) VALUES (99)")
    try:
        Database(PG_PATH, url=PG_URL).ensure()
        high_version_rejected = False
    except RuntimeError as exc:
        high_version_rejected = "高于" in str(exc)
    check("Postgres 拒绝更高版本 schema", high_version_rejected)

    _reset_schema()
    db = _db()
    status = db.status()
    check(
        "Postgres 迁移到当前版本",
        status == {"backend": "postgres", "connected": True, "schemaVersion": 7},
        str(status),
    )
    rows = db.query("SELECT version FROM schema_version")
    check("Postgres 迁移只记录一条版本", len(rows) == 1)
    db.close()

    reopened = _db()
    check(
        "Postgres 重启不重复迁移",
        reopened.query("SELECT version FROM schema_version")[0]["version"] == 7,
    )
    reopened.close()

    errors: list[str] = []

    def migrate() -> None:
        try:
            _db().close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=migrate) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    versions = _db().query("SELECT version FROM schema_version")
    check(
        "Postgres 并发启动只应用一次迁移",
        not errors and len(versions) == 1 and int(versions[0]["version"]) == 7,
        "; ".join(errors),
    )


def run_audit_tests() -> None:
    dbs = [_db(), _db()]
    stores = [ApiKeyStore(db) for db in dbs]
    total = 24
    errors: list[str] = []

    def create(index: int) -> None:
        store = stores[index % 2]
        try:
            store.create(
                {
                    "label": f"pg-key-{index}",
                    "tenant": "acme",
                    "dept": "dept-sales",
                    "sub": f"u-{index}",
                    "scopes": ["chat"],
                },
                actor="pg-test",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(create, range(total)))

    verifier = _db()
    with verifier.read() as conn:
        result = audit.verify(conn)
        entries = audit.list_entries(conn, limit=1000)["entries"]
    check("跨连接签发 Key 全部成功", not errors and len(entries) == total, "; ".join(errors))
    check("跨连接审计链无断点", result["brokenAt"] is None, str(result))
    check(
        "跨连接审计 id 连续",
        [entry["id"] for entry in entries] == list(range(1, total + 1)),
    )
    for db in dbs:
        db.close()
    verifier.close()


def run_outbox_tests() -> None:
    dbs = [_db(), _db()]
    total = 12
    with dbs[0].tx() as conn:
        for index in range(1000, 1000 + total):
            enqueue(conn, {"id": index, "action": "test.outbox", "atMs": index})
    hooks = [AuditWebhook(settings, db) for db in dbs]
    barrier = threading.Barrier(2)

    def claim(hook: AuditWebhook) -> set[int]:
        barrier.wait()
        return {int(row["id"]) for row in hook._claim()}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, hooks))
    first, second = results
    check("outbox 两连接认领互不重叠", first.isdisjoint(second), f"{first} / {second}")
    check("outbox 全部被认领", len(first | second) == total, f"{first | second}")
    for db in dbs:
        db.close()


def run_storage_tests() -> None:
    dbs = [_db(), _db()]
    stores = [UsageStore(db) for db in dbs]
    event = _usage_event("pg-dedupe-1")
    check("第一次写入账本成功", stores[0].record(event))
    check("跨连接重复 requestId 被忽略", stores[1].record(event) is False)
    check(
        "跨连接账本只有一条",
        int(dbs[0].query("SELECT COUNT(*) AS n FROM usage_events")[0]["n"]) == 1,
    )

    quota_a = SqliteQuota(dbs[0])
    quota_b = SqliteQuota(dbs[1])
    key = "acme:dept-sales:u-1"
    limits = {"monthlyTokens": 1000, "concurrency": 4}
    reservation = quota_a.reserve(key, limits, 100)
    quota_a.settle(reservation, 40)
    check("跨连接看到同一份已用量", quota_b.used_tokens("acme", "dept-sales", "u-1")["committedTokens"] == 40)
    check("跨连接剩余额度一致", quota_b.remaining(key, limits) == 960)
    for db in dbs:
        db.close()


def run_gateway_tests() -> None:
    upstreams = support.start_fake_upstreams(("openai",))
    server = support.start_gateway()
    try:
        support.create_provider(f"{upstreams['openai'].base_url}/v1")
        status, headers, body = support.http(
            "POST",
            "/v1/chat/completions",
            headers=support.bearer(),
            body={
                "model": "fake-chat",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )
        check(
            "Postgres 网关对话 200",
            status == 200 and body["choices"][0]["message"]["content"] == "你好",
            str(body)[:200],
        )

        status, _, health = support.http(
            "GET", "/gateway/v1/admin/health", headers=support.admin_headers()
        )
        database = health.get("database", {}) if isinstance(health, dict) else {}
        check(
            "健康页披露 Postgres 后端",
            status == 200
            and database.get("backend") == "postgres"
            and database.get("connected") is True
            and database.get("schemaVersion") == 7,
            json.dumps(database, ensure_ascii=False),
        )
        check(
            "健康页不回显 DSN/密码",
            "tt-m6-pass" not in json.dumps(health, ensure_ascii=False)
            and PG_URL not in json.dumps(health, ensure_ascii=False),
        )

        aggregate = state.usage_store.aggregate(tenant="acme", period="monthly")
        check(
            "Postgres 月度用量聚合可用",
            aggregate["totals"]["calls"] >= 1
            and bool(aggregate["rows"])
            and len(aggregate["rows"][0]["day"]) == 10,
            json.dumps(aggregate["totals"], ensure_ascii=False),
        )
        daily = state.usage_store.aggregate(tenant="acme", period="daily")
        check(
            "Postgres 当日用量聚合可用",
            daily["totals"]["calls"] >= 1,
            json.dumps(daily["totals"], ensure_ascii=False),
        )
        counter = state.db.query(
            "SELECT used_tokens FROM quota_counters WHERE tenant=? AND dept=? AND sub=?",
            ("acme", "dept-sales", "u-1"),
        )
        check(
            "Postgres 请求结算写入配额计数",
            bool(counter) and int(counter[0]["used_tokens"]) > 0,
            str(counter),
        )
    finally:
        support.stop_gateway(server)
        support.stop_fake_upstreams(upstreams)


def main() -> int:
    run_schema_tests()
    run_audit_tests()
    run_outbox_tests()
    run_storage_tests()
    run_gateway_tests()
    return support.report("M6 Postgres 后端自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
