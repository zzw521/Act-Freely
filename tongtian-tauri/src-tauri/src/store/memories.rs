//! 跨任务长期记忆（D-35：结构化存储 + 复核 + 冲突软退休 + 实体页 + 导出导入）
//!
//! 事实来源是桌面库 `memories` 表，不是第二份记忆文件：
//!
//! - 归属：`global`（所有项目可见）或 `workspace` + 工作区 id
//! - 证据：每行记下产生它的 `task_id` / `conv_id`，界面可追溯到具体任务
//! - 去重：`(scope, workspace_id, content_hash)` 唯一；重复任务合并为一行，
//!   累加 `occurrences` 并原地刷新 `updated_at`，不追加重复条目
//! - 复核：`active` / `pending` / `archived`；打开「写入前待审」后自动候选先落
//!   `pending`，确认才参与召回，驳回与退休只改状态不物理删除
//! - 冲突：结构化事实按 `(subject, predicate)` 判重，新事实生效时旧事实软退休并
//!   记 `superseded_by`；实体页由 `subject` 聚合，不另建第二张事实表
//! - 召回：按本轮目标做词面匹配（ASCII 词 + 中文二元组）、来源加权、时间排序与
//!   字符预算；命中条目记 `use_count` / `last_used_at`；没有命中就不注入
//! - 可携带：JSON 导出 / 导入（导入前自动备份），旧 `MEMORY.md` 条目只导入一次
//!
//! 引擎不直接写这张表：任务收尾时把候选放在 deliver 事件里，工具写入走
//! `memory_save` 事件，由 Rust 落库（存储归属：桌面库只由 Rust 主进程打开）。
//! 浏览器直连引擎没有 Rust 通道，仍写 `MEMORY.md`，行为保持不变。

use std::collections::{BTreeMap, HashSet};
use std::path::Path;

use rusqlite::{params, OptionalExtension};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::store::Store;

pub const SCOPE_GLOBAL: &str = "global";
pub const SCOPE_WORKSPACE: &str = "workspace";
pub const KINDS: &[&str] = &["task", "fact", "preference", "decision"];
pub const SOURCES: &[&str] = &["auto", "manual", "reflection"];
pub const STATUS_ACTIVE: &str = "active";
pub const STATUS_PENDING: &str = "pending";
pub const STATUS_ARCHIVED: &str = "archived";
pub const STATUSES: &[&str] = &[STATUS_ACTIVE, STATUS_PENDING, STATUS_ARCHIVED];
/// 复核动作：确认 / 驳回 / 退休 / 恢复
pub const REVIEW_ACTIONS: &[&str] = &["confirm", "reject", "archive", "restore"];
/// 单条记忆正文上限（与引擎 `MAX_AUTO_MEMORY_CHARS` 同量级，留出手写余量）
pub const MAX_CONTENT_CHARS: usize = 1000;
/// 结构化事实字段上限（subject / predicate / object）
pub const MAX_SUBJECT_CHARS: usize = 80;
pub const MAX_PREDICATE_CHARS: usize = 80;
pub const MAX_OBJECT_CHARS: usize = 300;
/// 单轮注入上限（与引擎 `MAX_MEMORY_ENTRIES` 一致）
pub const RETRIEVE_LIMIT: usize = 5;
/// 单轮注入的字符预算：宁可不塞满，也不让记忆占掉大段上下文
pub const INJECT_CHAR_BUDGET: usize = 1200;
/// 词面匹配的最低分（与引擎 `MEMORY_MIN_SCORE` 一致）
const MIN_SCORE: f64 = 0.25;
const CANDIDATE_LIMIT: usize = 500;
/// 导出 / 导入格式标识与版本；导入只接受同格式且不高于当前版本
pub const EXPORT_FORMAT: &str = "tongtian.memories";
pub const EXPORT_VERSION: i64 = 1;
/// 迁移包（.hmpkg）：zip 容器 + manifest + memories.json，sha256 只做完整性校验
pub const PACKAGE_FORMAT: &str = "tongtian.hmpkg";
pub const PACKAGE_VERSION: i64 = 1;
pub const PACKAGE_MAX_BYTES: usize = 32 * 1024 * 1024;
pub const PACKAGE_EXT: &str = "hmpkg";
/// 维护时每个归属默认保留的自动任务摘要条数
pub const COMPACT_KEEP_PER_SCOPE: i64 = 200;
/// 旧 MEMORY.md 结构化条目只导入一次；删除后不会被下次启动“复活”
const LEGACY_IMPORT_KEY: &str = "memories.legacy_imported";

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryRow {
    pub id: String,
    pub scope: String,
    /// 全局记忆为空串（前端按 scope 判断，不再区分 null/空串）
    pub workspace_id: String,
    pub kind: String,
    pub content: String,
    pub source: String,
    pub conv_id: Option<String>,
    pub task_id: Option<String>,
    /// 证据任务标题（任务已被清理时为 null）
    pub task_title: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub last_used_at: Option<String>,
    pub use_count: i64,
    pub occurrences: i64,
    /// active / pending / archived
    pub status: String,
    pub reviewed_at: Option<String>,
    /// 被哪条新事实取代（软退休时写入）
    pub superseded_by: Option<String>,
    /// 结构化事实：主体 / 属性 / 取值；普通摘要三者为空
    pub subject: String,
    pub predicate: String,
    pub object: String,
    /// 0.0–1.0；规则抽取低于人工录入
    pub confidence: f64,
}

#[derive(Debug, Clone)]
pub struct AddInput<'a> {
    pub content: &'a str,
    pub scope: Option<&'a str>,
    pub workspace_id: Option<&'a str>,
    pub kind: Option<&'a str>,
    pub source: Option<&'a str>,
    pub conv_id: Option<&'a str>,
    pub task_id: Option<&'a str>,
    /// 导入旧条目时保留原始 ISO 时间；新条目为 None（用当前时间）
    pub created_at: Option<&'a str>,
    /// active / pending（默认 active）；archived 仅导入时使用
    pub status: Option<&'a str>,
    pub subject: Option<&'a str>,
    pub predicate: Option<&'a str>,
    pub object: Option<&'a str>,
    pub confidence: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct AddOutcome {
    pub row: MemoryRow,
    /// true = 新插入；false = 与已有条目合并
    pub inserted: bool,
    /// 本次写入导致软退休的旧事实（通常一条）
    pub superseded: Vec<MemoryRow>,
}

#[derive(Debug, Clone)]
pub struct ListFilter<'a> {
    /// 指定时返回「全局 + 该工作区」；None = 不过滤
    pub workspace_id: Option<&'a str>,
    /// 指定时只看这一档（global / workspace）
    pub scope: Option<&'a str>,
    /// 指定时只看这一种状态（active / pending / archived）；None = 全部
    pub status: Option<&'a str>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone)]
pub struct RetrieveResult {
    pub rows: Vec<MemoryRow>,
    /// 当前可见范围内的记忆总数（不截断），用于界面如实说明
    pub total: usize,
}

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EntityPage {
    pub subject: String,
    pub count: usize,
    pub kinds: Vec<String>,
    pub latest: String,
    pub entries: Vec<MemoryRow>,
}

/// `MEMORY.md` 对账结果：只导入文件里有、库里没有的条目，不改文件、不删库
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DocSyncResult {
    pub file_count: usize,
    pub imported: usize,
    pub existing: usize,
    pub skipped: usize,
}

#[derive(Debug, Clone)]
pub enum PersistOutcome {
    Inserted(MemoryRow),
    Merged(MemoryRow),
    /// 新事实生效，旧事实软退休
    Superseded(MemoryRow, MemoryRow),
    Skipped(String),
}

impl PersistOutcome {
    pub fn row(&self) -> Option<&MemoryRow> {
        match self {
            Self::Inserted(row) | Self::Merged(row) | Self::Superseded(row, _) => Some(row),
            Self::Skipped(_) => None,
        }
    }
}

// ── 归一与校验 ─────────────────────────────────────────────

