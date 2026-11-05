//! 启动器命令行入口（隐藏子命令 `--tt-sandbox-run`）
//!
//! 只解析启动器需要的参数，遇到未知参数直接 fail-closed；
//! 子命令不会初始化 Tauri，也不加载任何前端资源。

use std::path::PathBuf;

use super::CLI_FLAG;

/// 启动器参数
#[derive(Debug, Clone)]
pub struct RunOptions {
    pub profile: String,
    pub workspace: Option<PathBuf>,
    /// 额外只读授权目录（解释器自身与脚本所在目录）
    pub read_dirs: Vec<PathBuf>,
    /// 允许保留给子进程的敏感环境变量名（用户显式为连接器配置的那些）
    pub keep_env: Vec<String>,
    pub network: bool,
    pub program: String,
    pub args: Vec<String>,
    /// 参数按原文拼接成交给子进程的命令行，不逐个加引号。
    ///
    /// 只给 `cmd.exe` 这类**不吃 MSVCRT argv 转义**的壳用：它们自己有一套引号规则，
    /// 逐参数转义会把 `"` 变成 `\"`，壳收到的是反斜杠而不是引号（D-48.4c 实测）。
    /// 开了这一档，隔离照做，但命令行与不进容器时逐字一致——容器不许改变命令的含义。
    pub raw_command_line: bool,
}

pub fn usage() -> String {
    format!(
        "用法：{CLI_FLAG} [--profile <名称>] [--workspace <目录>] [--read-dir <目录>] [--allow-env <变量名>] [--network on|off] [--raw-command-line] -- <程序> [参数...]"
    )
}

/// 命令行里出现启动器标记就接管；返回进程退出码
pub fn try_run() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    if !args.iter().any(|a| a == CLI_FLAG) {
        return None;
    }
    let opts = match parse(&args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("沙箱启动器参数错误：{e}");
            eprintln!("{}", usage());
            return Some(125);
        }
    };
    Some(dispatch(&opts))
}

fn parse(args: &[String]) -> Result<RunOptions, String> {
    let start = args
        .iter()
        .position(|a| a == CLI_FLAG)
        .ok_or_else(|| "缺少入口标记".to_string())?;
    let mut profile = super::PROFILE_NAME.to_string();
    let mut workspace: Option<PathBuf> = None;
    let mut read_dirs: Vec<PathBuf> = Vec::new();
    let mut keep_env: Vec<String> = Vec::new();
    let mut network = false;
    let mut raw_command_line = false;
    let mut i = start + 1;

    while i < args.len() {
        match args[i].as_str() {
            "--" => {
                let rest = &args[i + 1..];
                if rest.is_empty() {
                    return Err("`--` 之后必须给要执行的程序".into());
                }
                return Ok(RunOptions {
                    profile,
                    workspace,
                    read_dirs,
                    keep_env,
                    network,
                    program: rest[0].clone(),
                    args: rest[1..].to_vec(),
                    raw_command_line,
                });
            }
            "--profile" => {
                i += 1;
                profile = args
                    .get(i)
                    .filter(|s| !s.trim().is_empty())
                    .ok_or_else(|| "--profile 缺少值".to_string())?
                    .clone();
            }
            "--workspace" => {
                i += 1;
                let raw = args.get(i).ok_or_else(|| "--workspace 缺少值".to_string())?;
                if raw.trim().is_empty() {
                    return Err("--workspace 不能为空".into());
                }
                workspace = Some(PathBuf::from(raw));
            }
            "--read-dir" => {
                i += 1;
                let raw = args.get(i).ok_or_else(|| "--read-dir 缺少值".to_string())?;
                if raw.trim().is_empty() {
                    return Err("--read-dir 不能为空".into());
                }
                read_dirs.push(PathBuf::from(raw));
            }
            "--allow-env" => {
                i += 1;
                let raw = args.get(i).ok_or_else(|| "--allow-env 缺少值".to_string())?;
                if raw.trim().is_empty() {
                    return Err("--allow-env 不能为空".into());
                }
                keep_env.push(raw.trim().to_string());
            }
            "--network" => {
                i += 1;
                network = match args.get(i).map(|s| s.as_str()) {
                    Some("on") => true,
                    Some("off") => false,
                    _ => return Err("--network 只接受 on 或 off".into()),
                };
            }
            "--raw-command-line" => {
                raw_command_line = true;
            }
            other => return Err(format!("未知参数「{other}」")),
        }
        i += 1;
    }
    Err("缺少 `--` 与要执行的程序".into())
}

