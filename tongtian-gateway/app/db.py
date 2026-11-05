"""网关存储：默认 SQLite，M6 起可用 Postgres 跨主机共享。

SQLite 使用 WAL + `BEGIN IMMEDIATE` 串行化写；Postgres 使用显式 `BEGIN`、事务级
advisory lock（审计链/迁移）与 `SKIP LOCKED`（outbox）。两种后端共用同一套表名与
计数器语义，调用方只看到统一的 `?` 占位符与按名取值的结果行。
`usage_events` 是事实来源，`quota_counters` 可由事件重建（scripts/rebuild_quota.py）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # Postgres 只在配置 TT_GATEWAY_DB_URL 时使用
    import psycopg
    from psycopg import errors as pg_errors
except ImportError:  # pragma: no cover - SQLite-only 部署可不装 psycopg
    psycopg = None  # type: ignore[assignment]
    pg_errors = None  # type: ignore[assignment]

SCHEMA_VERSION = 7

# advisory lock 的命名空间；迁移与审计链各用一把，互不阻塞。
SCHEMA_LOCK_KEY = 0x5454474153434845  # "TTGASCHE"
AUDIT_LOCK_KEY = 0x5454474155444954  # "TTGAUDIT"

_POSTGRES_PREFIXES = ("postgresql://", "postgres://")


def is_postgres_url(url: str) -> bool:
    return url.strip().startswith(_POSTGRES_PREFIXES)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


_MIGRATION_1 = (
    """
    CREATE TABLE providers (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL,
      label TEXT NOT NULL DEFAULT '',
      base_url TEXT NOT NULL,
      api_version TEXT NOT NULL DEFAULT '',
      timeout_seconds INTEGER NOT NULL DEFAULT 120,
      allow_private INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      created_at_ms INTEGER NOT NULL,
      updated_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE provider_keys (
      id TEXT PRIMARY KEY,
      provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
      label TEXT NOT NULL DEFAULT '',
      ciphertext BLOB NOT NULL,
      nonce BLOB NOT NULL,
      key_version INTEGER NOT NULL,
      last4 TEXT NOT NULL,
      weight INTEGER NOT NULL DEFAULT 100,
      status TEXT NOT NULL DEFAULT 'active',
      created_at_ms INTEGER NOT NULL,
      rotated_at_ms INTEGER NOT NULL DEFAULT 0,
      disabled_at_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE usage_events (
      gateway_id TEXT NOT NULL,
      request_id TEXT NOT NULL,
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      project_id TEXT NOT NULL DEFAULT '',
      api_key_id TEXT NOT NULL DEFAULT '',
      protocol TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      model_alias TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL,
      provider TEXT NOT NULL,
      prompt_tokens INTEGER NOT NULL,
      completion_tokens INTEGER NOT NULL,
      cached_tokens INTEGER NOT NULL DEFAULT 0,
      reasoning_tokens INTEGER NOT NULL DEFAULT 0,
      total_tokens INTEGER NOT NULL,
      cost_micros INTEGER NOT NULL DEFAULT 0,
      currency TEXT NOT NULL DEFAULT '',
      price_version TEXT NOT NULL DEFAULT '',
      latency_ms INTEGER NOT NULL,
      status TEXT NOT NULL,
      error_code TEXT NOT NULL DEFAULT '',
      stream INTEGER NOT NULL,
      estimated INTEGER NOT NULL DEFAULT 0,
      started_at_ms INTEGER NOT NULL,
      finished_at_ms INTEGER NOT NULL,
      uploaded_at_ms INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (gateway_id, request_id)
    )
    """,
    "CREATE INDEX idx_usage_pending ON usage_events(uploaded_at_ms, finished_at_ms)",
    "CREATE INDEX idx_usage_tenant ON usage_events(tenant, dept, started_at_ms)",
    """
    CREATE TABLE quota_counters (
      period TEXT NOT NULL,
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      used_tokens INTEGER NOT NULL DEFAULT 0,
      updated_at_ms INTEGER NOT NULL,
      PRIMARY KEY (period, tenant, dept, sub)
    )
    """,
    """
    CREATE TABLE policy_cache (
      tenant TEXT PRIMARY KEY,
      document_json TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      etag TEXT NOT NULL DEFAULT '',
      fetched_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE api_keys (
      key_id TEXT PRIMARY KEY,
      secret_hash TEXT NOT NULL,
      label TEXT NOT NULL DEFAULT '',
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      scopes_json TEXT NOT NULL,
      model_allowlist_json TEXT NOT NULL DEFAULT '[]',
      expires_at_ms INTEGER NOT NULL DEFAULT 0,
      revoked_at_ms INTEGER NOT NULL DEFAULT 0,
      created_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at_ms INTEGER NOT NULL,
      actor TEXT NOT NULL,
      action TEXT NOT NULL,
      object TEXT NOT NULL,
      before_json TEXT NOT NULL DEFAULT '{}',
      after_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_provider_keys ON provider_keys(provider_id, status)",
)

# M1.4：按月配额覆盖（管理员临时调整当月额度，不动策略文档）。
# sub='' 表示部门级覆盖，成员没有个人覆盖时生效；period 为 UTC 自然月。
_MIGRATION_2 = (
    """
    CREATE TABLE quota_overrides (
      period         TEXT NOT NULL,
      tenant         TEXT NOT NULL,
      dept           TEXT NOT NULL DEFAULT '',
      sub            TEXT NOT NULL DEFAULT '',
      monthly_tokens INTEGER NOT NULL,
      updated_at_ms  INTEGER NOT NULL,
      updated_by     TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (period, tenant, dept, sub)
    )
    """,
    "CREATE INDEX idx_quota_overrides_tenant ON quota_overrides(period, tenant, dept)",
)

# M4：审计哈希链。存量行在迁移内按 id 顺序回填，旧条目 actor_role 记 admin。
_MIGRATION_3 = (
    "ALTER TABLE audit_log ADD COLUMN actor_role TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_log ADD COLUMN entry_hash TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX idx_audit_entry_hash ON audit_log(entry_hash)",
)

# M4.4：审计外送 outbox。审计写入与推送同一事务落库，后台线程失败重试。
_MIGRATION_4 = (
    """
    CREATE TABLE audit_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      audit_id INTEGER NOT NULL UNIQUE,
      payload_json TEXT NOT NULL,
      created_at_ms INTEGER NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at_ms INTEGER NOT NULL DEFAULT 0,
      delivered_at_ms INTEGER NOT NULL DEFAULT 0,
      last_status INTEGER NOT NULL DEFAULT 0,
      last_error TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX idx_audit_outbox_due "
    "ON audit_outbox(delivered_at_ms, next_attempt_at_ms)",
)

# M4.5：per-key 限流与月度 token 配额。计数器从 usage_events 回填，避免升级后当月用量归零。
_MIGRATION_5 = (
    "ALTER TABLE api_keys ADD COLUMN rate_limit_rpm INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN quota_monthly_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN rotated_at_ms INTEGER NOT NULL DEFAULT 0",
    """
    CREATE TABLE api_key_counters (
      period TEXT NOT NULL,
      key_id TEXT NOT NULL,
      used_tokens INTEGER NOT NULL DEFAULT 0,
      updated_at_ms INTEGER NOT NULL,
      PRIMARY KEY (period, key_id)
    )
    """,
    """
    INSERT INTO api_key_counters(period, key_id, used_tokens, updated_at_ms)
    SELECT strftime('%Y-%m', finished_at_ms / 1000, 'unixepoch') AS period,
           api_key_id,
           SUM(total_tokens),
           CAST(strftime('%s', 'now') AS INTEGER) * 1000
    FROM usage_events
    WHERE api_key_id <> ''
    GROUP BY period, api_key_id
    """,
)

# M5：审计外送租约。抢到租约的 worker 独占该批，崩溃后租约过期由其他 worker 接手。
_MIGRATION_6 = (
    "ALTER TABLE audit_outbox ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_outbox ADD COLUMN leased_until_ms INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX idx_audit_outbox_lease "
    "ON audit_outbox(delivered_at_ms, leased_until_ms)",
)

# M6：per-key TPM。
_MIGRATION_7 = (
    "ALTER TABLE api_keys ADD COLUMN rate_limit_tpm INTEGER NOT NULL DEFAULT 0",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
}

# Postgres 当前 schema（含全部增量列）。所有语句幂等，启动时在 advisory lock 内重放。
_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS providers (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL,
      label TEXT NOT NULL DEFAULT '',
      base_url TEXT NOT NULL,
      api_version TEXT NOT NULL DEFAULT '',
      timeout_seconds INTEGER NOT NULL DEFAULT 120,
      allow_private INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      created_at_ms BIGINT NOT NULL,
      updated_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_keys (
      id TEXT PRIMARY KEY,
      provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
      label TEXT NOT NULL DEFAULT '',
      ciphertext BYTEA NOT NULL,
      nonce BYTEA NOT NULL,
      key_version INTEGER NOT NULL,
      last4 TEXT NOT NULL,
      weight INTEGER NOT NULL DEFAULT 100,
      status TEXT NOT NULL DEFAULT 'active',
      created_at_ms BIGINT NOT NULL,
      rotated_at_ms BIGINT NOT NULL DEFAULT 0,
      disabled_at_ms BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_events (
      gateway_id TEXT NOT NULL,
      request_id TEXT NOT NULL,
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      project_id TEXT NOT NULL DEFAULT '',
      api_key_id TEXT NOT NULL DEFAULT '',
      protocol TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      model_alias TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL,
      provider TEXT NOT NULL,
      prompt_tokens INTEGER NOT NULL,
      completion_tokens INTEGER NOT NULL,
      cached_tokens INTEGER NOT NULL DEFAULT 0,
      reasoning_tokens INTEGER NOT NULL DEFAULT 0,
      total_tokens INTEGER NOT NULL,
      cost_micros BIGINT NOT NULL DEFAULT 0,
      currency TEXT NOT NULL DEFAULT '',
      price_version TEXT NOT NULL DEFAULT '',
      latency_ms INTEGER NOT NULL,
      status TEXT NOT NULL,
      error_code TEXT NOT NULL DEFAULT '',
      stream INTEGER NOT NULL,
      estimated INTEGER NOT NULL DEFAULT 0,
      started_at_ms BIGINT NOT NULL,
      finished_at_ms BIGINT NOT NULL,
      uploaded_at_ms BIGINT NOT NULL DEFAULT 0,
      PRIMARY KEY (gateway_id, request_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_pending "
    "ON usage_events(uploaded_at_ms, finished_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_usage_tenant "
    "ON usage_events(tenant, dept, started_at_ms)",
    """
    CREATE TABLE IF NOT EXISTS quota_counters (
      period TEXT NOT NULL,
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      used_tokens BIGINT NOT NULL DEFAULT 0,
      updated_at_ms BIGINT NOT NULL,
      PRIMARY KEY (period, tenant, dept, sub)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policy_cache (
      tenant TEXT PRIMARY KEY,
      document_json TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      etag TEXT NOT NULL DEFAULT '',
      fetched_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
      key_id TEXT PRIMARY KEY,
      secret_hash TEXT NOT NULL,
      label TEXT NOT NULL DEFAULT '',
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL,
      sub TEXT NOT NULL,
      scopes_json TEXT NOT NULL,
      model_allowlist_json TEXT NOT NULL DEFAULT '[]',
      expires_at_ms BIGINT NOT NULL DEFAULT 0,
      revoked_at_ms BIGINT NOT NULL DEFAULT 0,
      created_at_ms BIGINT NOT NULL,
      rate_limit_rpm INTEGER NOT NULL DEFAULT 0,
      quota_monthly_tokens BIGINT NOT NULL DEFAULT 0,
      rotated_at_ms BIGINT NOT NULL DEFAULT 0,
      rate_limit_tpm BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id BIGSERIAL PRIMARY KEY,
      at_ms BIGINT NOT NULL,
      actor TEXT NOT NULL,
      action TEXT NOT NULL,
      object TEXT NOT NULL,
      before_json TEXT NOT NULL DEFAULT '{}',
      after_json TEXT NOT NULL DEFAULT '{}',
      actor_role TEXT NOT NULL DEFAULT '',
      prev_hash TEXT NOT NULL DEFAULT '',
      entry_hash TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_provider_keys ON provider_keys(provider_id, status)",
    """
    CREATE TABLE IF NOT EXISTS quota_overrides (
      period TEXT NOT NULL,
      tenant TEXT NOT NULL,
      dept TEXT NOT NULL DEFAULT '',
      sub TEXT NOT NULL DEFAULT '',
      monthly_tokens BIGINT NOT NULL,
      updated_at_ms BIGINT NOT NULL,
      updated_by TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (period, tenant, dept, sub)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quota_overrides_tenant "
    "ON quota_overrides(period, tenant, dept)",
    "CREATE INDEX IF NOT EXISTS idx_audit_entry_hash ON audit_log(entry_hash)",
    """
    CREATE TABLE IF NOT EXISTS audit_outbox (
      id BIGSERIAL PRIMARY KEY,
      audit_id BIGINT NOT NULL UNIQUE,
      payload_json TEXT NOT NULL,
      created_at_ms BIGINT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at_ms BIGINT NOT NULL DEFAULT 0,
      delivered_at_ms BIGINT NOT NULL DEFAULT 0,
      last_status INTEGER NOT NULL DEFAULT 0,
      last_error TEXT NOT NULL DEFAULT '',
      lease_owner TEXT NOT NULL DEFAULT '',
      leased_until_ms BIGINT NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_outbox_due "
    "ON audit_outbox(delivered_at_ms, next_attempt_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_audit_outbox_lease "
    "ON audit_outbox(delivered_at_ms, leased_until_ms)",
    """
    CREATE TABLE IF NOT EXISTS api_key_counters (
      period TEXT NOT NULL,
      key_id TEXT NOT NULL,
      used_tokens BIGINT NOT NULL DEFAULT 0,
      updated_at_ms BIGINT NOT NULL,
      PRIMARY KEY (period, key_id)
    )
    """,
)


class Row:
    """按名/按位取值、按值迭代的结果行；兼容 sqlite3.Row 的既有用法。"""

    __slots__ = ("_names", "_positions", "_values")

    def __init__(self, names: tuple[str, ...], values: tuple) -> None:
        self._names = names
        self._positions = {name: index for index, name in enumerate(names)}
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._positions[key]]
        except KeyError:
            raise IndexError(f"结果行没有列 {key!r}") from None

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key) -> bool:
        return key in self._positions

    def __eq__(self, other) -> bool:
        if isinstance(other, Row):
            return self._values == other._values
        if isinstance(other, (tuple, list)):
            return self._values == tuple(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(self.items())!r})"

    def keys(self) -> list[str]:
        return list(self._names)

    def values(self) -> list:
        return list(self._values)

    def items(self) -> list[tuple[str, object]]:
        return list(zip(self._names, self._values))

    def get(self, key: str, default=None):
        index = self._positions.get(key)
        return default if index is None else self._values[index]


class Cursor:
    """游标包装：两种后端都返回统一的 `Row`。"""

    __slots__ = ("_cursor", "_names")

    def __init__(self, cursor) -> None:
        self._cursor = cursor
        description = getattr(cursor, "description", None) or ()
        self._names = tuple(item[0] for item in description)

    def _row(self, raw) -> Row | None:
        if raw is None:
            return None
        return Row(self._names, tuple(raw))

    def fetchone(self) -> Row | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Row]:
        return [self._row(raw) for raw in self._cursor.fetchall()]

    def __iter__(self):
        return (self._row(raw) for raw in self._cursor)

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @property
    def description(self):
        return self._cursor.description


class Connection:
    """DB-API 适配：SQLite 原样执行，Postgres 把 `?` 换成 `%s`。"""

    __slots__ = ("_raw", "dialect")

    def __init__(self, raw, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        if self.dialect == "postgres":
            has_params = params is not None and len(params) > 0
            raw = self._raw.execute(
                _postgres_sql(sql, escape_percent=has_params),
                params if has_params else None,
            )
        else:
            raw = self._raw.execute(sql, params)
        return Cursor(raw)

    def executemany(self, sql: str, params_seq) -> Cursor:
        if self.dialect == "postgres":
            # psycopg3 只有 cursor.executemany，连接对象上没有这个方法
            cursor = self._raw.cursor()
            cursor.executemany(_postgres_sql(sql), params_seq)
            return Cursor(cursor)
        return Cursor(self._raw.executemany(sql, params_seq))

    def close(self) -> None:
        self._raw.close()


def _postgres_sql(sql: str, *, escape_percent: bool = True) -> str:
    """把 `?` 占位符换成 `%s`；字符串字面量里的 `?` 不替换。

    只有在带参数执行时才需要把字面量 `%` 写成 `%%`，无参数时 psycopg 不做插值。
    """
    out: list[str] = []
    quote = ""
    index = 0
    size = len(sql)
    while index < size:
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < size and sql[index + 1] == quote:
                    out.append(char * 2)
                    index += 2
                    continue
                quote = ""
                out.append(char)
            elif char == "%" and escape_percent:
                out.append("%%")
            else:
                out.append(char)
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%" and escape_percent:
            out.append("%%")
        else:
            out.append(char)
        index += 1
    return "".join(out)


class Database:
    def __init__(self, path: Path | str, *, url: str = "") -> None:
        self.path = Path(path)
        self._url = url.strip()
        if self._url and not is_postgres_url(self._url):
            raise RuntimeError("TT_GATEWAY_DB_URL 必须是 postgresql:// 或 postgres:// 地址")
        self._dialect = "postgres" if self._url else "sqlite"
        self._lock = threading.RLock()
        self._conn: Connection | None = None
        self._depth = 0

    @property
    def dialect(self) -> str:
        return self._dialect

    def connect(self) -> Connection:
        with self._lock:
            if self._conn is None:
                if self._dialect == "postgres":
                    conn = self._connect_postgres()
                else:
                    conn = self._connect_sqlite()
                self._conn = conn
            return self._conn

    def _connect_sqlite(self) -> Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        raw.row_factory = sqlite3.Row
        _enable_wal(raw, timeout=10)
        raw.execute("PRAGMA synchronous=NORMAL")
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA busy_timeout=5000")
        try:
            _migrate(raw)
        except BaseException:
            raw.close()
            raise
        return Connection(raw, "sqlite")

    def _connect_postgres(self) -> Connection:
        if psycopg is None:
            raise RuntimeError(
                "配置了 TT_GATEWAY_DB_URL，但缺少 psycopg；请安装 psycopg[binary]"
            )
        raw = psycopg.connect(self._url, autocommit=True, connect_timeout=10)
        conn = Connection(raw, "postgres")
        try:
            _migrate_postgres(conn)
        except BaseException:
            raw.close()
            raise
        return conn

    def ensure(self) -> "Database":
        self.connect()
        return self

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """写事务：SQLite 用 BEGIN IMMEDIATE，Postgres 用 BEGIN；可嵌套 SAVEPOINT。"""
        with self._lock:
            conn = self.connect()
            depth = self._depth
            if depth == 0:
                conn._raw.execute(
                    "BEGIN IMMEDIATE" if self._dialect == "sqlite" else "BEGIN"
                )
            else:
                conn._raw.execute(f"SAVEPOINT sp{depth}")
            self._depth = depth + 1
            try:
                yield conn
            except BaseException:
                self._depth = depth
                if depth == 0:
                    conn._raw.execute("ROLLBACK")
                else:
                    conn._raw.execute(f"ROLLBACK TO SAVEPOINT sp{depth}")
                    conn._raw.execute(f"RELEASE SAVEPOINT sp{depth}")
                raise
            else:
                self._depth = depth
                if depth == 0:
                    conn._raw.execute("COMMIT")
                else:
                    conn._raw.execute(f"RELEASE SAVEPOINT sp{depth}")

    @contextmanager
    def read(self) -> Iterator[Connection]:
        """只读连接：与写事务共用同一把锁，避免读到迁移中的半成品。"""
        with self._lock:
            yield self.connect()

    def query(self, sql: str, params: tuple = ()) -> list[Row]:
        with self._lock:
            return list(self.connect().execute(sql, params).fetchall())

    def is_integrity_error(self, exc: BaseException) -> bool:
        if isinstance(exc, sqlite3.IntegrityError):
            return True
        return pg_errors is not None and isinstance(exc, pg_errors.IntegrityError)

    def is_db_error(self, exc: BaseException) -> bool:
        if isinstance(exc, sqlite3.Error):
            return True
        return pg_errors is not None and isinstance(exc, pg_errors.Error)

    def day_expression(self, column: str = "finished_at_ms") -> str:
        if self._dialect == "postgres":
            return (
                f"to_char(to_timestamp({column} / 1000.0) AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD')"
            )
        return f"strftime('%Y-%m-%d', {column}/1000, 'unixepoch')"

    def status(self) -> dict:
        """健康检查：只暴露后端、连通性与 schema 版本，不回显 DSN。"""
        result: dict = {
            "backend": self._dialect,
            "connected": False,
            "schemaVersion": None,
        }
        try:
            with self._lock:
                conn = self.connect()
                conn._raw.execute("SELECT 1")
                row = conn._raw.execute("SELECT version FROM schema_version").fetchone()
            result["connected"] = True
            result["schemaVersion"] = int(row[0]) if row else None
        except Exception:  # noqa: BLE001 - 健康检查失败只反映在字段里
            pass
        return result

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def lock_audit_chain(conn: Connection) -> None:
    """跨进程串行化审计链追加；SQLite 由 BEGIN IMMEDIATE 天然保证。"""
    if conn.dialect == "postgres":
        conn.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LOCK_KEY,)).fetchone()


def _enable_wal(conn: sqlite3.Connection, *, timeout: float) -> None:
    """多 worker 同时启动时，切换日志模式可能直接返回 busy（不走 busy_timeout）。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    while True:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = int(row["version"]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 {current} 高于程序支持的 {SCHEMA_VERSION}"
            )
        if current >= SCHEMA_VERSION:
            return
        version = current + 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 拿到写锁后重新读取：并发启动的其他 worker 可能已经应用了该迁移。
            locked_row = conn.execute("SELECT version FROM schema_version").fetchone()
            locked = int(locked_row["version"]) if locked_row else 0
            if locked > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {locked} 高于程序支持的 {SCHEMA_VERSION}"
                )
            if locked >= version:
                conn.execute("COMMIT")
                continue
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            if version == 3:
                from .audit_chain import backfill

                backfill(conn)
            if locked_row is None:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            else:
                conn.execute("UPDATE schema_version SET version = ?", (version,))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


# 对已有 Postgres 库补列；新库由 `_POSTGRES_SCHEMA` 一次建全，这些语句是空操作。
_POSTGRES_UPGRADES = (
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS actor_role TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS entry_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_outbox ADD COLUMN IF NOT EXISTS lease_owner TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_outbox ADD COLUMN IF NOT EXISTS leased_until_ms BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rate_limit_rpm INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS quota_monthly_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rotated_at_ms BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rate_limit_tpm BIGINT NOT NULL DEFAULT 0",
)


def _migrate_postgres(conn: Connection) -> None:
    """在事务级 advisory lock 内建表/补列，多主机同时启动只应用一次。"""
    raw = conn._raw
    raw.execute("BEGIN")
    try:
        # 先拿锁再建 schema_version：并发 CREATE TABLE IF NOT EXISTS 本身会撞
        # pg_type_typname_nsp_index（唯一索引），必须在锁内串行化
        raw.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
        raw.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = raw.execute("SELECT version FROM schema_version").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 {current} 高于程序支持的 {SCHEMA_VERSION}"
            )
        if current < SCHEMA_VERSION:
            for statement in _POSTGRES_SCHEMA:
                raw.execute(statement)
            for statement in _POSTGRES_UPGRADES:
                raw.execute(statement)
            if row is None:
                raw.execute(
                    "INSERT INTO schema_version(version) VALUES (%s)",
                    (SCHEMA_VERSION,),
                )
            else:
                raw.execute(
                    "UPDATE schema_version SET version = %s", (SCHEMA_VERSION,)
                )
    except BaseException:
        try:
            raw.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - 连接已断时不再掩盖原始异常
            pass
        raise
    else:
        raw.execute("COMMIT")
