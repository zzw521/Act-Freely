//! 本地 harness 的上下文预算与压缩（与引擎 `context_budget.py` 同一口径）。
//!
//! 引擎链路早已按「窗口 − 动态预留」触发 AgentScope 压缩；本地 harness 过去
//! 没有这一步，长会话只能撞模型上限。这里补的是同一件事实，不是第二套标准：
//!
//! * 阈值：`窗口 − max(窗口×5%, 4096)`，预留覆盖没被计入的部分——工具 schema、
//!   上一条模型输出、请求开销。
//! * 窗口：`modelConfig.contextWindow`；有容灾链时按链上**最小**窗口算
//!   （转移后同一份上下文必须装得下）。没填按 8192 估算并如实说明。
//! * 开关：`runtime.agentPolicy.autoCompress`（设置页同一个键，缺省开启）。
//!
//! 计数口径必须如实：引擎用 tiktoken 精算，harness 没有分词器，端点回报的
//! `prompt_tokens` 是真实计数，其余情况按字符估算（CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token）。
//! 估算会低估中文以外的文本、高估代码，所以触发点本身是保守近似，界面必须摊开。
//! 回报值只是**上一次**请求的账，本轮新增的消息不在里面，故与字符粗估取较大者
//! （见 `estimate_tokens`）。
//!
//! 压缩保不住的是「最近保留的几组轮次」，所以单条工具结果自己超过窗口时，光靠压缩救不回来——
//! 这一半由工具面的分页预算兜住（`harness/tools.rs` 的 `TEXT_UNITS_*` / `LIST_ITEMS_*`，
//! 与引擎 `ticket_worker_adapter.py` 同一组数字）。
//!
//! 压缩只替换「更早的消息」，且绝不拆散 `assistant(tool_calls) ↔ tool` 配对——
//! 拆开会让后续请求被端点直接拒绝。摘要模型调用失败时保持原消息不动，
//! 把原因播报出去，任务不因此中断。

use serde_json::{json, Value};

/// 上下文窗口缺失时的估算值（与引擎一致，不假装知道真实值）
pub const DEFAULT_CONTEXT_WINDOW: u64 = 8192;
pub const MIN_CONTEXT_WINDOW: u64 = 1024;
pub const MAX_CONTEXT_WINDOW: u64 = 2_000_000;
/// 动态预留：窗口的 5%，下限 4096（与引擎 COMPRESSION_RESERVE_* 同口径）
pub const RESERVE_FRACTION: f64 = 0.05;
pub const RESERVE_MIN_TOKENS: u64 = 4096;
pub const MIN_TRIGGER_THRESHOLD: u64 = 512;
/// 压缩后保留的最近「轮次」数：一组 = 一次助手输出 + 它的工具结果们
pub const KEEP_RECENT_TURNS: usize = 3;
/// 送进摘要模型的原文上限（字符）；端点没有 max_tokens 参数，靠输入约束
pub const MAX_SUMMARY_INPUT_CHARS: usize = 100_000;
/// 单条消息进入摘要输入时的截断
pub const MAX_SUMMARY_ITEM_CHARS: usize = 4_000;
/// 摘要自身回写的上限：摘要无限膨胀会把压缩变成新的负担
pub const MAX_SUMMARY_CHARS: usize = 8_000;
/// 摘要消息的固定标记：下一轮压缩据此认出并合并旧摘要，避免多份摘要堆叠
pub const SUMMARY_MARK: &str = "【上下文压缩摘要】";

#[derive(Debug, Clone, Default)]
pub struct Policy {
    pub enabled: bool,
    pub context_window: u64,
    pub trigger_threshold: u64,
    /// 窗口是否取自模型配置（false = 按缺省估算）
    pub window_from_model: bool,
    pub notes: Vec<String>,
}

fn window_value(v: Option<&Value>) -> Option<u64> {
    let n = v?.as_u64()?;
    (MIN_CONTEXT_WINDOW..=MAX_CONTEXT_WINDOW).contains(&n).then_some(n)
}

/// 与引擎 `fallback_window` 同口径：容灾链里最小的可用窗口。
pub fn min_fallback_window(model_config: &Value) -> Option<u64> {
    model_config
        .get("fallbacks")?
        .as_array()?
        .iter()
        .filter_map(|item| window_value(item.get("contextWindow")))
        .min()
}

pub fn reserve_of(window: u64) -> u64 {
    ((window as f64 * RESERVE_FRACTION) as u64).max(RESERVE_MIN_TOKENS)
}

