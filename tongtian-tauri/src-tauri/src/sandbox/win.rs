//! Windows AppContainer 实现
//!
//! 只按 Windows 官方文档实现（CreateAppContainerProfile / SECURITY_CAPABILITIES /
//! Job Object / 管道），不逐行翻译 Apache-2.0 的 Python 参考树，避免衍生作品归属。
//!
//! 关键事实：
//! - AppContainer 令牌默认**读不到用户目录**，也不带网络能力；网络能力只能整组授予。
//! - 工作区显式授予读写；数据根里的密钥、会话库、审计导出、意识目录显式拒绝。
//! - 子进程的环境块做了过滤，避免把模型密钥与令牌带进沙箱。

use std::ffi::{c_void, OsStr};
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::FromRawHandle;
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::{CloseHandle, LocalFree, HANDLE};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSidToSidW,
};
use windows_sys::Win32::Security::{
    FreeSid, PSID, SECURITY_ATTRIBUTES, SECURITY_CAPABILITIES, SID_AND_ATTRIBUTES,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
    InitializeProcThreadAttributeList, UpdateProcThreadAttribute, WaitForSingleObject,
    CREATE_NO_WINDOW, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
    PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, STARTUPINFOEXW,
    STARTUPINFOW,
};

use super::cli::RunOptions;

const SE_GROUP_ENABLED: u32 = 0x0000_0004;
const OI_CI_MODIFY: &str = "(OI)(CI)M";
const OI_CI_READ_EXEC: &str = "(OI)(CI)(RX)";
const OI_CI_FULL: &str = "(OI)(CI)(F)";
const FILE_FULL: &str = "(F)";
const STARTF_USESTDHANDLES: u32 = 0x0000_0100;
const CREATE_UNICODE_ENVIRONMENT: u32 = 0x0000_0400;
const ERROR_ALREADY_EXISTS_HR: u32 = 0x8007_00B7;

#[link(name = "userenv")]
extern "system" {
    fn CreateAppContainerProfile(
        pszappcontainername: *const u16,
        pszdisplayname: *const u16,
        pszdescription: *const u16,
        pcapabilities: *const SID_AND_ATTRIBUTES,
        dwcapabilitycount: u32,
        ppsidappcontainersid: *mut PSID,
    ) -> i32;

    fn DeriveAppContainerSidFromAppContainerName(
        pszappcontainername: *const u16,
        ppsidappcontainersid: *mut PSID,
    ) -> i32;
}

struct Profile {
    sid: PSID,
    sid_text: String,
}

impl Drop for Profile {
    fn drop(&mut self) {
        if !self.sid.is_null() {
            unsafe {
                FreeSid(self.sid);
            }
        }
    }
}

/// 启动器主流程；失败时打印中文原因并返回失败码（125 配置失败 / 126 启动失败 / 127 程序不存在）
pub fn run(opts: &RunOptions) -> i32 {
    match run_inner(opts) {
        Ok(code) => code,
        Err((code, e)) => {
            eprintln!("沙箱启动失败：{e}");
            code
        }
    }
}

fn run_inner(opts: &RunOptions) -> Result<i32, (i32, String)> {
    let setup = |e: String| (125, e);
    let profile = ensure_profile(&opts.profile).map_err(setup)?;
    apply_acls(&profile.sid_text, opts).map_err(setup)?;

    let temp = sandbox_temp_dir(&opts.profile).map_err(setup)?;
    std::fs::create_dir_all(&temp)
        .map_err(|e| {
            (
                125,
                format!("SANDBOX_TEMP_FAILED: 无法创建沙箱临时目录 {}（{e}）", temp.display()),
            )
        })?;
    std::env::set_var("TEMP", &temp);
    std::env::set_var("TMP", &temp);
    std::env::set_var("TT_SANDBOXED", "1");

    if let Some(ws) = &opts.workspace {
        if !ws.is_dir() {
            return Err((
                125,
                format!(
                    "SANDBOX_WORKSPACE_MISSING: 工作区目录不存在：{}",
                    ws.display()
                ),
            ));
        }
    }

    let resolved = resolve_program(&opts.program, &opts.args).map_err(|e| (127, e))?;
    launch(&profile, resolved, opts).map_err(|e| (126, e))
}

