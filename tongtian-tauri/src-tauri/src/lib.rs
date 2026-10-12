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
//! - model_usage（真实用量台账：引擎 usage 事件按「模型 × 日」累加，daily/monthly/total）
//! - usage_stats / usage_report（用量总览与按日趋势报表，读同一 model_usage 台账）
//! - file_list / file_read / file_preview / file_stage_attachment（文件能力 + 附件上传）
//! - settings_get / settings_set（设置持久化）
//! - awareness_get / update / memories / memory_add / memory_delete
//!   （意识以 `~/.tongtianwork/awareness/main` 下真实文件为事实来源，与引擎同目录，不再第二份数据库副本）
//!
//! 账号与登录态（网页验证登录 · Loopback 方案，TT_AUTH_MOCK 开关）：
//! - auth_status / auth_login / auth_logout
//!
//! 安全与治理（PRD §4.7 安全基线 P0）：
//! - security_scan_data / security_get_domain_control / security_set_domain_control
//! - compliance_status / compliance_update（合规状态聚合与硬开关）
//! - sandbox_policy_get / sandbox_policy_set
//! - audit_log_query / audit_log_export
//! - 出站调用（model_test）前经 policy::ensure_domain_allowed 强制出域校验；
//!   model_*/auth_*/文件与任务关键动作追加审计留痕（脱敏后落库）

mod align;
mod auth;
mod awareness;
mod bridge;
mod file;
mod mcp;
mod model;
mod proxy;
mod security;
mod skill;
mod store;
mod suite;
mod system;
mod task;
mod workspace;

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
    /// 保持系统唤醒（真实阻止系统睡眠，设置页与定时任务页共用）
    pub keep_awake: Arc<task::keep_awake::KeepAwake>,
    /// 正在执行的定时任务（防同一任务并发重复触发）
    pub schedule_running: Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
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

/// 安全工作环境开关（设置项 secureEnv；未设置或值非布尔真值时按关闭处理）
fn secure_env_enabled(store: &store::Store) -> bool {
    let raw = store::settings::get(store, "secureEnv").ok().flatten();
    match raw {
        Some(v) => matches!(v.trim(), "true" | "1"),
        None => false,
    }
}

/// 自选工作目录校验：只接受「存在的目录」，缺省/空白视为未选择。
/// 引擎会照着下发的路径直接建会话子目录，若不校验就会出现「界面显示 D:\a、产出落在别处」，
/// 那等于把用户选的目录丢掉——这里改为明确拒绝。
fn validate_work_dir(raw: Option<&str>) -> Result<Option<std::path::PathBuf>, String> {
    let Some(path) = raw.map(str::trim).filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let buf = std::path::PathBuf::from(path);
    if !buf.is_dir() {
        return Err(format!(
            "WORK_DIR_INVALID: 工作目录不存在或不是目录：{}",
            buf.display()
        ));
    }
    Ok(Some(buf))
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
    conversation_id: Option<String>,
    turn_index: Option<i64>,
    model_binding: Option<String>,
    model_config: Option<Value>,
    work_dir: Option<String>,
    history: Option<Vec<Value>>,
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

    // 对话内选择项：先校验工作目录，再受理任务（避免留下必然失败的幽灵任务）
    let work_dir = validate_work_dir(work_dir.as_deref())?;
    let overrides = agent::SubmitOverrides {
        model_binding: model_binding.map(|s| s.trim().to_string()).filter(|s| !s.is_empty()),
        model_config: model_config.filter(|v| v.as_object().map_or(false, |o| !o.is_empty())),
        work_dir: work_dir.map(|p| p.display().to_string()),
        history: history.unwrap_or_default(),
    };

    // 任务注册表受理：转移 #1 (创建)→queued，并推送状态事件
    let record = p0.registry.create(&task_id, &ws);
    emit_status_change(&app, &record);
    sync_task_to_db(&p0, &task_id, &title);

    // 会话正文落库（用户提问）：渲染层带着会话键与轮次号提交，
    // 未带时以任务 id 作为会话键，保证正文不丢（重启后至少这轮问答可见）
    let conv_id = conversation_id
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| task_id.clone());
    let turn = turn_index.unwrap_or(0).max(0);
    if let Err(e) = store::messages::record_user_goal(
        &p0.store,
        &conv_id,
        turn,
        &task_id,
        &goal,
        now_ms_u64() as i64,
    ) {
        eprintln!("[store] user message persist failed: {e}");
    }

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
            store: Some(Arc::clone(&services.store)),
            secure_env: secure_env_enabled(&services.store),
        };
        let result = agent::send_message_with_attachments(
            &app2,
            &bridge,
            &ws2,
            &tid,
            &goal,
            attachments.unwrap_or_default(),
            overrides,
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

    // 任务受理留痕（PRD §4.7.4）：目标文本入库前经 audit 统一脱敏
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_TASK_SUBMIT,
        security::audit::SEV_INFO,
        &task_id,
        &format!("提交任务：{title}"),
    );

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
                    store: Some(Arc::clone(&services.store)),
                    secure_env: secure_env_enabled(&services.store),
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

    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_TASK_CANCEL,
        security::audit::SEV_INFO,
        &task_id,
        if cancellable { "用户取消任务" } else { "任务已终态，取消未生效" },
    );

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

