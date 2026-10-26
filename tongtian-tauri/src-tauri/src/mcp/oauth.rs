//! 远程 MCP 连接器 OAuth 2.0（授权码 + PKCE，公共客户端）
//!
//! 边界：
//! - 只支持授权码 + S256 PKCE，不保存 client secret，因此只适用于公共客户端；
//!   需要 secret 或 mTLS 的提供方不在当前支持范围。
//! - 授权地址 / token 地址必须 HTTPS；仅本机回环允许 HTTP（便于本地自建服务与测试）。
//! - 令牌只进系统凭据库（keyring，不可用时降级为进程内存），不落 SQLite、不进渲染层。
//! - 不做动态客户端注册、设备码流程与后台轮询刷新；令牌在「连接」前按需刷新。

use std::net::IpAddr;
use std::time::Duration;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::store::Store;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_URL_LEN: usize = 2048;
const MAX_CLIENT_ID_LEN: usize = 512;
const MAX_SCOPES: usize = 20;
const MAX_SCOPE_LEN: usize = 200;
const MAX_TOKEN_BYTES: usize = 256 * 1024;
/// 提前 60 秒续期，避免「刚连上就过期」
const EXPIRY_SKEW_MS: i64 = 60_000;
pub const CALLBACK_TIMEOUT_SECS: u64 = 180;

pub const AUTH_NONE: &str = "none";
pub const AUTH_AUTHORIZED: &str = "authorized";
pub const AUTH_EXPIRED: &str = "expired";

/// 非敏感 OAuth 配置：可随连接器行进渲染层，令牌与 secret 不在其中
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct OAuthConfig {
    pub authorization_url: String,
    pub token_url: String,
    pub client_id: String,
    #[serde(default)]
    pub scopes: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Token {
    pub access_token: String,
    #[serde(default)]
    pub refresh_token: Option<String>,
    #[serde(default)]
    pub token_type: Option<String>,
    /// 绝对到期时间；提供方未返回 expires_in 时为 None（不做无依据的过期判断）
    #[serde(default)]
    pub expires_at_ms: Option<i64>,
    #[serde(default)]
    pub scope: Option<String>,
}

fn invalid_config(detail: &str) -> String {
    format!("MCP_OAUTH_INVALID_CONFIG: {detail}")
}

// ── URL 与网络安全边界 ─────────────────────────────────────────

fn bare_host(host: &str) -> &str {
    host.strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .unwrap_or(host)
}

fn host_is_loopback(url: &reqwest::Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    match bare_host(host).parse::<IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        Err(_) => false,
    }
}

fn ipv4_is_public(ip: std::net::Ipv4Addr) -> bool {
    let o = ip.octets();
    !(ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_documentation()
        || ip.is_unspecified()
        || ip.is_multicast()
        || o[0] == 0
        || (o[0] == 100 && (64..128).contains(&o[1]))
        || (o[0] == 192 && o[1] == 0 && o[2] == 0)
        || (o[0] == 198 && (o[1] == 18 || o[1] == 19)))
}

fn ipv6_is_public(ip: std::net::Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return ipv4_is_public(v4);
    }
    let seg = ip.segments();
    !(ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || (seg[0] & 0xfe00) == 0xfc00
        || (seg[0] & 0xffc0) == 0xfe80
        || (seg[0] == 0x2001 && seg[1] == 0x0db8))
}

fn ip_is_public(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => ipv4_is_public(v4),
        IpAddr::V6(v6) => ipv6_is_public(v6),
    }
}

/// 校验 OAuth 端点：HTTPS，或本机回环 HTTP；拒绝 URL 内嵌凭据
fn validate_endpoint(raw: &str, label: &str) -> Result<reqwest::Url, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(invalid_config(&format!("{label}不能为空")));
    }
    if trimmed.len() > MAX_URL_LEN {
        return Err(invalid_config(&format!("{label}过长")));
    }
    let url = reqwest::Url::parse(trimmed)
        .map_err(|_| invalid_config(&format!("{label}不是合法 URL，请填写完整的 http(s) 地址")))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(invalid_config(&format!("{label}不能包含用户名或密码")));
    }
    if url.host_str().is_none() {
        return Err(invalid_config(&format!("{label}缺少主机名")));
    }
    match url.scheme() {
        "https" => Ok(url),
        "http" if host_is_loopback(&url) => Ok(url),
        "http" => Err(invalid_config(&format!(
            "{label}必须使用 HTTPS；仅 localhost / 127.0.0.1 / ::1 允许 HTTP"
        ))),
        _ => Err(invalid_config(&format!("{label}只支持 http(s)"))),
    }
}

