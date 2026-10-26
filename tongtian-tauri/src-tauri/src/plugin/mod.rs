//! 插件 SDK：套件包（plugin.json）安装前校验与起始模板
//!
//! - plugin_validate: 对上传的 .zip 做只读预检，输出清单摘要、指令正文来源、
//!   技能文件、被忽略字段与阻断项；不落盘、不改动任何已安装套件
//! - plugin_template: 返回起始 plugin.json / commands / skills / README 文本
//!
//! 校验口径与 `suite::install_bytes_in` 同源（大小上限、plugin.json 定位、
//! 路径安全、指令名规则），差别只在「先解释清楚再安装」：能修的问题进
//! errors/warnings，安装器仍按原规则拒绝。
//!
//! 边界：本应用只消费 commands 与包内 SKILL.md；mcpServers / hooks / agents
//! 等字段不会执行，校验会把它们列进 ignoredFields，不假装支持。

use std::collections::{HashMap, HashSet};

use base64::Engine as _;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::file::office::{zip_entries, zip_read, ZipEntry};
use crate::suite::{
    find_manifest_entry, safe_relative, suite_id_of, MAX_ENTRIES, MAX_ENTRY_BYTES,
    MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES, MAX_ZIP_BYTES,
};

/// 单条指令正文进入执行面的上限（与引擎 MAX_SKILL_BODY_CHARS 同档）
const MAX_BODY_CHARS: usize = 20_000;
/// 指令正文文件读取上限（与 suite::read_command_file 同档）
const MAX_BODY_FILE_BYTES: u64 = 256 * 1024;
/// 单个套件允许声明的指令数
const MAX_COMMANDS: usize = 100;
/// 套件内 SKILL.md 扫描上限（与 suite 运行时装配同档）
const MAX_SKILLS: usize = 50;
/// 清单允许的顶层字段；其余字段安装时会被丢弃，校验逐项列出
const MANIFEST_KEYS: [&str; 9] = [
    "name",
    "version",
    "author",
    "category",
    "description",
    "commands",
    "skills",
    "dataLinks",
    "license",
];

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ValidateReq {
    pub file_name: Option<String>,
    pub zip_base64: Option<String>,
}

fn invalid_package(reason: impl std::fmt::Display) -> String {
    format!("PLUGIN_INVALID_PACKAGE: {reason}")
}

fn decode_zip(raw: &str) -> Result<Vec<u8>, String> {
    let body = raw
        .trim()
        .rsplit(',')
        .next()
        .unwrap_or_default()
        .replace(['\r', '\n', ' ', '\t'], "");
    if body.is_empty() {
        return Err(invalid_package("未收到插件包内容（请上传 .zip）"));
    }
    base64::engine::general_purpose::STANDARD
        .decode(body.as_bytes())
        .map_err(|e| invalid_package(format!("插件包 base64 解码失败：{e}")))
}

struct RawCommand {
    name: String,
    description: String,
    content: Option<String>,
    file: Option<String>,
}

fn str_of(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

fn first_str(m: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|k| {
        m.get(*k)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    })
}

/// commands 三种写法（数组字符串 / 数组对象 / 名称→描述对象）与安装器同口径
fn collect_commands(manifest: &Value) -> Vec<RawCommand> {
    let mut out = Vec::new();
    match manifest.get("commands") {
        Some(Value::Array(items)) => {
            for it in items {
                match it {
                    Value::String(s) => out.push(RawCommand {
                        name: s.trim().to_string(),
                        description: String::new(),
                        content: None,
                        file: None,
                    }),
                    Value::Object(m) => {
                        let Some(name) = first_str(m, &["name", "command", "id"]) else {
                            continue;
                        };
                        out.push(RawCommand {
                            name,
                            description: first_str(m, &["description", "desc"]).unwrap_or_default(),
                            content: first_str(m, &["content", "prompt", "body", "instructions"]),
                            file: first_str(m, &["file", "path"]),
                        });
                    }
                    _ => {}
                }
            }
        }
        Some(Value::Object(m)) => {
            for (name, val) in m {
                match val {
                    Value::String(desc) => out.push(RawCommand {
                        name: name.trim().to_string(),
                        description: desc.trim().to_string(),
                        content: None,
                        file: None,
                    }),
                    Value::Object(body) => out.push(RawCommand {
                        name: name.trim().to_string(),
                        description: first_str(body, &["description", "desc"]).unwrap_or_default(),
                        content: first_str(body, &["content", "prompt", "body", "instructions"]),
                        file: first_str(body, &["file", "path"]),
                    }),
                    _ => out.push(RawCommand {
                        name: name.trim().to_string(),
                        description: String::new(),
                        content: None,
                        file: None,
                    }),
                }
            }
        }
        _ => {}
    }
    out
}

