//! chat_messages 表：会话正文持久化（对话刷新/重启后仍能恢复的数据层）
//!
//! 一行 = 一个会话（conv_id）某一轮（turn_index）某个角色（role）的正文。
//! 事件流（bridge::event_store）只留存 24h，且界面对「本地任务与会话数据仍会保留」
//! 是有承诺的，故正文独立落库、不设 TTL。
//!
//! 主键含 role，因此「重新生成」同一轮时是覆盖本轮回复（与界面一致），
//! 不会为同一轮累积多个版本；写入只碰 chat_messages，不动其他表。

use serde::Serialize;
use serde_json::Value;

use crate::store::Store;

pub const ROLE_USER: &str = "user";
pub const ROLE_ASSISTANT: &str = "assistant";

/// 回复评价取值（与渲染层 `'up' | 'down'` 字面量一致）
pub const FEEDBACK_UP: &str = "up";
pub const FEEDBACK_DOWN: &str = "down";

/// 一轮对话的一条正文
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MessageRow {
    pub conv_id: String,
    pub turn_index: i64,
    pub role: String,
    /// 本轮任务 id（过程明细事件按它在 event_store 里取回）
    pub task_id: String,
    pub content: String,
    /// 交付物文件名清单（仅 assistant 行有值）
    pub artifacts: Vec<String>,
    pub ts_ms: i64,
    /// 本轮回复的评价（join `chat_feedback` 得出，''=未评价；仅 assistant 行有值）
    pub feedback: String,
}

/// 会话摘要行（侧边栏列表用；标题取首轮提问，与渲染层同一口径截取）
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConversationRow {
    pub conv_id: String,
    /// 首轮用户提问（截断到 60 字，供渲染层生成标题）
    pub first_goal: String,
    pub turn_count: i64,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    /// 最近一轮的任务 id（归档标记按它匹配）
    pub last_task_id: String,
}

fn artifacts_to_json(artifacts: &[String]) -> String {
    Value::Array(artifacts.iter().map(|a| Value::String(a.clone())).collect()).to_string()
}

fn artifacts_from_json(raw: &str) -> Vec<String> {
    serde_json::from_str::<Vec<String>>(raw).unwrap_or_default()
}

/// 写入（覆盖同轮同角色的既有正文）
pub fn put(store: &Store, row: &MessageRow) -> Result<(), String> {
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO chat_messages (conv_id, turn_index, role, task_id, content, artifacts_json, ts_ms)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
             ON CONFLICT(conv_id, turn_index, role) DO UPDATE SET
                task_id = excluded.task_id,
                content = excluded.content,
                artifacts_json = excluded.artifacts_json,
                ts_ms = excluded.ts_ms",
            rusqlite::params![
                row.conv_id,
                row.turn_index,
                row.role,
                row.task_id,
                row.content,
                artifacts_to_json(&row.artifacts),
                row.ts_ms,
            ],
        )
        .map(|_| ())
        .map_err(|e| format!("CHAT_MESSAGE_SAVE_FAILED: {e}"))
    })
}

/// 记录一轮的用户提问（提交任务时调用）
pub fn record_user_goal(
    store: &Store,
    conv_id: &str,
    turn_index: i64,
    task_id: &str,
    goal: &str,
    ts_ms: i64,
) -> Result<(), String> {
    put(
        store,
        &MessageRow {
            conv_id: conv_id.to_string(),
            turn_index,
            role: ROLE_USER.to_string(),
            task_id: task_id.to_string(),
            content: goal.to_string(),
            artifacts: Vec::new(),
            ts_ms,
            feedback: String::new(),
        },
    )
}

/// 任务 → (conv_id, turn_index) 绑定：取该任务用户行的归属轮次
fn task_binding(c: &rusqlite::Connection, task_id: &str) -> Result<Option<(String, i64)>, String> {
    let mut stmt = c
        .prepare("SELECT conv_id, turn_index FROM chat_messages WHERE task_id = ?1 AND role = ?2 LIMIT 1")
        .map_err(|e| e.to_string())?;
    let mut rows = stmt
        .query(rusqlite::params![task_id, ROLE_USER])
        .map_err(|e| e.to_string())?;
    match rows.next().map_err(|e| e.to_string())? {
        Some(row) => Ok(Some((
            row.get::<_, String>(0).map_err(|e| e.to_string())?,
            row.get::<_, i64>(1).map_err(|e| e.to_string())?,
        ))),
        None => Ok(None),
    }
}

