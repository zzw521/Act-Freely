//! harness 的提示词与消息装配。
//!
//! 系统提示 = 基础人设 + 当前时间 + 意识（协作风格/画像/手册/记忆）+ 技能清单
//! + 工作台模式 + 工作目录；执行消息 = 此前轮次历史 + 目标 + 已批准计划 + 附件正文。
//! 技能正文不进 prompt，由 use_skill 按需取，避免「装 20 个技能就烧掉 20 段上下文」。

use serde_json::{json, Value};

use crate::harness::tools::Skill;

const MAX_AWARENESS_FILE_CHARS: usize = 6000;
const MAX_MEMORY_FREEFORM_CHARS: usize = 3000;
const MAX_SKILL_LIST_ITEMS: usize = 40;
const MAX_HISTORY_ITEMS: usize = 20;
const MAX_HISTORY_ITEM_CHARS: usize = 600;

const AWARENESS_SECTIONS: [(&str, &str); 4] = [
    ("SOUL.md", "协作风格"),
    ("USER_PROFILE.md", "用户画像"),
    ("WORK_MANUAL.md", "工作手册"),
    ("MEMORY.md", "长期记忆"),
];

const BASE_PROMPT: &str = "你是“通天办公”AI 工作助手，运行在用户的本地电脑上。

工作方式:
1. 分析用户目标，必要时先查看相关文件再动手
2. 使用工具完成任务（读取/写入/修改文件；需要某技能时先取技能正文）
3. 涉及删除或覆盖文件前务必谨慎，优先保留原内容
4. 完成后用简洁中文总结做了什么、产出了哪些文件

约束:
- 只在当前工作目录范围内操作文件
- 用户要求创建/生成/保存文件时，必须调用文件写入工具真实写入，禁止仅在回复中声称已写入
- 声称任何文件操作完成前，必须先确认对应工具调用已成功执行
";

fn one_line(text: &str, limit: usize) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= limit {
        flat
    } else {
        let mut out: String = flat.chars().take(limit).collect();
        out.push('…');
        out
    }
}

fn truncated(text: &str, limit: usize, label: &str) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let mut out: String = text.chars().take(limit).collect();
    out.push_str(&format!(
        "\n…（{label}已截断，原文共 {} 字）",
        text.chars().count()
    ));
    out
}

fn mode_of(raw: &str) -> &'static str {
    match raw.trim().to_lowercase().as_str() {
        "design" => "design",
        "slides" => "slides",
        "writing" => "writing",
        _ => "general",
    }
}

fn mode_prompt(mode: &str) -> &'static str {
    match mode {
        "design" => {
            "\n\n## 工作台模式：设计\n\
             本轮按设计模式工作：优先产出可直接在浏览器打开的单文件 HTML/CSS 原型\
             （响应式布局、真实可交互），把页面写入工作目录的 .html 文件。\
             可以说明配色、字体与组件，但不要声称调用了画布或设计工具，也不要把 HTML 说成图片稿。"
        }
        "writing" => {
            "\n\n## 工作台模式：写作\n\
             本轮按写作模式工作：先给结构大纲，再写完整正文；长文落成 .md 文件，\
             回复里给摘要与文件位置。润色/改写类请求保留原意，不擅自增删事实。"
        }
        _ => "",
    }
}

/// 本轮启用的技能/套件指令（正文随包，use_skill 直接取）。
pub fn skills_of(runtime: &Value) -> Vec<Skill> {
    let Some(raw) = runtime.get("skills").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut out: Vec<Skill> = Vec::new();
    for item in raw {
        if item.get("scanBlocked").and_then(|v| v.as_bool()) == Some(true) {
            continue;
        }
        let id = item
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if id.is_empty() || out.iter().any(|s| s.id == id) {
            continue;
        }
        out.push(Skill {
            id: id.clone(),
            name: item
                .get("name")
                .and_then(|v| v.as_str())
                .filter(|s| !s.trim().is_empty())
                .unwrap_or(&id)
                .to_string(),
            description: item
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string(),
            content: item
                .get("content")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        });
    }
    out
}

