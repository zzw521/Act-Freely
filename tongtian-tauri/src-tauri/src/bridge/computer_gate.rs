//! 电脑操控本机网关（P1/P2：Windows 截屏 / 点击 / 键盘）
//!
//! 为什么执行放在 Rust 而不是引擎：
//! - 截屏与键鼠注入属于桌面壳能力，引擎是子进程，不应该直接持有系统输入权限；
//! - 策略门禁、原生确认框与审计都落 SQLite：引擎只能「请求」，不能绕过档位；
//! - token 只随 runtime 下发给本机引擎，不进渲染层、不写事件正文。
//!
//! 安全边界：
//! - 只绑 127.0.0.1，端口随机；每次请求校验 token；
//! - 每次调用都重新读档位（设置页刚禁用就立即生效），非法档位 fail-closed；
//! - 「每次询问」由系统 MessageBox 确认，用户取消即拒绝并留 warning 审计；
//! - Win 键组合与任务管理器快捷键在解析阶段直接拒绝；
//! - 审计不记输入正文、不记截图内容，只记动作描述、尺寸与结果。

use std::sync::Arc;

use base64::Engine as _;
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};

use crate::bridge::hook_gate::{read_head, write_response};
use crate::computer::{self, Action};
use crate::security::audit;
use crate::P0Services;

#[derive(Clone, Debug)]
pub struct Gate {
    /// 基础地址，引擎在后接 `/screenshot` 或 `/action`
    pub url: String,
    pub token: String,
}

static GATE: std::sync::OnceLock<Gate> = std::sync::OnceLock::new();

pub fn endpoint() -> Option<Gate> {
    GATE.get().cloned()
}

