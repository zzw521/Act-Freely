//! 意识（awareness:* 五通道的桌面权威实现）
//!
//! 事实来源是意识目录下的**真实文件**，而不是第二份数据库副本：
//!
//! ```text
//! {TONGTIANWORK_HOME | ~/.tongtianwork}/awareness/main/
//!   SOUL.md          协作风格（四个预设模板之一，可定制正文）
//!   WORK_MANUAL.md   工作手册
//!   USER_PROFILE.md  用户画像
//!   MEMORY.md        长期记忆（每行一条结构化条目 + 用户自己的正文）
//!   PROFILE.json     意识开关与风格选择（引擎通道读写同一文件，避免双写源）
//! ```
//!
//! 引擎侧 `engine/server.py` 的 `/api/awareness/*` 指向**同一目录**，因此桌面（Rust）
//! 与浏览器直连引擎两条通道看到的意识内容一致；无文件系统的 Mock 通道由
//! `renderer/lib/awareness-local.ts` 以同一套语义（同一行格式、同一份默认模板）兜底。
//!
//! 存量数据保护（开发规范）：
//! - 能力文件缺失时才按默认模板初始化，已存在的文件永不被初始化覆盖
//! - 记忆条目按行追加/删除，MEMORY.md 里用户自己写的正文原样保留
//! - 只有 `FILE_NAMES` 白名单内的文件可写，`PROFILE.json` 不接受外部直写

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::State;

use crate::security::audit;
use crate::P0Services;

/// 允许读写的能力文件名（与引擎 `AWARENESS_FILE_NAMES`、contracts.ts 完全一致）
pub const FILE_NAMES: &[&str] = &["SOUL.md", "WORK_MANUAL.md", "USER_PROFILE.md", "MEMORY.md"];
/// 协作风格预设 id（与设置页 SOUL.md 模板、contracts.ts 的 AwarenessStyleId 一致）
pub const STYLE_IDS: &[&str] = &["efficient", "deliberate", "gentle", "assertive"];
pub const DEFAULT_STYLE: &str = "efficient";
/// 记忆条目字段分隔符（id · source · createdAt · content）
pub const MEMORY_DELIM: &str = " · ";
/// 意识开关与风格选择的落盘文件名
pub const PROFILE_FILE: &str = "PROFILE.json";
/// 未落盘文件的时间占位
pub const DEFAULT_MARK: &str = "系统默认";

/// 意识画像（落盘于 PROFILE.json）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Profile {
    pub mode: bool,
    pub auto_memory: bool,
    pub style: String,
    pub updated_at: String,
}

impl Default for Profile {
    fn default() -> Self {
        Self {
            mode: true,
            auto_memory: true,
            style: DEFAULT_STYLE.to_string(),
            updated_at: String::new(),
        }
    }
}

/// 能力文件磁盘视图
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FileEntry {
    pub content: String,
    /// ISO UTC 时间（未落盘时为「系统默认」）
    pub updated_at: String,
}

/// 长期记忆条目（MEMORY.md 的结构化行）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryEntry {
    pub id: String,
    pub content: String,
    /// auto / manual / reflection
    pub source: String,
    pub created_at: String,
}

/// `awareness:get` 返回体：画像字段 + 目录 + 四个能力文件
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct View {
    pub mode: bool,
    pub auto_memory: bool,
    pub style: String,
    pub updated_at: String,
    pub dir: String,
    pub files: BTreeMap<String, FileEntry>,
}

// ── 目录与默认模板 ──────────────────────────────────────────

fn user_home() -> PathBuf {
    let raw = std::env::var("USERPROFILE")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("HOME").ok().filter(|s| !s.trim().is_empty()))
        .unwrap_or_else(|| ".".to_string());
    PathBuf::from(raw)
}

/// 意识目录：`TONGTIANWORK_HOME` 覆盖（测试与自定义数据根用），缺省 `~/.tongtianwork`
pub fn dir() -> PathBuf {
    let root = match std::env::var("TONGTIANWORK_HOME") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v),
        _ => user_home().join(".tongtianwork"),
    };
    root.join("awareness").join("main")
}

