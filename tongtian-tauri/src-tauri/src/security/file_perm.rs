//! 文件访问档位（会话内选择；设置项 `filePermissionDefault` 只提供缺省）
//!
//! 三条轴要分清，别把它们混成一个开关：
//! - `security::tool_approval`（off/sensitive/all）管「要不要逐个工具问」；
//! - 本模块（ask_every_time/auto_approve/full_access）管「能写到哪、能不能删」；
//! - `security::policy` 的出域开关管「能不能出网」。
//!
//! 档位语义（与用户在对话输入框左下角看到的三档一一对应）：
//! - `ask_every_time` 每次询问：目录内的每次创建/更新/删除都先弹系统确认框，越出目录仍拒绝；
//! - `auto_approve`   自动审批：只在会话绑定的工作目录（及工作区文件白名单）内直接放行，越界拒绝；
//! - `full_access`    完全访问：可越出绑定目录，且只有这一档允许删除；家目录/系统关键目录/应用数据目录仍硬拒。
//!
//! 与引擎侧 `engine/sandbox_runtime.py` 同一套判定与同一批错误码
//! （delete_requires_full_access / hard_deny / outside_boundary）：两条执行面
//! （Python 引擎、本地 harness）必须给出同样的拒绝理由，否则换一条路就换个边界。
//!
//! 落点顺序也钉死：先看是否落在允许区，再看删除是否需要完全访问，
//! 再看硬拒清单，最后才是档位边界——硬拒永远排在「用户选了哪档」之后判断不了的事情上，
//! 任何档位都越不过去。

use std::path::{Path, PathBuf};

use crate::store::{settings, Store};

/// 运行时下发键（引擎与 harness 都读这一个键）
pub const RUNTIME_KEY: &str = "filePermission";
/// 全局默认档位的设置键
pub const SETTINGS_KEY: &str = "filePermissionDefault";

pub const MODES: [&str; 3] = ["ask_every_time", "auto_approve", "full_access"];
/// 最保守档：没表态、存量脏值、读不到设置时都回到这里
pub const DEFAULT_MODE: &str = "ask_every_time";

pub const LABELS: [(&str, &str); 3] = [
    ("ask_every_time", "每次询问"),
    ("auto_approve", "自动审批"),
    ("full_access", "完全访问"),
];

/// 档位中文名；调用方都在 `format!` 里用，未识别的历史值给一个静态兜底，
/// 避免把外部字符串的原生命周期拖进返回值。
pub fn label(mode: &str) -> &'static str {
    LABELS
        .iter()
        .find(|(id, _)| *id == mode)
        .map(|(_, name)| *name)
        .unwrap_or("未识别档位")
}

/// 严格校验：渲染层显式提交的档位必须是三档之一，坏值直接拒绝提交
pub fn normalize(raw: &str) -> Result<String, String> {
    let v = raw.trim().trim_matches('"').to_ascii_lowercase();
    if MODES.contains(&v.as_str()) {
        Ok(v)
    } else {
        Err(format!(
            "FILE_PERMISSION_INVALID: 文件访问档位非法「{v}」，仅支持 {}",
            MODES.join(" / ")
        ))
    }
}

/// 宽松归一：设置里的全局默认与检查点里的历史值读不出来时回到最保守档，
/// 但不能因为「缺省」就落到更宽的档——非法值一律按「每次询问」处理。
pub fn normalize_or_default(raw: Option<&str>) -> String {
    match raw {
        Some(v) if MODES.contains(&v.trim().trim_matches('"').to_ascii_lowercase().as_str()) => {
            v.trim().trim_matches('"').to_ascii_lowercase()
        }
        _ => DEFAULT_MODE.to_string(),
    }
}

/// 全局默认档位（带引号的 JSON 文本同样归一）。
/// 返回值第二项是给界面看的说明：坏值不阻断提交，但要说清「已按最保守档执行」。
pub fn read_default(store: &Store) -> (String, Option<String>) {
    match settings::get(store, SETTINGS_KEY) {
        Ok(Some(raw)) => {
            let v = raw.trim().trim_matches('"').to_ascii_lowercase();
            if MODES.contains(&v.as_str()) {
                (v, None)
            } else {
                (
                    DEFAULT_MODE.to_string(),
                    Some(format!(
                        "FILE_PERMISSION_INVALID: 设置里的默认档位「{v}」无法识别，本轮按「每次询问」执行"
                    )),
                )
            }
        }
        // 读不出来（含 store 层错误）时同样是保守档：门禁不能因为一次读失败就放开
        _ => (DEFAULT_MODE.to_string(), None),
    }
}

