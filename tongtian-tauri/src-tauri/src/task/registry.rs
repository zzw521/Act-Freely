//! 任务注册表（流 B2，三层架构演进 P0）
//!
//! 替代 lib.rs 中裸 spawn 模式：chat_submit 经注册表受理并先推 queued 事件。
//! TaskRecord 字段对齐 03-business.md §2.1 的 9 个 Task 新增字段。
//! 本地 local-first 退化：queued 入队即出队（<100ms）、queue_position 恒为 0。

use std::collections::HashMap;
use std::sync::RwLock;

use super::status;

/// 任务记录（serde camelCase 与前端 TaskSummary.meta 对齐）
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskRecord {
    pub task_id: String,
    pub workspace_id: String,
    /// 14 态状态字面值（见 task::status）
    pub status: String,
    /// executing 子态 / 等待成因（rate_limited/circuit_break/quota_wait/...）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wait_reason: Option<String>,
    /// 已重试次数（上限 3，手动重试累计不重置）
    pub retry_count: u8,
    /// 乐观锁版本号（本地单调递增）
    pub revision: i64,
    /// 状态成因码（BUDGET_EXHAUSTED / QUEUE_TIMEOUT / LEASE_LOST / REJECTED / USER_CANCELLED）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status_reason_code: Option<String>,
    /// 排队位次（本地恒 0）
    pub queue_position: u32,
    /// 执行租约 ID
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_id: Option<String>,
    /// 是否已请求取消（cancel 请求与事件流的握手标记）
    pub cancel_requested: bool,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
}

/// 重试次数上限（与 task-state-machine.ts MAX_RETRY_COUNT 镜像）
pub const MAX_RETRY_COUNT: u8 = 3;

pub struct TaskRegistry {
    tasks: RwLock<HashMap<String, TaskRecord>>,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

impl Default for TaskRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskRegistry {
    pub fn new() -> Self {
        Self {
            tasks: RwLock::new(HashMap::new()),
        }
    }

    /// 受理新任务：创建记录并置为 queued（转移 #1；本地 <100ms 后转 planning）
    pub fn create(&self, task_id: &str, workspace_id: &str) -> TaskRecord {
        let now = now_ms();
        let record = TaskRecord {
            task_id: task_id.to_string(),
            workspace_id: workspace_id.to_string(),
            status: status::QUEUED.to_string(),
            wait_reason: None,
            retry_count: 0,
            revision: 1,
            status_reason_code: None,
            queue_position: 0,
            lease_id: None,
            cancel_requested: false,
            created_at_ms: now,
            updated_at_ms: now,
        };
        self.tasks
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(task_id.to_string(), record.clone());
        record
    }

    pub fn get(&self, task_id: &str) -> Option<TaskRecord> {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(task_id)
            .cloned()
    }

    /// 状态更新（revision 单调递增；调用方负责转移合法性校验，
    /// 权威转移表在 src/shared/task-state-machine.ts）
    pub fn update_status(
        &self,
        task_id: &str,
        new_status: &str,
        reason_code: Option<&str>,
    ) -> Option<TaskRecord> {
        let mut map = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        map.get_mut(task_id).map(|r| {
            r.status = new_status.to_string();
            r.status_reason_code = reason_code.map(|s| s.to_string());
            if new_status != status::EXECUTING {
                r.wait_reason = None;
            }
            r.revision += 1;
            r.updated_at_ms = now_ms();
            r.clone()
        })
    }

    /// executing 子态标注（转移 #20：限流等待以 wait_reason 表达，不设独立状态）
    pub fn set_wait_reason(&self, task_id: &str, reason: Option<&str>) -> Option<TaskRecord> {
        let mut map = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        map.get_mut(task_id).map(|r| {
            r.wait_reason = reason.map(|s| s.to_string());
            r.updated_at_ms = now_ms();
            r.clone()
        })
    }

