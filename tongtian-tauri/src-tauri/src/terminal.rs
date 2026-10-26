//! 终端 AI+ 与 ACP 的桌面代理。
//!
//! 原始 shell 是本项目权限最高的执行面之一，因此：
//! - 默认关闭；只有用户在设置里显式开启后才代理任何请求（配置 ≠ 授权）；
//! - 引擎端点只接受本进程生成的 `TT_TERMINAL_TOKEN`；浏览器直连、手工启动的引擎、
//!   IM 通道都拿不到令牌；
//! - 每次开关、打开会话、输入、AI 建议都写安全审计；输入正文与提问正文不落审计，
//!   只记来源（manual / ai）与字符数，避免口令被日志留存；
//! - ACP 委派同样默认关闭，启动命令只由用户填写，模型不能指定可执行文件。
//!
//! 审计分工：终端动作写桌面端 `audit_logs`；外部 ACP Agent 的协议级明细由引擎
//! 写入 `{home}/acp/audit.jsonl`（多次委派可追溯，且不含文件正文）。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

use crate::bridge::agent::{endpoint_of, BridgeState};
use crate::security::audit;
use crate::store::{settings, Store};

pub const ENABLED_KEY: &str = "terminalEnabled";
pub const ACP_ENABLED_KEY: &str = "acpEnabled";
pub const ACP_COMMAND_KEY: &str = "acpCommand";
pub const ACP_PERMISSION_KEY: &str = "acpPermission";
pub const ACP_TIMEOUT_KEY: &str = "acpTimeoutSeconds";

pub const ACP_PERMISSIONS: [&str; 2] = ["deny", "allow"];
pub const DEFAULT_ACP_PERMISSION: &str = "deny";
pub const DEFAULT_ACP_TIMEOUT: u64 = 600;
pub const MIN_ACP_TIMEOUT: u64 = 60;
pub const MAX_ACP_TIMEOUT: u64 = 1800;
pub const MAX_INPUT_CHARS: usize = 64 * 1024;

/// 引擎启动时注入的终端令牌。只存在于本进程与其子进程环境里，
/// 不存数据库、不写日志、不进渲染层。
static ENGINE_TOKEN: OnceLock<String> = OnceLock::new();

/// 会话 → 打开它的 workspace。输入/读取/关闭/AI 都路由回同一个引擎实例。
static SESSIONS: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();

fn sessions() -> &'static Mutex<HashMap<String, String>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn engine_token() -> &'static str {
    ENGINE_TOKEN.get_or_init(|| {
        format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        )
    })
}

fn home() -> std::path::PathBuf {
    let raw = std::env::var("USERPROFILE")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("HOME").ok().filter(|s| !s.trim().is_empty()))
        .unwrap_or_else(|| ".".to_string());
    std::path::PathBuf::from(raw)
}

/// 与浏览器/知识库同一套数据根目录约定
fn data_home() -> std::path::PathBuf {
    match std::env::var("TONGTIANWORK_HOME") {
        Ok(v) if !v.trim().is_empty() => std::path::PathBuf::from(v),
        _ => home().join(".tongtianwork"),
    }
}

pub fn acp_audit_path() -> std::path::PathBuf {
    data_home().join("acp").join("audit.jsonl")
}

// ── 终端开关 ─────────────────────────────────────────────────

pub fn read_enabled(store: &Store) -> Result<(bool, bool), String> {
    match settings::get(store, ENABLED_KEY)? {
        None => Ok((false, false)),
        Some(raw) => parse_bool(&raw).map(|v| (v, true)),
    }
}

fn parse_bool(raw: &str) -> Result<bool, String> {
    match raw.trim().trim_matches('"').to_ascii_lowercase().as_str() {
        "true" | "1" | "on" => Ok(true),
        "false" | "0" | "off" => Ok(false),
        other => Err(format!(
            "TERMINAL_POLICY_INVALID: 终端开关取值非法「{other}」，仅接受 true / false"
        )),
    }
}

pub fn set_enabled(store: &Store, enabled: bool) -> Result<(), String> {
    settings::set(store, ENABLED_KEY, if enabled { "true" } else { "false" })
}

fn require_enabled(store: &Store) -> Result<(), String> {
    match read_enabled(store) {
        Ok((true, _)) => Ok(()),
        Ok((false, _)) => Err(
            "TERMINAL_DISABLED: 终端 AI+ 未启用，请在「设置 → ACP 与终端」中显式开启".into(),
        ),
        Err(e) => Err(e),
    }
}