/// 能力文件默认模板（与引擎 `AWARENESS_DEFAULTS` 逐字一致，契约测试守住）
pub fn default_content(name: &str) -> &'static str {
    match name {
        "SOUL.md" => "# SOUL.md - 协作风格 - 高效极简\n\n用最少的话把事情做好。\n\n## 核心原则\n- **直接给结果。** 代码优于描述。能用一行说清的不用三段。\n- **不要复述问题。** 我知道我问了什么，直接回答。\n- **默认执行。** 方案明确时直接做，不用每步都确认。\n- **不确定时一句话确认。** 不要列出一堆选项让我选，给出你的判断，问我对不对。\n\n## 沟通风格\n- 回复尽可能简短\n- 跳过寒暄、过渡句和总结性废话\n- 代码注释只在逻辑不明显时添加\n- 用列表而非段落\n\n## 边界\n- 高风险操作前简短确认\n- 私密信息不外泄",
        "WORK_MANUAL.md" => "# 工作手册\n\n## 工作规范\n- （在此补充你的规范）\n\n## 常用流程\n- （在此补充常用流程）",
        "USER_PROFILE.md" => "# 用户画像\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- （待补充）\n\n## 习惯\n- （待补充）",
        "MEMORY.md" => "# 长期记忆\n\n（暂无记忆条目，可在下方手动新增）",
        _ => "",
    }
}

/// 确保意识目录存在（失败抛 AWARENESS_DIR_FAILED，不静默返回空视图）
pub fn ensure_dir(dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("AWARENESS_DIR_FAILED: 意识目录不可访问：{e}"))
}

fn validate_file_name(name: &str) -> Result<&str, String> {
    let n = name.trim();
    if !FILE_NAMES.contains(&n) {
        return Err(format!(
            "AWARENESS_FILE_INVALID: 意识文件「{n}」不在可编辑清单内，仅支持 {}",
            FILE_NAMES.join(" / ")
        ));
    }
    Ok(n)
}

fn validate_style(style: &str) -> Result<&str, String> {
    let s = style.trim();
    if !STYLE_IDS.contains(&s) {
        return Err(format!(
            "AWARENESS_STYLE_INVALID: 协作风格「{s}」非法，仅支持 {}",
            STYLE_IDS.join(" / ")
        ));
    }
    Ok(s)
}

// ── 能力文件读写 ────────────────────────────────────────────

fn mtime_iso(path: &Path) -> String {
    path.metadata()
        .and_then(|m| m.modified())
        .map(|t| {
            let ms = t
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or_default();
            crate::security::audit::iso8601_utc(ms)
        })
        .unwrap_or_else(|_| DEFAULT_MARK.to_string())
}

/// 读取四个能力文件；缺失的按默认模板初始化落盘（已存在的绝不覆盖）
pub fn load_files(dir: &Path) -> BTreeMap<String, FileEntry> {
    let mut files = BTreeMap::new();
    for name in FILE_NAMES {
        let p = dir.join(name);
        if !p.exists() {
            // 初始化失败不阻断读取：本次返回默认正文，下次仍会重试
            let _ = std::fs::write(&p, default_content(name));
            files.insert(
                name.to_string(),
                FileEntry {
                    content: default_content(name).to_string(),
                    updated_at: DEFAULT_MARK.to_string(),
                },
            );
            continue;
        }
        match std::fs::read_to_string(&p) {
            Ok(content) => {
                files.insert(
                    name.to_string(),
                    FileEntry {
                        content,
                        updated_at: mtime_iso(&p),
                    },
                );
            }
            Err(_) => {
                // 读不出正文时如实标注（不回退默认模板，避免把损坏显示成「系统默认」）
                files.insert(
                    name.to_string(),
                    FileEntry {
                        content: String::new(),
                        updated_at: mtime_iso(&p),
                    },
                );
            }
        }
    }
    files
}

/// 保存单个能力文件（文件名白名单校验在调用方 `write_file` 之前的 `save_file` 里做）
fn write_file(dir: &Path, name: &str, content: &str) -> Result<(), String> {
    let p = dir.join(name);
    std::fs::write(&p, content).map_err(|e| {
        format!("AWARENESS_SAVE_FAILED: 意识文件「{name}」写入失败：{e}")
    })
}