/// 只有「完全访问」允许删除：删除不可逆，另外两档再宽松也不给这个能力
pub fn allows_delete(mode: &str) -> bool {
    mode == "full_access"
}

/// 只有「完全访问」允许越出绑定工作目录（自动审批与每次询问都锁在目录内）
pub fn allows_outside(mode: &str) -> bool {
    mode == "full_access"
}

/// 目录内的写删要不要先问一次：只有「每次询问」这一档问
pub fn asks_in_area(mode: &str) -> bool {
    mode == "ask_every_time"
}

// ── 硬拒清单 ──────────────────────────────────────────────────
//
// 与引擎侧 `_deny_roots()` 同源：家目录、应用数据目录、系统关键目录。
// 这一层与档位无关，是完全访问也放不开的部分。

/// 比对用的路径键。
///
/// Windows 的 `canonicalize()` 会给路径加 `\\?\` 前缀（UNC 是 `\\?\UNC\server\share`），
/// 而 `\\?\` 开头的路径不再做前缀匹配——不剥掉它，`\\?\C:\Windows\System32\x`
/// 就能躲过「c:/windows」这条硬拒项。所以先剥前缀、再统一分隔符与大小写。
fn key(text: &str) -> String {
    let raw = text.trim();
    // 剥掉 Windows 命名空间前缀：canonicalize 的 `\\?\` 与设备路径的 `\\.\`。
    // UNC 写作 `\\?\UNC\server\share`，剥完还原成 `\\server\share`。
    let body = if let Some(rest) = raw.strip_prefix(r"\\?\") {
        match rest.strip_prefix("UNC\\") {
            Some(unc) => format!(r"\\{unc}"),
            None => rest.to_string(),
        }
    } else if let Some(rest) = raw.strip_prefix(r"\\.\") {
        rest.to_string()
    } else {
        raw.to_string()
    };
    body.replace('\\', "/").trim_end_matches('/').to_ascii_lowercase()
}

fn home_dir() -> PathBuf {
    std::env::var("USERPROFILE")
        .ok()
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| PathBuf::from(dirs_home().unwrap_or(String::from("."))))
}

fn dirs_home() -> Option<String> {
    std::env::var("HOME").ok().filter(|s| !s.trim().is_empty())
}

/// 应用数据根：`TONGTIANWORK_HOME` 优先，缺省 `~/.tongtianwork`（与各模块同一口径）
pub fn data_root() -> PathBuf {
    match std::env::var("TONGTIANWORK_HOME") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v),
        _ => home_dir().join(".tongtianwork"),
    }
}

/// 回收目录：删除前先把文件复制到这里，与引擎侧 `file-trash` 同名同处
pub fn trash_dir() -> PathBuf {
    data_root().join("file-trash")
}

/// 硬拒根清单（已归一为可比对小写串）
fn deny_roots() -> Vec<String> {
    let mut roots = vec![key(&home_dir().display().to_string()), key(&data_root().display().to_string())];
    if cfg!(windows) {
        for drive in ["C:", "D:"] {
            for tail in [
                "Windows",
                "Program Files",
                "Program Files (x86)",
                "ProgramData",
                "$Recycle.Bin",
                "System Volume Information",
            ] {
                roots.push(key(&format!("{drive}\\{tail}")));
            }
        }
    } else {
        for p in [
            "/System", "/usr", "/etc", "/bin", "/sbin", "/lib", "/boot", "/dev", "/proc", "/sys",
            "/var", "/root", "/private/var/root",
        ] {
            roots.push(key(p));
        }
    }
    roots.retain(|r| !r.is_empty() && r != "/" && r.chars().any(|c| c == '/'));
    roots
}

/// 命中硬拒清单时返回命中的那条根，供拒绝理由写清「被谁挡住」
pub fn hard_deny_hit(path: &Path) -> Option<String> {
    let target = key(&path.display().to_string());
    if target.is_empty() {
        return None;
    }
    deny_roots()
        .into_iter()
        .find(|root| target == *root || target.starts_with(&format!("{root}/")))
}

