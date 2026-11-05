//! 本地 harness 执行器：计划生成、两阶段审批后执行、断点续跑与事件播报。
//!
//! 事件与引擎同走 `agent::ingest_event`（持久化 → 计量 → 适配 → 推送），
//! 因此前端与审计不需要为本地运行时新增分支。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{json, Value};

use crate::bridge::agent::{self, P0Context};
use crate::bridge::sse::AgentEvent;
use crate::harness::checkpoint::{self, Checkpoint, Meta};
use crate::harness::compress;
use crate::harness::model::{self, ModelError, Outcome};
use crate::harness::prompt::{self, PlanStep};
use crate::harness::provider;
use crate::harness::tools::{Gate, ToolBox};
use crate::harness::{max_iters, Slot};
use crate::security::{audit, classify, file_perm, tool_approval};
use crate::P0Services;

/// runtime 里承载文件访问档位的键（与 `chat_submit` 盖进去的是同一个键）
const SEC_FILE_KEY: &str = file_perm::RUNTIME_KEY;

const MODE_NOTE: &str = "本地 harness 运行时：模型路由、文件工具、技能、上下文压缩与检查点在本机进程内完成；\
    本轮不含连接器、浏览器、电脑操控、知识库与专家工具。";
const MAX_AUTO_MEMORY_ENTRIES: usize = 50;
const MAX_AUTO_MEMORY_CHARS: usize = 400;
const SECRET_HINTS: [&str; 12] = [
    "sk-",
    "akia",
    "ghp_",
    "xoxb-",
    "xoxp-",
    "bearer ",
    "api_key=",
    "apikey=",
    "api-key=",
    "password=",
    "secret=",
    "token=",
];

fn p0_of(services: &P0Services, privacy: bool) -> P0Context {
    P0Context {
        events: Some(Arc::clone(&services.events)),
        quota: Some(Arc::clone(&services.quota)),
        registry: Some(Arc::clone(&services.registry)),
        store: Some(Arc::clone(&services.store)),
        // 门控已把 secureEnv 交给引擎；harness 路径下不成立
        secure_env: false,
        privacy,
    }
}

/// 事件播报器：task_id + P0 上下文 + 前端 emit 通道
struct Emit<'a> {
    task_id: &'a str,
    p0: &'a P0Context,
    tx: &'a tokio::sync::mpsc::Sender<Value>,
}

impl Emit<'_> {
    async fn event(&self, kind: &str, data: Value) {
        let ev = AgentEvent {
            kind: kind.to_string(),
            data,
            seq: None,
        };
        agent::ingest_event(&ev, self.task_id, self.p0, self.tx).await;
    }

    /// 一条完整的过程说明（开始 + 完成），与引擎的 context 步骤同形
    async fn note(&self, step_id: &str, text: &str) {
        self.event(
            "step_start",
            json!({ "step_id": step_id, "stepType": "context", "description": text }),
        )
        .await;
        self.event("step_done", json!({ "step_id": step_id, "output": text }))
            .await;
    }

    /// 单任务预算读数（缺陷 D-14.1）：与日额度分开的另一把尺子，只看本任务累计。
    /// 没有配额闸门（Mock 场景）时返回 None，此时不假装拦得住。
    fn task_budget(&self) -> Option<crate::task::quota::TaskBudgetSnapshot> {
        self.p0
            .quota
            .as_ref()
            .map(|gate| gate.task_snapshot(self.task_id))
    }

    /// 本任务是否已用满单任务预算：用满就在下一次模型调用之前停下。
    fn budget_blocked(&self) -> bool {
        self.task_budget().map(|s| s.blocked).unwrap_or(false)
    }

    async fn usage(&self, out: &Outcome) {
        self.event(
            "usage",
            json!({ "usage": {
                "promptTokens": out.prompt_tokens,
                "completionTokens": out.completion_tokens,
                "model": out.model,
                "latencyMs": out.latency_ms,
                // 首包：用量台账不存这一列（库里没有对应字段），但过程事件里要留着，
                // 长任务基准与「慢首包」诊断靠它把「排队」和「出字慢」分开看
                "firstTokenMs": out.first_token_ms,
                "costMicros": out.cost_micros,
                "costCurrency": out.currency,
                "priced": out.cost_micros.is_some(),
            }}),
        )
        .await;
    }

    async fn switches(&self, prefix: &str, out: &Outcome) {
        for (i, switch) in out.switches.iter().enumerate() {
            let from = switch.get("from").and_then(Value::as_str).unwrap_or("");
            let to = switch.get("to").and_then(Value::as_str).unwrap_or("");
            let reason = switch.get("reason").and_then(Value::as_str).unwrap_or("");
            self.event("route_switch", switch.clone()).await;
            self.note(
                &format!("{prefix}-route-{}", i + 1),
                &format!(
                    "主模型「{from}」调用失败，已切换到备用模型「{to}」：{reason}"
                ),
            )
            .await;
        }
    }
}

/// 阶段一：生成计划并进入 pending_review（失败时事件里如实给出错误）。
pub async fn plan(
    app: &tauri::AppHandle,
    services: &P0Services,
    slot: &Arc<Slot>,
) -> Result<(), String> {
    let privacy = slot.snapshot().privacy;
    let (tx, handle) = agent::harness_channel(app);
    let p0 = p0_of(services, privacy);
    let result = plan_inner(&Emit { task_id: &slot.task_id, p0: &p0, tx: &tx }, slot).await;
    match &result {
        Ok(()) => {
            Emit { task_id: &slot.task_id, p0: &p0, tx: &tx }
                .event(
                    "status_change",
                    json!({ "taskId": slot.task_id, "status": "pending_review" }),
                )
                .await;
        }
        Err(err) => {
            let (code, message) = split_code(err);
            Emit { task_id: &slot.task_id, p0: &p0, tx: &tx }
                .event("error", json!({ "code": code, "message": message }))
                .await;
        }
    }
    Emit { task_id: &slot.task_id, p0: &p0, tx: &tx }
        .event("done", json!({ "taskId": slot.task_id }))
        .await;
    drop(tx);
    let _ = handle.await;
    result
}

async fn plan_inner(em: &Emit<'_>, slot: &Arc<Slot>) -> Result<(), String> {
    let snap = slot.snapshot();
    if snap.goal.trim().is_empty() {
        return Err("EMPTY_GOAL: 任务描述为空".to_string());
    }
    let endpoints = model::endpoints(Some(&snap.model_config))?;
    let (system, notes) = prompt::system_prompt(&snap.runtime, &snap.mode, &snap.work_dir);
    em.note("harness-mode", MODE_NOTE).await;
    for (i, note) in notes.iter().enumerate() {
        em.note(&format!("ctx-{}", i + 1), note).await;
    }
    let messages = prompt::plan_messages(&system, &snap.goal, &snap.attachments);
    let out = model::stream(&endpoints, &messages, &[], &|| slot.is_cancelled())
        .await
        .map_err(model_error)?;
    em.usage(&out).await;
    em.switches("plan", &out).await;
    let steps = prompt::parse_plan(&out.text)?;

    slot.update(|inner| {
        inner.plan = steps.clone();
        inner.checkpoint.meta = meta_of(&snap, &steps);
        inner.checkpoint.reason = "计划已生成，等待批准".to_string();
        inner.checkpoint.resumable = false;
        if !snap.privacy {
            // 计划留盘：应用重启后仍可批准执行；没有对话快照，不显示「继续」入口
            if let Err(e) = inner.checkpoint.persist() {
                eprintln!("[harness] prepared checkpoint not saved: {e}");
            }
        }
    });

    em.event(
        "plan",
        json!({
            "plan_id": format!("plan-{}", slot.task_id),
            "steps": prompt::plan_to_events(&steps),
        }),
    )
    .await;
    Ok(())
}

/// 应用 patch：按步骤 id 合并描述（与引擎 modify 同一口径，不允许新增步骤）。
pub async fn modify_plan(
    app: &tauri::AppHandle,
    slot: &Arc<Slot>,
    patch: &Value,
) -> Result<(), String> {
    let snap = slot.snapshot();
    let mut steps = snap.plan.clone();
    if steps.is_empty() {
        return Err("PLAN_INVALID: 本地 harness 没有可修改的计划（可能已重启，请重新提交任务）"
            .to_string());
    }
    if let Some(items) = patch.as_array() {
        for item in items {
            let Some(id) = item.get("id").and_then(Value::as_str) else {
                continue;
            };
            let desc = item
                .get("description")
                .or_else(|| item.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if desc.is_empty() {
                continue;
            }
            if let Some(step) = steps.iter_mut().find(|s| s.id == id) {
                step.description = desc.to_string();
            }
        }
    }
    slot.update(|inner| {
        inner.plan = steps.clone();
        inner.checkpoint.meta.plan = prompt::plan_to_events(&steps);
        if !snap.privacy {
            if let Err(e) = inner.checkpoint.persist() {
                eprintln!("[harness] modified checkpoint not saved: {e}");
            }
        }
    });
    let privacy = snap.privacy;
    // 事件通道单独建一次：modify 不经过模型，只需要重发计划
    let (tx, handle) = agent::harness_channel(app);
    let p0 = P0Context { privacy, ..P0Context::default() };
    let em = Emit { task_id: &slot.task_id, p0: &p0, tx: &tx };
    em.event(
        "plan",
        json!({
            "plan_id": format!("plan-{}", slot.task_id),
            "steps": prompt::plan_to_events(&steps),
        }),
    )
    .await;
    em.event(
        "status_change",
        json!({ "taskId": slot.task_id, "status": "pending_review" }),
    )
    .await;
    em.event("done", json!({ "taskId": slot.task_id })).await;
    drop(tx);
    let _ = handle.await;
    Ok(())
}

/// 拒绝计划：落终态事件并删掉计划留盘。
pub async fn reject(app: &tauri::AppHandle, slot: &Arc<Slot>) {
    let privacy = slot.snapshot().privacy;
    let (tx, handle) = agent::harness_channel(app);
    let p0 = P0Context { privacy, ..P0Context::default() };
    let em = Emit { task_id: &slot.task_id, p0: &p0, tx: &tx };
    em.event(
        "status_change",
        json!({
            "taskId": slot.task_id,
            "status": "done",
            "statusReasonCode": "REJECTED",
        }),
    )
    .await;
    em.event("done", json!({ "taskId": slot.task_id })).await;
    drop(tx);
    let _ = handle.await;
    slot.update(|inner| inner.checkpoint.delete());
}

#[derive(Debug)]
enum End {
    Done {
        final_text: String,
        artifacts: Vec<String>,
    },
    Cancelled,
    /// 单任务预算用满（缺陷 D-14.1）：停在下一轮模型调用之前，检查点留成可续
    BudgetPaused,
    Failed(String),
}

/// 阶段二：执行已批准的计划；resume=true 时从检查点快照继续。
pub async fn execute(
    app: &tauri::AppHandle,
    services: &P0Services,
    slot: &Arc<Slot>,
    resume: bool,
) -> Result<(), String> {
    let privacy = slot.snapshot().privacy;
    let (tx, handle) = agent::harness_channel(app);
    let p0 = p0_of(services, privacy);
    let em = Emit { task_id: &slot.task_id, p0: &p0, tx: &tx };
    let (end, mut cp) = run_loop(&em, slot, resume).await;
    // 收尾单独一层：`execute` 要 AppHandle（事件通道从它身上取），而「被预算停下要把
    // 检查点留成可续、状态要转 budget_paused」这段恰恰是最该被单测真跑一遍的部分。
    let end = finish_end(&em, slot, &mut cp, end).await;
    drop(tx);
    let _ = handle.await;
    match end {
        End::Failed(message) => Err(message),
        _ => Ok(()),
    }
}

/// 把 `run_loop` 的结局落成检查点、任务状态与收尾事件（deliver / status_change / error）。
///
/// 四种结局各有自己的落盘口径：正常完成不留检查点并当场清掉按任务累计；取消与被预算
/// 停下都要把检查点留成可续（差别只在状态与原因）；失败也留，但按「执行中断」措辞。
/// 返回传入的 `end` 供调用方决定返回值。模块内可见即可：只有 `execute` 与单测用得到。
async fn finish_end(
    em: &Emit<'_>,
    slot: &Arc<Slot>,
    cp: &mut Checkpoint,
    end: End,
) -> End {
    let privacy = slot.snapshot().privacy;
    let p0 = em.p0;
    let tx = em.tx;
    let snap = slot.snapshot();
    let steps = snap.plan.clone();
    match &end {
        End::Done { final_text, artifacts } => {
            cp.mark_final(false, "任务已正常完成，无需续跑");
            if !privacy {
                cp.delete();
            }
            // 正常收尾 = 这一份按任务累计再也不会被续跑用到（隐私模式连检查点都没落盘），
            // 当场清掉，别在台账里堆「再也回不来」的任务计数。
            if let Some(gate) = &p0.quota {
                gate.forget_task(&slot.task_id);
            }
            em.event(
                "deliver",
                json!({
                    "summary": "任务完成",
                    "artifacts": artifacts,
                    "final_text": final_text,
                }),
            )
            .await;
        }
        End::Cancelled => {
            cp.mark_final(true, "用户取消了任务，可从最近一次模型调用前的检查点继续");
            em.event(
                "status_change",
                json!({
                    "taskId": slot.task_id,
                    "status": "cancelled",
                    "statusReasonCode": "USER_CANCELLED",
                }),
            )
            .await;
            persist(em, privacy, cp, &snap, &steps).await;
        }
        End::BudgetPaused => {
            // 被预算停下的任务不是「跑完了」也不是「失败了」：检查点必须留得下、起得来。
            cp.mark_final(true, "单任务预算已用满，可从最近一次模型调用前的检查点继续");
            let reason = match em.task_budget() {
                Some(s) => format!(
                    "本任务已用满单任务预算（{} / {} Token）：已在下一次模型调用之前停下。\
                     在设置 →「模型与用量」调高「单任务预算」后，点任务上的「继续」从这里唤回。",
                    s.used_tokens, s.limit_tokens
                ),
                None => "本任务已用满单任务预算：已在下一次模型调用之前停下；\
                        调高「单任务预算」后点任务上的「继续」唤回。"
                    .to_string(),
            };
            // 计量点通常已经转过状态并播报过；这里补一次只在「还没转」时生效，
            // 不重复甩 status_change（见 pause_running_task 的活跃态判定）
            agent::pause_running_task(&slot.task_id, p0, tx, &reason).await;
            persist(em, privacy, cp, &snap, &steps).await;
        }
        End::Failed(message) => {
            cp.mark_final(true, &format!("执行中断：{message}"));
            let (code, text) = split_code(message);
            em.event("error", json!({ "code": code, "message": text }))
                .await;
            persist(em, privacy, cp, &snap, &steps).await;
        }
    }
    let info = cp.public_info();
    slot.update(|inner| inner.checkpoint = cp.clone());
    em.event("checkpoint", info).await;
    em.event("done", json!({ "taskId": slot.task_id })).await;
    end
}

/// 本轮检查点该落进哪个执行体目录。
///
/// 盘上恢复的快照已经带着归属，原样沿用它：续跑时 runtime 是重建的（不带专家、
/// 也可能换了专家），重新盖章会把中断前的进度搬进别人的目录，那一轮的历史就找不回来了。
/// 空归属只有「本线程第一次跑」这一种来源，按 runtime 里的负责人专家 id 盖章；
/// 没有专家就是 `leader`，与升级前的扁平快照同一归属。
fn agent_scope_for(cp_scope: &str, runtime: &Value) -> String {
    if !cp_scope.trim().is_empty() {
        return cp_scope.to_string();
    }
    checkpoint::leader_scope(
        runtime
            .get("expertId")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty()),
    )
}

async fn persist(
    em: &Emit<'_>,
    privacy: bool,
    cp: &mut Checkpoint,
    snap: &crate::harness::Snapshot,
    steps: &[PlanStep],
) {
    if privacy {
        cp.delete();
        em.note("harness-checkpoint", "隐私模式：本轮不写检查点，无法从检查点继续。")
            .await;
        return;
    }
    cp.meta = meta_of(snap, steps);
    if !cp.resumable {
        cp.delete();
        return;
    }
    if let Err(e) = cp.persist() {
        em.note(
            "harness-checkpoint",
            &format!("检查点写盘失败：{e}；刷新后无法从检查点继续。"),
        )
        .await;
    }
}

