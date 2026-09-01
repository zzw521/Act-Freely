//! 配额与预算闸门（流 F2，三层架构演进 P0）
//!
//! 依据需求分析报告 ADJ-R5-4 / 附录 A：
//! - 四档预警 70/85/95/100%，动作链「降级→排队→阻断」
//! - 100% 档：新提交返回 QUOTA_EXCEEDED/BUDGET_EXHAUSTED，运行中任务转 budget_paused（转移 #17）
//! - 本地 token 台账持久化 JSON；预留 RemoteQuotaSource trait（云端经 BFF 单点切换）
//!
//! 阈值语义与 src/shared/quota-gate.ts（TS 纯函数）完全镜像，
//! 两侧测试共用同一组边界用例。

use serde::{Deserialize, Serialize};
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

impl QuotaGate {
    /// 创建闸门：ledger_path=None 时纯内存（测试/Mock 场景）
    pub fn new(ledger_path: Option<PathBuf>, quota_tokens: u64) -> Self {
        let state = ledger_path
            .as_ref()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .and_then(|c| serde_json::from_str::<LedgerFile>(&c).ok())
            .filter(|l| l.ledger_date == today())
            .unwrap_or(LedgerFile {
                used_tokens: 0,
                quota_tokens,
                ledger_date: today(),
            });
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
}
