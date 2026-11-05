//! 线程内换模型：历史原样保留，按新端点重新编码；接不住的部分必须说清楚。
//!
//! 会话里换模型是常见操作（长文换长窗口、省钱换便宜档、临时换本地模型）。
//! 这一层的职责不是「换模型能不能跑」，而是三条不许悄悄发生的事实：
//!
//! 1. **历史不许因为换模型而丢**：此前轮次的对话按规范条目（role + 正文）带给新端点，
//!    条数与单条长度的口径仍与引擎一致（最近 20 条、单条 600 字）；
//!    超出这个口径的部分如实说明，不假装带上了。
//! 2. **按新端点重新编码**：多模态分段展平为文字、非法角色按引擎口径归一、
//!    空正文条目跳过——每一条都在过程里看得见，而不是静默消失。
//! 3. **工具 schema 接不住时明确提示**：工具名/描述/参数不符合 OpenAI 兼容端点的
//!    命名规则时不下发那一个工具，并说明原因；端点侧真的拒绝工具调用时，
//!    由 `model` 层退回纯文本并回报，本轮绝不能让用户以为文件已经写好了。
//!
//! 这里不猜任何模型的能力：能力事实只有两个来源——本机能算出的窗口配置，
//! 以及端点自己的回答（见 `model::stream` 的整链重试与 `call_degraded` 退回逻辑）。

use serde_json::{json, Value};

/// OpenAI 兼容端点对函数名的约束：`^[a-zA-Z0-9_-]{1,64}$`
pub const TOOL_NAME_MAX_CHARS: usize = 64;
/// 后端愿意接收的历史条目上限（与引擎 `_normalize_history` 的 40 条一致）
pub const MAX_CANONICAL_HISTORY_ITEMS: usize = 40;
/// 历史条目的类型标记：本轮真正带上的正文来源（调试与契约测试都看这个）
pub const HISTORY_ROLE_USER: &str = "user";
pub const HISTORY_ROLE_ASSISTANT: &str = "assistant";

/// 本轮模型的可 fact 画像：只从模型配置里读，不含密钥，也不猜能力。
#[derive(Debug, Clone, Default)]
pub struct Profile {
    pub model_id: String,
    pub display: String,
    pub model_name: String,
    pub base_url: String,
    /// 配置里声明的上下文窗口（None = 没填或填得不合法）
    pub context_window: Option<u64>,
    /// 是否真的拿到了一个已保存模型（false = 走环境变量兜底，没有 modelId）
    pub known: bool,
    /// 需要让用户在本轮过程里看到的话（口径问题、不可用配置等）
    pub notes: Vec<String>,
}

fn text_of(obj: &Value, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|k| obj.get(*k).and_then(Value::as_str))
        .unwrap_or("")
        .trim()
        .to_string()
}

fn window_of(cfg: &Value) -> Option<u64> {
    let n = cfg.get("contextWindow").and_then(Value::as_u64)?;
    (n >= crate::harness::compress::MIN_CONTEXT_WINDOW
        && n <= crate::harness::compress::MAX_CONTEXT_WINDOW)
        .then_some(n)
}

impl Profile {
    /// 界面与提示里指代这个模型的说法
    pub fn label(&self) -> String {
        if !self.display.is_empty() {
            return self.display.clone();
        }
        if !self.model_name.is_empty() {
            return self.model_name.clone();
        }
        "环境变量指定的模型".to_string()
    }

    /// 窗口大小的人类可读写法（1_000_000 → 1,000,000）
    fn window_text(&self) -> String {
        match self.context_window {
            Some(w) => group_thousands(w),
            None => "未填（按 8,192 估算）".to_string(),
        }
    }
}

