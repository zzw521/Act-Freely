//! 常驻事件入口与授权层（缺陷 D-46 · 影子段：账本 + 策略 + gate + 事件源，零自动执行）
//!
//! 这一段要解决的问题不是「让 Agent 更聪明」，而是「Agent 有没有依据在没人交代的情况下动手」。
//! 当前产品的任务只能由人在会话里发起，`harness` 与引擎都是请求—执行—交付；这一层把
//! 环境事件变成可记账、可判定、可回看的第一等公民，但**不接任何执行体**。
//!
//! 三段分工（本模块实现前两段的全部，以及第三段的「只记账不执行」那一半）：
//!
//! ```text
//! 事件源（Source）── 游标读取 ──→ 事件账本（Trigger）── gate（策略判定）──→ 判定留痕（Decision）
//!                                                                   │
//!                                            ┌── 阳性候选 ──→ assess（模型研判）──→ 值得/不值得/拿不准
//!                                            │
//!                                            observe 档：只记「本可接单」→ 界面候选清单
//!                                            auto 档：需要工单层 D-47 与执行体隔离 D-48，本段拒绝
//! ```
//!
//! 两层研判各自有预算与留痕：gate 是确定性的、零成本、每次都判；`assess` 调模型、按
//! `assessMaxPerDay`（缺省 50）与现有配额闸门限量，一条候选只判一次。两层都不执行任何动作。
//! 判定行上有两个版本键，分工不同：`policyVersion` 是「依据哪一版策略判出来的」，永远不改写；
//! `assessQueueVersion` 是「现在排在哪个版本的研判队列里」，策略升版时由 `save_policy` 重排。
//! 于是「改了策略」有明确后果：还没研判的候选按新版重排，在跑的那一次结论作废（钱已花，结论不用）。
//!
//! 落点在桌面 Rust 主进程（不是引擎侧 Python），三条理由：
//! 1. **存储归属**：事件账本必须与判定留痕同库、同事务，而桌面库 `tongtian.db` 只由 Rust
//!    主进程打开（`tests/unit/storage/ownership-contract.test.ts` 拦截跨库直连）。引擎另建一份
//!    事件库就是 D-43 已经记过一次的「两处口径不一」。
//! 2. **凭据边界**：第一个事件源是账号服务项目事件，access token 只在 Rust 侧内存里进
//!    `Authorization` 头（沿用 D-06 的 `collab/watch.rs`）。搬到引擎侧就要把令牌下发给
//!    Python 进程，直接违反「不落日志、不进错误文案、只在内存与钥匙串」这条边界。
//! 3. **默认关**：这一层的默认档位是 `observe`，缺省策略里没有任何入口授权。引擎不启动、
//!    没配模型时，账本与 gate 仍然可测可用；反过来把授权层放进引擎，桌面端不跑引擎就没有策略。
//!
//! 四类事件源里，`im_inbound` 仍在引擎侧（六条通道的真实企业凭据未验收），本段只登记不读取，
//! 巡检时如实报 `SUPERAGENT_SOURCE_NOT_WIRED`；引擎侧那两条链路（`engine/im_channels.py`、
//! `engine/subagent_runtime.py`）后续把事件**交给这里的账本**，而不是自建第二套判定。
//!
//! 口径与证据：`docs/Super-agent-对标DSH.md`（对标结论与不做的事）、
//! `docs/缺陷与实现计划-任我行.md` D-46（缺陷、方案与验收）。

pub mod assess;
pub mod gate;
pub mod ledger;
pub mod policy;
pub mod sources;

use serde_json::{json, Value};
use std::path::Path;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, State};

use crate::store::Store;
use crate::P0Services;

/// 一轮巡检完成后的事件名（界面据此刷新候选清单；只在「有事发生」时投）
pub const EVENT_SUPERAGENT: &str = "superagent-event";

/// 账号服务项目事件（D-06 已打通的那条流，本段的首验事件源）
pub const SOURCE_KIND_PROJECT_EVENT: &str = "project_event";
/// 定时到点（复用桌面 `scheduled_tasks` 的扫描语义，到点即一条事件）
pub const SOURCE_KIND_SCHEDULE: &str = "schedule";
/// 条件满足（「等 N 小时」「额度将用尽」这类等待态是一种事件源，不给研判加等待状态）
pub const SOURCE_KIND_CONDITION: &str = "condition";
/// 本机文件与工作区变化
pub const SOURCE_KIND_WORKSPACE_FILE: &str = "workspace_file";
/// IM 通道入站消息（六条通道都在引擎侧，真实企业凭据未验收，界面必须如实标注）
pub const SOURCE_KIND_IM_INBOUND: &str = "im_inbound";

