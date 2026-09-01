//! 文件服务：file_list / file_read / file_preview / 附件暂存
//!
//! 白名单规则（交互补设计 §4.3）：Agent 文件操作范围 =
//! `workspaces/{id}/files/` ∪ SandboxPolicy.file_allowlist（settings_kv 暂存，缺省为空）。
//! 越界访问返回 FILE_ACCESS_DENIED；大小上限 2MB（FILE_TOO_LARGE）。
//!
//! 全部能力函数提供 `_in(home, ...)` 变体注入数据根目录（测试隔离，避免进程级 env 竞争），
//! 公开入口缺省走 python_env::base_dir()（TONGTIAN_HOME 可覆盖，双目录设计规范）。

use serde::Serialize;
use std::path::{Path, PathBuf};

use crate::bridge::python_env;
use crate::store::Store;

/// 单文件大小上限（2MB）
const MAX_READ_BYTES: u64 = 2 * 1024 * 1024;
/// 附件单文件上限（交互补设计 §3.3：50MB）
const MAX_ATTACHMENT_BYTES: u64 = 50 * 1024 * 1024;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FileInfo {
    pub name: String,
    pub path: String,
    pub is_directory: bool,
    pub size: u64,
    pub modified_at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FilePreview {
    pub path: String,
    pub content: String,
    pub mime_type: String,
    /// 是否可直接预览（docx/xlsx/pdf 本批无转换能力，显式标注不静默）
    pub supported: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AttachmentMeta {
    pub file_name: String,
    pub staged_path: String,
    pub size_bytes: u64,
    pub mime: String,
    /// 数据分级检测结果（public/internal/confidential）
    pub sensitivity: String,
}

// ── 白名单 ─────────────────────────────────────────────────

/// 数据根目录（生产路径）
fn data_home() -> PathBuf {
    python_env::base_dir()
}

/// 工作区文件根目录（与引擎 work-dir 同源布局：{home}/workspaces/{ws}/files）
fn workspace_files_root_in(home: &Path, workspace_id: &str) -> PathBuf {
    home.join("workspaces").join(workspace_id).join("files")
}

/// 工作区文件根目录（生产入口）
pub fn workspace_files_root(workspace_id: &str) -> PathBuf {
    workspace_files_root_in(&data_home(), workspace_id)
}

/// 允许访问的根目录集合
fn allowed_roots_in(home: &Path, store: &Store, workspace_id: &str) -> Vec<PathBuf> {
    let mut roots = vec![workspace_files_root_in(home, workspace_id)];
    // SandboxPolicy.file_allowlist 暂存于 settings_kv（键：sandbox.file_allowlist.{ws}）
    let key = format!("sandbox.file_allowlist.{workspace_id}");
    if let Ok(Some(json)) = crate::store::settings::get(store, &key) {
        if let Ok(arr) = serde_json::from_str::<Vec<String>>(&json) {
            roots.extend(arr.into_iter().map(PathBuf::from));
        }
    }
    roots
}

/// 对存在的路径规范化；不存在时回退到「存在的祖先 + 剩余部分拼接」
fn canonicalize_existing(path: &Path) -> Option<PathBuf> {
    if let Ok(c) = path.canonicalize() {
        return Some(c);
    }
    let mut existing = path.to_path_buf();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if existing.exists() {
            let base = existing.canonicalize().ok()?;
            let mut full = base;
            for part in tail.iter().rev() {
                full = full.join(part);
            }
            return Some(full);
        }
        match existing.file_name() {
            Some(name) => {
                tail.push(name.to_os_string());
                if !existing.pop() {
                    return None;
                }
            }
            None => return None,
        }
    }
}

/// 路径消毒：规范化后校验是否位于白名单根之内
fn check_within_whitelist_in(
    home: &Path,
    store: &Store,
    workspace_id: &str,
    path: &Path,
) -> Result<PathBuf, String> {
    let canon = canonicalize_existing(path)
        .ok_or_else(|| format!("FILE_ACCESS_DENIED: 路径无法解析 {}", path.display()))?;
    for root in allowed_roots_in(home, store, workspace_id) {
        if let Some(root_canon) = canonicalize_existing(&root) {
            if canon.starts_with(&root_canon) {
                return Ok(canon);
            }
        }
    }
    Err(format!(
        "FILE_ACCESS_DENIED: {} 不在工作区文件白名单内",
        path.display()
    ))
}

// ── 能力实现 ───────────────────────────────────────────────

/// 列出目录内容（白名单校验）
pub fn list_dir_in(
    home: &Path,
    store: &Store,
    workspace_id: &str,
    dir_path: &str,
) -> Result<Vec<FileInfo>, String> {
    let dir = check_within_whitelist_in(home, store, workspace_id, Path::new(dir_path))?;
    if !dir.is_dir() {
        return Err(format!("FILE_NOT_FOUND: 目录不存在 {}", dir_path));
    }
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("FILE_ACCESS_DENIED: {e}"))?;
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let meta = entry.metadata().ok();
        out.push(FileInfo {
            name: entry.file_name().to_string_lossy().to_string(),
            path: entry.path().to_string_lossy().to_string(),
            is_directory: meta.as_ref().map(|m| m.is_dir()).unwrap_or(false),
            size: meta.as_ref().map(|m| m.len()).unwrap_or(0),
            modified_at: meta
                .as_ref()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis().to_string())
                .unwrap_or_default(),
        });
    }
    // 目录优先，名称排序
    out.sort_by(|a, b| b.is_directory.cmp(&a.is_directory).then(a.name.cmp(&b.name)));
    Ok(out)
}

