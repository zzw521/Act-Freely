//! settings_kv 读写（settings_get / settings_set_many 的数据层）

use crate::store::Store;

/// 读取单个设置项
pub fn get(store: &Store, key: &str) -> Result<Option<String>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare("SELECT value FROM settings_kv WHERE key = ?1")
            .map_err(|e| e.to_string())?;
        let mut rows = stmt.query([key]).map_err(|e| e.to_string())?;
        match rows.next().map_err(|e| e.to_string())? {
            Some(row) => Ok(Some(row.get::<_, String>(0).map_err(|e| e.to_string())?)),
            None => Ok(None),
        }
    })
}

/// 写入（覆盖）单个设置项
pub fn set(store: &Store, key: &str, value: &str) -> Result<(), String> {
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO settings_kv (key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rusqlite::params![key, value],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    })
}

/// 删除单个设置项（auth 退出登录清理账号 profile 等）
pub fn delete(store: &Store, key: &str) -> Result<(), String> {
    store.with_conn(|c| {
        c.execute("DELETE FROM settings_kv WHERE key = ?1", rusqlite::params![key])
            .map(|_| ())
            .map_err(|e| e.to_string())
    })
}

/// 读取全部设置项（settings_get 返回整体）
pub fn all(store: &Store) -> Result<std::collections::HashMap<String, String>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare("SELECT key, value FROM settings_kv")
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| e.to_string())?;
        let mut map = std::collections::HashMap::new();
        for row in rows {
            let (k, v) = row.map_err(|e| e.to_string())?;
            map.insert(k, v);
        }
        Ok(map)
    })
}

/// 设置值 → 库内字符串（写入侧统一 JSON 编码；字符串原样存避免双重转义）
pub fn encode_value(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// 库内字符串 → 设置值（读取侧还原类型；非 JSON 文本回退字符串，
/// 避免 "false" 这类文本在渲染进程被当成真值）
pub fn decode_value(raw: &str) -> serde_json::Value {
    serde_json::from_str::<serde_json::Value>(raw)
        .unwrap_or_else(|_| serde_json::Value::String(raw.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-settings-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_set_get_roundtrip() {
        let (store, dir) = temp_store("rt");
        set(&store, "theme", "dark").unwrap();
        assert_eq!(get(&store, "theme").unwrap().as_deref(), Some("dark"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_overwrite() {
        let (store, dir) = temp_store("ow");
        set(&store, "lang", "zh").unwrap();
        set(&store, "lang", "en").unwrap();
        assert_eq!(get(&store, "lang").unwrap().as_deref(), Some("en"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_missing_key_none() {
        let (store, dir) = temp_store("miss");
        assert_eq!(get(&store, "nope").unwrap(), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_delete_key() {
        let (store, dir) = temp_store("del");
        set(&store, "auth.profile", "{}").unwrap();
        delete(&store, "auth.profile").unwrap();
        assert_eq!(get(&store, "auth.profile").unwrap(), None);
        // 删除不存在的键不报错（幂等）
        delete(&store, "auth.profile").unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_all_returns_map() {
        let (store, dir) = temp_store("all");
        set(&store, "a", "1").unwrap();
        set(&store, "b", "2").unwrap();
        let map = all(&store).unwrap();
        assert_eq!(map.get("a").map(String::as_str), Some("1"));
        assert_eq!(map.get("b").map(String::as_str), Some("2"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_encode_decode_roundtrip_types() {
        use serde_json::json;
        // 布尔/数字/对象经编码后读取还原类型（"false" 不再被当作真值）
        assert_eq!(decode_value(&encode_value(&json!(false))), json!(false));
        assert_eq!(decode_value(&encode_value(&json!(true))), json!(true));
        assert_eq!(decode_value(&encode_value(&json!(30))), json!(30));
        assert_eq!(decode_value(&encode_value(&json!({"k": 1}))), json!({"k": 1}));
        // 普通字符串原样存、原样读（不额外加引号）
        assert_eq!(encode_value(&json!("跟随系统")), "跟随系统");
        assert_eq!(decode_value("跟随系统"), json!("跟随系统"));
        // 历史裸文本（非 JSON）回退字符串，不报错
        assert_eq!(decode_value("not-json"), json!("not-json"));
    }
}
