// src/shared/quota-gate.ts — 配额四档闸门纯函数（流 F1）
//
// 依据：需求分析报告 ADJ-R5-4（预警档位 70/85/95/100%，超限动作顺序 降级→排队→阻断）
// 纯函数实现，renderer/Rust 镜像实现（src-tauri/src/task/quota.rs）共用同一阈值语义，
// 便于单元测试覆盖四档边界。
//
// 下半段是预算闸门（缺陷 D-14）：把「读到的用量 + 用户设的预算」判成档位，
// 并把给用户看的中文原因与恢复方式一并产出，设置页 / 用量页 / 提交闸门三处同一口径。

import type { QuotaSnapshot } from './contracts';

/** 四档阈值（百分比） */
export const QUOTA_TIERS = [
  { threshold: 100, tier: '100', action: 'block' },
  { threshold: 95, tier: '95', action: 'queue' },
  { threshold: 85, tier: '85', action: 'degrade' },
  { threshold: 70, tier: '70', action: 'notify' },
] as const;

export type QuotaTier = '70' | '85' | '95' | '100';
export type QuotaAction = 'notify' | 'degrade' | 'queue' | 'block';

/**
 * 计算使用率（百分比，保留两位小数）。
 * quota<=0 视为无配额限制，返回 0。
 */
export function usagePercent(usedTokens: number, quotaTokens: number): number {
  if (quotaTokens <= 0) return 0;
  return Math.min((usedTokens / quotaTokens) * 100, Number.MAX_SAFE_INTEGER);
}

/**
 * 判定当前档位与动作（从高到低匹配）。
 * 动作链语义（ADJ-R5-4）：
 * - 70%  notify：仅提示
 * - 85%  degrade：降级到备用模型（降级模型成功率基线 ≥80% 方可启用）
 * - 95%  queue：新任务排队，不新启动执行
 * - 100% block：新提交拒绝（BUDGET_EXHAUSTED/QUOTA_EXCEEDED），运行中转 budget_paused
 */
export function evaluateQuota(usedTokens: number, quotaTokens: number): QuotaSnapshot {
  const used = Math.max(0, usedTokens);
  const quota = Math.max(0, quotaTokens);
  const pct = usagePercent(used, quota);

  for (const t of QUOTA_TIERS) {
    if (pct >= t.threshold) {
      return {
        usedTokens: used,
        quotaTokens: quota,
        tier: t.tier as QuotaTier,
        action: t.action as QuotaAction,
      };
    }
  }
  // 低于 70%：以 70 档 notify 作为基线快照（不触发任何预警）
  return { usedTokens: used, quotaTokens: quota, tier: '70', action: 'notify' };
}

/** 是否应阻断新任务提交（100% 档） */
export function shouldBlock(snapshot: QuotaSnapshot): boolean {
  return snapshot.action === 'block';
}

/** 是否应让新任务排队（≥95% 档） */
export function shouldQueue(snapshot: QuotaSnapshot): boolean {
  return snapshot.action === 'queue' || snapshot.action === 'block';
}

/**
 * 档位变化检测（用于产出 quota_warning 事件，只在档位跃迁时触发，避免事件风暴）。
 * 返回 true 表示需要发出预警事件。
 */
export function tierChanged(prev: QuotaSnapshot | null, next: QuotaSnapshot): boolean {
  if (!prev) return next.action !== 'notify' || usagePercent(next.usedTokens, next.quotaTokens) >= 70;
  return prev.tier !== next.tier;
}

/** 错误码联动：100% 档对「新提交」与「运行中任务」分别映射的错误码 */
export function blockErrorCodeFor(isNewSubmission: boolean): 'BUDGET_EXHAUSTED' | 'QUOTA_EXCEEDED' {
  // 预算（组织级主闸门）耗尽优先用 BUDGET_EXHAUSTED；纯配额超限用 QUOTA_EXCEEDED。
  // 桌面本地台账统一按配额语义返回 QUOTA_EXCEEDED，云端 BFF 侧按组织预算返回 BUDGET_EXHAUSTED。
  return isNewSubmission ? 'QUOTA_EXCEEDED' : 'BUDGET_EXHAUSTED';
}

// ── 预算闸门（缺陷 D-14）：渲染层按用量台账判档，文案与判定同处一地便于测试 ──

/** 预算作用周期（与用量台账 `usage:stats` 的 period 同源，不含 total） */
export type BudgetScope = 'daily' | 'monthly';

export const BUDGET_SCOPE_LABEL: Record<BudgetScope, string> = {
  daily: '今日',
  monthly: '本月',
};

