//! 开机自启动（设置页「系统设置」分区）
//!
//! 目标：开关承诺的「登录电脑时自动启动通天办公」必须真的落到系统启动项，
//! 而不是只往 settings_kv 写一个布尔值。
//!
//! Windows：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下以 `TongTianOffice`
//! 值名写入当前可执行文件路径，关闭时删除该值；状态一律回读注册表——
//! `reg` 写入失败会被当作失败返回，不会静默当成已生效。
//! 其他平台：返回 `AUTOSTART_UNSUPPORTED`，由界面如实说明，不假装开启。
//!
//! 启动命令取 `current_exe()`：开发态（`target\debug`）与安装包路径不同，
//! 故每次开启都重写值，避免留下指向旧构建的启动项。

use serde_json::{json, Value};
use std::process::Command;

/// 设置项键名（settings_kv）
pub const SETTING_KEY: &str = "autoStart";

/// 注册表值名（用 ASCII，避免中文值名在 reg 控制台输出里编码歧义导致回读误判）
pub const VALUE_NAME: &str = "TongTianOffice";

/// 当前用户启动项键（HKCU 不需要管理员权限）
pub const RUN_KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";

/// reg 动作（纯数据，便于把参数装配单测化）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Query,
    Add,
    Delete,
}

/// 启动项登记的命令（当前可执行文件绝对路径；取不到返回空串）
pub fn launch_command() -> String {
    std::env::current_exe()
        .map(|p| p.display().to_string())
        .unwrap_or_default()
}

/// 组装 reg 命令参数（不执行；单测覆盖，避免测试触碰真实注册表）
pub fn reg_args(action: Action, exe: &str) -> Vec<String> {
    let key = RUN_KEY.to_string();
    let name = VALUE_NAME.to_string();
    match action {
        Action::Query => vec!["query".into(), key, "/v".into(), name],
        Action::Add => vec![
            "add".into(),
            key,
            "/v".into(),
            name,
            "/t".into(),
            "REG_SZ".into(),
            "/d".into(),
            exe.into(),
            "/f".into(),
        ],
        Action::Delete => vec!["delete".into(), key, "/v".into(), name, "/f".into()],
    }
}

