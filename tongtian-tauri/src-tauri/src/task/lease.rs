//! lease 租约管理器（流 E1，三层架构演进 P0）
//!
//! 依据需求分析报告 ADJ-R2-4 / 附录 A：
//! - 心跳 10s / 租约 30s / 超时回收 60s（触发 executing→queued + LEASE_LOST）/ 空闲释放 5min
//! - **禁止抢占**：显式接管需对端确认（needConfirmation=true）
//! - **单 tokio interval 扫描全部租约**（禁止每任务一个 timer）
//!
//! 本地 local-first 退化：桌面端恒为唯一 holder；lease_lost 仅在引擎流中断/
//! 进程重启等异常时由调用方显式上报。参数集中为常量表，云端接入时仅换存储层。

use std::collections::HashMap;
use std::sync::Mutex;

/// 心跳间隔（报告附录 A 冻结值）
pub const HEARTBEAT_INTERVAL_MS: u64 = 10_000;
/// 租约有效期
pub const LEASE_DURATION_MS: u64 = 30_000;
/// 超时回收阈值（心跳超时 60s → executing→queued + LEASE_LOST，转移 #18）
pub const TIMEOUT_RECLAIM_MS: u64 = 60_000;
/// 空闲自动释放（持有端空闲 5min 释放 lease）
pub const IDLE_RELEASE_MS: u64 = 5 * 60_000;
/// 扫描循环间隔（单 interval 驱动全部租约）
pub const SCAN_INTERVAL_MS: u64 = 10_000;

/// 租约记录（时间戳均为 epoch ms，便于测试注入时钟）
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LeaseRecord {
    pub task_id: String,
    pub lease_id: String,
    pub holder: String,
    pub heartbeat_at: u64,
    pub lease_until: u64,
    /// 最近一次控制活动（接管/指令）时间，用于空闲释放判定
    pub last_activity: u64,
}

impl LeaseRecord {
    pub fn is_idle(&self, now_ms: u64) -> bool {
        now_ms.saturating_sub(self.last_activity) >= IDLE_RELEASE_MS
    }
}

/// 扫描结果事件
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LeaseEvent {
    /// 心跳超时回收（转移 #18：executing→queued + LEASE_LOST）
    TimeoutReclaimed { task_id: String, lease_id: String },
    /// 空闲 5min 自动释放
    IdleReleased { task_id: String, lease_id: String },
}

/// 接管结果（禁止抢占语义）
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TakeoverResult {
    pub need_confirmation: bool,
    pub current_holder: Option<String>,
    pub message: String,
}

pub struct LeaseManager {
    holder_id: String,
    leases: Mutex<HashMap<String, LeaseRecord>>,
    seq: Mutex<u64>,
}

impl LeaseManager {
    pub fn new(holder_id: impl Into<String>) -> Self {
        Self {
            holder_id: holder_id.into(),
            leases: Mutex::new(HashMap::new()),
            seq: Mutex::new(0),
        }
    }

    fn next_lease_id(&self) -> String {
        let mut seq = self.seq.lock().unwrap_or_else(|e| e.into_inner());
        *seq += 1;
        format!("lease-{}", *seq)
    }

    /// 签发租约。本地 local-first：本设备恒为唯一 holder，直接成功。
    /// 若已有其他 holder 活跃持有（云端语义），返回 Err（对应 TASK_ALREADY_RUNNING）。
    pub fn acquire(&self, task_id: &str, now_ms: u64) -> Result<LeaseRecord, TakeoverResult> {
        let mut map = self.leases.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(existing) = map.get(task_id) {
            let expired = now_ms.saturating_sub(existing.heartbeat_at) >= TIMEOUT_RECLAIM_MS;
            if !expired && existing.holder != self.holder_id {
                // 禁止抢占（ADJ-R2-4）：返回需确认的接管请求
                return Err(TakeoverResult {
                    need_confirmation: true,
                    current_holder: Some(existing.holder.clone()),
                    message: format!("该任务正在 {} 执行", existing.holder),
                });
            }
        }
        let record = LeaseRecord {
            task_id: task_id.to_string(),
            lease_id: self.next_lease_id(),
            holder: self.holder_id.clone(),
            heartbeat_at: now_ms,
            lease_until: now_ms + LEASE_DURATION_MS,
            last_activity: now_ms,
        };
        map.insert(task_id.to_string(), record.clone());
        Ok(record)
    }