fn group_thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// 解析本轮模型画像；`Value::Null`（环境变量兜底）也返回可用画像并留一条说明。
pub fn profile_of(cfg: &Value) -> Profile {
    if !cfg.is_object() {
        return Profile {
            notes: vec![
                "本轮没有可选的已保存模型，按环境变量 TT_BASE_URL/TT_MODEL 调用；\
                 换模型请在「设置 → 模型与用量」里添加后在输入框选择。"
                    .to_string(),
            ],
            ..Default::default()
        };
    }
    let raw_window = cfg.get("contextWindow");
    let context_window = window_of(cfg);
    let mut notes = Vec::new();
    if raw_window.is_some() && context_window.is_none() {
        notes.push(format!(
            "模型配置的上下文窗口 {} 不可用（允许 {}~{}），本轮压缩阈值按 8,192 tokens 估算。",
            raw_window.map(|v| v.to_string()).unwrap_or_default(),
            crate::harness::compress::MIN_CONTEXT_WINDOW,
            crate::harness::compress::MAX_CONTEXT_WINDOW
        ));
    }
    let fallback_windows: Vec<u64> = cfg
        .get("fallbacks")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter_map(window_of).collect())
        .unwrap_or_default();
    if let (Some(min), Some(w)) = (fallback_windows.iter().min(), context_window) {
        if *min < w {
            notes.push(format!(
                "容灾链里最小的上下文窗口是 {} tokens：一旦转移，本轮带着的历史必须装得下，\
                 压缩阈值按它算。",
                group_thousands(*min)
            ));
        }
    }
    Profile {
        model_id: text_of(cfg, &["modelId", "model_id"]),
        display: text_of(cfg, &["displayName", "display_name"]),
        model_name: text_of(cfg, &["modelName", "model_name", "model"]),
        base_url: text_of(cfg, &["baseUrl", "base_url"]),
        context_window,
        known: !text_of(cfg, &["modelName", "model_name", "model"]).is_empty(),
        notes,
    }
}

/// 两个画像是否同一个模型：优先比 id，id 缺失时比「服务端模型名 + 接入地址」。
pub fn same_model(a: &Profile, b: &Profile) -> bool {
    if !a.known || !b.known {
        return true; // 有一方是环境变量兜底：没有可比的事实，不编一条「切换」出来
    }
    if !a.model_id.is_empty() && !b.model_id.is_empty() {
        return a.model_id == b.model_id;
    }
    a.model_name == b.model_name && a.base_url == b.base_url
}

/// 线程内换模型的可见说明：历史带没带、窗口怎么变、阈值往哪走都说清。
/// `trigger` 为本轮实际压缩阈值（None = 压缩关闭，不报这个数）。
pub fn switch_note(from: &Profile, to: &Profile, trigger: Option<u64>) -> String {
    let mut out = format!(
        "本轮模型由「{}」切换为「{}」：此前轮次的对话历史不会因换模型丢失，\
         按 OpenAI 兼容格式原样带上并重新编码。",
        from.label(),
        to.label()
    );
    if from.context_window != to.context_window {
        out.push_str(&format!(
            "上下文窗口 {} → {}。",
            from.window_text(),
            to.window_text()
        ));
    }
    if let Some(t) = trigger {
        out.push_str(&format!("本轮压缩阈值为 {} tokens。", group_thousands(t)));
    }
    out
}

/// 换模型续跑的说明：检查点是上一个模型跑出来的，快照里的对话仍按原样继续。
pub fn resume_switch_note(from: &Profile, to: &Profile, messages: usize) -> String {
    format!(
        "中断前的对话由「{}」产出，本轮改用「{}」继续：检查点里的 {} 条消息原样保留、\
         按新端点重新编码后接着跑，已执行成功的副作用工具仍按签名跳过，不会重复落盘。",
        from.label(),
        to.label(),
        messages
    )
}

fn content_to_text(content: &Value) -> (String, usize, usize) {
    match content {
        Value::String(s) => (s.clone(), 0, 0),
        Value::Array(parts) => {
            let mut texts = Vec::new();
            let mut images = 0usize;
            let mut others = 0usize;
            for part in parts {
                match part.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        let t = part
                            .get("text")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        if !t.trim().is_empty() {
                            texts.push(t);
                        }
                    }
                    Some("image_url") | Some("image") => images += 1,
                    _ => {
                        // 分段里直接写 content 的变体（部分网关）也认文本
                        let t = part.get("text").and_then(Value::as_str).unwrap_or("");
                        if !t.trim().is_empty() {
                            texts.push(t.to_string());
                        } else {
                            others += 1;
                        }
                    }
                }
            }
            (texts.join("\n"), images, others)
        }
        _ => (String::new(), 0, 1),
    }
}

