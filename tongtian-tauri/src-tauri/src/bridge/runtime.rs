//! 执行面输入装配（能力台账 P0-1 技能/意识/连接器、P0-2 附件）
//!
//! 引擎是纯执行器：它不知道技能装在哪、意识文件在哪个目录、哪些技能被禁用、
//! 哪个连接器握手验证过，也读不到 docx/pdf。这些事实全在 Rust 侧
//! （`skills/` 目录 + `settings_kv` 禁用清单 + awareness 目录 + `mcp_servers` 表 +
//! 附件暂存文件），所以每次提交由这里装配成 /api/chat 的 `runtime` 与 `attachments`
//! 两个键，引擎才有东西可消费。
//!
//! 口径：读不到的材料照样带上并在条目里写 `error` / `note`，由引擎在事件里如实播报。
//! 静默少给等于让界面继续假装「附件已生效」。

use base64::Engine as _;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

use crate::awareness;
use crate::bridge::python_env;
use crate::file::{office, service};
use crate::knowledge;
use crate::mcp;
use crate::skill;
use crate::store::Store;
use crate::suite;

/// 附件读盘上限：Office/PDF 抽取要在内存里做，再大就该让用户先拆分而不是卡死提交
const MAX_ATTACH_BYTES: u64 = 32 * 1024 * 1024;
/// 单个附件正文进入模型的字符保留量（引擎侧 20000 字再兜一道，这里留出截断说明的位置）
const ATTACH_BODY_KEEP: usize = 19_000;

/// 按扩展名判定「文本类」的集合（前端没给 mime 或给成 octet-stream 时的判据）
const TEXT_EXTS: &[&str] = &[
    "md", "txt", "json", "jsonl", "yaml", "yml", "toml", "ini", "conf", "csv", "tsv", "log",
    "sql", "xml", "html", "htm", "css", "js", "jsx", "ts", "tsx", "py", "rs", "go", "java",
    "c", "h", "cpp", "hpp", "sh", "ps1", "bat", "svg",
];
const IMAGE_EXTS: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "ico"];

/// 一次提交装配出的执行面输入
#[derive(Debug, Clone)]
pub struct Bundle {
    /// /api/chat 的 `runtime` 键（意识 + 启用技能）
    pub runtime: Value,
    /// /api/chat 的 `attachments` 键（原字段 + 正文或失败说明）
    pub attachments: Vec<Value>,
}

/// 装配执行面输入。`store` 缺失（Mock / 单测）时技能清单为空，其余照常。
pub fn build(store: Option<&Store>, workspace_id: &str, attachments: &[Value]) -> Bundle {
    build_in(
        &awareness::dir(),
        &python_env::base_dir(),
        store,
        workspace_id,
        attachments,
    )
}

/// 纯函数版本：目录由调用方给出，便于用临时目录做单测，不触碰用户真实数据目录
fn build_in(
    awareness_dir: &Path,
    skills_base: &Path,
    store: Option<&Store>,
    workspace_id: &str,
    attachments: &[Value],
) -> Bundle {
    let (skills, suite_notes) = skills_block(skills_base, store);
    // knowledge 目录与意识目录同根：{home}/awareness/main → {home}/knowledge
    let knowledge_root = awareness_dir
        .parent()
        .and_then(Path::parent)
        .map(|home| home.join("knowledge"))
        .unwrap_or_else(knowledge::root);
    Bundle {
        runtime: json!({
            "awareness": awareness_block(awareness_dir),
            "skills": skills,
            // 本地知识库：引擎据此建索引并注册检索/读全文工具
            "knowledge": knowledge::runtime_bases(&knowledge_root),
            // 套件指令没能进入执行面的原因（重名跳过 / 正文缺失由条目 error 携带）
            "suiteNotes": suite_notes,
            // 专家团队：主 Agent 的派发清单（每个专家的模型配置只随请求下发给本机引擎）
            "experts": crate::expert::runtime_block(store),
            "mcp": mcp_block(store),
            "hooks": hooks_block(store),
            "sandbox": sandbox_block(store, workspace_id),
            // 电脑操控：档位与回环网关一次性下发给本机引擎（token 不进渲染层）
            "computerUse": computer_use_block(store),
            // 浏览器 AI+：受控会话与逐次放行；同样只把 token 交给本机引擎
            "browserUse": browser_use_block(store),
            // 出站脱敏：引擎在每次模型调用前按档位处理文本（off/mask/block）
            "privacy": privacy_block(store),
            // 上下文效率：单轮工具循环上限、自动压缩、连接器工具按需载入；引擎负责最终归一
            "agentPolicy": agent_policy_block(store),
            // 幻灯片模板：内置主题规格 + 用户导入的 .pptx 路径；引擎按此渲染
            "pptxTemplates": crate::pptx::runtime_block(store),
            // 逐工具审批：档位 + 本机确认网关；token 同样只随请求进入本机引擎
            "toolApproval": tool_approval_block(store),
            // ACP 委派：外部编码 Agent 的启动命令、审批档位与审计路径；
            // 引擎只在本机拉起该命令，模型只能给任务描述，不能改命令
            "acp": crate::terminal::acp_block(store),
        }),
        attachments: attachments.iter().map(hydrate).collect(),
    }
}

// ── 意识 ─────────────────────────────────────────────────────

/// 意识四文件 + 开关，压成 name → 正文（updatedAt 是界面层的事，不进 prompt）
fn awareness_block(dir: &Path) -> Value {
    match awareness::view(dir) {
        Ok(v) => {
            let mut files = serde_json::Map::new();
            for (name, entry) in &v.files {
                files.insert(name.clone(), Value::String(entry.content.clone()));
            }
            json!({
                "mode": v.mode,
                "autoMemory": v.auto_memory,
                "style": v.style,
                "files": Value::Object(files),
            })
        }
        // 目录不可访问：把原因交给引擎，让它作为事件说明「这轮意识没生效」
        Err(e) => json!({ "error": e }),
    }
}

// ── 技能 ─────────────────────────────────────────────────────