// ── profile ──────────────────────────────────────────────────────────

fn ensure_profile(name: &str) -> Result<Profile, String> {
    let name_w = wide(name);
    let mut sid: PSID = null_mut();
    let hr = unsafe {
        CreateAppContainerProfile(
            name_w.as_ptr(),
            name_w.as_ptr(),
            name_w.as_ptr(),
            null(),
            0,
            &mut sid,
        )
    };
    if hr < 0 {
        let code = hr as u32;
        if code == ERROR_ALREADY_EXISTS_HR {
            let again = unsafe { DeriveAppContainerSidFromAppContainerName(name_w.as_ptr(), &mut sid) };
            if again < 0 {
                return Err(format!(
                    "SANDBOX_PROFILE_FAILED: AppContainer profile「{name}」已存在但无法取得 SID（HRESULT 0x{:08X}）",
                    again as u32
                ));
            }
        } else {
            return Err(format!(
                "SANDBOX_PROFILE_FAILED: 创建 AppContainer profile「{name}」失败（HRESULT 0x{code:08X}）"
            ));
        }
    }
    if sid.is_null() {
        return Err("SANDBOX_PROFILE_FAILED: 系统返回了空 SID".to_string());
    }
    let sid_text = sid_to_string(sid)?;
    Ok(Profile { sid, sid_text })
}

/// 探测 profile 是否已创建（不创建）；供设置页显示真实状态
pub fn profile_ready(name: &str) -> Result<bool, String> {    let name_w = wide(name);
    let mut sid: PSID = null_mut();
    let hr = unsafe { DeriveAppContainerSidFromAppContainerName(name_w.as_ptr(), &mut sid) };
    if hr < 0 {
        return Ok(false);
    }
    if !sid.is_null() {
        unsafe {
            FreeSid(sid);
        }
    }
    Ok(true)
}

/// 启用系统隔离前预创建 profile：权限不足等问题在设置页就暴露，不拖到第一次开终端
pub fn ensure_profile_ready(name: &str) -> Result<(), String> {
    ensure_profile(name).map(|_| ())
}

fn sid_to_string(sid: PSID) -> Result<String, String> {
    let mut raw: *mut u16 = null_mut();
    let ok = unsafe { ConvertSidToStringSidW(sid, &mut raw) };
    if ok == 0 {
        return Err(format!(
            "SANDBOX_PROFILE_FAILED: SID 转字符串失败（{}）",
            std::io::Error::last_os_error()
        ));
    }
    let text = unsafe { from_wide_ptr(raw) };
    unsafe {
        LocalFree(raw as *mut c_void);
    }
    Ok(text)
}

fn capability_sid(name: &str) -> Result<PSID, String> {
    let text = wide(name);
    let mut sid: PSID = null_mut();
    let ok = unsafe { ConvertStringSidToSidW(text.as_ptr(), &mut sid) };
    if ok == 0 || sid.is_null() {
        return Err(format!(
            "SANDBOX_CAPABILITY_FAILED: 无法解析网络能力 SID「{name}」（{}）",
            std::io::Error::last_os_error()
        ));
    }
    Ok(sid)
}

// ── ACL ──────────────────────────────────────────────────────────────