async fn run_loop(em: &Emit<'_>, slot: &Arc<Slot>, resume: bool) -> (End, Checkpoint) {
    let snap = slot.snapshot();
    let endpoints = match model::endpoints(Some(&snap.model_config)) {
        Ok(v) => v,
        Err(e) => return (End::Failed(e), snap.checkpoint.clone()),
    };
    // 本轮模型画像 + 压缩策略先算出来：换模型的说明要报出新阈值，
    // 而阈值来自压缩策略（与引擎同一口径）。
    let profile = provider::profile_of(&snap.model_config);
    let compress_policy = compress::policy(&snap.runtime, &snap.model_config);
    let skills = prompt::skills_of(&snap.runtime);
    // 档位来自 runtime（提交时盖进去）；老检查点或没有这个键时走缺省构造，
    // 由 ToolBox::new 固定回最保守的「每次询问」，不在这里重复写一遍默认值。
    let declared = snap
        .runtime
        .get(SEC_FILE_KEY)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let built = match declared {
        Some(mode) => ToolBox::with_permission(&snap.work_dir, skills, mode),
        None => ToolBox::new(&snap.work_dir, skills),
    };
    let toolbox = match built {
        Ok(v) => v,
        Err(e) => return (End::Failed(e), snap.checkpoint.clone()),
    };
    let tools = toolbox.specs();
    // 工具清单先按端点规则预检：不符合命名规则的工具不下发（否则整份清单
    // 会被拒，一轮都跑不动），下不去了就要在过程里说清是哪一个、为什么。
    let (specs, mut schema_notes) = provider::validate_tools(&tools);
    schema_notes.extend(profile.notes.iter().cloned());
    for (i, note) in schema_notes.iter().enumerate() {
        em.note(&format!("ctx-model-{i}"), note).await;
    }
    if specs.is_empty() && !tools.is_empty() {
        em.note("harness-no-tools", &provider::no_tools_note(&profile))
            .await;
    }
    let tools = specs;
    // 档位如实播报一次：用户要能在过程里看到「这一轮到底能写到哪」，
    // 而不是事后从被拒的报错里倒推
    em.note(
        "file-permission",
        &crate::security::file_perm::note(
            toolbox.permission(),
            Some(snap.work_dir.as_path()),
        ),
    )
    .await;
    // 线程内换了模型：这句话必须说在过程里——历史是原样带过来的，
    // 只是按新端点重新编码；窗口与压缩阈值的变化也一并给出。
    if let Some(prev) = snap.prev_model.as_ref().map(|v| provider::profile_of(v)) {
        if !provider::same_model(&prev, &profile) {
            em.note(
                "harness-model-switch",
                &provider::switch_note(
                    &prev,
                    &profile,
                    compress_policy.enabled.then_some(compress_policy.trigger_threshold),
                ),
            )
            .await;
        }
    }

    let mut cp = snap.checkpoint.clone();
    cp.begin_run();
    // 检查点归属：负责人（本轮带专家时含专家 id）各占一个目录，团队成员用自己的 scope。
    // 从盘上恢复的快照已经带着归属，不能被本轮重新盖章带进别人的目录。
    cp.agent_scope = agent_scope_for(&cp.agent_scope, &snap.runtime);
    let privacy = snap.privacy;
    // 恢复所需的 meta 一开始就写全：轮次中间也要落盘（见 flush），
    // 那时若 meta 还是空的，检查点文件会因为「工作目录不存在」而废掉。
    cp.meta = meta_of(&snap, &snap.plan);
    let resumed = resume && !cp.messages.is_empty();
    let mut messages = if resumed {
        em.note(
            "harness-resume",
            "已从本地检查点恢复：中断前完成的工具调用会按签名跳过，随后继续执行。",
        )
        .await;
        // 中断前的对话是另一个模型跑出来的：快照原样保留，只按新端点重新编码
        if let Some(from) = checkpoint_model(&cp) {
            if !provider::same_model(&from, &profile) {
                em.note(
                    "harness-model-switch",
                    &provider::resume_switch_note(&from, &profile, cp.messages.len()),
                )
                .await;
            }
        }
        cp.messages.clone()
    } else {
        let (system, notes) = prompt::system_prompt(&snap.runtime, &snap.mode, &snap.work_dir);
        if !resume {
            for (i, note) in notes.iter().enumerate() {
                em.note(&format!("ctx-{i}"), note).await;
            }
        }
        let (user, user_notes) = prompt::user_message(
            &snap.goal,
            &snap.attachments,
            &prompt::plan_descriptions(&snap.plan),
        );
        for (i, note) in user_notes.iter().enumerate() {
            em.note(&format!("ctx-user-{i}"), note).await;
        }
        // 历史轮次按本轮端点重新编码；哪几条没带上、哪几条被改写都摊出来
        let (history, history_notes) = prompt::history_encoding(&snap.history);
        for (i, note) in history_notes.iter().enumerate() {
            em.note(&format!("ctx-history-{i}"), note).await;
        }
        let mut messages = vec![json!({ "role": "system", "content": system })];
        messages.extend(history);
        messages.push(json!({ "role": "user", "content": user }));
        messages
    };

    // 执行前文件基线：恢复时沿用中断前的基线，产物才算得全
    let baseline = if resume && !cp.baseline_files.is_empty() {
        cp.baseline_files.clone()
    } else {
        let now = snapshot_files(&snap.work_dir);
        cp.baseline_files = now.clone();
        now
    };

    let mut step_no = 2u32;
    let first_step = snap
        .plan
        .first()
        .map(|s| s.description.clone())
        .unwrap_or_else(|| "执行任务".to_string());
    em.event(
        "step_start",
        json!({ "step_id": "step-1", "stepType": "llm_call", "description": first_step }),
    )
    .await;

    let max = max_iters(&snap.runtime);
    // 压缩策略在上面已解析（换模型说明要用它的阈值）；首次触发时把真实代价播报一次。
    let mut compress_noted = false;
    for note in &compress_policy.notes {
        em.note("ctx-compress-policy", note).await;
    }
    let mut final_text: Option<String> = None;
    let mut last_text = String::new();
    // 整条容灾链都带不动工具时，模型层退回纯文本并在这里回报：
    // 结尾必须重复一次事实，否则用户读到「已生成 xxx」会以为文件真落了盘
    let mut tools_degraded: Option<String> = None;
    let mut degrade_noted = false;

    // 中断发生在一轮工具调用中间（最常见的是停在文件确认框上）时，
    // 标记对得上就从欠着的那几个工具原地继续：已经拿到结果的那几个直接沿用，
    // 连「重问一次模型」都省了，更不会再赌模型这次给出完全一样的参数。
    if resumed {
        if cp.pending_turn.is_some() {
            match cp.resume_turn(&messages) {
                Ok(pending) => {
                    let total = cp
                        .pending_turn
                        .as_ref()
                        .map(|t| t.calls.len())
                        .unwrap_or(0);
                    cp.clear_turn();
                    if !pending.is_empty() {
                        let names = pending
                            .iter()
                            .map(|c| c.tool.as_str())
                            .collect::<Vec<_>>()
                            .join("、");
                        em.note(
                            "harness-resume-turn",
                            &format!(
                                "中断发生在一次工具调用里：本轮 {total} 个工具调用中，\
                                 前 {} 个的结果已在检查点里，原样沿用、不再重复执行；\
                                 现在从「{names}」继续，需要确认的会再问你一次。",
                                total - pending.len()
                            ),
                        )
                        .await;
                        for call in &pending {
                            if slot.is_cancelled() {
                                return (End::Cancelled, cp);
                            }
                            run_tool_call(
                                &em,
                                slot,
                                &toolbox,
                                &mut cp,
                                &mut messages,
                                &mut step_no,
                                call,
                                privacy,
                            )
                            .await;
                        }
                        flush(&em, privacy, &mut cp, &messages).await;
                    }
                }
                Err(reason) => {
                    cp.clear_turn();
                    let removed = checkpoint::rewind_unpaired(&mut messages);
                    em.note(
                        "harness-resume-turn",
                        &format!(
                            "无法从工具轮次中间原地继续（{reason}）：\
                             已撤掉那条没配齐结果的调用（{removed} 条消息），重新问一次模型；\
                             中断前已成功执行的副作用工具会按签名跳过，不会重复落盘。"
                        ),
                    )
                    .await;
                }
            }
        }
        // 兜底：进入下一次模型请求的消息里不能有半截工具调用或孤儿结果
        // （旧版本留下的检查点、或快照被体积裁剪打断配对时都会出现）。
        let dangling = checkpoint::rewind_unpaired(&mut messages);
        let orphans = checkpoint::drop_orphan_tool_results(&mut messages);
        if dangling + orphans > 0 {
            em.note(
                "harness-resume-turn",
                &format!(
                    "清理了恢复消息里没配齐的工具调用（{dangling} 条调用与结果、{orphans} 条孤儿结果）：\
                     这些内容无法原样交给模型，去掉后从当前进度继续。"
                ),
            )
            .await;
        }
    }

    for _ in 0..max {
        if slot.is_cancelled() {
            return (End::Cancelled, cp);
        }
        // 单任务预算（缺陷 D-14.1）：用满就停在下一轮模型调用之前，
        // 连压缩用的摘要调用也不发——那是真实用量，不能一边说停了继续烧 token。
        if em.budget_blocked() {
            return (End::BudgetPaused, cp);
        }
        if compress_policy.enabled {
            let (estimate, basis) = compress::estimate_tokens(&messages, cp.last_prompt_tokens);
            if estimate >= compress_policy.trigger_threshold {
                let transcript = compress::summary_input(&messages);
                let summary = if transcript.trim().is_empty() {
                    None
                } else {
                    model::oneshot(
                        &endpoints,
                        &compress::summary_prompt(&snap.goal, &transcript),
                        &|| slot.is_cancelled(),
                    )
                    .await
                    .ok()
                };
                match summary.and_then(|s| compress::apply_summary(&messages, &s.text).map(|r| (s, r)))
                {
                    Some((outcome, (next, merged))) => {
                        em.usage(&outcome).await;
                        cp.compressed_messages += merged as u64;
                        messages = next;
                        // 摘要后计数作废：这份压缩过的上下文还没带出网，
                        // 下一次调用回报的 prompt_tokens 才是新基准
                        cp.last_prompt_tokens = 0;
                        if !compress_noted {
                            compress_noted = true;
                            em.note(
                                "ctx-compress",
                                &compress::note(&compress_policy, merged, basis, estimate),
                            )
                            .await;
                        }
                    }
                    None => {
                        if !compress_noted {
                            compress_noted = true;
                            em.note(
                                "ctx-compress",
                                &format!(
                    "上下文已达约 {estimate} tokens（口径：{basis}），本轮未能完成压缩：\
                     要么更早的消息不足以摘要（没有可压缩的内容），要么摘要模型调用失败。\
                     对话原文保持不变，任务继续；若超出窗口被模型拒绝，请缩短任务或换更大窗口的模型。"
                                ),
                            )
                            .await;
                        }
                    }
                }
            }
        }
        cp.capture(&messages);
        let out = match model::stream(&endpoints, &messages, &tools, &|| slot.is_cancelled()).await {
            Ok(v) => v,
            Err(ModelError::Cancelled) => return (End::Cancelled, cp),
            Err(ModelError::Failed(msg)) => return (End::Failed(msg), cp),
        };
        em.usage(&out).await;
        em.switches("run", &out).await;
        // 端点把工具清单拒了、整链也带不动工具：本轮只剩文本回答。
        // 这件事必须写在过程里并在结尾重复一次，否则用户会以为文件已经写好。
        if let Some(degraded) = out.tools_degraded.clone() {
            let model = degraded
                .get("model")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| profile.label());
            let reason = degraded
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            tools_degraded = Some(model.clone());
            if !degrade_noted {
                degrade_noted = true;
                em.note(
                    "harness-tools-degraded",
                    &format!(
                        "模型「{model}」不接受本轮的工具清单，已退回纯文本执行：\
                         这一轮不会再有任何文件读写动作。原因：{reason}\
                         换一个支持工具调用的模型（或在容灾链里配一个）再跑一次，才能真正产出文件。"
                    ),
                )
                .await;
            }
        }
        if out.prompt_tokens > 0 {
            cp.last_prompt_tokens = out.prompt_tokens;
        }
        last_text = out.text.clone();
        if out.tool_calls.is_empty() {
            final_text = Some(out.text);
            break;
        }
        // 工具调用先原样登记进检查点：确认框会阻塞进程，用户在框上退出应用时，
        // 重启后要知道「这一轮欠哪一个」，而不是靠模型再猜一遍同样的参数。
        let turn_calls: Vec<checkpoint::TurnCall> = out
            .tool_calls
            .iter()
            .map(|c| checkpoint::TurnCall {
                id: c.id.clone(),
                tool: c.name.clone(),
                arguments: c.arguments.clone(),
                signature: serde_json::from_str::<Value>(&c.arguments)
                    .ok()
                    .filter(Value::is_object)
                    .map(|args| checkpoint::signature_of(&c.name, &args))
                    .unwrap_or_default(),
            })
            .collect();
        let calls: Vec<Value> = turn_calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.tool, "arguments": c.arguments },
                })
            })
            .collect();
        messages.push(json!({
            "role": "assistant",
            "content": out.text,
            "tool_calls": calls,
        }));
        cp.begin_turn(turn_calls.clone());
        flush(&em, privacy, &mut cp, &messages).await;

        for call in &turn_calls {
            if slot.is_cancelled() {
                return (End::Cancelled, cp);
            }
            run_tool_call(
                &em,
                slot,
                &toolbox,
                &mut cp,
                &mut messages,
                &mut step_no,
                call,
                privacy,
            )
            .await;
        }
        // 整轮的结果都配齐了：标记当场清掉，免得下次恢复误以为还欠工具
        cp.clear_turn();
    }

    let mut text = final_text.unwrap_or_else(|| {
        if last_text.trim().is_empty() {
            "已达到单轮工具循环上限，任务未完成；可继续对话补充指示。".to_string()
        } else {
            last_text
        }
    });
    // 工具被端点整链拒收：正文里再补一句事实。模型很擅长在纯文本模式下
    // 声称「已为你生成 xxx.md」，这句话必须由运行时来说，不能等模型自觉。
    if let Some(model) = tools_degraded {
        text.push_str(&format!(
            "\n\n———\n本轮没有执行任何文件读写：模型「{model}」不接受工具调用，\
             以上只是它的文字回答。改用支持工具调用的模型后重跑这一步，产物才会真正落盘。"
        ));
    }
    em.event(
        "step_done",
        json!({ "step_id": "step-1", "output": truncate_chars(&text, 300) }),
    )
    .await;

    let artifacts = diff_files(&snap.work_dir, &baseline, &snapshot_files(&snap.work_dir));
    if let Some(note) = auto_memory_note(&snap.runtime, snap.privacy, &snap.goal, &text, &artifacts) {
        em.note("memory-auto", &note).await;
    }
    (
        End::Done {
            final_text: text,
            artifacts,
        },
        cp,
    )
}

