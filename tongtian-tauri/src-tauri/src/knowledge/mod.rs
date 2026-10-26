//! 本地知识库（RAG 的「文档事实来源」）
//!
//! 与技能/意识一样，桌面端只保管**真实文件**，不在数据库里另存一份正文：
//!
//! ```text
//! {TONGTIANWORK_HOME | ~/.tongtianwork}/knowledge/{kbId}/
//!   meta.json     知识库元信息（名称/描述/启停/软删除标记）
//!   docs/         用户添加的原始文件（只增不删；移除时移入 .trash/）
//!   text/         抽取出的纯文本（引擎据此建索引、做检索）
//!   .trash/       用户移除的文档（可人工找回）
//! ```
//!
//! 引擎 `engine/knowledge_runtime.py` 直接读同一目录建 SQLite FTS 索引，
//! 因此「页面里能看到几篇文档」与「模型检索到几篇」来自同一份磁盘事实。
//!
//! 存量数据保护：
//! - 删除知识库是**软删除**（写 deletedAt），目录与文档保留；
//! - 移除单篇文档是移动到 `.trash/`，不做物理删除；
//! - 添加文档遇到同名直接拒绝，不覆盖已有文件。

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

use crate::file::{office, service};
use crate::security::audit::iso8601_utc;

/// 单篇文档上限：与附件读取上限同口径（32MB），保证抽取在内存里可完成
pub const MAX_DOC_BYTES: u64 = 32 * 1024 * 1024;
/// 单个知识库文档数上限：过多会让一次全量重建索引明显变慢
pub const MAX_DOCS: usize = 500;
pub const MAX_NAME_CHARS: usize = 60;
pub const MAX_DESC_CHARS: usize = 500;

const META_FILE: &str = "meta.json";
const TRASH_DIR: &str = ".trash";
/// 引擎落索引的位置（Rust 不读，只在界面提示时使用）
const INDEX_FILE: &str = ".index.sqlite";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Meta {
    pub id: String,
    pub name: String,
    pub description: String,
    pub enabled: bool,
    pub created_at: i64,
    pub updated_at: i64,
    /// 软删除时间；有值即不再出现在列表与执行面
    pub deleted_at: Option<i64>,
}