fn apply_acls(sid: &str, opts: &RunOptions) -> Result<(), String> {
    let temp = sandbox_temp_dir(&opts.profile)?;
    std::fs::create_dir_all(&temp).map_err(|e| {
        format!("SANDBOX_TEMP_FAILED: 无法创建沙箱临时目录 {}（{e}）", temp.display())
    })?;
    let mut cache = GrantCache::load(&opts.profile);

    if let Some(ws) = &opts.workspace {
        // 工作区授权失败等于隔离没有落地，必须拒绝执行
        ensure_acl(sid, ws, OI_CI_MODIFY, false, &mut cache)?;
    }
    ensure_acl(sid, &temp, OI_CI_MODIFY, false, &mut cache)?;

    // 只读目录是「尽量给」：系统目录往往不允许普通用户改 ACL，
    // 且 AppContainer 对 Windows / Program Files 本来就有读权限，跳过可避免无谓的全树遍历。
    for dir in runtime_dirs().into_iter().chain(opts.read_dirs.iter().cloned()) {
        if dir.is_dir() && !is_system_dir(&dir) {
            let _ = ensure_acl(sid, &dir, OI_CI_READ_EXEC, false, &mut cache);
        }
    }

    for (path, spec) in sensitive_paths() {
        if path.exists() {
            // 拒绝必须成功：失败等于密钥与会话库没有保护
            ensure_acl(sid, &path, spec, true, &mut cache)?;
        }
    }
    cache.save(&opts.profile);
    Ok(())
}

/// 授权 / 拒绝记录缓存：icacls 逐文件传播在大目录上要几分钟，
/// 同一 (路径, 权限, 方向) 只做一次；`TT_SANDBOX_NO_CACHE=1` 可强制重做。
/// 缓存文件放在容器不可写的 profile 目录，子进程无法伪造授权。
struct GrantCache {
    lines: std::collections::HashSet<String>,
}

impl GrantCache {
    fn load(profile: &str) -> Self {
        let mut lines = std::collections::HashSet::new();
        if std::env::var("TT_SANDBOX_NO_CACHE").is_err() {
            if let Some(file) = cache_file(profile) {
                if let Ok(text) = std::fs::read_to_string(file) {
                    for line in text.lines().filter(|l| !l.trim().is_empty()) {
                        lines.insert(line.to_string());
                    }
                }
            }
        }
        Self { lines }
    }

    fn key(path: &Path, spec: &str, deny: bool) -> String {
        format!(
            "{}\t{spec}\t{}",
            if deny { "deny" } else { "grant" },
            path.to_string_lossy()
        )
    }

    fn has(&self, path: &Path, spec: &str, deny: bool) -> bool {
        self.lines.contains(&Self::key(path, spec, deny))
    }

    fn save(mut self, profile: &str) {
        let Some(path) = cache_file(profile) else {
            return;
        };
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let mut lines: Vec<String> = self.lines.drain().collect();
        lines.sort_unstable();
        let _ = std::fs::write(&path, lines.join("\n"));
    }
}

fn cache_file(profile: &str) -> Option<PathBuf> {
    sandbox_temp_dir(profile)
        .ok()
        .map(|temp| temp.with_file_name("grants.txt"))
}

/// 系统目录：AppContainer 已可读，且普通用户无改 ACL 的权限，不再逐文件授予
fn is_system_dir(path: &Path) -> bool {
    let text = path.to_string_lossy().replace('/', "\\").to_ascii_lowercase();
    let roots = [
        std::env::var("WINDIR").unwrap_or_else(|_| "C:\\Windows".to_string()),
        std::env::var("ProgramFiles").unwrap_or_else(|_| "C:\\Program Files".to_string()),
        std::env::var("ProgramFiles(x86)")
            .unwrap_or_else(|_| "C:\\Program Files (x86)".to_string()),
        std::env::var("ProgramW6432").unwrap_or_else(|_| "C:\\Program Files".to_string()),
    ];
    roots
        .iter()
        .filter(|r| !r.trim().is_empty())
        .map(|r| r.replace('/', "\\").trim_end_matches('\\').to_ascii_lowercase())
        .any(|r| text == r || text.starts_with(&format!("{r}\\")))
}

fn ensure_acl(
    sid: &str,
    path: &Path,
    spec: &str,
    deny: bool,
    cache: &mut GrantCache,
) -> Result<(), String> {
    if cache.has(path, spec, deny) {
        return Ok(());
    }
    icacls(path, &format!("*{sid}:{spec}"), deny)?;
    cache
        .lines
        .insert(GrantCache::key(path, spec, deny));
    Ok(())
}

