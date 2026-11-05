//! 工单桥（缺陷 D-47）：服务端工单层 14 个端点逐一映射成桌面命令。
//!
//! 口径与服务端 `app/tickets.py` 对齐，这里只补三件本地就该拦住的事：
//! - 建单必须有标题与授权正文（`title` / `brief`）——服务端同样会拒，但错误文案
//!   要说是「没给正文」而不是「网络失败」；
//! - 报告六态（`progress / waiting / ready / blocked / failed / dropped`）与
//!   结束三态（`finished / dropped / lost`）在本地先校验，别把拼错的 kind 发出去；
//! - 白名单转发：客户端对象里不在契约内的字段一律丢掉，与服务端 `extra="forbid"` 同口径。
//!
//! 「等人」的判定、停止撤权、扇出不给授权都在服务端强制；这一层不做任何状态推断，
//! 也不新存凭据——token 仍走 `collab::request` 的内存缓存与 401 刷新。

use std::sync::Arc;

use serde_json::{json, Map, Value};
use tauri::State;

use crate::collab::{project_path, request};
use crate::P0Services;

const REPORT_KINDS: [&str; 6] = ["progress", "waiting", "ready", "blocked", "failed", "dropped"];
const ASSIGNMENT_END_STATUSES: [&str; 3] = ["finished", "dropped", "lost"];

fn invalid(detail: &str) -> String {
    format!("COLLAB_INVALID_INPUT: {detail}")
}

fn ticket_path(project_id: &str, ticket_id: &str) -> String {
    format!("{}/tickets/{ticket_id}", project_path(project_id))
}

fn require_id(kind: &str, raw: &str) -> Result<(), String> {
    if raw.trim().is_empty() {
        return Err(invalid(&format!("{kind}不能为空")));
    }
    Ok(())
}

/// 可选字符串：缺省给默认值；带了但不是字符串就如实拒绝，不静默转成空串
fn opt_str(obj: &Map<String, Value>, key: &str, default: &str) -> Result<String, String> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(default.to_string()),
        Some(Value::String(text)) => Ok(text.clone()),
        Some(_) => Err(invalid(&format!("字段 {key} 必须是字符串"))),
    }
}

/// 可选字符串数组（产物名、候选项）：缺省空数组；非字符串元素逐个拒绝
fn opt_str_list(obj: &Map<String, Value>, key: &str) -> Result<Vec<String>, String> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    Value::String(text) => out.push(text.clone()),
                    _ => return Err(invalid(&format!("{key} 里只能放字符串"))),
                }
            }
            Ok(out)
        }
        Some(_) => Err(invalid(&format!("字段 {key} 必须是数组"))),
    }
}

fn require_str(obj: &Map<String, Value>, key: &str, label: &str) -> Result<String, String> {
    let text = opt_str(obj, key, "")?;
    if text.trim().is_empty() {
        return Err(invalid(&format!("{label}不能为空")));
    }
    Ok(text)
}

// ── 入参构造（白名单） ─────────────────────────────────────────

/// 建单：标题与 brief 必填；其余按白名单透传，多余字段丢掉
pub(crate) fn build_ticket_create(input: &Value) -> Result<Value, String> {
    let obj = input
        .as_object()
        .ok_or_else(|| invalid("建单内容必须是对象"))?;
    let title = require_str(obj, "title", "工单标题")?;
    let brief = require_str(obj, "brief", "授权正文")?;
    let intensity = opt_str(obj, "intensity", "explicit")?;
    if intensity != "explicit" && intensity != "inferred" {
        return Err(invalid("强度只有 explicit 与 inferred 两种"));
    }
    let source_ref = opt_str(obj, "sourceRef", "")?;
    // 与服务端同口径：推断授权必须说清从哪条事件推断出来的，不然拒发
    if intensity == "inferred" && source_ref.trim().is_empty() {
        return Err(invalid("推断授权必须带上来源引用（sourceRef）"));
    }
    let policy_version = match obj.get("policyVersion") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n.as_i64().filter(|v| *v >= 0).ok_or_else(|| invalid("policyVersion 必须是非负整数"))? as i64,
        Some(_) => return Err(invalid("policyVersion 必须是数字")),
    };
    Ok(json!({
        "title": title,
        "detail": opt_str(obj, "detail", "")?,
        "brief": brief,
        "scope": opt_str(obj, "scope", "")?,
        "outOfScope": opt_str(obj, "outOfScope", "")?,
        "intensity": intensity,
        "sourceKind": opt_str(obj, "sourceKind", "")?,
        "sourceRef": source_ref,
        "policyVersion": policy_version,
        "subscribeTaskId": opt_str(obj, "subscribeTaskId", "")?,
    }))
}

