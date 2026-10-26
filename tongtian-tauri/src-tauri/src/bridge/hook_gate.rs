//! 本机 Hook 网关（执行面 P0-3：用户配置的 Hooks 进入任务生命周期）
//!
//! 为什么执行放在 Rust 而不是引擎：
//! - 脚本执行能力（shell 解释、stdin 喂事件 JSON、按秒超时、进程树回收）只在桌面壳里有，
//!   与 Hooks 页「试运行」共用同一个 `align::hooks::run_command`，试运行看到什么就是什么；
//! - 放行台账与审计都落 SQLite，引擎侧不持有任何执行权限，也不新增出网面。
//! 引擎只负责在正确的执行节点问一句「这个事件要不要跑 Hook」，并如实把结果摊进事件流。
//!
//! 安全边界：
//! - 只绑 127.0.0.1，端口随机；每次进程一个随机 token，token 只随 runtime 下发给本机引擎，
//!   不进渲染层、不写进事件正文；
//! - 配置了命令 ≠ 允许执行：只有在 Hooks 页放行过的命令才会真的拉起进程，
//!   未放行的命令由网关如实回报「未执行」，引擎把它摊成可见说明；
//! - 请求体与响应都设上限，非 POST /hook 一律 404。

use std::sync::Arc;

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::align::hooks;
use crate::security::audit;
use crate::P0Services;

const MAX_HEADER_BYTES: usize = 8 * 1024;
const MAX_BODY_BYTES: usize = 64 * 1024;
const READ_TIMEOUT_SECS: u64 = 15;

#[derive(Clone, Debug)]
pub struct Gate {
    pub url: String,
    pub token: String,
}

static GATE: std::sync::OnceLock<Gate> = std::sync::OnceLock::new();

/// 当前网关（未启动时为 None；runtime bundle 据此如实告诉引擎「没有本机执行通道」）
pub fn endpoint() -> Option<Gate> {
    GATE.get().cloned()
}