    /// 重试计数 +1（手动重试累计不重置，转移 #34 语义）
    pub fn bump_retry(&self, task_id: &str) -> Option<u8> {
        let mut map = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        map.get_mut(task_id).map(|r| {
            r.retry_count = r.retry_count.saturating_add(1);
            r.updated_at_ms = now_ms();
            r.retry_count
        })
    }

    pub fn retry_exhausted(&self, task_id: &str) -> bool {
        self.get(task_id)
            .map(|r| r.retry_count >= MAX_RETRY_COUNT)
            .unwrap_or(false)
    }

    /// 绑定租约（queued→planning，转移 #2）
    pub fn bind_lease(&self, task_id: &str, lease_id: &str) {
        let mut map = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        if let Some(r) = map.get_mut(task_id) {
            r.lease_id = Some(lease_id.to_string());
            r.updated_at_ms = now_ms();
        }
    }

    /// 标记取消请求（幂等；由事件流消费侧完成实际取消）
    pub fn request_cancel(&self, task_id: &str) -> Option<TaskRecord> {
        let mut map = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        map.get_mut(task_id).map(|r| {
            r.cancel_requested = true;
            r.updated_at_ms = now_ms();
            r.clone()
        })
    }

    pub fn list(&self) -> Vec<TaskRecord> {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .values()
            .cloned()
            .collect()
    }

    /// 终态清理前的统计视图（chat:history / task:state 使用）
    pub fn len(&self) -> usize {
        self.tasks.read().unwrap_or_else(|e| e.into_inner()).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_enters_queued() {
        let reg = TaskRegistry::new();
        let r = reg.create("t1", "default");
        assert_eq!(r.status, status::QUEUED);
        assert_eq!(r.revision, 1);
        assert_eq!(r.queue_position, 0); // 本地退化：无排队
    }

    #[test]
    fn test_update_status_bumps_revision() {
        let reg = TaskRegistry::new();
        reg.create("t1", "default");
        let r = reg.update_status("t1", status::PLANNING, None).unwrap();
        assert_eq!(r.revision, 2);
        let r = reg
            .update_status("t1", status::FAILED, Some("QUEUE_TIMEOUT"))
            .unwrap();
        assert_eq!(r.status_reason_code.as_deref(), Some("QUEUE_TIMEOUT"));
        assert_eq!(r.revision, 3);
    }

    #[test]
    fn test_wait_reason_cleared_on_non_executing() {
        let reg = TaskRegistry::new();
        reg.create("t1", "default");
        reg.update_status("t1", status::EXECUTING, None);
        reg.set_wait_reason("t1", Some("rate_limited"));
        assert_eq!(reg.get("t1").unwrap().wait_reason.as_deref(), Some("rate_limited"));
        // 离开 executing 自动清除子态
        reg.update_status("t1", status::PAUSED, None);
        assert!(reg.get("t1").unwrap().wait_reason.is_none());
    }

    #[test]
    fn test_retry_count_accumulates_and_exhausts() {
        let reg = TaskRegistry::new();
        reg.create("t1", "default");
        for _ in 0..MAX_RETRY_COUNT {
            assert!(!reg.retry_exhausted("t1"));
            reg.bump_retry("t1");
        }
        assert!(reg.retry_exhausted("t1"));
    }

    #[test]
    fn test_cancel_request_idempotent() {
        let reg = TaskRegistry::new();
        reg.create("t1", "default");
        reg.request_cancel("t1").unwrap();
        reg.request_cancel("t1").unwrap();
        assert!(reg.get("t1").unwrap().cancel_requested);
    }

    #[test]
    fn test_missing_task_returns_none() {
        let reg = TaskRegistry::new();
        assert!(reg.get("nope").is_none());
        assert!(reg.update_status("nope", status::DONE, None).is_none());
        assert!(reg.is_empty());
    }

    #[test]
    fn test_bind_lease() {
        let reg = TaskRegistry::new();
        reg.create("t1", "default");
        reg.bind_lease("t1", "lease-1");
        assert_eq!(reg.get("t1").unwrap().lease_id.as_deref(), Some("lease-1"));
    }
}
