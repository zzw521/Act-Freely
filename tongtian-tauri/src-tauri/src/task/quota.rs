//! 配额与预算闸门（流 F2，三层架构演进 P0）
//!
//! 依据需求分析报告 ADJ-R5-4 / 附录 A：
//! - 四档预警 70/85/95/100%，动作链「降级→排队→阻断」
//! - 100% 档：新提交返回 QUOTA_EXCEEDED/BUDGET_EXHAUSTED，运行中任务转 budget_paused（转移 #17）
//! - 本地 token 台账持久化 JSON；预留 RemoteQuotaSource trait（云端经 BFF 单点切换）
//!
//! 阈值语义与 src/shared/quota-gate.ts（TS 纯函数）完全镜像，
//! 两侧测试共用同一组边界用例。
//!
//! 单任务级预算（缺陷 D-14.1）在同一份台账里按 task_id 分别累计：
//! 日额度回答「今天一共用了多少」，单任务预算回答「这一个任务用了多少」。
//! 上限判定的四档语义复用 `evaluate`，所以两种预算在界面与事件里口径一致。

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Mutex;

/// 四档阈值（百分比，从高到低匹配）
pub const TIER_THRESHOLDS: [(u32, &str, &str); 4] = [
    (100, "100", "block"),
    (95, "95", "queue"),
    (85, "85", "degrade"),
    (70, "70", "notify"),
];

/// 本地默认日配额（token）：对齐报告 ADJ-R5-3 免费层 20 万/日
pub const DEFAULT_DAILY_QUOTA_TOKENS: u64 = 200_000;

/// 配额快照（与 contracts.ts QuotaSnapshot 对齐）
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct QuotaSnapshot {
    pub used_tokens: u64,
    pub quota_tokens: u64,
    pub tier: String,
    pub action: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
}

/// 台账持久化结构
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LedgerFile {
    used_tokens: u64,
    quota_tokens: u64,
    /// 台账日期（yyyy-mm-dd），跨日重置
    ledger_date: String,
    /// 单任务 token 上限（0 = 不限）；与日额度同册持久化，改后立刻生效
    #[serde(default)]
    per_task_limit_tokens: u64,
    /// 按任务累计的用量（task_id → 已用）。旧台账没有这两个键，按默认值补齐。
    #[serde(default)]
    tasks: BTreeMap<String, TaskUsage>,
}

/// 单个任务的累计（缺陷 D-14.1）
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TaskUsage {
    used_tokens: u64,
    /// 最后一次计入用量的毫秒时间戳：清理「早就结束、再不会回来」的任务
    updated_at_ms: u64,
}

/// 单任务预算快照（与 contracts.ts TaskBudgetSnapshot 对齐）
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskBudgetSnapshot {
    pub task_id: String,
    pub used_tokens: u64,
    /// 0 = 未设上限
    pub limit_tokens: u64,
    /// 与日额度同一套 70/85/95/100 档位语义
    pub tier: String,
    pub action: String,
    /// 已用到上限：执行面据此拦停本任务
    pub blocked: bool,
}

/// 单任务预算跃迁（去抖规则与日额度一致：只在「本次跨过阈值」时给一次）
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskBudgetTransition {
    /// 未设上限，或仍在预算内
    WithinBudget,
    /// 本次跨越上限：调用方据此把任务转 budget_paused（转移 #17）
    Exceeded(TaskBudgetSnapshot),
}

/// 任务累计保留上限：超过条数或久未更新的条目按最旧清理，
/// 避免天天提交的任务把台账写成无界的历史堆。
const MAX_TRACKED_TASKS: usize = 500;
const TASK_TTL_MS: u64 = 7 * 86_400_000;

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

