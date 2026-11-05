//! 本地 harness 的对话检查点：快照 + 副作用台账落盘，支持刷新/重启后续跑。
//!
//! 与引擎检查点分目录存放（`checkpoints-harness`），并在文件里带 `runtime` 标记，
//! 避免两套运行时互相把对方的快照当成自己的恢复起点。文件里只有恢复所需的
//! 对话与台账，不含模型密钥；隐私模式不落盘。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::harness::clock;

pub const CHECKPOINT_VERSION: u32 = 1;
pub const RUNTIME_TAG: &str = "harness";
pub const TTL_SECONDS: i64 = 7 * 24 * 3600;
pub const MAX_CHECKPOINT_CHARS: usize = 400_000;
pub const MAX_DISK_CHARS: usize = 16_000_000;
pub const MAX_TOOL_RESULT_CHARS: usize = 4_000;
pub const MAX_TOOL_INPUT_CHARS: usize = 8_000;
pub const MAX_TOOL_RECORDS: usize = 1_000;

/// 负责人任务的检查点目录名（也是所有旧快照的归属：升级前只有扁平一层）。
pub const DEFAULT_SCOPE: &str = "leader";
/// 执行体目录的上一级：`checkpoints-harness/agents/<scope>/<hash>.json`。
pub const AGENTS_DIR: &str = "agents";
/// 团队成员目录的前缀：与负责人族分开，按任务恢复时不会把成员的快照当自己的。
pub const MEMBER_PREFIX: &str = "member";
/// 目录名长度上限：超出部分用完整 id 的短哈希后缀，避免撞名与超长路径。
const SCOPE_MAX_CHARS: usize = 64;

/// 工具调用指纹：工具名 + 规范化 JSON 输入（不保存原始参数）
pub fn signature_of(name: &str, args: &Value) -> String {
    let canonical = serde_json::to_string(args).unwrap_or_else(|_| args.to_string());
    let mut hasher = Sha256::new();
    hasher.update(name.as_bytes());
    hasher.update(b"\n");
    hasher.update(canonical.as_bytes());
    format!("{:x}", hasher.finalize())
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct ToolRecord {
    pub tool: String,
    pub content: String,
    pub at_ms: i64,
    pub run: u64,
    pub skipped: u64,
}

/// 续跑需要重新组装本轮输入的事实（不含密钥）
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Meta {
    pub goal: String,
    pub mode: String,
    pub work_dir: String,
    pub history: Vec<Value>,
    pub attachments: Vec<Value>,
    /// 已批准/已生成的计划步骤（[{id,type,description}]），重启后仍能批准执行
    pub plan: Vec<Value>,
    /// 本轮确认过的文件访问档位：续跑必须沿用，不能让重启把「每次询问」
    /// 悄悄变成设置里的全局默认（或反过来升档）。老检查点没这个键时按最保守档。
    #[serde(default)]
    pub file_permission: String,
    /// 产出这份快照的模型：续跑换了模型时，界面要能说清「历史仍是原来那份」，
    /// 而不是让用户以为中断前的对话被换模型的口径重写过。
    #[serde(default)]
    pub model_id: String,
    #[serde(default)]
    pub model_display: String,
}

/// 一轮模型输出里的单个工具调用；指纹用于「参数是否还是当初那一份」的判定。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct TurnCall {
    pub id: String,
    pub tool: String,
    pub arguments: String,
    pub signature: String,
}

/// 中断时仍未拿到结果的模型回合。
///
/// 只登记调用清单，不在结构里存「执行到第几个」——续跑时以消息里实际存在的
/// `tool` 结果为准（见 `resume_turn`），计数器和事实对不上时事实赢。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct PendingTurn {
    pub calls: Vec<TurnCall>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Checkpoint {
    pub version: u32,
    pub runtime: String,
    pub task_id: String,
    pub saved_at_ms: i64,
    pub resumable: bool,
    pub reason: String,
    pub messages: Vec<Value>,
    #[serde(default)]
    pub tool_records: BTreeMap<String, ToolRecord>,
    #[serde(default)]
    pub meta: Meta,
    #[serde(default)]
    pub run_id: u64,
    #[serde(default)]
    pub model_calls: u64,
    #[serde(default)]
    pub executed_side_effects: u64,
    #[serde(default)]
    pub skipped_duplicates: u64,
    #[serde(default)]
    pub dropped_messages: u64,
    /// 上一次调用回报的 prompt_tokens：续跑后压缩阈值仍以端点真实计数为准
    #[serde(default)]
    pub last_prompt_tokens: u64,
    /// 本任务累计被压缩合并的消息条数（摘要已替掉它们）
    #[serde(default)]
    pub compressed_messages: u64,
    #[serde(default)]
    pub capture_error: String,
    /// 执行前文件基线（相对路径 → "size:mtime"），恢复时沿用，产物才算得全
    #[serde(default)]
    pub baseline_files: BTreeMap<String, String>,
    /// 模型这一轮给出的工具调用中还没全部拿到结果的部分；
    /// 没有它时，「在确认框上退出应用」只能重问模型，已完成的那几个工具
    /// 只能靠签名台账跳过——参数稍有不同就会真的重放一遍。
    #[serde(default)]
    pub pending_turn: Option<PendingTurn>,
    /// 这份快照属于哪个执行体（负责人 / 团队成员）。
    /// 空 = 升级前的旧快照或本轮还没盖章，一律按「负责人」处理（`scope()` 兜底）。
    #[serde(default)]
    pub agent_scope: String,
}

impl Checkpoint {
    pub fn new(task_id: &str) -> Self {
        Self {
            version: CHECKPOINT_VERSION,
            runtime: RUNTIME_TAG.to_string(),
            task_id: task_id.to_string(),
            saved_at_ms: 0,
            resumable: false,
            reason: "尚未产生检查点".to_string(),
            messages: Vec::new(),
            tool_records: BTreeMap::new(),
            meta: Meta::default(),
            run_id: 0,
            model_calls: 0,
            executed_side_effects: 0,
            skipped_duplicates: 0,
            dropped_messages: 0,
            last_prompt_tokens: 0,
            compressed_messages: 0,
            capture_error: String::new(),
            baseline_files: BTreeMap::new(),
            pending_turn: None,
            agent_scope: String::new(),
        }
    }

