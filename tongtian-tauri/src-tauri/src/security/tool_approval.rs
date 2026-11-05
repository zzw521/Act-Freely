//! 逐工具审批档位与系统确认框（设置项 `toolApprovalMode`）
//!
//! 引擎在工具真正执行前，把「工具名 + 入参摘要」发到本机回环网关
//! （`bridge/tool_gate.rs`），由这里弹系统确认框。档位语义：
//! - `off`：不审批（默认）；
//! - `sensitive`：写文件/命令执行/联网/连接器/子 Agent 等副作用工具询问；
//! - `all`：除浏览器/电脑操控（已有逐操作确认）外的所有工具都询问。
//!
//! 边界：审批发生在沙箱与 Hooks 之后；被沙箱拒绝或 Hook 阻断的调用不会弹框。
//! 浏览器与电脑操控各自有独立确认，不在这里重复询问。

use crate::store::{settings, Store};

pub const POLICY_KEY: &str = "toolApprovalMode";
pub const MODES: [&str; 3] = ["off", "sensitive", "all"];
pub const DEFAULT_MODE: &str = "off";

/// 确认框/审计里允许的最长描述，防止模型给出超长入参撑爆界面
pub const MAX_DESCRIPTION: usize = 600;

pub fn read_mode(store: &Store) -> Result<(String, bool), String> {
    let stored = settings::get(store, POLICY_KEY)?;
    match stored {
        None => Ok((DEFAULT_MODE.to_string(), false)),
        Some(raw) => normalize(raw.trim().trim_matches('"')).map(|v| (v, true)),
    }
}

pub fn normalize(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"');
    if MODES.contains(&v) {
        Ok(v.to_string())
    } else {
        Err(format!(
            "TOOL_APPROVAL_INVALID_POLICY: 工具审批档位非法「{v}」，仅支持 {}",
            MODES.join(" / ")
        ))
    }
}

/// 把工具名与摘要压成一行可读描述（去控制字符、限长）。
pub fn describe(tool: &str, summary: &str) -> String {
    let tool = clean(tool, 80);
    let prefix = format!("工具：{tool}");
    if summary.trim().is_empty() {
        return prefix;
    }
    // 预算是整条描述，不是摘要单独的长度：工具名也占额度
    let budget = MAX_DESCRIPTION.saturating_sub(prefix.chars().count() + 1);
    let summary = clean(summary, budget);
    if summary.is_empty() {
        prefix
    } else {
        format!("{prefix}\n{summary}")
    }
}

fn clean(raw: &str, limit: usize) -> String {
    let one = raw
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>();
    let one = one.split_whitespace().collect::<Vec<_>>().join(" ");
    if one.chars().count() <= limit {
        return one;
    }
    let kept: String = one.chars().take(limit).collect();
    format!("{kept}…")
}

/// 询问用户是否允许本次工具调用；用户取消即拒绝（返回带错误码的原因）。
pub fn confirm(description: &str) -> Result<(), String> {
    confirm_dialog(&ASK_TOOL, description)
}

/// 一次系统确认框的全部文案。同一套平台实现服务两种确认：
/// 逐工具审批（`toolApprovalMode`）与文件访问档位（`filePermission`）——
/// 两者的拒绝都必须是真的没执行，但标题与「去哪儿改」的指引不能混。
pub struct Ask {
    pub caption: &'static str,
    pub lead: &'static str,
    pub hint: &'static str,
    pub denied_code: &'static str,
    pub denied_text: &'static str,
}

pub const ASK_TOOL: Ask = Ask {
    caption: "通天办公 · 工具审批",
    lead: "通天办公请求执行一个工具：",
    hint: "可在「设置 → 系统设置 → 工具审批」调整档位",
    denied_code: "TOOL_APPROVAL_DENIED_BY_USER",
    denied_text: "用户拒绝了本次工具调用",
};

pub const ASK_FILE: Ask = Ask {
    caption: "通天办公 · 文件访问确认",
    lead: "文件访问档位为「每次询问」，通天办公请求在真实目录里改动文件：",
    hint: "档位可在对话输入框里 + 号旁的盾牌处调整",
    denied_code: "FILE_PERMISSION_DENIED_BY_USER",
    denied_text: "用户拒绝了本次文件操作",
};

/// 文件访问档位（「每次询问」）的确认框：走同一套系统弹窗，只换文案与拒绝码
pub fn confirm_file_action(description: &str) -> Result<(), String> {
    confirm_dialog(&ASK_FILE, description)
}

#[cfg(windows)]
fn confirm_dialog(ask: &Ask, description: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONWARNING, MB_OKCANCEL, MB_SETFOREGROUND, MB_TOPMOST,
    };

    let text = format!(
        "{}\n\n{description}\n\n允许这一次操作吗？\n\
         （{}；拒绝后模型不会执行该操作）",
        ask.lead, ask.hint
    );
    let text = wide(&text);
    let caption = wide(ask.caption);
    let ret = unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            caption.as_ptr(),
            MB_OKCANCEL | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND,
        )
    };
    if ret == 1 {
        Ok(())
    } else {
        Err(format!("{}: {}", ask.denied_code, ask.denied_text))
    }
}

