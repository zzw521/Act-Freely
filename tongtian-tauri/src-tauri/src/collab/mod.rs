//! 项目协作客户端（跨用户 / 跨机器，人工触发的任务交接）
//!
//! 共享边界（服务端只存这些）：
//! - 项目元数据、成员与角色、任务、评论
//! - 结果摘要、产物**文件名**、执行机器标识
//!
//! 不共享：对话内容、产物正文、技能、凭据、运行时状态。
//! 任务由负责人显式在自己机器上执行并回报，不做 Agent 之间的自动接力。
//!
//! 安全边界：
//! - 服务地址复用 TT_AUTH_SERVER_URL；远程必须 HTTPS，仅本机回环允许 HTTP
//! - access token 只进 Authorization 头（内存缓存），不落日志、不进错误文案
//! - 401 时强制刷新一次并重试；仍失败如实报错

use std::net::IpAddr;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

use crate::auth::service as auth_service;
use crate::store::Store;
use crate::P0Services;

const MAX_BASE_LEN: usize = 2048;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

// ── 服务地址 ────────────────────────────────────────────────────

fn invalid_base(detail: &str) -> String {
    format!("COLLAB_SERVER_INVALID: {detail}")
}

fn host_is_loopback(url: &reqwest::Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let bare = host
        .strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .unwrap_or(host);
    bare.eq_ignore_ascii_case("localhost")
        || bare
            .parse::<IpAddr>()
            .map(|ip| ip.is_loopback())
            .unwrap_or(false)
}

/// 归一协作服务地址：远程 HTTPS、本机回环可 HTTP、拒绝内嵌凭据与查询串
fn normalize_base(raw: &str) -> Result<String, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Err(invalid_base("未配置协作服务地址"));
    }
    if raw.len() > MAX_BASE_LEN {
        return Err(invalid_base("地址过长"));
    }
    let mut url = reqwest::Url::parse(raw)
        .map_err(|_| invalid_base("地址不是完整 URL，请填写 http(s)://主机[:端口]"))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(invalid_base("地址里不能包含用户名或密码"));
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err(invalid_base("地址不能带查询参数或锚点"));
    }
    if url.host_str().is_none() {
        return Err(invalid_base("地址缺少主机名"));
    }
    let local = host_is_loopback(&url);
    match url.scheme() {
        "https" => {}
        "http" if local => {}
        "http" => {
            return Err(invalid_base(
                "远程协作服务必须使用 HTTPS；本机调试可用 http://127.0.0.1 或 http://localhost",
            ))
        }
        _ => return Err(invalid_base("只支持 http / https 地址")),
    }
    let path = url.path().trim_end_matches('/').to_string();
    url.set_path(&path);
    url.set_query(None);
    url.set_fragment(None);
    Ok(url.as_str().trim_end_matches('/').to_string())
}

fn client_for_base(base: &str) -> Result<reqwest::Client, String> {
    let local = reqwest::Url::parse(base)
        .map(|url| host_is_loopback(&url))
        .unwrap_or(false);
    let builder = reqwest::Client::builder()
        // 不让 302 把带 token 的请求带去别的地址
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .user_agent("TongTianOffice-Collab/1.0");
    let builder = if local {
        builder.no_proxy()
    } else {
        crate::proxy::current().apply(builder)?
    };
    builder
        .build()
        .map_err(|e| format!("COLLAB_NETWORK: 无法创建网络客户端（{e}）"))
}

// ── 错误映射 ────────────────────────────────────────────────────

fn network_reason(err: &reqwest::Error) -> &'static str {
    if err.is_timeout() {
        "连接或读取超时"
    } else if err.is_connect() {
        "无法建立连接"
    } else {
        "网络请求失败"
    }
}

/// 服务端错误体形如 `{code, message}`；原样拼成 `CODE: 中文`，不附带响应正文
fn server_error(status: u16, body: &Value) -> String {
    let code = body.get("code").and_then(|v| v.as_str()).unwrap_or("");
    let message = body.get("message").and_then(|v| v.as_str()).unwrap_or("");
    match (code.is_empty(), message.is_empty()) {
        (false, false) => format!("{code}: {message}"),
        (false, true) => format!("{code}: 协作服务拒绝了该请求"),
        (true, _) => format!("COLLAB_HTTP_{status}: 协作服务返回错误"),
    }
}