/// 技能清单没能完整进入执行面的原因（界面按步骤事件摊出来）。
pub fn skill_notes(runtime: &Value) -> Vec<String> {
    let mut notes = Vec::new();
    let Some(raw) = runtime.get("skills").and_then(|v| v.as_array()) else {
        return notes;
    };
    let blocked: Vec<String> = raw
        .iter()
        .filter(|s| s.get("scanBlocked").and_then(|v| v.as_bool()) == Some(true))
        .map(|s| {
            format!(
                "{}（{}）",
                s.get("id").and_then(|v| v.as_str()).unwrap_or(""),
                s.get("name").and_then(|v| v.as_str()).unwrap_or("")
            )
        })
        .collect();
    if !blocked.is_empty() {
        notes.push(format!(
            "技能被安全扫描判定为高风险，正文未进入执行面：{}；请修改 SKILL.md 后重新安装或先卸载",
            blocked.join("、")
        ));
    }
    for item in raw {
        if item.get("scanBlocked").and_then(|v| v.as_bool()) == Some(true) {
            continue;
        }
        let err = item.get("error").and_then(|v| v.as_str()).unwrap_or("").trim();
        if !err.is_empty() {
            notes.push(format!(
                "技能「{}」未提供正文：{err}",
                item.get("id").and_then(|v| v.as_str()).unwrap_or("")
            ));
        }
    }
    let total = raw
        .iter()
        .filter(|s| s.get("scanBlocked").and_then(|v| v.as_bool()) != Some(true))
        .count();
    if total > MAX_SKILL_LIST_ITEMS {
        notes.push(format!(
            "技能/套件指令 {total} 个超过 {MAX_SKILL_LIST_ITEMS} 个，仅前 {MAX_SKILL_LIST_ITEMS} 个进入清单"
        ));
    }
    notes
}

fn awareness_prompt(runtime: &Value) -> (String, Vec<String>) {
    let Some(block) = runtime.get("awareness") else {
        return (String::new(), Vec::new());
    };
    if !block.is_object() {
        return (String::new(), Vec::new());
    }
    if block.get("mode").and_then(|v| v.as_bool()) == Some(false) {
        return (
            String::new(),
            vec!["意识开关已关闭，本轮未注入协作风格/画像/手册/记忆".to_string()],
        );
    }
    if let Some(err) = block.get("error").and_then(|v| v.as_str()) {
        if !err.trim().is_empty() {
            return (
                String::new(),
                vec![format!("意识内容读取失败，本轮未注入：{err}")],
            );
        }
    }
    let empty = serde_json::Map::new();
    let files = block
        .get("files")
        .and_then(|v| v.as_object())
        .unwrap_or(&empty);
    let mut out = String::new();
    let mut notes = Vec::new();
    for (name, title) in AWARENESS_SECTIONS {
        let content = files
            .get(name)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if content.is_empty() {
            continue;
        }
        let limit = if name == "MEMORY.md" {
            MAX_MEMORY_FREEFORM_CHARS
        } else {
            MAX_AWARENESS_FILE_CHARS
        };
        out.push_str(&format!("\n\n## {title}\n{}", truncated(&content, limit, title)));
    }
    (out, notes.drain(..).collect())
}

fn skills_prompt(skills: &[Skill]) -> String {
    if skills.is_empty() {
        return String::new();
    }
    let lines: Vec<String> = skills
        .iter()
        .take(MAX_SKILL_LIST_ITEMS)
        .map(|s| {
            format!(
                "- {}（{}）：{}",
                s.id,
                s.name,
                if s.description.is_empty() {
                    "（SKILL.md 未写描述）"
                } else {
                    s.description.as_str()
                }
            )
        })
        .collect();
    format!(
        "\n\n## 可用技能与套件指令\n\
         用户已安装并启用了以下技能。正文是具体做法，需要按某个技能干活时，\
         先调用 use_skill 获取其正文，再据此执行；不要凭名称臆测流程。\n\n{}",
        lines.join("\n")
    )
}

