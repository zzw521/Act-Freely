//! 项目事件订阅（缺陷 D-06）：把账号服务的项目事件流转成桌面事件，替掉 10 秒轮询。
//!
//! 语义对齐 `tongtian-server/app/projects.py`，并以 `tests/test_server.py`
//! 的「项目事件流·实时与找回」为实测依据（本机 509ms / 518ms 可见，断线补齐与接口一致）：
//! - 事件带全局自增 `seq`（跨项目不连续），只能比大小，不能假设相邻；
//! - 订阅始终传 `since=已交付的最大 seq`，服务端按 `seq > since` 读表，
//!   断线期间写入的事件在重连首轮补拉，不丢也不重；
//! - 被移出项目时服务端**只**发 `closed(membership)`，`member.removed` 本身不经流下发，
//!   所以 closed 是唯一终止信号：收到就停，界面据此提示「你已不在该项目」；
//! - 空闲 15 秒服务端发 `: ping`；反代仍可能掐流（D-01 实测：掐断 SSE 的是空闲超时），
//!   因此本地有 45 秒静默看门狗，超时当作断线重连，而不是等页面下次刷新。
//!
//! 令牌边界：access token 只进 Authorization 头，不进 URL、日志与错误文案；
//! 401 之后下一轮强制刷新（`force_access_token`），不在流里携带任何凭据。

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use futures_util::StreamExt;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, State};

use crate::auth::service as auth_service;
use crate::collab::{network_reason, normalize_base, stream_client_for_base};
use crate::store::Store;
use crate::P0Services;

/// 渲染层订阅的项目事件
pub const EVENT_COLLAB: &str = "collab-event";
/// 订阅自身状态（streaming / degraded / closed / stopped…），界面据此决定实时还是降级轮询
pub const EVENT_COLLAB_STATE: &str = "collab-watch-state";

/// 读超时：服务端心跳 15 秒，留 3 倍余量。超过即认定连接已死（被反代掐断或对端崩溃）。
const READ_TIMEOUT: Duration = Duration::from_secs(45);
/// 重连退避（毫秒），超出末位后固定按上限重试；收到事件即归零。
const BACKOFF_MS: [u64; 5] = [500, 1_000, 2_000, 4_000, 8_000];
const BACKOFF_CAP_MS: u64 = 15_000;

// ── 纯函数：可单测，不碰网络 ──────────────────────────────────

/// 项目事件流地址：游标走查询参数，凭据一律走请求头
pub fn stream_url(base: &str, project_id: &str, cursor: i64) -> Result<String, String> {
    if !is_safe_project_id(project_id) {
        return Err("COLLAB_INVALID_INPUT: 项目 id 不合法，无法订阅事件".to_string());
    }
    let base = normalize_base(base)?;
    Ok(format!(
        "{base}/api/v1/projects/{project_id}/events/stream?since={cursor}"
    ))
}

/// 增量补拉地址（订阅断开期间的兜底，语义与流一致）
pub fn events_url(base: &str, project_id: &str, cursor: i64) -> Result<String, String> {
    if !is_safe_project_id(project_id) {
        return Err("COLLAB_INVALID_INPUT: 项目 id 不合法，无法读取事件".to_string());
    }
    let base = normalize_base(base)?;
    Ok(format!(
        "{base}/api/v1/projects/{project_id}/events?since={cursor}&limit=200"
    ))
}