/// 会话列表（chat_messages 聚合，按最近活跃倒序）——重启后重建侧边栏「任务」
#[tauri::command]
async fn chat_conversations(
    p0: State<'_, Arc<P0Services>>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let rows = store::messages::list_conversations(&p0.store, limit)?;
    Ok(serde_json::to_value(rows).unwrap_or(Value::Array(vec![])))
}

/// 单个会话的正文（轮次升序；用户提问与助手回复逐行返回，由渲染层组装轮次）
#[tauri::command]
async fn chat_messages(
    p0: State<'_, Arc<P0Services>>,
    conversation_id: String,
    limit: Option<usize>,
) -> Result<Value, String> {
    let id = conversation_id.trim();
    if id.is_empty() {
        return Err("CHAT_INVALID_CONVERSATION: 会话标识不能为空".to_string());
    }
    let rows = store::messages::list_by_conv(&p0.store, id, limit)?;
    Ok(serde_json::to_value(rows).unwrap_or(Value::Array(vec![])))
}

/// 记录/取消一轮回复的点赞或点踩（feedback 为空即取消，正文不受影响）
#[tauri::command]
async fn chat_feedback(
    p0: State<'_, Arc<P0Services>>,
    conversation_id: String,
    turn_index: i64,
    feedback: Option<String>,
) -> Result<Value, String> {
    let id = conversation_id.trim();
    if id.is_empty() {
        return Err("CHAT_INVALID_CONVERSATION: 会话标识不能为空".to_string());
    }
    let value = feedback.as_deref().map(str::trim).filter(|v| !v.is_empty());
    store::messages::set_feedback(&p0.store, id, turn_index, value, crate::bridge::event_store::now_ms() as i64)?;
    Ok(Value::Object(serde_json::Map::from_iter([(
        "ok".to_string(),
        Value::Bool(true),
    )])))
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
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_CONFIG_CHANGE,
        security::audit::SEV_INFO,
        &cfg.model_name,
        &format!("新增模型配置：{}（来源 {}）", cfg.name, cfg.source),
    );
    Ok(serde_json::to_value(cfg).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_update(
    p0: State<'_, Arc<P0Services>>,
    req: model::service::ModelUpdateReq,
) -> Result<Value, String> {
    let cfg = model::service::update(&p0.store, req)?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_CONFIG_CHANGE,
        security::audit::SEV_INFO,
        &cfg.model_name,
        &format!("更新模型配置：{}", cfg.name),
    );
    Ok(serde_json::to_value(cfg).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_delete(p0: State<'_, Arc<P0Services>>, id: String) -> Result<Value, String> {
    // 先取名称用于留痕（删除后无从查证）
    let name = model::service::get(&p0.store, &id)?
        .map(|c| c.name)
        .unwrap_or_else(|| id.clone());
    model::service::delete(&p0.store, &id)?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_CONFIG_CHANGE,
        security::audit::SEV_WARNING,
        &name,
        "删除模型配置",
    );
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

    // 出域校验（PRD §4.7.1 真实执行点）：硬开关/白名单拦截时不出站，并留痕
    if let Err(denied) = security::policy::ensure_domain_allowed(&p0.store, &req.base_url) {
        security::audit::record_quiet(
            &p0.store,
            security::audit::ACTION_ROUTE_SWITCH,
            security::audit::SEV_WARNING,
            &req.base_url,
            &format!("出域校验拦截模型连通性测试：{denied}"),
        );
        return Err(denied);
    }

    let resp = model::service::test(req).await;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_MODEL_CALL,
        if resp.ok {
            security::audit::SEV_INFO
        } else {
            security::audit::SEV_ERROR
        },
        &resp.model,
        &match &resp.error {
            Some(e) => format!("连通性测试失败：{e}"),
            None => format!("连通性测试通过（{}ms）", resp.latency_ms),
        },
    );
    Ok(serde_json::to_value(resp).unwrap_or(Value::Null))
}