    /// 落盘目录用的执行体名：没盖过章（空串）或旧快照一律按负责人。
    pub fn scope(&self) -> String {
        if self.agent_scope.trim().is_empty() {
            DEFAULT_SCOPE.to_string()
        } else {
            sanitize_scope(&self.agent_scope)
        }
    }

    /// 新一轮执行（approve / resume 各算一次）；同轮内重复调用不跳过。
    pub fn begin_run(&mut self) -> u64 {
        self.run_id += 1;
        if !self.resumable && !self.messages.is_empty() {
            self.resumable = true;
        }
        self.run_id
    }

    /// 每次模型调用前采集记忆快照；失败保留上一份有效快照并记录原因。
    pub fn capture(&mut self, messages: &[Value]) {
        if self.snapshot(messages) {
            self.model_calls += 1;
        }
    }

    /// 只采集快照，不动模型调用计数。
    ///
    /// 工具轮次中间也要落一次：确认框是阻塞调用，用户在框上关掉应用时，
    /// 「这一轮欠哪几个工具」只能从这种快照里读回来。
    /// 返回是否写入成功（超限保留上一份并记下原因）。
    pub fn snapshot(&mut self, messages: &[Value]) -> bool {
        let mut items: Vec<Value> = messages.iter().map(sanitize_message).collect();
        let mut dropped = 0u64;
        let mut encoded = serde_json::to_string(&items).unwrap_or_default();
        while encoded.len() > MAX_CHECKPOINT_CHARS && items.len() > 1 {
            let drop_at = if items.first().and_then(|m| m.get("role")).and_then(Value::as_str) == Some("system") {
                1
            } else {
                0
            };
            items.remove(drop_at);
            dropped += 1;
            encoded = serde_json::to_string(&items).unwrap_or_default();
        }
        if encoded.len() > MAX_CHECKPOINT_CHARS {
            self.capture_error = format!(
                "单条消息超过检查点上限（{} 字符），本轮无法保存快照",
                encoded.len()
            );
            return false;
        }
        self.messages = items;
        self.dropped_messages = dropped;
        self.capture_error.clear();
        self.resumable = true;
        self.reason = "执行中断后可从此检查点继续".to_string();
        true
    }

    /// 模型给出工具调用后立刻登记整轮清单（含已经执行完的那些）。
    pub fn begin_turn(&mut self, calls: Vec<TurnCall>) {
        self.pending_turn = if calls.is_empty() {
            None
        } else {
            Some(PendingTurn { calls })
        };
    }

    /// 整轮都拿到结果了：标记必须清掉，否则下次恢复会误以为还欠工具。
    pub fn clear_turn(&mut self) {
        self.pending_turn = None;
    }

    /// 当前快照里还欠结果的工具调用（判定失败时按「不欠」处理）。
    pub fn pending_calls(&self) -> Vec<TurnCall> {
        self.resume_turn(&self.messages).unwrap_or_default()
    }

    /// 续跑判定：标记的这一轮和消息里的 `tool_calls` 对得上时，
    /// 按原顺序返回还没拿到结果的调用；对不上就给出中文原因，调用方退回重问模型。
    ///
    /// 已执行的那些不需要重放：结果本来就在这份消息里，直接喂回模型即可。
    pub fn resume_turn(&self, messages: &[Value]) -> Result<Vec<TurnCall>, String> {
        let Some(turn) = self.pending_turn.as_ref() else {
            return Err("检查点没有未完成工具轮次的标记".to_string());
        };
        if turn.calls.is_empty() {
            return Err("标记里的工具清单为空".to_string());
        }
        let Some(idx) = unpaired_tail(messages) else {
            if has_tool_calls(messages) {
                return Ok(Vec::new());
            }
            return Err("检查点快照里的那条工具调用消息已被裁剪掉".to_string());
        };
        let listed = messages[idx]
            .get("tool_calls")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if listed.len() != turn.calls.len() {
            return Err(format!(
                "快照里的工具调用有 {} 个，标记登记了 {} 个，对不上",
                listed.len(),
                turn.calls.len()
            ));
        }
        let answered: BTreeSet<String> = messages[idx + 1..]
            .iter()
            .filter_map(|m| m.get("tool_call_id").and_then(Value::as_str))
            .map(str::to_string)
            .collect();
        let mut pending = Vec::new();
        for (i, call) in turn.calls.iter().enumerate() {
            let listed_id = listed[i].get("id").and_then(Value::as_str).unwrap_or("");
            if listed_id != call.id {
                return Err(format!("第 {} 个工具调用与标记登记的不一致", i + 1));
            }
            if answered.contains(&call.id) {
                continue;
            }
            // 参数必须能原样解析且指纹一致：检查点会截断超长参数，
            // 拿被截断的参数重放等于偷偷改了模型要做的事，必须退回重问。
            let Some(args) = serde_json::from_str::<Value>(&call.arguments)
                .ok()
                .filter(Value::is_object)
            else {
                return Err(format!("工具「{}」的参数在检查点里不完整", call.tool));
            };
            if signature_of(&call.tool, &args) != call.signature {
                return Err(format!("工具「{}」的参数与登记时的指纹不一致（可能被截断）", call.tool));
            }
            pending.push(call.clone());
        }
        Ok(pending)
    }

    pub fn record_tool(&mut self, name: &str, signature: &str, content: &str) {
        if self.tool_records.len() >= MAX_TOOL_RECORDS && !self.tool_records.contains_key(signature) {
            return;
        }
        let record = self.tool_records.entry(signature.to_string()).or_default();
        record.tool = name.to_string();
        record.content = cap(content, MAX_TOOL_RESULT_CHARS);
        record.at_ms = clock::now_ms();
        record.run = self.run_id;
        self.executed_side_effects += 1;
    }

    /// 只返回「此前执行轮次」的记录；同一轮内的重复调用照常执行。
    pub fn previous_record(&self, signature: &str) -> Option<ToolRecord> {
        let record = self.tool_records.get(signature)?;
        if record.run >= self.run_id {
            return None;
        }
        Some(record.clone())
    }

    pub fn skip_previous(&mut self, signature: &str) {
        if let Some(record) = self.tool_records.get_mut(signature) {
            record.skipped += 1;
            self.skipped_duplicates += 1;
        }
    }

    /// 收尾：正常完成/回滚不保留大对象；中断/取消/失败保留快照以待续跑。
    pub fn mark_final(&mut self, resumable: bool, reason: &str) {
        let has_snapshot = !self.messages.is_empty();
        self.resumable = resumable && has_snapshot;
        self.reason = if resumable && !has_snapshot {
            format!("{reason}（尚未采集到模型调用前快照，无法续跑）")
        } else {
            reason.to_string()
        };
        if !self.resumable {
            self.messages.clear();
            self.saved_at_ms = 0;
            self.pending_turn = None;
        }
    }

