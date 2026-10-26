//! IM 频道桌面通道
//!
//! 频道长连接必须跑在引擎进程里：Rust 侧离线依赖树中没有任何 WebSocket crate，
//! 而随包 Python 有 aiohttp（`engine/slack_channel.py` 用它连 Slack Socket Mode）。
//! 所以桌面通道做的是把请求转发给引擎 `/api/im/*`，并把引擎的 `{ok:false,code,message}`
//! 还原成 `CODE: 原因`，与其余 command 的错误口径一致。
//!
//! 引擎没跑时如实报 `IM_ENGINE_UNAVAILABLE` —— 不在前端另造一份频道清单或假状态，
//! 因为「绑定成没成、连没连上」这件事只有引擎知道。

use serde_json::{json, Value};

use super::agent::{any_running_port, BridgeState};

/// kind 会拼进 URL 路径，必须先过白名单校验（只放安全字符，长度受限）
pub fn validate_kind(kind: &str) -> Result<String, String> {
    let k = kind.trim();
    if k.is_empty() || k.len() > 32 {
        return Err("IM_CHANNEL_INVALID: 频道标识长度需在 1~32 之间".into());
    }
    if !k
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Err(format!("IM_CHANNEL_INVALID: 频道标识不合法（{kind}）"));
    }
    Ok(k.to_string())
}

/// 引擎回执 → 前端错误口径：`ok=false` 时取 code/message，HTTP 层错误单独成码
pub fn engine_outcome(status: u16, raw: &Value) -> Result<Value, String> {
    if raw.get("ok").and_then(Value::as_bool) == Some(false) {
        let code = raw.get("code").and_then(Value::as_str).unwrap_or("IM_CHANNEL_ERROR");
        let detail = raw.get("message").and_then(Value::as_str).unwrap_or("");
        return Err(if detail.is_empty() {
            code.to_string()
        } else {
            format!("{code}: {detail}")
        });
    }
    if status >= 400 {
        return Err(format!("IM_ENGINE_ERROR: 引擎接口返回 HTTP {status}"));
    }
    Ok(raw.clone())
}

async fn call(bridge: &BridgeState, method: &str, path: String, body: Option<Value>) -> Result<Value, String> {
    let Some(port) = any_running_port(bridge).await else {
        return Err(
            "IM_ENGINE_UNAVAILABLE: IM 频道的长连接由引擎进程承载，引擎未运行时无法绑定或查看频道"
                .to_string(),
        );
    };
    let url = format!("http://127.0.0.1:{port}{path}");
    let client = crate::proxy::loopback_client();
    let resp = match method {
        "GET" => client.get(&url).send().await,
        _ => client
            .post(&url)
            .json(&body.unwrap_or_else(|| json!({})))
            .send()
            .await,
    }
    .map_err(|e| format!("IM_ENGINE_UNREACHABLE: 引擎未响应（{e}）"))?;
    let status = resp.status().as_u16();
    let raw = resp
        .json::<Value>()
        .await
        .map_err(|e| format!("IM_BAD_RESPONSE: 引擎频道接口返回体无法解析（{e}）"))?;
    engine_outcome(status, &raw)
}

/// 频道清单与真实状态（`im:channels`）
#[tauri::command]
pub async fn im_channels(bridge: tauri::State<'_, BridgeState>) -> Result<Value, String> {
    call(bridge.inner(), "GET", "/api/im/channels".into(), None).await
}

/// 绑定频道：引擎先真实校验凭据，通过才落盘并起连接（`im:configure`）
#[tauri::command]
pub async fn im_configure(
    kind: String,
    credentials: Value,
    bridge: tauri::State<'_, BridgeState>,
) -> Result<Value, String> {
    let kind = validate_kind(&kind)?;
    let creds = credentials
        .as_object()
        .ok_or_else(|| "IM_CREDENTIAL_MISSING: 未提交凭据".to_string())?;
    call(
        bridge.inner(),
        "POST",
        format!("/api/im/{kind}/config"),
        Some(json!({ "credentials": creds })),
    )
    .await
}

#[tauri::command]
pub async fn im_start(kind: String, bridge: tauri::State<'_, BridgeState>) -> Result<Value, String> {
    let kind = validate_kind(&kind)?;
    call(bridge.inner(), "POST", format!("/api/im/{kind}/start"), None).await
}

#[tauri::command]
pub async fn im_stop(kind: String, bridge: tauri::State<'_, BridgeState>) -> Result<Value, String> {
    let kind = validate_kind(&kind)?;
    call(bridge.inner(), "POST", format!("/api/im/{kind}/stop"), None).await
}

/// 解绑：停连接并删除该通道凭据登记，IM 会话历史保留（`im:forget`）
#[tauri::command]
pub async fn im_forget(kind: String, bridge: tauri::State<'_, BridgeState>) -> Result<Value, String> {
    let kind = validate_kind(&kind)?;
    call(bridge.inner(), "POST", format!("/api/im/{kind}/forget"), None).await
}

