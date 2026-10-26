//! 浏览器 AI+ 执行器：受控浏览器会话
//!
//! 与电脑操控同一条原则：配置 ≠ 授权，授权之外还要逐次可控。
//! - 策略三档：每次询问（默认，系统确认框）/ 自动执行 / 禁用；
//! - 用**独立配置目录**启动 Edge/Chrome：不读取、不修改用户日常浏览器的 Cookie、
//!   密码与登录态，窗口可见，用户随时可以接管或关闭；
//! - 启动、打开页面、点击、输入、滚动、读取快照都经本机回环网关放行并写审计；
//! - 引擎只拿到本次会话的页面 WebSocket 地址，拿不到任意浏览器控制能力；
//! - 审计只记域名、动作、元素描述与字符数，不记页面正文、输入正文与截图内容。
//!
//! 数据保护：只使用 `{TONGTIANWORK_HOME|~/.tongtianwork}/browser` 下的专用目录；
//! 停止会话只结束本应用启动的那个进程，不触碰用户自己打开的浏览器。

use std::fs;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::store::{settings, Store};

pub const POLICY_KEY: &str = "browserUsePolicy";
pub const POLICIES: [&str; 3] = ["ask_every_time", "auto", "disabled"];
pub const DEFAULT_POLICY: &str = "ask_every_time";

/// 单条 URL 上限：防止把超长 data/查询串塞进日志与确认框
pub const MAX_URL_CHARS: usize = 4096;
/// 页面启动等待上限（DevToolsActivePort 出现）
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);
const PROBE_TIMEOUT: Duration = Duration::from_millis(350);

static ENSURE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BrowserInstall {
    pub name: String,
    pub executable: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionInfo {
    pub pid: u32,
    pub port: u16,
    pub browser: String,
    pub executable: String,
    pub started_at: i64,
}

// ── 目录 ─────────────────────────────────────────────────────

fn home() -> PathBuf {
    let raw = std::env::var("USERPROFILE")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("HOME").ok().filter(|s| !s.trim().is_empty()))
        .unwrap_or_else(|| ".".to_string());
    PathBuf::from(raw)
}

/// 浏览器执行器的根目录；与知识库同用 TONGTIANWORK_HOME 覆盖
pub fn root() -> PathBuf {
    match std::env::var("TONGTIANWORK_HOME") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v).join("browser"),
        _ => home().join(".tongtianwork").join("browser"),
    }
}

pub fn profile_dir() -> PathBuf {
    root().join("profile")
}