pub const SOURCE_KINDS: &[&str] = &[
    SOURCE_KIND_PROJECT_EVENT,
    SOURCE_KIND_SCHEDULE,
    SOURCE_KIND_CONDITION,
    SOURCE_KIND_WORKSPACE_FILE,
    SOURCE_KIND_IM_INBOUND,
];

/// 事件源显示名（界面与判定理由共用一份，避免各处自己翻译）
pub fn source_kind_label(kind: &str) -> &'static str {
    match kind {
        SOURCE_KIND_PROJECT_EVENT => "项目事件",
        SOURCE_KIND_SCHEDULE => "定时到点",
        SOURCE_KIND_CONDITION => "条件满足",
        SOURCE_KIND_WORKSPACE_FILE => "工作区变化",
        SOURCE_KIND_IM_INBOUND => "IM 入站消息",
        _ => "未知事件源",
    }
}

/// 强度显示名（`explicit` 是唯一可以直接开工的强度）
pub fn strength_label(strength: &str) -> &'static str {
    match strength {
        gate::STRENGTH_EXPLICIT => "直接指派",
        gate::STRENGTH_INFERRED => "主动推断",
        _ => "未获授权",
    }
}

/// 档位显示名（默认 observe；`auto` 在本段被策略层拒绝）
pub fn mode_label(mode: &str) -> &'static str {
    match mode {
        policy::MODE_OFF => "关闭（不研判）",
        policy::MODE_OBSERVE => "影子模式（研判照做，零自动执行）",
        policy::MODE_AUTO => "自动接单（尚未开放）",
        _ => "未知档位",
    }
}

// ── IPC（界面只读这一层；显示名也在这里给齐，避免前端自己翻译出第二套口径）──
//
// 为什么显示名在 Rust 侧拼：`source_kind_label` / `strength_label` / `mode_label` 同时被
// gate 的判定理由用着（`basis`、`note` 里的中文就是它们），前端再翻一遍迟早对不上。
// 注意各张表里 `kind` 的含义不同——入口表的 `kind` 是事件源类型，事件表的 `kind` 是上游
// 事件类型（如 `task.commented`），只有前者能被 `source_kind_label` 翻译。

/// 一轮巡检里单个入口的状态显示名（界面与后台循环共用，别在前端再翻一次）
pub fn pump_status_label(status: &str) -> &'static str {
    match status {
        "ok" => "已读取",
        "waiting" => "未到点",
        "idle_login_required" => "需要登录账号",
        "fetch_failed" => "读取失败",
        "ingest_failed" => "入账失败",
        "capped" => "今日研判已达上限",
        _ => "未知状态",
    }
}

/// 巡检摘要补显示名：入口类型、状态、档位，以及「本轮跳过」的原因。
///
/// 后台循环投的事件与「立即巡检」命令的返回都过这里，界面拿到的两条路径形状完全一致。
pub fn label_pump_summary(summary: &Value) -> Value {
    let mut out = summary.clone();
    if let Some(mode) = summary.get("acceptMode").and_then(|v| v.as_str()).map(String::from) {
        out["modeLabel"] = json!(mode_label(&mode));
    }
    if let Some(skipped) = summary.get("skipped").and_then(|v| v.as_str()).map(String::from) {
        let reason = if skipped == "no_enabled_source" {
            "没有启用的事件入口"
        } else {
            "本轮跳过"
        };
        out["skippedLabel"] = json!(reason);
    }
    if let Some(runs) = out.get_mut("sources").and_then(|v| v.as_array_mut()) {
        for run in runs.iter_mut() {
            if let Some(kind) = run.get("kind").and_then(|v| v.as_str()).map(String::from) {
                run["kindLabel"] = json!(source_kind_label(&kind));
            }
            if let Some(status) = run.get("status").and_then(|v| v.as_str()).map(String::from) {
                run["statusLabel"] = json!(pump_status_label(&status));
            }
        }
    }
    out
}

/// 入口行 → JSON（`SourceRow` 已 derive Serialize，字段名统一 camelCase）
fn source_row_to_value(row: &ledger::SourceRow) -> Value {
    serde_json::to_value(row).unwrap_or(Value::Null)
}

