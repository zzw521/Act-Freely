//! 后台常驻：关闭主窗口后进程继续运行（托盘驻留）。
//!
//! 真实边界：
//! - 只有 `runInBackground` 开启且托盘创建成功时，关窗才会隐藏而不是退出；
//!   托盘失败时宁可照旧退出，也不把窗口藏进一个没有入口的后台进程。
//! - 托盘菜单的「退出」是显式退出：置位 quitting 后调用 `app.exit(0)`，
//!   不会被关窗/退出拦截逻辑挡回来。
//! - 开机自启在后台常驻开启时带 `--hidden`，登录后直接驻留托盘，不弹窗。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// 设置项键名（settings_kv）
pub const SETTING_KEY: &str = "runInBackground";

/// 开机自启时静默启动到托盘的参数
pub const HIDDEN_ARG: &str = "--hidden";

/// 进程级后台状态：托盘是否真的创建成功、用户是否在主动退出。
#[derive(Default)]
pub struct BackgroundState {
    tray_ready: AtomicBool,
    quitting: AtomicBool,
}

impl BackgroundState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn mark_tray_ready(&self) {
        self.tray_ready.store(true, Ordering::SeqCst);
    }

    pub fn tray_ready(&self) -> bool {
        self.tray_ready.load(Ordering::SeqCst)
    }

    pub fn request_quit(&self) {
        self.quitting.store(true, Ordering::SeqCst);
    }

    pub fn is_quitting(&self) -> bool {
        self.quitting.load(Ordering::SeqCst)
    }
}

pub fn new_state() -> Arc<BackgroundState> {
    Arc::new(BackgroundState::new())
}

/// 关窗时隐藏而不是退出：用户开启 + 托盘真实可用 + 不是主动退出。
pub fn should_hide_on_close(enabled: bool, tray_ready: bool, quitting: bool) -> bool {
    enabled && tray_ready && !quitting
}

/// 最后窗口关闭触发 ExitRequested 时是否拦截。
/// 显式退出（`code=Some`，如托盘「退出」或 `app.exit`）不拦，避免关不掉。
pub fn should_prevent_exit(
    enabled: bool,
    tray_ready: bool,
    quitting: bool,
    code: Option<i32>,
) -> bool {
    enabled && tray_ready && !quitting && code.is_none()
}

/// 启动参数里是否带 `--hidden`（整段匹配，不接受 `--hidden=1` 之类近似值）。
pub fn has_hidden_arg<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    args.into_iter().any(|a| a.as_ref() == HIDDEN_ARG)
}

/// 开机自启写入注册表的命令：后台常驻时追加 `--hidden`。
pub fn command_for(exe: &str, background: bool) -> String {
    if background && !exe.is_empty() {
        format!("{exe} {HIDDEN_ARG}")
    } else {
        exe.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn close_hides_only_with_setting_and_ready_tray() {
        assert!(should_hide_on_close(true, true, false));
        assert!(!should_hide_on_close(false, true, false));
        // 托盘没建起来时隐藏窗口等于把进程藏死，必须放弃
        assert!(!should_hide_on_close(true, false, false));
        assert!(!should_hide_on_close(true, true, true));
    }

    #[test]
    fn explicit_exit_is_never_prevented() {
        // code=Some 表示托盘退出 / app.exit 等显式退出，拦截会关不掉
        assert!(should_prevent_exit(true, true, false, None));
        assert!(!should_prevent_exit(true, true, false, Some(0)));
        assert!(!should_prevent_exit(true, true, true, None));
        assert!(!should_prevent_exit(false, true, false, None));
        assert!(!should_prevent_exit(true, false, false, None));
    }

    #[test]
    fn hidden_arg_matches_exact_token_only() {
        let args = vec![
            "C:\\Apps\\tongtian.exe".to_string(),
            "--hidden".to_string(),
        ];
        assert!(has_hidden_arg(args));
        assert!(!has_hidden_arg(vec!["--hidden=1".to_string()]));
        assert!(!has_hidden_arg(vec!["C:\\Apps\\tongtian.exe".to_string()]));
    }

    #[test]
    fn autostart_command_appends_hidden_only_when_background() {
        assert_eq!(
            command_for("C:\\Apps\\通天办公.exe", true),
            "C:\\Apps\\通天办公.exe --hidden"
        );
        assert_eq!(command_for("C:\\Apps\\通天办公.exe", false), "C:\\Apps\\通天办公.exe");
        assert_eq!(command_for("", true), "");
    }

    #[test]
    fn state_defaults_to_not_ready_and_not_quitting() {
        let state = BackgroundState::new();
        assert!(!state.tray_ready());
        assert!(!state.is_quitting());
        state.mark_tray_ready();
        assert!(state.tray_ready());
        state.request_quit();
        assert!(state.is_quitting());
    }
}