pub fn list_dir(store: &Store, workspace_id: &str, dir_path: &str) -> Result<Vec<FileInfo>, String> {
    list_dir_in(&data_home(), store, workspace_id, dir_path)
}

/// 读取文本文件（白名单 + 2MB 上限）
pub fn read_file_in(
    home: &Path,
    store: &Store,
    workspace_id: &str,
    file_path: &str,
) -> Result<String, String> {
    let path = check_within_whitelist_in(home, store, workspace_id, Path::new(file_path))?;
    if !path.is_file() {
        return Err(format!("FILE_NOT_FOUND: {file_path}"));
    }
    let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    if size > MAX_READ_BYTES {
        return Err(format!(
            "FILE_TOO_LARGE: 文件 {} 超出 2MB 读取上限（{} bytes）",
            file_path, size
        ));
    }
    std::fs::read_to_string(&path)
        .map_err(|e| format!("FILE_READ_FAILED: {e}（可能是二进制文件）"))
}

pub fn read_file(store: &Store, workspace_id: &str, file_path: &str) -> Result<String, String> {
    read_file_in(&data_home(), store, workspace_id, file_path)
}

fn mime_of(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("txt") => "text/plain",
        Some("md") => "text/markdown",
        Some("csv") => "text/csv",
        Some("json") => "application/json",
        Some("docx") => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        Some("xlsx") => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        Some("pdf") => "application/pdf",
        _ => "application/octet-stream",
    }
}

/// 批量预览：txt/md/csv/json 直读；docx/xlsx/pdf 返回元信息 + 转换未落地提示（不静默）
pub fn preview_in(
    home: &Path,
    store: &Store,
    workspace_id: &str,
    file_paths: &[String],
) -> Result<Vec<FilePreview>, String> {
    let mut out = Vec::new();
    for fp in file_paths {
        let path = check_within_whitelist_in(home, store, workspace_id, Path::new(fp))?;
        let mime = mime_of(&path);
        let text_preview = matches!(mime, "text/plain" | "text/markdown" | "text/csv" | "application/json");
        if text_preview {
            match read_file_in(home, store, workspace_id, fp) {
                Ok(content) => out.push(FilePreview {
                    path: fp.clone(),
                    content,
                    mime_type: mime.to_string(),
                    supported: true,
                    note: None,
                }),
                Err(e) => out.push(FilePreview {
                    path: fp.clone(),
                    content: String::new(),
                    mime_type: mime.to_string(),
                    supported: false,
                    note: Some(e),
                }),
            }
        } else {
            out.push(FilePreview {
                path: fp.clone(),
                content: String::new(),
                mime_type: mime.to_string(),
                supported: false,
                note: Some("FILE_CONVERSION_FAILED: 格式转换能力下批落地，当前仅可查看元信息".into()),
            });
        }
    }
    Ok(out)
}

