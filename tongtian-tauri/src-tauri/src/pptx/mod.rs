//! 幻灯片模板库（内置主题 + 本机 .pptx 模板导入）
//!
//! 模板目录与知识库/意识同根，都是用户可见的真实文件：
//!
//! ```text
//! {TONGTIANWORK_HOME | ~/.tongtianwork}/pptx-templates/
//!   meta.json          导入台账（名称/文件名/大小/导入时间）
//!   <id>.pptx          用户导入的模板原文件
//!   .trash/            删除的模板（移入，不物理删除）
//! ```
//!
//! 内置主题是代码里的配色/字体/版式定义，不占安装包体积；导入模板则保留用户
//! 原文件的母版与版式。引擎按 `runtime.pptxTemplates` 里的路径与主题渲染，
//! 因此「设置页选中的模板」与「实际生成的 .pptx」来自同一份磁盘事实。
//!
//! 存量数据保护：导入不覆盖同名文件；删除是移动到 `.trash/`，不做物理删除。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::State;

use crate::file::office;
use crate::store::Store;

/// 单个模板文件上限：母版/版式/缩略图齐全的模板通常几 MB，20MB 已很宽裕
pub const MAX_TEMPLATE_BYTES: u64 = 20 * 1024 * 1024;
/// 解压后总大小上限，防 zip 炸弹
const MAX_TOTAL_UNCOMPRESSED: u64 = 200 * 1024 * 1024;
const MAX_ENTRIES: usize = 4000;
pub const MAX_TEMPLATES: usize = 30;
pub const MAX_NAME_CHARS: usize = 60;
pub const DEFAULT_TEMPLATE_ID: &str = "business-blue";
pub const SELECTED_KEY: &str = "pptxTemplateId";

const META_FILE: &str = "meta.json";
const TRASH_DIR: &str = ".trash";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Theme {
    pub background: String,
    pub foreground: String,
    pub accent: String,
    pub muted: String,
    pub surface: String,
    pub title_font: String,
    pub body_font: String,
    pub title_size: u32,
    pub body_size: u32,
    /// 页面左右留白（英寸）
    pub margin: f32,
    /// 封面/内容页是否绘制强调色竖条
    pub accent_bar: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TemplateView {
    pub id: String,
    pub name: String,
    pub description: String,
    pub builtin: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub theme: Option<Theme>,
    /// 导入模板的绝对路径；仅本机引擎消费，界面不展示
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imported_at: Option<i64>,
    /// 文件仍在盘上；缺失时界面如实标注而不是假装可用
    pub available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
struct Meta {
    id: String,
    name: String,
    file_name: String,
    size: u64,
    imported_at: i64,
    original_name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
struct Ledger {
    templates: Vec<Meta>,
}

// ── 内置主题 ─────────────────────────────────────────────────

fn theme(
    background: &str,
    foreground: &str,
    accent: &str,
    muted: &str,
    surface: &str,
    title_font: &str,
    body_font: &str,
    accent_bar: bool,
) -> Theme {
    Theme {
        background: background.to_string(),
        foreground: foreground.to_string(),
        accent: accent.to_string(),
        muted: muted.to_string(),
        surface: surface.to_string(),
        title_font: title_font.to_string(),
        body_font: body_font.to_string(),
        title_size: 40,
        body_size: 18,
        margin: 0.7,
        accent_bar,
    }
}

/// 三套内置主题；id 与前端 `shared/pptx-templates.ts` 保持一致。
pub fn builtins() -> Vec<TemplateView> {
    vec![
        TemplateView {
            id: "business-blue".to_string(),
            name: "商务蓝".to_string(),
            description: "白底 + 深蓝强调色，适合季度汇报、方案评审".to_string(),
            builtin: true,
            theme: Some(theme(
                "#FFFFFF", "#1F2937", "#2563EB", "#64748B", "#EFF6FF",
                "Microsoft YaHei", "Microsoft YaHei", true,
            )),
            path: None,
            size: None,
            imported_at: None,
            available: true,
            error: None,
        },
        TemplateView {
            id: "minimal-gray".to_string(),
            name: "简约灰".to_string(),
            description: "留白更多、无装饰条，适合内部同步与极简风格".to_string(),
            builtin: true,
            theme: Some(theme(
                "#FFFFFF", "#111827", "#374151", "#6B7280", "#F3F4F6",
                "Segoe UI", "Microsoft YaHei", false,
            )),
            path: None,
            size: None,
            imported_at: None,
            available: true,
            error: None,
        },
        TemplateView {
            id: "warm-earth".to_string(),
            name: "暖棕".to_string(),
            description: "米白底 + 棕橙强调色，适合培训、复盘与对外分享".to_string(),
            builtin: true,
            theme: Some(theme(
                "#FFFBF5", "#3F2E24", "#B45309", "#8A6A56", "#FDF3E7",
                "Georgia", "Microsoft YaHei", true,
            )),
            path: None,
            size: None,
            imported_at: None,
            available: true,
            error: None,
        },
    ]
}

// ── 目录与台账 ───────────────────────────────────────────────

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
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v).join("pptx-templates"),
        _ => home().join(".tongtianwork").join("pptx-templates"),
    }
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn read_ledger(root: &Path) -> Result<Ledger, String> {
    let path = root.join(META_FILE);
    if !path.is_file() {
        return Ok(Ledger::default());
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("PPTX_LEDGER_UNREADABLE: 模板台账读取失败：{e}"))?;
    serde_json::from_str(&raw)
        .map_err(|e| format!("PPTX_LEDGER_INVALID: 模板台账不是合法 JSON：{e}"))
}

fn write_ledger(root: &Path, ledger: &Ledger) -> Result<(), String> {
    std::fs::create_dir_all(root)
        .map_err(|e| format!("PPTX_SAVE_FAILED: 模板目录不可写：{e}"))?;
    let tmp = root.join(format!("{META_FILE}.tmp"));
    let body = serde_json::to_vec_pretty(ledger)
        .map_err(|e| format!("PPTX_SAVE_FAILED: 模板台账序列化失败：{e}"))?;
    std::fs::write(&tmp, body)
        .map_err(|e| format!("PPTX_SAVE_FAILED: 模板台账写入失败：{e}"))?;
    std::fs::rename(&tmp, root.join(META_FILE))
        .map_err(|e| format!("PPTX_SAVE_FAILED: 模板台账替换失败：{e}"))
}

fn selected_id(store: Option<&Store>) -> String {
    let Some(store) = store else {
        return DEFAULT_TEMPLATE_ID.to_string();
    };
    match crate::store::settings::get(store, SELECTED_KEY) {
        Ok(Some(raw)) => {
            let value = crate::store::settings::decode_value(&raw);
            value
                .as_str()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| DEFAULT_TEMPLATE_ID.to_string())
        }
        _ => DEFAULT_TEMPLATE_ID.to_string(),
    }
}

