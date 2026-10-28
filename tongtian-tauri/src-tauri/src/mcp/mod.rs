//! MCP 连接器：服务器注册表 + 真实 JSON-RPC 握手
//!
//! 契约（`mcp:listServers|addServer|removeServer|connect|disconnect|listTools`）：
//! - 配置以 SQLite `mcp_servers` 表为事实来源（V6 增量建表，不改动存量表）
//! - 「连接」= 按配置真实发起 MCP 握手（`initialize` → `initialized` → `tools/list`），
//!   成功则缓存工具清单并置 connected，失败则置 failed 并留存服务器返回的错误摘要
//! - 「断开」= 清除已验证状态与工具缓存；探测进程随探测结束退出，不驻留后台进程
//! - 远程 Streamable HTTP 支持 OAuth 2.0（授权码 + PKCE），令牌只存系统凭据库；
//!   授权配置（auth_json）可展示，令牌/授权码不进 IPC、不进日志
//!
//! 安全边界：
//! - `env` / `headers` 的值只在本机存储并注入子进程/请求头，序列化给渲染层时只给键名，
//!   避免密钥经由 IPC 进入 WebView（可从 DevTools 读到）
//! - SQL 全部走参数化；子进程参数以数组传递，不经 Shell 解析

pub mod oauth;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, State};
use tauri_plugin_opener::OpenerExt;

use crate::security::audit;
use crate::store::Store;

pub const TRANSPORT_STDIO: &str = "stdio";
pub const TRANSPORT_STREAMABLE_HTTP: &str = "streamable_http";
pub const TRANSPORT_SSE: &str = "sse";

pub const STATUS_OFFLINE: &str = "offline";
pub const STATUS_CONNECTED: &str = "connected";
pub const STATUS_FAILED: &str = "failed";

/// 与主流 MCP 服务器兼容的协议版本（服务器不支持时会返回自己的版本）
pub const PROTOCOL_VERSION: &str = "2024-11-05";
pub const CLIENT_NAME: &str = "tongtian-office";

const DEFAULT_TIMEOUT_SECS: i64 = 60;
const MAX_TIMEOUT_SECS: i64 = 600;

/// 新增服务器入参（与渲染层 `McpAddReq` 同口径，camelCase）
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct AddServer {
    pub name: String,
    #[serde(default)]
    pub transport: Option<String>,
    #[serde(default)]
    pub command: Option<String>,
    #[serde(default)]
    pub args: Option<Vec<String>>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub env: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub headers: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub timeout_secs: Option<i64>,
    /// 远程连接器的 OAuth 配置（仅 Streamable HTTP）；只含非敏感参数
    #[serde(default)]
    pub auth: Option<oauth::OAuthConfig>,
}

/// 服务器行（渲染层 `McpServer`）：只带 env/header 的键名，不带值
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerRow {
    pub server_id: String,
    pub name: String,
    pub transport: String,
    pub command: String,
    pub args: Vec<String>,
    pub url: String,
    pub env_keys: Vec<String>,
    pub header_keys: Vec<String>,
    pub timeout_secs: i64,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    pub tools: Vec<ToolRow>,
    pub connected: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub probed_at_ms: Option<i64>,
    /// 非敏感 OAuth 配置（授权地址 / token 地址 / client_id / scopes）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auth: Option<oauth::OAuthConfig>,
    /// none / authorized / expired（只反映系统凭据库里是否有可用令牌）
    pub auth_status: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

/// MCP 工具行（渲染层 `McpTool`）
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ToolRow {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
}

/// 连接探测所需的完整配置（含 env/headers 的值，只在 Rust 侧使用）
#[derive(Debug, Clone)]
pub struct ProbeTarget {
    pub server_id: String,
    pub name: String,
    pub transport: String,
    pub command: String,
    pub args: Vec<String>,
    pub url: String,
    pub env: BTreeMap<String, String>,
    pub headers: BTreeMap<String, String>,
    pub timeout_secs: i64,
    pub auth: Option<oauth::OAuthConfig>,
    /// 当前可用访问令牌（仅 Rust 侧使用，不进列表视图）
    pub access_token: Option<String>,
}

// ── 校验与规范化 ──────────────────────────────────────────────

/// 传输类型：接受 UI 的 STDIO/SSE/STREAMABLE_HTTP 写法
pub fn normalize_transport(raw: Option<&str>) -> Result<String, String> {
    let v = raw.unwrap_or(TRANSPORT_STDIO).trim().to_lowercase();
    match v.replace('-', "_").as_str() {
        "" | "stdio" => Ok(TRANSPORT_STDIO.to_string()),
        "sse" => Ok(TRANSPORT_SSE.to_string()),
        "streamable_http" | "streamablehttp" | "http" => Ok(TRANSPORT_STREAMABLE_HTTP.to_string()),
        other => Err(format!(
            "MCP_INVALID_TRANSPORT: 不支持的传输类型「{other}」，仅支持 stdio / streamable_http / sse"
        )),
    }
}

fn normalize_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    if name.is_empty() {
        return Err("MCP_INVALID_NAME: 连接器名称不能为空".to_string());
    }
    if name.chars().count() > 60 {
        return Err("MCP_INVALID_NAME: 连接器名称过长（不超过 60 字）".to_string());
    }
    Ok(name.to_string())
}

fn normalize_url(raw: &str) -> Result<String, String> {
    let url = raw.trim();
    if url.is_empty() {
        return Err("MCP_INVALID_URL: 服务器地址不能为空".to_string());
    }
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return Err("MCP_INVALID_URL: 服务器地址必须以 http:// 或 https:// 开头".to_string());
    }
    if url.chars().any(char::is_whitespace) {
        return Err("MCP_INVALID_URL: 服务器地址不能包含空格".to_string());
    }
    Ok(url.to_string())
}

fn normalize_command(raw: &str) -> Result<String, String> {
    let cmd = raw.trim();
    if cmd.is_empty() {
        return Err("MCP_INVALID_COMMAND: stdio 连接器必须填写启动命令".to_string());
    }
    if cmd.contains('\n') || cmd.contains('\r') {
        return Err("MCP_INVALID_COMMAND: 启动命令不能包含换行".to_string());
    }
    Ok(cmd.to_string())
}

fn normalize_timeout(raw: Option<i64>) -> i64 {
    match raw {
        None => DEFAULT_TIMEOUT_SECS,
        Some(v) if v <= 0 => DEFAULT_TIMEOUT_SECS,
        Some(v) => v.min(MAX_TIMEOUT_SECS),
    }
}

fn normalize_args(raw: Option<&Vec<String>>) -> Vec<String> {
    raw.map(|list| {
        list.iter()
            .map(|a| a.to_string())
            .filter(|a| !a.trim().is_empty())
            .collect()
    })
    .unwrap_or_default()
}

fn normalize_map(raw: Option<&BTreeMap<String, String>>) -> BTreeMap<String, String> {
    raw.cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|(k, _)| !k.trim().is_empty())
        .collect()
}

fn is_secret_free_map(map: &BTreeMap<String, String>) -> bool {
    !map.values().any(|v| v.contains('\n') || v.contains('\r'))
}

/// OAuth 只适用于 Streamable HTTP；其他传输显式配置 OAuth 视为配置错误
fn normalize_auth(
    transport: &str,
    raw: Option<&oauth::OAuthConfig>,
) -> Result<Option<oauth::OAuthConfig>, String> {
    match (transport, raw) {
        (TRANSPORT_STREAMABLE_HTTP, Some(cfg)) => oauth::normalize_config(cfg).map(Some),
        (_, Some(_)) => Err(
            "MCP_OAUTH_INVALID_CONFIG: OAuth 仅支持 Streamable HTTP 连接器".to_string(),
        ),
        (_, None) => Ok(None),
    }
}

// ── 注册表读写 ────────────────────────────────────────────────