// ── ACP 配置 ─────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub struct AcpConfig {
    pub enabled: bool,
    pub command: String,
    pub permission: String,
    pub timeout_seconds: u64,
}

impl Default for AcpConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            command: String::new(),
            permission: DEFAULT_ACP_PERMISSION.to_string(),
            timeout_seconds: DEFAULT_ACP_TIMEOUT,
        }
    }
}

pub fn normalize_permission(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"').to_ascii_lowercase();
    if ACP_PERMISSIONS.contains(&v.as_str()) {
        Ok(v)
    } else {
        Err(format!(
            "ACP_PERMISSION_INVALID: ACP 审批策略非法「{v}」，仅支持 {}",
            ACP_PERMISSIONS.join(" / ")
        ))
    }
}

pub fn normalize_timeout(raw: u64) -> Result<u64, String> {
    if (MIN_ACP_TIMEOUT..=MAX_ACP_TIMEOUT).contains(&raw) {
        Ok(raw)
    } else {
        Err(format!(
            "ACP_TIMEOUT_INVALID: 超时时间需在 {MIN_ACP_TIMEOUT}–{MAX_ACP_TIMEOUT} 秒之间"
        ))
    }
}

pub fn read_acp(store: &Store) -> Result<(AcpConfig, bool), String> {
    let enabled_raw = settings::get(store, ACP_ENABLED_KEY)?;
    let (enabled, persisted) = match enabled_raw {
        None => (false, false),
        Some(raw) => (parse_bool(&raw)?, true),
    };
    let command = settings::get(store, ACP_COMMAND_KEY)?
        .unwrap_or_default()
        .trim()
        .trim_matches('"')
        .to_string();
    let permission = match settings::get(store, ACP_PERMISSION_KEY)? {
        None => DEFAULT_ACP_PERMISSION.to_string(),
        Some(raw) => normalize_permission(&raw)?,
    };
    let timeout_seconds = match settings::get(store, ACP_TIMEOUT_KEY)? {
        None => DEFAULT_ACP_TIMEOUT,
        Some(raw) => {
            let parsed = raw
                .trim()
                .trim_matches('"')
                .parse::<u64>()
                .map_err(|_| format!("ACP_TIMEOUT_INVALID: 超时时间不是整数「{raw}」"))?;
            normalize_timeout(parsed)?
        }
    };
    // 设置可能被手工写入：启用却没命令时按未启用处理，不能让引擎拿空命令去拉进程
    if enabled && command.is_empty() {
        return Err(
            "ACP_COMMAND_MISSING: ACP 委派已启用但没有启动命令，请在「设置 → ACP 与终端」填写"
                .into(),
        );
    }
    Ok((
        AcpConfig {
            enabled,
            command,
            permission,
            timeout_seconds,
        },
        persisted,
    ))
}

pub fn set_acp(store: &Store, cfg: &AcpConfig) -> Result<(), String> {
    let permission = normalize_permission(&cfg.permission)?;
    let timeout = normalize_timeout(cfg.timeout_seconds)?;
    if cfg.enabled && cfg.command.trim().is_empty() {
        return Err("ACP_COMMAND_REQUIRED: 启用 ACP 委派前必须填写外部 Agent 启动命令".into());
    }
    settings::set(store, ACP_ENABLED_KEY, if cfg.enabled { "true" } else { "false" })?;
    settings::set(store, ACP_COMMAND_KEY, cfg.command.trim())?;
    settings::set(store, ACP_PERMISSION_KEY, &permission)?;
    settings::set(store, ACP_TIMEOUT_KEY, &timeout.to_string())?;
    Ok(())
}

/// /api/chat 的 `runtime.acp` 段。读失败时 fail-closed，并带可读原因。
pub fn acp_block(store: Option<&Store>) -> Value {
    let Some(store) = store else {
        return json!({ "enabled": false, "persisted": false });
    };
    match read_acp(store) {
        Ok((cfg, persisted)) => json!({
            "enabled": cfg.enabled,
            "command": cfg.command,
            "permission": cfg.permission,
            "timeoutSeconds": cfg.timeout_seconds,
            "auditPath": acp_audit_path().to_string_lossy(),
            "persisted": persisted,
        }),
        Err(e) => json!({
            "enabled": false,
            "persisted": true,
            "auditPath": acp_audit_path().to_string_lossy(),
            "error": e,
        }),
    }
}