/// 主动发一条文本（定时任务结果推送）。
///
/// 目标必须是该通道真实存在的会话标识；钉钉/企微/飞书只支持回贴已有会话，
/// 发不出去时把引擎错误原样返回，由调用方如实记入执行记录。
pub async fn send(
    bridge: &BridgeState,
    kind: &str,
    target: &str,
    text: &str,
) -> Result<Value, String> {
    let kind = validate_kind(kind)?;
    let target = target.trim();
    if target.is_empty() {
        return Err("IM_TARGET_MISSING: 未提供目标会话".into());
    }
    let text = text.trim();
    if text.is_empty() {
        return Err("IM_TEXT_MISSING: 推送内容为空".into());
    }
    call(
        bridge,
        "POST",
        format!("/api/im/{kind}/send"),
        Some(json!({ "target": target, "text": text })),
    )
    .await
}

/// 手动推送（`im:send`）；定时任务推送走同一实现
#[tauri::command]
pub async fn im_send(
    kind: String,
    target: String,
    text: String,
    bridge: tauri::State<'_, BridgeState>,
) -> Result<Value, String> {
    send(bridge.inner(), &kind, &target, &text).await
}

/// IM 频道偏好当前值（`im:preferencesGet`）。
/// 值由引擎持有：IM 回贴正文是引擎拼的，桌面端只负责转达，
/// 存一份到 SQLite 就会变成「界面显示的开关」和「真正生效的开关」两件事。
#[tauri::command]
pub async fn im_preferences_get(bridge: tauri::State<'_, BridgeState>) -> Result<Value, String> {
    call(bridge.inner(), "GET", "/api/im/preferences".into(), None).await
}

/// 写 IM 频道偏好（`im:preferencesSet`）：Rust 先挡掉非布尔值，键合法性由引擎裁定
#[tauri::command]
pub async fn im_preferences_set(
    preferences: Value,
    bridge: tauri::State<'_, BridgeState>,
) -> Result<Value, String> {
    let patch = validate_preferences(&preferences)?;
    call(
        bridge.inner(),
        "POST",
        "/api/im/preferences".into(),
        Some(json!({ "preferences": patch })),
    )
    .await
}

/// 偏好补丁校验（独立出来便于单测：空补丁与非布尔值都要在出网前挡下）
pub fn validate_preferences(raw: &Value) -> Result<serde_json::Map<String, Value>, String> {
    let patch = raw
        .as_object()
        .cloned()
        .ok_or_else(|| "IM_PREFERENCE_INVALID: 偏好项格式不对".to_string())?;
    if patch.is_empty() {
        return Err("IM_PREFERENCE_INVALID: 未提交偏好项".into());
    }
    for (k, v) in &patch {
        if !v.is_boolean() {
            return Err(format!("IM_PREFERENCE_INVALID: {k} 只接受 true/false"));
        }
    }
    Ok(patch)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kind_must_be_path_safe() {
        assert_eq!(validate_kind(" slack ").unwrap(), "slack");
        assert_eq!(validate_kind("ding-talk_1").unwrap(), "ding-talk_1");
        for bad in ["", "Slack", "a/b", "../x", "a b", &"z".repeat(33)] {
            assert!(validate_kind(bad).is_err(), "{bad} 应被拒绝");
        }
        assert!(validate_kind("../x").unwrap_err().starts_with("IM_CHANNEL_INVALID"));
    }

    #[test]
    fn engine_failure_becomes_code_reason() {
        let raw = json!({ "ok": false, "code": "IM_AUTH_FAILED", "message": "Slack error=invalid_auth" });
        let err = engine_outcome(400, &raw).unwrap_err();
        assert_eq!(err, "IM_AUTH_FAILED: Slack error=invalid_auth");
    }

    #[test]
    fn engine_failure_without_detail_keeps_code() {
        let err = engine_outcome(400, &json!({ "ok": false, "code": "IM_UNSUPPORTED" })).unwrap_err();
        assert_eq!(err, "IM_UNSUPPORTED");
    }

    #[test]
    fn http_error_without_code_gets_engine_error_code() {
        let err = engine_outcome(500, &json!({ "detail": "boom" })).unwrap_err();
        assert!(err.starts_with("IM_ENGINE_ERROR: "), "{err}");
    }

    #[test]
    fn success_payload_passes_through() {
        let raw = json!({ "ok": true, "channels": [{ "kind": "slack" }] });
        assert_eq!(engine_outcome(200, &raw).unwrap(), raw);
    }

    #[test]
    fn preferences_only_accept_nonempty_booleans() {
        assert_eq!(
            validate_preferences(&json!({ "showToolProcess": true })).unwrap()["showToolProcess"],
            json!(true)
        );
        assert!(validate_preferences(&json!({})).unwrap_err().starts_with("IM_PREFERENCE_INVALID"));
        assert!(validate_preferences(&json!({ "showToolProcess": "true" }))
            .unwrap_err()
            .contains("只接受 true/false"));
        assert!(validate_preferences(&json!("yes"))
            .unwrap_err()
            .starts_with("IM_PREFERENCE_INVALID"));
    }
}