pub fn preview(store: &Store, workspace_id: &str, file_paths: &[String]) -> Result<Vec<FilePreview>, String> {
    preview_in(&data_home(), store, workspace_id, file_paths)
}

/// 数据分级检测（简化版：关键词匹配，镜像 spec §6.3 敏感规则）
fn detect_sensitivity(content_sample: &str, file_name: &str) -> &'static str {
    let hay = format!("{file_name}\n{content_sample}");
    if hay.contains("绝密") || hay.contains("机密") {
        "confidential"
    } else if hay.contains("内部") || hay.contains("秘密") {
        "internal"
    } else {
        "public"
    }
}

/// 附件暂存（交互补设计 §3.1 附件模式）：
/// 复制到 `{home}/workspaces/{ws}/files/attachments/{task_id}/`，返回 AttachmentMeta
pub fn stage_attachment_in(
    home: &Path,
    workspace_id: &str,
    task_id: &str,
    src_path: &str,
) -> Result<AttachmentMeta, String> {
    let src = Path::new(src_path);
    if !src.is_file() {
        return Err(format!("FILE_NOT_FOUND: {src_path}"));
    }
    let file_name = src
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .ok_or_else(|| format!("FILE_REJECTED: 无法解析文件名 {src_path}"))?;

    // 格式校验（docx/xlsx/pdf/csv/txt/md/json）
    let mime = mime_of(src);
    let allowed = matches!(
        mime,
        "text/plain"
            | "text/markdown"
            | "text/csv"
            | "application/json"
            | "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            | "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            | "application/pdf"
    );
    if !allowed {
        return Err(format!("FILE_REJECTED: 不支持的附件格式（{mime}）"));
    }

    let size = std::fs::metadata(src).map(|m| m.len()).unwrap_or(0);
    if size > MAX_ATTACHMENT_BYTES {
        return Err(format!(
            "FILE_REJECTED: 附件超出 50MB 上限（{} bytes）",
            size
        ));
    }

    // 暂存目录（与引擎 work-dir 同源布局）
    let safe_task: String = task_id
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    let dest_dir = workspace_files_root_in(home, workspace_id)
        .join("attachments")
        .join(&safe_task);
    std::fs::create_dir_all(&dest_dir)
        .map_err(|e| format!("FILE_BACKUP_FAILED: 暂存目录创建失败 {e}"))?;
    let dest = dest_dir.join(&file_name);
    std::fs::copy(src, &dest).map_err(|e| format!("FILE_BACKUP_FAILED: 附件复制失败 {e}"))?;

    // 敏感度抽样检测（读前 8KB 文本）
    let sample = std::fs::read_to_string(&dest)
        .map(|s| s.chars().take(8192).collect::<String>())
        .unwrap_or_default();
    let sensitivity = detect_sensitivity(&sample, &file_name);

    Ok(AttachmentMeta {
        file_name,
        staged_path: dest.to_string_lossy().to_string(),
        size_bytes: size,
        mime: mime.to_string(),
        sensitivity: sensitivity.to_string(),
    })
}

