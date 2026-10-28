//! 通天办公 - Tauri 后端入口
//!
//! 阶段二：AgentBridge 集成
//! - agent_start_workspace
//! - chat_submit: 提交任务（两阶段协议：stage=plan 生成计划待审阅）
//! - system_info: 示例 command
//!
//! 三层架构演进 P0（只保留界面真正消费的通道）：
//! - chat_cancel（工作台终止）/ event_replay（事件缺口与历史恢复）
//! - 配额闸门与租约管理仍在提交/执行链路上执行，但不提供无人调用的查询命令
//!
//! 核心闭环补齐（占位清单第一批）：
//! - chat_review / chat_control / chat_history（Workbench 审阅/控制/历史真实化）
//! - model_list/add/update/delete/test/set_default（ModelHub 五件套）
//! - model_detect_local（零配置本地模型：真实探测本机 Ollama，只接受回环地址）
//! - model_usage（真实用量台账：引擎 usage 事件按「模型 × 日」累加，daily/monthly/total）
//! - usage_stats / usage_report（用量总览与按日趋势报表，读同一 model_usage 台账）
//! - file_list / file_read / file_preview / file_stage_attachment（文件能力 + 附件上传）
//! - settings_get / settings_set_many（设置持久化；批量 patch 是唯一写入口）
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
//! - 在线市场：SHA-256 完整性 + Ed25519 发布者签名；信任锚只在本机
//!   `market.trustedPublishers`，索引自报公钥不能替代用户信任

mod align;
mod auth;
mod awareness;
mod background;
mod bridge;
mod browser;
mod collab;
mod computer;
mod expert;
mod file;
mod harness;
mod knowledge;
mod market;
mod mcp;
mod model;
mod plugin;
mod pptx;
mod proxy;
mod remote;
pub mod sandbox;
mod security;
mod skill;
mod store;
mod suite;
mod system;
mod task;
mod terminal;
mod win_console;
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
fn system_info(bg: State<'_, Arc<background::BackgroundState>>) -> Value {
    json!({
        "name": "tongtian-tauri",
        "version": env!("CARGO_PKG_VERSION"),
        "platform": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        // 托盘是否真的创建成功：设置页据此决定「后台常驻」能否开启，
        // 不能只看平台名，否则托盘建失败时会把窗口藏进没有入口的进程
        "backgroundResident": bg.tray_ready(),
    })
}