fn dispatch(opts: &RunOptions) -> i32 {
    #[cfg(windows)]
    {
        super::win::run(opts)
    }
    #[cfg(not(windows))]
    {
        let _ = opts;
        eprintln!("沙箱启动失败：当前平台不支持 AppContainer，命令未执行");
        125
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parses_program_after_separator() {
        let opts = parse(&argv(&[
            "app.exe",
            CLI_FLAG,
            "--workspace",
            "D:/ws",
            "--network",
            "on",
            "--",
            "python.exe",
            "-c",
            "print(1)",
        ]))
        .unwrap();
        assert_eq!(opts.program, "python.exe");
        assert_eq!(opts.args, vec!["-c", "print(1)"]);
        assert!(opts.network);
        assert_eq!(opts.workspace.unwrap().to_string_lossy(), "D:/ws");
    }

    #[test]
    fn rejects_missing_separator() {
        assert!(parse(&argv(&["app.exe", CLI_FLAG, "--network", "off"])).is_err());
    }

    #[test]
    fn rejects_unknown_flag() {
        let err = parse(&argv(&["app.exe", CLI_FLAG, "--evil", "--", "cmd.exe"])).unwrap_err();
        assert!(err.contains("未知参数"));
    }

    #[test]
    fn rejects_bad_network_value() {
        assert!(parse(&argv(&["app.exe", CLI_FLAG, "--network", "yes", "--", "cmd.exe"])).is_err());
    }

    #[test]
    fn parses_repeated_read_dirs() {
        let opts = parse(&argv(&[
            "app.exe",
            CLI_FLAG,
            "--read-dir",
            "C:/tools/node",
            "--read-dir",
            "D:/mcp",
            "--",
            "node",
            "server.js",
        ]))
        .unwrap();
        assert_eq!(opts.read_dirs.len(), 2);
        assert_eq!(opts.read_dirs[1].to_string_lossy(), "D:/mcp");
    }

    #[test]
    fn defaults_to_no_workspace_and_no_network() {
        let opts = parse(&argv(&["app.exe", CLI_FLAG, "--", "cmd.exe"])).unwrap();
        assert!(opts.workspace.is_none());
        assert!(!opts.network);
        assert_eq!(opts.profile, super::super::PROFILE_NAME);
    }

    #[test]
    fn raw_command_line_defaults_off() {
        // 不带这一档时行为与改造前完全一致：其余调用方（终端、MCP、ACP、Hooks）不受影响
        let opts = parse(&argv(&["app.exe", CLI_FLAG, "--", "python.exe", "-c", "print(1)"])).unwrap();
        assert!(!opts.raw_command_line);
    }

    #[test]
    fn parses_raw_command_line_switch() {
        let opts = parse(&argv(&[
            "app.exe",
            CLI_FLAG,
            "--network",
            "off",
            "--raw-command-line",
            "--",
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            "echo \"hello world\"",
        ]))
        .unwrap();
        assert!(opts.raw_command_line);
        assert!(!opts.network);
        assert_eq!(opts.program, "cmd.exe");
    }

    #[test]
    fn raw_command_line_keeps_the_tail_verbatim() {
        // 解析阶段不许动参数：调用方拼好的那条命令行要原样送到 win::resolve_program
        let opts = parse(&argv(&[
            "app.exe",
            CLI_FLAG,
            "--raw-command-line",
            "--",
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            "echo \"hello world\"",
        ]))
        .unwrap();
        assert_eq!(opts.args.last().unwrap(), "echo \"hello world\"");
        assert_eq!(opts.args[0], "/d");
    }
}
