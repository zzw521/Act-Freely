//! task 模块 — 三层架构演进 P0：任务注册表 / 租约 / 配额闸门

pub mod lease;
pub mod quota;
pub mod registry;

/// 14 态状态字面值（与 src/shared/types.ts TaskStatus 对齐，契约镜像）
pub mod status {
    pub const IDLE: &str = "idle";
    pub const QUEUED: &str = "queued";
    pub const PLANNING: &str = "planning";
    pub const PENDING_REVIEW: &str = "pending_review";
    pub const EXECUTING: &str = "executing";
    pub const PAUSED: &str = "paused";
    pub const BUDGET_PAUSED: &str = "budget_paused";
    pub const RETRY_PENDING: &str = "retry_pending";
    pub const DELIVERING: &str = "delivering";
    pub const DONE: &str = "done";
    pub const FAILED: &str = "failed";
    pub const ROLLED_BACK: &str = "rolled_back";
    pub const CANCELLED: &str = "cancelled";
    pub const ARCHIVED: &str = "archived";
}