fn imported_views(root: &Path, ledger: &Ledger, notes: &mut Vec<String>) -> Vec<TemplateView> {
    let mut out = Vec::new();
    for meta in &ledger.templates {
        let path = root.join(&meta.file_name);
        let exists = path.is_file();
        out.push(TemplateView {
            id: meta.id.clone(),
            name: meta.name.clone(),
            description: format!("用户导入的 .pptx 模板（{}）", meta.original_name),
            builtin: false,
            theme: None,
            path: Some(path.to_string_lossy().to_string()),
            size: Some(meta.size),
            imported_at: Some(meta.imported_at),
            available: exists,
            error: if exists {
                None
            } else {
                Some("模板文件已不在模板目录，请重新导入或删除该条目".to_string())
            },
        });
        if !exists {
            notes.push(format!("模板「{}」的文件缺失，无法用于生成", meta.name));
        }
    }
    out
}

/// 所有模板（内置 + 导入）与当前选择。选择失效时退回默认主题并留下说明。
fn state_in(root: &Path, store: Option<&Store>) -> Value {
    let mut notes: Vec<String> = Vec::new();
    let ledger = match read_ledger(root) {
        Ok(l) => l,
        Err(e) => {
            notes.push(e.clone());
            Ledger::default()
        }
    };
    let mut templates = builtins();
    templates.extend(imported_views(root, &ledger, &mut notes));
    let wanted = selected_id(store);
    let selected = templates
        .iter()
        .find(|t| t.id == wanted)
        .cloned()
        .unwrap_or_else(|| {
            notes.push(format!(
                "所选模板「{wanted}」不存在，已按默认主题「商务蓝」执行"
            ));
            templates
                .iter()
                .find(|t| t.id == DEFAULT_TEMPLATE_ID)
                .cloned()
                .expect("内置默认模板必须存在")
        });
    json!({
        "templates": templates,
        "selectedId": selected.id,
        "selected": selected,
        "defaultId": DEFAULT_TEMPLATE_ID,
        "root": root.to_string_lossy(),
        "notes": notes,
    })
}

