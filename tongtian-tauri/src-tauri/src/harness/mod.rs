//! 客户端内嵌 harness：模型路由、工具、技能与对话检查点都在 Tauri 进程内完成。
//!
//! 定位与边界：本地 harness 承担「计划 → 审批 → 执行」的完整 Agent 循环，
//! 处理文件读写类任务；需要连接器、浏览器、电脑操控、知识库、专家、幻灯片、
//! Hooks/审批/出站脱敏/系统沙箱的能力仍交给 Python 引擎。门控理由必须可见：
//! 不能因为静默降级让用户以为某项能力已经生效。

pub mod checkpoint;
pub mod clock;
pub mod model;
pub mod prompt;
pub mod runner;
pub mod tools;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::harness::checkpoint::Checkpoint;
use crate::harness::prompt::PlanStep;

/// 单轮工具循环上限的兜底（runtime.agentPolicy.maxIters 缺省时使用）
pub const DEFAULT_MAX_ITERS: u32 = 8;
pub const MAX_ITERS_LIMIT: u32 = 32;

/// 任务在一个 harness 进程内的全部状态（计划、检查点、取消标记）
pub struct Slot {
    pub task_id: String,
    pub goal: String,
    pub mode: String,
    pub work_dir: PathBuf,
    pub cancelled: AtomicBool,
    pub inner: Mutex<SlotInner>,
}

pub struct SlotInner {
    pub model_config: Value,
    pub runtime: Value,
    pub history: Vec<Value>,
    pub attachments: Vec<Value>,
    pub privacy: bool,
    pub plan: Vec<PlanStep>,
    pub checkpoint: Checkpoint,
}

impl Slot {
    pub fn new(
        task_id: &str,
        goal: &str,
        mode: &str,
        work_dir: PathBuf,
        model_config: Value,
        runtime: Value,
        history: Vec<Value>,
        attachments: Vec<Value>,
        privacy: bool,
    ) -> Arc<Self> {
        Arc::new(Self {
            task_id: task_id.to_string(),
            goal: goal.to_string(),
            mode: mode.to_string(),
            work_dir,
            cancelled: AtomicBool::new(false),
            inner: Mutex::new(SlotInner {
                model_config,
                runtime,
                history,
                attachments,
                privacy,
                plan: Vec::new(),
                checkpoint: Checkpoint::new(task_id),
            }),
        })
    }

    pub fn cancel(&self) {
        self.cancelled
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// 取一份执行快照（锁内只做克隆，绝不跨 await 持有）
    pub fn snapshot(&self) -> Snapshot {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        Snapshot {
            goal: self.goal.clone(),
            mode: self.mode.clone(),
            work_dir: self.work_dir.clone(),
            model_config: inner.model_config.clone(),
            runtime: inner.runtime.clone(),
            history: inner.history.clone(),
            attachments: inner.attachments.clone(),
            privacy: inner.privacy,
            plan: inner.plan.clone(),
            checkpoint: inner.checkpoint.clone(),
        }
    }

    pub fn update<T>(&self, f: impl FnOnce(&mut SlotInner) -> T) -> T {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        f(&mut inner)
    }
}

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub goal: String,
    pub mode: String,
    pub work_dir: PathBuf,
    pub model_config: Value,
    pub runtime: Value,
    pub history: Vec<Value>,
    pub attachments: Vec<Value>,
    pub privacy: bool,
    pub plan: Vec<PlanStep>,
    pub checkpoint: Checkpoint,
}

/// 进程内任务表：提交 → 计划 → 审批 → 执行之间保留任务状态
#[derive(Default)]
pub struct Hub {
    tasks: Mutex<HashMap<String, Arc<Slot>>>,
}

impl Hub {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&self, slot: Arc<Slot>) {
        if let Ok(mut map) = self.tasks.lock() {
            map.insert(slot.task_id.clone(), slot);
        }
    }

    pub fn get(&self, task_id: &str) -> Option<Arc<Slot>> {
        self.tasks.lock().ok()?.get(task_id).cloned()
    }

    pub fn remove(&self, task_id: &str) -> Option<Arc<Slot>> {
        self.tasks.lock().ok()?.remove(task_id)
    }

    /// 取消请求：任务必须在当前进程内；返回是否找到任务
    pub fn cancel(&self, task_id: &str) -> bool {
        match self.get(task_id) {
            Some(slot) => {
                slot.cancel();
                true
            }
            None => false,
        }
    }

}