    /// 原子写入磁盘；失败返回中文原因，不影响任务收尾。
    /// 落盘并同步内存里的 savedAt（检查点事件的 savedAt 显示用）
    pub fn persist(&mut self) -> Result<(), String> {
        self.saved_at_ms = clock::now_ms();
        save_in(&home(), self)
    }

    pub fn delete(&self) {
        delete_in(&home(), &self.scope(), &self.task_id)
    }

    /// 查询接口的对外视图：只报告状态与计数，不返回对话内容/工具参数。
    pub fn public_info(&self) -> Value {
        let pending = self.pending_calls();
        json!({
            "type": "checkpoint",
            "taskId": self.task_id,
            "resumable": self.resumable && !self.messages.is_empty(),
            "reason": self.reason,
            "savedAt": clock::format_local_ms(self.saved_at_ms),
            "scope": "disk",
            "snapshotMessages": self.messages.len(),
            "executedTools": self.executed_side_effects,
            "sideEffects": self.tool_records.len(),
            "skippedDuplicates": self.skipped_duplicates,
            "modelCalls": self.model_calls,
            "mediaOmitted": 0,
            "droppedMessages": self.dropped_messages,
            "compressedMessages": self.compressed_messages,
            "contextEstimateTokens": self.last_prompt_tokens,
            "captureError": self.capture_error,
            // 中断点位置：这些工具还没结果，续跑时会从它们原地继续（含待审批的那一个）
            "pendingToolCalls": pending.len(),
            "pendingTools": pending.iter().map(|c| c.tool.clone()).collect::<Vec<String>>(),
            // 快照由哪个模型产出：换模型续跑时界面要说清「历史是原来那份」
            "producedByModel": self.meta.model_display,
            "producedByModelId": self.meta.model_id,
            // 快照属于哪个执行体（负责人 / 团队成员）：团队页按成员列续跑入口要用
            "agentScope": self.scope(),
        })
    }
}

fn cap(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let kept: String = text.chars().take(limit).collect();
    format!("{kept}…（已截断）")
}

fn sanitize_message(msg: &Value) -> Value {
    let role = msg.get("role").and_then(Value::as_str).unwrap_or("");
    let mut out = serde_json::Map::new();
    if !role.is_empty() {
        out.insert("role".into(), Value::String(role.to_string()));
    }
    if let Some(content) = msg.get("content").and_then(Value::as_str) {
        // 只截断工具结果；用户/助手正文是继续推理的上下文，保留原文，
        // 体积超限时整体从最早消息开始丢弃并计数。
        let body = if role == "tool" {
            cap(content, MAX_TOOL_RESULT_CHARS)
        } else {
            content.to_string()
        };
        out.insert("content".into(), Value::String(body));
    }
    if let Some(id) = msg.get("tool_call_id").and_then(Value::as_str) {
        out.insert("tool_call_id".into(), Value::String(id.to_string()));
    }
    if let Some(name) = msg.get("name").and_then(Value::as_str) {
        out.insert("name".into(), Value::String(name.to_string()));
    }
    if let Some(calls) = msg.get("tool_calls").and_then(Value::as_array) {
        let items: Vec<Value> = calls
            .iter()
            .map(|c| {
                let name = c
                    .get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let args = c
                    .get("function")
                    .and_then(|f| f.get("arguments"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                json!({
                    "id": c.get("id").and_then(Value::as_str).unwrap_or(""),
                    "type": "function",
                    "function": { "name": name, "arguments": cap(args, MAX_TOOL_INPUT_CHARS) },
                })
            })
            .collect();
        out.insert("tool_calls".into(), Value::Array(items));
    }
    Value::Object(out)
}

fn has_tool_calls(messages: &[Value]) -> bool {
    messages
        .iter()
        .any(|m| m.get("tool_calls").and_then(Value::as_array).is_some())
}

/// 尾部有没有「助手发起了工具调用，但结果还没配齐」。
/// 返回那条助手消息的下标；配齐了或压根没有工具调用时返回 `None`。
///
/// 半截配对直接喂给模型，多数端点会拒收整份请求，所以恢复前必须判这一步。
pub fn unpaired_tail(messages: &[Value]) -> Option<usize> {
    let idx = messages
        .iter()
        .rposition(|m| m.get("tool_calls").and_then(Value::as_array).is_some())?;
    let listed = messages[idx]
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|a| a.len())
        .unwrap_or(0);
    let tail = &messages[idx + 1..];
    let only_tools = tail
        .iter()
        .all(|m| m.get("role").and_then(Value::as_str) == Some("tool"));
    let answered = tail
        .iter()
        .filter(|m| m.get("role").and_then(Value::as_str) == Some("tool"))
        .count();
    if only_tools && answered >= listed {
        None
    } else {
        Some(idx)
    }
}

/// 退回「重问模型」：把最后那条结果没配齐的工具调用消息及其后半截摘掉。
///
/// 摘掉的这几个已成功的副作用不会丢：它们在台账里，重问后按签名跳过。
/// 返回摘掉的条数。
pub fn rewind_unpaired(messages: &mut Vec<Value>) -> usize {
    match unpaired_tail(messages) {
        Some(idx) => {
            let removed = messages.len() - idx;
            messages.truncate(idx);
            removed
        }
        None => 0,
    }
}

/// 去掉没有配对的 `tool` 结果（发起它的助手消息已被检查点裁剪掉时会出现）。
/// 端点同样不接受孤儿结果，返回去掉的条数。
pub fn drop_orphan_tool_results(messages: &mut Vec<Value>) -> usize {
    let ids: BTreeSet<String> = messages
        .iter()
        .filter_map(|m| m.get("tool_calls").and_then(Value::as_array))
        .flatten()
        .filter_map(|c| c.get("id").and_then(Value::as_str))
        .map(str::to_string)
        .collect();
    let before = messages.len();
    messages.retain(|m| {
        if m.get("role").and_then(Value::as_str) != Some("tool") {
            return true;
        }
        m.get("tool_call_id")
            .and_then(Value::as_str)
            .map(|id| ids.contains(id))
            .unwrap_or(false)
    });
    before - messages.len()
}

/// 检查点根目录：与引擎的 `checkpoints` 分开，避免互相误读。
pub fn home() -> PathBuf {
    let root = std::env::var("TONGTIANWORK_HOME")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let base = match root {
        Some(dir) => PathBuf::from(dir),
        None => dirs_home().unwrap_or_else(|| PathBuf::from(".")).join(".tongtianwork"),
    };
    base.join("checkpoints-harness")
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn file_of(base: &Path, task_id: &str) -> PathBuf {
    base.join(hashed_name(task_id))
}

/// 文件名只由 taskId 决定：执行体（负责人 / 团队成员）的区分体现在目录上，
/// 同一个 taskId 落进两个执行体也各有一份，不会互相覆盖。
fn hashed_name(task_id: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(task_id.as_bytes());
    format!("{:x}.json", hasher.finalize())
}

/// 名字里没有可用字符时的兜底目录名：用完整串的短哈希，
/// 两个不同的 id 不会因此撞进同一个目录。
fn short_hash(trimmed: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(trimmed.as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    format!("scope-{}", &digest[..8])
}

/// 把任意 id 变成安全的目录名：只留 ASCII 字母数字与 `_ -`，其余换成连字符。
/// 注册表 id 里带 `../`、路径分隔符或中文都不许直接当目录名用。
pub fn sanitize_scope(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return DEFAULT_SCOPE.to_string();
    }
    let mut out = String::new();
    let mut prev_dash = false;
    for ch in trimmed.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
            out.push(ch.to_ascii_lowercase());
            prev_dash = false;
        } else if !prev_dash {
            out.push('-');
            prev_dash = true;
        }
    }
    let out = out.trim_matches('-').to_string();
    if out.is_empty() {
        // 名字里一个可用字符都没有（例如纯中文 id）：改用短哈希，别把整个目录名弄没了
        return short_hash(trimmed);
    }
    if out.chars().count() <= SCOPE_MAX_CHARS {
        return out;
    }
    let mut head: String = out.chars().take(SCOPE_MAX_CHARS).collect();
    head.push('-');
    head.push_str(short_hash(trimmed).strip_prefix("scope-").unwrap_or("over-long"));
    head
}