/// 补上入口类型的显示名：入口表读 `kind`，候选清单读 `sourceKind`
fn with_source_kind_label(mut row: Value) -> Value {
    let kind = row
        .get("kind")
        .or_else(|| row.get("sourceKind"))
        .and_then(|v| v.as_str())
        .map(String::from);
    let Some(kind) = kind else {
        return row;
    };
    let label = json!(source_kind_label(&kind));
    if row.get("sourceKind").is_some() {
        row["sourceKindLabel"] = label;
    } else {
        row["kindLabel"] = label;
    }
    row
}

/// 补上强度显示名（判定留痕与候选清单都有）
fn with_strength_label(mut row: Value) -> Value {
    if let Some(strength) = row.get("strength").and_then(|v| v.as_str()).map(String::from) {
        row["strengthLabel"] = json!(strength_label(&strength));
    }
    row
}

/// 补上第二层（模型研判）的显示名：结论与状态各一枚，界面与导出都不自己翻译。
///
/// `assessStatus` 为空串时显示「未研判」——这一行的含义是「第二层还没花钱判它」，
/// 不是「判过了但没结论」，两者在清单里必须一眼分得开。
fn with_assess_label(mut row: Value) -> Value {
    if let Some(verdict) = row
        .get("assessVerdict")
        .and_then(|v| v.as_str())
        .map(String::from)
    {
        row["assessVerdictLabel"] = json!(assess::verdict_label(&verdict));
    }
    if let Some(status) = row
        .get("assessStatus")
        .and_then(|v| v.as_str())
        .map(String::from)
    {
        row["assessStatusLabel"] = json!(assess::status_label(&status));
    }
    // 重排不是错误也不是结论，是这一行的待办换了版本：给一句中文，界面与导出都不自己推断
    let requeued_at = row.get("requeuedAtMs").and_then(Value::as_i64).unwrap_or(0);
    let gate_version = row.get("policyVersion").and_then(Value::as_i64).unwrap_or(0);
    let queue_version = row
        .get("assessQueueVersion")
        .and_then(Value::as_i64)
        .unwrap_or(gate_version);
    if requeued_at > 0 {
        row["requeueLabel"] = json!(format!(
            "判定依据 v{gate_version}，因策略升版改排在 v{queue_version} 的研判队列"
        ));
    }
    row
}

fn value_rows(rows: Vec<Value>) -> Value {
    Value::Array(rows)
}

/// 台账概览：档位、策略版本、今日研判数、入口数、事件数、候选数
#[tauri::command]
pub async fn superagent_stats(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let mut summary = ledger::stats(&p0.store)?;
    if let Some(mode) = summary.get("acceptMode").and_then(|v| v.as_str()).map(String::from) {
        // 与 get_policy / save_policy 同名（modeLabel），界面只认一个字段
        summary["modeLabel"] = json!(mode_label(&mode));
        // 「是不是缺省档位」由策略层回答，界面不自己比较字符串：DEFAULT_MODE 改了这里就跟着改
        summary["modeIsDefault"] = json!(policy::is_default_mode(&mode));
    }
    Ok(summary)
}

/// 事件入口列表（含游标、是否在读、最近一次失败原因与显示名）
#[tauri::command]
pub async fn superagent_list_sources(
    p0: State<'_, Arc<P0Services>>,
) -> Result<Value, String> {
    let rows = ledger::list_sources(&p0.store)?;
    Ok(value_rows(
        rows.iter()
            .map(source_row_to_value)
            .map(with_source_kind_label)
            .collect(),
    ))
}

/// 登记一个事件入口实例。登记不等于开跑：新入口一律 `enabled = false`，
/// 必须再显式调 `superagent_set_source_enabled`，且策略里还得有这一入口的授权。
#[tauri::command]
pub async fn superagent_register_source(
    p0: State<'_, Arc<P0Services>>,
    kind: String,
    instance: String,
    label: String,
    config: Option<Value>,
) -> Result<Value, String> {
    let row = ledger::register_source(
        &p0.store,
        &kind,
        &instance,
        &label,
        config.unwrap_or_else(|| json!({})),
    )?;
    Ok(with_source_kind_label(source_row_to_value(&row)))
}

/// 开/关一个入口的**读取**（运维动作）。取消授权要改策略，两者是两回事。
#[tauri::command]
pub async fn superagent_set_source_enabled(
    p0: State<'_, Arc<P0Services>>,
    source_id: String,
    enabled: bool,
) -> Result<Value, String> {
    let row = ledger::set_source_enabled(&p0.store, &source_id, enabled)?;
    Ok(with_source_kind_label(source_row_to_value(&row)))
}