/// 领取（Assignment）：mandateId 必填；digest 允许缺省
pub(crate) fn build_assignment(input: &Value) -> Result<Value, String> {
    let obj = input
        .as_object()
        .ok_or_else(|| invalid("领取内容必须是对象"))?;
    let mandate_id = require_str(obj, "mandateId", "授权书编号")?;
    let workspace_kind = opt_str(obj, "workspaceKind", "isolated")?;
    Ok(json!({
        "mandateId": mandate_id,
        "workerId": opt_str(obj, "workerId", "")?,
        "workspaceKind": workspace_kind,
        "workspaceRef": opt_str(obj, "workspaceRef", "")?,
        "checkpointRef": opt_str(obj, "checkpointRef", "")?,
        "machine": opt_str(obj, "machine", "")?,
        "recoveryNote": opt_str(obj, "recoveryNote", "")?,
        "expectedMandateDigest": opt_str(obj, "expectedMandateDigest", "")?,
    }))
}

/// 结构化报告：kind 必须落在六态里；`waiting` 必须带 question——
/// 服务端从不读正文猜情绪，这一条口径从客户端就守住
pub(crate) fn build_report(input: &Value) -> Result<Value, String> {
    let obj = input
        .as_object()
        .ok_or_else(|| invalid("报告内容必须是对象"))?;
    let kind = require_str(obj, "kind", "报告类型")?;
    if !REPORT_KINDS.contains(&kind.as_str()) {
        return Err(invalid(&format!(
            "报告类型只能是 {} 之一",
            REPORT_KINDS.join(" / ")
        )));
    }
    let summary = require_str(obj, "summary", "报告摘要")?;
    let question = opt_str(obj, "question", "")?;
    if kind == "waiting" && question.trim().is_empty() {
        return Err(invalid("waiting 报告必须带上要问人的那句话"));
    }
    let context_seq = match obj.get("contextSeq") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n.as_i64().filter(|v| *v >= 0).ok_or_else(|| invalid("contextSeq 必须是非负整数"))?,
        Some(_) => return Err(invalid("contextSeq 必须是数字")),
    };
    Ok(json!({
        "assignmentId": opt_str(obj, "assignmentId", "")?,
        "kind": kind,
        "summary": summary,
        "detail": opt_str(obj, "detail", "")?,
        "artifacts": opt_str_list(obj, "artifacts")?,
        "question": question,
        "reason": opt_str(obj, "reason", "")?,
        "options": opt_str_list(obj, "options")?,
        "contextSeq": context_seq,
    }))
}

// ── 查询串 ─────────────────────────────────────────────────────

fn encode_query(pairs: &[(&str, String)]) -> String {
    let mut out = String::new();
    for (key, value) in pairs {
        let sep = if out.is_empty() { '?' } else { '&' };
        out.push(sep);
        out.push_str(key);
        out.push('=');
        for byte in value.as_bytes() {
            match byte {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    out.push(*byte as char)
                }
                other => out.push_str(&format!("%{other:02X}")),
            }
        }
    }
    out
}

// ── commands：读 ───────────────────────────────────────────────

#[tauri::command]
pub async fn collab_ticket_my_tickets(
    p0: State<'_, Arc<P0Services>>,
    needs_human: Option<bool>,
    limit: Option<i64>,
) -> Result<Value, String> {
    let path = format!(
        "/api/v1/agent/tickets{}",
        encode_query(&[
            ("needsHuman", (needs_human.unwrap_or(true)).to_string()),
            ("limit", limit.unwrap_or(50).to_string()),
        ])
    );
    request(&p0.store, reqwest::Method::GET, &path, None).await
}

