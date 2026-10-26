//! Auth 核心逻辑（command 与逻辑分离，纯函数可单测）
//!
//! 网页验证登录 Loopback 方案（云端 Phase A 前的本地过渡）：
//! - Rust 起 127.0.0.1 随机端口一次性回调服务
//! - 浏览器在 Auth 服务完成登录/注册后 302 回跳携带 code/state
//! - state 校验通过后 code 换 token；refresh token 入 keyring（Vault），
//!   账号 profile 入 settings_kv
//!
//! 默认连接综合服务端（tongtian-server，127.0.0.1:8000）做真实账号验证；
//! 离线开发可设 TT_AUTH_MOCK=true 走模拟登录。

use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::store::settings;
use crate::store::Store;

/// settings_kv 中账号 profile 的键（JSON 字符串）
pub const PROFILE_KEY: &str = "auth.profile";
/// settings_kv 中账号服务地址的键（缺省用综合服务端）
pub const SERVER_SETTING_KEY: &str = "auth.serverUrl";
/// 综合服务端默认地址（tongtian-server/init.sh 的默认端口）
pub const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8000";
/// keyring 中 refresh token 的账户名
pub const REFRESH_ACCOUNT: &str = "auth-refresh-token";
/// 登录态变更事件（前端 auth store 监听）
pub const EVENT_AUTH: &str = "auth-event";
/// loopback 回调等待上限
pub const LOGIN_TIMEOUT_SECS: u64 = 120;

/// 登录成功回跳后返回给浏览器的提示页
pub const SUCCESS_HTML: &str = r#"<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>登录成功 - 通天办公</title></head><body style="font-family:'Microsoft YaHei',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#EEF2FF"><div style="background:#fff;border-radius:16px;padding:36px 48px;box-shadow:0 16px 48px rgba(79,70,229,.14);text-align:center"><h2 style="color:#059669;margin:0 0 8px">登录成功</h2><p style="color:#6B7280;font-size:13px;margin:0">验证通过，可关闭此页面返回通天办公</p></div></body></html>"#;

/// Mock 开关：只有显式 TT_AUTH_MOCK=true 才模拟登录；缺省连接综合服务端做真实验证。
/// 离线开发仍可设 true 跳过网络（与 dev-rules 的 Mock 开关要求一致）。
pub fn mock_enabled() -> bool {
    mock_from_env(std::env::var("TT_AUTH_MOCK").ok().as_deref())
}

pub fn mock_from_env(raw: Option<&str>) -> bool {
    matches!(
        raw.map(|v| v.trim().to_ascii_lowercase()).as_deref(),
        Some("true" | "1" | "on" | "yes")
    )
}

/// 账号 / 协作服务地址：环境变量 → settings_kv → 内置综合服务端（127.0.0.1:8000）
pub fn auth_server_url(store: &Store) -> String {
    let env = std::env::var("TT_AUTH_SERVER_URL").ok();
    let stored = settings::get(store, SERVER_SETTING_KEY).ok().flatten();
    resolve_server_url(env.as_deref(), stored.as_deref())
}

/// 纯解析函数（可单测）：显式配置优先，空值回落默认综合服务端
pub fn resolve_server_url(env_value: Option<&str>, stored: Option<&str>) -> String {
    let picked = env_value
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .or_else(|| stored.map(str::trim).filter(|v| !v.is_empty()));
    match picked {
        Some(url) => url.trim_end_matches('/').to_string(),
        None => DEFAULT_SERVER_URL.to_string(),
    }
}

/// 地址校验：登录授权码与令牌不会明文走远程网络
pub fn validate_server_url(base: &str) -> Result<(), String> {
    if base.trim().is_empty() {
        return Err("AUTH_SERVER_URL_INVALID: 账号服务地址为空".into());
    }
    let url = reqwest::Url::parse(base)
        .map_err(|_| format!("AUTH_SERVER_URL_INVALID: 账号服务地址无效（{base}）"))?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(format!("AUTH_SERVER_URL_INVALID: 账号服务地址无效（{base}）"));
    }
    if url.scheme() != "https" && !is_loopback_base(base) {
        return Err(format!(
            "AUTH_SERVER_INSECURE: 远程账号服务必须使用 HTTPS（当前：{base}）"
        ));
    }
    Ok(())
}

