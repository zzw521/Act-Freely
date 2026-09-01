//! AgentBridge — Python Agent 子进程生命周期与 HTTP/SSE 通信
//!
//! 移植自 Electron 版 bridge/agent-bridge.ts：
//! - 每个 workspace 独立子进程 + 独立端口（18000-19000）
//! - 启动后轮询 /health 直到就绪（最长 30s）
//! - 后台健康巡检：连续 3 次失败标记 stopped
//! - sendMessage: POST /api/chat（SSE 流）→ 解析 → tauri event "chat-event"

use std::collections::{HashMap, HashSet};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::io::AsyncBufReadExt;
use tokio::sync::Mutex;

use super::python_env::{agent_start_command, detect_agent, detect_python, ensure_workspace_dir, PythonInfo};
use super::sse::{adapt_to_chat_event, SseFeed};

// ── 常量（与 Electron 版保持一致） ─────────────────────────────

const PORT_RANGE_START: u16 = 18000;
const PORT_RANGE_END: u16 = 19000;
const HEALTH_CHECK_MAX_WAIT_MS: u128 = 30_000;
const HEALTH_CHECK_POLL_MS: u64 = 500;
const WATCH_INTERVAL_MS: u64 = 10_000;
const HEALTH_CHECK_MAX_FAILURES: u32 = 3;

// 三层架构演进 P0：事件管道/重放参数
/// emit 通道容量（背压上限，避免无界积压）
const EMIT_CHANNEL_CAPACITY: usize = 1024;
/// 断线补齐轮询间隔（GET /api/events/replay）
const CATCHUP_POLL_MS: u64 = 500;
/// 断线补齐总超时（引擎窗口 10min，桌面侧 30s 轮询预算足够）
const CATCHUP_TIMEOUT_MS: u64 = 30_000;

/// 共享 HTTP client（无锁，reqwest::Client 内部 Arc；移出全局 Mutex 热路径）
static SHARED_HTTP: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();

pub fn http_client() -> &'static reqwest::Client {
    SHARED_HTTP.get_or_init(reqwest::Client::new)
}

/// 前端事件名
pub const EVENT_CHAT: &str = "chat-event";
pub const EVENT_STATUS: &str = "agent-status";

// ── 数据结构 ─────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum InstanceStatus {
    Starting,
    Ready,
    Busy,
    Stopped,
    Error,
}

struct Instance {
    port: u16,
    pid: u32,
    status: InstanceStatus,
    health_fail_count: u32,
}

pub struct AgentBridge {
    python: Option<PythonInfo>,
    instances: HashMap<String, Instance>,
    used_ports: HashSet<u16>,
    http: reqwest::Client,
}

pub type BridgeState = Arc<Mutex<AgentBridge>>;

pub fn new_bridge_state() -> BridgeState {
    Arc::new(Mutex::new(AgentBridge {
        python: None,
        instances: HashMap::new(),
        used_ports: HashSet::new(),
        http: reqwest::Client::new(),
    }))
}

fn log(msg: &str) {
    println!("[bridge] {msg}");
}

/// 在 18000-19000 范围内找一个未被占用且可绑定的端口
async fn find_free_port(used: &HashSet<u16>) -> Option<u16> {
    for port in PORT_RANGE_START..PORT_RANGE_END {
        if used.contains(&port) {
            continue;
        }
        if tokio::net::TcpListener::bind(("127.0.0.1", port)).await.is_ok() {
            return Some(port);
        }
    }
    None
}

fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/health")
}

fn emit_status(app: &AppHandle, workspace_id: &str, status: InstanceStatus) {
    let _ = app.emit(
        EVENT_STATUS,
        json!({ "workspaceId": workspace_id, "status": status }),
    );
}

// ── 启动 workspace ───────────────────────────────────────────