// ── 校验与导入 ───────────────────────────────────────────────

fn validate_name(name: &str) -> Result<String, String> {
    let n = name.trim();
    if n.is_empty() {
        return Err("PPTX_INVALID_NAME: 模板名称不能为空".to_string());
    }
    let n: String = n.chars().take(MAX_NAME_CHARS).collect();
    if n.chars().any(|c| c.is_control()) {
        return Err("PPTX_INVALID_NAME: 模板名称不能包含控制字符".to_string());
    }
    Ok(n)
}

/// 只接受真正的 .pptx 容器：必备部件齐全、无路径穿越条目、解压总量受限。
pub fn validate_template_bytes(file_name: &str, bytes: &[u8]) -> Result<(), String> {
    if !file_name.to_ascii_lowercase().ends_with(".pptx") {
        return Err("PPTX_TEMPLATE_UNSUPPORTED: 只支持 .pptx（.pptm 宏模板请另存为 .pptx）"
            .to_string());
    }
    if bytes.len() as u64 > MAX_TEMPLATE_BYTES {
        return Err(format!(
            "PPTX_TEMPLATE_TOO_LARGE: 模板 {:.1}MB 超过 {}MB 上限",
            bytes.len() as f64 / (1024.0 * 1024.0),
            MAX_TEMPLATE_BYTES / 1024 / 1024
        ));
    }
    let entries = office::zip_entries(bytes)
        .map_err(|e| format!("PPTX_TEMPLATE_INVALID: {}", e.trim_start_matches("FILE_CONVERSION_FAILED: ")))?;
    if entries.len() > MAX_ENTRIES {
        return Err(format!(
            "PPTX_TEMPLATE_INVALID: 压缩包条目过多（{} > {MAX_ENTRIES}）",
            entries.len()
        ));
    }
    let mut total = 0u64;
    let mut has_content_types = false;
    let mut has_presentation = false;
    let mut has_layout = false;
    for entry in &entries {
        let lower = entry.name.to_ascii_lowercase();
        if entry.name.starts_with('/') || lower.split('/').any(|seg| seg == "..") {
            return Err(format!(
                "PPTX_TEMPLATE_INVALID: 压缩包内含越界路径「{}」，已拒绝导入",
                entry.name
            ));
        }
        total = total.saturating_add(entry.uncomp_size);
        if total > MAX_TOTAL_UNCOMPRESSED {
            return Err("PPTX_TEMPLATE_INVALID: 解压后体积过大，已拒绝导入".to_string());
        }
        has_content_types |= lower == "[content_types].xml";
        has_presentation |= lower == "ppt/presentation.xml";
        has_layout |= lower.starts_with("ppt/slidelayouts/") && lower.ends_with(".xml");
    }
    if !has_content_types || !has_presentation || !has_layout {
        return Err(
            "PPTX_TEMPLATE_INVALID: 不是完整的 PowerPoint 演示文稿（缺少内容类型/演示文稿/版式部件）"
                .to_string(),
        );
    }
    Ok(())
}

