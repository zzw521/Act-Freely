//! Python 环境探测 — 移植自 Electron 版 bridge/python-env.ts
//!
//! - detect_python: 探测系统 Python（python/python3/py + Windows 常见路径）
//! - detect_agent: 通过 pip show qwenpaw 检测 Agent 安装
//! - agent_start_command: 生成 Agent 启动命令
//! - ensure_workspace_dir: 工作区目录管理

use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::process::Command;

/// 随包资源根目录：打包后由 Tauri 提供（`resources` 配置的目标），开发模式不设置。
/// 在 `setup` 钩子里通过 init_resource_dir 注入一次。
static RESOURCE_DIR: OnceLock<PathBuf> = OnceLock::new();

pub fn init_resource_dir(dir: PathBuf) {
    let _ = RESOURCE_DIR.set(dir);
}

/// 随包嵌入式 Python 解释器（`vendor/python` → 打包后 `resources/python/`）
fn bundled_python() -> Option<PathBuf> {
    let dir = RESOURCE_DIR.get()?;
    let cand = if cfg!(windows) {
        dir.join("python").join("python.exe")
    } else {
        dir.join("python").join("bin").join("python3")
    };
    cand.exists().then_some(cand)
}

#[derive(Debug, Clone)]
pub struct PythonInfo {
    pub path: String,
    pub version: String,
}

#[derive(Debug, Clone)]
pub struct AgentInfo {
    pub installed: bool,
    pub version: Option<String>,
}

/// 从 `python --version` 输出中解析版本号（如 "Python 3.12.1" → "3.12.1"）
fn parse_python_version(output: &str) -> Option<String> {
    for token in output.split_whitespace() {
        let parts: Vec<&str> = token.split('.').collect();
        if parts.len() >= 2 && parts.iter().all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit())) {
            return Some(token.to_string());
        }
    }
    None
}

async fn python_version(python: &str) -> Option<String> {
    let mut cmd = Command::new(python);
    cmd.arg("--version");
    crate::win_console::hide_console_async(&mut cmd);
    let output = tokio::time::timeout(Duration::from_secs(5), cmd.output())
        .await
        .ok()?
        .ok()?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let text = if stdout.trim().is_empty() {
        String::from_utf8_lossy(&output.stderr).to_string()
    } else {
        stdout.to_string()
    };
    parse_python_version(&text)
}

/// 探测 Python：随包嵌入式解释器优先，其次系统 PATH 与 Windows 常见安装路径
pub async fn detect_python() -> Option<PythonInfo> {
    if let Some(path) = bundled_python() {
        let path_str = path.to_string_lossy().to_string();
        if let Some(version) = python_version(&path_str).await {
            return Some(PythonInfo {
                path: path_str,
                version,
            });
        }
    }

    let candidates: Vec<&str> = if cfg!(windows) {
        vec!["python", "python3", "py"]
    } else {
        vec!["python3", "python"]
    };

    for cmd in candidates {
        if let Some(version) = python_version(cmd).await {
            return Some(PythonInfo {
                path: cmd.to_string(),
                version,
            });
        }
    }

    // Windows 常见安装路径
    if cfg!(windows) {
        let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
        let paths = [
            format!("{local}\\Programs\\Python\\Python312\\python.exe"),
            format!("{local}\\Programs\\Python\\Python311\\python.exe"),
            format!("{local}\\Programs\\Python\\Python310\\python.exe"),
            "C:\\Python312\\python.exe".to_string(),
            "C:\\Python311\\python.exe".to_string(),
            "C:\\Python310\\python.exe".to_string(),
        ];
        for p in paths {
            if std::path::Path::new(&p).exists() {
                if let Some(version) = python_version(&p).await {
                    return Some(PythonInfo { path: p, version });
                }
            }
        }
    }

    None
}

/// 检测 qwenpaw 是否安装在给定 Python 环境中
pub async fn detect_agent(python_path: &str) -> AgentInfo {
    let mut cmd = Command::new(python_path);
    cmd.args(["-m", "pip", "show", "qwenpaw"]);
    crate::win_console::hide_console_async(&mut cmd);
    let result = tokio::time::timeout(Duration::from_secs(10), cmd.output()).await;

    match result {
        Ok(Ok(output)) if output.status.success() => {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let version = stdout
                .lines()
                .find(|l| l.starts_with("Version:"))
                .map(|l| l.trim_start_matches("Version:").trim().to_string());
            AgentInfo {
                installed: true,
                version,
            }
        }
        _ => AgentInfo {
            installed: false,
            version: None,
        },
    }
}

/// 数据根目录：TONGTIAN_HOME 环境变量优先（双目录设计规范），
/// 缺省 %APPDATA%\tongtian-office（Windows）或 ~/.tongtian
pub fn base_dir() -> PathBuf {
    if let Ok(home) = std::env::var("TONGTIAN_HOME") {
        if !home.trim().is_empty() {
            return PathBuf::from(home);
        }
    }
    if cfg!(windows) {
        if let Ok(appdata) = std::env::var("APPDATA") {
            return PathBuf::from(appdata).join("tongtian-office");
        }
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".tongtian")
}

/// 工作区目录路径
pub fn workspace_dir(workspace_id: &str) -> PathBuf {
    base_dir().join("workspaces").join(workspace_id)
}