/** 单档预算读数 */
export interface BudgetUsage {
  scope: BudgetScope;
  /** 该周期已用 Token（来自用量台账） */
  usedTokens: number;
  /** 该周期预算（≤0 视为不设预算，不参与判定） */
  quotaTokens: number;
}

/** 参与判定的单档结果 */
export interface BudgetEntry {
  scope: BudgetScope;
  usedTokens: number;
  quotaTokens: number;
  remaining: number;
  percent: number;
  snapshot: QuotaSnapshot;
}

/** 预算判定结果（设置页、用量页、提交闸门共用同一份口径） */
export interface BudgetVerdict {
  /** 两档都没设预算：闸门不参与判定 */
  unlimited: boolean;
  /** 用量读数失败的档位：按 fail-open 处理（不阻断，但界面必须说明） */
  unreadable: BudgetScope[];
  /** 参与判定的档位，按使用率从高到低 */
  entries: BudgetEntry[];
  /** 最紧的一档；entries 为空时为 null */
  worst: BudgetEntry | null;
  /** 100% 档：拒绝新提交 */
  blocked: boolean;
  /** ≥95% 档：新提交按排队/阻断语义处理 */
  queued: boolean;
}

/** 预算剩余（未设预算返回 null，界面据此显示「不限」而不是 0） */
export function remainingTokens(usedTokens: number, quotaTokens: number): number | null {
  if (quotaTokens <= 0) return null;
  return Math.max(0, quotaTokens - Math.max(0, usedTokens));
}

/** 使用率的中文展示（正常区间留一位小数；超用成倍时取整，避免 530000.0% 这种噪声） */
export function formatUsagePercent(pct: number): string {
  return pct >= 1000 ? String(Math.round(pct)) : pct.toFixed(1);
}

/** 档位动作 → 用户能感知的后果（界面与系统通知共用，避免两处文案漂移） */
export const TIER_CONSEQUENCE_ZH: Record<QuotaAction, string> = {
  notify: '仅提示，任务照常执行',
  degrade: '后续任务可能降级到容灾链上的备用模型',
  queue: '新任务排队等待额度回笼，不再新启动执行',
  block: '新任务已拒绝提交，运行中的任务转入预算暂停',
};

function resetHintZh(scope: BudgetScope): string {
  return scope === 'daily' ? '下一个 UTC 日（约 00:00 UTC）自动重置' : '下月 1 日（UTC）自动重置';
}

/** 恢复方式：到点自动重置是主路径，调高预算是急需时的出路 */
export function recoveryHintZh(action: QuotaAction, scope: BudgetScope): string {
  if (action === 'block' || action === 'queue') {
    return `${resetHintZh(scope)}；急需继续使用时，在设置 →「模型与用量」调高${BUDGET_SCOPE_LABEL[scope]}预算`;
  }
  return `${resetHintZh(scope)}，无需操作`;
}

/**
 * 判定各档预算：只保留设了预算的档位，最紧的一档代表整体状态。
 * `unreadable` 为读数失败的档位（用量台账不可读时按 fail-open 放行，不假装还有余量）。
 */
export function judgeBudgets(
  usages: readonly BudgetUsage[],
  unreadable: readonly BudgetScope[] = [],
): BudgetVerdict {
  const entries: BudgetEntry[] = usages
    .filter((u) => u.quotaTokens > 0)
    .map((u) => {
      const used = Math.max(0, u.usedTokens);
      const snapshot = evaluateQuota(used, u.quotaTokens);
      return {
        scope: u.scope,
        usedTokens: used,
        quotaTokens: u.quotaTokens,
        remaining: remainingTokens(used, u.quotaTokens) ?? 0,
        percent: usagePercent(used, u.quotaTokens),
        snapshot,
      };
    })
    .sort((a, b) => b.percent - a.percent);
  const worst = entries[0] ?? null;
  return {
    unlimited: entries.length === 0 && unreadable.length === 0,
    unreadable: [...unreadable],
    entries,
    worst,
    blocked: worst !== null && shouldBlock(worst.snapshot),
    queued: worst !== null && shouldQueue(worst.snapshot),
  };
}

function fmtTokens(n: number): string {
  return n.toLocaleString('en-US');
}

/** 单档中文摘要（用量页与通知正文的同一句话） */
export function budgetEntrySummaryZh(entry: BudgetEntry): string {
  const label = BUDGET_SCOPE_LABEL[entry.scope];
  return (
    `${label}预算 ${fmtTokens(entry.quotaTokens)} Token：已用 ${fmtTokens(entry.usedTokens)}` +
    `（${formatUsagePercent(entry.percent)}%），剩 ${fmtTokens(entry.remaining)}` +
    `——${TIER_CONSEQUENCE_ZH[entry.snapshot.action]}`
  );
}