#[tauri::command]
async fn model_set_default(p0: State<'_, Arc<P0Services>>, id: String) -> Result<Value, String> {
    model::service::set_default(&p0.store, &id)?;
    let name = model::service::get(&p0.store, &id)?
        .map(|c| c.model_name)
        .unwrap_or_else(|| id.clone());
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_ROUTE_SWITCH,
        security::audit::SEV_INFO,
        &name,
        "切换全局默认模型",
    );
    Ok(json!({ "ok": true }))
}

/// 模型用量统计（真实台账：引擎 usage 事件按「模型 × 日」累加）
#[tauri::command]
async fn model_usage(p0: State<'_, Arc<P0Services>>, period: String) -> Result<Value, String> {
    let period = model::usage::normalize_period(&period)?;
    let rows = model::usage::query(&p0.store, &period)?;
    Ok(json!({ "period": period, "rows": rows }))
}

/// 用量总览（`usage:stats`）：同一台账的周期汇总，供用量页顶部总览条
#[tauri::command]
async fn usage_stats(p0: State<'_, Arc<P0Services>>, period: String) -> Result<Value, String> {
    let view = model::usage::stats(&p0.store, &period)?;
    Ok(serde_json::to_value(view).map_err(|e| format!("USAGE_STATS_FAILED: {e}"))?)
}

/// 用量报表（`usage:report`）：按日趋势 + 每模型明细 + 汇总
#[tauri::command]
async fn usage_report(p0: State<'_, Arc<P0Services>>, period: String) -> Result<Value, String> {
    let view = model::usage::report(&p0.store, &period)?;
    Ok(serde_json::to_value(view).map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?)
}

// ── 设置持久化（settings_kv） ─────────────────────────────────

#[tauri::command]
async fn settings_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let map = store::settings::all(&p0.store)?;
    // 写入侧统一 JSON 编码（见 settings_set_many），读取侧还原类型，
    // 避免 "false" 这类字符串在渲染进程被当成真值
    let mut out = serde_json::Map::new();
    for (k, v) in map {
        out.insert(k, store::settings::decode_value(&v));
    }
    Ok(Value::Object(out))
}

#[tauri::command]
async fn settings_set(
    p0: State<'_, Arc<P0Services>>,
    key: String,
    value: String,
) -> Result<Value, String> {
    let parsed = serde_json::from_str(&value).unwrap_or(json!(value));
    // 代理组按键联动，落库前先按最终状态校验（见 settings_set_many 注释）
    let mut probe = serde_json::Map::new();
    probe.insert(key.clone(), parsed.clone());
    proxy::spec_from_patch(&p0.store, &probe)?;
    store::settings::set(&p0.store, &key, &value)?;
    apply_setting_side_effect(&p0, &key, &parsed)?;
    Ok(json!({ "ok": true, "key": key }))
}

/// 批量写设置（设置页对象 patch）：值为任意 JSON，序列化后落库；
/// 需要系统行为联动的键（保持唤醒 / 网络代理）写入后立即生效
#[tauri::command]
async fn settings_set_many(p0: State<'_, Arc<P0Services>>, patch: Value) -> Result<Value, String> {
    let obj = patch
        .as_object()
        .ok_or_else(|| "VALIDATION_ERROR: settings 补丁必须是对象".to_string())?;
    // HashMap 遍历顺序不定：代理三键若分两轮校验，可能先落 mode 再因地址缺失报错，
    // 留下半个生效配置。故先按「补丁叠加库内现值」的最终状态整组校验，通过后才写。
    proxy::spec_from_patch(&p0.store, obj)?;
    let mut keys = Vec::new();
    for (k, v) in obj {
        store::settings::set(&p0.store, k, &store::settings::encode_value(v))?;
        apply_setting_side_effect(&p0, k, v)?;
        keys.push(k.clone());
    }
    Ok(json!({ "ok": true, "keys": keys }))
}

