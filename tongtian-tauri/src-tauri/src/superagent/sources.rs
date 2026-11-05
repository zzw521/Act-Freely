//! 首批事件源（D-46 第三段）：把环境里的变化读成账本里的事件
//!
//! 这一层只做三件事：**读取 → 记账 → 交给 gate**。它不判断「该不该干活」（那是
//! `gate` + 策略），也不干活（本段没有任何执行路径）。四类源各自的诚实边界：
//!
//! | 事件源 | 本段的读取方式 | 明确没做 |
//! | --- | --- | --- |
//! | `project_event` | 增量补拉 `GET /projects/{id}/events?since=游标`（D-06 已实测的同一接口），令牌只进 `Authorization` 头 | 不用 SSE：实时性交给页面订阅，账本要的是不重不漏而不是毫秒级 |
//! | `schedule` | 到点即一条事件：`cursor` 复用为「上一次已消费的刻度」，`config.everyMs` / `config.atMs` 声明节奏 | 不与 `scheduled_tasks` 合并：那是「跑一个任务」，这里是「发一条事件」 |
//! | `condition` | 与定时同一套到点数学（「等 N 小时后再看」） | 额度、外部状态这类**探针条件**未实现：配置里没有时间声明就会被如实记成入口错误，而不是静默不触发 |
//! | `workspace_file` | 扫目录 mtime，只记「相对路径 + 修改时间」，**不读文件内容** | 没有系统级 watcher：靠巡检时间窗，跨窗重复由 `(source, external_id)` 幂等吸收；游标是 mtime 水位，所以**拷进来的旧时间戳文件不会被当成变化**（宁漏不误报），深度与条目数都封顶 |
//! | `im_inbound` | 本段不读取（六条通道都在引擎侧，真实企业凭据未验收） | 引擎侧链路把事件交进本账本的入口尚未接线 |
//!
//! 三条贯穿本文件的纪律：
//! 1. **游标只前进**（`ledger::ingest` 内 `WHERE cursor < ?1`），因此重启、重复投递、
//!    扫描窗口重叠都只会多读而不会重放；
//! 2. **正文与载荷都不原样入库**：`ledger` 侧统一截断，超预算载荷只留截断标记；
//! 3. **判定留痕受日上限约束**：超限后仍继续记账（事件不能丢），但停止新增判定，
//!    并在巡检摘要里如实给出 `skippedByCap`。

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::Emitter;

use crate::auth::service as auth_service;
use crate::collab::{client_for_base, network_reason, normalize_base};
use crate::store::Store;
use crate::superagent::gate::{self, GateInput};
use crate::superagent::ledger::{self, NewTrigger, SourceRow};
use crate::superagent::policy::PolicyDoc;
use crate::superagent::{
    SOURCE_KIND_CONDITION, SOURCE_KIND_IM_INBOUND, SOURCE_KIND_PROJECT_EVENT,
    SOURCE_KIND_SCHEDULE, SOURCE_KIND_WORKSPACE_FILE,
};

/// 一轮巡检拉多少页项目事件（服务端单页上限 200）
const EVENT_PAGE_LIMIT: i64 = 200;
const MAX_EVENT_PAGES: usize = 5;
/// 定时/条件的最小间隔：一分钟。低于它就是把「主动接活」变成轮询刷候选
pub const MIN_INTERVAL_MS: i64 = 60_000;
/// 单次巡检最多扫多少个文件条目（只读目录项，不读内容）
pub const MAX_FILE_ENTRIES: usize = 2000;
/// 目录递归深度上限（含根目录自身为第 1 层）
pub const MAX_DIR_DEPTH: usize = 3;
/// 后台循环的巡检间隔。桌面端内常驻，不宣称 7×24
pub const SCAN_INTERVAL: Duration = Duration::from_secs(5 * 60);
/// 没有启用的入口时睡多久（别让用户登记了一个入口还要等五分钟）
pub const IDLE_SCAN_INTERVAL: Duration = Duration::from_secs(30);
/// 这些目录名一律跳过：版本库、依赖与缓存不是「工作区变化」
pub const IGNORED_DIR_NAMES: &[&str] = &[
    ".git",
    ".svn",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
];

/// 项目事件 → 待入库事件（纯函数：不查库、不看时间，可单测重放）
///
/// `addressed_agent` 恒为 `false`：项目事件里没有「这条事件指向本应用」的结构化字段，
/// 正文里的 `@任我行` 只是文本。gate 的不变量是**正文不能改变强度**，所以这里宁可不给
/// 直接指派强度，让规则去决定是否推断。真要支持点名，得先在服务端把提及做成字段。
pub fn project_event_to_trigger(source: &SourceRow, event: &Value) -> Option<NewTrigger> {
    let seq = event.get("seq").and_then(|v| v.as_i64())?;
    let kind = event
        .get("kind")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if kind.is_empty() {
        return None;
    }
    let actor = event
        .get("actorUid")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let payload = event.get("payload").cloned().unwrap_or_else(|| json!({}));
    let (text, thread_key) = describe_project_event(&kind, &payload);
    let created = event.get("createdAtMs").and_then(|v| v.as_i64());
    Some(NewTrigger {
        source_id: source.id.clone(),
        // seq 全库自增，跨项目不连续；入口已经按项目实例分开，所以项目内 seq 就是幂等键
        external_id: seq.to_string(),
        kind: kind.clone(),
        text,
        actor: actor.clone(),
        thread_key,
        addressed_agent: false,
        source_seq: seq,
        // 带上游那条事件的原始结构，体积由账本封顶（超限只留截断标记），这里不复制正文
        payload: json!({ "seq": seq, "kind": &kind, "actorUid": &actor, "upstream": &payload }),
        arrived_at_ms: created,
    })
}