fn is_safe_project_id(raw: &str) -> bool {
    let id = raw.trim();
    !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// 第 attempt 次重连前应等待的毫秒数（0 次起算，指数退避后封顶）
pub fn backoff_ms(attempt: usize) -> u64 {
    match BACKOFF_MS.get(attempt) {
        Some(ms) => *ms,
        None => BACKOFF_CAP_MS,
    }
}

/// SSE 事件块（以空行分隔的一段）。`Comment` 是心跳，不是事件。
#[derive(Debug, PartialEq, Clone)]
pub enum Frame {
    Named {
        name: String,
        id: Option<i64>,
        data: Value,
    },
    Comment,
    Empty,
}

/// 解析一个 SSE 块：`event:` 名称、`data:` JSON、`id:` 游标；无法解析 JSON 时给 raw。
pub fn parse_frame(block: &str) -> Frame {
    let mut name = String::new();
    let mut data = String::new();
    let mut id: Option<i64> = None;
    let mut saw_field = false;
    for line in block.lines() {
        if line.is_empty() {
            continue;
        }
        if let Some(rest) = line.strip_prefix(':') {
            // `: ping` / `: keepalive`：只证明连接还活着
            let _ = rest;
            continue;
        }
        if let Some(rest) = line.strip_prefix("event:") {
            name = rest.trim().to_string();
            saw_field = true;
        } else if let Some(rest) = line.strip_prefix("data:") {
            let value = rest.strip_prefix(' ').unwrap_or(rest.trim());
            if !data.is_empty() {
                data.push('\n');
            }
            data.push_str(value);
            saw_field = true;
        } else if let Some(rest) = line.strip_prefix("id:") {
            id = rest.trim().parse::<i64>().ok();
            saw_field = true;
        }
    }
    if !saw_field {
        return Frame::Comment;
    }
    if name.is_empty() && data.is_empty() && id.is_none() {
        return Frame::Empty;
    }
    let parsed: Value = if data.is_empty() {
        json!({})
    } else {
        serde_json::from_str(&data).unwrap_or_else(|_| json!({ "raw": data }))
    };
    Frame::Named {
        name: if name.is_empty() {
            "message".to_string()
        } else {
            name
        },
        id,
        data: parsed,
    }
}

/// 从缓冲区里取走第一个完整块（以空行分隔）；CRLF 已在写入前归一成 LF。
pub fn take_block(buffer: &mut String) -> Option<String> {
    let pos = buffer.find("\n\n")?;
    let head = buffer[..pos].to_string();
    buffer.drain(..pos + 2);
    Some(head)
}

/// 游标推进：只有比当前水位更新才接受（返回 true）。seq 全局自增，跨项目不连续。
pub fn advance_cursor(cursor: &AtomicI64, seq: i64) -> bool {
    loop {
        let current = cursor.load(Ordering::SeqCst);
        if seq <= current {
            return false;
        }
        match cursor.compare_exchange(current, seq, Ordering::SeqCst, Ordering::SeqCst) {
            Ok(_) => return true,
            Err(_) => continue,
        }
    }
}

// ── 投递目标与令牌来源 ────────────────────────────────────────

/// 事件投递：桌面走 Tauri emit，测试走内存收集（同一份代码，避免只测「假」的那条路）
#[derive(Clone)]
pub enum Sink {
    Tauri(AppHandle),
    Collect(Arc<Mutex<Vec<(String, Value)>>>),
}

impl Sink {
    fn emit(&self, channel: &str, payload: Value) {
        match self {
            Sink::Tauri(app) => {
                let _ = app.emit(channel, payload);
            }
            Sink::Collect(boxed) => {
                if let Ok(mut log) = boxed.lock() {
                    log.push((channel.to_string(), payload));
                }
            }
        }
    }
}

/// 令牌来源：真实运行取账号服务，测试用固定值（都不会把令牌写进 URL）
pub enum TokenProvider {
    Static(String),
    Account(Arc<Store>),
}

impl TokenProvider {
    async fn get(&mut self, force_refresh: bool) -> Result<String, String> {
        match self {
            TokenProvider::Static(token) => Ok(token.clone()),
            TokenProvider::Account(store) => {
                if force_refresh {
                    auth_service::force_access_token(store).await
                } else {
                    auth_service::access_token(store).await
                }
            }
        }
    }
}

// ── 订阅循环 ──────────────────────────────────────────────────

#[derive(Debug, PartialEq)]
pub enum WatchOutcome {
    /// 主动停止（切项目、退出页面、退出登录）
    Stopped,
    /// 服务端明确关闭订阅（被移出项目、项目已删），不该再重连
    Closed(String),
}

enum Pump {
    /// 服务端发了 closed，附带原因
    Closed(String),
    /// 用户主动停止
    Stopped,
    /// 连接中断或静默超时，可重连（原因如实带给界面）
    Interrupted(String),
}

fn emit_state(sink: &Sink, project_id: &str, state: &str, cursor: i64, reason: &str) {
    sink.emit(
        EVENT_COLLAB_STATE,
        json!({
            "projectId": project_id,
            "state": state,
            "cursor": cursor,
            "reason": reason,
        }),
    );
}

fn emit_stopped(sink: &Sink, project_id: &str, cursor: &AtomicI64) {
    emit_state(
        sink,
        project_id,
        "stopped",
        cursor.load(Ordering::SeqCst),
        "",
    );
}

/// 退避等待。等待期间被要求停止时，先把 `stopped` 如实报给界面再结束循环：
/// 否则订阅在退避里悄悄退出，页面上的状态会停在「降级」不再变化。
async fn wait_before_retry(
    stop: &Arc<AtomicBool>,
    attempt: usize,
    sink: &Sink,
    project_id: &str,
    cursor: &Arc<AtomicI64>,
) -> bool {
    if sleep_backoff(stop, attempt).await {
        true
    } else {
        emit_stopped(sink, project_id, cursor);
        false
    }
}

/// 单条连接的读取与投递；返回为什么结束，由上层决定退避还是终止。
async fn pump_connection(
    response: reqwest::Response,
    project_id: &str,
    cursor: &Arc<AtomicI64>,
    sink: &Sink,
    stop: &Arc<AtomicBool>,
    delivered: &AtomicBool,
) -> Pump {
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    loop {
        if stop.load(Ordering::SeqCst) {
            return Pump::Stopped;
        }
        let next = tokio::time::timeout(READ_TIMEOUT, stream.next()).await;
        let chunk = match next {
            Err(_) => {
                return Pump::Interrupted(
                    "订阅静默超过 45 秒（连服务端 15 秒心跳都没到），按断线重连".to_string(),
                )
            }
            Ok(None) => {
                return Pump::Interrupted("事件流被服务端或网关结束".to_string());
            }
            Ok(Some(Err(err))) => {
                return Pump::Interrupted(format!(
                    "事件流读取失败：{}",
                    network_reason(&err)
                ))
            }
            Ok(Some(Ok(bytes))) => String::from_utf8_lossy(&bytes).replace('\r', ""),
        };
        buffer.push_str(&chunk);
        while let Some(block) = take_block(&mut buffer) {
            match parse_frame(&block) {
                Frame::Comment | Frame::Empty => continue,
                Frame::Named { name, id, data } => match name.as_str() {
                    "ready" => {
                        // 首包到达即认为订阅可用；游标以本地已交付水位为准，
                        // 服务端回带的 cursor 只作诊断（两者应一致）
                        delivered.store(true, Ordering::SeqCst);
                        emit_state(
                            sink,
                            project_id,
                            "streaming",
                            cursor.load(Ordering::SeqCst),
                            &data
                                .get("cursor")
                                .and_then(|v| v.as_i64())
                                .map(|v| format!("readyCursor={v}"))
                                .unwrap_or_default(),
                        );
                    }
                    "message" => {
                        let seq = data.get("seq").and_then(|v| v.as_i64()).or(id);
                        let Some(seq) = seq else { continue };
                        if !advance_cursor(cursor, seq) {
                            continue; // 已交付过（重连补拉重叠），不重复投给界面
                        }
                        delivered.store(true, Ordering::SeqCst);
                        sink.emit(
                            EVENT_COLLAB,
                            json!({
                                "projectId": project_id,
                                "seq": seq,
                                "kind": data.get("kind").cloned().unwrap_or(Value::Null),
                                "actorUid": data.get("actorUid").cloned().unwrap_or(Value::Null),
                                "payload": data.get("payload").cloned().unwrap_or(Value::Null),
                                "createdAtMs": data.get("createdAtMs").cloned().unwrap_or(Value::Null),
                            }),
                        );
                    }
                    "closed" => {
                        let reason = data
                            .get("reason")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown")
                            .to_string();
                        return Pump::Closed(match reason.as_str() {
                            "membership" => "你已不在该项目内，停止订阅".to_string(),
                            other => format!("服务端结束订阅（{other}）"),
                        });
                    }
                    other => {
                        // 未知事件名：不猜语义，只留游标不动，等下一块
                        let _ = other;
                    }
                },
            }
        }
    }
}

/// 订阅主循环：连接 → 投递 → 断了按游标退避重连，直到主动停止或服务端判终止。
pub async fn run_watch(
    client: reqwest::Client,
    base: String,
    project_id: String,
    cursor: Arc<AtomicI64>,
    mut tokens: TokenProvider,
    sink: Sink,
    stop: Arc<AtomicBool>,
) -> WatchOutcome {
    let mut attempt: usize = 0;
    loop {
        if stop.load(Ordering::SeqCst) {
            emit_state(
                &sink,
                &project_id,
                "stopped",
                cursor.load(Ordering::SeqCst),
                "",
            );
            return WatchOutcome::Stopped;
        }
        // 重连时强制刷新令牌：长任务里 access token 会过期，拿旧值反复撞 401 没有意义
        let token = match tokens.get(attempt > 0).await {
            Ok(token) => token,
            Err(err) => {
                emit_state(
                    &sink,
                    &project_id,
                    "degraded",
                    cursor.load(Ordering::SeqCst),
                    &err,
                );
                if !wait_before_retry(&stop, attempt, &sink, &project_id, &cursor).await {
                    return WatchOutcome::Stopped;
                }
                attempt += 1;
                continue;
            }
        };
        let url = match stream_url(&base, &project_id, cursor.load(Ordering::SeqCst)) {
            Ok(url) => url,
            Err(err) => {
                emit_state(&sink, &project_id, "closed", cursor.load(Ordering::SeqCst), &err);
                return WatchOutcome::Closed(err);
            }
        };
        emit_state(
            &sink,
            &project_id,
            "connecting",
            cursor.load(Ordering::SeqCst),
            "",
        );
        let response = client
            .get(&url)
            .header(reqwest::header::AUTHORIZATION, format!("Bearer {token}"))
            .header(reqwest::header::ACCEPT, "text/event-stream")
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(err) => {
                let reason = format!("无法连接协作服务（{}）", network_reason(&err));
                emit_state(
                    &sink,
                    &project_id,
                    "degraded",
                    cursor.load(Ordering::SeqCst),
                    &reason,
                );
                if !wait_before_retry(&stop, attempt, &sink, &project_id, &cursor).await {
                    return WatchOutcome::Stopped;
                }
                attempt += 1;
                continue;
            }
        };
        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED {
            // 下一轮强制刷新；界面上先退回降级（轮询接管），不要假装还在实时
            let reason = "登录令牌已过期，正在刷新重连".to_string();
            emit_state(
                &sink,
                &project_id,
                "degraded",
                cursor.load(Ordering::SeqCst),
                &reason,
            );
            if !wait_before_retry(&stop, attempt, &sink, &project_id, &cursor).await {
                return WatchOutcome::Stopped;
            }
            attempt += 1;
            continue;
        }
        if status == reqwest::StatusCode::FORBIDDEN || status == reqwest::StatusCode::NOT_FOUND {
            let reason = format!("订阅被拒（HTTP {}）：项目可能已删除或你已不在项目内", status.as_u16());
            emit_state(
                &sink,
                &project_id,
                "closed",
                cursor.load(Ordering::SeqCst),
                &reason,
            );
            return WatchOutcome::Closed(reason);
        }
        if !status.is_success() {
            let reason = format!("订阅失败（HTTP {}）", status.as_u16());
            emit_state(
                &sink,
                &project_id,
                "degraded",
                cursor.load(Ordering::SeqCst),
                &reason,
            );
            if !wait_before_retry(&stop, attempt, &sink, &project_id, &cursor).await {
                return WatchOutcome::Stopped;
            }
            attempt += 1;
            continue;
        }

        let delivered = AtomicBool::new(false);
        let outcome = pump_connection(
            response,
            &project_id,
            &cursor,
            &sink,
            &stop,
            &delivered,
        )
        .await;
        if stop.load(Ordering::SeqCst) {
            emit_state(
                &sink,
                &project_id,
                "stopped",
                cursor.load(Ordering::SeqCst),
                "",
            );
            return WatchOutcome::Stopped;
        }
        match outcome {
            Pump::Closed(reason) => {
                emit_state(
                    &sink,
                    &project_id,
                    "closed",
                    cursor.load(Ordering::SeqCst),
                    &reason,
                );
                return WatchOutcome::Closed(reason);
            }
            Pump::Stopped => {
                emit_state(
                    &sink,
                    &project_id,
                    "stopped",
                    cursor.load(Ordering::SeqCst),
                    "",
                );
                return WatchOutcome::Stopped;
            }
            Pump::Interrupted(reason) => {
                // 本轮真的收到过东西（ready 或事件）说明连接有效，退避归零；
                // 一上来就断则继续加退避，避免服务端挂了还高频重连
                attempt = if delivered.load(Ordering::SeqCst) {
                    0
                } else {
                    attempt + 1
                };
                emit_state(
                    &sink,
                    &project_id,
                    "degraded",
                    cursor.load(Ordering::SeqCst),
                    &reason,
                );
                if !wait_before_retry(&stop, attempt, &sink, &project_id, &cursor).await {
                    return WatchOutcome::Stopped;
                }
            }
        }
    }
}

