//! 通天办公 - Tauri 后端入口
//!
//! 阶段二：AgentBridge 集成
//! - agent_status / agent_start_workspace / agent_stop_workspace
//! - chat_submit: 提交任务（两阶段协议：stage=plan 生成计划待审阅）
//! - system_info: 示例 command
//!
//! 三层架构演进 P0（附加式扩展，现有签名不变）：
//! - chat_cancel / task_state / quota_status / lease_info / lease_takeover / event_replay
//!
//! 核心闭环补齐（占位清单第一批）：
//! - chat_review / chat_control / chat_history（Workbench 审阅/控制/历史真实化）
//! - model_list/add/update/delete/test/set_default（ModelHub 五件套）
//! - file_list / file_read / file_preview / file_stage_attachment（文件能力 + 附件上传）
//! - settings_get / settings_set（设置持久化）
//!
//! 账号与登录态（网页验证登录 · Loopback 方案，TT_AUTH_MOCK 开关）：
//! - auth_status / auth_login / auth_logout

mod auth;
mod bridge;
mod file;
mod model;
mod store;
mod task;

use std::sync::Arc;

use bridge::agent::{self, BridgeState, P0Context};
use bridge::event_store::{spawn_ttl_cleanup_loop, EventStore};
use serde_json::{json, Value};
use store::Store;
use task::lease::{LeaseEvent, LeaseManager};
use task::quota::{QuotaGate, DEFAULT_DAILY_QUOTA_TOKENS};
use task::registry::TaskRegistry;
use task::status;
use tauri::{AppHandle, Emitter, Manager, State};

/// P0 + 核心闭环服务集合（任务注册表/租约/配额/事件存储/持久化）
pub struct P0Services {
    pub registry: Arc<TaskRegistry>,
    pub leases: Arc<LeaseManager>,
    pub quota: Arc<QuotaGate>,
    pub events: Arc<EventStore>,
    pub store: Arc<Store>,
}

fn now_ms_u64() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[tauri::command]
fn system_info() -> Value {
    json!({
        "name": "tongtian-tauri",
        "version": env!("CARGO_PKG_VERSION"),
        "platform": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
    })
}

/// 查询桥接状态（Python 探测结果 + 各 workspace 实例）
#[tauri::command]
async fn agent_status(state: State<'_, BridgeState>) -> Result<Value, String> {
    Ok(agent::bridge_status(&state.inner()).await)
}

/// 启动指定 workspace 的 Agent 实例
#[tauri::command]
async fn agent_start_workspace(
    app: AppHandle,
    state: State<'_, BridgeState>,
    workspace_id: String,
) -> Result<Value, String> {
    let port = agent::start_workspace(&app, &state.inner(), &workspace_id).await?;
    Ok(json!({ "workspaceId": workspace_id, "port": port }))
}

/// 停止指定 workspace 的 Agent 实例
#[tauri::command]
async fn agent_stop_workspace(
    app: AppHandle,
    state: State<'_, BridgeState>,
    workspace_id: String,
) -> Result<Value, String> {
    agent::stop_workspace(&state.inner(), &app, &workspace_id).await?;
    Ok(json!({ "success": true }))
}

/// 推送任务状态变更事件（status_change，含 wait_reason/成因码）
fn emit_status_change(app: &AppHandle, record: &task::registry::TaskRecord) {
    let _ = app.emit(
        agent::EVENT_CHAT,
        json!({
            "type": "status_change",
            "taskId": record.task_id,
            "status": record.status,
            "waitReason": record.wait_reason,
            "statusReasonCode": record.status_reason_code,
            "queuePosition": record.queue_position,
        }),
    );
}