fn session_file() -> PathBuf {
    root().join("session.json")
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

// ── 浏览器发现 ───────────────────────────────────────────────

fn candidate_paths() -> Vec<(String, PathBuf)> {
    let mut out: Vec<(String, PathBuf)> = Vec::new();
    let mut push = |name: &str, path: PathBuf| out.push((name.to_string(), path));

    #[cfg(windows)]
    {
        for env in ["ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"] {
            let Some(base) = std::env::var_os(env) else {
                continue;
            };
            let base = PathBuf::from(base);
            push("Microsoft Edge", base.join("Microsoft/Edge/Application/msedge.exe"));
            push("Google Chrome", base.join("Google/Chrome/Application/chrome.exe"));
        }
    }
    #[cfg(target_os = "macos")]
    {
        push(
            "Microsoft Edge",
            PathBuf::from("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        );
        push(
            "Google Chrome",
            PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        );
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        for dir in ["/usr/bin", "/usr/local/bin", "/snap/bin", "/opt/google/chrome"] {
            let d = PathBuf::from(dir);
            for bin in ["microsoft-edge", "microsoft-edge-stable", "google-chrome", "chromium", "chromium-browser"] {
                let name = if bin.contains("edge") {
                    "Microsoft Edge"
                } else {
                    "Google Chrome / Chromium"
                };
                push(name, d.join(bin));
            }
        }
    }
    out
}

fn path_binaries() -> Vec<(String, PathBuf)> {
    let mut out = Vec::new();
    let Some(path) = std::env::var_os("PATH") else {
        return out;
    };
    let names: [(&str, &str); 5] = [
        ("Microsoft Edge", "msedge"),
        ("Google Chrome", "chrome"),
        ("Google Chrome / Chromium", "google-chrome"),
        ("Chromium", "chromium"),
        ("Chromium", "chromium-browser"),
    ];
    for dir in std::env::split_paths(&path) {
        for (name, bin) in names {
            #[cfg(windows)]
            let file = dir.join(format!("{bin}.exe"));
            #[cfg(not(windows))]
            let file = dir.join(bin);
            if file.is_file() {
                out.push((name.to_string(), file));
            }
        }
    }
    out
}

/// 找本机可用的 Edge/Chrome。找不到时返回 None，由页面/引擎如实说明。
pub fn discover() -> Option<BrowserInstall> {
    candidate_paths()
        .into_iter()
        .chain(path_binaries())
        .find(|(_, p)| p.is_file())
        .map(|(name, path)| BrowserInstall {
            name,
            executable: path.to_string_lossy().to_string(),
        })
}

pub fn platform() -> &'static str {
    std::env::consts::OS
}

pub fn supported() -> bool {
    discover().is_some()
}

pub fn features() -> Value {
    json!({
        "snapshot": true,
        "click": true,
        "type": true,
        "scroll": true,
        "screenshot": true,
        "navigation": true,
    })
}

// ── 策略 ─────────────────────────────────────────────────────

/// 策略归一：未保存过用默认档；已保存但值非法时 fail-closed 返回错误，
/// 由调用方如实说明「这轮浏览器 AI+ 不可用」，而不是猜一个档位。
pub fn read_policy(store: &Store) -> Result<(String, bool), String> {
    let stored = settings::get(store, POLICY_KEY)?;
    match stored {
        None => Ok((DEFAULT_POLICY.to_string(), false)),
        Some(raw) => {
            let v = raw.trim().trim_matches('"');
            if POLICIES.contains(&v) {
                Ok((v.to_string(), true))
            } else {
                Err(format!(
                    "BROWSER_USE_INVALID_POLICY: 浏览器 AI+ 档位非法「{v}」，仅支持 {}",
                    POLICIES.join(" / ")
                ))
            }
        }
    }
}

pub fn normalize_policy(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"');
    if POLICIES.contains(&v) {
        Ok(v.to_string())
    } else {
        Err(format!(
            "BROWSER_USE_INVALID_POLICY: 浏览器 AI+ 档位非法「{v}」，仅支持 {}",
            POLICIES.join(" / ")
        ))
    }
}

// ── URL 校验与描述 ──────────────────────────────────────────

/// 受控浏览器只允许打开 http/https：file:// 会读本机文件，javascript:/data: 能执行脚本，
/// chrome:// 与 devtools:// 能接触浏览器内部能力，一律在入口拒绝。
pub fn normalize_url(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() {
        return Err("BROWSER_USE_BLOCKED_SCHEME: 打开页面需要提供 URL".into());
    }
    if v.chars().count() > MAX_URL_CHARS {
        return Err(format!(
            "BROWSER_USE_INVALID_ACTION: URL 超过 {MAX_URL_CHARS} 个字符"
        ));
    }
    if v.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("BROWSER_USE_INVALID_ACTION: URL 不能包含空白或控制字符".into());
    }
    let lower = v.to_ascii_lowercase();
    let rest = if let Some(r) = lower.strip_prefix("https://") {
        &v[v.len() - r.len()..]
    } else if let Some(r) = lower.strip_prefix("http://") {
        &v[v.len() - r.len()..]
    } else {
        return Err(
            "BROWSER_USE_BLOCKED_SCHEME: 受控浏览器只允许打开 http/https 页面\
             （已拒绝 file / javascript / data / chrome 等协议）"
                .into(),
        );
    };
    let host = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("")
        .rsplit('@')
        .next()
        .unwrap_or("");
    if host.is_empty() {
        return Err("BROWSER_USE_INVALID_ACTION: URL 缺少主机名".into());
    }
    Ok(v.to_string())
}

/// 审计里的 URL：去掉查询串与片段（可能含令牌、搜索词等敏感内容），只留协议/主机/路径。
pub fn audit_url(raw: &str) -> String {
    let v = raw.trim();
    let cut = v.find(['?', '#']).unwrap_or(v.len());
    let mut out = v[..cut].to_string();
    if out.chars().count() > 200 {
        out = out.chars().take(200).collect::<String>() + "…";
    }
    out
}

/// 确认框/审计里的主机（带端口），解析失败时退回脱敏 URL。
pub fn host_of(raw: &str) -> String {
    let v = raw.trim();
    let Some((scheme, rest)) = v.split_once("://") else {
        return audit_url(v);
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = authority.rsplit('@').next().unwrap_or(authority);
    if host.is_empty() {
        format!("{scheme}://")
    } else {
        host.to_string()
    }
}

fn brief(text: &str, limit: usize) -> String {
    let one = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if one.chars().count() <= limit {
        one
    } else {
        one.chars().take(limit).collect::<String>() + "…"
    }
}

/// 给用户看的动作描述。输入正文不进确认框/审计，只报字符数。
pub fn describe_action(action: &str, target: &str, detail: &str) -> String {
    let host = host_of(target);
    let host = if host.is_empty() { "页面".to_string() } else { host };
    let detail = brief(detail, 180);
    match action {
        "open" => format!("在受控浏览器中打开 {}", brief(&audit_url(target), 240)),
        "snapshot" => format!("读取当前网页的可见内容（{host}）"),
        "click" => {
            if detail.is_empty() {
                format!("点击网页元素（{host}）")
            } else {
                format!("点击「{detail}」（{host}）")
            }
        }
        "type" => {
            if detail.is_empty() {
                format!("在网页中输入文本（{host}）")
            } else {
                format!("在网页中输入：{detail}（{host}）")
            }
        }
        "select" => {
            if detail.is_empty() {
                format!("选择网页下拉选项（{host}）")
            } else {
                format!("选择下拉选项：{detail}（{host}）")
            }
        }
        "scroll" => {
            if detail.is_empty() {
                format!("滚动网页（{host}）")
            } else {
                format!("滚动网页：{detail}（{host}）")
            }
        }
        "screenshot" => format!("截取当前网页画面（{host}）"),
        other => format!("执行网页动作 {other}（{host}）"),
    }
}

pub fn is_known_action(action: &str) -> bool {
    matches!(
        action,
        "open" | "snapshot" | "click" | "type" | "select" | "scroll" | "screenshot"
    )
}

// ── 确认框 ───────────────────────────────────────────────────

/// 按当前档位决定是否放行；`ask_every_time` 弹系统确认框，用户取消即拒绝。
pub fn gate(policy: &str, description: &str) -> Result<(), String> {
    match policy {
        "auto" => Ok(()),
        "ask_every_time" => confirm_dialog(description),
        _ => Err("BROWSER_USE_DISABLED: 浏览器 AI+ 已禁用".into()),
    }
}

#[cfg(windows)]
fn confirm_dialog(description: &str) -> Result<(), String> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONWARNING, MB_OKCANCEL, MB_SETFOREGROUND, MB_TOPMOST,
    };

    let text = format!(
        "通天办公请求使用受控浏览器：\n\n{description}\n\n允许这一次操作吗？\n\
         （可在「浏览器 AI+」页改为自动执行或禁用；受控浏览器使用独立配置目录，不碰日常浏览器数据）"
    );
    let text = wide(&text);
    let caption = wide("通天办公 · 浏览器 AI+");
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
        Err("BROWSER_USE_DENIED_BY_USER: 用户拒绝了本次浏览器操作".into())
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
fn confirm_dialog(description: &str) -> Result<(), String> {
    let escaped = description.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!(
        "display dialog \"通天办公请求使用受控浏览器：\\n\\n{escaped}\\n\\n允许这一次操作吗？\" \
         buttons {{\"拒绝\", \"允许\"}} default button \"允许\" with icon caution"
    );
    let out = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .map_err(|e| format!("BROWSER_USE_CONFIRM_UNAVAILABLE: 无法拉起系统确认框：{e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    if out.status.success() && stdout.contains("允许") {
        Ok(())
    } else if out.status.success() {
        Err("BROWSER_USE_DENIED_BY_USER: 用户拒绝了本次浏览器操作".into())
    } else {
        Err(format!(
            "BROWSER_USE_CONFIRM_UNAVAILABLE: 系统确认框未完成：{}",
            brief(&String::from_utf8_lossy(&out.stderr), 200)
        ))
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
fn confirm_dialog(description: &str) -> Result<(), String> {
    let text = format!(
        "通天办公请求使用受控浏览器：\n\n{description}\n\n允许这一次操作吗？"
    );
    if let Ok(status) = Command::new("zenity")
        .args([
            "--question",
            "--title=通天办公 · 浏览器 AI+",
            "--ok-label=允许",
            "--cancel-label=拒绝",
            &format!("--text={text}"),
        ])
        .status()
    {
        return if status.success() {
            Ok(())
        } else {
            Err("BROWSER_USE_DENIED_BY_USER: 用户拒绝了本次浏览器操作".into())
        };
    }
    if let Ok(status) = Command::new("kdialog")
        .args(["--title", "通天办公 · 浏览器 AI+", "--yesno", &text])
        .status()
    {
        return if status.success() {
            Ok(())
        } else {
            Err("BROWSER_USE_DENIED_BY_USER: 用户拒绝了本次浏览器操作".into())
        };
    }
    Err(
        "BROWSER_USE_CONFIRM_UNAVAILABLE: 当前桌面没有可用的确认框（zenity/kdialog 均未安装），\
         请把档位改为「自动执行」或「禁用」"
            .into(),
    )
}

// ── 会话 ─────────────────────────────────────────────────────

pub fn parse_devtools_port(text: &str) -> Option<u16> {
    text.lines()
        .next()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .and_then(|s| s.parse::<u16>().ok())
        .filter(|p| *p > 0)
}

fn probe_port(port: u16) -> bool {
    if port == 0 {
        return false;
    }
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, PROBE_TIMEOUT).is_ok()
}

fn read_session() -> Option<SessionInfo> {
    let raw = fs::read_to_string(session_file()).ok()?;
    serde_json::from_str::<SessionInfo>(&raw).ok()
}

/// 当前真实在跑的受控会话：session.json 只是线索，端口能连上才算数。
pub fn current_session() -> Option<SessionInfo> {
    let info = read_session()?;
    if probe_port(info.port) {
        Some(info)
    } else {
        None
    }
}

pub fn running() -> bool {
    current_session().is_some()
}

fn write_session(info: &SessionInfo) -> Result<(), String> {
    let dir = root();
    fs::create_dir_all(&dir).map_err(|e| format!("BROWSER_USE_SESSION_FAILED: 创建浏览器目录失败：{e}"))?;
    let raw = serde_json::to_string_pretty(info)
        .map_err(|e| format!("BROWSER_USE_SESSION_FAILED: 会话信息序列化失败：{e}"))?;
    fs::write(session_file(), raw)
        .map_err(|e| format!("BROWSER_USE_SESSION_FAILED: 会话信息写入失败：{e}"))
}

/// 启动受控浏览器（已在运行则直接复用）。返回 (会话, 是否本次新启动)。
///
/// 启动参数说明：
/// - `--remote-debugging-port=0`：让浏览器自己挑一个空闲端口并写入 DevToolsActivePort，
///   避免固定端口被占用或与其他调试会话串线；
/// - `--remote-allow-origins=*`：允许本机引擎的 WebSocket 握手；
/// - `--no-first-run` / `--no-default-browser-check`：不弹首次运行向导、不改系统默认浏览器；
/// - 独立 `--user-data-dir`：与用户日常浏览器数据完全隔离。
pub fn ensure() -> Result<(SessionInfo, bool), String> {
    let _guard = ENSURE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(info) = current_session() {
        return Ok((info, false));
    }
    let install = discover().ok_or_else(|| {
        "BROWSER_USE_NOT_FOUND: 本机没有找到 Microsoft Edge 或 Google Chrome，无法启动受控浏览器".to_string()
    })?;
    let profile = profile_dir();
    fs::create_dir_all(&profile)
        .map_err(|e| format!("BROWSER_USE_SESSION_FAILED: 创建浏览器配置目录失败：{e}"))?;
    let devtools = profile.join("DevToolsActivePort");
    let _ = fs::remove_file(&devtools);

    let mut cmd = Command::new(&install.executable);
    cmd.arg(format!("--user-data-dir={}", profile.display()))
        .arg("--remote-debugging-port=0")
        .arg("--remote-allow-origins=*")
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-sync")
        .arg("--disable-component-update")
        .arg("--disable-background-networking")
        .arg("about:blank")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    let mut child = cmd.spawn().map_err(|e| {
        format!(
            "BROWSER_USE_LAUNCH_FAILED: 启动 {} 失败：{e}",
            install.name
        )
    })?;
    let pid = child.id();
    let started = Instant::now();
    loop {
        if let Ok(text) = fs::read_to_string(&devtools) {
            if let Some(port) = parse_devtools_port(&text) {
                let info = SessionInfo {
                    pid,
                    port,
                    browser: install.name.clone(),
                    executable: install.executable.clone(),
                    started_at: now_ms(),
                };
                write_session(&info)?;
                return Ok((info, true));
            }
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "BROWSER_USE_LAUNCH_FAILED: {} 启动后立即退出（{status}）",
                install.name
            ));
        }
        if started.elapsed() >= STARTUP_TIMEOUT {
            let _ = child.kill();
            return Err(format!(
                "BROWSER_USE_LAUNCH_TIMEOUT: {} 在 {} 秒内没有打开调试端口",
                install.name,
                STARTUP_TIMEOUT.as_secs()
            ));
        }
        std::thread::sleep(Duration::from_millis(120));
    }
}

/// 结束本应用启动的受控浏览器。用户自己打开的浏览器不在 session.json 里，不会被波及。
pub fn stop() -> Result<bool, String> {
    let Some(info) = read_session() else {
        return Ok(false);
    };
    let was_running = probe_port(info.port);
    if was_running {
        kill_pid(info.pid)?;
        // 给进程一点退出时间，避免下一轮 ensure 立刻又连上旧端口
        let started = Instant::now();
        while started.elapsed() < Duration::from_secs(5) && probe_port(info.port) {
            std::thread::sleep(Duration::from_millis(120));
        }
    }
    let _ = fs::remove_file(session_file());
    let _ = fs::remove_file(profile_dir().join("DevToolsActivePort"));
    Ok(was_running)
}

#[cfg(windows)]
fn kill_pid(pid: u32) -> Result<(), String> {
    if pid == 0 {
        return Ok(());
    }
    let status = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| format!("BROWSER_USE_STOP_FAILED: 结束受控浏览器失败：{e}"))?;
    if status.success() {
        Ok(())
    } else {
        // 进程可能已自行退出：端口探测会给出最终事实，这里不把「已经没了」当失败
        Ok(())
    }
}