/// 系统提示 + 未能注入部分的说明（说明由调用方摊成可见步骤）。
pub fn system_prompt(runtime: &Value, mode: &str, work_dir: &std::path::Path) -> (String, Vec<String>) {
    let weekdays = ["日", "一", "二", "三", "四", "五", "六"];
    let now = crate::harness::clock::local_now();
    let time_hint = format!(
        "\n\n当前时间：{}，星期{}。涉及日期、时间或“今天/本周/本月”等表述时以此为准；\
         生成带日期的文件名时使用该日期。",
        now.0, weekdays[now.1]
    );
    let mut notes = Vec::new();
    let (awareness, awareness_notes) = awareness_prompt(runtime);
    notes.extend(awareness_notes);
    let skills = skills_of(runtime);
    notes.extend(skill_notes(runtime));
    let mut prompt = format!(
        "{BASE_PROMPT}{time_hint}{awareness}{}",
        skills_prompt(&skills)
    );
    prompt.push_str(mode_prompt(mode_of(mode)));
    prompt.push_str(&format!(
        "\n\n## 工作目录\n本轮所有文件操作都限定在：{}\n",
        work_dir.display()
    ));
    (prompt, notes)
}

/// 历史轮次（截断口径与引擎一致：最近 20 条，单条 600 字）。
pub fn history_messages(history: &[Value]) -> Vec<Value> {
    let start = history.len().saturating_sub(MAX_HISTORY_ITEMS);
    history[start..]
        .iter()
        .filter_map(|h| {
            let mut content = h.get("content").and_then(|v| v.as_str())?.trim().to_string();
            if content.is_empty() {
                return None;
            }
            if content.chars().count() > MAX_HISTORY_ITEM_CHARS {
                content = content.chars().take(MAX_HISTORY_ITEM_CHARS).collect::<String>()
                    + "…（已截断）";
            }
            let role = match h.get("role").and_then(|v| v.as_str()) {
                Some("assistant") => "assistant",
                _ => "user",
            };
            Some(json!({ "role": role, "content": content }))
        })
        .collect()
}

/// 附件正文 + 未进入模型的说明（只认 Rust 抽取好的 text；图片本地 harness 不下发）。
pub fn attachments_text(attachments: &[Value]) -> (String, Vec<String>) {
    let mut parts: Vec<String> = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    for att in attachments {
        let name = att.get("fileName").and_then(|v| v.as_str()).unwrap_or("附件");
        if let Some(err) = att.get("error").and_then(|v| v.as_str()) {
            if !err.trim().is_empty() {
                notes.push(format!("附件「{name}」未能读取正文：{err}"));
                continue;
            }
        }
        if att.get("image").map(|v| v.is_object()).unwrap_or(false) {
            notes.push(format!(
                "图片「{name}」未进入本地 harness（本模式只发送文本；需要看图请在本轮启用引擎运行时）"
            ));
            continue;
        }
        let text = att.get("text").and_then(|v| v.as_str()).unwrap_or("").trim();
        if text.is_empty() {
            let note = att.get("note").and_then(|v| v.as_str()).unwrap_or("");
            notes.push(format!(
                "附件「{name}」正文未进入模型{}",
                if note.is_empty() {
                    "（Rust 侧未抽取到正文）".to_string()
                } else {
                    format!("：{note}")
                }
            ));
            continue;
        }
        parts.push(format!("### 附件：{name}\n{text}"));
    }
    (parts.join("\n\n"), notes)
}

/// 执行阶段的用户消息：目标 + 已批准计划 + 附件正文。
pub fn user_message(goal: &str, attachments: &[Value], plan: &[String]) -> (String, Vec<String>) {
    let (attach_text, notes) = attachments_text(attachments);
    let mut out = goal.trim().to_string();
    if !plan.is_empty() {
        out.push_str("\n\n已批准的执行计划：\n");
        for (i, step) in plan.iter().enumerate() {
            out.push_str(&format!("{}. {}\n", i + 1, step));
        }
    }
    if !attach_text.is_empty() {
        out.push_str("\n\n以下是用户随本轮消息提供的材料：\n");
        out.push_str(&attach_text);
        out.push('\n');
    }
    (out, notes)
}

