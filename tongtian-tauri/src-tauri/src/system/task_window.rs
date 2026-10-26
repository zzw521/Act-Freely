//! 独立任务窗口（实验特性 popWindow 的真实后端）
//!
//! 前端传会话 id 与窗口标题，这里创建（或复用）一个独立 WebviewWindow：
//! - 窗口加载同一份前端资源，路由带上 `#/?conv=<id>&pop=1`；
//! - 弹窗态由布局层识别 `pop=1`，隐藏侧边栏只保留工作台；
//! - 同一会话重复点击只聚焦已存在的窗口，不会开出一堆重复窗口。
//!
//! 边界：独立窗口与应用主窗口共享同一份 Rust 后端与 SQLite；它不是第二个应用进程。

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

/// 窗口 label 允许的字符（Tauri label 会进系统窗口标识，避免奇怪字符）
fn safe_label(conversation_id: &str) -> Option<String> {
    let trimmed = conversation_id.trim();
    if trimmed.is_empty() || trimmed.len() > 80 {
        return None;
    }
    if !trimmed
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return None;
    }
    Some(format!("task-{trimmed}"))
}

/// 打开（或聚焦）某个会话的独立任务窗口。
#[tauri::command]
pub async fn open_task_window(
    app: AppHandle,
    conversation_id: String,
    title: Option<String>,
) -> Result<Value, String> {
    let label = safe_label(&conversation_id)
        .ok_or_else(|| "TASK_WINDOW_INVALID: 会话标识为空或包含不支持字符".to_string())?;

    if let Some(existing) = app.get_webview_window(&label) {
        existing
            .show()
            .map_err(|e| format!("TASK_WINDOW_FOCUS_FAILED: {e}"))?;
        existing
            .set_focus()
            .map_err(|e| format!("TASK_WINDOW_FOCUS_FAILED: {e}"))?;
        return Ok(json!({ "label": label, "existing": true }));
    }

    let title = title
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "通天办公 · 独立任务".to_string());
    let title = if title.chars().count() > 60 {
        format!("{}…", title.chars().take(59).collect::<String>())
    } else {
        title
    };

    // 会话 id 已限定为 [A-Za-z0-9_-]，可直接放进 URL
    let url = format!("index.html#/?conv={conversation_id}&pop=1");
    let window = WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(url.into()))
        .title(title)
        .inner_size(1100.0, 780.0)
        .min_inner_size(720.0, 520.0)
        .center()
        .build()
        .map_err(|e| format!("TASK_WINDOW_CREATE_FAILED: {e}"))?;
    let _ = window.set_focus();

    Ok(json!({ "label": label, "existing": false }))
}

#[cfg(test)]
mod tests {
    use super::safe_label;

    #[test]
    fn safe_label_accepts_generated_conversation_ids() {
        assert_eq!(
            safe_label("conv-1729500000000-3").as_deref(),
            Some("task-conv-1729500000000-3")
        );
        assert_eq!(safe_label("abc_DEF-1").as_deref(), Some("task-abc_DEF-1"));
    }

    #[test]
    fn safe_label_rejects_empty_and_unsafe_input() {
        for bad in ["", "   ", "conv/../../etc", "会话-1", "conv 1", "a&b=1", "#x"] {
            assert!(safe_label(bad).is_none(), "不应接受 {bad:?}");
        }
        assert!(safe_label(&"a".repeat(81)).is_none());
    }
}
