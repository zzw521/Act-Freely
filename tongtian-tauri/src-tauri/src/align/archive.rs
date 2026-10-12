//! 任务归档（千问对标整改 spec §6.3 task_archive_*）
//!
//! 归档标记与任务本体分表存放：`archived_tasks` 只记「哪个任务被归档于何时」，
//! 任务正文仍在 `tasks` 表，取消归档不需要重放任何数据。
//!
//! 破坏性边界（开发规范：禁止误删存量数据）：
//! - unarchive 只移除归档标记，绝不动 `tasks`
//! - archive_delete 只删用户点名确认的那一条
//! - archive_clear 必须带确认词 DELETE，且仅清归档清单，保留任务本体

use serde::Serialize;
use serde_json::Value;

use crate::security::audit;
use crate::store::Store;

pub const DEFAULT_WORKSPACE: &str = "default";
pub const CLEAR_CONFIRM_WORD: &str = "DELETE";

/// 渲染层 ArchiveItem 对应的视图行
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ArchiveView {
    pub task_id: String,
    pub title: String,
    pub workspace_id: String,
    pub archived_at: String,
    pub archived_at_ms: i64,
}

fn validate_task_id(task_id: &str) -> Result<&str, String> {
    let id = task_id.trim();
    if id.is_empty() {
        return Err("ARCHIVE_INVALID_TASK_ID: 任务标识不能为空".to_string());
    }
    Ok(id)
}

/// 任务是否登记过（chat_history 的数据层有行即存在）
fn task_exists(c: &rusqlite::Connection, task_id: &str) -> Result<bool, String> {
    c.query_row(
        "SELECT COUNT(*) FROM tasks WHERE task_id = ?1",
        [task_id],
        |r| r.get::<_, i64>(0),
    )
    .map(|n| n > 0)
    .map_err(|e| e.to_string())
}