/// 指令名能否作为路径片段与 / 引用（不合法时安装后也无法调用）
fn command_name_error(name: &str) -> Option<String> {
    let count = name.chars().count();
    if name.is_empty() {
        return Some("指令名为空".to_string());
    }
    if count > 128 {
        return Some(format!("指令名超过 128 字（{}）", count));
    }
    if name == "." || name == ".." {
        return Some("指令名不能是 . 或 ..".to_string());
    }
    if name
        .chars()
        .any(|c| c == '/' || c == '\\' || c == ':' || c.is_control())
    {
        return Some(format!("指令名「{name}」含路径分隔符或控制字符"));
    }
    None
}

fn manifest_payload(m: &Value) -> Value {
    json!({
        "name": str_of(m, "name"),
        "version": str_of(m, "version"),
        "author": str_of(m, "author"),
        "category": str_of(m, "category"),
        "description": str_of(m, "description"),
    })
}

/// ZIP 内相对清单目录的路径（统一 `/` 分隔、去掉前缀目录）
fn rel_to_prefix(name: &str, prefix: &str) -> String {
    let n = name.trim_start_matches("./").replace('\\', "/");
    n.strip_prefix(prefix).unwrap_or(&n).trim_start_matches('/').to_string()
}

fn read_text_entry(
    bytes: &[u8],
    entries: &[ZipEntry],
    lookup: &HashMap<String, usize>,
    rel: &str,
) -> Result<String, String> {
    let key = rel.to_lowercase();
    let idx = lookup
        .get(&key)
        .copied()
        .ok_or_else(|| format!("插件包内不存在文件「{rel}」"))?;
    let entry = &entries[idx];
    if entry.uncomp_size > MAX_BODY_FILE_BYTES {
        return Err(format!(
            "正文文件「{rel}」超过 {}KB，安装后读取会失败",
            MAX_BODY_FILE_BYTES / 1024
        ));
    }
    let raw = zip_read(bytes, entry)?;
    Ok(String::from_utf8_lossy(&raw).trim().to_string())
}

fn body_preview(body: &str) -> String {
    let head: String = body.chars().take(120).collect();
    if body.chars().count() > 120 {
        format!("{head}…")
    } else {
        head
    }
}