/// 历史重新编码：口径（条数/单条长度）与引擎一致，被改写的部分逐条留说明。
///
/// 返回的条目只含 `role` 与 `content`；`max_items`/`max_item_chars` 由调用方给出
/// （`prompt::history_messages` 传引擎同款 20/600），超出上限的更早历史如实报数。
pub fn encode_history(
    history: &[Value],
    max_items: usize,
    max_item_chars: usize,
) -> (Vec<Value>, Vec<String>) {
    let mut notes: Vec<String> = Vec::new();
    if history.len() > MAX_CANONICAL_HISTORY_ITEMS {
        notes.push(format!(
            "对话历史 {} 条超过后端接收上限 {} 条，只带最近 {} 条。",
            history.len(),
            MAX_CANONICAL_HISTORY_ITEMS,
            MAX_CANONICAL_HISTORY_ITEMS
        ));
    }
    let capped = std::cmp::min(history.len(), MAX_CANONICAL_HISTORY_ITEMS);
    let start = history.len().saturating_sub(capped);
    let scoped = &history[start..];
    let keep_from = scoped.len().saturating_sub(max_items);
    if keep_from > 0 {
        notes.push(format!(
            "更早的 {keep_from} 条历史超出「最近 {max_items} 条」的带上范围，本轮未进入上下文\
             （与引擎同一口径；压缩摘要不在此列）。"
        ));
    }
    let mut out: Vec<Value> = Vec::new();
    for (i, item) in scoped[keep_from..].iter().enumerate() {
        let seq = keep_from + i + 1;
        if !item.is_object() {
            notes.push(format!("第 {seq} 条历史不是对话条目，本轮未带上。"));
            continue;
        }
        let raw_role = item.get("role").and_then(Value::as_str).unwrap_or("");
        let (text, images, others) = content_to_text(
            item.get("content").unwrap_or(&Value::Null),
        );
        let mut content = text.trim().to_string();
        if images > 0 || others > 0 {
            let mut dropped = Vec::new();
            if images > 0 {
                dropped.push(format!("{images} 段图片"));
            }
            if others > 0 {
                dropped.push(format!("{others} 段无法识别的分段"));
            }
            notes.push(format!(
                "第 {seq} 条历史是分段消息，只取其中的文字带上（未带上 {}）；\
                 本地 harness 不发图片，需要看图请在本轮启用引擎运行时。",
                dropped.join("、")
            ));
        }
        if content.is_empty() {
            notes.push(format!("第 {seq} 条历史没有可带上的正文，本轮未带上。"));
            continue;
        }
        if content.chars().count() > max_item_chars {
            content = content.chars().take(max_item_chars).collect::<String>()
                + "…（已截断）";
            notes.push(format!(
                "第 {seq} 条历史超出单条 {max_item_chars} 字，已截断带上（与引擎同一口径）。"
            ));
        }
        let role = match raw_role {
            HISTORY_ROLE_USER | HISTORY_ROLE_ASSISTANT => raw_role,
            other => {
                if !other.trim().is_empty() {
                    notes.push(format!(
                        "第 {seq} 条历史的角色「{other}」不是对话角色，按用户消息带上\
                         （与引擎同一归一）。"
                    ));
                }
                HISTORY_ROLE_USER
            }
        };
        out.push(json!({ "role": role, "content": content }));
    }
    (out, notes)
}