/// 远程主机的 DNS 结果必须全部是公网地址，防止把 token 发到内网服务
async fn ensure_resolved_public(url: &reqwest::Url, label: &str) -> Result<(), String> {
    if host_is_loopback(url) {
        return Ok(());
    }
    let Some(host) = url.host_str() else {
        return Err(invalid_config(&format!("{label}缺少主机名")));
    };
    let port = url.port_or_known_default().unwrap_or(443);
    let addrs = tokio::net::lookup_host((host, port))
        .await
        .map_err(|e| format!("MCP_OAUTH_NETWORK: 无法解析{label}主机（{e}）"))?;
    let mut any = false;
    for addr in addrs {
        any = true;
        if !ip_is_public(addr.ip()) {
            return Err(invalid_config(&format!(
                "{label}解析到非公网地址，已拒绝（{host}）"
            )));
        }
    }
    if !any {
        return Err(format!("MCP_OAUTH_NETWORK: {label}主机没有可用地址（{host}）"));
    }
    Ok(())
}

pub fn normalize_config(raw: &OAuthConfig) -> Result<OAuthConfig, String> {
    let authorization_url = validate_endpoint(&raw.authorization_url, "授权地址")?
        .to_string();
    let token_url = validate_endpoint(&raw.token_url, "token 地址")?.to_string();
    let client_id = raw.client_id.trim();
    if client_id.is_empty() {
        return Err(invalid_config("client_id 不能为空"));
    }
    if client_id.chars().count() > MAX_CLIENT_ID_LEN || client_id.chars().any(char::is_whitespace) {
        return Err(invalid_config("client_id 非法（不能包含空白且不超过 512 字符）"));
    }
    if raw.scopes.len() > MAX_SCOPES {
        return Err(invalid_config("授权范围过多（不超过 20 项）"));
    }
    let mut scopes = Vec::new();
    for scope in &raw.scopes {
        let s = scope.trim();
        if s.is_empty() {
            continue;
        }
        if s.chars().count() > MAX_SCOPE_LEN || s.chars().any(char::is_whitespace) {
            return Err(invalid_config(&format!("授权范围「{s}」非法")));
        }
        if !scopes.iter().any(|v: &String| v == s) {
            scopes.push(s.to_string());
        }
    }
    Ok(OAuthConfig {
        authorization_url,
        token_url,
        client_id: client_id.to_string(),
        scopes,
    })
}

/// 存储口径：空对象 / 空串视为未配置；坏 JSON 也按未配置处理（不会误发令牌）
pub fn parse_stored_config(raw: &str) -> Option<OAuthConfig> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let value: Value = serde_json::from_str(trimmed).ok()?;
    if value.get("authorizationUrl").is_none() {
        return None;
    }
    serde_json::from_value::<OAuthConfig>(value).ok()
}

// ── PKCE / 授权地址 ───────────────────────────────────────────

fn gen_state() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

/// verifier 使用 64 位十六进制（unreserved，长度落在 RFC 7636 的 43~128 区间）
fn gen_verifier() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

