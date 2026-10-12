//! 专家套件（岗位工作流包）本地安装台账
//!
//! command：
//! - suite_root:      返回套件运行目录与回收目录（页面如实展示安装路径）
//! - suite_list:      列出 `{data_home}/suites/<id>/manifest.json` 真实存在的套件
//! - suite_install:   解析上传的 .zip 套件包（plugin.json 必填），解包并增量安装
//! - suite_uninstall: 把套件目录移入 `suites_removed/` 回收目录（不物理删除，可找回）
//!
//! 设计约束：
//! - manifest.json（由套件包 plugin.json 归一而来）即事实来源，不引入新表，无需迁移；
//! - 卸载采用「移入回收目录」而非删除，符合数据保护规范（严禁销毁存量数据）；
//! - 套件广场尚无远端来源，安装只接受本地 .zip 字节（前端 base64 透传），没有包就如实拒绝；
//! - 解包只落盘安全相对路径（拒绝绝对路径、盘符与 `..` 穿越），避免 zip 写穿数据目录；
//! - 离线构建无 zip crate，ZIP 解析复用 `file::office`（EOCD → 中央目录 → 本地头 + DEFLATE）；
//! - 纯函数接受 `base` 路径参数，便于用临时目录做单元测试，不触碰真实数据目录。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine as _;
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::State;

use crate::bridge::python_env;
use crate::file::office::{zip_entries, zip_read, ZipEntry};
use crate::security::audit;

/// 套件包体积上限（解压前，16MB）
const MAX_ZIP_BYTES: usize = 16 * 1024 * 1024;
/// 套件包条目数上限
const MAX_ENTRIES: usize = 256;
/// 单条目解压后上限（4MB）
const MAX_ENTRY_BYTES: u64 = 4 * 1024 * 1024;
/// 整包解压后上限（32MB），防 zip 炸弹
const MAX_TOTAL_BYTES: u64 = 32 * 1024 * 1024;
/// plugin.json 上限（256KB）
const MAX_MANIFEST_BYTES: u64 = 256 * 1024;
/// 套件标识最大长度（与技能标识同档）
const MAX_ID_LEN: usize = 64;
/// 目录遍历条目上限，防异常深层目录拖垮列表
const MAX_WALK_ENTRIES: usize = 2000;

/// 安装入参（与前端 SuiteInstallReq 同口径，camelCase）
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallReq {
    /// 原始文件名（仅用于审计与报错，落盘目录以 plugin.json 的 name 为准）
    pub file_name: Option<String>,
    /// zip 字节（base64，允许 data URL 前缀）
    pub zip_base64: Option<String>,
}

/// 套件目录根（运行目录下的 suites/，与技能目录同层）
pub fn suites_root(base: &Path) -> PathBuf {
    base.join("suites")
}

/// 回收目录根（卸载移入，不物理删除）
pub fn trash_root(base: &Path) -> PathBuf {
    base.join("suites_removed")
}

/// 套件目录信息（页面展示真实安装路径）
pub fn root_info(base: &Path) -> Value {
    json!({
        "supported": true,
        "suitesRoot": suites_root(base).to_string_lossy(),
        "trashRoot": trash_root(base).to_string_lossy(),
    })
}

fn incompatible(reason: impl std::fmt::Display) -> String {
    format!("SUITE_INCOMPATIBLE: {reason}")
}

fn install_failed(reason: impl std::fmt::Display) -> String {
    format!("SUITE_INSTALL_FAILED: {reason}")
}

/// 展示名 → 套件标识：ASCII 名称转写为 slug；含非 ASCII（如「金融行业套件」）
/// 时退回名称哈希，保证标识天然不含路径分隔符
pub fn suite_id_of(raw_name: &str) -> String {
    let name = raw_name.trim();
    if name.chars().all(|c| c.is_ascii()) {
        if let Ok(slug) = crate::skill::slugify(name) {
            if !slug.is_empty() && slug.len() <= MAX_ID_LEN {
                return slug;
            }
        }
    }
    // crc32fast 已在依赖内：哈希定长且稳定，同名套件得到同一标识
    let mut h = crc32fast::Hasher::new();
    h.update(name.as_bytes());
    format!("suite-{:08x}", h.finalize())
}

/// 校验套件标识（卸载与列表入参；标识由本地生成，字符集固定）
fn normalize_id(raw: &str) -> Result<String, String> {
    let id = raw.trim();
    if id.is_empty() || id.len() > MAX_ID_LEN {
        return Err(incompatible("套件标识为空或长度异常"));
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Err(incompatible(format!(
            "套件标识「{id}」仅支持小写字母、数字、下划线与连字符"
        )));
    }
    Ok(id.to_string())
}