/// 从 `reg query` 的文本输出里取本启动项已登记的路径。
/// 输出形如：`    TongTianOffice    REG_SZ    C:\Apps\通天办公.exe`
pub fn parse_query(stdout: &str) -> Option<String> {
    for line in stdout.lines() {
        let t = line.trim_start();
        // 值名长度可能落在别的行的多字节字符中间（注册表里还有其他程序的中文值名）
        if !t.is_char_boundary(VALUE_NAME.len())
            || !t[..VALUE_NAME.len()].eq_ignore_ascii_case(VALUE_NAME)
        {
            continue;
        }
        let rest = &t[VALUE_NAME.len()..];
        // 值名必须整段匹配：TongTianOfficeHelper 之类的前缀相同但不是我们
        if !rest.starts_with(' ') && !rest.starts_with('\t') {
            continue;
        }
        let rest = rest.trim_start();
        // 跳过类型列，取剩余整段作为值数据
        let value = match rest.find("REG_SZ") {
            Some(i) => rest[i + "REG_SZ".len()..].trim(),
            None => rest.trim(),
        };
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

/// reg 认为「目标本就不存在」的退出码。
/// 实机验证：`reg query`/`reg delete` 对缺失值都给 1（系统找不到指定的注册表项或值），
/// 个别 Windows 版本的 delete 会给 2。这两种都说明「本来就没有启动项」，不是失败。
pub fn code_means_absent(action: Action, code: i32) -> bool {
    matches!(
        (action, code),
        (Action::Query, 1) | (Action::Query, 2) | (Action::Delete, 1) | (Action::Delete, 2)
    )
}

#[cfg(windows)]
fn run_reg(args: &[String]) -> Result<std::process::Output, String> {
    Command::new("reg")
        .args(args)
        .output()
        .map_err(|e| format!("AUTOSTART_FAILED: 无法调用系统启动项配置命令（{e}）"))
}

#[cfg(not(windows))]
fn run_reg(_args: &[String]) -> Result<std::process::Output, String> {
    Err("AUTOSTART_UNSUPPORTED: 当前平台暂不支持应用内设置开机自启动".into())
}

/// 注册表里是否已有本应用的启动项，以及登记的路径
#[cfg(windows)]
pub fn registered() -> Result<Option<String>, String> {
    let out = run_reg(&reg_args(Action::Query, ""))?;
    match out.status.code() {
        Some(0) => Ok(parse_query(&String::from_utf8_lossy(&out.stdout))),
        Some(code) if code_means_absent(Action::Query, code) => Ok(None),
        Some(code) => Err(format!(
            "AUTOSTART_FAILED: 读取启动项失败（reg 退出码 {code}）"
        )),
        None => Err("AUTOSTART_FAILED: 读取启动项被中断".into()),
    }
}

#[cfg(not(windows))]
pub fn registered() -> Result<Option<String>, String> {
    // 非 Windows 没有这个注册表概念：如实「未登记」，写入时再由 apply 报 unsupported
    Ok(None)
}

/// 写入/删除启动项，并回读校验（回读不一致即报错，不留下「显示已开但其实没写」）
pub fn apply(enabled: bool) -> Result<(), String> {
    if !cfg!(windows) {
        // 关闭在语义上等价于「系统里本来就没有本应用的启动项」，可以如实落库；
        // 开启则必须报不支持，不能让开关亮着却没有启动项
        if !enabled {
            return Ok(());
        }
        return Err("AUTOSTART_UNSUPPORTED: 当前平台暂不支持应用内设置开机自启动，请在系统设置中手动配置".into());
    }
    let exe = launch_command();
    if exe.is_empty() {
        return Err("AUTOSTART_FAILED: 无法定位当前程序路径，启动项未写入".into());
    }
    let action = if enabled { Action::Add } else { Action::Delete };
    let out = run_reg(&reg_args(action, &exe))?;
    if !out.status.success() {
        let code = out.status.code();
        // 关闭时「启动项本来就不在」不是失败（reg 对缺失值给 1）：交给下面的回读确认事实
        let already_absent = matches!(action, Action::Delete)
            && code.map_or(false, |c| code_means_absent(Action::Delete, c));
        if !already_absent {
            let detail = String::from_utf8_lossy(&out.stderr).trim().to_string();
            return Err(match code {
                Some(c) => format!(
                    "AUTOSTART_FAILED: 启动项写入失败（reg 退出码 {c}{}）",
                    if detail.is_empty() { String::new() } else { format!("：{detail}") }
                ),
                None => "AUTOSTART_FAILED: 启动项写入被中断".into(),
            });
        }
    }
    let now = registered()?;
    match (enabled, now) {
        (true, Some(path)) if path == exe => Ok(()),
        (true, Some(path)) => Err(format!(
            "AUTOSTART_FAILED: 启动项已存在但指向其他程序（{path}），请在系统设置中手动调整"
        )),
        (true, None) => Err("AUTOSTART_FAILED: 启动项写入后回读不到，未确认生效".into()),
        (false, None) => Ok(()),
        (false, Some(path)) => Err(format!(
            "AUTOSTART_FAILED: 启动项未能删除，仍指向 {path}"
        )),
    }
}

/// 当前状态（requested 为设置里的意愿，enabled 为注册表事实）
pub fn status() -> Result<Value, String> {
    let registered_path = registered()?;
    let exe = launch_command();
    Ok(json!({
        "supported": cfg!(windows),
        "enabled": registered_path.is_some(),
        "command": registered_path,
        "currentCommand": exe,
        // 开发态构建路径变动时，启动项会指向旧路径：如实告知而不是当作正常
        "stale": registered_path.as_deref().map_or(false, |p| !exe.is_empty() && p != exe),
        "valueName": VALUE_NAME,
        "runKey": RUN_KEY,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_args_target_our_run_value_only() {
        let args = reg_args(Action::Query, "");
        assert_eq!(args[0], "query");
        assert_eq!(args[1], RUN_KEY);
        assert!(args.contains(&"/v".to_string()));
        assert!(args.contains(&VALUE_NAME.to_string()));
        // 绝不允许通配：删除/覆盖整个 Run 键会连带干掉其他软件的启动项
        assert!(!args.iter().any(|a| a.contains('*')));
        assert!(!args.iter().any(|a| a == "/f"));
    }

    #[test]
    fn add_args_write_reg_sz_with_forced_overwrite() {
        let args = reg_args(Action::Add, "C:\\Apps\\tt.exe");
        assert_eq!(args[0], "add");
        let i = args.iter().position(|a| a == "/d").unwrap();
        assert_eq!(args[i + 1], "C:\\Apps\\tt.exe");
        assert!(args.contains(&"REG_SZ".to_string()));
        assert!(args.last().unwrap() == "/f");
    }

    #[test]
    fn delete_args_remove_single_value() {
        let args = reg_args(Action::Delete, "");
        assert_eq!(args[0], "delete");
        assert_eq!(args[1], RUN_KEY);
        assert!(args.contains(&VALUE_NAME.to_string()));
        assert!(!args.iter().any(|a| a.contains('*')));
    }

    #[test]
    fn parse_query_reads_registered_path_with_spaces_and_cjk() {
        let stdout = "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\n    OneDrive    REG_SZ    C:\\Users\\zzw\\OneDrive.exe\n    TongTianOffice    REG_SZ    C:\\Program Files\\通天办公\\tong-tian.exe\n";
        assert_eq!(
            parse_query(stdout).as_deref(),
            Some("C:\\Program Files\\通天办公\\tong-tian.exe")
        );
    }

    #[test]
    fn parse_query_misses_other_values() {
        let stdout = "HKEY_CURRENT_USER\\...\\Run\n    SomethingElse    REG_SZ    C:\\x.exe\n";
        assert_eq!(parse_query(stdout), None);
        assert_eq!(parse_query(""), None);
    }

    #[test]
    fn parse_query_requires_whole_value_name() {
        // 前缀相同但不是本应用的值名不得被认成已开启
        let stdout = "    TongTianOfficeHelper    REG_SZ    C:\\other.exe\n";
        assert_eq!(parse_query(stdout), None);
    }

    #[test]
    fn parse_query_survives_cjk_lines_before_ours() {
        // 中文值名的字节长度可能让前缀比较落在字符中间，必须按字符边界判断
        let stdout = "    通天办公其他项其他其他    REG_SZ    C:\\a.exe\n    TongTianOffice    REG_SZ    C:\\b.exe\n";
        assert_eq!(parse_query(stdout).as_deref(), Some("C:\\b.exe"));
    }

    #[test]
    fn parse_query_is_case_insensitive_on_value_name() {
        // reg 输出的值名大小写由写入时决定，回读不能因为大小写漏判成「未开启」
        let stdout = "    tongtianoffice    REG_SZ    C:\\a.exe\n";
        assert_eq!(parse_query(stdout).as_deref(), Some("C:\\a.exe"));
    }

    #[test]
    fn absent_exit_codes_only_for_reads_and_removals() {
        // 实机：reg query / reg delete 对缺失值都给 1（个别版本给 2）；写入(ADD)失败没有这种宽容
        assert!(code_means_absent(Action::Query, 1));
        assert!(code_means_absent(Action::Query, 2));
        assert!(code_means_absent(Action::Delete, 1));
        assert!(code_means_absent(Action::Delete, 2));
        assert!(!code_means_absent(Action::Add, 1));
        assert!(!code_means_absent(Action::Add, 2));
        assert!(!code_means_absent(Action::Query, 0));
        assert!(!code_means_absent(Action::Delete, 5));
    }

    #[test]
    fn parse_query_handles_real_reg_output_shape() {
        // 与 Windows 实机输出同形状（值名后 4 空格 + REG_SZ + 4 空格 + 路径）
        let stdout = "\r\nHKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\r\n    TongTianOffice    REG_SZ    D:\\qoder\\tongtianwork\\src-tauri\\target\\debug\\tongtian-tauri.exe\r\n";
        assert_eq!(
            parse_query(stdout).as_deref(),
            Some("D:\\qoder\\tongtianwork\\src-tauri\\target\\debug\\tongtian-tauri.exe")
        );
    }

    #[test]
    fn apply_without_platform_support_reports_unsupported_off_windows() {
        if cfg!(windows) {
            // Windows 上不断言 unsupported（真实平台能力由手工验证），只校验空路径守卫
            assert!(!launch_command().is_empty(), "测试进程应能取到自身路径");
            return;
        }
        let e = apply(true).unwrap_err();
        assert!(e.starts_with("AUTOSTART_UNSUPPORTED"), "{e}");
    }

    #[cfg(windows)]
    #[test]
    fn registered_read_is_non_destructive_and_tolerates_missing_value() {
        // 只读查询：注册表里没有本应用启动项时必须解析成 None 而不是报错，
        // 否则开关永远显示「读取失败」，用户无法开启
        assert!(registered().is_ok());
    }
}
