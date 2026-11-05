//! 常驻事件账本（D-46 第一段）：入口登记、幂等游标、事件入库、策略版本与判定留痕
//!
//! 事实来源是桌面库 `tongtian.db` 的四张 `sa_` 表（见 `store/migrate.rs` V16），
//! 只有 Rust 主进程打开这张库；引擎不写、不读，也不新建第二份事件库
//! （由 `tests/unit/storage/ownership-contract.test.ts` 从源码层面拦截）。
//!
//! 四条纪律（对齐 DSH 原型，证据见 `docs/Super-agent-对标DSH.md` 附录 D）：
//!
//! 1. **游标只前进**：`sa_sources.cursor` 是每个入口实例自己的读取位置。
//!    重启后从这里续读，因此「关闭再打开应用」不会把历史事件当成新事件重放。
//!    推进语句写成 `WHERE id = ? AND cursor < ?`，比大小交给 SQLite，不靠内存值。
//! 2. **事件是幂等单位**：`sa_triggers` 以 `(source_id, external_id)` 唯一。
//!    断线补拉与重连重叠的同一条事件只会落一行，也不会产生第二个工作单元。
//! 3. **记账与推进同事务**：一条事件的写入和游标前进在一个事务里完成。
//!    中途崩溃宁可重读（幂等命中，不重复干活），也不要漏读。
//! 4. **判定挂在策略版本上**：`sa_decisions` 以 `(trigger_id, policy_version)` 唯一，
//!    旧判定不删；策略变更后按新版本重算，能回答「当时依据哪一版策略」。
//! 5. **待研判队列另有一个版本键**：`assess_queue_version` 记「这一行现在排在哪个策略版本的
//!    模型研判队列里」，每次 `save_policy` 都按当前生效版本重排一次（幂等），而 `policy_version`
//!    一律不改写。两个键分开才有「改策略 → 存量候选按新版重排，判定留痕仍是旧版」这个语义；
//!    合成一列要么丢留痕，要么让老候选永远孤悬（V17 就是后者，见 `store/migrate.rs` V18）。
//!
//! 隐私与凭据边界：这里只存来源声明的字段（发起人 id、事件类型、线程键）与截断后的正文/结构；
//! 不存令牌、不存密钥、不写日志。正文上限 `MAX_TRIGGER_TEXT_CHARS`，原始结构上限
//! `MAX_PAYLOAD_CHARS`，超限只留标记而不搬原文，避免把整份文档或上游结构里的凭据字段
//! 留在桌面库里。

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use serde_json::{json, Value};

use crate::store::Store;
use crate::superagent::gate::GateVerdict;
use crate::superagent::policy::{self, PolicyDoc};

/// 事件正文入库上限（字符）：够一次研判与界面回看，不放整篇文档
pub const MAX_TRIGGER_TEXT_CHARS: usize = 2000;
/// 原始结构入库上限（字符）
pub const MAX_PAYLOAD_CHARS: usize = 8000;
/// 判定依据与理由的入库上限
pub const MAX_BASIS_CHARS: usize = 400;
pub const MAX_REASON_CHARS: usize = 240;
/// 入口最近一次失败原因的入库上限
pub const MAX_ERROR_CHARS: usize = 240;
/// 界面与循环默认一次取多少条
pub const DEFAULT_LIST_LIMIT: usize = 50;
pub const MAX_LIST_LIMIT: usize = 500;
/// 策略来源标记
pub const POLICY_SOURCE_DEFAULT: &str = "default";
pub const POLICY_SOURCE_USER: &str = "user";

