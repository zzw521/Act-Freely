//! 准入策略（D-46 第二段）：把「谁能通过哪个入口开工」写成有版本、可哈希、可回放的文档
//!
//! 为什么要有这一层：主动接活的授权不能靠提示词，也不能散在代码里。DSH 原型的做法是把
//! 策略本身做成一等公民（Policy 有版本、研判结果记下当时用的版本，变更即失效重排队），
//! 这里对齐同一套纪律，并把它落到桌面库 `sa_policy` 表（见 `store/migrate.rs` V16）。
//!
//! 口径（与设计文档 `docs/Super-agent-对标DSH.md` §3 一致）：
//!
//! - **缺省 observe**：`acceptMode` 三档 `off / observe / auto`，默认 `observe`——
//!   研判照做、阳性只记「本可接单」，不产生任何执行。`auto` 需要用户显式改，本段代码
//!   没有任何路径把默认值变成 `auto`。
//! - **按入口许可，不做跨入口身份映射**：`sources[]` 以 `(kind, instance)` 为键，
//!   一个项目、一个目录、一个 IM 会话各是一条。某人在项目 A 有权限，不代表他在项目 B 也有。
//! - **名单只有两种用途**：`allowUsers` 决定谁的点名算「直接指派」；`bots` 决定谁的
//!   消息一律不接单（避免 Agent 之间互相触发）。
//! - **规则是白名单而不是黑名单**：只有 `enabled: true` 的规则能给出 `inferred` 强度，
//!   没命中任何启用规则就是 `not_admitted`。规则字段留空表示「不限」，不是「全不匹配」。
//! - **版本化与变更失效**：文档规范化后取 sha256 前 16 位作 `digest`；内容没变不新增
//!   版本，变了就版本 +1。判定行按 `(trigger_id, policy_version)` 唯一，旧判定保留可追溯，
//!   在跑的研判由事件循环按版本丢弃后重排队。
//! - **两层研判各自有预算**：`dailyEvalCap` 管第一层（确定性 gate），`assessMaxPerDay`
//!   管第二层（模型研判，见 `assess`）。后者缺省 50、写 `0` 就是显式关闭第二层，
//!   上限不超过前者——模型不该比记账判得更勤。老版本策略里没有这一字段，读出来按缺省。
//!
//! 隐私与凭据边界（沿用仓库既有口径）：策略里只放 id 与名字，不放令牌、不放密钥、
//! 不放正文样本；`sa_triggers.text` 有长度上限，且这些内容只进本机桌面库，不写日志。

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const MODE_OFF: &str = "off";
pub const MODE_OBSERVE: &str = "observe";
pub const MODE_AUTO: &str = "auto";
pub const MODES: &[&str] = &[MODE_OFF, MODE_OBSERVE, MODE_AUTO];
/// 缺省档位：影子模式。任何「读不到策略 / 策略损坏」的兜底都必须回到这一档
pub const DEFAULT_MODE: &str = MODE_OBSERVE;
/// 一天最多研判多少条事件（成本闸门，由事件循环执行，不在 gate 判定里）
pub const DEFAULT_DAILY_EVAL_CAP: i64 = 200;
pub const MIN_DAILY_EVAL_CAP: i64 = 1;
pub const MAX_DAILY_EVAL_CAP: i64 = 2000;
/// 第二层（模型研判）的缺省日预算：影子阶段一天最多问模型 50 次，够看清「本可接单」的分布，
/// 又不至于让一轮巡检悄悄吃掉当天配额。策略里没写过这一字段的老版本按这一档生效。
pub const DEFAULT_ASSESS_CAP: i64 = 50;
/// 第二层预算上限：模型研判不会比第一层的每日研判上限更频繁，成本闸门只紧不松
pub const MAX_ASSESS_CAP: i64 = MAX_DAILY_EVAL_CAP;
/// 单条正文参与匹配前的截断长度（策略里的 needles 也按同一上限校验）
pub const MAX_NEEDLE_CHARS: usize = 64;
pub const MAX_LIST_ENTRIES: usize = 200;
pub const MAX_RULES_PER_SOURCE: usize = 50;