/// 实时循环与断点续跑共用的单步执行：判档位 →（必要时）系统确认框 → 运行 → 记账与播报。
///
/// 抽出来只为了一件事：恢复时必须走同一条判定路径，否则「当场继续」和
/// 「重启后继续」会出现两套拒绝文案、两种审计记录，用户看不出差别在哪。
async fn run_tool_call(
    em: &Emit<'_>,
    slot: &Arc<Slot>,
    toolbox: &ToolBox,
    cp: &mut Checkpoint,
    messages: &mut Vec<Value>,
    step_no: &mut u32,
    call: &checkpoint::TurnCall,
    privacy: bool,
) {
    let step_id = format!("step-{step_no}");
    *step_no += 1;
    em.event(
        "step_start",
        json!({
            "step_id": step_id,
            "stepType": "tool_call",
            "tool": call.tool,
            "description": format!("调用工具: {}", call.tool),
        }),
    )
    .await;

    let parsed = serde_json::from_str::<Value>(&call.arguments)
        .ok()
        .filter(Value::is_object);
    let Some(args) = parsed else {
        let content = "错误：工具参数不是合法 JSON 对象，本次调用未执行".to_string();
        em.event("step_failed", json!({ "step_id": step_id, "error": content }))
            .await;
        messages.push(json!({ "role": "tool", "tool_call_id": call.id, "content": content }));
        return;
    };

    let signature = checkpoint::signature_of(&call.tool, &args);
    if let Some(prev) = cp.previous_record(&signature) {
        cp.skip_previous(&signature);
        let note = format!(
            "[断点续跑] 工具「{}」这次调用与中断前已成功执行的一次完全相同，\
             已跳过重复执行，直接返回当时的结果。",
            call.tool
        );
        em.event("step_done", json!({ "step_id": step_id, "output": note }))
            .await;
        messages.push(json!({
            "role": "tool",
            "tool_call_id": call.id,
            "content": format!("{note}\n\n{}", prev.content),
        }));
        return;
    }

    // 文件访问档位在动手前判定：拒绝直接回给模型，要求确认就先弹系统框。
    // 确认框是阻塞调用，放到 blocking 线程里跑，别卡住事件循环。
    let run = match toolbox.gate(&call.tool, &args) {
        Gate::Refused(msg) => {
            em.event("step_failed", json!({ "step_id": step_id, "error": msg }))
                .await;
            messages.push(json!({ "role": "tool", "tool_call_id": call.id, "content": msg }));
            return;
        }
        Gate::Ask(desc) => {
            // 关键一步：确认框要等人，先把「这一轮欠这一个」写到盘上再问。
            // 在框上退出应用、断电、进程被杀，都从这一个继续，
            // 而不是重问一次模型、赌它给出完全一样的参数。
            flush(em, privacy, cp, messages).await;
            // 审计只记遮住用户名路径的那一版
            let audit_desc = classify::redact_paths(&desc);
            let text = desc.clone();
            let confirmed = tauri::async_runtime::spawn_blocking(move || {
                tool_approval::confirm_file_action(&text)
            })
            .await;
            let verdict = match confirmed {
                Ok(v) => v,
                Err(e) => Err(format!(
                    "FILE_PERMISSION_CONFIRM_UNAVAILABLE: 确认框线程异常：{e}"
                )),
            };
            match verdict {
                Ok(()) if slot.is_cancelled() => {
                    // 框开着的时候用户点了停止：不能当他没说过，这次写盘作废
                    if let Some(store) = em.p0.store.as_ref() {
                        audit::record_quiet(
                            store,
                            audit::ACTION_FILE_WRITE,
                            audit::SEV_WARNING,
                            &call.tool,
                            &format!("未执行（任务已取消）：{audit_desc}"),
                        );
                    }
                    let content = "任务已取消，本次文件写入未执行".to_string();
                    em.event("step_failed", json!({ "step_id": step_id, "error": content }))
                        .await;
                    messages.push(json!({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    }));
                    return;
                }
                Ok(()) => {
                    if let Some(store) = em.p0.store.as_ref() {
                        audit::record_quiet(
                            store,
                            audit::ACTION_FILE_WRITE,
                            audit::SEV_INFO,
                            &call.tool,
                            &format!("用户放行：{audit_desc}"),
                        );
                    }
                    toolbox.run_after_approval(&call.tool, &args)
                }
                Err(e) => {
                    if let Some(store) = em.p0.store.as_ref() {
                        audit::record_quiet(
                            store,
                            audit::ACTION_FILE_WRITE,
                            audit::SEV_WARNING,
                            &call.tool,
                            &format!("未执行：{audit_desc}；原因：{e}"),
                        );
                    }
                    let content = format!("错误：{e}");
                    em.event("step_failed", json!({ "step_id": step_id, "error": content }))
                        .await;
                    messages.push(json!({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": format!("{content}（档位「每次询问」）"),
                    }));
                    return;
                }
            }
        }
        Gate::Pass => toolbox.run(&call.tool, &args),
    };
    if run.side_effect {
        cp.record_tool(&call.tool, &signature, &run.content);
    }
    em.event(
        "step_done",
        json!({
            "step_id": step_id,
            "output": truncate_chars(&run.content, 300),
        }),
    )
    .await;
    messages.push(json!({
        "role": "tool",
        "tool_call_id": call.id,
        "content": run.content,
    }));
    // 拿到结果就落一次盘：下一个调用可能又是确认框，中途中断要能从这儿接着走
    flush(em, privacy, cp, messages).await;
}

/// 轮次中间的落盘：只更新快照并写文件，不重复播报档位与压缩那些说明。
/// 隐私模式一律不落盘（与收尾的 `persist` 同一口径），失败要说出来但不改变执行。
async fn flush(em: &Emit<'_>, privacy: bool, cp: &mut Checkpoint, messages: &[Value]) {
    if privacy {
        return;
    }
    cp.snapshot(messages);
    if let Err(e) = cp.persist() {
        em.note(
            "harness-checkpoint",
            &format!(
                "检查点写盘失败：{e}；若在这一刻中断，无法从待审批的工具原地继续，\
                 只能重问模型（已成功的副作用仍会按签名跳过）。"
            ),
        )
        .await;
    }
}

fn meta_of(snap: &crate::harness::Snapshot, steps: &[PlanStep]) -> Meta {
    let profile = provider::profile_of(&snap.model_config);
    Meta {
        goal: snap.goal.clone(),
        mode: snap.mode.clone(),
        work_dir: snap.work_dir.display().to_string(),
        history: snap.history.clone(),
        // 图片附件不进检查点（base64 体积大且续跑不支持视觉）
        attachments: snap
            .attachments
            .iter()
            .filter(|a| a.get("image").is_none())
            .cloned()
            .collect(),
        plan: prompt::plan_to_events(steps),
        // 档位跟着检查点走：续跑用当初那一档，而不是重读一次全局默认
        file_permission: snap
            .runtime
            .get(SEC_FILE_KEY)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        // 产出这份快照的模型：换模型续跑时界面要说清「历史仍是原来那份模型写的」。
        // 记的是「设置里那台模型的 id + 显示名」，与本轮画像同源（provider::profile_of），
        // 两边口径不同就会把「没换模型」误报成换了。
        model_id: profile.model_id.clone(),
        model_display: profile
            .known
            .then(|| profile.label())
            .unwrap_or_default(),
    }
}

/// 检查点里记录的「产出模型」：没记（老快照或环境变量兜底）时返回 None，
/// 不拿一个猜出来的名字去报「换了模型」。
fn checkpoint_model(cp: &Checkpoint) -> Option<provider::Profile> {
    if cp.meta.model_id.is_empty() && cp.meta.model_display.is_empty() {
        return None;
    }
    Some(provider::Profile {
        model_id: cp.meta.model_id.clone(),
        display: cp.meta.model_display.clone(),
        known: true,
        ..Default::default()
    })
}

fn model_error(err: ModelError) -> String {
    match err {
        ModelError::Cancelled => "TASK_CANCELLED: 用户取消了任务".to_string(),
        ModelError::Failed(msg) => msg,
    }
}

fn split_code(message: &str) -> (String, String) {
    if let Some((code, rest)) = message.split_once(':') {
        let code = code.trim();
        if !code.is_empty()
            && code.len() <= 40
            && code
                .chars()
                .all(|c| c.is_ascii_uppercase() || c == '_' || c.is_ascii_digit())
        {
            return (code.to_string(), rest.trim().to_string());
        }
    }
    ("HARNESS_ERROR".to_string(), message.to_string())
}

/// 失败码统一口径：`CODE: 中文说明` 取 CODE，未识别时按 HARNESS_ERROR。
pub fn error_code(message: &str) -> String {
    split_code(message).0
}

fn truncate_chars(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    text.chars().take(limit).collect::<String>() + "…"
}

// ── 文件基线 / 产物 ───────────────────────────────────────────

const MAX_SNAPSHOT_ENTRIES: usize = 4_000;

fn snapshot_files(root: &Path) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    walk(root, root, &mut out, 0);
    out
}

fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<String, String>, depth: usize) {
    if out.len() >= MAX_SNAPSHOT_ENTRIES || depth > 8 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() {
            walk(root, &path, out, depth + 1);
            continue;
        }
        if !meta.is_file() {
            continue;
        }
        let Ok(rel) = path.strip_prefix(root) else {
            continue;
        };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis())
            .unwrap_or(0);
        out.insert(
            rel.to_string_lossy().replace('\\', "/"),
            format!("{}:{mtime}", meta.len()),
        );
    }
}

fn diff_files(
    root: &Path,
    before: &BTreeMap<String, String>,
    after: &BTreeMap<String, String>,
) -> Vec<String> {
    let mut out: Vec<String> = after
        .iter()
        .filter(|(path, meta)| before.get(*path) != Some(*meta))
        .filter(|(path, _)| !path.ends_with(".tmp") && !path.contains("/.tt-tmp"))
        .map(|(path, _)| {
            root.join(path)
                .to_string_lossy()
                .to_string()
        })
        .take(50)
        .collect();
    out.sort();
    out
}

// ── 自动记忆（与引擎同一口径：目标 + 结论 + 产物，不额外调用模型） ──

fn one_line(text: &str, limit: usize) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= limit {
        return flat;
    }
    flat.chars().take(limit).collect::<String>() + "…"
}

fn auto_memory_content(goal: &str, final_text: &str, artifacts: &[String]) -> String {
    let mut parts: Vec<String> = Vec::new();
    let goal_line = one_line(goal, 120);
    if !goal_line.is_empty() {
        parts.push(format!("目标：{goal_line}"));
    }
    let conclusion = one_line(final_text, 180);
    if !conclusion.is_empty() {
        parts.push(format!("结论：{conclusion}"));
    }
    let mut names: Vec<String> = Vec::new();
    for item in artifacts {
        if let Some(name) = Path::new(item).file_name().and_then(|n| n.to_str()) {
            if !name.is_empty() && !names.iter().any(|n| n == name) {
                names.push(name.to_string());
            }
        }
    }
    if !names.is_empty() {
        let shown = names.iter().take(3).cloned().collect::<Vec<_>>().join("、");
        parts.push(format!("产物：{shown}{}", if names.len() > 3 { "…" } else { "" }));
    }
    one_line(&parts.join("；"), MAX_AUTO_MEMORY_CHARS)
}

fn looks_like_secret(text: &str) -> bool {
    let lowered = text.to_lowercase();
    SECRET_HINTS.iter().any(|hint| lowered.contains(hint))
}

fn auto_memory_note(
    runtime: &Value,
    privacy: bool,
    goal: &str,
    final_text: &str,
    artifacts: &[String],
) -> Option<String> {
    if privacy {
        return Some("隐私模式已开启，本轮未写入自动记忆".to_string());
    }
    let block = runtime.get("awareness");
    let (mode, auto) = match block {
        Some(b) if b.get("error").and_then(Value::as_str).map(|s| !s.trim().is_empty()).unwrap_or(false) => {
            return Some("意识配置读取失败，本轮未写入自动记忆".to_string());
        }
        Some(b) => (
            b.get("mode").and_then(Value::as_bool).unwrap_or(true),
            b.get("autoMemory").and_then(Value::as_bool).unwrap_or(true),
        ),
        None => (true, true),
    };
    if !mode {
        return Some("意识模式已关闭，本轮未写入自动记忆".to_string());
    }
    if !auto {
        return None;
    }
    let content = auto_memory_content(goal, final_text, artifacts);
    if content.is_empty() {
        return None;
    }
    if looks_like_secret(&content) {
        return Some("本轮内容疑似包含凭据，已跳过自动记忆（避免密钥落盘）".to_string());
    }
    let dir = crate::awareness::dir();
    let entries = match crate::awareness::list_memories(&dir) {
        Ok(v) => v,
        Err(e) => return Some(format!("自动记忆读取失败，本轮未写入：{e}")),
    };
    if entries.iter().any(|e| e.content.trim() == content) {
        return Some("本轮自动记忆与已有条目相同，未重复写入".to_string());
    }
    let auto_count = entries.iter().filter(|e| e.source == "auto").count();
    if auto_count >= MAX_AUTO_MEMORY_ENTRIES {
        return Some(format!(
            "自动记忆已有 {auto_count} 条，本轮未写入（不会自动删除历史记忆，可在设置页整理）"
        ));
    }
    match crate::awareness::add_memory(&dir, &content, Some("auto"), crate::harness::clock::now_ms()) {
        Ok(_) => None,
        Err(e) => Some(format!("自动记忆写入失败：{e}")),
    }
}

/// 从磁盘检查点重建任务（应用重启后审批/续跑）。
pub fn slot_from_checkpoint(
    task_id: &str,
    model_config: Value,
    runtime: Value,
    privacy: bool,
) -> Result<Arc<Slot>, String> {
    let cp = checkpoint::load(task_id).ok_or_else(|| {
        "CHECKPOINT_UNAVAILABLE: 本地 harness 没有可恢复的检查点".to_string()
    })?;
    let work_dir = PathBuf::from(cp.meta.work_dir.trim());
    if cp.meta.work_dir.trim().is_empty() || !work_dir.is_dir() {
        return Err(format!(
            "CHECKPOINT_WORKDIR_MISSING: 检查点的工作目录已不存在：{}",
            cp.meta.work_dir
        ));
    }
    let plan = plan_from_meta(&cp.meta.plan);
    let slot = Slot::new(
        task_id,
        &cp.meta.goal,
        &cp.meta.mode,
        work_dir,
        model_config,
        runtime,
        cp.meta.history.clone(),
        cp.meta.attachments.clone(),
        privacy,
    );
    let plan_for_slot = plan.clone();
    slot.update(|inner| {
        inner.checkpoint = cp.clone();
        inner.plan = plan_for_slot;
    });
    Ok(slot)
}