impl LedgerFile {
    /// 只保留最近更新的 MAX_TRACKED_TASKS 条（且丢掉超过 TTL 的），
    /// 正在跑的任务每次都刷新 updated_at_ms，不会被清掉。
    fn trim_tasks(&mut self, now: u64) {
        self.tasks
            .retain(|_, u| now.saturating_sub(u.updated_at_ms) < TASK_TTL_MS);
        if self.tasks.len() <= MAX_TRACKED_TASKS {
            return;
        }
        let mut by_age: Vec<(u64, String)> = self
            .tasks
            .iter()
            .map(|(id, u)| (u.updated_at_ms, id.clone()))
            .collect();
        by_age.sort();
        let drop = self.tasks.len() - MAX_TRACKED_TASKS;
        for (_, id) in by_age.into_iter().take(drop) {
            self.tasks.remove(&id);
        }
    }
}

/// 档位跃迁结果（用于产出 quota_warning 事件，去抖：同档不重复）
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TierTransition {
    Unchanged,
    Entered(QuotaSnapshot),
}

/// 云端配额来源 trait（Phase B 经用户端 BFF 拉取组织级权威台账；本期仅本地实现）
pub trait RemoteQuotaSource: Send + Sync {
    fn fetch_quota(&self) -> Option<(u64, u64)>;
}

pub struct QuotaGate {
    ledger_path: Option<PathBuf>,
    state: Mutex<LedgerFile>,
    remote: Option<Box<dyn RemoteQuotaSource>>,
}

fn today() -> String {
    // 无 chrono 依赖：按 epoch 天数推导 UTC 日期
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = secs / 86_400;
    // 简化日历推算（Civil from days，Howard Hinnant 算法）
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{:04}-{:02}-{:02}", y, m, d)
}

pub fn evaluate(used_tokens: u64, quota_tokens: u64) -> QuotaSnapshot {
    if quota_tokens == 0 {
        // 无配额限制：基线档
        return QuotaSnapshot {
            used_tokens,
            quota_tokens,
            tier: "70".into(),
            action: "notify".into(),
            updated_at: None,
        };
    }
    let pct = (used_tokens as u128 * 100) / quota_tokens as u128;
    for (threshold, tier, action) in TIER_THRESHOLDS {
        if pct >= threshold as u128 {
            return QuotaSnapshot {
                used_tokens,
                quota_tokens,
                tier: (*tier).into(),
                action: (*action).into(),
                updated_at: None,
            };
        }
    }
    QuotaSnapshot {
        used_tokens,
        quota_tokens,
        tier: "70".into(),
        action: "notify".into(),
        updated_at: None,
    }
}

/// 单任务预算读数（在锁内调用，快照即当前事实）
fn snapshot_of(task_id: &str, ledger: &LedgerFile) -> TaskBudgetSnapshot {
    let used = ledger
        .tasks
        .get(task_id)
        .map(|u| u.used_tokens)
        .unwrap_or(0);
    let limit = ledger.per_task_limit_tokens;
    let tiered = evaluate(used, limit);
    TaskBudgetSnapshot {
        task_id: task_id.to_string(),
        used_tokens: used,
        limit_tokens: limit,
        tier: tiered.tier,
        action: tiered.action,
        blocked: limit > 0 && used >= limit,
    }
}

impl QuotaGate {
    /// 创建闸门：ledger_path=None 时纯内存（测试/Mock 场景）
    pub fn new(ledger_path: Option<PathBuf>, quota_tokens: u64) -> Self {
        let loaded = ledger_path
            .as_ref()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .and_then(|c| serde_json::from_str::<LedgerFile>(&c).ok());
        let t = today();
        let state = match loaded {
            // 当日台账：原样续用
            Some(l) if l.ledger_date == t => l,
            // 跨日：只清「今天一共用了多少」。单任务上限与按任务累计必须留着——
            // 它们是任务维度的量，跟 UTC 日无关；跟着清零等于让中断一夜的任务
            // 白拿一份额度，单任务预算就形同没设。
            Some(mut l) => {
                let mut rolled = LedgerFile {
                    used_tokens: 0,
                    quota_tokens: l.quota_tokens,
                    ledger_date: t,
                    per_task_limit_tokens: l.per_task_limit_tokens,
                    tasks: std::mem::take(&mut l.tasks),
                };
                rolled.trim_tasks(now_ms());
                rolled
            }            None => LedgerFile {
                used_tokens: 0,
                quota_tokens,
                ledger_date: t,
                ..Default::default()
            },
        };
        Self {
            ledger_path,
            state: Mutex::new(state),
            remote: None,
        }
    }