/// 落在允许区内：绑定工作目录之内（工作区文件白名单由引擎侧的沙箱规则继续把关，
/// harness 侧没有白名单概念，只有工作目录）
pub fn in_work_dir(path: &Path, work_dir: Option<&Path>) -> bool {
    let Some(base) = work_dir else {
        return false;
    };
    let target = key(&path.display().to_string());
    let base = key(&base.display().to_string());
    !base.is_empty() && (target == base || target.starts_with(&format!("{base}/")))
}

/// 一次写/删的放行判定。返回 `None` 即允许，`Some((错误码尾缀, 给用户看的理由))` 即拒绝。
///
/// 判定顺序与引擎侧 `SandboxPolicy.permission_denial` 一致：
/// 目录内 → 只有删除还需要完全访问；目录外 → 删除、硬拒、边界依次拦。
pub fn denial(
    mode: &str,
    target: &Path,
    work_dir: Option<&Path>,
    write: bool,
    delete: bool,
) -> Option<(String, String)> {
    if !write && !delete {
        return None;
    }
    let name = label(mode);
    let action = if delete { "删除" } else { "写入" };
    let shown = target.display().to_string();
    let inside = in_work_dir(target, work_dir);
    let delete_needs_full = delete && !allows_delete(mode);
    if inside {
        if delete_needs_full {
            return Some((
                "delete_requires_full_access".to_string(),
                format!("当前文件访问档位是「{name}」，删除文件需要切到「完全访问」；文件未被删除：{shown}"),
            ));
        }
        return None;
    }
    if delete_needs_full {
        return Some((
            "delete_requires_full_access".to_string(),
            format!("当前文件访问档位是「{name}」，删除文件需要切到「完全访问」；文件未被删除：{shown}"),
        ));
    }
    if let Some(hit) = hard_deny_hit(target) {
        return Some((
            "hard_deny".to_string(),
            format!("路径落进硬拒清单（{hit}），任何档位都不允许{action}；被拒路径：{shown}"),
        ));
    }
    if !allows_outside(mode) {
        let boundary = work_dir
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "（未绑定工作目录）".to_string());
        return Some((
            "outside_boundary".to_string(),
            format!(
                "当前文件访问档位是「{name}」，只允许{action}绑定工作目录（{boundary}）内的文件；\
                 要越出目录请在对话输入框切到「完全访问」。被拒路径：{shown}"
            ),
        ));
    }
    None
}

/// 读放的边界：目录内永远可读；目录外只有「完全访问」可以。
///
/// 引擎侧的读取边界由工作区沙箱（L0/L1/L2）把关，本地 harness 没有沙箱块，
/// 于是按这一条把读也锁进绑定目录——两条执行面都遵循「没到完全访问就不越界」。
pub fn read_denial(mode: &str, target: &Path, work_dir: Option<&Path>) -> Option<String> {
    if in_work_dir(target, work_dir) || allows_outside(mode) {
        return None;
    }
    let boundary = work_dir
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| "（未绑定工作目录）".to_string());
    Some(format!(
        "当前文件访问档位是「{}」，只能读绑定工作目录（{boundary}）内的文件；\
         要读目录外请切到「完全访问」。被拒路径：{}",
        label(mode),
        target.display()
    ))
}

/// 一次访问的统一判定：读取走 `read_denial`，写/删走 `denial`，
/// 返回给调用方的都是同一句「为什么没让做」。
pub fn access_denial(mode: &str, target: &Path, work_dir: Option<&Path>, write: bool, delete: bool) -> Option<String> {
    if !write && !delete {
        return read_denial(mode, target, work_dir);
    }
    denial(mode, target, work_dir, write, delete).map(|(_, msg)| msg)
}

/// 给界面/事件流的一行档位说明（引擎侧 `permission_notes` 的对应物）
pub fn note(mode: &str, work_dir: Option<&Path>) -> String {
    let base = work_dir
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| "（未绑定）".to_string());
    match mode {
        "full_access" => "文件访问：完全访问——可读写工作目录之外的本机路径并删除文件；\
                          家目录、系统关键目录与应用数据目录仍硬拒。"
            .to_string(),
        "auto_approve" => format!(
            "文件访问：自动审批——只在绑定工作目录（{base}）内直接写盘，越出目录拒绝，删除仍需「完全访问」。"
        ),
        _ => format!(
            "文件访问：每次询问——目录内的创建/更新/删除会弹系统确认框（{base}），越出目录直接拒绝。"
        ),
    }
}