fn code_challenge(verifier: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

fn build_authorize_url(
    cfg: &OAuthConfig,
    redirect_uri: &str,
    state: &str,
    challenge: &str,
) -> Result<reqwest::Url, String> {
    let mut url = validate_endpoint(&cfg.authorization_url, "授权地址")?;
    {
        let mut pairs = url.query_pairs_mut();
        pairs
            .append_pair("response_type", "code")
            .append_pair("client_id", &cfg.client_id)
            .append_pair("redirect_uri", redirect_uri)
            .append_pair("state", state)
            .append_pair("code_challenge", challenge)
            .append_pair("code_challenge_method", "S256");
        if !cfg.scopes.is_empty() {
            pairs.append_pair("scope", &cfg.scopes.join(" "));
        }
    }
    Ok(url)
}

/// 回调参数（错误分支也要能解析，不能只看 code/state）
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Callback {
    pub code: Option<String>,
    pub state: Option<String>,
    pub error: Option<String>,
    pub error_description: Option<String>,
}

pub fn parse_callback_target(target: &str) -> Option<Callback> {
    let query = target.split_once('?')?.1;
    let mut out = Callback::default();
    for kv in query.split('&') {
        let Some((k, v)) = kv.split_once('=') else {
            continue;
        };
        let decoded = crate::auth::service::urldecode(v);
        match k {
            "code" => out.code = Some(decoded),
            "state" => out.state = Some(decoded),
            "error" => out.error = Some(decoded),
            "error_description" => out.error_description = Some(decoded),
            _ => {}
        }
    }
    Some(out)
}

async fn wait_callback(listener: tokio::net::TcpListener) -> Result<Callback, String> {
    loop {
        let (mut socket, _) = listener
            .accept()
            .await
            .map_err(|e| format!("MCP_OAUTH_NETWORK: 回调监听失败（{e}）"))?;
        let mut buf = vec![0u8; 8192];
        let n = socket.read(&mut buf).await.unwrap_or(0);
        let raw = String::from_utf8_lossy(&buf[..n]).to_string();
        let target = raw
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .unwrap_or_default()
            .to_string();
        if !target.starts_with("/callback") {
            let resp = "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot Found";
            let _ = socket.write_all(resp.as_bytes()).await;
            continue;
        }
        let Some(cb) = parse_callback_target(&target) else {
            let resp = "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 11\r\nConnection: close\r\n\r\nBad Request";
            let _ = socket.write_all(resp.as_bytes()).await;
            continue;
        };
        let (title, hint) = if cb.error.is_some() {
            ("授权未完成", "授权被拒绝或提供方返回错误，可关闭此页面返回通天办公")
        } else {
            ("授权完成", "可关闭此页面返回通天办公")
        };
        let body = format!(
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>{title} - 通天办公</title></head><body style=\"font-family:'Microsoft YaHei',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#EEF2FF\"><div style=\"background:#fff;border-radius:16px;padding:36px 48px;box-shadow:0 16px 48px rgba(79,70,229,.14);text-align:center\"><h2 style=\"color:#111827;margin:0 0 8px\">{title}</h2><p style=\"color:#6B7280;font-size:13px;margin:0\">{hint}</p></div></body></html>"
        );
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = socket.write_all(resp.as_bytes()).await;
        return Ok(cb);
    }
}

// ── token 交换与存储 ──────────────────────────────────────────

fn token_client(local: bool) -> Result<reqwest::Client, String> {
    let builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .user_agent("TongTianOffice-MCP/1.0");
    let builder = if local {
        builder.no_proxy()
    } else {
        crate::proxy::current().apply(builder)?
    };
    builder
        .build()
        .map_err(|e| format!("MCP_OAUTH_NETWORK: 无法创建网络客户端（{e}）"))
}

fn token_from_json(value: &Value, now_ms: i64) -> Result<Token, String> {
    let access = value
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "MCP_OAUTH_INVALID_RESPONSE: token 响应缺少 access_token".to_string())?;
    if access.len() > 16 * 1024 {
        return Err("MCP_OAUTH_INVALID_RESPONSE: access_token 过长".into());
    }
    let refresh = value
        .get("refresh_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    let expires = value
        .get("expires_in")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_u64().map(|n| n as i64))
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .filter(|v| *v > 0)
        .map(|secs| now_ms.saturating_add(secs.saturating_mul(1000)));
    Ok(Token {
        access_token: access.to_string(),
        refresh_token: refresh,
        token_type: value
            .get("token_type")
            .and_then(Value::as_str)
            .map(|s| s.to_string()),
        expires_at_ms: expires,
        scope: value
            .get("scope")
            .and_then(Value::as_str)
            .map(|s| s.to_string()),
    })
}

async fn post_token(cfg: &OAuthConfig, form: &[(&str, String)]) -> Result<Token, String> {
    let url = validate_endpoint(&cfg.token_url, "token 地址")?;
    ensure_resolved_public(&url, "token 地址").await?;
    let client = token_client(host_is_loopback(&url))?;
    let resp = client
        .post(url)
        .form(form)
        .send()
        .await
        .map_err(|e| {
            let reason = if e.is_timeout() {
                "请求超时"
            } else if e.is_connect() {
                "无法建立连接"
            } else {
                "网络请求失败"
            };
            format!("MCP_OAUTH_NETWORK: token 交换{reason}")
        })?;
    let status = resp.status();
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("MCP_OAUTH_NETWORK: 读取 token 响应失败（{e}）"))?;
    if bytes.len() > MAX_TOKEN_BYTES {
        return Err("MCP_OAUTH_INVALID_RESPONSE: token 响应过大".into());
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| "MCP_OAUTH_INVALID_RESPONSE: token 端点没有返回 JSON".to_string())?;
    if !status.is_success() {
        let code = value
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("token_rejected");
        let desc = value
            .get("error_description")
            .and_then(Value::as_str)
            .unwrap_or("");
        let detail = crate::mcp::truncate(&format!("{code} {desc}").trim().to_string(), 160);
        return Err(format!(
            "MCP_OAUTH_TOKEN_REJECTED: token 端点拒绝（HTTP {status}）：{detail}"
        ));
    }
    token_from_json(&value, crate::now_ms_u64() as i64)
}