/// 回环地址永远直连（系统代理常不 bypass 本机），远程走用户的代理配置
pub fn client_for_base(base: &str) -> reqwest::Client {
    if is_loopback_base(base) {
        crate::proxy::loopback_client()
    } else {
        crate::proxy::client()
    }
}

fn network_reason(err: &reqwest::Error) -> &'static str {
    if err.is_timeout() {
        "连接或读取超时"
    } else if err.is_connect() {
        "服务未启动或端口未监听"
    } else {
        "网络请求失败"
    }
}

/// 探测综合服务端：只有 `/health` 返回 ok 且 service=tongtian-server 才算可用，
/// 避免把别的本机服务当成账号服务端（登录页会打开在错误端口上）。
pub async fn probe_server(base: &str) -> Result<Value, String> {
    validate_server_url(base)?;
    let resp = client_for_base(base)
        .get(format!("{base}/health"))
        .timeout(std::time::Duration::from_secs(3))
        .send()
        .await
        .map_err(|e| {
            format!(
                "AUTH_SERVER_UNREACHABLE: 无法连接账号服务（{base}）：{}。请先运行 tongtian-server/init.sh 启动综合服务端",
                network_reason(&e)
            )
        })?;
    let status = resp.status();
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("AUTH_SERVER_BAD_RESPONSE: 账号服务健康检查响应无法解析（{e}）"))?;
    if !status.is_success() || body.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        return Err(format!(
            "AUTH_SERVER_BAD_RESPONSE: 账号服务健康检查失败（HTTP {}）",
            status.as_u16()
        ));
    }
    let service = body.get("service").and_then(|v| v.as_str()).unwrap_or("");
    if service != "tongtian-server" {
        return Err(format!(
            "AUTH_SERVER_MISMATCH: 该地址不是通天办公综合服务端（service={}）",
            if service.is_empty() { "未知" } else { service }
        ));
    }
    Ok(body)
}

/// 探活结果视图（不抛错，供设置页展示具体原因）
pub async fn probe_server_view(base: &str) -> Value {
    match probe_server(base).await {
        Ok(body) => json!({
            "baseUrl": base,
            "reachable": true,
            "service": body.get("service").cloned().unwrap_or(Value::Null),
            "version": body.get("version").cloned().unwrap_or(Value::Null),
            "error": null,
        }),
        Err(error) => json!({
            "baseUrl": base,
            "reachable": false,
            "service": null,
            "version": null,
            "error": error,
        }),
    }
}

/// 登录前必须确认服务可用，否则浏览器只会打开一个连不上的页面并空等回调
pub async fn ensure_server_ready(base: &str) -> Result<(), String> {
    probe_server(base).await.map(|_| ())
}

/// 生成登录流程 state（防 CSRF，回调时校验一致性）
pub fn gen_state() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Mock 用户 profile（开发自测，不发浏览器不起回调）
pub fn mock_profile() -> Value {
    json!({
        "uid": "tt-mock-8f3a2c91",
        "nickname": "张先生",
        "email": "mock@tongtian.local",
        "plan": "个人免费版",
        "mock": true,
    })
}