/// 启动指定 workspace 的 Agent 实例，返回分配端口
pub async fn start_workspace(
    app: &AppHandle,
    bridge: &BridgeState,
    workspace_id: &str,
) -> Result<u16, String> {
    // 已存在且存活则直接复用
    {
        let b = bridge.lock().await;
        if let Some(inst) = b.instances.get(workspace_id) {
            if matches!(inst.status, InstanceStatus::Ready | InstanceStatus::Starting | InstanceStatus::Busy) {
                return Ok(inst.port);
            }
        }
    }

    // 探测 Python 与 Agent 安装
    let python = {
        let mut b = bridge.lock().await;
        if b.python.is_none() {
            b.python = detect_python().await;
        }
        b.python.clone()
    };
    let python = python.ok_or("BRIDGE_PYTHON_NOT_FOUND: 需要 Python 3.10+")?;

    // 引擎选择（Mock 开关 + 引擎模式）：
    // - TT_AGENT_MOCK=1      → Mock Agent（纯模拟事件，无 LLM 依赖）
    // - TT_ENGINE=qwenpaw    → 真实 qwenpaw（需已安装）
    // - 默认（agentscope）    → 自研 AgentScope 引擎（读项目根 .env 模型配置）
    let mock_mode = std::env::var("TT_AGENT_MOCK").map(|v| v == "1").unwrap_or(false);
    let engine = std::env::var("TT_ENGINE").unwrap_or_else(|_| "agentscope".into());

    // 分配端口 + 工作目录
    let (port, work_dir) = {
        let mut b = bridge.lock().await;
        let port = find_free_port(&b.used_ports)
            .await
            .ok_or("BRIDGE_NO_FREE_PORT: 18000-19000 范围内无可用端口")?;
        b.used_ports.insert(port);
        let work_dir = ensure_workspace_dir(workspace_id)
            .map_err(|e| format!("BRIDGE_WORKDIR_FAILED: {e}"))?;
        (port, work_dir)
    };

    // 构造启动命令（mock / agentscope / qwenpaw 三种模式）
    let args = if mock_mode {
        log("TT_AGENT_MOCK=1 → launching mock agent");
        super::python_env::mock_agent_command(&python.path, port, &work_dir)?
    } else if engine == "qwenpaw" {
        let agent = detect_agent(&python.path).await;
        if !agent.installed {
            return Err("BRIDGE_AGENT_NOT_FOUND: 请先 pip install qwenpaw".into());
        }
        log(&format!(
            "Python {} + qwenpaw {:?} detected",
            python.version, agent.version
        ));
        agent_start_command(&python.path, port, &work_dir)
    } else {
        log("TT_ENGINE=agentscope → launching agentscope engine");
        super::python_env::agentscope_engine_command(&python.path, port, &work_dir)?
    };

    // spawn 子进程
    log(&format!("spawning agent for {workspace_id} on port {port}: {}", args.join(" ")));

    let mut child = tokio::process::Command::new(&args[0])
        .args(&args[1..])
        .current_dir(&work_dir)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("BRIDGE_SPAWN_FAILED: {e}"))?;

    let pid = child.id().unwrap_or(0);

    // stdout/stderr 日志泵
    if let Some(stdout) = child.stdout.take() {
        let ws = workspace_id.to_string();
        tokio::spawn(async move { pump_logs(stdout, ws, false).await });
    }
    if let Some(stderr) = child.stderr.take() {
        let ws = workspace_id.to_string();
        tokio::spawn(async move { pump_logs(stderr, ws, true).await });
    }

    {
        let mut b = bridge.lock().await;
        b.instances.insert(
            workspace_id.to_string(),
            Instance {
                port,
                pid,
                status: InstanceStatus::Starting,
                health_fail_count: 0,
            },
        );
    }
    emit_status(app, workspace_id, InstanceStatus::Starting);

    // 等待健康检查通过（最长 30s）
    let deadline = Instant::now() + Duration::from_millis(HEALTH_CHECK_MAX_WAIT_MS as u64);
    let mut ready = false;
    while Instant::now() < deadline {
        if health_ok(&bridge.lock().await.http, port).await {
            ready = true;
            break;
        }
        // 进程提前退出则快速失败
        if !process_alive(pid) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(HEALTH_CHECK_POLL_MS)).await;
    }

    if !ready {
        let mut b = bridge.lock().await;
        if let Some(inst) = b.instances.get_mut(workspace_id) {
            inst.status = InstanceStatus::Error;
        }
        emit_status(app, workspace_id, InstanceStatus::Error);
        return Err("BRIDGE_START_TIMEOUT: Agent 健康检查超时（30s）".into());
    }

    {
        let mut b = bridge.lock().await;
        if let Some(inst) = b.instances.get_mut(workspace_id) {
            inst.status = InstanceStatus::Ready;
        }
    }
    emit_status(app, workspace_id, InstanceStatus::Ready);
    log(&format!("workspace {workspace_id} ready on port {port}"));

    // 后台巡检：健康检查 + 进程存活检测
    let watch_bridge = Arc::clone(bridge);
    let watch_app = app.clone();
    let watch_ws = workspace_id.to_string();
    tokio::spawn(async move {
        watch_loop(&watch_app, &watch_bridge, &watch_ws, pid, port).await;
    });

    Ok(port)
}