async fn exchange_code(
    cfg: &OAuthConfig,
    code: &str,
    redirect_uri: &str,
    verifier: &str,
) -> Result<Token, String> {
    post_token(
        cfg,
        &[
            ("grant_type", "authorization_code".to_string()),
            ("code", code.to_string()),
            ("redirect_uri", redirect_uri.to_string()),
            ("client_id", cfg.client_id.clone()),
            ("code_verifier", verifier.to_string()),
        ],
    )
    .await
}

fn token_account(server_id: &str) -> String {
    format!("mcp-oauth:{server_id}")
}

pub fn load_token(store: &Store, server_id: &str) -> Option<Token> {
    let raw = store.vault().get_secret(&token_account(server_id))?;
    serde_json::from_str::<Token>(&raw).ok()
}

pub fn save_token(store: &Store, server_id: &str, token: &Token) -> Result<(), String> {
    let raw = serde_json::to_string(token)
        .map_err(|e| format!("MCP_OAUTH_INVALID_RESPONSE: 令牌序列化失败（{e}）"))?;
    store.vault().set_secret(&token_account(server_id), &raw)
}

pub fn delete_token(store: &Store, server_id: &str) {
    store.vault().delete_secret(&token_account(server_id));
}

fn token_expiring(token: &Token, now_ms: i64) -> bool {
    match token.expires_at_ms {
        Some(at) => at <= now_ms.saturating_add(EXPIRY_SKEW_MS),
        None => false,
    }
}

pub fn status_for(store: &Store, server_id: &str, now_ms: i64) -> &'static str {
    match load_token(store, server_id) {
        None => AUTH_NONE,
        Some(t) if token_expiring(&t, now_ms) => AUTH_EXPIRED,
        Some(_) => AUTH_AUTHORIZED,
    }
}

/// 发起授权：起本机回调 → 交给调用方打开系统浏览器 → 校验 state → 换 token → 落系统凭据库
pub async fn authorize<F>(
    store: &Store,
    server_id: &str,
    cfg: &OAuthConfig,
    open: F,
) -> Result<Token, String>
where
    F: FnOnce(&str) -> Result<(), String>,
{
    let cfg = normalize_config(cfg)?;
    let auth_url = validate_endpoint(&cfg.authorization_url, "授权地址")?;
    let token_url = validate_endpoint(&cfg.token_url, "token 地址")?;
    ensure_resolved_public(&auth_url, "授权地址").await?;
    ensure_resolved_public(&token_url, "token 地址").await?;

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("MCP_OAUTH_NETWORK: 无法启动本机回调（{e}）"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("MCP_OAUTH_NETWORK: 无法读取回调端口（{e}）"))?
        .port();
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");
    let state = gen_state();
    let verifier = gen_verifier();
    let challenge = code_challenge(&verifier);
    let url = build_authorize_url(&cfg, &redirect_uri, &state, &challenge)?;

    open(url.as_str()).map_err(|e| format!("MCP_OAUTH_OPEN_FAILED: {e}"))?;

    let callback = tokio::time::timeout(
        Duration::from_secs(CALLBACK_TIMEOUT_SECS),
        wait_callback(listener),
    )
    .await
    .map_err(|_| "MCP_OAUTH_TIMEOUT: 等待浏览器授权超时，请重试".to_string())??;

    if let Some(err) = callback.error {
        let desc = callback.error_description.unwrap_or_default();
        let detail = crate::mcp::truncate(&format!("{err} {desc}").trim().to_string(), 160);
        return Err(format!("MCP_OAUTH_DENIED: 授权被拒绝：{detail}"));
    }
    if callback.state.as_deref() != Some(state.as_str()) {
        return Err("MCP_OAUTH_STATE_MISMATCH: 回调 state 校验失败，已中止".into());
    }
    let code = callback
        .code
        .filter(|c| !c.trim().is_empty())
        .ok_or_else(|| "MCP_OAUTH_CALLBACK_INVALID: 回调缺少授权码".to_string())?;

    let token = exchange_code(&cfg, &code, &redirect_uri, &verifier).await?;
    save_token(store, server_id, &token)?;
    Ok(token)
}