#[cfg(not(windows))]
fn kill_pid(pid: u32) -> Result<(), String> {
    if pid == 0 {
        return Ok(());
    }
    Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| format!("BROWSER_USE_STOP_FAILED: 结束受控浏览器失败：{e}"))?;
    Ok(())
}

// ── Tauri commands ──────────────────────────────────────────

#[tauri::command]
pub async fn browser_use_get_policy(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let (policy, persisted) = read_policy(&p0.store)?;
    Ok(json!({ "policy": policy, "persisted": persisted }))
}

#[tauri::command]
pub async fn browser_use_set_policy(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
    policy: String,
) -> Result<Value, String> {
    let v = normalize_policy(&policy)?;
    settings::set(&p0.store, POLICY_KEY, &v)?;
    crate::security::audit::record_quiet(
        &p0.store,
        crate::security::audit::ACTION_CONFIG_CHANGE,
        crate::security::audit::SEV_INFO,
        POLICY_KEY,
        &format!("浏览器 AI+ 策略更新：{v}"),
    );
    Ok(json!({ "ok": true, "policy": v }))
}

/// 设置页/浏览器页的真实能力视图：浏览器、平台、档位、网关与会话状态一次说清。
#[tauri::command]
pub async fn browser_use_capabilities(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let install = discover();
    let supported = install.is_some();
    let (policy, persisted, policy_error) = match read_policy(&p0.store) {
        Ok((p, saved)) => (p, saved, None),
        Err(e) => ("disabled".to_string(), true, Some(e)),
    };
    let gate = crate::bridge::browser_gate::endpoint().is_some();
    let session = current_session();
    let mut reason = String::new();
    if let Some(e) = &policy_error {
        reason = format!("策略读取失败，浏览器 AI+ 按禁用处理：{e}");
    } else if !supported {
        reason = "本机没有找到 Microsoft Edge 或 Google Chrome，未安装浏览器时无法使用".into();
    } else if policy == "disabled" {
        reason = "浏览器 AI+ 已禁用".into();
    } else if !gate {
        reason = "本机浏览器网关未启动，执行器不可用".into();
    }
    Ok(json!({
        "supported": supported,
        "platform": platform(),
        "browser": install.as_ref().map(|b| b.name.clone()),
        "executable": install.as_ref().map(|b| b.executable.clone()),
        "features": features(),
        "policy": policy,
        "persisted": persisted,
        "gate": gate,
        "running": session.is_some(),
        "port": session.as_ref().map(|s| s.port),
        "profileDir": profile_dir().to_string_lossy(),
        "reason": reason,
        "policyError": policy_error,
    }))
}