impl Default for Meta {
    fn default() -> Self {
        Self {
            id: String::new(),
            name: String::new(),
            description: String::new(),
            enabled: true,
            created_at: 0,
            updated_at: 0,
            deleted_at: None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DocView {
    pub name: String,
    pub size: u64,
    pub updated_at: String,
    pub has_text: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct View {
    pub id: String,
    pub name: String,
    pub description: String,
    pub enabled: bool,
    pub created_at: String,
    pub updated_at: String,
    pub dir: String,
    pub docs: Vec<DocView>,
    /// 文档已落盘但抽不出正文（旧文件/手工拷入）：引擎会跳过，界面如实标出
    pub missing_text: Vec<String>,
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

pub fn root() -> PathBuf {
    match std::env::var("TONGTIANWORK_HOME") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v).join("knowledge"),
        _ => home().join(".tongtianwork").join("knowledge"),
    }
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn is_valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_')
}

fn validate_name(name: &str) -> Result<String, String> {
    let n = name.trim();
    if n.is_empty() {
        return Err("KNOWLEDGE_INVALID_NAME: 知识库名称不能为空".to_string());
    }
    if n.chars().count() > MAX_NAME_CHARS {
        return Err(format!(
            "KNOWLEDGE_INVALID_NAME: 知识库名称最多 {MAX_NAME_CHARS} 字"
        ));
    }
    if n.contains(['/', '\\', '\0']) {
        return Err("KNOWLEDGE_INVALID_NAME: 知识库名称不能包含路径分隔符".to_string());
    }
    Ok(n.to_string())
}

fn validate_desc(desc: &str) -> Result<String, String> {
    let d = desc.trim();
    if d.chars().count() > MAX_DESC_CHARS {
        return Err(format!(
            "KNOWLEDGE_INVALID_DESC: 知识库描述最多 {MAX_DESC_CHARS} 字"
        ));
    }
    Ok(d.to_string())
}

fn base_dir(root: &Path, id: &str) -> Result<PathBuf, String> {
    if !is_valid_id(id) {
        return Err(format!("KNOWLEDGE_INVALID_ID: 非法的知识库标识：{id}"));
    }
    Ok(root.join(id))
}

fn write_meta(dir: &Path, meta: &Meta) -> Result<(), String> {
    std::fs::create_dir_all(dir)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 知识库目录不可写：{e}"))?;
    let tmp = dir.join(format!("{META_FILE}.tmp"));
    let body = serde_json::to_vec_pretty(meta)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 元信息序列化失败：{e}"))?;
    std::fs::write(&tmp, body)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 元信息写入失败：{e}"))?;
    std::fs::rename(&tmp, dir.join(META_FILE))
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 元信息替换失败：{e}"))
}

fn read_meta(dir: &Path) -> Result<Meta, String> {
    let raw = std::fs::read_to_string(dir.join(META_FILE))
        .map_err(|e| format!("KNOWLEDGE_META_UNREADABLE: 元信息读取失败：{e}"))?;
    serde_json::from_str(&raw)
        .map_err(|e| format!("KNOWLEDGE_META_INVALID: 元信息不是合法 JSON：{e}"))
}

/// 文档文件名清洗：只保留文件名本身，拒绝路径穿越与控制字符
fn safe_doc_name(raw: &str) -> Result<String, String> {
    let name = raw
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("")
        .trim()
        .trim_matches('.');
    if name.is_empty() || name.contains('\0') {
        return Err("KNOWLEDGE_INVALID_DOC: 文档文件名无效".to_string());
    }
    if name.chars().count() > 120 {
        return Err("KNOWLEDGE_INVALID_DOC: 文档文件名过长（最多 120 字）".to_string());
    }
    Ok(name.to_string())
}

fn text_sidecar(dir: &Path, name: &str) -> PathBuf {
    dir.join("text").join(format!("{name}.txt"))
}

fn doc_names(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir.join("docs"))
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    names
}

fn view_of(dir: &Path) -> Result<View, String> {
    let meta = read_meta(dir)?;
    let mut docs = Vec::new();
    let mut missing_text = Vec::new();
    for name in doc_names(dir) {
        let path = dir.join("docs").join(&name);
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        let updated = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| iso8601_utc(d.as_millis() as i64))
            .unwrap_or_default();
        let has_text = text_sidecar(dir, &name).is_file();
        if !has_text {
            missing_text.push(name.clone());
        }
        docs.push(DocView {
            name,
            size,
            updated_at: updated,
            has_text,
        });
    }
    Ok(View {
        id: meta.id,
        name: meta.name,
        description: meta.description,
        enabled: meta.enabled,
        created_at: iso8601_utc(meta.created_at),
        updated_at: iso8601_utc(meta.updated_at),
        dir: dir.to_string_lossy().to_string(),
        docs,
        missing_text,
    })
}

fn list_in(root: &Path) -> Result<Vec<View>, String> {
    let rd = match std::fs::read_dir(root) {
        Ok(rd) => rd,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(format!("KNOWLEDGE_LIST_FAILED: 知识库目录读取失败：{e}")),
    };
    let mut out = Vec::new();
    for entry in rd.flatten() {
        if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }
        let dir = entry.path();
        if dir.file_name().and_then(|n| n.to_str()) == Some(TRASH_DIR) {
            continue;
        }
        match read_meta(&dir) {
            Ok(meta) if meta.deleted_at.is_none() => {}
            // 软删除或非知识库目录：跳过，不报错
            Ok(_) => continue,
            Err(_) => continue,
        }
        if let Ok(v) = view_of(&dir) {
            out.push(v);
        }
    }
    out.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    Ok(out)
}