/// 设置值 → 布尔（兼容 JSON 布尔与裸字符串两种历史存法）
fn bool_of_setting(value: &Value) -> bool {
    match value {
        Value::Bool(b) => *b,
        Value::String(s) => s.trim().trim_matches('"') == "true",
        _ => false,
    }
}

/// 设置项落库后的系统行为联动（保持唤醒 / 开机自启 / 网络代理）
fn apply_setting_side_effect(p0: &P0Services, key: &str, value: &Value) -> Result<(), String> {
    match key {
        task::keep_awake::SETTING_KEY => p0.keep_awake.apply(bool_of_setting(value)),
        system::autostart::SETTING_KEY => system::autostart::apply(bool_of_setting(value)),
        proxy::SETTING_KEY_MODE | proxy::SETTING_KEY_URL | proxy::SETTING_KEY_BYPASS => {
            // 整组校验后发布：自定义却没填地址 → 保存即失败，不留下半个生效配置
            let spec = proxy::spec(&p0.store)?;
            proxy::publish(spec);
            Ok(())
        }
        _ => Ok(()),
    }
}

/// 保持唤醒实际状态（requested 为持久化意愿，active 为约束是否真实生效）
#[tauri::command]
async fn keep_awake_status(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    Ok(json!({
        "requested": keep_awake_requested(&p0),
        "active": p0.keep_awake.is_active(),
        "supported": cfg!(windows),
    }))
}

/// 网络代理实际生效状态：active 为当前 Rust 出网/新启动引擎使用的配置，
/// staleEngines 为仍在用旧代理的运行中实例数（httpx 在 client 创建时解析代理，
/// 故已启动的引擎需重启才会切换）。
#[tauri::command]
async fn proxy_status(
    p0: State<'_, Arc<P0Services>>,
    bridge: State<'_, BridgeState>,
) -> Result<Value, String> {
    let spec = proxy::current();
    let stale = agent::proxy_stale_instances(&bridge).await;
    Ok(json!({
        "active": {
            "mode": proxy::mode_label(spec.mode),
            "url": spec.url,
            "bypass": spec.bypass,
        },
        "persisted": proxy::spec(&p0.store).ok().map(|s| json!({
            "mode": proxy::mode_label(s.mode),
            "url": s.url,
            "bypass": s.bypass,
        })),
        "effectiveBypass": spec.effective_bypass(),
        "staleEngines": stale,
    }))
}

/// 持久化的布尔设置项（settings_kv，兼容 JSON 布尔与裸字符串）
pub fn setting_bool(p0: &P0Services, key: &str) -> bool {
    store::settings::get(&p0.store, key)
        .ok()
        .flatten()
        .map(|v| v.trim().trim_matches('"') == "true")
        .unwrap_or(false)
}

/// 持久化的「保持唤醒」意愿（settings_kv keepAwake，兼容 JSON 与裸字符串）
fn keep_awake_requested(p0: &P0Services) -> bool {
    setting_bool(p0, task::keep_awake::SETTING_KEY)
}

// ── 定时任务（schedule_*：任务定义 + 真实调度执行 + 执行记录） ──────

/// 定时任务列表（含下次执行时间/上次执行状态）
#[tauri::command]
async fn schedule_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let rows = task::schedule::list(&p0.store)?;
    Ok(serde_json::to_value(rows).unwrap_or(Value::Array(vec![])))
}

/// 新建定时任务（频率/时刻/星期先规范化，非法即拒）
#[tauri::command]
async fn schedule_create(
    p0: State<'_, Arc<P0Services>>,
    req: task::schedule::NewSchedule,
) -> Result<Value, String> {
    let row = task::schedule::create(&p0.store, &req, now_ms_u64() as i64)?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_CONFIG_CHANGE,
        security::audit::SEV_INFO,
        &row.schedule_id,
        &format!(
            "新建定时任务：{}（{} {}）",
            row.name, row.freq, row.time_of_day
        ),
    );
    Ok(serde_json::to_value(row).unwrap_or(Value::Null))
}

