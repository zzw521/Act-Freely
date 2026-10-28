//! 本地 harness 的对话检查点：快照 + 副作用台账落盘，支持刷新/重启后续跑。
//!
//! 与引擎检查点分目录存放（`checkpoints-harness`），并在文件里带 `runtime` 标记，
//! 避免两套运行时互相把对方的快照当成自己的恢复起点。文件里只有恢复所需的
//! 对话与台账，不含模型密钥；隐私模式不落盘。

use std::collections::BTreeMap;
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
    #[serde(default)]
    pub capture_error: String,
    /// 执行前文件基线（相对路径 → "size:mtime"），恢复时沿用，产物才算得全
    #[serde(default)]
    pub baseline_files: BTreeMap<String, String>,
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
            capture_error: String::new(),
            baseline_files: BTreeMap::new(),
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
            return;
        }
        self.messages = items;
        self.dropped_messages = dropped;
        self.model_calls += 1;
        self.capture_error.clear();
        self.resumable = true;
        self.reason = "执行中断后可从此检查点继续".to_string();
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
        }
    }

    /// 原子写入磁盘；失败返回中文原因，不影响任务收尾。
    /// 落盘并同步内存里的 savedAt（检查点事件的 savedAt 显示用）
    pub fn persist(&mut self) -> Result<(), String> {
        self.saved_at_ms = clock::now_ms();
        save_in(&home(), self)
    }

    pub fn delete(&self) {
        delete_in(&home(), &self.task_id)
    }

    /// 查询接口的对外视图：只报告状态与计数，不返回对话内容/工具参数。
    pub fn public_info(&self) -> Value {
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
            "captureError": self.capture_error,
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
    let mut hasher = Sha256::new();
    hasher.update(task_id.as_bytes());
    base.join(format!("{:x}.json", hasher.finalize()))
}

pub fn delete_in(base: &Path, task_id: &str) {
    let _ = std::fs::remove_file(file_of(base, task_id));
}

pub fn save_in(base: &Path, cp: &Checkpoint) -> Result<(), String> {
    if cp.task_id.trim().is_empty() {
        return Err("检查点 taskId 为空".to_string());
    }
    let mut data = cp.clone();
    data.version = CHECKPOINT_VERSION;
    data.runtime = RUNTIME_TAG.to_string();
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
    std::fs::create_dir_all(base).map_err(|e| format!("检查点目录创建失败：{e}"))?;
    let path = file_of(base, &cp.task_id);
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    std::fs::write(&tmp, encoded).map_err(|e| format!("检查点写入失败：{e}"))?;
    std::fs::rename(&tmp, &path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("检查点落盘失败：{e}")
    })
}

/// 读取磁盘检查点；版本/运行时标记不符、过期、损坏一律按不存在处理并清掉坏文件。
pub fn load_in(base: &Path, task_id: &str) -> Option<Checkpoint> {
    if task_id.trim().is_empty() {
        return None;
    }
    let path = file_of(base, task_id);
    if !path.exists() {
        return None;
    }
    let parsed = std::fs::read_to_string(&path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Checkpoint>(&raw).ok());
    let Some(cp) = parsed else {
        delete_in(base, task_id);
        return None;
    };
    let fresh = cp.task_id == task_id
        && cp.version == CHECKPOINT_VERSION
        && cp.runtime == RUNTIME_TAG
        && cp.saved_at_ms > 0
        && clock::now_ms() - cp.saved_at_ms <= TTL_SECONDS * 1000;
    if !fresh {
        delete_in(base, task_id);
        return None;
    }
    Some(cp)
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
    fn other_runtime_or_expired_files_are_removed() {
        let base = temp_base("reject");
        let mut cp = Checkpoint::new("task-x");
        cp.capture(&msgs(1));
        cp.mark_final(true, "中断");
        save_in(&base, &cp).unwrap();

        let path = file_of(&base, "task-x");
        let mut raw: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        raw["runtime"] = json!("engine");
        std::fs::write(&path, raw.to_string()).unwrap();
        assert!(load_in(&base, "task-x").is_none(), "引擎快照不能被 harness 当成自己的");
        assert!(!path.exists(), "坏标记文件必须清掉");

        let mut cp = Checkpoint::new("task-old");
        cp.capture(&msgs(1));
        cp.mark_final(true, "中断");
        save_in(&base, &cp).unwrap();
        let path = file_of(&base, "task-old");
        let mut raw: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        raw["savedAtMs"] = json!(clock::now_ms() - (TTL_SECONDS + 60) * 1000);
        std::fs::write(&path, raw.to_string()).unwrap();
        assert!(load_in(&base, "task-old").is_none());
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
}