/// 新增连接器：校验后落库，重名拒绝（不覆盖既有配置）
pub fn add(store: &Store, req: &AddServer, now_ms: i64) -> Result<ServerRow, String> {
    let name = normalize_name(&req.name)?;
    let transport = normalize_transport(req.transport.as_deref())?;
    let command = match transport.as_str() {
        TRANSPORT_STDIO => normalize_command(req.command.as_deref().unwrap_or_default())?,
        _ => req.command.as_deref().unwrap_or("").trim().to_string(),
    };
    let url = match transport.as_str() {
        TRANSPORT_STDIO => String::new(),
        _ => normalize_url(req.url.as_deref().unwrap_or_default())?,
    };
    let args = normalize_args(req.args.as_ref());
    let env = normalize_map(req.env.as_ref());
    let headers = normalize_map(req.headers.as_ref());
    if !is_secret_free_map(&env) || !is_secret_free_map(&headers) {
        return Err("MCP_INVALID_HEADER: 环境变量与请求头不能包含换行".to_string());
    }
    let timeout_secs = normalize_timeout(req.timeout_secs);
    let auth = normalize_auth(&transport, req.auth.as_ref())?;
    if auth.is_some() && headers.keys().any(|k| k.eq_ignore_ascii_case("authorization")) {
        return Err(
            "MCP_OAUTH_INVALID_CONFIG: 启用 OAuth 时不能再配置 Authorization 请求头，否则会覆盖自动附加的令牌"
                .to_string(),
        );
    }
    let auth_json =
        serde_json::to_string(&auth).map_err(|e| format!("MCP_SAVE_FAILED: {e}"))?;
    let server_id = format!("mcp-{}", uuid::Uuid::new_v4());

    store.with_conn(|c| {
        let dup: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM mcp_servers WHERE name = ?1",
                [name.as_str()],
                |r| r.get(0),
            )
            .map_err(|e| format!("MCP_QUERY_FAILED: {e}"))?;
        if dup > 0 {
            return Err(format!(
                "MCP_SERVER_EXISTS: 已存在同名连接器「{name}」，请先删除或换个名称"
            ));
        }
        c.execute(
            "INSERT INTO mcp_servers (server_id, name, transport, command, args_json, url, \
             env_json, headers_json, timeout_secs, status, last_error, tools_json, probed_at_ms, \
             created_at_ms, updated_at_ms, auth_json) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, NULL, '[]', NULL, ?11, ?11, ?12)",
            rusqlite::params![
                server_id,
                name,
                transport,
                command,
                serde_json::to_string(&args).unwrap_or_else(|_| "[]".to_string()),
                url,
                serde_json::to_string(&env).unwrap_or_else(|_| "{}".to_string()),
                serde_json::to_string(&headers).unwrap_or_else(|_| "{}".to_string()),
                timeout_secs,
                STATUS_OFFLINE,
                now_ms,
                auth_json,
            ],
        )
        .map_err(|e| format!("MCP_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;

    get(store, &server_id)?
        .ok_or_else(|| format!("MCP_SAVE_FAILED: 连接器「{server_id}」写入后未能读回"))
}

/// 全部连接器（按名称稳定排序）
pub fn list(store: &Store) -> Result<Vec<ServerRow>, String> {
    let rows = store.with_conn(|c| {
        let mut stmt = c
            .prepare(&format!(
                "SELECT {COLUMNS} FROM mcp_servers ORDER BY name"
            ))
            .map_err(|e| format!("MCP_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], row_to_server)
            .map_err(|e| format!("MCP_QUERY_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("MCP_QUERY_FAILED: {e}"))
    })?;
    Ok(enrich_auth(store, rows))
}

/// 单个连接器
pub fn get(store: &Store, server_id: &str) -> Result<Option<ServerRow>, String> {
    let id = server_id.trim();
    if id.is_empty() {
        return Err("MCP_INVALID_ID: 缺少连接器标识".to_string());
    }
    let row = store.with_conn(|c| {
        c.query_row(
            &format!("SELECT {COLUMNS} FROM mcp_servers WHERE server_id = ?1"),
            [id],
            row_to_server,
        )
        .map(Some)
        .or_else(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                Ok(None)
            } else {
                Err(format!("MCP_QUERY_FAILED: {e}"))
            }
        })
    })?;
    Ok(row.map(|mut r| {
        r.auth_status = auth_status_for(store, &r);
        r
    }))
}

/// 授权状态需要读系统凭据库，放在 DB 锁之外补全，避免在 with_conn 内嵌套访问
fn enrich_auth(store: &Store, mut rows: Vec<ServerRow>) -> Vec<ServerRow> {
    for row in &mut rows {
        row.auth_status = auth_status_for(store, row);
    }
    rows
}

fn auth_status_for(store: &Store, row: &ServerRow) -> String {
    if row.auth.is_some() {
        oauth::status_for(store, &row.server_id, crate::now_ms_u64() as i64).to_string()
    } else {
        oauth::AUTH_NONE.to_string()
    }
}

/// 探测目标（含 env/headers 的值；仅在 Rust 侧使用，不返回给渲染层）
pub fn probe_target(store: &Store, server_id: &str) -> Result<ProbeTarget, String> {
    let id = server_id.trim();
    if id.is_empty() {
        return Err("MCP_INVALID_ID: 缺少连接器标识".to_string());
    }
    let mut target = store.with_conn(|c| {
        c.query_row(
            "SELECT server_id, name, transport, command, args_json, url, env_json, headers_json, \
             timeout_secs, auth_json \
             FROM mcp_servers WHERE server_id = ?1",
            [id],
            |r| {
                let auth_raw: String = r.get(9)?;
                Ok(ProbeTarget {
                    server_id: r.get(0)?,
                    name: r.get(1)?,
                    transport: r.get(2)?,
                    command: r.get(3)?,
                    args: serde_json::from_str(r.get::<_, String>(4)?.as_str()).unwrap_or_default(),
                    url: r.get(5)?,
                    env: serde_json::from_str(r.get::<_, String>(6)?.as_str()).unwrap_or_default(),
                    headers: serde_json::from_str(r.get::<_, String>(7)?.as_str())
                        .unwrap_or_default(),
                    timeout_secs: r.get(8)?,
                    auth: oauth::parse_stored_config(&auth_raw),
                    access_token: None,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => {
                format!("MCP_SERVER_NOT_FOUND: 连接器不存在：{id}")
            }
            other => format!("MCP_QUERY_FAILED: {other}"),
        })
    })?;
    if target.auth.is_some() {
        target.access_token = oauth::load_token(store, &target.server_id).map(|t| t.access_token);
    }
    Ok(target)
}

/// 删除连接器配置（仅删这一行，不影响任务/审计等存量数据）
pub fn remove(store: &Store, server_id: &str) -> Result<bool, String> {
    let id = server_id.trim();
    if id.is_empty() {
        return Err("MCP_INVALID_ID: 缺少连接器标识".to_string());
    }
    store.with_conn(|c| {
        c.execute("DELETE FROM mcp_servers WHERE server_id = ?1", [id])
            .map(|n| n > 0)
            .map_err(|e| format!("MCP_SAVE_FAILED: {e}"))
    })
}

/// 记录连接结果：成功缓存工具清单，失败保留错误摘要
pub fn record_probe(
    store: &Store,
    server_id: &str,
    status: &str,
    last_error: Option<&str>,
    tools: &[ToolRow],
    now_ms: i64,
) -> Result<ServerRow, String> {
    let id = server_id.trim();
    if id.is_empty() {
        return Err("MCP_INVALID_ID: 缺少连接器标识".to_string());
    }
    store.with_conn(|c| {
        let changed = c
            .execute(
                "UPDATE mcp_servers SET status = ?2, last_error = ?3, tools_json = ?4, \
                 probed_at_ms = ?5, updated_at_ms = ?5 WHERE server_id = ?1",
                rusqlite::params![
                    id,
                    status,
                    last_error.map(str::trim).filter(|s| !s.is_empty()),
                    serde_json::to_string(tools).unwrap_or_else(|_| "[]".to_string()),
                    now_ms,
                ],
            )
            .map_err(|e| format!("MCP_SAVE_FAILED: {e}"))?;
        if changed == 0 {
            return Err(format!("MCP_SERVER_NOT_FOUND: 连接器不存在：{id}"));
        }
        Ok(())
    })?;
    get(store, id)?.ok_or_else(|| format!("MCP_SERVER_NOT_FOUND: 连接器不存在：{id}"))
}

/// 断开：清除已验证状态与工具缓存（配置本身保留，随时可再连）
pub fn disconnect(store: &Store, server_id: &str, now_ms: i64) -> Result<ServerRow, String> {
    record_probe(store, server_id, STATUS_OFFLINE, None, &[], now_ms)
}

/// 缓存的工具清单（未连接时为空数组，不伪造工具）
pub fn tools_of(store: &Store, server_id: &str) -> Result<Vec<ToolRow>, String> {
    get(store, server_id)?.map(|r| r.tools).ok_or_else(|| {
        format!(
            "MCP_SERVER_NOT_FOUND: 连接器不存在：{}",
            server_id.trim()
        )
    })
}

/// 执行面消费的连接器：只下发「连接」验证过的服务器，带 env/headers 的真实取值
///
/// 与 `list` 的分工：列表页只给键名（密钥不经 IPC 进 WebView），这里给值，因为引擎
/// 要靠它拉起 stdio 子进程或带鉴权头请求 —— 与 `/api/chat` 里的 model_config.apiKey
/// 属同一信任边界（只发给 127.0.0.1 上的随包子进程）。
/// 未连接的连接器不下发：配置存在 ≠ 工具可用，不替用户启动没验证过的进程。
pub fn for_runtime(store: &Store) -> Result<Vec<Value>, String> {
    let now = crate::now_ms_u64() as i64;
    let mut out = Vec::new();
    for r in list(store)?.iter().filter(|r| r.connected) {
        let t = probe_target(store, &r.server_id)?;
        let mut headers = t.headers.clone();
        if t.auth.is_some() {
            // 授权缺失/过期时不下发：引擎不能带失效凭据去请求，也不能静默跳过鉴权
            let Some(token) = oauth::runtime_authorized(store, &r.server_id, now) else {
                continue;
            };
            let (key, value) = oauth::bearer_header(&token);
            headers.insert(key, value);
        }
        out.push(json!({
            "serverId": r.server_id,
            "name": r.name,
            "transport": t.transport,
            "command": t.command,
            "args": t.args,
            "url": t.url,
            "env": t.env,
            "headers": headers,
            "timeoutSecs": t.timeout_secs,
            "tools": r.tools,
        }));
    }
    Ok(out)
}

const COLUMNS: &str = "server_id, name, transport, command, args_json, url, env_json, headers_json, \
             timeout_secs, status, last_error, tools_json, probed_at_ms, created_at_ms, updated_at_ms, \
             auth_json";

fn row_to_server(r: &rusqlite::Row) -> rusqlite::Result<ServerRow> {
    let status: String = r.get(9)?;
    let auth_raw: String = r.get(15)?;
    Ok(ServerRow {
        server_id: r.get(0)?,
        name: r.get(1)?,
        transport: r.get(2)?,
        command: r.get(3)?,
        args: parse_string_list(&r.get::<_, String>(4)?),
        url: r.get(5)?,
        env_keys: json_keys(&r.get::<_, String>(6)?),
        header_keys: json_keys(&r.get::<_, String>(7)?),
        timeout_secs: r.get(8)?,
        status: status.clone(),
        last_error: r.get(10)?,
        tools: parse_tools(&r.get::<_, String>(11)?),
        connected: status == STATUS_CONNECTED,
        probed_at_ms: r.get(12)?,
        auth: oauth::parse_stored_config(&auth_raw),
        auth_status: oauth::AUTH_NONE.to_string(),
        created_at_ms: r.get(13)?,
        updated_at_ms: r.get(14)?,
    })
}

fn parse_string_list(raw: &str) -> Vec<String> {
    serde_json::from_str(raw).unwrap_or_default()
}

fn json_keys(raw: &str) -> Vec<String> {
    serde_json::from_str::<BTreeMap<String, String>>(raw)
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default()
}

fn parse_tools(raw: &str) -> Vec<ToolRow> {
    serde_json::from_str(raw).unwrap_or_default()
}

/// 探测请求体（纯函数，便于单测覆盖协议字段）
pub fn initialize_request(id: i64) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": { "name": CLIENT_NAME, "version": env!("CARGO_PKG_VERSION") }
        }
    })
}

pub fn initialized_notification() -> Value {
    json!({ "jsonrpc": "2.0", "method": "notifications/initialized" })
}

pub fn tools_list_request(id: i64) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "method": "tools/list", "params": {} })
}

