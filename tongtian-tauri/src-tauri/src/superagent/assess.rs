//! 第二层研判（D-46 影子段）：对 gate 放行的候选问一句「值不值得现在开工」
//!
//! 分工（与 `docs/Super-agent-对标DSH.md` §3 的 Eval 两层一致）：
//!
//! ```text
//! 事件 ──gate（确定性·白名单·零成本）──→ 阳性候选 ──assess（模型·有成本）──→ 值得/不值得/拿不准 + 理由
//! ```
//!
//! 第一层只回答「有没有授权」，它看不懂内容。第二层才判断这件事值不值得占用人的注意力——
//! 这也是影子阶段最该攒的东西：有了「模型认为值得」与「实际有没有人接手」的对照，
//! 后面决定 `auto` 档时才不是拍脑袋。
//!
//! 四条纪律：
//!
//! 1. **只判阳性**：阴性判定没有「值不值得开工」可问，也不该花钱。
//! 2. **一条候选只判一次**：回写走 `ledger::record_assessment`，条件里带着「还没判过」。
//!    失败的尝试同样记账（也花了钱）但不自动重试；要再看一次由人决定。
//! 3. **策略升版即在跑的那次作废**：回写时带上取出候选时的队列版本，版本已经被
//!    `save_policy` 挪过就说明这一份是按旧策略语境问出来的——拒收（`Superseded`），
//!    候选按新版本重排后再判一次。旧结论留在行里比没有结论更坏：它看起来像依据，实际没人认。
//! 4. **成本三道闸**：策略里的 `assessMaxPerDay`（缺省 50，`0` 是显式关闭）、
//!    本 UTC 日已判条数、以及现有的 [`QuotaGate`]（100% 档直接不发起调用）。
//!    任何一道闸拦住都不写判定行——「没判」和「判不出来」是两件事，不能混成一列。
//! 5. **零执行是结构性的**：这一层只调 [`crate::harness::model::stream`]，
//!    工具列表传空，既不起任务也不开进程（`tests/unit/storage/ownership-contract.test.ts`
//!    把这一条钉成源码层面的检查）。表名一律不出现在本文件：读写都经过 `ledger`。
//!
//! 隐私与凭据边界：提示词只带账本里已经存过的字段（入口类型、事件类型、发起人 id、
//! 线程键、截断后的正文与第一层理由）；密钥只在 `harness::model` 的内存里进请求头，
//! 不进提示词、不进判定行、也不进错误文案。研判结论只落本机库，不写日志。

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};

use crate::harness::model::{self, ModelError, Outcome};
use crate::store::Store;
use crate::superagent::ledger;
use crate::superagent::{policy, source_kind_label, strength_label};
use crate::task::quota::QuotaGate;

/// 已判定并写库
pub const STATUS_DONE: &str = "done";
/// 发起过调用但没成功（同样写库：失败也花了钱，要留痕）
pub const STATUS_FAILED: &str = "failed";
/// 配额闸门挡住，一次调用都没发起（不写库，下一轮自然还会排队）
pub const STATUS_BLOCKED: &str = "blocked";
/// 本机没有可用模型配置（不写库；配好模型后待研判的候选还会被读到）
pub const STATUS_UNAVAILABLE: &str = "unavailable";

pub const VERDICT_WORTH: &str = "worth";
pub const VERDICT_NOT_WORTH: &str = "not_worth";
pub const VERDICT_UNSURE: &str = "unsure";

/// 单次模型调用的上限：候选研判是后台动作，不该让一轮巡检挂在一次慢响应上
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// 进提示词的正文上限（账本里存得更多，但一句「值不值得」不需要整篇文档）
pub const MAX_PROMPT_TEXT_CHARS: usize = 600;
/// 模型自述理由的入库上限（比第一层的 `reason` 更短：这一句是给人扫一眼的）
pub const MAX_ASSESS_REASON_CHARS: usize = 160;
/// 一次调用最多判几条（界面点一次「模型研判」的批量；后台循环用缺省）
pub const DEFAULT_BATCH: usize = 5;
pub const MAX_BATCH: usize = 20;

/// 结论显示名（界面与导出共用，前端不写第二套翻译）
pub fn verdict_label(verdict: &str) -> &'static str {
    match verdict {
        VERDICT_WORTH => "值得开工",
        VERDICT_NOT_WORTH => "不值得现在开工",
        VERDICT_UNSURE => "拿不准",
        _ => "未研判",
    }
}

/// 状态显示名：只有 `done` / `failed` 会出现在判定行里，其余两种只在巡检摘要中
pub fn status_label(status: &str) -> &'static str {
    match status {
        STATUS_DONE => "已研判",
        STATUS_FAILED => "研判失败",
        STATUS_BLOCKED => "配额阻断（未发起调用）",
        STATUS_UNAVAILABLE => "没有可用模型（未发起调用）",
        "" => "未研判",
        _ => "未知状态",
    }
}

/// 一次研判的结果（写进判定行的第二层留痕列）
#[derive(Debug, Clone)]
pub struct Assessment {
    /// `done` / `failed` / `blocked` / `unavailable`
    pub status: String,
    /// `worth` / `not_worth` / `unsure`；没有结论时是空串
    pub verdict: String,
    pub reason: String,
    /// 用哪个模型判的（可复核；容灾切换后写实际生效的那个）
    pub model: String,
    /// 本次调用的 token 合计（计量口径，失败时可能是 0）
    pub tokens: i64,
    /// 中文原因，形如 `CODE: 详情`，不含凭据
    pub error: String,
}