const MS_PER_DAY: i64 = 86_400_000;

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// UTC 日界起点（与用量台账同口径，作为每日研判上限的窗口起点）
pub fn utc_day_start_ms(ms: i64) -> i64 {
    ms.div_euclid(MS_PER_DAY) * MS_PER_DAY
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceRow {
    pub id: String,
    pub kind: String,
    pub instance: String,
    pub label: String,
    /// 是否在读取（运维动作）；有没有授权看策略文档，两者都要显式打开
    pub enabled: bool,
    pub cursor: i64,
    pub config: Value,
    pub last_error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TriggerRow {
    pub id: String,
    pub source_id: String,
    pub external_id: String,
    pub kind: String,
    pub text: String,
    pub actor: String,
    pub thread_key: String,
    /// 来源声明的事实：这条事件是否直接指向本应用
    pub addressed_agent: bool,
    pub source_seq: i64,
    pub arrived_at_ms: i64,
    pub payload: Value,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DecisionRow {
    pub id: String,
    pub trigger_id: String,
    pub policy_version: i64,
    pub strength: String,
    pub outcome: String,
    pub would_accept: bool,
    pub basis: String,
    pub matched_rules: Vec<String>,
    pub reason: String,
    pub decided_at_ms: i64,
}

/// 一条待入库的事件（由入口适配器构造，字段全部来自上游）
#[derive(Debug, Clone)]
pub struct NewTrigger {
    pub source_id: String,
    pub external_id: String,
    pub kind: String,
    pub text: String,
    pub actor: String,
    pub thread_key: String,
    pub addressed_agent: bool,
    /// 上游的读取位置（项目事件即 `seq`）；只有比当前水位新才推进游标
    pub source_seq: i64,
    pub payload: Value,
    /// 事件自身的发生时间（缺省用到达时间）
    pub arrived_at_ms: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct IngestOutcome {
    pub trigger: TriggerRow,
    /// false = `(source_id, external_id)` 已存在，本次没有新增行
    pub inserted: bool,
    pub cursor_advanced: bool,
}

#[derive(Debug, Clone)]
pub struct SavePolicyOutcome {
    pub version: i64,
    /// false = 内容与当前生效版本一致，没有新增版本
    pub changed: bool,
    pub doc: PolicyDoc,
    /// 本次保存里被拉进「当前版本研判队列」的候选条数。重排是幂等的：已经在当前队列的不算，
    /// 所以正常路径（含内容没变的保存）这里是 0；只有真的升了版、或者候选的队列键落后于当前
    /// 版本（上一次保存重排失败、或 V17 时代留下的孤儿）才会大于 0。
    pub requeued: usize,
}

/// 第二层结论的回写结果。三种都要能分开说清，因为它们对「钱」的含义不同。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AssessmentWrite {
    /// 结论已写进判定行（这一条候选从此不再进队列）
    Written,
    /// 这一行已经有一份结论了（并发的一轮先落了笔）：不重复花钱，也不算失败
    AlreadyJudged,
    /// 研判期间策略升版、这一行已被重排到新队列：本次结论作废，按新策略再判一次
    Superseded,
}

// ── 入口登记与游标 ────────────────────────────────────────────

/// 登记一个事件入口实例（幂等）：已存在就只更新标签与配置，
/// **游标与 enabled 不动**——重复登记不该把读取位置倒回 0，也不该悄悄重新开启。
/// 新建的行 `enabled = 0`：登记不等于开跑，必须显式打开一次。
pub fn register_source(
    store: &Store,
    kind: &str,
    instance: &str,
    label: &str,
    config: Value,
) -> Result<SourceRow, String> {
    if !crate::superagent::SOURCE_KINDS.contains(&kind) {
        return Err(format!(
            "SUPERAGENT_SOURCE_KIND_UNSUPPORTED: 未知事件入口类型 {kind:?}"
        ));
    }
    let instance = instance.trim();
    if instance.is_empty()
        && matches!(
            kind,
            crate::superagent::SOURCE_KIND_PROJECT_EVENT | crate::superagent::SOURCE_KIND_IM_INBOUND
        )
    {
        return Err(format!(
            "SUPERAGENT_SOURCE_INSTANCE_REQUIRED: 入口 {kind} 必须指定具体实例（项目 id / 会话 id）"
        ));
    }
    let label = label.trim().to_string();
    let config_text = compact_json(&config, MAX_PAYLOAD_CHARS);
    let now = now_ms();
    store.with_conn(|c| {
        let existing = find_source(c, kind, instance)?;
        let row = match existing {
            Some(mut row) => {
                c.execute(
                    "UPDATE sa_sources SET label = ?1, config = ?2, updated_at_ms = ?3 WHERE id = ?4",
                    params![label, config_text, now, row.id],
                )
                .map_err(|e| format!("SUPERAGENT_SOURCE_UPDATE_FAILED: {e}"))?;
                row.label = label;
                row.config = parse_json(&config_text);
                row.updated_at_ms = now;
                row
            }
            None => {
                let id = format!("sasrc-{}", uuid::Uuid::new_v4().simple());
                c.execute(
                    "INSERT INTO sa_sources (id, kind, instance, label, enabled, cursor, config, \
                     created_at_ms, updated_at_ms) VALUES (?1, ?2, ?3, ?4, 0, 0, ?5, ?6, ?7)",
                    params![id, kind, instance, label, config_text, now, now],
                )
                .map_err(|e| format!("SUPERAGENT_SOURCE_INSERT_FAILED: {e}"))?;
                SourceRow {
                    id,
                    kind: kind.to_string(),
                    instance: instance.to_string(),
                    label,
                    enabled: false,
                    cursor: 0,
                    config: parse_json(&config_text),
                    last_error: None,
                    created_at_ms: now,
                    updated_at_ms: now,
                }
            }
        };
        Ok(row)
    })
}

fn find_source(c: &Connection, kind: &str, instance: &str) -> Result<Option<SourceRow>, String> {
    c.query_row(
        SOURCE_SELECT,
        params![kind, instance],
        map_source_row,
    )
    .optional()
    .map_err(|e| format!("SUPERAGENT_SOURCE_READ_FAILED: {e}"))
}

const SOURCE_SELECT: &str = "SELECT id, kind, instance, label, enabled, cursor, config, last_error, \
     created_at_ms, updated_at_ms FROM sa_sources WHERE kind = ?1 AND instance = ?2";

const SOURCE_SELECT_BY_ID: &str = "SELECT id, kind, instance, label, enabled, cursor, config, \
     last_error, created_at_ms, updated_at_ms FROM sa_sources WHERE id = ?1";

fn map_source_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<SourceRow> {
    let config_text: String = r.get(6)?;
    Ok(SourceRow {
        id: r.get(0)?,
        kind: r.get(1)?,
        instance: r.get(2)?,
        label: r.get(3)?,
        enabled: r.get::<_, i64>(4)? != 0,
        cursor: r.get(5)?,
        config: parse_json(&config_text),
        last_error: r.get(7)?,
        created_at_ms: r.get(8)?,
        updated_at_ms: r.get(9)?,
    })
}

fn parse_json(text: &str) -> Value {
    serde_json::from_str(text).unwrap_or(json!({}))
}

pub fn list_sources(store: &Store) -> Result<Vec<SourceRow>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT id, kind, instance, label, enabled, cursor, config, last_error, \
                        created_at_ms, updated_at_ms FROM sa_sources ORDER BY kind, instance",
            )
            .map_err(|e| format!("SUPERAGENT_SOURCE_LIST_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], map_source_row)
            .map_err(|e| format!("SUPERAGENT_SOURCE_LIST_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SUPERAGENT_SOURCE_LIST_FAILED: {e}"))
    })
}

pub fn get_source(store: &Store, source_id: &str) -> Result<Option<SourceRow>, String> {
    store.with_conn(|c| {
        c.query_row(SOURCE_SELECT_BY_ID, params![source_id], map_source_row)
            .optional()
            .map_err(|e| format!("SUPERAGENT_SOURCE_READ_FAILED: {e}"))
    })
}

/// 开/关一个入口的读取。开启必须显式调用一次：登记不等于开跑。
pub fn set_source_enabled(store: &Store, source_id: &str, enabled: bool) -> Result<SourceRow, String> {
    let now = now_ms();
    store.with_conn(|c| {
        let changed = c
            .execute(
                "UPDATE sa_sources SET enabled = ?1, updated_at_ms = ?2 WHERE id = ?3",
                params![enabled as i64, now, source_id],
            )
            .map_err(|e| format!("SUPERAGENT_SOURCE_UPDATE_FAILED: {e}"))?;
        if changed == 0 {
            return Err(format!(
                "SUPERAGENT_SOURCE_NOT_FOUND: 没有找到事件入口 {source_id}"
            ));
        }
        source_by_id(c, source_id)
    })
}

/// 记下入口最近一次读取失败的原因（不含任何凭据，界面直接展示）
pub fn note_source_error(store: &Store, source_id: &str, reason: Option<&str>) -> Result<(), String> {
    let cleaned = reason.map(|raw| truncate_chars(raw.trim(), MAX_ERROR_CHARS));
    let now = now_ms();
    store.with_conn(|c| {
        let changed = c
            .execute(
                "UPDATE sa_sources SET last_error = ?1, updated_at_ms = ?2 WHERE id = ?3",
                params![cleaned, now, source_id],
            )
            .map_err(|e| format!("SUPERAGENT_SOURCE_UPDATE_FAILED: {e}"))?;
        if changed == 0 {
            return Err(format!(
                "SUPERAGENT_SOURCE_NOT_FOUND: 没有找到事件入口 {source_id}"
            ));
        }
        Ok(())
    })
}

fn source_by_id(c: &Connection, source_id: &str) -> Result<SourceRow, String> {
    c.query_row(SOURCE_SELECT_BY_ID, params![source_id], map_source_row)
        .optional()
        .map_err(|e| format!("SUPERAGENT_SOURCE_READ_FAILED: {e}"))?
        .ok_or_else(|| format!("SUPERAGENT_SOURCE_NOT_FOUND: 没有找到事件入口 {source_id}"))
}

/// 游标只前进（比大小交给 SQL）。返回 false 表示这条不比当前水位新，不做任何写入。
pub fn advance_cursor(store: &Store, source_id: &str, seq: i64) -> Result<bool, String> {
    let now = now_ms();
    store.with_conn(|c| {
        let changed = c
            .execute(
                "UPDATE sa_sources SET cursor = ?1, updated_at_ms = ?2 WHERE id = ?3 AND cursor < ?1",
                params![seq, now, source_id],
            )
            .map_err(|e| format!("SUPERAGENT_CURSOR_UPDATE_FAILED: {e}"))?;
        Ok(changed == 1)
    })
}

// ── 事件入库 ──────────────────────────────────────────────────

/// 写入一条事件并推进游标，两者同事务。
///
/// 重复投递（断线补拉重叠、重连重放）走 `ON CONFLICT DO NOTHING`，
/// 返回 `inserted = false` 且**不会产生第二个工作单元**；游标按水位照常比较，
/// 否则重启后还会再读一遍同一段。
pub fn ingest(store: &Store, input: &NewTrigger) -> Result<IngestOutcome, String> {
    if input.external_id.trim().is_empty() {
        return Err(
            "SUPERAGENT_TRIGGER_ID_MISSING: 事件缺上游 id，无法判断是否重复投递".to_string(),
        );
    }
    let text = truncate_chars(input.text.trim(), MAX_TRIGGER_TEXT_CHARS);
    let payload = compact_json(&input.payload, MAX_PAYLOAD_CHARS);
    let arrived = input.arrived_at_ms.unwrap_or_else(now_ms);
    store.with_conn(|c| {
        let tx = c
            .unchecked_transaction()
            .map_err(|e| format!("SUPERAGENT_INGEST_BEGIN_FAILED: {e}"))?;
        // 先确认入口已登记：没登记就连游标都不该动
        let registered = tx
            .query_row(
                "SELECT 1 FROM sa_sources WHERE id = ?1",
                params![input.source_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| format!("SUPERAGENT_SOURCE_READ_FAILED: {e}"))?
            .is_some();
        if !registered {
            return Err(format!(
                "SUPERAGENT_SOURCE_NOT_FOUND: 事件入口 {} 尚未登记，不能记账",
                input.source_id
            ));
        }
        let cursor_advanced = tx
            .execute(
                "UPDATE sa_sources SET cursor = ?1, updated_at_ms = ?2 WHERE id = ?3 AND cursor < ?1",
                params![input.source_seq, arrived, input.source_id],
            )
            .map_err(|e| format!("SUPERAGENT_CURSOR_UPDATE_FAILED: {e}"))?
            == 1;
        let id = format!("satrg-{}", uuid::Uuid::new_v4().simple());
        let inserted = tx
            .execute(
                "INSERT INTO sa_triggers (id, source_id, external_id, kind, text, actor, thread_key, \
                    addressed_agent, source_seq, arrived_at_ms, payload) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
                 ON CONFLICT (source_id, external_id) DO NOTHING",
                params![
                    id,
                    input.source_id,
                    input.external_id,
                    input.kind,
                    text,
                    input.actor.trim(),
                    input.thread_key.trim(),
                    input.addressed_agent as i64,
                    input.source_seq,
                    arrived,
                    payload,
                ],
            )
            .map_err(|e| format!("SUPERAGENT_TRIGGER_INSERT_FAILED: {e}"))?
            == 1;
        let trigger = if inserted {
            trigger_by_id(&tx, &id)?
        } else {
            // 幂等命中：取回已有的那一行，调用方拿到的是同一个工作单元 id
            tx.query_row(
                "SELECT id, source_id, external_id, kind, text, actor, thread_key, addressed_agent, \
                        source_seq, arrived_at_ms, payload FROM sa_triggers \
                 WHERE source_id = ?1 AND external_id = ?2",
                params![input.source_id, input.external_id],
                map_trigger_row,
            )
            .optional()
            .map_err(|e| format!("SUPERAGENT_TRIGGER_READ_FAILED: {e}"))?
            .ok_or_else(|| {
                "SUPERAGENT_TRIGGER_READ_FAILED: 唯一键冲突却没读到已有事件行".to_string()
            })?
        };
        tx.commit().map_err(|e| format!("SUPERAGENT_INGEST_COMMIT_FAILED: {e}"))?;
        Ok(IngestOutcome {
            trigger,
            inserted,
            cursor_advanced,
        })
    })
}

fn trigger_by_id(c: &Connection, id: &str) -> Result<TriggerRow, String> {
    c.query_row(
        "SELECT id, source_id, external_id, kind, text, actor, thread_key, addressed_agent, \
                source_seq, arrived_at_ms, payload FROM sa_triggers WHERE id = ?1",
        params![id],
        map_trigger_row,
    )
    .optional()
    .map_err(|e| format!("SUPERAGENT_TRIGGER_READ_FAILED: {e}"))?
    .ok_or_else(|| format!("SUPERAGENT_TRIGGER_NOT_FOUND: 事件 {id} 写入后读不到"))
}

fn map_trigger_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<TriggerRow> {
    Ok(TriggerRow {
        id: r.get(0)?,
        source_id: r.get(1)?,
        external_id: r.get(2)?,
        kind: r.get(3)?,
        text: r.get(4)?,
        actor: r.get(5)?,
        thread_key: r.get(6)?,
        addressed_agent: r.get::<_, i64>(7)? != 0,
        source_seq: r.get(8)?,
        arrived_at_ms: r.get(9)?,
        payload: parse_json(&r.get::<_, String>(10)?),
    })
}

pub fn get_trigger(store: &Store, trigger_id: &str) -> Result<Option<TriggerRow>, String> {
    store.with_conn(|c| {
        c.query_row(
            "SELECT id, source_id, external_id, kind, text, actor, thread_key, addressed_agent, \
                    source_seq, arrived_at_ms, payload FROM sa_triggers WHERE id = ?1",
            params![trigger_id],
            map_trigger_row,
        )
        .optional()
        .map_err(|e| format!("SUPERAGENT_TRIGGER_READ_FAILED: {e}"))
    })
}

pub fn list_triggers(store: &Store, limit: Option<usize>) -> Result<Vec<TriggerRow>, String> {
    let limit = clamp_limit(limit);
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT id, source_id, external_id, kind, text, actor, thread_key, addressed_agent, \
                        source_seq, arrived_at_ms, payload FROM sa_triggers \
                 ORDER BY arrived_at_ms DESC, id DESC LIMIT ?1",
            )
            .map_err(|e| format!("SUPERAGENT_TRIGGER_LIST_FAILED: {e}"))?;
        let rows = stmt
            .query_map(params![limit as i64], map_trigger_row)
            .map_err(|e| format!("SUPERAGENT_TRIGGER_LIST_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SUPERAGENT_TRIGGER_LIST_FAILED: {e}"))
    })
}