fn create_in(root: &Path, name: &str, description: &str) -> Result<View, String> {
    let name = validate_name(name)?;
    let description = validate_desc(description)?;
    let existing = list_in(root)?;
    if existing
        .iter()
        .any(|v| v.name.eq_ignore_ascii_case(&name))
    {
        return Err(format!("KNOWLEDGE_EXISTS: 知识库「{name}」已存在"));
    }
    let id = format!(
        "kb-{}",
        &uuid::Uuid::new_v4().simple().to_string()[..10]
    );
    let dir = root.join(&id);
    for sub in ["docs", "text", TRASH_DIR] {
        std::fs::create_dir_all(dir.join(sub))
            .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 知识库目录创建失败：{e}"))?;
    }
    let ts = now_ms();
    write_meta(
        &dir,
        &Meta {
            id,
            name,
            description,
            enabled: true,
            created_at: ts,
            updated_at: ts,
            deleted_at: None,
        },
    )?;
    view_of(&dir)
}

fn update_in(
    root: &Path,
    id: &str,
    name: Option<&str>,
    description: Option<&str>,
    enabled: Option<bool>,
) -> Result<View, String> {
    let dir = base_dir(root, id)?;
    let mut meta = read_meta(&dir)?;
    if meta.deleted_at.is_some() {
        return Err("KNOWLEDGE_NOT_FOUND: 该知识库已删除".to_string());
    }
    if let Some(name) = name {
        meta.name = validate_name(name)?;
    }
    if let Some(description) = description {
        meta.description = validate_desc(description)?;
    }
    if let Some(enabled) = enabled {
        meta.enabled = enabled;
    }
    meta.updated_at = now_ms();
    write_meta(&dir, &meta)?;
    view_of(&dir)
}

fn soft_delete_in(root: &Path, id: &str) -> Result<View, String> {
    let dir = base_dir(root, id)?;
    let mut meta = read_meta(&dir)?;
    if meta.deleted_at.is_some() {
        return Err("KNOWLEDGE_NOT_FOUND: 该知识库已删除".to_string());
    }
    meta.deleted_at = Some(now_ms());
    meta.updated_at = meta.deleted_at.unwrap_or_else(now_ms);
    write_meta(&dir, &meta)?;
    view_of(&dir)
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AddResult {
    pub base: View,
    pub added: Vec<String>,
    /// 每个失败文件的原因（部分成功时也逐个摊出来）
    pub errors: Vec<String>,
}

fn extract_text(name: &str, mime: &str, bytes: &[u8]) -> Result<String, String> {
    if mime.starts_with("text/")
        || matches!(
            mime,
            "application/json" | "application/xml" | "application/xhtml+xml"
        )
    {
        return Ok(String::from_utf8_lossy(bytes).to_string());
    }
    match office::extract(mime, bytes) {
        Ok(ex) if ex.supported && !ex.content.trim().is_empty() => Ok(ex.content),
        Ok(ex) => Err(format!(
            "正文抽取为空（{}）",
            ex.note.as_deref().unwrap_or("无法解析")
        )),
        Err(e) => Err(e.trim_start_matches("FILE_CONVERSION_FAILED: ").to_string()),
    }
}

fn add_docs_in(root: &Path, id: &str, paths: &[String]) -> Result<AddResult, String> {
    if paths.is_empty() {
        return Err("KNOWLEDGE_NO_DOCS: 未选择任何文件".to_string());
    }
    let dir = base_dir(root, id)?;
    let meta = read_meta(&dir)?;
    if meta.deleted_at.is_some() {
        return Err("KNOWLEDGE_NOT_FOUND: 该知识库已删除".to_string());
    }
    std::fs::create_dir_all(dir.join("docs"))
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: docs 目录不可写：{e}"))?;
    std::fs::create_dir_all(dir.join("text"))
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: text 目录不可写：{e}"))?;

    let mut used: BTreeSet<String> = doc_names(&dir).into_iter().collect();
    let mut count = used.len();
    let mut added = Vec::new();
    let mut errors = Vec::new();

    for raw in paths {
        let path = PathBuf::from(raw);
        let name = match path
            .file_name()
            .and_then(|n| n.to_str())
            .ok_or_else(|| "KNOWLEDGE_INVALID_DOC: 路径没有文件名".to_string())
            .and_then(safe_doc_name)
        {
            Ok(n) => n,
            Err(e) => {
                errors.push(format!("{raw}：{e}"));
                continue;
            }
        };
        if used.contains(&name) {
            errors.push(format!("{name}：同名文档已存在，请先移除后再添加"));
            continue;
        }
        if count >= MAX_DOCS {
            errors.push(format!(
                "{name}：知识库已达到 {MAX_DOCS} 篇文档上限，未添加"
            ));
            continue;
        }
        let meta_fs = match std::fs::metadata(&path) {
            Ok(m) => m,
            Err(e) => {
                errors.push(format!("{name}：文件读不到（{e}）"));
                continue;
            }
        };
        if !meta_fs.is_file() {
            errors.push(format!("{name}：不是文件"));
            continue;
        }
        if meta_fs.len() > MAX_DOC_BYTES {
            errors.push(format!(
                "{name}：{}MB 超过 {}MB 上限",
                meta_fs.len() / (1024 * 1024),
                MAX_DOC_BYTES / (1024 * 1024)
            ));
            continue;
        }
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(e) => {
                errors.push(format!("{name}：读取失败（{e}）"));
                continue;
            }
        };
        let mime = service::mime_of(&path);
        let text = match extract_text(&name, mime, &bytes) {
            Ok(t) => t,
            Err(e) => {
                errors.push(format!("{name}：{e}"));
                continue;
            }
        };
        if text.trim().is_empty() {
            errors.push(format!("{name}：正文为空，未添加"));
            continue;
        }
        if let Err(e) = std::fs::write(dir.join("docs").join(&name), &bytes) {
            errors.push(format!("{name}：原文件写入失败（{e}）"));
            continue;
        }
        if let Err(e) = std::fs::write(text_sidecar(&dir, &name), text) {
            // 原文件已写入但正文缺失会让索引与界面不一致：回滚这一篇
            let _ = std::fs::remove_file(dir.join("docs").join(&name));
            errors.push(format!("{name}：正文写入失败（{e}）"));
            continue;
        }
        used.insert(name.clone());
        count += 1;
        added.push(name);
    }

    if !added.is_empty() {
        let mut meta = read_meta(&dir)?;
        meta.updated_at = now_ms();
        write_meta(&dir, &meta)?;
    }
    Ok(AddResult {
        base: view_of(&dir)?,
        added,
        errors,
    })
}

