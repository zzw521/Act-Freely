"""M5.2：同机多进程同时打开同一 SQLite 时的迁移与版本安全。"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import check, report  # noqa: E402
from app.db import MIGRATIONS, SCHEMA_VERSION, Database  # noqa: E402


def _seed_previous_version(path: str) -> None:
    """把库做到 SCHEMA_VERSION-1，模拟升级时有多个 worker 同时启动。"""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        for version in range(1, SCHEMA_VERSION):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION - 1,)
        )
    finally:
        conn.close()


def _open_worker(path: str, barrier, queue) -> None:
    from app.db import Database as WorkerDatabase

    barrier.wait()
    try:
        db = WorkerDatabase(path).ensure()
        row = db.query("SELECT version FROM schema_version")[0]
        tables = {
            name
            for name, in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        db.close()
        queue.put(("ok", int(row["version"]), "api_key_counters" in tables))
    except BaseException as exc:  # noqa: BLE001 - 子进程必须回传失败原因
        queue.put(("error", f"{type(exc).__name__}: {exc}", False))


def test_fresh_and_reopen(tmp: Path) -> None:
    path = tmp / "fresh.db"
    db = Database(path).ensure()
    row = db.query("SELECT version FROM schema_version")[0]
    check("全新库迁移到当前版本", int(row["version"]) == SCHEMA_VERSION)
    tables = {name for name, in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    check(
        "M4/M5 表存在",
        {"api_keys", "api_key_counters", "audit_outbox", "quota_counters"} <= tables,
    )
    db.close()

    again = Database(path).ensure()
    rows = again.query("SELECT version FROM schema_version")
    check("重复打开不重复迁移", len(rows) == 1 and int(rows[0]["version"]) == SCHEMA_VERSION)
    again.close()


def test_concurrent_upgrade(tmp: Path) -> None:
    path = str(tmp / "concurrent.db")
    _seed_previous_version(path)

    ctx = mp.get_context("spawn")
    workers = 4
    barrier = ctx.Barrier(workers)
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_open_worker, args=(path, barrier, queue), daemon=True)
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in range(workers)]
    for process in processes:
        process.join(timeout=60)

    errors = [item[1] for item in results if item[0] != "ok"]
    check("并发迁移全部成功", not errors, "; ".join(errors))
    check(
        "并发迁移后版本一致",
        all(item[0] == "ok" and item[1] == SCHEMA_VERSION for item in results),
        str(results),
    )
    check("并发迁移都看到新表", all(item[2] for item in results), str(results))

    db = Database(path).ensure()
    rows = db.query("SELECT version FROM schema_version")
    check("迁移只记录一条版本", len(rows) == 1, str([dict(r) for r in rows]))
    check("最终版本正确", int(rows[0]["version"]) == SCHEMA_VERSION)
    db.close()


def test_newer_schema_rejected(tmp: Path) -> None:
    path = tmp / "newer.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    finally:
        conn.close()
    try:
        Database(path).connect()
    except RuntimeError as exc:
        check("拒绝更高版本 schema", "高于" in str(exc), str(exc))
    else:
        check("拒绝更高版本 schema", False, "未抛错")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tt-m5-schema-"))
    test_fresh_and_reopen(tmp)
    test_concurrent_upgrade(tmp)
    test_newer_schema_rejected(tmp)
    return report("M5 多进程迁移自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