/// 主动候选清单（影子模式的唯一产出：这些事件「本可接单」，但一条都没有被执行）
#[tauri::command]
pub async fn superagent_list_candidates(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = ledger::list_candidates(&p0.store, limit)?;
    Ok(value_rows(
        rows.into_iter()
            .map(with_source_kind_label)
            .map(with_strength_label)
            .map(with_assess_label)
            .collect(),
    ))
}

// ── 候选清单导出（验收项③：`observe` 下要能带走「本可接单」的清单与理由） ──

/// 导出文件里必须自带这句话：它是**研判记录**，不是执行日志。
/// 拿走一份 CSV 的人（或三个月后的自己）不该把它误读成「这些活 Agent 已经做了」。
pub const EXPORT_NOTE: &str =
    "影子模式（observe）研判记录：下列事件按已授权的启用规则判为「本可接单」，但没有一条被执行；模型研判那一列给出的「值不值得开工」同样只是记录；要开工请在会话里正常发起。";

/// CSV 列（顺序即导出顺序；机器可读的键名保持英文，中文只出现在 `*Label` 值里）
const EXPORT_COLUMNS: &[&str] = &[
    "decisionId",
    "policyVersion",
    "strength",
    "strengthLabel",
    "outcome",
    "wouldAccept",
    "sourceKind",
    "sourceKindLabel",
    "sourceInstance",
    "externalId",
    "eventKind",
    "actor",
    "threadKey",
    "addressedAgent",
    "arrivedAt",
    "decidedAt",
    "reason",
    "basis",
    "matchedRules",
    "text",
    "assessStatus",
    "assessStatusLabel",
    "assessVerdict",
    "assessVerdictLabel",
    "assessReason",
    "assessModel",
    "assessTokens",
    "assessedAt",
    "assessQueueVersion",
    "requeueLabel",
];

/// 单元格取值：账本里的字段类型不一（布尔、毫秒、命中规则数组），统一成一段可贴进表格的文本
fn cell_text(row: &Value, key: &str) -> String {
    match row.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(flag)) => flag.to_string(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            })
            .collect::<Vec<_>>()
            .join("; "),
        // 载荷这类嵌套结构不进 CSV（列宽不可控），JSON 导出里原样保留
        Some(Value::Object(_)) => String::new(),
    }
}

/// RFC 4180：事件正文可能含逗号、引号与换行，一律转义，不让一份清单把自己撑成两行
fn csv_cell(raw: &str) -> String {
    let needs_quote =
        raw.contains(',') || raw.contains('"') || raw.contains('\n') || raw.contains('\r');
    let escaped = raw.replace('"', "\"\"");
    if needs_quote {
        format!("\"{escaped}\"")
    } else {
        escaped
    }
}

/// 时间列写成 UTC ISO，比裸毫秒好核对（与审计导出同一格式化函数，避免出现两种时间口径）
fn iso_ms(ms: i64) -> String {
    crate::security::audit::iso8601_utc(ms)
}

fn candidates_csv(rows: &[Value]) -> String {
    let mut out = EXPORT_COLUMNS.join(",");
    out.push_str("\r\n");
    for row in rows {
        let line = EXPORT_COLUMNS
            .iter()
            .map(|key| {
                let text = match *key {
                    // 账本里存的是毫秒，导出时补成可读时间
                    "arrivedAt" => iso_ms(row.get("arrivedAtMs").and_then(Value::as_i64).unwrap_or(0)),
                    "decidedAt" => iso_ms(row.get("decidedAtMs").and_then(Value::as_i64).unwrap_or(0)),
                    // 第二层还没判过时留空，不写 1970 这种看起来像时间的数字
                    "assessedAt" => match row.get("assessedAtMs").and_then(Value::as_i64) {
                        Some(ms) if ms > 0 => iso_ms(ms),
                        _ => String::new(),
                    },
                    other => cell_text(row, other),
                };
                csv_cell(&text)
            })
            .collect::<Vec<_>>()
            .join(",");
        out.push_str(&line);
        out.push_str("\r\n");
    }
    out
}

/// 只新建文件、不覆盖存量：文件名带 UTC 时间戳（与审计/记忆导出同口径）
fn export_file_name(format: &str, ms: i64) -> String {
    format!(
        "superagent-candidates-{}.{}",
        iso_ms(ms).replace(':', "-").replace('.', "-"),
        format
    )
}

