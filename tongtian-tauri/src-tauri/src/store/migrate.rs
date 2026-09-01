//! 版本化迁移（schema_version 表驱动，幂等）

/// 全部迁移脚本（索引即版本号的声明顺序）
const MIGRATIONS: &[&str] = &[
    // ── V1：核心闭环首批表（model_configs / tasks / settings_kv） ──
    r#"
    CREATE TABLE IF NOT EXISTS model_configs (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        source         TEXT NOT NULL,           -- openai_compat / ollama / preset_* / custom
        base_url       TEXT NOT NULL DEFAULT '',
        model_name     TEXT NOT NULL,
        context_window INTEGER NOT NULL DEFAULT 4096,
        is_default     INTEGER NOT NULL DEFAULT 0,
        agent_binding  TEXT NOT NULL DEFAULT '',
        force_local    INTEGER NOT NULL DEFAULT 0,
        has_api_key    INTEGER NOT NULL DEFAULT 0, -- 密钥本体存 keyring，此处仅引用标记
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks (
        task_id            TEXT PRIMARY KEY,
        workspace_id       TEXT NOT NULL DEFAULT 'default',
        title              TEXT NOT NULL DEFAULT '',
        status             TEXT NOT NULL,
        wait_reason        TEXT,
        retry_count        INTEGER NOT NULL DEFAULT 0,
        status_reason_code TEXT,
        queue_position     INTEGER NOT NULL DEFAULT 0,
        created_at_ms      INTEGER NOT NULL,
        updated_at_ms      INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks (updated_at_ms DESC);

    CREATE TABLE IF NOT EXISTS settings_kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    "#,
];

/// 执行所有尚未应用的迁移
pub fn run(conn: &rusqlite::Connection) -> Result<(), String> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );",
    )
    .map_err(|e| format!("STORE_MIGRATE_FAILED: {e}"))?;

    let current: i64 = conn
        .query_row("SELECT COALESCE(MAX(version), 0) FROM schema_version", [], |r| {
            r.get(0)
        })
        .map_err(|e| format!("STORE_MIGRATE_FAILED: {e}"))?;

    for (i, sql) in MIGRATIONS.iter().enumerate() {
        let version = (i + 1) as i64;
        if version <= current {
            continue; // 已应用，幂等跳过
        }
        conn.execute_batch(sql)
            .map_err(|e| format!("STORE_MIGRATE_V{version}_FAILED: {e}"))?;
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?1, datetime('now'))",
            [version],
        )
        .map_err(|e| format!("STORE_MIGRATE_RECORD_FAILED: {e}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mem_conn() -> rusqlite::Connection {
        rusqlite::Connection::open_in_memory().unwrap()
    }

    #[test]
    fn test_run_creates_tables() {
        let conn = mem_conn();
        run(&conn).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('model_configs','tasks','settings_kv','schema_version')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 4);
    }

    #[test]
    fn test_run_idempotent() {
        let conn = mem_conn();
        run(&conn).unwrap();
        run(&conn).unwrap(); // 二次执行不报错、不重复插入版本
        let versions: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(versions, MIGRATIONS.len() as i64);
    }

    #[test]
    fn test_incremental_migration_skips_applied() {
        let conn = mem_conn();
        run(&conn).unwrap();
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }
}