// ── 策略版本 ──────────────────────────────────────────────────

/// 首次启动写入第 1 版缺省策略（observe、零入口授权）。已有版本时原样返回当前版本。
pub fn ensure_default_policy(store: &Store) -> Result<i64, String> {
    store.with_conn(|c| {
        let current: i64 = c
            .query_row("SELECT COALESCE(MAX(version), 0) FROM sa_policy", [], |r| r.get(0))
            .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
        if current > 0 {
            return Ok(current);
        }
        insert_policy(c, &policy::default_doc(), POLICY_SOURCE_DEFAULT)
    })
}

fn insert_policy(c: &Connection, doc: &PolicyDoc, source: &str) -> Result<i64, String> {
    let version: i64 = c
        .query_row(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM sa_policy",
            [],
            |r| r.get(0),
        )
        .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
    c.execute(
        "INSERT INTO sa_policy (version, doc, digest, source, created_at_ms) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![
            version,
            policy::canonical_json(doc),
            policy::digest(doc),
            source,
            now_ms()
        ],
    )
    .map_err(|e| format!("SUPERAGENT_POLICY_INSERT_FAILED: {e}"))?;
    Ok(version)
}

/// 保存策略：先规范化校验，`auto` 在本段被挡住；内容与当前生效版本一致则不新增版本。
///
/// 无论有没有新增版本，都把「还没研判的阳性候选」重排到当前版本的队列（见
/// `requeue_pending_decisions`）：策略变了，旧队列里那条候选的待办身份就失效了；不重排它们
/// 会永远留在账本里但没人再处理。内容没变时也走一次，是因为这一步幂等且能自愈两类孤儿：
/// 上一次保存时重排失败留下的（错误码 `SUPERAGENT_POLICY_REQUEUE_FAILED` 说的那一批），
/// 以及 V17 时代升过版、从此再没人追研判的存量候选——升级后它们的队列键回填成自己的 gate
/// 版本，只有再点一次「保存策略」才会被拉回当前队列。已经在当前队列里的行不重复打标记，
/// 所以正常路径下这一次重排是 0 条。
pub fn save_policy(store: &Store, raw: &Value, source: &str) -> Result<SavePolicyOutcome, String> {
    let doc = policy::parse_doc(raw)?;
    policy::assert_mode_allowed(&doc)?;
    let trimmed = source.trim();
    let source_tag = if trimmed.is_empty() {
        POLICY_SOURCE_USER
    } else {
        trimmed
    };
    store.with_conn(|c| {
        let mut changed = true;
        let version = match latest_policy(c)? {
            Some((current_version, current_doc))
                if policy::digest(&current_doc) == policy::digest(&doc) =>
            {
                changed = false;
                current_version
            }
            _ => insert_policy(c, &doc, source_tag)?,
        };
        let requeued = requeue_pending_decisions(c, version, now_ms())?;
        Ok(SavePolicyOutcome {
            version,
            changed,
            doc,
            requeued,
        })
    })
}

/// 把待研判的阳性候选重排进给定策略版本的队列（`save_policy` 每次保存都调，幂等）。
///
/// 只挪队列键（`assess_queue_version`）与重排时刻，**绝不动 `policy_version`**——
/// 那一列记的是「当时的 gate 依据哪一版策略给出」，是留痕，改写它等于删掉可追溯性。
/// 三个条件各自挡掉一类不该动的行：
/// - `assess_status = ''`：已经判过（done / failed）的不重排，重判等于为同一个判断再花一次模型钱；
/// - `would_accept = 1`：阴性判定从没进过第二层队列，不需要挪；
/// - `assess_queue_version <> ?1`：已经在当前队列里的不重复打「重排过」的标记。
///
/// 返回值是这一轮重排了多少条，界面要把它显示出来：「改了策略，N 条候选按新版重排」是用户
/// 该看见的因果，不是内部记账。
fn requeue_pending_decisions(
    c: &Connection,
    new_version: i64,
    now: i64,
) -> Result<usize, String> {
    let changed = c
        .execute(
            "UPDATE sa_decisions SET assess_queue_version = ?1, requeued_at_ms = ?2 \
             WHERE assess_status = '' AND would_accept = 1 AND assess_queue_version <> ?1",
            params![new_version, now],
        )
        .map_err(|e| format!("SUPERAGENT_POLICY_REQUEUE_FAILED: {e}"))?;
    Ok(changed)
}

fn latest_policy(c: &Connection) -> Result<Option<(i64, PolicyDoc)>, String> {
    let row = c
        .query_row(
            "SELECT version, doc FROM sa_policy ORDER BY version DESC LIMIT 1",
            [],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
    Ok(row.map(|(version, text)| (version, parse_policy_doc(&text))))
}

/// 策略正文读不出来时的兜底：一律回到最保守的缺省文档（observe + 零入口授权）。
/// 策略层出问题只能导致「不接单」，不能变成「照单全收」。
fn parse_policy_doc(text: &str) -> PolicyDoc {
    serde_json::from_str::<PolicyDoc>(text)
        .map(|mut doc| {
            if policy::normalize(&mut doc).is_err() {
                return policy::default_doc();
            }
            // 已经存过的版本里如果写着 auto（例如旧数据），按 observe 生效
            if doc.accept_mode == policy::MODE_AUTO {
                doc.accept_mode = policy::DEFAULT_MODE.to_string();
            }
            doc
        })
        .unwrap_or_else(|_| policy::default_doc())
}

pub fn effective_policy(store: &Store) -> (i64, PolicyDoc) {
    match store.with_conn(|c| latest_policy(c)) {
        Ok(Some((version, doc))) => (version, doc),
        _ => (0, policy::default_doc()),
    }
}

pub fn policy_at_version(store: &Store, version: i64) -> Result<Option<PolicyDoc>, String> {
    store.with_conn(|c| {
        let text = c
            .query_row(
                "SELECT doc FROM sa_policy WHERE version = ?1",
                params![version],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
        let Some(text) = text else { return Ok(None) };
        Ok(Some(parse_policy_doc(&text)))
    })
}

pub fn list_policy_versions(store: &Store, limit: Option<usize>) -> Result<Vec<Value>, String> {
    let limit = clamp_limit(limit);
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT version, digest, source, created_at_ms, doc FROM sa_policy \
                 ORDER BY version DESC LIMIT ?1",
            )
            .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
        let rows = stmt
            .query_map(params![limit as i64], |r| {
                Ok(json!({
                    "version": r.get::<_, i64>(0)?,
                    "digest": r.get::<_, String>(1)?,
                    "source": r.get::<_, String>(2)?,
                    "createdAtMs": r.get::<_, i64>(3)?,
                    "doc": parse_json(&r.get::<_, String>(4)?),
                }))
            })
            .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SUPERAGENT_POLICY_READ_FAILED: {e}"))
    })
}

// ── 判定留痕与候选清单 ────────────────────────────────────────

/// 记一条判定。同一 `(trigger, policy_version)` 只留第一条，重复判定不覆盖历史。
/// 新行的研判队列键就是它自己的判定版本——此后只有策略升版会把它挪到新队列。
/// 返回 `(行, 是否新增)`；`false` 表示这条事件在这一版策略下已经判过。
pub fn record_decision(
    store: &Store,
    trigger_id: &str,
    policy_version: i64,
    verdict: &GateVerdict,
) -> Result<(DecisionRow, bool), String> {
    let id = format!("sadec-{}", uuid::Uuid::new_v4().simple());
    let now = now_ms();
    let rules = serde_json::to_string(&verdict.matched_rules).unwrap_or_else(|_| "[]".to_string());
    let basis = truncate_chars(&verdict.basis, MAX_BASIS_CHARS);
    let reason = truncate_chars(&verdict.reason, MAX_REASON_CHARS);
    store.with_conn(|c| {
        let inserted = c
            .execute(
                "INSERT INTO sa_decisions (id, trigger_id, policy_version, strength, outcome, \
                    would_accept, basis, matched_rules, reason, decided_at_ms, assess_queue_version) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
                 ON CONFLICT (trigger_id, policy_version) DO NOTHING",
                params![
                    id,
                    trigger_id,
                    policy_version,
                    verdict.strength,
                    verdict.outcome,
                    verdict.would_accept as i64,
                    basis,
                    rules,
                    reason,
                    now,
                    policy_version,
                ],
            )
            .map_err(|e| format!("SUPERAGENT_DECISION_INSERT_FAILED: {e}"))?
            == 1;
        let row = decision_by_trigger(c, trigger_id, policy_version)?;
        Ok((row, inserted))
    })
}