/// 百分号解码（回调参数可能含 URL 编码字符）
pub fn urldecode(s: &str) -> String {
    fn hex(b: u8) -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(b - b'a' + 10),
            b'A'..=b'F' => Some(b - b'A' + 10),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 3 <= bytes.len() => {
                if let (Some(h), Some(l)) = (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                    out.push(h * 16 + l);
                    i += 3;
                    continue;
                }
                out.push(b'%');
                i += 1;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

/// 百分号编码（仅保留 unreserved 字符，用于拼接 redirect_uri）
pub fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// 从 loopback 回调的 HTTP 请求报文解析 code/state（仅 GET）
pub fn parse_callback_request(raw: &str) -> Option<(String, String)> {
    let first = raw.lines().next()?;
    let mut parts = first.split_whitespace();
    if parts.next()? != "GET" {
        return None;
    }
    let path = parts.next()?;
    let query = path.split_once('?')?.1;
    let mut code = None;
    let mut state = None;
    for kv in query.split('&') {
        if let Some((k, v)) = kv.split_once('=') {
            match k {
                "code" => code = Some(urldecode(v)),
                "state" => state = Some(urldecode(v)),
                _ => {}
            }
        }
    }
    Some((code?, state?))
}

/// 一次性 loopback 回调服务：阻塞直到收到 GET /callback?code=&state=
/// 无关请求（如 favicon）响应后继续等待，避免浏览器挂起
pub async fn wait_loopback_callback(
    listener: tokio::net::TcpListener,
) -> Result<(String, String), String> {
    loop {
        let (mut socket, _) = listener
            .accept()
            .await
            .map_err(|e| format!("CALLBACK_ACCEPT_FAILED: {e}"))?;
        let mut buf = vec![0u8; 8192];
        let n = socket.read(&mut buf).await.unwrap_or(0);
        let raw = String::from_utf8_lossy(&buf[..n]).to_string();
        let parsed = parse_callback_request(&raw);
        let (status, body) = if parsed.is_some() {
            ("200 OK", SUCCESS_HTML)
        } else {
            ("404 Not Found", "Not Found")
        };
        let resp = format!(
            "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = socket.write_all(resp.as_bytes()).await;
        if let Some(pair) = parsed {
            return Ok(pair);
        }
    }
}

/// 授权码换 token（综合服务端 /api/auth/exchange）
pub async fn exchange_code(base_url: &str, code: &str, state: &str) -> Result<Value, String> {
    validate_server_url(base_url)?;
    let resp = client_for_base(base_url)
        .post(format!("{base_url}/api/auth/exchange"))
        .json(&json!({ "code": code, "state": state }))
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| {
            format!(
                "AUTH_EXCHANGE_FAILED: 无法连接账号服务（{base_url}）：{}",
                network_reason(&e)
            )
        })?;
    let status = resp.status();
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("AUTH_EXCHANGE_PARSE: {e}"))?;
    if !status.is_success() || body.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        let msg = body
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("授权码交换失败");
        return Err(format!("AUTH_EXCHANGE_REJECTED: {msg}"));
    }
    Ok(body)
}

/// 尽力而为的远端 session 吊销（账号服务不可达时不阻断本地退出）
pub async fn revoke_remote(base_url: &str, refresh_token: &str) {
    if validate_server_url(base_url).is_err() {
        return;
    }
    let _ = client_for_base(base_url)
        .post(format!("{base_url}/api/auth/logout"))
        .json(&json!({ "refreshToken": refresh_token }))
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await;
}

/// 登录成功持久化：profile → settings_kv，refresh token → keyring（Vault）
pub fn save_session(store: &Store, profile: &Value, refresh_token: &str) -> Result<(), String> {
    settings::set(store, PROFILE_KEY, &profile.to_string())?;
    store.vault().set_secret(REFRESH_ACCOUNT, refresh_token)?;
    invalidate_access_token();
    Ok(())
}

// ── 访问令牌（协作等在线接口用；只在内存缓存，不落盘） ─────────────

/// access token 内存缓存：refresh token 轮换后必须失效
struct CachedAccess {
    token: String,
    expires_at_ms: i64,
}

static ACCESS_CACHE: Mutex<Option<CachedAccess>> = Mutex::new(None);
/// 并发命令只允许一个刷新请求，避免 refresh token 轮换互相作废
static REFRESH_LOCK: OnceLock<tokio::sync::Mutex<()>> = OnceLock::new();

const ACCESS_EXPIRY_SKEW_MS: i64 = 60_000;

fn now_ms_i64() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

pub fn invalidate_access_token() {
    *ACCESS_CACHE.lock().unwrap_or_else(|e| e.into_inner()) = None;
}