fn remove_doc_in(root: &Path, id: &str, doc: &str) -> Result<View, String> {
    let dir = base_dir(root, id)?;
    let name = safe_doc_name(doc)?;
    let doc_path = dir.join("docs").join(&name);
    if !doc_path.is_file() {
        return Err(format!("KNOWLEDGE_DOC_NOT_FOUND: 文档「{name}」不存在"));
    }
    let trash = dir.join(TRASH_DIR);
    std::fs::create_dir_all(&trash)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 回收目录不可写：{e}"))?;
    let stamp = now_ms();
    let target = trash.join(format!("{stamp}-{name}"));
    std::fs::rename(&doc_path, &target)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 文档移入回收目录失败：{e}"))?;
    let sidecar = text_sidecar(&dir, &name);
    if sidecar.is_file() {
        let _ = std::fs::rename(&sidecar, trash.join(format!("{stamp}-{name}.txt")));
    }
    // 索引是派生物：删掉让引擎下次按 docs/ 全量重建，避免检索到已移除的正文
    let _ = std::fs::remove_file(dir.join(INDEX_FILE));
    let mut meta = read_meta(&dir)?;
    meta.updated_at = now_ms();
    write_meta(&dir, &meta)?;
    view_of(&dir)
}

/// 执行面输入：启用且有正文的知识库（路径 + 名称 + 文档数）
pub fn runtime_bases(root: &Path) -> Value {
    let views = match list_in(root) {
        Ok(v) => v,
        Err(e) => return json!({ "bases": [], "error": e }),
    };
    let bases: Vec<Value> = views
        .into_iter()
        .filter(|v| v.enabled)
        .map(|v| {
            json!({
                "id": v.id,
                "name": v.name,
                "description": v.description,
                "dir": v.dir,
                "docCount": v.docs.len(),
            })
        })
        .collect();
    json!({ "bases": bases })
}

// ── Tauri commands ──────────────────────────────────────────

#[tauri::command]
pub async fn knowledge_list() -> Result<Value, String> {
    Ok(serde_json::to_value(list_in(&root())?)
        .map_err(|e| format!("KNOWLEDGE_LIST_FAILED: 序列化失败：{e}"))?)
}

