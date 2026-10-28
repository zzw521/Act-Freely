//! 系统级沙箱（Windows AppContainer）
//!
//! 策略沙箱（`engine/sandbox_runtime.py`）在工作目录、工具名单与路径规则上拦工具调用；
//! 这里提供第二层：真实子进程以 AppContainer 令牌启动，写越界与读数据根由系统 ACL 拒绝。
//!
//! 启动方式是「主程序隐藏子命令」：`<exe> --tt-sandbox-run -- <program> [args...]`。
//! 单二进制避免 sidecar 打包与签名分叉；子命令在 Tauri 初始化之前返回，不拉起 WebView。
//! 任何设置失败都 fail-closed：命令不执行，退出码 125，原因用中文写到 stderr。

pub mod cli;
#[cfg(windows)]
mod win;

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{OnceLock, RwLock};

/// 隐藏子命令标记（出现在命令行即走启动器分支）
pub const CLI_FLAG: &str = "--tt-sandbox-run";
/// AppContainer profile 名（同一台机器复用同一个容器，避免每次启动重建）
pub const PROFILE_NAME: &str = "TongTian.Agent";
/// 引擎侧读取的启动器路径（空表示系统沙箱未启用）
pub const ENV_LAUNCHER: &str = "TT_SANDBOX_BIN";
/// 引擎侧读取的网络授权
pub const ENV_NETWORK: &str = "TT_SANDBOX_NETWORK";
/// 额外需要沙箱可读的运行时目录（分号分隔，由主程序注入）
pub const ENV_RUNTIME_DIRS: &str = "TT_SANDBOX_RUNTIME_DIRS";

/// 系统沙箱全局设置（与前端 `SystemSandboxConfig` 契约一致）
///
/// `enabled` 是用户意图，不是生效事实；生效状态由 `status()` 按平台与 profile 实测得出。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SystemSandboxConfig {
    pub enabled: bool,
    pub network: bool,
    pub updated_at_ms: i64,
}

impl Default for SystemSandboxConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            network: false,
            updated_at_ms: 0,
        }
    }
}

/// 系统沙箱状态：区分「请求」「平台支持」「实际生效」
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SystemSandboxStatus {
    pub requested: bool,
    pub supported: bool,
    pub effective: bool,
    pub platform: String,
    pub profile: String,
    pub network: bool,
    pub profile_ready: bool,
    pub error: Option<String>,
}

fn platform_name() -> String {
    if cfg!(windows) {
        "windows".to_string()
    } else if cfg!(target_os = "macos") {
        "macos".to_string()
    } else {
        "linux".to_string()
    }
}

/// 平台是否具备 AppContainer 实现
pub fn platform_supported() -> bool {
    cfg!(windows)
}

/// 计算真实生效状态。未启用时不探测 profile，避免无谓的系统调用。
pub fn status(config: &SystemSandboxConfig) -> SystemSandboxStatus {
    let supported = platform_supported();
    let mut error: Option<String> = None;
    let mut profile_ready = false;

    if config.enabled {
        if !supported {
            error = Some(format!(
                "当前平台（{}）不支持系统级沙箱，仅 Windows 10/11 提供 AppContainer",
                platform_name()
            ));
        } else {
            #[cfg(windows)]
            match win::profile_ready(PROFILE_NAME) {
                Ok(ready) => profile_ready = ready,
                Err(e) => error = Some(e),
            }
        }
    }

    SystemSandboxStatus {
        requested: config.enabled,
        supported,
        effective: config.enabled && supported && error.is_none(),
        platform: platform_name(),
        profile: PROFILE_NAME.to_string(),
        network: config.network,
        profile_ready,
        error,
    }
}

/// 启用前预检：真实创建 AppContainer profile，失败就拒绝启用（不落一份假状态）
pub fn prepare(network: bool) -> Result<SystemSandboxStatus, String> {
    if !platform_supported() {
        return Err(format!(
            "SANDBOX_UNSUPPORTED: 当前平台（{}）不支持系统级沙箱，仅 Windows 10/11 提供 AppContainer",
            platform_name()
        ));
    }
    #[cfg(windows)]
    win::ensure_profile_ready(PROFILE_NAME)?;
    let config = SystemSandboxConfig {
        enabled: true,
        network,
        updated_at_ms: 0,
    };
    Ok(status(&config))
}

/// 把一条真实命令包装成启动器调用。
///
/// 失败意味着「打算隔离但定位不到自身可执行文件」，调用方必须拒绝执行（fail-closed）。
pub fn wrap_argv(
    program: &str,
    args: &[String],
    workspace: Option<&Path>,
    network: bool,
    read_dirs: &[PathBuf],
    keep_env: &[String],
) -> Result<(PathBuf, Vec<String>), String> {
    let exe = std::env::current_exe().map_err(|e| {
        format!("SANDBOX_LAUNCHER_MISSING: 无法定位程序自身路径，系统沙箱不可用（{e}）")
    })?;
    let mut out: Vec<String> = vec![
        CLI_FLAG.to_string(),
        "--profile".to_string(),
        PROFILE_NAME.to_string(),
        "--network".to_string(),
        if network { "on" } else { "off" }.to_string(),
    ];
    if let Some(ws) = workspace {
        out.push("--workspace".to_string());
        out.push(ws.to_string_lossy().into_owned());
    }
    for dir in read_dirs {
        out.push("--read-dir".to_string());
        out.push(dir.to_string_lossy().into_owned());
    }
    for name in keep_env {
        out.push("--allow-env".to_string());
        out.push(name.clone());
    }
    out.push("--".to_string());
    out.push(program.to_string());
    out.extend(args.iter().cloned());
    Ok((exe, out))
}