/// 启用的技能（含 SKILL.md 正文）+ 已安装套件的指令（正文按需读取）。
///
/// 两类条目走同一条消费路径（system prompt 清单 + use_skill 取正文），用
/// `kind` / `source` 区分措辞。已安装技能优先：套件指令与技能同名时跳过并留说明，
/// 不让两套流程抢同一个 `/名称` 引用。技能目录不存在时只是空清单，不是错误。
fn skills_block(skills_base: &Path, store: Option<&Store>) -> (Vec<Value>, Vec<String>) {
    let disabled = store.map(skill::disabled_ids).unwrap_or_default();
    let mut skills: Vec<Value> = skill::enabled_with_body(skills_base, &disabled)
        .into_iter()
        .map(|v| {
            json!({
                "id": v.get("id").cloned().unwrap_or(Value::Null),
                "name": v.get("name").cloned().unwrap_or(Value::Null),
                "description": v.get("description").cloned().unwrap_or(Value::Null),
                "content": v.get("content").cloned().unwrap_or(Value::String(String::new())),
                "kind": "skill",
                "source": "installed",
            })
        })
        .collect();

    let mut reserved: HashSet<String> = HashSet::new();
    for s in &skills {
        if let Some(id) = s.get("id").and_then(Value::as_str) {
            reserved.insert(id.to_lowercase());
        }
        if let Some(name) = s.get("name").and_then(Value::as_str) {
            reserved.insert(name.to_lowercase());
        }
    }

    let suite_runtime = suite::runtime_commands(skills_base);
    let mut notes = suite_runtime.notes;
    for entry in suite_runtime.skills.into_iter().chain(suite_runtime.commands) {
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let name = entry
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let kind = if entry.get("kind").and_then(Value::as_str) == Some("command") {
            "套件指令"
        } else {
            "套件技能"
        };
        if (!id.is_empty() && reserved.contains(&id)) || (!name.is_empty() && reserved.contains(&name))
        {
            notes.push(format!(
                "{kind}「{}」与已安装技能同名，本轮以已安装技能为准",
                entry.get("name").and_then(Value::as_str).unwrap_or(&id)
            ));
            continue;
        }
        if !id.is_empty() {
            reserved.insert(id);
        }
        if !name.is_empty() {
            reserved.insert(name);
        }
        skills.push(entry);
    }
    (skills, notes)
}

// ── MCP 连接器 ───────────────────────────────────────────────

/// 已连接（握手验证过）的连接器清单；没有 store（Mock/单测）时视为「无连接器」而非错误
fn mcp_block(store: Option<&Store>) -> Value {
    match store {
        Some(s) => match mcp::for_runtime(s) {
            Ok(rows) => Value::Array(rows),
            // 表读不出来：把原因交给引擎，让它说明「这轮连接器没进执行面」
            Err(e) => json!({ "error": e }),
        },
        None => json!([]),
    }
}

// ── Hooks ────────────────────────────────────────────────────

/// 用户 Hooks 的执行面输入：配置概览 + 本机网关地址与一次性 token。
///
/// 命令正文与放行台账都在 SQLite/引擎进程外，引擎拿不到执行能力，只能在触发点问网关
/// 「这个事件该跑哪条脚本」；`token` 只随 /api/chat 请求体到本机引擎，不进渲染层、不进事件正文。
fn hooks_block(store: Option<&Store>) -> Value {
    let Some(s) = store else {
        // Mock / 单测通道：没有 SQLite 就没有配置可读，按「未配置」处理，不伪造网关
        return json!({ "configured": 0 });
    };
    match crate::align::hooks::runtime_summary(s) {
        Ok(mut summary) => {
            match crate::bridge::hook_gate::endpoint() {
                Some(g) => {
                    if let Some(map) = summary.as_object_mut() {
                        map.insert("gateUrl".into(), json!(g.url));
                        map.insert("token".into(), json!(g.token));
                    }
                }
                // 配了脚本却没有执行通道：如实写出来，引擎会摊成可见说明
                None => {
                    if let Some(map) = summary.as_object_mut() {
                        map.insert(
                            "gateError".into(),
                            json!("本机 Hook 网关未启动（回环端口占用失败），配置的脚本本轮不会执行"),
                        );
                    }
                }
            }
            summary
        }
        Err(e) => json!({ "error": e }),
    }
}

// ── 沙箱策略 ─────────────────────────────────────────────────

/// 本工作空间的沙箱策略（工具/文件白黑名单）随 runtime 下发。
///
/// 未配置时给出空清单而不是省略键：引擎据此区分「没有限制」与「Rust 没下发」。
/// 读取失败时保留 `error`，引擎按 fail-closed 处理（本轮工具调用全部拒绝）并说明原因。
fn sandbox_block(store: Option<&Store>, workspace_id: &str) -> Value {
    let base = json!({
        "workspaceId": workspace_id,
        "configured": false,
        "executionLevel": "L1",
        "toolAllowlist": [],
        "toolBlocklist": [],
        "fileAllowlist": [],
        "fileBlocklist": [],
        // 执行授权默认关闭：引擎据此不注册 run_command / fetch_url
        "execEnabled": false,
        "execAllowlist": [],
        "networkEnabled": false,
        "networkAllowlist": [],
    });
    let Some(s) = store else {
        return base;
    };
    match crate::security::policy::get_sandbox(s, workspace_id) {
        Ok(Some(p)) => json!({
            "workspaceId": p.workspace_id,
            "configured": true,
            "executionLevel": p.execution_level,
            "toolAllowlist": p.tool_allowlist,
            "toolBlocklist": p.tool_blocklist,
            "fileAllowlist": p.file_allowlist,
            "fileBlocklist": p.file_blocklist,
            "execEnabled": p.exec_enabled,
            "execAllowlist": p.exec_allowlist,
            "networkEnabled": p.network_enabled,
            "networkAllowlist": p.network_allowlist,
        }),
        Ok(None) => base,
        Err(e) => json!({ "workspaceId": workspace_id, "error": e }),
    }
}

// ── 电脑操控 ─────────────────────────────────────────────────

/// 电脑操控档位 + 回环网关。token 只随本次请求进入本机引擎，
/// 不进渲染层与事件；网关未启动或平台不支持时如实给出原因，不凭空发地址。
fn computer_use_block(store: Option<&Store>) -> Value {
    let supported = crate::computer::supported();
    let mut block = json!({
        "supported": supported,
        "platform": crate::computer::platform(),
        "features": crate::computer::features(),
        "policy": crate::computer::DEFAULT_POLICY,
        "persisted": false,
    });
    let Some(s) = store else {
        block["gateError"] = json!("未提供存储，电脑操控档位不可读");
        return block;
    };
    match crate::computer::read_policy(s) {
        Ok((policy, persisted)) => {
            block["policy"] = json!(policy);
            block["persisted"] = json!(persisted);
            if supported && policy != "disabled" {
                match crate::bridge::computer_gate::endpoint() {
                    Some(g) => {
                        block["gateUrl"] = json!(g.url);
                        block["gateToken"] = json!(g.token);
                    }
                    None => {
                        block["gateError"] = json!("本机电脑操控网关未启动，执行器不可用")
                    }
                }
            }
        }
        Err(e) => {
            // fail-closed：坏档位按禁用处理，但把原因交给引擎说明「这轮为什么不可用」
            block["policy"] = json!("disabled");
            block["persisted"] = json!(true);
            block["error"] = json!(e);
        }
    }
    block
}

// ── 浏览器 AI+ ──────────────────────────────────────────────