fn import_in(root: &Path, path: &Path) -> Result<Value, String> {
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| "PPTX_INVALID_NAME: 模板路径没有文件名".to_string())?;
    let ext_ok = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("pptx"))
        .unwrap_or(false);
    if !ext_ok {
        return Err("PPTX_TEMPLATE_UNSUPPORTED: 只支持 .pptx 模板".to_string());
    }
    let meta_fs = std::fs::metadata(path)
        .map_err(|e| format!("PPTX_TEMPLATE_UNREADABLE: 模板文件读不到：{e}"))?;
    if !meta_fs.is_file() {
        return Err("PPTX_TEMPLATE_UNREADABLE: 选择的路径不是文件".to_string());
    }
    if meta_fs.len() > MAX_TEMPLATE_BYTES {
        return Err(format!(
            "PPTX_TEMPLATE_TOO_LARGE: 模板 {:.1}MB 超过 {}MB 上限",
            meta_fs.len() as f64 / (1024.0 * 1024.0),
            MAX_TEMPLATE_BYTES / 1024 / 1024
        ));
    }
    let bytes = std::fs::read(path)
        .map_err(|e| format!("PPTX_TEMPLATE_UNREADABLE: 模板读取失败：{e}"))?;
    validate_template_bytes(file_name, &bytes)?;

    let mut ledger = read_ledger(root)?;
    if ledger.templates.len() >= MAX_TEMPLATES {
        return Err(format!("PPTX_LIMIT_REACHED: 最多导入 {MAX_TEMPLATES} 个模板"));
    }
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("导入模板");
    let name = validate_name(stem)?;
    if ledger
        .templates
        .iter()
        .any(|m| m.name.eq_ignore_ascii_case(&name))
    {
        return Err(format!("PPTX_TEMPLATE_EXISTS: 同名模板「{name}」已存在"));
    }

    let stamp = now_ms();
    let mut id = format!("imported-{stamp}");
    let mut n = 1;
    while ledger.templates.iter().any(|m| m.id == id) {
        id = format!("imported-{stamp}-{n}");
        n += 1;
    }
    let stored_name = format!("{id}.pptx");
    std::fs::create_dir_all(root)
        .map_err(|e| format!("PPTX_SAVE_FAILED: 模板目录不可写：{e}"))?;
    let target = root.join(&stored_name);
    // create_new：即使同毫秒并发导入也不会覆盖已有文件
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
            .map_err(|e| format!("PPTX_SAVE_FAILED: 模板写入失败：{e}"))?;
        f.write_all(&bytes)
            .map_err(|e| format!("PPTX_SAVE_FAILED: 模板写入失败：{e}"))?;
    }
    ledger.templates.push(Meta {
        id: id.clone(),
        name,
        file_name: stored_name,
        size: bytes.len() as u64,
        imported_at: stamp,
        original_name: file_name.to_string(),
    });
    if let Err(e) = write_ledger(root, &ledger) {
        let _ = std::fs::remove_file(&target);
        return Err(e);
    }
    Ok(json!({ "ok": true, "importedId": id }))
}

fn delete_in(root: &Path, store: Option<&Store>, template_id: &str) -> Result<Value, String> {
    let mut ledger = read_ledger(root)?;
    let idx = ledger
        .templates
        .iter()
        .position(|m| m.id == template_id)
        .ok_or_else(|| "PPTX_TEMPLATE_NOT_FOUND: 模板不存在（内置模板不可删除）".to_string())?;
    let meta = ledger.templates.remove(idx);
    let source = root.join(&meta.file_name);
    if source.is_file() {
        let trash = root.join(TRASH_DIR);
        std::fs::create_dir_all(&trash)
            .map_err(|e| format!("PPTX_SAVE_FAILED: 回收目录不可写：{e}"))?;
        let target = trash.join(format!("{}-{}", now_ms(), meta.file_name));
        std::fs::rename(&source, &target)
            .map_err(|e| format!("PPTX_SAVE_FAILED: 模板移入回收目录失败：{e}"))?;
    }
    write_ledger(root, &ledger)?;
    if let Some(store) = store {
        if selected_id(Some(store)) == template_id {
            crate::store::settings::set(store, SELECTED_KEY, DEFAULT_TEMPLATE_ID)?;
        }
    }
    Ok(json!({ "ok": true, "deletedId": template_id, "name": meta.name }))
}

// ── 运行时 ───────────────────────────────────────────────────

/// 执行面 bundle 的 `pptxTemplates` 段。只把本机模板路径与主题交给本机引擎，
/// 引擎按 `root` 做路径收敛；不把用户文件内容读进渲染层。
pub fn runtime_block(store: Option<&Store>) -> Value {
    state_in(&root(), store)
}

// ── Tauri commands ──────────────────────────────────────────

#[tauri::command]
pub async fn pptx_templates_list(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
) -> Result<Value, String> {
    Ok(state_in(&root(), Some(&p0.store)))
}

#[tauri::command]
pub async fn pptx_template_import(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    path: String,
) -> Result<Value, String> {
    let root = root();
    let result = import_in(&root, Path::new(&path))?;
    let mut state = state_in(&root, Some(&p0.store));
    if let Value::Object(ref mut obj) = state {
        obj.insert("importedId".to_string(), result["importedId"].clone());
    }
    Ok(state)
}