/// 预检插件包（纯函数，不写盘）
pub fn validate_bytes(bytes: &[u8], file_name: &str) -> Result<Value, String> {
    if bytes.len() > MAX_ZIP_BYTES {
        return Err(invalid_package(format!(
            "插件包超过 {}MB 上限（收到 {}KB）",
            MAX_ZIP_BYTES / 1024 / 1024,
            bytes.len() / 1024
        )));
    }
    let entries = zip_entries(bytes).map_err(|e| invalid_package(&e))?;
    if entries.is_empty() {
        return Err(invalid_package("插件包内没有任何文件"));
    }

    let mut errors: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    if entries.len() > MAX_ENTRIES {
        errors.push(format!(
            "包内条目过多（{} 项，上限 {MAX_ENTRIES}）",
            entries.len()
        ));
    }
    let mut total_bytes: u64 = 0;
    for e in &entries {
        if e.uncomp_size > MAX_ENTRY_BYTES {
            errors.push(format!(
                "文件「{}」解压后超过 {}MB",
                e.name,
                MAX_ENTRY_BYTES / 1024 / 1024
            ));
        }
        if !e.name.ends_with('/') && safe_relative(&e.name).is_none() {
            errors.push(format!("包内含不安全路径：{}", e.name));
        }
        total_bytes = total_bytes.saturating_add(e.uncomp_size);
    }
    if total_bytes > MAX_TOTAL_BYTES {
        errors.push(format!(
            "整包解压后超过 {}MB",
            MAX_TOTAL_BYTES / 1024 / 1024
        ));
    }

    let mut lookup: HashMap<String, usize> = HashMap::new();
    for (i, e) in entries.iter().enumerate() {
        let rel = e.name.trim_start_matches("./").replace('\\', "/");
        if !rel.is_empty() && !rel.ends_with('/') {
            lookup.entry(rel.to_lowercase()).or_insert(i);
        }
    }

    let manifest_idx = find_manifest_entry(&entries);
    let mut manifest = Value::Null;
    if let Some(idx) = manifest_idx {
        let entry = &entries[idx];
        if entry.uncomp_size > MAX_MANIFEST_BYTES {
            errors.push("plugin.json 超过 256KB 上限".to_string());
        } else {
            let raw = zip_read(bytes, entry).map_err(|e| invalid_package(&e))?;
            match serde_json::from_slice::<Value>(&raw) {
                Ok(v) if v.is_object() => manifest = v,
                Ok(_) => errors.push("plugin.json 必须是 JSON 对象".to_string()),
                Err(e) => errors.push(format!("plugin.json 不是合法 JSON：{e}")),
            }
        }
    } else {
        errors.push("缺少 plugin.json，应位于根目录或单层目录内".to_string());
    }

    let mut commands_out: Vec<Value> = Vec::new();
    let mut skill_files: Vec<String> = Vec::new();
    let mut declared_skill_count = 0usize;
    let mut ignored_fields: Vec<String> = Vec::new();
    let name = str_of(&manifest, "name").unwrap_or_default();
    let mut plugin_id = String::new();

    if manifest.is_object() {
        if name.is_empty() {
            errors.push("plugin.json 缺少非空的 name 字段".to_string());
        } else if name.chars().count() > 200 {
            errors.push("套件名超过 200 字".to_string());
        } else {
            plugin_id = suite_id_of(&name);
        }
        if str_of(&manifest, "version").is_none() {
            warnings.push("未标注 version，安装后列表显示「未标注版本」".to_string());
        }
        for key in ["author", "category", "description"] {
            if let Some(v) = manifest.get(key) {
                if !v.is_string() {
                    warnings.push(format!("字段 {key} 不是字符串，安装时会被丢弃"));
                }
            }
        }

        let prefix = manifest_idx
            .map(|i| {
                let raw = entries[i].name.trim_start_matches("./").replace('\\', "/");
                raw.rsplit_once('/')
                    .map(|(p, _)| format!("{}/", p.trim_start_matches('/')))
                    .unwrap_or_default()
            })
            .unwrap_or_default();

        for (i, e) in entries.iter().enumerate() {
            let rel = rel_to_prefix(&e.name, &prefix);
            if rel.to_lowercase().ends_with("skill.md") && !e.name.ends_with('/') {
                skill_files.push(rel);
            }
            let _ = i;
        }
        skill_files.sort();
        skill_files.truncate(MAX_SKILLS + 1);

        match manifest.get("skills") {
            Some(Value::Array(items)) => {
                declared_skill_count = items
                    .iter()
                    .filter(|it| match it {
                        Value::String(s) => !s.trim().is_empty(),
                        Value::Object(m) => {
                            first_str(m, &["name", "path", "file"]).is_some()
                        }
                        _ => false,
                    })
                    .count();
                for it in items {
                    let raw_path = match it {
                        Value::String(s) => Some(s.clone()),
                        Value::Object(m) => first_str(m, &["path", "file"]),
                        _ => None,
                    };
                    if let Some(p) = raw_path {
                        if safe_relative(&p).is_none() {
                            warnings.push(format!("声明的技能路径不安全，已忽略：{p}"));
                        }
                    }
                }
            }
            Some(_) => warnings.push("skills 字段不是数组，安装时会被丢弃".to_string()),
            None => {}
        }
        if !skill_files.is_empty() {
            if declared_skill_count > 0 && declared_skill_count != skill_files.len() {
                warnings.push(format!(
                    "声明了 {declared_skill_count} 个技能，包内实际找到 {} 个 SKILL.md；安装后以实际文件为准",
                    skill_files.len()
                ));
            }
            if skill_files.len() > MAX_SKILLS {
                warnings.push(format!(
                    "包内 SKILL.md 超过 {MAX_SKILLS} 个，超出的技能不会进入执行面"
                ));
            }
        }

        if let Some(v) = manifest.get("dataLinks") {
            if !v.is_array() {
                warnings.push("dataLinks 字段不是数组，安装时会被丢弃".to_string());
            }
        }

        for (key, _) in manifest.as_object().map(|m| m.iter()).into_iter().flatten() {
            if !MANIFEST_KEYS.contains(&key.as_str()) {
                ignored_fields.push(key.clone());
            }
        }
        ignored_fields.sort();
        let behavior_fields: Vec<&String> = ignored_fields
            .iter()
            .filter(|k| {
                ["mcpServers", "hooks", "agents", "permissions", "tools"]
                    .iter()
                    .any(|b| k.eq_ignore_ascii_case(b))
            })
            .collect();
        if !behavior_fields.is_empty() {
            let names: Vec<String> = behavior_fields.iter().map(|s| s.to_string()).collect();
            warnings.push(format!(
                "字段 {} 不会被执行：本应用只消费 commands 与包内 SKILL.md",
                names.join(" / ")
            ));
        }

        let raw_commands = collect_commands(&manifest);
        if raw_commands.len() > MAX_COMMANDS {
            errors.push(format!(
                "声明的指令过多（{} 条，上限 {MAX_COMMANDS}）",
                raw_commands.len()
            ));
        }
        let mut seen: HashSet<String> = HashSet::new();
        for cmd in raw_commands.iter().take(MAX_COMMANDS) {
            let mut row = json!({
                "name": cmd.name,
                "description": cmd.description,
                "bodySource": "missing",
                "bodyChars": 0,
                "error": Value::Null,
            });
            if let Some(err) = command_name_error(&cmd.name) {
                errors.push(err.clone());
                row["error"] = json!(err);
                commands_out.push(row);
                continue;
            }
            let key = cmd.name.to_lowercase();
            if !seen.insert(key) {
                warnings.push(format!("指令「{}」名称重复，只保留第一条", cmd.name));
                continue;
            }
            let (body, source, mut error) = if let Some(c) =
                cmd.content.as_deref().map(str::trim).filter(|s| !s.is_empty())
            {
                (Some(c.to_string()), "content", None)
            } else if let Some(raw_file) =
                cmd.file.as_deref().map(str::trim).filter(|s| !s.is_empty())
            {
                match safe_relative(raw_file) {
                    None => (
                        None,
                        "file",
                        Some(format!("正文文件路径不安全：{raw_file}")),
                    ),
                    Some(p) => {
                        let rel = p.to_string_lossy().replace('\\', "/");
                        match read_text_entry(bytes, &entries, &lookup, &format!("{prefix}{rel}")) {
                            Ok(text) => (Some(text), "file", None),
                            Err(e) => (None, "file", Some(e)),
                        }
                    }
                }
            } else {
                let candidates = [
                    format!("commands/{}.md", cmd.name),
                    format!("commands/{}.txt", cmd.name),
                    format!("commands/{}/PROMPT.md", cmd.name),
                    format!("prompts/{}.md", cmd.name),
                    format!("prompts/{}.txt", cmd.name),
                    format!(".qoder-plugin/commands/{}.md", cmd.name),
                    format!(".qoder-plugin/commands/{}/PROMPT.md", cmd.name),
                    format!(".qoder-plugin/prompts/{}.md", cmd.name),
                ];
                let mut found = None;
                for rel in candidates {
                    if let Ok(text) =
                        read_text_entry(bytes, &entries, &lookup, &format!("{prefix}{rel}"))
                    {
                        found = Some(text);
                        break;
                    }
                }
                match found {
                    Some(text) => (Some(text), "convention", None),
                    None => (
                        None,
                        "missing",
                        Some(format!(
                            "未提供指令正文（可添加 commands/{}.md，或在 plugin.json 里写 content）",
                            cmd.name
                        )),
                    ),
                }
            };
            let body_chars = body.as_deref().map(|b| b.chars().count()).unwrap_or(0);
            if body_chars > MAX_BODY_CHARS {
                warnings.push(format!(
                    "指令「{}」正文 {body_chars} 字，超过 {MAX_BODY_CHARS} 字的部分会在执行时截断",
                    cmd.name
                ));
            }
            if error.is_some() && source == "missing" {
                warnings.push(format!(
                    "指令「{}」没有正文，安装后能出现在 / 候选但无法取用",
                    cmd.name
                ));
            }
            row["bodySource"] = json!(source);
            row["bodyChars"] = json!(body_chars);
            row["preview"] = json!(body.as_deref().map(body_preview).unwrap_or_default());
            if error.is_some() {
                row["error"] = json!(error.take().unwrap_or_default());
            }
            commands_out.push(row);
        }
        if commands_out.is_empty() {
            warnings.push("插件包没有声明任何指令，只能作为技能包使用".to_string());
        }
    }

    let installable = errors.is_empty();
    Ok(json!({
        "ok": installable,
        "installable": installable,
        "fileName": file_name,
        "pluginId": plugin_id,
        "manifest": manifest_payload(&manifest),
        "commands": commands_out,
        "skillFiles": skill_files,
        "declaredSkillCount": declared_skill_count,
        "ignoredFields": ignored_fields,
        "warnings": warnings,
        "errors": errors,
        "fileCount": entries.len(),
        "totalBytes": total_bytes,
    }))
}

