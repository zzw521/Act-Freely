//! Auth 核心逻辑（command 与逻辑分离，纯函数可单测）
//!
//! 网页验证登录 Loopback 方案（云端 Phase A 前的本地过渡）：
//! - Rust 起 127.0.0.1 随机端口一次性回调服务
//! - 浏览器在 Auth 服务完成登录/注册后 302 回跳携带 code/state
//! - state 校验通过后 code 换 token；refresh token 入 keyring（Vault），
//!   账号 profile 入 settings_kv
//!
//! Mock 开关（开发规范）：TT_AUTH_MOCK 缺省开启，联调设 false。

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::store::settings;
use crate::store::Store;

/// settings_kv 中账号 profile 的键（JSON 字符串）
pub const PROFILE_KEY: &str = "auth.profile";
/// keyring 中 refresh token 的账户名
pub const REFRESH_ACCOUNT: &str = "auth-refresh-token";
/// 登录态变更事件（前端 auth store 监听）
pub const EVENT_AUTH: &str = "auth-event";
/// loopback 回调等待上限
pub const LOGIN_TIMEOUT_SECS: u64 = 120;

/// 登录成功回跳后返回给浏览器的提示页
pub const SUCCESS_HTML: &str = r#"<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>登录成功 - 通天办公</title></head><body style="font-family:'Microsoft YaHei',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#EEF2FF"><div style="background:#fff;border-radius:16px;padding:36px 48px;box-shadow:0 16px 48px rgba(79,70,229,.14);text-align:center"><h2 style="color:#059669;margin:0 0 8px">登录成功</h2><p style="color:#6B7280;font-size:13px;margin:0">验证通过，可关闭此页面返回通天办公</p></div></body></html>"#;

/// Mock 开关：TT_AUTH_MOCK 缺省开启（开发阶段），设 false/0/off 走真实网页登录
pub fn mock_enabled() -> bool {
    match std::env::var("TT_AUTH_MOCK") {
        Ok(v) => !matches!(v.trim(), "false" | "0" | "off"),
        Err(_) => true,
    }
}

/// Auth 服务地址（本地过渡实现；云端 Phase A 上线后仅换此 URL）
pub fn auth_server_url() -> String {
    std::env::var("TT_AUTH_SERVER_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:18080".into())
        .trim_end_matches('/')
        .to_string()
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

/// 授权码换 token（Auth 服务 /api/auth/exchange）
pub async fn exchange_code(base_url: &str, code: &str, state: &str) -> Result<Value, String> {
    let resp = crate::proxy::client()
        .post(format!("{base_url}/api/auth/exchange"))
        .json(&json!({ "code": code, "state": state }))
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| format!("AUTH_EXCHANGE_FAILED: {e}"))?;
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

/// 尽力而为的远端 session 吊销（Auth 服务不可达时不阻断本地退出）
pub async fn revoke_remote(base_url: &str, refresh_token: &str) {
    let _ = crate::proxy::client()
        .post(format!("{base_url}/api/auth/logout"))
        .json(&json!({ "refreshToken": refresh_token }))
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await;
}

/// 登录成功持久化：profile → settings_kv，refresh token → keyring（Vault）
pub fn save_session(store: &Store, profile: &Value, refresh_token: &str) -> Result<(), String> {
    settings::set(store, PROFILE_KEY, &profile.to_string())?;
    store.vault().set_secret(REFRESH_ACCOUNT, refresh_token)
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
