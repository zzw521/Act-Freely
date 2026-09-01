//! SSE 事件流解析器 — 移植自 Electron 版 bridge/sse-parser.ts
//!
//! QwenPaw SSE 格式:
//! ```text
//! event: <type>
//! data: <JSON>
//! （空行分隔事件）
//! ```
//!
//! 三层架构演进 P0 扩展（需求分析报告 A-04/B4）：
//! ```text
//! id: {task_id}:{seq}   ← 单调序号，用于断线重放与去重
//! ```
//!
//! - 支持多行 data 累积
//! - JSON 解析失败时回退为 { "raw": ... }
//! - 未知事件类型被忽略
//! - seq 双通道提取：SSE id 行优先，data.seq 兜底

use serde::Serialize;
use serde_json::Value;

/// Agent 原始事件（解析自 SSE 流）
#[derive(Debug, Clone, Serialize)]
pub struct AgentEvent {
    #[serde(rename = "type")]
    pub kind: String,
    pub data: Value,
    /// 单调序号（三层架构演进 P0；旧引擎无 id 行时为 None，契约兼容）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
}

/// 已知事件类型白名单（三层架构演进新增 status_change/quota_warning）
const KNOWN_TYPES: [&str; 10] = [
    "plan",
    "step_start",
    "step_progress",
    "step_done",
    "step_failed",
    "deliver",
    "done",
    "error",
    "status_change",
    "quota_warning",
];

/// 增量式 SSE 解析器：持续 feed 字节块，产出完整事件
#[derive(Debug, Default)]
pub struct SseFeed {
    buffer: String,
}

impl SseFeed {
    pub fn new() -> Self {
        Self { buffer: String::new() }
    }

    /// 输入一段数据，返回其中完整解析出的事件
    pub fn feed(&mut self, chunk: &str) -> Vec<AgentEvent> {
        self.buffer.push_str(chunk);
        let mut events = Vec::new();

        // 事件以空行（\n\n）分隔；最后一段可能不完整，保留在 buffer
        let mut parts: Vec<&str> = self.buffer.split("\n\n").collect();
        let last = parts.pop().unwrap_or("").to_string();

        for part in parts {
            if let Some(event) = parse_block(part) {
                events.push(event);
            }
        }

        self.buffer = last;
        events
    }

    /// 流结束时处理残留 buffer
    pub fn finish(&mut self) -> Vec<AgentEvent> {
        let rest = std::mem::take(&mut self.buffer);
        if rest.trim().is_empty() {
            return Vec::new();
        }
        parse_block(&rest).into_iter().collect()
    }
}

/// 解析单个事件块（event:/data:/id: 行）
fn parse_block(raw: &str) -> Option<AgentEvent> {
    let mut event_type = String::new();
    let mut event_data = String::new();
    let mut event_id = String::new();

    for line in raw.lines() {
        if let Some(rest) = line.strip_prefix("event:") {
            event_type = rest.trim().to_string();
        } else if let Some(rest) = line.strip_prefix("data:") {
            // 多行 data 累积
            if !event_data.is_empty() {
                event_data.push('\n');
            }
            event_data.push_str(rest.trim());
        } else if let Some(rest) = line.strip_prefix("id:") {
            // 三层架构演进 P0：捕获 id 行提取 seq（格式 `{task_id}:{seq}` 或纯数字）
            event_id = rest.trim().to_string();
        }
        // 忽略 retry:/注释行（`: keepalive` 心跳不产出事件）
    }

    if event_type.is_empty() && event_data.is_empty() {
        return None;
    }

    // JSON 解析，失败回退 raw
    let mut data: Value = if event_data.is_empty() {
        Value::Object(Default::default())
    } else {
        serde_json::from_str(&event_data).unwrap_or_else(|_| {
            serde_json::json!({ "raw": event_data })
        })
    };

    let kind = map_event_type(&event_type, &data)?;

    // seq 双通道提取：SSE id 行优先（取最后一个 ':' 后数字），data.seq 兜底。
    // 以 tt-enable-seq feature 包裹（计划 D1 回滚保险）：关闭时 seq 恒为 None，
    // 回退旧引擎语义，契约兼容。
    #[cfg(feature = "tt-enable-seq")]
    let seq: Option<u64> =
        seq_from_id(&event_id).or_else(|| data.get("seq").and_then(|v| v.as_u64()));
    #[cfg(not(feature = "tt-enable-seq"))]
    let seq: Option<u64> = {
        let _ = &event_id;
        None
    };
    // 统一把 seq 写回 data，供下游适配层单通道读取
    if let Some(s) = seq {
        if let Some(obj) = data.as_object_mut() {
            obj.insert("seq".to_string(), Value::from(s));
        }
    }

    Some(AgentEvent { kind, data, seq })
}

