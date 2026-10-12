//! 连接器授权状态持久化（千问对标整改 spec §6.3 connector_*）
//!
//! 只存「用户是否授权过某个连接器」这一事实，不做任何假授权：
//! - list 在无记录时返回空数组（页面据此展示未授权态）
//! - authorize/revoke 幂等 upsert，重复点击不会产生脏数据

use serde::Serialize;
use serde_json::{json, Value};

use crate::security::audit;
use crate::store::Store;

pub const CATEGORY_BUILTIN: &str = "builtin";
pub const CATEGORY_MARKET: &str = "market";

pub const AUTH_NONE: &str = "none";
pub const AUTHORIZED: &str = "authorized";

/// 渲染层 ConnectorState 对应的持久化行
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConnectorView {
    pub connector_id: String,
    pub category: String,
    pub installed: bool,
    pub auth_status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub server_ref: Option<String>,
    pub updated_at: String,
}

fn validate_id(connector_id: &str) -> Result<&str, String> {
    let id = connector_id.trim();
    if id.is_empty() {
        return Err("CONNECTOR_INVALID_ID: 连接器标识不能为空".to_string());
    }
    Ok(id)
}

fn validate_category(category: Option<&str>) -> Result<String, String> {
    let raw = category.map(str::trim).filter(|c| !c.is_empty());
    match raw {
        None => Ok(CATEGORY_BUILTIN.to_string()),
        Some(c) if c == CATEGORY_BUILTIN || c == CATEGORY_MARKET => Ok(c.to_string()),
        Some(c) => Err(format!(
            "CONNECTOR_INVALID_CATEGORY: 连接器分类非法「{c}」，仅支持 {CATEGORY_BUILTIN} / {CATEGORY_MARKET}"
        )),
    }
}

/// 授权（安装）某连接器：installed=1 + authorized
pub fn authorize(
    store: &Store,
    connector_id: &str,
    category: Option<&str>,
    server_ref: Option<&str>,
    now_ms: i64,
) -> Result<ConnectorView, String> {
    let id = validate_id(connector_id)?;
    let cat = validate_category(category)?;
    let row = store.with_conn(|c| {
        c.execute(
            "INSERT INTO connector_states (connector_id, category, installed, auth_status, server_ref, updated_at_ms)
             VALUES (?1, ?2, 1, ?3, ?4, ?5)
             ON CONFLICT(connector_id) DO UPDATE SET
                category = excluded.category,
                installed = 1,
                auth_status = excluded.auth_status,
                server_ref = COALESCE(excluded.server_ref, connector_states.server_ref),
                updated_at_ms = excluded.updated_at_ms",
            rusqlite::params![
                id,
                cat,
                AUTHORIZED,
                server_ref.map(str::trim).filter(|s| !s.is_empty()),
                now_ms
            ],
        )
        .map_err(|e| format!("CONNECTOR_SAVE_FAILED: {e}"))?;
        select_one(c, id).map_err(|e| format!("CONNECTOR_SAVE_FAILED: {e}"))
    })?;
    Ok(row)
}

/// 取消授权：保留行以便页面显示「曾配置但未授权」
pub fn revoke(store: &Store, connector_id: &str, now_ms: i64) -> Result<(), String> {
    let id = validate_id(connector_id)?;
    store.with_conn(|c| {
        let exists: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM connector_states WHERE connector_id = ?1",
                [id],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if exists == 0 {
            return Err(format!("CONNECTOR_NOT_FOUND: 连接器「{id}」没有授权记录，无需撤销"));
        }
        c.execute(
            "UPDATE connector_states SET installed = 0, auth_status = ?2, updated_at_ms = ?3
             WHERE connector_id = ?1",
            rusqlite::params![id, AUTH_NONE, now_ms],
        )
        .map(|_| ())
        .map_err(|e| format!("CONNECTOR_SAVE_FAILED: {e}"))
    })
}

/// 全部连接器状态（按标识稳定排序）
pub fn list(store: &Store) -> Result<Vec<ConnectorView>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT connector_id, category, installed, auth_status, server_ref, updated_at_ms
                 FROM connector_states ORDER BY connector_id",
            )
            .map_err(|e| format!("CONNECTOR_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(ConnectorView {
                    connector_id: r.get(0)?,
                    category: r.get(1)?,
                    installed: r.get::<_, i64>(2)? != 0,
                    auth_status: r.get(3)?,
                    server_ref: r.get(4)?,
                    updated_at: audit::iso8601_utc(r.get::<_, i64>(5)?),
                })
            })
            .map_err(|e| format!("CONNECTOR_QUERY_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("CONNECTOR_QUERY_FAILED: {e}"))
    })
}

fn select_one(c: &rusqlite::Connection, id: &str) -> Result<ConnectorView, String> {
    c.query_row(
        "SELECT connector_id, category, installed, auth_status, server_ref, updated_at_ms
         FROM connector_states WHERE connector_id = ?1",
        [id],
        |r| {
            Ok(ConnectorView {
                connector_id: r.get(0)?,
                category: r.get(1)?,
                installed: r.get::<_, i64>(2)? != 0,
                auth_status: r.get(3)?,
                server_ref: r.get(4)?,
                updated_at: audit::iso8601_utc(r.get::<_, i64>(5)?),
            })
        },
    )
    .map_err(|e| e.to_string())
}