    /// 挂载云端配额来源（Phase B 启用；当前本地实现不使用）
    #[allow(dead_code)]
    pub fn with_remote(mut self, remote: Box<dyn RemoteQuotaSource>) -> Self {
        self.remote = Some(remote);
        self
    }

    fn snapshot_of(&self, ledger: &LedgerFile) -> QuotaSnapshot {
        let mut s = evaluate(ledger.used_tokens, ledger.quota_tokens);
        s.updated_at = Some(ledger.ledger_date.clone());
        s
    }

    /// 当前快照
    pub fn snapshot(&self) -> QuotaSnapshot {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        self.snapshot_of(&g)
    }

    /// 运行期调整日配额（设置页保存后立即生效，无需重启）。
    /// 当日已用保留，按新额度重判；0 表示不限。跨日时先按既有规则清零。
    pub fn set_quota(&self, quota_tokens: u64) -> QuotaSnapshot {
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let t = today();
        if g.ledger_date != t {
            g.used_tokens = 0;
            g.ledger_date = t;
        }
        g.quota_tokens = quota_tokens;
        self.persist(&g);
        self.snapshot_of(&g)
    }

    /// 累计用量（step_done/deliver 的 usage 字段），返回档位跃迁事件（去抖）。
    /// 跃迁判定基于「跨越阈值」：before_pct < 阈值 ≤ after_pct（首次进入 70 档也触发）
    pub fn add_usage(&self, tokens: u64) -> TierTransition {
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        // 跨日重置
        let t = today();
        if g.ledger_date != t {
            g.used_tokens = 0;
            g.ledger_date = t;
        }
        let before_pct = if g.quota_tokens == 0 {
            0u128
        } else {
            g.used_tokens as u128 * 100 / g.quota_tokens as u128
        };
        g.used_tokens = g.used_tokens.saturating_add(tokens);
        let after_pct = if g.quota_tokens == 0 {
            0u128
        } else {
            g.used_tokens as u128 * 100 / g.quota_tokens as u128
        };
        self.persist(&g);
        // 阈值从高到低排列，首个命中即本次跨越的最高档位
        let crossed = TIER_THRESHOLDS
            .iter()
            .any(|(threshold, _, _)| after_pct >= *threshold as u128 && before_pct < *threshold as u128);
        if crossed {
            TierTransition::Entered(self.snapshot_of(&g))
        } else {
            TierTransition::Unchanged
        }
    }