fn decision_by_trigger(
    c: &Connection,
    trigger_id: &str,
    policy_version: i64,
) -> Result<DecisionRow, String> {
    c.query_row(
        "SELECT id, trigger_id, policy_version, strength, outcome, would_accept, basis, \
                matched_rules, reason, decided_at_ms FROM sa_decisions \
         WHERE trigger_id = ?1 AND policy_version = ?2",
        params![trigger_id, policy_version],
        map_decision_row,
    )
    .optional()
    .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))?
    .ok_or_else(|| {
        format!(
            "SUPERAGENT_DECISION_READ_FAILED: 判定行写不到（事件 {trigger_id}，策略 v{policy_version}）"
        )
    })
}

fn map_decision_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<DecisionRow> {
    Ok(DecisionRow {
        id: r.get(0)?,
        trigger_id: r.get(1)?,
        policy_version: r.get(2)?,
        strength: r.get(3)?,
        outcome: r.get(4)?,
        would_accept: r.get::<_, i64>(5)? != 0,
        basis: r.get(6)?,
        matched_rules: serde_json::from_str(&r.get::<_, String>(7)?).unwrap_or_default(),
        reason: r.get(8)?,
        decided_at_ms: r.get(9)?,
    })
}

/// 同一 Thread 在当前策略版本下是否已经有一条「本可接单」的判定。
///
/// DSH 的纪律是「一个 Thread 上同时只跑一次研判」：同一会话里连着五条消息不该
/// 攒出五个候选。这里把它钉成一次查询——空 thread_key 一律返回 false（无会话归属的事件
/// 各自独立，比如一条定时到点）。
pub fn thread_has_candidate(
    store: &Store,
    thread_key: &str,
    policy_version: i64,
) -> Result<bool, String> {
    let thread = thread_key.trim();
    if thread.is_empty() {
        return Ok(false);
    }
    store.with_conn(|c| {
        let hits: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sa_decisions d \
                 JOIN sa_triggers t ON t.id = d.trigger_id \
                 WHERE t.thread_key = ?1 AND d.policy_version = ?2 AND d.would_accept = 1",
                params![thread, policy_version],
                |r| r.get::<_, i64>(0),
            )
            .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))?;
        Ok(hits > 0)
    })
}

pub fn list_decisions(store: &Store, limit: Option<usize>) -> Result<Vec<DecisionRow>, String> {
    let limit = clamp_limit(limit);
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT id, trigger_id, policy_version, strength, outcome, would_accept, basis, \
                        matched_rules, reason, decided_at_ms FROM sa_decisions \
                 ORDER BY decided_at_ms DESC, id DESC LIMIT ?1",
            )
            .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))?;
        let rows = stmt
            .query_map(params![limit as i64], map_decision_row)
            .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))
    })
}

/// 影子模式的候选清单：判定为「本可接单」的事件 + 依据 + 策略版本，界面只读这一份
pub fn list_candidates(store: &Store, limit: Option<usize>) -> Result<Vec<Value>, String> {
    query_candidates(store, "", &[], limit)
}

/// 待做第二层研判的候选：排在当前生效策略版本队列里、还没有研判结论的阳性判定。
///
/// 三条限制都是成本口径：
/// - 只读阳性（阴性没有「值不值得开工」可问）；
/// - 只读**队列键等于当前版本**的行（`assess_queue_version`，不是 gate 的 `policy_version`：
///   策略升版时 `save_policy` 会把待研判候选重排到新队列，所以老候选不会孤悬，也不会被追判）；
/// - 只读 `assess_status = ''`（一条候选只判一次，失败过的不自动重试，见 `record_assessment`）。
pub fn list_unassessed_candidates(
    store: &Store,
    policy_version: i64,
    limit: Option<usize>,
) -> Result<Vec<Value>, String> {
    query_candidates(
        store,
        " AND d.assess_status = '' AND d.assess_queue_version = ?2",
        &[policy_version],
        limit,
    )
}

/// 本 UTC 日已做过第二层研判的条数（模型预算的分子；含失败的尝试，失败也花了钱）
pub fn count_assessed_today(store: &Store) -> Result<i64, String> {
    let since = utc_day_start_ms(now_ms());
    store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions \
             WHERE assessed_at_ms >= ?1 AND assess_status IN ('done', 'failed')",
            params![since],
            |r| r.get(0),
        )
        .map_err(|e| format!("SUPERAGENT_ASSESS_COUNT_FAILED: {e}"))
    })
}

/// 候选清单的公共查询：`extra_where` 只允许追加条件，追加参数（`?2` 起）排在 limit 之后绑定
fn query_candidates(
    store: &Store,
    extra_where: &str,
    extra_params: &[i64],
    limit: Option<usize>,
) -> Result<Vec<Value>, String> {
    let sql = format!(
        "SELECT d.id, d.trigger_id, d.policy_version, d.strength, d.outcome, d.would_accept, \
                d.basis, d.matched_rules, d.reason, d.decided_at_ms, \
                t.source_id, s.kind, s.instance, s.label, t.external_id, t.kind, t.text, \
                t.actor, t.thread_key, t.addressed_agent, t.arrived_at_ms, t.payload, \
                d.assess_status, d.assess_verdict, d.assess_reason, d.assess_model, \
                d.assess_tokens, d.assess_error, d.assessed_at_ms, \
                d.assess_queue_version, d.requeued_at_ms \
         FROM sa_decisions d \
         JOIN sa_triggers t ON t.id = d.trigger_id \
         JOIN sa_sources s ON s.id = t.source_id \
         WHERE d.would_accept = 1{extra_where} \
         ORDER BY d.decided_at_ms DESC, d.id DESC LIMIT ?1"
    );
    let mut binds: Vec<i64> = vec![clamp_limit(limit) as i64];
    binds.extend_from_slice(extra_params);
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&sql)
            .map_err(|e| format!("SUPERAGENT_CANDIDATE_READ_FAILED: {e}"))?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), map_candidate_row)
            .map_err(|e| format!("SUPERAGENT_CANDIDATE_READ_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SUPERAGENT_CANDIDATE_READ_FAILED: {e}"))
    })
}

/// 一行候选 → JSON（列序与 `query_candidates` 的 SELECT 一致；
/// 第二层的字段一律带 `assess` 前缀，`assessStatus` 为空串就是「还没判过」）
fn map_candidate_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let rules_text: String = r.get(7)?;
    Ok(json!({
        "decisionId": r.get::<_, String>(0)?,
        "triggerId": r.get::<_, String>(1)?,
        "policyVersion": r.get::<_, i64>(2)?,
        "strength": r.get::<_, String>(3)?,
        "outcome": r.get::<_, String>(4)?,
        "wouldAccept": r.get::<_, i64>(5)? != 0,
        "basis": r.get::<_, String>(6)?,
        "matchedRules": serde_json::from_str::<Vec<String>>(&rules_text)
            .unwrap_or_default(),
        "reason": r.get::<_, String>(8)?,
        "decidedAtMs": r.get::<_, i64>(9)?,
        "sourceId": r.get::<_, String>(10)?,
        "sourceKind": r.get::<_, String>(11)?,
        "sourceInstance": r.get::<_, String>(12)?,
        "sourceLabel": r.get::<_, String>(13)?,
        "externalId": r.get::<_, String>(14)?,
        "eventKind": r.get::<_, String>(15)?,
        "text": r.get::<_, String>(16)?,
        "actor": r.get::<_, String>(17)?,
        "threadKey": r.get::<_, String>(18)?,
        "addressedAgent": r.get::<_, i64>(19)? != 0,
        "arrivedAtMs": r.get::<_, i64>(20)?,
        "payload": parse_json(&r.get::<_, String>(21)?),
        // ── 第二层（模型研判）留痕 ──
        "assessStatus": r.get::<_, String>(22)?,
        "assessVerdict": r.get::<_, String>(23)?,
        "assessReason": r.get::<_, String>(24)?,
        "assessModel": r.get::<_, String>(25)?,
        "assessTokens": r.get::<_, i64>(26)?,
        "assessError": r.get::<_, String>(27)?,
        "assessedAtMs": r.get::<_, i64>(28)?,
        // gate 依据的版本（`policyVersion`）与现在排在哪个版本的研判队列里可以不同：
        // 策略升版会重排队，但不会改写判定留痕
        "assessQueueVersion": r.get::<_, i64>(29)?,
        "requeuedAtMs": r.get::<_, i64>(30)?,
    }))
}