// ── 引擎 HTTP 代理 ───────────────────────────────────────────

async fn base_url(bridge: &BridgeState, workspace_id: Option<&str>) -> Result<String, String> {
    let endpoint = endpoint_of(bridge, workspace_id).await?;
    endpoint
        .get("baseUrl")
        .and_then(Value::as_str)
        .map(|s| s.to_string())
        .ok_or_else(|| "ENGINE_NOT_RUNNING: 引擎端点没有返回 baseUrl".to_string())
}

async fn post(
    base: &str,
    path: &str,
    payload: Value,
    timeout: Duration,
) -> Result<Value, String> {
    let resp = crate::proxy::loopback_client()
        .post(format!("{}{}", base.trim_end_matches('/'), path))
        .header("X-TT-Terminal-Token", engine_token())
        .timeout(timeout)
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("ENGINE_UNREACHABLE: 无法连接本机引擎：{e}"))?;
    read_response(resp).await
}

async fn get(base: &str, path: &str, query: &[(&str, String)]) -> Result<Value, String> {
    let resp = crate::proxy::loopback_client()
        .get(format!("{}{}", base.trim_end_matches('/'), path))
        .header("X-TT-Terminal-Token", engine_token())
        .query(query)
        .timeout(Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("ENGINE_UNREACHABLE: 无法连接本机引擎：{e}"))?;
    read_response(resp).await
}

async fn read_response(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let body = resp
        .json::<Value>()
        .await
        .unwrap_or_else(|_| json!({ "ok": false, "message": "引擎返回了无法解析的响应" }));
    if status.is_success() {
        return Ok(body);
    }
    let code = body
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("TERMINAL_FAILED");
    let message = body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("终端请求失败");
    Err(format!("{code}: {message}"))
}

fn session_workspace(session_id: &str) -> Result<Option<String>, String> {
    let map = sessions()
        .lock()
        .map_err(|_| "TERMINAL_STATE_FAILED: 终端会话表不可用".to_string())?;
    if !map.contains_key(session_id) {
        return Err("TERMINAL_SESSION_UNKNOWN: 本应用没有这个终端会话，可能已被关闭".into());
    }
    // 空串代表「默认工作区」，必须还原成 None 才能命中 instances 的默认项
    Ok(map.get(session_id).cloned().filter(|s| !s.is_empty()))
}

fn remember(session_id: &str, workspace_id: Option<&str>) {
    if let Ok(mut map) = sessions().lock() {
        map.insert(session_id.to_string(), workspace_id.unwrap_or("").to_string());
    }
}

fn forget(session_id: &str) {
    if let Ok(mut map) = sessions().lock() {
        map.remove(session_id);
    }
}

// ── Tauri commands：终端 ─────────────────────────────────────

#[tauri::command]
pub async fn terminal_get_policy(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let (enabled, persisted) = read_enabled(&p0.store)?;
    Ok(json!({ "enabled": enabled, "persisted": persisted }))
}

/// 开关终端。开启是高风险动作：必须由用户在设置页显式确认，并写审计。
#[tauri::command]
pub async fn terminal_set_policy(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    enabled: bool,
) -> Result<Value, String> {
    set_enabled(&p0.store, enabled)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        if enabled { audit::SEV_WARNING } else { audit::SEV_INFO },
        ENABLED_KEY,
        if enabled {
            "开启终端 AI+：允许本机 shell 会话与命令建议（输入仍需用户确认）"
        } else {
            "关闭终端 AI+"
        },
    );
    Ok(json!({ "ok": true, "enabled": enabled }))
}