#[cfg(windows)]
fn wide(s: &str) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    std::ffi::OsStr::new(s)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(target_os = "macos")]
fn confirm_dialog(ask: &Ask, description: &str) -> Result<(), String> {
    use std::process::Command;

    let escaped = description.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!(
        "display dialog \"{}\\n\\n{escaped}\\n\\n允许这一次操作吗？\" \
         buttons {{\"拒绝\", \"允许\"}} default button \"允许\" with icon caution",
        ask.lead
    );
    let out = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .map_err(|e| format!("TOOL_APPROVAL_CONFIRM_UNAVAILABLE: 无法拉起系统确认框：{e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    if out.status.success() && stdout.contains("允许") {
        Ok(())
    } else if out.status.success() {
        Err(format!("{}: {}", ask.denied_code, ask.denied_text))
    } else {
        Err(format!(
            "TOOL_APPROVAL_CONFIRM_UNAVAILABLE: 系统确认框未完成：{}",
            String::from_utf8_lossy(&out.stderr).trim()
        ))
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
fn confirm_dialog(ask: &Ask, description: &str) -> Result<(), String> {
    use std::process::Command;

    let text = format!("{}\n\n{description}\n\n允许这一次操作吗？", ask.lead);
    let title = format!("--title={}", ask.caption);
    if let Ok(status) = Command::new("zenity")
        .args([
            "--question",
            &title,
            "--ok-label=允许",
            "--cancel-label=拒绝",
            &format!("--text={text}"),
        ])
        .status()
    {
        return if status.success() {
            Ok(())
        } else {
            Err(format!("{}: {}", ask.denied_code, ask.denied_text))
        };
    }
    if let Ok(status) = Command::new("kdialog")
        .args(["--yesno", &text, "--title", ask.caption])
        .status()
    {
        return if status.success() {
            Ok(())
        } else {
            Err(format!("{}: {}", ask.denied_code, ask.denied_text))
        };
    }
    Err("TOOL_APPROVAL_CONFIRM_UNAVAILABLE: 当前平台没有可用的系统确认框（需要 zenity 或 kdialog）".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store(name: &str) -> (Store, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-tool-approval-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn defaults_to_off_and_reports_not_persisted() {
        let (store, dir) = temp_store("default");
        assert_eq!(read_mode(&store).unwrap(), ("off".to_string(), false));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn accepts_known_modes() {
        let (store, dir) = temp_store("modes");
        settings::set(&store, POLICY_KEY, "sensitive").unwrap();
        assert_eq!(read_mode(&store).unwrap(), ("sensitive".to_string(), true));
        settings::set(&store, POLICY_KEY, "\"all\"").unwrap();
        assert_eq!(read_mode(&store).unwrap(), ("all".to_string(), true));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn invalid_mode_fails_closed_with_readable_code() {
        let (store, dir) = temp_store("bad");
        settings::set(&store, POLICY_KEY, "everything").unwrap();
        let err = read_mode(&store).unwrap_err();
        assert!(err.contains("TOOL_APPROVAL_INVALID_POLICY"), "{err}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn file_permission_ask_has_its_own_code_and_points_at_the_tier_switch() {
        // 两种确认共用一套弹窗，但拒绝码与「去哪儿改」的指引必须分开：
        // 文件档位是在对话输入框切的，把它指到「工具审批」会让人改错开关
        assert_eq!(ASK_TOOL.denied_code, "TOOL_APPROVAL_DENIED_BY_USER");
        assert_eq!(ASK_FILE.denied_code, "FILE_PERMISSION_DENIED_BY_USER");
        assert_ne!(ASK_TOOL.denied_text, ASK_FILE.denied_text);
        assert!(ASK_FILE.lead.contains("每次询问"), "{}", ASK_FILE.lead);
        assert!(ASK_FILE.hint.contains("盾牌"), "{}", ASK_FILE.hint);
        assert!(!ASK_TOOL.hint.contains("盾牌"));
    }

    #[test]
    fn describe_strips_control_chars_and_caps_length() {        let long = "x".repeat(MAX_DESCRIPTION + 50);
        let text = describe("write\u{0}_file", &format!("文件：a.md\r\n正文 {}", long));
        assert!(!text.contains('\u{0}') && !text.contains('\r'), "{text}");
        assert!(text.chars().count() <= MAX_DESCRIPTION + 12, "{text}");
        assert!(text.starts_with("工具：write _file"), "{text}");
        assert_eq!(describe("run_command", ""), "工具：run_command");
    }
}