/// 需要拒绝访问的敏感项：会话库、配额台账、审计导出、意识目录
fn sensitive_paths() -> Vec<(PathBuf, &'static str)> {
    let root = crate::store::db::runtime_home();
    let mut out: Vec<(PathBuf, &'static str)> = vec![
        (root.join("tongtian.db"), FILE_FULL),
        (root.join("tongtian.db-wal"), FILE_FULL),
        (root.join("tongtian.db-shm"), FILE_FULL),
        (root.join("quota-ledger.json"), FILE_FULL),
        (root.join("exports"), OI_CI_FULL),
    ];
    if let Some(work) = work_home() {
        out.push((work, OI_CI_FULL));
    }
    out
}

/// `TONGTIANWORK_HOME` 优先，缺省 `%USERPROFILE%\.tongtianwork`
fn work_home() -> Option<PathBuf> {
    if let Ok(raw) = std::env::var("TONGTIANWORK_HOME") {
        if !raw.trim().is_empty() {
            return Some(PathBuf::from(raw));
        }
    }
    std::env::var("USERPROFILE")
        .ok()
        .map(|home| PathBuf::from(home).join(".tongtianwork"))
}

fn runtime_dirs() -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    if let Ok(raw) = std::env::var(super::ENV_RUNTIME_DIRS) {
        for item in raw.split(';').filter(|s| !s.trim().is_empty()) {
            out.push(PathBuf::from(item));
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            out.push(parent.to_path_buf());
        }
    }
    out
}

fn sandbox_temp_dir(profile: &str) -> Result<PathBuf, String> {
    let base = std::env::var("LOCALAPPDATA")
        .map_err(|_| "SANDBOX_TEMP_FAILED: 缺少 LOCALAPPDATA 环境变量".to_string())?;
    Ok(PathBuf::from(base)
        .join("TongTianSandbox")
        .join(profile)
        .join("temp"))
}

fn icacls(path: &Path, spec: &str, deny: bool) -> Result<(), String> {
    let mut cmd = std::process::Command::new("icacls");
    cmd.arg(path)
        .arg(if deny { "/deny" } else { "/grant" })
        .arg(spec)
        .arg("/Q")
        .arg("/C")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped());
    crate::win_console::hide_console(&mut cmd);
    let out = cmd.output().map_err(|e| {
        format!(
            "SANDBOX_ACL_FAILED: 无法执行 icacls 设置 {} 的权限（{e}）",
            path.display()
        )
    })?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(format!(
            "SANDBOX_ACL_FAILED: {} 设置失败（{}）",
            if deny { "拒绝访问" } else { "授予访问" },
            stderr.trim()
        ));
    }
    Ok(())
}

// ── 命令解析 ────────────────────────────────────────────────────────

struct Resolved {
    app: PathBuf,
    argv: Vec<String>,
}

/// 解析可执行文件：绝对路径直接用；裸命令名按 PATH + PATHEXT 查找；
/// `.cmd` / `.bat` 交给 `cmd.exe /C`（AppContainer 不能直接执行批处理）
fn resolve_program(program: &str, args: &[String]) -> Result<Resolved, String> {
    let direct = Path::new(program);
    let candidate = if direct.is_absolute() || program.contains('/') || program.contains('\\') {
        if direct.is_file() {
            Some(direct.to_path_buf())
        } else {
            None
        }
    } else {
        find_in_path(program)
    };
    let Some(app) = candidate else {
        return Err(format!(
            "SANDBOX_PROGRAM_NOT_FOUND: 找不到要执行的程序「{program}」，请确认已安装并在 PATH 中"
        ));
    };
    let ext = app
        .extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    if ext == "cmd" || ext == "bat" {
        let comspec = std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".to_string());
        let mut line = quote_arg(&app.to_string_lossy());
        for a in args {
            line.push(' ');
            line.push_str(&quote_arg(a));
        }
        return Ok(Resolved {
            app: PathBuf::from(comspec),
            argv: vec!["/C".to_string(), line],
        });
    }
    Ok(Resolved {
        app,
        argv: args.iter().map(|a| quote_arg(a)).collect(),
    })
}