/// 记录一轮的助手回复（deliver 事件到达时调用）。
///
/// 正文优先取模型最终回复文本，缺失时退回交付摘要；两者都为空则不落库
/// （本轮没有可恢复的正文，宁可不写也不造假数据）。
/// 任务没有登记过用户提问时（如本功能上线前的历史任务），以任务 id 作为会话键，
/// 保证回复仍能在重启后看到，而不是被丢弃。
pub fn record_assistant_reply(
    store: &Store,
    task_id: &str,
    final_text: &str,
    summary: &str,
    artifacts: &[String],
    ts_ms: i64,
) -> Result<(), String> {
    let content = if !final_text.trim().is_empty() {
        final_text.to_string()
    } else if !summary.trim().is_empty() {
        summary.to_string()
    } else {
        return Ok(());
    };
    let (conv_id, turn_index) = store.with_conn(|c| task_binding(c, task_id))?
        .unwrap_or_else(|| (task_id.to_string(), 0));
    put(
        store,
        &MessageRow {
            conv_id,
            turn_index,
            role: ROLE_ASSISTANT.to_string(),
            task_id: task_id.to_string(),
            content,
            artifacts: artifacts.to_vec(),
            ts_ms,
            feedback: String::new(),
        },
    )
}

/// 记录/取消一轮回复的评价（点赞 up、点踩 down；`None`=取消评价）
///
/// 评价挂在「会话的某一轮」上，取消即删除这条评价记录，正文行不动。
/// 该轮在库里没有正文时拒绝写入（`CHAT_FEEDBACK_TURN_MISSING`），避免把评价记到不存在的回复上。
pub fn set_feedback(
    store: &Store,
    conv_id: &str,
    turn_index: i64,
    value: Option<&str>,
    ts_ms: i64,
) -> Result<(), String> {
    if !matches!(value, None | Some(FEEDBACK_UP) | Some(FEEDBACK_DOWN)) {
        return Err(format!(
            "CHAT_FEEDBACK_INVALID_VALUE: 只接受 {FEEDBACK_UP}/{FEEDBACK_DOWN}/取消"
        ));
    }
    store.with_conn(|c| {
        let exists: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM chat_messages WHERE conv_id = ?1 AND turn_index = ?2",
                rusqlite::params![conv_id, turn_index],
                |r| r.get(0),
            )
            .map_err(|e| format!("CHAT_FEEDBACK_CHECK_FAILED: {e}"))?;
        if exists == 0 {
            return Err(format!(
                "CHAT_FEEDBACK_TURN_MISSING: 会话 {conv_id} 第 {turn_index} 轮没有正文记录"
            ));
        }
        match value {
            Some(v) => c.execute(
                "INSERT INTO chat_feedback (conv_id, turn_index, value, ts_ms) VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(conv_id, turn_index) DO UPDATE SET value = excluded.value, ts_ms = excluded.ts_ms",
                rusqlite::params![conv_id, turn_index, v, ts_ms],
            ),
            None => c.execute(
                "DELETE FROM chat_feedback WHERE conv_id = ?1 AND turn_index = ?2",
                rusqlite::params![conv_id, turn_index],
            ),
        }
        .map(|_| ())
        .map_err(|e| format!("CHAT_FEEDBACK_SAVE_FAILED: {e}"))
    })
}

/// 按会话读取正文（轮次升序，同轮内 user 先于 assistant）
pub fn list_by_conv(
    store: &Store,
    conv_id: &str,
    limit: Option<usize>,
) -> Result<Vec<MessageRow>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT m.conv_id, m.turn_index, m.role, m.task_id, m.content, m.artifacts_json, m.ts_ms,
                        COALESCE(f.value, '')
                 FROM chat_messages m
                 LEFT JOIN chat_feedback f
                        ON f.conv_id = m.conv_id AND f.turn_index = m.turn_index
                       AND m.role = 'assistant'
                 WHERE m.conv_id = ?1
                 ORDER BY m.turn_index ASC,
                          CASE m.role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END ASC",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([conv_id], |r| {
                Ok(MessageRow {
                    conv_id: r.get(0)?,
                    turn_index: r.get(1)?,
                    role: r.get(2)?,
                    task_id: r.get(3)?,
                    content: r.get(4)?,
                    artifacts: artifacts_from_json(&r.get::<_, String>(5)?),
                    ts_ms: r.get(6)?,
                    feedback: r.get::<_, String>(7).unwrap_or_default(),
                })
            })
            .map_err(|e| e.to_string())?;
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