/// 浏览器档位 + 回环网关 + 会话状态。token 只随本次请求进入本机引擎，
/// 不进渲染层与事件；没有浏览器/网关未启动/档位损坏时如实给出原因。
fn browser_use_block(store: Option<&Store>) -> Value {
    let install = crate::browser::discover();
    let supported = install.is_some();
    let mut block = json!({
        "supported": supported,
        "platform": crate::browser::platform(),
        "browser": install.as_ref().map(|b| b.name.clone()),
        "features": crate::browser::features(),
        "policy": crate::browser::DEFAULT_POLICY,
        "persisted": false,
        "running": crate::browser::running(),
        "profileDir": crate::browser::profile_dir().to_string_lossy(),
    });
    let Some(s) = store else {
        block["gateError"] = json!("未提供存储，浏览器档位不可读");
        return block;
    };
    match crate::browser::read_policy(s) {
        Ok((policy, persisted)) => {
            block["policy"] = json!(policy);
            block["persisted"] = json!(persisted);
            if supported && policy != "disabled" {
                match crate::bridge::browser_gate::endpoint() {
                    Some(g) => {
                        block["gateUrl"] = json!(g.url);
                        block["gateToken"] = json!(g.token);
                    }
                    None => block["gateError"] = json!("本机浏览器网关未启动，执行器不可用"),
                }
            }
        }
        Err(e) => {
            // fail-closed：坏档位按禁用处理，但把原因交给引擎说明「这轮为什么不可用」
            block["policy"] = json!("disabled");
            block["persisted"] = json!(true);
            block["error"] = json!(e);
        }
    }
    block
}

// ── 出站脱敏 ─────────────────────────────────────────────────

/// 出站脱敏档位。真正脱敏在引擎侧；这里只下发档位与读取失败原因，
/// 非法值 fail-closed 为 off 并带上可读错误，不静默改变用户设置。
fn privacy_block(store: Option<&Store>) -> Value {
    let Some(s) = store else {
        return json!({ "mode": crate::security::pii::DEFAULT_MODE, "persisted": false });
    };
    match crate::security::pii::read_mode(s) {
        Ok((mode, persisted)) => json!({ "mode": mode, "persisted": persisted }),
        Err(e) => json!({ "mode": "off", "persisted": true, "error": e }),
    }
}

// ── 上下文与工具效率 ─────────────────────────────────────────

const AGENT_MAX_ITERS_KEY: &str = "agentMaxIters";
const AGENT_AUTO_COMPRESS_KEY: &str = "agentAutoCompress";
const AGENT_LAZY_MCP_KEY: &str = "agentLazyMcpTools";
const AGENT_MIN_ITERS: i64 = 1;
const AGENT_MAX_ITERS: i64 = 32;
const AGENT_DEFAULT_ITERS: i64 = 8;

/// 单轮工具循环上限与上下文效率开关（settings_kv）。
/// 引擎会按同一口径再归一一次；这里先按库内原值标注越界/非法，
/// 让用户看到「设置页写的值」与「实际执行的值」为什么不一致。
fn agent_policy_block(store: Option<&Store>) -> Value {
    let mut block = json!({
        "maxIters": AGENT_DEFAULT_ITERS,
        "autoCompress": true,
        "lazyMcpTools": true,
        "persisted": false,
        "notes": [],
    });
    let Some(s) = store else {
        return block;
    };
    let mut notes: Vec<String> = Vec::new();
    let mut persisted = false;
    if let Ok(Some(raw)) = crate::store::settings::get(s, AGENT_MAX_ITERS_KEY) {
        persisted = true;
        match crate::store::settings::decode_value(&raw).as_i64() {
            Some(n) if (AGENT_MIN_ITERS..=AGENT_MAX_ITERS).contains(&n) => {
                block["maxIters"] = json!(n)
            }
            Some(n) => {
                let clamped = n.clamp(AGENT_MIN_ITERS, AGENT_MAX_ITERS);
                block["maxIters"] = json!(clamped);
                notes.push(format!(
                    "单轮工具循环上限 {n} 超出允许范围 {AGENT_MIN_ITERS}~{AGENT_MAX_ITERS}，已按 {clamped} 执行。"
                ));
            }
            None => notes.push(format!(
                "单轮工具循环上限「{}」不是整数，已按默认 {AGENT_DEFAULT_ITERS} 执行。",
                raw.trim()
            )),
        }
    }
    for (key, field, label) in [
        (AGENT_AUTO_COMPRESS_KEY, "autoCompress", "上下文压缩"),
        (AGENT_LAZY_MCP_KEY, "lazyMcpTools", "连接器工具按需载入"),
    ] {
        if let Ok(Some(raw)) = crate::store::settings::get(s, key) {
            persisted = true;
            match setting_bool_strict(&raw) {
                Some(value) => block[field] = json!(value),
                None => notes.push(format!(
                    "{label}开关「{}」不是布尔值，已按默认开启执行。",
                    raw.trim()
                )),
            }
        }
    }
    block["persisted"] = json!(persisted);
    block["notes"] = json!(notes);
    block
}

