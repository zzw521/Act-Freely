#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // 系统级沙箱启动器走隐藏子命令，必须在 Tauri 初始化之前返回
    if let Some(code) = tongtian_tauri_lib::sandbox::cli::try_run() {
        std::process::exit(code);
    }
    tongtian_tauri_lib::run()
}
