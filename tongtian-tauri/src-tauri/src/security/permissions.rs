//! 系统权限清单与授权入口（设置页「系统权限」区）
//!
//! 这里的每条状态只允许两种来源：操作系统能真实回答的（前端用 web API 探测，
//! 见 `probe` 字段），以及「需要在系统设置里管理」。本模块负责给出每个能力在
//! 当前平台**真实可达**的设置面板 URI；平台根本没有这个开关时如实说明，
//! 而不是像以前那样写死一行「已授权」。
//!
//! 面板 URI 全部是系统内置的深链（`ms-settings:` / `x-apple.systempreferences:`），
//! 打开动作复用已注册好的 `tauri-plugin-opener`，不引入新依赖。

use serde_json::{json, Value};
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

/// 请求的能力未收录（前端拼错 key）
pub const ERROR_KEY_UNKNOWN: &str = "PERMISSION_KEY_UNKNOWN";
/// 当前平台没有该开关，或没有可达的设置面板
pub const ERROR_PANE_UNAVAILABLE: &str = "PERMISSION_PANE_UNAVAILABLE";
/// 系统拒绝打开设置面板（策略禁用深链、面板缺失等）
pub const ERROR_OPEN_FAILED: &str = "PERMISSION_OPEN_FAILED";

/// 前端能拿到真实状态的两项：通知与麦克风（其余只能去系统设置）
pub const PROBE_NOTIFICATION: &str = "notification";
pub const PROBE_MICROPHONE: &str = "microphone";

pub struct Capability {
    pub key: &'static str,
    pub label: &'static str,
    pub purpose: &'static str,
    /// 状态探测通道；`None` 表示无法从代码里读出真实状态
    pub probe: Option<&'static str>,
    pub macos_pane: Option<&'static str>,
    pub windows_pane: Option<&'static str>,
    /// 当前平台没有这个面板时的如实解释（替代原来的「已授权」）
    pub no_pane_note: &'static str,
}

/// 设置页展示顺序与文案口径由这张表唯一决定
pub const CAPABILITIES: [Capability; 7] = [
    Capability {
        key: "disk",
        label: "完全磁盘访问权限",
        purpose: "读写项目文件等操作",
        probe: None,
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"),
        windows_pane: None,
        no_pane_note: "Windows 无此开关，文件读写跟随当前用户权限",
    },
    Capability {
        key: "screen",
        label: "录屏与系统录音",
        purpose: "屏幕内容识别、反馈截图",
        probe: None,
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"),
        windows_pane: None,
        no_pane_note: "Windows 无系统级录屏开关，由应用在采集时申请",
    },
    Capability {
        key: "accessibility",
        label: "辅助功能",
        purpose: "监听全局快捷键，快捷唤起",
        probe: None,
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"),
        windows_pane: None,
        no_pane_note: "Windows 全局快捷键无需授权",
    },
    Capability {
        key: "microphone",
        label: "麦克风",
        purpose: "语音输入等功能",
        probe: Some(PROBE_MICROPHONE),
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"),
        windows_pane: Some("ms-settings:privacy-microphone"),
        no_pane_note: "当前平台请在系统设置中管理麦克风权限",
    },
    Capability {
        key: "automation",
        label: "自动化",
        purpose: "与系统应用协作（提醒/日历/备忘录）",
        probe: None,
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"),
        windows_pane: None,
        no_pane_note: "Windows 无自动化授权中心，按用户权限直接调用",
    },
    Capability {
        key: "notifications",
        label: "通知",
        purpose: "任务完成、消息提醒",
        probe: Some(PROBE_NOTIFICATION),
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Notifications"),
        windows_pane: Some("ms-settings:notifications"),
        no_pane_note: "当前平台请在系统设置中管理通知权限",
    },
    Capability {
        key: "location",
        label: "位置服务",
        purpose: "处理与地理位置相关的任务",
        probe: None,
        macos_pane: Some("x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices"),
        windows_pane: Some("ms-settings:privacy-location"),
        no_pane_note: "当前平台请在系统设置中管理位置权限",
    },
];

fn find(key: &str) -> Option<&'static Capability> {
    CAPABILITIES.iter().find(|c| c.key == key)
}