/// 用户主动启动受控浏览器（不弹逐次确认；档位为禁用时拒绝）。
#[tauri::command]
pub async fn browser_use_start(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let (policy, _) = read_policy(&p0.store)?;
    if policy == "disabled" {
        return Err("BROWSER_USE_DISABLED: 浏览器 AI+ 已禁用，请先切换档位".into());
    }
    let (info, launched) = tauri::async_runtime::spawn_blocking(ensure)
        .await
        .map_err(|e| format!("BROWSER_USE_LAUNCH_FAILED: 启动线程异常：{e}"))??;
    if launched {
        crate::security::audit::record_quiet(
            &p0.store,
            crate::security::audit::ACTION_BROWSER_SESSION,
            crate::security::audit::SEV_INFO,
            &info.browser,
            &format!(
                "启动受控浏览器（端口 {}，独立配置目录 {}）",
                info.port,
                profile_dir().display()
            ),
        );
    }
    Ok(json!({
        "ok": true,
        "launched": launched,
        "pid": info.pid,
        "port": info.port,
        "browser": info.browser,
        "profileDir": profile_dir().to_string_lossy(),
    }))
}

/// 用户主动结束受控浏览器；只结束本应用启动的那一个进程。
#[tauri::command]
pub async fn browser_use_stop(
    p0: tauri::State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    let stopped = tauri::async_runtime::spawn_blocking(stop)
        .await
        .map_err(|e| format!("BROWSER_USE_STOP_FAILED: 停止线程异常：{e}"))??;
    if stopped {
        crate::security::audit::record_quiet(
            &p0.store,
            crate::security::audit::ACTION_BROWSER_SESSION,
            crate::security::audit::SEV_INFO,
            "受控浏览器",
            "用户结束受控浏览器会话",
        );
    }
    Ok(json!({ "ok": true, "stopped": stopped }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy_is_enumerated_and_trimmed() {
        assert_eq!(normalize_policy(" auto ").unwrap(), "auto");
        assert_eq!(normalize_policy("\"disabled\"").unwrap(), "disabled");
        assert!(normalize_policy("always")
            .unwrap_err()
            .contains("BROWSER_USE_INVALID_POLICY"));
    }

    #[test]
    fn devtools_port_parses_first_line_only() {
        assert_eq!(parse_devtools_port("54321\n/devtools/browser/abc\n"), Some(54321));
        assert_eq!(parse_devtools_port("\n54321\n"), None);
        assert_eq!(parse_devtools_port("0\n/devtools/browser/abc\n"), None);
        assert_eq!(parse_devtools_port("not-a-port\n"), None);
        assert_eq!(parse_devtools_port(""), None);
    }

    #[test]
    fn only_http_and_https_are_allowed() {
        assert_eq!(normalize_url(" https://example.com/a?b=1 ").unwrap(), "https://example.com/a?b=1");
        assert!(normalize_url("http://127.0.0.1:8000/").is_ok());
        for blocked in [
            "file:///C:/Users/a/secrets.txt",
            "javascript:alert(1)",
            "data:text/html,hello",
            "chrome://settings",
            "devtools://devtools/bundled/inspector.html",
            "about:blank",
        ] {
            let err = normalize_url(blocked).unwrap_err();
            assert!(
                err.starts_with("BROWSER_USE_BLOCKED_SCHEME"),
                "{blocked} → {err}"
            );
        }
        assert!(normalize_url("https://").unwrap_err().contains("缺少主机名"));
        assert!(normalize_url("https://example.com/a b").is_err());
    }

    #[test]
    fn audit_url_strips_query_and_fragment() {
        assert_eq!(
            audit_url("https://example.com/path?token=secret#frag"),
            "https://example.com/path"
        );
        assert_eq!(host_of("https://user:pw@example.com:8443/a?b"), "example.com:8443");
        assert_eq!(host_of("not a url"), "not a url");
    }

    #[test]
    fn action_descriptions_never_include_typed_text() {
        let click = describe_action("click", "https://shop.example.com/item?q=1", "按钮「立即购买」");
        assert!(click.contains("shop.example.com"), "{click}");
        assert!(click.contains("立即购买"), "{click}");
        assert!(!click.contains("?q=1"), "确认框不展示查询串：{click}");

        let typed = describe_action("type", "https://mail.example.com", "输入 12 个字符");
        assert!(typed.contains("12 个字符"), "{typed}");
        assert!(!typed.contains("password"), "{typed}");

        let open = describe_action("open", "https://example.com/a?token=abc", "");
        assert!(!open.contains("token=abc"), "{open}");
    }

    #[test]
    fn known_actions_cover_the_engine_tool_surface() {
        for a in ["open", "snapshot", "click", "type", "select", "scroll", "screenshot"] {
            assert!(is_known_action(a), "{a}");
        }
        assert!(!is_known_action("eval"));
        assert!(!is_known_action(""));
    }

    #[test]
    fn profile_lives_under_dedicated_browser_root() {
        let root = root();
        assert!(root.ends_with("browser"), "{}", root.display());
        assert_eq!(profile_dir(), root.join("profile"));
        assert!(session_file().starts_with(&root));
    }
}