/// 解析引擎 HTTP 端点（按 workspace 取真实端口；未传 workspace 时取任一在跑实例）
#[tauri::command]
async fn engine_endpoint(
    state: State<'_, BridgeState>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    agent::endpoint_of(&state.inner(), workspace_id.as_deref()).await
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

/// 私有模式开关（设置项 privacyMode；未设置或值非布尔真值时按关闭处理）
fn privacy_enabled(store: &store::Store) -> bool {
    let raw = store::settings::get(store, "privacyMode").ok().flatten();
    match raw {
        Some(v) => matches!(v.trim().trim_matches('"'), "true" | "1"),
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

/// 本地 harness 的工作目录：用户自选优先，否则与引擎同用 workspace 目录
fn harness_work_dir(
    chosen: Option<&std::path::Path>,
    ws: &str,
) -> Result<std::path::PathBuf, String> {
    match chosen {
        Some(path) => Ok(path.to_path_buf()),
        None => bridge::python_env::ensure_workspace_dir(ws)
            .map_err(|e| format!("WORK_DIR_UNAVAILABLE: 无法创建 workspace 工作目录：{e}")),
    }
}

/// 取（或按磁盘检查点重建）本地 harness 任务；`None` 表示该任务不归 harness。
async fn harness_slot_for(
    hub: &harness::Hub,
    task_id: &str,
    ws: &str,
    store: &Arc<Store>,
    model_id: Option<&str>,
    privacy: bool,
) -> Result<Option<Arc<harness::Slot>>, String> {
    if let Some(slot) = hub.get(task_id) {
        return Ok(Some(slot));
    }
    if harness::checkpoint::load(task_id).is_none() {
        return Ok(None);
    }
    // 磁盘检查点不保存密钥：重启后按任务记住的模型 id 重新解析端点与密钥
    let cfg_store = Arc::clone(store);
    let cfg_model = model_id.map(str::to_string);
    let model_config = tauri::async_runtime::spawn_blocking(move || {
        model::service::runtime_config(&cfg_store, cfg_model.as_deref())
    })
    .await
    .map_err(|e| format!("CHECKPOINT_MODEL_UNAVAILABLE: 模型配置解析线程异常：{e}"))??;
    let rt_store = Arc::clone(store);
    let rt_ws = ws.to_string();
    let runtime = tauri::async_runtime::spawn_blocking(move || {
        bridge::runtime::build(Some(&rt_store), &rt_ws, &[]).runtime
    })
    .await
    .map_err(|e| format!("RUNTIME_BUNDLE_PANIC: 执行面输入装配线程异常：{e}"))?;
    let slot = harness::runner::slot_from_checkpoint(
        task_id,
        model_config.unwrap_or(Value::Null),
        runtime,
        privacy,
    )?;
    hub.insert(slot.clone());
    Ok(Some(slot))
}

/// harness 任务收尾：状态回写、正文同步与租约释放（与引擎路径同一状态机）
fn finish_harness_task(
    app: &AppHandle,
    services: &Arc<P0Services>,
    task_id: &str,
    result: Result<(), String>,
    restore: Option<(String, Option<String>)>,
    privacy: bool,
) {
    let cancel_requested = services
        .registry
        .get(task_id)
        .map(|r| r.cancel_requested)
        .unwrap_or(false);
    match result {
        Ok(()) => {
            let next = if cancel_requested {
                status::CANCELLED
            } else {
                status::DONE
            };
            if let Some(r) = services.registry.update_status(task_id, next, None) {
                emit_status_change(app, &r);
            }
        }
        Err(err) => {
            // 恢复没有真正跑起来：回到之前的状态，不能把可重试的中断误标成 failed
            if let Some((previous, reason)) = restore {
                eprintln!("[harness] resume failed: {err}");
                if let Some(r) =
                    services
                        .registry
                        .update_status(task_id, &previous, reason.as_deref())
                {
                    emit_status_change(app, &r);
                }
                let _ = app.emit(
                    agent::EVENT_CHAT,
                    json!({ "type": "error", "code": "CHECKPOINT_RESUME_FAILED", "message": err }),
                );
                let _ = app.emit(
                    agent::EVENT_CHAT,
                    json!({ "type": "done", "taskId": task_id }),
                );
            } else {
                eprintln!("[harness] task failed: {err}");
                let code = harness::runner::error_code(&err);
                if let Some(r) =
                    services
                        .registry
                        .update_status(task_id, status::FAILED, Some(&code))
                {
                    emit_status_change(app, &r);
                }
            }
        }
    }
    if !privacy {
        sync_task_to_db(services, task_id, "");
    }
    services.leases.release(task_id);
}

/// 提交任务：注册表受理（queued→planning）→ 配额闸门 → 租约 → 两阶段计划生成。
/// 计划流结束后任务进入 pending_review 待审阅（approve 后经 chat_review 执行）。
#[tauri::command]
async fn chat_submit(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    hub: State<'_, harness::Hub>,
    workspace_id: Option<String>,
    goal: String,
    // 工作台模式：只接受白名单值，未知值显式失败而不是静默按通用模式跑
    mode: Option<String>,
    attachments: Option<Vec<Value>>,
    conversation_id: Option<String>,
    turn_index: Option<i64>,
    model_binding: Option<String>,
    // 所选已保存模型的 id（端点与密钥由 Rust 从 SQLite + keyring 解析，渲染层不传密钥）
    model_id: Option<String>,
    // 本轮负责人专家：只传注册表 id，提示词/模型绑定由引擎从 runtime.experts 取
    expert_id: Option<String>,
    work_dir: Option<String>,
    history: Option<Vec<Value>>,
) -> Result<Value, String> {
    let ws = workspace_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".into());
    let task_id = format!("task-{}", now_ms_u64());
    let title: String = goal.chars().take(30).collect();
    // 隐私模式：本轮目标与回复不写正文库、不写可回放事件，任务标题也不落任务表
    let privacy = privacy_enabled(&p0.store);

    // 配额闸门：100% 档阻断新提交（错误码 QUOTA_EXCEEDED，ADJ-R5-4）
    if let Err(code) = p0.quota.check_submit() {
        return Err(format!("{code}: 配额已用尽，新任务被阻断"));
    }

    // 对话内选择项：先校验工作目录，再受理任务（避免留下必然失败的幽灵任务）
    let work_dir = validate_work_dir(work_dir.as_deref())?;
    let mode = match mode.as_deref().map(str::trim).filter(|m| !m.is_empty()) {
        None => None,
        Some(m) if matches!(m, "general" | "design" | "slides" | "writing") => Some(m.to_string()),
        Some(m) => {
            return Err(format!(
                "MODE_INVALID: 未知工作台模式 {m}（可用：general/design/slides/writing）"
            ))
        }
    };
    // 模型选择在受理阶段就解析：模型不存在/缺密钥时直接拒绝提交，
    // 不留下一个跑起来才发现调用不了的幽灵任务（Auto 且有默认模型时也在此绑定）
    let model_config = model::service::runtime_config(&p0.store, model_id.as_deref())?;
    // 本地 harness 的计划与执行同样需要这份配置；引擎路径继续用 override 下发
    let slot_model_config = model_config.clone();
    // 负责人专家在受理阶段校验：不存在/绑定模型不可用直接拒绝，不留下必然失败的任务
    let expert_id = match expert_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        Some(raw) => {
            let id = raw.to_lowercase();
            if expert::get(&p0.store, &id)?.is_none() {
                return Err(format!("EXPERT_NOT_FOUND: 负责人专家 {id} 不存在，请刷新专家团队"));
            }
            expert::lead_model_config(&p0.store, &id)?;
            Some(id)
        }
        None => None,
    };
    let work_dir_path = work_dir.clone();

    let attachments = attachments.unwrap_or_default();
    // 执行面输入只装配一次：门控与真正执行的引擎共用同一份 runtime
    let (runtime, hydrated) = {
        let store = Some(Arc::clone(&p0.store));
        let ws_for_runtime = ws.clone();
        let raw = attachments.clone();
        let bundle = tauri::async_runtime::spawn_blocking(move || {
            bridge::runtime::build(store.as_deref(), &ws_for_runtime, &raw)
        })
        .await
        .map_err(|e| format!("RUNTIME_BUNDLE_PANIC: 执行面输入装配线程异常：{e}"))?;
        (bundle.runtime, bundle.attachments)
    };
    // 默认由客户端内嵌 harness 承接；只有它确实做不到的能力才交给引擎。
    // 图片识别与正文抽取只在装配后成立：门控与 harness 都看装配结果
    let harness_reason = harness::engine_required(&harness::GateRequest {
        mode: mode.as_deref().unwrap_or(""),
        expert_id: expert_id.as_deref(),
        attachments: &hydrated,
        runtime: &runtime,
        secure_env: secure_env_enabled(&p0.store),
    });
    let overrides = agent::SubmitOverrides {
        mode,
        model_binding: model_binding.map(|s| s.trim().to_string()).filter(|s| !s.is_empty()),
        model_config,
        expert_id,
        work_dir: work_dir.map(|p| p.display().to_string()),
        history: history.unwrap_or_default(),
        runtime: Some(runtime.clone()),
        // 执行面输入（意识/技能）由桥接层装配，这里不替它决定内容
        ..agent::SubmitOverrides::default()
    };

    // 任务注册表受理：转移 #1 (创建)→queued，并推送状态事件
    let record = p0.registry.create(&task_id, &ws);
    // 记住本轮选中的已保存模型：引擎重启后 resume 要据此重新解析端点与密钥
    p0.registry.set_model_id(
        &task_id,
        model_id.as_deref().map(str::trim).filter(|s| !s.is_empty()),
    );
    emit_status_change(&app, &record);
    // 任务标题由目标前 30 字截取，属于正文内容；隐私模式不落任务表
    if !privacy {
        sync_task_to_db(&p0, &task_id, &title);
    }

    // 会话正文落库（用户提问）：渲染层带着会话键与轮次号提交，
    // 未带时以任务 id 作为会话键，保证正文不丢（重启后至少这轮问答可见）。
    // 隐私模式下跳过：本轮内容只存在于当前进程内存。
    let conv_id = conversation_id
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| task_id.clone());
    let turn = turn_index.unwrap_or(0).max(0);
    if !privacy {
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

    if harness_reason.is_none() {
        // 本地 harness：计划在本机进程内生成，不拉起 Python 引擎
        let work_dir = harness_work_dir(work_dir_path.as_deref(), &ws)?;
        let mode_value = overrides.mode.clone().unwrap_or_default();
        let history = overrides.history.clone();
        let slot = harness::Slot::new(
            &task_id,
            &goal,
            &mode_value,
            work_dir,
            slot_model_config.unwrap_or(Value::Null),
            runtime.clone(),
            history,
            hydrated,
            privacy,
        );
        hub.insert(slot.clone());
        let app2 = app.clone();
        let services = p0.inner().clone();
        let tid = task_id.clone();
        tauri::async_runtime::spawn(async move {
            let result = harness::runner::plan(&app2, &services, &slot).await;
            match result {
                Ok(()) => {
                    let cancel_requested = services
                        .registry
                        .get(&tid)
                        .map(|r| r.cancel_requested)
                        .unwrap_or(false);
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
                    eprintln!("[harness] plan failed: {err}");
                    // 错误事件已由 runner 发出；这里只落状态，不重复播报
                    let code = harness::runner::error_code(&err);
                    if let Some(r) = services
                        .registry
                        .update_status(&tid, status::FAILED, Some(&code))
                    {
                        emit_status_change(&app2, &r);
                    }
                }
            }
            if !privacy {
                sync_task_to_db(&services, &tid, "");
            }
            // 计划阶段结束释放租约；approve 执行阶段重新签发
            services.leases.release(&tid);
        });
    } else {
        // 门控命中：把「为什么交给引擎」摊成可见步骤，避免用户以为本地能力失效
        let reason = harness_reason.unwrap_or_default();
        let _ = app.emit(
            agent::EVENT_CHAT,
            json!({
                "type": "step_start",
                "stepId": "gate-note",
                "stepType": "context",
                "description": format!("本轮由引擎执行：{reason}"),
            }),
        );
        let _ = app.emit(
            agent::EVENT_CHAT,
            json!({ "type": "step_done", "stepId": "gate-note", "output": reason }),
        );

        // 确保实例已启动（首次自动拉起）
        agent::start_workspace(&app, &state.inner(), &ws).await?;

        // 后台执行计划生成流（两阶段 stage=plan），事件经 chat-event 推送前端
        let bridge = state.inner().clone();
        let app2 = app.clone();
        let ws2 = ws.clone();
        let tid = task_id.clone();
        let services = p0.inner().clone();
        let attachments = attachments.clone();
        tauri::async_runtime::spawn(async move {
            let ctx = P0Context {
                events: Some(Arc::clone(&services.events)),
                quota: Some(Arc::clone(&services.quota)),
                registry: Some(Arc::clone(&services.registry)),
                store: Some(Arc::clone(&services.store)),
                secure_env: secure_env_enabled(&services.store),
                privacy,
            };
            let result = agent::send_message_with_attachments(
                &app2,
                &bridge,
                &ws2,
                &tid,
                &goal,
                attachments,
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
                    let _ = app2
                        .emit(agent::EVENT_CHAT, json!({ "type": "done", "taskId": tid }));
                }
            }
            if !privacy {
                sync_task_to_db(&services, &tid, "");
            }
            // 计划阶段结束释放租约；approve 执行阶段重新签发
            services.leases.release(&tid);
        });
    }

    // 任务受理留痕（PRD §4.7.4）：目标文本入库前经 audit 统一脱敏。
    // 隐私模式下只记录「发生过一次提交」，不写目标摘要。
    let audit_details = if privacy {
        "提交任务（隐私模式：不记录目标与标题）".to_string()
    } else {
        format!("提交任务：{title}")
    };
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_TASK_SUBMIT,
        security::audit::SEV_INFO,
        &task_id,
        &audit_details,
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
    hub: State<'_, harness::Hub>,
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
            // 执行阶段沿用提交时的隐私开关：期间用户可能已改设置，以当前值为准
            let privacy = privacy_enabled(&p0.store);
            // 本地 harness 计划（进程内或留盘检查点）优先由本机执行；
            // 重建失败时明确报错，不把 harness 的计划交给引擎
            let harness_slot = harness_slot_for(
                &hub,
                &task_id,
                &ws,
                &p0.store,
                record.model_id.as_deref(),
                privacy,
            )
            .await?;
            // 转移 #9：pending_review→executing；重新签发执行租约
            let lease = p0
                .leases
                .acquire(&task_id, now_ms_u64())
                .map_err(|t| format!("TASK_ALREADY_RUNNING: {}", t.message))?;
            p0.registry.bind_lease(&task_id, &lease.lease_id);
            if let Some(r) = p0.registry.update_status(&task_id, status::EXECUTING, None) {
                emit_status_change(&app, &r);
            }
            if !privacy {
                sync_task_to_db(&p0, &task_id, "");
            }

            if let Some(slot) = harness_slot {
                let app2 = app.clone();
                let services = p0.inner().clone();
                let tid = task_id.clone();
                tauri::async_runtime::spawn(async move {
                    let result = harness::runner::execute(&app2, &services, &slot, false).await;
                    finish_harness_task(&app2, &services, &tid, result, None, privacy);
                });
            } else {
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
                        privacy,
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
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                    services.leases.release(&tid);
                });
            }
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::EXECUTING }))
        }
        "resume" => {
            // 断点续跑：检查点决定能否恢复，Rust 只做状态与租约编排。
            // 已结束的任务（done/failed/cancelled）才有检查点；执行中请走 control pause/resume。
            let resumable_status = matches!(
                record.status.as_str(),
                status::DONE | status::FAILED | status::CANCELLED
            );
            if !resumable_status {
                return Err(format!(
                    "VALIDATION_ERROR: 当前状态 {} 不能从检查点恢复（仅 done/failed/cancelled 可恢复）",
                    record.status
                ));
            }
            let privacy = privacy_enabled(&p0.store);
            let previous_status = record.status.clone();
            let previous_reason = record.status_reason_code.clone();
            // 磁盘检查点不保存密钥：恢复时重新解析模型配置、重新装配执行面输入
            // （本地 harness 与引擎各自从自己的检查点目录取，互不串读）
            let harness_slot = harness_slot_for(
                &hub,
                &task_id,
                &ws,
                &p0.store,
                record.model_id.as_deref(),
                privacy,
            )
            .await
            .map_err(|err| format!("CHECKPOINT_RESUME_FAILED: 无法恢复本地执行上下文：{err}"))?;
            let lease = p0
                .leases
                .acquire(&task_id, now_ms_u64())
                .map_err(|t| format!("TASK_ALREADY_RUNNING: {}", t.message))?;
            p0.registry.bind_lease(&task_id, &lease.lease_id);
            if let Some(r) = p0.registry.update_status(&task_id, status::EXECUTING, None) {
                emit_status_change(&app, &r);
            }
            if !privacy {
                sync_task_to_db(&p0, &task_id, "");
            }

            if let Some(slot) = harness_slot {
                let app2 = app.clone();
                let services = p0.inner().clone();
                let tid = task_id.clone();
                tauri::async_runtime::spawn(async move {
                    let result = harness::runner::execute(&app2, &services, &slot, true).await;
                    finish_harness_task(
                        &app2,
                        &services,
                        &tid,
                        result,
                        Some((previous_status, previous_reason)),
                        privacy,
                    );
                });
            } else {
                // 引擎路径：重新下发 modelConfig 与 runtime（磁盘里不存密钥）
                let model_config =
                    model::service::runtime_config(&p0.store, record.model_id.as_deref()).map_err(
                        |err| {
                            format!("CHECKPOINT_MODEL_UNAVAILABLE: 断点续跑需要重新解析模型配置，但{err}")
                        },
                    )?;
                let runtime_bundle = {
                    let store = p0.store.clone();
                    let ws_for_runtime = ws.clone();
                    tauri::async_runtime::spawn_blocking(move || {
                        bridge::runtime::build(Some(&store), &ws_for_runtime, &[])
                    })
                    .await
                    .map_err(|e| format!("RUNTIME_BUNDLE_PANIC: 执行面输入装配线程异常：{e}"))?
                };
                let resume_context = json!({
                    "modelConfig": model_config,
                    "runtime": runtime_bundle.runtime,
                });

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
                        privacy,
                    };
                    let result = agent::review_task_with(
                        &app2, &bridge, &ws2, &tid, "resume", patch, ctx,
                        Some(resume_context),
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
                            // 恢复没有真正开始（检查点不可用/引擎已重启）：回到之前的状态，
                            // 不能把一个可重试的中断误标成 failed。
                            eprintln!("[bridge] review resume failed: {err}");
                            if let Some(r) = services.registry.update_status(
                                &tid,
                                &previous_status,
                                previous_reason.as_deref(),
                            ) {
                                emit_status_change(&app2, &r);
                            }
                            let _ = app2.emit(
                                agent::EVENT_CHAT,
                                json!({ "type": "error", "code": "CHECKPOINT_RESUME_FAILED", "message": err }),
                            );
                            let _ = app2.emit(
                                agent::EVENT_CHAT,
                                json!({ "type": "done", "taskId": tid }),
                            );
                        }
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                    services.leases.release(&tid);
                });
            }
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
            let privacy = privacy_enabled(&p0.store);
            // harness 计划：进程内直接收尾；重启后只剩留盘计划时按文件清理
            let harness_owned = harness::checkpoint::load(&task_id).is_some();
            if let Some(slot) = hub.remove(&task_id) {
                let app2 = app.clone();
                let services = p0.inner().clone();
                let tid = task_id.clone();
                tauri::async_runtime::spawn(async move {
                    harness::runner::reject(&app2, &slot).await;
                    if let Some(r) = services
                        .registry
                        .update_status(&tid, status::DONE, Some("REJECTED"))
                    {
                        emit_status_change(&app2, &r);
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                    services.leases.release(&tid);
                });
            } else if harness_owned {
                if let Some(cp) = harness::checkpoint::load(&task_id) {
                    cp.delete();
                }
                let app2 = app.clone();
                let services = p0.inner().clone();
                let tid = task_id.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = app2.emit(
                        agent::EVENT_CHAT,
                        json!({
                            "type": "status_change",
                            "taskId": tid,
                            "status": "done",
                            "statusReasonCode": "REJECTED",
                        }),
                    );
                    let _ = app2
                        .emit(agent::EVENT_CHAT, json!({ "type": "done", "taskId": tid }));
                    if let Some(r) = services
                        .registry
                        .update_status(&tid, status::DONE, Some("REJECTED"))
                    {
                        emit_status_change(&app2, &r);
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                    services.leases.release(&tid);
                });
            } else {
                let bridge = state.inner().clone();
                let app2 = app.clone();
                let ws2 = ws.clone();
                let tid = task_id.clone();
                let services = p0.inner().clone();
                tauri::async_runtime::spawn(async move {
                    let ctx = P0Context { privacy, ..P0Context::default() };
                    let _ = agent::review_task(&app2, &bridge, &ws2, &tid, "reject", patch, ctx)
                        .await
                        .map_err(|e| eprintln!("[bridge] review reject failed: {e}"));
                    if let Some(r) = services
                        .registry
                        .update_status(&tid, status::DONE, Some("REJECTED"))
                    {
                        emit_status_change(&app2, &r);
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                    services.leases.release(&tid);
                });
            }
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
            let privacy = privacy_enabled(&p0.store);
            let harness_slot = harness_slot_for(
                &hub,
                &task_id,
                &ws,
                &p0.store,
                record.model_id.as_deref(),
                privacy,
            )
            .await?;
            if let Some(slot) = harness_slot {
                let app2 = app.clone();
                let services = p0.inner().clone();
                let tid = task_id.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(e) = harness::runner::modify_plan(&app2, &slot, &patch).await {
                        eprintln!("[harness] modify failed: {e}");
                        let code = harness::runner::error_code(&e);
                        let _ = app2.emit(
                            agent::EVENT_CHAT,
                            json!({ "type": "error", "code": code, "message": e }),
                        );
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                });
            } else {
                let bridge = state.inner().clone();
                let app2 = app.clone();
                let ws2 = ws.clone();
                let tid = task_id.clone();
                let services = p0.inner().clone();
                tauri::async_runtime::spawn(async move {
                    let ctx = P0Context { privacy, ..P0Context::default() };
                    let result =
                        agent::review_task(&app2, &bridge, &ws2, &tid, "modify", patch, ctx).await;
                    if let Err(e) = result {
                        eprintln!("[bridge] review modify failed: {e}");
                        let _ = app2.emit(
                            agent::EVENT_CHAT,
                            json!({ "type": "error", "code": "BRIDGE_ERROR", "message": e }),
                        );
                    }
                    if !privacy {
                        sync_task_to_db(&services, &tid, "");
                    }
                });
            }
            Ok(json!({ "ok": true, "taskId": task_id, "status": status::PENDING_REVIEW }))
        }
        _ => Err(format!("VALIDATION_ERROR: 未知审阅动作 {action}")),
    }
}