pub fn stage_attachment(
    workspace_id: &str,
    task_id: &str,
    src_path: &str,
) -> Result<AttachmentMeta, String> {
    stage_attachment_in(&data_home(), workspace_id, task_id, src_path)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造隔离的临时 home（不依赖进程级 env，避免并行测试竞争）
    fn temp_home(name: &str) -> (PathBuf, String, PathBuf, Store) {
        let home = std::env::temp_dir().join(format!(
            "tt-file-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        let ws = "ws-test".to_string();
        let files = workspace_files_root_in(&home, &ws);
        std::fs::create_dir_all(&files).unwrap();
        let store = Store::open(home.join("store.db")).unwrap();
        (home, ws, files, store)
    }

    fn cleanup(home: &Path) {
        let _ = std::fs::remove_dir_all(home);
    }

    #[test]
    fn test_list_dir_within_whitelist() {
        let (home, ws, files, store) = temp_home("list");
        std::fs::write(files.join("a.txt"), "hello").unwrap();
        std::fs::create_dir_all(files.join("sub")).unwrap();
        let items = list_dir_in(&home, &store, &ws, files.to_str().unwrap()).unwrap();
        assert_eq!(items.len(), 2);
        assert!(items[0].is_directory); // 目录优先
        cleanup(&home);
    }

    #[test]
    fn test_list_dir_outside_whitelist_denied() {
        let (home, ws, _files, store) = temp_home("deny");
        let err = list_dir_in(&home, &store, &ws, std::env::temp_dir().to_str().unwrap())
            .unwrap_err();
        assert!(err.starts_with("FILE_ACCESS_DENIED"));
        cleanup(&home);
    }

    #[test]
    fn test_read_and_preview_text() {
        let (home, ws, files, store) = temp_home("read");
        std::fs::write(files.join("note.md"), "# 标题").unwrap();
        let content = read_file_in(&home, &store, &ws, files.join("note.md").to_str().unwrap()).unwrap();
        assert_eq!(content, "# 标题");
        let previews = preview_in(
            &home,
            &store,
            &ws,
            &[files.join("note.md").to_str().unwrap().to_string()],
        )
        .unwrap();
        assert!(previews[0].supported);
        assert_eq!(previews[0].mime_type, "text/markdown");
        cleanup(&home);
    }

    #[test]
    fn test_preview_binary_reports_conversion_not_ready() {
        let (home, ws, files, store) = temp_home("bin");
        std::fs::write(files.join("doc.docx"), b"PK\x03\x04fake").unwrap();
        let previews = preview_in(
            &home,
            &store,
            &ws,
            &[files.join("doc.docx").to_str().unwrap().to_string()],
        )
        .unwrap();
        assert!(!previews[0].supported);
        assert!(previews[0].note.as_ref().unwrap().contains("FILE_CONVERSION_FAILED"));
        cleanup(&home);
    }

    #[test]
    fn test_read_too_large() {
        let (home, ws, files, store) = temp_home("large");
        let big = vec![b'a'; (MAX_READ_BYTES + 1) as usize];
        std::fs::write(files.join("big.txt"), &big).unwrap();
        let err = read_file_in(&home, &store, &ws, files.join("big.txt").to_str().unwrap())
            .unwrap_err();
        assert!(err.starts_with("FILE_TOO_LARGE"));
        cleanup(&home);
    }

    #[test]
    fn test_stage_attachment_lands_in_task_dir() {
        let (home, ws, files, _store) = temp_home("stage");
        // 源文件在白名单外（home 根）——附件暂存不要求源在白名单（来自系统对话框）
        let src = home.join("upload.txt");
        std::fs::write(&src, "内部资料：季度数据").unwrap();
        let meta = stage_attachment_in(&home, &ws, "task-1", src.to_str().unwrap()).unwrap();
        assert!(meta.staged_path.contains("attachments"));
        assert!(meta.staged_path.contains("task-1"));
        assert!(Path::new(&meta.staged_path).exists());
        assert_eq!(meta.sensitivity, "internal"); // 关键词命中
        assert!(files.join("attachments").exists());
        cleanup(&home);
    }

    #[test]
    fn test_stage_attachment_rejects_bad_format() {
        let (home, ws, _files, _store) = temp_home("fmt");
        let src = home.join("malware.exe");
        std::fs::write(&src, b"MZ").unwrap();
        let err = stage_attachment_in(&home, &ws, "task-2", src.to_str().unwrap()).unwrap_err();
        assert!(err.starts_with("FILE_REJECTED"));
        cleanup(&home);
    }

    #[test]
    fn test_allowlist_extension_via_settings() {
        let (home, ws, _files, store) = temp_home("allow");
        let extra = home.join("extra-dir");
        std::fs::create_dir_all(&extra).unwrap();
        std::fs::write(extra.join("x.txt"), "ok").unwrap();
        // 未加白名单：拒绝
        assert!(read_file_in(&home, &store, &ws, extra.join("x.txt").to_str().unwrap()).is_err());
        // 写入 allowlist 后放行
        crate::store::settings::set(
            &store,
            &format!("sandbox.file_allowlist.{ws}"),
            &serde_json::to_string(&vec![extra.to_string_lossy().to_string()]).unwrap(),
        )
        .unwrap();
        let content = read_file_in(&home, &store, &ws, extra.join("x.txt").to_str().unwrap()).unwrap();
        assert_eq!(content, "ok");
        cleanup(&home);
    }
}