/// 启动网关（幂等）。非 Windows 平台不监听端口：能力块会如实标 supported=false。
pub fn start(services: Arc<P0Services>) {
    if GATE.get().is_some() || !computer::supported() {
        return;
    }
    let std_listener = match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[computer-gate] bind failed（电脑操控不可用）: {e}");
            return;
        }
    };
    let port = match std_listener.local_addr() {
        Ok(a) => a.port(),
        Err(e) => {
            eprintln!("[computer-gate] local_addr failed: {e}");
            return;
        }
    };
    let token = uuid::Uuid::new_v4().to_string();
    if GATE
        .set(Gate {
            url: format!("http://127.0.0.1:{port}/computer"),
            token: token.clone(),
        })
        .is_err()
    {
        return;
    }
    if let Err(e) = std_listener.set_nonblocking(true) {
        eprintln!("[computer-gate] set_nonblocking failed: {e}");
        return;
    }
    tauri::async_runtime::spawn(async move {
        let Ok(listener) = TcpListener::from_std(std_listener) else {
            eprintln!("[computer-gate] 无法接入 tokio runtime，电脑操控不可用");
            return;
        };
        println!("[computer-gate] listening on port {port}");
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
                eprintln!("[computer-gate] accept failed: {e}");
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
}

async fn handle(mut stream: TcpStream, services: Arc<P0Services>) -> std::io::Result<()> {
    let (method, path, headers, body) = match read_head(&mut stream).await {
        Ok(h) => h,
        Err(_) => return Ok(()),
    };
    if method != "POST" || (path != "/computer/screenshot" && path != "/computer/action") {
        return write_response(&mut stream, 404, &json!({ "ok": false, "error": "NOT_FOUND" })).await;
    }
    let expected = GATE.get().map(|g| g.token.clone()).unwrap_or_default();
    if headers.get("x-tt-computer-token").map(String::as_str) != Some(expected.as_str()) {
        return write_response(
            &mut stream,
            401,
            &json!({ "ok": false, "error": "COMPUTER_USE_GATE_UNAUTHORIZED" }),
        )
        .await;
    }
    let Ok(req) = serde_json::from_str::<Value>(&body) else {
        return write_response(
            &mut stream,
            400,
            &json!({ "ok": false, "error": "COMPUTER_USE_BAD_JSON" }),
        )
        .await;
    };
    let task_id = req
        .get("taskId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(120)
        .collect::<String>();
    let source = if task_id.is_empty() {
        "桌面".to_string()
    } else {
        format!("任务 {task_id}")
    };

    let policy = match computer::read_policy(&services.store) {
        Ok((p, _)) => p,
        Err(e) => {
            audit(
                &services,
                audit::ACTION_COMPUTER_ACTION,
                audit::SEV_WARNING,
                &source,
                &format!("电脑操控策略读取失败，请求被拒绝：{e}"),
            );
            return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
        }
    };
    if policy == "disabled" {
        audit(
            &services,
            if path.ends_with("screenshot") {
                audit::ACTION_COMPUTER_CAPTURE
            } else {
                audit::ACTION_COMPUTER_ACTION
            },
            audit::SEV_WARNING,
            &source,
            "电脑操控已禁用，请求被拒绝",
        );
        return write_response(
            &mut stream,
            403,
            &json!({ "ok": false, "error": "COMPUTER_USE_DISABLED: 电脑操控已禁用" }),
        )
        .await;
    }

    if path == "/computer/screenshot" {
        screenshot(stream, &services, &source, &policy).await
    } else {
        action(stream, &services, &source, &policy, req).await
    }
}

async fn screenshot(
    mut stream: TcpStream,
    services: &Arc<P0Services>,
    source: &str,
    policy: &str,
) -> std::io::Result<()> {
    let description = "截取整个屏幕（包括所有显示器上的可见内容）";
    if let Err(e) = confirm(policy, description).await {
        audit(
            services,
            audit::ACTION_COMPUTER_CAPTURE,
            audit::SEV_WARNING,
            source,
            &format!("截屏未执行：{e}"),
        );
        return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
    }
    match tauri::async_runtime::spawn_blocking(computer::capture).await {
        Ok(Ok(cap)) => {
            let kb = cap.png.len() as f64 / 1024.0;
            audit(
                services,
                audit::ACTION_COMPUTER_CAPTURE,
                audit::SEV_INFO,
                source,
                &format!(
                    "截屏 {}x{}{}（{kb:.0} KB，画面内容不入审计）",
                    cap.width,
                    cap.height,
                    if cap.scaled {
                        format!("，原始 {}x{} 已降采样", cap.original_width, cap.original_height)
                    } else {
                        String::new()
                    }
                ),
            );
            let data = base64::engine::general_purpose::STANDARD.encode(&cap.png);
            write_response(
                &mut stream,
                200,
                &json!({
                    "ok": true,
                    "mime": "image/png",
                    "imageBase64": data,
                    "width": cap.width,
                    "height": cap.height,
                    "originalWidth": cap.original_width,
                    "originalHeight": cap.original_height,
                    "originX": cap.origin_x,
                    "originY": cap.origin_y,
                    "scaled": cap.scaled,
                }),
            )
            .await
        }
        Ok(Err(e)) => {
            audit(
                services,
                audit::ACTION_COMPUTER_CAPTURE,
                audit::SEV_WARNING,
                source,
                &format!("截屏失败：{e}"),
            );
            write_response(&mut stream, 500, &json!({ "ok": false, "error": e })).await
        }
        Err(e) => write_response(
            &mut stream,
            500,
            &json!({ "ok": false, "error": format!("COMPUTER_USE_CAPTURE_FAILED: 截屏线程异常：{e}") }),
        )
        .await,
    }
}

async fn action(
    mut stream: TcpStream,
    services: &Arc<P0Services>,
    source: &str,
    policy: &str,
    req: Value,
) -> std::io::Result<()> {
    let act: Action = match computer::parse_action(&req) {
        Ok(a) => a,
        Err(e) => {
            audit(
                services,
                audit::ACTION_COMPUTER_ACTION,
                audit::SEV_WARNING,
                source,
                &format!("动作被拒绝：{e}"),
            );
            return write_response(&mut stream, 400, &json!({ "ok": false, "error": e })).await;
        }
    };
    let description = computer::describe_action(&act);
    if let Err(e) = confirm(policy, &description).await {
        audit(
            services,
            audit::ACTION_COMPUTER_ACTION,
            audit::SEV_WARNING,
            source,
            &format!("{description} 未执行：{e}"),
        );
        return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
    }
    match tauri::async_runtime::spawn_blocking(move || computer::perform_action(&act)).await {
        Ok(Ok(())) => {
            audit(
                services,
                audit::ACTION_COMPUTER_ACTION,
                audit::SEV_INFO,
                source,
                &format!("已执行：{description}"),
            );
            write_response(&mut stream, 200, &json!({ "ok": true, "action": description })).await
        }
        Ok(Err(e)) => {
            audit(
                services,
                audit::ACTION_COMPUTER_ACTION,
                audit::SEV_WARNING,
                source,
                &format!("{description} 失败：{e}"),
            );
            write_response(&mut stream, 500, &json!({ "ok": false, "error": e })).await
        }
        Err(e) => write_response(
            &mut stream,
            500,
            &json!({ "ok": false, "error": format!("COMPUTER_USE_ACTION_FAILED: 操作线程异常：{e}") }),
        )
        .await,
    }
}

/// 系统确认框会阻塞所在线程，放到 blocking 池，避免堵住网关的 accept/IO
async fn confirm(policy: &str, description: &str) -> Result<(), String> {
    if policy == "auto" {
        return Ok(());
    }
    if policy != "ask_every_time" {
        return Err("COMPUTER_USE_DISABLED: 电脑操控已禁用".into());
    }
    let desc = description.to_string();
    match tauri::async_runtime::spawn_blocking(move || computer::gate("ask_every_time", &desc)).await {
        Ok(r) => r,
        Err(e) => Err(format!("COMPUTER_USE_ACTION_FAILED: 确认框线程异常：{e}")),
    }
}

fn audit(services: &Arc<P0Services>, action: &str, severity: &str, source: &str, details: &str) {
    // 单条审计详情上限，避免异常长的错误把表格撑爆
    let details = details.chars().take(500).collect::<String>();
    audit::record_quiet(&services.store, action, severity, source, &details);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gate_endpoint_is_loopback_and_tokenized_when_started() {
        if let Some(g) = endpoint() {
            assert!(g.url.starts_with("http://127.0.0.1:"), "只绑回环：{}", g.url);
            assert!(g.url.ends_with("/computer"));
            assert!(!g.token.is_empty());
        }
    }

}