/// 负责人（本轮主持人）的执行体名：带了专家就把专家 id 并进目录名，
/// 「按 Agent 分目录」在这里成立——不同专家的快照分开存、能分开列。
/// id 单独消毒后再拼前缀：纯中文 id 消毒成 `scope-<hash>`，整串拼起来仍是 `leader-scope-…`，
/// 既不会把两个不同专家撞进同一个目录，也不会丢掉「这一族是负责人」的标记。
pub fn leader_scope(expert_id: Option<&str>) -> String {
    match expert_id.map(str::trim).filter(|s| !s.is_empty()) {
        Some(id) => format!("{DEFAULT_SCOPE}-{}", scope_tag(id)),
        None => DEFAULT_SCOPE.to_string(),
    }
}

/// 团队成员的执行体名：成员 id 参与命名，两个成员跑同一个任务也各存各的。
/// 前缀固定为 `member-`，与负责人那族分开——按任务恢复时只从负责人族里找。
pub fn member_scope(member_id: &str) -> String {
    format!("{MEMBER_PREFIX}-{}", scope_tag(member_id))
}

/// id 部分消毒：空串不能洗成 `leader`（那会把成员的快照写进负责人的家），单独走哈希。
fn scope_tag(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return short_hash(trimmed);
    }
    sanitize_scope(trimmed)
}

/// 是否属于负责人这一族（`leader` 或 `leader-<专家>`）。
/// 按任务恢复时先找这一族，成员目录只在负责人一份都没有时才兜底。
fn is_leader_family(scope: &str) -> bool {
    scope == DEFAULT_SCOPE || scope.starts_with(&format!("{DEFAULT_SCOPE}-"))
}

/// 某个执行体的检查点目录（`checkpoints-harness/agents/<scope>`）。
pub fn dir_in(base: &Path, scope: &str) -> PathBuf {
    base.join(AGENTS_DIR).join(sanitize_scope(scope))
}

/// 某个执行体在某任务下的检查点文件。
pub fn file_in(base: &Path, scope: &str, task_id: &str) -> PathBuf {
    dir_in(base, scope).join(hashed_name(task_id))
}

/// 已经有检查点落盘的执行体目录名（团队页按成员列出、按执行体清理都靠它）。
pub fn scopes(base: &Path) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(base.join(AGENTS_DIR)) else {
        return Vec::new();
    };
    let mut out: Vec<String> = entries
        .flatten()
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    out.sort();
    out
}

/// 该执行体留下的、仍在有效期内的检查点（坏文件与过期文件按不存在处理并清掉）。
pub fn list_in(base: &Path, scope: &str) -> Vec<Checkpoint> {
    let dir = dir_in(base, scope);
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        if let Some(cp) = read_fresh(&path) {
            out.push(cp);
        }
    }
    out.sort_by(|a, b| a.task_id.cmp(&b.task_id));
    out
}

pub fn delete_in(base: &Path, scope: &str, task_id: &str) {
    let _ = std::fs::remove_file(file_in(base, scope, task_id));
    // 负责人的快照在升级前是扁平存放的：那份副本只有这里会清，一起删掉
    if sanitize_scope(scope) == DEFAULT_SCOPE {
        let _ = std::fs::remove_file(file_of(base, task_id));
    }
}

fn delete_path(path: &Path) {
    let _ = std::fs::remove_file(path);
}

/// 校验单个检查点文件：版本/运行时标记不符、过期、损坏一律按不存在处理并清掉坏文件。
/// 不比对 taskId（列表接口按目录整体扫；单任务读取另有 `read_valid`）。
fn read_fresh(path: &Path) -> Option<Checkpoint> {
    let parsed = std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Checkpoint>(&raw).ok());
    let Some(cp) = parsed else {
        delete_path(path);
        return None;
    };
    let fresh = cp.version == CHECKPOINT_VERSION
        && cp.runtime == RUNTIME_TAG
        && cp.saved_at_ms > 0
        && clock::now_ms() - cp.saved_at_ms <= TTL_SECONDS * 1000;
    if !fresh {
        delete_path(path);
        return None;
    }
    Some(cp)
}

fn read_valid(path: &Path, task_id: &str) -> Option<Checkpoint> {
    if !path.exists() {
        return None;
    }
    let cp = read_fresh(path)?;
    if cp.task_id != task_id {
        // 文件名是 taskId 的散列，正常情况下对不上说明文件被挪动过：不认，删掉
        delete_path(path);
        return None;
    }
    Some(cp)
}