/// 策略损坏或读不到时的兜底判定：只认 observe，其他档位一律按最保守的 observe 处理
pub fn is_default_mode(mode: &str) -> bool {
    mode == DEFAULT_MODE
}

/// 一条启用规则：命中即给出 `inferred` 强度。留空的字段表示「不限」。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Rule {
    pub id: String,
    /// 默认关闭：写了规则不等于启用，避免「先建后忘」变成隐式授权
    pub enabled: bool,
    /// 上游事件类型白名单（如 `task.commented`）
    pub kinds: Vec<String>,
    /// 发起人白名单（留空 = 不限，仍受入口 allowUsers 约束，见 `gate`）
    pub actors: Vec<String>,
    /// 正文命中词（不区分大小写的子串；任一命中即算命中）
    pub text_any: Vec<String>,
    /// 线程/会话键命中词
    pub thread_any: Vec<String>,
    /// 太短的消息不值得开工（如只有「在吗」）
    pub min_chars: usize,
}

impl Default for Rule {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: false,
            kinds: Vec::new(),
            actors: Vec::new(),
            text_any: Vec::new(),
            thread_any: Vec::new(),
            min_chars: 0,
        }
    }
}

impl Rule {
    /// 确定性匹配：不看语义、不调模型，只比字段。
    /// 返回 true 表示这条规则命中（`enabled` 为 false 时永远不命中）。
    pub fn matches(&self, kind: &str, actor: &str, text: &str, thread: &str) -> bool {
        if !self.enabled {
            return false;
        }
        if !self.kinds.is_empty() && !contains_ci(&self.kinds, kind) {
            return false;
        }
        if !self.actors.is_empty() && !contains_ci(&self.actors, actor) {
            return false;
        }
        if !self.thread_any.is_empty() && !any_needle(&self.thread_any, thread) {
            return false;
        }
        if !self.text_any.is_empty() && !any_needle(&self.text_any, text) {
            return false;
        }
        if self.min_chars > 0 && text.chars().count() < self.min_chars {
            return false;
        }
        true
    }
}

/// 一个事件入口的授权：谁能点名指派、谁的发言一律不接单、哪些规则启用
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct SourcePolicy {
    pub kind: String,
    pub instance: String,
    /// 该入口是否参与准入。与账本里 `sa_sources.enabled`（是否在读取）是两回事：
    /// 停止读取是运维动作，取消授权是安全动作，两者都要显式做。
    pub enabled: bool,
    pub label: String,
    pub allow_users: Vec<String>,
    pub bots: Vec<String>,
    pub rules: Vec<Rule>,
}

impl Default for SourcePolicy {
    fn default() -> Self {
        Self {
            kind: String::new(),
            instance: String::new(),
            enabled: false,
            label: String::new(),
            allow_users: Vec::new(),
            bots: Vec::new(),
            rules: Vec::new(),
        }
    }
}

/// 完整策略文档
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct PolicyDoc {
    pub accept_mode: String,
    pub daily_eval_cap: i64,
    /// 第二层（模型研判）的当日预算。`None` = 这份文档还没写过这一字段（老版本），
    /// 规范化后补成 `DEFAULT_ASSESS_CAP`；`Some(0)` 是用户显式关闭第二层，
    /// 与「没配模型」区分开：前者是决定，后者是状态。
    pub assess_max_per_day: Option<i64>,
    pub sources: Vec<SourcePolicy>,
}

impl Default for PolicyDoc {
    fn default() -> Self {
        Self {
            accept_mode: DEFAULT_MODE.to_string(),
            daily_eval_cap: DEFAULT_DAILY_EVAL_CAP,
            assess_max_per_day: Some(DEFAULT_ASSESS_CAP),
            sources: Vec::new(),
        }
    }
}