/// 解析压缩策略：开关来自 `runtime.agentPolicy.autoCompress`，窗口来自模型配置。
/// 所有回退都留下用户可见说明，与引擎 `resolve()` 逐条对应。
pub fn policy(runtime: &Value, model_config: &Value) -> Policy {
    let mut out = Policy::default();
    if runtime
        .get("agentPolicy")
        .and_then(|p| p.get("autoCompress"))
        .and_then(Value::as_bool)
        == Some(false)
    {
        out.notes
            .push("上下文压缩已关闭（设置「上下文自动压缩」）：长任务超出窗口时可能被模型拒绝。".to_string());
        return out;
    }
    out.enabled = true;

    let mut window = window_value(model_config.get("contextWindow"));
    if window.is_none() && model_config.get("contextWindow").is_some() {
        out.notes.push(format!(
            "模型上下文窗口 {} 不可用（允许 {MIN_CONTEXT_WINDOW}~{MAX_CONTEXT_WINDOW}），已按 {DEFAULT_CONTEXT_WINDOW} tokens 估算。",
            model_config.get("contextWindow").map(|v| v.to_string()).unwrap_or_default()
        ));
    }
    let fallback = min_fallback_window(model_config);
    if let (Some(f), Some(w)) = (fallback, window) {
        if f < w {
            out.notes.push(format!(
                "容灾链里最小上下文窗口为 {f} tokens，压缩阈值按该窗口计算（转移后同一份上下文必须装得下）。"
            ));
            window = Some(f);
        }
    }
    let effective = window.unwrap_or(DEFAULT_CONTEXT_WINDOW);
    out.window_from_model = window.is_some();
    out.context_window = effective;
    out.trigger_threshold = effective
        .saturating_sub(reserve_of(effective))
        .max(MIN_TRIGGER_THRESHOLD);
    out
}

/// 字符 → token 的粗估：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token；
/// 工具 schema 没算进来（本地 harness 固定 7 个工具，量级很小）。
pub fn rough_tokens(text: &str) -> u64 {
    let mut cjk = 0u64;
    let mut other = 0u64;
    for ch in text.chars() {
        if is_cjk(ch) {
            cjk += 1;
        } else {
            other += 1;
        }
    }
    cjk + other.div_ceil(4)
}

fn is_cjk(ch: char) -> bool {
    matches!(ch as u32,
        0x3000..=0x30FF | 0x3400..=0x4DBF | 0x4E00..=0x9FFF
        | 0xAC00..=0xD7AF | 0xF900..=0xFAFF | 0xFF00..=0xFFEF)
}

fn message_text(msg: &Value) -> String {
    let content = msg.get("content").and_then(Value::as_str).unwrap_or("");
    let calls = msg
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|c| {
                    let f = c.get("function")?;
                    let name = f.get("name").and_then(Value::as_str).unwrap_or("");
                    let args = f.get("arguments").and_then(Value::as_str).unwrap_or("");
                    Some(format!("{name}{args}"))
                })
                .collect::<Vec<_>>()
                .join("")
        })
        .unwrap_or_default();
    format!("{content}{calls}")
}

/// 本轮输入的 token 估计：取「上一次调用回报的 `prompt_tokens`」与「当前全部消息的字符粗估」
/// 的**较大者**。
///
/// 为什么不直接信回报值：那是**上一次**请求的账。本轮在它之上又追加了新消息（工具结果、
/// 用户补充），拿旧数当新账会系统性低估——低估的代价不是「多算了一点」，而是**该压的那一轮
/// 没压**，下一次请求当场被窗口拒掉，正是 D-48.4 要防的事。两条口径各有对方覆盖不到的部分：
/// 回报值含工具 schema 与请求框架（粗估不算这些），粗估含本轮新增（回报值不算这些），
/// 取大值是「两头都不吃亏」的保守近似，界面按实际用的那条口径说明。
pub fn estimate_tokens(messages: &[Value], last_prompt_tokens: u64) -> (u64, &'static str) {
    let rough = messages
        .iter()
        .map(|m| rough_tokens(&message_text(m)))
        .sum::<u64>();
    if last_prompt_tokens > 0 {
        if rough > last_prompt_tokens {
            return (
                rough,
                "字符粗估（本轮新增消息已多于上一次回报的 prompt_tokens，不拿旧账当新账）",
            );
        }
        return (last_prompt_tokens, "上一次调用回报的真实 prompt_tokens");
    }
    (rough, "字符粗估（端点未回报 usage，未计工具 schema）")
}