/// 注册表 → tasks 表同步（title 为空时保留 DB 既有值）
fn sync_task_to_db(services: &P0Services, task_id: &str, title: &str) {
    if let Some(rec) = services.registry.get(task_id) {
        let row = store::tasks::TaskRow {
            task_id: rec.task_id.clone(),
            workspace_id: rec.workspace_id.clone(),
            title: title.to_string(),
            status: rec.status.clone(),
            wait_reason: rec.wait_reason.clone(),
            retry_count: rec.retry_count as i64,
            status_reason_code: rec.status_reason_code.clone(),
            queue_position: rec.queue_position as i64,
            created_at_ms: rec.created_at_ms as i64,
            updated_at_ms: rec.updated_at_ms as i64,
        };
        if let Err(e) = store::tasks::upsert(&services.store, &row) {
            eprintln!("[store] task persist failed: {e}");
        }
    }
}

/// 提交任务：注册表受理（queued→planning）→ 配额闸门 → 租约 → 两阶段计划生成。
/// 计划流结束后任务进入 pending_review 待审阅（approve 后经 chat_review 执行）。
#[tauri::command]
async fn chat_submit(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    workspace_id: Option<String>,
    goal: String,
    attachments: Option<Vec<Value>>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    let task_id = format!("task-{}", now_ms_u64());
    let title: String = goal.chars().take(30).collect();

    // 配额闸门：100% 档阻断新提交（错误码 QUOTA_EXCEEDED，ADJ-R5-4）
    if let Err(code) = p0.quota.check_submit() {
        return Err(format!("{code}: 配额已用尽，新任务被阻断"));
    }

    // 任务注册表受理：转移 #1 (创建)→queued，并推送状态事件
    let record = p0.registry.create(&task_id, &ws);
    emit_status_change(&app, &record);
    sync_task_to_db(&p0, &task_id, &title);

    // 本地 local-first 退化：queued 入队即出队（<100ms），立即签发租约转 planning
    match p0.leases.acquire(&task_id, now_ms_u64()) {
        Ok(lease) => {
            p0.registry.bind_lease(&task_id, &lease.lease_id);
            if let Some(r) = p0.registry.update_status(&task_id, status::PLANNING, None) {
                emit_status_change(&app, &r);
            }
        }
        Err(takeover) => {
            // 禁止抢占：其他端持有活跃租约（云端语义，本地单端不会触发）
            p0.registry.update_status(&task_id, status::CANCELLED, Some("TASK_ALREADY_RUNNING"));
            sync_task_to_db(&p0, &task_id, "");
            return Err(format!("TASK_ALREADY_RUNNING: {}", takeover.message));
        }
    }

    // 确保实例已启动（首次自动拉起）
    agent::start_workspace(&app, &state.inner(), &ws).await?;

    // 后台执行计划生成流（两阶段 stage=plan），事件经 chat-event 推送前端
    let bridge = state.inner().clone();
    let app2 = app.clone();
    let ws2 = ws.clone();
    let tid = task_id.clone();
    let services = p0.inner().clone();
    let attachments = attachments;
    tauri::async_runtime::spawn(async move {
        let ctx = P0Context {
            events: Some(Arc::clone(&services.events)),
            quota: Some(Arc::clone(&services.quota)),
            registry: Some(Arc::clone(&services.registry)),
        };
        let result = agent::send_message_with_attachments(
            &app2,
            &bridge,
            &ws2,
            &tid,
            &goal,
            attachments.unwrap_or_default(),
            ctx,
        )
        .await;
        let cancel_requested = services
            .registry
            .get(&tid)
            .map(|r| r.cancel_requested)
            .unwrap_or(false);
        match result {
            Ok(()) => {
                // 两阶段协议：计划流结束 → pending_review 待审阅（非任务终态）
                let next = if cancel_requested {
                    status::CANCELLED
                } else {
                    status::PENDING_REVIEW
                };
                if let Some(r) = services.registry.update_status(&tid, next, None) {
                    emit_status_change(&app2, &r);
                }
            }
            Err(err) => {
                eprintln!("[bridge] chat failed: {err}");
                if let Some(r) = services
                    .registry
                    .update_status(&tid, status::FAILED, Some("BRIDGE_ERROR"))
                {
                    emit_status_change(&app2, &r);
                }
                let _ = app2.emit(
                    agent::EVENT_CHAT,
                    json!({ "type": "error", "code": "BRIDGE_ERROR", "message": err }),
                );
                let _ = app2.emit(agent::EVENT_CHAT, json!({ "type": "done", "taskId": tid }));
            }
        }
        sync_task_to_db(&services, &tid, "");
        // 计划阶段结束释放租约；approve 执行阶段重新签发
        services.leases.release(&tid);
    });

    Ok(json!({
        "taskId": task_id,
        "workspaceId": ws,
        "status": status::PLANNING,
        "quota": p0.quota.snapshot(),
    }))
}