/// 导出到指定目录（测试注入临时目录，不去碰真实运行目录）
pub fn export_candidates_to(
    store: &Store,
    dir: &Path,
    format: &str,
) -> Result<(String, usize), String> {
    let format = match format {
        "csv" => "csv",
        "json" => "json",
        other => {
            return Err(format!(
                "SUPERAGENT_EXPORT_FORMAT_UNSUPPORTED: {other}（只支持 json 与 csv）"
            ))
        }
    };
    let rows: Vec<Value> = ledger::list_candidates(store, None)?
        .into_iter()
        .map(with_source_kind_label)
        .map(with_strength_label)
        .map(with_assess_label)
        .collect();
    let count = rows.len();
    // 头部写档位与策略版本：一份不知道自己是哪个策略版本下研判出来的清单没法复核
    let (version, doc) = ledger::effective_policy(store);
    let now = ledger::now_ms();
    let body = if format == "json" {
        let wrapped = json!({
            "generatedAt": iso_ms(now),
            "generatedAtMs": now,
            "acceptMode": doc.accept_mode,
            "modeLabel": mode_label(&doc.accept_mode),
            "policyVersion": version,
            "dailyEvalCap": doc.daily_eval_cap,
            "assessCap": doc.assess_cap(),
            "count": count,
            "note": EXPORT_NOTE,
            "columns": EXPORT_COLUMNS,
            "candidates": rows,
        });
        serde_json::to_string_pretty(&wrapped)
            .map_err(|e| format!("SUPERAGENT_EXPORT_FAILED: 清单序列化失败 {e}"))?
    } else {
        let header = format!(
            "# acceptMode={} policyVersion={} assessCap={} count={} {}\r\n",
            doc.accept_mode,
            version,
            doc.assess_cap(),
            count,
            EXPORT_NOTE.replace('\n', " ")
        );
        // 注释行只给人在表格里看一眼的上下文；机器读的还是下面的正式表头
        format!("{}{}", header, candidates_csv(&rows))
    };
    std::fs::create_dir_all(dir)
        .map_err(|e| format!("SUPERAGENT_EXPORT_FAILED: 导出目录创建失败 {e}"))?;
    let path = dir.join(export_file_name(format, now));
    std::fs::write(&path, body)
        .map_err(|e| format!("SUPERAGENT_EXPORT_FAILED: 清单写入失败 {e}"))?;
    Ok((path.to_string_lossy().to_string(), count))
}

/// 导出主动候选清单（用户显式点「导出清单」才落盘；巡检循环本身不写任何文件）
#[tauri::command]
pub async fn superagent_export_candidates(
    p0: State<'_, Arc<P0Services>>,
    format: Option<String>,
) -> Result<Value, String> {
    let fmt = match format.as_deref() {
        None | Some("") => "json",
        Some(other) => other,
    };
    let dir = crate::store::db::runtime_home().join("exports");
    let (path, count) = export_candidates_to(&p0.store, &dir, fmt)?;
    Ok(json!({
        "path": path,
        "count": count,
        "format": if fmt == "csv" { "csv" } else { "json" },
        "note": EXPORT_NOTE,
    }))
}

/// 判定留痕（含阴性：为什么没做也要看得见）
#[tauri::command]
pub async fn superagent_list_decisions(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = ledger::list_decisions(&p0.store, limit)?;
    Ok(value_rows(
        rows.into_iter()
            .map(|row| serde_json::to_value(row).unwrap_or(Value::Null))
            .map(with_strength_label)
            .collect(),
    ))
}

/// 事件账本（最近若干条）。入口类型不在事件行里，界面用 `superagent_list_sources`
/// 的 sourceId → kindLabel 对照即可，不在这里重复一份翻译。
#[tauri::command]
pub async fn superagent_list_triggers(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = ledger::list_triggers(&p0.store, limit)?;
    Ok(value_rows(
        rows.into_iter()
            .map(|row| serde_json::to_value(row).unwrap_or(Value::Null))
            .collect(),
    ))
}

/// 单条事件（候选清单点进去看的那一行）。
/// 注意事件行的 `kind` 是上游事件类型（如 `task.commented`），不是事件源类型。
#[tauri::command]
pub async fn superagent_get_trigger(
    p0: State<'_, Arc<P0Services>>,
    trigger_id: String,
) -> Result<Value, String> {
    let row = ledger::get_trigger(&p0.store, &trigger_id)?;
    row.map(|row| serde_json::to_value(row).unwrap_or(Value::Null))
        .ok_or_else(|| "SUPERAGENT_TRIGGER_NOT_FOUND: 这条事件不在账本里（可能换了本机库或已被清理）".to_string())
}