/// 响应 → 工具清单；服务器返回 JSON-RPC error 时如实转为可读错误码
pub fn tools_of_response(resp: &Value) -> Result<Vec<ToolRow>, String> {
    if let Some(err) = resp.get("error").filter(|e| !e.is_null()) {
        let msg = err
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("服务器返回未知错误");
        return Err(format!("MCP_PROTOCOL_ERROR: {msg}"));
    }
    let tools = resp
        .get("result")
        .and_then(|r| r.get("tools"))
        .and_then(|t| t.as_array())
        .ok_or_else(|| {
            "MCP_PROTOCOL_ERROR: 服务器响应缺少 result.tools 字段".to_string()
        })?;
    let mut out = Vec::with_capacity(tools.len());
    for t in tools {
        let name = t
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .trim()
            .to_string();
        if name.is_empty() {
            continue; // 无名工具不可调用，直接丢弃而不是展示成空条目
        }
        out.push(ToolRow {
            name,
            description: t
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            input_schema: t
                .get("inputSchema")
                .cloned()
                .unwrap_or_else(|| json!({ "type": "object", "properties": {} })),
        });
    }
    Ok(out)
}

/// Streamable HTTP 响应可能是 application/json，也可能是 text/event-stream 帧
pub fn json_from_body(content_type: &str, body: &str) -> Result<Value, String> {
    if content_type.contains("text/event-stream") || body.contains("data:") {
        for line in body.lines() {
            let line = line.trim_start_matches("data:").trim();
            if line.is_empty() || line == "[DONE]" {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<Value>(line) {
                if v.get("jsonrpc").is_some() {
                    return Ok(v);
                }
            }
        }
        return Err("MCP_PROTOCOL_ERROR: SSE 响应中没有 JSON-RPC 消息".to_string());
    }
    serde_json::from_str::<Value>(body)
        .map_err(|e| format!("MCP_PROTOCOL_ERROR: 响应不是合法 JSON：{e}"))
}

/// 子进程命令行（Windows 下 npx/npm 等是 .cmd，需经 cmd /C 解析；其余平台直接 exec）
pub fn spawn_plan(command: &str, args: &[String]) -> (String, Vec<String>) {
    #[cfg(windows)]
    {
        let lower = command.to_lowercase();
        if lower.ends_with(".exe") || std::path::Path::new(command).is_absolute() {
            return (command.to_string(), args.to_vec());
        }
        let mut full = vec!["/C".to_string(), command.to_string()];
        full.extend(args.iter().cloned());
        return ("cmd".to_string(), full);
    }
    #[cfg(not(windows))]
    {
        (command.to_string(), args.to_vec())
    }
}

// ── 握手探测 ──────────────────────────────────────────────────

/// JSON-RPC error 字段 → 可读错误串
fn jsonrpc_error(resp: &Value) -> Option<String> {
    let err = resp.get("error").filter(|e| !e.is_null())?;
    let msg = err
        .get("message")
        .and_then(|v| v.as_str())
        .unwrap_or("服务器返回未知错误");
    Some(format!("MCP_PROTOCOL_ERROR: {msg}"))
}

fn timeout_of(secs: i64) -> std::time::Duration {
    std::time::Duration::from_secs(secs.max(1) as u64)
}

fn client_info() -> Value {
    json!({ "name": CLIENT_NAME, "version": env!("CARGO_PKG_VERSION") })
}

type ProbeFut = std::pin::Pin<
    Box<dyn std::future::Future<Output = Result<Vec<ToolRow>, String>> + Send + 'static>,
>;

/// 按配置发起真实握手（initialize → initialized → tools/list），返回服务器声明的工具清单
pub async fn probe(target: &ProbeTarget) -> Result<Vec<ToolRow>, String> {
    if target.auth.is_some()
        && target.transport == TRANSPORT_STREAMABLE_HTTP
        && target.access_token.is_none()
    {
        return Err(
            "MCP_OAUTH_REQUIRED: 连接器尚未取得访问令牌，请先完成 OAuth 授权".to_string(),
        );
    }
    let job = target.clone();
    let mut fut: ProbeFut = match job.transport.as_str() {
        TRANSPORT_STDIO => Box::pin(stdio_handshake(job.clone())),
        TRANSPORT_STREAMABLE_HTTP => Box::pin(http_handshake(job.clone())),
        TRANSPORT_SSE => {
            return Err(
                "MCP_TRANSPORT_UNSUPPORTED: SSE 传输需常驻事件流，当前请改用 Streamable HTTP 端点"
                    .to_string(),
            )
        }
        other => {
            return Err(format!(
                "MCP_INVALID_TRANSPORT: 不支持的传输类型「{other}」，仅支持 stdio / streamable_http / sse"
            ))
        }
    };
    match tokio::time::timeout(timeout_of(job.timeout_secs), &mut fut).await {
        Ok(r) => r,
        Err(_) => Err(format!(
            "MCP_TIMEOUT: 与「{}」握手超时（{} 秒内未完成 initialize / tools/list 往返）",
            job.name, job.timeout_secs
        )),
    }
}

async fn stdio_handshake(job: ProbeTarget) -> Result<Vec<ToolRow>, String> {
    use tokio::io::AsyncBufReadExt;

    let (program, args) = spawn_plan(&job.command, &job.args);
    // 系统隔离开启时，探测进程与真实会话走同一个启动器；套壳失败直接拒绝，不静默降级
    let keep_env: Vec<String> = job.env.keys().cloned().collect();
    let (program, args) = match crate::sandbox::wrap_for_spawn(&program, &args, &keep_env)
        .map_err(|e| format!("MCP_SANDBOX_FAILED: {e}"))?
    {
        Some((exe, argv)) => (exe.to_string_lossy().into_owned(), argv),
        None => (program, args),
    };
    let mut cmd = tokio::process::Command::new(&program);
    cmd.args(&args)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    for (k, v) in &job.env {
        cmd.env(k, v);
    }
    crate::win_console::hide_console_async(&mut cmd);

    let mut child = cmd.spawn().map_err(|e| {
        format!(
            "MCP_CONNECTION_FAILED: 启动「{}」失败：{e}（请确认命令已安装并可在 PATH 中找到）",
            job.name
        )
    })?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "MCP_CONNECTION_FAILED: 无法写入子进程标准输入".to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "MCP_CONNECTION_FAILED: 无法读取子进程标准输出".to_string())?;
    let mut lines = tokio::io::BufReader::new(stdout).lines();

    let mut init_req = initialize_request(1);
    init_req["params"]["clientInfo"] = client_info();
    if write_line(&mut stdin, &init_req).await.is_err() {
        let _ = child.kill().await;
        return Err(format!(
            "MCP_CONNECTION_FAILED: 「{}」已关闭标准输入",
            job.name
        ));
    }
    let init = match read_response(&mut lines, 1, &job.name).await {
        Ok(v) => v,
        Err(e) => {
            let _ = child.kill().await;
            return Err(e);
        }
    };
    if let Some(bad) = jsonrpc_error(&init).or_else(|| {
        init.get("result").is_none().then(|| {
            format!(
                "MCP_PROTOCOL_ERROR: 「{}」的 initialize 响应缺少 result 字段",
                job.name
            )
        })
    }) {
        let _ = child.kill().await;
        return Err(bad);
    }

    if write_line(&mut stdin, &initialized_notification()).await.is_err()
        || write_line(&mut stdin, &tools_list_request(2)).await.is_err()
    {
        let _ = child.kill().await;
        return Err(format!(
            "MCP_CONNECTION_FAILED: 「{}」在处理请求中途退出",
            job.name
        ));
    }
    let listed = read_response(&mut lines, 2, &job.name).await;
    let _ = child.kill().await;
    tools_of_response(&listed?)
}