    /// 心跳续约（10s 周期调用）
    pub fn heartbeat(&self, task_id: &str, now_ms: u64) -> Option<LeaseRecord> {
        let mut map = self.leases.lock().unwrap_or_else(|e| e.into_inner());
        map.get_mut(task_id).map(|r| {
            r.heartbeat_at = now_ms;
            r.lease_until = now_ms + LEASE_DURATION_MS;
            r.clone()
        })
    }

    /// 记录控制活动（推迟空闲释放）
    pub fn touch(&self, task_id: &str, now_ms: u64) {
        if let Ok(mut map) = self.leases.lock() {
            if let Some(r) = map.get_mut(task_id) {
                r.last_activity = now_ms;
            }
        }
    }

    /// 显式释放（任务终态时调用）
    pub fn release(&self, task_id: &str) -> Option<LeaseRecord> {
        self.leases
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(task_id)
    }

    /// 接管请求：本地单端场景无对端，恒返回「无需确认即可获取」；
    /// 云端语义下由 BFF 转发对端确认。保持禁止抢占：不直接夺锁。
    pub fn takeover(&self, task_id: &str, now_ms: u64) -> TakeoverResult {
        let map = self.leases.lock().unwrap_or_else(|e| e.into_inner());
        match map.get(task_id) {
            Some(existing)
                if existing.holder != self.holder_id
                    && now_ms.saturating_sub(existing.heartbeat_at) < TIMEOUT_RECLAIM_MS =>
            {
                TakeoverResult {
                    need_confirmation: true,
                    current_holder: Some(existing.holder.clone()),
                    message: "禁止抢占：需对端确认或等待空闲 5min 自动释放".to_string(),
                }
            }
            _ => TakeoverResult {
                need_confirmation: false,
                current_holder: None,
                message: "可获取租约".to_string(),
            },
        }
    }

    /// 单 interval 扫描：超时回收 + 空闲释放（O(n)，桌面级任务数开销可忽略）
    pub fn scan(&self, now_ms: u64) -> Vec<LeaseEvent> {
        let mut map = self.leases.lock().unwrap_or_else(|e| e.into_inner());
        let mut events = Vec::new();
        let mut to_remove = Vec::new();
        for (task_id, record) in map.iter() {
            if now_ms.saturating_sub(record.heartbeat_at) >= TIMEOUT_RECLAIM_MS {
                events.push(LeaseEvent::TimeoutReclaimed {
                    task_id: task_id.clone(),
                    lease_id: record.lease_id.clone(),
                });
                to_remove.push(task_id.clone());
            } else if record.is_idle(now_ms) {
                events.push(LeaseEvent::IdleReleased {
                    task_id: task_id.clone(),
                    lease_id: record.lease_id.clone(),
                });
                to_remove.push(task_id.clone());
            }
        }
        for task_id in to_remove {
            map.remove(&task_id);
        }
        events
    }

    pub fn get(&self, task_id: &str) -> Option<LeaseRecord> {
        self.leases
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(task_id)
            .cloned()
    }

    /// 全部租约视图（lease_info command）
    pub fn list(&self) -> Vec<LeaseRecord> {
        self.leases
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
            .cloned()
            .collect()
    }