/// 设置页/终端页的能力视图：开关、平台、shell、管道限制与 ACP 配置一次说清。
#[tauri::command]
pub async fn terminal_capabilities(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
) -> Result<Value, String> {
    let engine = endpoint_of(&bridge, None).await.ok();
    let engine_url = engine
        .as_ref()
        .and_then(|v| v.get("baseUrl"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let (enabled, persisted, policy_error) = match read_enabled(&p0.store) {
        Ok((v, p)) => (v, p, None),
        Err(e) => (false, true, Some(e)),
    };
    let acp = match read_acp(&p0.store) {
        Ok((cfg, persisted)) => json!({
            "enabled": cfg.enabled,
            "command": cfg.command,
            "permission": cfg.permission,
            "timeoutSeconds": cfg.timeout_seconds,
            "persisted": persisted,
            "auditPath": acp_audit_path().to_string_lossy(),
        }),
        Err(e) => json!({ "enabled": false, "persisted": true, "error": e }),
    };
    let reason = match &policy_error {
        Some(e) => format!("终端开关读取失败，已按关闭处理：{e}"),
        None if !enabled => "终端 AI+ 未启用".to_string(),
        None => String::new(),
    };
    Ok(json!({
        "enabled": enabled,
        "persisted": persisted,
        "policyError": policy_error,
        "platform": std::env::consts::OS,
        "shell": if cfg!(windows) {
            "cmd.exe".to_string()
        } else {
            std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".into())
        },
        "pipe": true,
        "maxSessions": 5,
        "inputLimit": MAX_INPUT_CHARS,
        "reason": reason,
        "engineRunning": engine_url.is_some(),
        "engineUrl": engine_url,
        "acp": acp,
    }))
}

#[tauri::command]
pub async fn terminal_open(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
    cwd: String,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    require_enabled(&p0.store)?;
    let path = std::path::Path::new(cwd.trim());
    if cwd.trim().is_empty() || !path.is_dir() {
        return Err(format!(
            "TERMINAL_CWD_INVALID: 工作目录不存在：{}",
            cwd.trim()
        ));
    }
    let base = base_url(&bridge, workspace_id.as_deref()).await?;
    let result = post(
        &base,
        "/api/terminal/open",
        json!({ "cwd": path.to_string_lossy() }),
        Duration::from_secs(20),
    )
    .await?;
    if let Some(sid) = result.get("sessionId").and_then(Value::as_str) {
        remember(sid, workspace_id.as_deref());
        audit::record_quiet(
            &p0.store,
            audit::ACTION_TERMINAL_SESSION,
            audit::SEV_WARNING,
            &path.to_string_lossy(),
            &format!(
                "打开终端会话 {}（shell={}，管道模式）",
                sid,
                result.get("shell").and_then(Value::as_str).unwrap_or("?")
            ),
        );
    }
    Ok(result)
}

#[tauri::command]
pub async fn terminal_input(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
    session_id: String,
    data: String,
    source: Option<String>,
) -> Result<Value, String> {
    require_enabled(&p0.store)?;
    if data.chars().count() > MAX_INPUT_CHARS {
        return Err(format!(
            "TERMINAL_INPUT_TOO_LARGE: 单次输入超过 {} 字符",
            MAX_INPUT_CHARS
        ));
    }
    let source = match source.as_deref() {
        Some("ai") => "ai",
        Some("manual") | None => "manual",
        Some(other) => {
            return Err(format!(
                "TERMINAL_SOURCE_INVALID: 输入来源非法「{other}」，仅支持 manual / ai"
            ))
        }
    };
    let ws = session_workspace(&session_id)?;
    let base = base_url(&bridge, ws.as_deref()).await?;
    let result = post(
        &base,
        "/api/terminal/input",
        json!({ "sessionId": session_id, "data": data }),
        Duration::from_secs(20),
    )
    .await?;
    // 只记来源与字符数：命令行里可能带口令/连接串，不把正文写进审计
    audit::record_quiet(
        &p0.store,
        audit::ACTION_TERMINAL_INPUT,
        if source == "ai" { audit::SEV_WARNING } else { audit::SEV_INFO },
        &session_id,
        &format!("终端输入（来源 {source}，{} 字符）", data.chars().count()),
    );
    Ok(result)
}

#[tauri::command]
pub async fn terminal_read(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
    session_id: String,
    offset: Option<u64>,
) -> Result<Value, String> {
    require_enabled(&p0.store)?;
    let ws = session_workspace(&session_id)?;
    let base = base_url(&bridge, ws.as_deref()).await?;
    get(
        &base,
        "/api/terminal/read",
        &[
            ("session", session_id),
            ("offset", offset.unwrap_or(0).to_string()),
        ],
    )
    .await
}

#[tauri::command]
pub async fn terminal_close(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
    session_id: String,
) -> Result<Value, String> {
    require_enabled(&p0.store)?;
    // 关会话比开更安全：即使本进程没有这条记录（例如界面刷新后），也允许尝试回收
    let ws = session_workspace(&session_id).unwrap_or(None);
    let base = base_url(&bridge, ws.as_deref()).await?;
    let result = post(
        &base,
        "/api/terminal/close",
        json!({ "sessionId": session_id }),
        Duration::from_secs(20),
    )
    .await?;
    forget(&session_id);
    audit::record_quiet(
        &p0.store,
        audit::ACTION_TERMINAL_SESSION,
        audit::SEV_INFO,
        &session_id,
        "关闭终端会话",
    );
    Ok(result)
}

/// AI 命令建议：只返回建议，不执行；出站脱敏档位由桌面端设置统一决定。
#[tauri::command]
pub async fn terminal_ai(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
    session_id: String,
    question: String,
    model_id: Option<String>,
) -> Result<Value, String> {
    require_enabled(&p0.store)?;
    if question.trim().is_empty() {
        return Err("TERMINAL_QUESTION_REQUIRED: 请先描述要解决的问题".into());
    }
    let pii_mode = match crate::security::pii::read_mode(&p0.store) {
        Ok((mode, _)) => mode,
        Err(e) => return Err(format!("PII_MODE_INVALID: 出站脱敏档位读取失败：{e}")),
    };
    let ws = session_workspace(&session_id)?;
    let base = base_url(&bridge, ws.as_deref()).await?;
    let result = post(
        &base,
        "/api/terminal/ai",
        json!({
            "sessionId": session_id,
            "question": question,
            "piiOutboundMode": pii_mode,
            "modelId": model_id.unwrap_or_default(),
        }),
        Duration::from_secs(180),
    )
    .await?;
    let suggestion = result.get("suggestion").cloned().unwrap_or_else(|| json!({}));
    let commands = suggestion
        .get("commands")
        .and_then(Value::as_array)
        .map(|v| v.len())
        .unwrap_or(0);
    audit::record_quiet(
        &p0.store,
        audit::ACTION_TERMINAL_AI,
        audit::SEV_INFO,
        &session_id,
        &format!(
            "终端 AI 建议：{} 条命令，风险 {}，脱敏 {}，提问 {} 字",
            commands,
            suggestion.get("risk").and_then(Value::as_str).unwrap_or("unknown"),
            pii_mode,
            question.chars().count(),
        ),
    );
    Ok(result)
}

// ── Tauri commands：ACP ──────────────────────────────────────

#[tauri::command]
pub async fn acp_get_config(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let (cfg, persisted) = read_acp(&p0.store)?;
    Ok(json!({
        "enabled": cfg.enabled,
        "command": cfg.command,
        "permission": cfg.permission,
        "timeoutSeconds": cfg.timeout_seconds,
        "persisted": persisted,
        "auditPath": acp_audit_path().to_string_lossy(),
        "permissions": ACP_PERMISSIONS,
    }))
}

#[tauri::command]
pub async fn acp_set_config(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    enabled: bool,
    command: String,
    permission: String,
    timeout_seconds: u64,
) -> Result<Value, String> {
    let cfg = AcpConfig {
        enabled,
        command,
        permission,
        timeout_seconds,
    };
    set_acp(&p0.store, &cfg)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_ACP_CONFIG,
        if enabled { audit::SEV_WARNING } else { audit::SEV_INFO },
        ACP_ENABLED_KEY,
        &format!(
            "ACP 委派{}（审批策略 {}，超时 {}s，命令 {}）",
            if enabled { "已启用" } else { "已关闭" },
            cfg.permission,
            cfg.timeout_seconds,
            cfg.command.trim()
        ),
    );
    Ok(json!({ "ok": true, "enabled": enabled }))
}

/// 设置页展示的 ACP 服务端启动命令 + 引擎地址。外部 IDE/终端据此接入。
#[tauri::command]
pub async fn acp_status(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    bridge: State<'_, BridgeState>,
) -> Result<Value, String> {
    let cfg = read_acp(&p0.store).map(|(c, _)| c).unwrap_or_default();
    let bridge_state = bridge.inner().clone();
    let engine = endpoint_of(&bridge_state, None).await;
    let engine_url = engine
        .as_ref()
        .ok()
        .and_then(|v| v.get("baseUrl"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let python = crate::bridge::python_env::detect_python().await;
    let mut reason = String::new();
    let command = match (&python, engine_url.as_deref()) {
        (Some(py), Some(url)) => match crate::bridge::python_env::acp_server_command(&py.path, Some(url)) {
            Ok(parts) => Some(crate::bridge::python_env::join_command(&parts)),
            Err(e) => {
                reason = e;
                None
            }
        },
        (Some(py), None) => match crate::bridge::python_env::acp_server_command(&py.path, None) {
            Ok(parts) => {
                reason = "引擎未运行：命令已生成，但需要先启动桌面端引擎才能接活".into();
                Some(crate::bridge::python_env::join_command(&parts))
            }
            Err(e) => {
                reason = e;
                None
            }
        },
        (None, _) => {
            reason = "未找到可用的 Python 运行时，无法生成 ACP 服务端命令".into();
            None
        }
    };
    Ok(json!({
        "enabled": cfg.enabled,
        "command": command,
        "engineUrl": engine_url,
        "engineRunning": engine_url.is_some(),
        "python": python.map(|p| p.path),
        "reason": reason,
        "protocolVersion": 1,
        "note": "ACP 服务端只做协议适配：模型与执行仍由本机引擎负责；不加载桌面端的意识/技能/专家运行时",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-terminal-{name}-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        (Store::open(dir.join("test.db")).unwrap(), dir)
    }

    #[test]
    fn test_terminal_policy_defaults_off_and_roundtrips() {
        let (store, dir) = temp_store("policy");
        let (enabled, persisted) = read_enabled(&store).unwrap();
        assert!(!enabled);
        assert!(!persisted);
        set_enabled(&store, true).unwrap();
        assert_eq!(read_enabled(&store).unwrap(), (true, true));
        set_enabled(&store, false).unwrap();
        assert_eq!(read_enabled(&store).unwrap(), (false, true));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_terminal_policy_rejects_garbage() {
        let (store, dir) = temp_store("policy-bad");
        settings::set(&store, ENABLED_KEY, "maybe").unwrap();
        let err = read_enabled(&store).unwrap_err();
        assert!(err.contains("TERMINAL_POLICY_INVALID"), "{err}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_acp_config_normalizes_and_requires_command() {
        let (store, dir) = temp_store("acp");
        assert_eq!(read_acp(&store).unwrap().0, AcpConfig::default());
        let mut cfg = AcpConfig {
            enabled: true,
            command: "npx -y @zed-industries/claude-code-acp".into(),
            permission: "ALLOW".into(),
            timeout_seconds: 600,
        };
        set_acp(&store, &cfg).unwrap();
        let saved = read_acp(&store).unwrap().0;
        assert!(saved.enabled);
        assert_eq!(saved.permission, "allow");
        cfg.command = "  ".into();
        assert!(set_acp(&store, &cfg).unwrap_err().contains("ACP_COMMAND_REQUIRED"));
        cfg.command = "npx x".into();
        cfg.permission = "sometimes".into();
        assert!(set_acp(&store, &cfg).unwrap_err().contains("ACP_PERMISSION_INVALID"));
        cfg.permission = "deny".into();
        cfg.timeout_seconds = 5;
        assert!(set_acp(&store, &cfg).unwrap_err().contains("ACP_TIMEOUT_INVALID"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_acp_block_is_fail_closed() {
        let direct = acp_block(None);
        assert_eq!(direct["enabled"], false);
        assert_eq!(direct["persisted"], false);

        let (store, dir) = temp_store("acp-block");
        let block = acp_block(Some(&store));
        assert_eq!(block["enabled"], false);
        assert!(block["auditPath"].as_str().unwrap().ends_with("audit.jsonl"));
        settings::set(&store, ACP_ENABLED_KEY, "wat").unwrap();
        let broken = acp_block(Some(&store));
        assert_eq!(broken["enabled"], false);
        assert!(broken["error"].as_str().unwrap().contains("TERMINAL_POLICY_INVALID"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_engine_token_is_stable_and_long() {
        let first = engine_token();
        let second = engine_token();
        assert_eq!(first, second);
        assert!(first.len() >= 32);
    }

    #[test]
    fn test_session_table_routes_and_forgets() {
        remember("term-test-1", Some("ws-a"));
        assert_eq!(session_workspace("term-test-1").unwrap().as_deref(), Some("ws-a"));
        remember("term-test-2", None);
        assert_eq!(session_workspace("term-test-2").unwrap(), None);
        forget("term-test-1");
        assert!(session_workspace("term-test-1").is_err());
        forget("term-test-2");
    }
}