async fn pump_logs<R>(reader: R, workspace_id: String, is_err: bool)
where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
{
    let mut lines = tokio::io::BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim().is_empty() {
            continue;
        }
        if is_err {
            eprintln!("[agent:{workspace_id}] {line}");
        } else {
            println!("[agent:{workspace_id}] {line}");
        }
    }
}

async fn health_ok(client: &reqwest::Client, port: u16) -> bool {
    match tokio::time::timeout(Duration::from_secs(3), client.get(health_url(port)).send()).await {
        Ok(Ok(resp)) => resp.status().is_success(),
        _ => false,
    }
}

fn process_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    {
        std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}")])
            .output()
            .map(|o| String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()))
            .unwrap_or(false)
    }
    #[cfg(not(windows))]
    {
        std::path::Path::new(&format!("/proc/{pid}")).exists()
    }
}

/// 后台巡检循环
async fn watch_loop(app: &AppHandle, bridge: &BridgeState, workspace_id: &str, pid: u32, port: u16) {
    let mut interval = tokio::time::interval(Duration::from_millis(WATCH_INTERVAL_MS));
    interval.tick().await; // 首个 tick 立即返回，跳过

    loop {
        interval.tick().await;

        // 实例已被移除 → 退出巡检
        let exists = {
            let b = bridge.lock().await;
            b.instances.contains_key(workspace_id)
        };
        if !exists {
            return;
        }

        // 进程已死 → 标记 stopped
        if !process_alive(pid) {
            log(&format!("agent process {pid} exited for {workspace_id}"));
            let mut b = bridge.lock().await;
            b.used_ports.remove(&port);
            b.instances.remove(workspace_id);
            emit_status(app, workspace_id, InstanceStatus::Stopped);
            return;
        }

        // 健康检查
        let ok = {
            let b = bridge.lock().await;
            health_ok(&b.http, port).await
        };
        let mut b = bridge.lock().await;
        if let Some(inst) = b.instances.get_mut(workspace_id) {
            if ok {
                inst.health_fail_count = 0;
            } else {
                inst.health_fail_count += 1;
                log(&format!(
                    "health check failed for {workspace_id} ({}/{})",
                    inst.health_fail_count, HEALTH_CHECK_MAX_FAILURES
                ));
                if inst.health_fail_count >= HEALTH_CHECK_MAX_FAILURES {
                    inst.status = InstanceStatus::Error;
                    emit_status(app, workspace_id, InstanceStatus::Error);
                    return;
                }
            }
        }
    }
}