/// 计划生成的消息：要求返回严格的 JSON，解析失败如实报错，不伪造步骤。
pub fn plan_messages(system: &str, goal: &str, attachments: &[Value]) -> Vec<Value> {
    let (attach_text, _) = attachments_text(attachments);
    let mut user = format!(
        "请把下面的任务拆成 3~6 个可执行步骤，按执行顺序给出。\n\
         只输出 JSON，形如：{{\"steps\":[{{\"type\":\"llm_call\",\"description\":\"…\"}}]}}。\n\
         type 只能取：llm_call、file_read、file_write、file_convert、file_organize。\n\
         步骤描述用简洁中文，写清要做什么，不要写工具参数。\n\n任务：{}",
        goal.trim()
    );
    if !attach_text.is_empty() {
        user.push_str("\n\n材料：\n");
        user.push_str(&attach_text);
    }
    vec![
        json!({ "role": "system", "content": system }),
        json!({ "role": "user", "content": user }),
    ]
}

#[derive(Debug, Clone)]
pub struct PlanStep {
    pub id: String,
    pub step_type: String,
    pub description: String,
}

const STEP_TYPES: [&str; 5] = [
    "llm_call",
    "file_read",
    "file_write",
    "file_convert",
    "file_organize",
];

fn strip_fence(text: &str) -> &str {
    let trimmed = text.trim();
    if !trimmed.starts_with("```") {
        return trimmed;
    }
    let after = trimmed
        .find('\n')
        .map(|i| &trimmed[i + 1..])
        .unwrap_or("");
    after.trim_end_matches("```").trim()
}