fn tool_name_ok(name: &str) -> bool {
    !name.is_empty()
        && name.chars().count() <= TOOL_NAME_MAX_CHARS
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// 工具 schema 预检：不符合 OpenAI 兼容端点规则的工具不下发，并逐个说明原因。
///
/// 这里只做「端点会不会因此整轮失败」的判断，不判断模型用得好不好——
/// 后者是端点自己的事，真被拒了由 `model` 层退回纯文本并如实回报。
pub fn validate_tools(specs: &[Value]) -> (Vec<Value>, Vec<String>) {
    let mut kept: Vec<Value> = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for spec in specs {
        let function = spec.get("function").unwrap_or(spec);
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let label = if name.is_empty() {
            "（未命名工具）".to_string()
        } else {
            format!("工具「{name}」")
        };
        if name.is_empty() {
            notes.push(format!("{label} 没有名字，本轮不下发。"));
            continue;
        }
        if !tool_name_ok(&name) {
            notes.push(format!(
                "{label} 的名称不符合端点规则（仅允许字母、数字、下划线与连字符，\
                 长度 1~{TOOL_NAME_MAX_CHARS}），本轮不下发该工具。"
            ));
            continue;
        }
        if seen.contains(&name) {
            notes.push(format!("{label} 重复下发，本轮只保留第一个。"));
            continue;
        }
        let description = function
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if description.is_empty() {
            notes.push(format!(
                "{label} 没有说明，模型无从判断何时调用，本轮不下发该工具。"
            ));
            continue;
        }
        let params = function.get("parameters");
        let usable = match params {
            Some(v) => v.is_object(),
            // 没写 parameters 的裸函数按「无参数」处理，端点仍能接受
            None => true,
        };
        if !usable {
            notes.push(format!(
                "{label} 的参数描述不是 JSON 对象，端点会拒收整份工具清单，本轮不下发该工具。"
            ));
            continue;
        }
        seen.push(name);
        kept.push(spec.clone());
    }
    (kept, notes)
}

/// 工具全部下不了发时的兜底说明（本轮只能文本回答，必须写在脸上）。
pub fn no_tools_note(profile: &Profile) -> String {
    format!(
        "本轮没有可下发的工具：模型「{}」只能文本回答，文件不会被真实读写。\
         请检查工具配置或换一个 OpenAI 兼容的工具调用端点。",
        profile.label()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> Value {
        json!({
            "modelId": "m-1",
            "displayName": "通义千问 Plus",
            "modelName": "qwen-plus",
            "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "contextWindow": 1_000_000
        })
    }

    #[test]
    fn profile_reads_identity_and_window() {
        let p = profile_of(&cfg());
        assert_eq!(p.model_id, "m-1");
        assert_eq!(p.label(), "通义千问 Plus");
        assert_eq!(p.context_window, Some(1_000_000));
        assert!(p.known);
        assert!(p.notes.is_empty(), "{:?}", p.notes);
    }

    #[test]
    fn bad_window_is_reported_not_silently_clamped() {
        let mut c = cfg();
        c["contextWindow"] = json!(7);
        let p = profile_of(&c);
        assert_eq!(p.context_window, None);
        assert!(p.notes[0].contains("不可用"), "{:?}", p.notes);
        // 窗口没填时不编说明：缺省估算由压缩策略那条说明负责
        let mut c2 = cfg();
        c2.as_object_mut().unwrap().remove("contextWindow");
        assert!(profile_of(&c2).notes.is_empty());
    }

    #[test]
    fn null_config_reports_env_fallback_without_inventing_a_model() {
        let p = profile_of(&Value::Null);
        assert!(!p.known);
        assert_eq!(p.label(), "环境变量指定的模型");
        assert!(p.notes[0].contains("TT_MODEL"));
        // 兜底与具名模型之间不报「切换」：没有可比的事实
        assert!(same_model(&p, &profile_of(&cfg())));
    }

    #[test]
    fn same_model_prefers_id_then_name_and_host() {
        let a = profile_of(&cfg());
        let mut b = cfg();
        b["displayName"] = json!("同一个模型的别名");
        assert!(same_model(&a, &profile_of(&b)));
        let mut c = cfg();
        c["modelId"] = json!("m-2");
        c["modelName"] = json!("qwen-max");
        assert!(!same_model(&a, &profile_of(&c)));
        // 没有 id 的自定义端点：按「模型名 + 接入地址」判定，两项都相同才算同一台
        let d = json!({ "modelName": "qwen-plus", "baseUrl": "https://x.test/v1" });
        let e = json!({ "modelName": "qwen-plus", "baseUrl": "https://y.test/v1" });
        assert!(
            !same_model(&profile_of(&d), &profile_of(&e)),
            "同名但接入地址不同：是两台服务，要报切换"
        );
        let f = json!({
            "modelName": "qwen-plus", "baseUrl": "https://x.test/v1", "displayName": "自家别名",
        });
        assert!(same_model(&profile_of(&d), &profile_of(&f)));
        let g = json!({ "modelName": "qwen-turbo", "baseUrl": "https://x.test/v1" });
        assert!(!same_model(&profile_of(&d), &profile_of(&g)));
    }

    #[test]
    fn switch_note_states_history_is_kept_and_window_changes() {
        let from = profile_of(&cfg());
        let mut to_cfg = cfg();
        to_cfg["modelId"] = json!("m-2");
        to_cfg["displayName"] = json!("本地小模型");
        to_cfg["contextWindow"] = json!(32768);
        let to = profile_of(&to_cfg);
        let note = switch_note(&from, &to, Some(27_904));
        assert!(note.contains("通义千问 Plus"), "{note}");
        assert!(note.contains("本地小模型"), "{note}");
        assert!(note.contains("不会因换模型丢失"), "{note}");
        assert!(note.contains("1,000,000 → 32,768"), "{note}");
        assert!(note.contains("27,904"), "{note}");
        // 同窗口不报窗口变化；同一模型不产生这条说明
        assert!(!switch_note(&from, &from, None).contains("→"));
    }

    #[test]
    fn resume_switch_note_carries_message_count() {
        let from = profile_of(&cfg());
        let mut to_cfg = cfg();
        to_cfg["modelId"] = json!("m-2");
        to_cfg["displayName"] = json!("备用模型");
        let note = resume_switch_note(&from, &profile_of(&to_cfg), 12);
        assert!(note.contains("12 条消息原样保留"), "{note}");
        assert!(note.contains("按签名跳过"), "{note}");
    }

    #[test]
    fn encode_history_keeps_engine_parity_and_reports_rewrites() {
        let history = vec![
            json!({"role": "user", "content": "第一问"}),
            json!({"role": "assistant", "content": [{"type": "text", "text": "分段答"}, {"type": "image_url"}]}),
            json!({"role": "tool", "content": "工具原文"}),
            json!("不是条目"),
            json!({"role": "user", "content": "   "}),
            json!({"role": "user", "content": "字".repeat(700)}),
        ];
        let (out, notes) = encode_history(&history, 20, 600);
        // 引擎同款归一：非法角色 → user、非法条目与空正文才丢掉，其余一条不少
        assert_eq!(out.len(), 4, "{out:?}");
        assert_eq!(out[0]["content"], "第一问");
        // 分段消息只取文字（图片不发）；非法角色按引擎口径归一为 user 并留说明
        assert_eq!(out[1]["content"], "分段答");
        assert_eq!(out[1]["role"], "assistant");
        assert_eq!(out[2]["role"], "user");
        assert_eq!(out[2]["content"], "工具原文");
        assert_eq!(out[3]["role"], "user");
        assert!(out[3]["content"].as_str().unwrap().ends_with("（已截断）"));
        assert!(notes.iter().any(|n| n.contains("分段消息")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("不是对话条目")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("没有可带上的正文")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("不是对话角色")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("单条 600 字")), "{notes:?}");
    }

    #[test]
    fn encode_history_reports_both_caps() {
        let history: Vec<Value> = (0..45)
            .map(|i| json!({"role": "user", "content": format!("t{i}")}))
            .collect();
        let (out, notes) = encode_history(&history, 20, 600);
        assert_eq!(out.len(), 20);
        assert_eq!(out[19]["content"], "t44");
        assert!(
            notes.iter().any(|n| n.contains("45 条超过后端接收上限 40 条")),
            "{notes:?}"
        );
        assert!(notes.iter().any(|n| n.contains("更早的 20 条历史")), "{notes:?}");
    }

    #[test]
    fn validate_tools_drops_only_the_offenders() {
        let specs = vec![
            json!({"type": "function", "function": {"name": "write_text_file", "description": "写", "parameters": {"type": "object"}}}),
            json!({"type": "function", "function": {"name": "好的工具", "description": "中文名", "parameters": {}}}),
            json!({"type": "function", "function": {"name": "x".repeat(70), "description": "太长", "parameters": {}}}),
            json!({"type": "function", "function": {"name": "use_skill", "description": "  ", "parameters": {}}}),
            json!({"type": "function", "function": {"name": "list_directory", "description": "列", "parameters": []}}),
            json!({"type": "function", "function": {"name": "write_text_file", "description": "重复", "parameters": {}}}),
        ];
        let (kept, notes) = validate_tools(&specs);
        let names: Vec<&str> = kept
            .iter()
            .map(|k| k["function"]["name"].as_str().unwrap_or(""))
            .collect();
        assert_eq!(names, vec!["write_text_file"]);
        assert_eq!(notes.len(), 5, "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("端点规则")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("长度 1~64")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("没有说明")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("不是 JSON 对象")), "{notes:?}");
        assert!(notes.iter().any(|n| n.contains("只保留第一个")), "{notes:?}");
    }

    #[test]
    fn harness_builtin_tools_pass_validation_untouched() {
        // 本机 7 个工具必须一个都不被预检拦下：拦下就等于功能缺失，是回归
        let specs = vec![
            "list_directory",
            "view_text_file",
            "read_document",
            "write_text_file",
            "insert_text_file",
            "delete_text_file",
            "use_skill",
        ];
        let built: Vec<Value> = specs
            .iter()
            .map(|n| {
                json!({"type": "function", "function": {
                    "name": n, "description": format!("{n} 的说明"),
                    "parameters": {"type": "object", "properties": {}}
                }})
            })
            .collect();
        let (kept, notes) = validate_tools(&built);
        assert_eq!(kept.len(), 7, "{notes:?}");
        assert!(notes.is_empty(), "{notes:?}");
    }

    #[test]
    fn numbers_are_grouped_for_reading() {
        assert_eq!(group_thousands(1_000_000), "1,000,000");
        assert_eq!(group_thousands(32_768), "32,768");
        assert_eq!(group_thousands(512), "512");
    }
}
