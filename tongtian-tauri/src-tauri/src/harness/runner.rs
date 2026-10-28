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
use crate::harness::model::{self, ModelError, Outcome};
use crate::harness::prompt::{self, PlanStep};
use crate::harness::tools::ToolBox;
use crate::harness::{max_iters, Slot};
use crate::P0Services;

const MODE_NOTE: &str = "本地 harness 运行时：模型路由、文件工具、技能与检查点在本机进程内完成；\
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

    async fn usage(&self, out: &Outcome) {
        self.event(
            "usage",
            json!({ "usage": {
                "promptTokens": out.prompt_tokens,
                "completionTokens": out.completion_tokens,
                "model": out.model,
                "latencyMs": out.latency_ms,
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

    let snap = slot.snapshot();
    let steps = snap.plan.clone();
    match &end {
        End::Done { final_text, artifacts } => {
            cp.mark_final(false, "任务已正常完成，无需续跑");
            if !privacy {
                cp.delete();
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
            persist(&em, privacy, &mut cp, &snap, &steps).await;
        }
        End::Failed(message) => {
            cp.mark_final(true, &format!("执行中断：{message}"));
            let (code, text) = split_code(message);
            em.event("error", json!({ "code": code, "message": text }))
                .await;
            persist(&em, privacy, &mut cp, &snap, &steps).await;
        }
    }
    let info = cp.public_info();
    slot.update(|inner| inner.checkpoint = cp.clone());
    em.event("checkpoint", info).await;
    em.event("done", json!({ "taskId": slot.task_id })).await;
    drop(tx);
    let _ = handle.await;
    match end {
        End::Failed(message) => Err(message),
        _ => Ok(()),
    }
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
    let toolbox = match ToolBox::new(&snap.work_dir, prompt::skills_of(&snap.runtime)) {
        Ok(v) => v,
        Err(e) => return (End::Failed(e), snap.checkpoint.clone()),
    };
    let tools = toolbox.specs();

    let mut cp = snap.checkpoint.clone();
    cp.begin_run();
    let resumed = resume && !cp.messages.is_empty();
    let mut messages = if resumed {
        em.note(
            "harness-resume",
            "已从本地检查点恢复：中断前完成的工具调用会按签名跳过，随后继续执行。",
        )
        .await;
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
        let mut messages = vec![json!({ "role": "system", "content": system })];
        messages.extend(prompt::history_messages(&snap.history));
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
    let mut step_no = 2u32;
    let mut final_text: Option<String> = None;
    let mut last_text = String::new();

    for _ in 0..max {
        if slot.is_cancelled() {
            return (End::Cancelled, cp);
        }
        cp.capture(&messages);
        let out = match model::stream(&endpoints, &messages, &tools, &|| slot.is_cancelled()).await {
            Ok(v) => v,
            Err(ModelError::Cancelled) => return (End::Cancelled, cp),
            Err(ModelError::Failed(msg)) => return (End::Failed(msg), cp),
        };
        em.usage(&out).await;
        em.switches("run", &out).await;
        last_text = out.text.clone();
        if out.tool_calls.is_empty() {
            final_text = Some(out.text);
            break;
        }
        let calls: Vec<Value> = out
            .tool_calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.name, "arguments": c.arguments },
                })
            })
            .collect();
        messages.push(json!({
            "role": "assistant",
            "content": out.text,
            "tool_calls": calls,
        }));

        for call in &out.tool_calls {
            if slot.is_cancelled() {
                return (End::Cancelled, cp);
            }
            let step_id = format!("step-{step_no}");
            step_no += 1;
            em.event(
                "step_start",
                json!({
                    "step_id": step_id,
                    "stepType": "tool_call",
                    "tool": call.name,
                    "description": format!("调用工具: {}", call.name),
                }),
            )
            .await;

            let parsed = serde_json::from_str::<Value>(&call.arguments)
                .ok()
                .filter(Value::is_object);
            let Some(args) = parsed else {
                let content = "错误：工具参数不是合法 JSON 对象，本次调用未执行".to_string();
                em.event(
                    "step_failed",
                    json!({ "step_id": step_id, "error": content }),
                )
                .await;
                messages.push(json!({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                }));
                continue;
            };

            let signature = checkpoint::signature_of(&call.name, &args);
            if let Some(prev) = cp.previous_record(&signature) {
                cp.skip_previous(&signature);
                let note = format!(
                    "[断点续跑] 工具「{}」这次调用与中断前已成功执行的一次完全相同，\
                     已跳过重复执行，直接返回当时的结果。",
                    call.name
                );
                em.event(
                    "step_done",
                    json!({ "step_id": step_id, "output": note }),
                )
                .await;
                messages.push(json!({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": format!("{note}\n\n{}", prev.content),
                }));
                continue;
            }

            let run = toolbox.run(&call.name, &args);
            if run.side_effect {
                cp.record_tool(&call.name, &signature, &run.content);
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
        }
    }

    let text = final_text.unwrap_or_else(|| {
        if last_text.trim().is_empty() {
            "已达到单轮工具循环上限，任务未完成；可继续对话补充指示。".to_string()
        } else {
            last_text
        }
    });
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

fn meta_of(snap: &crate::harness::Snapshot, steps: &[PlanStep]) -> Meta {
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
    }
}

fn model_error(err: ModelError) -> String {
    match err {
        ModelError::Cancelled => "TASK_CANCELLED: 用户取消了任务".to_string(),
        ModelError::Failed(msg) => msg,
    }
}

fn split_code(message: &str) -> (String, String) {    if let Some((code, rest)) = message.split_once(':') {
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
            json!({ "awareness": { "mode": true, "autoMemory": false } }),
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
            // 关闭自动记忆：测试不得写用户真实意识目录
            json!({ "awareness": { "mode": true, "autoMemory": false } }),
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
            json!({ "awareness": { "mode": true, "autoMemory": false } }),
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
}