/// MCP stdio 传输：每条 JSON-RPC 消息占一行
async fn write_line(
    stdin: &mut tokio::process::ChildStdin,
    msg: &Value,
) -> Result<(), std::io::Error> {
    use tokio::io::AsyncWriteExt;
    let mut raw = serde_json::to_vec(msg)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    raw.push(b'\n');
    stdin.write_all(&raw).await?;
    stdin.flush().await
}

async fn read_response(
    lines: &mut tokio::io::Lines<tokio::io::BufReader<tokio::process::ChildStdout>>,
    expect_id: i64,
    server_name: &str,
) -> Result<Value, String> {
    loop {
        let next = lines
            .next_line()
            .await
            .map_err(|e| format!("MCP_CONNECTION_FAILED: 读取「{server_name}」失败：{e}"))?;
        let Some(line) = next else {
            return Err(format!(
                "MCP_CONNECTION_FAILED: 「{server_name}」在未应答前退出（启动命令可能失败或服务器崩溃）"
            ));
        };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue; // 服务器把日志写到 stdout 时跳过非 JSON 行
        };
        // 只取本请求的应答，忽略通知与其他并发响应
        if value.get("id").and_then(|v| v.as_i64()) == Some(expect_id) {
            return Ok(value);
        }
    }
}

async fn http_handshake(job: ProbeTarget) -> Result<Vec<ToolRow>, String> {
    // 远端 MCP 走「网络代理」设置；本机 MCP（localhost/127.0.0.1）直连，
    // 系统代理常不 bypass 回环，交给代理会得到 401/503 之类与服务器无关的失败
    let client = if is_loopback_url(&job.url) {
        crate::proxy::loopback_client()
    } else {
        crate::proxy::client()
    };

    let mut init_req = initialize_request(1);
    init_req["params"]["clientInfo"] = client_info();
    let (init, session) = send_jsonrpc(&client, &job, &init_req, None).await?;
    if let Some(bad) = jsonrpc_error(&init).or_else(|| {
        init.get("result").is_none().then(|| {
            format!(
                "MCP_PROTOCOL_ERROR: 「{}」的 initialize 响应缺少 result 字段",
                job.name
            )
        })
    }) {
        return Err(bad);
    }

    // initialized 通知无响应体，其失败不作为连接结论（部分服务器直接回 202）
    let _ = send_jsonrpc(&client, &job, &initialized_notification(), session.as_deref()).await;
    let (listed, _) = send_jsonrpc(&client, &job, &tools_list_request(2), session.as_deref()).await?;
    tools_of_response(&listed)
}

fn is_loopback_url(raw: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(raw) else {
        return false;
    };
    match url.host_str() {
        Some(host) if host.eq_ignore_ascii_case("localhost") => true,
        Some(host) => host
            .trim_start_matches('[')
            .trim_end_matches(']')
            .parse::<std::net::IpAddr>()
            .map(|ip| ip.is_loopback())
            .unwrap_or(false),
        None => false,
    }
}

async fn send_jsonrpc(
    client: &reqwest::Client,
    job: &ProbeTarget,
    body: &Value,
    session: Option<&str>,
) -> Result<(Value, Option<String>), String> {
    let mut req = client
        .post(&job.url)
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream");
    // OAuth 令牌先行注入；用户显式配置的 Authorization 头优先级更高（同名头覆盖）
    if let Some(token) = job.access_token.as_deref() {
        req = req.bearer_auth(token);
    }
    for (k, v) in &job.headers {
        req = req.header(k.as_str(), v.as_str());
    }
    if session.is_some() {
        req = req.header("mcp-protocol-version", PROTOCOL_VERSION);
    }
    if let Some(sid) = session {
        req = req.header("mcp-session-id", sid);
    }

    let resp = req
        .json(body)
        .send()
        .await
        .map_err(|e| format!("MCP_CONNECTION_FAILED: 请求「{}」失败：{e}", job.url))?;
    let status = resp.status();
    let new_session = resp
        .headers()
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_string();
    let text = resp
        .text()
        .await
        .map_err(|e| format!("MCP_CONNECTION_FAILED: 读取响应失败：{e}"))?;
    if !status.is_success() {
        if status == reqwest::StatusCode::UNAUTHORIZED && job.auth.is_some() {
            return Err(format!(
                "MCP_OAUTH_REJECTED: 「{}」返回 HTTP 401，授权可能已失效：{}",
                job.name,
                truncate(text.trim(), 160)
            ));
        }
        return Err(format!(
            "MCP_CONNECTION_FAILED: 「{}」返回 HTTP {status}：{}",
            job.name,
            truncate(text.trim(), 160)
        ));
    }
    if text.trim().is_empty() {
        return Ok((json!({ "result": {} }), new_session));
    }
    Ok((json_from_body(&content_type, &text)?, new_session))
}

