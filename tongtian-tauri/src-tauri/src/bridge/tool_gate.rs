//! 逐工具审批本机网关
//!
//! 引擎在副作用工具真正执行前 POST `/tool/approve`，由桌面壳弹系统确认框。
//! 同一个端点服务两类确认：`kind` 缺省是逐工具审批（受 `toolApprovalMode` 档位约束，
//! off 即 403），`kind: "file_permission"` 是会话档位「每次询问」的写/删确认
//! （与审批档位无关，off 时照样弹；只有档位声明为「每次询问」才受理）。
//! 为什么放在 Rust：
//! - 原生确认框与审计属于桌面壳，引擎拿不到任意弹窗/系统交互能力；
//! - 档位每次请求都重新读取（设置页刚关闭就立即生效），非法档位 fail-closed；
//! - token 只随 runtime 下发给本机引擎，不进渲染层、不写事件正文。
//!
//! 安全边界：
//! - 只绑 127.0.0.1，端口随机；每次请求校验 `x-tt-tool-token`；
//! - 只接受 POST `/tool/approve`，字段限长并去控制字符；
//! - 描述只含工具名与入参摘要（引擎已剔除正文与密钥），审计再统一脱敏；
//! - 用户拒绝留 warning 级审计，放行留 info 级审计。

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};

use crate::bridge::hook_gate::{read_head, write_response};
use crate::security::{audit, classify, tool_approval};
use crate::P0Services;

#[derive(Clone, Debug)]
pub struct Gate {
    /// 基础地址，引擎后接 `/approve`
    pub url: String,
    pub token: String,
}

static GATE: std::sync::OnceLock<Gate> = std::sync::OnceLock::new();

pub fn endpoint() -> Option<Gate> {
    GATE.get().cloned()
}