/// 审阅计划（转移 #9/#10/#11）：approve→executing / reject→done(REJECTED) / modify→planning
#[tauri::command]
async fn chat_review(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
    action: String,
    patch: Option<Value>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let record = p0
        .registry
        .get(&task_id)
        .ok_or_else(|| format!("TASK_NOT_FOUND: {task_id}"))?;
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| record.workspace_id.clone());
    let patch = patch.unwrap_or(Value::Array(vec![]));

    match action.as_str() {
        "approve" => {
            if record.status != status::PENDING_REVIEW {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 pending_review 可批准（当前 {}）",
                    record.status
                ));
            }
            // 转移 #9：pending_review→executing；重新签发执行租约
            let lease = p0
                .leases
                .acquire(&task_id, now_ms_u64())
                .map_err(|t| format!("TASK_ALREADY_RUNNING: {}", t.message))?;
            p0.registry.bind_lease(&task_id, &lease.lease_id);
            if let Some(r) = p0.registry.update_status(&task_id, status::EXECUTING, None) {
                emit_status_change(&app, &r);
            }
            sync_task_to_db(&p0, &task_id, "");

            // 后台消费执行流（step_*/deliver/done 事件）
            let bridge = state.inner().clone();
            let app2 = app.clone();
            let ws2 = ws.clone();
            let tid = task_id.clone();
            let services = p0.inner().clone();
            tauri::async_runtime::spawn(async move {
                let ctx = P0Context {
                    events: Some(Arc::clone(&services.events)),
                    quota: Some(Arc::clone(&services.quota)),
                    registry: Some(Arc::clone(&services.registry)),
                };
                let result = agent::review_task(
                    &app2, &bridge, &ws2, &tid, "approve", patch, ctx,
                )
                .await;
                let cancel_requested = services
                    .registry
                    .get(&tid)
                    .map(|r| r.cancel_requested)
                    .unwrap_or(false);
                match result {
                    Ok(()) => {
                        let next = if cancel_requested {
                            status::CANCELLED
                        } else {
                            status::DONE
                        };
                        if let Some(r) = services.registry.update_status(&tid, next, None) {
                            emit_status_change(&app2, &r);
                        }
                    }
                    Err(err) => {
                        eprintln!("[bridge] review approve failed: {err}");
                        if let Some(r) = services
                            .registry
                            .update_status(&tid, status::FAILED, Some("BRIDGE_ERROR"))
                        {
                            emit_status_change(&app2, &r);
                        }
                        let _ = app2.emit(
                            agent::EVENT_CHAT,
                            json!({ "type": "error", "code": "BRIDGE_ERROR", "message": err }),
                        );
                        let _ = app2
                            .emit(agent::EVENT_CHAT, json!({ "type": "done", "taskId": tid }));
                    }
                }
                sync_task_to_db(&services, &tid, "");
                services.leases.release(&tid);
            });
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::EXECUTING }))
        }
        "reject" => {
            if record.status != status::PENDING_REVIEW {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 pending_review 可拒绝（当前 {}）",
                    record.status
                ));
            }
            // 转移 #10：pending_review→done(REJECTED)；短流消费后落终态
            let bridge = state.inner().clone();
            let app2 = app.clone();
            let ws2 = ws.clone();
            let tid = task_id.clone();
            let services = p0.inner().clone();
            tauri::async_runtime::spawn(async move {
                let ctx = P0Context::default();
                let _ = agent::review_task(&app2, &bridge, &ws2, &tid, "reject", patch, ctx)
                    .await
                    .map_err(|e| eprintln!("[bridge] review reject failed: {e}"));
                if let Some(r) = services
                    .registry
                    .update_status(&tid, status::DONE, Some("REJECTED"))
                {
                    emit_status_change(&app2, &r);
                }
                sync_task_to_db(&services, &tid, "");
                services.leases.release(&tid);
            });
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::DONE }))
        }
        "modify" => {
            if record.status != status::PENDING_REVIEW {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 pending_review 可修改（当前 {}）",
                    record.status
                ));
            }
            // 转移 #11：携带 patch 重新生成 plan（状态保持 pending_review）
            let bridge = state.inner().clone();
            let app2 = app.clone();
            let ws2 = ws.clone();
            let tid = task_id.clone();
            let services = p0.inner().clone();
            tauri::async_runtime::spawn(async move {
                let ctx = P0Context::default();
                let result =
                    agent::review_task(&app2, &bridge, &ws2, &tid, "modify", patch, ctx).await;
                if let Err(e) = result {
                    eprintln!("[bridge] review modify failed: {e}");
                    let _ = app2.emit(
                        agent::EVENT_CHAT,
                        json!({ "type": "error", "code": "BRIDGE_ERROR", "message": e }),
                    );
                }
                sync_task_to_db(&services, &tid, "");
            });
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::PENDING_REVIEW }))
        }
        _ => Err(format!("VALIDATION_ERROR: 未知审阅动作 {action}")),
    }
}