fn normalize_content(content: &str) -> String {
    content.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn content_hash(content: &str) -> String {
    let digest = Sha256::digest(content.as_bytes());
    let mut out = String::with_capacity(64);
    for byte in digest.iter() {
        use std::fmt::Write as _;
        let _ = write!(&mut out, "{byte:02x}");
    }
    out
}

fn normalize_scope(
    scope: Option<&str>,
    workspace_id: Option<&str>,
) -> Result<(String, String), String> {
    let ws_raw = workspace_id.map(str::trim).unwrap_or("");
    let raw = scope.map(str::trim).unwrap_or("");
    let (scope, ws) = match raw {
        "" => {
            if ws_raw.is_empty() {
                (SCOPE_GLOBAL, "")
            } else {
                (SCOPE_WORKSPACE, ws_raw)
            }
        }
        SCOPE_GLOBAL => (SCOPE_GLOBAL, ""),
        SCOPE_WORKSPACE => (SCOPE_WORKSPACE, ws_raw),
        other => {
            return Err(format!(
                "MEMORY_INVALID_SCOPE: 记忆归属「{other}」非法，仅支持 global / workspace"
            ))
        }
    };
    if scope == SCOPE_WORKSPACE {
        let ok = !ws.is_empty()
            && ws.len() <= 64
            && ws
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_');
        if !ok {
            return Err(
                "MEMORY_INVALID_SCOPE: 项目记忆必须带合法工作区 id（小写字母/数字/-/_，64 字符以内）"
                    .to_string(),
            );
        }
    }
    Ok((scope.to_string(), ws.to_string()))
}

fn normalize_kind(kind: Option<&str>) -> Result<String, String> {
    let raw = kind.map(str::trim).unwrap_or("");
    if raw.is_empty() {
        return Ok("fact".to_string());
    }
    if !KINDS.contains(&raw) {
        return Err(format!(
            "MEMORY_INVALID_KIND: 记忆类型「{raw}」非法，仅支持 {}",
            KINDS.join(" / ")
        ));
    }
    Ok(raw.to_string())
}

fn normalize_source(source: Option<&str>) -> String {
    let raw = source.map(str::trim).unwrap_or("");
    if SOURCES.contains(&raw) {
        raw.to_string()
    } else {
        "manual".to_string()
    }
}

fn normalize_status(status: Option<&str>) -> Result<String, String> {
    let raw = status.map(str::trim).unwrap_or("");
    if raw.is_empty() {
        return Ok(STATUS_ACTIVE.to_string());
    }
    if !STATUSES.contains(&raw) {
        return Err(format!(
            "MEMORY_STATUS_INVALID: 记忆状态「{raw}」非法，仅支持 {}",
            STATUSES.join(" / ")
        ));
    }
    Ok(raw.to_string())
}

fn normalize_fact_field(raw: Option<&str>, max: usize, label: &str) -> Result<String, String> {
    let text = raw.map(str::trim).unwrap_or("");
    let count = text.chars().count();
    if count > max {
        return Err(format!(
            "MEMORY_INVALID_FACT: 事实字段「{label}」最多 {max} 字（当前 {count} 字）"
        ));
    }
    Ok(text.to_string())
}

// ── 行映射 ─────────────────────────────────────────────────

const SELECT_COLUMNS: &str = "m.id, m.scope, m.workspace_id, m.kind, m.content, m.source, \
     m.conv_id, m.task_id, t.title, m.created_at, m.updated_at, m.last_used_at, \
     m.use_count, m.occurrences, m.status, m.reviewed_at, m.superseded_by, \
     m.subject, m.predicate, m.object, m.confidence";

fn row_from(r: &rusqlite::Row<'_>) -> rusqlite::Result<MemoryRow> {
    Ok(MemoryRow {
        id: r.get(0)?,
        scope: r.get(1)?,
        workspace_id: r.get(2)?,
        kind: r.get(3)?,
        content: r.get(4)?,
        source: r.get(5)?,
        conv_id: r.get(6)?,
        task_id: r.get(7)?,
        task_title: r.get(8)?,
        created_at: r.get(9)?,
        updated_at: r.get(10)?,
        last_used_at: r.get(11)?,
        use_count: r.get(12)?,
        occurrences: r.get(13)?,
        status: r.get(14)?,
        reviewed_at: r.get(15)?,
        superseded_by: r.get(16)?,
        subject: r.get(17)?,
        predicate: r.get(18)?,
        object: r.get(19)?,
        confidence: r.get(20)?,
    })
}

fn get_by_id(conn: &rusqlite::Connection, id: &str) -> Result<MemoryRow, String> {
    conn.query_row(
        &format!(
            "SELECT {SELECT_COLUMNS} FROM memories m \
             LEFT JOIN tasks t ON t.task_id = m.task_id WHERE m.id = ?1"
        ),
        params![id],
        row_from,
    )
    .optional()
    .map_err(|e| format!("MEMORY_LOAD_FAILED: {e}"))?
    .ok_or_else(|| format!("MEMORY_NOT_FOUND: 记忆条目「{id}」已不存在，可能已被删除"))
}

fn get_by_scope_hash(
    conn: &rusqlite::Connection,
    scope: &str,
    workspace_id: &str,
    hash: &str,
) -> Result<Option<MemoryRow>, String> {
    conn.query_row(
        &format!(
            "SELECT {SELECT_COLUMNS} FROM memories m \
             LEFT JOIN tasks t ON t.task_id = m.task_id \
             WHERE m.scope = ?1 AND m.workspace_id = ?2 AND m.content_hash = ?3"
        ),
        params![scope, workspace_id, hash],
        row_from,
    )
    .optional()
    .map_err(|e| format!("MEMORY_LOAD_FAILED: {e}"))
}

/// 同一归属、同一 (subject, predicate) 但取值不同的活跃事实 → 软退休。
/// 不物理删除：旧行的证据与 `superseded_by` 都保留，界面可按「已归档」回溯。
fn resolve_conflicts(
    conn: &rusqlite::Connection,
    scope: &str,
    workspace_id: &str,
    subject: &str,
    predicate: &str,
    object: &str,
    keep_id: &str,
    now_iso: &str,
) -> Result<Vec<MemoryRow>, String> {
    if subject.is_empty() || predicate.is_empty() || object.is_empty() {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT id FROM memories \
             WHERE scope = ?1 AND workspace_id = ?2 AND status = 'active' \
               AND lower(subject) = lower(?3) AND lower(predicate) = lower(?4) \
               AND object <> ?5 AND id <> ?6",
        )
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
    let ids = stmt
        .query_map(
            params![scope, workspace_id, subject, predicate, object, keep_id],
            |r| r.get::<_, String>(0),
        )
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
    drop(stmt);
    let mut retired = Vec::new();
    for id in ids {
        conn.execute(
            "UPDATE memories SET status = 'archived', superseded_by = ?2, \
             reviewed_at = ?3, updated_at = ?3 WHERE id = ?1",
            params![id, keep_id, now_iso],
        )
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
        retired.push(get_by_id(conn, &id)?);
    }
    Ok(retired)
}

// ── 写 ─────────────────────────────────────────────────────

/// 新增一条记忆；同归属同内容已存在时合并（出现次数 +1，来源按 manual/reflection 优先）。
/// 结构化事实与已有活跃事实冲突时，旧事实软退休并记 `superseded_by`。
pub fn add(store: &Store, input: AddInput<'_>, now_iso: &str) -> Result<AddOutcome, String> {
    let content = normalize_content(input.content);
    if content.is_empty() {
        return Err("MEMORY_CONTENT_EMPTY: 记忆条目内容不能为空".to_string());
    }
    if content.chars().count() > MAX_CONTENT_CHARS {
        return Err(format!(
            "MEMORY_CONTENT_TOO_LONG: 记忆条目最多 {MAX_CONTENT_CHARS} 字（当前 {} 字）",
            content.chars().count()
        ));
    }
    let (scope, workspace_id) = normalize_scope(input.scope, input.workspace_id)?;
    let kind = normalize_kind(input.kind)?;
    let source = normalize_source(input.source);
    let status = normalize_status(input.status)?;
    let subject = normalize_fact_field(input.subject, MAX_SUBJECT_CHARS, "主体")?;
    let predicate = normalize_fact_field(input.predicate, MAX_PREDICATE_CHARS, "属性")?;
    let object = normalize_fact_field(input.object, MAX_OBJECT_CHARS, "取值")?;
    let confidence = input
        .confidence
        .filter(|v| v.is_finite())
        .map(|v| v.clamp(0.0, 1.0))
        .unwrap_or(1.0);
    let hash = content_hash(&content);
    let created_at = input
        .created_at
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(now_iso);
    let conv_id = input
        .conv_id
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    let task_id = input
        .task_id
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);

    store.with_conn(|conn| {
        let existing: Option<String> = conn
            .query_row(
                "SELECT id FROM memories WHERE scope = ?1 AND workspace_id = ?2 AND content_hash = ?3",
                params![scope, workspace_id, hash],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
        if let Some(id) = existing {
            // 合并：确认动作可把待审/已归档的原条目恢复为 active；
            // 待审写入不覆盖已生效条目的状态，避免「再跑一次任务」把用户确认过的记忆打回待审。
            conn.execute(
                "UPDATE memories SET \
                   occurrences = occurrences + 1, \
                   updated_at = ?2, \
                   source = CASE WHEN source = 'auto' THEN ?3 ELSE source END, \
                   status = CASE \
                              WHEN ?4 = 'archived' THEN 'archived' \
                              WHEN ?4 = 'active' THEN 'active' \
                              ELSE status END, \
                   reviewed_at = CASE WHEN ?4 IN ('active', 'archived') THEN ?5 ELSE reviewed_at END, \
                   superseded_by = CASE WHEN ?4 = 'active' THEN NULL ELSE superseded_by END, \
                   subject = CASE WHEN ?6 = '' THEN subject ELSE ?6 END, \
                   predicate = CASE WHEN ?7 = '' THEN predicate ELSE ?7 END, \
                   object = CASE WHEN ?8 = '' THEN object ELSE ?8 END, \
                   confidence = CASE WHEN ?6 = '' AND ?7 = '' AND ?8 = '' THEN confidence ELSE ?9 END \
                 WHERE id = ?1",
                params![id, now_iso, source, status, now_iso, subject, predicate, object, confidence],
            )
            .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
            let mut row = get_by_id(conn, &id)?;
            // 合并也可能让一条旧事实重新生效（例如再次写入已被取代的取值），
            // 此时同样要退休当前的冲突事实，保持「同属性只有一条 active」。
            let superseded = if status == STATUS_ACTIVE {
                resolve_conflicts(
                    conn,
                    &row.scope,
                    &row.workspace_id,
                    &row.subject,
                    &row.predicate,
                    &row.object,
                    &id,
                    now_iso,
                )?
            } else {
                Vec::new()
            };
            if !superseded.is_empty() {
                row = get_by_id(conn, &id)?;
            }
            return Ok(AddOutcome {
                row,
                inserted: false,
                superseded,
            });
        }
        let id = format!("mem-{}", uuid::Uuid::new_v4());
        conn.execute(
            "INSERT INTO memories \
             (id, scope, workspace_id, kind, content, content_hash, source, conv_id, task_id, \
              created_at, updated_at, last_used_at, use_count, occurrences, status, reviewed_at, \
              superseded_by, subject, predicate, object, confidence) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, NULL, 0, 1, ?12, NULL, \
                     NULL, ?13, ?14, ?15, ?16)",
            params![
                id,
                scope,
                workspace_id,
                kind,
                content,
                hash,
                source,
                conv_id,
                task_id,
                created_at,
                now_iso,
                status,
                subject,
                predicate,
                object,
                confidence,
            ],
        )
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
        // 只有确认生效的事实才触发冲突退休；待审条目等确认时再处理
        let superseded = if status == STATUS_ACTIVE {
            resolve_conflicts(
                conn,
                &scope,
                &workspace_id,
                &subject,
                &predicate,
                &object,
                &id,
                now_iso,
            )?
        } else {
            Vec::new()
        };
        Ok(AddOutcome {
            row: get_by_id(conn, &id)?,
            inserted: true,
            superseded,
        })
    })
}

/// 复核动作：确认待审、驳回候选、手动退休、恢复归档。
/// 只改状态，不物理删除；被新事实取代的旧行不允许直接恢复（先确认新事实去留）。
pub fn set_status(
    store: &Store,
    id: &str,
    action: &str,
    now_iso: &str,
) -> Result<MemoryRow, String> {
    let target = id.trim();
    if target.is_empty() {
        return Err("MEMORY_NOT_FOUND: 记忆条目标识不能为空".to_string());
    }
    if !REVIEW_ACTIONS.contains(&action) {
        return Err(format!(
            "MEMORY_STATUS_INVALID: 复核动作「{action}」非法，仅支持 {}",
            REVIEW_ACTIONS.join(" / ")
        ));
    }
    store.with_conn(|conn| {
        let row = get_by_id(conn, target)?;
        if row.superseded_by.is_some() && matches!(action, "confirm" | "restore") {
            return Err(format!(
                "MEMORY_SUPERSEDED: 这条记忆已被更新的同类事实取代，不能直接恢复；\
                 如需保留请新增一条并确认"
            ));
        }
        let next = match action {
            "confirm" => {
                if !matches!(row.status.as_str(), STATUS_PENDING | STATUS_ARCHIVED) {
                    return Err(format!(
                        "MEMORY_STATUS_INVALID: 只有待审或已归档的记忆可确认（当前 {}）",
                        row.status
                    ));
                }
                STATUS_ACTIVE
            }
            "reject" => {
                if row.status != STATUS_PENDING {
                    return Err(format!(
                        "MEMORY_STATUS_INVALID: 只有待审记忆可驳回（当前 {}）",
                        row.status
                    ));
                }
                STATUS_ARCHIVED
            }
            "archive" => {
                if row.status == STATUS_ARCHIVED {
                    return Err("MEMORY_STATUS_INVALID: 这条记忆已经是归档状态".to_string());
                }
                STATUS_ARCHIVED
            }
            _ => {
                if row.status != STATUS_ARCHIVED {
                    return Err(format!(
                        "MEMORY_STATUS_INVALID: 只有已归档记忆可恢复（当前 {}）",
                        row.status
                    ));
                }
                STATUS_ACTIVE
            }
        };
        conn.execute(
            "UPDATE memories SET status = ?2, reviewed_at = ?3, updated_at = ?3, \
             superseded_by = CASE WHEN ?2 = 'active' THEN NULL ELSE superseded_by END \
             WHERE id = ?1",
            params![target, next, now_iso],
        )
        .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
        if next == STATUS_ACTIVE {
            resolve_conflicts(
                conn,
                &row.scope,
                &row.workspace_id,
                &row.subject,
                &row.predicate,
                &row.object,
                target,
                now_iso,
            )?;
        }
        get_by_id(conn, target)
    })
}

/// 删除用户点名的那一条；未命中如实报错，不静默成功
pub fn delete(store: &Store, id: &str) -> Result<bool, String> {
    let target = id.trim();
    if target.is_empty() {
        return Err("MEMORY_NOT_FOUND: 记忆条目标识不能为空".to_string());
    }
    store.with_conn(|conn| {
        let affected = conn
            .execute("DELETE FROM memories WHERE id = ?1", params![target])
            .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
        if affected == 0 {
            return Err(format!(
                "MEMORY_NOT_FOUND: 记忆条目「{target}」已不存在，可能已被删除"
            ));
        }
        Ok(true)
    })
}

// ── 读 ─────────────────────────────────────────────────────

/// 列出记忆（新→旧）。workspace 给定时包含全局条目 + 该项目条目。
pub fn list(store: &Store, filter: ListFilter<'_>) -> Result<Vec<MemoryRow>, String> {
    let ws = filter.workspace_id.map(str::trim).unwrap_or("");
    let scope = filter.scope.map(str::trim).unwrap_or("");
    let status = filter.status.map(str::trim).unwrap_or("");
    if !scope.is_empty() && scope != SCOPE_GLOBAL && scope != SCOPE_WORKSPACE {
        return Err(format!(
            "MEMORY_INVALID_SCOPE: 记忆归属「{scope}」非法，仅支持 global / workspace"
        ));
    }
    if !status.is_empty() && !STATUSES.contains(&status) {
        return Err(format!(
            "MEMORY_STATUS_INVALID: 记忆状态「{status}」非法，仅支持 {}",
            STATUSES.join(" / ")
        ));
    }
    let limit = filter.limit.unwrap_or(CANDIDATE_LIMIT) as i64;
    store.with_conn(|conn| {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {SELECT_COLUMNS} FROM memories m \
                 LEFT JOIN tasks t ON t.task_id = m.task_id \
                 WHERE (?1 = '' OR m.scope = 'global' OR m.workspace_id = ?1) \
                   AND (?2 = '' OR m.scope = ?2) \
                   AND (?3 = '' OR m.status = ?3) \
                 ORDER BY m.updated_at DESC, m.id DESC LIMIT ?4"
            ))
            .map_err(|e| format!("MEMORY_LIST_FAILED: {e}"))?;
        let rows = stmt
            .query_map(params![ws, scope, status, limit], row_from)
            .map_err(|e| format!("MEMORY_LIST_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("MEMORY_LIST_FAILED: {e}"))
    })
}

/// 按本轮目标检索记忆并记录引用次数。
///
/// 只取 `active`；查询为空时退回最近条目；有目标但一条都不命中时返回空——
/// 宁可不注入，也不塞无关记忆。结果受条数与字符预算双重约束。
pub fn retrieve(
    store: &Store,
    workspace_id: &str,
    query: &str,
    limit: Option<usize>,
    now_iso: &str,
) -> Result<RetrieveResult, String> {
    let candidates = list(
        store,
        ListFilter {
            workspace_id: Some(workspace_id),
            scope: None,
            status: Some(STATUS_ACTIVE),
            limit: Some(CANDIDATE_LIMIT),
        },
    )?;
    let total = candidates.len();
    let limit = limit.unwrap_or(RETRIEVE_LIMIT).max(1);
    let query_tokens = tokens(query);
    let mut scored: Vec<(f64, MemoryRow)> = if query_tokens.is_empty() {
        candidates.into_iter().map(|row| (0.0, row)).collect()
    } else {
        candidates
            .into_iter()
            .filter_map(|row| {
                let hits = query_tokens.intersection(&tokens(&row.content)).count();
                if hits == 0 {
                    return None;
                }
                let score = hits as f64 / query_tokens.len() as f64 + source_boost(&row.source);
                if score < MIN_SCORE {
                    return None;
                }
                Some((score, row))
            })
            .collect()
    };
    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| b.1.updated_at.cmp(&a.1.updated_at))
    });
    let mut picked: Vec<MemoryRow> = Vec::new();
    let mut used_chars = 0usize;
    for (_, row) in scored {
        if picked.len() >= limit {
            break;
        }
        let chars = row.content.chars().count();
        // 预算允许超一条：第一条再长也要有上下文，后续条目按预算截断
        if !picked.is_empty() && used_chars + chars > INJECT_CHAR_BUDGET {
            continue;
        }
        used_chars += chars;
        picked.push(row);
    }

    if !picked.is_empty() {
        store.with_conn(|conn| {
            for row in &picked {
                conn.execute(
                    "UPDATE memories SET use_count = use_count + 1, last_used_at = ?2 WHERE id = ?1",
                    params![row.id, now_iso],
                )
                .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
            }
            Ok(())
        })?;
        for row in &mut picked {
            row.use_count += 1;
            row.last_used_at = Some(now_iso.to_string());
        }
    }
    Ok(RetrieveResult { rows: picked, total })
}