/// 授权动作的审计摘要（command 层留痕用）
pub fn audit_summary(view: &ConnectorView) -> String {
    format!(
        "连接器授权：{}（{}）已安装={}",
        view.connector_id, view.category, view.installed
    )
}

/// 撤销动作的审计摘要
pub fn revoke_summary(connector_id: &str) -> String {
    format!("连接器授权撤销：{connector_id}")
}

pub fn ok_value(view: &ConnectorView) -> Value {
    serde_json::to_value(view).unwrap_or_else(|_| json!({ "connectorId": view.connector_id }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::security::audit;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-connector-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_list_empty_by_default() {
        let (store, dir) = temp_store("empty");
        assert!(list(&store).unwrap().is_empty(), "无授权记录时不得伪造连接器");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_authorize_creates_authorized_row() {
        let (store, dir) = temp_store("authorize");
        let ms = 1_700_000_000_000;
        let v = authorize(&store, "dingtalk", None, None, ms).unwrap();
        assert_eq!(v.connector_id, "dingtalk");
        assert_eq!(v.category, CATEGORY_BUILTIN, "分类缺省为 builtin");
        assert!(v.installed);
        assert_eq!(v.auth_status, AUTHORIZED);
        assert_eq!(v.updated_at, audit::iso8601_utc(ms));
        assert!(v.server_ref.is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_authorize_is_idempotent_upsert() {
        let (store, dir) = temp_store("idempotent");
        authorize(&store, "crm", Some(CATEGORY_MARKET), Some("srv-1"), 1).unwrap();
        authorize(&store, "crm", Some(CATEGORY_MARKET), None, 999).unwrap();
        let rows = list(&store).unwrap();
        assert_eq!(rows.len(), 1, "重复授权不得追加脏行");
        assert_eq!(rows[0].auth_status, AUTHORIZED);
        assert!(rows[0].updated_at > audit::iso8601_utc(1), "后写覆盖时间戳");
        // server_ref 留空时保留原值（后续点击不应抹掉已绑定的服务引用）
        assert_eq!(rows[0].server_ref.as_deref(), Some("srv-1"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_authorize_can_update_server_ref() {
        let (store, dir) = temp_store("ref");
        authorize(&store, "crm", None, Some("old"), 1).unwrap();
        let v = authorize(&store, "crm", None, Some("new"), 2).unwrap();
        assert_eq!(v.server_ref.as_deref(), Some("new"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_revoke_keeps_row_and_clears_auth() {
        let (store, dir) = temp_store("revoke");
        authorize(&store, "a", None, None, 1).unwrap();
        authorize(&store, "b", None, None, 1).unwrap();
        revoke(&store, "a", 9).unwrap();
        let rows = list(&store).unwrap();
        assert_eq!(rows.len(), 2, "撤销不得删除存量行");
        let a = rows.iter().find(|r| r.connector_id == "a").unwrap();
        assert!(!a.installed);
        assert_eq!(a.auth_status, AUTH_NONE);
        let b = rows.iter().find(|r| r.connector_id == "b").unwrap();
        assert_eq!(b.auth_status, AUTHORIZED, "撤销单个连接器不应影响其他连接器");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_revoke_unknown_is_rejected() {
        let (store, dir) = temp_store("revoke-miss");
        let err = revoke(&store, "ghost", 1).unwrap_err();
        assert!(err.starts_with("CONNECTOR_NOT_FOUND"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_invalid_inputs_are_rejected() {
        let (store, dir) = temp_store("invalid");
        assert!(authorize(&store, "   ", None, None, 1)
            .unwrap_err()
            .starts_with("CONNECTOR_INVALID_ID"));
        assert!(authorize(&store, "ok", Some("weird"), None, 1)
            .unwrap_err()
            .starts_with("CONNECTOR_INVALID_CATEGORY"));
        assert_eq!(validate_category(Some(CATEGORY_MARKET)).unwrap(), CATEGORY_MARKET);
        assert_eq!(validate_category(Some("  ")).unwrap(), CATEGORY_BUILTIN);
        assert_eq!(validate_category(None).unwrap(), CATEGORY_BUILTIN);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_id_is_trimmed_before_write() {
        let (store, dir) = temp_store("trim");
        let v = authorize(&store, "  spaced  ", None, Some("  "), 1).unwrap();
        assert_eq!(v.connector_id, "spaced");
        assert!(v.server_ref.is_none(), "空白 server_ref 不应入库");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_summaries_mention_the_connector() {
        let (store, dir) = temp_store("summary");
        let v = authorize(&store, "wiki", None, None, 1).unwrap();
        assert!(audit_summary(&v).contains("wiki"));
        assert!(revoke_summary("wiki").contains("wiki"));
        assert_eq!(ok_value(&v)["connectorId"], "wiki");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_orders_by_connector_id() {
        let (store, dir) = temp_store("order");
        for id in ["zeta", "alpha", "mid"] {
            authorize(&store, id, None, None, 1).unwrap();
        }
        let rows = list(&store).unwrap();
        let ids = rows
            .iter()
            .map(|r| r.connector_id.as_str())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec!["alpha", "mid", "zeta"]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
