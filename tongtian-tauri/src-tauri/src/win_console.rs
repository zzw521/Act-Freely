//! Windows 控制台窗口抑制
//!
//! 桌面端是 GUI 子系统（main.rs: `windows_subsystem = "windows"`），自身没有控制台。
//! 从它拉起控制台程序（python、tasklist、powershell、reg…）时，Windows 默认会为
//! 子进程新建一个控制台窗口：引擎常驻时窗口会一直挂着，存活轮询时则每 10 秒闪一次。
//! 统一在这里给子进程加 `CREATE_NO_WINDOW`，不改变 stdout/stderr 管道。

#[cfg(windows)]
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[cfg(windows)]
pub fn hide_console(cmd: &mut std::process::Command) -> &mut std::process::Command {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(CREATE_NO_WINDOW)
}

#[cfg(windows)]
pub fn hide_console_async(cmd: &mut tokio::process::Command) -> &mut tokio::process::Command {
    cmd.creation_flags(CREATE_NO_WINDOW)
}

#[cfg(not(windows))]
pub fn hide_console(cmd: &mut std::process::Command) -> &mut std::process::Command {
    cmd
}

#[cfg(not(windows))]
pub fn hide_console_async(cmd: &mut tokio::process::Command) -> &mut tokio::process::Command {
    cmd
}