impl Assessment {
    fn done(verdict: String, reason: String, model: String, tokens: i64) -> Self {
        Self {
            status: STATUS_DONE.to_string(),
            verdict,
            reason,
            model,
            tokens,
            error: String::new(),
        }
    }

    fn failed(error: String) -> Self {
        Self {
            status: STATUS_FAILED.to_string(),
            verdict: String::new(),
            reason: String::new(),
            model: String::new(),
            tokens: 0,
            error,
        }
    }

    /// 这一条要不要写进判定行。
    ///
    /// 被闸门挡住时不写：那一行保持「未研判」，等下一轮或等用户配好模型。
    /// 把 `blocked` 写进库里会让这条候选永远不再被研判，那是把「没花成钱」当成「判过了」。
    fn persists(&self) -> bool {
        matches!(self.status.as_str(), STATUS_DONE | STATUS_FAILED)
    }
}

/// 第二层的系统提示词：口径只有一句——「判断，不动手」
pub fn system_prompt() -> String {
    "你是桌面应用里的「主动接单」研判器，正在影子模式里做记录。\n\
     给你一条已经通过授权检查的环境事件，你只回答一个问题：这件事现在值不值得由 Agent 主动开工。\n\
     判定标准（从严）：\n\
     - worth：诉求明确、有具体交付物、靠这条事件本身就能开工。\n\
     - not_worth：闲聊/寒暄、缺少可执行目标、需要人来决定、或应当由发起人自己发起。\n\
     - unsure：信息不够判断。\n\
     你没有任何执行能力：不要写代码、不要给步骤清单、不要假设已经开了任务。\n\
     不要复述正文原文，只给结论和一句中文理由。\n\
     严格输出一行 JSON，不要额外文字与代码围栏：\n\
     {\"verdict\":\"worth|not_worth|unsure\",\"reason\":\"不超过40字的中文理由\"}"
        .to_string()
}

/// 一条候选 → 消息列表（system + user）。
///
/// 正文字数在这里截断：账本存的可能是整段评论，但「值不值得开工」不需要读完一整篇。
pub fn build_messages(candidate: &Value) -> Vec<Value> {
    let text = candidate.get("text").and_then(Value::as_str).unwrap_or("");
    let clipped = clip(text, MAX_PROMPT_TEXT_CHARS);
    let kind = candidate
        .get("sourceKind")
        .and_then(Value::as_str)
        .unwrap_or("");
    let strength = candidate
        .get("strength")
        .and_then(Value::as_str)
        .unwrap_or("");
    let lines = vec![
        format!("入口类型：{}", source_kind_label(kind)),
        format!(
            "入口实例：{}",
            empty_to_none(candidate.get("sourceInstance")).unwrap_or("（本机）")
        ),
        format!(
            "事件类型：{}",
            empty_to_none(candidate.get("eventKind")).unwrap_or("（未声明）")
        ),
        format!(
            "发起人：{}",
            empty_to_none(candidate.get("actor")).unwrap_or("（未知）")
        ),
        format!("授权强度：{}", strength_label(strength)),
        format!(
            "第一层判定理由：{}",
            empty_to_none(candidate.get("reason")).unwrap_or("（无）")
        ),
        format!(
            "会话键：{}",
            empty_to_none(candidate.get("threadKey")).unwrap_or("（无会话归属）")
        ),
        format!("事件正文（可能已截断）：{clipped}"),
        "请按格式输出 JSON。".to_string(),
    ];
    vec![
        json!({ "role": "system", "content": system_prompt() }),
        json!({ "role": "user", "content": lines.join("\n") }),
    ]
}

/// 模型输出 → `(结论, 理由)`。
///
/// 容错但不猜：解析不出 JSON 时先看有没有出现明确的结论词（`not_worth` 先于 `worth`，
/// 避免子串误判），一个都没有就是 `unsure`，理由里带回模型实际说的那句（截断）。
pub fn parse_assessment(raw: &str) -> (String, String) {
    let cleaned = strip_fences(raw);
    let spoken = || clip(cleaned.trim(), MAX_ASSESS_REASON_CHARS);
    if let Some(object) = json_slice(&cleaned) {
        if let Ok(value) = serde_json::from_str::<Value>(object) {
            let verdict = normalize_verdict(value.get("verdict").and_then(Value::as_str));
            let reason = value
                .get("reason")
                .and_then(Value::as_str)
                .map(|text| clip(text.trim(), MAX_ASSESS_REASON_CHARS))
                .unwrap_or_default();
            return match verdict {
                Some(verdict) => (
                    verdict.to_string(),
                    if reason.is_empty() {
                        "模型没给理由".to_string()
                    } else {
                        reason
                    },
                ),
                // JSON 形状对了但结论字段不认识：不猜成 worth，也不当成失败
                None => (
                    VERDICT_UNSURE.to_string(),
                    if reason.is_empty() {
                        "模型给的结论不在允许的三档里".to_string()
                    } else {
                        reason
                    },
                ),
            };
        }
    }
    let lowered = cleaned.to_lowercase();
    for (token, verdict) in [
        (VERDICT_NOT_WORTH, VERDICT_NOT_WORTH),
        (VERDICT_WORTH, VERDICT_WORTH),
        (VERDICT_UNSURE, VERDICT_UNSURE),
    ] {
        if lowered.contains(token) {
            // 结论词命中了，理由就是模型那句话本身
            return (verdict.to_string(), spoken());
        }
    }
    (
        VERDICT_UNSURE.to_string(),
        fallback(spoken(), "模型未按约定的格式回答，取不到结论"),
    )
}