/// 确保工作区目录存在
pub fn ensure_workspace_dir(workspace_id: &str) -> std::io::Result<PathBuf> {
    let dir = workspace_dir(workspace_id);
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

/// Agent 启动命令：python -m qwenpaw --port <port> --work-dir <dir>
pub fn agent_start_command(python_path: &str, port: u16, work_dir: &PathBuf) -> Vec<String> {
    vec![
        python_path.to_string(),
        "-m".to_string(),
        "qwenpaw".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--work-dir".to_string(),
        work_dir.to_string_lossy().to_string(),
    ]
}

/// 定位随包/项目内脚本，两种布局依次尝试：
/// 1) 安装布局 `<resource_dir>/<relative>`（tauri.conf.json bundle.resources 的目标）
/// 2) 开发布局 `<项目根>/<relative>`（exe 位于 项目根/src-tauri/target/<profile>/）
fn locate_project_script(relative: &str) -> Result<PathBuf, String> {
    if let Some(dir) = RESOURCE_DIR.get() {
        let packaged = dir.join(relative);
        if packaged.exists() {
            return Ok(packaged);
        }
    }

    let exe = std::env::current_exe().map_err(|e| format!("BRIDGE_EXE_PATH: {e}"))?;
    if let Some(root) = exe.ancestors().nth(4) {
        let dev = root.join(relative);
        if dev.exists() {
            return Ok(dev);
        }
    }

    Err(format!(
        "BRIDGE_SCRIPT_NOT_FOUND: {relative} 未找到（resource_dir={:?}, exe={}）",
        RESOURCE_DIR.get(),
        exe.display()
    ))
}

/// Mock Agent 启动命令（TT_AGENT_MOCK=1）：
/// python <项目根>/scripts/mock-agent.py --port <port> --work-dir <dir>
pub fn mock_agent_command(python_path: &str, port: u16, work_dir: &PathBuf) -> Result<Vec<String>, String> {
    let script = locate_project_script("scripts/mock-agent.py")?;
    Ok(vec![
        python_path.to_string(),
        script.to_string_lossy().to_string(),
        "--port".to_string(),
        port.to_string(),
        "--work-dir".to_string(),
        work_dir.to_string_lossy().to_string(),
    ])
}

/// ACP 服务端启动命令（stdio JSON-RPC，供外部 IDE/终端接入）：
/// python <项目根>/engine/acp_server.py [--engine-url <url>]
pub fn acp_server_command(python_path: &str, engine_url: Option<&str>) -> Result<Vec<String>, String> {
    let script = locate_project_script("engine/acp_server.py")?;
    let mut cmd = vec![
        python_path.to_string(),
        script.to_string_lossy().to_string(),
    ];
    if let Some(url) = engine_url.filter(|u| !u.trim().is_empty()) {
        cmd.push("--engine-url".to_string());
        cmd.push(url.to_string());
    }
    Ok(cmd)
}

/// 给设置页展示的命令行：只做可读拼接，不经过 shell。
pub fn join_command(parts: &[String]) -> String {
    parts
        .iter()
        .map(|p| {
            if p.contains(' ') {
                format!("\"{p}\"")
            } else {
                p.clone()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// AgentScope 引擎启动命令（默认引擎）：
/// python <项目根>/engine/server.py --port <port> --work-dir <dir>
pub fn agentscope_engine_command(python_path: &str, port: u16, work_dir: &PathBuf) -> Result<Vec<String>, String> {
    let script = locate_project_script("engine/server.py")?;
    Ok(vec![
        python_path.to_string(),
        script.to_string_lossy().to_string(),
        "--port".to_string(),
        port.to_string(),
        "--work-dir".to_string(),
        work_dir.to_string_lossy().to_string(),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_python_version_stdout() {
        assert_eq!(parse_python_version("Python 3.12.1"), Some("3.12.1".to_string()));
    }

    #[test]
    fn test_parse_python_version_with_prefix() {
        assert_eq!(
            parse_python_version("Python 3.10.4\n"),
            Some("3.10.4".to_string())
        );
    }

    #[test]
    fn test_parse_python_version_invalid() {
        assert_eq!(parse_python_version("not a version"), None);
        assert_eq!(parse_python_version(""), None);
    }

    #[test]
    fn test_agent_start_command() {
        let dir = PathBuf::from("C:\\work");
        let cmd = agent_start_command("python", 18001, &dir);
        assert_eq!(cmd[0], "python");
        assert_eq!(cmd[1], "-m");
        assert_eq!(cmd[2], "qwenpaw");
        assert_eq!(cmd[3], "--port");
        assert_eq!(cmd[4], "18001");
        assert_eq!(cmd[5], "--work-dir");
    }

    #[test]
    fn test_workspace_dir_contains_id() {
        let dir = workspace_dir("ws-test");
        assert!(dir.to_string_lossy().contains("ws-test"));
        assert!(dir.to_string_lossy().contains("workspaces"));
    }

    /// 安装布局下脚本应从 resource_dir 解析，而非 exe 上溯四层
    #[test]
    fn test_locate_script_prefers_resource_dir() {
        let dir = std::env::temp_dir().join(format!("tt-res-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("engine")).unwrap();
        std::fs::write(dir.join("engine").join("server.py"), b"# stub").unwrap();

        init_resource_dir(dir.clone());
        assert_eq!(
            locate_project_script("engine/server.py").unwrap(),
            dir.join("engine").join("server.py")
        );
        // 未随包的脚本不能被误定位
        assert!(locate_project_script("engine/missing.py").is_err());

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 声明为 resources 的文件必须真实存在，否则 tauri build 会在打包阶段失败
    #[test]
    fn test_bundled_resources_exist() {
        let project_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
        for rel in ["engine/server.py", "engine/requirements.txt", "scripts/mock-agent.py"] {
            assert!(
                project_root.join(rel).exists(),
                "bundle.resources 声明的文件缺失: {rel}"
            );
        }
    }
}