/// 发出请求；401 时用注入的刷新器换一次 token 再试，最多两次
async fn request_with<Fut>(
    base: &str,
    method: reqwest::Method,
    path: &str,
    body: Option<&Value>,
    mut token: String,
    mut refresh: impl FnMut() -> Fut,
) -> Result<Value, String>
where
    Fut: std::future::Future<Output = Result<String, String>>,
{
    let base = normalize_base(base)?;
    let client = client_for_base(&base)?;
    let url = format!("{base}{path}");
    for attempt in 0..2 {
        let mut req = client
            .request(method.clone(), &url)
            .header(reqwest::header::AUTHORIZATION, format!("Bearer {token}"))
            .header(reqwest::header::ACCEPT, "application/json");
        if let Some(payload) = body {
            req = req.json(payload);
        }
        let resp = req
            .send()
            .await
            .map_err(|e| format!("COLLAB_NETWORK: 无法连接协作服务（{}）", network_reason(&e)))?;
        let status = resp.status();
        if status == reqwest::StatusCode::UNAUTHORIZED {
            if attempt == 0 {
                token = refresh().await?;
                continue;
            }
            return Err("COLLAB_AUTH_EXPIRED: 登录态已失效，请重新登录".to_string());
        }
        let payload: Value = resp
            .json()
            .await
            .map_err(|e| format!("COLLAB_BAD_RESPONSE: 协作服务响应无法解析（{e}）"))?;
        if !status.is_success() || payload.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            return Err(server_error(status.as_u16(), &payload));
        }
        return Ok(payload);
    }
    Err("COLLAB_AUTH_EXPIRED: 登录态已失效，请重新登录".to_string())
}

async fn request(
    store: &Store,
    method: reqwest::Method,
    path: &str,
    body: Option<&Value>,
) -> Result<Value, String> {
    let token = auth_service::access_token(store).await?;
    request_with(
        &auth_service::auth_server_url(store),
        method,
        path,
        body,
        token,
        || auth_service::force_access_token(store),
    )
    .await
}

fn project_path(project_id: &str) -> String {
    format!("/api/v1/projects/{project_id}")
}

// ── commands ────────────────────────────────────────────────────

#[tauri::command]
pub async fn collab_status(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let store = &p0.store;
    let base = auth_service::auth_server_url(store);
    let valid = normalize_base(&base).is_ok();
    let logged_in = auth_service::has_real_session(store);
    Ok(json!({
        "supported": true,
        // 地址不合法时也如实回传，便于设置页提示具体原因
        "baseUrl": base,
        "serverValid": valid,
        "loggedIn": logged_in,
    }))
}

#[tauri::command]
pub async fn collab_list_projects(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    request(&p0.store, reqwest::Method::GET, "/api/v1/projects", None).await
}