/// 编辑定时任务（缺省字段沿用原配置；启用中的任务按当前时刻重算下次执行时间）
#[tauri::command]
async fn schedule_update(
    p0: State<'_, Arc<P0Services>>,
    req: task::schedule::UpdateSchedule,
) -> Result<Value, String> {
    let schedule_id = req.schedule_id.trim().to_string();
    if schedule_id.is_empty() {
        return Err("SCHEDULE_INVALID_ID: 缺少定时任务标识".into());
    }
    let before = task::schedule::get(&p0.store, &schedule_id)?
        .ok_or_else(|| format!("SCHEDULE_NOT_FOUND: {schedule_id}"))?;
    let row = task::schedule::update(&p0.store, &req, now_ms_u64() as i64)?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_CONFIG_CHANGE,
        security::audit::SEV_INFO,
        &row.schedule_id,
        &format!(
            "编辑定时任务：{} → {}（{} {}）",
            before.name, row.name, row.freq, row.time_of_day
        ),
    );
    Ok(serde_json::to_value(row).unwrap_or(Value::Null))
}

/// 启用/禁用定时任务（禁用清空下次执行时间）
#[tauri::command]
async fn schedule_toggle(
    p0: State<'_, Arc<P0Services>>,
    schedule_id: String,
    enabled: bool,
) -> Result<Value, String> {
    let row = task::schedule::set_enabled(&p0.store, &schedule_id, enabled, now_ms_u64() as i64)?;
    Ok(serde_json::to_value(row).unwrap_or(Value::Null))
}

/// 删除定时任务（连同执行记录）
#[tauri::command]
async fn schedule_delete(
    p0: State<'_, Arc<P0Services>>,
    schedule_id: String,
) -> Result<Value, String> {
    let removed = task::schedule::remove(&p0.store, &schedule_id)?;
    Ok(json!({ "ok": true, "removed": removed }))
}

/// 执行记录（可按任务过滤；按开始时间倒序）
#[tauri::command]
async fn schedule_runs(
    p0: State<'_, Arc<P0Services>>,
    schedule_id: Option<String>,
    limit: Option<usize>,
) -> Result<Value, String> {
    let sid = schedule_id.filter(|s| !s.trim().is_empty());
    let rows = task::schedule::list_runs(&p0.store, sid.as_deref(), limit.unwrap_or(0))?;
    Ok(serde_json::to_value(rows).unwrap_or(Value::Array(vec![])))
}

/// 立即执行（手动触发，与到点触发共用同一条执行链路）
#[tauri::command]
async fn schedule_run_now(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    schedule_id: String,
) -> Result<Value, String> {
    let row = task::schedule::get(&p0.store, &schedule_id)?
        .ok_or_else(|| format!("SCHEDULE_NOT_FOUND: {schedule_id}"))?;
    let bridge = state.inner().clone();
    let services = p0.inner().clone();
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = execute_schedule(&app2, &bridge, &services, &row).await {
            eprintln!("[schedule] 手动触发失败: {e}");
        }
    });
    Ok(json!({ "ok": true, "scheduleId": schedule_id, "started": true }))
}

/// 定时任务执行：提交计划流 → 自动批准执行流（等待终态）→ 回写执行记录
/// 与手动提交共用注册表/租约/配额/用量计量，区别仅在「无需人工审阅」。
async fn execute_schedule(
    app: &AppHandle,
    bridge: &BridgeState,
    services: &Arc<P0Services>,
    sched: &task::schedule::ScheduleRow,
) -> Result<String, String> {
    {
        let mut running = services
            .schedule_running
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if !running.insert(sched.schedule_id.clone()) {
            return Err(format!(
                "SCHEDULE_ALREADY_RUNNING: 定时任务「{}」正在执行中",
                sched.name
            ));
        }
    }
    let result = execute_schedule_inner(app, bridge, services, sched).await;
    if let Ok(mut running) = services.schedule_running.lock() {
        running.remove(&sched.schedule_id);
    }
    result
}