#[tauri::command]
pub async fn collab_ticket_list(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    stage: Option<String>,
    limit: Option<i64>,
) -> Result<Value, String> {
    require_id("项目编号", &project_id)?;
    let path = format!(
        "{}/tickets{}",
        project_path(&project_id),
        encode_query(&[
            ("stage", stage.unwrap_or_default()),
            ("limit", limit.unwrap_or(50).to_string()),
        ])
    );
    request(&p0.store, reqwest::Method::GET, &path, None).await
}

#[tauri::command]
pub async fn collab_ticket_detail(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
) -> Result<Value, String> {
    require_id("项目编号", &project_id)?;
    require_id("工单编号", &ticket_id)?;
    request(
        &p0.store,
        reqwest::Method::GET,
        &ticket_path(&project_id, &ticket_id),
        None,
    )
    .await
}

// ── commands：写 ───────────────────────────────────────────────

#[tauri::command]
pub async fn collab_ticket_create(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    input: Value,
) -> Result<Value, String> {
    require_id("项目编号", &project_id)?;
    let payload = build_ticket_create(&input)?;
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/tickets", project_path(&project_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_subscribe(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    target_ref: String,
    target_kind: Option<String>,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    let target_ref = target_ref.trim().to_string();
    if target_ref.is_empty() {
        return Err(invalid("订阅目标不能为空"));
    }
    let payload = json!({
        "targetKind": target_kind.unwrap_or_else(|| "task".to_string()),
        "targetRef": target_ref,
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/subscriptions", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_claim(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    input: Value,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    let payload = build_assignment(&input)?;
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/assignments", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_end_assignment(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    assignment_id: String,
    status: String,
    exit_reason: Option<String>,
) -> Result<Value, String> {
    require_id("绑定编号", &assignment_id)?;
    if !ASSIGNMENT_END_STATUSES.contains(&status.as_str()) {
        return Err(invalid("结束状态只能是 finished / dropped / lost 之一"));
    }
    let payload = json!({
        "status": status,
        "exitReason": exit_reason.unwrap_or_default(),
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!(
            "{}/assignments/{assignment_id}/end",
            ticket_path(&project_id, &ticket_id)
        ),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_report(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    input: Value,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    let payload = build_report(&input)?;
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/reports", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_handback(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    assignment_id: String,
    reason: String,
    requested: String,
) -> Result<Value, String> {
    require_id("绑定编号", &assignment_id)?;
    if reason.trim().is_empty() || requested.trim().is_empty() {
        return Err(invalid("交回必须同时给理由和希望扩大的范围"));
    }
    let payload = json!({
        "assignmentId": assignment_id,
        "reason": reason,
        "requested": requested,
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/handback", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_answer(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    attention_id: String,
    answer: String,
) -> Result<Value, String> {
    require_id("问题编号", &attention_id)?;
    if answer.trim().is_empty() {
        return Err(invalid("回答内容不能为空"));
    }
    let payload = json!({ "answer": answer });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!(
            "{}/attentions/{attention_id}/answer",
            ticket_path(&project_id, &ticket_id)
        ),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_accept(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    accept: bool,
    note: Option<String>,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    let payload = json!({ "accept": accept, "note": note.unwrap_or_default() });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/accept", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_amend(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    brief: String,
    scope: Option<String>,
    out_of_scope: Option<String>,
    reason: Option<String>,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    if brief.trim().is_empty() {
        return Err(invalid("新授权正文不能为空"));
    }
    let payload = json!({
        "brief": brief,
        "scope": scope.unwrap_or_default(),
        "outOfScope": out_of_scope.unwrap_or_default(),
        "reason": reason.unwrap_or_default(),
    });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!(
            "{}/mandate/amend",
            ticket_path(&project_id, &ticket_id)
        ),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_stop(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    reason: Option<String>,
) -> Result<Value, String> {
    require_id("工单编号", &ticket_id)?;
    let payload = json!({ "reason": reason.unwrap_or_default() });
    request(
        &p0.store,
        reqwest::Method::POST,
        &format!("{}/stop", ticket_path(&project_id, &ticket_id)),
        Some(&payload),
    )
    .await
}

#[tauri::command]
pub async fn collab_ticket_mark_read(
    p0: State<'_, Arc<P0Services>>,
    project_id: String,
    ticket_id: String,
    assignment_id: String,
    event_seq: i64,
) -> Result<Value, String> {
    require_id("绑定编号", &assignment_id)?;
    if event_seq < 1 {
        return Err(invalid("已读落账的事件编号必须是正整数"));
    }
    let path = format!(
        "{}/assignments/{assignment_id}/read-step{}",
        ticket_path(&project_id, &ticket_id),
        encode_query(&[("eventSeq", event_seq.to_string())])
    );
    request(&p0.store, reqwest::Method::POST, &path, None).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collab::request_with;

    #[test]
    fn create_requires_title_and_brief_and_drops_unknown_fields() {
        let input = json!({
            "title": "整理本周数据",
            "brief": "只读周报口径，产出趋势图",
            "outOfScope": "不碰线上库",
            "stage": "working",
            "createdBy": "someone",
        });
        let out = build_ticket_create(&input).unwrap();
        assert_eq!(out["title"], "整理本周数据");
        assert_eq!(out["outOfScope"], "不碰线上库");
        assert_eq!(out["intensity"], "explicit");
        assert_eq!(out["policyVersion"], 0);
        assert!(out.get("stage").is_none());
        assert!(out.get("createdBy").is_none());
        let err = build_ticket_create(&json!({ "title": "只有标题" })).unwrap_err();
        assert!(err.starts_with("COLLAB_INVALID_INPUT:"), "{err}");
        assert!(err.contains("授权正文"), "{err}");
        assert!(build_ticket_create(&json!("nope")).is_err());
    }

    #[test]
    fn create_rejects_bad_intensity_and_negative_policy_version() {
        let base = json!({ "title": "t", "brief": "b" });
        let mut obj = base.as_object().unwrap().clone();
        obj.insert("intensity".into(), json!("aggressive"));
        assert!(build_ticket_create(&Value::Object(obj))
            .unwrap_err()
            .contains("explicit"));
        let mut obj = base.as_object().unwrap().clone();
        obj.insert("policyVersion".into(), json!(-1));
        assert!(build_ticket_create(&Value::Object(obj)).is_err());
        let mut obj = base.as_object().unwrap().clone();
        obj.insert("policyVersion".into(), json!("3"));
        assert!(build_ticket_create(&Value::Object(obj)).is_err());
    }

    #[test]
    fn inferred_intensity_must_carry_a_source_ref() {
        // 与服务端同口径：推断授权要说清来源，本地先拒，别让请求跑一趟才回来
        let err = build_ticket_create(&json!({
            "title": "t", "brief": "b", "intensity": "inferred",
        }))
        .unwrap_err();
        assert!(err.contains("sourceRef"), "{err}");
        let ok = build_ticket_create(&json!({
            "title": "t", "brief": "b", "intensity": "inferred",
            "sourceRef": "evt-42", "sourceKind": "project_event",
        }))
        .unwrap();
        assert_eq!(ok["sourceRef"], "evt-42");
    }

    #[test]
    fn assignment_requires_mandate_id() {
        assert!(build_assignment(&json!({ "machine": "desk-1" }))
            .unwrap_err()
            .contains("授权书编号"));
        let out = build_assignment(&json!({ "mandateId": "agd-1", "workspaceRef": "wt/1" }))
            .unwrap();
        assert_eq!(out["mandateId"], "agd-1");
        assert_eq!(out["workspaceKind"], "isolated");
        assert_eq!(out["workspaceRef"], "wt/1");
    }

    #[test]
    fn report_validates_kind_and_waiting_needs_question() {
        let ok = build_report(&json!({ "kind": "ready", "summary": "做完了" })).unwrap();
        assert_eq!(ok["kind"], "ready");
        assert_eq!(ok["contextSeq"], 0);
        let err = build_report(&json!({ "kind": "done", "summary": "x" })).unwrap_err();
        assert!(err.contains("progress / waiting"), "{err}");
        let err = build_report(&json!({ "kind": "waiting", "summary": "卡住了" })).unwrap_err();
        assert!(err.contains("问人"), "{err}");
        let ok = build_report(&json!({
            "kind": "waiting", "summary": "卡住了", "question": "要哪个口径？",
            "artifacts": ["a.png"], "options": ["周", "月"],
        }))
        .unwrap();
        assert_eq!(ok["options"], json!(["周", "月"]));
        assert!(build_report(&json!({
            "kind": "progress", "summary": "s", "artifacts": [1],
        }))
        .unwrap_err()
        .contains("字符串"));
    }

    #[test]
    fn end_status_and_mark_read_are_bounded_locally() {
        assert!(ASSIGNMENT_END_STATUSES.contains(&"lost"));
        assert_eq!(
            encode_query(&[("eventSeq", "7".to_string())]),
            "?eventSeq=7"
        );
    }

    #[test]
    fn encode_query_percent_escapes_and_joins() {
        assert_eq!(
            encode_query(&[("stage", "awaiting answer".to_string()), ("limit", "50".to_string())]),
            "?stage=awaiting%20answer&limit=50"
        );
        assert_eq!(encode_query(&[]), "");
    }

    #[tokio::test]
    async fn claim_posts_whitelisted_payload_to_ticket_path() {
        // 本地假上游：只验证路径与请求头真的带上了 Bearer，正文按白名单成形
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(std::sync::Mutex::new(String::new()));
        let captured = seen.clone();
        let handle = std::thread::spawn(move || {
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
            *captured.lock().unwrap() = String::from_utf8_lossy(&raw).to_string();
            let body = r#"{"ok":true,"assignment":{},"ticket":{}}"#;
            let response = format!(
                "HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes());
        });
        let payload = build_assignment(&json!({ "mandateId": "agd-1", "machine": "desk-1" })).unwrap();
        let out = request_with(
            &format!("http://{addr}"),
            reqwest::Method::POST,
            "/api/v1/projects/prj-1/tickets/atk-0000000000000000/assignments",
            Some(&payload),
            "tok".to_string(),
            || async { Ok("tok".to_string()) },
        )
        .await
        .unwrap();
        handle.join().unwrap();
        assert_eq!(out["ok"], json!(true));
        let seen = seen.lock().unwrap();
        assert!(seen.contains("POST /api/v1/projects/prj-1/tickets/atk-0000000000000000/assignments"), "{seen}");
        assert!(seen.to_ascii_lowercase().contains("authorization: bearer tok"));
        assert!(seen.contains("agd-1"));
    }

    #[tokio::test]
    async fn server_error_code_passes_through_unchanged() {
        // 停止后的写入被服务端拒：文案必须原样是 `AGENT_TICKET_CLOSED: …`，不被本地改写
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
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
            let body = r#"{"ok":false,"code":"AGENT_TICKET_CLOSED","message":"工单已收口，停止之后不再接受任何写入"}"#;
            let response = format!(
                "HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes());
        });
        let err = request_with(
            &format!("http://{addr}"),
            reqwest::Method::POST,
            "/api/v1/projects/prj-1/tickets/atk-0000000000000000/stop",
            Some(&json!({ "reason": "先停" })),
            "tok".to_string(),
            || async { Ok("tok".to_string()) },
        )
        .await
        .unwrap_err();
        handle.join().unwrap();
        assert!(err.starts_with("AGENT_TICKET_CLOSED: "), "{err}");
        assert!(err.contains("工单已收口"), "{err}");
    }

    #[test]
    fn blank_ids_are_refused_before_any_network_call() {
        assert!(require_id("工单编号", "  ").unwrap_err().starts_with("COLLAB_INVALID_INPUT:"));
        assert!(require_id("工单编号", "atk-1").is_ok());
    }
}