#[tauri::command]
pub async fn collab_create_project(
    p0: State<'_, Arc<P0Services>>,
    name: String,
    description: Option<String>,
) -> Result<Value, String> {
    let payload = json!({ "name": name, "description": description.unwrap_or_default() });
    request(
        &p0.store,
        reqwest::Method::POST,
        "/api/v1/projects",
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_project_detail(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
) -> Result<Value, String> {
    request(
        &p0.store,
        reqwest::Method::GET,
        &project_path(&project_id),
        None,
    )
    .await
}

#[tauri::command]
pub async fn collab_create_invite(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    email: String,
    role: String,
) -> Result<Value, String> {
    let payload = json!({ "email": email, "role": role });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/invites", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_list_invites(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
) -> Result<Value, String> {
    request(
        &p0.store,
        reqwest::Method::GET,
        &format!("{}/invites", project_path(&project_id)),
        None,
    )
    .await
}

#[tauri::command]
pub async fn collab_accept_invite(
    p0: State<'_, Arc<P0Services>>,
    token: String,
) -> Result<Value, String> {
    let payload = json!({ "token": token });
    request(
        &p0.store,
        reqwest::Method::POST,
        "/api/v1/projects/invites/accept",
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_update_member_role(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    uid: String,
    role: String,
) -> Result<Value, String> {
    let payload = json!({ "role": role });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/members/{uid}/role", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_remove_member(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    uid: String,
) -> Result<Value, String> {
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/members/{uid}/remove", project_path(&project_id)),
        None,
    )
    .await
}

#[tauri::command]
pub async fn collab_transfer_owner(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    uid: String,
) -> Result<Value, String> {
    let payload = json!({ "uid": uid });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/transfer", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_leave_project(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
) -> Result<Value, String> {
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/leave", project_path(&project_id)),
        None,
    )
    .await
}

#[tauri::command]
pub async fn collab_create_task(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    title: String,
    detail: Option<String>,
    assignee_uid: Option<String>,
) -> Result<Value, String> {
    let payload = json!({
        "title": title,
        "detail": detail.unwrap_or_default(),
        "assigneeUid": assignee_uid,
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/tasks", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

/// 只转发白名单字段；`assigneeUid: null` 表示取消负责人（与缺省不同）
fn build_task_patch(patch: &Value) -> Result<Value, String> {
    let obj = patch
        .as_object()
        .ok_or_else(|| "COLLAB_INVALID_INPUT: 任务修改内容必须是对象".to_string())?;
    if obj
        .get("version")
        .and_then(|v| v.as_i64())
        .filter(|v| *v >= 1)
        .is_none()
    {
        return Err("COLLAB_INVALID_INPUT: 任务修改缺少版本号".to_string());
    }
    let mut payload = serde_json::Map::new();
    payload.insert("version".into(), obj["version"].clone());
    for key in ["title", "detail", "status", "assigneeUid"] {
        if let Some(value) = obj.get(key) {
            payload.insert(key.into(), value.clone());
        }
    }
    Ok(Value::Object(payload))
}

#[tauri::command]
pub async fn collab_update_task(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    task_id: String,
    patch: Value,
) -> Result<Value, String> {
    let payload = build_task_patch(&patch)?;
    request(
        &p0.store,
        reqwest::Method::PATCH,
        &format!("{}/tasks/{task_id}", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_add_comment(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    task_id: String,
    body: String,
) -> Result<Value, String> {
    let payload = json!({ "body": body });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/tasks/{task_id}/comments", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_report_result(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    task_id: String,
    version: i64,
    summary: String,
    artifacts: Vec<String>,
    machine: String,
    status: String,
) -> Result<Value, String> {
    let payload = json!({
        "version": version,
        "summary": summary,
        "artifacts": artifacts,
        "machine": machine,
        "status": status,
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/tasks/{task_id}/result", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::sync::{Arc as StdArc, Mutex as StdMutex};
    use std::thread::JoinHandle;

    /// 本地顺序应答服务：每次请求读到头就回一条，返回（地址、捕获的请求头、线程）
    fn serve_sequence(
        replies: Vec<(&'static str, &'static str)>,
    ) -> (String, StdArc<StdMutex<Vec<String>>>, JoinHandle<()>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = StdArc::new(StdMutex::new(Vec::new()));
        let captured = seen.clone();
        let handle = std::thread::spawn(move || {
            for (status, body) in replies {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buf = [0u8; 8192];
                let mut raw = Vec::new();
                loop {
                    let n = stream.read(&mut buf).unwrap();
                    if n == 0 {
                        break;
                    }
                    raw.extend_from_slice(&buf[..n]);
                    if raw.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                captured
                    .lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&raw).to_string());
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
            }
        });
        (format!("http://{addr}"), seen, handle)
    }

    async fn get(base: &str, token: &str) -> Result<Value, String> {
        request_with(
            base,
            reqwest::Method::GET,
            "/api/v1/projects",
            None,
            token.to_string(),
            || async { Ok("fresh-token".to_string()) },
        )
        .await
    }

    #[tokio::test]
    async fn request_refreshes_once_after_401_and_reuses_new_token() {
        let (base, seen, handle) = serve_sequence(vec![
            (
                "401 Unauthorized",
                r#"{"ok":false,"code":"TOKEN_EXPIRED","message":"access token 已过期"}"#,
            ),
            ("200 OK", r#"{"ok":true,"projects":[]}"#),
        ]);
        let out = get(&base, "stale-token").await.unwrap();
        handle.join().unwrap();
        assert_eq!(out["projects"], json!([]));
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        assert!(seen[0]
            .to_ascii_lowercase()
            .contains("authorization: bearer stale-token"));
        assert!(seen[1]
            .to_ascii_lowercase()
            .contains("authorization: bearer fresh-token"));
    }

    #[tokio::test]
    async fn request_gives_up_after_second_401_with_login_required_error() {
        let (base, seen, handle) = serve_sequence(vec![
            ("401 Unauthorized", r#"{"ok":false}"#),
            ("401 Unauthorized", r#"{"ok":false}"#),
        ]);
        let err = get(&base, "stale-token").await.unwrap_err();
        handle.join().unwrap();
        assert_eq!(err, "COLLAB_AUTH_EXPIRED: 登录态已失效，请重新登录");
        assert_eq!(seen.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn request_maps_http_error_without_refreshing() {
        let (base, seen, handle) = serve_sequence(vec![(
            "409 Conflict",
            r#"{"ok":false,"code":"PROJECT_CONFLICT","message":"任务已被其他人修改"}"#,
        )]);
        let err = get(&base, "good-token").await.unwrap_err();
        handle.join().unwrap();
        assert_eq!(err, "PROJECT_CONFLICT: 任务已被其他人修改");
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn request_rejects_ok_false_even_with_http_200() {
        let (base, _seen, handle) = serve_sequence(vec![(
            "200 OK",
            r#"{"ok":false,"code":"PROJECT_FORBIDDEN","message":"没有权限"}"#,
        )]);
        let err = get(&base, "good-token").await.unwrap_err();
        handle.join().unwrap();
        assert_eq!(err, "PROJECT_FORBIDDEN: 没有权限");
    }

    #[tokio::test]
    async fn request_reports_unparseable_response() {
        let (base, _seen, handle) = serve_sequence(vec![("200 OK", "not-json")]);
        let err = get(&base, "good-token").await.unwrap_err();
        handle.join().unwrap();
        assert!(err.starts_with("COLLAB_BAD_RESPONSE:"), "{err}");
    }

    #[tokio::test]
    async fn request_rejects_remote_http_before_any_network_call() {
        let err = request_with(
            "http://collab.example.com",
            reqwest::Method::GET,
            "/api/v1/projects",
            None,
            "t".to_string(),
            || async { Ok("t".to_string()) },
        )
        .await
        .unwrap_err();
        assert!(err.starts_with("COLLAB_SERVER_INVALID:"), "{err}");
    }

    #[test]
    fn normalize_base_allows_loopback_http() {
        assert_eq!(
            normalize_base("http://127.0.0.1:18080/").unwrap(),
            "http://127.0.0.1:18080"
        );
        assert_eq!(
            normalize_base("http://localhost:18080///").unwrap(),
            "http://localhost:18080"
        );
        assert_eq!(
            normalize_base("http://[::1]:18080").unwrap(),
            "http://[::1]:18080"
        );
    }

    #[test]
    fn normalize_base_allows_remote_https_only() {
        assert_eq!(
            normalize_base("https://collab.example.com/").unwrap(),
            "https://collab.example.com"
        );
        let err = normalize_base("http://collab.example.com").unwrap_err();
        assert!(err.starts_with("COLLAB_SERVER_INVALID:"), "{err}");
        assert!(err.contains("HTTPS"), "{err}");
    }

    #[test]
    fn normalize_base_rejects_embedded_credentials_and_query() {
        for raw in [
            "https://user:pass@collab.example.com",
            "https://collab.example.com/?token=abc",
            "https://collab.example.com/#x",
            "ftp://127.0.0.1",
            "",
        ] {
            let err = normalize_base(raw).unwrap_err();
            assert!(err.starts_with("COLLAB_SERVER_INVALID:"), "{raw} -> {err}");
        }
    }

    #[test]
    fn server_error_maps_code_and_message() {
        let body = json!({ "ok": false, "code": "PROJECT_CONFLICT", "message": "任务已被其他人修改" });
        assert_eq!(
            server_error(409, &body),
            "PROJECT_CONFLICT: 任务已被其他人修改"
        );
        let body = json!({ "ok": false, "code": "PROJECT_FORBIDDEN" });
        assert_eq!(server_error(403, &body), "PROJECT_FORBIDDEN: 协作服务拒绝了该请求");
        let body = json!({ "detail": "boom" });
        assert_eq!(
            server_error(500, &body),
            "COLLAB_HTTP_500: 协作服务返回错误"
        );
    }

    #[test]
    fn task_patch_keeps_allowed_fields_and_drops_extras() {
        let patch = json!({
            "version": 3,
            "title": "改标题",
            "assigneeUid": null,
            "projectId": "prj-should-not-forward",
            "createdBy": "someone",
        });
        let out = build_task_patch(&patch).unwrap();
        assert_eq!(out["version"], 3);
        assert_eq!(out["title"], "改标题");
        assert!(out["assigneeUid"].is_null());
        assert!(out.get("projectId").is_none());
        assert!(out.get("createdBy").is_none());
    }

    #[test]
    fn task_patch_requires_version() {
        assert!(build_task_patch(&json!({ "title": "x" }))
            .unwrap_err()
            .starts_with("COLLAB_INVALID_INPUT:"));
        assert!(build_task_patch(&json!({ "version": 0 }))
            .unwrap_err()
            .starts_with("COLLAB_INVALID_INPUT:"));
        assert!(build_task_patch(&json!("nope"))
            .unwrap_err()
            .starts_with("COLLAB_INVALID_INPUT:"));
    }
}