pub fn save_in(base: &Path, cp: &Checkpoint) -> Result<(), String> {
    if cp.task_id.trim().is_empty() {
        return Err("检查点 taskId 为空".to_string());
    }
    let mut data = cp.clone();
    data.version = CHECKPOINT_VERSION;
    data.runtime = RUNTIME_TAG.to_string();
    // 落盘时把归属写实（空串只存在于内存里的新建快照）：文件与目录不一致就没法解释
    data.agent_scope = cp.scope();
    data.saved_at_ms = clock::now_ms();
    let encoded = serde_json::to_string(&data)
        .map_err(|e| format!("检查点序列化失败：{e}"))?;
    if encoded.len() > MAX_DISK_CHARS {
        return Err(format!(
            "检查点体积 {} 字符超过磁盘上限 {}",
            encoded.len(),
            MAX_DISK_CHARS
        ));
    }
    let dir = dir_in(base, &cp.scope());
    std::fs::create_dir_all(&dir).map_err(|e| format!("检查点目录创建失败：{e}"))?;
    let path = dir.join(hashed_name(&cp.task_id));
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    std::fs::write(&tmp, encoded).map_err(|e| format!("检查点写入失败：{e}"))?;
    std::fs::rename(&tmp, &path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("检查点落盘失败：{e}")
    })
}

/// 只取某个执行体的检查点：团队成员续跑时用自己的 scope，绝不会拿到别人的快照。
pub fn load_scoped(base: &Path, scope: &str, task_id: &str) -> Option<Checkpoint> {
    if task_id.trim().is_empty() {
        return None;
    }
    read_valid(&file_in(base, scope, task_id), task_id)
}

/// 按任务读检查点：先扫各执行体目录，再回落到升级前的扁平位置。
/// 命中旧位置时按快照自己的执行体搬进对应目录（一次迁移，搬不动也不影响本轮读取）。
pub fn load_in(base: &Path, task_id: &str) -> Option<Checkpoint> {
    if task_id.trim().is_empty() {
        return None;
    }
    let name = hashed_name(task_id);
    // 先扫负责人那族（leader / leader-<专家>），再扫成员目录；同族内按名字稳定排序。
    // 不能只按目录名字典序排：中文专家 id 会消毒成 `scope-<hash>`，可能排到 `member-…` 后面，
    // 那样负责人续跑时会拿到某个成员的进度——那是另一个人的上下文。
    let mut found: Vec<String> = scopes(base);
    found.sort();
    // 稳定排序：先按「是不是负责人族」分两层，同层内仍按目录名字序，保证跨机器顺序一致
    found.sort_by(|a, b| is_leader_family(b).cmp(&is_leader_family(a)));
    let mut dirs: Vec<PathBuf> = found.iter().map(|s| dir_in(base, s)).collect();
    let leader = dir_in(base, DEFAULT_SCOPE);
    if !dirs.contains(&leader) {
        dirs.insert(0, leader);
    }
    for dir in dirs {
        if let Some(cp) = read_valid(&dir.join(&name), task_id) {
            return Some(cp);
        }
    }
    let legacy = file_of(base, task_id);
    let cp = read_valid(&legacy, task_id)?;
    let mut migrated = cp;
    if migrated.agent_scope.trim().is_empty() {
        migrated.agent_scope = DEFAULT_SCOPE.to_string();
    }
    // 一次迁移：按快照自己的归属重写进执行体目录，成功后才清旧位置。
    // 写失败（目录不可用、体积超限等）就留着旧文件，下轮还能读到。
    if file_in(base, &migrated.scope(), task_id) != legacy {
        if save_in(base, &migrated).is_ok() {
            delete_path(&legacy);
        }
    }
    Some(migrated)
}