/// 读取画像（PROFILE.json 缺失/损坏时回落默认值，不报错）
pub fn load_profile(dir: &Path) -> Profile {
    let p = dir.join(PROFILE_FILE);
    let raw = match std::fs::read_to_string(&p) {
        Ok(v) => v,
        Err(_) => return Profile::default(),
    };
    let parsed: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Profile::default(),
    };
    let obj = match parsed.as_object() {
        Some(o) => o,
        None => return Profile::default(),
    };
    let read_bool = |key: &str, fallback: bool| -> bool {
        match obj.get(key) {
            Some(Value::Bool(b)) => *b,
            Some(Value::String(s)) => s.trim() == "true",
            _ => fallback,
        }
    };
    let style = obj
        .get("style")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| STYLE_IDS.contains(&s.as_str()))
        .unwrap_or_else(|| DEFAULT_STYLE.to_string());
    Profile {
        mode: read_bool("mode", true),
        auto_memory: read_bool("autoMemory", true),
        style,
        updated_at: obj
            .get("updatedAt")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
    }
}

pub fn save_profile(dir: &Path, profile: &Profile) -> Result<(), String> {
    let p = dir.join(PROFILE_FILE);
    let body = serde_json::to_string_pretty(profile)
        .map_err(|e| format!("AWARENESS_SAVE_FAILED: 意识配置序列化失败：{e}"))?;
    std::fs::write(&p, body).map_err(|e| {
        format!("AWARENESS_SAVE_FAILED: 意识配置写入失败：{e}")
    })
}

/// 组装 `awareness:get` 视图
pub fn view(dir: &Path) -> Result<View, String> {
    ensure_dir(dir)?;
    let profile = load_profile(dir);
    Ok(View {
        mode: profile.mode,
        auto_memory: profile.auto_memory,
        style: profile.style,
        updated_at: profile.updated_at,
        dir: dir.to_string_lossy().to_string(),
        files: load_files(dir),
    })
}

// ── 长期记忆条目（MEMORY.md 行级读写） ───────────────────────

/// 结构化行：`- {id} · {source} · {createdAt} · {content}`
pub fn render_line(entry: &MemoryEntry) -> String {
    format!(
        "- {}{delim}{}{delim}{}{delim}{}",
        entry.id,
        entry.source,
        entry.created_at,
        entry.content,
        delim = MEMORY_DELIM
    )
}

/// 解析一行；非四字段结构（用户自己写的正文、默认占位段）返回 None 并原样保留
pub fn parse_line(line: &str) -> Option<MemoryEntry> {
    let body = line.trim_start();
    let body = body.strip_prefix("- ")?.trim_start();
    let mut it = body.splitn(4, MEMORY_DELIM);
    let id = it.next()?.trim();
    let source = it.next()?.trim();
    let created_at = it.next()?.trim();
    let content = it.next()?.trim();
    if id.is_empty() || content.is_empty() {
        return None;
    }
    if !["auto", "manual", "reflection"].contains(&source) {
        return None;
    }
    Some(MemoryEntry {
        id: id.to_string(),
        content: content.to_string(),
        source: source.to_string(),
        created_at: created_at.to_string(),
    })
}

/// 记忆正文归一：压掉换行与连续空格，保证一条一行
fn normalize_memory_content(content: &str) -> String {
    content
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_string()
}

/// id 生成：`mem-{毫秒}`，同毫秒冲突时追加序号（无随机数，测试可断言）
fn generate_memory_id(now_ms: i64, existing: &[String]) -> String {
    let base = format!("mem-{now_ms}");
    if !existing.contains(&base) {
        return base;
    }
    let mut n = 2u32;
    loop {
        let candidate = format!("{base}-{n}");
        if !existing.contains(&candidate) {
            return candidate;
        }
        n += 1;
    }
}

