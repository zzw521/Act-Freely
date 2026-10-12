//! system 模块 — 操作系统级集成
//!
//! command：
//! - system_autostart_status: 开机自启动的实际状态（Windows 读注册表启动项；
//!   其他平台 supported=false 且 enabled=false，如实反映「系统里没有本应用启动项」）
//!
//! 写路径复用 `settings_set`：`autoStart` 落库后立即写/删启动项并回读校验，
//! 失败即返回错误（设置页以 strict 方式保存，写入失败不会被当成开启成功）。

pub mod autostart;

use std::sync::Arc;

use serde_json::Value;
use tauri::State;

use crate::P0Services;

/// 开机自启动状态（requested 为设置里的意愿，enabled 为系统启动项事实）
pub fn autostart_view(requested: bool) -> Result<Value, String> {
    let mut view = autostart::status()?;
    if let Some(map) = view.as_object_mut() {
        map.insert("requested".into(), serde_json::json!(requested));
    }
    Ok(view)
}

/// 开机自启动状态（requested 为设置里的意愿，enabled 为系统启动项事实）
#[tauri::command]
pub async fn system_autostart_status(
    p0: State<'_, Arc<P0Services>>,
) -> Result<Value, String> {
    autostart_view(crate::setting_bool(&p0, autostart::SETTING_KEY))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requested_flag_merges_into_os_fact() {
        // 意愿与事实分两栏，界面才能指出「设置里开着但启动项没写」这种不一致
        let v = autostart_view(true).unwrap();
        assert_eq!(v["requested"], serde_json::json!(true));
        assert!(v["enabled"].is_boolean());
    }

    #[test]
    fn status_shape_is_self_describing_for_ui() {
        // 界面靠 supported/enabled/stale 三个事实决定文案，缺一个就会退化成猜
        let v = autostart::status().unwrap();
        for key in [
            "supported",
            "enabled",
            "command",
            "currentCommand",
            "stale",
            "valueName",
            "runKey",
        ] {
            assert!(v.get(key).is_some(), "状态缺少字段 {key}：{v}");
        }
        assert_eq!(v["valueName"], autostart::VALUE_NAME);
        // 未登记时不得留下「指向某处」的错觉
        if v["enabled"] == serde_json::json!(false) {
            assert!(v["command"].is_null(), "{v}");
            assert_eq!(v["stale"], serde_json::json!(false));
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn unsupported_platform_reports_not_enabled() {
        let v = autostart::status().unwrap();
        assert_eq!(v["supported"], serde_json::json!(false));
        assert_eq!(v["enabled"], serde_json::json!(false));
        // 关闭是语义上的既成事实，可以保存；开启必须报错
        autostart::apply(false).unwrap();
        assert!(autostart::apply(true)
            .unwrap_err()
            .starts_with("AUTOSTART_UNSUPPORTED"));
    }
}