async fn execute_schedule_inner(
    app: &AppHandle,
    bridge: &BridgeState,
    services: &Arc<P0Services>,
    sched: &task::schedule::ScheduleRow,
) -> Result<String, String> {
    let goal = if sched.description.trim().is_empty() {
        sched.name.clone()
    } else {
        sched.description.clone()
    };
    let title: String = format!("[定时] {}", goal.chars().take(24).collect::<String>());

    // 配额闸门：与手动提交同一口径（100% 档阻断并留痕）
    let started_at = now_ms_u64() as i64;
    if let Err(code) = services.quota.check_submit() {
        let run_id = task::schedule::start_run(&services.store, &sched.schedule_id, None, started_at)?;
        let summary = format!("{code}: 配额已用尽，本次执行被阻断");
        task::schedule::finish_run(
            &services.store,
            &run_id,
            &sched.schedule_id,
            None,
            task::schedule::RUN_BLOCKED,
            &summary,
            now_ms_u64() as i64,
        )?;
        return Err(summary);
    }

    let task_id = format!("task-{}", uuid::Uuid::new_v4().simple());
    let record = services.registry.create(&task_id, &sched.workspace_id);
    emit_status_change(app, &record);
    sync_task_to_db(services, &task_id, &title);

    match services.leases.acquire(&task_id, now_ms_u64()) {
        Ok(lease) => {
            services.registry.bind_lease(&task_id, &lease.lease_id);
            if let Some(r) = services.registry.update_status(&task_id, status::PLANNING, None) {
                emit_status_change(app, &r);
            }
        }
        Err(takeover) => {
            services
                .registry
                .update_status(&task_id, status::CANCELLED, Some("TASK_ALREADY_RUNNING"));
            sync_task_to_db(services, &task_id, "");
            return Err(format!("TASK_ALREADY_RUNNING: {}", takeover.message));
        }
    }

    let run_id =
        task::schedule::start_run(&services.store, &sched.schedule_id, Some(&task_id), started_at)?;
    let ctx = P0Context {
        events: Some(Arc::clone(&services.events)),
        quota: Some(Arc::clone(&services.quota)),
        registry: Some(Arc::clone(&services.registry)),
        store: Some(Arc::clone(&services.store)),
        secure_env: secure_env_enabled(&services.store),
    };

    // 阶段一：计划流（与手动提交一致，结束后进入 pending_review）
    let outcome = async {
        agent::start_workspace(app, bridge, &sched.workspace_id).await?;
        agent::send_message(app, bridge, &sched.workspace_id, &task_id, &goal, ctx.clone()).await?;
        services.leases.release(&task_id);
        if let Some(r) = services
            .registry
            .update_status(&task_id, status::PENDING_REVIEW, None)
        {
            emit_status_change(app, &r);
        }

        // 阶段二：定时任务无需人工审阅 → 自动批准执行
        let lease = services
            .leases
            .acquire(&task_id, now_ms_u64())
            .map_err(|t| format!("TASK_ALREADY_RUNNING: {}", t.message))?;
        services.registry.bind_lease(&task_id, &lease.lease_id);
        if let Some(r) = services.registry.update_status(&task_id, status::EXECUTING, None) {
            emit_status_change(app, &r);
        }
        agent::review_task(
            app,
            bridge,
            &sched.workspace_id,
            &task_id,
            "approve",
            Value::Array(vec![]),
            ctx,
        )
        .await
    }
    .await;

    let finished_at = now_ms_u64() as i64;
    let (run_status, summary) = match outcome {
        Ok(()) => {
            if let Some(r) = services.registry.update_status(&task_id, status::DONE, None) {
                emit_status_change(app, &r);
            }
            (
                task::schedule::RUN_SUCCEEDED,
                format!("执行完成（任务 {task_id}）"),
            )
        }
        Err(e) => {
            eprintln!("[schedule] 执行失败 schedule={} task={task_id}: {e}", sched.schedule_id);
            if let Some(r) = services
                .registry
                .update_status(&task_id, status::FAILED, Some("BRIDGE_ERROR"))
            {
                emit_status_change(app, &r);
            }
            let _ = app.emit(
                agent::EVENT_CHAT,
                json!({ "type": "error", "code": "BRIDGE_ERROR", "message": e }),
            );
            let _ = app.emit(agent::EVENT_CHAT, json!({ "type": "done", "taskId": task_id }));
            (task::schedule::RUN_FAILED, format!("执行失败：{e}"))
        }
    };

    sync_task_to_db(services, &task_id, "");
    services.leases.release(&task_id);
    task::schedule::finish_run(
        &services.store,
        &run_id,
        &sched.schedule_id,
        Some(&task_id),
        run_status,
        &summary,
        finished_at,
    )?;
    security::audit::record_quiet(
        &services.store,
        security::audit::ACTION_SCHEDULE_RUN,
        if run_status == task::schedule::RUN_SUCCEEDED {
            security::audit::SEV_INFO
        } else {
            security::audit::SEV_WARNING
        },
        &sched.schedule_id,
        &format!("定时任务「{}」{}", sched.name, summary),
    );
    Ok(task_id)
}

