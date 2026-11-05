"""按账本重建 quota_counters。

`usage_events` 是事实来源，`quota_counters` 只是可重建的累计值。
结算口径与 SqliteQuota.settle 一致：不论成功/失败/取消，都按事件里的
total_tokens 累加（失败请求通常是 0，取消请求按已产生用量）。

用法：
    python scripts/rebuild_quota.py [--db PATH] [--db-url URL] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import Database, now_ms  # noqa: E402


def _period(finished_at_ms: int) -> str:
    moment = datetime.fromtimestamp(finished_at_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m")


def rebuild(
    db_path: Path, *, dry_run: bool, db_url: str = ""
) -> list[tuple[str, str, str, str, int]]:
    db = Database(db_path, url=db_url).ensure()
    try:
        grouped: dict[tuple[str, str, str, str], int] = {}
        for row in db.query(
            "SELECT tenant, dept, sub, finished_at_ms, total_tokens FROM usage_events"
        ):
            key = (
                _period(int(row["finished_at_ms"])),
                str(row["tenant"]),
                str(row["dept"]),
                str(row["sub"]),
            )
            grouped[key] = grouped.get(key, 0) + int(row["total_tokens"])
        result = [
            (period, tenant, dept, sub, tokens)
            for (period, tenant, dept, sub), tokens in sorted(grouped.items())
        ]
        if dry_run:
            return result
        with db.tx() as conn:
            conn.execute("DELETE FROM quota_counters")
            conn.executemany(
                "INSERT INTO quota_counters"
                "(period, tenant, dept, sub, used_tokens, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (period, tenant, dept, sub, tokens, now_ms())
                    for period, tenant, dept, sub, tokens in result
                ],
            )
        return result
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="按 usage_events 重建 quota_counters")
    parser.add_argument("--db", type=Path, default=None, help="gateway.db 路径")
    parser.add_argument(
        "--db-url", default="", help="postgresql:// 地址；默认取 TT_GATEWAY_DB_URL"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印结果不写库")
    args = parser.parse_args(argv)

    db_path = args.db
    db_url = args.db_url
    if db_path is None or not db_url:
        from app.config import settings

        db_path = db_path or settings.db_path
        db_url = db_url or settings.db_url
    if not db_url and not Path(db_path).exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2

    rows = rebuild(Path(db_path), dry_run=args.dry_run, db_url=db_url)
    for period, tenant, dept, sub, tokens in rows:
        print(f"{period} {tenant}:{dept}:{sub} {tokens}")
    print(f"{'预演' if args.dry_run else '已重建'} {len(rows)} 个配额计数器")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