async fn refresh(
    store: &Store,
    server_id: &str,
    cfg: &OAuthConfig,
    token: &Token,
) -> Result<Token, String> {
    let Some(refresh_token) = token.refresh_token.as_deref() else {
        return Err("MCP_OAUTH_REAUTH_REQUIRED: 授权已过期，请重新授权".into());
    };
    let cfg = normalize_config(cfg)?;
    let next = post_token(
        &cfg,
        &[
            ("grant_type", "refresh_token".to_string()),
            ("refresh_token", refresh_token.to_string()),
            ("client_id", cfg.client_id.clone()),
        ],
    )
    .await
    .map_err(|e| {
        if e.starts_with("MCP_OAUTH_TOKEN_REJECTED") {
            format!("MCP_OAUTH_REAUTH_REQUIRED: 刷新令牌已被提供方拒绝，请重新授权")
        } else {
            e
        }
    })?;
    let next = Token {
        refresh_token: next
            .refresh_token
            .or_else(|| token.refresh_token.clone()),
        ..next
    };
    save_token(store, server_id, &next)?;
    Ok(next)
}

/// 连接前取可用令牌：过期且可刷新则续期，不可刷新则要求重新授权
pub async fn ensure_access_token(
    store: &Store,
    server_id: &str,
    cfg: &OAuthConfig,
    now_ms: i64,
) -> Result<Token, String> {
    let token = load_token(store, server_id)
        .ok_or_else(|| "MCP_OAUTH_REQUIRED: 尚未完成 OAuth 授权，请先点击「授权」".to_string())?;
    if !token_expiring(&token, now_ms) {
        return Ok(token);
    }
    if token.refresh_token.is_none() {
        return Err("MCP_OAUTH_REAUTH_REQUIRED: 授权已过期，请重新授权".into());
    }
    match refresh(store, server_id, cfg, &token).await {
        Ok(t) => Ok(t),
        Err(e) if e.starts_with("MCP_OAUTH_REAUTH_REQUIRED") => {
            delete_token(store, server_id);
            Err(e)
        }
        Err(e) => Err(e),
    }
}

/// 服务器返回 401 后强制续期一次（连接阶段使用）
pub async fn force_refresh(
    store: &Store,
    server_id: &str,
    cfg: &OAuthConfig,
) -> Result<Token, String> {
    let token = load_token(store, server_id)
        .ok_or_else(|| "MCP_OAUTH_REAUTH_REQUIRED: 授权已失效，请重新授权".to_string())?;
    match refresh(store, server_id, cfg, &token).await {
        Ok(t) => Ok(t),
        Err(e) if e.starts_with("MCP_OAUTH_REAUTH_REQUIRED") => {
            delete_token(store, server_id);
            Err(e)
        }
        Err(e) => Err(e),
    }
}

/// 执行面使用：未授权/已过期的连接器不下发，避免把已失效凭据交给引擎
pub fn runtime_authorized(store: &Store, server_id: &str, now_ms: i64) -> Option<Token> {
    let token = load_token(store, server_id)?;
    if token_expiring(&token, now_ms) {
        None
    } else {
        Some(token)
    }
}

/// 审计用：只保留来源主机，不带路径、查询串或令牌
pub fn endpoint_host(raw: &str) -> String {
    reqwest::Url::parse(raw.trim())
        .ok()
        .and_then(|u| u.host_str().map(str::to_string))
        .unwrap_or_default()
}