fn cached_access_token() -> Option<String> {
    let cache = ACCESS_CACHE.lock().unwrap_or_else(|e| e.into_inner());
    cache
        .as_ref()
        .filter(|item| item.expires_at_ms > now_ms_i64())
        .map(|item| item.token.clone())
}

fn is_loopback_base(base: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(base) else {
        return false;
    };
    matches!(
        url.host_str(),
        Some("localhost") | Some("::1") | Some("0.0.0.0")
    ) || url
        .host_str()
        .map(|host| host.starts_with("127."))
        .unwrap_or(false)
}

/// 取有效 access token：优先内存缓存，过期后用一个 refresh token 换新并轮换落库。
pub async fn access_token(store: &Store) -> Result<String, String> {
    if let Some(token) = cached_access_token() {
        return Ok(token);
    }
    refresh_access_token(store, false).await
}

/// 丢弃缓存后强制刷新（接口返回 401 时重试一次）
pub async fn force_access_token(store: &Store) -> Result<String, String> {
    refresh_access_token(store, true).await
}

async fn refresh_access_token(store: &Store, force: bool) -> Result<String, String> {
    let lock = REFRESH_LOCK.get_or_init(|| tokio::sync::Mutex::new(()));
    let _guard = lock.lock().await;
    if !force {
        if let Some(token) = cached_access_token() {
            return Ok(token);
        }
    } else {
        invalidate_access_token();
    }

    let refresh = store
        .vault()
        .get_secret(REFRESH_ACCOUNT)
        .filter(|token| !token.trim().is_empty())
        .ok_or_else(|| "COLLAB_LOGIN_REQUIRED: 请先登录真实账号再使用项目协作".to_string())?;
    if refresh.starts_with("mock-") {
        return Err(
            "COLLAB_LOGIN_REQUIRED: 当前是 Mock 登录，项目协作需要连接真实账号服务（TT_AUTH_MOCK=false）"
                .to_string(),
        );
    }

    let base = auth_server_url(store);
    validate_server_url(&base)?;
    let client = client_for_base(&base);
    let resp = client
        .post(format!("{base}/api/v1/auth/refresh"))
        .json(&json!({ "refreshToken": refresh }))
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("COLLAB_AUTH_NETWORK: 无法连接账号服务（{e}）"))?;
    let status = resp.status();
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("COLLAB_AUTH_REFRESH_FAILED: 账号服务响应无法解析（{e}）"))?;
    if !status.is_success() || body.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        let message = body
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("刷新登录态失败");
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(format!("COLLAB_AUTH_EXPIRED: {message}"));
        }
        return Err(format!("COLLAB_AUTH_REFRESH_FAILED: {message}"));
    }

    let access = body
        .get("accessToken")
        .and_then(|v| v.as_str())
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| "COLLAB_AUTH_REFRESH_FAILED: 账号服务响应缺少 accessToken".to_string())?;
    let expires_in = body.get("expiresIn").and_then(|v| v.as_u64()).unwrap_or(900);
    let rotated = body
        .get("refreshToken")
        .and_then(|v| v.as_str())
        .filter(|v| !v.trim().is_empty())
        .unwrap_or(&refresh);
    // 轮换后的 refresh token 必须落库；落不下去就如实失败，避免下次静默用失效凭据
    store.vault().set_secret(REFRESH_ACCOUNT, rotated)?;
    if let Some(profile) = body.get("profile") {
        let _ = settings::set(store, PROFILE_KEY, &profile.to_string());
    }

    let ttl_ms = (expires_in as i64) * 1000 - ACCESS_EXPIRY_SKEW_MS;
    let expires_at_ms = now_ms_i64() + ttl_ms.max(30_000);
    *ACCESS_CACHE.lock().unwrap_or_else(|e| e.into_inner()) = Some(CachedAccess {
        token: access.to_string(),
        expires_at_ms,
    });
    Ok(access.to_string())
}

/// 是否持有可刷新的真实登录态（只看本机落库，不发起网络请求，不读取 token 内容）
pub fn has_real_session(store: &Store) -> bool {
    store
        .vault()
        .get_secret(REFRESH_ACCOUNT)
        .map(|token| {
            let token = token.trim();
            !token.is_empty() && !token.starts_with("mock-")
        })
        .unwrap_or(false)
}