/// 回写第二层的研判结论。
///
/// 两个条件一起决定「这一笔能不能写」：
/// - `assess_status = ''`：一条判定只接受第一次回写。重判等于再花一次模型钱，影子阶段不开这个口子。
/// - `assess_queue_version = ?8`：**发起这一轮时读到的队列版本**必须还是当前版本。策略在模型
///   回答期间升了版，`save_policy` 已把这一行重排到新队列，这一份结论是按旧策略的语境问出来的，
///   作废它（返回 `Superseded`）比留下一个「版本对不上却看不出来」的结论更诚实。
///   作废不等于白花时间：token 在发出调用时就已记进配额与用量表，钱是真的花了，只是结论不用。
///
/// 写不进去时补一次单行查询区分「并发轮已写过」与「已被重排」，两者都不算失败。
pub fn record_assessment(
    store: &Store,
    decision_id: &str,
    queue_version: i64,
    assessment: &crate::superagent::assess::Assessment,
) -> Result<AssessmentWrite, String> {
    let reason = truncate_chars(assessment.reason.trim(), MAX_REASON_CHARS);
    let error = truncate_chars(assessment.error.trim(), MAX_ERROR_CHARS);
    let model = truncate_chars(assessment.model.trim(), MAX_BASIS_CHARS);
    let now = now_ms();
    store.with_conn(|c| {
        let changed = c
            .execute(
                "UPDATE sa_decisions SET assess_status = ?1, assess_verdict = ?2, \
                    assess_reason = ?3, assess_model = ?4, assess_tokens = ?5, \
                    assess_error = ?6, assessed_at_ms = ?7 \
                 WHERE id = ?9 AND assess_status = '' AND assess_queue_version = ?8",
                params![
                    assessment.status,
                    assessment.verdict,
                    reason,
                    model,
                    assessment.tokens,
                    error,
                    now,
                    queue_version,
                    decision_id,
                ],
            )
            .map_err(|e| format!("SUPERAGENT_ASSESS_WRITE_FAILED: {e}"))?;
        if changed == 1 {
            return Ok(AssessmentWrite::Written);
        }
        let state = c
            .query_row(
                "SELECT assess_status <> '', assess_queue_version = ?2 FROM sa_decisions WHERE id = ?1",
                params![decision_id, queue_version],
                |r| Ok((r.get::<_, i64>(0)? == 1, r.get::<_, i64>(1)? == 1)),
            )
            .optional()
            .map_err(|e| format!("SUPERAGENT_ASSESS_WRITE_FAILED: {e}"))?;
        Ok(match state {
            // 行已经不在了（换库/清理）：没什么可写的，按已判待处理，不报错也不重试
            None => AssessmentWrite::AlreadyJudged,
            Some((judged, same_queue)) => {
                if judged || same_queue {
                    AssessmentWrite::AlreadyJudged
                } else {
                    AssessmentWrite::Superseded
                }
            }
        })
    })
}

/// 本 UTC 日已判定条数（每日研判上限的分子，由事件循环读取）
pub fn count_decisions_today(store: &Store) -> Result<i64, String> {
    let since = utc_day_start_ms(now_ms());
    store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions WHERE decided_at_ms >= ?1",
            params![since],
            |r| r.get(0),
        )
        .map_err(|e| format!("SUPERAGENT_DECISION_COUNT_FAILED: {e}"))
    })
}

/// 台账概览（界面与自测共用同一份口径，不各自数）
pub fn stats(store: &Store) -> Result<Value, String> {
    let (version, doc) = effective_policy(store);
    let sources = list_sources(store)?;
    let enabled_sources = sources.iter().filter(|s| s.enabled).count();
    let count = |where_sql: &str| -> Result<i64, String> {
        store.with_conn(|c| {
            c.query_row(
                &format!("SELECT COUNT(*) FROM sa_triggers t WHERE 1=1{where_sql}"),
                [],
                |r| r.get(0),
            )
            .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
        })
    };
    let triggers = count("")?;
    let decisions = store.with_conn(|c| {
        c.query_row("SELECT COUNT(*) FROM sa_decisions", [], |r| r.get::<_, i64>(0))
            .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
    })?;
    let candidates = store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions WHERE would_accept = 1",
            [],
            |r| r.get::<_, i64>(0),
        )
        .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
    })?;
    // 第二层（模型研判）的账：判过多少、今天判过多少、当前策略版本下还欠多少
    let assessed = store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions WHERE assess_status = 'done'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
    })?;
    let assess_pending = store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions \
             WHERE would_accept = 1 AND assess_status = '' AND assess_queue_version = ?1",
            params![version],
            |r| r.get::<_, i64>(0),
        )
        .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
    })?;
    // 历史上被策略升版重排过的候选条数（界面用它回答「改了策略有没有把候选弄丢」）
    let requeued = store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM sa_decisions WHERE requeued_at_ms > 0",
            [],
            |r| r.get::<_, i64>(0),
        )
        .map_err(|e| format!("SUPERAGENT_STATS_FAILED: {e}"))
    })?;
    Ok(json!({
        "ok": true,
        "acceptMode": doc.accept_mode,
        "policyVersion": version,
        "dailyEvalCap": doc.daily_eval_cap,
        "assessCap": doc.assess_cap(),
        "decidedToday": count_decisions_today(store).unwrap_or(0),
        "assessedToday": count_assessed_today(store).unwrap_or(0),
        "assessed": assessed,
        "assessPending": assess_pending,
        "requeued": requeued,
        "sources": sources.len(),
        "enabledSources": enabled_sources,
        "triggers": triggers,
        "decisions": decisions,
        "candidates": candidates,
        "note": "影子模式：候选只是「本可接单」的记录，模型研判只给「值不值得开工」的判断，没有任何事件被自动执行",
    }))
}

// ── 小工具 ────────────────────────────────────────────────────

fn clamp_limit(limit: Option<usize>) -> usize {
    limit.unwrap_or(DEFAULT_LIST_LIMIT).clamp(1, MAX_LIST_LIMIT)
}

fn truncate_chars(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    text.chars().take(max).collect()
}