/// 模型没给理由时用兜底文案；给了就保留给的那句（已按入库上限截断）
fn fallback(text: String, when_silent: &str) -> String {
    if text.trim().is_empty() {
        when_silent.to_string()
    } else {
        text
    }
}

fn normalize_verdict(raw: Option<&str>) -> Option<&'static str> {
    match raw.unwrap_or("").trim().to_ascii_lowercase().as_str() {
        VERDICT_WORTH => Some(VERDICT_WORTH),
        VERDICT_NOT_WORTH => Some(VERDICT_NOT_WORTH),
        VERDICT_UNSURE => Some(VERDICT_UNSURE),
        _ => None,
    }
}

/// 剥掉 ```json 围栏与首尾空白（模型常见的包装，不算格式错误）
fn strip_fences(raw: &str) -> String {
    let text = raw.trim();
    let text = text.trim_start_matches('`').trim_end_matches('`');
    let text = text
        .strip_prefix("json")
        .or_else(|| text.strip_prefix("JSON"))
        .unwrap_or(text);
    text.trim().to_string()
}

/// 取第一个 `{` 到最后一个 `}` 之间的片段；没有就返回 `None`
fn json_slice(text: &str) -> Option<&str> {
    let start = text.find('{')?;
    let end = text.rfind('}')?;
    if end <= start {
        return None;
    }
    Some(&text[start..=end])
}

fn clip(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let mut out: String = text.chars().take(max).collect();
    out.push('…');
    out
}

fn empty_to_none(value: Option<&Value>) -> Option<&str> {
    match value.and_then(Value::as_str) {
        Some(text) if !text.trim().is_empty() => Some(text),
        _ => None,
    }
}

/// 解析本轮要用的模型端点（默认模型 + 容灾链）。
///
/// `runtime_config` 是同步的（读库 + 取钥匙串），放到 blocking 线程里跑，
/// 不占着事件循环。没配默认模型不是错误，是「这一层暂时跑不了」。
async fn resolve_endpoints(store: &Arc<Store>) -> Result<Vec<model::Endpoint>, String> {
    let cfg_store = Arc::clone(store);
    let config = tokio::task::spawn_blocking(move || {
        crate::model::service::runtime_config(&cfg_store, None)
    })
    .await
    .map_err(|e| format!("SUPERAGENT_ASSESS_MODEL_MISSING: 模型配置解析线程异常：{e}"))?
    .map_err(|e| format!("SUPERAGENT_ASSESS_MODEL_MISSING: {e}"))?;
    let Some(config) = config else {
        return Err(
            "SUPERAGENT_ASSESS_MODEL_MISSING: 本机还没配置默认模型，第二层研判跑不起来"
                .to_string(),
        );
    };
    let endpoints = model::endpoints(Some(&config))?;
    if endpoints.is_empty() {
        return Err(
            "SUPERAGENT_ASSESS_MODEL_MISSING: 默认模型没有可用的端点与密钥".to_string(),
        );
    }
    Ok(endpoints)
}

/// 判一条候选：调用模型 → 计量 → 给出结论。
///
/// 调用前已经过了配额闸门（`run` 里检查），这里只负责「问一次、记一次账」。
/// 工具列表传空 `&[]`：这一层没有能力，也没有入口去执行任何东西。
async fn ask(
    store: &Arc<Store>,
    quota: &QuotaGate,
    endpoints: &[model::Endpoint],
    candidate: &Value,
) -> Assessment {
    let messages = build_messages(candidate);
    let never_cancelled = || false;
    let call = model::stream(endpoints, &messages, &[], &never_cancelled);
    let outcome: Result<Outcome, String> =
        match tokio::time::timeout(REQUEST_TIMEOUT, call).await {
            Ok(Ok(outcome)) => Ok(outcome),
            Ok(Err(ModelError::Cancelled)) => Err(
                "SUPERAGENT_ASSESS_FAILED: 研判调用被取消（影子循环不应该出现，请反馈）"
                    .to_string(),
            ),
            Ok(Err(ModelError::Failed(err))) => {
                Err(format!("SUPERAGENT_ASSESS_FAILED: {err}"))
            }
            Err(_) => Err(format!(
                "SUPERAGENT_ASSESS_TIMEOUT: 模型在 {} 秒内没给出研判",
                REQUEST_TIMEOUT.as_secs()
            )),
        };
    let outcome = match outcome {
        Ok(outcome) => outcome,
        Err(error) => return Assessment::failed(error),
    };

    // 用量先入账再判定：超时与解析失败都不能让这次调用变成「没花过钱」。
    // 注意超时分支拿不到 usage（响应没回来），那部分花费只能由配额台账之外的真实账单体现。
    let tokens = outcome.prompt_tokens + outcome.completion_tokens;
    if tokens > 0 {
        quota.add_usage(tokens);
        crate::model::usage::record_sample_quiet(
            store,
            &outcome.model,
            outcome.prompt_tokens,
            outcome.completion_tokens,
            Some(outcome.latency_ms as i64),
            outcome.cost_micros,
            outcome.currency.as_deref(),
            outcome.cost_micros.is_some(),
        );
    }
    let (verdict, reason) = parse_assessment(&outcome.text);
    Assessment::done(
        verdict,
        reason,
        if outcome.model.trim().is_empty() {
            endpoints
                .first()
                .map(|ep| ep.display.clone())
                .unwrap_or_default()
        } else {
            outcome.model.clone()
        },
        tokens as i64,
    )
}