/// 读取账号 profile（未登录返回 None）
pub fn load_profile(store: &Store) -> Option<Value> {
    settings::get(store, PROFILE_KEY)
        .ok()
        .flatten()
        .filter(|s| !s.trim().is_empty())
        .and_then(|s| serde_json::from_str(&s).ok())
}

/// 清理边界：keyring token + 账号 profile。本地任务/会话数据不删除（local-first）
pub fn clear_session(store: &Store) {
    store.vault().delete_secret(REFRESH_ACCOUNT);
    let _ = settings::delete(store, PROFILE_KEY);
    invalidate_access_token();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-auth-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_save_load_clear_session_boundary() {
        let (store, dir) = temp_store("session");
        // 无关设置项（模拟本地数据）
        settings::set(&store, "theme", "dark").unwrap();

        let profile = mock_profile();
        save_session(&store, &profile, "rt-123").unwrap();
        let loaded = load_profile(&store).expect("已登录应有 profile");
        assert_eq!(loaded["uid"], "tt-mock-8f3a2c91");
        assert_eq!(
            store.vault().get_secret(REFRESH_ACCOUNT).as_deref(),
            Some("rt-123")
        );

        // 清理边界：token 与 profile 删除，无关设置保留
        clear_session(&store);
        assert!(load_profile(&store).is_none());
        assert!(store.vault().get_secret(REFRESH_ACCOUNT).is_none());
        assert_eq!(settings::get(&store, "theme").unwrap().as_deref(), Some("dark"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_load_profile_absent_or_invalid() {
        let (store, dir) = temp_store("absent");
        assert!(load_profile(&store).is_none());
        // 非法 JSON 容错为未登录
        settings::set(&store, PROFILE_KEY, "not-json").unwrap();
        assert!(load_profile(&store).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_parse_callback_request() {
        let raw = "GET /callback?code=abc123&state=xyz789 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n";
        assert_eq!(
            parse_callback_request(raw),
            Some(("abc123".into(), "xyz789".into()))
        );
        // URL 编码参数可还原
        let raw = "GET /callback?code=a%3Db&state=s+1 HTTP/1.1\r\n\r\n";
        assert_eq!(
            parse_callback_request(raw),
            Some(("a=b".into(), "s 1".into()))
        );
        // 缺 state / 非 GET / 无查询串均拒绝
        assert_eq!(parse_callback_request("GET /callback?code=abc HTTP/1.1\r\n"), None);
        assert_eq!(parse_callback_request("POST /callback?code=a&state=b HTTP/1.1\r\n"), None);
        assert_eq!(parse_callback_request("GET /callback HTTP/1.1\r\n"), None);
        assert_eq!(parse_callback_request(""), None);
    }

    #[test]
    fn test_resolve_server_url_precedence() {
        // 环境变量优先，其次 settings_kv，最后内置综合服务端
        assert_eq!(
            resolve_server_url(Some("https://auth.example.com/"), Some("http://127.0.0.1:9000")),
            "https://auth.example.com"
        );
        assert_eq!(
            resolve_server_url(Some("  "), Some("http://127.0.0.1:9000/")),
            "http://127.0.0.1:9000"
        );
        assert_eq!(resolve_server_url(None, None), DEFAULT_SERVER_URL);
        assert_eq!(DEFAULT_SERVER_URL, "http://127.0.0.1:8000");
    }

    #[test]
    fn test_mock_switch_defaults_to_real_mode() {
        // 缺省/显式关闭都走真实登录，只有显式开启才模拟
        assert!(!mock_from_env(None));
        assert!(!mock_from_env(Some("")));
        assert!(!mock_from_env(Some("false")));
        assert!(!mock_from_env(Some("0")));
        assert!(mock_from_env(Some("true")));
        assert!(mock_from_env(Some("TRUE")));
        assert!(mock_from_env(Some("on")));
        assert!(mock_from_env(Some("1")));
    }

    #[test]
    fn test_validate_server_url() {
        assert!(validate_server_url("http://127.0.0.1:8000").is_ok());
        assert!(validate_server_url("http://localhost:8000").is_ok());
        assert!(validate_server_url("https://auth.example.com").is_ok());
        // 远程明文 HTTP 会暴露授权码与令牌
        assert!(validate_server_url("http://auth.example.com")
            .unwrap_err()
            .contains("AUTH_SERVER_INSECURE"));
        assert!(validate_server_url("ftp://127.0.0.1")
            .unwrap_err()
            .contains("AUTH_SERVER_URL_INVALID"));
        assert!(validate_server_url("not-a-url")
            .unwrap_err()
            .contains("AUTH_SERVER_URL_INVALID"));
        assert!(validate_server_url("  ")
            .unwrap_err()
            .contains("AUTH_SERVER_URL_INVALID"));
    }

    /// 起一个只回 /health JSON 的本机假服务端
    async fn spawn_health_server(service: &'static str) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                let mut buf = [0u8; 2048];
                let _ = socket.read(&mut buf).await;
                let body = format!(r#"{{"ok":true,"service":"{service}","version":"test"}}"#);
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = socket.write_all(resp.as_bytes()).await;
            }
        });
        format!("http://{addr}")
    }

    #[tokio::test]
    async fn test_probe_server_accepts_integrated_server() {
        let base = spawn_health_server("tongtian-server").await;
        let body = probe_server(&base).await.unwrap();
        assert_eq!(body["service"], "tongtian-server");
        let view = probe_server_view(&base).await;
        assert_eq!(view["reachable"], true);
        assert_eq!(view["version"], "test");
        assert!(view["error"].is_null());
    }

    #[tokio::test]
    async fn test_probe_server_rejects_other_service() {
        let base = spawn_health_server("some-other-app").await;
        let err = probe_server(&base).await.unwrap_err();
        assert!(err.contains("AUTH_SERVER_MISMATCH"), "{err}");
        let view = probe_server_view(&base).await;
        assert_eq!(view["reachable"], false);
        assert!(view["error"].as_str().unwrap().contains("AUTH_SERVER_MISMATCH"));
    }

    #[tokio::test]
    async fn test_probe_server_unreachable_fails_fast() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        drop(listener);
        let err = probe_server(&base).await.unwrap_err();
        assert!(err.contains("AUTH_SERVER_UNREACHABLE"), "{err}");
    }

    #[test]
    fn test_gen_state_unique() {
        let a = gen_state();
        let b = gen_state();
        assert!(!a.is_empty());
        assert_ne!(a, b);
    }

    #[test]
    fn test_urlencode_roundtrip() {
        let s = "http://127.0.0.1:9999/callback";
        assert_eq!(urldecode(&urlencode(s)), s);
    }

    /// loopback 回调端到端：无关请求跳过，callback 请求取码返回
    #[tokio::test]
    async fn test_loopback_callback_roundtrip() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let handle = tokio::spawn(wait_loopback_callback(listener));

        // 先发无关请求（favicon），服务应跳过并继续等待
        let mut s = tokio::net::TcpStream::connect(addr).await.unwrap();
        s.write_all(b"GET /favicon.ico HTTP/1.1\r\nHost: x\r\n\r\n").await.unwrap();
        let mut buf = [0u8; 512];
        let n = s.read(&mut buf).await.unwrap();
        assert!(String::from_utf8_lossy(&buf[..n]).contains("404"));

        // 正式回调请求
        let mut s2 = tokio::net::TcpStream::connect(addr).await.unwrap();
        s2.write_all(b"GET /callback?code=c-1&state=st-9 HTTP/1.1\r\nHost: x\r\n\r\n")
            .await
            .unwrap();
        let mut buf2 = [0u8; 4096];
        let n2 = s2.read(&mut buf2).await.unwrap();
        assert!(String::from_utf8_lossy(&buf2[..n2]).contains("登录成功"));

        let (code, state) = handle.await.unwrap().unwrap();
        assert_eq!((code.as_str(), state.as_str()), ("c-1", "st-9"));
    }
}