/// 当前生效策略 + 可选项。`auto` 如实标为「尚未开放」，界面上就不该出现能选它的开关。
#[tauri::command]
pub async fn superagent_get_policy(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let (version, doc) = ledger::effective_policy(&p0.store);
    Ok(json!({
        "version": version,
        "doc": doc,
        "modeLabel": mode_label(&doc.accept_mode),
        "allowedModes": [policy::MODE_OFF, policy::MODE_OBSERVE],
        "autoAvailable": false,
        "autoBlockedBy": "工单层（D-47）与执行体隔离（D-48）尚未落地",
        "sourceKinds": SOURCE_KINDS.iter().map(|k| json!({
            "kind": k, "label": source_kind_label(k),
            // IM 入站在本段只登记不读取，界面必须能标出来
            "readable": k != &SOURCE_KIND_IM_INBOUND,
        })).collect::<Vec<_>>(),
    }))
}

/// 保存策略：内容与当前版本一致不新增版本；`auto` 被挡住并如实报错。
///
/// 真的升了版，就把「还没研判」的阳性候选重排到新版本的研判队列，并把条数如实带回——
/// 用户改完策略必须能看见「这一改让哪几件事重新排过队」，否则策略层就是个没有后果的表单。
#[tauri::command]
pub async fn superagent_save_policy(
    p0: State<'_, Arc<P0Services>>,
    doc: Value,
) -> Result<Value, String> {
    let outcome = ledger::save_policy(&p0.store, &doc, ledger::POLICY_SOURCE_USER)?;
    Ok(json!({
        "version": outcome.version,
        "changed": outcome.changed,
        "doc": outcome.doc,
        "modeLabel": mode_label(&outcome.doc.accept_mode),
        "requeued": outcome.requeued,
        "requeueNote": if outcome.requeued > 0 {
            json!(format!(
                "{} 条还没研判的候选已排进 v{} 的研判队列，下一轮会按当前策略再问一次模型",
                outcome.requeued, outcome.version
            ))
        } else {
            Value::Null
        },
    }))
}