/// 供 Rust 侧 spawn 点（MCP 探测）使用：系统沙箱未启用时返回 None，启用时返回包装后的命令。
///
/// 没有工作区：调用方只得到启动器 + 只读运行时目录；需要写文件的场景必须显式给工作区。
pub fn wrap_for_spawn(
    program: &str,
    args: &[String],
    keep_env: &[String],
) -> Result<Option<(PathBuf, Vec<String>)>, String> {
    let Some(cfg) = active() else {
        return Ok(None);
    };
    let read_dirs = read_dirs_for(program, args);
    wrap_argv(program, args, None, cfg.network, &read_dirs, keep_env).map(Some)
}

/// 命令本身所在目录（以及首个作为脚本文件的参数所在目录）作为只读授权，
/// 让 `node <script>` / `python <script>` 这类解释器能读到自己的程序。
fn read_dirs_for(program: &str, args: &[String]) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    let mut push = |p: PathBuf| {
        if !out.contains(&p) {
            out.push(p);
        }
    };
    if let Some(dir) = program_dir(program) {
        push(dir);
    }
    if let Some(first) = args.first() {
        let p = PathBuf::from(first);
        if p.is_file() {
            if let Some(dir) = p.parent() {
                push(dir.to_path_buf());
            }
        }
    }
    out
}

#[cfg(windows)]
fn program_dir(program: &str) -> Option<PathBuf> {
    let path = Path::new(program);
    if path.is_absolute() || program.contains('/') || program.contains('\\') {
        return path.parent().map(|p| p.to_path_buf());
    }
    win::find_in_path(program).and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

#[cfg(not(windows))]
fn program_dir(program: &str) -> Option<PathBuf> {
    let path = Path::new(program);
    if path.is_absolute() || program.contains('/') {
        return path.parent().map(|p| p.to_path_buf());
    }
    None
}

/// 给引擎进程注入的沙箱环境变量（未启用时返回 None）
pub fn engine_env(config: &SystemSandboxConfig) -> Option<Vec<(String, String)>> {
    if !config.enabled || !platform_supported() {
        return None;
    }
    let exe = std::env::current_exe().ok()?;
    let mut dirs: Vec<String> = Vec::new();
    if let Some(parent) = exe.parent() {
        dirs.push(parent.to_string_lossy().into_owned());
    }
    Some(vec![
        (ENV_LAUNCHER.to_string(), exe.to_string_lossy().into_owned()),
        (
            ENV_NETWORK.to_string(),
            if config.network { "on" } else { "off" }.to_string(),
        ),
        (ENV_RUNTIME_DIRS.to_string(), dirs.join(";")),
    ])
}

// ── 运行时生效状态 ──────────────────────────────────────────────────
//
// spawn 点（Hooks / MCP 探测）没有 Store 句柄，又必须读到「当前是否启用」这一事实，
// 所以在进程内维护一份只读快照；设置变更与启动时各写一次。

static ACTIVE: OnceLock<RwLock<Option<SystemSandboxConfig>>> = OnceLock::new();

fn active_cell() -> &'static RwLock<Option<SystemSandboxConfig>> {
    ACTIVE.get_or_init(|| RwLock::new(None))
}

/// 写入当前生效配置；未启用传 None
pub fn set_active(config: Option<SystemSandboxConfig>) {
    let cell = active_cell();
    if let Ok(mut guard) = cell.write() {
        *guard = config.filter(|c| c.enabled);
    }
}

/// 读取当前生效配置（None 表示未启用或未初始化）
pub fn active() -> Option<SystemSandboxConfig> {
    active_cell().read().ok().and_then(|g| g.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_argv_preserves_program_and_args() {
        let args = vec!["-c".to_string(), "print(1)".to_string()];
        let (exe, argv) = wrap_argv(
            "python.exe",
            &args,
            Some(Path::new("D:/ws")),
            false,
            &[],
            &[],
        )
        .unwrap();
        assert!(exe.exists());
        assert_eq!(argv[0], CLI_FLAG);
        assert!(argv.contains(&"--workspace".to_string()));
        assert!(argv.contains(&"D:/ws".to_string()));
        let sep = argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(&argv[sep + 1..], &["python.exe", "-c", "print(1)"]);
    }

    #[test]
    fn wrap_argv_carries_read_dirs_before_separator() {
        let (_, argv) = wrap_argv(
            "node",
            &["server.js".to_string()],
            None,
            true,
            &[PathBuf::from("C:/tools/node")],
            &["GITHUB_TOKEN".to_string()],
        )
        .unwrap();
        assert!(argv.contains(&"--read-dir".to_string()));
        assert!(argv.contains(&"C:/tools/node".to_string()));
        let keep = argv.iter().position(|a| a == "--allow-env").unwrap();
        assert_eq!(argv[keep + 1], "GITHUB_TOKEN");
        let sep = argv.iter().position(|a| a == "--").unwrap();
        assert!(argv
            .iter()
            .position(|a| a == "--read-dir")
            .unwrap()
            < sep);
    }

    #[test]
    fn status_without_enable_is_inert() {
        let s = status(&SystemSandboxConfig::default());
        assert!(!s.requested);
        assert!(!s.effective);
        assert!(s.error.is_none());
    }

    #[test]
    fn status_on_unsupported_platform_reports_error() {
        if platform_supported() {
            return;
        }
        let s = status(&SystemSandboxConfig {
            enabled: true,
            network: false,
            updated_at_ms: 0,
        });
        assert!(s.supported == false);
        assert!(!s.effective);
        assert!(s.error.unwrap().contains("不支持"));
    }
}