fn truncate(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s.to_string();
    }
    s.chars().take(max_chars).collect::<String>() + "…"
}

// ── command 层 ────────────────────────────────────────────────

fn to_value(row: &ServerRow) -> Value {
    serde_json::to_value(row).unwrap_or_else(|_| json!({ "serverId": row.server_id }))
}

/// 列出已登记的 MCP 连接器（无记录时为空数组，不伪造）
#[tauri::command]
pub async fn mcp_list_servers(p0: State<'_, Arc<crate::P0Services>>) -> Result<Value, String> {
    serde_json::to_value(list(&p0.store)?).map_err(|e| format!("MCP_QUERY_FAILED: {e}"))
}

/// 登记连接器（配置落库，状态从「未连接」开始）
#[tauri::command]
pub async fn mcp_add_server(
    p0: State<'_, Arc<crate::P0Services>>,
    req: AddServer,
) -> Result<Value, String> {
    let row = add(&p0.store, &req, crate::now_ms_u64() as i64)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        &row.server_id,
        &format!("登记 MCP 连接器：{}（{}）", row.name, row.transport),
    );
    Ok(to_value(&row))
}

/// 移除连接器配置（只删这一条登记，不动任务/审计等存量数据）
#[tauri::command]
pub async fn mcp_remove_server(
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    let row = get(&p0.store, &server_id)?
        .ok_or_else(|| format!("MCP_SERVER_NOT_FOUND: {}", server_id.trim()))?;
    // 先清凭据库再删行：配置没了不能把令牌留在系统凭据库里
    oauth::delete_token(&p0.store, &row.server_id);
    let removed = remove(&p0.store, &server_id)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        &row.server_id,
        &format!("移除 MCP 连接器：{}", row.name),
    );
    Ok(json!({ "ok": removed, "serverId": row.server_id, "name": row.name }))
}

fn note_connect_failure(store: &Store, server_id: &str, error: &str, now_ms: i64) {
    // 失败原因同样落库：页面显示「上次连接失败：…」，而不是静默回到未验证态
    if let Ok(row) = record_probe(store, server_id, STATUS_FAILED, Some(error), &[], now_ms) {
        audit::record_quiet(
            store,
            audit::ACTION_CONFIG_CHANGE,
            audit::SEV_WARNING,
            &row.server_id,
            &format!("MCP 连接器连接失败：{error}"),
        );
    }
}

/// 连接：按配置真实握手；OAuth 连接器先取可用令牌，401 时强制续期并重试一次
#[tauri::command]
pub async fn mcp_connect(
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    let mut target = probe_target(&p0.store, &server_id)?;
    let now = crate::now_ms_u64() as i64;
    if let Some(cfg) = target.auth.clone() {
        match oauth::ensure_access_token(&p0.store, &server_id, &cfg, now).await {
            Ok(token) => target.access_token = Some(token.access_token),
            Err(e) => {
                note_connect_failure(&p0.store, &server_id, &e, now);
                return Err(e);
            }
        }
    }
    let mut outcome = probe(&target).await;
    if let Some(cfg) = target.auth.clone() {
        if let Err(e) = &outcome {
            if e.starts_with("MCP_OAUTH_REJECTED") {
                match oauth::force_refresh(&p0.store, &server_id, &cfg).await {
                    Ok(token) => {
                        target.access_token = Some(token.access_token);
                        outcome = probe(&target).await;
                    }
                    Err(refresh_err) => outcome = Err(refresh_err),
                }
            }
        }
    }
    match outcome {
        Ok(tools) => {
            let count = tools.len();
            let row = record_probe(&p0.store, &server_id, STATUS_CONNECTED, None, &tools, now)?;
            audit::record_quiet(
                &p0.store,
                audit::ACTION_CONFIG_CHANGE,
                audit::SEV_INFO,
                &row.server_id,
                &format!("MCP 连接器已连接：{}（{count} 个工具）", row.name),
            );
            Ok(to_value(&row))
        }
        Err(e) => {
            note_connect_failure(&p0.store, &server_id, &e, now);
            Err(e)
        }
    }
}

/// 并发授权互斥：同一时刻只允许一条 OAuth 流程（本机回调与用户操作都需唯一）
static OAUTH_BUSY: Mutex<Option<String>> = Mutex::new(None);

fn begin_oauth(server_id: &str) -> Result<(), String> {
    let mut busy = OAUTH_BUSY.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(current) = busy.as_deref() {
        return Err(format!(
            "MCP_OAUTH_IN_PROGRESS: 已有授权流程进行中（{current}），请先完成或稍后再试"
        ));
    }
    *busy = Some(server_id.to_string());
    Ok(())
}

fn end_oauth() {
    *OAUTH_BUSY.lock().unwrap_or_else(|e| e.into_inner()) = None;
}

/// 发起 OAuth 授权：拉起系统浏览器 → 本机回调 → 换令牌入系统凭据库
///
/// 令牌与授权码不进入返回值、审计与日志；成功后清掉旧连接态，要求用新令牌重新验证。
#[tauri::command]
pub async fn mcp_oauth_authorize(
    app: AppHandle,
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    let target = probe_target(&p0.store, &server_id)?;
    let Some(cfg) = target.auth.clone() else {
        return Err(
            "MCP_OAUTH_INVALID_CONFIG: 该连接器未配置 OAuth，请先登记授权参数".to_string(),
        );
    };
    begin_oauth(&target.server_id)?;
    let result = oauth::authorize(&p0.store, &target.server_id, &cfg, |url| {
        app.opener()
            .open_url(url, None::<&str>)
            .map_err(|e| format!("无法打开系统浏览器（{e}）"))
    })
    .await;
    end_oauth();
    match result {
        Ok(_) => {
            let row = disconnect(&p0.store, &target.server_id, crate::now_ms_u64() as i64)?;
            audit::record_quiet(
                &p0.store,
                audit::ACTION_CONNECTOR_AUTH,
                audit::SEV_INFO,
                &row.server_id,
                &format!(
                    "MCP OAuth 授权完成：{}（{}）",
                    row.name,
                    oauth::endpoint_host(&cfg.authorization_url)
                ),
            );
            Ok(to_value(&row))
        }
        Err(e) => {
            audit::record_quiet(
                &p0.store,
                audit::ACTION_CONNECTOR_AUTH,
                audit::SEV_WARNING,
                &target.server_id,
                &format!("MCP OAuth 授权失败：{e}"),
            );
            Err(e)
        }
    }
}

/// 撤销本地授权：删除系统凭据库里的令牌并清连接态；已颁发令牌由提供方按其策略过期
#[tauri::command]
pub async fn mcp_oauth_revoke(
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    let row = get(&p0.store, &server_id)?
        .ok_or_else(|| format!("MCP_SERVER_NOT_FOUND: {}", server_id.trim()))?;
    oauth::delete_token(&p0.store, &row.server_id);
    let row = disconnect(&p0.store, &row.server_id, crate::now_ms_u64() as i64)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONNECTOR_AUTH,
        audit::SEV_INFO,
        &row.server_id,
        &format!("MCP OAuth 授权已撤销：{}（本地令牌已清除）", row.name),
    );
    Ok(to_value(&row))
}

/// 断开：清除连接状态与工具缓存，配置保留可随时再连
#[tauri::command]
pub async fn mcp_disconnect(
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    let row = disconnect(&p0.store, &server_id, crate::now_ms_u64() as i64)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        &row.server_id,
        &format!("MCP 连接器已断开：{}", row.name),
    );
    Ok(to_value(&row))
}