/// 任务控制（转移 #14/#21/#22）：pause(executing→paused) / resume(paused→executing) /
/// rollback_to(paused→rolled_back)
#[tauri::command]
async fn chat_control(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
    action: String,
    step_id: Option<String>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let record = p0
        .registry
        .get(&task_id)
        .ok_or_else(|| format!("TASK_NOT_FOUND: {task_id}"))?;
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| record.workspace_id.clone());

    match action.as_str() {
        "pause" => {
            if record.status != status::EXECUTING {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 executing 可暂停（当前 {}）",
                    record.status
                ));
            }
            let resp = agent::control_task(&state.inner(), &ws, &task_id, "pause", None).await?;
            if resp.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                return Err(format!("BRIDGE_CONTROL_FAILED: {resp}"));
            }
            if let Some(r) = p0.registry.update_status(&task_id, status::PAUSED, None) {
                emit_status_change(&app, &r);
            }
            sync_task_to_db(&p0, &task_id, "");
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::PAUSED }))
        }
        "resume" => {
            if record.status != status::PAUSED {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 paused 可继续（当前 {}）",
                    record.status
                ));
            }
            let resp = agent::control_task(&state.inner(), &ws, &task_id, "resume", None).await?;
            if resp.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                return Err(format!("BRIDGE_CONTROL_FAILED: {resp}"));
            }
            if let Some(r) = p0.registry.update_status(&task_id, status::EXECUTING, None) {
                emit_status_change(&app, &r);
            }
            sync_task_to_db(&p0, &task_id, "");
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::EXECUTING }))
        }
        "rollback_to" => {
            if record.status != status::PAUSED {
                return Err(format!(
                    "VALIDATION_ERROR: 仅 paused 可回滚（当前 {}）",
                    record.status
                ));
            }
            let sid = step_id.clone().unwrap_or_default();
            if sid.is_empty() {
                return Err("VALIDATION_ERROR: rollback_to 需指定 stepId".into());
            }
            let resp = agent::control_task(
                &state.inner(),
                &ws,
                &task_id,
                "rollback_to",
                Some(&sid),
            )
            .await?;
            if resp.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                return Err(format!("BRIDGE_CONTROL_FAILED: {resp}"));
            }
            // 转移 #22：paused→rolled_back（文件级恢复随备份模块下批落地）
            if let Some(r) = p0.registry.update_status(
                &task_id,
                status::ROLLED_BACK,
                Some(&format!("ROLLBACK_TO:{sid}")),
            ) {
                emit_status_change(&app, &r);
            }
            sync_task_to_db(&p0, &task_id, "");
            p0.leases.release(&task_id);
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::ROLLED_BACK }))
        }
        _ => Err(format!("VALIDATION_ERROR: 未知控制动作 {action}")),
    }
}