/// 退避等待；返回 false 表示期间被要求停止，应立刻退出。
async fn sleep_backoff(stop: &Arc<AtomicBool>, attempt: usize) -> bool {
    let total = backoff_ms(attempt);
    let deadline = std::time::Instant::now() + Duration::from_millis(total);
    while std::time::Instant::now() < deadline {
        if stop.load(Ordering::SeqCst) {
            return false;
        }
        let remaining = deadline - std::time::Instant::now();
        tokio::time::sleep(remaining.min(Duration::from_millis(200))).await;
    }
    !stop.load(Ordering::SeqCst)
}

// ── 订阅登记表（每项目一条，切项目/关闭页面时停掉） ────────────

#[derive(Clone)]
pub struct WatchSlot {
    stop: Arc<AtomicBool>,
    cursor: Arc<AtomicI64>,
}

fn registry() -> &'static Mutex<HashMap<String, WatchSlot>> {
    static REGISTRY: OnceLock<Mutex<HashMap<String, WatchSlot>>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}

fn slot_entry(project_id: &str) -> Option<WatchSlot> {
    registry()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .get(project_id)
        .cloned()
}

fn drop_slot(project_id: &str) {
    if let Ok(mut table) = registry().lock() {
        table.remove(project_id);
    }
}

fn stop_slot(project_id: &str) -> bool {
    let existing = slot_entry(project_id);
    match existing {
        Some(slot) => {
            slot.stop.store(true, Ordering::SeqCst);
            drop_slot(project_id);
            true
        }
        None => false,
    }
}