/// 会话列表（按最近活跃倒序；正文不落库的会话不会出现，故不伪造条目）
pub fn list_conversations(store: &Store, limit: Option<usize>) -> Result<Vec<ConversationRow>, String> {
    let sql = "SELECT c.conv_id,
                      (SELECT m.content FROM chat_messages m
                        WHERE m.conv_id = c.conv_id AND m.role = 'user'
                        ORDER BY m.turn_index ASC LIMIT 1),
                      (SELECT COUNT(DISTINCT m.turn_index) FROM chat_messages m
                        WHERE m.conv_id = c.conv_id),
                      MIN(c.ts_ms),
                      MAX(c.ts_ms),
                      (SELECT m.task_id FROM chat_messages m
                        WHERE m.conv_id = c.conv_id AND m.task_id <> ''
                        ORDER BY m.turn_index DESC LIMIT 1)
               FROM chat_messages c
               GROUP BY c.conv_id
               ORDER BY MAX(c.ts_ms) DESC";
    store.with_conn(|c| {
        let mut stmt = c.prepare(sql).map_err(|e| e.to_string())?;
        let rows = stmt.query_map([], |r| {
            let goal: String = r.get::<_, String>(1).unwrap_or_default();
            Ok(ConversationRow {
                conv_id: r.get(0)?,
                // 60 字足够渲染层截出 20 字标题，同时避免整段长提问塞进列表
                first_goal: goal.chars().take(60).collect(),
                turn_count: r.get(2)?,
                created_at_ms: r.get(3)?,
                updated_at_ms: r.get(4)?,
                last_task_id: r.get::<_, String>(5).unwrap_or_default(),
            })
        })
        .map_err(|e| e.to_string())?;
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
            "tt-messages-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_record_user_and_reply_roundtrip() {
        let (store, dir) = temp_store("roundtrip");
        record_user_goal(&store, "conv-1", 0, "task-1", "帮我整理周报", 1000).unwrap();
        record_assistant_reply(&store, "task-1", "本周完成 3 项", "任务完成", &["周报.md".into()], 2000)
            .unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].role, ROLE_USER);
        assert_eq!(rows[0].content, "帮我整理周报");
        assert_eq!(rows[1].role, ROLE_ASSISTANT);
        assert_eq!(rows[1].content, "本周完成 3 项");
        assert_eq!(rows[1].artifacts, vec!["周报.md".to_string()]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_multi_turn_ordering() {
        let (store, dir) = temp_store("turns");
        record_user_goal(&store, "conv-1", 1, "task-2", "第二轮提问", 3000).unwrap();
        record_assistant_reply(&store, "task-2", "第二轮回复", "", &[], 4000).unwrap();
        record_user_goal(&store, "conv-1", 0, "task-1", "首轮提问", 1000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        // 轮次升序、同轮 user 先于 assistant（乱序写入也要按对话顺序读回）
        assert_eq!(
            rows.iter()
                .map(|r| (r.turn_index, r.role.as_str()))
                .collect::<Vec<_>>(),
            vec![(0, "user"), (1, "user"), (1, "assistant")]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_regenerate_overwrites_same_turn_reply() {
        let (store, dir) = temp_store("regen");
        record_user_goal(&store, "conv-1", 0, "task-1", "提问", 1000).unwrap();
        record_assistant_reply(&store, "task-1", "第一版回复", "", &[], 2000).unwrap();
        // 重新生成：同一轮的新任务覆盖本轮回复，不追加第二条
        record_user_goal(&store, "conv-1", 0, "task-2", "提问", 3000).unwrap();
        record_assistant_reply(&store, "task-2", "第二版回复", "", &["a.md".into()], 4000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[1].content, "第二版回复");
        assert_eq!(rows[1].task_id, "task-2");
        assert_eq!(rows[1].artifacts, vec!["a.md".to_string()]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_reply_falls_back_to_summary_and_skips_empty() {
        let (store, dir) = temp_store("fallback");
        record_user_goal(&store, "conv-1", 0, "task-1", "提问", 1000).unwrap();
        // 模型无最终文本时退回摘要，不丢整轮
        record_assistant_reply(&store, "task-1", "   ", "已生成 2 个文件", &[], 2000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows[1].content, "已生成 2 个文件");
        // 正文与摘要都为空：没有可恢复内容，不落库（而不是写一条空占位）
        record_assistant_reply(&store, "task-1", "", "", &[], 3000).unwrap();
        let again = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(again.len(), 2);
        assert_eq!(again[1].ts_ms, 2000);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_reply_without_user_row_keys_by_task() {
        let (store, dir) = temp_store("orphan");
        // 未登记提问的任务（功能上线前的历史流）：回复以 task 为会话键保留
        record_assistant_reply(&store, "task-legacy", "旧任务回复", "", &[], 5000).unwrap();
        let rows = list_by_conv(&store, "task-legacy", None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].role, ROLE_ASSISTANT);
        assert_eq!(rows[0].turn_index, 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_conversations_summary() {
        let (store, dir) = temp_store("convs");
        let long_goal = "通".repeat(80);
        record_user_goal(&store, "conv-a", 0, &format!("task-a-0"), &long_goal, 1000).unwrap();
        record_assistant_reply(&store, "task-a-0", "A 的回复", "", &[], 2000).unwrap();
        record_user_goal(&store, "conv-b", 0, "task-b-0", "B 的首轮", 3000).unwrap();
        record_user_goal(&store, "conv-b", 1, "task-b-1", "B 的第二轮提问很长", 4000).unwrap();

        let convs = list_conversations(&store, None).unwrap();
        // 最近活跃倒序：conv-b（4000）先于 conv-a（2000）
        assert_eq!(
            convs.iter().map(|c| c.conv_id.as_str()).collect::<Vec<_>>(),
            vec!["conv-b", "conv-a"]
        );
        assert_eq!(convs[0].turn_count, 2);
        assert_eq!(convs[0].last_task_id, "task-b-1");
        assert_eq!(convs[0].created_at_ms, 3000);
        assert_eq!(convs[0].updated_at_ms, 4000);
        // 标题取首轮提问（不是最新一轮），长文本截到 60 字
        assert_eq!(convs[1].first_goal, "通".repeat(60));
        assert_eq!(convs[0].first_goal, "B 的首轮");

        let limited = list_conversations(&store, Some(1)).unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].conv_id, "conv-b");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_by_conv_isolated_between_conversations() {
        let (store, dir) = temp_store("isolate");
        record_user_goal(&store, "conv-a", 0, "task-a", "A 的提问", 1000).unwrap();
        record_user_goal(&store, "conv-b", 0, "task-b", "B 的提问", 2000).unwrap();
        let rows = list_by_conv(&store, "conv-a", None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].content, "A 的提问");
        assert!(list_by_conv(&store, "conv-missing", None).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_artifacts_json_robust() {
        assert_eq!(artifacts_to_json(&[]), "[]");
        let json = artifacts_to_json(&["含\"引号\".md".into(), "中文 报告.md".into()]);
        assert_eq!(
            artifacts_from_json(&json),
            vec!["含\"引号\".md".to_string(), "中文 报告.md".to_string()]
        );
        // 脏数据回退空清单，不让一行坏 JSON 打断会话恢复
        assert!(artifacts_from_json("not-json").is_empty());
    }

    #[test]
    fn test_persist_survives_reopen() {
        // 重启后仍可恢复（本表无 TTL，与事件流 24h 窗口互补）
        let dir = std::env::temp_dir().join(format!(
            "tt-messages-reopen-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("t.db");
        {
            let store = Store::open(db.clone()).unwrap();
            record_user_goal(&store, "conv-1", 0, "task-1", "重启前的提问", 1000).unwrap();
        }
        let store2 = Store::open(db).unwrap();
        let rows = list_by_conv(&store2, "conv-1", None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].content, "重启前的提问");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_feedback_roundtrip_and_change() {
        let (store, dir) = temp_store("feedback");
        record_user_goal(&store, "conv-1", 0, "task-1", "提问", 1000).unwrap();
        record_assistant_reply(&store, "task-1", "回复", "", &[], 2000).unwrap();
        // 未评价：assistant 行为空串，user 行永远不带评价
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows[0].feedback, "");
        assert_eq!(rows[1].feedback, "");
        set_feedback(&store, "conv-1", 0, Some(FEEDBACK_UP), 3000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows[0].feedback, "", "点赞只属本轮回复，不污染提问行");
        assert_eq!(rows[1].feedback, FEEDBACK_UP);
        // 改评价：同轮覆盖，不累积第二条
        set_feedback(&store, "conv-1", 0, Some(FEEDBACK_DOWN), 4000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows[1].feedback, FEEDBACK_DOWN);
        // 取消评价：删记录，正文行必须原样保留
        set_feedback(&store, "conv-1", 0, None, 5000).unwrap();
        let rows = list_by_conv(&store, "conv-1", None).unwrap();
        assert_eq!(rows.len(), 2, "取消评价只删评价，不得删正文");
        assert_eq!(rows[1].feedback, "");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_feedback_rejects_unknown_value_and_missing_turn() {
        let (store, dir) = temp_store("feedback-bad");
        record_user_goal(&store, "conv-1", 0, "task-1", "提问", 1000).unwrap();
        let err = set_feedback(&store, "conv-1", 0, Some("meh"), 2000).unwrap_err();
        assert!(err.starts_with("CHAT_FEEDBACK_INVALID_VALUE"), "实际错误：{err}");
        // 没有正文的轮次不可评价，避免留下悬空评价
        let err = set_feedback(&store, "conv-1", 7, Some(FEEDBACK_UP), 2000).unwrap_err();
        assert!(err.starts_with("CHAT_FEEDBACK_TURN_MISSING"), "实际错误：{err}");
        let err = set_feedback(&store, "conv-missing", 0, Some(FEEDBACK_UP), 2000).unwrap_err();
        assert!(err.starts_with("CHAT_FEEDBACK_TURN_MISSING"), "实际错误：{err}");
        // 取消不存在的评价：幂等成功，不报错
        set_feedback(&store, "conv-1", 0, None, 2000).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_feedback_isolated_between_conversations() {
        let (store, dir) = temp_store("feedback-isolate");
        record_user_goal(&store, "conv-a", 0, "task-a", "A 的提问", 1000).unwrap();
        record_assistant_reply(&store, "task-a", "A 的回复", "", &[], 1500).unwrap();
        record_user_goal(&store, "conv-b", 0, "task-b", "B 的提问", 2000).unwrap();
        record_assistant_reply(&store, "task-b", "B 的回复", "", &[], 2500).unwrap();
        set_feedback(&store, "conv-b", 0, Some(FEEDBACK_UP), 3000).unwrap();
        assert_eq!(list_by_conv(&store, "conv-a", None).unwrap()[1].feedback, "");
        assert_eq!(list_by_conv(&store, "conv-b", None).unwrap()[1].feedback, FEEDBACK_UP);
        // 重新生成同轮：正文被新任务覆盖，评价按 (会话, 轮次) 归属，仍随本轮回复显示
        record_user_goal(&store, "conv-b", 0, "task-b2", "B 的提问", 4000).unwrap();
        record_assistant_reply(&store, "task-b2", "B 的第二版回复", "", &[], 5000).unwrap();
        let rows = list_by_conv(&store, "conv-b", None).unwrap();
        assert_eq!(rows[1].content, "B 的第二版回复");
        assert_eq!(rows[1].feedback, FEEDBACK_UP);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_feedback_survives_reopen() {
        let dir = std::env::temp_dir().join(format!(
            "tt-feedback-reopen-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("t.db");
        {
            let store = Store::open(db.clone()).unwrap();
            record_user_goal(&store, "conv-1", 0, "task-1", "提问", 1000).unwrap();
            record_assistant_reply(&store, "task-1", "回复", "", &[], 2000).unwrap();
            set_feedback(&store, "conv-1", 0, Some(FEEDBACK_DOWN), 3000).unwrap();
        }
        let store2 = Store::open(db).unwrap();
        let rows = list_by_conv(&store2, "conv-1", None).unwrap();
        assert_eq!(rows[1].feedback, FEEDBACK_DOWN, "重启后点赞/点踩必须仍在");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