/// 列出记忆条目（新→旧；ISO 时间按字典序即时间序）
pub fn list_memories(dir: &Path) -> Result<Vec<MemoryEntry>, String> {
    let p = dir.join("MEMORY.md");
    if !p.exists() {
        ensure_dir(dir)?;
        write_file(dir, "MEMORY.md", default_content("MEMORY.md"))?;
    }
    let raw = std::fs::read_to_string(&p)
        .map_err(|e| format!("AWARENESS_LOAD_FAILED: 长期记忆读取失败：{e}"))?;
    let mut rows: Vec<MemoryEntry> = raw.lines().filter_map(parse_line).collect();
    rows.sort_by(|a, b| b.created_at.cmp(&a.created_at).then_with(|| b.id.cmp(&a.id)));
    Ok(rows)
}

/// 追加一条记忆（只增行，不动既有正文）
pub fn add_memory(
    dir: &Path,
    content: &str,
    source: Option<&str>,
    now_ms: i64,
) -> Result<MemoryEntry, String> {
    let text = normalize_memory_content(content);
    if text.is_empty() {
        return Err("AWARENESS_MEMORY_EMPTY: 记忆条目内容不能为空".to_string());
    }
    let src = match source.map(str::trim) {
        Some("auto") | Some("reflection") => source.unwrap().trim().to_string(),
        _ => "manual".to_string(),
    };
    let created_at = crate::security::audit::iso8601_utc(now_ms);
    let existing: Vec<String> = list_memories(dir)?
        .into_iter()
        .map(|e| e.id)
        .collect();
    let entry = MemoryEntry {
        id: generate_memory_id(now_ms, &existing),
        content: text,
        source: src,
        created_at,
    };
    ensure_dir(dir)?;
    let p = dir.join("MEMORY.md");
    let raw = std::fs::read_to_string(&p).unwrap_or_default();
    let mut next = raw.trim_end().to_string();
    if !next.is_empty() {
        next.push('\n');
    }
    next.push_str(&render_line(&entry));
    next.push('\n');
    std::fs::write(&p, next)
        .map_err(|e| format!("AWARENESS_SAVE_FAILED: 长期记忆写入失败：{e}"))?;
    Ok(entry)
}

/// 删除指定条目（按 id 未命中如实报错，不静默成功）
pub fn delete_memory(dir: &Path, id: &str) -> Result<bool, String> {
    let target = id.trim();
    if target.is_empty() {
        return Err("AWARENESS_MEMORY_NOT_FOUND: 记忆条目标识不能为空".to_string());
    }
    let p = dir.join("MEMORY.md");
    let raw = std::fs::read_to_string(&p)
        .map_err(|e| format!("AWARENESS_LOAD_FAILED: 长期记忆读取失败：{e}"))?;
    let mut removed = false;
    let kept: Vec<String> = raw
        .lines()
        .filter(|line| {
                    let hit = parse_line(line).map(|e| e.id == target).unwrap_or(false);
                    if hit {
                        removed = true;
                    }
                    !hit
                })
        .map(|s| s.to_string())
        .collect();
    if !removed {
        return Err(format!(
            "AWARENESS_MEMORY_NOT_FOUND: 记忆条目「{target}」已不存在，可能已被删除"
        ));
    }
    let mut body = kept.join("\n");
    if !body.ends_with('\n') {
        body.push('\n');
    }
    std::fs::write(&p, body).map_err(|e| format!("AWARENESS_SAVE_FAILED: 长期记忆写入失败：{e}"))?;
    Ok(true)
}