/// 订阅自己结束了（被移出项目 / 主动停止）就把登记摘掉，`collab_watch_status`
/// 才不会报一条已经不存在的流。只摘自己这一代：期间若有新订阅顶上来，不能误删。
fn release_slot(project_id: &str, stop: &Arc<AtomicBool>) {
    if let Ok(mut table) = registry().lock() {
        let mine = table
            .get(project_id)
            .map(|slot| Arc::ptr_eq(&slot.stop, stop))
            .unwrap_or(false);
        if mine {
            table.remove(project_id);
        }
    }
}

// ── commands ──────────────────────────────────────────────────

/// 订阅项目事件流。`since_seq` 传页面当前快照的 seq：快照之后的变更都从流里来，
/// 不会出现「快照比流旧一格」的空窗。重复调用同一个项目会顶掉旧订阅。
#[tauri::command]
pub async fn collab_watch_start(
    p0: State<'_, Arc<P0Services>>,
    app: AppHandle,
    project_id: String,
    since_seq: i64,
) -> Result<Value, String> {
    let project_id = project_id.trim().to_string();
    if !is_safe_project_id(&project_id) {
        return Err("COLLAB_INVALID_INPUT: 项目 id 不合法".to_string());
    }
    let base = auth_service::auth_server_url(&p0.store);
    let base = normalize_base(&base)?;
    let client = stream_client_for_base(&base)?;

    // 旧订阅先停：同一项目挂两条流会把事件投两遍
    stop_slot(&project_id);

    let cursor = Arc::new(AtomicI64::new(since_seq.max(0)));
    let stop = Arc::new(AtomicBool::new(false));
    registry().lock().unwrap_or_else(|poisoned| poisoned.into_inner()).insert(
        project_id.clone(),
        WatchSlot {
            stop: Arc::clone(&stop),
            cursor: Arc::clone(&cursor),
        },
    );
    let spawn_project = project_id.clone();
    let registered_stop = Arc::clone(&stop);
    let spawn_cursor = Arc::clone(&cursor);
    let p0_store = Arc::clone(&p0.store);
    let sink = Sink::Tauri(app);
    tokio::spawn(async move {
        let outcome = run_watch(
            client,
            base,
            spawn_project.clone(),
            spawn_cursor,
            TokenProvider::Account(p0_store),
            sink,
            Arc::clone(&stop),
        )
        .await;
        release_slot(&spawn_project, &registered_stop);
        outcome
    });
    Ok(json!({
        "ok": true,
        "projectId": project_id,
        "cursor": cursor.load(Ordering::SeqCst),
        "state": "connecting",
    }))
}

/// 停止某项目的订阅（切换项目或离开页面时调用）
#[tauri::command]
pub async fn collab_watch_stop(project_id: String) -> Result<Value, String> {
    let stopped = stop_slot(project_id.trim());
    Ok(json!({ "ok": true, "stopped": stopped }))
}