/// 实体页：按 `subject` 聚合活跃事实，不另建实体表。
/// 同名主体跨项目合并会串味，因此沿用调用方传入的 workspace 可见范围。
pub fn entities(store: &Store, workspace_id: &str) -> Result<Vec<EntityPage>, String> {
    let rows = list(
        store,
        ListFilter {
            workspace_id: Some(workspace_id),
            scope: None,
            status: Some(STATUS_ACTIVE),
            limit: Some(CANDIDATE_LIMIT),
        },
    )?;
    let mut groups: BTreeMap<String, Vec<MemoryRow>> = BTreeMap::new();
    for row in rows {
        let subject = row.subject.trim();
        if subject.is_empty() {
            continue;
        }
        groups.entry(subject.to_string()).or_default().push(row);
    }
    let mut pages: Vec<EntityPage> = groups
        .into_iter()
        .map(|(subject, mut entries)| {
            entries.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
            let mut kinds: Vec<String> = entries.iter().map(|r| r.kind.clone()).collect();
            kinds.sort();
            kinds.dedup();
            let latest = entries
                .first()
                .map(|r| r.updated_at.clone())
                .unwrap_or_default();
            let count = entries.len();
            entries.truncate(5);
            EntityPage {
                subject,
                count,
                kinds,
                latest,
                entries,
            }
        })
        .collect();
    pages.sort_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| b.latest.cmp(&a.latest))
    });
    Ok(pages)
}