    pub fn holder_id(&self) -> &str {
        &self.holder_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T0: u64 = 1_000_000;

    #[test]
    fn test_acquire_and_heartbeat_renewal() {
        let mgr = LeaseManager::new("local-device");
        let lease = mgr.acquire("t1", T0).unwrap();
        assert_eq!(lease.holder, "local-device");
        assert_eq!(lease.lease_until, T0 + LEASE_DURATION_MS);

        let renewed = mgr.heartbeat("t1", T0 + HEARTBEAT_INTERVAL_MS).unwrap();
        assert_eq!(renewed.heartbeat_at, T0 + HEARTBEAT_INTERVAL_MS);
        assert_eq!(
            renewed.lease_until,
            T0 + HEARTBEAT_INTERVAL_MS + LEASE_DURATION_MS
        );
    }

    #[test]
    fn test_timeout_reclaim_after_60s() {
        let mgr = LeaseManager::new("local-device");
        mgr.acquire("t1", T0).unwrap();
        // 59s：未超时
        assert!(mgr.scan(T0 + TIMEOUT_RECLAIM_MS - 1).is_empty());
        // 60s：回收并产出 TimeoutReclaimed（触发 LEASE_LOST）
        let events = mgr.scan(T0 + TIMEOUT_RECLAIM_MS);
        assert_eq!(events.len(), 1);
        assert_eq!(
            events[0],
            LeaseEvent::TimeoutReclaimed {
                task_id: "t1".into(),
                lease_id: "lease-1".into()
            }
        );
        assert!(mgr.get("t1").is_none());
    }

    #[test]
    fn test_idle_release_after_5min() {
        let mgr = LeaseManager::new("local-device");
        mgr.acquire("t1", T0).unwrap();
        // 持续心跳保活（未超时）但无控制活动
        let mut t = T0;
        while t < T0 + IDLE_RELEASE_MS {
            t += HEARTBEAT_INTERVAL_MS;
            mgr.heartbeat("t1", t);
        }
        // 心跳未超时，但 last_activity 停在 T0 → 5min 后空闲释放
        let events = mgr.scan(T0 + IDLE_RELEASE_MS + 1);
        assert_eq!(events.len(), 1);
        assert_eq!(
            events[0],
            LeaseEvent::IdleReleased {
                task_id: "t1".into(),
                lease_id: "lease-1".into()
            }
        );
    }

    #[test]
    fn test_touch_delays_idle_release() {
        let mgr = LeaseManager::new("local-device");
        mgr.acquire("t1", T0).unwrap();
        mgr.touch("t1", T0 + IDLE_RELEASE_MS - 1000);
        mgr.heartbeat("t1", T0 + IDLE_RELEASE_MS);
        // 空闲计时从 touch 重置，此刻不应释放
        assert!(mgr.scan(T0 + IDLE_RELEASE_MS + 100).is_empty());
    }

    #[test]
    fn test_no_preemption_other_holder_active() {
        let mgr = LeaseManager::new("local-device");
        // 模拟云端：已有其他 holder 活跃持有（直接注入记录）
        {
            let mut map = mgr.leases.lock().unwrap();
            map.insert(
                "t1".into(),
                LeaseRecord {
                    task_id: "t1".into(),
                    lease_id: "lease-remote".into(),
                    holder: "web-client".into(),
                    heartbeat_at: T0,
                    lease_until: T0 + LEASE_DURATION_MS,
                    last_activity: T0,
                },
            );
        }
        // acquire 被拒，返回需确认的接管请求
        let err = mgr.acquire("t1", T0 + 1000).unwrap_err();
        assert!(err.need_confirmation);
        assert_eq!(err.current_holder.as_deref(), Some("web-client"));
        // takeover 同样禁止抢占
        let takeover = mgr.takeover("t1", T0 + 1000);
        assert!(takeover.need_confirmation);
    }

    #[test]
    fn test_takeover_allowed_after_timeout() {
        let mgr = LeaseManager::new("local-device");
        {
            let mut map = mgr.leases.lock().unwrap();
            map.insert(
                "t1".into(),
                LeaseRecord {
                    task_id: "t1".into(),
                    lease_id: "lease-remote".into(),
                    holder: "web-client".into(),
                    heartbeat_at: T0,
                    lease_until: T0 + LEASE_DURATION_MS,
                    last_activity: T0,
                },
            );
        }
        // 对端心跳超时后允许接管
        let takeover = mgr.takeover("t1", T0 + TIMEOUT_RECLAIM_MS);
        assert!(!takeover.need_confirmation);
    }

    #[test]
    fn test_release_on_terminal_state() {
        let mgr = LeaseManager::new("local-device");
        mgr.acquire("t1", T0).unwrap();
        assert!(mgr.release("t1").is_some());
        assert!(mgr.get("t1").is_none());
        assert!(mgr.release("t1").is_none());
    }
}