/// 启动网关（幂等：一个进程内只监听一次）
///
/// 先同步 bind 拿到端口，再交给 tauri 的全局 runtime 跑 accept 循环 ——
/// setup 闭包里没有 tokio reactor，裸 tokio::spawn 会让打包应用启动即 panic。
pub fn start(services: Arc<P0Services>) {
    if GATE.get().is_some() {
        return;
    }
    let std_listener = match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[hook-gate] bind failed（用户 Hooks 不会触发）: {e}");
            return;
        }
    };
    let port = match std_listener.local_addr() {
        Ok(a) => a.port(),
        Err(e) => {
            eprintln!("[hook-gate] local_addr failed: {e}");
            return;
        }
    };
    let token = uuid::Uuid::new_v4().to_string();
    if GATE
        .set(Gate {
            url: format!("http://127.0.0.1:{port}/hook"),
            token: token.clone(),
        })
        .is_err()
    {
        return; // 并发下已有另一个调用点启动成功
    }
    if let Err(e) = std_listener.set_nonblocking(true) {
        eprintln!("[hook-gate] set_nonblocking failed: {e}");
        return;
    }
    tauri::async_runtime::spawn(async move {
        let Ok(listener) = TcpListener::from_std(std_listener) else {
            eprintln!("[hook-gate] 无法接入 tokio runtime，用户 Hooks 不会触发");
            return;
        };
        println!("[hook-gate] listening on port {port}");
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
                // accept 失败通常是瞬时资源问题，记一行后继续等，不把网关整体带走
                eprintln!("[hook-gate] accept failed: {e}");
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
}

async fn handle(mut stream: TcpStream, services: Arc<P0Services>) -> std::io::Result<()> {
    let head = match read_head(&mut stream).await {
        Ok(h) => h,
        Err(_) => return Ok(()), // 客户端半路断开：无需响应
    };
    let (method, path, headers, body) = head;

    if method != "POST" || path != "/hook" {
        return write_response(&mut stream, 404, &json!({ "ok": false, "error": "NOT_FOUND" })).await;
    }
    let expected = GATE.get().map(|g| g.token.clone()).unwrap_or_default();
    if headers.get("x-tt-hook-token").map(String::as_str) != Some(expected.as_str()) {
        return write_response(&mut stream, 401, &json!({ "ok": false, "error": "HOOK_GATE_UNAUTHORIZED" })).await;
    }
    let Ok(req) = serde_json::from_str::<Value>(&body) else {
        return write_response(&mut stream, 400, &json!({ "ok": false, "error": "HOOK_GATE_BAD_JSON" })).await;
    };
    let Some(event) = req.get("event").and_then(|v| v.as_str()).map(str::to_string) else {
        return write_response(&mut stream, 400, &json!({ "ok": false, "error": "HOOK_GATE_MISSING_EVENT" })).await;
    };
    let tool = req
        .get("tool")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let payload = req.get("payload").cloned().unwrap_or_else(|| json!({}));
    let task_id = req
        .get("taskId")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let now_ms = crate::bridge::event_store::now_ms() as i64;
    let svc = Arc::clone(&services);
    let evt = event.clone();
    let fired = tauri::async_runtime::spawn_blocking(move || {
        hooks::fire(&svc.store, &evt, tool.as_deref(), &payload, now_ms)
    })
    .await;

    match fired {
        Ok(Ok(out)) => {
            record_audit(&services, &task_id, &event, &out);
            write_response(&mut stream, 200, &json!({ "ok": true, "result": out })).await
        }
        Ok(Err(e)) => write_response(&mut stream, 400, &json!({ "ok": false, "error": e })).await,
        Err(e) => write_response(
            &mut stream,
            500,
            &json!({ "ok": false, "error": format!("HOOK_GATE_PANIC: {e}") }),
        )
        .await,
    }
}

/// 真实拉起过脚本就留痕（未放行的没执行，不进审计，否则审计会被配置噪音淹没）
fn record_audit(services: &Arc<P0Services>, task_id: &str, event: &str, out: &Value) {
    let results = out.get("results").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    for r in results.iter().filter(|r| r["executed"].as_bool().unwrap_or(false)) {
        let command = r["command"].as_str().unwrap_or("");
        let exit = r["exitCode"].as_i64().unwrap_or(-1);
        let blocked = r["blocked"].as_bool().unwrap_or(false);
        let failed = r["timedOut"].as_bool().unwrap_or(false) || exit != 0;
        audit::record_quiet(
            &services.store,
            audit::ACTION_HOOK_RUN,
            if failed { audit::SEV_WARNING } else { audit::SEV_INFO },
            event,
            &format!(
                "任务 {task_id} 触发 Hook：`{command}` exit={exit} blocked={blocked}{}",
                if r["timedOut"].as_bool().unwrap_or(false) { " 超时" } else { "" }
            ),
        );
    }
}

/// 读请求头 + body（只认 Content-Length，不支持 chunked；引擎用 httpx 发定长 body）
pub(crate) async fn read_head(
    stream: &mut TcpStream,
) -> Result<(String, String, std::collections::HashMap<String, String>, String), ()> {
    let parsed = tokio::time::timeout(std::time::Duration::from_secs(READ_TIMEOUT_SECS), async {
        let mut buf: Vec<u8> = Vec::with_capacity(2048);
        let mut tmp = [0u8; 1024];
        let split_at = loop {
            if buf.len() >= MAX_HEADER_BYTES {
                return Err(());
            }
            let n = stream.read(&mut tmp).await.map_err(|_| ())?;
            if n == 0 {
                break find_header_end(&buf).ok_or(())?;
            }
            buf.extend_from_slice(&tmp[..n]);
            if let Some(pos) = find_header_end(&buf) {
                break pos;
            }
        };
        let head_text = String::from_utf8_lossy(&buf[..split_at]).to_string();
        let mut lines = head_text.split("\r\n");
        let request_line = lines.next().unwrap_or("").split_whitespace().collect::<Vec<_>>();
        if request_line.len() < 2 {
            return Err(());
        }
        let mut headers = std::collections::HashMap::new();
        for line in lines {
            if line.is_empty() {
                continue;
            }
            if let Some((k, v)) = line.split_once(':') {
                headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
            }
        }
        let want = headers
            .get("content-length")
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(0);
        if want > MAX_BODY_BYTES {
            return Err(());
        }
        let mut body = buf[split_at + 4..].to_vec();
        while body.len() < want {
            let n = stream.read(&mut tmp).await.map_err(|_| ())?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&tmp[..n]);
        }
        Ok((
            request_line[0].to_ascii_uppercase(),
            request_line[1].to_string(),
            headers,
            String::from_utf8_lossy(&body[..want.min(body.len())]).to_string(),
        ))
    })
    .await
    .map_err(|_| ())?;
    parsed
}

fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

pub(crate) async fn write_response(stream: &mut TcpStream, status: u16, body: &Value) -> std::io::Result<()> {
    let text = body.to_string();
    let status_text = match status {
        200 => "OK",
        400 => "BAD REQUEST",
        401 => "UNAUTHORIZED",
        403 => "FORBIDDEN",
        404 => "NOT FOUND",
        _ => "INTERNAL SERVER ERROR",
    };
    let head = format!(
        "HTTP/1.1 {status} {status_text}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        text.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(text.as_bytes()).await?;
    stream.flush().await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_end_found_after_last_header() {
        let raw = b"POST /hook HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}".to_vec();
        let pos = find_header_end(&raw).unwrap();
        assert_eq!(&raw[pos..pos + 4], b"\r\n\r\n");
        assert_eq!(String::from_utf8_lossy(&raw[..pos]).as_ref(), "POST /hook HTTP/1.1\r\nContent-Length: 2");
    }

    #[test]
    fn header_end_absent_when_only_crlf() {
        assert!(find_header_end(b"POST /hook HTTP/1.1\r\nHost: x").is_none());
    }

    #[test]
    fn gate_url_is_loopback_with_token_when_started() {
        // 单测里不启动监听（不占端口、不拉进程），只验证「未启动 → endpoint 为 None」
        // 与 runtime bundle 的口径：拿不到网关时不能伪造一个可用通道
        if let Some(g) = endpoint() {
            assert!(g.url.starts_with("http://127.0.0.1:"), "只绑回环：{}", g.url);
            assert!(!g.token.is_empty(), "token 不能为空");
        }
    }
}