/// 序列化并限制体积：超了只留「已截断」标记，不把整份上游结构搬进桌面库，
/// 顺带避免把结构里可能出现的凭据字段原样落盘
fn compact_json(value: &Value, max: usize) -> String {
    let text = serde_json::to_string(value).unwrap_or_else(|_| "{}".to_string());
    if text.chars().count() <= max {
        return text;
    }
    json!({ "truncated": true }).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::superagent::{gate, policy};
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-sa-{name}-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("tongtian.db")).unwrap();
        (store, dir)
    }

    fn cleanup(dir: &PathBuf) {
        let _ = std::fs::remove_dir_all(dir);
    }

    fn trigger(source_id: &str, external_id: &str, seq: i64) -> NewTrigger {
        NewTrigger {
            source_id: source_id.to_string(),
            external_id: external_id.to_string(),
            kind: "task.commented".to_string(),
            text: format!("帮我评审这份部署方案（{external_id}），谢谢"),
            actor: "usr-owner".to_string(),
            thread_key: "tsk-1".to_string(),
            addressed_agent: false,
            source_seq: seq,
            payload: json!({ "seq": seq, "taskId": "tsk-1" }),
            arrived_at_ms: Some(1_730_000_000_000 + seq),
        }
    }

    fn review_policy_doc(mode: &str) -> Value {
        json!({
            "acceptMode": mode,
            "dailyEvalCap": 120,
            "sources": [{
                "kind": "project_event", "instance": "prj-1", "enabled": true,
                "label": "演练项目",
                "allowUsers": ["usr-owner"], "bots": [],
                "rules": [{"id": "review", "enabled": true, "kinds": ["task.commented"],
                           "textAny": ["帮我评审"], "minChars": 6}]
            }]
        })
    }

    #[test]
    fn register_is_idempotent_and_keeps_cursor_on_repeat() {
        let (store, dir) = temp_store("register");
        let first =
            register_source(&store, "project_event", "prj-1", "演练项目", json!({"watch": ["task.commented"]}))
                .unwrap();
        assert_eq!(first.cursor, 0);
        assert!(!first.enabled, "登记不等于开跑");
        // 先把游标推到一个位置，再重复登记：倒回 0 就等于重启后重放历史事件
        set_source_enabled(&store, &first.id, true).unwrap();
        advance_cursor(&store, &first.id, 9_000).unwrap();
        let again = register_source(&store, "project_event", "prj-1", "改过标签", json!({})).unwrap();
        assert_eq!(again.id, first.id, "同一入口重复登记不该产生第二行");
        assert_eq!(again.cursor, 9_000, "重复登记不许把读取位置倒回去");
        assert!(again.enabled, "重复登记不许悄悄关掉已在读的入口");
        assert_eq!(again.label, "改过标签");
        assert_eq!(list_sources(&store).unwrap().len(), 1);
        cleanup(&dir);
    }

    #[test]
    fn register_rejects_unsupported_kind_and_missing_instance() {
        let (store, dir) = temp_store("register-bad");
        let err = register_source(&store, "email", "inbox", "", json!({})).unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_KIND_UNSUPPORTED"), "{err}");
        let err = register_source(&store, "project_event", "  ", "", json!({})).unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_INSTANCE_REQUIRED"), "{err}");
        let err = register_source(&store, "im_inbound", "", "", json!({})).unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_INSTANCE_REQUIRED"), "{err}");
        // 本机唯一入口允许空 instance
        let row = register_source(&store, "workspace_file", "", "工作区", json!({})).unwrap();
        assert_eq!(row.instance, "");
        cleanup(&dir);
    }

    #[test]
    fn cursor_only_moves_forward() {
        let (store, dir) = temp_store("cursor");
        let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
        assert!(advance_cursor(&store, &source.id, 5).unwrap());
        assert!(advance_cursor(&store, &source.id, 7).unwrap());
        // 迟到的旧事件与重复投递都不许把游标拖回去（seq 全局自增，本项目里 5→7 正常）
        assert!(!advance_cursor(&store, &source.id, 6).unwrap());
        assert!(!advance_cursor(&store, &source.id, 7).unwrap());
        assert_eq!(get_source(&store, &source.id).unwrap().unwrap().cursor, 7);
        cleanup(&dir);
    }

    #[test]
    fn duplicate_delivery_yields_one_trigger_row() {
        let (store, dir) = temp_store("dedupe");
        let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
        let first = ingest(&store, &trigger(&source.id, "7", 7)).unwrap();
        assert!(first.inserted);
        assert!(first.cursor_advanced);
        // 断线补拉重叠：同一条事件再来一次，只拿到已有那一行，不新增
        let again = ingest(&store, &trigger(&source.id, "7", 7)).unwrap();
        assert!(!again.inserted, "同一条事件重复投递不许产生第二行");
        assert_eq!(again.trigger.id, first.trigger.id, "必须回到同一个工作单元");
        assert!(!again.cursor_advanced);
        assert_eq!(list_triggers(&store, None).unwrap().len(), 1);
        cleanup(&dir);
    }

    #[test]
    fn restart_reads_from_stored_cursor_and_does_not_replay() {
        // 「关闭再打开应用不重放历史事件」的等价测试：重新 open 同一个库文件，
        // 从落盘游标续读；重叠区段按 external_id 幂等，不重复记账也不倒退游标
        let (store, dir) = temp_store("restart");
        let source_id = {
            ensure_default_policy(&store).unwrap();
            let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
            set_source_enabled(&store, &source.id, true).unwrap();
            for seq in [11_i64, 13, 14] {
                ingest(&store, &trigger(&source.id, &seq.to_string(), seq)).unwrap();
            }
            source.id
        };
        drop(store);
        // 进程重启：重新打开同一份库
        let store = Store::open(dir.join("tongtian.db")).unwrap();
        let source = get_source(&store, &source_id).unwrap().expect("游标必须落盘");
        assert!(source.enabled);
        assert_eq!(source.cursor, 14, "重启后读取位置应停在最后一条已交付事件");
        assert_eq!(list_triggers(&store, None).unwrap().len(), 3);
        // 服务端按 since=14 续读；这里模拟它把 14 又发了一次，然后只给新的 15
        let replay = ingest(&store, &trigger(&source_id, "14", 14)).unwrap();
        assert!(!replay.inserted, "重放的那一条不许变成新工作单元");
        let fresh = ingest(&store, &trigger(&source_id, "15", 15)).unwrap();
        assert!(fresh.inserted);
        assert_eq!(list_triggers(&store, None).unwrap().len(), 4, "只有真正的新事件才进账本");
        assert_eq!(get_source(&store, &source_id).unwrap().unwrap().cursor, 15);
        cleanup(&dir);
    }

    #[test]
    fn ingest_without_registered_source_fails_loudly_and_writes_nothing() {
        let (store, dir) = temp_store("no-source");
        let err = ingest(&store, &trigger("sasrc-missing", "1", 1)).unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_NOT_FOUND"), "{err}");
        assert!(list_triggers(&store, None).unwrap().is_empty(), "入口不存在时不许留下孤行");
        let err = {
            let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
            let mut input = trigger(&source.id, "  ", 5);
            input.external_id = "  ".to_string();
            ingest(&store, &input).unwrap_err()
        };
        assert!(err.contains("SUPERAGENT_TRIGGER_ID_MISSING"), "{err}");
        cleanup(&dir);
    }

    #[test]
    fn long_text_is_capped_and_oversized_payload_is_not_stored() {
        let (store, dir) = temp_store("caps");
        let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
        let long_text = "帮".repeat(MAX_TRIGGER_TEXT_CHARS + 500);
        let input = NewTrigger {
            source_id: source.id.clone(),
            external_id: "42".to_string(),
            kind: "task.commented".to_string(),
            text: long_text,
            actor: "usr-owner".to_string(),
            thread_key: "tsk-1".to_string(),
            addressed_agent: true,
            source_seq: 42,
            payload: json!({ "body": "x".repeat(MAX_PAYLOAD_CHARS * 2), "token": "secret-token" }),
            arrived_at_ms: Some(1_730_000_000_000),
        };
        let outcome = ingest(&store, &input).unwrap();
        assert_eq!(outcome.trigger.text.chars().count(), MAX_TRIGGER_TEXT_CHARS);
        let payload = outcome.trigger.payload;
        assert_eq!(payload["truncated"].as_bool(), Some(true), "{payload}");
        let text = payload.to_string();
        assert!(!text.contains("secret-token"), "上游结构里的凭据字段不许原样入库");
        assert!(!text.contains('x'), "超限时连预览都不该带上整段原文：{text}");
        cleanup(&dir);
    }

    #[test]
    fn default_policy_is_written_once_and_reports_current_version() {
        let (store, dir) = temp_store("policy");
        assert_eq!(ensure_default_policy(&store).unwrap(), 1);
        assert_eq!(ensure_default_policy(&store).unwrap(), 1, "已有版本不许再写一份缺省策略");
        let (version, doc) = effective_policy(&store);
        assert_eq!(version, 1);
        assert_eq!(doc.accept_mode, policy::MODE_OBSERVE);
        assert!(doc.sources.is_empty());
        cleanup(&dir);
    }

    #[test]
    fn saving_same_policy_does_not_bump_version() {
        let (store, dir) = temp_store("policy-save");
        ensure_default_policy(&store).unwrap();
        let first = save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
        assert_eq!(first.version, 2);
        assert!(first.changed);
        // 同一份内容再存一遍：digest 相同 ⇒ 不新增版本
        let again = save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
        assert_eq!(again.version, 2, "内容没变不许新增策略版本");
        assert!(!again.changed);
        // 多给一个人指派权就必须 +1，并留下旧版本可追溯
        let wider = json!({
            "acceptMode": "observe", "dailyEvalCap": 120,
            "sources": [{
                "kind": "project_event", "instance": "prj-1", "enabled": true, "label": "演练项目",
                "allowUsers": ["usr-owner", "usr-peer"], "bots": [],
                "rules": [{"id": "review", "enabled": true, "kinds": ["task.commented"],
                           "textAny": ["帮我评审"], "minChars": 6}]
            }]
        });
        let third = save_policy(&store, &wider, "user").unwrap();
        assert_eq!(third.version, 3);
        let old = policy_at_version(&store, 2).unwrap().unwrap();
        assert_eq!(old.sources[0].allow_users, vec!["usr-owner".to_string()]);
        assert_eq!(list_policy_versions(&store, None).unwrap().len(), 3);
        cleanup(&dir);
    }

    /// 造一条真阳性判定（走 `gate` + `record_decision`，不手插 SQL，免得测的是假形状）
    fn seed_candidate(store: &Store, version: i64, external_id: &str) -> String {
        let source = register_source(store, "project_event", "prj-1", "演练项目", json!({})).unwrap();
        let doc = policy_at_version(store, version).unwrap().unwrap();
        let outcome = ingest(store, &trigger(&source.id, external_id, 90)).unwrap();
        let verdict = gate::decide(
            &doc,
            &gate::GateInput {
                kind: "project_event",
                instance: "prj-1",
                actor: "usr-owner",
                event_kind: "task.commented",
                text: &outcome.trigger.text,
                thread_key: "tsk-1",
                addressed_agent: outcome.trigger.addressed_agent,
            },
        );
        assert_eq!(verdict.outcome, gate::OUTCOME_WOULD_ACCEPT, "种子必须是阳性");
        let (row, _) = record_decision(store, &outcome.trigger.id, version, &verdict).unwrap();
        row.id
    }

    fn judgement(text: &str) -> crate::superagent::assess::Assessment {
        crate::superagent::assess::Assessment {
            status: crate::superagent::assess::STATUS_DONE.to_string(),
            verdict: crate::superagent::assess::VERDICT_WORTH.to_string(),
            reason: text.to_string(),
            model: "演练模型".to_string(),
            tokens: 1200,
            error: String::new(),
        }
    }

    /// 策略升版 → 待研判候选重排到新队列，而判定版本（留痕）一个字都不改。
    /// 这一条测的是 DSH 的「变更即失效重排队」：老候选不许永远孤悬在账本里没人处理。
    #[test]
    fn policy_bump_requeues_pending_candidates_without_rewriting_the_gate_version() {
        let (store, dir) = temp_store("requeue");
        ensure_default_policy(&store).unwrap();
        let v2 = save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
        assert_eq!(v2.version, 2);
        let decision_id = seed_candidate(&store, 2, "9");

        // 升版前：v2 队列里躺着这一条，v3 队列是空的
        let before = list_unassessed_candidates(&store, 2, None).unwrap();
        assert_eq!(before.len(), 1);
        assert_eq!(list_unassessed_candidates(&store, 3, None).unwrap().len(), 0);
        let before_reason = before[0]["reason"].clone();
        let before_decided_at = before[0]["decidedAtMs"].clone();

        let wider = json!({
            "acceptMode": "observe", "dailyEvalCap": 120,
            "sources": [{
                "kind": "project_event", "instance": "prj-1", "enabled": true,
                "allowUsers": ["usr-owner", "usr-peer"], "bots": [], "rules": []
            }]
        });
        let bump = save_policy(&store, &wider, "user").unwrap();
        assert_eq!(bump.version, 3);
        assert_eq!(bump.requeued, 1, "改了策略要如实报出重排了几条");

        // 升版后：这一条改排在 v3 队列里，v2 队列空了（界面与统计都按当前版本读）
        assert_eq!(list_unassessed_candidates(&store, 2, None).unwrap().len(), 0);
        let moved = list_unassessed_candidates(&store, 3, None).unwrap();
        assert_eq!(moved.len(), 1, "待研判候选不许因为升版而消失");
        assert_eq!(moved[0]["decisionId"].as_str(), Some(decision_id.as_str()));
        // 留痕不能被改写：这一条仍然是「依据 v2 判出来的」，只是待办挂到了 v3
        assert_eq!(moved[0]["policyVersion"].as_i64(), Some(2));
        assert_eq!(moved[0]["assessQueueVersion"].as_i64(), Some(3));
        assert!(moved[0]["requeuedAtMs"].as_i64().unwrap_or(0) > 0, "重排要留下时刻");
        assert_eq!(moved[0]["assessStatus"].as_str(), Some(""), "重排不等于已研判");
        assert_eq!(
            moved[0]["reason"], before_reason,
            "重排只挪队列键，判定理由与留痕时刻都不许动"
        );
        assert_eq!(moved[0]["decidedAtMs"], before_decided_at);

        // 内容没变的保存不新增版本；重排照跑但是幂等的，已经在 v3 队列里的一条也不动
        let same = save_policy(&store, &wider, "user").unwrap();
        assert!(!same.changed);
        assert_eq!(same.requeued, 0, "重排是幂等的：不因为多点一次保存就把候选再挪一遍");
        // 统计里「待研判」跟着队列键走，不因为换版就报 0
        let stats = stats(&store).unwrap();
        assert_eq!(stats["policyVersion"].as_i64(), Some(3));
        assert_eq!(stats["assessPending"].as_i64(), Some(1));
        assert_eq!(stats["requeued"].as_i64(), Some(1));
        cleanup(&dir);
    }

    /// 重排只碰「还没研判的阳性候选」：判过的留着（重判等于再花一次模型钱），阴性本来就不在队列里。
    #[test]
    fn judged_and_negative_rows_stay_put_when_the_policy_bumps() {
        let (store, dir) = temp_store("requeue-scope");
        ensure_default_policy(&store).unwrap();
        let v2 = save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
        let judged = seed_candidate(&store, 2, "9");
        let pending = seed_candidate(&store, 2, "10");
        assert_eq!(
            record_assessment(&store, &judged, 2, &judgement("上下文里正开着")).unwrap(),
            AssessmentWrite::Written
        );
        // 一条阴性留痕：不在第二层队列里，升版也不该碰它
        let source = register_source(&store, "project_event", "prj-1", "演练项目", json!({})).unwrap();
        let denied = ingest(&store, &trigger(&source.id, "11", 91)).unwrap();
        let negative = gate::GateVerdict {
            strength: gate::STRENGTH_NOT_ADMITTED.to_string(),
            outcome: gate::OUTCOME_NOT_ADMITTED.to_string(),
            basis: String::new(),
            matched_rules: vec![],
            would_accept: false,
            reason: "名单外".to_string(),
        };
        let (denied_row, _) = record_decision(&store, &denied.trigger.id, 2, &negative).unwrap();

        let bump = save_policy(
            &store,
            &json!({"acceptMode": "observe", "dailyEvalCap": 60, "sources": []}),
            "user",
        )
        .unwrap();
        assert_eq!(bump.version, 3);
        assert_eq!(bump.requeued, 1, "只有还没研判的那条阳性被重排：{pending}");

        let rows = list_candidates(&store, None).unwrap();
        let by_id = |id: &str| {
            rows.iter()
                .find(|row| row["decisionId"].as_str() == Some(id))
                .cloned()
                .unwrap_or(Value::Null)
        };
        let judged_row = by_id(&judged);
        assert_eq!(judged_row["assessStatus"].as_str(), Some("done"), "结论要留着可追溯");
        assert_eq!(judged_row["assessQueueVersion"].as_i64(), Some(2), "已研判的不重排");
        assert_eq!(judged_row["requeuedAtMs"].as_i64(), Some(0));
        assert_eq!(judged_row["assessVerdict"].as_str(), Some("worth"));
        let pending_row = by_id(&pending);
        assert_eq!(pending_row["assessQueueVersion"].as_i64(), Some(3));
        assert!(
            pending_row["requeuedAtMs"].as_i64().unwrap_or(0) > 0,
            "重排过的那条要留下时刻"
        );
        // 候选清单只列阳性（`would_accept = 1`），所以阴性那一行要从表里直接读，不能问清单
        let (denied_queue, denied_requeued, denied_status): (i64, i64, String) = store
            .with_conn(|c| {
                c.query_row(
                    "SELECT assess_queue_version, requeued_at_ms, assess_status \
                     FROM sa_decisions WHERE id = ?1",
                    rusqlite::params![denied_row.id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .map_err(|e| format!("SUPERAGENT_DECISION_READ_FAILED: {e}"))
            })
            .unwrap();
        assert_eq!(denied_queue, 2, "阴性从没进过第二层队列，重排不许碰它");
        assert_eq!(denied_requeued, 0, "没进过队列的行不该被打上重排标记");
        assert_eq!(denied_status.as_str(), "", "阴性判定没有第二层结论");
        assert!(
            list_candidates(&store, None)
                .unwrap()
                .iter()
                .all(|row| row["decisionId"].as_str() != Some(denied_row.id.as_str())),
            "阴性判定不许混进候选清单"
        );
        cleanup(&dir);
    }

    /// 队列键落后于当前版本的候选（上一次保存重排失败、或 V17 时代留下的孤儿）要能自愈：
    /// 内容一个字没改再点一次「保存策略」，不新增版本，但把这些候选拉回当前队列。
    #[test]
    fn an_unchanged_save_still_pulls_lagging_candidates_back_into_the_current_queue() {
        let (store, dir) = temp_store("requeue-heal");
        ensure_default_policy(&store).unwrap();
        let raw = review_policy_doc("observe");
        let v2 = save_policy(&store, &raw, "user").unwrap();
        assert_eq!(v2.version, 2);
        let decision_id = seed_candidate(&store, 2, "9");
        // 手工造成落后：这一行还挂在 v1 的队列里，正是 V17 升版后留下的孤儿形状
        store
            .with_conn(|c| {
                c.execute(
                    "UPDATE sa_decisions SET assess_queue_version = 1 WHERE id = ?1",
                    params![decision_id],
                )
                .map_err(|e| format!("SUPERAGENT_POLICY_REQUEUE_FAILED: {e}"))
            })
            .unwrap();
        assert_eq!(
            list_unassessed_candidates(&store, 2, None).unwrap().len(),
            0,
            "按当前版本取队列取不到它——这就是它永远不被研判的原因"
        );
        assert_eq!(
            stats(&store).unwrap()["assessPending"].as_i64(),
            Some(0),
            "统计只数当前队列，界面因此看不出欠账"
        );

        let again = save_policy(&store, &raw, "user").unwrap();
        assert!(!again.changed, "内容没变不新增版本");
        assert_eq!(again.version, 2);
        assert_eq!(again.requeued, 1, "落后的候选要能被这一次保存拉回来：{again:?}");
        let healed = list_unassessed_candidates(&store, 2, None).unwrap();
        assert_eq!(healed.len(), 1, "拉回来后当前队列就看得见它");
        assert_eq!(healed[0]["decisionId"].as_str(), Some(decision_id.as_str()));
        assert_eq!(healed[0]["policyVersion"].as_i64(), Some(2), "自愈也不改留痕版本");
        assert!(healed[0]["requeuedAtMs"].as_i64().unwrap_or(0) > 0, "重排要留下时刻");
        assert_eq!(stats(&store).unwrap()["assessPending"].as_i64(), Some(1));
        // 幂等：已经在当前队列的不再挪，多点几次保存不会把候选反复搬来搬去
        let third = save_policy(&store, &raw, "user").unwrap();
        assert_eq!(third.requeued, 0, "重排是幂等的");
        cleanup(&dir);
    }

    /// 在跑的研判遇到策略升版：结论作废（`Superseded`），候选按新版本还能再判一次。
    /// 这就是 DSH 的「丢弃在跑研判 + 重排队」在同一行上的两半，也是 #100 当时唯一没落地的那句。
    #[test]
    fn a_verdict_from_a_stale_round_is_dropped_and_the_candidate_gets_rejudged() {
        let (store, dir) = temp_store("requeue-supersede");
        ensure_default_policy(&store).unwrap();
        let v2 = save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
        let decision_id = seed_candidate(&store, 2, "9");

        // 后台一轮按 v2 取出候选、问完模型；这期间用户把策略改成了 v3
        let bump = save_policy(
            &store,
            &json!({"acceptMode": "observe", "dailyEvalCap": 60, "sources": []}),
            "user",
        )
        .unwrap();
        assert_eq!(bump.requeued, 1);

        // 迟到的结论按旧队列版本回写 → 拒收，且不动那一行
        assert_eq!(
            record_assessment(&store, &decision_id, 2, &judgement("按旧策略问出来的结论")).unwrap(),
            AssessmentWrite::Superseded
        );
        let stale = list_candidates(&store, None).unwrap()
            .into_iter()
            .find(|row| row["decisionId"].as_str() == Some(decision_id.as_str()))
            .unwrap();
        assert_eq!(stale["assessStatus"].as_str(), Some(""), "作废的结论不许留在判定行里");
        assert_eq!(stale["assessVerdict"].as_str(), Some(""));
        assert_eq!(stale["assessReason"].as_str(), Some(""));
        assert_eq!(stale["assessQueueVersion"].as_i64(), Some(3));

        // 新版本队列里还有它：下一轮照常判，这次写得进去
        assert_eq!(list_unassessed_candidates(&store, 3, None).unwrap().len(), 1);
        assert_eq!(
            record_assessment(&store, &decision_id, 3, &judgement("按新策略重问的结论")).unwrap(),
            AssessmentWrite::Written
        );
        assert_eq!(count_assessed_today(&store).unwrap(), 1, "重判花的钱要进当日预算分子");
        // 判过之后再拿旧版本号回写：是「已有结论」，不是「被重排」
        assert_eq!(
            record_assessment(&store, &decision_id, 2, &judgement("第三次")).unwrap(),
            AssessmentWrite::AlreadyJudged
        );
        assert_eq!(list_unassessed_candidates(&store, 3, None).unwrap().len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn auto_policy_is_refused_until_ticket_layers_exist() {
        let (store, dir) = temp_store("policy-auto");
        ensure_default_policy(&store).unwrap();
        let err = save_policy(&store, &review_policy_doc("auto"), "user").unwrap_err();
        assert!(err.contains("SUPERAGENT_MODE_NOT_AVAILABLE"), "{err}");
        // 被挡住之后生效档位仍是 observe，且没多出一个版本
        let (version, doc) = effective_policy(&store);
        assert_eq!(version, 1);
        assert_eq!(doc.accept_mode, policy::MODE_OBSERVE);
        cleanup(&dir);
    }

    #[test]
    fn corrupt_or_stored_auto_policy_falls_back_to_conservative_default() {
        let (store, dir) = temp_store("policy-corrupt");
        ensure_default_policy(&store).unwrap();
        // 模拟半截写入或人为改坏：正文不是合法 JSON
        store
            .with_conn(|c| {
                c.execute("UPDATE sa_policy SET doc = ?1 WHERE version = 1", params!["not-json"])
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        let (version, doc) = effective_policy(&store);
        assert_eq!(version, 1, "版本号要如实报出，界面才知道读的是哪一版");
        assert_eq!(doc.accept_mode, policy::MODE_OBSERVE, "策略损坏只能回落到最保守档位");
        assert!(doc.sources.is_empty(), "策略损坏不许保留任何入口授权");
        // 手工塞进一版写着 auto 的旧数据：读出来按 observe 生效，不放开执行
        store
            .with_conn(|c| {
                let mut legacy = policy::default_doc();
                legacy.accept_mode = policy::MODE_AUTO.to_string();
                c.execute(
                    "INSERT INTO sa_policy (version, doc, digest, source, created_at_ms) \
                     VALUES (9, ?1, 'legacy', 'import', 1)",
                    params![policy::canonical_json(&legacy)],
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
            })
            .unwrap();
        let (version, doc) = effective_policy(&store);
        assert_eq!(version, 9);
        assert_eq!(doc.accept_mode, policy::MODE_OBSERVE, "旧库里写着 auto 也不放开执行");
        cleanup(&dir);
    }

    #[test]
    fn decision_is_unique_per_policy_version_and_keeps_history() {
        let (store, dir) = temp_store("decision");
        ensure_default_policy(&store).unwrap();
        let (version, doc) = {
            save_policy(&store, &review_policy_doc("observe"), "user").unwrap();
            effective_policy(&store)
        };
        assert_eq!(version, 2);
        let source = register_source(&store, "project_event", "prj-1", "演练项目", json!({})).unwrap();
        let outcome = ingest(&store, &trigger(&source.id, "9", 9)).unwrap();
        let verdict = gate::decide(
            &doc,
            &gate::GateInput {
                kind: "project_event",
                instance: "prj-1",
                actor: "usr-owner",
                event_kind: "task.commented",
                text: &outcome.trigger.text,
                thread_key: "tsk-1",
                addressed_agent: outcome.trigger.addressed_agent,
            },
        );
        assert_eq!(verdict.outcome, gate::OUTCOME_WOULD_ACCEPT);
        let (row, inserted) = record_decision(&store, &outcome.trigger.id, version, &verdict).unwrap();
        assert!(inserted);
        assert_eq!(row.policy_version, 2);
        assert!(!row.basis.is_empty(), "判定要留下依据");
        // 同一版策略重判不许覆盖历史（重启后续跑、循环重入都会走这里）
        let (_, again) = record_decision(&store, &outcome.trigger.id, version, &verdict).unwrap();
        assert!(!again, "同一事件同一策略版本只留一条判定");
        let candidates = list_candidates(&store, None).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0]["strength"].as_str(), Some(gate::STRENGTH_INFERRED));
        assert_eq!(candidates[0]["sourceKind"].as_str(), Some("project_event"));
        assert_eq!(candidates[0]["sourceInstance"].as_str(), Some("prj-1"));
        assert_eq!(candidates[0]["policyVersion"].as_i64(), Some(2));
        assert_eq!(candidates[0]["wouldAccept"].as_bool(), Some(true));
        assert_eq!(candidates[0]["externalId"].as_str(), Some("9"));
        // 策略变更后按新版本重判，旧判定仍在，能回答「当时依据哪一版」
        let (version2, stricter) = {
            let raw = json!({"acceptMode": "observe", "dailyEvalCap": 60, "sources": []});
            save_policy(&store, &raw, "user").unwrap();
            effective_policy(&store)
        };
        assert_eq!(version2, 3);
        let denied = gate::decide(
            &stricter,
            &gate::GateInput {
                kind: "project_event",
                instance: "prj-1",
                actor: "usr-owner",
                event_kind: "task.commented",
                text: &outcome.trigger.text,
                thread_key: "tsk-1",
                addressed_agent: false,
            },
        );
        assert_eq!(denied.outcome, gate::OUTCOME_NOT_ADMITTED);
        let (_, inserted_v3) = record_decision(&store, &outcome.trigger.id, version2, &denied).unwrap();
        assert!(inserted_v3, "策略变更后必须允许按新版本重判");
        assert_eq!(list_decisions(&store, None).unwrap().len(), 2, "旧判定不许被删");
        assert_eq!(
            list_candidates(&store, None).unwrap().len(),
            1,
            "候选清单只数 would_accept = 1 的判定"
        );
        assert_eq!(count_decisions_today(&store).unwrap(), 2);
        cleanup(&dir);
    }

    #[test]
    fn stats_reports_shadow_mode_without_any_execution_claim() {
        let (store, dir) = temp_store("stats");
        ensure_default_policy(&store).unwrap();
        let source = register_source(&store, "project_event", "prj-1", "演练项目", json!({})).unwrap();
        set_source_enabled(&store, &source.id, true).unwrap();
        ingest(&store, &trigger(&source.id, "3", 3)).unwrap();
        let value = stats(&store).unwrap();
        assert_eq!(value["acceptMode"].as_str(), Some(policy::MODE_OBSERVE));
        assert_eq!(value["policyVersion"].as_i64(), Some(1));
        assert_eq!(value["sources"].as_i64(), Some(1));
        assert_eq!(value["enabledSources"].as_i64(), Some(1));
        assert_eq!(value["triggers"].as_i64(), Some(1));
        assert_eq!(value["decisions"].as_i64(), Some(0), "没研判就不许有判定");
        assert_eq!(value["candidates"].as_i64(), Some(0));
        assert!(value["note"].as_str().unwrap().contains("没有任何事件被自动执行"));
        cleanup(&dir);
    }

    #[test]
    fn note_source_error_stores_readable_reason_without_credentials() {
        let (store, dir) = temp_store("error");
        let source = register_source(&store, "project_event", "prj-1", "", json!({})).unwrap();
        note_source_error(&store, &source.id, Some("无法连接协作服务（连接被拒绝）")).unwrap();
        let row = get_source(&store, &source.id).unwrap().unwrap();
        assert!(row.last_error.unwrap().contains("连接被拒绝"));
        note_source_error(&store, &source.id, None).unwrap();
        assert!(get_source(&store, &source.id).unwrap().unwrap().last_error.is_none());
        let err = note_source_error(&store, "sasrc-missing", Some("x")).unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_NOT_FOUND"), "{err}");
        // 超长原因截断，不撑爆界面
        let long = "说".repeat(MAX_ERROR_CHARS + 100);
        note_source_error(&store, &source.id, Some(&long)).unwrap();
        assert_eq!(
            get_source(&store, &source.id).unwrap().unwrap().last_error.unwrap().chars().count(),
            MAX_ERROR_CHARS
        );
        cleanup(&dir);
    }

    #[test]
    fn utc_day_boundary_counts_only_today() {
        assert_eq!(utc_day_start_ms(0), 0);
        assert_eq!(utc_day_start_ms(86_400_000), 86_400_000);
        assert_eq!(utc_day_start_ms(86_400_001), 86_400_000);
        // 异常值不多算一天（div_euclid 对负数向下取整）
        assert_eq!(utc_day_start_ms(-1), -86_400_000);
    }

    #[test]
    fn list_limit_is_clamped() {
        assert_eq!(clamp_limit(None), DEFAULT_LIST_LIMIT);
        assert_eq!(clamp_limit(Some(0)), 1, "一次至少取一条，否则界面会以为是空");
        assert_eq!(clamp_limit(Some(99_999)), MAX_LIST_LIMIT);
    }
}