pub fn plan_from_meta(values: &[Value]) -> Vec<PlanStep> {
    values
        .iter()
        .filter_map(|v| {
            let description = v.get("description").and_then(Value::as_str)?.trim();
            if description.is_empty() {
                return None;
            }
            Some(PlanStep {
                id: v
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("step-1")
                    .to_string(),
                step_type: v
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("llm_call")
                    .to_string(),
                description: description.to_string(),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_code_separates_known_codes() {
        assert_eq!(
            split_code("MODEL_KEY_MISSING: 缺密钥"),
            ("MODEL_KEY_MISSING".to_string(), "缺密钥".to_string())
        );
        assert_eq!(
            split_code("普通错误"),
            ("HARNESS_ERROR".to_string(), "普通错误".to_string())
        );
        assert_eq!(
            split_code("error: lower"),
            ("HARNESS_ERROR".to_string(), "error: lower".to_string())
        );
    }

    #[test]
    fn auto_memory_content_is_one_line_and_capped() {
        let text = auto_memory_content(
            "整理周报\n把华东数据补齐",
            "已生成周报，包含三张表",
            &[
                "/tmp/out/weekly.md".to_string(),
                "/tmp/out/weekly.md".to_string(),
                "/tmp/out/a.xlsx".to_string(),
            ],
        );
        assert!(!text.contains('\n'));
        assert!(text.contains("目标：整理周报 把华东数据补齐"), "{text}");
        assert!(text.contains("产物：weekly.md、a.xlsx"), "{text}");
        assert!(text.chars().count() <= MAX_AUTO_MEMORY_CHARS);
    }

    #[test]
    fn secret_content_is_rejected() {
        assert!(looks_like_secret("token=abc"));
        assert!(looks_like_secret("Bearer abc"));
        assert!(!looks_like_secret("周报已完成"));
    }

    #[test]
    fn artifact_diff_reports_new_and_changed_only() {
        let root = std::env::temp_dir().join(format!("tt-harness-diff-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::write(root.join("a.md"), "one").unwrap();
        let before = snapshot_files(&root);
        std::fs::write(root.join("a.md"), "two-longer").unwrap();
        std::fs::write(root.join("sub/b.md"), "new").unwrap();
        let after = snapshot_files(&root);
        let diff = diff_files(&root, &before, &after);
        assert_eq!(diff.len(), 2, "{diff:?}");
        assert!(diff.iter().any(|p| p.ends_with("a.md")));
        assert!(diff.iter().any(|p| p.ends_with("b.md")));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn plan_from_meta_skips_empty_descriptions() {
        let plan = plan_from_meta(&[
            json!({ "id": "step-1", "type": "file_read", "description": "读数据" }),
            json!({ "id": "step-2", "description": "  " }),
        ]);
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].step_type, "file_read");
    }

    /// 检查点归属：第一次跑按负责人专家盖章；从盘上恢复的快照绝不重新盖章。
    #[test]
    fn agent_scope_keeps_restored_ownership() {
        assert_eq!(agent_scope_for("", &json!({})), checkpoint::DEFAULT_SCOPE);
        assert_eq!(
            agent_scope_for("", &json!({ "expertId": "Expert-A" })),
            checkpoint::leader_scope(Some("Expert-A")),
        );
        assert_eq!(
            agent_scope_for("", &json!({ "expertId": "   " })),
            checkpoint::DEFAULT_SCOPE,
            "空白专家 id 不算一个专家"
        );
        // 续跑时 runtime 是重建的（不带专家，或换了一个专家）：
        // 重新盖章会把中断前的进度搬进别人的目录，那一轮的历史就再也找不回来
        let restored = checkpoint::member_scope("李四");
        assert_eq!(
            agent_scope_for(&restored, &json!({ "expertId": "Expert-B" })),
            restored,
            "盘上带着归属就原样沿用"
        );
    }

    /// 阶段一：模型返回计划 JSON → 解析成步骤并播报 plan 事件。
    #[tokio::test]
    async fn plan_inner_parses_model_plan() {
        use std::io::{Read, Write};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 8192];
            let _ = sock.read(&mut buf);
            let body = concat!(
                "data: {\"choices\":[{\"delta\":{\"content\":",
                "\"{\\\"steps\\\":[{\\\"description\\\":\\\"读文件\\\",\\\"type\\\":\\\"file_read\\\"}]}\"",
                "}}]}\n\n",
                "data: [DONE]\n\n"
            );
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = sock.write_all(resp.as_bytes());
        });

        let root = std::env::temp_dir().join(format!("tt-harness-plan-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let slot = Slot::new(
            "task-plan",
            "读一个文件并总结",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            // 关闭自动记忆：测试不得写用户真实意识目录。
            // 档位显式设成「自动审批」：单测里绝不弹系统确认框（缺省档是每次询问）
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            // 隐私模式：计划不落盘，测试不碰用户真实检查点目录
            true,
        );
        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let result = {
            let p0 = P0Context::default();
            let em = Emit { task_id: "task-plan", p0: &p0, tx: &tx };
            plan_inner(&em, &slot).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        assert!(result.is_ok(), "{result:?}");
        let plan = slot.snapshot().plan;
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].description, "读文件");
        let plan_event = events
            .iter()
            .find(|e| e["type"] == "plan")
            .expect("缺少 plan 事件");
        assert_eq!(plan_event["steps"][0]["description"], "读文件");
        server.join().unwrap();
        std::fs::remove_dir_all(&root).ok();
    }

    /// 端到端：假 OpenAI 兼容服务端 → 首轮工具调用 → 落盘 → 次轮自然语言收尾。
    #[tokio::test]
    async fn run_loop_executes_tool_then_finishes() {
        use std::io::{Read, Write};

        let root = std::env::temp_dir().join(format!("tt-harness-loop-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let bodies = [
                concat!(
                    "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",",
                    "\"function\":{\"name\":\"write_text_file\",\"arguments\":",
                    "\"{\\\"file_path\\\":\\\"out.md\\\",\\\"content\\\":\\\"hi\\\"}\"}}]}}]}\n\n",
                    "data: [DONE]\n\n"
                ),
                "data: {\"choices\":[{\"delta\":{\"content\":\"写好了\"}}]}\n\ndata: [DONE]\n\n",
            ];
            listener.set_nonblocking(true).unwrap();
            let mut served = 0usize;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
            while served < bodies.len() && std::time::Instant::now() < deadline {
                match listener.accept() {
                    Ok((mut sock, _)) => {
                        let mut buf = [0u8; 8192];
                        let _ = sock.read(&mut buf);
                        let body = bodies[served];
                        let resp = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = sock.write_all(resp.as_bytes());
                        served += 1;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(20));
                    }
                    Err(_) => break,
                }
            }
            served
        });

        let slot = Slot::new(
            "task-loop",
            "写一个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            // 关闭自动记忆：测试不得写用户真实意识目录。
            // 档位显式设成「自动审批」：单测里绝不弹系统确认框（缺省档是每次询问）
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            false,
        );
        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-loop", p0: &p0, tx: &tx };
            run_loop(&em, &slot, false).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        match end {
            End::Done { final_text, artifacts } => {
                assert_eq!(final_text, "写好了");
                assert_eq!(artifacts.len(), 1, "{artifacts:?}");
                assert!(artifacts[0].ends_with("out.md"), "{artifacts:?}");
            }
            other => panic!("unexpected end: {other:?}"),
        }
        assert_eq!(std::fs::read_to_string(root.join("out.md")).unwrap(), "hi");
        assert!(cp.executed_side_effects >= 1);
        assert!(events.iter().any(|e| e["type"] == "step_done"));
        assert_eq!(server.join().unwrap(), 2, "两轮模型调用都应到达");
        std::fs::remove_dir_all(&root).ok();
    }

    /// D-14.1 执行点：任务进循环前就已用满单任务预算 → 一次模型调用都不发。
    #[tokio::test]
    async fn run_loop_stops_before_the_model_when_task_budget_is_spent() {
        use std::io::Read;

        let root = std::env::temp_dir().join(format!(
            "tt-harness-budget-spent-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        // 监听但故意不 serve：断言「没有请求进来」就是最好的证据
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();

        let slot = Slot::new(
            "task-budget-spent",
            "写一个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            true,
        );
        // 预算 100，已用 120：还没进循环就该停
        let gate = Arc::new(crate::task::quota::QuotaGate::new(None, 1_000_000));
        gate.set_per_task_limit(100);
        gate.add_task_usage("task-budget-spent", 120);
        let registry = Arc::new(crate::task::registry::TaskRegistry::new());
        registry.create("task-budget-spent", "default");
        registry.update_status("task-budget-spent", crate::task::status::EXECUTING, None);
        let p0 = P0Context {
            events: None,
            quota: Some(Arc::clone(&gate)),
            registry: Some(Arc::clone(&registry)),
            store: None,
            secure_env: false,
            privacy: false,
        };

        let (tx, _rx) = tokio::sync::mpsc::channel(256);
        let (end, _cp) = {
            let em = Emit {
                task_id: "task-budget-spent",
                p0: &p0,
                tx: &tx,
            };
            run_loop(&em, &slot, false).await
        };

        assert!(matches!(end, End::BudgetPaused), "{end:?}");
        assert!(
            matches!(
                listener.accept(),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock
            ),
            "预算用满后不该再发一次模型请求"
        );
        // 没发请求就没有新用量：已用仍是 120，不被「停了还继续烧」打脸
        assert_eq!(gate.task_snapshot("task-budget-spent").used_tokens, 120);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// D-14.1 执行点：本轮用量把单任务预算顶穿上限 → 状态转 budget_paused（转移 #17），
    /// 且下一轮模型调用不再发出。
    #[tokio::test]
    async fn run_loop_crossing_task_budget_pauses_and_does_not_start_next_round() {
        use std::io::{Read, Write};

        let root = std::env::temp_dir().join(format!(
            "tt-harness-budget-cross-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            // 只备一份响应：工具调用 + 顶穿上限的用量（60+60 ≥ 100）
            let body = concat!(
                "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",",
                "\"function\":{\"name\":\"write_text_file\",\"arguments\":",
                "\"{\\\"file_path\\\":\\\"b.md\\\",\\\"content\\\":\\\"第一轮写的\\\"}\"}}]}}]}\n\n",
                "data: {\"usage\":{\"prompt_tokens\":60,\"completion_tokens\":60}}\n\n",
                "data: [DONE]\n\n"
            );
            listener.set_nonblocking(true).unwrap();
            let mut served = 0usize;
            let deadline = std::time::Instant::now() + std::time::Duration::from_millis(1_500);
            while served < 1 && std::time::Instant::now() < deadline {
                match listener.accept() {
                    Ok((mut sock, _)) => {
                        let mut buf = [0u8; 8192];
                        let _ = sock.read(&mut buf);
                        let resp = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = sock.write_all(resp.as_bytes());
                        served += 1;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(20));
                    }
                    Err(_) => break,
                }
            }
            served
        });

        let slot = Slot::new(
            "task-budget-cross",
            "写一个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            false,
        );
        let gate = Arc::new(crate::task::quota::QuotaGate::new(None, 1_000_000));
        gate.set_per_task_limit(100);
        let registry = Arc::new(crate::task::registry::TaskRegistry::new());
        registry.create("task-budget-cross", "default");
        registry.update_status("task-budget-cross", crate::task::status::EXECUTING, None);
        let p0 = P0Context {
            events: None,
            quota: Some(Arc::clone(&gate)),
            registry: Some(Arc::clone(&registry)),
            store: None,
            secure_env: false,
            privacy: false,
        };

        let (tx, mut rx) = tokio::sync::mpsc::channel(256);
        let (end, cp) = {
            let em = Emit {
                task_id: "task-budget-cross",
                p0: &p0,
                tx: &tx,
            };
            run_loop(&em, &slot, false).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        assert!(matches!(end, End::BudgetPaused), "{end:?}");
        // 只发了这一轮；第二轮（收尾用的自然语言）没有发出
        assert_eq!(server.join().unwrap(), 1, "预算顶穿后不该发下一轮模型调用");
        let snap = gate.task_snapshot("task-budget-cross");
        assert_eq!(snap.used_tokens, 120, "{snap:?}");
        assert!(snap.blocked);
        // 计量点在这一轮的 usage 事件里就把任务转成了 budget_paused（转移 #17）
        let record = registry.get("task-budget-cross").unwrap();
        assert_eq!(record.status, crate::task::status::BUDGET_PAUSED);
        assert_eq!(
            record.status_reason_code.as_deref(),
            Some("BUDGET_EXHAUSTED")
        );
        // 检查点要留成「可续」：调高预算后靠它原地唤回
        assert!(cp.resumable, "被预算停下的任务必须可从检查点继续");
        // 状态与原因都在事件流里可见
        assert!(events
            .iter()
            .any(|e| e["type"] == "status_change" && e["status"] == "budget_paused"));
        assert!(events
            .iter()
            .any(|e| e["type"] == "quota_warning" && e["scope"] == "task"));
        assert!(events
            .iter()
            .any(|e| e["type"] == "step_done" && e["stepId"] == "budget-pause"));
        // 别的任务不受牵连：日额度也只按实际用量走
        assert_eq!(gate.snapshot().used_tokens, 120);
        assert!(gate.check_submit().is_ok());
        std::fs::remove_dir_all(&root).ok();
    }

    /// 检查点落盘的**数据根**：`checkpoint::home()` 读 `TONGTIANWORK_HOME`，缺省回落用户目录。
    /// 不设这一层，`run_loop` / `finish_end` 里的 `persist()` 会把自测检查点写进真实
    /// `~/.tongtianwork`——与引擎自测当年没隔离数据根、把假用量写进真实台账同一类污染。
    /// 每个用例都用同一个「按进程」的目录：并发跑也不会互相改写到别的目录上去。
    fn harness_test_home() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("tt-harness-home-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        if std::env::var_os("TONGTIANWORK_HOME").is_none() {
            std::env::set_var("TONGTIANWORK_HOME", &dir);
        }
        dir
    }

    /// 预算暂停的**收尾与唤回**整链（缺陷 D-14.1 / D-14.2 的机器可复现证据）。
    ///
    /// 此前只有 `run_loop` 的出口形状被测过：`execute` 里那段「把结局落成检查点、任务状态
    /// 与收尾事件」要 `AppHandle`，从没在测试里跑过，而它恰恰是「调高点继续」能不能落地的关键。
    /// 这条把整链接上：跨线 → `finish_end` 落盘可续 → 调高上限 → 走真实恢复入口
    /// `slot_from_checkpoint` 原地唤回 → Done → 检查点删除、按任务累计清零。
    /// 全程离线，模型端点是本机回环上的一次性假响应。
    #[tokio::test]
    async fn budget_pause_finalizes_to_disk_then_resumes_to_done_after_raising_limit() {
        let home = harness_test_home();
        let root = std::env::temp_dir().join(format!("tt-bud-finalize-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        // 第一份带工具调用与顶穿上限的用量（60+60 ≥ 100），第二份是调高后续跑的收尾回答
        const ROUND_ONE: &'static str = concat!(
            "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",",
            "\"function\":{\"name\":\"write_text_file\",\"arguments\":",
            "\"{\\\"file_path\\\":\\\"b.md\\\",\\\"content\\\":\\\"第一轮写的\\\"}\"}}]}}]}\n\n",
            "data: {\"usage\":{\"prompt_tokens\":60,\"completion_tokens\":60}}\n\n",
            "data: [DONE]\n\n"
        );
        const ROUND_TWO: &'static str =
            "data: {\"choices\":[{\"delta\":{\"content\":\"两个文件都写好了\"}}]}\n\ndata: [DONE]\n\n";
        let server = std::thread::spawn(move || pump(listener, &[ROUND_ONE, ROUND_TWO], false));

        let model_config = json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") });
        let runtime = json!({
            "awareness": { "mode": true, "autoMemory": false },
            "filePermission": "auto_approve",
        });

        let ledger_path = home.join("quota-ledger-finalize.json");
        let _ = std::fs::remove_file(&ledger_path);
        let gate = Arc::new(crate::task::quota::QuotaGate::new(
            Some(ledger_path.clone()),
            1_000_000,
        ));
        gate.set_per_task_limit(100);
        let registry = Arc::new(crate::task::registry::TaskRegistry::new());
        registry.create("task-bud-finalize", "default");
        registry.update_status("task-bud-finalize", crate::task::status::EXECUTING, None);
        let store = Arc::new(crate::store::Store::open(home.join("finalize.db")).unwrap());
        let p0 = P0Context {
            events: None,
            quota: Some(Arc::clone(&gate)),
            registry: Some(Arc::clone(&registry)),
            store: Some(store),
            secure_env: false,
            privacy: false,
        };

        // ① 顶穿上限的那一轮照常执行，下一轮在模型调用之前被拦下
        let slot = Slot::new(
            "task-bud-finalize",
            "写一个文件再汇报",
            "general",
            root.clone(),
            model_config.clone(),
            runtime.clone(),
            Vec::new(),
            Vec::new(),
            false,
        );
        let (tx, mut rx) = tokio::sync::mpsc::channel(512);
        let end = {
            let em = Emit {
                task_id: "task-bud-finalize",
                p0: &p0,
                tx: &tx,
            };
            let (end, mut cp) = run_loop(&em, &slot, false).await;
            finish_end(&em, &slot, &mut cp, end).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        assert!(matches!(end, End::BudgetPaused), "{end:?}");

        // 检查点真的落到盘上，且标成可续：这是「点继续」唯一的事实来源
        let disk = checkpoint::load("task-bud-finalize").expect("被预算停下的任务要把检查点留在盘上");
        assert!(disk.resumable, "{:?}", disk.reason);
        assert!(
            disk.reason.contains("单任务预算已用满"),
            "检查点原因要写明是被预算停的：{}",
            disk.reason
        );
        assert!(!disk.messages.is_empty(), "可续就得带着对话快照");

        // 台账按任务累计落盘（界面与恢复都读这一份）
        let raw = std::fs::read_to_string(&ledger_path).unwrap();
        let book: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(book["perTaskLimitTokens"], 100, "{book}");
        assert!(
            book["tasks"]["task-bud-finalize"]["usedTokens"].as_u64().unwrap_or(0) >= 100,
            "{book}"
        );

        // 状态与原因在界面可见，收尾事件带着可续的检查点
        let record = registry.get("task-bud-finalize").unwrap();
        assert_eq!(record.status, crate::task::status::BUDGET_PAUSED);
        assert!(events.iter().any(|e| e["type"] == "status_change"
            && e["status"] == "budget_paused"));
        let note = events
            .iter()
            .find(|e| e["type"] == "step_done" && e["stepId"] == "budget-pause")
            .expect("预算拦停要给一条看得见的中文说明");
        assert!(
            note["output"].as_str().unwrap_or_default().contains("调高"),
            "说明要写清怎么唤回：{note}"
        );
        let cp_event = events
            .iter()
            .find(|e| e["type"] == "checkpoint")
            .expect("收尾要把检查点状态报给界面");
        assert_eq!(cp_event["resumable"], json!(true), "{cp_event}");
        assert!(events.iter().any(|e| e["type"] == "done"));
        // 这一轮的工具真的执行了
        assert_eq!(
            std::fs::read_to_string(root.join("b.md")).unwrap(),
            "第一轮写的"
        );

        // ② 调高上限（保存即生效，但**不**自动唤回）→ 走真实恢复入口原地续跑到完成
        gate.set_per_task_limit(1_000);
        assert!(!gate.task_budget_blocked("task-bud-finalize"), "调高后拦停判据要松开");
        let restored = slot_from_checkpoint(
            "task-bud-finalize",
            model_config.clone(),
            runtime.clone(),
            false,
        )
        .expect("检查点要能被恢复入口读回来");
        let (tx2, mut rx2) = tokio::sync::mpsc::channel(512);
        let end2 = {
            let em2 = Emit {
                task_id: "task-bud-finalize",
                p0: &p0,
                tx: &tx2,
            };
            let (end2, mut cp2) = run_loop(&em2, &restored, true).await;
            finish_end(&em2, &restored, &mut cp2, end2).await
        };
        drop(tx2);
        let mut events2 = Vec::new();
        while let Some(ev) = rx2.recv().await {
            events2.push(ev);
        }
        match &end2 {
            End::Done { final_text, .. } => assert!(final_text.contains("写好了"), "{final_text}"),
            other => panic!("唤回后应当跑到完成：{other:?}"),
        }
        assert!(events2.iter().any(|e| e["type"] == "deliver"));
        // 正常收尾：检查点删掉、按任务累计清零（否则同名任务重提交会白拿上一轮的计数）
        assert!(checkpoint::load("task-bud-finalize").is_none(), "完成后不该留检查点");
        let book2: Value = serde_json::from_str(&std::fs::read_to_string(&ledger_path).unwrap()).unwrap();
        assert!(
            book2["tasks"]
                .get("task-bud-finalize")
                .is_none_or(Value::is_null),
            "终态要清掉本任务的累计：{book2}"
        );
        // 两次模型调用：顶穿上限的那一轮 + 唤回后的收尾轮，中间那一轮没发出去
        assert_eq!(server.join().unwrap(), 2, "拦停期间不该有第三次模型调用");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 真模型跑满单任务预算（D-14.1 / D-14.2 的真机验收，台账里的 D-14.3）。
    ///
    /// 与上一条同一条链路，唯一区别是模型换成真服务：计入台账的是服务端**真返回**的
    /// `usage.prompt_tokens + completion_tokens`，不是假端点回报的固定数，也不是字节折算。
    ///
    /// 默认不跑（`#[ignore]`）。Key 只从环境变量进进程，不落进任何文件、任何打印：
    /// `TT_API_KEY=sk-xxx bash scripts/run-rust-tests.sh harness::runner -- --ignored --nocapture`
    ///
    /// 口径（报告里要照抄）：
    /// - 没设 `TT_API_KEY` 就打印一行 skipped 直接返回，不算失败也不算通过；
    /// - `model_config` 传 `null` → `model::endpoints` 回落到环境变量（`TT_PROVIDER` 缺省
    ///   dashscope、`TT_MODEL` 缺省 qwen-plus、`TT_BASE_URL` 缺省百炼兼容地址）。这样 Key
    ///   既不进 slot、也不进检查点，用例末尾再把落盘的检查点序列化出来显式断言不含 Key；
    /// - 上限故意设得很小（缺省 50 tokens，`TT_TASK_BUDGET_TOKENS` 可覆盖）：真实第一轮带着
    ///   系统提示 + 工具清单，必然跨线。拦停点因此与「模型这轮答得好不好」无关，
    ///   测的是执行面判据本身；
    /// - 检查点与台账都写在临时数据根，不碰用户真实 `~/.tongtianwork`；
    /// - 唤回后把上限抬到 +200_000，`maxIters` 6 轮，够真模型把「写文件 → 读回 → 汇报」走完。
    ///   轮次用满时 `run_loop` 也会返回 Done（正文换成「已达到单轮工具循环上限」），
    ///   所以证据 JSON 里单独记 `hitIterCap`，别把「跑到了 Done」当成「任务真做完」。
    #[tokio::test]
    #[ignore]
    async fn real_model_task_budget_pause_then_resume_to_done() {
        let key = std::env::var("TT_API_KEY").unwrap_or_default();
        if key.trim().is_empty() {
            println!("REALMODEL_JSON {{\"skipped\":\"TT_API_KEY 未设置\"}}");
            return;
        }
        let model = std::env::var("TT_MODEL").unwrap_or_else(|_| "qwen-plus".to_string());
        let limit: u64 = std::env::var("TT_TASK_BUDGET_TOKENS")
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .filter(|n| *n > 0)
            .unwrap_or(50);

        let home = harness_test_home();
        let root = std::env::temp_dir().join(format!("tt-real-budget-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let task_id = "task-real-budget";
        let ledger_path = home.join("quota-ledger-realmodel.json");
        let _ = std::fs::remove_file(&ledger_path);
        let gate = Arc::new(crate::task::quota::QuotaGate::new(
            Some(ledger_path.clone()),
            1_000_000,
        ));
        gate.set_per_task_limit(limit);
        let registry = Arc::new(crate::task::registry::TaskRegistry::new());
        registry.create(task_id, "default");
        registry.update_status(task_id, crate::task::status::EXECUTING, None);
        let store = Arc::new(crate::store::Store::open(home.join("realmodel.db")).unwrap());
        let p0 = P0Context {
            events: None,
            quota: Some(Arc::clone(&gate)),
            registry: Some(Arc::clone(&registry)),
            store: Some(Arc::clone(&store)),
            secure_env: false,
            privacy: false,
        };

        // 至少要两轮才答得完的任务：写盘 → 读回 → 汇报
        let goal = "在 real-budget.md 里写入「真机验收」四个字，然后读回这个文件，最后用一句话汇报它的内容";
        let runtime = json!({
            "awareness": { "mode": true, "autoMemory": false },
            "filePermission": "auto_approve",
            "agentPolicy": { "maxIters": 6, "autoCompress": false },
        });
        let model_config = Value::Null;

        // ① 真实模型跑第一轮 → 真实用量跨线上限 → 下一轮在调用之前被拦下
        let slot = Slot::new(
            task_id,
            goal,
            "general",
            root.clone(),
            model_config.clone(),
            runtime.clone(),
            Vec::new(),
            Vec::new(),
            false,
        );
        let (tx, mut rx) = tokio::sync::mpsc::channel(4096);
        let pause_wall = std::time::Instant::now();
        let end = {
            let em = Emit {
                task_id,
                p0: &p0,
                tx: &tx,
            };
            let (end, mut cp) = run_loop(&em, &slot, false).await;
            finish_end(&em, &slot, &mut cp, end).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        let pause_ms = pause_wall.elapsed().as_millis() as u64;

        let book: Value =
            serde_json::from_str(&std::fs::read_to_string(&ledger_path).unwrap()).unwrap();
        let used_at_pause = book["tasks"][task_id]["usedTokens"].as_u64().unwrap_or(0);
        assert!(
            used_at_pause > 0,
            "真模型的 usage 要真的进台账（不是折算、不是假端点回报）：{book}"
        );
        assert!(
            matches!(end, End::BudgetPaused),
            "真实用量把上限跑满后应当转 BudgetPaused：{end:?}（已用 {used_at_pause}，上限 {limit}）"
        );
        assert!(used_at_pause >= limit, "{book}");

        let disk = checkpoint::load(task_id).expect("被预算停下要把检查点留在盘上");
        assert!(disk.resumable, "{:?}", disk.reason);
        assert!(disk.reason.contains("单任务预算已用满"), "{}", disk.reason);
        assert!(!disk.messages.is_empty(), "可续就得带着对话快照");
        let rendered_cp = serde_json::to_string(&disk).unwrap();
        assert!(
            !rendered_cp.contains(key.trim()),
            "API Key 绝不允许落进检查点"
        );
        assert_eq!(
            registry.get(task_id).unwrap().status,
            crate::task::status::BUDGET_PAUSED
        );
        let note = events
            .iter()
            .find(|e| e["type"] == "step_done" && e["stepId"] == "budget-pause")
            .expect("预算拦停要有看得见的中文说明");
        assert!(
            note["output"].as_str().unwrap_or_default().contains("调高"),
            "{note}"
        );

        // ② 调高上限 → 真实恢复入口原地唤回 → 跑到 Done
        let raised = limit + 200_000;
        gate.set_per_task_limit(raised);
        assert!(!gate.task_budget_blocked(task_id), "调高后拦停判据要松开");
        let restored = slot_from_checkpoint(task_id, model_config.clone(), runtime.clone(), false)
            .expect("检查点要能被恢复入口读回来");
        let (tx2, mut rx2) = tokio::sync::mpsc::channel(4096);
        let resume_wall = std::time::Instant::now();
        let end2 = {
            let em2 = Emit {
                task_id,
                p0: &p0,
                tx: &tx2,
            };
            let (end2, mut cp2) = run_loop(&em2, &restored, true).await;
            finish_end(&em2, &restored, &mut cp2, end2).await
        };
        drop(tx2);
        let mut events2 = Vec::new();
        while let Some(ev) = rx2.recv().await {
            events2.push(ev);
        }
        let resume_ms = resume_wall.elapsed().as_millis() as u64;
        let final_text = match &end2 {
            End::Done { final_text, .. } => final_text.clone(),
            other => panic!("调高上限后唤回应当跑到完成：{other:?}"),
        };
        let hit_iter_cap = final_text.contains("已达到单轮工具循环上限");
        assert!(
            events2.iter().any(|e| e["type"] == "deliver"),
            "收尾要投递正文"
        );
        assert!(checkpoint::load(task_id).is_none(), "终态不该留检查点");
        let book2: Value =
            serde_json::from_str(&std::fs::read_to_string(&ledger_path).unwrap()).unwrap();
        assert!(
            book2["tasks"].get(task_id).is_none_or(Value::is_null),
            "终态要清掉本任务的累计：{book2}"
        );
        let used_total = book2["usedTokens"].as_u64().unwrap_or(0);
        let wrote_file = std::fs::read_to_string(root.join("real-budget.md"))
            .unwrap_or_default();

        // 一行证据，供台账/报告直接引用。**不含 Key。**
        println!(
            "REALMODEL_JSON {}",
            json!({
                "provider": std::env::var("TT_PROVIDER").unwrap_or_else(|_| "dashscope".to_string()),
                "model": model,
                "channel": "harness",
                "perTaskLimitTokens": limit,
                "usedTokensAtPause": used_at_pause,
                "statusAfterPause": registry.get(task_id).map(|r| r.status.clone()),
                "checkpointResumable": disk.resumable,
                "pauseWallMs": pause_ms,
                "raisedLimitTokens": raised,
                "resumeWallMs": resume_ms,
                "resumedTo": "done",
                "hitIterCap": hit_iter_cap,
                "finalTextChars": final_text.chars().count(),
                "finalTextHead": final_text.chars().take(80).collect::<String>(),
                "writtenFileChars": wrote_file.chars().count(),
                "usedTokensTotal": used_total,
                "keyOnDisk": false,
                "events": events.len() + events2.len(),
            })
        );
        std::fs::remove_dir_all(&root).ok();
    }

    /// 续跑：中断前已成功执行的写文件工具，恢复后按签名跳过，不重复落盘。
    #[tokio::test]
    async fn run_loop_resume_skips_previous_side_effect() {
        use std::io::{Read, Write};

        let root = std::env::temp_dir().join(format!("tt-harness-resume-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("out.md"), "旧内容").unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let bodies = [
                concat!(
                    "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",",
                    "\"function\":{\"name\":\"write_text_file\",\"arguments\":",
                    "\"{\\\"file_path\\\":\\\"out.md\\\",\\\"content\\\":\\\"续写内容\\\"}\"}}]}}]}\n\n",
                    "data: [DONE]\n\n"
                ),
                "data: {\"choices\":[{\"delta\":{\"content\":\"续跑完成\"}}]}\n\ndata: [DONE]\n\n",
            ];
            listener.set_nonblocking(true).unwrap();
            let mut served = 0usize;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
            while served < bodies.len() && std::time::Instant::now() < deadline {
                match listener.accept() {
                    Ok((mut sock, _)) => {
                        let mut buf = [0u8; 8192];
                        let _ = sock.read(&mut buf);
                        let body = bodies[served];
                        let resp = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = sock.write_all(resp.as_bytes());
                        served += 1;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(20));
                    }
                    Err(_) => break,
                }
            }
            served
        });

        let slot = Slot::new(
            "task-resume",
            "改一个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            // 关闭自动记忆：测试不得写用户真实意识目录。
            // 档位显式设成「自动审批」：单测里绝不弹系统确认框（缺省档是每次询问）
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            true,
        );
        let args = json!({ "file_path": "out.md", "content": "续写内容" });
        slot.update(|inner| {
            let cp = &mut inner.checkpoint;
            cp.messages = vec![
                json!({ "role": "system", "content": "sys" }),
                json!({ "role": "user", "content": "改一个文件" }),
            ];
            cp.run_id = 1;
            cp.resumable = true;
            cp.record_tool(
                "write_text_file",
                &checkpoint::signature_of("write_text_file", &args),
                "已写入（中断前）",
            );
        });

        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-resume", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        match end {
            End::Done { final_text, .. } => assert_eq!(final_text, "续跑完成"),
            other => panic!("unexpected end: {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(root.join("out.md")).unwrap(),
            "旧内容",
            "续跑不得重复执行已成功的写操作"
        );
        assert_eq!(cp.skipped_duplicates, 1);
        assert_eq!(cp.executed_side_effects, 1, "不应产生新的副作用记录");
        let skipped = events
            .iter()
            .find(|e| e["type"] == "step_done" && e["output"].as_str().unwrap_or("").contains("断点续跑"))
            .expect("缺少跳过重复执行的提示");
        assert!(skipped["output"].as_str().unwrap().contains("write_text_file"));
        assert!(events.iter().any(|e| e["output"].as_str().is_some_and(|s| s.contains("已从本地检查点恢复"))));
        assert_eq!(server.join().unwrap(), 2, "恢复后仍应有两次模型调用");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 中断发生在一轮工具调用的中间（第一个已写盘、第二个停在确认框上）：
    /// 恢复时直接把第二个执行掉，绝不重问模型——已拿到的结果原样沿用。
    #[tokio::test]
    async fn run_loop_resume_continues_pending_tool() {
        let root = std::env::temp_dir().join(format!("tt-harness-turn-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        // 中断前 c1 已经写好的文件：续跑不能再碰它
        std::fs::write(root.join("a.md"), "中断前写的").unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        // 只准备一次应答：真发生「重问模型」就会多要一次调用，这里数得出来
        let body = "data: {\"choices\":[{\"delta\":{\"content\":\"两个文件都写好了\"}}]}\n\ndata: [DONE]\n\n";
        let server = std::thread::spawn(move || pump(listener, &[body], false));

        let args_a = json!({ "file_path": "a.md", "content": "不该再写一遍" });
        let args_b = json!({ "file_path": "b.md", "content": "续跑写的" });
        let calls = vec![
            checkpoint::TurnCall {
                id: "c1".into(),
                tool: "write_text_file".into(),
                arguments: args_a.to_string(),
                signature: checkpoint::signature_of("write_text_file", &args_a),
            },
            checkpoint::TurnCall {
                id: "c2".into(),
                tool: "write_text_file".into(),
                arguments: args_b.to_string(),
                signature: checkpoint::signature_of("write_text_file", &args_b),
            },
        ];
        let listed: Vec<Value> = calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.tool, "arguments": c.arguments },
                })
            })
            .collect();

        let slot = Slot::new(
            "task-turn",
            "写两个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            // 单测里不弹系统确认框：档位显式设成自动审批（缺省是每次询问）
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            false,
        );
        slot.update(|inner| {
            let cp = &mut inner.checkpoint;
            cp.messages = vec![
                json!({ "role": "system", "content": "sys" }),
                json!({ "role": "user", "content": "写两个文件" }),
                json!({ "role": "assistant", "content": "", "tool_calls": listed }),
                json!({ "role": "tool", "tool_call_id": "c1", "content": "已写入 a.md（中断前）" }),
            ];
            cp.run_id = 1;
            cp.resumable = true;
            cp.record_tool("write_text_file", &calls[0].signature, "已写入 a.md（中断前）");
            cp.begin_turn(calls.clone());
            cp.baseline_files = snapshot_files(&root);
        });

        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-turn", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        match end {
            End::Done { final_text, .. } => assert_eq!(final_text, "两个文件都写好了"),
            other => panic!("unexpected end: {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(root.join("b.md")).unwrap(),
            "续跑写的",
            "欠着的那个工具必须补上"
        );
        assert_eq!(
            std::fs::read_to_string(root.join("a.md")).unwrap(),
            "中断前写的",
            "已有结果的那个不能重放"
        );
        assert_eq!(server.join().unwrap(), 1, "原地续跑不该再问一次模型");
        assert!(cp.pending_turn.is_none(), "整轮配齐后标记必须清掉");
        assert_eq!(cp.executed_side_effects, 2, "只多这一次写盘");
        assert_eq!(cp.model_calls, 1, "恢复后只发生了收尾那一次调用");
        let note = events
            .iter()
            .find(|e| e["stepId"] == "harness-resume-turn" && e["type"] == "step_done")
            .expect("缺少「从待审批工具继续」的说明");
        let text = note["output"].as_str().unwrap();
        assert!(text.contains("write_text_file"), "{text}");
        assert!(text.contains("原样沿用"), "{text}");
        // 补上的结果进了消息，配对完整才敢交给模型
        assert!(cp
            .messages
            .iter()
            .any(|m| m["tool_call_id"].as_str() == Some("c2")));
        std::fs::remove_dir_all(&root).ok();
    }

    /// 老检查点（没有工具轮次标记）里留着半截工具调用：恢复时先清理再重问模型，
    /// 已成功的写盘仍按签名跳过——不能把不配对的会话直接交给模型换一次 400。
    #[tokio::test]
    async fn run_loop_resume_rewinds_dangling_tool_calls() {
        let root = std::env::temp_dir().join(format!("tt-harness-rewind-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("a.md"), "中断前写的").unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let bodies = [
            concat!(
                "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"cX\",",
                "\"function\":{\"name\":\"write_text_file\",\"arguments\":",
                "\"{\\\"file_path\\\":\\\"a.md\\\",\\\"content\\\":\\\"不该再写一遍\\\"}\"}}]}}]}\n\n",
                "data: [DONE]\n\n"
            ),
            "data: {\"choices\":[{\"delta\":{\"content\":\"续跑完成\"}}]}\n\ndata: [DONE]\n\n",
        ];
        let server = std::thread::spawn(move || pump(listener, &bodies, false));

        let args = json!({ "file_path": "a.md", "content": "不该再写一遍" });
        let slot = Slot::new(
            "task-rewind",
            "改一个文件",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1") }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve"
            }),
            Vec::new(),
            Vec::new(),
            false,
        );
        slot.update(|inner| {
            let cp = &mut inner.checkpoint;
            cp.messages = vec![
                json!({ "role": "system", "content": "sys" }),
                json!({ "role": "user", "content": "改一个文件" }),
                json!({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        { "id": "cX", "type": "function",
                          "function": { "name": "write_text_file", "arguments": args.to_string() } }
                    ]
                }),
                // 没有对应的 tool 结果：这条就是半截轮次
            ];
            cp.run_id = 1;
            cp.resumable = true;
            cp.record_tool("write_text_file", &checkpoint::signature_of("write_text_file", &args), "已写入");
            cp.baseline_files = snapshot_files(&root);
        });

        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-rewind", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        match end {
            End::Done { final_text, .. } => assert_eq!(final_text, "续跑完成"),
            other => panic!("unexpected end: {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(root.join("a.md")).unwrap(),
            "中断前写的",
            "重问模型后仍要按签名跳过已成功的写盘"
        );
        assert_eq!(cp.skipped_duplicates, 1);
        assert!(checkpoint::unpaired_tail(&cp.messages).is_none(), "交给消息的历史必须配对");
        let note = events
            .iter()
            .find(|e| e["stepId"] == "harness-resume-turn" && e["type"] == "step_done")
            .expect("缺少清理半截工具调用的说明");
        assert!(note["output"].as_str().unwrap().contains("没配齐"), "{note}");
        assert_eq!(server.join().unwrap(), 2, "清理后重问一次模型 + 一次收尾");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 上下文压缩：超出阈值时把更早的消息并入一条摘要，工具配对不拆散，任务照常收尾。
    #[tokio::test]
    async fn run_loop_compresses_older_messages() {
        let root = std::env::temp_dir().join(format!("tt-harness-compact-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let bodies = [
                // 1) 压缩摘要（oneshot，不带 tools）
                "data: {\"choices\":[{\"delta\":{\"content\":\"已读过数据并核对字段，下一步写文件\"}}]}\n\ndata: [DONE]\n\n",
                // 2) 主调用：直接收尾（带 usage，作为压缩后的新计数基准）
                "data: {\"choices\":[{\"delta\":{\"content\":\"压缩后完成\"}}]}\n\ndata: {\"usage\":{\"prompt_tokens\":2600,\"completion_tokens\":20}}\n\ndata: [DONE]\n\n",
            ];
            serve_sse(listener, &bodies)
        });

        let big = "字".repeat(600);
        let mut cp = checkpoint::Checkpoint::new("task-compact");
        cp.messages = vec![json!({ "role": "system", "content": "sys" }),
                           json!({ "role": "user", "content": "继续任务" })];
        for i in 0..8 {
            cp.messages.push(json!({ "role": "user", "content": format!("轮次{big}{i}") }));
            cp.messages.push(json!({ "role": "assistant", "content": format!("好的{big}{i}") }));
        }
        cp.resumable = true;
        cp.last_prompt_tokens = 6000;

        let slot = Slot::new(
            "task-compact",
            "继续任务",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1"), "contextWindow": 8192 }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve",
                "agentPolicy": { "maxIters": 2, "autoCompress": true }
            }),
            Vec::new(),
            Vec::new(),
            true,
        );
        slot.update(|inner| inner.checkpoint = cp);

        let (tx, mut rx) = tokio::sync::mpsc::channel(128);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-compact", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        assert!(matches!(end, End::Done { .. }), "{end:?}");
        assert!(cp.compressed_messages >= 10, "最早的整段应被并入摘要：{}", cp.compressed_messages);
        assert_eq!(cp.last_prompt_tokens, 2600, "压缩后以新调用回报的计数为准");
        let body = cp.messages.iter().filter_map(|m| m.get("content")).filter_map(Value::as_str).collect::<Vec<_>>().join("\n");
        assert!(body.contains(compress::SUMMARY_MARK), "检查点快照应存压缩后的上下文：{body}");
        assert!(cp.messages.len() < 18, "{:?}", cp.messages.len());
        let noted = events
            .iter()
            .find(|e| {
                e["type"] == "step_done"
                    && e["stepId"].as_str().unwrap_or("") == "ctx-compress"
            })
            .expect("缺少压缩口径说明");
        let text = noted["output"].as_str().unwrap();
        assert!(text.contains("4096") && text.contains("保留最近 3 组"), "{text}");
        assert_eq!(server.join().unwrap(), 2, "摘要一次 + 主调用一次都应到达");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 摘要模型不可用时：原文不动、如实说明原因，任务不中断。
    #[tokio::test]
    async fn run_loop_compression_failure_keeps_task_alive() {
        let root = std::env::temp_dir().join(format!("tt-harness-cfail-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let bodies = [
                // 1) 摘要调用被拒（非 2xx）
                "",
                // 2) 主调用正常返回
                "data: {\"choices\":[{\"delta\":{\"content\":\"照常完成\"}}]}\n\ndata: [DONE]\n\n",
            ];
            serve_sse_or_error(listener, &bodies)
        });

        let big = "长".repeat(600);
        let mut cp = checkpoint::Checkpoint::new("task-cfail");
        cp.messages = vec![json!({ "role": "system", "content": "sys" }),
                           json!({ "role": "user", "content": "继续任务" })];
        for i in 0..4 {
            cp.messages.push(json!({ "role": "user", "content": format!("u{big}{i}") }));
            cp.messages.push(json!({ "role": "assistant", "content": format!("a{big}{i}") }));
        }
        cp.resumable = true;
        // 窗口 12000 → 阈值 12000−4096=7904；端点回报 9000 已越过，才会走摘要分支
        cp.last_prompt_tokens = 9000;

        let slot = Slot::new(
            "task-cfail",
            "继续任务",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1"), "contextWindow": 12000 }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve",
                "agentPolicy": { "maxIters": 2 }
            }),
            Vec::new(),
            Vec::new(),
            true,
        );
        slot.update(|inner| inner.checkpoint = cp);

        let (tx, mut rx) = tokio::sync::mpsc::channel(128);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-cfail", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        match &end {
            End::Done { final_text, .. } => assert_eq!(final_text, "照常完成"),
            other => panic!("unexpected end: {other:?}"),
        }
        assert_eq!(cp.compressed_messages, 0, "摘要失败不得改动上下文");
        assert!(events
            .iter()
            .any(|e| e["type"] == "step_done"
                && e["stepId"].as_str().unwrap_or("") == "ctx-compress"
                && e["output"].as_str().unwrap_or("").contains("未能完成压缩")),
            "缺少压缩失败说明");
        assert_eq!(server.join().unwrap(), 2);
        std::fs::remove_dir_all(&root).ok();
    }

    /// 开关关闭：不压，但设置口径要可见（与引擎 notes 同一职责）。
    #[tokio::test]
    async fn run_loop_compression_off_is_silent_but_reported() {
        let root = std::env::temp_dir().join(format!("tt-harness-coff-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let bodies = [
                "data: {\"choices\":[{\"delta\":{\"content\":\"完成\"}}]}\n\ndata: [DONE]\n\n",
            ];
            serve_sse(listener, &bodies)
        });

        let big = "内".repeat(600);
        let mut cp = checkpoint::Checkpoint::new("task-coff");
        cp.messages = vec![json!({ "role": "system", "content": "sys" }),
                           json!({ "role": "user", "content": "继续任务" })];
        for i in 0..4 {
            cp.messages.push(json!({ "role": "user", "content": format!("u{big}{i}") }));
            cp.messages.push(json!({ "role": "assistant", "content": format!("a{big}{i}") }));
        }
        cp.resumable = true;
        cp.last_prompt_tokens = 6000;

        let slot = Slot::new(
            "task-coff",
            "继续任务",
            "general",
            root.clone(),
            json!({ "modelName": "fake", "baseUrl": format!("http://{addr}/v1"), "contextWindow": 8192 }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve",
                "agentPolicy": { "maxIters": 1, "autoCompress": false }
            }),
            Vec::new(),
            Vec::new(),
            true,
        );
        slot.update(|inner| inner.checkpoint = cp);

        let (tx, mut rx) = tokio::sync::mpsc::channel(128);
        let p0 = P0Context::default();
        let (end, cp) = {
            let em = Emit { task_id: "task-coff", p0: &p0, tx: &tx };
            run_loop(&em, &slot, true).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }

        assert!(matches!(end, End::Done { .. }), "{end:?}");
        assert_eq!(cp.compressed_messages, 0);
        assert!(events.iter().any(|e| e["type"] == "step_done"
            && e["stepId"].as_str().unwrap_or("") == "ctx-compress-policy"
            && e["output"].as_str().unwrap_or("").contains("上下文压缩已关闭")));
        assert!(!events.iter().any(|e| e["type"] == "step_done"
            && e["stepId"].as_str().unwrap_or("") == "ctx-compress"));
        assert_eq!(server.join().unwrap(), 1, "关闭后不应有摘要调用");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 端到端：线程内换了模型。此前轮次的对话必须原样带上并按新端点重新编码，
    /// 被改写/没带上的部分要在过程里说清——不能「换了模型就失忆」，也不能静默丢内容。
    #[tokio::test]
    async fn run_loop_model_switch_keeps_history() {
        let root = std::env::temp_dir().join(format!("tt-harness-switch-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let ok = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"收到上文\"}}]}\n\n",
            "data: [DONE]\n\n",
        );
        let server = std::thread::spawn(move || pump_scripted(listener, &[(200, ok)]));

        let long = "长".repeat(700);
        let slot = Slot::new(
            "task-switch",
            "接着上次说",
            "general",
            root.clone(),
            json!({
                "modelId": "m-new",
                "displayName": "新模型",
                "modelName": "fake-new",
                "baseUrl": format!("http://{addr}/v1"),
                "contextWindow": 32768,
            }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve",
                "agentPolicy": { "autoCompress": false }
            }),
            vec![
                json!({ "role": "user", "content": "旧会话内容" }),
                json!({
                    "role": "assistant",
                    "content": [
                        { "type": "text", "text": "图里的标题" },
                        { "type": "image_url", "image_url": { "url": "data:image/png;base64,AAA" } },
                    ]
                }),
                json!({ "role": "user", "content": long }),
            ],
            Vec::new(),
            // 隐私模式：本测不写检查点
            true,
        );
        slot.set_previous_model(json!({
            "modelId": "m-old",
            "displayName": "旧模型",
            "modelName": "fake-old",
            "contextWindow": 1_000_000,
        }));

        let (tx, mut rx) = tokio::sync::mpsc::channel(128);
        let p0 = P0Context::default();
        let (end, _cp) = {
            let em = Emit { task_id: "task-switch", p0: &p0, tx: &tx };
            run_loop(&em, &slot, false).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        let (served, requests) = server.join().unwrap();
        assert_eq!(served, 1, "{end:?}");
        assert!(matches!(end, End::Done { .. }), "{end:?}");

        // 换模型这件事必须写在过程里，并报出窗口变化
        let note = events
            .iter()
            .find(|e| e["type"] == "step_done" && e["stepId"].as_str() == Some("harness-model-switch"))
            .expect("换模型要在过程里说明");
        let text = note["output"].as_str().unwrap_or("");
        assert!(text.contains("旧模型") && text.contains("新模型"), "{text}");
        assert!(text.contains("不会因换模型丢失"), "{text}");
        assert!(text.contains("1,000,000 → 32,768"), "{text}");

        // 被改写/没带上的部分逐条摊出来
        let outputs: Vec<&str> = events
            .iter()
            .filter(|e| e["type"] == "step_done")
            .map(|e| e["output"].as_str().unwrap_or(""))
            .collect();
        assert!(
            outputs.iter().any(|o| o.contains("分段消息") && o.contains("1 段图片")),
            "图片分段要如实说明：{outputs:?}"
        );
        assert!(
            outputs.iter().any(|o| o.contains("单条 600 字")),
            "超长的历史要说清是截断带上：{outputs:?}"
        );

        // 实际发出去的请求：文字全在，图片不发，超长的按引擎口径截断
        let sent = requests.concat();
        assert!(sent.contains("旧会话内容"), "换模型不能丢历史：{sent}");
        assert!(sent.contains("图里的标题"), "分段消息只取文字，文字仍要发出");
        assert!(!sent.contains("image_url"), "本地 harness 不发图片");
        assert!(sent.contains("…（已截断）"), "单条 600 字的口径与引擎一致");
        std::fs::remove_dir_all(&root).ok();
    }

    /// 端到端：端点整链拒收工具 → 退回纯文本，但过程与正文都要说清「本轮没写过文件」。
    /// 模型在纯文本模式下最擅长声称「已为你生成 report.md」，这句话只能由运行时来纠正。
    #[tokio::test]
    async fn run_loop_tools_rejected_admits_no_file_written() {
        let root = std::env::temp_dir().join(format!("tt-harness-degraded-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let ok = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"已为你生成 report.md\"}}]}\n\n",
            "data: [DONE]\n\n",
        );
        let replies: [(u16, &'static str); 2] = [
            (
                400,
                "{\"error\":{\"message\":\"This model does not support tools\"}}",
            ),
            (200, ok),
        ];
        let server = std::thread::spawn(move || pump_scripted(listener, &replies));

        let slot = Slot::new(
            "task-degraded",
            "写一个 report.md",
            "general",
            root.clone(),
            json!({
                "modelId": "m-chat",
                "displayName": "只聊天模型",
                "modelName": "fake",
                "baseUrl": format!("http://{addr}/v1"),
            }),
            json!({
                "awareness": { "mode": true, "autoMemory": false },
                "filePermission": "auto_approve",
                "agentPolicy": { "autoCompress": false }
            }),
            Vec::new(),
            Vec::new(),
            true,
        );

        let (tx, mut rx) = tokio::sync::mpsc::channel(128);
        let p0 = P0Context::default();
        let (end, _cp) = {
            let em = Emit { task_id: "task-degraded", p0: &p0, tx: &tx };
            run_loop(&em, &slot, false).await
        };
        drop(tx);
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        let (served, requests) = server.join().unwrap();
        assert_eq!(served, 2, "带工具被拒后还应退纯文本再试一次");

        let degraded = events
            .iter()
            .find(|e| e["type"] == "step_done" && e["stepId"].as_str() == Some("harness-tools-degraded"))
            .expect("退纯文本这件事必须在过程里可见");
        let why = degraded["output"].as_str().unwrap_or("");
        assert!(why.contains("不接受本轮的工具清单"), "{why}");
        assert!(why.contains("does not support tools"), "原因要引端点原话：{why}");

        match &end {
            End::Done { final_text, .. } => {
                assert!(final_text.contains("已为你生成 report.md"), "回答本身不吞掉");
                assert!(
                    final_text.contains("本轮没有执行任何文件读写"),
                    "正文末尾要说清没有落盘：{final_text}"
                );
            }
            other => panic!("unexpected end: {other:?}"),
        }
        assert!(
            !root.join("report.md").exists(),
            "纯文本回答不许真的写出文件"
        );
        assert!(requests[0].contains("\"tools\""), "第一次请求带着工具清单");
        assert!(!requests[1].contains("\"tools\""), "退纯文本时不能再带被拒的字段");
        std::fs::remove_dir_all(&root).ok();
    }

    // ── 长任务基准（D-40 第 6 项，默认不跑：`scripts/run-rust-tests.sh -- --ignored --nocapture`）──

    /// 假端点在一次基准里量到的东西（全部来自实际发生的请求，不做二次加工）。
    #[derive(Default)]
    struct BenchRun {
        /// 带工具清单的调用次数（= 任务轮次）
        task_calls: usize,
        /// 压缩摘要调用次数（不带 tools，不占任务轮次）
        summary_calls: usize,
        /// 模型给出工具调用的次数
        tool_calls: usize,
        /// 收尾次数（= 跑了几段）
        segments_done: usize,
        /// 每次调用实际发出的请求体字节数
        bytes: Vec<u64>,
        /// 相邻两次请求到达服务端的间隔（毫秒）：含端点留的 ttft，也含上一轮的
        /// 尾包读取、工具执行与下一次请求的构造——一轮的真实节奏就在这个数里
        gaps_ms: Vec<u64>,
        /// 最后一次请求里的完整对话：用来把上一段接进下一段
        last_messages: Vec<Value>,
    }

    /// 基准用的假端点：按脚本一轮轮回答，并把「实际发出去的东西」量出来。
    ///
    /// - 带 `tools` 的请求算一轮任务调用：未到上限时回工具调用，到上限回正文收尾；
    /// - 不带 `tools` 的是压缩摘要调用，单独计数，不占任务轮次；
    /// - `prompt_tokens` = 实际请求体字节数 ÷ 4 向上取整。这是折算口径，不是真实分词，
    ///   但两条链路连的是同一个端点、用的是同一条公式，因此可比；
    /// - 响应分三段写：先只发响应头，隔 `ttft_ms` 发第一个内容增量，再隔 `stream_ms`
    ///   发用量包和 `[DONE]`。于是客户端计到的首包 ≈ `ttft_ms` + 自己那套流式处理的开销，
    ///   整包 ≈ `ttft_ms + stream_ms` + 同一份开销——两条链路连同一个端点，差的就是执行面。
    fn pump_bench(
        listener: std::net::TcpListener,
        rounds_per_segment: usize,
        segments: usize,
        body_chars: usize,
        ttft_ms: u64,
        stream_ms: u64,
        budget: std::time::Duration,
    ) -> BenchRun {
        use std::io::Write;

        listener.set_nonblocking(true).unwrap();
        let mut run = BenchRun::default();
        let deadline = std::time::Instant::now() + budget;
        let mut round = 0usize;
        let mut prev: Option<std::time::Instant> = None;
        while run.segments_done < segments && std::time::Instant::now() < deadline {
            let (mut sock, _) = match listener.accept() {
                Ok(pair) => pair,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    continue;
                }
                Err(_) => break,
            };
            // 间隔 = 上一次请求到达 → 这一次请求到达：里面既有端点故意留的首包与流式间隔，
            // 也有客户端读完尾包、执行工具、拼下一次请求的全部本地开销
            let arrived = std::time::Instant::now();
            if let Some(prev) = prev.replace(arrived) {
                run.gaps_ms.push(arrived.duration_since(prev).as_millis() as u64);
            }
            // 响应要分三段写（首包 / 尾包），套接字必须回到阻塞模式：
            // 非阻塞下 write_all 遇到大正文会半途 WouldBlock，被吞掉就成了「响应体不完整」
            let _ = sock.set_nonblocking(false);
            let raw = read_request(&mut sock);
            let (_, body) = raw.split_once("\r\n\r\n").unwrap_or(("", raw.as_str()));
            let tokens = (body.len() as u64).div_ceil(4);
            run.bytes.push(body.len() as u64);
            let parsed: Value = serde_json::from_str(body).unwrap_or(Value::Null);
            let with_tools = parsed
                .get("tools")
                .and_then(Value::as_array)
                .map(|v| !v.is_empty())
                .unwrap_or(false);
            if with_tools {
                // 记下这次真的发出去的对话：下一段拿它当「中断前已恢复的上下文」，
                // 比重新造一份历史更接近真实的长任务
                run.last_messages = parsed
                    .get("messages")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                run.task_calls += 1;
            }

            let first_data: String = if !with_tools {
                run.summary_calls += 1;
                let chunk =
                    json!({"choices": [{"delta": {"content": "基准摘要：已读数据并核对字段"}}]});
                format!("data: {chunk}\n\n")
            } else if round < rounds_per_segment {
                round += 1;
                let i = run.tool_calls;
                run.tool_calls += 1;
                let filler: String = "基准正文样例"
                    .repeat(body_chars / 6 + 1)
                    .chars()
                    .take(body_chars)
                    .collect();
                let args = json!({
                    "file_path": format!("bench/k{i}.mod.md"),
                    "content": format!("{filler}（第 {i} 次写入）"),
                });
                let delta = json!({"tool_calls": [{
                    "index": 0,
                    "id": format!("bench-{i}"),
                    "type": "function",
                    "function": {"name": "write_text_file", "arguments": args.to_string()},
                }]});
                let chunk = json!({"choices": [{"delta": delta}]});
                format!("data: {chunk}\n\n")
            } else {
                round = 0;
                run.segments_done += 1;
                let chunk = json!({"choices": [{"delta": {
                    "content": format!("第 {} 段收尾：基准产物已写进 bench 目录", run.segments_done)
                }}]});
                format!("data: {chunk}\n\n")
            };
            let usage = json!({"usage": {"prompt_tokens": tokens, "completion_tokens": 40}});
            let tail = format!("data: {usage}\n\ndata: [DONE]\n\n");
            let sse_len = first_data.len() + tail.len();
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n",
                sse_len
            );
            // 只发响应头就开始等：这一段等待就是「模型排队 + 想第一个 token」，
            // 客户端必须在首包里看到它，否则首包数字是自找的
            let _ = sock.write_all(headers.as_bytes());
            let _ = sock.flush();
            if ttft_ms > 0 {
                std::thread::sleep(std::time::Duration::from_millis(ttft_ms));
            }
            let _ = sock.write_all(first_data.as_bytes());
            let _ = sock.flush();
            if stream_ms > 0 {
                std::thread::sleep(std::time::Duration::from_millis(stream_ms));
            }
            let _ = sock.write_all(tail.as_bytes());
            let _ = sock.flush();
        }
        run
    }

    fn pct(values: &[u64], p: usize) -> u64 {
        if values.is_empty() {
            return 0;
        }
        let mut sorted = values.to_vec();
        sorted.sort_unstable();
        let idx = (p * sorted.len() / 100).min(sorted.len() - 1);
        sorted[idx]
    }

    fn avg(values: &[u64]) -> u64 {
        if values.is_empty() {
            return 0;
        }
        values.iter().sum::<u64>() / values.len() as u64
    }

    /// 同一组数值的摘要：基准报告只认这种「平均 / 中位 / 95 分位 / 峰值」口径
    fn stats(values: &[u64]) -> Value {
        json!({
            "avg": avg(values),
            "p50": pct(values, 50),
            "p95": pct(values, 95),
            "max": values.iter().max().copied().unwrap_or(0),
        })
    }

    /// 长任务基准（D-40 第 6 项）：连跑 4 段 × 每段 25 轮工具 + 1 次收尾 = 104 次模型调用、
    /// 100 次工具写盘，中途跨过一次上下文压缩触发点，最后再压一次
    /// 「停在确认框上时应用被退出」的续跑，把 token、首包、恢复耗时三类数据打成 JSON。
    ///
    /// 默认不跑（`#[ignore]`），跑法：
    /// `bash scripts/run-rust-tests.sh harness::runner -- --ignored --nocapture`
    ///
    /// 口径（报告里也要这么写清楚）：
    /// - 端点是本机假服务，不联网、不花真实 token；`prompt_tokens` = 实际请求体字节 ÷ 4 折算，
    ///   引擎侧基准连的是同一个端点、用的是同一条公式，所以两侧可比，绝对值不代表真实模型；
    ///   completion 侧是假端点固定回报的 40/次，只看趋势不看金额；
    /// - 首包 = 客户端从发出请求到解析出第一个有内容增量的时间（端点故意留 40ms）；
    ///   用量事件（kind = usage）在 ChatEvent 里没有对应变体、渲染链路会丢掉，
    ///   所以这两个数是直接调 `model::stream` 取样取来的，不是从事件流里回捞的；
    /// - 检查点写到临时数据根，不碰用户真实 `~/.tongtianwork`；
    /// - 档位显式 auto_approve，基准绝不弹系统确认框；autoMemory 关闭，不写意识目录。
    #[tokio::test]
    #[ignore]
    async fn bench_long_task_100_rounds() {
        const ROUNDS: usize = 25;
        const SEGMENTS: usize = 4;
        const BODY_CHARS: usize = 2400;
        const TTFT_MS: u64 = 40;
        const STREAM_MS: u64 = 25;

        // 数据根换成临时目录：基准要真的落盘检查点，但不能写进用户目录
        let data_root = std::env::temp_dir().join(format!("tt-bench-home-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data_root);
        std::fs::create_dir_all(&data_root).unwrap();
        std::env::set_var("TONGTIANWORK_HOME", &data_root);

        let root = std::env::temp_dir().join(format!("tt-bench-work-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let stub = std::thread::spawn(move || {
            pump_bench(
                listener,
                ROUNDS,
                SEGMENTS,
                BODY_CHARS,
                TTFT_MS,
                STREAM_MS,
                std::time::Duration::from_secs(240),
            )
        });

        let model_config = json!({
            "modelName": "bench-fake",
            "displayName": "基准假模型",
            "baseUrl": format!("http://{addr}/v1"),
            "contextWindow": 32_768,
        });
        let runtime = json!({
            "awareness": { "mode": true, "autoMemory": false },
            "filePermission": "auto_approve",
            "agentPolicy": { "maxIters": 30, "autoCompress": true },
        });

        // 工具清单：走一遍 run_loop 里的同一份预检，取样调用带的是同一个东西
        let tb = ToolBox::with_permission(&root, prompt::skills_of(&runtime), "auto_approve").unwrap();
        let raw_specs = tb.specs();
        let specs = provider::validate_tools(&raw_specs).0;

        let mut history: Vec<Value> = Vec::new();
        let mut events_total = 0usize;
        let mut compress_notes = 0usize;
        let mut final_cp = checkpoint::Checkpoint::new("bench-unused");
        let wall = std::time::Instant::now();

        for seg in 1..=SEGMENTS {
            let goal = format!("长任务基准第 {seg} 段：把 {ROUNDS} 个中间结果分别写进 bench 目录");
            let slot = Slot::new(
                "task-bench",
                &goal,
                "general",
                root.clone(),
                model_config.clone(),
                runtime.clone(),
                history.clone(),
                Vec::new(),
                // 隐私模式不落盘，基准要量的正是落盘那一份开销
                false,
            );
            let (tx, rx) = tokio::sync::mpsc::channel::<Value>(2048);
            let sink = std::sync::Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
            let collector = {
                let sink = std::sync::Arc::clone(&sink);
                tokio::spawn(async move {
                    let mut rx = rx;
                    while let Some(ev) = rx.recv().await {
                        sink.lock().unwrap().push(ev);
                    }
                })
            };
            let (end, cp) = {
                let p0 = P0Context::default();
                let em = Emit {
                    task_id: "task-bench",
                    p0: &p0,
                    tx: &tx,
                };
                run_loop(&em, &slot, false).await
            };
            drop(tx);
            let _ = collector.await;
            let events = std::mem::take(&mut *sink.lock().unwrap());
            events_total += events.len();
            // 只数 step_done：同一条说明会先发 step_start 再发 step_done，
            // 两个都带 stepId，都数就把一次压缩算成两次
            compress_notes += events
                .iter()
                .filter(|e| {
                    e["type"] == "step_done" && e["stepId"].as_str() == Some("ctx-compress")
                })
                .count();
            let final_text = match &end {
                End::Done { final_text, .. } => final_text.clone(),
                other => panic!("第 {seg} 段没有正常收尾：{other:?}"),
            };
            history.push(json!({ "role": "user", "content": goal }));
            history.push(json!({ "role": "assistant", "content": final_text }));
            final_cp = cp;
        }
        let wall_ms = wall.elapsed().as_millis() as u64;
        let run = stub.join().unwrap();
        assert_eq!(run.segments_done, SEGMENTS, "四段都要收尾");
        assert_eq!(
            run.task_calls,
            SEGMENTS * (ROUNDS + 1),
            "每段 {ROUNDS} 轮工具 + 1 次收尾调用"
        );
        // token 口径：端点是假的，请求体是真的。prompt 侧一律按「实际发出的字节 ÷ 4
        // 向上取整」折算，引擎侧连同一个端点、用同一条公式，两侧可比；绝对值不代表真实分词。
        let request_bytes_total: u64 = run.bytes.iter().sum();
        let max_request_bytes = *run.bytes.iter().max().unwrap_or(&0);
        let max_prompt = max_request_bytes.div_ceil(4);
        let prompt_tokens_total = request_bytes_total.div_ceil(4);
        assert!(
            compress_notes >= 1,
            "基准要把正文撑到跨过一次压缩触发点，否则没测到长任务那条路：\
             最大 prompt_tokens {max_prompt}（阈值 28_672），最大请求体 {max_request_bytes} 字节"
        );

        // ── 单次调用的首包 / 整包：直接走模型层取样 ──
        // 用量事件（kind = "usage"）在 ChatEvent 里没有对应变体，渲染链路会把它整条丢掉，
        // 所以这两个数不能从事件流里回捞——只能拿真正的 model::stream 发一轮，
        // 读它自己算出来的 first_token_ms / latency_ms / prompt_tokens。
        const SAMPLES: usize = 10;
        let sample_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let sample_addr = sample_listener.local_addr().unwrap();
        let sample_stub = std::thread::spawn(move || {
            pump_bench(
                sample_listener,
                0,
                SAMPLES * 2,
                0,
                TTFT_MS,
                STREAM_MS,
                std::time::Duration::from_secs(120),
            )
        });
        let sample_endpoints = model::endpoints(Some(&json!({
            "modelName": "bench-fake",
            "displayName": "基准假模型",
            "baseUrl": format!("http://{sample_addr}/v1"),
            "contextWindow": 32_768,
        })))
        .expect("基准端点解析");
        let thin = vec![
            json!({ "role": "system", "content": "基准系统提示" }),
            json!({ "role": "user", "content": "长任务基准：把中间结果写进 bench 目录" }),
        ];
        // 满上下文 = 基准最后一段真的发出去过的那一份（含压缩后的摘要与最近几组工具结果）
        let fat = run.last_messages.clone();
        let never_cancelled = || false;
        let mut thin_firsts: Vec<u64> = Vec::new();
        let mut thin_totals: Vec<u64> = Vec::new();
        let mut fat_firsts: Vec<u64> = Vec::new();
        let mut fat_totals: Vec<u64> = Vec::new();
        let mut fat_prompt = 0u64;
        for _ in 0..SAMPLES {
            let out = model::stream(&sample_endpoints, &thin, &specs, &never_cancelled)
                .await
                .expect("小上下文取样调用失败");
            thin_firsts.push(out.first_token_ms);
            thin_totals.push(out.latency_ms);
        }
        for _ in 0..SAMPLES {
            let out = model::stream(&sample_endpoints, &fat, &specs, &never_cancelled)
                .await
                .expect("满上下文取样调用失败");
            fat_firsts.push(out.first_token_ms);
            fat_totals.push(out.latency_ms);
            fat_prompt = fat_prompt.max(out.prompt_tokens);
        }
        assert_eq!(
            sample_stub.join().unwrap().task_calls,
            SAMPLES * 2,
            "两次取样都要真的打到端点"
        );
        assert!(
            fat.len() >= 8 && fat_prompt >= 1_000,
            "满上下文取样必须带上真的多轮对话：{} 条 / {fat_prompt} tokens",
            fat.len()
        );

        // 落盘体积与读写耗时：检查点每轮都写，长任务的固定成本就在这儿
        let bench_files = std::fs::read_dir(root.join("bench"))
            .map(|d| d.count())
            .unwrap_or(0);
        let mut cp = checkpoint::Checkpoint::new("task-bench-disk");
        let mut msgs = vec![json!({ "role": "system", "content": "基准系统提示" })];
        msgs.extend(run.last_messages.clone());
        cp.capture(&msgs);
        cp.mark_final(true, "基准：检查点读写取样");
        let mut save_ms: Vec<u64> = Vec::new();
        let mut load_ms: Vec<u64> = Vec::new();
        let mut disk_bytes = 0u64;
        for _ in 0..3 {
            let t = std::time::Instant::now();
            cp.persist().unwrap();
            save_ms.push(t.elapsed().as_micros() as u64 / 1000);
            let t = std::time::Instant::now();
            let loaded = checkpoint::load("task-bench-disk").expect("检查点读不回来");
            load_ms.push(t.elapsed().as_micros() as u64 / 1000);
            disk_bytes = checkpoint::file_in(&checkpoint::home(), &loaded.scope(), "task-bench-disk")
                .metadata()
                .map(|m| m.len())
                .unwrap_or(0);
        }
        let carried_messages = run.last_messages.len();

        // 每轮固定要做的预处理：历史重编 + 工具清单预检 + 请求体序列化
        let prep = std::time::Instant::now();
        let mut payload_bytes = 0usize;
        for _ in 0..20 {
            // 20 条 / 单条 600 字：与 prompt.rs 的同名常量一致（契约测试钉住不许漂移）
            let (encoded, _) = provider::encode_history(&msgs[1..], 20, 600);
            let (checked, _) = provider::validate_tools(&raw_specs);
            let body = json!({ "model": "bench-fake", "messages": encoded, "tools": checked });
            payload_bytes = serde_json::to_string(&body).map(|s| s.len()).unwrap_or(0);
        }
        let prep_us = prep.elapsed().as_micros() as u64 / 20;

        // 同口径的第二把尺：引擎侧的 preprocess 量的是「把真正要发出去的那份上下文序列化」，
        // 它不做历史截断。只比 harness 的历史编码那一段会低估固定开销，所以这里按
        // 实际携带的多轮上下文再量一次，报告里两个数分开列、各说各的口径。
        let prep_fat = std::time::Instant::now();
        let mut fat_payload_bytes = 0usize;
        for _ in 0..20 {
            let body = json!({ "model": "bench-fake", "messages": fat, "tools": specs });
            fat_payload_bytes = serde_json::to_string(&body).map(|s| s.len()).unwrap_or(0);
        }
        let prep_fat_us = prep_fat.elapsed().as_micros() as u64 / 20;

        // ── 审批中断续跑：停在确认框上时应用被退出 ──
        let resume_root =
            std::env::temp_dir().join(format!("tt-bench-resume-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&resume_root);
        std::fs::create_dir_all(resume_root.join("bench")).unwrap();
        std::fs::write(
            resume_root.join("bench/resume-done.md"),
            "中断前已经写好，续跑不许重做",
        )
        .unwrap();
        let done_args = json!({ "file_path": "bench/resume-done.md", "content": "不该再写" });
        let todo_args: Vec<Value> = (0..2)
            .map(|i| {
                json!({
                    "file_path": format!("bench/resume-{}.md", i + 1),
                    "content": "续跑补写的正文",
                })
            })
            .collect();
        let calls: Vec<checkpoint::TurnCall> = std::iter::once(&done_args)
            .chain(todo_args.iter())
            .enumerate()
            .map(|(i, args)| checkpoint::TurnCall {
                id: format!("r{i}"),
                tool: "write_text_file".into(),
                arguments: args.to_string(),
                signature: checkpoint::signature_of("write_text_file", args),
            })
            .collect();
        let listed: Vec<Value> = calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.tool, "arguments": c.arguments },
                })
            })
            .collect();
        let mut interrupted = checkpoint::Checkpoint::new("task-bench-resume");
        interrupted.messages = vec![
            json!({ "role": "system", "content": "基准系统提示" }),
            json!({ "role": "user", "content": "基准：把三个文件写完" }),
            json!({ "role": "assistant", "content": "", "tool_calls": listed }),
            json!({ "role": "tool", "tool_call_id": "r0", "content": "已写入 bench/resume-done.md" }),
        ];
        interrupted.resumable = true;
        interrupted.reason = "基准：停在确认框上时应用退出".into();
        interrupted.baseline_files = snapshot_files(&resume_root);
        interrupted.record_tool("write_text_file", &calls[0].signature, "已写入 bench/resume-done.md");
        interrupted.begin_turn(calls.clone());
        let t = std::time::Instant::now();
        interrupted.persist().unwrap();
        let interrupt_save_ms = t.elapsed().as_micros() as u64 / 1000;

        let resume_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let resume_addr = resume_listener.local_addr().unwrap();
        let resume_stub = std::thread::spawn(move || {
            pump(
                resume_listener,
                &["data: {\"choices\":[{\"delta\":{\"content\":\"三个文件都写好了\"}}]}\n\ndata: {\"usage\":{\"prompt_tokens\":800,\"completion_tokens\":9}}\n\ndata: [DONE]\n\n"],
                false,
            )
        });
        let resume_slot = Slot::new(
            "task-bench-resume",
            "基准：把三个文件写完",
            "general",
            resume_root.clone(),
            json!({
                "modelName": "bench-fake",
                "displayName": "基准假模型",
                "baseUrl": format!("http://{resume_addr}/v1"),
                "contextWindow": 32_768,
            }),
            runtime.clone(),
            Vec::new(),
            Vec::new(),
            false,
        );
        resume_slot.update(|inner| {
            inner.checkpoint = checkpoint::load("task-bench-resume").expect("恢复读不到检查点");
        });
        let (tx, rx) = tokio::sync::mpsc::channel::<Value>(256);
        let sink = std::sync::Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let collector = {
            let sink = std::sync::Arc::clone(&sink);
            tokio::spawn(async move {
                let mut rx = rx;
                while let Some(ev) = rx.recv().await {
                    sink.lock().unwrap().push(ev);
                }
            })
        };
        let t = std::time::Instant::now();
        let (end, resumed_cp) = {
            let p0 = P0Context::default();
            let em = Emit {
                task_id: "task-bench-resume",
                p0: &p0,
                tx: &tx,
            };
            run_loop(&em, &resume_slot, true).await
        };
        let restore_ms = t.elapsed().as_micros() as u64 / 1000;
        drop(tx);
        let _ = collector.await;
        assert!(matches!(end, End::Done { .. }), "{end:?}");
        for i in 1..=2 {
            assert!(
                resume_root.join(format!("bench/resume-{i}.md")).exists(),
                "欠着的工具必须补上：bench/resume-{i}.md 没落盘"
            );
        }
        assert_eq!(
            std::fs::read_to_string(resume_root.join("bench/resume-done.md")).unwrap(),
            "中断前已经写好，续跑不许重做",
            "已有结果的那个不能重放"
        );
        assert_eq!(resumed_cp.model_calls, 1, "原地续跑不该重问模型，只多收尾那一次");
        assert_eq!(resume_stub.join().unwrap(), 1);

        println!(
            "BENCH_JSON {}",
            json!({
                "runtime": "rust-harness",
                "caliber": {
                    "endpoint": "本地假端点，不联网、不花真实 token",
                    "promptTokens": "实际请求体字节 ÷ 4 向上取整（折算口径，非真实分词；引擎侧同一条公式）",
                    "completionTokens": "假端点固定回报 40/次，不代表真实产出",
                    "firstPacket": "客户端从发出请求到解析出第一个有内容增量的时间；端点先只发响应头，隔 TTFT 才发首包",
                    "preprocess": "两把尺分开列：preprocess_us 是 harness 每轮真做的历史编码（20 条×600 字截断）+ 工具预检 + 序列化；preprocess_full_context_us 只把实际携带的整份上下文序列化，与引擎侧 preprocess 同口径",
                    "stampedTTFTms": TTFT_MS,
                    "stampedStreamMs": STREAM_MS,
                    "maxItersPerSegment": 30,
                },
                "segments": SEGMENTS,
                "rounds_per_segment": ROUNDS + 1,
                "model_calls": run.task_calls,
                "tool_calls": run.tool_calls,
                "summary_calls": run.summary_calls,
                "files_written": bench_files,
                "wall_ms": wall_ms,
                "round_gap_ms": stats(&run.gaps_ms),
                "first_packet_ms_thin_ctx": stats(&thin_firsts),
                "whole_call_ms_thin_ctx": stats(&thin_totals),
                "first_packet_ms_full_ctx": stats(&fat_firsts),
                "whole_call_ms_full_ctx": stats(&fat_totals),
                "prompt_tokens_full_ctx": fat_prompt,
                "request_bytes_total": request_bytes_total,
                "request_bytes_avg": run.bytes.iter().sum::<u64>() / run.bytes.len() as u64,
                "request_bytes_max": max_request_bytes,
                "prompt_tokens_total": prompt_tokens_total,
                "prompt_tokens_max": max_prompt,
                "completion_tokens_reported": (run.task_calls + run.summary_calls) as u64 * 40,
                "carried_messages": carried_messages,
                "compression_events": compress_notes,
                "compressed_messages": final_cp.compressed_messages,
                "checkpoint_disk_bytes": disk_bytes,
                "checkpoint_save_ms": stats(&save_ms),
                "checkpoint_load_ms": stats(&load_ms),
                "preprocess_us": prep_us,
                "payload_bytes_serialized": payload_bytes,
                "preprocess_full_context_us": prep_fat_us,
                "payload_bytes_full_context": fat_payload_bytes,
                "events_total": events_total,
                "restore": {
                    "interrupt_save_ms": interrupt_save_ms,
                    "restore_ms": restore_ms,
                    "pending_tools": todo_args.len(),
                },
            })
        );
        std::fs::remove_dir_all(&root).ok();
        std::fs::remove_dir_all(&resume_root).ok();
        std::fs::remove_dir_all(&data_root).ok();
    }

    fn serve_sse(
        listener: std::net::TcpListener,
        bodies: &[&'static str],
    ) -> usize {
        pump(listener, bodies, false)
    }

    fn serve_sse_or_error(
        listener: std::net::TcpListener,
        bodies: &[&'static str],
    ) -> usize {
        pump(listener, bodies, true)
    }

    /// 顺序应答的假端点：可指定状态码（用来压「端点拒收工具」），
    /// 并把每个请求原文回传，断言才能看到实际发出去的 messages / tools。
    fn pump_scripted(
        listener: std::net::TcpListener,
        replies: &[(u16, &'static str)],
    ) -> (usize, Vec<String>) {
        use std::io::Write;

        listener.set_nonblocking(true).unwrap();
        let mut served = 0usize;
        let mut requests: Vec<String> = Vec::new();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        while served < replies.len() && std::time::Instant::now() < deadline {
            match listener.accept() {
                Ok((mut sock, _)) => {
                    requests.push(read_request(&mut sock));
                    let (status, body) = replies[served];
                    let resp = format!(
                        "HTTP/1.1 {status} X\r\nContent-Type: text/event-stream\r\n\
                         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes());
                    let _ = sock.flush();
                    served += 1;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(20));
                }
                Err(_) => break,
            }
        }
        (served, requests)
    }

    /// 顺序应答的小假端点：空 body 且 `error_first` 时回 503，用来压测摘要失败分支。
    fn pump(
        listener: std::net::TcpListener,
        bodies: &[&'static str],
        error_first: bool,
    ) -> usize {
        use std::io::Write;

        listener.set_nonblocking(true).unwrap();
        let mut served = 0usize;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        while served < bodies.len() && std::time::Instant::now() < deadline {
            match listener.accept() {
                Ok((mut sock, _)) => {
                    // 必须把请求体读干净再回话：压缩摘要的请求有几十 KB，
                    // 只读一个 8K 窗口就写响应会让客户端在发送中途被重置
                    read_request(&mut sock);
                    let body = bodies[served];
                    let resp = if error_first && body.is_empty() {
                        "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\
                         Connection: close\r\n\r\n"
                            .to_string()
                    } else {
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        )
                    };
                    let _ = sock.write_all(resp.as_bytes());
                    let _ = sock.flush();
                    served += 1;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(20));
                }
                Err(_) => break,
            }
        }
        served
    }

        /// 读完整请求（头部 + 声明长度的请求体）并回传原文，让断言能看到「实际发出去的东西」。
    fn read_request(sock: &mut std::net::TcpStream) -> String {
        use std::io::Read;

        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 8192];
        let header_end = loop {
            match sock.read(&mut chunk) {
                Ok(0) => break buf.len(),
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(pos) = find_double_newline(&buf) {
                        break pos;
                    }
                    if buf.len() > 8_000_000 {
                        break buf.len();
                    }
                }
                Err(_) => break buf.len(),
            }
        };
        let need = content_length(&buf[..header_end.min(buf.len())]).unwrap_or(0);
        while buf.len() < header_end + need {
            match sock.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
        String::from_utf8_lossy(&buf).to_string()
    }

    fn find_double_newline(buf: &[u8]) -> Option<usize> {
        buf.windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|p| p + 4)
    }

    fn content_length(head: &[u8]) -> Option<usize> {
        let text = String::from_utf8_lossy(head).to_ascii_lowercase();
        for line in text.lines() {
            if let Some(v) = line.strip_prefix("content-length:") {
                return v.trim().parse::<usize>().ok();
            }
        }
        None
    }
}