pub(super) fn find_in_path(program: &str) -> Option<PathBuf> {
    let mut names = vec![program.to_string()];
    if Path::new(program).extension().is_none() {
        let exts = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string());
        for ext in exts.split(';').filter(|s| !s.trim().is_empty()) {
            names.push(format!("{program}{}", ext.to_ascii_lowercase()));
            names.push(format!("{program}{ext}"));
        }
    }
    let path = std::env::var("PATH").unwrap_or_default();
    for dir in path.split(';').filter(|s| !s.trim().is_empty()) {
        for name in &names {
            let p = Path::new(dir).join(name);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

fn quote_arg(raw: &str) -> String {
    if !raw.is_empty() && !raw.contains([' ', '\t', '"']) {
        return raw.to_string();
    }
    let mut out = String::from("\"");
    let mut backslashes = 0usize;
    for ch in raw.chars() {
        match ch {
            '\\' => {
                backslashes += 1;
                out.push('\\');
            }
            '"' => {
                for _ in 0..backslashes {
                    out.push('\\');
                }
                backslashes = 0;
                out.push('\\');
                out.push('"');
            }
            other => {
                backslashes = 0;
                out.push(other);
            }
        }
    }
    for _ in 0..backslashes {
        out.push('\\');
    }
    out.push('"');
    out
}

// ── 进程启动 ────────────────────────────────────────────────────────

fn launch(profile: &Profile, resolved: Resolved, opts: &RunOptions) -> Result<i32, String> {
    let mut cmdline = quote_arg(&resolved.app.to_string_lossy());
    for a in &resolved.argv {
        cmdline.push(' ');
        cmdline.push_str(a);
    }
    let mut cmdline_w = wide(&cmdline);
    let app_w = wide(&resolved.app.to_string_lossy());
    if std::env::var("TT_SANDBOX_DEBUG").is_ok() {
        eprintln!("[tt-sandbox] app={} cmdline={cmdline}", resolved.app.display());
    }

    let caps = build_capabilities(profile.sid, opts.network)?;

    let mut attr_size: usize = 0;
    unsafe {
        InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut attr_size);
    }
    if attr_size == 0 {
        return Err(format!(
            "SANDBOX_LAUNCH_FAILED: 无法计算进程属性表大小（{}）",
            std::io::Error::last_os_error()
        ));
    }
    let mut attr_buf: Vec<usize> = vec![0; (attr_size + std::mem::size_of::<usize>() - 1) / std::mem::size_of::<usize>()];
    let attr_list = attr_buf.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
    let ok = unsafe { InitializeProcThreadAttributeList(attr_list, 1, 0, &mut attr_size) };
    if ok == 0 {
        return Err(format!(
            "SANDBOX_LAUNCH_FAILED: 初始化进程属性表失败（{}）",
            std::io::Error::last_os_error()
        ));
    }
    let _guard = AttrGuard(attr_list);

    let ok = unsafe {
        UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
            &caps as *const SECURITY_CAPABILITIES as *const c_void,
            std::mem::size_of::<SECURITY_CAPABILITIES>(),
            null_mut(),
            null(),
        )
    };
    if ok == 0 {
        return Err(format!(
            "SANDBOX_LAUNCH_FAILED: 写入 AppContainer 安全能力失败（{}）",
            std::io::Error::last_os_error()
        ));
    }

    let sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    let (child_in, parent_in) = pipe(&sa)?;
    let (parent_out, child_out) = pipe(&sa)?;
    let (parent_err, child_err) = pipe(&sa)?;
    // 父进程持有的一端不能被后续子进程继承
    unsafe {
        use windows_sys::Win32::Foundation::{SetHandleInformation, HANDLE_FLAG_INHERIT};
        SetHandleInformation(parent_in, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(parent_out, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(parent_err, HANDLE_FLAG_INHERIT, 0);
    }

    let mut si: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    si.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = child_in;
    si.StartupInfo.hStdOutput = child_out;
    si.StartupInfo.hStdError = child_err;
    si.lpAttributeList = attr_list;

    let env_block = filtered_env_block(&opts.keep_env);
    let cwd_w = opts
        .workspace
        .as_ref()
        .map(|p| wide(&p.to_string_lossy()));

    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        CreateProcessW(
            app_w.as_ptr(),
            cmdline_w.as_mut_ptr(),
            null(),
            null(),
            1,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            env_block.as_ptr() as *const c_void,
            cwd_w.as_ref().map(|w| w.as_ptr()).unwrap_or(null()),
            &si.StartupInfo as *const STARTUPINFOW,
            &mut pi,
        )
    };
    // 父进程不再需要子进程端句柄
    unsafe {
        CloseHandle(child_in);
        CloseHandle(child_out);
        CloseHandle(child_err);
    }
    if ok == 0 {
        let err = std::io::Error::last_os_error();
        unsafe {
            CloseHandle(parent_in);
            CloseHandle(parent_out);
            CloseHandle(parent_err);
        }
        return Err(format!(
            "SANDBOX_LAUNCH_FAILED: 启动「{}」失败（{err}）",
            resolved.app.display()
        ));
    }

    let job = unsafe { CreateJobObjectW(null(), null()) };
    if !job.is_null() {
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let set_ok = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if set_ok != 0 {
            unsafe {
                AssignProcessToJobObject(job, pi.hProcess);
            }
        }
    }

    // 句柄按 usize 传进线程：裸 HANDLE 不实现 Send
    let in_addr = parent_in as usize;
    let out_addr = parent_out as usize;
    let err_addr = parent_err as usize;
    let stdin_thread = std::thread::spawn(move || {
        let mut src = std::io::stdin();
        let mut dst = unsafe { File::from_raw_handle(in_addr as HANDLE) };
        copy_until_eof(&mut src, &mut dst);
    });
    let stdout_thread = std::thread::spawn(move || {
        let mut src = unsafe { File::from_raw_handle(out_addr as HANDLE) };
        let mut dst = std::io::stdout();
        copy_until_eof(&mut src, &mut dst);
    });
    let stderr_thread = std::thread::spawn(move || {
        let mut src = unsafe { File::from_raw_handle(err_addr as HANDLE) };
        let mut dst = std::io::stderr();
        copy_until_eof(&mut src, &mut dst);
    });

    unsafe {
        WaitForSingleObject(pi.hProcess, u32::MAX);
    }
    let mut code: u32 = 1;
    unsafe {
        GetExitCodeProcess(pi.hProcess, &mut code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        if !job.is_null() {
            CloseHandle(job);
        }
    }
    let _ = stdin_thread.join();
    let _ = stdout_thread.join();
    let _ = stderr_thread.join();
    Ok(code as i32)
}

fn copy_until_eof(src: &mut impl Read, dst: &mut impl Write) {
    let mut buf = [0u8; 8192];
    loop {
        match src.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                if dst.write_all(&buf[..n]).is_err() || dst.flush().is_err() {
                    break;
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
}

struct AttrGuard(LPPROC_THREAD_ATTRIBUTE_LIST);
impl Drop for AttrGuard {
    fn drop(&mut self) {
        unsafe { DeleteProcThreadAttributeList(self.0) };
    }
}

fn pipe(sa: &SECURITY_ATTRIBUTES) -> Result<(HANDLE, HANDLE), String> {
    let mut read: HANDLE = null_mut();
    let mut write: HANDLE = null_mut();
    let ok = unsafe { CreatePipe(&mut read, &mut write, sa, 0) };
    if ok == 0 {
        return Err(format!(
            "SANDBOX_PIPE_FAILED: 创建标准输入输出管道失败（{}）",
            std::io::Error::last_os_error()
        ));
    }
    Ok((read, write))
}

/// 构造 SECURITY_CAPABILITIES；关闭网络时能力个数为 0
fn build_capabilities(sid: PSID, network: bool) -> Result<SECURITY_CAPABILITIES, String> {
    let mut caps = SECURITY_CAPABILITIES {
        AppContainerSid: sid,
        Capabilities: null_mut(),
        CapabilityCount: 0,
        Reserved: 0,
    };
    if network {
        let names = ["S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-3"];
        let mut items: Vec<SID_AND_ATTRIBUTES> = Vec::new();
        for name in names {
            items.push(SID_AND_ATTRIBUTES {
                Sid: capability_sid(name)?,
                Attributes: SE_GROUP_ENABLED,
            });
        }
        caps.Capabilities = items.as_mut_ptr();
        caps.CapabilityCount = items.len() as u32;
        // 能力 SID 必须活到 CreateProcessW 之后；泄漏到进程结束可接受（数量固定且极小）
        std::mem::forget(items);
    }
    Ok(caps)
}

/// 过滤环境块：去掉模型密钥、令牌一类变量，避免沙箱内命令直接读走。
/// `keep` 是用户为某个连接器显式配置的变量名——那是授权，不是泄漏。
fn filtered_env_block(keep: &[String]) -> Vec<u16> {
    let keep: Vec<String> = keep.iter().map(|k| k.to_ascii_lowercase()).collect();
    let mut pairs: Vec<(String, String)> = std::env::vars()
        .filter(|(k, _)| !is_secret_key(k) || keep.contains(&k.to_ascii_lowercase()))
        .collect();
    pairs.sort_by(|a, b| a.0.to_ascii_lowercase().cmp(&b.0.to_ascii_lowercase()));
    let mut block: Vec<u16> = Vec::new();
    for (k, v) in pairs {
        for ch in format!("{k}={v}").encode_utf16() {
            block.push(ch);
        }
        block.push(0);
    }
    if block.is_empty() {
        block.push(0);
    }
    block.push(0);
    block
}

fn is_secret_key(key: &str) -> bool {
    let k = key.to_ascii_lowercase();
    ["token", "secret", "password", "api_key", "apikey", "access_key"]
        .iter()
        .any(|needle| k.contains(needle))
}

fn wide(text: &str) -> Vec<u16> {
    OsStr::new(text).encode_wide().chain(std::iter::once(0)).collect()
}

unsafe fn from_wide_ptr(ptr: *const u16) -> String {
    if ptr.is_null() {
        return String::new();
    }
    let mut len = 0usize;
    while *ptr.add(len) != 0 {
        len += 1;
    }
    String::from_utf16_lossy(std::slice::from_raw_parts(ptr, len))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_keys_are_filtered() {
        assert!(is_secret_key("TT_ENGINE_TOKEN"));
        assert!(is_secret_key("DASHSCOPE_API_KEY"));
        assert!(is_secret_key("AWS_SECRET_ACCESS_KEY"));
        assert!(!is_secret_key("PATH"));
        assert!(!is_secret_key("TEMP"));
    }

    #[test]
    fn env_block_is_double_terminated() {
        let block = filtered_env_block(&[]);
        assert!(block.len() >= 2);
        assert_eq!(block[block.len() - 1], 0);
        assert_eq!(block[block.len() - 2], 0);
    }

    #[test]
    fn quoting_matches_msvcrt_rules() {
        assert_eq!(quote_arg("python.exe"), "python.exe");
        assert_eq!(quote_arg("a b"), "\"a b\"");
        assert_eq!(quote_arg("a\"b"), "\"a\\\"b\"");
    }

    #[test]
    fn sensitive_paths_cover_session_db_and_awareness() {
        let items = sensitive_paths();
        let names: Vec<String> = items
            .iter()
            .map(|(p, _)| p.file_name().unwrap_or_default().to_string_lossy().into_owned())
            .collect();
        assert!(names.iter().any(|n| n == "tongtian.db"));
        assert!(names.iter().any(|n| n == "quota-ledger.json"));
    }
}