#[tauri::command]
pub async fn knowledge_create(name: String, description: Option<String>) -> Result<Value, String> {
    let v = create_in(&root(), &name, description.as_deref().unwrap_or(""))?;
    Ok(serde_json::to_value(v)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 序列化失败：{e}"))?)
}

#[tauri::command]
pub async fn knowledge_update(
    kb_id: String,
    name: Option<String>,
    description: Option<String>,
    enabled: Option<bool>,
) -> Result<Value, String> {
    let v = update_in(
        &root(),
        &kb_id,
        name.as_deref(),
        description.as_deref(),
        enabled,
    )?;
    Ok(serde_json::to_value(v)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 序列化失败：{e}"))?)
}

/// 软删除：只写 deletedAt；目录、文档、正文全部保留
#[tauri::command]
pub async fn knowledge_delete(kb_id: String) -> Result<Value, String> {
    let v = soft_delete_in(&root(), &kb_id)?;
    Ok(json!({ "ok": true, "id": v.id, "name": v.name, "deletedAt": iso8601_utc(now_ms()) }))
}

#[tauri::command]
pub async fn knowledge_add_docs(kb_id: String, paths: Vec<String>) -> Result<Value, String> {
    let r = add_docs_in(&root(), &kb_id, &paths)?;
    Ok(serde_json::to_value(r)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 序列化失败：{e}"))?)
}

#[tauri::command]
pub async fn knowledge_remove_doc(kb_id: String, doc: String) -> Result<Value, String> {
    let v = remove_doc_in(&root(), &kb_id, &doc)?;
    Ok(serde_json::to_value(v)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 序列化失败：{e}"))?)
}

#[tauri::command]
pub async fn knowledge_open_dir(app: AppHandle, kb_id: Option<String>) -> Result<(), String> {
    let dir = match kb_id.as_deref() {
        Some(id) if !id.trim().is_empty() => base_dir(&root(), id)?,
        _ => root(),
    };
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("KNOWLEDGE_SAVE_FAILED: 知识库目录不可访问：{e}"))?;
    app.opener()
        .open_path(dir.to_string_lossy(), None::<&str>)
        .map_err(|e| format!("KNOWLEDGE_OPEN_FAILED: 打开目录失败：{e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "tt-knowledge-{tag}-{}",
            now_ms()
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn create_list_update_and_soft_delete() {
        let root = temp_root("crud");
        let v = create_in(&root, "产品手册", "售后与定价").unwrap();
        assert!(v.id.starts_with("kb-"), "{}", v.id);
        assert_eq!(v.enabled, true);
        assert!(PathBuf::from(&v.dir).join("docs").is_dir());

        let list = list_in(&root).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name, "产品手册");

        let updated = update_in(&root, &v.id, Some("产品资料"), None, Some(false)).unwrap();
        assert_eq!(updated.name, "产品资料");
        assert_eq!(updated.enabled, false);

        let del = soft_delete_in(&root, &v.id).unwrap();
        assert!(del.created_at.len() > 0);
        assert!(list_in(&root).unwrap().is_empty(), "软删除后不再出现在列表");
        assert!(PathBuf::from(&v.dir).join("meta.json").is_file(), "目录与元信息保留");
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn duplicate_name_is_rejected() {
        let root = temp_root("dup");
        create_in(&root, "制度", "").unwrap();
        let err = create_in(&root, " 制度 ", "").unwrap_err();
        assert!(err.starts_with("KNOWLEDGE_EXISTS"), "{err}");
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn add_text_docs_extracts_and_indexes_sidecar() {
        let root = temp_root("add");
        let v = create_in(&root, "差旅制度", "").unwrap();
        let src = root.join("差旅报销标准.md");
        std::fs::write(&src, "# 差旅\n\n住宿标准：一线城市 600 元/晚。").unwrap();
        let r = add_docs_in(&root, &v.id, &[src.to_string_lossy().to_string()]).unwrap();
        assert_eq!(r.added, vec!["差旅报销标准.md"]);
        assert!(r.errors.is_empty(), "{:?}", r.errors);
        assert_eq!(r.base.docs.len(), 1);
        assert!(r.base.docs[0].has_text);
        let text = std::fs::read_to_string(text_sidecar(Path::new(&v.dir), "差旅报销标准.md")).unwrap();
        assert!(text.contains("600 元/晚"), "{text}");
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn duplicate_and_unsupported_docs_report_per_file_errors_without_half_writes() {
        let root = temp_root("partial");
        let v = create_in(&root, "资料", "").unwrap();
        let a = root.join("a.md");
        std::fs::write(&a, "正文 A").unwrap();
        let b = root.join("b.bin");
        std::fs::write(&b, b"\x00\x01").unwrap();

        let first = add_docs_in(&root, &v.id, &[a.to_string_lossy().to_string()]).unwrap();
        assert_eq!(first.added.len(), 1);

        let second = add_docs_in(
            &root,
            &v.id,
            &[a.to_string_lossy().to_string(), b.to_string_lossy().to_string()],
        )
        .unwrap();
        assert!(second.added.is_empty(), "{:?}", second.added);
        assert_eq!(second.errors.len(), 2, "{:?}", second.errors);
        assert!(second.errors[0].contains("同名"), "{:?}", second.errors);
        assert!(second.errors[1].contains("不支持"), "{:?}", second.errors);
        assert!(!Path::new(&v.dir).join("docs").join("b.bin").exists());
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn remove_doc_moves_to_trash_and_drops_index() {
        let root = temp_root("remove");
        let v = create_in(&root, "资料", "").unwrap();
        let a = root.join("a.md");
        std::fs::write(&a, "正文 A").unwrap();
        add_docs_in(&root, &v.id, &[a.to_string_lossy().to_string()]).unwrap();
        std::fs::write(Path::new(&v.dir).join(INDEX_FILE), b"idx").unwrap();

        let after = remove_doc_in(&root, &v.id, "a.md").unwrap();
        assert!(after.docs.is_empty());
        assert!(!Path::new(&v.dir).join("docs").join("a.md").exists());
        assert!(!Path::new(&v.dir).join(INDEX_FILE).exists(), "索引必须重建");
        let trash: Vec<_> = std::fs::read_dir(Path::new(&v.dir).join(TRASH_DIR))
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().to_string())
            .collect();
        assert!(trash.iter().any(|n| n.ends_with("a.md")), "{trash:?}");
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn path_traversal_doc_name_is_reduced_to_file_name() {
        let root = temp_root("traversal");
        let v = create_in(&root, "资料", "").unwrap();
        let nested = root.join("sub");
        std::fs::create_dir_all(&nested).unwrap();
        let file = nested.join("note.md");
        std::fs::write(&file, "内容").unwrap();
        let r = add_docs_in(&root, &v.id, &[file.to_string_lossy().to_string()]).unwrap();
        assert_eq!(r.added, vec!["note.md"]);
        assert!(Path::new(&v.dir).join("docs").join("note.md").is_file());
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn runtime_bases_only_include_enabled_live_bases() {
        let root = temp_root("runtime");
        let a = create_in(&root, "A", "描述 A").unwrap();
        let b = create_in(&root, "B", "").unwrap();
        update_in(&root, &b.id, None, None, Some(false)).unwrap();
        let block = runtime_bases(&root);
        let bases = block["bases"].as_array().unwrap();
        assert_eq!(bases.len(), 1, "{block}");
        assert_eq!(bases[0]["id"], a.id);
        assert_eq!(bases[0]["description"], "描述 A");
        assert_eq!(bases[0]["docCount"], 0);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn invalid_ids_and_names_are_rejected() {
        let root = temp_root("invalid");
        assert!(create_in(&root, "  ", "").unwrap_err().starts_with("KNOWLEDGE_INVALID_NAME"));
        assert!(create_in(&root, "a/b", "").unwrap_err().starts_with("KNOWLEDGE_INVALID_NAME"));
        assert!(base_dir(&root, "../x").is_err());
        assert_eq!(safe_doc_name("../../etc/passwd").unwrap(), "passwd");
        std::fs::remove_dir_all(&root).ok();
    }
}
