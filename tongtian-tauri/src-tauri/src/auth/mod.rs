//! 账号与登录态（网页验证登录 · Loopback 方案）
//!
//! command：
//! - auth_status: 登录态视图（settings_kv profile + keyring token）
//! - auth_login:  Mock 模式直接模拟登录；真实模式拉起浏览器网页登录，
//!                loopback 回调收码 → state 校验 → code 换 token → 持久化
//! - auth_logout: 尽力远端吊销 → 清理 keyring/profile（本地任务/会话保留）
//!
//! Mock 开关（开发规范）：TT_AUTH_MOCK 缺省开启；联调设 false。

pub mod service;

use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, State};
use tauri_plugin_opener::OpenerExt;

use crate::P0Services;

/// 并发登录互斥：同一时刻仅允许一条登录流程（loopback 端口/state 唯一性）
static LOGIN_BUSY: Mutex<bool> = Mutex::new(false);

/// 尝试占用登录槽（同步短临界区，guard 不跨 await）
fn try_begin_login() -> bool {
    let mut busy = LOGIN_BUSY.lock().unwrap_or_else(|e| e.into_inner());
    if *busy {
        return false;
    }
    *busy = true;
    true
}

/// 释放登录槽
fn end_login() {
    *LOGIN_BUSY.lock().unwrap_or_else(|e| e.into_inner()) = false;
}

/// 登录态查询：`{loggedIn, profile, mock}`
#[tauri::command]
pub async fn auth_status(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let profile = service::load_profile(&p0.store);
    Ok(json!({
        "loggedIn": profile.is_some(),
        "profile": profile,
        "mock": service::mock_enabled(),
    }))
}

/// 登录：Mock 模式模拟用户直登；真实模式走网页验证 + loopback 回调闭环
#[tauri::command]
pub async fn auth_login(
    app: AppHandle,
    p0: State<'_, Arc<P0Services>>,
) -> Result<Value, String> {
    if service::mock_enabled() {
        // 开发自测（Mock 开关规范）：不发浏览器不起回调，直接模拟登录
        let profile = service::mock_profile();
        service::save_session(&p0.store, &profile, "mock-refresh-token")?;
        let _ = app.emit(
            service::EVENT_AUTH,
            json!({ "type": "login", "profile": profile }),
        );
        return Ok(json!({ "ok": true, "profile": profile, "mock": true }));
    }

    if !try_begin_login() {
        return Err("AUTH_LOGIN_IN_PROGRESS: 已有登录流程进行中".into());
    }
    let result = real_login(&app, &p0).await;
    end_login();
    result
}

/// 真实网页登录闭环：loopback 监听 → 拉起浏览器 → 等回调 → 校验 → 换 token
async fn real_login(app: &AppHandle, p0: &Arc<P0Services>) -> Result<Value, String> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("CALLBACK_BIND_FAILED: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();

    let state = service::gen_state();
    let base = service::auth_server_url();
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");
    let login_url = format!(
        "{base}/auth/login?state={state}&redirect_uri={}",
        service::urlencode(&redirect_uri)
    );

    // 拉起系统浏览器打开网页登录页（tauri-plugin-opener）
    app.opener()
        .open_url(login_url, None::<&str>)
        .map_err(|e| format!("AUTH_OPEN_BROWSER_FAILED: {e}"))?;

    // 等待 loopback 回调（120s 超时）
    let (code, got_state) = tokio::time::timeout(
        std::time::Duration::from_secs(service::LOGIN_TIMEOUT_SECS),
        service::wait_loopback_callback(listener),
    )
    .await
    .map_err(|_| "AUTH_LOGIN_TIMEOUT: 等待登录超时，请重试".to_string())??;

    if got_state != state {
        return Err("AUTH_STATE_MISMATCH: 回调 state 校验失败".into());
    }

    let body = service::exchange_code(&base, &code, &state).await?;
    let refresh = body
        .get("refreshToken")
        .and_then(|v| v.as_str())
        .ok_or("AUTH_EXCHANGE_INVALID: 响应缺少 refreshToken")?;
    let profile = body.get("profile").cloned().unwrap_or(json!({}));

    service::save_session(&p0.store, &profile, refresh)?;
    let _ = app.emit(
        service::EVENT_AUTH,
        json!({ "type": "login", "profile": profile }),
    );
    Ok(json!({ "ok": true, "profile": profile, "mock": false }))
}

/// 退出登录：尽力远端吊销 → 清理 keyring token + 账号 profile。
/// 清理边界遵循 local-first：本地任务/会话数据一律保留。
#[tauri::command]
pub async fn auth_logout(
    app: AppHandle,
    p0: State<'_, Arc<P0Services>>,
) -> Result<Value, String> {
    let was_logged_in = service::load_profile(&p0.store).is_some();

    // 尽力而为的远端 session 吊销（Mock token 与服务不可达均不阻断）
    if let Some(refresh) = p0.store.vault().get_secret(service::REFRESH_ACCOUNT) {
        if !refresh.starts_with("mock-") {
            service::revoke_remote(&service::auth_server_url(), &refresh).await;
        }
    }

    service::clear_session(&p0.store);
    let _ = app.emit(service::EVENT_AUTH, json!({ "type": "logout" }));
    Ok(json!({ "ok": true, "wasLoggedIn": was_logged_in }))
}