pub fn bearer_header(token: &Token) -> (String, String) {
    (
        "Authorization".to_string(),
        format!("Bearer {}", token.access_token),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener as StdTcpListener;

    fn cfg(auth: &str, token: &str) -> OAuthConfig {
        OAuthConfig {
            authorization_url: auth.to_string(),
            token_url: token.to_string(),
            client_id: "tt-client".into(),
            scopes: vec!["read".into(), "write".into()],
        }
    }

    fn temp_store(name: &str) -> (Store, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-mcp-oauth-{name}-{}-{}",
            std::process::id(),
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        (Store::open(dir.join("t.db")).unwrap(), dir)
    }

    #[test]
    fn pkce_challenge_matches_rfc7636_vector() {
        assert_eq!(
            code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn endpoint_rules_allow_loopback_http_only() {
        assert!(validate_endpoint("https://id.example.com/authorize", "授权地址").is_ok());
        assert!(validate_endpoint("http://127.0.0.1:9000/authorize", "授权地址").is_ok());
        assert!(validate_endpoint("http://localhost:9000/authorize", "授权地址").is_ok());
        assert!(validate_endpoint("http://id.example.com/authorize", "授权地址").is_err());
        assert!(validate_endpoint("https://user:pass@id.example.com/a", "授权地址").is_err());
        assert!(validate_endpoint("ftp://id.example.com/a", "授权地址").is_err());
    }

    #[test]
    fn private_and_reserved_ips_are_not_public() {
        for ip in [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "2001:db8::1",
        ] {
            assert!(!ip_is_public(ip.parse().unwrap()), "{ip} 不应视为公网");
        }
        for ip in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"] {
            assert!(ip_is_public(ip.parse().unwrap()), "{ip} 应视为公网");
        }
    }

    #[test]
    fn config_normalization_dedupes_scopes_and_rejects_bad_client() {
        let mut c = cfg(
            "https://id.example.com/authorize",
            "https://id.example.com/token",
        );
        c.scopes = vec!["read".into(), " read ".into(), "write".into()];
        let n = normalize_config(&c).unwrap();
        assert_eq!(n.scopes, vec!["read", "write"]);
        c.client_id = "  ".into();
        assert!(normalize_config(&c)
            .unwrap_err()
            .starts_with("MCP_OAUTH_INVALID_CONFIG"));
    }

    #[test]
    fn authorize_url_carries_pkce_and_scope() {
        let c = cfg(
            "https://id.example.com/authorize?prompt=consent",
            "https://id.example.com/token",
        );
        let url = build_authorize_url(
            &c,
            "http://127.0.0.1:4567/callback",
            "state-1",
            "challenge-1",
        )
        .unwrap();
        let pairs = url.query_pairs().into_owned().collect::<BTreeMap<_, _>>();
        assert_eq!(pairs["response_type"], "code");
        assert_eq!(pairs["client_id"], "tt-client");
        assert_eq!(pairs["redirect_uri"], "http://127.0.0.1:4567/callback");
        assert_eq!(pairs["state"], "state-1");
        assert_eq!(pairs["code_challenge"], "challenge-1");
        assert_eq!(pairs["code_challenge_method"], "S256");
        assert_eq!(pairs["scope"], "read write");
        assert_eq!(pairs["prompt"], "consent", "已有查询串不得被覆盖");
    }

    #[test]
    fn callback_parses_code_error_and_state() {
        let ok = parse_callback_target("/callback?code=abc%2F1&state=s1").unwrap();
        assert_eq!(ok.code.as_deref(), Some("abc/1"));
        assert_eq!(ok.state.as_deref(), Some("s1"));
        assert!(ok.error.is_none());
        let err = parse_callback_target("/callback?error=access_denied&error_description=nope&state=s2")
            .unwrap();
        assert_eq!(err.error.as_deref(), Some("access_denied"));
        assert_eq!(err.error_description.as_deref(), Some("nope"));
        assert!(err.code.is_none());
        assert!(parse_callback_target("/favicon.ico").is_none());
    }

    #[test]
    fn token_json_parses_expiry_and_refresh() {
        let t = token_from_json(
            &serde_json::json!({
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "token_type": "Bearer",
                "expires_in": "3600",
                "scope": "read"
            }),
            1_700_000_000_000,
        )
        .unwrap();
        assert_eq!(t.access_token, "at-1");
        assert_eq!(t.refresh_token.as_deref(), Some("rt-1"));
        assert_eq!(t.expires_at_ms, Some(1_700_000_000_000 + 3_600_000));
        let err = token_from_json(&serde_json::json!({ "token_type": "Bearer" }), 0).unwrap_err();
        assert!(err.starts_with("MCP_OAUTH_INVALID_RESPONSE"), "{err}");
    }

    #[test]
    fn token_storage_is_keyring_and_status_tracks_expiry() {
        let (store, dir) = temp_store("store");
        let id = format!("mcp-{}", uuid::Uuid::new_v4());
        let base = 1_700_000_000_000i64;
        assert_eq!(status_for(&store, &id, base), AUTH_NONE);
        let t = Token {
            access_token: "secret-at".into(),
            refresh_token: Some("secret-rt".into()),
            token_type: Some("Bearer".into()),
            expires_at_ms: Some(base + 3_600_000),
            scope: None,
        };
        save_token(&store, &id, &t).unwrap();
        assert_eq!(status_for(&store, &id, base), AUTH_AUTHORIZED);
        // 距到期不足 60 秒即视为待续期
        assert_eq!(status_for(&store, &id, base + 3_600_000 - 1), AUTH_EXPIRED);
        assert_eq!(load_token(&store, &id).unwrap().access_token, "secret-at");
        delete_token(&store, &id);
        assert!(load_token(&store, &id).is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn stored_config_round_trip_and_ignores_empty() {
        assert!(parse_stored_config("{}").is_none());
        assert!(parse_stored_config("").is_none());
        assert!(parse_stored_config("not-json").is_none());
        let raw = serde_json::to_string(&cfg(
            "https://id.example.com/authorize",
            "https://id.example.com/token",
        ))
        .unwrap();
        let parsed = parse_stored_config(&raw).unwrap();
        assert_eq!(parsed.client_id, "tt-client");
        assert_eq!(parsed.scopes.len(), 2);
    }

    // ── 本地假 OAuth + MCP 服务：验证 PKCE、token 交换与 Bearer 注入 ──

    struct FakeOAuth {
        listener: StdTcpListener,
        challenge: std::sync::Arc<std::sync::Mutex<String>>,
        verifier_seen: std::sync::Arc<std::sync::Mutex<bool>>,
        bearer_seen: std::sync::Arc<std::sync::Mutex<bool>>,
    }

    fn read_http(stream: &mut std::net::TcpStream) -> (String, String) {
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        loop {
            let n = stream.read(&mut tmp).unwrap_or(0);
            if n == 0 {
                break;
            }
            buf.extend_from_slice(&tmp[..n]);
            if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&buf[..pos]).to_string();
                let len = head
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case("content-length")
                            .then(|| v.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = pos + 4;
                while buf.len() < body_start + len {
                    let n = stream.read(&mut tmp).unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&tmp[..n]);
                }
                let body = String::from_utf8_lossy(&buf[body_start..(body_start + len).min(buf.len())])
                    .to_string();
                return (head, body);
            }
        }
        (String::new(), String::new())
    }

    fn respond(stream: &mut std::net::TcpStream, status: &str, body: &str) {
        let resp = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(resp.as_bytes());
        let _ = stream.flush();
    }

    #[test]
    fn authorization_flow_and_bearer_handshake_work_end_to_end() {
        // 服务端：/token 校验 PKCE 并返回令牌；/mcp 校验 Bearer 后完成两次 JSON-RPC
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let base = format!("http://127.0.0.1:{port}");
        let challenge = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
        let verifier_seen = std::sync::Arc::new(std::sync::Mutex::new(false));
        let bearer_seen = std::sync::Arc::new(std::sync::Mutex::new(false));
        let c = challenge.clone();
        let v = verifier_seen.clone();
        let b = bearer_seen.clone();
        let server_base = base.clone();        let handle = std::thread::spawn(move || {
            // /token 1 次 + /mcp 3 次（initialize、initialized 通知、tools/list）
            for _ in 0..4 {
                let Ok((mut stream, _)) = listener.accept() else {
                    return;
                };
                let (head, body) = read_http(&mut stream);
                let path = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                if path.starts_with("/token") {
                    // 表单参数足够简单，直接子串匹配
                    let verifier_ok = body.contains("code_verifier=")
                        && {
                            let expected = code_challenge(
                                body.split("code_verifier=")
                                    .nth(1)
                                    .and_then(|s| s.split('&').next())
                                    .unwrap_or(""),
                            );
                            expected == *c.lock().unwrap()
                        };
                    *v.lock().unwrap() = verifier_ok;
                    if !verifier_ok || !body.contains("grant_type=authorization_code") {
                        respond(&mut stream, "400 Bad Request", r#"{"error":"invalid_grant"}"#);
                        continue;
                    }
                    respond(
                        &mut stream,
                        "200 OK",
                        r#"{"access_token":"at-test","refresh_token":"rt-test","token_type":"Bearer","expires_in":3600}"#,
                    );
                    continue;
                }
                if path.starts_with("/mcp") {
                    let authorized = head
                        .lines()
                        .any(|l| l.eq_ignore_ascii_case("authorization: Bearer at-test"));
                    *b.lock().unwrap() = authorized;
                    if !authorized {
                        respond(&mut stream, "401 Unauthorized", r#"{"error":"unauthorized"}"#);
                        continue;
                    }
                    let id = serde_json::from_str::<Value>(&body)
                        .ok()
                        .and_then(|v| v.get("id").cloned())
                        .unwrap_or(serde_json::json!(1));
                    if body.contains("initialize") {
                        respond(
                            &mut stream,
                            "200 OK",
                            &serde_json::json!({
                                "jsonrpc": "2.0",
                                "id": id,
                                "result": { "protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": { "name": "fake", "version": "1" } }
                            })
                            .to_string(),
                        );
                    } else if body.contains("tools/list") {
                        respond(
                            &mut stream,
                            "200 OK",
                            &serde_json::json!({
                                "jsonrpc": "2.0",
                                "id": id,
                                "result": { "tools": [{ "name": "read_rows", "description": "读取数据", "inputSchema": { "type": "object" } }] }
                            })
                            .to_string(),
                        );
                    } else {
                        respond(&mut stream, "200 OK", r#"{"jsonrpc":"2.0","id":1,"result":{}}"#);
                    }
                    continue;
                }
                respond(&mut stream, "404 Not Found", "{}");
            }
        });

        let (store, dir) = temp_store("flow");
        let id = format!("mcp-{}", uuid::Uuid::new_v4());
        let c = cfg(&format!("{server_base}/authorize"), &format!("{server_base}/token"));

        // 用测试闭包代替系统浏览器：回放提供方回调（state/challenge 都从授权 URL 读取）
        let challenge_for_cb = challenge.clone();
        let result = tauri::async_runtime::block_on(authorize(
            &store,
            &id,
            &c,
            move |url| {
                let parsed = reqwest::Url::parse(url).map_err(|e| e.to_string())?;
                let challenge = parsed
                    .query_pairs()
                    .find(|(k, _)| k == "code_challenge")
                    .map(|(_, v)| v.to_string())
                    .unwrap_or_default();
                if challenge.is_empty() {
                    return Err("missing challenge".into());
                }
                // 记录 challenge，供 /token 端校验 PKCE
                *challenge_for_cb.lock().unwrap() = challenge;
                let state = parsed
                    .query_pairs()
                    .find(|(k, _)| k == "state")
                    .map(|(_, v)| v.to_string())
                    .unwrap();
                let redirect = parsed
                    .query_pairs()
                    .find(|(k, _)| k == "redirect_uri")
                    .map(|(_, v)| v.to_string())
                    .unwrap();
                let redirect_url = reqwest::Url::parse(&redirect).map_err(|e| e.to_string())?;
                let port = redirect_url.port().ok_or("redirect 缺少端口")?;
                let mut stream = std::net::TcpStream::connect(("127.0.0.1", port))
                    .map_err(|e| e.to_string())?;
                let req = format!(
                    "GET /callback?code=code-1&state={state} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                );
                // 只发回调、不读响应：回调监听在授权函数返回后才被轮询
                stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
                stream.flush().map_err(|e| e.to_string())
            },
        ));
        let token = result.expect("授权流程应成功");
        assert_eq!(token.access_token, "at-test");
        assert_eq!(token.refresh_token.as_deref(), Some("rt-test"));
        assert_eq!(status_for(&store, &id, 1_700_000_000_000), AUTH_AUTHORIZED);

        let target = super::super::ProbeTarget {
            server_id: id.clone(),
            name: "假 MCP".into(),
            transport: "streamable_http".into(),
            command: String::new(),
            args: vec![],
            url: format!("{server_base}/mcp"),
            env: BTreeMap::new(),
            headers: BTreeMap::new(),
            timeout_secs: 5,
            auth: Some(c.clone()),
            access_token: Some(token.access_token.clone()),
        };
        let tools = tauri::async_runtime::block_on(super::super::probe(&target)).expect("握手应成功");
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].name, "read_rows");
        assert!(*bearer_seen.lock().unwrap(), "握手请求必须带 Bearer 令牌");
        assert!(*verifier_seen.lock().unwrap(), "token 交换必须携带与 challenge 匹配的 verifier");

        delete_token(&store, &id);
        std::fs::remove_dir_all(&dir).ok();
        handle.join().ok();
    }
}
