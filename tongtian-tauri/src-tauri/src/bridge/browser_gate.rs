//! 浏览器 AI+ 本机网关（受控浏览器会话 / 逐次放行）
//!
//! 为什么执行放在 Rust 而不是引擎：
//! - 浏览器的启动、专用配置目录、策略门禁、原生确认框与审计都属于桌面壳；
//! - 引擎只能通过一次性 token 回到本机网关「申请」页面与操作放行，
//!   拿不到任意浏览器控制能力，也绕不开档位；
//! - token 只随 runtime 下发给本机引擎，不进渲染层、不写事件正文。
//!
//! 安全边界：
//! - 只绑 127.0.0.1，端口随机；每次请求校验 token；
//! - 每次调用都重新读档位（设置页刚禁用就立即生效），非法档位 fail-closed；
//! - 「每次询问」由系统 MessageBox 确认，用户取消即拒绝；
//! - 只允许打开 http/https，file/javascript/data/chrome 等协议在入口拒绝；
//! - 页面 WebSocket 地址必须是本机回环的 `/devtools/page/`，防止被换到远端；
//! - 审计不记页面正文、输入正文与截图内容，只记域名、动作与结果。

use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};

use crate::bridge::hook_gate::{read_head, write_response};
use crate::browser;
use crate::security::audit;
use crate::P0Services;

#[derive(Clone, Debug)]
pub struct Gate {
    /// 基础地址，引擎在后接 `/browser/ensure` 或 `/browser/approve`
    pub url: String,
    pub token: String,
}

static GATE: std::sync::OnceLock<Gate> = std::sync::OnceLock::new();

pub fn endpoint() -> Option<Gate> {
    GATE.get().cloned()
}

/// 启动网关（幂等）。不要求浏览器已安装：能力页会如实标 supported=false，
/// 真正 ensure 时才返回「没找到 Edge/Chrome」。
pub fn start(services: Arc<P0Services>) {
    if GATE.get().is_some() {
        return;
    }
    let std_listener = match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[browser-gate] bind failed（浏览器 AI+ 不可用）: {e}");
            return;
        }
    };
    let port = match std_listener.local_addr() {
        Ok(a) => a.port(),
        Err(e) => {
            eprintln!("[browser-gate] local_addr failed: {e}");
            return;
        }
    };
    let token = uuid::Uuid::new_v4().to_string();
    if GATE
        .set(Gate {
            url: format!("http://127.0.0.1:{port}/browser"),
            token: token.clone(),
        })
        .is_err()
    {
        return;
    }
    if let Err(e) = std_listener.set_nonblocking(true) {
        eprintln!("[browser-gate] set_nonblocking failed: {e}");
        return;
    }
    tauri::async_runtime::spawn(async move {
        let Ok(listener) = TcpListener::from_std(std_listener) else {
            eprintln!("[browser-gate] 无法接入 tokio runtime，浏览器 AI+ 不可用");
            return;
        };
        println!("[browser-gate] listening on port {port}");
        serve(listener, services).await;
    });
}