fn role_of(m: &Value) -> &str {
    m.get("role").and_then(Value::as_str).unwrap_or("")
}

fn has_tool_calls(m: &Value) -> bool {
    m.get("tool_calls")
        .and_then(Value::as_array)
        .map(|a| !a.is_empty())
        .unwrap_or(false)
}

fn is_summary(m: &Value) -> bool {
    m.get("content")
        .and_then(Value::as_str)
        .map(|c| c.starts_with(SUMMARY_MARK))
        .unwrap_or(false)
}

/// 压缩切点：返回 `groups[cut..]` 保留、`[keep_from..cut)` 送去摘要。
/// 按「助手输出 + 它的工具结果们」成组，配对永远不会被拆散。
pub fn boundary(messages: &[Value]) -> (usize, usize, Option<usize>) {
    let keep_from = usize::from(role_of(messages.first().unwrap_or(&Value::Null)) == "system");
    let mut groups: Vec<(usize, usize)> = Vec::new();
    let mut i = keep_from;
    while i < messages.len() {
        if role_of(&messages[i]) == "tool" && groups.last().is_some() {
            let last = groups.len() - 1;
            if has_tool_calls(&messages[groups[last].0]) {
                groups[last].1 = i + 1;
                i += 1;
                continue;
            }
        }
        groups.push((i, i + 1));
        i += 1;
    }
    let cut = groups.len().saturating_sub(KEEP_RECENT_TURNS);
    let end = groups.get(cut).map(|g| g.0).unwrap_or(messages.len());
    let summary_at = (keep_from..end)
        .into_iter()
        .rev()
        .find(|idx| messages.get(*idx).map(is_summary).unwrap_or(false));
    (keep_from, end, summary_at)
}

/// 送去摘要的原文（按顺序摊平成纯文本，单条截断，总长超限时保留最近的部分）。
pub fn summary_input(messages: &[Value]) -> String {
    let (keep_from, end, _) = boundary(messages);
    let mut items: Vec<String> = Vec::new();
    for (idx, m) in messages[keep_from..end].iter().enumerate() {
        let text = m.get("content").and_then(Value::as_str).unwrap_or("").trim();
        if text.is_empty() {
            continue;
        }
        let body = if text.chars().count() > MAX_SUMMARY_ITEM_CHARS {
            let mut head: String = text.chars().take(MAX_SUMMARY_ITEM_CHARS).collect();
            head.push_str("…（本条已截断）");
            head
        } else {
            text.to_string()
        };
        items.push(format!("[{}] {}", idx + 1, body));
    }
    let mut out: Vec<String> = Vec::new();
    let mut total = 0usize;
    let mut truncated = false;
    for line in items.into_iter().rev() {
        let cost = line.chars().count() + 1;
        if truncated {
            continue;
        }
        if total + cost > MAX_SUMMARY_INPUT_CHARS && !out.is_empty() {
            out.push("…（更早内容超出摘要输入上限，未纳入本次摘要）".to_string());
            truncated = true;
            continue;
        }
        total += cost;
        out.push(line);
    }
    out.reverse();
    out.join("\n")
}

pub fn summary_prompt(original_goal: &str, transcript: &str) -> Vec<Value> {
    vec![
        json!({
            "role": "system",
            "content": "你是上下文压缩器。把下面的对话执行记录压缩成继续工作所需的摘要，\
                        用简洁中文分条输出，必须覆盖：任务目标；已完成的步骤与产出（文件路径、\
                        关键结论）；仍在进行的事项；下一步；后续必须记住的用户约束。\
                        只输出摘要正文，不要调用工具，不要寒暄，不要编造记录中不存在的事实。",
        }),
        json!({
            "role": "user",
            "content": format!(
                "任务目标（原文）：{}\n\n需要压缩的执行记录：\n{}",
                original_goal.trim(),
                transcript
            ),
        }),
    ]
}

/// 应用摘要：把 `[start..end)` 换成一条摘要消息。摘要失败返回 `None`，
/// 调用方保持原消息不动。连续多次压缩会把旧摘要一并并入，不堆叠多份。
pub fn apply_summary(messages: &[Value], text: &str) -> Option<(Vec<Value>, usize)> {
    let body = text.trim();
    if body.is_empty() {
        return None;
    }
    let mut body: String = body.chars().take(MAX_SUMMARY_CHARS).collect();
    if body.chars().count() < text.trim().chars().count() {
        body.push_str("…（摘要已截断）");
    }
    let (keep_from, end, summary_at) = boundary(messages);
    let start = summary_at.unwrap_or(keep_from);
    if end <= start {
        return None;
    }
    let mut out: Vec<Value> = messages[..start].to_vec();
    out.push(json!({ "role": "user", "content": format!("{SUMMARY_MARK}\n{body}") }));
    out.extend_from_slice(&messages[end..]);
    Some((out, end - start))
}