/// 一轮第二层研判：取当前策略版本下的阳性候选 → 逐条问模型 → 回写留痕。
///
/// 三道成本闸按「从便宜到贵」的顺序检查，任一命中都不发起调用：
/// 档位 `off`、`assessMaxPerDay` 为 0、当日预算用满、以及每条候选各自的配额闸门。
/// 返回摘要而不是错误——这一层跑不起来只是「今天没判」，不该把巡检循环或界面打红。
pub async fn run(store: &Arc<Store>, quota: &QuotaGate, limit: Option<usize>) -> Value {
    let started = ledger::now_ms();
    let (policy_version, doc) = ledger::effective_policy(store);
    let batch = limit
        .unwrap_or(DEFAULT_BATCH)
        .clamp(1, MAX_BATCH)
        .min(doc.assess_cap().max(0) as usize);
    let base = json!({
        "ok": true,
        "policyVersion": policy_version,
        "assessCap": doc.assess_cap(),
        "capUsedToday": 0,
        "batch": batch,
        "pending": 0,
        "attempted": 0,
        "written": 0,
        "superseded": 0,
        "worth": 0,
        "notWorth": 0,
        "unsure": 0,
        "failed": 0,
        "blocked": 0,
        "unavailable": 0,
        "model": null,
        "skipped": null,
        "skipReason": null,
        "lastError": null,
        "note": "影子模式：第二层只给「值不值得开工」的判断并记账，不创建任务、不执行任何动作",
    });
    let mut summary = base;
    // 档位 off：整轮研判都不做（第一层已经在 sources 侧跳过，这里兜住「只判存量候选」）
    if doc.accept_mode == policy::MODE_OFF {
        set_skip(&mut summary, "off", "当前档位是 off（不研判），第二层同样跳过");
        return finish(summary, started);
    }
    if doc.assess_cap() == 0 {
        set_skip(&mut summary, "cap_off", "策略里 assessMaxPerDay 为 0：第二层（模型研判）已被显式关闭");
        return finish(summary, started);
    }
    let used_today = ledger::count_assessed_today(store).unwrap_or(0);
    summary["capUsedToday"] = json!(used_today);
    if used_today >= doc.assess_cap() {
        set_skip(
            &mut summary,
            "cap_reached",
            &format!(
                "今日模型研判已达上限 {} 条，事件仍照常记账与判定",
                doc.assess_cap()
            ),
        );
        return finish(summary, started);
    }
    let room = (doc.assess_cap() - used_today).max(0) as usize;
    let pending = match ledger::list_unassessed_candidates(store, policy_version, Some(room.min(batch)))
    {
        Ok(rows) => rows,
        Err(error) => {
            set_skip(&mut summary, "read_failed", &error);
            return finish(summary, started);
        }
    };
    summary["pending"] = json!(pending.len());
    if pending.is_empty() {
        set_skip(&mut summary, "no_pending", "当前策略版本下没有待研判的候选（阴性判定不进这一层）");
        return finish(summary, started);
    }
    let mut endpoints: Option<Vec<model::Endpoint>> = None;
    let mut tally = Tally::default();
    for candidate in pending {
        let decision_id = candidate
            .get("decisionId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if decision_id.is_empty() {
            continue;
        }
        // 每条候选单独过一次配额闸门：一次调用可能就跨过 100% 档
        if let Err(code) = quota.check_submit() {
            tally.blocked += 1;
            tally.last_error = format!(
                "{code}: 当日 token 配额已到阻断档，剩余候选本轮不发起模型调用"
            );
            break;
        }
        if endpoints.is_none() {
            match resolve_endpoints(store).await {
                Ok(resolved) => {
                    summary["model"] = json!(resolved.first().map(|ep| ep.display.clone()));
                    endpoints = Some(resolved);
                }
                Err(error) => {
                    tally.unavailable += 1;
                    tally.last_error = error;
                    break;
                }
            }
        }
        let Some(resolved) = endpoints.as_ref() else {
            break;
        };
        let assessment = ask(store, quota, resolved, &candidate).await;
        tally.attempted += 1;
        tally.absorb(&assessment);
        if assessment.persists() {
            match ledger::record_assessment(store, &decision_id, policy_version, &assessment) {
                Ok(ledger::AssessmentWrite::Written) => tally.written += 1,
                // 写不进去通常意味着这一行已经被判过（并发的一轮先落了笔），不算失败
                Ok(ledger::AssessmentWrite::AlreadyJudged) => {}
                // 模型回答期间策略升了版：这一份是按旧策略语境问出来的结论，作废。
                // 钱已经记进配额与用量（调用发出时就记了），只是不用它——候选已按新版本重排，
                // 下一轮会再判一次。宁可多花一次，也不留一个版本对不上又看不出来的结论。
                Ok(ledger::AssessmentWrite::Superseded) => tally.superseded += 1,
                Err(error) => tally.last_error = error,
            }
        }
    }
    tally.apply(&mut summary);
    if tally.superseded > 0 {
        summary["note"] = json!(format!(
            "影子模式：第二层只判断并记账，不执行任何动作。本轮有 {} 条结论因策略升版作废\
             （这一轮是按 v{} 取出的候选），候选已按新的策略版本重新排队、会再判一次\
             （那几次调用的 token 已经花掉，不退）",
            tally.superseded, policy_version
        ));
    }
    if !tally.last_error.is_empty() {
        summary["lastError"] = json!(tally.last_error);
        if tally.blocked > 0 {
            set_skip(&mut summary, "quota_blocked", &tally.last_error);
        } else if tally.unavailable > 0 {
            set_skip(&mut summary, "model_unavailable", &tally.last_error);
        }
    }
    finish(summary, started)
}