/// 条目名 → 安全相对路径（拒绝绝对路径、盘符与 `..` 穿越；`.qoder-plugin/` 这类
/// 点号目录是套件包规范布局，必须允许）
fn safe_relative(name: &str) -> Option<PathBuf> {
    let normalized = name.replace('\\', "/");
    if normalized.is_empty() || normalized.starts_with('/') || normalized.contains(':') {
        return None;
    }
    let mut out = PathBuf::new();
    for seg in normalized.split('/') {
        match seg {
            "" | "." => continue,
            ".." => return None,
            s => out.push(s),
        }
    }
    if out.as_os_str().is_empty() {
        return None;
    }
    Some(out)
}

/// 在条目清单中定位 plugin.json（根目录或单层目录内，与页面「格式要求」同源）
fn find_manifest_entry(entries: &[ZipEntry]) -> Option<usize> {
    entries.iter().position(|e| {
        let n = e.name.trim_start_matches("./").trim_end_matches('/');
        let segs: Vec<&str> = n.split('/').filter(|s| !s.is_empty()).collect();
        segs.len() <= 2
            && segs
                .last()
                .map(|last| last.eq_ignore_ascii_case("plugin.json"))
                .unwrap_or(false)
    })
}

fn str_field(obj: &Value, key: &str) -> Option<String> {
    obj.get(key)
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// commands 兼容 `["weekly-report"]`、`[{name, description}]` 与 `{name: description}` 三种写法
fn parse_commands(obj: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    match obj.get("commands") {
        Some(Value::Array(items)) => {
            for it in items {
                match it {
                    Value::String(s) => {
                        let s = s.trim();
                        if !s.is_empty() {
                            out.push(json!({ "name": s, "description": null }));
                        }
                    }
                    Value::Object(m) => {
                        if let Some(name) = m.get("name").and_then(|v| v.as_str()).map(str::trim) {
                            if !name.is_empty() {
                                out.push(json!({
                                    "name": name,
                                    "description": m.get("description").and_then(|v| v.as_str()),
                                }));
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        Some(Value::Object(m)) => {
            for (name, desc) in m {
                let name = name.trim();
                if !name.is_empty() {
                    out.push(json!({ "name": name, "description": desc.as_str() }));
                }
            }
        }
        _ => {}
    }
    out
}

fn count_declared(obj: &Value, key: &str) -> usize {
    obj.get(key)
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter(|it| match it {
                    Value::String(s) => !s.trim().is_empty(),
                    Value::Object(m) => m.get("name").and_then(|v| v.as_str()).is_some(),
                    _ => false,
                })
                .count()
        })
        .unwrap_or(0)
}

/// 递归统计目录内文件数与 SKILL.md 数（条目数封顶，异常结构不影响列表）
fn walk(dir: &Path) -> (usize, usize) {
    let mut files = 0usize;
    let mut skills = 0usize;
    let mut stack = vec![dir.to_path_buf()];
    let mut seen = 0usize;
    while let Some(cur) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&cur) else { break };
        for entry in rd.filter_map(|e| e.ok()) {
            seen += 1;
            if seen > MAX_WALK_ENTRIES {
                return (files, skills);
            }
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.file_name().map(|n| n == "SKILL.md").unwrap_or(false) {
                files += 1;
                skills += 1;
            } else {
                files += 1;
            }
        }
    }
    (files, skills)
}

fn dir_mtime_ms(dir: &Path) -> u64 {
    dir.metadata()
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 套件目录 → 视图（manifest.json 缺失/损坏时 readable=false，页面据此提示重装）
pub fn dir_info(base: &Path, id: &str) -> Value {
    let dir = suites_root(base).join(id);
    let (file_count, skill_files) = walk(&dir);
    let installed_at = dir_mtime_ms(&dir);
    let manifest = std::fs::read_to_string(dir.join("manifest.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok());
    match manifest {
        Some(m) => {
            let name = str_field(&m, "name").unwrap_or_else(|| id.to_string());
            json!({
                "id": id,
                "name": name,
                "version": str_field(&m, "version").unwrap_or_else(|| "未标注版本".to_string()),
                "author": str_field(&m, "author"),
                "category": str_field(&m, "category"),
                "description": str_field(&m, "description"),
                "commands": parse_commands(&m),
                "skillCount": count_declared(&m, "skills").max(skill_files),
                "dataLinkCount": count_declared(&m, "dataLinks"),
                "fileCount": file_count,
                "installedAtMs": installed_at,
                "path": dir.to_string_lossy(),
                "readable": true,
                // 套件没有独立的启停开关：装上即可用，卸载即消失
                "enabled": true,
            })
        }
        None => json!({
            "id": id,
            "name": id,
            "version": "未知",
            "author": Value::Null,
            "category": Value::Null,
            "description": Value::Null,
            "commands": [],
            "skillCount": 0,
            "dataLinkCount": 0,
            "fileCount": file_count,
            "installedAtMs": installed_at,
            "path": dir.to_string_lossy(),
            // 台账文件读不出：如实标注，不编造套件内容
            "readable": false,
            "enabled": true,
        }),
    }
}

/// 列出已安装套件：仅返回目录下真实存在 manifest.json 的项，按安装时间倒序
pub fn list_in(base: &Path) -> Vec<Value> {
    let ids: Vec<String> = std::fs::read_dir(suites_root(base))
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .filter(|e| e.path().join("manifest.json").is_file())
                .filter_map(|e| e.file_name().to_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let mut rows: Vec<Value> = ids
        .iter()
        .filter(|id| normalize_id(id).is_ok())
        .map(|id| dir_info(base, id))
        .collect();
    rows.sort_by_key(|r| {
        std::cmp::Reverse(
            r.get("installedAtMs")
                .and_then(|v| v.as_u64())
                .unwrap_or_default(),
        )
    });
    rows
}

/// 解码 base64（容忍 data URL 前缀、换行与空白）
fn decode_zip(raw: &str) -> Result<Vec<u8>, String> {
    let body = raw
        .trim()
        .rsplit(',')
        .next()
        .unwrap_or_default()
        .replace(['\r', '\n', ' ', '\t'], "");
    if body.is_empty() {
        return Err(incompatible("未收到套件包内容"));
    }
    base64::engine::general_purpose::STANDARD
        .decode(body.as_bytes())
        .map_err(|e| incompatible(format!("套件包 base64 解码失败：{e}")))
}

/// 台账只保留页面需要的字段（包内额外字段不参与列表渲染）
fn clean_manifest(m: &Value) -> Value {
    let mut out = serde_json::Map::new();
    for key in ["name", "version", "author", "category", "description"] {
        if let Some(v) = str_field(m, key) {
            out.insert(key.to_string(), json!(v));
        }
    }
    if let Some(v) = m.get("commands") {
        out.insert("commands".to_string(), v.clone());
    }
    for key in ["skills", "dataLinks"] {
        if let Some(Value::Array(items)) = m.get(key) {
            out.insert(key.to_string(), Value::Array(items.clone()));
        }
    }
    Value::Object(out)
}

/// 解析并安装套件包（先解到暂存目录再整体落位；同名套件已存在时拒绝，不覆盖既有套件）
pub fn install_in(base: &Path, req: &InstallReq) -> Result<Value, String> {
    let zip_b64 = req
        .zip_base64
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| incompatible("未提供套件包内容（请上传 .zip）"))?;
    let bytes = decode_zip(zip_b64)?;
    if bytes.len() > MAX_ZIP_BYTES {
        return Err(incompatible(format!(
            "套件包超过 {}MB 上限（收到 {}KB）",
            MAX_ZIP_BYTES / 1024 / 1024,
            bytes.len() / 1024
        )));
    }
    let entries = zip_entries(&bytes).map_err(|e| incompatible(&e))?;
    if entries.is_empty() {
        return Err(incompatible("套件包内没有任何文件"));
    }
    if entries.len() > MAX_ENTRIES {
        return Err(incompatible(format!(
            "套件包条目过多（{} 项，上限 {}）",
            entries.len(),
            MAX_ENTRIES
        )));
    }
    let idx = find_manifest_entry(&entries)
        .ok_or_else(|| incompatible("套件包缺少 plugin.json，应位于根目录或单层目录内"))?;
    let manifest_bytes = zip_read(&bytes, &entries[idx]).map_err(|e| incompatible(&e))?;
    if manifest_bytes.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(incompatible("plugin.json 超过 256KB 上限"));
    }
    let manifest: Value = serde_json::from_slice(&manifest_bytes)
        .map_err(|e| incompatible(format!("plugin.json 不是合法 JSON：{e}")))?;
    let name =
        str_field(&manifest, "name").ok_or_else(|| incompatible("plugin.json 缺少非空的 name 字段"))?;
    let id = suite_id_of(&name);
    let target = suites_root(base).join(&id);
    if target.join("manifest.json").exists() {
        return Err(install_failed(format!(
            "套件「{name}」已安装，请先卸载后再安装（不覆盖既有套件）"
        )));
    }

    let staging = suites_root(base).join(format!(
        ".staging-{id}-{}",
        crate::bridge::event_store::now_ms()
    ));
    let fail = |err: String, staging: &Path| {
        let _ = std::fs::remove_dir_all(staging);
        err
    };
    std::fs::create_dir_all(&staging)
        .map_err(|e| install_failed(format!("无法创建套件暂存目录：{e}")))?;

    let mut total: u64 = 0;
    let mut written = 0usize;
    for entry in &entries {
        let rel = match safe_relative(&entry.name) {
            Some(rel) => rel,
            None => {
                return Err(fail(
                    incompatible(format!("套件包内含不安全路径：{}", entry.name)),
                    &staging,
                ))
            }
        };
        if entry.uncomp_size > MAX_ENTRY_BYTES {
            return Err(fail(
                incompatible(format!("套件包内 {} 解压后超过 4MB 上限", entry.name)),
                &staging,
            ));
        }
        total += entry.uncomp_size;
        if total > MAX_TOTAL_BYTES {
            return Err(fail(incompatible("套件包解压后总大小超过 32MB 上限"), &staging));
        }
        if entry.name.ends_with('/') {
            continue;
        }
        let data = match zip_read(&bytes, entry) {
            Ok(d) => d,
            Err(e) => return Err(fail(incompatible(&e), &staging)),
        };
        if data.len() as u64 > MAX_ENTRY_BYTES {
            return Err(fail(
                incompatible(format!("套件包内 {} 解压后超过 4MB 上限", entry.name)),
                &staging,
            ));
        }
        let dest = staging.join(&rel);
        if let Some(parent) = dest.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| install_failed(format!("无法创建 {}：{e}", parent.display())))?;
        }
        std::fs::write(&dest, &data)
            .map_err(|e| install_failed(format!("写入 {} 失败：{e}", dest.display())))?;
        written += 1;
    }
    // 归一化台账：字段固定，列表读取不必再猜包内路径
    std::fs::write(
        staging.join("manifest.json"),
        serde_json::to_vec_pretty(&clean_manifest(&manifest))
            .map_err(|e| install_failed(format!("套件台账生成失败：{e}")))?,
    )
    .map_err(|e| install_failed(format!("写入 manifest.json 失败：{e}")))?;
    if written == 0 {
        return Err(fail(incompatible("套件包内没有可安装的文件"), &staging));
    }
    if target.exists() {
        return Err(fail(
            install_failed(format!("套件目录已存在：{id}")),
            &staging,
        ));
    }
    std::fs::create_dir_all(suites_root(base))
        .map_err(|e| install_failed(format!("无法创建套件目录：{e}")))?;
    std::fs::rename(&staging, &target)
        .map_err(|e| install_failed(format!("套件目录落位失败：{e}")))?;
    // 视图统一走目录读取路径，保证安装回执与列表口径一致
    Ok(dir_info(base, &id))
}

/// 卸载套件：移入 `{data_home}/suites_removed/{id}-{时间戳}`，不物理删除
pub fn uninstall_in(base: &Path, raw_id: &str) -> Result<Value, String> {
    let id = normalize_id(raw_id)?;
    let dir = suites_root(base).join(&id);
    if !dir.is_dir() {
        return Err(format!("SUITE_NOT_FOUND: 套件目录不存在：{id}"));
    }
    std::fs::create_dir_all(trash_root(base))
        .map_err(|e| install_failed(format!("无法创建套件回收目录：{e}")))?;
    let ts = crate::bridge::event_store::now_ms();
    let trash = trash_root(base).join(format!("{id}-{ts}"));
    std::fs::rename(&dir, &trash)
        .map_err(|e| install_failed(format!("移出套件目录 {} 失败：{e}", dir.display())))?;
    Ok(json!({ "ok": true, "removed": id, "trashPath": trash.to_string_lossy() }))
}

// ── Tauri command ──────────────────────────────────────────────

/// 套件运行目录信息（安装路径 / 回收目录，供页面如实展示）
#[tauri::command]
pub async fn suite_root() -> Result<Value, String> {
    Ok(root_info(&python_env::base_dir()))
}

/// 列出已安装套件（真实扫描运行目录下的 suites/）
#[tauri::command]
pub async fn suite_list() -> Result<Value, String> {
    Ok(Value::Array(list_in(&python_env::base_dir())))
}

/// 安装套件（本地 .zip 字节；解包 + 归一台账）
#[tauri::command]
pub async fn suite_install(
    p0: State<'_, Arc<crate::P0Services>>,
    req: InstallReq,
) -> Result<Value, String> {
    let base = python_env::base_dir();
    let file_name = req
        .file_name
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("套件包")
        .to_string();
    let view = install_in(&base, &req)?;
    let id = view.get("id").and_then(|v| v.as_str()).unwrap_or("");
    let name = view
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or(id)
        .to_string();
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        id,
        &format!("安装专家套件：{name}（来源 {file_name}）"),
    );
    Ok(view)
}

/// 卸载套件（移入回收目录，可人工找回）
#[tauri::command]
pub async fn suite_uninstall(
    p0: State<'_, Arc<crate::P0Services>>,
    suite_id: String,
) -> Result<Value, String> {
    let res = uninstall_in(&python_env::base_dir(), &suite_id)?;
    let id = res.get("removed").and_then(|v| v.as_str()).unwrap_or("");
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        id,
        &format!("卸载专家套件：{id}"),
    );
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_base(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "tt-suite-{tag}-{}-{}",
            std::process::id(),
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    // ── 造包：STORE 直存的 ZIP（结构与真实套件包一致，仅不压缩）──

    fn u16le(v: u16) -> Vec<u8> {
        v.to_le_bytes().to_vec()
    }

    fn u32le(v: u32) -> Vec<u8> {
        v.to_le_bytes().to_vec()
    }

    fn crc(data: &[u8]) -> u32 {
        let mut h = crc32fast::Hasher::new();
        h.update(data);
        h.finalize()
    }

    /// 由 (路径, 内容) 构造 method=0（STORE）的 zip
    fn build_zip(files: &[(&str, &[u8])]) -> Vec<u8> {
        let mut body: Vec<u8> = Vec::new();
        let mut centrals: Vec<u8> = Vec::new();
        let mut count = 0u16;
        for (name, data) in files {
            let local_offset = body.len() as u32;
            let n = name.as_bytes();
            let c = crc(data);
            body.extend(u32le(0x04034b50));
            body.extend(u16le(20));
            body.extend(u16le(0));
            body.extend(u16le(0)); // method STORE
            body.extend(u16le(0));
            body.extend(u16le(0));
            body.extend(u32le(c));
            body.extend(u32le(data.len() as u32));
            body.extend(u32le(data.len() as u32));
            body.extend(u16le(n.len() as u16));
            body.extend(u16le(0));
            body.extend(n);
            body.extend(*data);

            centrals.extend(u32le(0x02014b50));
            centrals.extend(u16le(20));
            centrals.extend(u16le(20));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u32le(c));
            centrals.extend(u32le(data.len() as u32));
            centrals.extend(u32le(data.len() as u32));
            centrals.extend(u16le(n.len() as u16));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u16le(0));
            centrals.extend(u32le(0));
            centrals.extend(u32le(local_offset));
            centrals.extend(n);
            count += 1;
        }
        let cd_offset = body.len() as u32;
        body.extend(&centrals);
        body.extend(u32le(0x06054b50));
        body.extend(u16le(0));
        body.extend(u16le(0));
        body.extend(u16le(count));
        body.extend(u16le(count));
        body.extend(u32le(centrals.len() as u32));
        body.extend(u32le(cd_offset));
        body.extend(u16le(0));
        body
    }

    fn req(zip: &[u8]) -> InstallReq {
        InstallReq {
            file_name: Some("demo.zip".to_string()),
            zip_base64: Some(base64::engine::general_purpose::STANDARD.encode(zip)),
        }
    }

    fn plugin(name: &str) -> Vec<u8> {
        json!({
            "name": name,
            "version": "v1.0.0",
            "author": "@测试作者",
            "category": "财务",
            "description": "风控与合规流程套件",
            "commands": [{ "name": "weekly-report", "description": "生成周报" }],
            "skills": ["weekly-report"],
            "dataLinks": ["finance-db"],
        })
        .to_string()
        .into_bytes()
    }

    // ── 标识与路径安全 ──────────────────────────────────────

    #[test]
    fn ascii_name_becomes_slug() {
        assert_eq!(suite_id_of("Prompt Design Studio"), "prompt-design-studio");
    }

    #[test]
    fn chinese_name_falls_back_to_stable_hash_id() {
        // 与前端 suite-zip.ts 的 suiteIdOf 同口径（CRC-32 of UTF-8，小写 8 位十六进制）：
        // 两端算出同一个 id，桌面端与浏览器台账才能对上同一个套件。
        assert_eq!(suite_id_of("金融行业套件"), "suite-ac1b217b");
        assert_eq!(suite_id_of("法务合同审查包"), "suite-184f497e");
        let a = suite_id_of("金融行业套件");
        assert!(a.starts_with("suite-") && a.len() == 14);
        assert_eq!(a, suite_id_of("金融行业套件"));
        assert_ne!(a, suite_id_of("法律合规套件"));
    }

    #[test]
    fn normalize_id_rejects_path_separators() {
        for bad in ["a/b", "..", "a b", "", &"x".repeat(MAX_ID_LEN + 1)] {
            assert!(
                normalize_id(bad).unwrap_err().starts_with("SUITE_INCOMPATIBLE"),
                "{bad}"
            );
        }
        assert_eq!(normalize_id(" finance-suite ").unwrap(), "finance-suite");
    }

    #[test]
    fn safe_relative_blocks_traversal_and_absolute() {
        assert_eq!(safe_relative("a/b.txt"), Some(PathBuf::from("a/b.txt")));
        assert_eq!(
            safe_relative("./a/b.txt"),
            Some(PathBuf::from("a/b.txt"))
        );
        // 点号目录是套件包规范布局，必须放行
        assert_eq!(
            safe_relative(".qoder-plugin/plugin.json"),
            Some(PathBuf::from(".qoder-plugin/plugin.json"))
        );
        assert!(safe_relative("../evil.txt").is_none());
        assert!(safe_relative("a/../../evil.txt").is_none());
        assert!(safe_relative("/etc/passwd").is_none());
        assert!(safe_relative("C:/windows/a.txt").is_none());
        assert!(safe_relative("").is_none());
        assert!(safe_relative("./").is_none());
    }

    #[test]
    fn manifest_locator_accepts_root_and_single_dir_only() {
        let zip = build_zip(&[(".claudef-plugin/plugin.json", b"{}")]);
        assert_eq!(find_manifest_entry(&zip_entries(&zip).unwrap()), Some(0));
        let flat = build_zip(&[("plugin.json", b"{}")]);
        assert_eq!(find_manifest_entry(&zip_entries(&flat).unwrap()), Some(0));
        let nested = build_zip(&[("a/b/c/plugin.json", b"{}")]);
        assert_eq!(find_manifest_entry(&zip_entries(&nested).unwrap()), None);
    }

    #[test]
    fn commands_accept_string_object_and_map_shapes() {
        assert_eq!(
            parse_commands(&json!({ "commands": ["a", " "] }))[0]["name"],
            json!("a")
        );
        assert_eq!(
            parse_commands(&json!({ "commands": [{ "name": "a", "description": "说明" }] }))[0]
                ["description"],
            json!("说明")
        );
        assert_eq!(
            parse_commands(&json!({ "commands": { "b": "说明" } }))[0]["name"],
            json!("b")
        );
        assert!(parse_commands(&json!({ "commands": 5 })).is_empty());
    }

    // ── 安装 ────────────────────────────────────────────────

    #[test]
    fn install_unzips_and_writes_normalized_manifest() {
        let base = temp_base("install");
        let zip = build_zip(&[
            (".qoder-plugin/plugin.json", &plugin("finance-suite")),
            (
                ".qoder-plugin/skills/weekly-report/SKILL.md",
                "---\nname: weekly-report\n---\n正文\n".as_bytes(),
            ),
        ]);
        let view = install_in(&base, &req(&zip)).unwrap();
        assert_eq!(view["id"], json!("finance-suite"));
        assert_eq!(view["name"], json!("finance-suite"));
        assert_eq!(view["version"], json!("v1.0.0"));
        assert_eq!(view["category"], json!("财务"));
        assert_eq!(view["skillCount"], json!(1));
        assert_eq!(view["dataLinkCount"], json!(1));
        assert_eq!(view["fileCount"], json!(3));
        assert_eq!(view["commands"][0]["name"], json!("weekly-report"));
        assert_eq!(view["readable"], json!(true));
        let dir = suites_root(&base).join("finance-suite");
        assert!(dir.join("manifest.json").is_file());
        assert!(dir.join(".qoder-plugin/plugin.json").is_file());
        assert!(dir.join(".qoder-plugin/skills/weekly-report/SKILL.md").is_file());
        // 台账为归一字段，不含包内自定义键
        let stored: Value =
            serde_json::from_str(&std::fs::read_to_string(dir.join("manifest.json")).unwrap())
                .unwrap();
        assert_eq!(stored["name"], json!("finance-suite"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_rejects_missing_payload_and_bad_base64() {
        let base = temp_base("empty-req");
        assert!(install_in(
            &base,
            &InstallReq {
                file_name: None,
                zip_base64: None,
            }
        )
        .unwrap_err()
        .starts_with("SUITE_INCOMPATIBLE"));
        assert!(install_in(
            &base,
            &InstallReq {
                file_name: None,
                zip_base64: Some("!!!not-base64!!!".to_string()),
            }
        )
        .unwrap_err()
        .starts_with("SUITE_INCOMPATIBLE"));
        assert!(!suites_root(&base).exists());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_requires_plugin_json() {
        let base = temp_base("no-manifest");
        let zip = build_zip(&[("readme.md", b"hi")]);
        let err = install_in(&base, &req(&zip)).unwrap_err();
        assert!(err.contains("plugin.json"), "{err}");
        // 校验失败不留目录
        assert!(list_in(&base).is_empty());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_requires_non_empty_name_and_valid_json() {
        let base = temp_base("no-name");
        let no_name = build_zip(&[("plugin.json", br#"{"version":"v1"}"#)]);
        assert!(install_in(&base, &req(&no_name))
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        let bad_json = build_zip(&[("plugin.json", b"{oops")]);
        assert!(install_in(&base, &req(&bad_json))
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        assert!(list_in(&base).is_empty());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_rejects_not_a_zip() {
        let base = temp_base("not-zip");
        assert!(install_in(&base, &req(b"PK\x03\x04fake"))
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_never_overwrites_existing_suite() {
        let base = temp_base("overwrite");
        let zip = build_zip(&[("plugin.json", &plugin("dup-suite"))]);
        install_in(&base, &req(&zip)).unwrap();
        let before = std::fs::read(suites_root(&base).join("dup-suite/manifest.json")).unwrap();

        let err = install_in(&base, &req(&zip)).unwrap_err();
        assert!(err.starts_with("SUITE_INSTALL_FAILED"), "{err}");
        assert_eq!(
            std::fs::read(suites_root(&base).join("dup-suite/manifest.json")).unwrap(),
            before
        );
        assert_eq!(list_in(&base).len(), 1);
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_rejects_unsafe_entry_without_leaving_partial_dir() {
        let base = temp_base("evil");
        let zip = build_zip(&[
            ("plugin.json", &plugin("evil-suite")),
            ("../escaped.txt", b"boom"),
        ]);
        assert!(install_in(&base, &req(&zip))
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        assert!(!suites_root(&base).join("evil-suite").exists());
        assert!(!base.join("escaped.txt").exists());
        // 暂存目录也被清掉，不留半成品
        let strays: Vec<String> = std::fs::read_dir(&suites_root(&base))
            .map(|rd| {
                rd.filter_map(|e| e.ok())
                    .map(|e| e.file_name().to_string_lossy().to_string())
                    .collect()
            })
            .unwrap_or_default();
        assert!(strays.iter().all(|s| !s.starts_with(".staging-")), "{strays:?}");
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_rejects_oversized_entry() {
        let base = temp_base("oversize");
        let big = build_zip(&[
            ("plugin.json", &plugin("big-suite")),
            ("huge.txt", &vec![b'a'; (MAX_ENTRY_BYTES + 8) as usize]),
        ]);
        assert!(install_in(&base, &req(&big))
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        assert!(!suites_root(&base).join("big-suite").exists());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_rejects_oversized_package_bytes() {
        let base = temp_base("oversize-zip");
        let raw = vec![b'z'; MAX_ZIP_BYTES + 1];
        let req = InstallReq {
            file_name: None,
            zip_base64: Some(base64::engine::general_purpose::STANDARD.encode(&raw)),
        };
        assert!(install_in(&base, &req)
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_accepts_data_url_prefixed_base64() {
        let base = temp_base("dataurl");
        let zip = build_zip(&[("plugin.json", &plugin("dataurl-suite"))]);
        let raw = base64::engine::general_purpose::STANDARD.encode(&zip);
        let req = InstallReq {
            file_name: Some("dataurl-suite.zip".to_string()),
            zip_base64: Some(format!("data:application/zip;base64,{raw}")),
        };
        assert_eq!(install_in(&base, &req).unwrap()["id"], json!("dataurl-suite"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn install_of_chinese_name_writes_ascii_dir_with_display_name() {
        let base = temp_base("chinese");
        let zip = build_zip(&[("plugin.json", &plugin("金融行业套件"))]);
        let view = install_in(&base, &req(&zip)).unwrap();
        let id = view["id"].as_str().unwrap();
        assert!(id.starts_with("suite-"));
        assert_eq!(view["name"], json!("金融行业套件"));
        assert!(suites_root(&base).join(id).join("manifest.json").is_file());
        std::fs::remove_dir_all(&base).ok();
    }

    // ── 列表 ────────────────────────────────────────────────

    #[test]
    fn lists_installed_suites_newest_first() {
        let base = temp_base("list");
        let a = build_zip(&[("plugin.json", &plugin("alpha-suite"))]);
        install_in(&base, &req(&a)).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(5));
        let b = build_zip(&[("plugin.json", &plugin("beta-suite"))]);
        install_in(&base, &req(&b)).unwrap();
        let rows = list_in(&base);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["id"], json!("beta-suite"));
        assert_eq!(rows[0]["description"], json!("风控与合规流程套件"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn list_reports_broken_manifest_as_unreadable() {
        let base = temp_base("broken");
        let zip = build_zip(&[("plugin.json", &plugin("broken-suite"))]);
        install_in(&base, &req(&zip)).unwrap();
        std::fs::write(
            suites_root(&base).join("broken-suite/manifest.json"),
            "{oops",
        )
        .unwrap();
        let rows = list_in(&base);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["readable"], json!(false));
        assert_eq!(rows[0]["id"], json!("broken-suite"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn list_ignores_dirs_without_manifest_and_staging() {
        let base = temp_base("stray");
        std::fs::create_dir_all(suites_root(&base).join("no-manifest")).unwrap();
        std::fs::create_dir_all(suites_root(&base).join(".staging-x")).unwrap();
        std::fs::write(suites_root(&base).join("stray.txt"), "x").unwrap();
        assert!(list_in(&base).is_empty());
        std::fs::remove_dir_all(&base).ok();
    }

    // ── 卸载 ────────────────────────────────────────────────

    #[test]
    fn uninstall_moves_to_trash_instead_of_deleting() {
        let base = temp_base("uninstall");
        let zip = build_zip(&[
            ("plugin.json", &plugin("gone-suite")),
            ("assets/flow.md", "工作流说明\n".as_bytes()),
        ]);
        install_in(&base, &req(&zip)).unwrap();
        let res = uninstall_in(&base, "gone-suite").unwrap();
        assert_eq!(res["removed"], json!("gone-suite"));
        assert!(!suites_root(&base).join("gone-suite").exists());
        let trash = PathBuf::from(res["trashPath"].as_str().unwrap());
        // 存量内容可在回收目录找回（禁止物理删除）
        assert!(trash.join("manifest.json").is_file());
        assert!(trash.join("assets/flow.md").is_file());
        assert!(list_in(&base).is_empty());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn uninstall_missing_suite_reports_not_found() {
        let base = temp_base("uninstall-miss");
        assert!(uninstall_in(&base, "ghost")
            .unwrap_err()
            .starts_with("SUITE_NOT_FOUND"));
        assert!(uninstall_in(&base, "../escape")
            .unwrap_err()
            .starts_with("SUITE_INCOMPATIBLE"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn root_info_points_at_suites_and_trash_dirs() {
        let base = temp_base("root");
        let info = root_info(&base);
        assert_eq!(info["supported"], json!(true));
        assert_eq!(
            info["suitesRoot"].as_str().unwrap(),
            suites_root(&base).to_string_lossy()
        );
        assert!(info["trashRoot"].as_str().unwrap().ends_with("suites_removed"));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn clean_manifest_keeps_only_known_fields() {
        let v = clean_manifest(&json!({
            "name": "demo",
            "version": " ",
            "commands": ["a"],
            "skills": ["s"],
            "dataLinks": ["d"],
            "homepage": "https://x",
        }));
        let obj = v.as_object().unwrap();
        assert_eq!(obj.get("name").unwrap(), &json!("demo"));
        assert!(!obj.contains_key("homepage"));
        assert!(!obj.contains_key("version"), "空串版本不写入台账");
        assert_eq!(obj["skills"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn walk_counts_files_recursively_and_caps() {
        let base = temp_base("walk");
        let deep = base.join("pkg");
        std::fs::create_dir_all(deep.join("skills/demo")).unwrap();
        std::fs::write(deep.join("plugin.json"), "{}").unwrap();
        std::fs::write(deep.join("skills/demo/SKILL.md"), "x").unwrap();
        std::fs::write(deep.join("README.md"), "x").unwrap();
        assert_eq!(walk(&deep), (3, 1));
        assert_eq!(walk(&base.join("nowhere")), (0, 0));
        std::fs::remove_dir_all(&base).ok();
    }
}