    /// 新任务提交闸门：100% 档返回阻断错误码
    pub fn check_submit(&self) -> Result<(), &'static str> {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if evaluate(g.used_tokens, g.quota_tokens).action == "block" {
            // 桌面本地台账按配额语义返回 QUOTA_EXCEEDED（云端组织预算为 BUDGET_EXHAUSTED）
            Err("QUOTA_EXCEEDED")
        } else {
            Ok(())
        }
    }

    /// 是否应让新任务排队（≥95% 档，动作链 降级→排队→阻断）
    pub fn should_queue(&self) -> bool {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        matches!(evaluate(g.used_tokens, g.quota_tokens).action.as_str(), "queue" | "block")
    }

    // ── 单任务级预算（缺陷 D-14.1）───────────────────────────────────

    /// 设置单任务 token 上限（0 = 不限）：保存后立即生效，按新上限重判在跑的任务。
    /// 调高上限**不会**自动唤回已转入 budget_paused 的任务——那是转移 #26，
    /// 由用户在任务上点「继续」触发（见 chat_review 的 resume 分支）。
    pub fn set_per_task_limit(&self, tokens: u64) -> u64 {
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let t = today();
        if g.ledger_date != t {
            g.used_tokens = 0;
            g.ledger_date = t;
        }
        g.per_task_limit_tokens = tokens;
        self.persist(&g);
        tokens
    }

    /// 当前单任务上限（0 = 不限）
    pub fn per_task_limit(&self) -> u64 {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        g.per_task_limit_tokens
    }

    /// 本任务的已用/上限读数。从未计过量的任务如实给 used=0，不猜历史用量。
    pub fn task_snapshot(&self, task_id: &str) -> TaskBudgetSnapshot {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        snapshot_of(task_id, &g)
    }

    /// 按任务累计用量（同一条 usage 事件同时进日台账与本函数）。
    /// 只有「本次跨过上限」才返回 Exceeded，后续同档不再重复播报（与日额度去抖一致）。
    pub fn add_task_usage(&self, task_id: &str, tokens: u64) -> TaskBudgetTransition {
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let now = now_ms();
        let limit = g.per_task_limit_tokens;
        let before = g
            .tasks
            .get(task_id)
            .map(|u| u.used_tokens)
            .unwrap_or(0);
        let after = before.saturating_add(tokens);
        g.tasks.insert(
            task_id.to_string(),
            TaskUsage {
                used_tokens: after,
                updated_at_ms: now,
            },
        );
        g.trim_tasks(now);
        self.persist(&g);
        if limit == 0 || tokens == 0 || before >= limit || after < limit {
            return TaskBudgetTransition::WithinBudget;
        }
        TaskBudgetTransition::Exceeded(snapshot_of(task_id, &g))
    }

    /// 执行面的拦停判据：本任务是否已用到单任务上限（未设上限恒为 false）
    pub fn task_budget_blocked(&self, task_id: &str) -> bool {
        let g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        g.per_task_limit_tokens > 0
            && g.tasks
                .get(task_id)
                .map(|u| u.used_tokens >= g.per_task_limit_tokens)
                .unwrap_or(false)
    }

    /// 任务进入终态后清掉累计：否则「同名任务重新提交」会继承上一轮的计数，
    /// 表现为什么都没干就被预算拦住。
    pub fn forget_task(&self, task_id: &str) {
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if g.tasks.remove(task_id).is_some() {
            self.persist(&g);
        }
    }

    fn persist(&self, ledger: &LedgerFile) {
        if let Some(path) = &self.ledger_path {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            if let Ok(json) = serde_json::to_string(ledger) {
                if let Err(err) = std::fs::write(path, json) {
                    eprintln!("[quota] persist failed: {err}");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tier_boundaries_mirror_ts() {
        // 与 tests/unit/model/quota-gate.test.ts 同一组边界
        assert_eq!(evaluate(69, 100).action, "notify");
        assert_eq!(evaluate(70, 100).action, "notify");
        assert_eq!(evaluate(85, 100).action, "degrade");
        assert_eq!(evaluate(94, 100).action, "degrade");
        assert_eq!(evaluate(95, 100).action, "queue");
        assert_eq!(evaluate(99, 100).action, "queue");
        assert_eq!(evaluate(100, 100).action, "block");
        assert_eq!(evaluate(150, 100).action, "block");
    }

    #[test]
    fn test_zero_quota_means_unlimited() {
        let s = evaluate(u64::MAX, 0);
        assert_eq!(s.action, "notify");
    }

    #[test]
    fn test_add_usage_tier_transition_debounce() {
        let gate = QuotaGate::new(None, 100);
        // 69→70：进入 70 档
        let t = gate.add_usage(70);
        assert!(matches!(t, TierTransition::Entered(ref s) if s.tier == "70"));
        // 70→84：同档，不重复触发
        assert_eq!(gate.add_usage(14), TierTransition::Unchanged);
        // 84→85：跃迁到 degrade 档
        let t = gate.add_usage(1);
        assert!(matches!(t, TierTransition::Entered(ref s) if s.action == "degrade"));
    }

    #[test]
    fn test_check_submit_blocks_at_100_percent() {
        let gate = QuotaGate::new(None, 100);
        assert!(gate.check_submit().is_ok());
        gate.add_usage(99);
        assert!(gate.check_submit().is_ok()); // 99% 排队档不阻断提交
        gate.add_usage(1);
        assert_eq!(gate.check_submit(), Err("QUOTA_EXCEEDED"));
    }

    #[test]
    fn test_should_queue_from_95_percent() {
        let gate = QuotaGate::new(None, 100);
        assert!(!gate.should_queue());
        gate.add_usage(95);
        assert!(gate.should_queue());
    }

    #[test]
    fn test_ledger_persistence_roundtrip() {
        let dir = std::env::temp_dir().join(format!("tt-quota-test-{}", super::today()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("ledger.json");
        {
            let gate = QuotaGate::new(Some(path.clone()), 1000);
            gate.add_usage(500);
        }
        // 重新加载：台账恢复
        let gate2 = QuotaGate::new(Some(path.clone()), 1000);
        assert_eq!(gate2.snapshot().used_tokens, 500);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_budget_paused_error_code_mapping() {
        // 运行中任务 100% 触发 budget_paused 时的错误码为 BUDGET_EXHAUSTED（转移 #17），
        // 新提交为 QUOTA_EXCEEDED —— 与 TS blockErrorCodeFor 语义一致
        let gate = QuotaGate::new(None, 100);
        gate.add_usage(100);
        assert_eq!(gate.check_submit(), Err("QUOTA_EXCEEDED"));
    }

    #[test]
    fn test_set_quota_keeps_usage_and_rejudges_immediately() {
        let gate = QuotaGate::new(None, 1000);
        gate.add_usage(700);
        assert_eq!(gate.snapshot().action, "notify");
        // 调低到 700：已用等于额度，立即进入阻断档，无需重启
        let s = gate.set_quota(700);
        assert_eq!(s.quota_tokens, 700);
        assert_eq!(s.used_tokens, 700);
        assert_eq!(s.action, "block");
        assert_eq!(gate.check_submit(), Err("QUOTA_EXCEEDED"));
        // 调高后解除阻断，用量不清零
        let s = gate.set_quota(2000);
        assert_eq!(s.used_tokens, 700);
        assert!(gate.check_submit().is_ok());
        // 0 = 不限
        assert_eq!(gate.set_quota(0).action, "notify");
        assert!(gate.check_submit().is_ok());
    }

    #[test]
    fn test_set_quota_persists_for_restart() {
        let dir = std::env::temp_dir().join(format!(
            "tt-quota-set-{}-{}",
            super::today(),
            std::process::id()
        ));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("ledger.json");
        {
            let gate = QuotaGate::new(Some(path.clone()), 1000);
            gate.add_usage(300);
            gate.set_quota(500);
        }
        let gate2 = QuotaGate::new(Some(path.clone()), 1000);
        assert_eq!(gate2.snapshot().quota_tokens, 500);
        assert_eq!(gate2.snapshot().used_tokens, 300);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn task_gate_dir(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tt-task-budget-{}-{}-{}",
            tag,
            super::today(),
            std::process::id()
        ));
        let _ = std::fs::create_dir_all(&dir);
        dir
    }

    /// D-14.1：单任务预算只拦那一个任务，别的任务与日额度都不受影响
    #[test]
    fn test_per_task_budget_blocks_only_the_task_that_hit_it() {
        let gate = QuotaGate::new(None, 1_000_000); // 日额度宽裕，隔离出单任务维度
        assert_eq!(gate.set_per_task_limit(100), 100);

        let t = gate.add_task_usage("task-a", 120);
        assert!(
            matches!(t, TaskBudgetTransition::Exceeded(ref s) if s.blocked && s.action == "block"),
            "{t:?}"
        );
        assert!(gate.task_budget_blocked("task-a"));
        // 另一个任务照常：单任务预算不是全局开关
        assert_eq!(gate.add_task_usage("task-b", 50), TaskBudgetTransition::WithinBudget);
        assert!(!gate.task_budget_blocked("task-b"));
        let b = gate.task_snapshot("task-b");
        assert_eq!(b.used_tokens, 50);
        assert_eq!(b.limit_tokens, 100);
        assert_eq!(b.tier, "70");
        assert!(!b.blocked);
        // 两处计数各算各的：日额度只由 add_usage 累计（计量点在 bridge 里两个都喂，
        // 见 bridge::agent::p0_tests 的同一条 usage 同时进日台账与本任务台账）
        assert_eq!(gate.snapshot().used_tokens, 0);
        gate.add_usage(170);
        assert_eq!(gate.snapshot().used_tokens, 170);
        assert_eq!(gate.task_snapshot("task-a").used_tokens, 120);
        assert!(gate.check_submit().is_ok(), "日额度宽裕时不该拒绝新提交");
    }

    /// D-14.1：跨上限只播报一次；调高上限后同一任务立即解除拦停（唤回是用户的动作）
    #[test]
    fn test_per_task_transition_debounces_and_raising_limit_unblocks() {
        let gate = QuotaGate::new(None, 1_000_000);
        gate.set_per_task_limit(100);
        assert!(matches!(
            gate.add_task_usage("t", 100),
            TaskBudgetTransition::Exceeded(_)
        ));
        // 已经超限：继续用量不再重复跃迁（否则每轮都甩一条预算事件）
        assert_eq!(gate.add_task_usage("t", 30), TaskBudgetTransition::WithinBudget);
        assert_eq!(gate.task_snapshot("t").used_tokens, 130);
        assert!(gate.task_budget_blocked("t"));
        // 调高到 200：同一任务立刻脱离拦停（不需要重启，也不用把已用量清零）
        gate.set_per_task_limit(200);
        assert!(!gate.task_budget_blocked("t"));
        assert_eq!(gate.task_snapshot("t").used_tokens, 130);
        // 再往上调到 0 = 不限：同样解除
        gate.set_per_task_limit(0);
        assert!(!gate.task_budget_blocked("t"));
    }

    /// D-14.1：0 = 不限，但仍然按任务累计，界面要能说出「这个任务用了多少」
    #[test]
    fn test_per_task_limit_zero_is_unlimited_but_still_counts() {
        let gate = QuotaGate::new(None, 1_000_000);
        assert_eq!(gate.per_task_limit(), 0);
        assert_eq!(
            gate.add_task_usage("t", 9_999_999),
            TaskBudgetTransition::WithinBudget
        );
        let s = gate.task_snapshot("t");
        assert_eq!(s.used_tokens, 9_999_999);
        assert!(!s.blocked);
        assert_eq!(s.action, "notify", "未设上限时按基线档，不假装触顶");
        assert!(!gate.task_budget_blocked("t"));
    }

    /// D-14.1：档位语义与日额度同一把尺子（70/85/95/100）
    #[test]
    fn test_per_task_tiers_mirror_evaluate() {
        let gate = QuotaGate::new(None, 1_000_000);
        gate.set_per_task_limit(100);
        gate.add_task_usage("t", 70);
        assert_eq!(gate.task_snapshot("t").action, "notify");
        gate.add_task_usage("t", 15);
        assert_eq!(gate.task_snapshot("t").action, "degrade");
        gate.add_task_usage("t", 10);
        assert_eq!(gate.task_snapshot("t").action, "queue");
        assert!(!gate.task_budget_blocked("t"), "95% 还没到上限");
        gate.add_task_usage("t", 5);
        assert_eq!(gate.task_snapshot("t").action, "block");
        assert!(gate.task_budget_blocked("t"));
    }

    /// D-14.1：终态后清计数，避免「重新提交的同名任务一上来就被拦停」
    #[test]
    fn test_forget_task_clears_the_counter() {
        let gate = QuotaGate::new(None, 1_000_000);
        gate.set_per_task_limit(100);
        gate.add_task_usage("t", 100);
        assert!(gate.task_budget_blocked("t"));
        gate.forget_task("t");
        assert!(!gate.task_budget_blocked("t"));
        assert_eq!(gate.task_snapshot("t").used_tokens, 0);
        // 再次计入按新任务算：不会「本次就跃迁」以外的事件重复
        assert!(matches!(
            gate.add_task_usage("t", 100),
            TaskBudgetTransition::Exceeded(_)
        ));
        gate.forget_task("不存在的任务"); // 幂等：不该 panic
    }

    /// D-14.1：单任务上限与累计跨重启保留；日额度按 UTC 日重置，任务计数不跟着重置
    #[test]
    fn test_per_task_state_survives_daily_reset() {
        let dir = task_gate_dir("rollover");
        let path = dir.join("ledger.json");
        // 伪造一份「昨天」的台账：日额度已用满 + 一个任务已用 60 + 上限 200
        let yesterday_ledger = serde_json::json!({
            "usedTokens": 1000,
            "quotaTokens": 1000,
            "ledgerDate": "2000-01-01",
            "perTaskLimitTokens": 200,
            "tasks": {
                "keep": { "usedTokens": 60, "updatedAtMs": now_ms() },
                "stale": { "usedTokens": 30, "updatedAtMs": now_ms() - 8 * 86_400_000 }
            }
        });
        std::fs::write(&path, serde_json::to_string(&yesterday_ledger).unwrap()).unwrap();

        let gate = QuotaGate::new(Some(path.clone()), 1000);
        // 日台账跨日清零
        assert_eq!(gate.snapshot().used_tokens, 0);
        // 单任务上限与累计留着：中断一夜的任务不该白拿一份额度
        assert_eq!(gate.per_task_limit(), 200);
        assert_eq!(gate.task_snapshot("keep").used_tokens, 60);
        // 久未出现的任务按 TTL 清掉，不占着台账
        assert_eq!(gate.task_snapshot("stale").used_tokens, 0);
        assert!(!gate.task_budget_blocked("keep"));
        // 续用到上限仍能拦住
        assert!(matches!(
            gate.add_task_usage("keep", 140),
            TaskBudgetTransition::Exceeded(_)
        ));
        assert!(gate.task_budget_blocked("keep"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// D-14.1：升级前的老台账（没有这两个键）必须仍能读出，不能整本作废
    #[test]
    fn test_legacy_ledger_without_task_keys_still_loads() {
        let dir = task_gate_dir("legacy");
        let path = dir.join("ledger.json");
        let legacy = serde_json::json!({
            "usedTokens": 12345,
            "quotaTokens": 900,
            "ledgerDate": super::today()
        });
        std::fs::write(&path, serde_json::to_string(&legacy).unwrap()).unwrap();

        let gate = QuotaGate::new(Some(path.clone()), 1000);
        assert_eq!(gate.snapshot().used_tokens, 12345);
        assert_eq!(gate.snapshot().quota_tokens, 900);
        assert_eq!(gate.per_task_limit(), 0, "老台账没有单任务上限：按不限处理");
        assert!(!gate.task_budget_blocked("anything"));

        // 新设置写回后仍是同一份台账（不会因为补字段把日额度弄丢）
        gate.set_per_task_limit(50);
        let gate2 = QuotaGate::new(Some(path), 1000);
        assert_eq!(gate2.per_task_limit(), 50);
        assert_eq!(gate2.snapshot().used_tokens, 12345);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