// ── 模板（与 scripts/plugin-sdk.mjs 共用同一份文件） ─────────────

const TPL_MANIFEST: &str = include_str!("../../templates/plugin/plugin.json");
const TPL_COMMAND: &str = include_str!("../../templates/plugin/commands/hello.md");
const TPL_SKILL: &str = include_str!("../../templates/plugin/skills/example/SKILL.md");
const TPL_README: &str = include_str!("../../templates/plugin/README.md");

/// 起始模板：plugin.json 走 JSON 替换（名称里的引号不会破坏包），其余按文本替换
pub fn template_files(name: &str) -> Value {
    let clean = name.trim();
    let display = if clean.is_empty() {
        "我的套件".to_string()
    } else {
        clean.chars().take(80).collect::<String>()
    };
    let manifest = serde_json::from_str::<Value>(TPL_MANIFEST)
        .map(|mut v| {
            if let Some(obj) = v.as_object_mut() {
                obj.insert("name".to_string(), json!(display));
            }
            v
        })
        .unwrap_or_else(|_| json!({ "name": display }));
    json!({
        "files": [
            { "path": "plugin.json", "content": serde_json::to_string_pretty(&manifest).unwrap_or_default() },
            { "path": "commands/hello.md", "content": TPL_COMMAND },
            { "path": "skills/example/SKILL.md", "content": TPL_SKILL },
            { "path": "README.md", "content": TPL_README.replace("{{name}}", &display) },
        ],
        "notes": [
            "plugin.json 必须位于包根目录（或单层目录内），安装目录标识由 name 生成",
            "指令正文放 commands/<名称>.md，或在 plugin.json 的 commands 里写 content",
            "包内任意位置的 SKILL.md 都会作为技能进入执行面",
        ],
    })
}