/// 归档一个任务（重复归档刷新时间，不追加脏行）
///
/// 未显式给工作区时沿用任务本体上的工作区，避免归档后任务跑到 default 下。
pub fn archive(store: &Store, task_id: &str, workspace_id: Option<&str>, now_ms: i64) -> Result<ArchiveView, String> {
    let id = validate_task_id(task_id)?;
    let ws = workspace_id.map(str::trim).filter(|s| !s.is_empty());
    store.with_conn(|c| {
        if !task_exists(c, id)? {
            return Err(format!("ARCHIVE_TASK_NOT_FOUND: 任务「{id}」不存在，无法归档"));
        }
        let resolved = match ws {
            Some(w) => w.to_string(),
            None => c
                .query_row(
                    "SELECT workspace_id FROM tasks WHERE task_id = ?1",
                    [id],
                    |r| r.get::<_, String>(0),
                )
                .unwrap_or_else(|_| DEFAULT_WORKSPACE.to_string()),
        };
        c.execute(
            "INSERT INTO archived_tasks (task_id, workspace_id, archived_at_ms) VALUES (?1, ?2, ?3)
             ON CONFLICT(task_id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                archived_at_ms = excluded.archived_at_ms",
            rusqlite::params![id, resolved, now_ms],
        )
        .map_err(|e| format!("ARCHIVE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;
    get_one(store, id)
}

/// 归档清单（按归档时间倒序；标题取自任务本体，任务已删时诚实标注）
pub fn list(store: &Store) -> Result<Vec<ArchiveView>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT a.task_id, COALESCE(t.title, ''), a.workspace_id, a.archived_at_ms
                 FROM archived_tasks a LEFT JOIN tasks t ON t.task_id = a.task_id
                 ORDER BY a.archived_at_ms DESC, a.task_id",
            )
            .map_err(|e| format!("ARCHIVE_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], |r| {
                let ms: i64 = r.get(3)?;
                let title: String = r.get(1)?;
                Ok(ArchiveView {
                    task_id: r.get(0)?,
                    title: if title.trim().is_empty() {
                        "(无标题任务)".to_string()
                    } else {
                        title
                    },
                    workspace_id: r.get(2)?,
                    archived_at: audit::iso8601_utc(ms),
                    archived_at_ms: ms,
                })
            })
            .map_err(|e| format!("ARCHIVE_QUERY_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("ARCHIVE_QUERY_FAILED: {e}"))
    })
}

fn get_one(store: &Store, task_id: &str) -> Result<ArchiveView, String> {
    store.with_conn(|c| {
        c.query_row(
            "SELECT a.task_id, COALESCE(t.title, ''), a.workspace_id, a.archived_at_ms
             FROM archived_tasks a LEFT JOIN tasks t ON t.task_id = a.task_id
             WHERE a.task_id = ?1",
            [task_id],
            |r| {
                let ms: i64 = r.get(3)?;
                let title: String = r.get(1)?;
                Ok(ArchiveView {
                    task_id: r.get(0)?,
                    title: if title.trim().is_empty() {
                        "(无标题任务)".to_string()
                    } else {
                        title
                    },
                    workspace_id: r.get(2)?,
                    archived_at: audit::iso8601_utc(ms),
                    archived_at_ms: ms,
                })
            },
        )
        .map_err(|e| format!("ARCHIVE_QUERY_FAILED: {e}"))
    })
}

/// 取消归档：只移除标记，任务本体与历史保留
pub fn unarchive(store: &Store, task_id: &str) -> Result<bool, String> {
    let id = validate_task_id(task_id)?;
    store.with_conn(|c| {
        c.execute(
            "DELETE FROM archived_tasks WHERE task_id = ?1",
            [id],
        )
        .map(|n| n > 0)
        .map_err(|e| format!("ARCHIVE_SAVE_FAILED: {e}"))
    })
}

/// 彻底删除归档任务：移除标记并删除该任务本体行（仅此一条）
pub fn delete_archived(store: &Store, task_id: &str) -> Result<bool, String> {
    let id = validate_task_id(task_id)?;
    store.with_conn(|c| {
        let removed = c
            .execute("DELETE FROM archived_tasks WHERE task_id = ?1", [id])
            .map_err(|e| format!("ARCHIVE_SAVE_FAILED: {e}"))?;
        c.execute("DELETE FROM tasks WHERE task_id = ?1", [id])
            .map_err(|e| format!("ARCHIVE_DELETE_FAILED: {e}"))?;
        Ok(removed > 0)
    })
}

/// 清空归档清单（必须显式确认词；任务本体不受影响）
pub fn clear_all(store: &Store, confirm: &str) -> Result<usize, String> {
    if confirm.trim() != CLEAR_CONFIRM_WORD {
        return Err(format!(
            "ARCHIVE_CONFIRM_REQUIRED: 清空归档需输入确认词 {CLEAR_CONFIRM_WORD}"
        ));
    }
    store.with_conn(|c| {
        c.execute("DELETE FROM archived_tasks", [])
            .map_err(|e| format!("ARCHIVE_SAVE_FAILED: {e}"))
    })
}

/// 任务标题（审计摘要用；任务不存在时 None）
pub fn task_title(store: &Store, task_id: &str) -> Option<String> {
    store
        .with_conn(|c| {
            c.query_row(
                "SELECT title FROM tasks WHERE task_id = ?1",
                [task_id],
                |r| r.get::<_, String>(0),
            )
            .map_err(|e| e.to_string())
        })
        .ok()
}

/// 归档行 → 渲染层契约值（camelCase，含归档毫秒供本地时区展示）
pub fn to_value(view: &ArchiveView) -> Value {
    serde_json::to_value(view).unwrap_or(Value::Null)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::tasks::{upsert, TaskRow};
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-archive-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn seed_task(store: &Store, id: &str, title: &str, ws: &str, updated: i64) {
        upsert(
            store,
            &TaskRow {
                task_id: id.into(),
                workspace_id: ws.into(),
                title: title.into(),
                status: "done".into(),
                wait_reason: None,
                retry_count: 0,
                status_reason_code: None,
                queue_position: 0,
                created_at_ms: updated,
                updated_at_ms: updated,
            },
        )
        .unwrap();
    }

    #[test]
    fn test_list_empty_by_default() {
        let (store, dir) = temp_store("empty");
        assert!(list(&store).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_archives_existing_task_and_keeps_its_workspace() {
        let (store, dir) = temp_store("archive");
        seed_task(&store, "t1", "季度报表", "reports", 100);
        let v = archive(&store, "t1", None, 5_000).unwrap();
        assert_eq!(v.task_id, "t1");
        assert_eq!(v.title, "季度报表");
        assert_eq!(v.workspace_id, "reports", "未指定工作区时应沿用任务本体工作区");
        assert_eq!(v.archived_at_ms, 5_000);
        assert!(list(&store).unwrap().iter().any(|r| r.task_id == "t1"));
        assert!(!list(&store).unwrap().iter().any(|r| r.task_id == "ghost"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_archiving_unknown_task_is_rejected() {
        let (store, dir) = temp_store("miss");
        let err = archive(&store, "nope", None, 1).unwrap_err();
        assert!(err.starts_with("ARCHIVE_TASK_NOT_FOUND"), "{err}");
        assert!(list(&store).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_blank_task_id_is_rejected() {
        let (store, dir) = temp_store("blank");
        for r in [
            archive(&store, "  ", None, 1).map(|_| ()),
            unarchive(&store, "  ").map(|_| ()),
            delete_archived(&store, " ").map(|_| ()),
        ] {
            assert!(r.unwrap_err().starts_with("ARCHIVE_INVALID_TASK_ID"));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_duplicate_archive_refreshes_time_without_new_rows() {
        let (store, dir) = temp_store("dup");
        seed_task(&store, "t1", "A", "default", 1);
        archive(&store, "t1", None, 10).unwrap();
        archive(&store, "t1", Some("other"), 99).unwrap();
        let rows = list(&store).unwrap();
        assert_eq!(rows.len(), 1, "重复归档不得追加脏行");
        assert_eq!(rows[0].archived_at_ms, 99);
        assert_eq!(rows[0].workspace_id, "other", "显式指定工作区时以提交值为准");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_orders_by_newest_archive() {
        let (store, dir) = temp_store("order");
        seed_task(&store, "t1", "早", "default", 1);
        seed_task(&store, "t2", "晚", "default", 2);
        archive(&store, "t1", None, 100).unwrap();
        archive(&store, "t2", None, 900).unwrap();
        let rows = list(&store).unwrap();
        let ids = rows.iter().map(|r| r.task_id.as_str()).collect::<Vec<_>>();
        assert_eq!(ids, vec!["t2", "t1"]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_unarchive_removes_marker_only() {
        let (store, dir) = temp_store("unarchive");
        seed_task(&store, "t1", "保留我", "default", 1);
        archive(&store, "t1", None, 10).unwrap();
        assert!(unarchive(&store, "t1").unwrap());
        assert!(list(&store).unwrap().is_empty());
        assert!(!unarchive(&store, "t1").unwrap(), "重复取消归档应报未命中");
        // 任务本体必须仍在（chat_history 仍可见）
        let titles = store
            .with_conn(|c| {
                c.query_row("SELECT title FROM tasks WHERE task_id='t1'", [], |r| r.get::<_, String>(0))
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        assert_eq!(titles, "保留我");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_delete_archived_removes_only_the_named_task() {
        let (store, dir) = temp_store("delete");
        seed_task(&store, "t1", "删我", "default", 1);
        seed_task(&store, "t2", "留我", "default", 2);
        archive(&store, "t1", None, 10).unwrap();
        archive(&store, "t2", None, 20).unwrap();
        assert!(delete_archived(&store, "t1").unwrap());
        let rest = list(&store).unwrap();
        assert_eq!(rest.len(), 1);
        assert_eq!(rest[0].task_id, "t2");
        let remaining_titles: Vec<String> = store
            .with_conn(|c| {
                let mut stmt = c
                    .prepare("SELECT title FROM tasks ORDER BY task_id")
                    .map_err(|e| e.to_string())?;
                let rows = stmt.query_map([], |r| r.get::<_, String>(0)).map_err(|e| e.to_string())?;
                rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
            })
            .unwrap();
        assert_eq!(remaining_titles, vec!["留我"], "彻底删除只能影响被点名的任务");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_clear_requires_confirm_word_and_keeps_tasks() {
        let (store, dir) = temp_store("clear");
        seed_task(&store, "t1", "A", "default", 1);
        seed_task(&store, "t2", "B", "default", 2);
        archive(&store, "t1", None, 10).unwrap();
        archive(&store, "t2", None, 20).unwrap();

        let err = clear_all(&store, "delete").unwrap_err();
        assert!(err.starts_with("ARCHIVE_CONFIRM_REQUIRED"), "{err}");
        assert_eq!(list(&store).unwrap().len(), 2, "确认词不符时不得清掉任何行");

        assert_eq!(clear_all(&store, " DELETE ").unwrap(), 2);
        assert!(list(&store).unwrap().is_empty());
        let tasks: i64 = store
            .with_conn(|c| c.query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get(0)).map_err(|e| e.to_string()))
            .unwrap();
        assert_eq!(tasks, 2, "清空归档清单只清标记，任务本体必须保留");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_title_falls_back_when_task_body_missing() {
        let (store, dir) = temp_store("orphan");
        seed_task(&store, "t1", "   ", "default", 1);
        archive(&store, "t1", None, 10).unwrap();
        // 任务标题为空时列表仍要给出可辨识文案，不能显示空白
        assert_eq!(list(&store).unwrap()[0].title, "(无标题任务)");
        assert_eq!(task_title(&store, "t1").as_deref(), Some("   "));
        assert!(task_title(&store, "ghost").is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_archive_view_serializes_camel_case() {
        let (store, dir) = temp_store("view");
        seed_task(&store, "t1", "序列化", "default", 1);
        archive(&store, "t1", None, 10).unwrap();
        let v = to_value(&get_one(&store, "t1").unwrap());
        assert_eq!(v["taskId"], "t1");
        assert_eq!(v["workspaceId"], "default");
        assert_eq!(v["archivedAtMs"], 10);
        assert_eq!(v["archivedAt"].as_str().unwrap().chars().last(), Some('Z'));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