/// 策略版本历史（判定留痕按版本可追溯，界面要能回答「当时依据哪一版」）
#[tauri::command]
pub async fn superagent_policy_versions(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = ledger::list_policy_versions(&p0.store, limit)?;
    Ok(value_rows(
        rows.into_iter()
            .map(|mut row| {
                if let Some(mode) = row
                    .get("doc")
                    .and_then(|d| d.get("acceptMode"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
                {
                    row["modeLabel"] = json!(mode_label(&mode));
                }
                row
            })
            .collect(),
    ))
}

/// 立即巡检一轮（页面的「刷新」与真机验收共用同一条代码路径）。
/// 结果同样投一次事件，页面与后台循环看到的口径完全一致。
#[tauri::command]
pub async fn superagent_pump_now(
    p0: State<'_, Arc<P0Services>>,
    app: AppHandle,
) -> Result<Value, String> {
    let store = Arc::clone(&p0.store);
    let summary = sources::pump_once(&store).await;
    let summary = label_pump_summary(&summary);
    let _ = app.emit(EVENT_SUPERAGENT, &summary);
    Ok(summary)
}

/// 主动跑一轮第二层研判（模型研判）。
///
/// 界面上这是一个**显式动作**：点一次才会花模型钱，且受策略里的 `assessMaxPerDay`
/// 与现有配额闸门双重限量；后台巡检循环按同样的口径自己跑。返回的是摘要而不是错误——
/// 跑不起来（没配模型、配额到档）只是「今天没判」，界面要显示原因而不是报错红一条。
#[tauri::command]
pub async fn superagent_assess_now(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let store = Arc::clone(&p0.store);
    Ok(assess::run(&store, &p0.quota, limit).await)
}

/// 启动时确保第 1 版缺省策略在库（observe + 零授权）；已有版本原样返回。
/// 这一步必须在巡检循环之前跑完，否则第一轮读不到策略会按兜底走，界面看不到版本号。
pub fn ensure_startup_policy(store: &Store) -> Result<i64, String> {
    ledger::ensure_default_policy(store)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn display_names_are_the_only_copy_the_ui_gets() {
        assert_eq!(
            source_kind_label(SOURCE_KIND_PROJECT_EVENT),
            "项目事件",
            "入口显示名与 gate 判定理由同源，改一处就全都改"
        );
        assert_eq!(strength_label(gate::STRENGTH_EXPLICIT), "直接指派");
        assert_eq!(strength_label(gate::STRENGTH_NOT_ADMITTED), "未获授权");
        assert!(mode_label(policy::MODE_OBSERVE).contains("零自动执行"));
        // 未知值也要有名字，界面不该出现空白或英文原词
        assert_eq!(source_kind_label("email"), "未知事件源");
        assert_eq!(mode_label("auto"), "自动接单（尚未开放）");
    }

    #[test]
    fn pump_summary_labels_runs_and_mode_without_touching_numbers() {
        let summary = json!({
            "ok": true,
            "acceptMode": "observe",
            "inserted": 3,
            "sources": [{
                "sourceId": "src-1", "kind": "workspace_file", "instance": "",
                "fetched": 3, "inserted": 3, "decided": 2, "candidates": 1,
                "status": "capped", "error": "今日研判已达上限 200 条",
            }],
        });
        let labeled = label_pump_summary(&summary);
        assert_eq!(labeled["modeLabel"], "影子模式（研判照做，零自动执行）");
        let run = &labeled["sources"][0];
        assert_eq!(run["kindLabel"], "工作区变化");
        assert_eq!(run["statusLabel"], "今日研判已达上限");
        // 只补显示名：原始数字与错误文案一个字都不许动（界面据此判定的口径靠这些字段）
        assert_eq!(labeled["inserted"], 3);
        assert_eq!(run["inserted"], 3);
        assert_eq!(run["error"], "今日研判已达上限 200 条");
        // 原摘要不被就地改写（后台循环还要用它算 busy）
        assert!(summary.get("modeLabel").is_none());
    }

    #[test]
    fn skipped_round_says_why_in_chinese() {
        let labeled = label_pump_summary(&json!({
            "ok": true, "skipped": "no_enabled_source", "sources": []
        }));
        assert_eq!(labeled["skippedLabel"], "没有启用的事件入口");
    }

    #[test]
    fn event_kind_is_never_translated_as_a_source_kind() {
        // 事件行的 `kind` 是上游事件类型（task.commented），把它当入口类型翻就是第二套口径
        let row = with_source_kind_label(json!({ "id": "trg-1", "kind": "task.commented" }));
        assert_eq!(row["kindLabel"], "未知事件源");
        // 候选行的入口类型在 `sourceKind`，显示名跟着落在 `sourceKindLabel`
        let candidate = with_source_kind_label(json!({
            "decisionId": "dec-1", "sourceKind": "im_inbound", "strength": "not_admitted"
        }));
        assert_eq!(candidate["sourceKindLabel"], "IM 入站消息");
        assert!(candidate.get("kindLabel").is_none(), "候选行没有 kind 字段，不该凭空造一个");
        assert_eq!(with_strength_label(candidate)["strengthLabel"], "未获授权");
    }

    // ── 导出（验收项③）────────────────────────────────────────

    fn temp_store(name: &str) -> (Arc<Store>, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-sa-export-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Arc::new(Store::open(dir.join("tongtian.db")).unwrap());
        (store, dir)
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("tt-sa-scratch-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn cleanup(dir: &std::path::Path) {
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 造一条真候选：登记工作区入口 → 开读取 → 给授权 → 巡检一轮。
    /// 全程只走 ledger 与 sources，测试里不写第二套 SQL（写了就是绕过账本口径）。
    async fn one_candidate_store(tag: &str) -> (Arc<Store>, std::path::PathBuf, std::path::PathBuf) {
        let (store, db_dir) = temp_store(tag);
        let watched = scratch(&format!("{tag}-watched"));
        std::fs::create_dir_all(watched.join("docs")).unwrap();
        // 文件名里带逗号：CSV 导出的转义就靠它验证（正文一律不进账本，写什么都不会被搬走）
        std::fs::write(watched.join("docs/计划,表.md"), "正文永不进账本".as_bytes()).unwrap();
        ledger::ensure_default_policy(&store).unwrap();
        ledger::save_policy(
            &store,
            &json!({
                "acceptMode": policy::MODE_OBSERVE,
                "dailyEvalCap": 50,
                "sources": [{
                    "kind": SOURCE_KIND_WORKSPACE_FILE, "instance": "", "enabled": true,
                    "label": "工作区", "allowUsers": [], "bots": [],
                    "rules": [{ "id": "any", "enabled": true, "minChars": 4 }]
                }]
            }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        let source = ledger::register_source(
            &store,
            SOURCE_KIND_WORKSPACE_FILE,
            "",
            "工作区",
            json!({ "dir": watched.to_string_lossy() }),
        )
        .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        let summary = sources::pump_once(&store).await;
        assert_eq!(summary["candidates"].as_i64(), Some(1), "前置数据没造出来");
        (store, db_dir, watched)
    }

    #[tokio::test]
    async fn export_json_carries_the_shadow_note_and_rust_labels() {
        let (store, db_dir, watched) = one_candidate_store("json").await;
        let out = scratch("json-out");
        let (path, count) = export_candidates_to(&store, &out, "json").unwrap();
        assert_eq!(count, 1);
        assert!(path.ends_with(".json"));
        let text = std::fs::read_to_string(&path).unwrap();
        let doc: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(doc["count"].as_i64(), Some(1));
        assert_eq!(doc["acceptMode"].as_str(), Some(policy::MODE_OBSERVE));
        assert_eq!(doc["modeLabel"].as_str(), Some(mode_label(policy::MODE_OBSERVE)));
        // 清单要写清是哪个策略版本研判的（版本号由账本给，测试不猜它是第几版）
        assert_eq!(
            doc["policyVersion"].as_i64(),
            Some(ledger::effective_policy(&store).0)
        );
        // 拿走文件的人第一眼就得知道这不是执行日志
        assert!(doc["note"].as_str().unwrap().contains("没有一条被执行"));
        let row = &doc["candidates"][0];
        assert_eq!(row["sourceKindLabel"].as_str(), Some("工作区变化"));
        assert_eq!(row["strengthLabel"].as_str(), Some("主动推断"));
        assert!(!row["reason"].as_str().unwrap().is_empty(), "理由必须跟着清单走");
        assert_eq!(row["eventKind"].as_str(), Some("file.changed"));
        assert!(
            !text.contains("正文永不进账本"),
            "导出只带账本里存过的东西，不顺手去读文件正文"
        );
        cleanup(&out);
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn export_csv_escapes_commas_and_keeps_context_on_one_line() {
        let (store, db_dir, watched) = one_candidate_store("csv").await;
        let out = scratch("csv-out");
        let (path, count) = export_candidates_to(&store, &out, "csv").unwrap();
        assert!(path.ends_with(".csv"));
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = text.split("\r\n").collect();
        // 第 1 行是上下文注释，第 2 行才是表头：注释里不许有换行把它撑成两行
        assert!(
            lines[0].starts_with("# acceptMode=observe policyVersion="),
            "首行应是上下文注释，实际是 {:?}",
            lines[0]
        );
        assert!(lines[0].contains("count=1 "));
        assert!(lines[0].contains("没有一条被执行"));
        assert_eq!(lines[1], EXPORT_COLUMNS.join(","));
        // 一条候选就该占一行（引号成对，正文里的逗号不会把行撑断、也不会把换行带进来）
        assert_eq!(lines.len(), 4, "注释 + 表头 + 一行数据 + 结尾空串");
        assert_eq!(lines[2].matches('"').count() % 2, 0, "引号必须成对");
        assert_eq!(lines[2].matches("\n").count(), 0);
        assert!(text.contains("\"docs/计划,表.md\""), "带逗号的文件名要整体成一格");
        assert_eq!(&text[text.len() - 2..], "\r\n");
        assert_eq!(count, 1);
        cleanup(&out);
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn export_refuses_an_unknown_format_without_creating_the_directory() {
        let (store, db_dir, watched) = one_candidate_store("bad").await;
        let out = std::env::temp_dir().join(format!("tt-sa-bad-{}", ledger::now_ms()));
        let err = export_candidates_to(&store, &out, "xlsx").unwrap_err();
        assert!(err.starts_with("SUPERAGENT_EXPORT_FORMAT_UNSUPPORTED: xlsx"), "{err}");
        assert!(err.contains("只支持 json 与 csv"), "拒绝要带中文说明");
        assert!(!out.exists(), "不该先建目录再报错");
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn export_an_empty_ledger_says_zero_instead_of_failing() {
        // 零候选是常态（缺省策略零授权）：导出要给出「0 条 + 同一句影子说明」，不能报错
        let (store, db_dir) = temp_store("empty");
        ledger::ensure_default_policy(&store).unwrap();
        let out = scratch("empty-out");
        let (_path, count) = export_candidates_to(&store, &out, "json").unwrap();
        assert_eq!(count, 0);
        let files: Vec<_> = std::fs::read_dir(&out)
            .unwrap()
            .filter_map(|e| e.ok())
            .collect();
        assert_eq!(files.len(), 1);
        let text = std::fs::read_to_string(files[0].path()).unwrap();
        let doc: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(doc["candidates"].as_array().unwrap().len(), 0);
        assert!(doc["note"].as_str().unwrap().contains("没有一条被执行"));
        cleanup(&out);
        cleanup(&db_dir);
    }
}