fn set_skip(summary: &mut Value, reason: &str, text: &str) {
    summary["skipped"] = json!(reason);
    summary["skipReason"] = json!(text);
}

fn finish(mut summary: Value, started: i64) -> Value {
    summary["tookMs"] = json!(ledger::now_ms() - started);
    summary["startedAtMs"] = json!(started);
    summary
}

/// 一轮的计数（避免在 `run` 里散落七八个 `summary[...] += `）
#[derive(Default)]
struct Tally {
    attempted: usize,
    written: usize,
    /// 结论因策略升版被作废的条数（钱花了，结论不用，候选按新版重排后再判）
    superseded: usize,
    worth: usize,
    not_worth: usize,
    unsure: usize,
    failed: usize,
    blocked: usize,
    unavailable: usize,
    last_error: String,
}

impl Tally {
    fn absorb(&mut self, assessment: &Assessment) {
        match assessment.status.as_str() {
            STATUS_DONE => match assessment.verdict.as_str() {
                VERDICT_WORTH => self.worth += 1,
                VERDICT_NOT_WORTH => self.not_worth += 1,
                _ => self.unsure += 1,
            },
            STATUS_FAILED => self.failed += 1,
            _ => {}
        }
        if !assessment.error.is_empty() {
            self.last_error = assessment.error.clone();
        }
    }