/// 把项目事件的人可读正文与会话键取出来（只认识服务端已知的几种 payload）
fn describe_project_event(kind: &str, payload: &Value) -> (String, String) {
    let comment = payload.get("comment");
    let task = payload.get("task");
    let text = match kind {
        "task.commented" => comment
            .and_then(|c| c.get("body"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "task.created" | "task.updated" => task
            .and_then(|t| t.get("title"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "task.status_changed" => format!(
            "任务状态变更为 {}",
            task.and_then(|t| t.get("status"))
                .and_then(|v| v.as_str())
                .unwrap_or("未知")
        ),
        other => other.to_string(),
    };
    let thread_key = comment
        .and_then(|c| c.get("taskId"))
        .and_then(|v| v.as_str())
        .or_else(|| task.and_then(|t| t.get("id")).and_then(|v| v.as_str()))
        .unwrap_or("")
        .trim()
        .to_string();
    (text, thread_key)
}

/// 定时/条件源是否到点。返回 `(应推进到的游标, 事件 id, 正文)`；未到点返回 `None`。
///
/// `cursor` 在这里的含义是「最后一个已消费的毫秒刻度」，0 表示从未触发过。
/// `everyMs` 与 `atMs` 二选一：前者是周期，后者是一次性到点。
pub fn schedule_due(
    config: &Value,
    cursor: i64,
    now: i64,
) -> Result<Option<(i64, String, String)>, String> {
    if let Some(at) = config.get("atMs").and_then(|v| v.as_i64()) {
        if at <= 0 {
            return Err("SUPERAGENT_SCHEDULE_INVALID: 到点时间必须晚于 0".to_string());
        }
        if now < at || cursor >= at {
            return Ok(None);
        }
        let note = note_text(config);
        return Ok(Some((at, format!("at-{at}"), note)));
    }
    let raw = config
        .get("everyMs")
        .and_then(|v| v.as_i64())
        .unwrap_or_default();
    if raw <= 0 {
        return Err(
            "SUPERAGENT_SCHEDULE_INVALID: 定时/条件源需要 everyMs（≥ 60000 毫秒）或 atMs".to_string(),
        );
    }
    let every = raw.max(MIN_INTERVAL_MS);
    // 首次登记（游标 0）不立刻触发：否则「打开入口」这件事本身就变成一条候选
    let due = if cursor <= 0 {
        return Ok(Some((0, String::new(), String::new())));
    } else {
        cursor + every
    };
    if now < due {
        return Ok(None);
    }
    let note = note_text(config);
    Ok(Some((due, format!("tick-{due}"), note)))
}

fn note_text(config: &Value) -> String {
    let note = config
        .get("note")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if note.is_empty() {
        "定时到点：请按策略判断要不要接一件事".to_string()
    } else {
        note
    }
}

/// 一个文件的变更记录（不含任何文件内容）
#[derive(Debug, Clone, PartialEq)]
pub struct FileChange {
    pub path: String,
    pub modified_ms: i64,
}

/// 扫描目录，返回 `since_ms` 之后被修改过的文件（相对路径 + mtime）。
///
/// 只读目录项：不打开文件、不读内容，因此这条链路不会把正文或凭据带进账本。
/// 条目数与深度都封顶，避免误把整个磁盘挂成事件源。
pub fn workspace_file_changes(
    root: &Path,
    since_ms: i64,
    limit: usize,
) -> Result<Vec<FileChange>, String> {
    let mut out: Vec<FileChange> = Vec::new();
    let mut stack: Vec<(PathBuf, usize)> = vec![(root.to_path_buf(), 1)];
    while let Some((dir, depth)) = stack.pop() {
        if out.len() >= limit {
            break;
        }
        let entries = std::fs::read_dir(&dir).map_err(|e| {
            format!("SUPERAGENT_WORKSPACE_SCAN_FAILED: 目录读不出来（{e}）")
        })?;
        for entry in entries.flatten() {
            if out.len() >= limit {
                break;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with('.') || IGNORED_DIR_NAMES.iter().any(|skip| *skip == name.as_str())
            {
                continue;
            }
            let path = entry.path();
            let meta = match entry.metadata() {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if meta.is_dir() {
                if depth < MAX_DIR_DEPTH {
                    stack.push((path, depth + 1));
                }
                continue;
            }
            if !meta.is_file() {
                continue;
            }
            let modified_ms = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);
            if modified_ms <= since_ms {
                continue;
            }
            out.push(FileChange {
                path: relative(root, &path),
                modified_ms,
            });
        }
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

fn relative(root: &Path, path: &Path) -> String {
    let stripped = path.strip_prefix(root).unwrap_or(path);
    stripped.to_string_lossy().replace('\\', "/")
}

/// 文件变化 → 待入库事件（幂等键含 mtime，同一文件的两次修改是两条事件）
pub fn file_change_to_trigger(source: &SourceRow, change: &FileChange) -> NewTrigger {
    NewTrigger {
        source_id: source.id.clone(),
        external_id: format!("{}:{}", change.path, change.modified_ms),
        kind: "file.changed".to_string(),
        text: change.path.clone(),
        // 本机文件没有发起人；空 actor 意味着「不受名单约束」，见 gate 的 allowUsers 口径
        actor: String::new(),
        thread_key: change.path.clone(),
        addressed_agent: false,
        source_seq: change.modified_ms,
        payload: json!({ "path": &change.path, "modifiedMs": change.modified_ms }),
        arrived_at_ms: Some(change.modified_ms),
    }
}

// ── 巡检 ─────────────────────────────────────────────────────

/// 一条入口的处理结果（进巡检摘要，界面与日志都只读这一份）
#[derive(Debug, Clone)]
struct SourceRun {
    source_id: String,
    kind: String,
    instance: String,
    fetched: usize,
    inserted: usize,
    decided: usize,
    candidates: usize,
    status: &'static str,
    error: Option<String>,
}

impl SourceRun {
    fn to_value(&self) -> Value {
        json!({
            "sourceId": self.source_id,
            "kind": self.kind,
            "instance": self.instance,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "decided": self.decided,
            "candidates": self.candidates,
            "status": self.status,
            "error": self.error,
        })
    }
}

/// 一轮完整巡检：读取所有已启用的入口 → 记账 → gate 判定 → 留痕。
///
/// 返回摘要而不是错误：单个入口读不到（未登录、目录被移走）不能拖垮整轮，
/// 也不能悄悄跳过——错误写在 `sa_sources.last_error` 与摘要里。
pub async fn pump_once(store: &std::sync::Arc<Store>) -> Value {
    let started = ledger::now_ms();
    let sources = match ledger::list_sources(store) {
        Ok(list) => list,
        Err(e) => return json!({ "ok": false, "error": e }),
    };
    let enabled: Vec<SourceRow> = sources.into_iter().filter(|s| s.enabled).collect();
    if enabled.is_empty() {
        return json!({ "ok": true, "skipped": "no_enabled_source", "sources": [] });
    }
    let (policy_version, doc) = ledger::effective_policy(store);
    let mut runs: Vec<SourceRun> = Vec::new();
    let mut cap_used = ledger::count_decisions_today(store).unwrap_or(0);
    for source in enabled {
        let mut run = SourceRun {
            source_id: source.id.clone(),
            kind: source.kind.clone(),
            instance: source.instance.clone(),
            fetched: 0,
            inserted: 0,
            decided: 0,
            candidates: 0,
            status: "ok",
            error: None,
        };
        match collect_triggers(store, &source).await {
            Ok(Triggers::Items(items)) => {
                run.fetched = items.len();
                let _ = ledger::note_source_error(store, &source.id, None);
                for trigger in items {
                    match ledger::ingest(store, &trigger) {
                        Ok(outcome) if outcome.inserted => {
                            run.inserted += 1;
                            let (decided, candidate, note) = decide_and_record(
                                store,
                                &source,
                                &doc,
                                policy_version,
                                &mut cap_used,
                                doc.daily_eval_cap,
                                &outcome.trigger,
                            );
                            run.decided += decided;
                            run.candidates += candidate;
                            if let Some(reason) = note {
                                run.status = "capped";
                                run.error = Some(reason);
                            }
                        }
                        Ok(_) => {}
                        Err(e) => {
                            run.status = "ingest_failed";
                            run.error = Some(e.clone());
                            let _ = ledger::note_source_error(store, &source.id, Some(&e));
                        }
                    }
                }
            }
            Ok(Triggers::Waiting) => {
                run.status = "waiting";
            }
            Ok(Triggers::Idle) => {
                run.status = "idle_login_required";
            }
            Err(e) => {
                run.status = "fetch_failed";
                run.error = Some(e.clone());
                let _ = ledger::note_source_error(store, &source.id, Some(&e));
            }
        }
        runs.push(run);
    }
    let total = |field: &str| -> i64 {
        runs.iter()
            .map(|r| r.to_value()[field].as_i64().unwrap_or(0))
            .sum()
    };
    json!({
        "ok": true,
        "startedAtMs": started,
        "tookMs": ledger::now_ms() - started,
        "acceptMode": doc.accept_mode,
        "policyVersion": policy_version,
        "dailyEvalCap": doc.daily_eval_cap,
        "capUsed": cap_used,
        "fetched": total("fetched"),
        "inserted": total("inserted"),
        "decided": total("decided"),
        "candidates": total("candidates"),
        "sources": runs.iter().map(SourceRun::to_value).collect::<Vec<_>>(),
        "note": "影子模式：本函数只记账与判定，不创建任务、不执行任何动作",
    })
}

#[derive(Debug)]
enum Triggers {
    Items(Vec<NewTrigger>),
    /// 到点判定没到：本轮无事可做，不是错误
    Waiting,
    /// 需要登录态才能读，未登录是常态而不是故障，不写 last_error
    Idle,
}

async fn collect_triggers(
    store: &std::sync::Arc<Store>,
    source: &SourceRow,
) -> Result<Triggers, String> {
    match source.kind.as_str() {
        SOURCE_KIND_PROJECT_EVENT => match fetch_project_events(store, source).await? {
            // 未登录不是故障：本机没配账号服务时这一路本来就没数据，不写 last_error
            None => Ok(Triggers::Idle),
            Some(events) => {
                let items: Vec<NewTrigger> = events
                    .iter()
                    .filter_map(|event| project_event_to_trigger(source, event))
                    .collect();
                Ok(Triggers::Items(items))
            }
        }
        SOURCE_KIND_SCHEDULE | SOURCE_KIND_CONDITION => {
            let config = source.config.clone();
            match schedule_due(&config, source.cursor, ledger::now_ms())? {
                None => Ok(Triggers::Waiting),
                Some((0, _, _)) => {
                    // 首次登记只把游标推到「现在」，不发事件
                    let _ = ledger::advance_cursor(store, &source.id, ledger::now_ms());
                    Ok(Triggers::Waiting)
                }
                Some((due_ms, external_id, note)) => {
                    let item = NewTrigger {
                        source_id: source.id.clone(),
                        external_id,
                        kind: match source.kind.as_str() {
                            SOURCE_KIND_CONDITION => "condition.met",
                            _ => "schedule.due",
                        }
                        .to_string(),
                        text: note,
                        actor: String::new(),
                        thread_key: String::new(),
                        addressed_agent: false,
                        source_seq: due_ms,
                        payload: json!({ "dueMs": due_ms, "config": config }),
                        arrived_at_ms: Some(due_ms),
                    };
                    Ok(Triggers::Items(vec![item]))
                }
            }
        }
        SOURCE_KIND_WORKSPACE_FILE => {
            let dir = source
                .config
                .get("dir")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if dir.is_empty() {
                return Err("SUPERAGENT_WORKSPACE_DIR_MISSING: 工作区入口没有配置目录".to_string());
            }
            let root = PathBuf::from(&dir);
            if !root.is_dir() {
                return Err(format!(
                    "SUPERAGENT_WORKSPACE_DIR_MISSING: 目录不存在或不是目录（{dir}）"
                ));
            }
            let changes = workspace_file_changes(&root, source.cursor, MAX_FILE_ENTRIES)?;
            let items: Vec<NewTrigger> = changes
                .iter()
                .map(|change| file_change_to_trigger(source, change))
                .collect();
            Ok(Triggers::Items(items))
        }
        SOURCE_KIND_IM_INBOUND => Err(
            "SUPERAGENT_SOURCE_NOT_WIRED: IM 入站仍在引擎侧，尚未接到桌面账本".to_string(),
        ),
        other => Err(format!(
            "SUPERAGENT_SOURCE_KIND_UNSUPPORTED: 事件源类型 {other} 不在本段支持范围"
        )),
    }
}

/// gate 判定并留痕；返回 `(新增判定数, 候选数, 达到上限时的说明)`
///
/// `kind` / `instance` 一律取自正在巡检的 `SourceRow`，而不是事件行：`sa_triggers`
/// 只存 `source_id`，入口是什么类型、属于哪个项目实例由 `sa_sources` 说话。
/// 让 gate 读事件表里的字段就是第二套口径。
fn decide_and_record(
    store: &std::sync::Arc<Store>,
    source: &SourceRow,
    doc: &PolicyDoc,
    policy_version: i64,
    cap_used: &mut i64,
    daily_cap: i64,
    trigger: &ledger::TriggerRow,
) -> (usize, usize, Option<String>) {
    if *cap_used >= daily_cap {
        return (
            0,
            0,
            Some(format!(
                "今日研判已达上限 {daily_cap} 条，事件仍照常记账，判定本轮跳过"
            )),
        );
    }
    let input = GateInput {
        kind: &source.kind,
        instance: &source.instance,
        actor: &trigger.actor,
        event_kind: &trigger.kind,
        text: &trigger.text,
        thread_key: &trigger.thread_key,
        addressed_agent: trigger.addressed_agent,
    };
    let verdict = gate::decide(doc, &input);
    // 「同 Thread 只跑一次」：已有阳性判定的会话不再重复攒候选，但阴性照旧留痕
    if verdict.is_positive()
        && ledger::thread_has_candidate(store, &trigger.thread_key, policy_version)
            .unwrap_or(false)
    {
        return (0, 0, Some("该会话在本策略版本下已有候选，本轮不重复记账判定".to_string()));
    }
    match ledger::record_decision(store, &trigger.id, policy_version, &verdict) {
        Ok((_, true)) => {
            *cap_used += 1;
            (1, usize::from(verdict.would_accept), None)
        }
        Ok((_, false)) => (0, 0, None),
        Err(_) => (0, 0, None),
    }
}

// ── 项目事件读取（账号服务增量补拉）─────────────────────────────

/// 用 D-06 的补拉接口读游标之后的事件。
///
/// `Ok(None)` 表示「本机没有登录态」——那是常态而不是故障，既不写 last_error 也不报错；
/// 401 时强制刷新一次令牌再试，仍然失败就如实报错，不静默跳过。
/// 走 HTTP 补拉而不是 SSE：页面已经有 D-06 的实时订阅，账本要的是「不重不漏」而不是毫秒级。
async fn fetch_project_events(
    store: &std::sync::Arc<Store>,
    source: &SourceRow,
) -> Result<Option<Vec<Value>>, String> {
    let base = normalize_base(&auth_service::auth_server_url(store))?;
    let client = client_for_base(&base)?;
    let mut token = match auth_service::access_token(store).await {
        Ok(token) => token,
        Err(e) if e.contains("COLLAB_LOGIN_REQUIRED") => return Ok(None),
        Err(e) => return Err(e),
    };
    let mut cursor = source.cursor;
    let mut events: Vec<Value> = Vec::new();
    for _ in 0..MAX_EVENT_PAGES {
        let url = crate::collab::watch::events_url(&base, &source.instance, cursor)?;
        let body = match get_json(&client, &url, &token).await {
            Ok(body) => body,
            Err(e) if e.contains("COLLAB_AUTH_EXPIRED") => {
                token = auth_service::force_access_token(store).await?;
                get_json(&client, &url, &token)
                    .await
                    .map_err(|e| format!("SUPERAGENT_FETCH_EVENTS_FAILED: {e}"))?
            }
            Err(e) => return Err(format!("SUPERAGENT_FETCH_EVENTS_FAILED: {e}")),
        };
        let page: Vec<Value> = body
            .get("events")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let is_full_page = (page.len() as i64) >= EVENT_PAGE_LIMIT;
        let Some(last) = page
            .iter()
            .filter_map(|e| e.get("seq").and_then(|v| v.as_i64()))
            .max()
        else {
            break;
        };
        // 服务端按 `seq > since` 读表，所以本页最大值就是下一页的起点
        cursor = cursor.max(last);
        events.extend(page);
        if !is_full_page {
            break;
        }
    }
    Ok(Some(events))
}

/// GET 一个 JSON 响应（口径与 `collab` 的在线请求一致）：
/// 令牌只进 `Authorization` 头，不拼进 URL，也不出现在错误文案里；
/// 服务端错误体形如 `{code, message}`，原样拼成 `CODE: 中文`，不附带响应正文。
async fn get_json(client: &reqwest::Client, url: &str, token: &str) -> Result<Value, String> {
    let resp = client
        .get(url)
        .header(reqwest::header::AUTHORIZATION, format!("Bearer {token}"))
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .await
        .map_err(|e| format!("无法连接账号服务（{}）", network_reason(&e)))?;
    let status = resp.status();
    if status == reqwest::StatusCode::UNAUTHORIZED {
        return Err("COLLAB_AUTH_EXPIRED: 登录态已失效".to_string());
    }
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("SUPERAGENT_FETCH_EVENTS_FAILED: 响应无法解析（{e}）"))?;
    if !status.is_success() || body.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        let code = body.get("code").and_then(|v| v.as_str()).unwrap_or("");
        let message = body
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("账号服务返回错误");
        return Err(if code.is_empty() {
            format!("COLLAB_HTTP_{status}: {message}", status = status.as_u16())
        } else {
            format!("{code}: {message}")
        });
    }
    Ok(body)
}

/// 后台常驻循环（桌面端内，随应用存活；不宣称 7×24）
///
/// 没有任何启用入口时按 `IDLE_SCAN_INTERVAL` 轻睡，避免「刚登记一个入口要等满五分钟」。
/// 单轮出错只影响那一轮：循环不退出，也不因为一次网络失败就停止记账。
/// 只在「有事发生」时投一次事件（新事件、新候选、或某个入口报错），空转不打扰界面。
///
/// 第二层（模型研判）挂在这一轮之后，而且只在真的产出了候选时才尝试：
/// 「立即巡检」那个按钮不花模型钱，自主研判由循环按策略预算跑——两者分开，
/// 界面才说得清这笔花费是谁发起的。预算与配额闸门都在 `assess::run` 里检查。
pub async fn run_loop(
    store: std::sync::Arc<Store>,
    app: tauri::AppHandle,
    quota: std::sync::Arc<crate::task::quota::QuotaGate>,
) {
    loop {
        let mut summary = pump_once(&store).await;
        if summary
            .get("candidates")
            .and_then(|v| v.as_i64())
            .unwrap_or(0)
            > 0
        {
            summary["assessment"] = crate::superagent::assess::run(&store, &quota, None).await;
        }
        if worth_noticing(&summary) {
            // 与「立即巡检」命令走同一份显示名加工，界面两条路径看到的形状一致
            let summary = crate::superagent::label_pump_summary(&summary);
            let _ = app.emit(crate::superagent::EVENT_SUPERAGENT, &summary);
        }
        let busy = summary
            .get("sources")
            .and_then(|v| v.as_array())
            .map(|list| !list.is_empty())
            .unwrap_or(false);
        tokio::time::sleep(if busy {
            SCAN_INTERVAL
        } else {
            IDLE_SCAN_INTERVAL
        })
        .await;
    }
}

/// 这一轮的摘要值不值得投给界面：什么都没变就不发（发一次，页面就刷一次）
fn worth_noticing(summary: &Value) -> bool {
    let number = |key: &str| summary.get(key).and_then(|v| v.as_i64()).unwrap_or(0);
    if number("inserted") > 0 || number("candidates") > 0 {
        return true;
    }
    summary
        .get("sources")
        .and_then(|v| v.as_array())
        .map(|runs| {
            runs.iter().any(|run| {
                matches!(
                    run.get("status").and_then(|s| s.as_str()),
                    Some("fetch_failed") | Some("ingest_failed") | Some("capped")
                )
            })
        })
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::superagent::policy::MODE_OBSERVE;
    use rusqlite::params;
    use std::sync::Arc;

    fn temp_store(name: &str) -> (Arc<Store>, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-sa-src-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Arc::new(Store::open(dir.join("tongtian.db")).unwrap());
        (store, dir)
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("tt-sa-ws-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn cleanup(dir: &std::path::Path) {
        let _ = std::fs::remove_dir_all(dir);
    }

    fn write_file(dir: &std::path::Path, rel: &str) -> PathBuf {
        let path = dir.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&path, b"changed on disk, contents never read by the ledger").unwrap();
        path
    }

    fn source_row(kind: &str, instance: &str, cursor: i64, config: Value) -> SourceRow {
        SourceRow {
            id: "sasrc-test".to_string(),
            kind: kind.to_string(),
            instance: instance.to_string(),
            label: String::new(),
            enabled: true,
            cursor,
            config,
            last_error: None,
            created_at_ms: 0,
            updated_at_ms: 0,
        }
    }

    /// 把游标拨回过去，等价于「应用关掉了一段时间，现在又到点了」。
    /// `ledger::advance_cursor` 只许前进，测试要造历史水位只能直接写库。
    fn rewind_cursor(store: &Arc<Store>, source_id: &str, to: i64) {
        store
            .with_conn(|c| {
                c.execute(
                    "UPDATE sa_sources SET cursor = ?1 WHERE id = ?2",
                    params![to, source_id],
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
            })
            .unwrap();
    }

    // ── 项目事件映射（纯函数）──────────────────────────────────

    #[test]
    fn project_event_maps_upstream_fields_and_never_claims_a_mention() {
        let source = source_row(
            SOURCE_KIND_PROJECT_EVENT,
            "prj-1",
            0,
            json!({ "watch": ["task.commented"] }),
        );
        let event = json!({
            "seq": 41,
            "kind": "task.commented",
            "actorUid": "usr-owner",
            "createdAtMs": 1_730_000_000_000i64,
            "payload": { "comment": { "id": "tc-1", "taskId": "tsk-9", "body": "帮我评审这份部署方案" } },
        });
        let trigger = project_event_to_trigger(&source, &event).unwrap();
        assert_eq!(trigger.source_id, "sasrc-test");
        assert_eq!(trigger.external_id, "41", "seq 就是幂等键，重复补拉只会落一行");
        assert_eq!(trigger.source_seq, 41);
        assert_eq!(trigger.actor, "usr-owner", "发起人取来源声明的 actorUid");
        assert_eq!(trigger.text, "帮我评审这份部署方案");
        assert_eq!(trigger.thread_key, "tsk-9", "会话键取服务端声明的 taskId");
        assert_eq!(trigger.arrived_at_ms, Some(1_730_000_000_000));
        assert!(
            !trigger.addressed_agent,
            "项目事件没有结构化的提及字段，正文里的 @ 不许变成直接指派"
        );

        // 任务创建：正文取标题，会话键取 task.id
        let created = json!({
            "seq": 42, "kind": "task.created", "actorUid": "usr-peer",
            "payload": { "task": { "id": "tsk-8", "title": "把网关部署到测试机" } },
        });
        let trigger = project_event_to_trigger(&source, &created).unwrap();
        assert_eq!(trigger.text, "把网关部署到测试机");
        assert_eq!(trigger.thread_key, "tsk-8");
        assert_eq!(trigger.kind, "task.created");

        // 状态变更：没有正文可读，如实写成人能看懂的一句
        let status = json!({
            "seq": 43, "kind": "task.status_changed", "actorUid": "usr-owner",
            "payload": { "task": { "id": "tsk-8", "status": "in_review" } },
        });
        let trigger = project_event_to_trigger(&source, &status).unwrap();
        assert_eq!(trigger.text, "任务状态变更为 in_review");

        // 不认识的事件类型：正文用类型名，不编造内容
        let unknown = json!({ "seq": 44, "kind": "member.invited", "payload": {} });
        let trigger = project_event_to_trigger(&source, &unknown).unwrap();
        assert_eq!(trigger.text, "member.invited");
        assert!(trigger.thread_key.is_empty(), "没有会话归属就留空，不许瞎猜");

        // 缺 seq / 缺 kind 的事件无法记账（没有幂等键就不能进账本）
        assert!(project_event_to_trigger(&source, &json!({ "kind": "task.created" })).is_none());
        assert!(project_event_to_trigger(&source, &json!({ "seq": 45 })).is_none());
    }

    // ── 定时/条件到点数学 ─────────────────────────────────────

    #[test]
    fn first_registration_only_moves_the_cursor_without_firing() {
        // 「打开入口」这件事本身不该立刻变成一条候选
        let due = schedule_due(&json!({ "everyMs": 3_600_000 }), 0, 10_000_000).unwrap();
        assert_eq!(due.unwrap().0, 0, "首次登记只推游标，不发事件");
    }

    #[test]
    fn periodic_source_fires_one_event_per_tick_and_never_twice() {
        let config = json!({ "everyMs": 60_000, "note": "该看看构建有没有红" });
        // 未到点
        assert!(schedule_due(&config, 1_000_000, 1_030_000).unwrap().is_none());
        // 到点：一条 tick 事件，游标应推进到该刻度
        let fired = schedule_due(&config, 1_000_000, 1_060_000).unwrap().unwrap();
        assert_eq!(fired.0, 1_060_000);
        assert_eq!(fired.1, "tick-1060000", "幂等键由刻度决定，重复巡检只落一行");
        assert_eq!(fired.2, "该看看构建有没有红");
        // 同一刻度再查一遍（游标已推进）：不再触发
        assert!(schedule_due(&config, 1_060_000, 1_060_000).unwrap().is_none());
        // 落后很多轮也只补一条：事件源不是追赶进度的地方
        let late = schedule_due(&config, 1_000_000, 1_900_000).unwrap().unwrap();
        assert_eq!(late.0, 1_060_000, "只按上一刻度推进，不许一次攒出十条候选");
    }

    #[test]
    fn interval_below_one_minute_is_clamped_not_honoured() {
        // 一秒钟一次的「主动接活」就是刷屏，低于下限按一分钟走
        let config = json!({ "everyMs": 1_000 });
        assert!(schedule_due(&config, 500_000, 500_500).unwrap().is_none());
        assert!(schedule_due(&config, 500_000, 560_000).unwrap().is_some());
    }

    #[test]
    fn one_shot_schedule_fires_once_after_the_deadline() {
        let config = json!({ "atMs": 2_000_000 });
        assert!(schedule_due(&config, 0, 1_000_000).unwrap().is_none(), "还没到点");
        let fired = schedule_due(&config, 1_500_000, 2_000_000).unwrap().unwrap();
        assert_eq!(fired.0, 2_000_000);
        assert_eq!(fired.1, "at-2000000");
        // 已消费过这个刻度（重启后重复巡检）不再触发第二遍
        assert!(schedule_due(&config, 2_000_000, 2_500_000).unwrap().is_none());
    }

    #[test]
    fn schedule_config_without_any_time_declaration_fails_loudly() {
        // 条件源如果既没有 everyMs 也没有 atMs，必须报「配置不对」，
        // 而不是静默永不触发——那会让人以为「等待额度」已经生效
        let err = schedule_due(&json!({ "note": "等额度用完" }), 1_000_000, 2_000_000).unwrap_err();
        assert!(err.contains("SUPERAGENT_SCHEDULE_INVALID"), "{err}");
        let err = schedule_due(&json!({ "everyMs": 0 }), 1_000_000, 2_000_000).unwrap_err();
        assert!(err.contains("SUPERAGENT_SCHEDULE_INVALID"), "{err}");
        let err = schedule_due(&json!({ "atMs": -5 }), 0, 2_000_000).unwrap_err();
        assert!(err.contains("SUPERAGENT_SCHEDULE_INVALID"), "{err}");
    }

    // ── 工作区文件扫描 ────────────────────────────────────────

    #[test]
    fn workspace_scan_reads_only_entries_inside_the_time_window() {
        let dir = temp_dir("window");
        write_file(&dir, "src/a.ts");
        let since = ledger::now_ms();
        // 之后不再新建文件：这一批全部落在窗口外
        let changes = workspace_file_changes(&dir, since + 60_000, MAX_FILE_ENTRIES).unwrap();
        assert!(changes.is_empty(), "窗口之外的文件不该被当成变化");
        let changes = workspace_file_changes(&dir, 0, MAX_FILE_ENTRIES).unwrap();
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].path, "src/a.ts", "相对路径统一用 /，界面才可比");
        assert!(changes[0].modified_ms > 0);
        cleanup(&dir);
    }

    #[test]
    fn workspace_scan_skips_vcs_and_dependency_directories() {
        let dir = temp_dir("ignore");
        write_file(&dir, "notes.md");
        write_file(&dir, ".hidden.md");
        write_file(&dir, "node_modules/left-pad/index.js");
        write_file(&dir, ".git/config");
        write_file(&dir, "target/debug/build.rs");
        let changes = workspace_file_changes(&dir, 0, MAX_FILE_ENTRIES).unwrap();
        let paths: Vec<&str> = changes.iter().map(|c| c.path.as_str()).collect();
        assert_eq!(paths, vec!["notes.md"], "版本库、依赖、构建产物与隐藏文件都不是工作区变化：{paths:?}");
        cleanup(&dir);
    }

    #[test]
    fn workspace_scan_limits_depth_and_entry_count() {
        let dir = temp_dir("limits");
        write_file(&dir, "top.md");
        write_file(&dir, "pkg/inner.md");
        write_file(&dir, "pkg/deeper/bottom.md");
        write_file(&dir, "pkg/deeper/deepest/no.md");
        // 深度上限 3：根目录算第 1 层，第 3 层里的文件还能看见，再往里就不翻了
        let deep = workspace_file_changes(&dir, 0, MAX_FILE_ENTRIES).unwrap();
        let paths: Vec<&str> = deep.iter().map(|c| c.path.as_str()).collect();
        assert_eq!(
            paths,
            vec!["pkg/deeper/bottom.md", "pkg/inner.md", "top.md"],
            "结果必须按路径排序（界面可比），且超过 {MAX_DIR_DEPTH} 层的目录不读"
        );
        // 条目上限：一次巡检不许把整棵目录树搬进摘要
        let capped = workspace_file_changes(&dir, 0, 2).unwrap();
        assert_eq!(capped.len(), 2, "封顶在 {MAX_FILE_ENTRIES} 条以内也要生效");
        cleanup(&dir);
    }

    #[test]
    fn workspace_scan_reports_a_missing_directory_instead_of_silence() {
        let dir = temp_dir("missing").join("nope");
        let err = workspace_file_changes(&dir, 0, MAX_FILE_ENTRIES).unwrap_err();
        assert!(err.contains("SUPERAGENT_WORKSPACE_SCAN_FAILED"), "{err}");
    }

    #[test]
    fn file_change_idempotency_key_includes_the_mtime() {
        let source = source_row(SOURCE_KIND_WORKSPACE_FILE, "", 0, json!({}));
        let first = file_change_to_trigger(
            &source,
            &FileChange { path: "a.md".into(), modified_ms: 111 },
        );
        let later = file_change_to_trigger(
            &source,
            &FileChange { path: "a.md".into(), modified_ms: 222 },
        );
        assert_eq!(first.kind, "file.changed");
        assert_eq!(first.external_id, "a.md:111");
        assert_ne!(first.external_id, later.external_id, "同一文件的两次修改是两条事件");
        assert_eq!(first.thread_key, "a.md", "会话键就是文件，同文件重复变更能被去重");
        assert!(first.actor.is_empty(), "本机文件没有发起人");
        assert!(!first.addressed_agent);
        // 载荷里只有路径与时间：不含任何正文
        assert_eq!(first.payload["path"].as_str(), Some("a.md"));
        assert!(first.payload.get("content").is_none());
    }

    // ── 巡检循环 ──────────────────────────────────────────────

    /// 一条「凡是从这个入口来的都算命中」的最小策略，只用来把 pump 跑通：
    /// 判定强度、日上限这些语义另有专门的测试盯着
    fn open_policy_raw(mode: &str, kind: &str) -> Value {
        json!({
            "acceptMode": mode,
            "dailyEvalCap": 50,
            "sources": [{
                "kind": kind, "instance": "", "enabled": true, "label": "本机入口",
                "allowUsers": [], "bots": [],
                "rules": [{ "id": "any", "enabled": true, "minChars": 4 }]
            }]
        })
    }

    #[tokio::test]
    async fn pump_moves_the_cursor_and_does_not_replay_on_the_next_round() {
        let (store, db_dir) = temp_store("pump-schedule");
        ledger::ensure_default_policy(&store).unwrap();
        save_policy_for(&store, &open_policy_raw(MODE_OBSERVE, SOURCE_KIND_SCHEDULE));
        let source =
            ledger::register_source(&store, SOURCE_KIND_SCHEDULE, "", "每小时", json!({ "everyMs": 60_000 }))
                .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();

        // 第一轮：首次登记只推游标，一条事件都不该有
        let first = pump_once(&store).await;
        assert_eq!(first["ok"].as_bool(), Some(true));
        assert_eq!(first["inserted"].as_i64(), Some(0));
        assert_eq!(first["sources"][0]["status"].as_str(), Some("waiting"));
        assert!(first["note"].as_str().unwrap().contains("不执行任何动作"));
        assert!(ledger::list_triggers(&store, None).unwrap().is_empty());

        // 把游标拨回 61 秒前：等价于「上一分钟到过点，现在刚巡检完一轮」。
        // 留 59 秒的余量，第三轮才是在问「同一个刻度会不会重放」而不是「下一个刻度到没到」
        rewind_cursor(&store, &source.id, ledger::now_ms() - 61_000);
        let second = pump_once(&store).await;
        assert_eq!(second["inserted"].as_i64(), Some(1), "到点就该有一条事件");
        assert_eq!(second["decided"].as_i64(), Some(1));
        assert_eq!(second["candidates"].as_i64(), Some(1));
        assert_eq!(second["acceptMode"].as_str(), Some(MODE_OBSERVE));
        let candidates = ledger::list_candidates(&store, None).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0]["sourceKind"].as_str(), Some(SOURCE_KIND_SCHEDULE));
        assert_eq!(candidates[0]["eventKind"].as_str(), Some("schedule.due"));
        assert_eq!(candidates[0]["wouldAccept"].as_bool(), Some(true));
        assert_eq!(candidates[0]["strength"].as_str(), Some(gate::STRENGTH_INFERRED));

        // 第三轮：游标已越过刻度，同一条事件不许再变成第二个候选
        let third = pump_once(&store).await;
        assert_eq!(third["inserted"].as_i64(), Some(0), "未到点就不该重放");
        assert_eq!(third["candidates"].as_i64(), Some(0));
        assert_eq!(ledger::list_candidates(&store, None).unwrap().len(), 1);
        cleanup(&db_dir);
    }

    fn save_policy_for(store: &Arc<Store>, raw: &Value) {
        ledger::save_policy(store, raw, "test").unwrap();
    }

    #[tokio::test]
    async fn pump_with_default_policy_records_no_candidate() {
        // 缺省策略里没有任何入口授权：影子模式下连「本可接单」都不该记
        let (store, db_dir) = temp_store("pump-ungranted");
        ledger::ensure_default_policy(&store).unwrap();
        let source =
            ledger::register_source(&store, SOURCE_KIND_CONDITION, "", "等一小时", json!({ "everyMs": 60_000 }))
                .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        pump_once(&store).await;
        rewind_cursor(&store, &source.id, ledger::now_ms() - 120_000);
        let summary = pump_once(&store).await;
        assert_eq!(summary["inserted"].as_i64(), Some(1), "没授权也要照常记账");
        assert_eq!(summary["decided"].as_i64(), Some(1));
        assert_eq!(summary["candidates"].as_i64(), Some(0), "入口没被许可就不许攒候选");
        let decisions = ledger::list_decisions(&store, None).unwrap();
        assert_eq!(decisions.len(), 1);
        assert_eq!(decisions[0].strength, gate::STRENGTH_NOT_ADMITTED);
        assert!(decisions[0].basis.contains("不在许可策略里"), "{}", decisions[0].basis);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn pump_collects_workspace_changes_without_reading_file_contents() {
        let (store, db_dir) = temp_store("pump-ws");
        let watched = temp_dir("pump-ws");
        ledger::ensure_default_policy(&store).unwrap();
        save_policy_for(&store, &open_policy_raw(MODE_OBSERVE, SOURCE_KIND_WORKSPACE_FILE));
        let source = ledger::register_source(
            &store,
            SOURCE_KIND_WORKSPACE_FILE,
            "",
            "工作区",
            json!({ "dir": watched.to_string_lossy() }),
        )
        .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        // 文件名本身会进账本，正文不会：写一段带「密钥」字样的内容，验证它没被搬走
        write_file(&watched, "docs/plan.md");

        let summary = pump_once(&store).await;
        assert_eq!(summary["inserted"].as_i64(), Some(1));
        assert_eq!(summary["sources"][0]["status"].as_str(), Some("ok"));
        let triggers = ledger::list_triggers(&store, None).unwrap();
        assert_eq!(triggers.len(), 1);
        assert_eq!(triggers[0].kind, "file.changed");
        assert_eq!(triggers[0].text, "docs/plan.md");
        assert_eq!(triggers[0].external_id, format!("docs/plan.md:{}", triggers[0].source_seq));
        assert!(
            !triggers[0].payload.to_string().contains("changed on disk"),
            "这条链路只记目录项，不许把文件内容搬进账本"
        );

        // 第二轮：游标已停在 mtime 水位上，同一个文件不该再报一次
        let again = pump_once(&store).await;
        assert_eq!(again["fetched"].as_i64(), Some(0), "没有新变化就不该重记");
        assert_eq!(ledger::list_triggers(&store, None).unwrap().len(), 1);
        // 真正改一次：新 mtime 越过水位，才成为第二条事件。
        // 睡一秒是为了绕开个别文件系统只有秒级的 mtime 分辨率
        std::thread::sleep(Duration::from_millis(1_100));
        write_file(&watched, "docs/plan.md");
        let third = pump_once(&store).await;
        assert_eq!(third["inserted"].as_i64(), Some(1));
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn pump_reports_a_missing_workspace_directory_in_the_summary() {
        let (store, db_dir) = temp_store("pump-ws-missing");
        ledger::ensure_default_policy(&store).unwrap();
        let gone = temp_dir("pump-ws-missing").join("nope");
        let source = ledger::register_source(
            &store,
            SOURCE_KIND_WORKSPACE_FILE,
            "",
            "已移走的目录",
            json!({ "dir": gone.to_string_lossy() }),
        )
        .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        let summary = pump_once(&store).await;
        assert_eq!(summary["ok"].as_bool(), Some(true), "单个入口读不到不许拖垮整轮");
        assert_eq!(summary["sources"][0]["status"].as_str(), Some("fetch_failed"));
        assert!(summary["sources"][0]["error"]
            .as_str()
            .unwrap()
            .contains("SUPERAGENT_WORKSPACE_DIR_MISSING"));
        // 错误必须落到入口行上，界面才看得到「为什么一直没候选」
        let row = ledger::get_source(&store, &source.id).unwrap().unwrap();
        assert!(row.last_error.unwrap().contains("SUPERAGENT_WORKSPACE_DIR_MISSING"));
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn pump_fails_loudly_for_im_inbound_because_it_is_not_wired_yet() {
        let (store, db_dir) = temp_store("pump-im");
        ledger::ensure_default_policy(&store).unwrap();
        let source =
            ledger::register_source(&store, SOURCE_KIND_IM_INBOUND, "cid-1", "企业微信群", json!({}))
                .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        let summary = pump_once(&store).await;
        assert_eq!(summary["sources"][0]["status"].as_str(), Some("fetch_failed"));
        let err = summary["sources"][0]["error"].as_str().unwrap().to_string();
        assert!(err.contains("SUPERAGENT_SOURCE_NOT_WIRED"), "{err}");
        assert!(err.contains("引擎侧"), "报错要说清缺的是哪一段：{err}");
        assert_eq!(summary["inserted"].as_i64(), Some(0));
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn pump_skips_when_nothing_is_enabled_and_leaves_the_ledger_untouched() {
        let (store, db_dir) = temp_store("pump-empty");
        ledger::ensure_default_policy(&store).unwrap();
        let source =
            ledger::register_source(&store, SOURCE_KIND_SCHEDULE, "", "登记但没开", json!({ "everyMs": 60_000 }))
                .unwrap();
        let summary = pump_once(&store).await;
        assert_eq!(summary["skipped"].as_str(), Some("no_enabled_source"));
        assert_eq!(summary["sources"].as_array().unwrap().len(), 0);
        assert!(ledger::list_triggers(&store, None).unwrap().is_empty());
        assert_eq!(ledger::get_source(&store, &source.id).unwrap().unwrap().cursor, 0);
        cleanup(&db_dir);
    }

    #[test]
    fn daily_cap_stops_decisions_but_never_stops_bookkeeping() {
        let (store, db_dir) = temp_store("cap");
        ledger::ensure_default_policy(&store).unwrap();
        let source =
            ledger::register_source(&store, SOURCE_KIND_SCHEDULE, "", "定时", json!({})).unwrap();
        let outcome = ledger::ingest(
            &store,
            &NewTrigger {
                source_id: source.id.clone(),
                external_id: "tick-1".to_string(),
                kind: "schedule.due".to_string(),
                text: "该看看构建有没有红".to_string(),
                actor: String::new(),
                thread_key: String::new(),
                addressed_agent: false,
                source_seq: 1,
                payload: json!({}),
                arrived_at_ms: Some(ledger::now_ms()),
            },
        )
        .unwrap();
        let (_version, doc) = ledger::effective_policy(&store);
        let mut used = 0;
        // 上限已到：事件仍然在账本里，但这一轮不判
        let (decided, candidates, note) =
            decide_and_record(&store, &source, &doc, 1, &mut used, 0, &outcome.trigger);
        assert_eq!((decided, candidates), (0, 0));
        assert!(note.unwrap().contains("今日研判已达上限"));
        assert!(ledger::list_decisions(&store, None).unwrap().is_empty());
        assert_eq!(ledger::list_triggers(&store, None).unwrap().len(), 1, "超限只停判定，不停记账");
        cleanup(&db_dir);
    }

    #[test]
    fn same_thread_gets_one_candidate_per_policy_version() {
        let (store, db_dir) = temp_store("thread-dedupe");
        // 只存一版策略：它就是版本 1，改一个字就是版本 2，好验证「换版本允许重判」
        save_policy_for(&store, &open_policy_raw(MODE_OBSERVE, SOURCE_KIND_WORKSPACE_FILE));
        let (version, doc) = ledger::effective_policy(&store);
        assert_eq!(version, 1);
        let source =
            ledger::register_source(&store, SOURCE_KIND_WORKSPACE_FILE, "", "工作区", json!({})).unwrap();
        let mut used = 0;
        let first = ledger::ingest(
            &store,
            &file_change_to_trigger(
                &source,
                &FileChange { path: "docs/plan.md".into(), modified_ms: 100 },
            ),
        )
        .unwrap();
        let (decided, candidates, note) =
            decide_and_record(&store, &source, &doc, 1, &mut used, 50, &first.trigger);
        assert_eq!((decided, candidates), (1, 1), "{:?}", note);
        assert!(note.is_none());

        // 同一会话（同一个文件）的第二次变更：阳性判定不再重复攒候选
        let second = ledger::ingest(
            &store,
            &file_change_to_trigger(
                &source,
                &FileChange { path: "docs/plan.md".into(), modified_ms: 200 },
            ),
        )
        .unwrap();
        let (decided, candidates, note) =
            decide_and_record(&store, &source, &doc, 1, &mut used, 50, &second.trigger);
        assert_eq!((decided, candidates), (0, 0), "同一 Thread 同时只跑一次研判");
        assert!(note.unwrap().contains("已有候选"));
        assert_eq!(ledger::list_candidates(&store, None).unwrap().len(), 1);

        // 换一版策略（版本 2）：允许重判，旧候选仍是同一 Thread 的一条
        let (version2, _) = {
            let mut wider = open_policy_raw(MODE_OBSERVE, SOURCE_KIND_WORKSPACE_FILE);
            wider["sources"][0]["label"] = json!("工作区（改名）");
            ledger::save_policy(&store, &wider, "test").unwrap();
            ledger::effective_policy(&store)
        };
        assert_eq!(version2, 2);
        let (decided, candidates, _) =
            decide_and_record(&store, &source, &doc, version2, &mut used, 50, &second.trigger);
        assert_eq!((decided, candidates), (1, 1), "策略变更后按新版本重判");
        assert_eq!(ledger::list_candidates(&store, None).unwrap().len(), 2, "两个版本各留一条，旧判定不许被删");
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn unlisted_event_source_kind_is_rejected_at_the_door() {
        // 事件源不认识就是错误，不能悄悄什么都不做
        let (store, db_dir) = temp_store("bad-kind");
        ledger::ensure_default_policy(&store).unwrap();
        let source = source_row("email", "inbox", 0, json!({}));
        let err = collect_triggers(&store, &source).await.unwrap_err();
        assert!(err.contains("SUPERAGENT_SOURCE_KIND_UNSUPPORTED"), "{err}");
        cleanup(&db_dir);
    }

    #[test]
    fn scan_constants_are_conservative() {
        assert_eq!(MIN_INTERVAL_MS, 60_000, "低于一分钟就是把主动接活变成轮询");
        assert!(MAX_FILE_ENTRIES <= 5_000 && MAX_DIR_DEPTH <= 4, "扫描不许把整块磁盘挂成事件源");
        assert!(IDLE_SCAN_INTERVAL < SCAN_INTERVAL, "刚登记入口不该等满一个巡检周期");
    }
}