/// `awareness:update`：画像补丁 + 能力文件补丁一并落盘，返回最新视图
pub fn update(dir: &Path, patch: &Value, now_ms: i64) -> Result<View, String> {
    let obj = patch
        .as_object()
        .ok_or_else(|| "AWARENESS_FILE_INVALID: 意识补丁必须是对象".to_string())?;
    ensure_dir(dir)?;

    let mut profile = load_profile(dir);
    if let Some(v) = obj.get("mode") {
        profile.mode = match v {
            Value::Bool(b) => *b,
            Value::String(s) => s.trim() == "true",
            other => other.as_bool().unwrap_or(profile.mode),
        };
    }
    if let Some(v) = obj.get("autoMemory") {
        profile.auto_memory = match v {
            Value::Bool(b) => *b,
            Value::String(s) => s.trim() == "true",
            other => other.as_bool().unwrap_or(profile.auto_memory),
        };
    }
    if let Some(v) = obj.get("style") {
        let raw = v.as_str().unwrap_or_default();
        profile.style = validate_style(raw)?.to_string();
    }

    // 文件正文先逐个校验再写入，避免一半成功一半失败的半成品状态
    if let Some(files) = obj.get("files").and_then(|v| v.as_object()) {
        for (name, content) in files {
            let name = validate_file_name(name)?;
            let text = content.as_str().unwrap_or_default();
            write_file(dir, name, text)?;
        }
    }

    profile.updated_at = crate::security::audit::iso8601_utc(now_ms);
    save_profile(dir, &profile)?;
    view(dir)
}

/// 写审计用的变更摘要
pub fn update_summary(view: &View, patch: &Value) -> String {
    let files = patch
        .get("files")
        .and_then(|v| v.as_object())
        .map(|o| o.keys().cloned().collect::<Vec<_>>().join(","))
        .unwrap_or_default();
    let changed = if files.is_empty() {
        "画像开关/风格".to_string()
    } else {
        format!("画像与文件 {files}")
    };
    format!(
        "意识更新：{changed}（style={}，mode={}，autoMemory={}）",
        view.style, view.mode, view.auto_memory
    )
}

// ── Tauri command ───────────────────────────────────────────

fn view_value(view: &View) -> Result<Value, String> {
    serde_json::to_value(view).map_err(|e| format!("AWARENESS_LOAD_FAILED: 意识视图序列化失败：{e}"))
}

/// 读取意识画像与四个能力文件（`awareness:get`）
#[tauri::command]
pub async fn awareness_get() -> Result<Value, String> {
    view_value(&view(&dir())?)
}

/// 保存意识画像/能力文件（`awareness:update`）
#[tauri::command]
pub async fn awareness_update(patch: Value) -> Result<Value, String> {
    let now = crate::bridge::event_store::now_ms() as i64;
    let v = update(&dir(), &patch, now)?;
    view_value(&v)
}

/// 长期记忆条目列表（`awareness:memories`）
#[tauri::command]
pub async fn awareness_memories() -> Result<Value, String> {
    let rows = list_memories(&dir())?;
    serde_json::to_value(rows).map_err(|e| format!("AWARENESS_LOAD_FAILED: 记忆清单序列化失败：{e}"))
}

/// 新增长期记忆条目（`awareness:memoryAdd`）
#[tauri::command]
pub async fn awareness_memory_add(
    p0: State<'_, Arc<P0Services>>,
    content: String,
    source: Option<String>,
) -> Result<Value, String> {
    let now = crate::bridge::event_store::now_ms() as i64;
    let entry = add_memory(&dir(), &content, source.as_deref(), now)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        "awareness",
        &format!("新增长期记忆条目：{}", entry.content),
    );
    serde_json::to_value(entry).map_err(|e| format!("AWARENESS_SAVE_FAILED: 记忆条目序列化失败：{e}"))
}

