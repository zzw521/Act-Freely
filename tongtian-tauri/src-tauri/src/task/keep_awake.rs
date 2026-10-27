//! 保持系统唤醒（定时任务页 / 设置页共用）
//!
//! 目标：防止电脑在 Agent 工作时自动进入睡眠（界面承诺「真实生效」）。
//!
//! Windows：以隐藏子进程持有 `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`，
//! 关闭时终止子进程；进程存活即代表约束生效（`is_active` 用 try_wait 实测，
//! 脚本启动失败不会被静默当成成功）。
//! 其他平台：返回 `SCHEDULE_KEEP_AWAKE_UNSUPPORTED`，不静默假装生效。

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

/// 设置项键名（settings_kv；设置页与定时任务页共用同一口径）
pub const SETTING_KEY: &str = "keepAwake";

/// 0x80000000 ES_CONTINUOUS | 0x00000001 ES_SYSTEM_REQUIRED
#[cfg(windows)]
const ES_FLAGS: u64 = 2_147_483_649;

#[cfg(windows)]
const HOLDER_SCRIPT: &str = "Add-Type -Namespace TT -Name Power -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern uint SetThreadExecutionState(uint es);'; while ($true) { [TT.Power]::SetThreadExecutionState(2147483649) | Out-Null; Start-Sleep -Seconds 30 }";

/// 唤醒约束持有者（Tauri 托管状态，单实例）
pub struct KeepAwake {
    child: Mutex<Option<Child>>,
}

impl Default for KeepAwake {
    fn default() -> Self {
        Self::new()
    }
}

impl KeepAwake {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }

    /// 约束是否真实生效（子进程存活）
    pub fn is_active(&self) -> bool {
        let mut guard = self.child.lock().unwrap_or_else(|e| e.into_inner());
        match guard.as_mut() {
            None => false,
            Some(c) => match c.try_wait() {
                Ok(None) => true,
                // 已退出：清理句柄，标记未生效
                _ => {
                    *guard = None;
                    false
                }
            },
        }
    }

    /// 开启/关闭唤醒约束；开启时实测子进程是否存活，失败即报错（不静默）
    pub fn apply(&self, enabled: bool) -> Result<(), String> {
        if !enabled {
            self.stop();
            return Ok(());
        }
        if self.is_active() {
            return Ok(());
        }
        let child = spawn_holder()?;
        {
            let mut guard = self.child.lock().unwrap_or_else(|e| e.into_inner());
            *guard = Some(child);
        }
        Ok(())
    }

    /// 终止持有的子进程（关闭开关、退出应用时调用）
    pub fn stop(&self) {
        let mut guard = self.child.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut c) = guard.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }

    /// 供命令层做启动后的存活实测
    pub fn verify(&self) -> Result<(), String> {
        if self.is_active() {
            Ok(())
        } else {
            Err("SCHEDULE_KEEP_AWAKE_FAILED: 唤醒约束进程未能保持运行".into())
        }
    }
}

#[cfg(windows)]
fn spawn_holder() -> Result<Child, String> {
    let mut cmd = Command::new("powershell");
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        HOLDER_SCRIPT,
    ])
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null());
    // -WindowStyle Hidden 只在 PowerShell 自己拿到控制台后生效，先抑制窗口创建
    crate::win_console::hide_console(&mut cmd);
    cmd.spawn()
        .map_err(|e| format!("SCHEDULE_KEEP_AWAKE_FAILED: 唤醒约束进程启动失败（{e}）"))
}

#[cfg(not(windows))]
fn spawn_holder() -> Result<Child, String> {
    Err("SCHEDULE_KEEP_AWAKE_UNSUPPORTED: 当前平台暂不支持保持系统唤醒".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_is_inactive() {
        let k = KeepAwake::new();
        assert!(!k.is_active());
        // 关闭未开启的约束是幂等的
        k.apply(false).unwrap();
        assert!(!k.is_active());
    }

    #[test]
    fn test_stop_is_idempotent_without_child() {
        let k = KeepAwake::new();
        k.stop();
        k.stop();
        assert!(!k.is_active());
    }

    #[cfg(windows)]
    #[test]
    fn test_enable_then_disable_real_enforcement() {
        let k = KeepAwake::new();
        k.apply(true).unwrap();
        assert!(k.is_active(), "开启后子进程应存活");
        k.stop();
        assert!(!k.is_active(), "关闭后子进程应被终止");
    }

    #[cfg(not(windows))]
    #[test]
    fn test_unsupported_platform_reports_error() {
        let k = KeepAwake::new();
        let err = k.apply(true).unwrap_err();
        assert!(err.starts_with("SCHEDULE_KEEP_AWAKE_UNSUPPORTED:"), "{err}");
        assert!(!k.is_active());
    }
}