/// 引擎 deliver 事件里的候选记忆 → 桌面库（归属沿用本轮任务的工作区与会话）。
/// `review=true` 时自动候选先落 pending，确认后才参与召回。
pub fn persist_candidate(
    store: &Store,
    task_id: &str,
    candidate: &Value,
    now_iso: &str,
    review: bool,
) -> Result<PersistOutcome, String> {
    let content = candidate
        .get("content")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if content.trim().is_empty() {
        return Ok(PersistOutcome::Skipped("候选记忆内容为空".to_string()));
    }
    let kind = candidate.get("kind").and_then(|v| v.as_str());
    let source = candidate.get("source").and_then(|v| v.as_str());
    let status = candidate
        .get("status")
        .and_then(|v| v.as_str())
        .filter(|s| STATUSES.contains(s))
        .map(str::to_string)
        .unwrap_or_else(|| {
            if review && source == Some("auto") {
                STATUS_PENDING.to_string()
            } else {
                STATUS_ACTIVE.to_string()
            }
        });
    let subject = candidate.get("subject").and_then(|v| v.as_str());
    let predicate = candidate.get("predicate").and_then(|v| v.as_str());
    let object = candidate.get("object").and_then(|v| v.as_str());
    let confidence = candidate.get("confidence").and_then(|v| v.as_f64());
    let (conv_id, workspace_id) = store
        .with_conn(|conn| crate::store::messages::task_binding(conn, task_id))?
        .map(|(conv, _turn, ws)| (conv, ws))
        .unwrap_or_else(|| (task_id.to_string(), "default".to_string()));
    // 断线重放会把同一条 deliver 再送一遍：同一任务已贡献过这条内容时不再累加
    let normalized = normalize_content(content);
    let hash = content_hash(&normalized);
    if let Some(row) = store.with_conn(|conn| {
        get_by_scope_hash(conn, SCOPE_WORKSPACE, &workspace_id, &hash)
    })? {
        if row.task_id.as_deref() == Some(task_id) {
            return Ok(PersistOutcome::Merged(row));
        }
    }
    let outcome = add(
        store,
        AddInput {
            content,
            scope: Some(SCOPE_WORKSPACE),
            workspace_id: Some(&workspace_id),
            kind,
            source,
            conv_id: Some(&conv_id),
            task_id: Some(task_id),
            created_at: None,
            status: Some(&status),
            subject,
            predicate,
            object,
            confidence,
        },
        now_iso,
    )?;
    if let Some(old) = outcome.superseded.into_iter().next() {
        return Ok(PersistOutcome::Superseded(outcome.row, old));
    }
    Ok(if outcome.inserted {
        PersistOutcome::Inserted(outcome.row)
    } else {
        PersistOutcome::Merged(outcome.row)
    })
}

/// 旧 MEMORY.md 的结构化行只导入一次（文件本身不删不改，手写正文原样保留）。
/// 返回首次导入的条数；已导入过或文件不存在时为 0。
pub fn import_legacy(
    store: &Store,
    awareness_dir: &Path,
    now_iso: &str,
) -> Result<usize, String> {
    if crate::store::settings::get(store, LEGACY_IMPORT_KEY)?.as_deref() == Some("1") {
        return Ok(0);
    }
    // 不在启动阶段凭空创建意识文件：文件不存在就只打标记
    if !awareness_dir.join("MEMORY.md").exists() {
        crate::store::settings::set(store, LEGACY_IMPORT_KEY, "1")?;
        return Ok(0);
    }
    let entries = crate::awareness::list_memories(awareness_dir)?;
    let mut imported = 0usize;
    for entry in entries {
        let kind = if entry.source == "auto" { "task" } else { "fact" };
        let outcome = add(
            store,
            AddInput {
                content: &entry.content,
                scope: Some(SCOPE_GLOBAL),
                workspace_id: None,
                kind: Some(kind),
                source: Some(&entry.source),
                conv_id: None,
                task_id: None,
                created_at: Some(&entry.created_at),
                status: Some(STATUS_ACTIVE),
                subject: None,
                predicate: None,
                object: None,
                confidence: None,
            },
            now_iso,
        )?;
        if outcome.inserted {
            imported += 1;
        }
    }
    crate::store::settings::set(store, LEGACY_IMPORT_KEY, "1")?;
    Ok(imported)
}

/// 人工对账：把 `MEMORY.md` 里尚未入库的结构化行导入为全局记忆。
/// 与启动时的一次性导入不同，这里可重复执行且如实返回「文件条数 / 新导入 / 已存在」；
/// 文件本身不删不改，库里的条目也不会因为文件删行而被删除。
pub fn sync_doc(
    store: &Store,
    awareness_dir: &Path,
    now_iso: &str,
) -> Result<DocSyncResult, String> {
    if !awareness_dir.join("MEMORY.md").exists() {
        return Ok(DocSyncResult {
            file_count: 0,
            imported: 0,
            existing: 0,
            skipped: 0,
        });
    }
    let entries = crate::awareness::list_memories(awareness_dir)?;
    let mut result = DocSyncResult {
        file_count: entries.len(),
        imported: 0,
        existing: 0,
        skipped: 0,
    };
    for entry in entries {
        let kind = if entry.source == "auto" { "task" } else { "fact" };
        match add(
            store,
            AddInput {
                content: &entry.content,
                scope: Some(SCOPE_GLOBAL),
                workspace_id: None,
                kind: Some(kind),
                source: Some(&entry.source),
                conv_id: None,
                task_id: None,
                created_at: Some(&entry.created_at),
                status: Some(STATUS_ACTIVE),
                subject: None,
                predicate: None,
                object: None,
                confidence: None,
            },
            now_iso,
        ) {
            Ok(outcome) if outcome.inserted => result.imported += 1,
            Ok(_) => result.existing += 1,
            Err(_) => result.skipped += 1,
        }
    }
    Ok(result)
}