impl PolicyDoc {
    /// 找出某个入口实例的授权条目（`(kind, instance)` 精确匹配，不做前缀或通配）
    pub fn source(&self, kind: &str, instance: &str) -> Option<&SourcePolicy> {
        self.sources
            .iter()
            .find(|s| s.kind == kind && s.instance == instance)
    }

    pub fn is_observe(&self) -> bool {
        self.accept_mode == MODE_OBSERVE
    }

    /// 第二层今天还能判几条（规范化后的文档一定带值；未规范化时按缺省，不按 0）
    pub fn assess_cap(&self) -> i64 {
        self.assess_max_per_day.unwrap_or(DEFAULT_ASSESS_CAP).max(0)
    }

    /// 第二层是否启用：`0` 与档位 `off` 都算关，关闭的方式只有这两种，不靠内存标记
    pub fn assess_enabled(&self) -> bool {
        self.accept_mode != MODE_OFF && self.assess_cap() > 0
    }
}

/// 缺省文档的 JSON 形态（首次启动写进 `sa_policy` 第 1 版）
pub fn default_doc() -> PolicyDoc {
    PolicyDoc::default()
}

/// 解析并校验策略文档；任何不合法都如实报错，不做「猜一个能跑的值」
pub fn parse_doc(value: &Value) -> Result<PolicyDoc, String> {
    let mut doc: PolicyDoc = serde_json::from_value(value.clone())
        .map_err(|e| format!("SUPERAGENT_POLICY_INVALID: 策略结构无法解析（{e}）"))?;
    normalize(&mut doc)?;
    Ok(doc)
}