async fn serve(listener: TcpListener, services: Arc<P0Services>) {
    loop {
        match listener.accept().await {
            Ok((stream, _addr)) => {
                let svc = Arc::clone(&services);
                tauri::async_runtime::spawn(async move {
                    let _ = handle(stream, svc).await;
                });
            }
            Err(e) => {
                eprintln!("[browser-gate] accept failed: {e}");
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

async fn handle(mut stream: TcpStream, services: Arc<P0Services>) -> std::io::Result<()> {
    let (method, path, headers, body) = match read_head(&mut stream).await {
        Ok(h) => h,
        Err(_) => return Ok(()),
    };
    if method != "POST"
        || !matches!(
            path.as_str(),
            "/browser/ensure" | "/browser/approve" | "/browser/status"
        )
    {
        return write_response(&mut stream, 404, &json!({ "ok": false, "error": "NOT_FOUND" })).await;
    }
    let expected = GATE.get().map(|g| g.token.clone()).unwrap_or_default();
    if headers.get("x-tt-browser-token").map(String::as_str) != Some(expected.as_str()) {
        return write_response(
            &mut stream,
            401,
            &json!({ "ok": false, "error": "BROWSER_USE_GATE_UNAUTHORIZED" }),
        )
        .await;
    }
    let req = if body.trim().is_empty() {
        json!({})
    } else {
        match serde_json::from_str::<Value>(&body) {
            Ok(v) if v.is_object() => v,
            _ => {
                return write_response(
                    &mut stream,
                    400,
                    &json!({ "ok": false, "error": "BROWSER_USE_BAD_JSON" }),
                )
                .await
            }
        }
    };

    let policy = match browser::read_policy(&services.store) {
        Ok((p, _)) => p,
        Err(e) => {
            return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
        }
    };
    if policy == "disabled" {
        return write_response(
            &mut stream,
            403,
            &json!({ "ok": false, "error": "BROWSER_USE_DISABLED: 浏览器 AI+ 已禁用" }),
        )
        .await;
    }

    match path.as_str() {
        "/browser/ensure" => ensure(stream, &services, req).await,
        "/browser/approve" => approve(stream, &services, &policy, req).await,
        "/browser/status" => {
            let session = browser::current_session();
            write_response(
                &mut stream,
                200,
                &json!({
                    "ok": true,
                    "running": session.is_some(),
                    "port": session.as_ref().map(|s| s.port),
                    "browser": session.as_ref().map(|s| s.browser.clone()),
                }),
            )
            .await
        }
        _ => unreachable!(),
    }
}

async fn ensure(
    mut stream: TcpStream,
    services: &Arc<P0Services>,
    req: Value,
) -> std::io::Result<()> {
    let launched = tauri::async_runtime::spawn_blocking(browser::ensure).await;
    let (info, started) = match launched {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => {
            return write_response(&mut stream, 500, &json!({ "ok": false, "error": e })).await
        }
        Err(e) => {
            return write_response(
                &mut stream,
                500,
                &json!({ "ok": false, "error": format!("BROWSER_USE_SESSION_FAILED: 启动线程异常：{e}") }),
            )
            .await
        }
    };
    if started {
        audit::record_quiet(
            &services.store,
            audit::ACTION_BROWSER_SESSION,
            audit::SEV_INFO,
            &info.browser,
            &format!(
                "启动受控浏览器（端口 {}，独立配置目录 {}）",
                info.port,
                browser::profile_dir().display()
            ),
        );
    }
    let task = req
        .get("taskId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(120)
        .collect::<String>();
    let page = match resolve_page(info.port).await {
        Ok(p) => p,
        Err(e) => {
            return write_response(
                &mut stream,
                500,
                &json!({ "ok": false, "error": e, "taskId": task }),
            )
            .await
        }
    };
    write_response(
        &mut stream,
        200,
        &json!({
            "ok": true,
            "started": started,
            "browser": info.browser,
            "pid": info.pid,
            "port": info.port,
            "pageId": page["pageId"],
            "url": page["url"],
            "title": page["title"],
            "wsUrl": page["wsUrl"],
            "profileDir": browser::profile_dir().to_string_lossy(),
        }),
    )
    .await
}

async fn approve(
    mut stream: TcpStream,
    services: &Arc<P0Services>,
    policy: &str,
    req: Value,
) -> std::io::Result<()> {
    let field = |key: &str| req.get(key).and_then(Value::as_str).unwrap_or("");
    let action = field("action");
    let target = field("target");
    let detail = field("detail");
    if !browser::is_known_action(action) {
        return write_response(
            &mut stream,
            400,
            &json!({ "ok": false, "error": "BROWSER_USE_INVALID_ACTION: 未知的浏览器动作" }),
        )
        .await;
    }
    let target = if action == "open" {
        match browser::normalize_url(target) {
            Ok(u) => u,
            Err(e) => {
                return write_response(&mut stream, 400, &json!({ "ok": false, "error": e })).await
            }
        }
    } else {
        target.to_string()
    };
    let description = browser::describe_action(action, &target, detail);
    let p = policy.to_string();
    let desc = description.clone();
    let confirmed = tauri::async_runtime::spawn_blocking(move || browser::gate(&p, &desc)).await;
    match confirmed {
        Ok(Ok(())) => {}
        Ok(Err(e)) => {
            // 拒绝是安全事实：即使引擎随后退出，也要留痕
            audit::record_quiet(
                &services.store,
                audit::ACTION_BROWSER_ACTION,
                audit::SEV_WARNING,
                &browser::host_of(&target),
                &format!("{description} 未执行：{e}"),
            );
            return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
        }
        Err(e) => {
            return write_response(
                &mut stream,
                500,
                &json!({ "ok": false, "error": format!("BROWSER_USE_ACTION_FAILED: 确认框线程异常：{e}") }),
            )
            .await
        }
    }
    write_response(
        &mut stream,
        200,
        &json!({
            "ok": true,
            "action": action,
            "description": description,
            "policy": policy,
        }),
    )
    .await
}

/// 从浏览器的 `/json/list` 找到页面目标；没有页面时新建一个 about:blank。
/// 端口刚打开时 HTTP 可能还没就绪，因此在这里做有限次重试。
async fn resolve_page(port: u16) -> Result<Value, String> {
    let base = format!("http://127.0.0.1:{port}");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| format!("BROWSER_USE_SESSION_FAILED: 创建调试客户端失败：{e}"))?;
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut last = String::from("调试端口尚未响应");
    while Instant::now() < deadline {
        match client.get(format!("{base}/json/list")).send().await {
            Ok(resp) if resp.status().is_success() => {
                if let Ok(list) = resp.json::<Value>().await {
                    if let Some(page) = pick_page(port, &list) {
                        return Ok(page);
                    }
                    // 没有可用的 page target：请求浏览器新开一个空页。
                    // Chrome 111+ 要求 PUT；旧版本用 GET，失败也不影响下一轮重试。
                    let _ = client
                        .put(format!("{base}/json/new?about:blank"))
                        .send()
                        .await;
                    last = "还没有可用的页面目标".to_string();
                }
            }
            Ok(resp) => last = format!("HTTP {}", resp.status()),
            Err(e) => last = format!("{e}"),
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    Err(format!(
        "BROWSER_USE_SESSION_FAILED: 浏览器调试端口未就绪（{last}）"
    ))
}

fn pick_page(port: u16, list: &Value) -> Option<Value> {
    let arr = list.as_array()?;
    let page = arr.iter().find(|t| {
        t.get("type").and_then(Value::as_str) == Some("page")
            && t.get("webSocketDebuggerUrl")
                .and_then(Value::as_str)
                .is_some_and(|u| valid_page_ws(port, u))
    })?;
    Some(json!({
        "pageId": page.get("id").and_then(Value::as_str).unwrap_or(""),
        "url": page.get("url").and_then(Value::as_str).unwrap_or(""),
        "title": page.get("title").and_then(Value::as_str).unwrap_or(""),
        "wsUrl": page.get("webSocketDebuggerUrl").and_then(Value::as_str).unwrap_or(""),
    }))
}

/// 页面 WebSocket 必须是本机回环的 devtools/page 端点：
/// 不把引擎的 CDP 连接引向任何远端地址。
fn valid_page_ws(port: u16, url: &str) -> bool {
    let expected = format!("127.0.0.1:{port}/devtools/page/");
    let alternate = format!("localhost:{port}/devtools/page/");
    url.strip_prefix("ws://")
        .is_some_and(|rest| rest.starts_with(&expected) || rest.starts_with(&alternate))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_ws_must_stay_on_the_local_debug_port() {
        assert!(valid_page_ws(
            9222,
            "ws://127.0.0.1:9222/devtools/page/ABC"
        ));
        assert!(valid_page_ws(
            9222,
            "ws://localhost:9222/devtools/page/ABC"
        ));
        assert!(!valid_page_ws(
            9222,
            "ws://127.0.0.1:9333/devtools/page/ABC"
        ));
        assert!(!valid_page_ws(
            9222,
            "ws://evil.example.com:9222/devtools/page/ABC"
        ));
        assert!(!valid_page_ws(
            9222,
            "ws://127.0.0.1:9222/devtools/browser/ABC"
        ));
        assert!(!valid_page_ws(9222, "wss://127.0.0.1:9222/devtools/page/ABC"));
    }

    #[test]
    fn pick_page_skips_non_page_targets_and_invalid_ws() {
        let list = json!([
            { "type": "service_worker", "id": "sw", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/SW" },
            { "type": "page", "id": "bad", "webSocketDebuggerUrl": "ws://evil.example.com/devtools/page/BAD" },
            { "type": "page", "id": "ok", "url": "about:blank", "title": "新标签页",
              "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/OK" }
        ]);
        let page = pick_page(9222, &list).expect("应选中合法页面");
        assert_eq!(page["pageId"], "ok");
        assert_eq!(page["wsUrl"], "ws://127.0.0.1:9222/devtools/page/OK");
        assert!(pick_page(9222, &json!([])).is_none());
    }
}