/// 查询任务检查点状态：刷新页面后据此重建「从检查点继续」入口。
/// 进程内任务与磁盘检查点都走引擎查询；密钥与对话正文不经过这里。
#[tauri::command]
async fn chat_checkpoint_query(
    state: State<'_, BridgeState>,
    hub: State<'_, harness::Hub>,
    task_ids: Vec<String>,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let mut ids: Vec<String> = Vec::new();
    for raw in task_ids {
        let id = raw.trim().to_string();
        if !id.is_empty() && !ids.contains(&id) {
            ids.push(id);
        }
        if ids.len() >= 200 {
            break;
        }
    }
    if ids.is_empty() {
        return Ok(serde_json::json!({ "checkpoints": {} }));
    }
    // 本地 harness 的检查点先查（进程内 → harness 专属目录），其余交给引擎
    let mut merged = serde_json::Map::new();
    let mut remaining = Vec::new();
    for id in ids {
        if let Some(slot) = hub.get(&id) {
            merged.insert(id, slot.snapshot().checkpoint.public_info());
        } else if let Some(cp) = harness::checkpoint::load(&id) {
            merged.insert(id, cp.public_info());
        } else {
            remaining.push(id);
        }
    }
    if !remaining.is_empty() {
        match agent::query_checkpoints(state.inner(), workspace_id.as_deref(), remaining).await {
            Ok(value) => {
                if let Some(map) = value.get("checkpoints").and_then(Value::as_object) {
                    for (key, info) in map {
                        merged.insert(key.clone(), info.clone());
                    }
                }
            }
            // 引擎未启动时不影响 harness 检查点；一个都没有才如实报错
            Err(err) if merged.is_empty() => return Err(err),
            Err(_) => {}
        }
    }
    Ok(serde_json::json!({ "checkpoints": merged }))
}