/// 当前订阅情况（只读，不发起网络请求）：界面用它区分「实时」与「降级轮询」
#[tauri::command]
pub async fn collab_watch_status() -> Result<Value, String> {
    let table = registry().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let watches: Vec<Value> = table
        .iter()
        .map(|(project_id, slot)| {
            json!({
                "projectId": project_id,
                "cursor": slot.cursor.load(Ordering::SeqCst),
                "stopping": slot.stop.load(Ordering::SeqCst),
            })
        })
        .collect();
    Ok(json!({ "ok": true, "watches": watches }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    /// 服务端实际写法：`id: {seq}\nevent: message\ndata: {compact json}\n\n`
    fn message_frame(seq: i64, kind: &str, payload: Value) -> String {
        let data = json!({
            "seq": seq,
            "kind": kind,
            "actorUid": "usr-owner",
            "payload": payload,
            "createdAtMs": 1_730_000_000_000i64,
        });
        format!(
            "id: {seq}\nevent: message\ndata: {}\n\n",
            serde_json::to_string(&data).unwrap()
        )
    }

    fn ready_frame(cursor: i64) -> String {
        format!("event: ready\ndata: {{\"cursor\":{cursor}}}\n\n")
    }

    fn closed_frame(reason: &str) -> String {
        format!("event: closed\ndata: {{\"reason\":\"{reason}\"}}\n\n")
    }

    /// 假事件流服务：按顺序处理 N 次连接，记录每次的请求行，回固定 SSE 体后关闭。
    /// 与 `collab::tests::serve_sequence` 同源，只是把响应换成长连接体。
    fn serve_sse(bodies: Vec<String>) -> (String, Arc<Mutex<Vec<String>>>, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&seen);
        let handle = std::thread::spawn(move || {
            for body in bodies {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buf = [0u8; 4096];
                let mut raw = Vec::new();
                loop {
                    let n = stream.read(&mut buf).unwrap();
                    if n == 0 {
                        break;
                    }
                    raw.extend_from_slice(&buf[..n]);
                    if raw.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                let head = String::from_utf8_lossy(&raw)
                    .lines()
                    .next()
                    .unwrap_or("")
                    .to_string();
                captured.lock().unwrap().push(head);
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n{body}"
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.flush();
                let _ = stream.shutdown(std::net::Shutdown::Write);
            }
        });
        (format!("http://{addr}"), seen, handle)
    }

    /// 假服务只回状态码，用于验证 401/403 的处理
    fn serve_status(status: &'static str) -> (String, Arc<Mutex<Vec<String>>>, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&seen);
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let mut raw = Vec::new();
            loop {
                let n = stream.read(&mut buf).unwrap();
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buf[..n]);
                if raw.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            let head = String::from_utf8_lossy(&raw)
                .lines()
                .next()
                .unwrap_or("")
                .to_string();
            captured.lock().unwrap().push(head);
            let response = format!(
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}"
            );
            let _ = stream.write_all(response.as_bytes());
            let _ = stream.flush();
        });
        (format!("http://{addr}"), seen, handle)
    }

    struct Harness {
        cursor: Arc<AtomicI64>,
        sink: Sink,
        stop: Arc<AtomicBool>,
        log: Arc<Mutex<Vec<(String, Value)>>>,
    }

    impl Harness {
        fn new(since: i64) -> Self {
            let log = Arc::new(Mutex::new(Vec::new()));
            Self {
                cursor: Arc::new(AtomicI64::new(since)),
                sink: Sink::Collect(Arc::clone(&log)),
                stop: Arc::new(AtomicBool::new(false)),
                log,
            }
        }

        /// 投给界面的事件 seq（按投递顺序）
        fn delivered(&self) -> Vec<i64> {
            self.log
                .lock()
                .unwrap()
                .iter()
                .filter(|(channel, _)| channel == EVENT_COLLAB)
                .map(|(_, payload)| payload["seq"].as_i64().unwrap_or(-1))
                .collect()
        }

        /// 订阅状态序列（去掉重复的相邻项，看的是「换了几个状态」）
        fn states(&self) -> Vec<String> {
            let mut out: Vec<String> = Vec::new();
            for (channel, payload) in self.log.lock().unwrap().iter() {
                if channel != EVENT_COLLAB_STATE {
                    continue;
                }
                let state = payload["state"].as_str().unwrap_or("").to_string();
                if out.last().map(String::as_str) != Some(state.as_str()) {
                    out.push(state);
                }
            }
            out
        }
    }

    async fn watch(harness: &Harness, base: &str, project_id: &str) -> WatchOutcome {
        let client = stream_client_for_base(base).unwrap();
        let fut = run_watch(
            client,
            base.to_string(),
            project_id.to_string(),
            Arc::clone(&harness.cursor),
            TokenProvider::Static("secret-token".to_string()),
            harness.sink.clone(),
            Arc::clone(&harness.stop),
        );
        tokio::time::timeout(Duration::from_secs(20), fut)
            .await
            .expect("订阅未在 20 秒内结束，可能重连逻辑没有停在预期信号上")
    }

    // ── 解析与游标：纯函数 ────────────────────────────────────

    #[test]
    fn parses_ready_message_and_ping_frames() {
        assert_eq!(
            parse_frame(&ready_frame(42)),
            Frame::Named {
                name: "ready".to_string(),
                id: None,
                data: json!({"cursor": 42}),
            }
        );
        let block = "id: 7\nevent: message\ndata: {\"seq\":7,\"kind\":\"task.commented\"}";
        assert_eq!(
            parse_frame(block),
            Frame::Named {
                name: "message".to_string(),
                id: Some(7),
                data: json!({"seq": 7, "kind": "task.commented"}),
            }
        );
        // 心跳只是「连接还活着」，不是事件
        assert_eq!(parse_frame(": ping"), Frame::Comment);
        assert_eq!(parse_frame(""), Frame::Comment);
        // data 不是 JSON 时如实保留 raw，不静默丢
        assert_eq!(
            parse_frame("event: message\ndata: not-json"),
            Frame::Named {
                name: "message".to_string(),
                id: None,
                data: json!({"raw": "not-json"}),
            }
        );
    }

    #[test]
    fn take_block_only_yields_complete_blocks() {
        let mut buffer = String::from("event: ready\ndata: {\"cursor\":0}\n\nid: 5\nev");
        assert_eq!(
            take_block(&mut buffer),
            Some("event: ready\ndata: {\"cursor\":0}".to_string())
        );
        // 半截块留在缓冲区，等下一个字节
        assert_eq!(take_block(&mut buffer), None);
        assert_eq!(buffer, "id: 5\nev");
        buffer.push_str("ent: message\ndata: {}\n\n");
        assert_eq!(
            take_block(&mut buffer),
            Some("id: 5\nevent: message\ndata: {}".to_string())
        );
        assert!(buffer.is_empty());
    }

    #[test]
    fn cursor_is_monotone_not_contiguous() {
        let cursor = AtomicI64::new(0);
        // seq 全库自增，本项目里 5 之后直接跳到 7 是正常的（中间那条属于别的项目）
        assert!(advance_cursor(&cursor, 5));
        assert!(advance_cursor(&cursor, 7));
        // 迟到的旧事件与重复投递都不许把游标拖回去
        assert!(!advance_cursor(&cursor, 6));
        assert!(!advance_cursor(&cursor, 7));
        assert!(!advance_cursor(&cursor, 0));
        assert_eq!(cursor.load(Ordering::SeqCst), 7);
    }

    #[test]
    fn urls_carry_cursor_in_query_and_reject_bad_project_id() {
        let base = "http://127.0.0.1:18080";
        assert_eq!(
            stream_url(base, "prj-1", 7).unwrap(),
            "http://127.0.0.1:18080/api/v1/projects/prj-1/events/stream?since=7"
        );
        assert_eq!(
            events_url(base, "prj-1", 7).unwrap(),
            "http://127.0.0.1:18080/api/v1/projects/prj-1/events?since=7&limit=200"
        );
        // 令牌永远不进 URL：地址里只有 since
        for url in [
            stream_url(base, "prj-1", 7).unwrap(),
            events_url(base, "prj-1", 7).unwrap(),
        ] {
            assert!(!url.contains("secret-token"), "{url}");
        }
        for bad in ["", "  ", "prj/../../etc", "prj?x=1", &"x".repeat(65)] {
            assert!(
                stream_url(base, bad, 0).is_err(),
                "不合法项目 id 不该拼进地址：{bad:?}"
            );
            assert!(events_url(base, bad, 0).is_err());
        }
        // 远程 HTTP 在订阅侧同样被 normalize_base 挡住
        assert!(stream_url("http://collab.example.com", "prj-1", 0).is_err());
    }

    #[test]
    fn backoff_grows_then_caps() {
        assert_eq!(backoff_ms(0), 500);
        assert_eq!(backoff_ms(1), 1_000);
        assert_eq!(backoff_ms(4), 8_000);
        assert_eq!(backoff_ms(5), 15_000);
        assert_eq!(backoff_ms(99), 15_000);
    }

    // ── 订阅循环：对着假事件流跑真实 HTTP ─────────────────────

    #[tokio::test]
    async fn delivers_events_then_reconnects_from_cursor_without_duplicates() {
        // 第一连接：ready + 5 + 7（不连续），然后被切断（模拟断网/反代掐流）
        // 第二连接：服务端从 since=7 之后重放，且把 7 又发一次（重叠），随后 closed
        let (base, seen, handle) = serve_sse(vec![
            format!("{}{}", ready_frame(0), message_frame(5, "task.created", json!({"taskId": "tsk-1"}))
                ) + &message_frame(7, "task.commented", json!({"taskId": "tsk-1"})),
            format!(
                "{}{}{}",
                ready_frame(7),
                message_frame(7, "task.commented", json!({"taskId": "tsk-1"})),
                closed_frame("membership")
            ),
        ]);
        let harness = Harness::new(0);
        let outcome = watch(&harness, &base, "prj-1").await;
        handle.join().unwrap();

        assert_eq!(
            outcome,
            WatchOutcome::Closed("你已不在该项目内，停止订阅".to_string())
        );
        // 断线期间没有重复投递：重叠的 seq 7 只出一次
        assert_eq!(harness.delivered(), vec![5, 7]);
        assert_eq!(harness.cursor.load(Ordering::SeqCst), 7);
        // 重连用的是已交付水位，不是 0，也不是拍脑袋的 last-1
        let requests = seen.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert!(requests[0].contains("/events/stream?since=0"), "{}", requests[0]);
        assert!(requests[1].contains("/events/stream?since=7"), "{}", requests[1]);
        // 凭据只在请求头
        assert!(!requests[0].contains("secret-token"));
        drop(requests);
        let states = harness.states();
        assert_eq!(
            states,
            vec![
                "connecting".to_string(),
                "streaming".to_string(),
                "degraded".to_string(),
                // 每次发起连接都如实报 connecting，界面才知道现在在重试
                "connecting".to_string(),
                "streaming".to_string(),
                "closed".to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn membership_closed_is_the_only_terminal_signal_and_stops_retry() {
        let (base, _seen, handle) = serve_sse(vec![format!(
            "{}{}",
            ready_frame(0),
            closed_frame("membership")
        )]);
        let harness = Harness::new(0);
        let outcome = watch(&harness, &base, "prj-1").await;
        handle.join().unwrap();
        assert_eq!(
            outcome,
            WatchOutcome::Closed("你已不在该项目内，停止订阅".to_string())
        );
        assert!(harness.delivered().is_empty());
        assert_eq!(harness.states().last().map(String::as_str), Some("closed"));
    }

    #[tokio::test]
    async fn unauthorized_keeps_the_subscription_and_retries() {
        // access token 在长订阅里过期是常态：401 只能降级重连，不能把订阅拆掉，
        // 否则界面会静默失去实时（真要移出项目服务端会发 closed）
        let (base, seen, handle) = serve_status("401 Unauthorized");
        let harness = Harness::new(3);
        let client = stream_client_for_base(&base).unwrap();
        let outcome = tokio::time::timeout(
            Duration::from_secs(12),
            run_watch(
                client,
                base.clone(),
                "prj-1".to_string(),
                Arc::clone(&harness.cursor),
                TokenProvider::Static("secret-token".to_string()),
                harness.sink.clone(),
                {
                    // 第二次连接就要求停止，验证「401 之后仍在尝试」而不是直接返回
                    let stop = Arc::clone(&harness.stop);
                    let seen_for_thread = Arc::clone(&seen);
                    std::thread::spawn(move || {
                        while seen_for_thread.lock().unwrap().len() < 1 {
                            std::thread::sleep(Duration::from_millis(20));
                        }
                        stop.store(true, Ordering::SeqCst);
                    });
                    Arc::clone(&harness.stop)
                },
            ),
        )
        .await
        .expect("401 后订阅卡住未返回");
        assert_eq!(outcome, WatchOutcome::Stopped);
        assert!(seen.lock().unwrap().len() >= 1);
        assert!(
            harness.states().contains(&"degraded".to_string()),
            "401 应如实降级给界面：{:?}",
            harness.states()
        );
        let _ = handle;
    }

    #[tokio::test]
    async fn forbidden_or_missing_project_ends_the_subscription() {
        let (base, seen, handle) = serve_status("403 Forbidden");
        let harness = Harness::new(0);
        let outcome = watch(&harness, &base, "prj-1").await;
        handle.join().unwrap();
        assert!(matches!(outcome, WatchOutcome::Closed(_)));
        assert_eq!(harness.states().last().map(String::as_str), Some("closed"));
        // 判定为终止就不该再重试打服务端
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn stop_flag_exits_even_mid_backoff() {
        let (base, _seen, handle) = serve_sse(vec![ready_frame(0)]);
        let harness = Harness::new(0);
        let client = stream_client_for_base(&base).unwrap();
        let fut = run_watch(
            client,
            base.clone(),
            "prj-1".to_string(),
            Arc::clone(&harness.cursor),
            TokenProvider::Static("secret-token".to_string()),
            harness.sink.clone(),
            Arc::clone(&harness.stop),
        );
        let stop = Arc::clone(&harness.stop);
        let joined = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(300)).await;
            stop.store(true, Ordering::SeqCst);
        });
        let outcome = tokio::time::timeout(Duration::from_secs(15), fut)
            .await
            .expect("点了停止还退不掉，退出登录/离开页面会挂住");
        joined.await.unwrap();
        handle.join().unwrap();
        assert_eq!(outcome, WatchOutcome::Stopped);
        assert_eq!(harness.states().last().map(String::as_str), Some("stopped"));
    }

    #[test]
    fn release_slot_only_removes_its_own_generation() {
        // 订阅退出时必须从登记表摘掉自己，否则 collab_watch_status 会一直报一条
        // 已经不存在的流；但被新订阅顶替之后，旧的那代不许把新的摘掉。
        const PROJECT: &str = "prj-release-test";
        let first = Arc::new(AtomicBool::new(false));
        let second = Arc::new(AtomicBool::new(false));
        registry().lock().unwrap().insert(
            PROJECT.to_string(),
            WatchSlot {
                stop: Arc::clone(&first),
                cursor: Arc::new(AtomicI64::new(1)),
            },
        );
        registry().lock().unwrap().insert(
            PROJECT.to_string(),
            WatchSlot {
                stop: Arc::clone(&second),
                cursor: Arc::new(AtomicI64::new(2)),
            },
        );
        release_slot(PROJECT, &first);
        let still_there = slot_entry(PROJECT).expect("新一代订阅不该被旧订阅摘掉");
        assert_eq!(still_there.cursor.load(Ordering::SeqCst), 2);
        release_slot(PROJECT, &second);
        assert!(slot_entry(PROJECT).is_none());
    }

    // ── 双人真实验收（缺陷 D-06.4）·默认跳过 ────────────────────
    //
    // 上面的用例对着自造的服务端字节流跑，语义是从 `tests/test_server.py` 抄来的；
    // 这条不重造前提，直接对着**真实运行的账号服务**用生产同一条 `run_watch` 量：
    //   ① 另一个人写入 → 本端 1 秒内看到
    //   ② 本端断线（模拟断网）期间两人各写一条 → 按已交付游标重连后一条不丢、不重
    //   ③ 被移出项目 → 真实服务端只发 closed(membership)，订阅就此终止不再重连
    //
    // 跑法：`python ../scripts/collab-live-acceptance.py`（它起服务、备好两个账号与
    // 项目任务，再用环境变量把 base / 项目 / 任务 / 两个 access token 交进来）。
    // 凭据只出现在环境变量与 Authorization 头里，这里不打印、服务侧也不落盘。

    fn live_env(key: &str) -> String {
        std::env::var(key)
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| {
                panic!("缺少环境变量 {key}：请用 scripts/collab-live-acceptance.py 准备真实环境后再跑")
            })
    }

    async fn wait_until(what: &str, timeout: Duration, mut done: impl FnMut() -> bool) {
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            if done() {
                return;
            }
            if tokio::time::Instant::now() >= deadline {
                panic!("{what}：{}ms 内没有等到", timeout.as_millis());
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    /// 以真实令牌起一条订阅，返回收集器与后台任务
    async fn live_watch(
        base: &str,
        project: &str,
        cursor: i64,
        token: &str,
    ) -> (Harness, tokio::task::JoinHandle<WatchOutcome>) {
        let harness = Harness::new(cursor);
        let client = stream_client_for_base(base).expect("SSE 客户端建不起来");
        let join = tokio::spawn(run_watch(
            client,
            base.to_string(),
            project.to_string(),
            Arc::clone(&harness.cursor),
            TokenProvider::Static(token.to_string()),
            harness.sink.clone(),
            Arc::clone(&harness.stop),
        ));
        wait_until("订阅进入 streaming", Duration::from_secs(8), || {
            harness.states().iter().any(|s| s == "streaming")
        })
        .await;
        (harness, join)
    }

    /// 评论写入（返回 HTTP 状态码，201 才算成功）
    async fn post_comment(base: &str, project: &str, task: &str, token: &str, body_text: &str) -> u16 {
        use crate::collab::client_for_base;
        let client = client_for_base(base).expect("写入客户端建不起来");
        let url = format!("{base}/api/v1/projects/{project}/tasks/{task}/comments");
        let resp = client
            .post(&url)
            .bearer_auth(token)
            .json(&json!({ "body": body_text }))
            .send()
            .await
            .expect("评论请求没发出去：账号服务还在跑吗？");
        resp.status().as_u16()
    }

    /// 事件流里 `seq > after` 的那几条（按投递顺序）
    fn seqs_above(harness: &Harness, after: i64) -> Vec<i64> {
        harness
            .delivered()
            .into_iter()
            .filter(|seq| *seq > after)
            .collect()
    }

    /// 事件里的 actorUid（用来证明两条补齐的事件确实出自两个人）
    fn actors_of(harness: &Harness, seqs: &[i64]) -> Vec<String> {
        harness
            .log
            .lock()
            .unwrap()
            .iter()
            .filter(|(channel, payload)| {
                channel == EVENT_COLLAB
                    && seqs.contains(&payload["seq"].as_i64().unwrap_or(-1))
            })
            .map(|(_, payload)| payload["actorUid"].as_str().unwrap_or("").to_string())
            .collect()
    }

    #[tokio::test]
    #[ignore = "需要真实运行的账号服务与两个账号：python scripts/collab-live-acceptance.py"]
    async fn live_two_person_sync_reconnect_and_membership() {
        use crate::collab::client_for_base;

        let raw_base = live_env("TT_LIVE_COLLAB_BASE");
        let base = normalize_base(&raw_base).expect("TT_LIVE_COLLAB_BASE 不合法");
        let project = live_env("TT_LIVE_COLLAB_PROJECT");
        let task = live_env("TT_LIVE_COLLAB_TASK");
        let member_token = live_env("TT_LIVE_COLLAB_MEMBER_TOKEN");
        let owner_token = live_env("TT_LIVE_COLLAB_OWNER_TOKEN");

        // 快照水位：订阅从「已经看到的这条」起步，与页面完全一致
        let read_client = client_for_base(&base).expect("快照客户端建不起来");
        let snap: Value = read_client
            .get(format!("{base}/api/v1/projects/{project}"))
            .bearer_auth(&member_token)
            .send()
            .await
            .expect("项目快照请求没发出去")
            .json()
            .await
            .expect("项目快照不是合法 JSON");
        let start_seq = snap["seq"].as_i64().expect("项目快照没带事件游标 seq");

        // ① 负责人写入 → 成员端 1 秒内可见
        let (member, join) = live_watch(&base, &project, start_seq, &member_token).await;
        assert!(
            member.delivered().is_empty(),
            "从快照游标起步却还是收到了历史事件：会把旧状态当新变更投回界面"
        );
        let started = std::time::Instant::now();
        assert_eq!(
            post_comment(&base, &project, &task, &owner_token, "跨机器实时评论").await,
            201,
            "负责人的评论没写进去"
        );
        let mut seen: Vec<i64> = Vec::new();
        wait_until("成员端收到新事件", Duration::from_secs(5), || {
            seen = seqs_above(&member, start_seq);
            !seen.is_empty()
        })
        .await;
        let latency = started.elapsed();
        println!(
            "[D-06 双人验收] 负责人写入 → 成员端看到：{}ms（seq {}）",
            latency.as_millis(),
            seen[0]
        );
        assert!(
            latency <= Duration::from_millis(1000),
            "实测 {}ms，超过 1 秒验收线",
            latency.as_millis()
        );
        assert_eq!(member.cursor.load(Ordering::SeqCst), seen[0]);
        let delivered = seen[0];

        // ② 成员端断线（停掉订阅就是断线的等价：没有任何连接挂着，也不会有补投）
        member.stop.store(true, Ordering::SeqCst);
        let outcome = tokio::time::timeout(Duration::from_secs(20), join)
            .await
            .expect("订阅没在 20 秒内退出")
            .expect("订阅任务 panic");
        assert_eq!(outcome, WatchOutcome::Stopped, "主动停止不该被当成服务端关闭");
        assert_eq!(
            seqs_above(&member, delivered),
            Vec::<i64>::new(),
            "订阅已停止却还有新事件投给界面"
        );

        // 断线期间两人各写一条
        assert_eq!(
            post_comment(&base, &project, &task, &owner_token, "断线期间负责人写的").await,
            201
        );
        assert_eq!(
            post_comment(&base, &project, &task, &member_token, "断线期间成员自己写的").await,
            201
        );

        // ③ 从已交付游标重连：两条都要补回来，顺序递增且不重复
        let (again, join2) = live_watch(&base, &project, delivered, &member_token).await;
        let mut caught: Vec<i64> = Vec::new();
        wait_until("重连后补齐断线期间的事件", Duration::from_secs(8), || {
            caught = seqs_above(&again, delivered);
            caught.len() >= 2
        })
        .await;
        println!(
            "[D-06 双人验收] 断线期间 2 条写入，重连后补齐 seq {caught:?}（游标从 {delivered} 续起）"
        );
        assert_eq!(caught.len(), 2, "补齐的事件数与断线期间的写入不一致：{caught:?}");
        assert_eq!(
            caught,
            {
                let mut sorted = caught.clone();
                sorted.sort_unstable();
                sorted.dedup();
                sorted
            },
            "重连投递出现了重复或乱序"
        );
        let mut uid_of = actors_of(&again, &caught);
        uid_of.sort();
        uid_of.dedup();
        assert_eq!(uid_of.len(), 2, "两条补齐的事件应来自两个不同的人：{uid_of:?}");

        // ④ 被移出项目：真实服务端只发 closed(membership)，订阅就此终止，不再重连
        let members: Value = read_client
            .get(format!("{base}/api/v1/projects/{project}"))
            .bearer_auth(&owner_token)
            .send()
            .await
            .expect("成员快照请求没发出去")
            .json()
            .await
            .expect("成员快照不是合法 JSON");
        let member_uids: Vec<String> = members["members"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter(|r| r["role"].as_str() == Some("member"))
                    .filter_map(|r| r["uid"].as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        assert_eq!(member_uids.len(), 1, "演练项目里应当只有一个 member 角色的账号");
        assert_eq!(
            read_client
                .post(format!(
                    "{base}/api/v1/projects/{project}/members/{}/remove",
                    member_uids[0]
                ))
                .bearer_auth(&owner_token)
                .json(&json!({}))
                .send()
                .await
                .expect("移除成员请求没发出去")
                .status()
                .as_u16(),
            200,
            "负责人移除成员失败，无法验证终止信号"
        );
        let closed = tokio::time::timeout(Duration::from_secs(20), join2)
            .await
            .expect("被移出后订阅没在 20 秒内终止（说明 closed 之后还在重连）")
            .expect("订阅任务 panic");
        println!("[D-06 双人验收] 被移出项目 → 订阅终止：{closed:?}");
        assert_eq!(
            closed,
            WatchOutcome::Closed("你已不在该项目内，停止订阅".to_string()),
            "成员被移除后应当收到并识别 closed(membership)，就此停止订阅"
        );
    }
}