/// 启动网关（幂等）。不依赖任何外部服务，档位关闭时仍可启动，由请求侧判定。
pub fn start(services: Arc<P0Services>) {
    if GATE.get().is_some() {
        return;
    }
    let std_listener = match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[tool-gate] bind failed（逐工具审批不可用）: {e}");
            return;
        }
    };
    let port = match std_listener.local_addr() {
        Ok(a) => a.port(),
        Err(e) => {
            eprintln!("[tool-gate] local_addr failed: {e}");
            return;
        }
    };
    let token = uuid::Uuid::new_v4().to_string();
    if GATE
        .set(Gate {
            url: format!("http://127.0.0.1:{port}/tool"),
            token: token.clone(),
        })
        .is_err()
    {
        return;
    }
    if let Err(e) = std_listener.set_nonblocking(true) {
        eprintln!("[tool-gate] set_nonblocking failed: {e}");
        return;
    }
    tauri::async_runtime::spawn(async move {
        let Ok(listener) = TcpListener::from_std(std_listener) else {
            eprintln!("[tool-gate] 无法接入 tokio runtime，逐工具审批不可用");
            return;
        };
        println!("[tool-gate] listening on port {port}");
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
                eprintln!("[tool-gate] accept failed: {e}");
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
    if method != "POST" || path != "/tool/approve" {
        return write_response(&mut stream, 404, &json!({ "ok": false, "error": "NOT_FOUND" })).await;
    }
    let expected = GATE.get().map(|g| g.token.clone()).unwrap_or_default();
    if headers.get("x-tt-tool-token").map(String::as_str) != Some(expected.as_str()) {
        return write_response(
            &mut stream,
            401,
            &json!({ "ok": false, "error": "TOOL_APPROVAL_UNAUTHORIZED" }),
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
                    &json!({ "ok": false, "error": "TOOL_APPROVAL_BAD_JSON" }),
                )
                .await
            }
        }
    };

    // 请求种类：普通逐工具审批，还是「文件访问档位＝每次询问」的写/删确认。
    // 后者由引擎声明档位，壳侧只把它当作「要不要弹框」的依据：确认框永远只会让
    // 操作更难执行，不会更容易，所以伪造这个字段换不来任何放行。
    let kind = req
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("tool")
        .to_string();
    let is_file_ask = kind == "file_permission";
    if is_file_ask {
        // 只有「每次询问」这一档该弹框；别的档位来问说明引擎侧不对，拒绝而不是弹框
        let tier = req
            .get("filePermission")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if tier != crate::security::file_perm::DEFAULT_MODE {
            return write_response(
                &mut stream,
                403,
                &json!({ "ok": false, "error": format!("FILE_PERMISSION_ASK_NOT_REQUIRED: 档位「{tier}」不该发起逐次确认") }),
            )
            .await;
        }
    }

    // 每次请求重新读档位：设置页刚关闭就立即生效
    let mode = match tool_approval::read_mode(&services.store) {
        Ok((m, _)) => m,
        Err(e) => {
            return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
        }
    };
    if mode == "off" && !is_file_ask {
        // 审批关闭仍拒绝普通工具确认；文件档位确认不受这条影响（它由会话档位决定，
        // 与「要不要逐个工具问模型」是两条轴）
        return write_response(
            &mut stream,
            403,
            &json!({ "ok": false, "error": "TOOL_APPROVAL_DISABLED: 工具审批已关闭" }),
        )
        .await;
    }

    let tool = req
        .get("tool")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if tool.trim().is_empty() || tool.chars().count() > 120 {
        return write_response(
            &mut stream,
            400,
            &json!({ "ok": false, "error": "TOOL_APPROVAL_INVALID_TOOL: 工具名缺失或过长" }),
        )
        .await;
    }
    let summary = req
        .get("summary")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let task_id = req
        .get("taskId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(120)
        .collect::<String>();
    let (description, audit_description) = describe_for_user(&tool, &summary);
    // 两类确认共用一套系统弹窗与同一份审计口径，只在文案与「拒绝码」上分流。
    // 档位标志是 Copy 的，随闭包一起搬进阻塞线程即可，不能借用。
    let file_ask = is_file_ask;
    let audit_action = if is_file_ask {
        audit::ACTION_FILE_WRITE
    } else {
        audit::ACTION_TOOL_APPROVAL
    };
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
        if file_ask {
            tool_approval::confirm_file_action(&description)
        } else {
            tool_approval::confirm(&description)
        }
    })
    .await;
    match confirmed {
        Ok(Ok(())) => {
            audit::record_quiet(
                &services.store,
                audit_action,
                audit::SEV_INFO,
                &tool,
                &format!("用户放行：{audit_description}"),
            );
        }
        Ok(Err(e)) => {
            // 拒绝是安全事实：即使引擎随后退出也要留痕
            audit::record_quiet(
                &services.store,
                audit_action,
                audit::SEV_WARNING,
                &tool,
                &format!("未执行：{audit_description}；原因：{e}"),
            );
            return write_response(&mut stream, 403, &json!({ "ok": false, "error": e })).await;
        }
        Err(e) => {
            let message = format!("TOOL_APPROVAL_CONFIRM_UNAVAILABLE: 确认框线程异常：{e}");
            audit::record_quiet(
                &services.store,
                audit_action,
                audit::SEV_ERROR,
                &tool,
                &format!("未执行：{audit_description}；原因：{message}"),
            );
            return write_response(&mut stream, 500, &json!({ "ok": false, "error": message }))
                .await;
        }
    }
    write_response(
        &mut stream,
        200,
        &json!({ "ok": true, "tool": tool, "mode": mode, "kind": kind, "taskId": task_id }),
    )
    .await
}

/// 确认框与审计使用的两版描述：都先脱敏 PII/密钥，审计再额外遮住用户名路径。
fn describe_for_user(tool: &str, summary: &str) -> (String, String) {
    let description = tool_approval::describe(tool, &classify::redact(summary));
    let audit_description = classify::redact_paths(&description);
    (description, audit_description)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn describe_for_user_masks_pii_and_secrets_before_dialog_and_audit() {
        let (dialog, audit) =
            describe_for_user("run_command", "命令：curl -H 'key: sk-abcdefghijklmn' 13800138000");
        for text in [&dialog, &audit] {
            assert!(!text.contains("sk-abcdefghijklmn"), "{text}");
            assert!(!text.contains("13800138000"), "{text}");
            assert!(text.contains("run_command"), "{text}");
        }
    }

    #[test]
    fn audit_description_also_hides_user_home() {
        let (dialog, audit) = describe_for_user("write_text_file", "文件：C:\\Users\\alice\\a.md");
        assert!(dialog.contains("alice"));
        assert!(!audit.contains("alice"), "{audit}");
    }

    #[test]
    fn gate_path_is_fixed() {
        // 网关只暴露一个端点：路径写错时 404，不会误接到别的处理逻辑
        let gate = Gate {
            url: "http://127.0.0.1:1234/tool".to_string(),
            token: "t".to_string(),
        };
        assert!(gate.url.ends_with("/tool"));
    }

    #[test]
    fn describe_keeps_tool_and_summary_on_separate_lines() {
        let text = tool_approval::describe("write_text_file", "文件：a.md（正文 12 字）");
        assert_eq!(text, "工具：write_text_file\n文件：a.md（正文 12 字）");
    }
}