#[tauri::command]
pub async fn pptx_template_delete(
    p0: State<'_, std::sync::Arc<crate::P0Services>>,
    template_id: String,
) -> Result<Value, String> {
    let root = root();
    let result = delete_in(&root, Some(&p0.store), &template_id)?;
    let mut state = state_in(&root, Some(&p0.store));
    if let Value::Object(ref mut obj) = state {
        obj.insert("deletedId".to_string(), result["deletedId"].clone());
    }
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::file::office::zip_build;

    fn pptx_fixture() -> Vec<u8> {
        zip_build(&[
            ("[Content_Types].xml", b"<Types/>".as_slice(), 0),
            ("ppt/presentation.xml", b"<p:presentation/>".as_slice(), 8),
            ("ppt/slideLayouts/slideLayout1.xml", b"<p:sldLayout/>".as_slice(), 8),
        ])
    }

    fn temp_root(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("tt-pptx-{name}-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-pptx-db-{name}-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn builtins_have_three_stable_ids() {
        let ids: Vec<String> = builtins().into_iter().map(|t| t.id).collect();
        assert_eq!(ids, ["business-blue", "minimal-gray", "warm-earth"]);
    }

    #[test]
    fn validate_accepts_real_container_and_rejects_truncated_or_traversal() {
        let ok = pptx_fixture();
        assert!(validate_template_bytes("ok.pptx", &ok).is_ok());

        let mut broken = ok.clone();
        broken.truncate(broken.len() - 20);
        let err = validate_template_bytes("broken.pptx", &broken).unwrap_err();
        assert!(err.starts_with("PPTX_TEMPLATE_INVALID"), "{err}");

        let traversal = zip_build(&[
            ("[Content_Types].xml", b"<Types/>".as_slice(), 0),
            ("ppt/presentation.xml", b"<p:presentation/>".as_slice(), 0),
            ("ppt/slideLayouts/slideLayout1.xml", b"<p:sldLayout/>".as_slice(), 0),
            ("../evil.txt", b"x".as_slice(), 0),
        ]);
        let err = validate_template_bytes("evil.pptx", &traversal).unwrap_err();
        assert!(err.contains("越界路径"), "{err}");

        assert!(validate_template_bytes(
            "macro.pptm",
            &zip_build(&[("[Content_Types].xml", b"<Types/>".as_slice(), 0)])
        )
        .unwrap_err()
        .starts_with("PPTX_TEMPLATE_UNSUPPORTED"));
    }

    #[test]
    fn import_list_delete_roundtrip_never_overwrites() {
        let root = temp_root("roundtrip");
        let src_dir = temp_root("roundtrip-src");
        let src = src_dir.join("季度模板.pptx");
        std::fs::write(&src, pptx_fixture()).unwrap();

        let out = import_in(&root, &src).unwrap();
        assert_eq!(out["ok"], true);
        let ledger = read_ledger(&root).unwrap();
        assert_eq!(ledger.templates.len(), 1);
        assert_eq!(ledger.templates[0].name, "季度模板");
        let stored = root.join(&ledger.templates[0].file_name);
        assert!(stored.is_file());

        // 同名再次导入必须被拒绝，且不覆盖第一个文件
        let err = import_in(&root, &src).unwrap_err();
        assert!(err.starts_with("PPTX_TEMPLATE_EXISTS"), "{err}");
        assert_eq!(std::fs::read(&stored).unwrap(), pptx_fixture());

        // 删除是软删除：文件进 .trash，台账移除
        let (store, db_dir) = temp_store("roundtrip");
        crate::store::settings::set(&store, SELECTED_KEY, &ledger.templates[0].id).unwrap();
        let id = ledger.templates[0].id.clone();
        let del = delete_in(&root, Some(&store), &id).unwrap();
        assert_eq!(del["deletedId"], id);
        assert!(!stored.exists());
        let trash: Vec<_> = std::fs::read_dir(root.join(TRASH_DIR)).unwrap().flatten().collect();
        assert_eq!(trash.len(), 1);
        assert_eq!(selected_id(Some(&store)), DEFAULT_TEMPLATE_ID);

        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&src_dir);
        let _ = std::fs::remove_dir_all(&db_dir);
    }

    #[test]
    fn state_falls_back_when_selected_template_is_unavailable() {
        let root = temp_root("state");
        let (store, db_dir) = temp_store("state");
        crate::store::settings::set(&store, SELECTED_KEY, "missing-template").unwrap();
        let state = state_in(&root, Some(&store));
        assert_eq!(state["selectedId"], DEFAULT_TEMPLATE_ID);
        assert_eq!(state["templates"].as_array().unwrap().len(), 3);
        assert!(state["notes"].as_array().unwrap().iter().any(|n| n
            .as_str()
            .unwrap_or("")
            .contains("不存在")));
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&db_dir);
    }
}