pub fn load(task_id: &str) -> Option<Checkpoint> {
    load_in(&home(), task_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_base(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tt-harness-cp-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn msgs(n: usize) -> Vec<Value> {
        (0..n)
            .map(|i| json!({ "role": if i % 2 == 0 { "user" } else { "assistant" }, "content": format!("m{i}") }))
            .collect()
    }

    #[test]
    fn save_and_load_roundtrip() {
        let base = temp_base("roundtrip");
        let mut cp = Checkpoint::new("task-1");
        cp.meta.goal = "写周报".into();
        cp.capture(&msgs(3));
        cp.mark_final(true, "任务已中断");
        save_in(&base, &cp).unwrap();

        let loaded = load_in(&base, "task-1").unwrap();
        assert_eq!(loaded.runtime, RUNTIME_TAG);
        assert_eq!(loaded.messages.len(), 3);
        assert_eq!(loaded.meta.goal, "写周报");
        assert!(loaded.saved_at_ms > 0);
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn model_provenance_survives_the_disk_roundtrip() {
        let base = temp_base("model");
        let mut cp = Checkpoint::new("task-model");
        cp.meta.model_id = "m1".into();
        cp.meta.model_display = "上一个模型".into();
        cp.capture(&msgs(1));
        cp.mark_final(true, "中断");
        save_in(&base, &cp).unwrap();

        let loaded = load_in(&base, "task-model").unwrap();
        assert_eq!(loaded.meta.model_id, "m1");
        assert_eq!(loaded.public_info()["producedByModel"], "上一个模型");

        // 老检查点没有这两个键：按「不知道由哪个模型产出」处理，不编一个名字出来
        let path = file_in(&base, DEFAULT_SCOPE, "task-model");
        let mut raw: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let meta = raw["meta"].as_object_mut().unwrap();
        meta.remove("modelId");
        meta.remove("modelDisplay");
        std::fs::write(&path, raw.to_string()).unwrap();
        let legacy = load_in(&base, "task-model").unwrap();
        assert!(legacy.meta.model_id.is_empty());
        assert_eq!(legacy.public_info()["producedByModel"], "");
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn other_runtime_or_expired_files_are_removed() {
        let base = temp_base("reject");
        let mut cp = Checkpoint::new("task-x");
        cp.capture(&msgs(1));
        cp.mark_final(true, "中断");
        save_in(&base, &cp).unwrap();

        let path = file_in(&base, DEFAULT_SCOPE, "task-x");
        let mut raw: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        raw["runtime"] = json!("engine");
        std::fs::write(&path, raw.to_string()).unwrap();
        assert!(load_in(&base, "task-x").is_none(), "引擎快照不能被 harness 当成自己的");
        assert!(!path.exists(), "坏标记文件必须清掉");

        let mut cp = Checkpoint::new("task-old");
        cp.capture(&msgs(1));
        cp.mark_final(true, "中断");
        save_in(&base, &cp).unwrap();
        let path = file_in(&base, DEFAULT_SCOPE, "task-old");
        let mut raw: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        raw["savedAtMs"] = json!(clock::now_ms() - (TTL_SECONDS + 60) * 1000);
        std::fs::write(&path, raw.to_string()).unwrap();
        assert!(load_in(&base, "task-old").is_none());
        std::fs::remove_dir_all(&base).ok();
    }

    /// 检查点按执行体分目录：负责人与团队成员跑同一个 taskId 也各存各的，
    /// 谁都能只取到自己那一份（团队续跑要按成员恢复，不能串读）。
    #[test]
    fn checkpoints_are_partitioned_by_agent_scope() {
        let base = temp_base("scopes");
        let task = "task-team";

        let mut leader = Checkpoint::new(task);
        leader.capture(&vec![json!({ "role": "user", "content": "负责人的那份" })]);
        leader.mark_final(true, "中断");
        leader.agent_scope = leader_scope(Some("expert-lead"));
        save_in(&base, &leader).unwrap();

        for member in ["m-1", "m-2"] {
            let mut cp = Checkpoint::new(task);
            cp.agent_scope = member_scope(member);
            cp.capture(&vec![json!({ "role": "user", "content": format!("{member} 的进度") })]);
            cp.mark_final(true, "中断");
            save_in(&base, &cp).unwrap();
        }

        let scopes_found = scopes(&base);
        let mut expected = vec![
            leader_scope(Some("expert-lead")),
            member_scope("m-1"),
            member_scope("m-2"),
        ];
        expected.sort();
        assert_eq!(scopes_found, expected, "每个执行体各自一个目录");

        // 成员只拿得到自己那份
        let one = load_scoped(&base, &member_scope("m-1"), task).unwrap();
        assert!(one.messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("m-1 的进度"));
        assert_eq!(one.scope(), member_scope("m-1"));
        assert_eq!(one.public_info()["agentScope"], member_scope("m-1"));
        assert!(load_scoped(&base, &member_scope("m-2"), task).unwrap().messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("m-2 的进度"));
        assert!(load_scoped(&base, DEFAULT_SCOPE, task).is_none(), "没跑过的执行体不该有快照");
        assert_eq!(list_in(&base, &member_scope("m-1")).len(), 1);

        // 按任务读时先取负责人那一份；负责人删掉后仍能从成员目录找到（同名不同文件）
        let by_task = load_in(&base, task).unwrap();
        assert_eq!(by_task.scope(), leader_scope(Some("expert-lead")));
        delete_in(&base, &leader_scope(Some("expert-lead")), task);
        assert!(
            load_scoped(&base, &member_scope("m-1"), task).is_some(),
            "删负责人那份不能顺带删掉成员的"
        );
        let after = load_in(&base, task).unwrap();
        assert!(
            after.scope() == member_scope("m-1") || after.scope() == member_scope("m-2"),
            "负责人那份没了，就从成员目录里恢复：{}",
            after.scope()
        );
        std::fs::remove_dir_all(&base).ok();
    }

    /// 升级前落盘的扁平检查点：仍读得到，并按它自己的归属搬进执行体目录。
    #[test]
    fn legacy_flat_checkpoint_is_read_and_migrated() {
        let base = temp_base("legacy");
        let mut cp = Checkpoint::new("task-legacy");
        cp.capture(&msgs(2));
        cp.mark_final(true, "中断");
        // 旧版本落盘时也是走 save 那条路写的，时间戳一直在文件里；手工仿造时别漏
        cp.saved_at_ms = clock::now_ms();
        // 手工写成旧布局：扁平位置 + 没有 agentScope 这个键
        let mut raw = serde_json::to_value(&cp).unwrap();
        raw.as_object_mut().unwrap().remove("agentScope");
        std::fs::write(file_of(&base, "task-legacy"), raw.to_string()).unwrap();

        let loaded = load_in(&base, "task-legacy").unwrap();
        assert_eq!(loaded.scope(), DEFAULT_SCOPE, "旧快照没有归属就按负责人处理");
        assert_eq!(loaded.agent_scope, DEFAULT_SCOPE, "迁移时要写实归属，不留空串");
        assert!(
            file_in(&base, DEFAULT_SCOPE, "task-legacy").exists(),
            "读到之后要搬进执行体目录"
        );
        assert!(!file_of(&base, "task-legacy").exists(), "旧位置不该留第二份");
        // 搬完那份必须还能读：只是换了目录、补了归属，内容不能被迁移弄坏
        let again = load_scoped(&base, DEFAULT_SCOPE, "task-legacy").unwrap();
        assert_eq!(again.messages, loaded.messages);
        assert_eq!(again.reason, loaded.reason);
        std::fs::remove_dir_all(&base).ok();
    }

    /// 目录名消毒：注册表 id 带路径、点号或中文都不能把检查点写出根目录。
    #[test]
    fn scope_names_cannot_escape_the_checkpoint_dir() {
        for raw in [
            "../../evil",
            "..\\..\\evil",
            "/abs/path",
            "a/b/c",
            "  ",
            "..",
            "专家一号",
        ] {
            let name = sanitize_scope(raw);
            assert!(!name.contains('/') && !name.contains('\\'), "{raw} → {name}");
            assert!(!name.contains(".."), "{raw} → {name}");
            assert!(!name.is_empty());
            assert!(dir_in(Path::new("base"), raw).starts_with("base"));
        }
        assert_eq!(sanitize_scope("  "), DEFAULT_SCOPE);
        assert_eq!(sanitize_scope(""), DEFAULT_SCOPE);
        // 纯中文 id 也要彼此分开（否则两个专家会共用一个目录）
        assert_ne!(sanitize_scope("专家一号"), sanitize_scope("专家二号"));
        // 大小写与空格归一：同一个 id 的两种写法落进同一个目录
        assert_eq!(sanitize_scope("Expert One"), sanitize_scope("expert-one"));
        // 超长 id：截断 + 完整串短哈希，前缀相同的两个 id 不撞名
        let prefix = "x".repeat(80);
        let a = sanitize_scope(&prefix);
        let b = sanitize_scope(&format!("{prefix}y"));
        assert_ne!(a, b, "{a} / {b}");
        // 截断段 + 「-scope-<8 位哈希>」后缀
        assert!(a.chars().count() <= SCOPE_MAX_CHARS + 15, "{a}");
    }

    /// 中文命名的专家与成员：各占一个目录，且按任务恢复永远先拿负责人那一份。
    #[test]
    fn non_ascii_agent_ids_keep_separate_dirs() {
        let base = temp_base("non-ascii");
        let task = "task-cn";

        let lead = leader_scope(Some("专家一号"));
        let other = leader_scope(Some("专家二号"));
        let member = member_scope("李四");
        assert!(lead.starts_with("leader-") && other.starts_with("leader-"));
        assert_ne!(lead, other, "两个中文命名的专家不能共用一个目录");
        assert_ne!(member_scope("李四"), member_scope("王五"), "成员同理");
        assert!(member.starts_with("member-"), "{member}");
        assert_ne!(member_scope(""), DEFAULT_SCOPE, "空成员 id 也不能写进负责人的家");

        for (scope, text) in [(member.as_str(), "成员的那份"), (lead.as_str(), "负责人那份")] {
            let mut cp = Checkpoint::new(task);
            cp.agent_scope = scope.to_string();
            cp.capture(&vec![json!({ "role": "user", "content": text })]);
            cp.mark_final(true, "中断");
            save_in(&base, &cp).unwrap();
        }

        let by_task = load_in(&base, task).unwrap();
        assert_eq!(by_task.scope(), lead, "按任务读要先落到负责人族，不能拿到成员的进度");
        assert!(by_task.messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("负责人"));
        assert!(load_scoped(&base, &member, task)
            .unwrap()
            .messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("成员"));
        assert!(load_scoped(&base, &other, task).is_none(), "别的专家不该看到这份");
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn normal_finish_drops_snapshot() {
        let mut cp = Checkpoint::new("task-2");
        cp.capture(&msgs(2));
        cp.mark_final(false, "任务已完成");
        assert!(cp.messages.is_empty());
        assert!(!cp.resumable);
        let info = cp.public_info();
        assert_eq!(info["resumable"], false);
        assert_eq!(info["scope"], "disk");
        assert!(info["savedAt"].as_str().unwrap().is_empty());
    }

    #[test]
    fn side_effect_records_skip_only_previous_runs() {
        let mut cp = Checkpoint::new("task-3");
        cp.begin_run();
        let sig = signature_of("write_text_file", &json!({"path":"a.md","content":"x"}));
        cp.record_tool("write_text_file", &sig, "已写入");
        assert!(cp.previous_record(&sig).is_none(), "同一轮内重复写文件必须照常执行");

        cp.mark_final(true, "中断");
        cp.begin_run();
        let prev = cp.previous_record(&sig).unwrap();
        assert_eq!(prev.content, "已写入");
        cp.skip_previous(&sig);
        assert_eq!(cp.skipped_duplicates, 1);
        assert_eq!(cp.public_info()["skippedDuplicates"], 1);
    }

    #[test]
    fn capture_truncates_and_keeps_system_message() {
        let mut cp = Checkpoint::new("task-4");
        let mut items = vec![json!({"role":"system","content":"sys"})];
        for _ in 0..40 {
            items.push(json!({"role":"assistant","content":"字".repeat(20_000)}));
        }
        cp.capture(&items);
        assert_eq!(cp.messages[0]["content"], "sys");
        assert!(cp.dropped_messages > 0, "超限必须从最早消息丢弃并如实计数");
        assert!(serde_json::to_string(&cp.messages).unwrap().len() <= MAX_CHECKPOINT_CHARS);
    }

    #[test]
    fn signature_is_stable_and_argument_sensitive() {
        let a = signature_of("write_text_file", &json!({"path":"a.md","content":"1"}));
        let b = signature_of("write_text_file", &json!({"content":"1","path":"a.md"}));
        let c = signature_of("write_text_file", &json!({"path":"a.md","content":"2"}));
        assert_eq!(a, b, "字段顺序不影响指纹");
        assert_ne!(a, c);
    }

    #[test]
    fn empty_task_id_is_rejected() {
        let base = temp_base("empty");
        let cp = Checkpoint::new(" ");
        assert!(save_in(&base, &cp).unwrap_err().contains("taskId"));
        assert!(load_in(&base, "").is_none());
        std::fs::remove_dir_all(&base).ok();
    }

    /// 一轮模型给出的两个写文件调用：第一个已回结果，第二个还欠着。
    fn two_calls() -> Vec<TurnCall> {
        let a = json!({ "file_path": "a.md", "content": "1" });
        let b = json!({ "file_path": "b.md", "content": "2" });
        vec![
            TurnCall {
                id: "c1".into(),
                tool: "write_text_file".into(),
                arguments: a.to_string(),
                signature: signature_of("write_text_file", &a),
            },
            TurnCall {
                id: "c2".into(),
                tool: "write_text_file".into(),
                arguments: b.to_string(),
                signature: signature_of("write_text_file", &b),
            },
        ]
    }

    fn assistant_with(calls: &[TurnCall]) -> Value {
        json!({
            "role": "assistant",
            "content": "",
            "tool_calls": calls.iter().map(|c| json!({
                "id": c.id,
                "type": "function",
                "function": { "name": c.tool, "arguments": c.arguments },
            })).collect::<Vec<Value>>(),
        })
    }

    #[test]
    fn mid_turn_snapshot_does_not_inflate_model_calls() {
        let mut cp = Checkpoint::new("task-count");
        cp.capture(&msgs(1));
        cp.snapshot(&msgs(3));
        cp.snapshot(&msgs(4));
        assert_eq!(cp.model_calls, 1, "轮次中间落盘不能把模型调用数算多");
        assert_eq!(cp.messages.len(), 4);
        assert!(cp.resumable, "落过快照就具备续跑条件");
    }

    #[test]
    fn resume_turn_only_returns_what_has_no_result() {
        let calls = two_calls();
        let mut cp = Checkpoint::new("task-turn");
        cp.begin_turn(calls.clone());
        let mut items = vec![
            json!({ "role": "system", "content": "sys" }),
            json!({ "role": "user", "content": "写两个文件" }),
            assistant_with(&calls),
        ];
        assert_eq!(cp.resume_turn(&items).unwrap().len(), 2, "一个结果都没拿到");
        items.push(json!({ "role": "tool", "tool_call_id": "c1", "content": "已写入 a.md" }));
        let pending = cp.resume_turn(&items).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].id, "c2", "欠的是第二个，顺序不能乱");

        cp.snapshot(&items);
        assert_eq!(cp.pending_calls().len(), 1, "查询视图与判定同一口径");
        assert_eq!(cp.public_info()["pendingTools"].as_array().unwrap()[0], "write_text_file");
        assert!(
            !serde_json::to_string(&cp.public_info())
                .unwrap()
                .contains("file_path"),
            "对外视图只报工具名，不能带出参数"
        );

        items.push(json!({ "role": "tool", "tool_call_id": "c2", "content": "已写入 b.md" }));
        assert!(cp.resume_turn(&items).unwrap().is_empty(), "结果配齐就不欠了");
    }

    #[test]
    fn resume_turn_uses_marker_arguments_not_the_capped_copy() {
        // 快照里的工具参数会被截断（8000 字符），标记里存的是模型给的那一份：
        // 原地续跑必须用后者，否则等于把模型要做的事偷偷改短。
        let big = json!({ "file_path": "c.md", "content": "字".repeat(9_000) });
        let call = TurnCall {
            id: "c1".into(),
            tool: "write_text_file".into(),
            arguments: big.to_string(),
            signature: signature_of("write_text_file", &big),
        };
        let mut cp = Checkpoint::new("task-turn-big");
        cp.begin_turn(vec![call.clone()]);
        cp.snapshot(&[json!({ "role": "system", "content": "sys" }), assistant_with(&[call])]);
        let pending = cp.pending_calls();
        assert_eq!(pending.len(), 1);
        assert!(pending[0].arguments.chars().count() > 8_000, "续跑用的是完整参数");
        let stored = cp.messages[1]["tool_calls"][0]["function"]["arguments"]
            .as_str()
            .unwrap();
        assert!(stored.len() < pending[0].arguments.len(), "落盘的那份是被截断过的");
        assert!(stored.ends_with("…（已截断）"), "截断必须留下可见标记");
    }

    #[test]
    fn resume_turn_refuses_markers_that_do_not_match() {
        let calls = two_calls();
        let mut cp = Checkpoint::new("task-turn-bad");
        cp.begin_turn(calls.clone());
        // 消息里是另一轮调用：id 对不上就不能原地续跑
        let other = vec![TurnCall {
            id: "z9".into(),
            tool: "view_text_file".into(),
            arguments: "{}".into(),
            signature: signature_of("view_text_file", &json!({})),
        }];
        let items = vec![json!({ "role": "system", "content": "sys" }), assistant_with(&other)];
        assert!(cp.resume_turn(&items).unwrap_err().contains("对不上"));

        // 标记里的参数残缺（文件被半写/手改过）：宁可重问模型
        let broken = TurnCall {
            id: "c1".into(),
            tool: "write_text_file".into(),
            arguments: "{\"file_path\":\"a.md\"".into(),
            signature: calls[0].signature.clone(),
        };
        cp.begin_turn(vec![broken.clone()]);
        let items = vec![json!({ "role": "system", "content": "sys" }), assistant_with(&[broken])];
        assert!(cp.resume_turn(&items).unwrap_err().contains("不完整"));

        // 没有标记：老检查点走原路，交给调用方清理
        cp.clear_turn();
        assert!(cp.resume_turn(&items).unwrap_err().contains("没有未完成工具轮次"));
    }

    #[test]
    fn dangling_tool_calls_are_rewound_and_orphans_dropped() {
        let calls = two_calls();
        let mut items = vec![
            json!({ "role": "system", "content": "sys" }),
            json!({ "role": "user", "content": "写两个文件" }),
            assistant_with(&calls),
            json!({ "role": "tool", "tool_call_id": "c1", "content": "已写入" }),
        ];
        assert_eq!(unpaired_tail(&items), Some(2), "第二个结果还没拿到");
        assert_eq!(rewind_unpaired(&mut items), 2, "半截轮次整段摘掉");
        assert_eq!(items.len(), 2);
        assert_eq!(rewind_unpaired(&mut items), 0, "已经干净就不该再动");

        // 配对齐了就不该误摘：原地续跑成功后的消息原样保留
        let paired = vec![
            json!({ "role": "system", "content": "sys" }),
            assistant_with(&calls),
            json!({ "role": "tool", "tool_call_id": "c1", "content": "已写入" }),
            json!({ "role": "tool", "tool_call_id": "c2", "content": "已写入" }),
        ];
        let mut copy = paired.clone();
        assert_eq!(unpaired_tail(&copy), None);
        assert_eq!(rewind_unpaired(&mut copy), 0, "配齐的轮次一条都不能少");
        assert_eq!(copy.len(), paired.len());

        // 助手消息已被裁剪，只剩孤儿结果：端点同样会拒收
        let mut orphan = vec![
            json!({ "role": "system", "content": "sys" }),
            json!({ "role": "tool", "tool_call_id": "c1", "content": "结果" }),
            json!({ "role": "user", "content": "接着来" }),
        ];
        assert_eq!(drop_orphan_tool_results(&mut orphan), 1);
        assert_eq!(orphan[1]["content"], "接着来", "别的消息不能顺带删掉");
    }

    #[test]
    fn pending_turn_survives_disk_roundtrip_and_final_cleanup() {
        let base = temp_base("turn");
        let calls = two_calls();
        let mut cp = Checkpoint::new("task-turn-disk");
        cp.meta.work_dir = "C:/tmp/whatever".into();
        cp.snapshot(&[json!({ "role": "system", "content": "sys" }), assistant_with(&calls)]);
        cp.begin_turn(calls.clone());
        cp.mark_final(true, "在确认框上退出了应用");
        save_in(&base, &cp).unwrap();

        let loaded = load_in(&base, "task-turn-disk").unwrap();
        assert_eq!(
            loaded.pending_turn.as_ref().unwrap().calls.len(),
            2,
            "标记登记整轮调用，不只欠的那一个"
        );
        assert_eq!(loaded.pending_calls().len(), 2, "重启后要知道这一轮欠哪两个工具");

        let mut done = loaded;
        done.mark_final(false, "任务已完成");
        assert!(done.pending_turn.is_none(), "不续跑的检查点不能留欠工具标记");
        std::fs::remove_dir_all(&base).ok();
    }
}