/// 把删除的文件先复制进回收目录，再删原文件；备份失败就不删。
/// 返回回收后的文件名，供工具回显与审计引用。
/// 目录做成参数：生产落点是 `trash_dir()`，单测传临时目录，
/// 免得「删除先备份」这条把备份写进用户的真实数据根。
pub fn copy_into_trash_dir(dir: &Path, path: &Path, reason: &str) -> Result<String, String> {
    let file_name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "file".to_string());
    let stamp = crate::bridge::event_store::now_ms();
    std::fs::create_dir_all(dir)
        .map_err(|e| format!("TRASH_DIR_UNAVAILABLE: 回收目录不可写：{e}"))?;
    let mut candidate = dir.join(format!("{stamp}_{file_name}"));
    let mut n = 1u32;
    while candidate.exists() {
        candidate = dir.join(format!("{stamp}_{n}_{file_name}"));
        n += 1;
        if n > 999 {
            return Err("TRASH_NAME_EXHAUSTED: 回收目录同秒重名过多，放弃删除".to_string());
        }
    }
    std::fs::copy(path, &candidate).map_err(|e| {
        format!(
            "TRASH_BACKUP_FAILED: 未能复制进回收目录（{}），文件未被删除：{e}",
            candidate.display()
        )
    })?;
    // 附带来源与原因：找回时才知道它原来在哪、为什么被删
    let sidecar = format!("{}.from.json", candidate.display());
    let meta = serde_json::json!({
        "origin": path.display().to_string(),
        "reason": reason,
        "deletedAtMs": stamp,
        "trashName": candidate.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default(),
    });
    let _ = std::fs::write(&sidecar, meta.to_string());
    candidate
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .ok_or_else(|| "TRASH_NAME_UNAVAILABLE: 回收文件名无法读取".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn far_path() -> PathBuf {
        if cfg!(windows) {
            PathBuf::from("E:/somewhere/outside")
        } else {
            PathBuf::from("/tmp/tt-far-outside")
        }
    }

    fn work_dir() -> PathBuf {
        // 用例并发跑，且各自收尾时会删掉这个目录：名字必须精确到纳秒加线程，
        // 只到毫秒会让两个用例撞同一个目录，然后被对方的清理删掉。
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let p = std::env::temp_dir().join(format!(
            "tt-file-perm-{}-{}-{nanos}",
            std::process::id(),
            format!("{:?}", std::thread::current().id()).replace(['(', ')', '"'], "")
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn illegal_values_never_widen_the_tier() {
        assert_eq!(normalize_or_default(Some("full_access")), "full_access");
        assert_eq!(normalize_or_default(Some("  AUTO_APPROVE ")), "auto_approve");
        for bad in ["", "everything", "yes", "null"] {
            assert_eq!(normalize_or_default(Some(bad)), DEFAULT_MODE, "{bad}");
        }
        assert_eq!(normalize_or_default(None), DEFAULT_MODE);
        let err = normalize("everything").unwrap_err();
        assert!(err.contains("FILE_PERMISSION_INVALID"), "{err}");
    }

    #[test]
    fn delete_is_reserved_for_full_access_at_every_tier() {
        let base = work_dir();
        let inside = base.join("a.md");
        for mode in MODES {
            if mode == "full_access" {
                continue;
            }
            let (code, msg) = denial(mode, &inside, Some(&base), true, true).unwrap();
            assert_eq!(code, "delete_requires_full_access", "{msg}");
            assert!(msg.contains("未被删除"), "{msg}");
        }
        assert!(denial("full_access", &inside, Some(&base), true, true).is_none());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn writes_inside_the_work_dir_pass_and_outside_need_full_access() {
        let base = work_dir();
        let inside = base.join("notes/a.md");
        assert!(denial("ask_every_time", &inside, Some(&base), true, false).is_none());
        assert!(denial("auto_approve", &inside, Some(&base), true, false).is_none());
        let far = far_path();
        for mode in ["ask_every_time", "auto_approve"] {
            let (code, msg) = denial(mode, &far, Some(&base), true, false).unwrap();
            assert_eq!(code, "outside_boundary", "{msg}");
            assert!(msg.contains("完全访问"), "{msg}");
        }
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn hard_deny_beats_full_access() {
        let protected = if cfg!(windows) {
            PathBuf::from("C:/Windows/win.ini")
        } else {
            PathBuf::from("/etc/hosts")
        };
        let (code, msg) = denial("full_access", &protected, None, true, false).unwrap();
        assert_eq!(code, "hard_deny", "{msg}");
        assert!(msg.contains("任何档位"), "{msg}");
        // 家目录同样硬拒：完全访问放开的是边界，不是整台电脑
        let home_file = home_dir().join("Documents/private.docx");
        assert!(denial("full_access", &home_file, None, true, false).is_some());
        // 应用数据目录里放着库与回收站，同样不许写
        let inside_app_data = data_root().join("t.db");
        let (code, _) = denial("full_access", &inside_app_data, None, true, false).unwrap();
        assert_eq!(code, "hard_deny");
    }

    /// Windows 的 `canonicalize()` 给路径加 `\\?\` 前缀，harness 解析出的落点带前缀。
    /// 前缀不剥掉就变成「换一个写法，硬拒清单看不见」——这是真会被绕过去的洞，不是格式洁癖。
    #[test]
    fn windows_verbatim_prefix_does_not_slip_past_the_deny_list() {
        if !cfg!(windows) {
            return;
        }
        let verbatim = PathBuf::from(r"\\?\C:\Windows\System32\tt.md");
        let (code, msg) = denial("full_access", &verbatim, None, true, false)
            .expect("带 Windows 命名空间前缀的系统目录同样必须硬拒");
        assert_eq!(code, "hard_deny", "{msg}");
        // 前缀剥完的形式统一，UNC 与设备路径都不会留成第三种写法
        assert_eq!(key(r"\\?\C:\Windows"), "c:/windows");
        assert_eq!(key(r"\\?\UNC\server\share\x"), r"//server/share/x");
        assert_eq!(key(r"\\.\C:\Windows\a"), "c:/windows/a");
        // 剥前缀不影响「同一目录的两种写法互相认得出」
        let base = work_dir();
        let with_prefix = PathBuf::from(format!(r"\\?\{}\x.md", base.display()));
        assert!(in_work_dir(&with_prefix, Some(&base)), "{with_prefix:?}");
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn reads_are_never_gated_by_the_tier() {
        let far = far_path();
        assert!(denial("ask_every_time", &far, None, false, false).is_none());
    }
    #[test]
    fn no_work_dir_means_every_write_is_outside() {
        let (code, msg) = denial("auto_approve", Path::new(&far_path()), None, true, false).unwrap();
        assert_eq!(code, "outside_boundary", "{msg}");
        assert!(msg.contains("未绑定工作目录"), "{msg}");
    }

    #[test]
    fn trash_copy_keeps_file_when_backup_fails() {
        let missing = far_path().join("never-existed.md");
        let dir = work_dir().join("trash");
        let err = copy_into_trash_dir(&dir, &missing, "测试").unwrap_err();
        assert!(err.contains("TRASH_BACKUP_FAILED"), "{err}");
    }

    #[test]
    fn trash_copy_backs_up_then_leaves_the_original_for_the_caller() {
        let base = work_dir();
        // 回收目录显式传路径：单测绝不往用户真实的 ~/.tongtianwork 里写东西
        let dir = base.join("trash");
        let file = base.join("keep.md");
        std::fs::write(&file, "正文").unwrap();
        let name = copy_into_trash_dir(&dir, &file, "单测").unwrap();
        assert!(dir.join(&name).exists());
        // 原文件仍在：真正删除由调用方在备份成功后执行
        assert!(file.exists());
        let sidecar = format!("{}.from.json", dir.join(&name).display());
        assert!(Path::new(&sidecar).exists(), "{sidecar}");
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn trash_dir_follows_the_data_root() {
        // 与意识/知识库/浏览器同一口径：换数据根就换回收目录，不会散落到别处
        assert_eq!(trash_dir(), data_root().join("file-trash"));
    }

    #[test]
    fn note_names_the_boundary_the_user_is_actually_getting() {
        let base = work_dir();
        assert!(note("auto_approve", Some(&base)).contains("只在绑定工作目录"));
        assert!(note("full_access", Some(&base)).contains("仍硬拒"));
        assert!(note("ask_every_time", None).contains("未绑定"));
        std::fs::remove_dir_all(&base).ok();
    }
}