/// 当前平台真实可达的设置面板
pub fn pane_for(os: &str, c: &Capability) -> Option<&'static str> {
    match os {
        "macos" => c.macos_pane,
        "windows" => c.windows_pane,
        _ => None,
    }
}

pub fn view_of(os: &str, c: &Capability) -> Value {
    let pane = pane_for(os, c);
    json!({
        "key": c.key,
        "label": c.label,
        "purpose": c.purpose,
        "probe": c.probe,
        "pane": pane,
        // 有面板时不塞解释文案，避免页面出现「未授权 + 一段辩解」
        "note": if pane.is_some() { Value::Null } else { Value::String(c.no_pane_note.to_string()) },
    })
}

/// 清单视图：`{os, items[]}`，items 顺序即展示顺序
pub fn status(os: &str) -> Value {
    json!({
        "os": os,
        "items": CAPABILITIES.iter().map(|c| view_of(os, c)).collect::<Vec<Value>>(),
    })
}

/// 打开对应设置面板；平台不支持或未收录时如实报错
pub fn open(app: &AppHandle, key: &str) -> Result<Value, String> {
    let os = std::env::consts::OS;
    let c = find(key).ok_or_else(|| format!("{ERROR_KEY_UNKNOWN}: 未收录的系统能力 {key}"))?;
    let pane = pane_for(os, c)
        .ok_or_else(|| format!("{ERROR_PANE_UNAVAILABLE}: {}", c.no_pane_note))?;
    app.opener()
        .open_url(pane, None::<&str>)
        .map_err(|e| format!("{ERROR_OPEN_FAILED}: {e}"))?;
    Ok(json!({ "key": key, "opened": pane, "os": os }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_keys_are_unique_and_complete() {
        let mut seen: Vec<&str> = vec![];
        for c in CAPABILITIES.iter() {
            assert!(!c.label.is_empty() && !c.purpose.is_empty());
            assert!(!seen.contains(&c.key), "重复的能力键 {}", c.key);
            seen.push(c.key);
        }
        assert_eq!(seen.len(), 7);
    }

    #[test]
    fn only_notification_and_microphone_claim_a_real_probe() {
        let probes: Vec<&str> = CAPABILITIES
            .iter()
            .filter_map(|c| c.probe)
            .collect();
        assert_eq!(probes, vec![PROBE_MICROPHONE, PROBE_NOTIFICATION]);
    }

    #[test]
    fn macos_exposes_every_privacy_pane() {
        for c in CAPABILITIES.iter() {
            assert!(
                pane_for("macos", c).is_some(),
                "{} 在 macOS 应有隐私面板",
                c.key
            );
        }
        assert!(status("macos")["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|i| i["pane"].is_string()));
    }

    #[test]
    fn windows_only_exposes_panes_that_actually_exist() {
        let v = status("windows");
        let items = v["items"].as_array().unwrap();
        let with_pane: Vec<&str> = items
            .iter()
            .filter(|i| i["pane"].is_string())
            .map(|i| i["key"].as_str().unwrap())
            .collect();
        assert_eq!(with_pane, vec!["microphone", "notifications", "location"]);
        // 其余项必须带真实解释，而不是被省略或谎报已授权
        for i in items.iter().filter(|i| i["pane"].is_null()) {
            assert!(i["note"].as_str().unwrap().contains("无"));
        }
    }

    #[test]
    fn unknown_platform_reports_no_pane_but_keeps_the_list() {
        let v = status("linux");
        assert_eq!(v["os"], "linux");
        let items = v["items"].as_array().unwrap();
        assert_eq!(items.len(), CAPABILITIES.len());
        assert!(items.iter().all(|i| i["pane"].is_null()));
        assert!(items.iter().all(|i| i["note"].is_string()));
    }

    #[test]
    fn pane_uris_are_system_deeplinks_only() {
        for c in CAPABILITIES.iter() {
            for p in [c.macos_pane, c.windows_pane].into_iter().flatten() {
                assert!(
                    p.starts_with("ms-settings:") || p.starts_with("x-apple.systempreferences:"),
                    "{p} 不是系统设置深链"
                );
            }
        }
    }
}
