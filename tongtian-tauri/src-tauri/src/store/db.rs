//! DB 连接管理与运行目录解析

use std::path::PathBuf;

/// 运行目录（数据根）：`TONGTIAN_HOME` 环境变量优先，
/// 缺省回退 bridge::python_env::base_dir()（%APPDATA%\tongtian-office 或 ~/.tongtian）
pub fn runtime_home() -> PathBuf {
    if let Ok(home) = std::env::var("TONGTIAN_HOME") {
        if !home.trim().is_empty() {
            return PathBuf::from(home);
        }
    }
    crate::bridge::python_env::base_dir()
}

/// 打开 SQLite 连接（WAL 模式，外键开启）
pub fn open(path: &std::path::Path) -> Result<rusqlite::Connection, String> {
    let conn = rusqlite::Connection::open(path)
        .map_err(|e| format!("STORE_OPEN_FAILED: {e}"))?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| format!("STORE_PRAGMA_FAILED: {e}"))?;
    conn.pragma_update(None, "foreign_keys", "ON")
        .map_err(|e| format!("STORE_PRAGMA_FAILED: {e}"))?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_open_in_memory() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "journal_mode", "WAL").ok();
        assert!(conn
            .execute_batch("CREATE TABLE t(a TEXT)")
            .is_ok());
    }

    #[test]
    fn test_open_file_db() {
        let dir = std::env::temp_dir().join(format!("tt-db-open-{}", crate::bridge::event_store::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let conn = open(&dir.join("t.db"));
        assert!(conn.is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