/** 单档预警正文（档位跃迁时作为系统通知内容） */
export function budgetNoticeBodyZh(entry: BudgetEntry): string {
  return `${budgetEntrySummaryZh(entry)}。恢复方式：${recoveryHintZh(entry.snapshot.action, entry.scope)}`;
}

/** 阻断新提交的完整原因：档位 + 后果 + 恢复方式 + 错误码（与 Rust 侧 QUOTA_EXCEEDED 同语义） */
export function budgetBlockMessage(verdict: BudgetVerdict): string {
  const worst = verdict.worst;
  if (!worst) return 'BUDGET_UNKNOWN: 无法读取用量台账，不能确认预算是否用尽';
  return (
    `预算已用满：${budgetEntrySummaryZh(worst)}。` +
    `恢复方式：${recoveryHintZh(worst.snapshot.action, worst.scope)}。` +
    `（错误码 ${blockErrorCodeFor(true)}）`
  );
}

/** 读数失败时的可见提示（放行但要说清「这一档没判成」） */
export function budgetUnreadableNote(scopes: readonly BudgetScope[]): string {
  if (scopes.length === 0) return '';
  const labels = scopes.map((s) => BUDGET_SCOPE_LABEL[s]).join('、');
  return `注意：${labels}用量读取失败，本次未做预算判定（按放行处理）。`;
}

// ── 单任务级预算（缺陷 D-14.1）：与 Rust `task/quota.rs` 的 task_* 同口径 ──

/** 日额度回答「今天一共用了多少」；单任务预算回答「这一个任务用了多少」。 */
export const TASK_BUDGET_LABEL = '单任务预算';

/** 单任务预算判定结果（镜像 Rust `TaskBudgetSnapshot`，字段名按前端 camelCase） */
export interface TaskBudgetState {
  taskId: string;
  /** 本任务已用 Token（与日额度同一份 usage 事件累计，两本账都涨） */
  usedTokens: number;
  /** 上限；0 = 不限（仍会计数，但不拦） */
  limitTokens: number;
  /** 四档快照：复用 `evaluateQuota`，与 Rust 侧同一套阈值语义 */
  snapshot: QuotaSnapshot;
  /** 是否已到拦停线：上限 >0 且已用 ≥ 上限 */
  blocked: boolean;
}

/**
 * 判单任务预算。与 Rust `snapshot_of` 逐条对齐：
 * - 上限 ≤0 时不限，使用率按 `usagePercent` 记 0（不拿 0 冒充 100%）；
 * - `blocked` 是「已用 ≥ 上限」，不是「> 上限」——踩线即停。
 */
export function judgeTaskBudget(
  taskId: string,
  usedTokens: number,
  limitTokens: number,
): TaskBudgetState {
  const used = Math.max(0, Math.floor(usedTokens) || 0);
  const limit = Math.max(0, Math.floor(limitTokens) || 0);
  return {
    taskId,
    usedTokens: used,
    limitTokens: limit,
    snapshot: evaluateQuota(used, limit),
    blocked: limit > 0 && used >= limit,
  };
}

/** 单任务预警正文（`quota_warning` 且 scope=task 时界面与通知用同一句） */
export function taskBudgetNoticeBodyZh(state: TaskBudgetState): string {
  if (state.limitTokens <= 0) return `${TASK_BUDGET_LABEL}未设上限，本任务已用 ${fmtTokens(state.usedTokens)} Token`;
  const pct = formatUsagePercent(usagePercent(state.usedTokens, state.limitTokens));
  return (
    `${TASK_BUDGET_LABEL} ${fmtTokens(state.limitTokens)} Token：本任务已用 ${fmtTokens(state.usedTokens)}` +
    `（${pct}%），剩 ${fmtTokens(remainingTokens(state.usedTokens, state.limitTokens) ?? 0)}`
  );
}

/**
 * 恢复方式（缺陷 D-14.1/14.2）：单任务预算不会到点自动重置，
 * 必须人工调高后在任务上点「继续」唤回——不写清「不自动」就会让人以为等着就行。
 */
export function taskBudgetRecoveryHintZh(): string {
  return (
    `单任务预算不会自动重置：在设置 →「模型与用量」调高「${TASK_BUDGET_LABEL}」（填 0 为不限），` +
    '再在这个任务上点「继续」从最近一次检查点唤回；不调高也可以直接取消本任务。'
  );
}