/// 从 SSE id 行提取 seq：支持 `{task_id}:{seq}` 与纯数字两种格式
#[cfg(feature = "tt-enable-seq")]
fn seq_from_id(id: &str) -> Option<u64> {
    if id.is_empty() {
        return None;
    }
    let tail = id.rsplit(':').next().unwrap_or(id);
    tail.parse::<u64>().ok()
}

/// 事件类型映射：优先 event 行，缺失时从 data.type 推断
fn map_event_type(raw_type: &str, data: &Value) -> Option<String> {
    if KNOWN_TYPES.contains(&raw_type) {
        return Some(raw_type.to_string());
    }
    if raw_type.is_empty() {
        if let Some(t) = data.get("type").and_then(|v| v.as_str()) {
            if KNOWN_TYPES.contains(&t) {
                return Some(t.to_string());
            }
        }
    }
    None
}

/// 将 Agent 事件适配为前端 ChatEvent 结构（移植自 event-adapter.ts）
/// 三层架构演进 P0：透传 seq/taskId 字段（契约兼容，前端按 seq 去重排序）
pub fn adapt_to_chat_event(event: &AgentEvent, task_id: &str) -> Option<Value> {
    let d = &event.data;
    let str_field = |keys: &[&str]| -> String {
        for k in keys {
            if let Some(v) = d.get(k).and_then(|v| v.as_str()) {
                return v.to_string();
            }
        }
        String::new()
    };

    // 统一注入 seq/taskId（存在时）
    let with_meta = |mut v: Value| -> Value {
        if let Some(obj) = v.as_object_mut() {
            if let Some(s) = event.seq {
                obj.insert("seq".to_string(), Value::from(s));
            }
            if !obj.contains_key("taskId") {
                obj.insert("taskId".to_string(), Value::from(task_id));
            }
        }
        v
    };

    match event.kind.as_str() {
        "plan" => {
            let plan_id = str_field(&["planId", "plan_id"]);
            let steps: Vec<Value> = d
                .get("steps")
                .and_then(|s| s.as_array())
                .map(|arr| {
                    arr.iter()
                        .enumerate()
                        .map(|(i, s)| {
                            let id = s
                                .get("id")
                                .or_else(|| s.get("step_id"))
                                .and_then(|v| v.as_str())
                                .map(|s| s.to_string())
                                .unwrap_or_else(|| format!("step-{i}"));
                            let stype = s
                                .get("type")
                                .and_then(|v| v.as_str())
                                .unwrap_or("llm_call");
                            let description = s
                                .get("description")
                                .and_then(|v| v.as_str())
                                .unwrap_or("");
                            serde_json::json!({
                                "id": id,
                                "type": stype,
                                "description": description,
                            })
                        })
                        .collect()
                })
                .unwrap_or_default();
            Some(with_meta(serde_json::json!({ "type": "plan", "planId": plan_id, "steps": steps })))
        }
        "step_start" => Some(with_meta(serde_json::json!({
            "type": "step_start",
            "stepId": str_field(&["stepId", "step_id"]),
            "description": str_field(&["description"]),
        }))),
        "step_progress" => Some(with_meta(serde_json::json!({
            "type": "step_progress",
            "stepId": str_field(&["stepId", "step_id"]),
            "message": str_field(&["message"]),
        }))),
        "step_done" => Some(with_meta(serde_json::json!({
            "type": "step_done",
            "stepId": str_field(&["stepId", "step_id"]),
            "output": str_field(&["output"]),
        }))),
        "step_failed" => Some(with_meta(serde_json::json!({
            "type": "step_failed",
            "stepId": str_field(&["stepId", "step_id"]),
            "error": str_field(&["error"]),
        }))),
        "deliver" => {
            let artifacts: Vec<String> = d
                .get("artifacts")
                .and_then(|a| a.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            Some(with_meta(serde_json::json!({
                "type": "deliver",
                "taskId": task_id,
                "summary": str_field(&["summary"]),
                "artifacts": artifacts,
            })))
        }
        "done" => Some(with_meta(serde_json::json!({ "type": "done", "taskId": task_id }))),
        "error" => Some(with_meta(serde_json::json!({
            "type": "error",
            "code": str_field(&["code"]),
            "message": str_field(&["message"]),
        }))),
        // ── 三层架构演进新增事件 ──
        "status_change" => Some(with_meta(serde_json::json!({
            "type": "status_change",
            "taskId": str_field(&["taskId", "task_id"]),
            "status": str_field(&["status"]),
            "waitReason": d.get("waitReason").or_else(|| d.get("wait_reason")).cloned().unwrap_or(Value::Null),
            "statusReasonCode": str_field(&["statusReasonCode", "status_reason_code"]),
        }))),
        "quota_warning" => Some(with_meta(serde_json::json!({
            "type": "quota_warning",
            "level": str_field(&["level"]),
            "action": str_field(&["action"]),
            "usedTokens": d.get("usedTokens").or_else(|| d.get("used_tokens")).and_then(|v| v.as_u64()).unwrap_or(0),
            "quotaTokens": d.get("quotaTokens").or_else(|| d.get("quota_tokens")).and_then(|v| v.as_u64()).unwrap_or(0),
        }))),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk(s: &str) -> Vec<AgentEvent> {
        let mut feed = SseFeed::new();
        feed.feed(s)
    }

    #[test]
    fn test_parse_single_event() {
        let events = chunk("event: done\ndata: {}\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "done");
    }

    #[test]
    fn test_parse_multiple_events() {
        let events = chunk("event: step_start\ndata: {\"description\":\"a\"}\n\nevent: done\ndata: {}\n\n");
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].kind, "step_start");
        assert_eq!(events[1].kind, "done");
    }

    #[test]
    fn test_incomplete_buffer_waits() {
        let mut feed = SseFeed::new();
        assert!(feed.feed("event: done\n").is_empty());
        let events = feed.feed("data: {}\n\n");
        assert_eq!(events.len(), 1);
    }

    #[test]
    fn test_multiline_data() {
        let events = chunk("event: step_progress\ndata: {\"message\":\ndata: \"hello\"}\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].data["message"], "hello");
    }

    #[test]
    fn test_invalid_json_fallback_raw() {
        let events = chunk("event: step_progress\ndata: not-json\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].data["raw"], "not-json");
    }

    #[test]
    fn test_unknown_type_ignored() {
        let events = chunk("event: heartbeat\ndata: {}\n\n");
        assert!(events.is_empty());
    }

    #[test]
    fn test_type_inferred_from_data() {
        let events = chunk("data: {\"type\":\"done\"}\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "done");
    }

    #[test]
    fn test_finish_processes_remainder() {
        let mut feed = SseFeed::new();
        feed.feed("event: done\ndata: {}");
        let events = feed.finish();
        assert_eq!(events.len(), 1);
    }

    #[test]
    fn test_adapt_plan_event_snake_case() {
        let event = AgentEvent {
            kind: "plan".into(),
            data: serde_json::json!({
                "plan_id": "p-1",
                "steps": [{ "step_id": "s1", "type": "file_read", "description": "读文件" }]
            }),
            seq: None,
        };
        let chat = adapt_to_chat_event(&event, "t-1").unwrap();
        assert_eq!(chat["type"], "plan");
        assert_eq!(chat["planId"], "p-1");
        assert_eq!(chat["steps"][0]["id"], "s1");
        assert_eq!(chat["steps"][0]["description"], "读文件");
    }

    #[test]
    fn test_adapt_deliver_event() {
        let event = AgentEvent {
            kind: "deliver".into(),
            data: serde_json::json!({ "artifacts": ["a.md", "b.xlsx"], "summary": "完成" }),
            seq: None,
        };
        let chat = adapt_to_chat_event(&event, "t-9").unwrap();
        assert_eq!(chat["taskId"], "t-9");
        assert_eq!(chat["artifacts"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn test_adapt_done_and_error() {
        let done = AgentEvent { kind: "done".into(), data: serde_json::json!({}), seq: None };
        assert_eq!(adapt_to_chat_event(&done, "t").unwrap()["taskId"], "t");

        let err = AgentEvent {
            kind: "error".into(),
            data: serde_json::json!({ "code": "E1", "message": "boom" }),
            seq: None,
        };
        let chat = adapt_to_chat_event(&err, "t").unwrap();
        assert_eq!(chat["code"], "E1");
        assert_eq!(chat["message"], "boom");
    }

    // ── 三层架构演进 P0：seq / id 行 / 新事件类型（tt-enable-seq feature 下生效） ──

    #[test]
    #[cfg(feature = "tt-enable-seq")]
    fn test_parse_id_line_extracts_seq() {
        let events = chunk("id: task-1:42\nevent: step_done\ndata: {}\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].seq, Some(42));
        // seq 已写回 data 供下游单通道读取
        assert_eq!(events[0].data["seq"], 42);
    }

    #[test]
    #[cfg(feature = "tt-enable-seq")]
    fn test_parse_seq_fallback_from_data() {
        // 旧引擎无 id 行但 data 内携带 seq（双通道兜底）
        let events = chunk("event: done\ndata: {\"seq\":7}\n\n");
        assert_eq!(events[0].seq, Some(7));
    }

    #[test]
    #[cfg(not(feature = "tt-enable-seq"))]
    fn test_seq_disabled_is_none() {
        // feature 关闭：即使引擎发送 id 行，seq 仍为 None（回滚保险验证）
        let events = chunk("id: task-1:42\nevent: done\ndata: {}\n\n");
        assert_eq!(events[0].seq, None);
    }

    #[test]
    fn test_parse_without_seq_is_none() {
        let events = chunk("event: done\ndata: {}\n\n");
        assert_eq!(events[0].seq, None);
    }

    #[test]
    fn test_keepalive_comment_ignored() {
        // `: keepalive` 注释行不产出事件
        let events = chunk(": keepalive\n\nevent: done\ndata: {}\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "done");
    }

    #[test]
    fn test_adapt_status_change_event() {
        let event = AgentEvent {
            kind: "status_change".into(),
            data: serde_json::json!({
                "taskId": "t-1",
                "status": "cancelled",
                "statusReasonCode": "USER_CANCELLED"
            }),
            seq: Some(9),
        };
        let chat = adapt_to_chat_event(&event, "t-1").unwrap();
        assert_eq!(chat["status"], "cancelled");
        assert_eq!(chat["seq"], 9);
    }

    #[test]
    fn test_adapt_quota_warning_event() {
        let event = AgentEvent {
            kind: "quota_warning".into(),
            data: serde_json::json!({
                "level": "100",
                "action": "block",
                "used_tokens": 1000,
                "quota_tokens": 1000
            }),
            seq: Some(3),
        };
        let chat = adapt_to_chat_event(&event, "t-1").unwrap();
        assert_eq!(chat["level"], "100");
        assert_eq!(chat["usedTokens"], 1000);
    }

    #[test]
    fn test_adapt_injects_seq_to_legacy_events() {
        let event = AgentEvent {
            kind: "step_start".into(),
            data: serde_json::json!({ "step_id": "s1", "description": "x" }),
            seq: Some(5),
        };
        let chat = adapt_to_chat_event(&event, "t").unwrap();
        assert_eq!(chat["seq"], 5);
        assert_eq!(chat["taskId"], "t");
    }

    /// 黄金 SSE fixture（流 H3）：engine/server.py 与 sse.rs 对 id 行的理解必须一致。
    /// 与 scripts/mock-agent.py 输出格式保持同步（`id: {task_id}:{seq}`）。
    #[test]
    #[cfg(feature = "tt-enable-seq")]
    fn test_golden_sse_fixture() {
        let golden = concat!(
            "id: task-1:1\n",
            "event: step_start\n",
            "data: {\"step_id\": \"step-1\", \"description\": \"分析\", \"seq\": 1, \"taskId\": \"task-1\"}\n",
            "\n",
            "id: task-1:2\n",
            "event: done\n",
            "data: {\"seq\": 2, \"taskId\": \"task-1\"}\n",
            "\n",
        );
        let events = chunk(golden);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].kind, "step_start");
        assert_eq!(events[0].seq, Some(1));
        assert_eq!(events[1].kind, "done");
        assert_eq!(events[1].seq, Some(2));
        // seq 单调递增
        assert!(events[1].seq.unwrap() > events[0].seq.unwrap());
    }
}