// ── 导出 / 导入 / 维护 ─────────────────────────────────────

/// 导出为可携带 JSON（含归档与待审行，导入后可保持复核状态）
pub fn export_value(store: &Store, now_iso: &str) -> Result<Value, String> {
    let rows = list(
        store,
        ListFilter {
            workspace_id: None,
            scope: None,
            status: None,
            limit: Some(1_000_000),
        },
    )?;
    Ok(json!({
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": now_iso,
        "count": rows.len(),
        "entries": rows.iter().map(row_json).collect::<Vec<_>>(),
    }))
}

fn row_json(row: &MemoryRow) -> Value {
    json!({
        "id": row.id,
        "scope": row.scope,
        "workspaceId": row.workspace_id,
        "kind": row.kind,
        "content": row.content,
        "source": row.source,
        "convId": row.conv_id,
        "taskId": row.task_id,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "lastUsedAt": row.last_used_at,
        "useCount": row.use_count,
        "occurrences": row.occurrences,
        "status": row.status,
        "reviewedAt": row.reviewed_at,
        "supersededBy": row.superseded_by,
        "subject": row.subject,
        "predicate": row.predicate,
        "object": row.object,
        "confidence": row.confidence,
    })
}

/// 导出到目录（默认 `{TONGTIAN_HOME}/exports`），返回 (文件路径, 条数)
pub fn export_to(store: &Store, dir: &Path, now_iso: &str) -> Result<(String, usize), String> {
    let value = export_value(store, now_iso)?;
    let count = value.get("count").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
    std::fs::create_dir_all(dir).map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    let stamp = now_iso.replace(':', "-").replace('.', "-");
    let path = dir.join(format!("memories-{stamp}.json"));
    let body = serde_json::to_string_pretty(&value)
        .map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    std::fs::write(&path, body).map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    Ok((path.to_string_lossy().to_string(), count))
}

/// 从 JSON 导入。写入前先把当前全量导出到备份目录；备份失败即中止，不做半截导入。
/// 按 (scope, workspace, content_hash) 合并，已有同内容条目只累加出现次数，不改 id。
pub fn import_json(
    store: &Store,
    raw: &str,
    now_iso: &str,
    backup_dir: &Path,
) -> Result<Value, String> {
    let parsed: Value = serde_json::from_str(raw)
        .map_err(|e| format!("MEMORY_IMPORT_INVALID: 不是合法 JSON：{e}"))?;
    let entries = if let Some(arr) = parsed.as_array() {
        arr.clone()
    } else {
        let format = parsed.get("format").and_then(|v| v.as_str()).unwrap_or("");
        if !format.is_empty() && format != EXPORT_FORMAT {
            return Err(format!(
                "MEMORY_IMPORT_INVALID: 不是通天记忆导出文件（format={format}）"
            ));
        }
        let version = parsed.get("version").and_then(|v| v.as_i64()).unwrap_or(0);
        if version > EXPORT_VERSION {
            return Err(format!(
                "MEMORY_IMPORT_INVALID: 导出文件版本 {version} 高于当前支持的 {EXPORT_VERSION}，请先升级应用"
            ));
        }
        parsed
            .get("entries")
            .and_then(|v| v.as_array())
            .cloned()
            .ok_or_else(|| "MEMORY_IMPORT_INVALID: 文件里没有 entries 数组".to_string())?
    };
    if entries.len() > 10_000 {
        return Err("MEMORY_IMPORT_INVALID: 单次最多导入 10000 条记忆".to_string());
    }
    // 维护前备份：先落盘再动库；备份失败不继续
    let (backup_path, _) = export_to(store, backup_dir, now_iso)?;

    let mut inserted = 0usize;
    let mut merged = 0usize;
    let mut skipped = 0usize;
    for entry in entries.iter().take(10_000) {
        let content = entry.get("content").and_then(|v| v.as_str()).unwrap_or("");
        if content.trim().is_empty() {
            skipped += 1;
            continue;
        }
        let outcome = add(
            store,
            AddInput {
                content,
                scope: entry.get("scope").and_then(|v| v.as_str()),
                workspace_id: entry.get("workspaceId").and_then(|v| v.as_str()),
                kind: entry.get("kind").and_then(|v| v.as_str()),
                source: entry.get("source").and_then(|v| v.as_str()),
                conv_id: entry.get("convId").and_then(|v| v.as_str()),
                task_id: entry.get("taskId").and_then(|v| v.as_str()),
                created_at: entry.get("createdAt").and_then(|v| v.as_str()),
                status: entry.get("status").and_then(|v| v.as_str()),
                subject: entry.get("subject").and_then(|v| v.as_str()),
                predicate: entry.get("predicate").and_then(|v| v.as_str()),
                object: entry.get("object").and_then(|v| v.as_str()),
                confidence: entry.get("confidence").and_then(|v| v.as_f64()),
            },
            now_iso,
        );
        match outcome {
            Ok(o) if o.inserted => inserted += 1,
            Ok(_) => merged += 1,
            Err(_) => skipped += 1,
        }
    }
    Ok(json!({
        "inserted": inserted,
        "merged": merged,
        "skipped": skipped,
        "total": entries.len(),
        "backupPath": backup_path,
    }))
}

// ── 迁移包（.hmpkg） ─────────────────────────────────────────
//
// 容器是 zip（仅 STORE，不压缩）：`manifest.json` 记格式 / 版本 / 条数 / sha256，
// `memories.json` 与 JSON 导出的信封完全一致。导入仍走 `import_json`，
// 因此“导入前强制备份、按内容哈希去重、逐条报告”的口径完全复用。

const SIG_LOCAL: u32 = 0x0403_4B50;
const SIG_CENTRAL: u32 = 0x0201_4B50;
const SIG_EOCD: u32 = 0x0605_4B50;