/// 取消任务（转移 #3/#8/#19/#23/#28：非终态 → cancelled）
#[tauri::command]
async fn chat_cancel(
    app: AppHandle,
    state: State<'_, BridgeState>,
    p0: State<'_, Arc<P0Services>>,
    hub: State<'_, harness::Hub>,
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

    // 标记取消请求（幂等）：本地 harness 任务置进程内取消标记，其余通知引擎终止
    p0.registry.request_cancel(&task_id);
    let engine_resp = if hub.cancel(&task_id) {
        Ok(Value::Null)
    } else {
        agent::cancel_task(&state.inner(), &ws, &task_id).await
    };

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
        // 已终态时把真实状态带回渲染层：否则界面只能停在停止键上反复取消
        "status": record.status,
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

/// 改写某一轮的提问（本轮回复与之后的所有轮次随之作废，等重新执行）
#[tauri::command]
async fn chat_edit_turn(
    p0: State<'_, Arc<P0Services>>,
    conversation_id: String,
    turn_index: i64,
    content: String,
) -> Result<Value, String> {
    let id = conversation_id.trim();
    if id.is_empty() {
        return Err("CHAT_INVALID_CONVERSATION: 会话标识不能为空".to_string());
    }
    store::messages::edit_turn(
        &p0.store,
        id,
        turn_index,
        &content,
        crate::bridge::event_store::now_ms() as i64,
    )?;
    Ok(Value::Object(serde_json::Map::from_iter([(
        "ok".to_string(),
        Value::Bool(true),
    )])))
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

/// 零配置本地模型：真实探测本机 Ollama（只接受回环地址，不把渲染层变成任意探测通道）
#[tauri::command]
async fn model_detect_local(base_url: Option<String>) -> Result<Value, String> {
    let resp = model::local::detect(base_url.as_deref()).await?;
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

/// 设置项落库后的系统行为联动（保持唤醒 / 开机自启 / 后台常驻 / 网络代理）
fn apply_setting_side_effect(p0: &P0Services, key: &str, value: &Value) -> Result<(), String> {
    match key {
        task::keep_awake::SETTING_KEY => p0.keep_awake.apply(bool_of_setting(value)),
        system::autostart::SETTING_KEY => system::autostart::apply(
            bool_of_setting(value),
            setting_bool(p0, background::SETTING_KEY),
        ),
        background::SETTING_KEY => {
            // 自启命令里的 --hidden 跟随后台常驻：已开启自启时立即改写启动项，
            // 否则用户会保留「开机弹出主窗口」的旧命令却以为已静默驻留
            if setting_bool(p0, system::autostart::SETTING_KEY) {
                system::autostart::apply(true, bool_of_setting(value))?;
            }
            Ok(())
        }
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

/// 一句话解析定时任务（纯本地规则，不调用模型；不支持的写法明确报错）
#[tauri::command]
async fn schedule_parse(text: String) -> Result<Value, String> {
    let parsed = task::schedule::parse_natural(&text)?;
    Ok(serde_json::to_value(parsed).unwrap_or(Value::Null))
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
        privacy: privacy_enabled(&services.store),
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

    // 结果推送：只影响「执行记录里的附加说明」，推送失败不改任务本身的成败。
    let (summary, push_failed) = if sched.notify_channel.trim().is_empty() {
        (summary, false)
    } else {
        let push_text = format!("【定时任务】{}\n{}", sched.name, summary);
        match crate::bridge::im::send(
            bridge,
            &sched.notify_channel,
            &sched.notify_target,
            &push_text,
        )
        .await
        {
            Ok(_) => (
                format!(
                    "{summary}；已推送到 {}（{}）",
                    sched.notify_channel, sched.notify_target
                ),
                false,
            ),
            Err(e) => (format!("{summary}；推送失败：{e}"), true),
        }
    };

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
        if run_status == task::schedule::RUN_SUCCEEDED && !push_failed {
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

/// 用系统默认应用打开本地产物（扩展名白名单；拒绝可执行/脚本类）
#[tauri::command]
async fn file_open_external(
    p0: State<'_, Arc<P0Services>>,
    path: String,
) -> Result<(), String> {
    file::service::open_external(&path)?;
    security::audit::record_quiet(
        &p0.store,
        security::audit::ACTION_FILE_READ,
        security::audit::SEV_INFO,
        &path,
        "用系统默认应用打开产物",
    );
    Ok(())
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
        .manage(harness::Hub::new())
        .manage(remote::new_state())
        .manage(background::new_state())
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

            // 后台常驻：托盘建成才允许关窗隐藏；托盘失败时保持「关窗即退出」的原行为
            setup_background(&app.handle().clone(), &services);

            // 品牌外观：自定义名称/图标在每次启动时恢复（窗口标题、任务栏与托盘图标）
            system::branding::apply_startup(&app.handle().clone(), &services.store);

            // 保持唤醒：恢复上次开启的状态（真实生效，失败仅告警不阻断启动）
            if keep_awake_requested(&services) {
                if let Err(e) = services.keep_awake.apply(true) {
                    eprintln!("[keep-awake] 启动恢复失败: {e}");
                }
            }

            // 网络代理：启动即发布生效配置，后续 Rust 出网与引擎子进程都读这一份
            proxy::publish(proxy::spec_or_system(&services.store));

            // 系统级沙箱：恢复上次设置；平台不支持时不写快照，避免引擎收到无效授权
            match security::policy::get_system_sandbox(&services.store) {
                Ok(cfg) => sandbox::set_active(
                    Some(cfg).filter(|c| c.enabled && sandbox::platform_supported()),
                ),
                Err(e) => eprintln!("[sandbox] 系统隔离设置读取失败，按未启用处理: {e}"),
            }

            // Hook 网关：引擎执行链路在触发点回到这里，由 Rust 拉起用户放行过的脚本
            // （配了脚本 ≠ 会执行：未放行的命令网关只如实回报「未执行」）
            bridge::hook_gate::start(Arc::clone(&services));

            // 电脑操控网关：引擎的截屏/键鼠请求回到这里，由 Rust 做档位门禁、确认与审计
            bridge::computer_gate::start(Arc::clone(&services));

            // 浏览器 AI+ 网关：引擎的会话申请与逐次放行回到这里
            bridge::browser_gate::start(Arc::clone(&services));

            // 逐工具审批网关：引擎在副作用工具执行前回到这里，由 Rust 弹原生确认框
            bridge::tool_gate::start(Arc::clone(&services));

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
            agent_start_workspace,
            engine_endpoint,
            chat_submit,
            chat_review,
            chat_checkpoint_query,
            chat_cancel,
            chat_history,
            chat_conversations,
            chat_messages,
            chat_feedback,
            chat_edit_turn,
            event_replay,
            model_list,
            model_add,
            model_update,
            model_delete,
            model_test,
            model_detect_local,
            model_set_default,
            model_usage,
            usage_stats,
            usage_report,
            settings_get,
            settings_set_many,
            keep_awake_status,
            bridge::tools::agent_builtin_tools,
            bridge::im::im_channels,
            bridge::im::im_configure,
            bridge::im::im_start,
            bridge::im::im_stop,
            bridge::im::im_forget,
            bridge::im::im_send,
            bridge::im::im_preferences_get,
            bridge::im::im_preferences_set,
            system::system_autostart_status,
            system::badge::badge_support,
            system::badge::badge_set,
            system::branding::branding_get,
            system::branding::branding_customize,
            system::task_window::open_task_window,
            proxy_status,
            schedule_list,
            schedule_parse,
            schedule_create,
            schedule_update,
            schedule_toggle,
            schedule_delete,
            schedule_runs,
            schedule_run_now,
            file_list,
            file_read,
            file_preview,
            file_open_external,
            file_stage_attachment,
            auth::auth_status,
            auth::auth_login,
            auth::auth_logout,
            auth::auth_server_probe,
            collab::collab_status,
            collab::collab_list_projects,
            collab::collab_create_project,
            collab::collab_project_detail,
            collab::collab_create_invite,
            collab::collab_list_invites,
            collab::collab_accept_invite,
            collab::collab_update_member_role,
            collab::collab_remove_member,
            collab::collab_transfer_owner,
            collab::collab_leave_project,
            collab::collab_create_task,
            collab::collab_update_task,
            collab::collab_add_comment,
            collab::collab_report_result,
            security::security_scan_data,
            security::security_get_domain_control,
            security::security_set_domain_control,
            security::compliance_status,
            security::compliance_update,
            security::compliance_check,
            security::sandbox_policy_get,
            security::sandbox_policy_set,
            security::sandbox_system_get,
            security::sandbox_system_set,
            security::sandbox_env_usage,
            security::sandbox_env_clear,
            security::system_permission_status,
            security::system_permission_open,
            security::audit_log_query,
            security::audit_log_export,
            workspace::workspace_list,
            workspace::workspace_create,
            workspace::workspace_capabilities,
            skill::skill_root,
            skill::skill_list,
            skill::skill_install,
            skill::skill_uninstall,
            skill::skill_toggle,
            suite::suite_root,
            suite::suite_list,
            suite::suite_install,
            suite::suite_uninstall,
            plugin::plugin_validate,
            plugin::plugin_template,
            market::market_config_get,
            market::market_config_set,
            market::market_skill_list,
            market::market_skill_install,
            market::market_suite_list,
            market::market_suite_install,
            market::market_trust_get,
            market::market_trust_add,
            market::market_trust_remove,
            knowledge::knowledge_list,
            knowledge::knowledge_create,
            knowledge::knowledge_update,
            knowledge::knowledge_delete,
            knowledge::knowledge_add_docs,
            knowledge::knowledge_remove_doc,
            knowledge::knowledge_open_dir,
            pptx::pptx_templates_list,
            pptx::pptx_template_import,
            pptx::pptx_template_delete,
            expert::experts_list,
            expert::experts_save,
            expert::experts_delete,
            align::hooks_get,
            align::hooks_set,
            align::hooks_test,
            align::hooks_grant,
            align::hooks_revoke,
            align::hooks_status,
            align::task_archive,
            align::task_archive_list,
            align::task_unarchive,
            align::task_archive_delete,
            align::task_archive_clear,
            align::computer_use_get_policy,
            align::computer_use_set_policy,
            computer::computer_use_capabilities,
            computer::computer_use_capture,
            remote::remote_desktop_capabilities,
            remote::remote_desktop_status,
            remote::remote_desktop_start,
            remote::remote_desktop_frame,
            remote::remote_desktop_stop,
            browser::browser_use_get_policy,
            browser::browser_use_set_policy,
            browser::browser_use_capabilities,
            browser::browser_use_start,
            browser::browser_use_stop,
            terminal::terminal_get_policy,
            terminal::terminal_set_policy,
            terminal::terminal_capabilities,
            terminal::terminal_open,
            terminal::terminal_input,
            terminal::terminal_read,
            terminal::terminal_close,
            terminal::terminal_ai,
            terminal::acp_get_config,
            terminal::acp_set_config,
            terminal::acp_status,
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
            mcp::mcp_list_tools,
            mcp::mcp_oauth_authorize,
            mcp::mcp_oauth_revoke
        ])
        .build(tauri::generate_context!())
        .expect("error while building tongtian-tauri")
        .run(|handle, event| match event {
            // 关掉最后一个窗口时默认会退出：开启后台常驻且托盘可用则拦截。
            // 显式退出（code=Some，如托盘「退出」）不拦，避免关不掉。
            tauri::RunEvent::ExitRequested { code, api, .. } => {
                let enabled = handle
                    .try_state::<Arc<P0Services>>()
                    .map(|s| setting_bool(&s, background::SETTING_KEY))
                    .unwrap_or(false);
                let (ready, quitting) = handle
                    .try_state::<Arc<background::BackgroundState>>()
                    .map(|s| (s.tray_ready(), s.is_quitting()))
                    .unwrap_or((false, true));
                if background::should_prevent_exit(enabled, ready, quitting, code) {
                    api.prevent_exit();
                }
            }
            // 退出时释放唤醒约束子进程，避免遗留孤儿进程
            tauri::RunEvent::Exit => {
                if let Some(services) = handle.try_state::<Arc<P0Services>>() {
                    services.keep_awake.stop();
                }
            }
            _ => {}
        });
}

/// 托盘与关窗驻留：托盘创建成功才置 tray_ready，关窗才允许隐藏。
/// 托盘失败（例如资源缺失）时保持「关窗退出」，不把窗口藏进没有入口的进程。
fn setup_background(app: &AppHandle, services: &Arc<P0Services>) {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let bg = app
        .state::<Arc<background::BackgroundState>>()
        .inner()
        .clone();

    let result = (|| -> tauri::Result<()> {
        let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
        let quit = MenuItem::with_id(app, "quit", "退出应用", true, None::<&str>)?;
        let menu = Menu::with_items(app, &[&show, &quit])?;

        let quit_state = Arc::clone(&bg);
        let mut builder = TrayIconBuilder::with_id("tongtian-main")
            .menu(&menu)
            .show_menu_on_left_click(false)
            .on_menu_event(move |app, event| match event.id().as_ref() {
                "show" => show_main_window(app),
                "quit" => {
                    // 显式退出：先置位，避免被关窗/ExitRequested 的驻留逻辑拦回来
                    quit_state.request_quit();
                    app.exit(0);
                }
                _ => {}
            })
            .on_tray_icon_event(|tray, event| {
                if let TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } = event
                {
                    show_main_window(tray.app_handle());
                }
            });
        if let Some(icon) = app.default_window_icon() {
            builder = builder.icon(icon.clone());
        }
        builder.build(app)?;
        Ok(())
    })();

    match result {
        Ok(()) => {
            bg.mark_tray_ready();
            // 开机自启带 --hidden：托盘就绪后直接驻留，不弹出主窗口
            if background::has_hidden_arg(std::env::args())
                && setting_bool(services, background::SETTING_KEY)
            {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
        }
        Err(e) => eprintln!("[background] 托盘创建失败，关闭窗口仍将退出: {e}"),
    }

    // 关窗隐藏只在「设置开启 + 托盘真实可用 + 非主动退出」三者同时成立时生效
    if let Some(window) = app.get_webview_window("main") {
        let state = Arc::clone(&bg);
        let handle = app.clone();
        let services = Arc::clone(services);
        window.on_window_event(move |event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let enabled = setting_bool(&services, background::SETTING_KEY);
                if background::should_hide_on_close(enabled, state.tray_ready(), state.is_quitting()) {
                    api.prevent_close();
                    if let Some(w) = handle.get_webview_window("main") {
                        let _ = w.hide();
                    }
                }
            }
        });
    }
}

/// 从托盘回到主窗口：显示、还原最小化、聚焦（窗口已被关闭时静默忽略）
fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
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