// ── Tauri commands ────────────────────────────────────────────

#[tauri::command]
pub fn plugin_validate(req: ValidateReq) -> Result<Value, String> {
    let raw = req
        .zip_base64
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid_package("未提供插件包内容（请上传 .zip）"))?;
    let bytes = decode_zip(raw)?;
    let file_name = req
        .file_name
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("插件包");
    validate_bytes(&bytes, file_name)
}

#[tauri::command]
pub fn plugin_template(name: Option<String>) -> Value {
    template_files(name.as_deref().unwrap_or(""))
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn base_zip(manifest: &str, extra: &[(&str, &[u8])]) -> Vec<u8> {
        let mut files: Vec<(&str, &[u8])> = vec![("plugin.json", manifest.as_bytes())];
        files.extend_from_slice(extra);
        build_zip(&files)
    }

    #[test]
    fn validates_full_package_and_reports_sources() {
        let zip = base_zip(
            r#"{"name":"Research Kit","version":"1.2.0","description":"调研","commands":[
                {"name":"brief","description":"写简报","file":"commands/brief.md"},
                {"name":"check","content":"逐条核对"},
                {"name":"chat","description":"对话"}
            ],"skills":[{"path":"skills/fact/SKILL.md"}]}"#,
            &[
                ("commands/brief.md", "先列结论".as_bytes()),
                ("commands/chat.md", "引导提问".as_bytes()),
                ("skills/fact/SKILL.md", "---\nname: 事实核查\n---\n正文".as_bytes()),
            ],
        );
        let out = validate_bytes(&zip, "kit.zip").unwrap();
        assert_eq!(out["ok"], json!(true), "{out}");
        assert_eq!(out["pluginId"], json!("research-kit"));
        assert_eq!(out["manifest"]["name"], json!("Research Kit"));
        let cmds = out["commands"].as_array().unwrap();
        assert_eq!(cmds.len(), 3);
        assert_eq!(cmds[0]["bodySource"], json!("file"));
        assert_eq!(cmds[1]["bodySource"], json!("content"));
        assert_eq!(cmds[2]["bodySource"], json!("convention"));
        assert_eq!(cmds[0]["preview"], json!("先列结论"));
        assert_eq!(out["skillFiles"], json!(["skills/fact/SKILL.md"]));
        assert_eq!(out["declaredSkillCount"], json!(1));
        assert_eq!(out["ignoredFields"], json!([]));
        assert!(out["warnings"].as_array().unwrap().is_empty(), "{out}");
    }

    #[test]
    fn missing_manifest_and_bad_name_are_blocking() {
        let zip = build_zip(&[("readme.md", b"no manifest")]);
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["errors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e.as_str().unwrap().contains("缺少 plugin.json")));

        let zip = base_zip(r#"{"version":"1.0.0"}"#, &[]);
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["errors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e.as_str().unwrap().contains("name")));
    }

    #[test]
    fn unsafe_and_duplicate_commands_are_explained() {
        let zip = base_zip(
            r#"{"name":"危险指令","commands":[
                {"name":"../escape","content":"x"},
                {"name":"dup","content":"A"},
                {"name":"DUP","content":"B"}
            ]}"#,
            &[],
        );
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["errors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e.as_str().unwrap().contains("路径分隔符")));
        assert!(out["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| w.as_str().unwrap().contains("重复")));
        // 重复名只保留第一条
        let names: Vec<&str> = out["commands"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, vec!["../escape", "dup"]);
    }

    #[test]
    fn missing_body_is_warning_not_blocker_and_ignored_fields_are_listed() {
        let zip = base_zip(
            r#"{"name":"仅有声明","commands":[{"name":"nodody"}],
                "mcpServers":{"x":{}},"hooks":{"a":1},"license":"MIT"}"#,
            &[],
        );
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(true), "{out}");
        assert_eq!(out["commands"][0]["bodySource"], json!("missing"));
        assert!(out["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| w.as_str().unwrap().contains("没有正文")));
        assert_eq!(out["ignoredFields"], json!(["hooks", "mcpServers"]));
        assert!(out["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| w.as_str().unwrap().contains("不会被执行")));
    }

    #[test]
    fn oversized_command_body_file_is_reported() {
        let big = vec![b'a'; (MAX_BODY_FILE_BYTES as usize) + 1];
        let zip = base_zip(
            r#"{"name":"大文件","commands":[{"name":"big","file":"big.md"}]}"#,
            &[("big.md", big.as_slice())],
        );
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(true));
        assert!(out["commands"][0]["error"]
            .as_str()
            .unwrap()
            .contains("超过 256KB"));
    }

    #[test]
    fn template_files_match_names_and_are_valid_json() {
        let tpl = template_files("带\"引号\"的套件");
        let files = tpl["files"].as_array().unwrap();
        let manifest = files
            .iter()
            .find(|f| f["path"] == json!("plugin.json"))
            .unwrap();
        let parsed: Value = serde_json::from_str(manifest["content"].as_str().unwrap()).unwrap();
        assert_eq!(parsed["name"], json!("带\"引号\"的套件"));
        assert!(files
            .iter()
            .any(|f| f["path"] == json!("skills/example/SKILL.md")));
        assert!(tpl["notes"].as_array().unwrap().len() >= 3);
    }

    #[test]
    fn unsafe_entry_path_is_blocking() {
        let zip = build_zip(&[
            ("plugin.json", r#"{"name":"越界"}"#.as_bytes()),
            ("../escape.txt", b"x"),
        ]);
        let out = validate_bytes(&zip, "x.zip").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["errors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e.as_str().unwrap().contains("不安全路径")));
    }

    #[test]
    fn nested_dir_manifest_is_supported() {
        let zip = build_zip(&[
            ("my-plugin/plugin.json", br#"{"name":"Nested","commands":[]}"#),
            ("my-plugin/commands/a.md", b"body"),
        ]);
        let out = validate_bytes(&zip, "nested.zip").unwrap();
        assert_eq!(out["ok"], json!(true), "{out}");
        assert_eq!(out["manifest"]["name"], json!("Nested"));
    }
}