/// 定时任务扫描循环：到点即推进下次执行时间（防重复触发）并后台执行
async fn schedule_scan_loop(app: AppHandle, bridge: BridgeState, services: Arc<P0Services>) {
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(
        task::schedule::SCAN_INTERVAL_SECS,
    ));
    interval.tick().await; // 首个 tick 立即返回，跳过
    loop {
        interval.tick().await;
        let now_ms = now_ms_u64() as i64;
        let rows = match task::schedule::list(&services.store) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[schedule] 扫描失败: {e}");
                continue;
            }
        };
        for sched in task::schedule::due(&rows, now_ms) {
            if let Err(e) = task::schedule::advance_next(&services.store, &sched.schedule_id, now_ms)
            {
                eprintln!("[schedule] 推进下次执行时间失败 {}: {e}", sched.schedule_id);
                continue;
            }
            let app2 = app.clone();
            let bridge2 = bridge.clone();
            let services2 = Arc::clone(&services);
            tauri::async_runtime::spawn(async move {
                if let Err(e) = execute_schedule(&app2, &bridge2, &services2, &sched).await {
                    eprintln!("[schedule] 到点执行失败 {}: {e}", sched.schedule_id);
                }
            });
        }
    }
}

// ── 文件能力与附件上传（占位清单第一批 P0 + 交互补设计 §3） ──────

/// 文件白名单越界（FILE_ACCESS_DENIED）落一条沙箱违规审计后原样上抛。
/// 其它错误码不记录，避免审计噪音淹没真实越界尝试。
fn audit_file_denied<T>(res: Result<T, String>, p0: &P0Services, path: &str) -> Result<T, String> {
    if let Err(ref e) = res {
        if e.starts_with("FILE_ACCESS_DENIED") {
            security::audit::record_quiet(
                &p0.store,
                security::audit::ACTION_SANDBOX_VIOLATION,
                security::audit::SEV_WARNING,
                path,
                &format!("文件访问越界被拦截：{e}"),
            );
        }
    }
    res
}

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
    let items = audit_file_denied(
        file::service::list_dir(&p0.store, &ws, &target),
        &p0,
        &target,
    )?;
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
    let content = audit_file_denied(
        file::service::read_file(&p0.store, &ws, &file_path),
        &p0,
        &file_path,
    )?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_FILE_READ,
        security::audit::SEV_INFO,
        &file_path,
        &format!("读取文件（{} 字符）", content.chars().count()),
    );
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
    p0: State<'_, Arc<P0Services>>,
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
    let meta = audit_file_denied(
        file::service::stage_attachment(&ws, &tid, &src_path),
        &p0,
        &src_path,
    )?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_FILE_WRITE,
        if meta.sensitivity == "confidential" {
            security::audit::SEV_WARNING
        } else {
            security::audit::SEV_INFO
        },
        &meta.staged_path,
        &format!(
            "附件暂存：{}（{} 字节，分级 {}）",
            meta.file_name, meta.size_bytes, meta.sensitivity
        ),
    );
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
            // 配额台账落运行目录：跨重启保留当日用量（此前纯内存，重启即清零）
            let quota_ledger = store::db::runtime_home().join("quota-ledger.json");
            let services = Arc::new(P0Services {
                registry: Arc::new(TaskRegistry::new()),
                leases: Arc::new(LeaseManager::new("local-device")),
                quota: Arc::new(QuotaGate::new(Some(quota_ledger), DEFAULT_DAILY_QUOTA_TOKENS)),
                events: Arc::clone(&events),
                store,
                keep_awake: Arc::new(task::keep_awake::KeepAwake::new()),
                schedule_running: Arc::new(std::sync::Mutex::new(std::collections::HashSet::new())),
            });
            app.manage(services.clone());

            // 保持唤醒：恢复上次开启的状态（真实生效，失败仅告警不阻断启动）
            if keep_awake_requested(&services) {
                if let Err(e) = services.keep_awake.apply(true) {
                    eprintln!("[keep-awake] 启动恢复失败: {e}");
                }
            }

            // 网络代理：启动即发布生效配置，后续 Rust 出网与引擎子进程都读这一份
            proxy::publish(proxy::spec_or_system(&services.store));

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

            // 定时任务扫描循环：到点触发真实执行（提交→自动批准→记执行记录）
            let sched_services = Arc::clone(&services);
            let sched_bridge = app.state::<BridgeState>().inner().clone();
            let sched_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                schedule_scan_loop(sched_handle, sched_bridge, sched_services).await;
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
            chat_conversations,
            chat_messages,
            chat_feedback,
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
            model_usage,
            usage_stats,
            usage_report,
            settings_get,
            settings_set,
            settings_set_many,
            keep_awake_status,
            system::system_autostart_status,
            proxy_status,
            schedule_list,
            schedule_create,
            schedule_update,
            schedule_toggle,
            schedule_delete,
            schedule_runs,
            schedule_run_now,
            file_list,
            file_read,
            file_preview,
            file_stage_attachment,
            auth::auth_status,
            auth::auth_login,
            auth::auth_logout,
            security::security_scan_data,
            security::security_get_domain_control,
            security::security_set_domain_control,
            security::compliance_status,
            security::compliance_update,
            security::compliance_check,
            security::sandbox_policy_get,
            security::sandbox_policy_set,
            security::sandbox_env_usage,
            security::sandbox_env_clear,
            security::system_permission_status,
            security::system_permission_open,
            security::audit_log_query,
            security::audit_log_export,
            workspace::workspace_list,
            workspace::workspace_create,
            skill::skill_root,
            skill::skill_list,
            skill::skill_install,
            skill::skill_uninstall,
            skill::skill_toggle,
            suite::suite_root,
            suite::suite_list,
            suite::suite_install,
            suite::suite_uninstall,
            align::hooks_get,
            align::hooks_set,
            align::hooks_test,
            align::connector_list,
            align::connector_authorize,
            align::connector_revoke,
            align::task_archive,
            align::task_archive_list,
            align::task_unarchive,
            align::task_archive_delete,
            align::task_archive_clear,
            align::computer_use_get_policy,
            align::computer_use_set_policy,
            awareness::awareness_get,
            awareness::awareness_update,
            awareness::awareness_memories,
            awareness::awareness_memory_add,
            awareness::awareness_memory_delete,
            mcp::mcp_list_servers,
            mcp::mcp_add_server,
            mcp::mcp_remove_server,
            mcp::mcp_connect,
            mcp::mcp_disconnect,
            mcp::mcp_list_tools
        ])
        .build(tauri::generate_context!())
        .expect("error while building tongtian-tauri")
        .run(|handle, event| {
            // 退出时释放唤醒约束子进程，避免遗留孤儿进程
            if let tauri::RunEvent::Exit = event {
                if let Some(services) = handle.try_state::<Arc<P0Services>>() {
                    services.keep_awake.stop();
                }
            }
        });
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