/// 已缓存的工具清单（未连接时为空数组，不伪造工具）
#[tauri::command]
pub async fn mcp_list_tools(
    p0: State<'_, Arc<crate::P0Services>>,
    server_id: String,
) -> Result<Value, String> {
    serde_json::to_value(tools_of(&p0.store, &server_id)?).map_err(|e| format!("MCP_QUERY_FAILED: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-mcp-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        (Store::open(dir.join("t.db")).unwrap(), dir)
    }

    fn now() -> i64 {
        1_700_000_000_000
    }

    fn stdio_req(name: &str) -> AddServer {
        AddServer {
            name: name.to_string(),
            transport: Some("STDIO".to_string()),
            command: Some("npx".to_string()),
            args: Some(vec!["-y".to_string(), "@modelcontextprotocol/server-fs".to_string()]),
            ..Default::default()
        }
    }

    fn http_req(name: &str) -> AddServer {
        AddServer {
            name: name.to_string(),
            transport: Some("STREAMABLE_HTTP".to_string()),
            url: Some("http://localhost:8080/mcp".to_string()),
            headers: Some(BTreeMap::from([
                ("Authorization".to_string(), "Bearer sk-leak-check".to_string()),
            ])),
            ..Default::default()
        }
    }

    // ── 注册表 ────────────────────────────────────────────────

    #[test]
    fn test_list_empty_by_default() {
        let (store, dir) = temp_store("empty");
        assert!(list(&store).unwrap().is_empty(), "无登记时不得伪造连接器");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_add_stdio_defaults_to_offline() {
        let (store, dir) = temp_store("add");
        let row = add(&store, &stdio_req("本地文件系统"), now()).unwrap();
        assert_eq!(row.name, "本地文件系统");
        assert_eq!(row.transport, TRANSPORT_STDIO, "UI 的大写写法归一为小写枚举");
        assert_eq!(row.command, "npx");
        assert_eq!(row.args, vec!["-y", "@modelcontextprotocol/server-fs"]);
        assert_eq!(row.status, STATUS_OFFLINE);
        assert!(!row.connected);
        assert!(row.tools.is_empty(), "未连接不得有工具");
        assert_eq!(row.timeout_secs, DEFAULT_TIMEOUT_SECS);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_add_rejects_bad_input() {
        let (store, dir) = temp_store("invalid");
        let cases = vec![
            (AddServer { name: "  ".into(), ..Default::default() }, "MCP_INVALID_NAME"),
            (
                AddServer {
                    name: "x".into(),
                    transport: Some("carrier-pigeon".into()),
                    command: Some("ls".into()),
                    ..Default::default()
                },
                "MCP_INVALID_TRANSPORT",
            ),
            (
                // stdio 必须有启动命令
                AddServer {
                    name: "x".into(),
                    transport: Some("stdio".into()),
                    ..Default::default()
                },
                "MCP_INVALID_COMMAND",
            ),
            (
                AddServer {
                    name: "x".into(),
                    command: Some("  ".into()),
                    ..Default::default()
                },
                "MCP_INVALID_COMMAND",
            ),
            (
                AddServer {
                    name: "x".into(),
                    transport: Some("sse".into()),
                    ..Default::default()
                },
                "MCP_INVALID_URL",
            ),
            (
                AddServer {
                    name: "x".into(),
                    transport: Some("streamable_http".into()),
                    url: Some("localhost:8080".into()),
                    ..Default::default()
                },
                "MCP_INVALID_URL",
            ),
            (
                AddServer {
                    name: "x".into(),
                    transport: Some("streamable_http".into()),
                    url: Some("http://local host/mcp".into()),
                    ..Default::default()
                },
                "MCP_INVALID_URL",
            ),
            (
                AddServer {
                    name: "x".into(),
                    command: Some("ls\nrm -rf /".into()),
                    ..Default::default()
                },
                "MCP_INVALID_COMMAND",
            ),
            (
                AddServer {
                    name: "x".into(),
                    command: Some("ls".into()),
                    env: Some(BTreeMap::from([("K".into(), "v1\nv2".into())])),
                    ..Default::default()
                },
                "MCP_INVALID_HEADER",
            ),
        ];
        for (req, code) in cases {
            let err = add(&store, &req, now()).unwrap_err();
            assert!(err.starts_with(code), "{code} → {err}");
        }
        assert!(list(&store).unwrap().is_empty(), "被拒的登记不得落库");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_duplicate_name_is_refused_without_overwrite() {
        let (store, dir) = temp_store("dup");
        add(&store, &stdio_req("重复"), now()).unwrap();
        let err = add(&store, &stdio_req("  重复 "), now() + 1).unwrap_err();
        assert!(err.starts_with("MCP_SERVER_EXISTS"), "{err}");
        let rows = list(&store).unwrap();
        assert_eq!(rows.len(), 1, "重名登记不得覆盖既有配置");
        assert_eq!(rows[0].command, "npx");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_timeout_is_clamped() {
        let (store, dir) = temp_store("timeout");
        let mut req = stdio_req("t0");
        req.timeout_secs = Some(0);
        assert_eq!(add(&store, &req, now()).unwrap().timeout_secs, DEFAULT_TIMEOUT_SECS);
        req.name = "t9999".into();
        req.timeout_secs = Some(9999);
        assert_eq!(add(&store, &req, now()).unwrap().timeout_secs, MAX_TIMEOUT_SECS);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_remove_only_drops_the_named_row() {
        let (store, dir) = temp_store("remove");
        let a = add(&store, &stdio_req("a"), now()).unwrap();
        add(&store, &stdio_req("b"), now()).unwrap();
        assert!(remove(&store, &a.server_id).unwrap());
        assert!(!remove(&store, &a.server_id).unwrap(), "重复移除返回 false");
        assert_eq!(list(&store).unwrap().len(), 1, "移除不得牵连其他连接器");
        assert!(remove(&store, "   ").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_probe_target_round_trips_secrets() {
        let (store, dir) = temp_store("secret");
        let mut req = stdio_req("带密钥");
        req.env = Some(BTreeMap::from([
            ("API_KEY".into(), "sk-value".into()),
            ("空白".into(), "x".into()),
        ]));
        req.args = Some(vec!["-y".into(), " ".into()]);
        let row = add(&store, &req, now()).unwrap();
        let target = probe_target(&store, &row.server_id).unwrap();
        assert_eq!(target.env.get("API_KEY").map(String::as_str), Some("sk-value"), "值须可在 Rust 侧取回");
        assert_eq!(target.args.len(), 1, "空白参数不入库");
        assert!(probe_target(&store, "mcp-不存在")
            .unwrap_err()
            .starts_with("MCP_SERVER_NOT_FOUND"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_row_serialization_never_leaks_secret_values() {
        let (store, dir) = temp_store("leak");
        let mut req = stdio_req("密钥行");
        req.env = Some(BTreeMap::from([("API_KEY".into(), "sk-super-secret".into())]));
        let row = add(&store, &req, now()).unwrap();
        let http = add(&store, &http_req("远程头"), now()).unwrap();
        let json = serde_json::to_string(&vec![row, http]).unwrap();
        assert!(!json.contains("sk-super-secret"), "IPC 不得把 env 值送进 WebView");
        assert!(!json.contains("sk-leak-check"), "IPC 不得把 header 值送进 WebView");
        assert!(json.contains("API_KEY"), "键名可展示，便于用户确认配置");
        assert!(json.contains("Authorization"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn oauth_cfg() -> oauth::OAuthConfig {
        oauth::OAuthConfig {
            authorization_url: "https://id.example.com/authorize".into(),
            token_url: "https://id.example.com/token".into(),
            client_id: "tt-client".into(),
            scopes: vec!["read".into()],
        }
    }

    fn oauth_req(name: &str) -> AddServer {
        AddServer {
            name: name.into(),
            transport: Some("STREAMABLE_HTTP".into()),
            url: Some("https://mcp.example.com/mcp".into()),
            auth: Some(oauth_cfg()),
            ..Default::default()
        }
    }

    fn oauth_token(expires_at_ms: Option<i64>) -> oauth::Token {
        oauth::Token {
            access_token: "at-secret".into(),
            refresh_token: Some("rt-secret".into()),
            token_type: Some("Bearer".into()),
            expires_at_ms,
            scope: None,
        }
    }

    #[test]
    fn test_add_persists_oauth_config_without_token() {
        let (store, dir) = temp_store("oauth-add");
        let row = add(&store, &oauth_req("远端数据"), now()).unwrap();
        let auth = row.auth.as_ref().expect("OAuth 配置应随行读回");
        assert_eq!(auth.client_id, "tt-client");
        assert_eq!(row.auth_status, oauth::AUTH_NONE);
        let json = serde_json::to_string(&row).unwrap();
        assert!(json.contains("authorizationUrl"), "非敏感授权参数可供界面回显");
        assert!(!json.contains("at-secret") && !json.contains("rt-secret"), "令牌不得进列表视图");

        // 令牌进系统凭据库后，列表状态跟着变
        let live = crate::now_ms_u64() as i64 + 3_600_000;
        oauth::save_token(&store, &row.server_id, &oauth_token(Some(live))).unwrap();
        let listed = get(&store, &row.server_id).unwrap().unwrap();
        assert_eq!(listed.auth_status, oauth::AUTH_AUTHORIZED);
        let target = probe_target(&store, &row.server_id).unwrap();
        assert_eq!(target.access_token.as_deref(), Some("at-secret"));
        oauth::delete_token(&store, &row.server_id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_oauth_only_for_streamable_http_and_valid_endpoints() {
        let (store, dir) = temp_store("oauth-invalid");
        let mut stdio = oauth_req("本地也配 OAuth");
        stdio.transport = Some("stdio".into());
        stdio.command = Some("npx".into());
        stdio.url = None;
        let err = add(&store, &stdio, now()).unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_INVALID_CONFIG"), "{err}");

        let mut bad = oauth_req("坏地址");
        bad.auth = Some(oauth::OAuthConfig {
            authorization_url: "http://id.example.com/authorize".into(),
            ..oauth_cfg()
        });
        let err = add(&store, &bad, now()).unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_INVALID_CONFIG"), "{err}");
        assert!(list(&store).unwrap().is_empty(), "被拒的 OAuth 配置不得落库");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_oauth_rejects_explicit_authorization_header() {
        let (store, dir) = temp_store("oauth-header-conflict");
        let mut req = oauth_req("重复授权头");
        req.headers = Some(BTreeMap::from([(
            "authorization".into(),
            "Bearer stale".into(),
        )]));
        let err = add(&store, &req, now()).unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_INVALID_CONFIG"), "{err}");
        assert!(err.contains("覆盖"), "错误要说清为什么会拒绝：{err}");
        assert!(list(&store).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_for_runtime_injects_bearer_and_skips_expired_oauth() {
        let (store, dir) = temp_store("oauth-runtime");
        let row = add(&store, &oauth_req("受保护数据"), now()).unwrap();
        let tools = vec![ToolRow {
            name: "read_rows".into(),
            description: String::new(),
            input_schema: json!({ "type": "object" }),
        }];
        record_probe(&store, &row.server_id, STATUS_CONNECTED, None, &tools, now()).unwrap();

        // 未授权：即使连过也不下发，避免引擎发匿名请求或带空令牌
        assert!(for_runtime(&store).unwrap().is_empty());

        let live = crate::now_ms_u64() as i64 + 3_600_000;
        oauth::save_token(&store, &row.server_id, &oauth_token(Some(live))).unwrap();
        let rows = for_runtime(&store).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["headers"]["Authorization"], "Bearer at-secret");
        assert!(!rows[0].to_string().contains("rt-secret"), "刷新令牌不下发");

        // 过期：不下发失效凭据
        oauth::save_token(&store, &row.server_id, &oauth_token(Some(1))).unwrap();
        assert!(for_runtime(&store).unwrap().is_empty());
        oauth::delete_token(&store, &row.server_id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn test_probe_requires_token_for_oauth_connector() {
        let target = ProbeTarget {
            server_id: "mcp-oauth-missing".into(),
            name: "未授权".into(),
            transport: TRANSPORT_STREAMABLE_HTTP.into(),
            command: String::new(),
            args: vec![],
            url: "http://127.0.0.1:1/mcp".into(),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 1,
            auth: Some(oauth_cfg()),
            access_token: None,
        };
        let err = probe(&target).await.unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_REQUIRED"), "{err}");
    }

    #[tokio::test]
    async fn test_probe_maps_http_401_to_oauth_rejected() {
        use std::io::{Read as _, Write as _};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf);
                let body = r#"{"error":"unauthorized"}"#;
                let resp = format!(
                    "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        let target = ProbeTarget {
            server_id: "mcp-oauth-401".into(),
            name: "令牌失效".into(),
            transport: TRANSPORT_STREAMABLE_HTTP.into(),
            command: String::new(),
            args: vec![],
            url: format!("http://127.0.0.1:{port}/mcp"),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 5,
            auth: Some(oauth_cfg()),
            access_token: Some("expired-at".into()),
        };
        let err = probe(&target).await.unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_REJECTED"), "{err}");
    }

    #[test]
    fn test_record_probe_persists_status_and_tools() {
        let (store, dir) = temp_store("probe-ok");
        let row = add(&store, &stdio_req("舆情"), now()).unwrap();
        let tools = vec![ToolRow {
            name: "fetch".into(),
            description: "抓取网页".into(),
            input_schema: json!({ "type": "object", "properties": { "url": { "type": "string" } } }),
        }];
        let after = record_probe(&store, &row.server_id, STATUS_CONNECTED, None, &tools, now() + 5).unwrap();
        assert!(after.connected);
        assert_eq!(after.status, STATUS_CONNECTED);
        assert_eq!(after.tools, tools, "工具清单（含 inputSchema）应完整读回");
        assert_eq!(after.probed_at_ms, Some(now() + 5));
        assert_eq!(tools_of(&store, &row.server_id).unwrap(), tools);
        assert_eq!(list(&store).unwrap()[0].tools.len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_record_probe_failure_keeps_reason_and_clears_tools() {
        let (store, dir) = temp_store("probe-fail");
        let row = add(&store, &stdio_req("失败"), now()).unwrap();
        let tools = vec![ToolRow { name: "t".into(), description: String::new(), input_schema: json!({}) }];
        record_probe(&store, &row.server_id, STATUS_CONNECTED, None, &tools, now()).unwrap();
        let err = record_probe(
            &store,
            &row.server_id,
            STATUS_FAILED,
            Some("MCP_CONNECTION_FAILED: 启动失败"),
            &[],
            now() + 1,
        )
        .unwrap();
        assert_eq!(err.status, STATUS_FAILED);
        assert!(!err.connected);
        assert!(err.last_error.as_deref().unwrap().starts_with("MCP_CONNECTION_FAILED"));
        assert!(err.tools.is_empty(), "失败后不得保留上次工具清单");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_disconnect_clears_state_and_keeps_config() {
        let (store, dir) = temp_store("disconnect");
        let row = add(&store, &stdio_req("断开"), now()).unwrap();
        record_probe(
            &store,
            &row.server_id,
            STATUS_CONNECTED,
            None,
            &[ToolRow { name: "t".into(), description: String::new(), input_schema: json!({}) }],
            now(),
        )
        .unwrap();
        let off = disconnect(&store, &row.server_id, now() + 2).unwrap();
        assert_eq!(off.status, STATUS_OFFLINE);
        assert!(off.tools.is_empty());
        assert!(off.last_error.is_none());
        assert_eq!(off.command, "npx", "断开只清连接态，配置必须保留");
        let miss = disconnect(&store, "mcp-ghost", now()).unwrap_err();
        assert!(miss.starts_with("MCP_SERVER_NOT_FOUND"), "{miss}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_tools_of_unknown_server() {
        let (store, dir) = temp_store("tools-miss");
        assert!(tools_of(&store, "mcp-none").unwrap_err().starts_with("MCP_SERVER_NOT_FOUND"));
        assert!(tools_of(&store, "").unwrap_err().starts_with("MCP_INVALID_ID"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_orders_by_name() {
        let (store, dir) = temp_store("order");
        for name in ["zeta", "alpha", "mid"] {
            add(&store, &stdio_req(name), now()).unwrap();
        }
        let names = list(&store)
            .unwrap()
            .iter()
            .map(|r| r.name.clone())
            .collect::<Vec<_>>();
        assert_eq!(names, vec!["alpha", "mid", "zeta"]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── 协议纯函数 ────────────────────────────────────────────

    #[test]
    fn test_normalize_transport_accepts_ui_spellings() {
        assert_eq!(normalize_transport(None).unwrap(), TRANSPORT_STDIO);
        assert_eq!(normalize_transport(Some("")).unwrap(), TRANSPORT_STDIO);
        assert_eq!(normalize_transport(Some("STDIO")).unwrap(), TRANSPORT_STDIO);
        assert_eq!(
            normalize_transport(Some("Streamable-HTTP")).unwrap(),
            TRANSPORT_STREAMABLE_HTTP
        );
        assert_eq!(normalize_transport(Some("http")).unwrap(), TRANSPORT_STREAMABLE_HTTP);
        assert_eq!(normalize_transport(Some(" sse ")).unwrap(), TRANSPORT_SSE);
        assert!(normalize_transport(Some("ws")).unwrap_err().starts_with("MCP_INVALID_TRANSPORT"));
    }

    #[test]
    fn test_request_bodies_follow_jsonrpc() {
        let init = initialize_request(7);
        assert_eq!(init["jsonrpc"], "2.0");
        assert_eq!(init["id"], 7);
        assert_eq!(init["method"], "initialize");
        assert_eq!(init["params"]["protocolVersion"], PROTOCOL_VERSION);
        assert_eq!(init["params"]["clientInfo"]["name"], CLIENT_NAME);

        let notify = initialized_notification();
        assert_eq!(notify["method"], "notifications/initialized");
        assert!(notify.get("id").is_none(), "通知不得带 id");

        let list = tools_list_request(2);
        assert_eq!(list["method"], "tools/list");
        assert_eq!(list["id"], 2);
    }

    #[test]
    fn test_tools_of_response_parses_server_payload() {
        let resp = json!({
            "jsonrpc": "2.0",
            "id": 2,
            "result": { "tools": [
                { "name": "read_file", "description": "读文件", "inputSchema": { "type": "object" } },
                { "name": "  ", "description": "无名工具不可调用" },
                { "name": "no_schema" }
            ] }
        });
        let tools = tools_of_response(&resp).unwrap();
        assert_eq!(tools.len(), 2, "无名工具被丢弃");
        assert_eq!(tools[0].name, "read_file");
        assert_eq!(tools[0].input_schema, json!({ "type": "object" }));
        assert_eq!(tools[1].description, "");
        assert_eq!(tools[1].input_schema["type"], "object", "缺 schema 时给空对象骨架");
    }

    #[test]
    fn test_tools_of_response_surfaces_server_error() {
        let err = tools_of_response(&json!({ "jsonrpc": "2.0", "id": 2, "error": { "code": -32601, "message": "Method not found" } }))
            .unwrap_err();
        assert_eq!(err, "MCP_PROTOCOL_ERROR: Method not found");
        let missing = tools_of_response(&json!({ "jsonrpc": "2.0", "id": 2, "result": {} })).unwrap_err();
        assert!(missing.starts_with("MCP_PROTOCOL_ERROR"), "{missing}");
    }

    #[test]
    fn test_json_from_body_handles_json_and_sse() {
        let v = json_from_body("application/json", r#"{"jsonrpc":"2.0","id":2,"result":{}}"#).unwrap();
        assert_eq!(v["id"], 2);
        // Streamable HTTP 允许以 SSE 帧返回单条响应
        let sse = "event: message\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}\r\n\r\n";
        let v = json_from_body("text/event-stream", sse).unwrap();
        assert_eq!(v["result"]["tools"], json!([]));
        // 非 JSON-RPC 的 data 行（心跳）应被跳过
        let noisy = "data: ping\r\n\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":1}\r\n\r\n";
        assert_eq!(json_from_body("text/event-stream", noisy).unwrap()["id"], 1);
        assert!(json_from_body("application/json", "not json").unwrap_err().starts_with("MCP_PROTOCOL_ERROR"));
        assert!(json_from_body("text/event-stream", "data: 无 JSON").unwrap_err().starts_with("MCP_PROTOCOL_ERROR"));
    }

    #[test]
    fn test_spawn_plan_wraps_bare_commands_on_windows() {
        let (program, args) = spawn_plan("npx", &["-y".to_string(), "srv".to_string()]);
        if cfg!(windows) {
            assert_eq!(program, "cmd");
            assert_eq!(args, vec!["/C", "npx", "-y", "srv"]);
        } else {
            assert_eq!(program, "npx");
            assert_eq!(args, vec!["-y", "srv"]);
        }
        let (program, args) = spawn_plan("C:\\tools\\srv.exe", &["--flag".to_string()]);
        assert_eq!(program, "C:\\tools\\srv.exe");
        assert_eq!(args, vec!["--flag"]);
    }

    #[test]
    fn test_truncate_keeps_short_text_intact() {
        assert_eq!(truncate("短", 10), "短");
        assert_eq!(truncate("abcdefghij", 4), "abcd…");
    }

    // ── 异步：真实探测路径的错误处理 ─────────────────────────

    #[tokio::test]
    async fn test_probe_rejects_sse_and_unknown_transport() {
        let mut target = ProbeTarget {
            server_id: "mcp-test-sse".into(),
            name: "sse 服务器".into(),
            transport: TRANSPORT_SSE.into(),
            command: String::new(),
            args: vec![],
            url: "http://localhost:1/sse".into(),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 1,
            auth: None,
            access_token: None,
        };
        let err = probe(&target).await.unwrap_err();
        assert!(err.starts_with("MCP_TRANSPORT_UNSUPPORTED"), "{err}");
        target.transport = "carrier-pigeon".into();
        assert!(probe(&target).await.unwrap_err().starts_with("MCP_INVALID_TRANSPORT"));
    }

    #[tokio::test]
    async fn test_probe_missing_command_fails_honestly() {
        let target = ProbeTarget {
            server_id: "mcp-test-missing".into(),
            name: "不存在的命令".into(),
            transport: TRANSPORT_STDIO.into(),
            command: "tt-mcp-definitely-not-installed".into(),
            args: vec![],
            url: String::new(),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 5,
            auth: None,
            access_token: None,
        };
        let err = probe(&target).await.unwrap_err();
        assert!(err.starts_with("MCP_CONNECTION_FAILED"), "{err}");
        assert!(err.contains("不存在的命令"), "错误里要能看出是哪个连接器：{err}");
    }

    #[tokio::test]
    async fn test_probe_timeout_is_bounded() {
        // 命令能启动但永不应答：必须在 timeout_secs 内以 MCP_TIMEOUT 返回，而不是吊死
        let sleeper = if cfg!(windows) { "ping" } else { "sleep" };
        let args: Vec<String> = if cfg!(windows) {
            vec!["-n".into(), "30".into(), "127.0.0.1".into()]
        } else {
            vec!["30".into()]
        };
        let target = ProbeTarget {
            server_id: "mcp-test-timeout".into(),
            name: "无响应服务器".into(),
            transport: TRANSPORT_STDIO.into(),
            command: sleeper.into(),
            args,
            url: String::new(),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 1,
            auth: None,
            access_token: None,
        };
        let err = probe(&target).await.unwrap_err();
        assert!(
            err.starts_with("MCP_TIMEOUT") || err.starts_with("MCP_CONNECTION_FAILED"),
            "{err}"
        );
    }

    #[test]
    fn for_runtime_only_carries_connected_servers_with_secret_values() {
        let (store, dir) = temp_store("runtime");
        let mut req = stdio_req("echo");
        req.env = Some(BTreeMap::from([("TOKEN".to_string(), "sk-keep-local".to_string())]));
        let row = add(&store, &req, 1).unwrap();
        // 只添加未连接：配置存在 ≠ 工具可用，不能替用户启动没验证过的进程
        assert!(for_runtime(&store).unwrap().is_empty());

        let tools = vec![ToolRow {
            name: "echo".into(),
            description: "回显输入".into(),
            input_schema: json!({ "type": "object" }),
        }];
        record_probe(&store, &row.server_id, STATUS_CONNECTED, None, &tools, 2).unwrap();

        let rows = for_runtime(&store).unwrap();
        assert_eq!(rows.len(), 1);
        let r = &rows[0];
        assert_eq!(r["name"], "echo");
        assert_eq!(r["transport"], TRANSPORT_STDIO);
        assert_eq!(r["command"], "npx");
        assert_eq!(r["args"].as_array().unwrap().len(), 2);
        // 引擎要靠这些值拉起 stdio 子进程，列表页则只给键名
        assert_eq!(r["env"]["TOKEN"], "sk-keep-local");
        assert_eq!(r["tools"][0]["name"], "echo");

        // 断开后不再下发，但配置行仍在（不删存量数据，随时可再连）
        disconnect(&store, &row.server_id, 3).unwrap();
        assert!(for_runtime(&store).unwrap().is_empty());
        assert_eq!(list(&store).unwrap().len(), 1);
        std::fs::remove_dir_all(&dir).ok();
    }
}