/// 布尔设置项（JSON 布尔经 decode 后仍是带引号文本，统一按文本归一）
fn setting_bool_strict(raw: &str) -> Option<bool> {
    match raw.trim().trim_matches('"').to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

// ── 逐工具审批 ───────────────────────────────────────────────

/// 逐工具审批档位 + 回环网关。token 只随本次请求进入本机引擎，
/// 不进渲染层与事件；网关未启动或档位损坏时如实给出原因。
fn tool_approval_block(store: Option<&Store>) -> Value {
    let mut block = json!({
        "mode": crate::security::tool_approval::DEFAULT_MODE,
        "persisted": false,
    });
    let Some(s) = store else {
        return block;
    };
    match crate::security::tool_approval::read_mode(s) {
        Ok((mode, persisted)) => {
            block["mode"] = json!(mode);
            block["persisted"] = json!(persisted);
            if mode != "off" {
                match crate::bridge::tool_gate::endpoint() {
                    Some(g) => {
                        block["gateUrl"] = json!(g.url);
                        block["gateToken"] = json!(g.token);
                    }
                    None => {
                        block["gateError"] = json!("本机工具审批网关未启动，工具调用无法确认")
                    }
                }
            }
        }
        Err(e) => {
            // fail-closed：坏档位标成 off 并带上原因，引擎读到 error 会直接中止任务
            block["mode"] = json!("off");
            block["persisted"] = json!(true);
            block["error"] = json!(e);
        }
    }
    block
}

// ── 附件 ─────────────────────────────────────────────────────


fn ext_of(name: &str) -> String {
    name.rsplit('.').next().unwrap_or("").to_ascii_lowercase()
}

fn is_image(name: &str, mime: &str) -> bool {
    mime.starts_with("image/") || IMAGE_EXTS.contains(&ext_of(name).as_str())
}

/// 文本类判定：先看 mime（svg 已被 office 排除在外），再看扩展名
fn is_text(name: &str, mime: &str) -> bool {
    if mime.starts_with("text/") {
        return true;
    }
    if mime.is_empty() || mime == "application/octet-stream" {
        return TEXT_EXTS.contains(&ext_of(name).as_str());
    }
    matches!(
        mime,
        "application/json" | "application/xml" | "application/x-yaml" | "application/yaml"
    ) || TEXT_EXTS.contains(&ext_of(name).as_str())
}

/// 正文截断：把「原文还有多少」写进正文本身，而不是悄悄少给
fn cap_body(text: String) -> String {
    let total = text.chars().count();
    if total <= ATTACH_BODY_KEEP {
        return text;
    }
    let kept: String = text.chars().take(ATTACH_BODY_KEEP).collect();
    format!(
        "{kept}\n…（正文过长，本地只带入前 {ATTACH_BODY_KEEP} 字，原文共 {total} 字）"
    )
}

fn with_status(att: &Value, extra: Value) -> Value {
    let mut base = match att.as_object() {
        Some(obj) => Value::Object(obj.clone()),
        None => json!({}),
    };
    if let Some(map) = base.as_object_mut() {
        for (k, v) in extra.as_object().into_iter().flat_map(|m| m.clone()) {
            map.insert(k, v);
        }
    }
    base
}

/// 单个附件 → 带正文或带失败说明的条目（永远保留调用方原字段）
fn hydrate(att: &Value) -> Value {
    let name = att
        .get("fileName")
        .and_then(Value::as_str)
        .unwrap_or("附件")
        .to_string();
    let raw_path = att.get("stagedPath").and_then(Value::as_str).unwrap_or("").to_string();
    let given_mime = att
        .get("mime")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();

    if raw_path.trim().is_empty() {
        return with_status(att, json!({ "error": "未提供暂存路径（stagedPath 为空）" }));
    }
    let path = PathBuf::from(&raw_path);
    let meta = match std::fs::metadata(&path) {
        Ok(m) => m,
        Err(e) => return with_status(att, json!({ "error": format!("暂存文件读不到：{e}") })),
    };
    if !meta.is_file() {
        return with_status(att, json!({ "error": "暂存路径不是一个文件" }));
    }
    if meta.len() > MAX_ATTACH_BYTES {
        return with_status(
            att,
            json!({ "error": format!(
                "附件 {}MB 超过 {}MB 读取上限，正文未进入模型",
                meta.len() / (1024 * 1024),
                MAX_ATTACH_BYTES / (1024 * 1024)
            ) }),
        );
    }

    // 图片：转 base64 随请求下发，引擎按模型视觉能力决定是否组 ImageBlock。
    // 这里不代替引擎下判断，只如实说明「已下发、能否解析看模型」。
    if is_image(&name, &given_mime) {
        let mime = if given_mime.trim().is_empty() || given_mime == "application/octet-stream" {
            service::mime_of(&path).to_string()
        } else {
            given_mime.clone()
        };
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(e) => return with_status(att, json!({ "error": format!("图片读取失败：{e}") })),
        };
        if bytes.is_empty() {
            return with_status(att, json!({ "error": "图片内容为空" }));
        }
        let data = base64::engine::general_purpose::STANDARD.encode(&bytes);
        return with_status(
            att,
            json!({
                "image": { "mime": mime, "bytes": bytes.len(), "data": data },
                "note": "图片已随请求下发；能否解析取决于当前模型通道的视觉能力",
            }),
        );
    }

    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(e) => return with_status(att, json!({ "error": format!("附件读取失败：{e}") })),
    };

    if is_text(&name, &given_mime) {
        let text = String::from_utf8_lossy(&bytes).to_string();
        if text.trim().is_empty() {
            return with_status(att, json!({ "error": "附件内容为空" }));
        }
        return with_status(att, json!({ "text": cap_body(text) }));
    }

    // Office / PDF：mime 缺失时按扩展名补（与附件暂存同一张表），抽不出正文就如实报错
    let mime = if given_mime.is_empty() || given_mime == "application/octet-stream" {
        service::mime_of(&path).to_string()
    } else {
        given_mime.clone()
    };
    match office::extract(&mime, &bytes) {
        Ok(ex) => {
            if !ex.supported || ex.content.trim().is_empty() {
                return with_status(
                    att,
                    json!({ "error": format!("正文抽取为空（{}）", ex.note.as_deref().unwrap_or("无法解析")) }),
                );
            }
            let mut extra = json!({ "text": cap_body(ex.content) });
            if let Some(note) = ex.note {
                if let Some(map) = extra.as_object_mut() {
                    map.insert("note".to_string(), Value::String(note));
                }
            }
            with_status(att, extra)
        }
        Err(e) => with_status(att, json!({ "error": e })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::file::office::zip_build;

    /// 每用例独立临时根目录（显式带 tag，绝不落到用户真实数据目录）
    fn temp_home(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "tt-runtime-{tag}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn stage(home: &Path, name: &str, bytes: &[u8]) -> Value {
        let p = home.join(name);
        std::fs::write(&p, bytes).unwrap();
        json!({ "fileName": name, "mime": "", "stagedPath": p.to_string_lossy() })
    }

    fn docx_with(text: &str) -> Vec<u8> {
        zip_build(&[
            ("[Content_Types].xml", b"<?xml version=\"1.0\"?><Types/>", 0),
            (
                "word/document.xml",
                format!("<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
                    .as_bytes(),
                8,
            ),
        ])
    }

    // ── 附件：正文真的被读出来 ────────────────────────────────

    #[test]
    fn text_attachment_carries_body_and_keeps_caller_fields() {
        let home = temp_home("text");
        let att = stage(&home, "数据.csv", "区域,营收\n华东,1200\n".as_bytes());
        let out = hydrate(&att);
        assert_eq!(out["fileName"], "数据.csv");
        assert!(out.get("error").is_none(), "{out}");
        assert!(out["text"].as_str().unwrap().contains("华东,1200"), "{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn office_container_extracted_even_when_mime_missing() {
        // 前端常常不给 mime（或给 octet-stream），这里要按扩展名补表再抽正文
        let home = temp_home("docx");
        let att = stage(&home, "report.docx", &docx_with("华东"));
        let mut att = att;
        att["mime"] = json!("application/octet-stream");
        let out = hydrate(&att);
        assert!(out.get("error").is_none(), "{out}");
        assert!(out["text"].as_str().unwrap().contains("华东"), "{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn broken_container_reports_error_instead_of_empty_body() {
        let home = temp_home("bad-docx");
        let att = stage(&home, "bad.docx", b"PK\x03\x04fake");
        let out = hydrate(&att);
        assert!(out.get("text").is_none(), "{out}");
        let err = out["error"].as_str().unwrap_or_default();
        assert!(!err.is_empty(), "抽取失败必须摊出原因：{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn empty_text_attachment_is_reported() {
        let home = temp_home("empty");
        let out = hydrate(&stage(&home, "blank.md", b"  \n"));
        assert!(out["error"].as_str().unwrap().contains("内容为空"), "{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn image_is_base64_encoded_and_model_capability_left_to_engine() {
        let home = temp_home("image");
        let raw = b"\x89PNG\r\n\x1a\n";
        let mut by_mime = stage(&home, "chart.png", raw);
        by_mime["mime"] = json!("image/png");
        let out = hydrate(&by_mime);
        assert_eq!(out["image"]["mime"], "image/png", "{out}");
        assert_eq!(
            out["image"]["data"].as_str().unwrap(),
            "iVBORw0KGgo=",
            "图片必须原样 base64 下发给引擎：{out}"
        );
        assert_eq!(out["image"]["bytes"], raw.len(), "{out}");
        assert!(out["note"].as_str().unwrap().contains("视觉能力"), "{out}");
        assert!(out.get("text").is_none() && out.get("error").is_none(), "{out}");

        // mime 缺失时靠扩展名判定，并补出正确的 media_type
        let out = hydrate(&stage(&home, "photo.JPEG", b"\xff\xd8\xff"));
        assert_eq!(out["image"]["mime"], "image/jpeg", "{out}");
        assert!(out.get("text").is_none(), "{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn unusable_staged_paths_are_reported_one_by_one() {
        let home = temp_home("bad-path");
        let out = hydrate(&json!({ "fileName": "a.md" }));
        assert!(out["error"].as_str().unwrap().contains("stagedPath"), "{out}");

        let out = hydrate(&json!({
            "fileName": "gone.md",
            "stagedPath": home.join("gone.md").to_string_lossy(),
        }));
        assert!(out["error"].as_str().unwrap().contains("读不到"), "{out}");

        let mut dir_att = json!({ "fileName": "d.md", "mime": "text/markdown" });
        dir_att["stagedPath"] = json!(home.to_string_lossy());
        assert_eq!(hydrate(&dir_att)["error"], "暂存路径不是一个文件");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn oversize_attachment_refuses_to_inline() {
        let home = temp_home("oversize");
        let p = home.join("big.md");
        let file = std::fs::File::create(&p).unwrap();
        file.set_len(MAX_ATTACH_BYTES + 1).unwrap();
        drop(file);
        let out = hydrate(&json!({
            "fileName": "big.md", "mime": "text/markdown",
            "stagedPath": p.to_string_lossy(),
        }));
        assert!(out["error"].as_str().unwrap().contains("超过"), "{out}");
        assert!(out.get("text").is_none(), "{out}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn non_object_input_still_produces_a_status() {
        assert_eq!(hydrate(&json!("just-a-string"))["error"], "未提供暂存路径（stagedPath 为空）");
    }

    // ── 截断与判型 ────────────────────────────────────────────

    #[test]
    fn cap_body_declares_omitted_length() {
        assert_eq!(cap_body("短正文".to_string()), "短正文");
        let total = ATTACH_BODY_KEEP + 500;
        let out = cap_body("字".repeat(total));
        assert!(out.contains("正文过长"), "{out}");
        assert!(out.contains(&format!("原文共 {total} 字")), "{out}");
        assert!(out.chars().count() < total);
    }

    #[test]
    fn kind_detection_prefers_mime_then_extension() {
        assert!(is_text("a.md", ""));
        assert!(is_text("a.bin", "application/json"));
        assert!(is_text("a.txt", "text/plain"));
        assert!(!is_text("a.bin", "application/octet-stream"));
        assert!(is_image("a.jpeg", ""));
        assert!(is_image("a.bin", "image/png"));
        assert!(!is_image("a.md", "text/markdown"));
        assert_eq!(ext_of("Report.DOCX"), "docx");
        assert_eq!(ext_of("noext"), "noext");
    }

    #[test]
    fn with_status_preserves_original_fields() {
        let out = with_status(
            &json!({ "fileName": "a.md", "size": 12 }),
            json!({ "text": "hi", "note": "n" }),
        );
        assert_eq!(out["fileName"], "a.md");
        assert_eq!(out["size"], 12);
        assert_eq!(out["text"], "hi");
        assert_eq!(out["note"], "n");
    }

    // ── 意识 / 技能：装了什么就带上什么 ────────────────────────

    #[test]
    fn build_in_carries_awareness_files_and_enabled_skill_bodies() {
        let home = temp_home("bundle");
        let ads = home.join("awareness").join("main");
        std::fs::create_dir_all(&ads).unwrap();
        std::fs::write(ads.join("SOUL.md"), "# 协作风格\n只给结论，不给铺垫").unwrap();
        let sk = home.join("skills").join("weekly-report");
        std::fs::create_dir_all(&sk).unwrap();
        std::fs::write(
            sk.join("SKILL.md"),
            "---\nname: Weekly Report\ndescription: 按团队模板产出周报\n---\n第三步：把结论写进 summary.md\n",
        )
        .unwrap();

        let bundle = build_in(
            &ads,
            &home,
            None,
            "default",
            &[json!({ "fileName": "x.md", "stagedPath": "" })],
        );

        assert_eq!(bundle.runtime["awareness"]["mode"], json!(true));
        assert!(
            bundle.runtime["awareness"]["files"]["SOUL.md"]
                .as_str()
                .unwrap()
                .contains("只给结论"),
            "{}",
            bundle.runtime["awareness"]
        );
        let s = &bundle.runtime["skills"][0];
        assert_eq!(s["id"], "weekly-report");
        assert!(s["description"].as_str().unwrap().contains("周报"), "{s}");
        assert!(s["content"].as_str().unwrap().contains("summary.md"), "{s}");
        assert_eq!(bundle.attachments.len(), 1);
        assert!(bundle.attachments[0]["error"].as_str().unwrap().contains("stagedPath"));
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn build_in_carries_only_enabled_knowledge_bases() {
        let home = temp_home("knowledge-bundle");
        let ads = home.join("awareness").join("main");
        std::fs::create_dir_all(&ads).unwrap();
        let kb_root = home.join("knowledge");
        for (id, name, enabled) in [("kb-live", "产品手册", true), ("kb-off", "已停用", false)] {
            let dir = kb_root.join(id);
            std::fs::create_dir_all(dir.join("docs")).unwrap();
            std::fs::create_dir_all(dir.join("text")).unwrap();
            std::fs::write(dir.join("docs").join("a.md"), "正文").unwrap();
            std::fs::write(dir.join("text").join("a.md.txt"), "正文").unwrap();
            std::fs::write(
                dir.join("meta.json"),
                json!({
                    "id": id, "name": name, "description": "说明",
                    "enabled": enabled, "createdAt": 1, "updatedAt": 1, "deletedAt": null
                })
                .to_string(),
            )
            .unwrap();
        }

        let bundle = build_in(&ads, &home, None, "default", &[]);
        let bases = bundle.runtime["knowledge"]["bases"].as_array().unwrap();
        assert_eq!(bases.len(), 1, "{}", bundle.runtime["knowledge"]);
        assert_eq!(bases[0]["id"], "kb-live");
        assert_eq!(bases[0]["name"], "产品手册");
        assert_eq!(bases[0]["docCount"], 1);
        assert_eq!(
            PathBuf::from(bases[0]["dir"].as_str().unwrap()),
            kb_root.join("kb-live")
        );
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn build_in_carries_expert_registry_without_secrets_by_default() {
        let home = temp_home("experts-bundle");
        let ads = home.join("awareness").join("main");
        std::fs::create_dir_all(&ads).unwrap();
        let store = crate::store::Store::open(home.join("t.db")).unwrap();

        // 没有 store（Mock/单测）时如实标注未持久化，而不是假装有一个空注册表
        let mock = build_in(&ads, &home, None, "default", &[]);
        assert_eq!(
            mock.runtime["experts"],
            json!({ "items": [], "persisted": false })
        );

        crate::expert::save(
            &store,
            crate::expert::ExpertInput {
                id: None,
                name: "财务分析师".into(),
                description: "负责财报口径与成本结构分析".into(),
                prompt: "你是财务分析师，所有结论给出计算口径。".into(),
                model_id: None,
                skills: vec!["weekly-report".into()],
            },
        )
        .unwrap();

        let bundle = build_in(&ads, &home, Some(&store), "default", &[]);
        let items = bundle.runtime["experts"]["items"].as_array().unwrap();
        assert_eq!(bundle.runtime["experts"]["persisted"], json!(true));
        assert_eq!(items.len(), 1, "{}", bundle.runtime["experts"]);
        assert_eq!(items[0]["name"], "财务分析师");
        assert_eq!(items[0]["description"], "负责财报口径与成本结构分析");
        assert_eq!(items[0]["skills"], json!(["weekly-report"]));
        // 未绑定模型时不下发 modelConfig，由引擎回落到本轮对话模型
        assert!(items[0].get("modelConfig").is_none(), "{}", items[0]);
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn build_in_excludes_disabled_skills_and_stays_quiet_when_dirs_missing() {
        // 技能/意识目录都不存在：空清单 + 正常初始化后的意识视图，不是错误
        let sk_home = temp_home("no-skills");
        let aw_home = temp_home("no-awareness");
        let aw_dir = aw_home.join("awareness").join("main");
        let empty = build_in(&aw_dir, &sk_home, None, "default", &[]);
        assert_eq!(empty.runtime["skills"], json!([]));
        assert!(empty.runtime["awareness"]["error"].is_null(), "{}", empty.runtime);
        std::fs::remove_dir_all(&sk_home).ok();
        std::fs::remove_dir_all(&aw_home).ok();

        let home = temp_home("disabled");
        for id in ["alpha", "beta"] {
            let d = home.join("skills").join(id);
            std::fs::create_dir_all(&d).unwrap();
            std::fs::write(d.join("SKILL.md"), format!("---\ndescription: {id} 技能\n---\n正文\n")).unwrap();
        }
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        crate::skill::set_disabled(&store, "beta", true).unwrap();
        let bundle = build_in(&home.join("awareness"), &home, Some(&store), "default", &[]);

        let ids: Vec<&str> = bundle.runtime["skills"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["id"].as_str().unwrap_or_default())
            .collect();
        assert_eq!(ids, vec!["alpha"], "{bundle:?}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn suite_commands_reach_runtime_with_body_and_metadata() {
        let home = temp_home("suite-runtime");
        let suite_dir = home.join("suites/finance-suite");
        std::fs::create_dir_all(suite_dir.join("commands")).unwrap();
        std::fs::write(
            suite_dir.join("manifest.json"),
            json!({
                "name": "金融行业套件",
                "commands": [
                    { "name": "weekly-report", "description": "生成周报", "content": "第一步：核对数据" },
                    { "name": "risk-check", "description": "风险检查" }
                ]
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(suite_dir.join("commands/risk-check.md"), "先看仓位，再看集中度").unwrap();

        let bundle = build_in(&home.join("awareness"), &home, None, "default", &[]);
        let skills = bundle.runtime["skills"].as_array().unwrap();
        let weekly = skills
            .iter()
            .find(|s| s["id"] == json!("suite:finance-suite:weekly-report"))
            .unwrap();
        assert_eq!(weekly["kind"], json!("command"), "{weekly}");
        assert_eq!(weekly["source"], json!("suite:finance-suite"));
        assert_eq!(weekly["suite"], json!("金融行业套件"));
        assert_eq!(weekly["description"], json!("生成周报"));
        assert!(weekly["content"].as_str().unwrap().contains("核对数据"), "{weekly}");

        let risk = skills
            .iter()
            .find(|s| s["id"] == json!("suite:finance-suite:risk-check"))
            .unwrap();
        assert!(risk["content"].as_str().unwrap().contains("集中度"), "{risk}");
        assert_eq!(bundle.runtime["suiteNotes"], json!([]), "{:?}", bundle.runtime);
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn installed_skill_wins_over_suite_command_and_missing_body_is_visible() {
        let home = temp_home("suite-collision");
        let sk = home.join("skills").join("report");
        std::fs::create_dir_all(&sk).unwrap();
        std::fs::write(
            sk.join("SKILL.md"),
            "---\nname: report\ndescription: 已安装的周报技能\n---\n按技能流程执行\n",
        )
        .unwrap();

        let suite_dir = home.join("suites/demo-suite");
        std::fs::create_dir_all(&suite_dir).unwrap();
        std::fs::write(
            suite_dir.join("manifest.json"),
            json!({
                "name": "演示套件",
                "commands": [
                    { "name": "report", "description": "套件里的同名指令" },
                    { "name": "no-body", "description": "没有正文的指令" }
                ]
            })
            .to_string(),
        )
        .unwrap();

        let bundle = build_in(&home.join("awareness"), &home, None, "default", &[]);
        let skills = bundle.runtime["skills"].as_array().unwrap();
        // 已安装技能优先：同名套件指令不再下发
        assert_eq!(skills.len(), 2, "{skills:?}");
        assert_eq!(skills[0]["kind"], json!("skill"));
        assert_eq!(skills[0]["id"], json!("report"));
        let no_body = &skills[1];
        assert_eq!(no_body["kind"], json!("command"));
        assert_eq!(no_body["content"], json!(""));
        assert!(
            no_body["error"].as_str().unwrap().contains("未提供指令正文"),
            "{no_body}"
        );
        let notes = bundle.runtime["suiteNotes"].as_array().unwrap();
        assert!(
            notes.iter().any(|n| n.as_str().unwrap_or_default().contains("同名")),
            "{notes:?}"
        );
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn mcp_block_without_store_is_empty_but_still_present() {
        // 键必须存在：引擎据此区分「没有 store」与「压根没下发 runtime」
        assert_eq!(mcp_block(None), json!([]));
        let home = temp_home("mcp-none");
        let bundle = build_in(&home.join("awareness"), &home, None, "default", &[]);
        assert_eq!(bundle.runtime["mcp"], json!([]));
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn connected_mcp_server_reaches_the_runtime_bundle() {
        use std::collections::BTreeMap;
        let home = temp_home("mcp-live");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let req = crate::mcp::AddServer {
            name: "echo".to_string(),
            transport: Some("STDIO".to_string()),
            command: Some("python".to_string()),
            args: Some(vec!["-m".to_string(), "echo_server".to_string()]),
            env: Some(BTreeMap::from([("TOKEN".to_string(), "sk-1".to_string())])),
            ..Default::default()
        };
        let row = crate::mcp::add(&store, &req, 1).unwrap();
        // 未连接 → 不下发；连接验证后 → 带取值下发（引擎要靠它拉起子进程）
        assert_eq!(mcp_block(Some(&store)), json!([]));
        crate::mcp::record_probe(
            &store,
            &row.server_id,
            crate::mcp::STATUS_CONNECTED,
            None,
            &[crate::mcp::ToolRow {
                name: "echo".into(),
                description: "回显".into(),
                input_schema: json!({ "type": "object" }),
            }],
            2,
        )
        .unwrap();
        let block = mcp_block(Some(&store));
        assert_eq!(block.as_array().unwrap().len(), 1);
        assert_eq!(block[0]["env"]["TOKEN"], "sk-1");
        assert_eq!(block[0]["tools"][0]["name"], "echo");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn hooks_block_reports_no_channel_when_gate_not_started() {
        use crate::align::hooks;
        let home = temp_home("hooks-block");
        assert_eq!(hooks_block(None), json!({ "configured": 0 }));

        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        hooks::set(
            &store,
            &json!({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command", "command": "guard.sh", "timeout": 5}
            ]}]}}),
            1,
        )
        .unwrap();

        let block = hooks_block(Some(&store));
        assert_eq!(block["configured"], json!(1));
        assert_eq!(block["ungranted"], json!(["guard.sh"]), "没放行就要如实下发，引擎据此点名");
        assert_eq!(block["mountedUnfired"], json!([]));
        // 单测里网关没起：必须给 gateError，绝不能编一个 gateUrl 让引擎以为有执行通道
        assert!(block.get("gateUrl").is_none(), "{block}");
        let err = block["gateError"].as_str().unwrap_or_default();
        assert!(err.contains("未启动"), "{block}");

        let bundle = build_in(&home.join("awareness"), &home, Some(&store), "default", &[]);
        assert_eq!(bundle.runtime["hooks"]["configured"], json!(1), "键必须随每次提交下发");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn sandbox_block_is_always_present_and_defaults_to_no_restriction() {
        // 没有 store（Mock/单测）也必须下发空策略：引擎据此区分「没有限制」与「没收到」
        let block = sandbox_block(None, "default");
        assert_eq!(block["workspaceId"], "default");
        assert_eq!(block["configured"], false);
        assert_eq!(block["toolAllowlist"], json!([]));
        assert_eq!(block["fileBlocklist"], json!([]));

        let home = temp_home("sandbox-none");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let bundle = build_in(&home.join("awareness"), &home, Some(&store), "default", &[]);
        assert_eq!(bundle.runtime["sandbox"]["configured"], false, "{bundle:?}");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn sandbox_policy_reaches_only_its_own_workspace_runtime() {
        use crate::security::policy::{set_sandbox, SandboxPolicy};
        let home = temp_home("sandbox-live");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        set_sandbox(
            &store,
            &SandboxPolicy {
                workspace_id: "team-a".to_string(),
                execution_level: "L1".to_string(),
                file_allowlist: vec!["C:/Users/x/Documents".to_string()],
                file_blocklist: vec!["C:/Windows".to_string()],
                tool_allowlist: vec!["view_text_file".to_string()],
                tool_blocklist: vec!["write_text_file".to_string()],
                exec_enabled: true,
                exec_allowlist: vec!["python".to_string(), "git".to_string()],
                network_enabled: true,
                network_allowlist: vec!["api.example.com".to_string()],
                max_cpu_percent: 50,
                max_memory_mb: 512,
                max_disk_write_mbps: 10,
                updated_at_ms: 0,
            },
        )
        .unwrap();

        let a = build_in(&home.join("awareness"), &home, Some(&store), "team-a", &[]);
        let sb = &a.runtime["sandbox"];
        assert_eq!(sb["configured"], true, "{a:?}");
        assert_eq!(sb["executionLevel"], "L1");
        assert_eq!(sb["toolAllowlist"], json!(["view_text_file"]));
        assert_eq!(sb["toolBlocklist"], json!(["write_text_file"]));
        assert_eq!(sb["fileAllowlist"], json!(["C:/Users/x/Documents"]));
        assert_eq!(sb["fileBlocklist"], json!(["C:/Windows"]));
        assert_eq!(sb["execEnabled"], true);
        assert_eq!(sb["execAllowlist"], json!(["python", "git"]));
        assert_eq!(sb["networkEnabled"], true);
        assert_eq!(sb["networkAllowlist"], json!(["api.example.com"]));

        // 另一个 workspace 不受 team-a 的策略影响
        let b = build_in(&home.join("awareness"), &home, Some(&store), "team-b", &[]);
        assert_eq!(b.runtime["sandbox"]["configured"], false, "{b:?}");
        assert_eq!(b.runtime["sandbox"]["workspaceId"], "team-b");
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn computer_use_block_carries_policy_and_never_invents_a_gate() {
        let home = temp_home("computer-use");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["computerUse"];
        assert_eq!(b["policy"], "ask_every_time", "{b}");
        assert_eq!(b["persisted"], false);
        assert_eq!(b["supported"], json!(crate::computer::supported()));
        assert_eq!(b["platform"], crate::computer::platform());
        // 网关是进程级单例：单测里没有启动过，就不得凭空给出地址或 token
        assert!(b.get("gateToken").is_none(), "{b}");
        assert!(b.get("gateUrl").is_none(), "{b}");

        crate::store::settings::set(&store, crate::computer::POLICY_KEY, "disabled").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["computerUse"];
        assert_eq!(b["policy"], "disabled", "{b}");
        assert_eq!(b["persisted"], true);
        assert!(b.get("gateToken").is_none(), "禁用档位不得下发 token：{b}");

        // 坏档位 fail-closed，并带上可读原因
        crate::store::settings::set(&store, crate::computer::POLICY_KEY, "sometimes").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["computerUse"];
        assert_eq!(b["policy"], "disabled", "{b}");
        assert!(
            b["error"].as_str().unwrap().contains("COMPUTER_USE_INVALID_POLICY"),
            "{b}"
        );
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn browser_use_block_carries_policy_and_never_invents_a_gate() {
        let home = temp_home("browser-use");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["browserUse"];
        assert_eq!(b["policy"], "ask_every_time", "{b}");
        assert_eq!(b["persisted"], false);
        assert_eq!(b["supported"], json!(crate::browser::supported()));
        assert_eq!(b["platform"], crate::browser::platform());
        assert_eq!(b["features"]["snapshot"], true);
        // 网关是进程级单例：单测里没有启动过，就不得凭空给出地址或 token
        assert!(b.get("gateToken").is_none(), "{b}");
        assert!(b.get("gateUrl").is_none(), "{b}");
        assert!(
            std::path::Path::new(b["profileDir"].as_str().unwrap())
                .ends_with(std::path::Path::new("browser").join("profile")),
            "专用配置目录必须在浏览器根目录下：{b}"
        );

        crate::store::settings::set(&store, crate::browser::POLICY_KEY, "disabled").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["browserUse"];
        assert_eq!(b["policy"], "disabled", "{b}");
        assert_eq!(b["persisted"], true);
        assert!(b.get("gateToken").is_none(), "禁用档位不得下发 token：{b}");

        // 坏档位 fail-closed，并带上可读原因
        crate::store::settings::set(&store, crate::browser::POLICY_KEY, "always").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["browserUse"];
        assert_eq!(b["policy"], "disabled", "{b}");
        assert!(
            b["error"]
                .as_str()
                .unwrap()
                .contains("BROWSER_USE_INVALID_POLICY"),
            "{b}"
        );
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn acp_block_fails_closed_and_carries_config_when_enabled() {
        let home = temp_home("acp-block");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        // 默认关闭：引擎据此不注册 delegate_coding_task
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["acp"];
        assert_eq!(b["enabled"], false, "{b}");
        assert_eq!(b["persisted"], false, "{b}");
        assert_eq!(b["permission"], "deny", "{b}");
        assert!(b["auditPath"].as_str().unwrap().ends_with("audit.jsonl"));

        // 启用但没写命令：读取时就报错，绝不能拿空命令去拉进程
        crate::store::settings::set(&store, crate::terminal::ACP_ENABLED_KEY, "true").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["acp"];
        assert_eq!(b["enabled"], false, "{b}");
        assert!(
            b["error"].as_str().unwrap().contains("ACP_COMMAND_MISSING"),
            "{b}"
        );

        crate::store::settings::set(
            &store,
            crate::terminal::ACP_COMMAND_KEY,
            "codex acp --stdio",
        )
        .unwrap();
        crate::store::settings::set(&store, crate::terminal::ACP_PERMISSION_KEY, "allow").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["acp"];
        assert_eq!(b["enabled"], true, "{b}");
        assert_eq!(b["command"], "codex acp --stdio", "{b}");
        assert_eq!(b["permission"], "allow", "{b}");
        assert_eq!(b["timeoutSeconds"], 600, "{b}");
        assert!(b.get("error").is_none(), "{b}");

        // 坏档位 fail-closed
        crate::store::settings::set(&store, crate::terminal::ACP_PERMISSION_KEY, "maybe").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let b = &bundle.runtime["acp"];
        assert_eq!(b["enabled"], false, "{b}");
        assert!(b["error"].as_str().unwrap().contains("ACP_PERMISSION_INVALID"));
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn privacy_and_tool_approval_blocks_reach_runtime() {
        let home = temp_home("privacy-approval");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        assert_eq!(bundle.runtime["privacy"]["mode"], "off", "{bundle:?}");
        assert_eq!(bundle.runtime["privacy"]["persisted"], false);
        assert_eq!(bundle.runtime["toolApproval"]["mode"], "off");
        assert!(bundle.runtime["toolApproval"].get("gateToken").is_none());

        crate::store::settings::set(&store, crate::security::pii::POLICY_KEY, "mask").unwrap();
        crate::store::settings::set(
            &store,
            crate::security::tool_approval::POLICY_KEY,
            "sensitive",
        )
        .unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        assert_eq!(bundle.runtime["privacy"]["mode"], "mask", "{bundle:?}");
        assert_eq!(bundle.runtime["privacy"]["persisted"], true);
        assert_eq!(bundle.runtime["toolApproval"]["mode"], "sensitive");
        // 单测里网关没起：必须给 gateError，不能编一个地址让引擎以为能审批
        assert!(bundle.runtime["toolApproval"].get("gateUrl").is_none());
        assert!(bundle.runtime["toolApproval"]["gateError"]
            .as_str()
            .unwrap()
            .contains("未启动"));

        // 坏档位 fail-closed，并带上可读原因
        crate::store::settings::set(&store, crate::security::pii::POLICY_KEY, "sometimes").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        assert_eq!(bundle.runtime["privacy"]["mode"], "off");
        assert!(bundle.runtime["privacy"]["error"]
            .as_str()
            .unwrap()
            .contains("PII_MODE_INVALID"));
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn agent_policy_defaults_and_valid_settings_reach_runtime() {
        let home = temp_home("agent-policy");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let block = &bundle.runtime["agentPolicy"];
        assert_eq!(block["maxIters"], 8, "{bundle:?}");
        assert_eq!(block["autoCompress"], true);
        assert_eq!(block["lazyMcpTools"], true);
        assert_eq!(block["persisted"], false);
        assert_eq!(block["notes"], json!([]));

        crate::store::settings::set(&store, AGENT_MAX_ITERS_KEY, "16").unwrap();
        crate::store::settings::set(&store, AGENT_AUTO_COMPRESS_KEY, "false").unwrap();
        crate::store::settings::set(&store, AGENT_LAZY_MCP_KEY, "false").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let block = &bundle.runtime["agentPolicy"];
        assert_eq!(block["maxIters"], 16, "{bundle:?}");
        assert_eq!(block["autoCompress"], false);
        assert_eq!(block["lazyMcpTools"], false);
        assert_eq!(block["persisted"], true);
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn agent_policy_invalid_values_are_clamped_with_visible_notes() {
        let home = temp_home("agent-policy-bad");
        let store = crate::store::Store::open(home.join("t.db")).unwrap();
        let aw = home.join("awareness");

        crate::store::settings::set(&store, AGENT_MAX_ITERS_KEY, "99").unwrap();
        crate::store::settings::set(&store, AGENT_AUTO_COMPRESS_KEY, "maybe").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let block = &bundle.runtime["agentPolicy"];
        assert_eq!(block["maxIters"], 32, "{bundle:?}");
        assert_eq!(block["autoCompress"], true, "非法值按默认开启");
        let notes = block["notes"].as_array().unwrap();
        assert!(notes.iter().any(|n| n.as_str().unwrap().contains("超出允许范围")));
        assert!(notes.iter().any(|n| n.as_str().unwrap().contains("不是布尔值")));

        crate::store::settings::set(&store, AGENT_MAX_ITERS_KEY, "many").unwrap();
        let bundle = build_in(&aw, &home, Some(&store), "default", &[]);
        let block = &bundle.runtime["agentPolicy"];
        assert_eq!(block["maxIters"], 8, "{bundle:?}");
        assert!(block["notes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|n| n.as_str().unwrap().contains("不是整数")));
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn awareness_dir_failure_is_passed_through_as_error_reason() {
        // 根目录本身是文件 → create_dir_all 失败，原因必须进 runtime 让引擎播报
        let home = temp_home("aw-fail");
        let blocker = home.join("not-a-dir");
        std::fs::write(&blocker, b"x").unwrap();
        let v = awareness_block(&blocker.join("awareness"));
        assert!(
            v["error"].as_str().unwrap().contains("AWARENESS_DIR_FAILED"),
            "{v}"
        );
        std::fs::remove_dir_all(&home).ok();
    }
}