/// 门控输入：只看「本轮请求本身」与「已配置的能力事实」
pub struct GateRequest<'a> {
    pub mode: &'a str,
    pub expert_id: Option<&'a str>,
    pub attachments: &'a [Value],
    pub runtime: &'a Value,
    pub secure_env: bool,
}

/// 返回 `Some(原因)` 表示本轮必须交给引擎；`None` 表示本地 harness 可承接。
pub fn engine_required(req: &GateRequest<'_>) -> Option<String> {
    if req.mode == "slides" {
        return Some("slides 模式需要引擎的 PPTX 模板与渲染工具".to_string());
    }
    if req.expert_id.map(|s| !s.trim().is_empty()).unwrap_or(false) {
        return Some("本轮指定了负责人专家，专家提示词与模型绑定由引擎执行".to_string());
    }
    if req.attachments.iter().any(|a| a.get("image").is_some()) {
        return Some("本轮包含图片附件，需要引擎的视觉通道".to_string());
    }
    if req.secure_env {
        return Some("安全工作环境（secureEnv）由引擎的隔离目录与沙箱执行".to_string());
    }
    let rt = req.runtime;
    let block_error = |key: &str| -> bool {
        rt.get(key)
            .and_then(|b| b.get("error"))
            .and_then(Value::as_str)
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false)
    };
    // 配置读不出来时无法确认能力状态：交给引擎按 fail-closed 处理，不能猜成「没配」
    if block_error("mcp") {
        return Some("连接器配置读取失败，无法确认本轮是否需要连接器".to_string());
    }
    if block_error("privacy") {
        return Some("出站脱敏配置读取失败，本轮由引擎按 fail-closed 执行".to_string());
    }
    let arr_len = |key: &str| rt.get(key).and_then(Value::as_array).map(|a| a.len()).unwrap_or(0);
    if arr_len("mcp") > 0 {
        return Some("本轮已连接 MCP 连接器，harness 不含连接器工具".to_string());
    }
    let bases = rt
        .get("knowledge")
        .and_then(|k| k.get("bases"))
        .and_then(Value::as_array)
        .map(|a| a.len())
        .unwrap_or(0);
    if bases > 0 {
        return Some("本轮启用了知识库，检索工具由引擎提供".to_string());
    }
    let hooks = rt
        .get("hooks")
        .and_then(|h| h.get("configured"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if hooks > 0 {
        return Some("已配置 Hooks，必须由引擎在工具触发点执行，避免静默绕过".to_string());
    }
    let approval = rt
        .get("toolApproval")
        .and_then(|t| t.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("off");
    if approval != "off" {
        return Some("已开启逐工具审批，harness 不含审批网关".to_string());
    }
    let pii = rt
        .get("privacy")
        .and_then(|p| p.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("off");
    if pii != "off" {
        return Some("出站脱敏档位不是 off，本轮由引擎执行以免未脱敏发送".to_string());
    }
    let acp = rt
        .get("acp")
        .and_then(|a| a.get("enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if acp {
        return Some("ACP 委派已启用，外部 Coding Agent 由引擎拉起".to_string());
    }
    None
}

/// 单轮工具循环上限：与 Engine 的 agentPolicy 同一口径（1..=32，默认 8）
pub fn max_iters(runtime: &Value) -> u32 {
    runtime
        .get("agentPolicy")
        .and_then(|p| p.get("maxIters"))
        .and_then(Value::as_u64)
        .map(|n| n.clamp(1, MAX_ITERS_LIMIT as u64) as u32)
        .unwrap_or(DEFAULT_MAX_ITERS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn base_runtime() -> Value {
        json!({
            "mcp": [],
            "knowledge": { "bases": [] },
            "hooks": { "configured": 0 },
            "toolApproval": { "mode": "off" },
            "privacy": { "mode": "off" },
            "acp": { "enabled": false },
        })
    }

    fn gate<'a>(mode: &'a str, attachments: &'a [Value], runtime: &'a Value) -> Option<String> {
        engine_required(&GateRequest {
            mode,
            expert_id: None,
            attachments,
            runtime,
            secure_env: false,
        })
    }

    #[test]
    fn plain_general_task_runs_in_harness() {
        assert!(gate("general", &[], &base_runtime()).is_none());
        assert!(gate("design", &[], &base_runtime()).is_none());
    }

    #[test]
    fn engine_only_capabilities_are_routed_with_reason() {
        assert!(gate("slides", &[], &base_runtime()).unwrap().contains("slides"));
        let img = vec![json!({ "fileName": "a.png", "image": { "data": "xx" } })];
        assert!(gate("general", &img, &base_runtime()).unwrap().contains("图片"));
        let mut rt = base_runtime();
        rt["mcp"] = json!([{ "serverId": "s1" }]);
        assert!(gate("general", &[], &rt).unwrap().contains("MCP"));
        let mut rt = base_runtime();
        rt["knowledge"]["bases"] = json!([{ "id": "kb" }]);
        assert!(gate("general", &[], &rt).unwrap().contains("知识库"));
        let mut rt = base_runtime();
        rt["hooks"]["configured"] = json!(2);
        assert!(gate("general", &[], &rt).unwrap().contains("Hooks"));
        let mut rt = base_runtime();
        rt["toolApproval"]["mode"] = json!("sensitive");
        assert!(gate("general", &[], &rt).unwrap().contains("审批"));
        let mut rt = base_runtime();
        rt["privacy"]["mode"] = json!("mask");
        assert!(gate("general", &[], &rt).unwrap().contains("脱敏"));
        let mut rt = base_runtime();
        rt["acp"]["enabled"] = json!(true);
        assert!(gate("general", &[], &rt).unwrap().contains("ACP"));
    }

    #[test]
    fn text_attachments_and_secure_env() {
        let text = vec![json!({ "fileName": "a.md", "text": "正文" })];
        assert!(gate("general", &text, &base_runtime()).is_none());
        let reason = engine_required(&GateRequest {
            mode: "general",
            expert_id: Some("finance"),
            attachments: &[],
            runtime: &base_runtime(),
            secure_env: false,
        });
        assert!(reason.unwrap().contains("专家"));
        let reason = engine_required(&GateRequest {
            mode: "general",
            expert_id: None,
            attachments: &[],
            runtime: &base_runtime(),
            secure_env: true,
        });
        assert!(reason.unwrap().contains("secureEnv"));
    }

    #[test]
    fn unreadable_capability_config_fails_closed() {
        let mut rt = base_runtime();
        rt["mcp"] = json!({ "error": "database locked" });
        assert!(gate("general", &[], &rt).unwrap().contains("连接器"));
        let mut rt = base_runtime();
        rt["privacy"] = json!({ "mode": "off", "error": "database locked" });
        assert!(gate("general", &[], &rt).unwrap().contains("脱敏"));
    }

    #[test]
    fn max_iters_is_clamped() {
        assert_eq!(max_iters(&json!({})), DEFAULT_MAX_ITERS);
        assert_eq!(max_iters(&json!({ "agentPolicy": { "maxIters": 16 } })), 16);
        assert_eq!(max_iters(&json!({ "agentPolicy": { "maxIters": 99 } })), MAX_ITERS_LIMIT);
        assert_eq!(max_iters(&json!({ "agentPolicy": { "maxIters": 0 } })), 1);
    }

    #[test]
    fn hub_cancel_and_remove() {
        let hub = Hub::new();
        let slot = Slot::new(
            "t1",
            "目标",
            "general",
            std::env::temp_dir(),
            json!({}),
            json!({}),
            Vec::new(),
            Vec::new(),
            false,
        );
        hub.insert(slot.clone());
        assert!(hub.cancel("t1"));
        assert!(slot.is_cancelled());
        assert!(hub.remove("t1").is_some());
        assert!(hub.get("t1").is_none());
        assert!(!hub.cancel("t1"));
    }
}