/// 取消任务（转移 #3/#8/#19/#23/#28：非终态 → cancelled）
#[tauri::command]
async fn chat_cancel(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let record = p0
        .registry
        .get(&task_id)
        .ok_or_else(|| format!("TASK_NOT_FOUND: {task_id}"))?;
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| record.workspace_id.clone());

    // 标记取消请求（幂等），并通知引擎终止执行
    p0.registry.request_cancel(&task_id);
    let engine_resp = agent::cancel_task(&state.inner(), &ws, &task_id).await;

    let cancellable = !matches!(
        record.status.as_str(),
        "done" | "failed" | "cancelled" | "archived"
    );
    if cancellable {
        if let Some(r) = p0
            .registry
            .update_status(&task_id, status::CANCELLED, Some("USER_CANCELLED"))
        {
            emit_status_change(&app, &r);
        }
        sync_task_to_db(&p0, &task_id, "");
        p0.leases.release(&task_id);
    }

    Ok(json!({
        "taskId": task_id,
        "cancelled": cancellable,
        "engine": engine_resp.unwrap_or(Value::Null),
    }))
}

/// 历史任务（tasks 表，按更新时间倒序，含 14 态 meta）
#[tauri::command]
async fn chat_history(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = store::tasks::list_recent(&p0.store, limit)?;
    let summaries: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "id": r.task_id,
                "title": r.title,
                "status": r.status,
                "workspaceId": r.workspace_id,
                "createdAt": r.created_at_ms,
                "updatedAt": r.updated_at_ms,
                "meta": {
                    "waitReason": r.wait_reason,
                    "queuePosition": r.queue_position,
                    "retryCount": r.retry_count,
                    "statusReasonCode": r.status_reason_code,
                },
            })
        })
        .collect();
    Ok(Value::Array(summaries))
}

/// 查询任务 14 态状态（含 wait_reason/重试计数/租约/成因码）
#[tauri::command]
async fn task_state(p0: State<'_, Arc<P0Services>>, task_id: String) -> Result<Value, String> {
    p0.registry
        .get(&task_id)
        .map(|r| serde_json::to_value(&r).unwrap_or(Value::Null))
        .ok_or_else(|| format!("TASK_NOT_FOUND: {task_id}"))
}

/// 配额快照（四档预警 70/85/95/100%）
#[tauri::command]
async fn quota_status(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    Ok(serde_json::to_value(p0.quota.snapshot()).unwrap_or(Value::Null))
}

/// 租约视图（全部活跃租约）
#[tauri::command]
async fn lease_info(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    Ok(json!({
        "holder": p0.leases.holder_id(),
        "leases": p0.leases.list(),
    }))
}

/// 接管请求（禁止抢占：活跃租约需对端确认，ADJ-R2-4）
#[tauri::command]
async fn lease_takeover(
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
) -> Result<Value, String> {
    Ok(serde_json::to_value(p0.leases.takeover(&task_id, now_ms_u64())).unwrap_or(Value::Null))
}

/// 事件重放（24h 持久化 + ≥10min 重放窗口，seq > from_seq）
#[tauri::command]
async fn event_replay(
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
    from_seq: Option<u64>,
) -> Result<Value, String> {
    let events = p0.events.replay(&task_id, from_seq.unwrap_or(0));
    Ok(json!({ "taskId": task_id, "events": events }))
}

// ── Model Hub 五件套（占位清单第一批 P0） ──────────────────────

#[tauri::command]
async fn model_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let list = model::service::list(&p0.store)?;
    Ok(serde_json::to_value(list).unwrap_or(Value::Array(vec![])))
}