/// 规范化 + 校验：去空白、去重、限规模、补默认档位。校验失败返回中文原因。
pub fn normalize(doc: &mut PolicyDoc) -> Result<(), String> {
    let mode = doc.accept_mode.trim().to_ascii_lowercase();
    if mode.is_empty() {
        doc.accept_mode = DEFAULT_MODE.to_string();
    } else if !MODES.contains(&mode.as_str()) {
        return Err(format!(
            "SUPERAGENT_POLICY_INVALID: acceptMode 只能是 off / observe / auto，实为 {mode:?}"
        ));
    } else {
        doc.accept_mode = mode;
    }
    if doc.daily_eval_cap < MIN_DAILY_EVAL_CAP {
        doc.daily_eval_cap = DEFAULT_DAILY_EVAL_CAP;
    }
    if doc.daily_eval_cap > MAX_DAILY_EVAL_CAP {
        return Err(format!(
            "SUPERAGENT_POLICY_INVALID: 每日研判上限最多 {MAX_DAILY_EVAL_CAP} 条（影子阶段不该更多）"
        ));
    }
    // 第二层预算：没写过这一字段的文档补成缺省；0 是「显式关闭」，与负数（写错了）区分开
    doc.assess_max_per_day = Some(match doc.assess_max_per_day {
        None => DEFAULT_ASSESS_CAP,
        Some(value) if value < 0 => {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 模型研判预算不能是负数（要关闭第二层请填 0，实为 {value}）"
            ))
        }
        Some(value) if value > MAX_ASSESS_CAP => {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 模型研判预算最多 {MAX_ASSESS_CAP} 条/日（不超过第一层的每日研判上限）"
            ))
        }
        Some(value) => value,
    });
    let mut seen_sources: Vec<(String, String)> = Vec::new();
    let mut out_sources: Vec<SourcePolicy> = Vec::new();
    for mut source in std::mem::take(&mut doc.sources) {
        source.kind = source.kind.trim().to_string();
        source.instance = source.instance.trim().to_string();
        source.label = source.label.trim().to_string();
        if source.kind.is_empty() {
            return Err("SUPERAGENT_POLICY_INVALID: 入口策略缺 kind".to_string());
        }
        if !matches!(
            source.kind.as_str(),
            crate::superagent::SOURCE_KIND_PROJECT_EVENT
                | crate::superagent::SOURCE_KIND_SCHEDULE
                | crate::superagent::SOURCE_KIND_CONDITION
                | crate::superagent::SOURCE_KIND_WORKSPACE_FILE
                | crate::superagent::SOURCE_KIND_IM_INBOUND
        ) {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 未知事件入口类型 {:?}",
                source.kind
            ));
        }
        // schedule / condition / workspace_file 是本机实例，允许用空 instance 表示「本机唯一入口」；
        // 项目事件与 IM 入站必须点名到具体项目/会话，不能整类放行。
        if source.instance.is_empty()
            && matches!(
                source.kind.as_str(),
                crate::superagent::SOURCE_KIND_PROJECT_EVENT | crate::superagent::SOURCE_KIND_IM_INBOUND
            )
        {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 入口 {} 必须指定具体实例（项目 id / 会话 id），不许整类放行",
                source.kind
            ));
        }
        if seen_sources
            .iter()
            .any(|(k, i)| *k == source.kind && *i == source.instance)
        {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 入口 {} 重复配置",
                source_key(&source.kind, &source.instance)
            ));
        }
        seen_sources.push((source.kind.clone(), source.instance.clone()));
        dedupe_list(&mut source.allow_users)?;
        dedupe_list(&mut source.bots)?;
        if source.rules.len() > MAX_RULES_PER_SOURCE {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 入口 {} 的规则最多 {MAX_RULES_PER_SOURCE} 条",
                source_key(&source.kind, &source.instance)
            ));
        }
        let mut seen_rules: Vec<String> = Vec::new();
        for rule in source.rules.iter_mut() {
            rule.id = rule.id.trim().to_string();
            if rule.id.is_empty() {
                return Err(format!(
                    "SUPERAGENT_POLICY_INVALID: 入口 {} 有规则缺 id",
                    source_key(&source.kind, &source.instance)
                ));
            }
            if seen_rules.contains(&rule.id) {
                return Err(format!(
                    "SUPERAGENT_POLICY_INVALID: 规则 id {} 在同一入口里重复",
                    rule.id
                ));
            }
            seen_rules.push(rule.id.clone());
            dedupe_list(&mut rule.kinds)?;
            dedupe_list(&mut rule.actors)?;
            dedupe_list(&mut rule.thread_any)?;
            dedupe_list_lower(&mut rule.text_any)?;
        }
        out_sources.push(source);
    }
    doc.sources = out_sources;
    Ok(())
}

fn dedupe_list(list: &mut Vec<String>) -> Result<(), String> {
    let mut out: Vec<String> = Vec::with_capacity(list.len());
    for raw in list.iter() {
        let item = raw.trim();
        if item.is_empty() {
            continue;
        }
        if item.chars().count() > MAX_NEEDLE_CHARS {
            return Err(format!(
                "SUPERAGENT_POLICY_INVALID: 名单项过长（最多 {MAX_NEEDLE_CHARS} 字）：{}",
                item.chars().take(24).collect::<String>()
            ));
        }
        if !out.iter().any(|kept: &String| kept == item) {
            out.push(item.to_string());
        }
    }
    if out.len() > MAX_LIST_ENTRIES {
        return Err(format!(
            "SUPERAGENT_POLICY_INVALID: 名单最多 {MAX_LIST_ENTRIES} 项，请改用规则"
        ));
    }
    *list = out;
    Ok(())
}

/// 正文命中词去空白并统一小写：匹配是不区分大小写的子串比较
fn dedupe_list_lower(list: &mut Vec<String>) -> Result<(), String> {
    dedupe_list(list)?;
    for needle in list.iter_mut() {
        *needle = needle.to_ascii_lowercase();
    }
    Ok(())
}

/// 规范化后的可比较文本：字段顺序由结构体决定，因此同一份内容永远得到同一串
pub fn canonical_json(doc: &PolicyDoc) -> String {
    serde_json::to_string(doc).unwrap_or_else(|_| "{}".to_string())
}