pub fn parse_plan(text: &str) -> Result<Vec<PlanStep>, String> {
    let raw = strip_fence(text);
    let start = raw.find('{').ok_or_else(|| {
        format!(
            "PLAN_INVALID: 模型返回的计划不是 JSON：{}",
            one_line(raw, 160)
        )
    })?;
    let end = raw.rfind('}').ok_or_else(|| {
        format!(
            "PLAN_INVALID: 模型返回的计划不是完整 JSON：{}",
            one_line(raw, 160)
        )
    })?;
    let parsed: Value = serde_json::from_str(&raw[start..=end]).map_err(|e| {
        format!(
            "PLAN_INVALID: 计划 JSON 解析失败（{e}）：{}",
            one_line(&raw[start..=end], 160)
        )
    })?;
    let arr = parsed
        .get("steps")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "PLAN_INVALID: 计划里没有 steps 数组".to_string())?;
    let mut out = Vec::new();
    for (i, item) in arr.iter().take(8).enumerate() {
        let description = item
            .get("description")
            .or_else(|| item.get("title"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if description.is_empty() {
            continue;
        }
        let step_type = item
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("llm_call");
        let step_type = if STEP_TYPES.contains(&step_type) {
            step_type
        } else {
            "llm_call"
        };
        out.push(PlanStep {
            id: format!("step-{}", i + 1),
            step_type: step_type.to_string(),
            description: description.to_string(),
        });
    }
    if out.is_empty() {
        return Err("PLAN_INVALID: 模型没有给出任何可执行步骤".to_string());
    }
    Ok(out)
}

/// 计划步骤 → 事件数组（modify 后重排也保持顺序稳定）。
pub fn plan_to_events(steps: &[PlanStep]) -> Vec<Value> {
    steps
        .iter()
        .enumerate()
        .map(|(i, s)| {
            json!({
                "id": s.id,
                "order": i + 1,
                "type": s.step_type,
                "description": s.description,
            })
        })
        .collect()
}

/// 执行阶段对模型的计划约束文本（modify 后的口径）。
pub fn plan_descriptions(steps: &[PlanStep]) -> Vec<String> {
    steps.iter().map(|s| s.description.clone()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_plan_plain_and_fenced() {
        let plain = r#"{"steps":[{"type":"file_read","description":"读取周报数据"},{"type":"file_write","description":"写出周报"}]}"#;
        let steps = parse_plan(plain).unwrap();
        assert_eq!(steps.len(), 2);
        assert_eq!(steps[0].id, "step-1");
        assert_eq!(steps[0].step_type, "file_read");
        let fenced = format!("```json\n{plain}\n```");
        assert_eq!(parse_plan(&fenced).unwrap().len(), 2);
    }

    #[test]
    fn test_parse_plan_rejects_garbage_and_empty() {
        assert!(parse_plan("好的，我来计划一下").unwrap_err().contains("PLAN_INVALID"));
        assert!(parse_plan(r#"{"steps":[]}"#).unwrap_err().contains("PLAN_INVALID"));
        assert!(parse_plan(r#"{"steps":[{"description":"  "}]}"#)
            .unwrap_err()
            .contains("PLAN_INVALID"));
    }

    #[test]
    fn test_parse_plan_unknown_type_falls_back() {
        let steps = parse_plan(r#"{"steps":[{"type":"run_shell","description":"执行"}]}"#).unwrap();
        assert_eq!(steps[0].step_type, "llm_call");
    }

    #[test]
    fn test_history_truncates() {
        let history: Vec<Value> = (0..25)
            .map(|i| json!({"role": if i % 2 == 0 { "user" } else { "assistant" }, "content": format!("t{i}")}))
            .collect();
        let out = history_messages(&history);
        assert_eq!(out.len(), 20);
        assert_eq!(out[0]["content"], "t5");
        let long = json!([{ "role": "user", "content": "字".repeat(700) }]);
        let out = history_messages(long.as_array().unwrap());
        assert!(out[0]["content"].as_str().unwrap().ends_with("（已截断）"));
    }

    #[test]
    fn test_attachments_text_reports_images_and_missing_text() {
        let atts = vec![
            json!({"fileName":"a.md","text":"正文"}),
            json!({"fileName":"b.png","image":{"data":"xx"}}),
            json!({"fileName":"c.docx","error":"解析失败"}),
        ];
        let (text, notes) = attachments_text(&atts);
        assert!(text.contains("### 附件：a.md"));
        assert_eq!(notes.len(), 2);
        assert!(notes[0].contains("图片"));
    }

    #[test]
    fn test_user_message_includes_plan_without_extra_blank_lines() {
        let (msg, _) = user_message("写周报", &[], &["读取数据".to_string(), "写出文件".to_string()]);
        assert!(msg.starts_with("写周报"));
        assert!(msg.contains("1. 读取数据"));
        assert!(msg.contains("2. 写出文件"));
    }

    #[test]
    fn test_system_prompt_has_mode_and_skills() {
        let runtime = json!({
            "awareness": {"mode": true, "files": {"SOUL.md": "直接给结论"}},
            "skills": [{"id":"weekly","name":"周报","description":"写周报","content":"正文"}]
        });
        let (prompt, notes) = system_prompt(&runtime, "writing", std::path::Path::new("C:/work"));
        assert!(prompt.contains("## 协作风格"));
        assert!(prompt.contains("use_skill"));
        assert!(prompt.contains("写作"));
        assert!(prompt.contains("C:/work"));
        assert!(notes.is_empty());
    }

    #[test]
    fn test_awareness_off_and_error_are_visible() {
        let off = json!({"awareness": {"mode": false}});
        let (prompt, notes) = system_prompt(&off, "general", std::path::Path::new("C:/work"));
        assert!(!prompt.contains("## 协作风格"));
        assert!(notes[0].contains("意识开关已关闭"));
        let err = json!({"awareness": {"error": "目录不可读"}});
        let (_, notes) = system_prompt(&err, "general", std::path::Path::new("C:/work"));
        assert!(notes[0].contains("目录不可读"));
    }

    #[test]
    fn test_plan_to_events_orders_steps() {
        let steps = parse_plan(r#"{"steps":[{"description":"a"},{"description":"b"}]}"#).unwrap();
        let v = plan_to_events(&steps);
        assert_eq!(v[0]["order"], 1);
        assert_eq!(v[1]["id"], "step-2");
    }
}