#[tauri::command]
async fn model_add(
    p0: State<'_, Arc<P0Services>>,
    req: model::service::ModelAddReq,
) -> Result<Value, String> {
    let cfg = model::service::add(&p0.store, req)?;
    Ok(serde_json::to_value(cfg).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_update(
    p0: State<'_, Arc<P0Services>>,
    req: model::service::ModelUpdateReq,
) -> Result<Value, String> {
    let cfg = model::service::update(&p0.store, req)?;
    Ok(serde_json::to_value(cfg).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_delete(p0: State<'_, Arc<P0Services>>, id: String) -> Result<Value, String> {
    model::service::delete(&p0.store, &id)?;
    Ok(json!({ "ok": true }))
}

#[tauri::command]
async fn model_test(
    p0: State<'_, Arc<P0Services>>,
    mut req: model::service::ModelTestReq,
) -> Result<Value, String> {
    // 列表卡片测试：传已存模型 id 时补齐配置与 keyring 密钥
    if let Some(id) = &req.id {
        if let Some(cfg) = model::service::get(&p0.store, id)? {
            if req.base_url.is_empty() {
                req.base_url = cfg.base_url.clone();
            }
            if req.model_name.is_empty() {
                req.model_name = cfg.model_name.clone();
            }
            if req.api_key.is_empty() {
                req.api_key = p0.store.vault().get_secret(id).unwrap_or_default();
            }
        }
    }
    let resp = model::service::test(req).await;
    Ok(serde_json::to_value(resp).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_set_default(p0: State<'_, Arc<P0Services>>, id: String) -> Result<Value, String> {
    model::service::set_default(&p0.store, &id)?;
    Ok(json!({ "ok": true }))
}

// ── 设置持久化（settings_kv） ─────────────────────────────────

#[tauri::command]
async fn settings_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let map = store::settings::all(&p0.store)?;
    Ok(serde_json::to_value(map).unwrap_or(json!({})))
}

#[tauri::command]
async fn settings_set(
    p0: State<'_, Arc<P0Services>>,
    key: String,
    value: String,
) -> Result<Value, String> {
    store::settings::set(&p0.store, &key, &value)?;
    Ok(json!({ "ok": true, "key": key }))
}

// ── 文件能力与附件上传（占位清单第一批 P0 + 交互补设计 §3） ──────

#[tauri::command]
async fn file_list(
    p0: State<'_, Arc<P0Services>>,
    dir_path: String,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    // 空目录缺省为工作区文件根（workspaces/{ws}/files）
    let target = if dir_path.trim().is_empty() {
        file::service::workspace_files_root(&ws)
            .to_string_lossy()
            .to_string()
    } else {
        dir_path
    };
    let items = file::service::list_dir(&p0.store, &ws, &target)?;
    Ok(serde_json::to_value(items).unwrap_or(Value::Array(vec![])))
}

#[tauri::command]
async fn file_read(
    p0: State<'_, Arc<P0Services>>,
    file_path: String,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    let content = file::service::read_file(&p0.store, &ws, &file_path)?;
    Ok(json!({ "path": file_path, "content": content }))
}

#[tauri::command]
async fn file_preview(
    p0: State<'_, Arc<P0Services>>,
    file_paths: Vec<String>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    let previews = file::service::preview(&p0.store, &ws, &file_paths)?;
    Ok(serde_json::to_value(previews).unwrap_or(Value::Array(vec![])))
}

/// 附件暂存（交互补设计 §3.1 附件模式）：复制到运行目录并返回 AttachmentMeta
#[tauri::command]
async fn file_stage_attachment(
    src_path: String,
    task_id: Option<String>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    let tid = task_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("pending-{}", now_ms_u64()));
    let meta = file::service::stage_attachment(&ws, &tid, &src_path)?;
    Ok(serde_json::to_value(meta).unwrap_or(Value::Null))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(agent::new_bridge_state())
        .setup(|app| {
            // 随包资源根目录注入：安装后引擎脚本与嵌入式 Python 由此定位（开发模式无此目录）
            if let Ok(resource_dir) = app.path().resource_dir() {
                bridge::python_env::init_resource_dir(resource_dir);
            }

            // 存储地基：运行目录 SQLite（失败时回退 temp，保证应用可用）
            let store = match Store::open_default() {
                Ok(s) => Arc::new(s),
                Err(e) => {
                    eprintln!("[store] 运行目录数据库初始化失败，回退 temp: {e}");
                    Arc::new(
                        Store::open(std::env::temp_dir().join("tongtian-fallback.db"))
                            .expect("store fallback init"),
                    )
                }
            };

            // 事件存储目录：优先 app data 目录，回退当前目录
            let events_dir = app
                .path()
                .app_data_dir()
                .map(|d| d.join("events"))
                .unwrap_or_else(|_| std::path::PathBuf::from(".tongtian-events"));
            let events = Arc::new(EventStore::new(events_dir));
            let services = Arc::new(P0Services {
                registry: Arc::new(TaskRegistry::new()),
                leases: Arc::new(LeaseManager::new("local-device")),
                quota: Arc::new(QuotaGate::new(None, DEFAULT_DAILY_QUOTA_TOKENS)),
                events: Arc::clone(&events),
                store,
            });
            app.manage(services.clone());

            // 事件存储后台任务：异步批量刷盘 + 24h TTL 清理
            event_store_start(&events);
            spawn_ttl_cleanup_loop(Arc::clone(&events));

            // 租约扫描循环：单 interval 驱动（心跳超时回收 → LEASE_LOST）
            let leases = Arc::clone(&services.leases);
            let registry = Arc::clone(&services.registry);
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                lease_scan_loop(handle, leases, registry).await;
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            system_info,
            agent_status,
            agent_start_workspace,
            agent_stop_workspace,
            chat_submit,
            chat_review,
            chat_control,
            chat_cancel,
            chat_history,
            task_state,
            quota_status,
            lease_info,
            lease_takeover,
            event_replay,
            model_list,
            model_add,
            model_update,
            model_delete,
            model_test,
            model_set_default,
            settings_get,
            settings_set,
            file_list,
            file_read,
            file_preview,
            file_stage_attachment,
            auth::auth_status,
            auth::auth_login,
            auth::auth_logout
        ])
        .run(tauri::generate_context!())
        .expect("error while running tongtian-tauri")
}

/// 启动事件存储刷盘任务（独立函数便于测试替换）
fn event_store_start(events: &Arc<EventStore>) {
    let store = Arc::clone(events);
    tauri::async_runtime::spawn(async move {
        store.start_flusher().await.ok();
    });
}

/// 租约扫描循环：超时回收触发 executing→queued + LEASE_LOST（转移 #18）
async fn lease_scan_loop(
    app: AppHandle,
    leases: Arc<LeaseManager>,
    registry: Arc<TaskRegistry>,
) {
    let mut interval = tokio::time::interval(std::time::Duration::from_millis(
        task::lease::SCAN_INTERVAL_MS,
    ));
    interval.tick().await; // 首个 tick 立即返回，跳过
    loop {
        interval.tick().await;
        let now_ms = now_ms_u64();
        for event in leases.scan(now_ms) {
            match event {
                LeaseEvent::TimeoutReclaimed { task_id, .. } => {
                    // 转移 #18：executing→queued（从最后 success Step 恢复）
                    if let Some(r) =
                        registry.update_status(&task_id, status::QUEUED, Some("LEASE_LOST"))
                    {
                        emit_status_change(&app, &r);
                    }
                    let _ = app.emit(
                        agent::EVENT_CHAT,
                        json!({
                            "type": "error",
                            "code": "LEASE_LOST",
                            "message": "执行租约超时回收，任务重新排队",
                            "taskId": task_id,
                        }),
                    );
                }
                LeaseEvent::IdleReleased { task_id, .. } => {
                    eprintln!("[lease] idle released: {task_id}");
                }
            }
        }
    }
}