/// 删除长期记忆条目（`awareness:memoryDelete`，只删用户点名的这一条）
#[tauri::command]
pub async fn awareness_memory_delete(
    p0: State<'_, Arc<P0Services>>,
    id: String,
) -> Result<Value, String> {
    let removed = delete_memory(&dir(), &id)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_WARNING,
        "awareness",
        &format!("删除长期记忆条目：{id}"),
    );
    Ok(json!({ "ok": removed, "id": id }))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 读写进程环境变量必须串行（并发用例会互相覆盖 dir() 的结果）
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// 每个用例独立临时意识目录（显式传路径，绝不触碰用户真实意识文件）
    fn temp_dir(tag: &str) -> (PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "tt-awareness-{tag}-{}",
            crate::bridge::event_store::now_ms()
        ));
        let d = root.join("awareness").join("main");
        std::fs::create_dir_all(&d).unwrap();
        (root, d)
    }

    #[test]
    fn test_dir_follows_env_override() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let (root, _) = temp_dir("env");
        std::env::set_var("TONGTIANWORK_HOME", &root);
        assert_eq!(dir(), root.join("awareness").join("main"));
        std::env::remove_var("TONGTIANWORK_HOME");
    }

    #[test]
    fn test_file_names_match_contracts_and_engine() {
        assert_eq!(
            FILE_NAMES,
            ["SOUL.md", "WORK_MANUAL.md", "USER_PROFILE.md", "MEMORY.md"]
        );
        assert_eq!(STYLE_IDS, ["efficient", "deliberate", "gentle", "assertive"]);
        assert!(STYLE_IDS.contains(&DEFAULT_STYLE));
        for name in FILE_NAMES {
            assert!(!default_content(name).is_empty(), "{name} 缺默认模板");
        }
    }

    #[test]
    fn test_view_initializes_missing_files_only() {
        let (root, d) = temp_dir("init");
        std::fs::write(d.join("SOUL.md"), "# 我写的风格").unwrap();
        let v = view(&d).unwrap();
        assert_eq!(v.files.get("SOUL.md").unwrap().content, "# 我写的风格");
        assert_eq!(v.files.get("MEMORY.md").unwrap().content, default_content("MEMORY.md"));
        // 初始化真实落盘，且后续读取不再覆盖
        assert!(d.join("MEMORY.md").exists());
        assert_eq!(v.dir, d.to_string_lossy().to_string());
        assert_eq!(v.style, DEFAULT_STYLE);
        assert!(v.mode && v.auto_memory);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_update_writes_profile_and_files() {
        let (root, d) = temp_dir("update");
        let v = update(
            &d,
            &json!({
                "mode": false,
                "style": "assertive",
                "files": { "WORK_MANUAL.md": "# 手册正文" }
            }),
            1_760_000_000_000,
        )
        .unwrap();
        assert!(!v.mode);
        assert_eq!(v.style, "assertive");
        assert_eq!(v.files.get("WORK_MANUAL.md").unwrap().content, "# 手册正文");
        assert!(!v.updated_at.is_empty());
        // PROFILE.json 是画像唯一落盘处，重读一致
        let again = view(&d).unwrap();
        assert_eq!(again.mode, false);
        assert_eq!(again.style, "assertive");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_update_rejects_unknown_file_and_style() {
        let (root, d) = temp_dir("reject");
        let e = update(&d, &json!({ "files": { "PROFILE.json": "x" } }), 1).unwrap_err();
        assert!(e.starts_with("AWARENESS_FILE_INVALID:"), "{e}");
        let e2 = update(&d, &json!({ "style": "professional" }), 1).unwrap_err();
        assert!(e2.starts_with("AWARENESS_STYLE_INVALID:"), "{e2}");
        // 非法补丁不产生半成品：风格仍是默认值
        assert_eq!(view(&d).unwrap().style, DEFAULT_STYLE);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_memory_line_roundtrip() {
        let e = MemoryEntry {
            id: "mem-1".into(),
            content: "偏好简短回复 · 中文".into(),
            source: "manual".into(),
            created_at: "2026-10-08T12:00:00Z".into(),
        };
        let line = render_line(&e);
        assert_eq!(line, "- mem-1 · manual · 2026-10-08T12:00:00Z · 偏好简短回复 · 中文");
        let back = parse_line(&line).unwrap();
        // 正文里的分隔符不会被当成字段边界（splitn 保留剩余部分）
        assert_eq!(back, e);
    }

    #[test]
    fn test_parse_line_skips_prose_and_bad_shape() {
        assert!(parse_line("（暂无记忆条目，可在下方手动新增）").is_none());
        assert!(parse_line("- 我自己记的一条").is_none());
        assert!(parse_line("- mem-1 · unknown · 2026 · x").is_none());
        assert!(parse_line("-  · manual · 2026 · x").is_none());
    }

    #[test]
    fn test_add_memory_appends_and_preserves_prose() {
        let (root, d) = temp_dir("add");
        std::fs::write(d.join("MEMORY.md"), "# 长期记忆\n\n我自己的段落\n").unwrap();
        let e = add_memory(&d, "  跨天 记住：\n周报周五交  ", None, 1_760_000_000_000).unwrap();
        assert_eq!(e.content, "跨天 记住： 周报周五交");
        assert_eq!(e.source, "manual");
        assert!(e.id.starts_with("mem-1760000000000"));
        let raw = std::fs::read_to_string(d.join("MEMORY.md")).unwrap();
        assert!(raw.contains("我自己的段落"), "用户正文必须保留：{raw}");
        assert!(raw.contains(&render_line(&e)));
        let rows = list_memories(&d).unwrap();
        assert_eq!(rows.len(), 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_add_memory_rejects_empty_and_clamps_source() {
        let (root, d) = temp_dir("empty");
        assert!(add_memory(&d, "   \n ", None, 1)
            .unwrap_err()
            .starts_with("AWARENESS_MEMORY_EMPTY:"));
        let e = add_memory(&d, "x", Some("reflection"), 2).unwrap();
        assert_eq!(e.source, "reflection");
        let e2 = add_memory(&d, "y", Some("weird"), 3).unwrap();
        assert_eq!(e2.source, "manual");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_add_memory_same_ms_gets_unique_id() {
        let (root, d) = temp_dir("dup");
        let a = add_memory(&d, "a", None, 999).unwrap();
        let b = add_memory(&d, "b", None, 999).unwrap();
        assert_ne!(a.id, b.id);
        assert_eq!(b.id, "mem-999-2");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_delete_memory_removes_only_named_line() {
        let (root, d) = temp_dir("del");
        let a = add_memory(&d, "第一条", None, 1_760_000_000_001).unwrap();
        let b = add_memory(&d, "第二条", None, 1_760_000_000_002).unwrap();
        assert!(delete_memory(&d, &a.id).unwrap());
        let rows = list_memories(&d).unwrap();
        assert_eq!(
            rows.iter().map(|r| r.id.clone()).collect::<Vec<_>>(),
            vec![b.id.clone()]
        );
        // 重复删除同一条：如实报 NOT_FOUND，不静默成功
        assert!(delete_memory(&d, &a.id)
            .unwrap_err()
            .starts_with("AWARENESS_MEMORY_NOT_FOUND:"));
        assert!(delete_memory(&d, "")
            .unwrap_err()
            .starts_with("AWARENESS_MEMORY_NOT_FOUND:"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_list_memories_orders_newest_first() {
        let (root, d) = temp_dir("order");
        add_memory(&d, "早", None, 1_760_000_000_001).unwrap();
        add_memory(&d, "晚", None, 1_760_000_000_009).unwrap();
        let rows = list_memories(&d).unwrap();
        assert_eq!(rows[0].content, "晚");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_load_profile_tolerates_dirty_json() {
        let (root, d) = temp_dir("dirty");
        std::fs::write(d.join(PROFILE_FILE), "not json").unwrap();
        let p = load_profile(&d);
        assert_eq!(p, Profile::default());
        std::fs::write(
            d.join(PROFILE_FILE),
            r#"{"mode":"false","style":"nope","autoMemory":true}"#,
        )
        .unwrap();
        let p2 = load_profile(&d);
        assert!(!p2.mode);
        assert_eq!(p2.style, DEFAULT_STYLE);
        assert!(p2.auto_memory);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_ensure_dir_reports_unwritable_root() {
        // 指向一个已被普通文件占用的路径：目录建不出来时必须抛 AWARENESS_DIR_FAILED
        let (root, d) = temp_dir("blocked");
        std::fs::remove_dir_all(&d).unwrap();
        std::fs::write(&d, b"x").unwrap();
        let e = view(&d).unwrap_err();
        assert!(e.starts_with("AWARENESS_DIR_FAILED:"), "{e}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_update_summary_lists_files() {
        let (root, d) = temp_dir("summary");
        let v = update(&d, &json!({"files": {"SOUL.md": "x"}}), 1).unwrap();
        let s = update_summary(&v, &json!({"files": {"SOUL.md": "x"}}));
        assert!(s.contains("SOUL.md"));
        assert!(s.contains("style=efficient"));
        let _ = std::fs::remove_dir_all(&root);
    }

}