/// 策略指纹（sha256 前 16 位）。版本行存它，用于「内容没变不新增版本」。
pub fn digest(doc: &PolicyDoc) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical_json(doc).as_bytes());
    let hex = format!("{:x}", hasher.finalize());
    hex.chars().take(16).collect()
}

pub fn source_key(kind: &str, instance: &str) -> String {
    if instance.is_empty() {
        kind.to_string()
    } else {
        format!("{kind}/{instance}")
    }
}

fn contains_ci(list: &[String], value: &str) -> bool {
    let needle = value.trim().to_ascii_lowercase();
    if needle.is_empty() {
        return false;
    }
    list.iter()
        .any(|item| item.trim().to_ascii_lowercase() == needle)
}

fn any_needle(needles: &[String], haystack: &str) -> bool {
    let text = haystack.to_lowercase();
    needles
        .iter()
        .map(|needle| needle.trim())
        .filter(|needle| !needle.is_empty())
        .any(|needle| text.contains(needle))
}

/// 校验一份策略能否作为「生效档位」被接受：`auto` 必须有可审计依据。
///
/// 本段（D-46 影子阶段）不接执行体，所以任何把档位改成 `auto` 的写入都要被挡住：
/// 只允许 `off` 与 `observe`。`auto` 要等 D-47（工单与待人工）与 D-48（执行体隔离）落地后
/// 由那两段的验收放开，不能先有开关再找执行器。
pub fn assert_mode_allowed(doc: &PolicyDoc) -> Result<(), String> {
    if doc.accept_mode == MODE_AUTO {
        return Err(
            "SUPERAGENT_MODE_NOT_AVAILABLE: 自动接单需要工单层（D-47）与执行体隔离（D-48）先落地，当前只支持 off / observe".to_string(),
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn source(kind: &str, instance: &str) -> SourcePolicy {
        SourcePolicy {
            kind: kind.to_string(),
            instance: instance.to_string(),
            enabled: true,
            label: String::new(),
            allow_users: vec!["usr-owner".to_string()],
            bots: vec!["bot-other-agent".to_string()],
            rules: vec![Rule {
                id: "review-request".to_string(),
                enabled: true,
                kinds: vec!["task.commented".to_string()],
                actors: Vec::new(),
                text_any: vec!["帮我评审".to_string()],
                thread_any: Vec::new(),
                min_chars: 6,
            }],
        }
    }

    fn doc(mode: &str, sources: Vec<SourcePolicy>) -> PolicyDoc {
        PolicyDoc {
            accept_mode: mode.to_string(),
            daily_eval_cap: DEFAULT_DAILY_EVAL_CAP,
            assess_max_per_day: Some(DEFAULT_ASSESS_CAP),
            sources,
        }
    }

    #[test]
    fn default_policy_is_observe_and_carries_no_grant() {
        let doc = default_doc();
        assert_eq!(doc.accept_mode, MODE_OBSERVE);
        assert!(doc.is_observe());
        assert!(doc.sources.is_empty(), "缺省策略不预置任何入口授权");
        // 缺省策略下任何事件都进不来
        let input = crate::superagent::gate::GateInput {
            kind: crate::superagent::SOURCE_KIND_PROJECT_EVENT,
            instance: "prj-1",
            actor: "usr-owner",
            event_kind: "task.commented",
            text: "帮我评审这份方案，谢谢",
            thread_key: "tsk-1",
            addressed_agent: true,
        };
        let verdict = crate::superagent::gate::decide(&doc, &input);
        assert_eq!(verdict.strength, crate::superagent::gate::STRENGTH_NOT_ADMITTED);
        assert_eq!(verdict.outcome, crate::superagent::gate::OUTCOME_NOT_ADMITTED);
        assert!(!verdict.would_accept, "没登记入口时连「本可接单」都不该记");
    }

    #[test]
    fn second_layer_budget_defaults_and_zero_means_off() {
        // 老版本的策略里没有这一字段：读出来按缺省预算，而不是「一条都不判」
        let legacy = json!({"acceptMode": "observe", "dailyEvalCap": 200, "sources": []});
        let doc = parse_doc(&legacy).unwrap();
        assert_eq!(doc.assess_max_per_day, Some(DEFAULT_ASSESS_CAP));
        assert_eq!(doc.assess_cap(), DEFAULT_ASSESS_CAP);
        assert!(doc.assess_enabled(), "缺省策略是影子模式：第二层照做，只是零执行");
        // 显式写 0 是「关掉第二层」这个决定，和「没配模型」那种状态不同
        let off = json!({"acceptMode": "observe", "assessMaxPerDay": 0, "sources": []});
        let doc = parse_doc(&off).unwrap();
        assert_eq!(doc.assess_cap(), 0);
        assert!(!doc.assess_enabled());
        // 档位 off 时第二层跟着停：整轮研判都不做，不靠内存标记
        let closed = json!({"acceptMode": "off", "assessMaxPerDay": 30, "sources": []});
        assert!(!parse_doc(&closed).unwrap().assess_enabled());
        // 负数是写错了，不当成「关闭」也不当成「不限」
        let bad = json!({"acceptMode": "observe", "assessMaxPerDay": -1, "sources": []});
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("不能是负数"), "{err}");
        // 超过上限如实拒绝：模型研判不该比第一层的每日研判上限更频繁
        let bad = json!({"acceptMode": "observe", "assessMaxPerDay": MAX_ASSESS_CAP + 1, "sources": []});
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("模型研判预算最多"), "{err}");
        // 缺省文档本身就带着这一档（`ensure_default_policy` 写的第一版就是这个形状）
        assert_eq!(default_doc().assess_cap(), DEFAULT_ASSESS_CAP);
    }

    #[test]
    fn rule_matching_is_deterministic_and_conservative() {
        let rule = Rule {
            id: "review".to_string(),
            enabled: true,
            kinds: vec!["task.commented".to_string()],
            actors: Vec::new(),
            text_any: vec!["帮我评审".to_string()],
            thread_any: Vec::new(),
            min_chars: 6,
        };
        assert!(rule.matches("task.commented", "usr-a", "帮我评审这份部署方案", "tsk-1"));
        // 大小写与全半角空格之外的差异不影响子串命中
        assert!(rule.matches("TASK.COMMENTED", "usr-a", "  帮我评审下 ", "tsk-1"));
        // 事件类型不符
        assert!(!rule.matches("task.created", "usr-a", "帮我评审这份部署方案", "tsk-1"));
        // 正文不含命中词
        assert!(!rule.matches("task.commented", "usr-a", "收到，我看看", "tsk-1"));
        // 字数不足（只有「帮我评审」四个字）
        assert!(!rule.matches("task.commented", "usr-a", "帮我评审", "tsk-1"));
        // 未启用的规则永远不命中：写了规则不等于授权
        let mut off = rule.clone();
        off.enabled = false;
        assert!(!off.matches("task.commented", "usr-a", "帮我评审这份部署方案", "tsk-1"));
    }

    #[test]
    fn parsing_rejects_bad_policy_instead_of_guessing() {
        // 未知档位
        let bad = json!({"acceptMode": "always", "dailyEvalCap": 200, "sources": []});
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("acceptMode"), "{err}");
        // 未知入口类型
        let bad = json!({"acceptMode": "observe", "sources": [{"kind": "email", "instance": "inbox"}]});
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("未知事件入口类型"), "{err}");
        // 项目事件必须点名到具体项目，不许整类放行
        let bad = json!({"acceptMode": "observe", "sources": [{"kind": "project_event", "enabled": true}]});
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("必须指定具体实例"), "{err}");
        // 同入口重复规则 id
        let bad = json!({
            "acceptMode": "observe",
            "sources": [{"kind": "project_event", "instance": "prj-1", "enabled": true,
                "rules": [{"id": "r1", "enabled": true}, {"id": "r1", "enabled": true}]}]
        });
        let err = parse_doc(&bad).unwrap_err();
        assert!(err.contains("重复"), "{err}");
    }

    #[test]
    fn normalize_trims_dedupes_and_falls_back_to_observe_default_cap() {
        let raw = json!({
            "acceptMode": "  OBSERVE ",
            "dailyEvalCap": 0,
            "sources": [{
                "kind": "project_event",
                "instance": "  prj-1 ",
                "enabled": true,
                "allowUsers": ["usr-owner", " usr-owner ", ""],
                "bots": [],
                "rules": [{"id": " review ", "enabled": true, "kinds": ["task.commented"],
                           "textAny": [" 帮我评审 ", "帮我评审"]}]
            }]
        });
        let mut doc = parse_doc(&raw).unwrap();
        // 档位规范化为小写；上限为 0 视为未配置，回到缺省而不是「一条都不研判」
        assert_eq!(doc.accept_mode, MODE_OBSERVE);
        assert_eq!(doc.daily_eval_cap, DEFAULT_DAILY_EVAL_CAP);
        let source = doc.sources.first().unwrap();
        assert_eq!(source.instance, "prj-1");
        assert_eq!(source.allow_users, vec!["usr-owner".to_string()]);
        assert_eq!(source.rules[0].id, "review");
        // 命中词统一小写并去重，第二次解析结果必须完全一致（否则 digest 会漂）
        assert_eq!(source.rules[0].text_any, vec!["帮我评审".to_string()]);
        normalize(&mut doc).unwrap();
        assert_eq!(canonical_json(&doc), canonical_json(&parse_doc(&raw).unwrap()));
    }

    #[test]
    fn digest_is_stable_and_changes_only_with_content() {
        let a = doc(MODE_OBSERVE, vec![source(crate::superagent::SOURCE_KIND_PROJECT_EVENT, "prj-1")]);
        let b = doc(MODE_OBSERVE, vec![source(crate::superagent::SOURCE_KIND_PROJECT_EVENT, "prj-1")]);
        assert_eq!(digest(&a), digest(&b), "同样的内容必须得到同一个指纹");
        let mut changed = a.clone();
        changed.sources[0].allow_users.push("usr-peer".to_string());
        assert_ne!(digest(&a), digest(&changed), "加了一个可指派的人就该视为新版本");
        assert_eq!(digest(&a).len(), 16);
        // 换档位同样换版本
        let mut mode_changed = a.clone();
        mode_changed.accept_mode = MODE_OFF.to_string();
        assert_ne!(digest(&a), digest(&mode_changed));
    }

    #[test]
    fn auto_mode_is_not_available_before_ticket_layer() {
        let observe = doc(MODE_OBSERVE, vec![]);
        assert!(assert_mode_allowed(&observe).is_ok());
        let off = doc(MODE_OFF, vec![]);
        assert!(assert_mode_allowed(&off).is_ok());
        let auto = doc(MODE_AUTO, vec![source(crate::superagent::SOURCE_KIND_PROJECT_EVENT, "prj-1")]);
        let err = assert_mode_allowed(&auto).unwrap_err();
        assert!(err.contains("SUPERAGENT_MODE_NOT_AVAILABLE"), "{err}");
        assert!(err.contains("D-47"), "报错要点名缺的是哪一段，便于用户理解：{err}");
    }

    #[test]
    fn source_key_keeps_instance_visible() {
        assert_eq!(
            source_key(crate::superagent::SOURCE_KIND_PROJECT_EVENT, "prj-1"),
            "project_event/prj-1"
        );
        assert_eq!(source_key(crate::superagent::SOURCE_KIND_WORKSPACE_FILE, ""), "workspace_file");
    }
}