// ── 停止 workspace ───────────────────────────────────────────

pub async fn stop_workspace(bridge: &BridgeState, app: &AppHandle, workspace_id: &str) -> Result<(), String> {
    let removed = {
        let mut b = bridge.lock().await;
        b.used_ports.retain(|_| true); // keep ports of others
        let inst = b.instances.remove(workspace_id);
        if let Some(ref i) = inst {
            b.used_ports.remove(&i.port);
        }
        inst
    };

    match removed {
        Some(inst) => {
            kill_pid(inst.pid);
            emit_status(app, workspace_id, InstanceStatus::Stopped);
            log(&format!("workspace {workspace_id} stopped"));
            Ok(())
        }
        None => Err(format!("未找到 workspace 实例: {workspace_id}")),
    }
}

fn kill_pid(pid: u32) {
    if pid == 0 {
        return;
    }
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .output();
    }
    #[cfg(not(windows))]
    {
        let _ = std::process::Command::new("kill")
            .args(["-9", &pid.to_string()])
            .output();
    }
}

// ── 发送消息（SSE 流式） ─────────────────────────────────────

/// 三层架构演进 P0 上下文：事件持久化 + 配额计量 + 任务注册表（均可选，Mock/测试场景可缺省）
#[derive(Clone, Default)]
pub struct P0Context {
    pub events: Option<Arc<super::event_store::EventStore>>,
    pub quota: Option<Arc<crate::task::quota::QuotaGate>>,
    pub registry: Option<Arc<crate::task::registry::TaskRegistry>>,
}

/// 向 Agent 提交消息，流式消费 SSE 并通过 tauri event 转发 ChatEvent
pub async fn send_message(
    app: &AppHandle,
    bridge: &BridgeState,
    workspace_id: &str,
    task_id: &str,
    goal: &str,
    p0: P0Context,
) -> Result<(), String> {
    send_message_with_attachments(app, bridge, workspace_id, task_id, goal, Vec::new(), p0).await
}

/// 携带附件的提交（交互补设计 §3 附件模式：AttachmentMeta 透传引擎）
pub async fn send_message_with_attachments(
    app: &AppHandle,
    bridge: &BridgeState,
    workspace_id: &str,
    task_id: &str,
    goal: &str,
    attachments: Vec<Value>,
    p0: P0Context,
) -> Result<(), String> {
    // 取出端口并置 busy
    let port = {
        let mut b = bridge.lock().await;
        let inst = b
            .instances
            .get_mut(workspace_id)
            .ok_or_else(|| format!("workspace 未启动: {workspace_id}"))?;
        if inst.status == InstanceStatus::Error || inst.status == InstanceStatus::Stopped {
            return Err(format!("workspace 不可用: {:?}", inst.status));
        }
        inst.status = InstanceStatus::Busy;
        inst.port
    };
    emit_status(app, workspace_id, InstanceStatus::Busy);

    let result = stream_chat(app, workspace_id, task_id, goal, port, attachments, p0).await;

    // 恢复 ready
    {
        let mut b = bridge.lock().await;
        if let Some(inst) = b.instances.get_mut(workspace_id) {
            if inst.status == InstanceStatus::Busy {
                inst.status = InstanceStatus::Ready;
            }
        }
    }
    emit_status(app, workspace_id, InstanceStatus::Ready);

    result
}

/// seq 去重与 gap 检测守卫（三层架构演进 P0：重放场景防重复渲染）
struct SeqGuard {
    last: Option<u64>,
}

impl SeqGuard {
    fn new() -> Self {
        Self { last: None }
    }

    /// 返回 true 表示事件应放行；false 表示重复应丢弃
    fn accept(&mut self, seq: Option<u64>) -> bool {
        let Some(s) = seq else {
            return true; // 无 seq（旧引擎）：不去重
        };
        match self.last {
            Some(last) if s <= last => false, // 重复/乱序旧事件
            _ => {
                self.last = Some(s);
                true
            }
        }
    }