    fn apply(&self, summary: &mut Value) {
        summary["attempted"] = json!(self.attempted);
        summary["written"] = json!(self.written);
        summary["superseded"] = json!(self.superseded);
        summary["worth"] = json!(self.worth);
        summary["notWorth"] = json!(self.not_worth);
        summary["unsure"] = json!(self.unsure);
        summary["failed"] = json!(self.failed);
        summary["blocked"] = json!(self.blocked);
        summary["unavailable"] = json!(self.unavailable);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;
    use crate::superagent::{gate, policy, sources};
    use serde_json::json;

    fn temp_store(name: &str) -> (Arc<Store>, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-sa-assess-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Arc::new(Store::open(dir.join("tongtian.db")).unwrap());
        (store, dir)
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("tt-sa-assess-scratch-{name}-{}", ledger::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn cleanup(dir: &std::path::Path) {
        let _ = std::fs::remove_dir_all(dir);
    }

    /// 造一条真候选（与导出测试同一套路：登记入口 → 开读取 → 授权 → 巡检一轮）
    async fn one_candidate(tag: &str, assess_cap: i64) -> (Arc<Store>, std::path::PathBuf, std::path::PathBuf) {
        let (store, db_dir) = temp_store(tag);
        let watched = scratch(&format!("{tag}-watched"));
        std::fs::create_dir_all(watched.join("docs")).unwrap();
        std::fs::write(watched.join("docs/评审请求.md"), "正文永不进提示词之外的地方".as_bytes()).unwrap();
        ledger::ensure_default_policy(&store).unwrap();
        ledger::save_policy(
            &store,
            &json!({
                "acceptMode": policy::MODE_OBSERVE,
                "dailyEvalCap": 50,
                "assessMaxPerDay": assess_cap,
                "sources": [{
                    "kind": crate::superagent::SOURCE_KIND_WORKSPACE_FILE,
                    "instance": "", "enabled": true, "label": "工作区",
                    "allowUsers": [], "bots": [],
                    "rules": [{ "id": "any", "enabled": true, "minChars": 4 }]
                }]
            }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        let source = ledger::register_source(
            &store,
            crate::superagent::SOURCE_KIND_WORKSPACE_FILE,
            "",
            "工作区",
            json!({ "dir": watched.to_string_lossy() }),
        )
        .unwrap();
        ledger::set_source_enabled(&store, &source.id, true).unwrap();
        let summary = sources::pump_once(&store).await;
        assert_eq!(summary["candidates"].as_i64(), Some(1), "前置数据没造出来");
        (store, db_dir, watched)
    }

    fn quota_with(used: u64, total: u64) -> QuotaGate {
        let gate = QuotaGate::new(None, total);
        if used > 0 {
            gate.add_usage(used);
        }
        gate
    }

    fn candidate_fixture() -> Value {
        json!({
            "decisionId": "sadec-1",
            "sourceKind": crate::superagent::SOURCE_KIND_PROJECT_EVENT,
            "sourceInstance": "prj-1",
            "eventKind": "task.commented",
            "actor": "usr-owner",
            "strength": gate::STRENGTH_INFERRED,
            "reason": "命中启用规则 review-request",
            "threadKey": "tsk-1",
            "text": "帮我评审这份部署方案，重点是回滚路径".to_string(),
            "payload": { "note": "别把这里的东西搬进提示词", "token": "sk-secret" }
        })
    }

    #[test]
    fn prompt_carries_the_ledger_fields_and_no_secrets() {
        let messages = build_messages(&candidate_fixture());
        assert_eq!(messages.len(), 2, "system + user 两条，不带历史");
        let user = messages[1]["content"].as_str().unwrap();
        assert!(user.contains("项目事件"), "入口类型用 Rust 的显示名：{user}");
        assert!(user.contains("主动推断"), "强度用显示名，不出现英文原词");
        assert!(user.contains("命中启用规则 review-request"), "第一层的理由要一起给模型");
        assert!(user.contains("帮我评审这份部署方案"));
        // 载荷不进提示词：那里面上游想放什么都有，凭据字段尤其不能跟着出本机
        assert!(!user.contains("sk-secret"), "密钥不许进提示词");
        assert!(!user.contains("别把这里的东西"), "载荷不进提示词");
        // 系统提示词自己声明没有执行能力
        let system = messages[0]["content"].as_str().unwrap();
        assert!(system.contains("影子模式"));
        assert!(system.contains("不要写代码"));
    }

    #[test]
    fn prompt_clips_long_text_without_breaking_chars() {
        let mut candidate = candidate_fixture();
        candidate["text"] = json!("长".repeat(MAX_PROMPT_TEXT_CHARS + 500));
        let user = build_messages(&candidate)[1]["content"]
            .as_str()
            .unwrap()
            .to_string();
        // 截断到位且带省略号，超出部分一个字都不许进请求
        assert!(user.contains(&format!("{}…", "长".repeat(MAX_PROMPT_TEXT_CHARS))));
        assert!(!user.contains(&"长".repeat(MAX_PROMPT_TEXT_CHARS + 1)), "超出部分不许进请求");
    }

    #[test]
    fn verdict_parsing_accepts_fences_and_rejects_guessing() {
        let (verdict, reason) = parse_assessment("{\"verdict\":\"worth\",\"reason\":\"目标明确可直接开工\"}");
        assert_eq!(verdict, VERDICT_WORTH);
        assert_eq!(reason, "目标明确可直接开工");
        // 代码围栏与前后寒暄都不算格式错误
        let (verdict, _) = parse_assessment("```json\n{\"verdict\":\"not_worth\",\"reason\":\"只是打招呼\"}\n```");
        assert_eq!(verdict, VERDICT_NOT_WORTH);
        // 一句话里同时出现两个结论词：更保守的那个赢
        let (verdict, _) = parse_assessment("not_worth（不是 worth）");
        assert_eq!(verdict, VERDICT_NOT_WORTH);
        // 不认识的值不许被猜成 worth
        let (verdict, reason) = parse_assessment("{\"verdict\":\"maybe\",\"reason\":\"信息不足\"}");
        assert_eq!(verdict, VERDICT_UNSURE, "非三档值只能算拿不准：{reason}");
        // 完全没结论：unsure + 保留模型那句话（截断）作为理由，界面看得懂为什么没结论
        let (verdict, reason) = parse_assessment("我需要更多上下文才能判断");
        assert_eq!(verdict, VERDICT_UNSURE);
        assert!(reason.contains("更多上下文"));
        // 理由的截断口径：`clip` 留 max 个字再加一个省略号，整段回答不许搬进判定行
        let long = "理".repeat(MAX_ASSESS_REASON_CHARS + 100);
        let (_, reason) = parse_assessment(&format!("{{\"verdict\":\"worth\",\"reason\":\"{long}\"}}"));
        assert!(reason.chars().count() <= MAX_ASSESS_REASON_CHARS + 1);
        assert!(reason.ends_with('…'), "被截断的理由要看得出来被截断了");
        assert!(!reason.contains(&"理".repeat(MAX_ASSESS_REASON_CHARS + 1)));
    }

    #[test]
    fn only_done_and_failed_are_written_back() {
        assert!(Assessment::done(VERDICT_WORTH.to_string(), "r".into(), "m".into(), 1).persists());
        assert!(Assessment::failed("SUPERAGENT_ASSESS_TIMEOUT: 太慢".into()).persists());
        // 被挡住的两类不写库：写了就等于「这条候选已经判过」，下一轮再也不会判它
        for status in [STATUS_BLOCKED, STATUS_UNAVAILABLE] {
            let assessment = Assessment {
                status: status.to_string(),
                verdict: String::new(),
                reason: String::new(),
                model: String::new(),
                tokens: 0,
                error: "QUOTA_EXCEEDED: 当日配额已到阻断档".to_string(),
            };
            assert!(!assessment.persists(), "{status} 不许写进判定行");
        }
        assert_eq!(status_label(STATUS_BLOCKED), "配额阻断（未发起调用）");
        assert_eq!(status_label(""), "未研判");
        assert_eq!(verdict_label(VERDICT_NOT_WORTH), "不值得现在开工");
        assert_eq!(verdict_label(""), "未研判");
    }

    #[tokio::test]
    async fn closed_switches_skip_without_calling_the_model() {
        // 三种「不花钱」的开关都要在发起调用之前就返回（离线机器上同样成立）
        let (store, db_dir, watched) = one_candidate("off", 50).await;
        let quota = quota_with(0, 100_000);
        ledger::save_policy(
            &store,
            &json!({ "acceptMode": policy::MODE_OFF, "dailyEvalCap": 50, "assessMaxPerDay": 50, "sources": [] }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        let summary = run(&store, &quota, None).await;
        assert_eq!(summary["skipped"].as_str(), Some("off"), "{summary}");
        assert_eq!(summary["attempted"].as_i64(), Some(0));

        // assessMaxPerDay = 0：用户显式关掉第二层，候选照旧记账，只是不问模型
        ledger::save_policy(
            &store,
            &json!({ "acceptMode": policy::MODE_OBSERVE, "dailyEvalCap": 50, "assessMaxPerDay": 0, "sources": [] }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        let summary = run(&store, &quota, None).await;
        assert_eq!(summary["skipped"].as_str(), Some("cap_off"), "{summary}");
        assert_eq!(summary["attempted"].as_i64(), Some(0));
        // 关掉第二层只是「不问模型」：候选还在，也没有被悄悄写上一个结论
        let rows = ledger::list_candidates(&store, None).unwrap();
        assert_eq!(rows.len(), 1, "关掉第二层不该动候选清单");
        assert_eq!(rows[0]["assessStatus"].as_str(), Some(""));
        assert_eq!(rows[0]["assessVerdict"].as_str(), Some(""));
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn daily_budget_is_counted_from_the_ledger_not_from_memory() {
        // 预算分子读库：重启进程不会把「今天已经判过几条」清零，这是成本闸门的底线
        let (store, db_dir, watched) = one_candidate("cap", 1).await;
        let quota = quota_with(0, 100_000);
        let pending = ledger::list_unassessed_candidates(
            &store,
            ledger::effective_policy(&store).0,
            None,
        )
        .unwrap();
        let decision_id = pending[0]["decisionId"].as_str().unwrap().to_string();
        ledger::record_assessment(
            &store,
            &decision_id,
            ledger::effective_policy(&store).0,
            &Assessment::done(VERDICT_UNSURE.to_string(), "先占一条预算".into(), "某模型".into(), 12),
        )
        .unwrap();
        assert_eq!(ledger::count_assessed_today(&store).unwrap(), 1);
        let summary = run(&store, &quota, None).await;
        assert_eq!(summary["skipped"].as_str(), Some("cap_reached"), "{summary}");
        assert_eq!(summary["attempted"].as_i64(), Some(0));
        assert_eq!(summary["assessCap"].as_i64(), Some(1));
        // 判过的那条不再出现在待研判队列里
        assert!(ledger::list_unassessed_candidates(
            &store,
            ledger::effective_policy(&store).0,
            None,
        )
        .unwrap()
        .is_empty());
        // 已研判的旧候选不重排：换版之后不会为同一个判断再花一次钱
        ledger::save_policy(
            &store,
            &json!({ "acceptMode": policy::MODE_OBSERVE, "dailyEvalCap": 50, "assessMaxPerDay": 30, "sources": [] }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        let new_version = ledger::effective_policy(&store).0;
        assert!(ledger::list_unassessed_candidates(&store, new_version, None)
            .unwrap()
            .is_empty());
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn quota_block_leaves_the_candidate_unjudged() {
        // 100% 档：一条模型调用都不发起，判定行也保持「未研判」——
        // 配额恢复或跨日重置后，这一条还会被队列读到
        let (store, db_dir, watched) = one_candidate("blocked", 50).await;
        let quota = quota_with(20, 10);
        assert!(quota.check_submit().is_err(), "前置：闸门应当已经阻断");
        let summary = run(&store, &quota, None).await;
        assert_eq!(summary["attempted"].as_i64(), Some(0), "阻断时不许发调用");
        assert_eq!(summary["blocked"].as_i64(), Some(1), "{summary}");
        assert_eq!(summary["skipped"].as_str(), Some("quota_blocked"), "{summary}");
        assert!(summary["skipReason"]
            .as_str()
            .unwrap()
            .contains("QUOTA_EXCEEDED"));
        let version = ledger::effective_policy(&store).0;
        assert_eq!(
            ledger::list_unassessed_candidates(&store, version, None)
                .unwrap()
                .len(),
            1,
            "被挡住的候选必须还排在队列里"
        );
        assert_eq!(ledger::count_assessed_today(&store).unwrap(), 0);
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn requeued_candidates_come_back_into_the_next_round() {
        // 策略升版把待研判候选重排到新队列之后，下一轮必须重新看见它——
        // 否则「重排队」只是账本上的一个说法，跑起来还是把候选丢在旧版本里。
        let (store, db_dir, watched) = one_candidate("requeue", 50).await;
        let old_version = ledger::effective_policy(&store).0;
        let bump = ledger::save_policy(
            &store,
            &json!({
                "acceptMode": policy::MODE_OBSERVE,
                "dailyEvalCap": 40,
                "assessMaxPerDay": 50,
                "sources": []
            }),
            ledger::POLICY_SOURCE_USER,
        )
        .unwrap();
        assert_eq!(bump.version, old_version + 1);
        assert_eq!(bump.requeued, 1, "候选要按新版本重排：{bump:?}");

        // 配额阻断这一轮不发起任何调用，但队列读数要如实反映「还欠一条」
        let quota = quota_with(20, 10);
        let summary = run(&store, &quota, None).await;
        assert_eq!(summary["policyVersion"].as_i64(), Some(bump.version), "{summary}");
        assert_eq!(summary["pending"].as_i64(), Some(1), "重排后的候选要重新进入队列");
        assert_eq!(summary["attempted"].as_i64(), Some(0), "阻断的一轮不许发调用");
        assert_eq!(summary["superseded"].as_i64(), Some(0), "没发调用就没什么可作废");
        // 判定行仍是「未研判」，且留痕里的判定版本没有被改写
        let row = &ledger::list_candidates(&store, None).unwrap()[0];
        assert_eq!(row["assessStatus"].as_str(), Some(""));
        assert_eq!(row["policyVersion"].as_i64(), Some(old_version));
        assert_eq!(row["assessQueueVersion"].as_i64(), Some(bump.version));
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn negatives_never_enter_the_second_layer() {
        // 阴性判定（未获授权）不进这一层：没有「值不值得开工」可问，也不该花钱
        let (store, db_dir) = temp_store("negative");
        ledger::ensure_default_policy(&store).unwrap();
        let source = ledger::register_source(
            &store,
            crate::superagent::SOURCE_KIND_IM_INBOUND,
            "chat-1",
            "IM",
            json!({}),
        )
        .unwrap();
        let trigger = ledger::NewTrigger {
            source_id: source.id.clone(),
            external_id: "evt-1".to_string(),
            kind: "message.inbound".to_string(),
            text: "在吗".to_string(),
            actor: "usr-x".to_string(),
            thread_key: "chat-1".to_string(),
            addressed_agent: false,
            source_seq: 1,
            payload: json!({}),
            arrived_at_ms: None,
        };
        let outcome = ledger::ingest(&store, &trigger).unwrap();
        let verdict = gate::decide(
            &ledger::effective_policy(&store).1,
            &gate::GateInput {
                kind: &source.kind,
                instance: &source.instance,
                actor: &trigger.actor,
                event_kind: &trigger.kind,
                text: &trigger.text,
                thread_key: &trigger.thread_key,
                addressed_agent: false,
            },
        );
        assert!(!verdict.would_accept, "缺省策略下不该是阳性");
        ledger::record_decision(&store, &outcome.trigger.id, 1, &verdict).unwrap();
        let version = ledger::effective_policy(&store).0;
        assert!(ledger::list_unassessed_candidates(&store, version, None)
            .unwrap()
            .is_empty());
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn write_back_happens_once_per_candidate() {
        let (store, db_dir, watched) = one_candidate("once", 50).await;
        let version = ledger::effective_policy(&store).0;
        let pending = ledger::list_unassessed_candidates(&store, version, None).unwrap();
        assert_eq!(pending.len(), 1);
        let decision_id = pending[0]["decisionId"].as_str().unwrap().to_string();
        assert_eq!(pending[0]["assessStatus"].as_str(), Some(""));
        let first = Assessment::done(
            VERDICT_WORTH.to_string(),
            "诉求具体，能直接开工".to_string(),
            "qwen-plus".to_string(),
            321,
        );
        assert_eq!(
            ledger::record_assessment(&store, &decision_id, version, &first).unwrap(),
            ledger::AssessmentWrite::Written
        );
        // 第二次写（比如另一轮巡检抢到了同一条）不改已有结论：一条候选只判一次
        let second = Assessment::failed("SUPERAGENT_ASSESS_FAILED: 重复写".to_string());
        assert_eq!(
            ledger::record_assessment(&store, &decision_id, version, &second).unwrap(),
            ledger::AssessmentWrite::AlreadyJudged,
            "已研判的行不该被第二次回写"
        );
        let row = ledger::list_candidates(&store, None)
            .unwrap()
            .into_iter()
            .find(|row| row["decisionId"].as_str() == Some(&decision_id))
            .unwrap();
        assert_eq!(row["assessStatus"].as_str(), Some(STATUS_DONE));
        assert_eq!(row["assessVerdict"].as_str(), Some(VERDICT_WORTH));
        assert_eq!(row["assessReason"].as_str(), Some("诉求具体，能直接开工"));
        assert_eq!(row["assessModel"].as_str(), Some("qwen-plus"));
        assert_eq!(row["assessTokens"].as_i64(), Some(321));
        assert!(row["assessedAtMs"].as_i64().unwrap() > 0);
        // 统计跟着走：已研判 1、待研判 0
        let stats = ledger::stats(&store).unwrap();
        assert_eq!(stats["assessed"].as_i64(), Some(1));
        assert_eq!(stats["assessPending"].as_i64(), Some(0));
        assert_eq!(stats["assessedToday"].as_i64(), Some(1));
        cleanup(&watched);
        cleanup(&db_dir);
    }

    #[tokio::test]
    async fn failed_attempts_are_recorded_but_not_retried() {
        let (store, db_dir, watched) = one_candidate("failed", 50).await;
        let version = ledger::effective_policy(&store).0;
        let decision_id = ledger::list_unassessed_candidates(&store, version, None).unwrap()[0]["decisionId"]
            .as_str()
            .unwrap()
            .to_string();
        ledger::record_assessment(
            &store,
            &decision_id,
            version,
            &Assessment::failed("SUPERAGENT_ASSESS_TIMEOUT: 模型在 30 秒内没给出研判".to_string()),
        )
        .unwrap();
        // 失败也计一次预算（确实花了钱），但不再自动重试
        assert_eq!(ledger::count_assessed_today(&store).unwrap(), 1);
        assert!(ledger::list_unassessed_candidates(&store, version, None)
            .unwrap()
            .is_empty());
        let row = ledger::list_candidates(&store, None).unwrap()[0].clone();
        assert_eq!(row["assessStatus"].as_str(), Some(STATUS_FAILED));
        assert_eq!(row["assessVerdict"].as_str(), Some(""));
        assert!(row["assessError"].as_str().unwrap().contains("SUPERAGENT_ASSESS_TIMEOUT"));
        cleanup(&watched);
        cleanup(&db_dir);
    }
}
