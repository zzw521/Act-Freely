//! 存储地基（核心闭环补齐 · 流 1）
//!
//! - SQLite（rusqlite bundled）持久化：model_configs / tasks / settings_kv
//! - keyring 密钥保管（API Key 不落库，DB 仅存引用标记）
//! - 版本化迁移（schema_version）
//!
//! 运行目录：`TONGTIAN_HOME` 环境变量覆盖，缺省 `%APPDATA%\tongtian-office`（双目录设计规范）

pub mod db;
pub mod migrate;
pub mod settings;
pub mod tasks;
pub mod vault;

use std::path::PathBuf;
use std::sync::Mutex;

/// 存储聚合：DB 连接 + 密钥保管箱
pub struct Store {
    conn: Mutex<rusqlite::Connection>,
    vault: vault::Vault,
}

impl Store {
    /// 在运行目录打开（或创建）数据库并执行迁移
    pub fn open_default() -> Result<Self, String> {
        let dir = db::runtime_home();
        std::fs::create_dir_all(&dir)
            .map_err(|e| format!("STORE_MKDIR_FAILED: {e}"))?;
        Self::open(dir.join("tongtian.db"))
    }

    /// 在指定路径打开数据库（测试可注入 temp 路径）
    pub fn open(path: PathBuf) -> Result<Self, String> {
        let conn = db::open(&path)?;
        migrate::run(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
            vault: vault::Vault::new("tongtian-tauri"),
        })
    }

    /// 借用 DB 连接（持锁执行，调用方保持短临界区）
    pub fn with_conn<T>(&self, f: impl FnOnce(&rusqlite::Connection) -> Result<T, String>) -> Result<T, String> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        f(&conn)
    }

    pub fn vault(&self) -> &vault::Vault {
        &self.vault
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("tt-store-{name}-{}", crate::bridge::event_store::now_ms()))
    }

    #[test]
    fn test_open_and_migration_idempotent() {
        let dir = temp_db("open");
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("tongtian.db");
        // 首次打开：迁移执行
        let store = Store::open(db_path.clone()).unwrap();
        let v1: i64 = store
            .with_conn(|c| {
                c.query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        assert!(v1 >= 1);
        drop(store);
        // 二次打开：迁移幂等不报错
        let store2 = Store::open(db_path.clone()).unwrap();
        let tables: Vec<String> = store2
            .with_conn(|c| {
                let mut stmt = c
                    .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                    .map_err(|e| e.to_string())?;
                let rows = stmt
                    .query_map([], |r| r.get::<_, String>(0))
                    .map_err(|e| e.to_string())?;
                rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
            })
            .unwrap();
        for t in ["model_configs", "tasks", "settings_kv", "schema_version"] {
            assert!(tables.iter().any(|n| n == t), "缺表 {t}: {tables:?}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_runtime_home_env_override() {
        // TONGTIAN_HOME 覆盖运行目录（双目录设计规范）
        std::env::set_var("TONGTIAN_HOME", "d:/tt-test-home");
        let home = db::runtime_home();
        std::env::remove_var("TONGTIAN_HOME");
        assert_eq!(home, PathBuf::from("d:/tt-test-home"));
    }
}