/// 写一个最小 STORE zip（UTF-8 文件名）。只用于导出迁移包，不做压缩。
fn build_store_zip(files: &[(&str, &[u8])]) -> Result<Vec<u8>, String> {
    if files.len() > u16::MAX as usize {
        return Err("MEMORY_EXPORT_FAILED: 迁移包部件过多".to_string());
    }
    let mut out: Vec<u8> = Vec::new();
    let mut central: Vec<(String, u32, u32, u32)> = Vec::new();
    for (name, data) in files {
        if data.len() > u32::MAX as usize || out.len() > u32::MAX as usize {
            return Err("MEMORY_EXPORT_FAILED: 迁移包超过 4GB，不支持".to_string());
        }
        let crc = crc32fast::hash(data);
        let offset = out.len() as u32;
        out.extend_from_slice(&SIG_LOCAL.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&0x0800u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0x0021u16.to_le_bytes());
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&(name.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(data);
        central.push((name.to_string(), crc, data.len() as u32, offset));
    }
    let cd_start = out.len() as u32;
    for (name, crc, size, offset) in &central {
        out.extend_from_slice(&SIG_CENTRAL.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&0x0800u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0x0021u16.to_le_bytes());
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&(name.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&offset.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
    }
    let cd_size = out.len() as u32 - cd_start;
    out.extend_from_slice(&SIG_EOCD.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&(central.len() as u16).to_le_bytes());
    out.extend_from_slice(&(central.len() as u16).to_le_bytes());
    out.extend_from_slice(&cd_size.to_le_bytes());
    out.extend_from_slice(&cd_start.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    Ok(out)
}

fn package_part(
    bytes: &[u8],
    entries: &[crate::file::office::ZipEntry],
    name: &str,
) -> Result<Option<Vec<u8>>, String> {
    let lower = name.to_ascii_lowercase();
    let Some(entry) = entries
        .iter()
        .find(|e| e.name.to_ascii_lowercase() == lower)
    else {
        return Ok(None);
    };
    crate::file::office::zip_read(bytes, entry)
        .map(Some)
        .map_err(|e| format!("MEMORY_IMPORT_INVALID: 迁移包部件 {name} 读取失败（{e}）"))
}

/// 导出迁移包到目录（默认 `{TONGTIAN_HOME}/exports`），返回 (路径, 条数)
pub fn export_package(
    store: &Store,
    dir: &Path,
    now_iso: &str,
) -> Result<(String, usize), String> {
    let value = export_value(store, now_iso)?;
    let count = value.get("count").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
    let memories = serde_json::to_vec_pretty(&value)
        .map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    let checksum = content_hash(
        std::str::from_utf8(&memories)
            .map_err(|e| format!("MEMORY_EXPORT_FAILED: 导出内容不是 UTF-8：{e}"))?,
    );
    let manifest = json!({
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "createdAt": now_iso,
        "app": "tongtian-office",
        "count": count,
        "checksum": format!("sha256:{checksum}"),
    });
    let manifest_bytes = serde_json::to_vec_pretty(&manifest)
        .map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    let files: Vec<(&str, &[u8])> = vec![
        ("manifest.json", manifest_bytes.as_slice()),
        ("memories.json", memories.as_slice()),
    ];
    let package = build_store_zip(&files)?;
    std::fs::create_dir_all(dir).map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    let stamp = now_iso.replace(':', "-").replace('.', "-");
    let path = dir.join(format!("memories-{stamp}.{PACKAGE_EXT}"));
    std::fs::write(&path, package).map_err(|e| format!("MEMORY_EXPORT_FAILED: {e}"))?;
    Ok((path.to_string_lossy().to_string(), count))
}

/// 从迁移包导入：校验格式 / 版本 / 完整性后复用 `import_json`（含强制备份）。
pub fn import_package(
    store: &Store,
    bytes: &[u8],
    now_iso: &str,
    backup_dir: &Path,
) -> Result<Value, String> {
    if bytes.len() > PACKAGE_MAX_BYTES {
        return Err("MEMORY_IMPORT_INVALID: 迁移包超过 32MB".to_string());
    }
    let entries = crate::file::office::zip_entries(bytes)
        .map_err(|e| format!("MEMORY_IMPORT_INVALID: 不是有效的 .hmpkg 迁移包（{e}）"))?;
    let manifest_raw = package_part(bytes, &entries, "manifest.json")?
        .ok_or_else(|| "MEMORY_IMPORT_INVALID: 迁移包缺少 manifest.json".to_string())?;
    let manifest: Value = serde_json::from_slice(&manifest_raw)
        .map_err(|e| format!("MEMORY_IMPORT_INVALID: manifest.json 不是合法 JSON：{e}"))?;
    let format = manifest.get("format").and_then(|v| v.as_str()).unwrap_or("");
    if format != PACKAGE_FORMAT {
        return Err(format!(
            "MEMORY_IMPORT_INVALID: 不是通天记忆迁移包（format={format}）"
        ));
    }
    let version = manifest
        .get("version")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    if version > PACKAGE_VERSION {
        return Err(format!(
            "MEMORY_IMPORT_INVALID: 迁移包版本 {version} 高于当前支持的 {PACKAGE_VERSION}，请先升级应用"
        ));
    }
    let memories_raw = package_part(bytes, &entries, "memories.json")?
        .ok_or_else(|| "MEMORY_IMPORT_INVALID: 迁移包缺少 memories.json".to_string())?;
    let memories_text = std::str::from_utf8(&memories_raw)
        .map_err(|e| format!("MEMORY_IMPORT_INVALID: memories.json 不是 UTF-8：{e}"))?;
    let checksum = manifest
        .get("checksum")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if !checksum.is_empty() {
        let want = checksum
            .strip_prefix("sha256:")
            .unwrap_or(checksum)
            .trim();
        if !want.eq_ignore_ascii_case(&content_hash(memories_text)) {
            return Err("MEMORY_IMPORT_INVALID: 迁移包校验失败（memories.json 与 manifest 不一致），文件可能被改动或损坏".to_string());
        }
    }
    let mut result = import_json(store, memories_text, now_iso, backup_dir)?;
    if let Some(obj) = result.as_object_mut() {
        obj.insert("packageVersion".to_string(), json!(version));
        obj.insert(
            "exportedAt".to_string(),
            manifest
                .get("createdAt")
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    Ok(result)
}

/// 在线瘦身/去重压缩：每个归属只保留最近的 `keep` 条自动任务摘要，其余软归档。/// 不删除正文与证据，随时可在「已归档」里恢复；人工录入与结构化事实不动。
pub fn compact(store: &Store, keep: i64, now_iso: &str) -> Result<usize, String> {
    let keep = keep.max(0);
    store.with_conn(|conn| {
        let groups = {
            let mut stmt = conn
                .prepare(
                    "SELECT scope, workspace_id FROM memories \
                     WHERE status = 'active' AND kind = 'task' AND source = 'auto' \
                     GROUP BY scope, workspace_id",
                )
                .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
            let rows = stmt
                .query_map([], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?
        };
        let mut archived = 0usize;
        for (scope, ws) in groups {
            let ids = {
                let mut stmt = conn
                    .prepare(
                        "SELECT id FROM memories \
                         WHERE status = 'active' AND kind = 'task' AND source = 'auto' \
                           AND scope = ?1 AND workspace_id = ?2 \
                         ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?3",
                    )
                    .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
                let rows = stmt
                    .query_map(params![scope, ws, keep], |r| r.get::<_, String>(0))
                    .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
                rows.collect::<Result<Vec<_>, _>>()
                    .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?
            };
            for id in ids {
                conn.execute(
                    "UPDATE memories SET status = 'archived', reviewed_at = ?2, updated_at = ?2 \
                     WHERE id = ?1",
                    params![id, now_iso],
                )
                .map_err(|e| format!("MEMORY_SAVE_FAILED: {e}"))?;
                archived += 1;
            }
        }
        Ok(archived)
    })
}

// ── 词面检索 ───────────────────────────────────────────────

fn source_boost(source: &str) -> f64 {
    match source {
        "manual" | "reflection" => 0.08,
        _ => 0.0,
    }
}

fn is_cjk(ch: char) -> bool {
    matches!(ch as u32, 0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xf900..=0xfaff)
}

fn push_cjk_tokens(run: &[char], out: &mut HashSet<String>) {
    if run.len() == 1 {
        out.insert(run[0].to_string());
    } else if run.len() > 1 {
        for pair in run.windows(2) {
            out.insert(pair.iter().collect());
        }
    }
}

/// 与引擎同口径的最小单元：ASCII 词（≥2 字符）+ 中文二元组
fn tokens(text: &str) -> HashSet<String> {
    let lowered = text.to_lowercase();
    let mut out = HashSet::new();
    let mut word = String::new();
    let mut cjk: Vec<char> = Vec::new();
    let flush_word = |word: &mut String, out: &mut HashSet<String>| {
        if word.chars().count() >= 2 {
            out.insert(word.clone());
        }
        word.clear();
    };
    for ch in lowered.chars() {
        if is_cjk(ch) {
            flush_word(&mut word, &mut out);
            cjk.push(ch);
            continue;
        }
        if !cjk.is_empty() {
            push_cjk_tokens(&cjk, &mut out);
            cjk.clear();
        }
        if ch.is_ascii_alphanumeric() || ch == '.' || ch == '_' || ch == '+' || ch == '-' {
            word.push(ch);
        } else {
            flush_word(&mut word, &mut out);
        }
    }
    flush_word(&mut word, &mut out);
    push_cjk_tokens(&cjk, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-memory-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn iso(ms: i64) -> String {
        crate::security::audit::iso8601_utc(ms)
    }

    fn base<'a>(content: &'a str) -> AddInput<'a> {
        AddInput {
            content,
            scope: None,
            workspace_id: None,
            kind: None,
            source: None,
            conv_id: None,
            task_id: None,
            created_at: None,
            status: None,
            subject: None,
            predicate: None,
            object: None,
            confidence: None,
        }
    }

    fn add_ws(store: &Store, ws: &str, content: &str, now: &str) -> MemoryRow {
        add(
            store,
            AddInput {
                content,
                scope: Some(SCOPE_WORKSPACE),
                workspace_id: Some(ws),
                kind: Some("task"),
                source: Some("auto"),
                conv_id: Some("conv-1"),
                task_id: Some("task-1"),
                ..base(content)
            },
            now,
        )
        .unwrap()
        .row
    }

    fn all<'a>() -> ListFilter<'a> {
        ListFilter {
            workspace_id: None,
            scope: None,
            status: None,
            limit: None,
        }
    }

    #[test]
    fn test_add_dedups_and_counts_occurrences() {
        let (store, dir) = temp_store("dedup");
        let now = iso(1_760_000_000_000);
        let a = add_ws(&store, "ws-a", "目标：整理周报；结论：已完成", &now);
        let b = add_ws(&store, "ws-a", "  目标：整理周报；结论：已完成  ", &iso(1_760_000_001_000));
        assert_eq!(a.id, b.id);
        assert_eq!(b.occurrences, 2);
        assert_eq!(list(&store, all()).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_scope_isolation_and_global_visibility() {
        let (store, dir) = temp_store("scope");
        let now = iso(1_760_000_000_000);
        add_ws(&store, "ws-a", "A 项目的事实", &now);
        add_ws(&store, "ws-b", "B 项目的事实", &now);
        add(
            &store,
            AddInput {
                content: "全局偏好：中文",
                scope: Some(SCOPE_GLOBAL),
                kind: Some("preference"),
                source: Some("manual"),
                ..base("全局偏好：中文")
            },
            &now,
        )
        .unwrap();
        let a = list(
            &store,
            ListFilter {
                workspace_id: Some("ws-a"),
                scope: None,
                status: None,
                limit: None,
            },
        )
        .unwrap();
        let contents: Vec<&str> = a.iter().map(|r| r.content.as_str()).collect();
        assert!(contents.contains(&"A 项目的事实"));
        assert!(contents.contains(&"全局偏好：中文"));
        assert!(!contents.contains(&"B 项目的事实"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_invalid_scope_and_empty_content_are_rejected() {
        let (store, dir) = temp_store("invalid");
        let now = iso(1_760_000_000_000);
        let e = add(
            &store,
            AddInput {
                scope: Some("team"),
                ..base("x")
            },
            &now,
        )
        .unwrap_err();
        assert!(e.starts_with("MEMORY_INVALID_SCOPE:"));
        let e2 = add(&store, base("   "), &now).unwrap_err();
        assert!(e2.starts_with("MEMORY_CONTENT_EMPTY:"));
        let e3 = add(
            &store,
            AddInput {
                kind: Some("weird"),
                ..base("x")
            },
            &now,
        )
        .unwrap_err();
        assert!(e3.starts_with("MEMORY_INVALID_KIND:"));
        let e4 = add(
            &store,
            AddInput {
                status: Some("sleeping"),
                ..base("x")
            },
            &now,
        )
        .unwrap_err();
        assert!(e4.starts_with("MEMORY_STATUS_INVALID:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_retrieve_scores_and_marks_used() {
        let (store, dir) = temp_store("retrieve");
        let now = iso(1_760_000_000_000);
        let hit = add_ws(&store, "ws-a", "周报模板采用三段式结构", &now);
        add_ws(&store, "ws-a", "客户 A 的接口使用 OAuth 鉴权", &now);
        add_ws(&store, "ws-b", "别的项目：周报模板", &now);
        let result = retrieve(&store, "ws-a", "帮我写周报模板", Some(5), &iso(1_760_000_002_000)).unwrap();
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].id, hit.id);
        assert_eq!(result.rows[0].use_count, 1);
        // 没有命中词时不注入（不塞无关记忆）
        assert!(retrieve(&store, "ws-a", "量子计算", Some(5), &now).unwrap().rows.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_pending_review_excludes_from_retrieval_until_confirmed() {
        let (store, dir) = temp_store("review");
        let now = iso(1_760_000_000_000);
        let row = add(
            &store,
            AddInput {
                content: "周报模板采用四段式结构",
                scope: Some(SCOPE_WORKSPACE),
                workspace_id: Some("ws-a"),
                kind: Some("fact"),
                source: Some("auto"),
                status: Some(STATUS_PENDING),
                ..base("周报模板采用四段式结构")
            },
            &now,
        )
        .unwrap()
        .row;
        assert_eq!(row.status, STATUS_PENDING);
        assert!(retrieve(&store, "ws-a", "周报模板", Some(5), &now).unwrap().rows.is_empty());
        let confirmed = set_status(&store, &row.id, "confirm", &iso(1_760_000_001_000)).unwrap();
        assert_eq!(confirmed.status, STATUS_ACTIVE);
        assert_eq!(retrieve(&store, "ws-a", "周报模板", Some(5), &now).unwrap().rows.len(), 1);
        // 驳回后不再注入，但正文仍在（软退休）
        let archived = set_status(&store, &row.id, "archive", &iso(1_760_000_002_000)).unwrap();
        assert_eq!(archived.status, STATUS_ARCHIVED);
        assert!(retrieve(&store, "ws-a", "周报模板", Some(5), &now).unwrap().rows.is_empty());
        let restored = set_status(&store, &row.id, "restore", &now).unwrap();
        assert_eq!(restored.status, STATUS_ACTIVE);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_conflicting_fact_soft_retires_old_row() {
        let (store, dir) = temp_store("conflict");
        let now = iso(1_760_000_000_000);
        let old = add(
            &store,
            AddInput {
                content: "项目截止日是周五",
                scope: Some(SCOPE_WORKSPACE),
                workspace_id: Some("ws-a"),
                kind: Some("fact"),
                source: Some("manual"),
                subject: Some("项目截止日"),
                predicate: Some("是"),
                object: Some("周五"),
                ..base("项目截止日是周五")
            },
            &now,
        )
        .unwrap()
        .row;
        let outcome = add(
            &store,
            AddInput {
                content: "项目截止日是周一",
                scope: Some(SCOPE_WORKSPACE),
                workspace_id: Some("ws-a"),
                kind: Some("fact"),
                source: Some("manual"),
                subject: Some("项目截止日"),
                predicate: Some("是"),
                object: Some("周一"),
                ..base("项目截止日是周一")
            },
            &iso(1_760_000_001_000),
        )
        .unwrap();
        assert_eq!(outcome.superseded.len(), 1);
        assert_eq!(outcome.superseded[0].id, old.id);
        let rows = list(&store, all()).unwrap();
        let archived = rows.iter().find(|r| r.id == old.id).unwrap();
        assert_eq!(archived.status, STATUS_ARCHIVED);
        assert_eq!(archived.superseded_by.as_deref(), Some(outcome.row.id.as_str()));
        // 被取代的旧事实不能直接恢复
        let err = set_status(&store, &old.id, "restore", &now).unwrap_err();
        assert!(err.starts_with("MEMORY_SUPERSEDED:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_entities_group_by_subject() {
        let (store, dir) = temp_store("entities");
        let now = iso(1_760_000_000_000);
        for (content, subject, predicate, object) in [
            ("客户 A 使用 OAuth 鉴权", "客户 A", "鉴权方式", "OAuth"),
            ("客户 A 的接口限流为每分钟 60 次", "客户 A", "限流", "每分钟 60 次"),
            ("客户 B 位于上海", "客户 B", "所在地", "上海"),
        ] {
            add(
                &store,
                AddInput {
                    content,
                    scope: Some(SCOPE_WORKSPACE),
                    workspace_id: Some("ws-a"),
                    kind: Some("fact"),
                    source: Some("manual"),
                    subject: Some(subject),
                    predicate: Some(predicate),
                    object: Some(object),
                    ..base(content)
                },
                &now,
            )
            .unwrap();
        }
        let pages = entities(&store, "ws-a").unwrap();
        assert_eq!(pages.len(), 2);
        assert_eq!(pages[0].subject, "客户 A");
        assert_eq!(pages[0].count, 2);
        assert_eq!(pages[0].entries.len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_export_import_roundtrip_and_backup() {
        let (store, dir) = temp_store("export");
        let now = iso(1_760_000_000_000);
        add_ws(&store, "ws-a", "要导出的记忆", &now);
        let value = export_value(&store, &now).unwrap();
        assert_eq!(value["format"], EXPORT_FORMAT);
        assert_eq!(value["count"], 1);
        let raw = serde_json::to_string(&value).unwrap();

        let (store2, dir2) = temp_store("import");
        let result = import_json(
            &store2,
            &raw,
            &iso(1_760_000_002_000),
            &dir2.join("backups"),
        )
        .unwrap();
        assert_eq!(result["inserted"], 1);
        assert_eq!(result["merged"], 0);
        assert!(std::path::Path::new(result["backupPath"].as_str().unwrap()).exists());
        let rows = list(&store2, all()).unwrap();
        assert_eq!(rows[0].content, "要导出的记忆");
        assert_eq!(rows[0].workspace_id, "ws-a");
        // 再导入一次按内容合并，不产生重复行
        let again = import_json(&store2, &raw, &now, &dir2.join("backups")).unwrap();
        assert_eq!(again["inserted"], 0);
        assert_eq!(again["merged"], 1);
        assert_eq!(list(&store2, all()).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&dir2);
    }

    #[test]
    fn test_import_rejects_wrong_format() {
        let (store, dir) = temp_store("import-bad");
        let err = import_json(
            &store,
            r#"{"format":"other","version":1,"entries":[]}"#,
            &iso(1_760_000_000_000),
            &dir.join("backups"),
        )
        .unwrap_err();
        assert!(err.starts_with("MEMORY_IMPORT_INVALID:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_compact_archives_old_auto_tasks_only() {
        let (store, dir) = temp_store("compact");
        for i in 0..3 {
            add_ws(&store, "ws-a", &format!("任务摘要 {i}"), &iso(1_760_000_000_000 + i * 1000));
        }
        add(
            &store,
            AddInput {
                content: "人工事实不能压缩",
                scope: Some(SCOPE_WORKSPACE),
                workspace_id: Some("ws-a"),
                kind: Some("fact"),
                source: Some("manual"),
                ..base("人工事实不能压缩")
            },
            &iso(1_760_000_000_000 + 5000),
        )
        .unwrap();
        let archived = compact(&store, 1, &iso(1_760_000_010_000)).unwrap();
        assert_eq!(archived, 2);
        let active = list(
            &store,
            ListFilter {
                workspace_id: None,
                scope: None,
                status: Some(STATUS_ACTIVE),
                limit: None,
            },
        )
        .unwrap();
        assert!(active.iter().any(|r| r.content == "人工事实不能压缩"));
        assert!(active.iter().any(|r| r.content == "任务摘要 2"));
        assert_eq!(active.iter().filter(|r| r.kind == "task").count(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_delete_reports_missing() {
        let (store, dir) = temp_store("delete");
        let now = iso(1_760_000_000_000);
        let row = add_ws(&store, "ws-a", "要删除的记忆", &now);
        assert!(delete(&store, &row.id).unwrap());
        assert!(delete(&store, &row.id).unwrap_err().starts_with("MEMORY_NOT_FOUND:"));
        assert!(delete(&store, " ").unwrap_err().starts_with("MEMORY_NOT_FOUND:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_import_legacy_is_one_shot_and_keeps_file() {
        let (store, dir) = temp_store("legacy");
        let awareness = dir.join("awareness").join("main");
        std::fs::create_dir_all(&awareness).unwrap();
        std::fs::write(
            awareness.join("MEMORY.md"),
            "# 长期记忆\n\n- mem-1 · manual · 2026-01-01T00:00:00.000Z · 用户偏好简短回复\n",
        )
        .unwrap();
        let now = iso(1_760_000_000_000);
        assert_eq!(import_legacy(&store, &awareness, &now).unwrap(), 1);
        assert_eq!(import_legacy(&store, &awareness, &now).unwrap(), 0);
        assert!(awareness.join("MEMORY.md").exists());
        let rows = list(&store, all()).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].content, "用户偏好简短回复");
        assert_eq!(rows[0].created_at, "2026-01-01T00:00:00.000Z");
        assert_eq!(rows[0].status, STATUS_ACTIVE);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_tokens_cover_ascii_and_cjk_bigrams() {
        let t = tokens("OAuth 鉴权与周报");
        assert!(t.contains("oauth"));
        assert!(t.contains("鉴权"));
        assert!(t.contains("周报"));
        assert!(!t.contains("a"));
    }

    #[test]
    fn test_sync_doc_imports_once_and_keeps_file() {
        let (store, dir) = temp_store("doc-sync");
        let awareness = dir.join("awareness").join("main");
        std::fs::create_dir_all(&awareness).unwrap();
        std::fs::write(
            awareness.join("MEMORY.md"),
            "# 长期记忆\n\n\
             - mem-1 · manual · 2026-01-01T00:00:00.000Z · 用户偏好简短回复\n\
             - mem-2 · auto · 2026-01-01T00:01:00.000Z · 自动摘要一条\n",
        )
        .unwrap();
        let now = iso(1_760_000_000_000);
        let first = sync_doc(&store, &awareness, &now).unwrap();
        assert_eq!(first.file_count, 2);
        assert_eq!(first.imported, 2);
        assert_eq!(first.existing, 0);
        // 可重复执行：第二次全部命中已存在，不产生重复行
        let second = sync_doc(&store, &awareness, &now).unwrap();
        assert_eq!(second.file_count, 2);
        assert_eq!(second.imported, 0);
        assert_eq!(second.existing, 2);
        assert_eq!(list(&store, all()).unwrap().len(), 2);
        let rows = list(&store, all()).unwrap();
        let manual = rows.iter().find(|r| r.content == "用户偏好简短回复").unwrap();
        let auto = rows.iter().find(|r| r.content == "自动摘要一条").unwrap();
        assert_eq!(manual.scope, SCOPE_GLOBAL);
        assert_eq!(manual.kind, "fact");
        assert_eq!(auto.kind, "task");
        // 文件只读不写：对账后原文件仍在
        assert!(awareness.join("MEMORY.md").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_sync_doc_without_file_returns_zero() {
        let (store, dir) = temp_store("doc-sync-missing");
        let awareness = dir.join("awareness").join("none");
        let result = sync_doc(&store, &awareness, &iso(1_760_000_000_000)).unwrap();
        assert_eq!(result.file_count, 0);
        assert_eq!(result.imported, 0);
        // 没有 MEMORY.md 时不顺手创建文件
        assert!(!awareness.join("MEMORY.md").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_package_export_import_roundtrip_with_backup() {
        let (store, dir) = temp_store("pkg-export");
        let now = iso(1_760_000_000_000);
        add_ws(&store, "ws-a", "迁移包里的记忆", &now);
        let exports = dir.join("exports");
        let (path, count) = export_package(&store, &exports, &now).unwrap();
        assert_eq!(count, 1);
        assert!(path.ends_with(&format!(".{PACKAGE_EXT}")));
        let bytes = std::fs::read(&path).unwrap();

        // manifest 可读且字段齐全
        let entries = crate::file::office::zip_entries(&bytes).unwrap();
        let manifest: Value = serde_json::from_slice(
            &package_part(&bytes, &entries, "manifest.json").unwrap().unwrap(),
        )
        .unwrap();
        assert_eq!(manifest["format"], PACKAGE_FORMAT);
        assert_eq!(manifest["version"], PACKAGE_VERSION);
        assert_eq!(manifest["count"], 1);
        assert!(manifest["checksum"].as_str().unwrap().starts_with("sha256:"));

        let (store2, dir2) = temp_store("pkg-import");
        let result = import_package(
            &store2,
            &bytes,
            &iso(1_760_000_002_000),
            &dir2.join("backups"),
        )
        .unwrap();
        assert_eq!(result["packageVersion"], 1);
        assert_eq!(result["exportedAt"], now);
        assert_eq!(result["inserted"], 1);
        // 导入前强制备份，旧库可回滚
        assert!(std::path::Path::new(result["backupPath"].as_str().unwrap()).exists());
        let rows = list(&store2, all()).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].content, "迁移包里的记忆");
        // 重复导入按内容合并，不重复计数
        let again = import_package(
            &store2,
            &bytes,
            &iso(1_760_000_003_000),
            &dir2.join("backups"),
        )
        .unwrap();
        assert_eq!(again["inserted"], 0);
        assert_eq!(again["merged"], 1);
        assert_eq!(list(&store2, all()).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&dir2);
    }

    #[test]
    fn test_package_rejects_tampered_and_foreign_archives() {
        let (store, dir) = temp_store("pkg-bad");
        let now = iso(1_760_000_000_000);
        add_ws(&store, "ws-a", "会被篡改的记忆", &now);
        let (path, _) = export_package(&store, &dir.join("exports"), &now).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        let entries = crate::file::office::zip_entries(&bytes).unwrap();
        let manifest = package_part(&bytes, &entries, "manifest.json").unwrap().unwrap();
        let memories = package_part(&bytes, &entries, "memories.json").unwrap().unwrap();

        // 改 memories.json 但保留旧 checksum：必须在入口拦住
        let tampered = build_store_zip(&[
            ("manifest.json", manifest.as_slice()),
            (
                "memories.json",
                &b"{\"format\":\"tongtian.memory\",\"version\":1,\"count\":0,\"entries\":[]}"[..],
            ),
        ])
        .unwrap();
        let err = import_package(&store, &tampered, &now, &dir.join("backups")).unwrap_err();
        assert!(err.contains("迁移包校验失败"), "{err}");

        // 不是迁移包格式（普通 ZIP / 缺 manifest）
        let foreign = build_store_zip(&[("memories.json", memories.as_slice())]).unwrap();
        let err = import_package(&store, &foreign, &now, &dir.join("backups")).unwrap_err();
        assert!(err.contains("缺少 manifest.json"), "{err}");

        // 版本高于当前支持：提示升级而不是硬导
        let future = serde_json::to_vec(&json!({
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION + 1,
            "createdAt": now,
            "count": 0,
            "checksum": "",
        }))
        .unwrap();
        let future_pkg =
            build_store_zip(&[("manifest.json", future.as_slice()), ("memories.json", memories.as_slice())])
                .unwrap();
        let err = import_package(&store, &future_pkg, &now, &dir.join("backups")).unwrap_err();
        assert!(err.contains("高于当前支持"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
