//! tasks 表持久化（chat_history 的数据层）
//!
//! 与 task::registry 内存注册表配合：注册表状态变更时同步落库，
//! 重启后 chat_history 从 DB 恢复（含 14 态 meta 字段）。

use crate::store::Store;
use serde::Serialize;

/// 持久化任务摘要（对齐 contracts.ts TaskSummary + meta）
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskRow {
    pub task_id: String,
    pub workspace_id: String,
    pub title: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wait_reason: Option<String>,
    pub retry_count: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status_reason_code: Option<String>,
    pub queue_position: i64,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

/// 插入或更新任务记录（upsert）
pub fn upsert(store: &Store, row: &TaskRow) -> Result<(), String> {
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO tasks (task_id, workspace_id, title, status, wait_reason,
                                 retry_count, status_reason_code, queue_position,
                                 created_at_ms, updated_at_ms)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
             ON CONFLICT(task_id) DO UPDATE SET
                title = CASE WHEN excluded.title = '' THEN tasks.title ELSE excluded.title END,
                status = excluded.status,
                wait_reason = excluded.wait_reason,
                retry_count = excluded.retry_count,
                status_reason_code = excluded.status_reason_code,
                queue_position = excluded.queue_position,
                updated_at_ms = excluded.updated_at_ms",
            rusqlite::params![
                row.task_id,
                row.workspace_id,
                row.title,
                row.status,
                row.wait_reason,
                row.retry_count,
                row.status_reason_code,
                row.queue_position,
                row.created_at_ms,
                row.updated_at_ms,
            ],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    })
}

/// 按更新时间倒序列出任务（chat_history，limit 可空）
pub fn list_recent(store: &Store, limit: Option<usize>) -> Result<Vec<TaskRow>, String> {
    store.with_conn(|c| {
        let sql = "SELECT task_id, workspace_id, title, status, wait_reason,
                          retry_count, status_reason_code, queue_position,
                          created_at_ms, updated_at_ms
                   FROM tasks ORDER BY updated_at_ms DESC";
        let mut stmt = c.prepare(sql).map_err(|e| e.to_string())?;
        let rows = stmt.query_map([], |r| {
            Ok(TaskRow {
                task_id: r.get(0)?,
                workspace_id: r.get(1)?,
                title: r.get(2)?,
                status: r.get(3)?,
                wait_reason: r.get(4)?,
                retry_count: r.get(5)?,
                status_reason_code: r.get(6)?,
                queue_position: r.get(7)?,
                created_at_ms: r.get(8)?,
                updated_at_ms: r.get(9)?,
            })
        });
        let rows = rows.map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(|e| e.to_string())?);
            if let Some(l) = limit {
                if out.len() >= l {
                    break;
                }
            }
        }
        Ok(out)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-tasks-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn row(id: &str, status: &str, updated: i64) -> TaskRow {
        TaskRow {
            task_id: id.into(),
            workspace_id: "default".into(),
            title: format!("任务 {id}"),
            status: status.into(),
            wait_reason: None,
            retry_count: 0,
            status_reason_code: None,
            queue_position: 0,
            created_at_ms: updated,
            updated_at_ms: updated,
        }
    }

    #[test]
    fn test_upsert_and_list_order() {
        let (store, dir) = temp_store("order");
        upsert(&store, &row("t1", "done", 100)).unwrap();
        upsert(&store, &row("t2", "done", 300)).unwrap();
        upsert(&store, &row("t3", "failed", 200)).unwrap();
        let list = list_recent(&store, None).unwrap();
        assert_eq!(
            list.iter().map(|r| r.task_id.as_str()).collect::<Vec<_>>(),
            vec!["t2", "t3", "t1"]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_upsert_updates_existing() {
        let (store, dir) = temp_store("up");
        upsert(&store, &row("t1", "queued", 100)).unwrap();
        let mut updated = row("t1", "done", 500);
        updated.status_reason_code = Some("REJECTED".into());
        upsert(&store, &updated).unwrap();
        let list = list_recent(&store, None).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].status, "done");
        assert_eq!(list[0].status_reason_code.as_deref(), Some("REJECTED"));
        assert_eq!(list[0].updated_at_ms, 500);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_limit_applied() {
        let (store, dir) = temp_store("limit");
        for i in 0..5 {
            upsert(&store, &row(&format!("t{i}"), "done", i)).unwrap();
        }
        let list = list_recent(&store, Some(2)).unwrap();
        assert_eq!(list.len(), 2);
        assert_eq!(list[0].task_id, "t4"); // 最新优先
        let _ = std::fs::remove_dir_all(&dir);
    }
}