/// 用户可见的真实口径说明（首次触发时播报；界面要能看到代价与计数方式）。
pub fn note(policy: &Policy, merged: usize, basis: &str, estimate: u64) -> String {
    format!(
        "上下文压缩（本地 harness）：按 {} tokens 窗口的动态预留（max(窗口×5%, {})），\
         约 {} tokens 触发；本轮估算 {} tokens（口径：{}），合并 {} 条更早消息为一条摘要，\
         保留最近 {} 组轮次与全部工具配对。压缩额外调用了一次模型生成摘要；\
         摘要失败时保留原文、只说明原因，不中断任务。{}",
        policy.context_window,
        RESERVE_MIN_TOKENS,
        policy.trigger_threshold,
        estimate,
        basis,
        merged,
        KEEP_RECENT_TURNS,
        if policy.window_from_model {
            String::new()
        } else {
            "（模型未填写上下文窗口，按缺省估算）".to_string()
        }
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(role: &str, content: &str) -> Value {
        json!({ "role": role, "content": content })
    }

    fn assistant_with_calls(id: &str) -> Value {
        json!({
            "role": "assistant",
            "content": "执行中",
            "tool_calls": [{
                "id": id,
                "type": "function",
                "function": { "name": "view_text_file", "arguments": "{\"file_path\":\"a.md\"}" },
            }],
        })
    }

    #[test]
    fn threshold_matches_engine_formula() {
        // 与 engine/context_budget.py 同一公式：窗口 − max(窗口×5%, 4096)
        let p = policy(&json!({}), &json!({ "contextWindow": 1_000_000 }));
        assert_eq!(p.trigger_threshold, 950_000);
        assert!(p.window_from_model);
        assert!(p.enabled);
        assert_eq!(policy(&json!({}), &json!({})).trigger_threshold, 4096);
        assert_eq!(
            policy(&json!({}), &json!({ "contextWindow": 32_000 })).trigger_threshold,
            27_904
        );
    }

    #[test]
    fn invalid_missing_and_fallback_windows_are_reported() {
        let p = policy(&json!({}), &json!({ "contextWindow": 10 }));
        assert_eq!(p.context_window, DEFAULT_CONTEXT_WINDOW);
        assert!(!p.window_from_model);
        assert!(p.notes.iter().any(|n| n.contains("不可用")), "{:?}", p.notes);

        let p = policy(
            &json!({}),
            &json!({
                "contextWindow": 1_000_000,
                "fallbacks": [{ "contextWindow": 65_536 }, { "contextWindow": 128_000 }],
            }),
        );
        assert_eq!(p.context_window, 65_536, "阈值按链上最小窗口");
        assert_eq!(p.trigger_threshold, 65_536 - 4096);
        assert!(p.notes.iter().any(|n| n.contains("容灾链")), "{:?}", p.notes);
    }

    #[test]
    fn switch_off_disables_with_visible_reason() {
        let p = policy(&json!({ "agentPolicy": { "autoCompress": false } }), &json!({}));
        assert!(!p.enabled);
        assert!(p.notes[0].contains("已关闭"));
        // 非法/缺失都按默认开启，与引擎一致
        assert!(policy(&json!({ "agentPolicy": { "autoCompress": "maybe" } }), &json!({})).enabled);
    }

    #[test]
    fn rough_tokens_counts_cjk_heavier_than_ascii() {
        assert_eq!(rough_tokens("写周报"), 3);
        assert_eq!(rough_tokens("abcd"), 1);
        assert_eq!(rough_tokens(""), 0);
    }

    #[test]
    fn estimate_prefers_reported_usage() {
        let msgs = vec![msg("user", "随便")];
        assert_eq!(estimate_tokens(&msgs, 1234), (1234, "上一次调用回报的真实 prompt_tokens"));
        let (v, basis) = estimate_tokens(&msgs, 0);
        assert!(v > 0 && basis.contains("粗估"), "{v} {basis}");
    }

    #[test]
    fn estimate_refuses_to_use_a_stale_reported_count() {
        // 上一次请求回报 5000 tokens，此后又追加了一条 12000 字符的工具结果：
        // 拿旧账当新账就是系统性低估，代价是「该压的那一轮没压」，下一请求当场撞窗口
        let msgs = vec![msg("user", "随便"), msg("tool", &"汉".repeat(12_000))];
        let (v, basis) = estimate_tokens(&msgs, 5_000);
        assert_eq!(v, 12_002, "本轮新增超过旧账时按新账走");
        assert!(basis.contains("不拿旧账当新账"), "{basis}");
        // 反向也要成立：回报值更大时不因为粗估偏弱而无谓提前压缩
        let (kept, kept_basis) = estimate_tokens(&msgs, 90_000);
        assert_eq!(kept, 90_000);
        assert_eq!(kept_basis, "上一次调用回报的真实 prompt_tokens");
    }

    #[test]
    fn boundary_keeps_system_and_whole_tool_groups() {
        let msgs = vec![
            msg("system", "sys"),
            msg("user", "目标"),
            assistant_with_calls("c1"),
            json!({ "role": "tool", "tool_call_id": "c1", "content": "结果1" }),
            assistant_with_calls("c2"),
            json!({ "role": "tool", "tool_call_id": "c2", "content": "结果2" }),
            assistant_with_calls("c3"),
            json!({ "role": "tool", "tool_call_id": "c3", "content": "结果3" }),
            msg("assistant", "收尾"),
        ];
        let (keep_from, end, summary_at) = boundary(&msgs);
        assert_eq!(keep_from, 1, "系统提示永远保留");
        assert_eq!(summary_at, None);
        // 最近 3 组 = [c2,c3,收尾]；「目标」与 c1 整组进摘要，配对不拆散
        assert_eq!(end, 4);
        assert_eq!(role_of(&msgs[end - 1]), "tool");
    }

    #[test]
    fn apply_summary_merges_previous_summary_once() {
        let mut msgs = vec![msg("system", "sys"), msg("user", "目标")];
        for i in 0..6 {
            msgs.push(assistant_with_calls(&format!("c{i}")));
            msgs.push(json!({ "role": "tool", "tool_call_id": format!("c{i}"), "content": "ok" }));
        }
        let (first, merged1) = apply_summary(&msgs, "第一轮摘要").unwrap();
        assert!(merged1 >= 2);
        assert!(first[1]["content"].as_str().unwrap().starts_with(SUMMARY_MARK));
        assert_eq!(first.len(), msgs.len() - merged1 + 1);

        // 再压一次：旧摘要被并入新摘要，上下文里只有一份
        let mut more = first.clone();
        for i in 0..4 {
            more.push(assistant_with_calls(&format!("d{i}")));
            more.push(json!({ "role": "tool", "tool_call_id": format!("d{i}"), "content": "ok" }));
        }
        let (again, merged2) = apply_summary(&more, "第二轮摘要").unwrap();
        assert_eq!(
            again
                .iter()
                .filter(|m| m.get("content").and_then(Value::as_str).unwrap_or("").starts_with(SUMMARY_MARK))
                .count(),
            1,
            "摘要不堆叠"
        );
        assert!(again[1]["content"].as_str().unwrap().contains("第二轮摘要"));
        assert!(merged2 >= 1);
    }

    #[test]
    fn empty_summary_keeps_messages_untouched() {
        let msgs = vec![msg("system", "sys"), msg("user", "a"), msg("assistant", "b")];
        assert!(apply_summary(&msgs, "   ").is_none());
    }

    #[test]
    fn summary_input_prefers_recent_when_over_budget() {
        let mut msgs = vec![msg("system", "sys")];
        for i in 0..60 {
            // 每条都会被截到 MAX_SUMMARY_ITEM_CHARS，合计远超 100K 输入上限
            msgs.push(msg("user", &"长".repeat(MAX_SUMMARY_ITEM_CHARS + i)));
        }
        let text = summary_input(&msgs);
        let (keep_from, end, _) = boundary(&msgs);
        let last_no = end - keep_from;
        assert!(text.contains("超出摘要输入上限"), "必须如实说明有内容没进摘要");
        assert!(
            text.contains(&format!("[{last_no}] ")),
            "必须保留最近的内容（最后一项编号 {last_no}）"
        );
        assert!(!text.contains("[1] "), "最早的整段应当被舍弃");
        assert!(text.chars().count() <= MAX_SUMMARY_INPUT_CHARS + MAX_SUMMARY_ITEM_CHARS + 40);
    }
}