    /// 检测 gap：当前 seq 与上一个不连续（需触发重放补齐）
    fn has_gap(&self, seq: Option<u64>) -> bool {
        match (self.last, seq) {
            (Some(last), Some(s)) => s > last + 1,
            _ => false,
        }
    }

    fn last(&self) -> u64 {
        self.last.unwrap_or(0)
    }
}

/// 独立 emit 任务：解析循环与事件转发解耦（背压 + 批量 drain，
/// 避免逐事件 emit 阻塞 SSE 解析热路径）
fn spawn_emitter(
    app: AppHandle,
    mut rx: tokio::sync::mpsc::Receiver<Value>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some(first) = rx.recv().await {
            let _ = app.emit(EVENT_CHAT, first);
            // 批量 drain：一次性清空已积压事件，降低调度开销
            while let Ok(ev) = rx.try_recv() {
                let _ = app.emit(EVENT_CHAT, ev);
            }
        }
    })
}

/// 从引擎重放端点拉取 seq > from_seq 的事件（断线补齐/gap 修复）
async fn fetch_replay(port: u16, task_id: &str, from_seq: u64) -> Vec<super::sse::AgentEvent> {
    let url = format!(
        "http://127.0.0.1:{port}/api/events/replay?task_id={task_id}&from_seq={from_seq}"
    );
    let Ok(resp) = http_client().get(&url).send().await else {
        return Vec::new();
    };
    let Ok(body) = resp.json::<Value>().await else {
        return Vec::new();
    };
    body.get("events")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|e| {
                    let kind = e.get("type").and_then(|t| t.as_str())?.to_string();
                    let data = e.get("data").cloned().unwrap_or(Value::Null);
                    let seq = e.get("seq").and_then(|s| s.as_u64());
                    Some(super::sse::AgentEvent { kind, data, seq })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// 向引擎发送取消请求（POST /api/chat/cancel，三层架构演进 P0）
pub async fn cancel_task(
    bridge: &BridgeState,
    workspace_id: &str,
    task_id: &str,
) -> Result<Value, String> {
    let port = {
        let b = bridge.lock().await;
        b.instances
            .get(workspace_id)
            .map(|i| i.port)
            .ok_or_else(|| format!("workspace 未启动: {workspace_id}"))?
    };
    let resp = http_client()
        .post(format!("http://127.0.0.1:{port}/api/chat/cancel"))
        .json(&json!({ "task_id": task_id }))
        .send()
        .await
        .map_err(|e| format!("BRIDGE_COMM_FAILED: {e}"))?;
    resp.json::<Value>()
        .await
        .map_err(|e| format!("BRIDGE_CANCEL_PARSE: {e}"))
}

/// 处理解析出的事件：持久化 → gap 修复 → 去重 → 配额计量 → 适配 → 投递 emit 通道
async fn forward_events(
    events: Vec<super::sse::AgentEvent>,
    seq_guard: &mut SeqGuard,
    tx: &tokio::sync::mpsc::Sender<Value>,
    port: u16,
    task_id: &str,
    saw_done: &mut bool,
    p0: &P0Context,
) {
    for ev in events {
        if ev.kind == "done" {
            *saw_done = true;
        }
        // gap 检测先于 accept（accept 会更新 last）
        if seq_guard.has_gap(ev.seq) {
            let missed = fetch_replay(port, task_id, seq_guard.last()).await;
            for m in missed {
                if m.kind == "done" {
                    *saw_done = true;
                }
                if seq_guard.accept(m.seq) {
                    persist_and_meter(&m, task_id, p0, tx).await;
                    if let Some(chat) = adapt_to_chat_event(&m, task_id) {
                        let _ = tx.send(chat).await;
                    }
                }
            }
        }
        if !seq_guard.accept(ev.seq) {
            continue; // 重放重复事件丢弃
        }
        persist_and_meter(&ev, task_id, p0, tx).await;
        if let Some(chat) = adapt_to_chat_event(&ev, task_id) {
            let _ = tx.send(chat).await;
        }
    }
}

/// 事件持久化（event_store，24h）+ 配额计量（step_done/deliver 的 usage 字段）
async fn persist_and_meter(
    ev: &super::sse::AgentEvent,
    task_id: &str,
    p0: &P0Context,
    tx: &tokio::sync::mpsc::Sender<Value>,
) {
    if let Some(store) = &p0.events {
        store.append(task_id, &ev.kind, ev.seq, ev.data.clone());
    }
    if let Some(gate) = &p0.quota {
        // 从 usage 字段累计 token（promptTokens + completionTokens，兼容 snake_case）
        let tokens = ev
            .data
            .get("usage")
            .map(|u| {
                let prompt = u
                    .get("promptTokens")
                    .or_else(|| u.get("prompt_tokens"))
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let completion = u
                    .get("completionTokens")
                    .or_else(|| u.get("completion_tokens"))
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                prompt + completion
            })
            .unwrap_or(0);
        if tokens > 0 {
            if let crate::task::quota::TierTransition::Entered(snap) = gate.add_usage(tokens) {
                // 档位跃迁产出 quota_warning 事件（去抖：同档不重复）
                let _ = tx
                    .send(json!({
                        "type": "quota_warning",
                        "level": snap.tier,
                        "action": snap.action,
                        "usedTokens": snap.used_tokens,
                        "quotaTokens": snap.quota_tokens,
                        "taskId": task_id,
                    }))
                    .await;
                // 100% 阻断档：运行中任务转 budget_paused（转移 #17，BUDGET_EXHAUSTED）；
                // 预算恢复/配额重置后按入队 FIFO 回 executing（转移 #26，云端语义，
                // 本地 local-first 随跨日台账重置自然恢复）
                if snap.action == "block" {
                    if let Some(reg) = &p0.registry {
                        if let Some(record) = reg.get(task_id) {
                            // 活跃态均可暂停（本地流式执行期间状态为 planning，
                            // 云端语义下为 executing，两者均对应转移 #17）
                            let pausable = matches!(
                                record.status.as_str(),
                                "queued" | "planning" | "executing" | "delivering"
                            );
                            if pausable {
                                if let Some(updated) = reg.update_status(
                                    task_id,
                                    crate::task::status::BUDGET_PAUSED,
                                    Some("BUDGET_EXHAUSTED"),
                                ) {
                                    let _ = tx
                                        .send(json!({
                                            "type": "status_change",
                                            "taskId": task_id,
                                            "status": updated.status,
                                            "statusReasonCode": updated.status_reason_code,
                                        }))
                                        .await;
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

async fn stream_chat(
    app: &AppHandle,
    workspace_id: &str,
    task_id: &str,
    goal: &str,
    port: u16,
    attachments: Vec<Value>,
    p0: P0Context,
) -> Result<(), String> {
    // 两阶段协议（核心闭环补齐）：提交时仅生成计划待审阅（stage=plan），
    // approve 后经 review_task 进入执行流
    let _ = workspace_id;
    let body = json!({
        "message": goal,
        "task_id": task_id,
        "stage": "plan",
        "attachments": attachments,
    });
    stream_post(app, task_id, port, "/api/chat", body, p0).await
}

/// 审阅任务（approve 返回执行 SSE 流；reject/modify 返回短流），消费方式与 chat 一致
pub async fn review_task(
    app: &AppHandle,
    bridge: &BridgeState,
    workspace_id: &str,
    task_id: &str,
    action: &str,
    patch: Value,
    p0: P0Context,
) -> Result<(), String> {
    let port = {
        let b = bridge.lock().await;
        b.instances
            .get(workspace_id)
            .map(|i| i.port)
            .ok_or_else(|| format!("workspace 未启动: {workspace_id}"))?
    };
    let body = json!({ "task_id": task_id, "action": action, "patch": patch });
    stream_post(app, task_id, port, "/api/chat/review", body, p0).await
}

/// 任务控制转发（pause/resume/rollback_to，引擎返回 JSON）
pub async fn control_task(
    bridge: &BridgeState,
    workspace_id: &str,
    task_id: &str,
    action: &str,
    step_id: Option<&str>,
) -> Result<Value, String> {
    let port = {
        let b = bridge.lock().await;
        b.instances
            .get(workspace_id)
            .map(|i| i.port)
            .ok_or_else(|| format!("workspace 未启动: {workspace_id}"))?
    };
    let resp = http_client()
        .post(format!("http://127.0.0.1:{port}/api/chat/control"))
        .json(&json!({
            "task_id": task_id,
            "action": action,
            "step_id": step_id.unwrap_or(""),
        }))
        .send()
        .await
        .map_err(|e| format!("BRIDGE_COMM_FAILED: {e}"))?;
    resp.json::<Value>()
        .await
        .map_err(|e| format!("BRIDGE_CONTROL_PARSE: {e}"))
}

/// 通用 SSE POST 消费管道：独立 emit 任务 + seq 去重/gap 重放 + 断线补齐
async fn stream_post(
    app: &AppHandle,
    task_id: &str,
    port: u16,
    path: &str,
    body: Value,
    p0: P0Context,
) -> Result<(), String> {
    // 独立 emit 任务 + 有界通道（背压；解析不被前端消费速度拖慢）
    let (tx, rx) = tokio::sync::mpsc::channel::<Value>(EMIT_CHANNEL_CAPACITY);
    let emitter = spawn_emitter(app.clone(), rx);

    let client = http_client();
    let resp = client
        .post(format!("http://127.0.0.1:{port}{path}"))
        .header("Accept", "text/event-stream")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("BRIDGE_COMM_FAILED: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("BRIDGE_CHAT_HTTP_{}", resp.status().as_u16()));
    }

    let mut stream = resp.bytes_stream();
    let mut feed = SseFeed::new();
    let mut seq_guard = SeqGuard::new();
    let mut saw_done = false;
    let mut stream_broken = false;

    while let Some(chunk) = stream.next().await {
        match chunk {
            Ok(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let events = feed.feed(&text);
                forward_events(events, &mut seq_guard, &tx, port, task_id, &mut saw_done, &p0)
                    .await;
            }
            Err(e) => {
                // 断线：转入补齐循环（事件丢失=0 目标，由引擎重放窗口保障）
                log(&format!("stream broken for {task_id}: {e}"));
                stream_broken = true;
                break;
            }
        }
    }

    if !stream_broken {
        let rest = feed.finish();
        forward_events(rest, &mut seq_guard, &tx, port, task_id, &mut saw_done, &p0).await;
    }

    // 断线且未见 done：轮询重放端点补齐直至 done 或超时（桌面 local-first 降级路径）
    if stream_broken && !saw_done {
        let deadline = Instant::now() + Duration::from_millis(CATCHUP_TIMEOUT_MS);
        while Instant::now() < deadline && !saw_done {
            tokio::time::sleep(Duration::from_millis(CATCHUP_POLL_MS)).await;
            let events = fetch_replay(port, task_id, seq_guard.last()).await;
            if events.is_empty() {
                continue;
            }
            forward_events(events, &mut seq_guard, &tx, port, task_id, &mut saw_done, &p0)
                .await;
        }
    }

    drop(tx);
    let _ = emitter.await;

    // Agent 未显式发送 done 时兜底，确保前端退出 loading
    if !saw_done {
        let _ = app.emit(EVENT_CHAT, json!({ "type": "done", "taskId": task_id }));
    }

    Ok(())
}

#[cfg(test)]
mod p0_tests {
    use super::*;
    use crate::task::quota::QuotaGate;
    use crate::task::registry::TaskRegistry;

    /// 计划 F2 验收：100% 阻断档运行中任务转 budget_paused（转移 #17）
    #[tokio::test]
    async fn test_persist_and_meter_budget_paused_transition() {
        let registry = Arc::new(TaskRegistry::new());
        registry.create("t-budget", "default");
        // 本地流式执行期间状态为 planning（活跃态）
        registry.update_status("t-budget", crate::task::status::PLANNING, None);

        let gate = Arc::new(QuotaGate::new(None, 100)); // 配额 100 token，便于打满
        let p0 = P0Context {
            events: None,
            quota: Some(Arc::clone(&gate)),
            registry: Some(Arc::clone(&registry)),
        };
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Value>(16);

        // 用量直接打满 100%（跨越 70/85/95/100 四档阈值）
        let ev = super::super::sse::AgentEvent {
            kind: "step_done".into(),
            data: serde_json::json!({
                "step_id": "s1",
                "usage": { "promptTokens": 60, "completionTokens": 60 }
            }),
            seq: Some(1),
        };
        persist_and_meter(&ev, "t-budget", &p0, &tx).await;
        drop(tx);

        // 收到 quota_warning + status_change(budget_paused) 事件
        let mut saw_warning = false;
        let mut saw_paused = false;
        while let Some(msg) = rx.recv().await {
            match msg.get("type").and_then(|t| t.as_str()) {
                Some("quota_warning") => {
                    saw_warning = true;
                    assert_eq!(msg["action"], "block");
                }
                Some("status_change") => {
                    saw_paused = true;
                    assert_eq!(msg["status"], "budget_paused");
                    assert_eq!(msg["statusReasonCode"], "BUDGET_EXHAUSTED");
                }
                _ => {}
            }
        }
        assert!(saw_warning, "应产出 quota_warning 事件");
        assert!(saw_paused, "应产出 status_change(budget_paused) 事件");

        // 注册表状态已转移（转移 #17）
        let record = registry.get("t-budget").unwrap();
        assert_eq!(record.status, crate::task::status::BUDGET_PAUSED);
        assert_eq!(record.status_reason_code.as_deref(), Some("BUDGET_EXHAUSTED"));

        // 后续新提交被阻断（QUOTA_EXCEEDED）
        assert_eq!(gate.check_submit(), Err("QUOTA_EXCEEDED"));
    }

    /// 计划 D3/F2：事件持久化在 persist_and_meter 中生效
    #[tokio::test]
    async fn test_persist_and_meter_event_store() {
        let dir = std::env::temp_dir().join(format!("tt-agent-persist-test-{}", crate::bridge::event_store::now_ms()));
        let store = Arc::new(super::super::event_store::EventStore::new(dir));
        let p0 = P0Context {
            events: Some(Arc::clone(&store)),
            quota: None,
            registry: None,
        };
        let (tx, _rx) = tokio::sync::mpsc::channel::<Value>(16);
        let ev = super::super::sse::AgentEvent {
            kind: "done".into(),
            data: serde_json::json!({}),
            seq: Some(7),
        };
        persist_and_meter(&ev, "t-store", &p0, &tx).await;
        let replay = store.replay("t-store", 0);
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].seq, Some(7));
    }
}

// ── 状态查询 ─────────────────────────────────────────────────

pub async fn bridge_status(bridge: &BridgeState) -> Value {
    let b = bridge.lock().await;
    let instances: Value = b
        .instances
        .iter()
        .map(|(id, inst)| {
            json!({
                "workspaceId": id,
                "port": inst.port,
                "pid": inst.pid,
                "status": inst.status,
            })
        })
        .collect();

    let python = match &b.python {
        Some(p) => json!({ "path": p.path, "version": p.version }),
        None => Value::Null,
    };

    json!({ "python": python, "instances": instances })
}