#[cfg(test)]
mod tests {
    use super::validate_work_dir;

    #[test]
    fn missing_or_blank_work_dir_means_not_selected() {
        assert!(validate_work_dir(None).unwrap().is_none());
        assert!(validate_work_dir(Some("")).unwrap().is_none());
        assert!(validate_work_dir(Some("   ")).unwrap().is_none());
    }

    #[test]
    fn nonexistent_dir_is_rejected_not_silently_dropped() {
        let missing = std::env::temp_dir().join(format!("tt-no-such-wd-{}", std::process::id()));
        let err = validate_work_dir(Some(missing.to_str().unwrap())).unwrap_err();
        assert!(err.starts_with("WORK_DIR_INVALID:"), "{err}");
        // 拒绝原因带用户看到的路径，便于定位（而不是笼统说「工作目录非法」）
        assert!(err.contains("tt-no-such-wd"), "{err}");
    }

    #[test]
    fn existing_dir_passes_through_trimmed() {
        let dir = std::env::temp_dir();
        let ok = validate_work_dir(Some(&format!("  {}  ", dir.display())))
            .unwrap()
            .expect("已存在的目录应通过校验");
        assert!(ok.is_dir());
    }

    #[test]
    fn file_path_is_not_accepted_as_work_dir() {
        let file = std::env::temp_dir().join("tt-workdir-not-a-dir.txt");
        std::fs::write(&file, "x").unwrap();
        let err = validate_work_dir(Some(file.to_str().unwrap())).unwrap_err();
        assert!(err.starts_with("WORK_DIR_INVALID:"), "{err}");
        let _ = std::fs::remove_file(&file);
    }
}
