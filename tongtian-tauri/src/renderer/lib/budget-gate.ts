/**
 * 预算闸门的取数与提醒侧（缺陷 D-14）
 *
 * 判定与文案都在 `shared/quota-gate.ts` 的纯函数里；本模块只负责三件事：
 * 1. 从设置读预算（缺键用出厂值，显式填 0 是「这一档不限」，不能被出厂值盖回去）；
 * 2. 向用量台账取「今日 / 本月」已用量——读不到就 fail-open，但把失败档位如实带回；
 * 3. 档位跃迁去抖，产出该提醒的档位（同一档一次会话只提醒一次）。
 *
 * 口径边界（务必如实告知，别让人以为这是两本互不相干的账）：
 * 渲染层判的是 `usage:stats` 的引擎上报 Token；Rust 的 `quota-ledger.json`
 * 另有自己的日配额台账（默认 20 万/日；设置页保存日额度会同步过去，月额度不同步）。
 */

import { DEFAULT_BUDGET } from '../../shared/contracts';
import { errorTextOf } from '../../shared/error-messages';
import type { AppSettingsData, BudgetSettings, UsageStats } from '../../shared/contracts';
import {
  BUDGET_SCOPE_LABEL,
  budgetNoticeBodyZh,
  judgeBudgets,
} from '../../shared/quota-gate';
import type { BudgetEntry, BudgetScope, BudgetUsage, BudgetVerdict } from '../../shared/quota-gate';
import { claimNoticeKey, resetNoticeClaims } from './notify-local';

/** 用量台账读取器（由调用方注入：桌面/引擎/Mock 都经同一 `usage:stats` 通道） */
export type BudgetStatsReader = (period: BudgetScope) => Promise<UsageStats>;

export interface BudgetRead {
  verdict: BudgetVerdict;
  /** 读数失败原因（按档位）；界面据此说明「这一档没判成」，不拿 0 冒充余量 */
  errors: Partial<Record<BudgetScope, string>>;
  /** 本次判定实际采用的预算 */
  budget: BudgetSettings;
}

const SCOPES: BudgetScope[] = ['daily', 'monthly'];

function toNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : fallback;
}

/** 合并出厂预算：只有键缺失/非法才用默认值，0 是用户的有效选择 */
export function normalizeBudget(budget: Partial<BudgetSettings> | null | undefined): BudgetSettings {
  return {
    dailyTokens: toNumber(budget?.dailyTokens, DEFAULT_BUDGET.dailyTokens),
    monthlyTokens: toNumber(budget?.monthlyTokens, DEFAULT_BUDGET.monthlyTokens),
    perTaskTokens: toNumber(budget?.perTaskTokens, DEFAULT_BUDGET.perTaskTokens),
  };
}

/**
 * 读预算 + 读用量 → 判定结果。
 * `settings=null`（读设置失败）时按出厂预算判定：闸门宁可保守地拦，也不能因为读不到设置就形同虚设。
 */
export async function readBudgetVerdict(
  readStats: BudgetStatsReader,
  settings?: AppSettingsData | null,
): Promise<BudgetRead> {
  const budget = normalizeBudget(settings?.budget);
  const usages: BudgetUsage[] = [];
  const errors: Partial<Record<BudgetScope, string>> = {};
  const unreadable: BudgetScope[] = [];
  for (const scope of SCOPES) {
    const quotaTokens = scope === 'daily' ? budget.dailyTokens : budget.monthlyTokens;
    if (quotaTokens <= 0) continue;
    try {
      const view = await readStats(scope);
      usages.push({ scope, usedTokens: view?.totals?.tokensUsed ?? 0, quotaTokens });
    } catch (e) {
      errors[scope] = errorTextOf(e, '该周期用量读取失败');
      unreadable.push(scope);
    }
  }
  return { verdict: judgeBudgets(usages, unreadable), errors, budget };
}

// ── 档位跃迁去抖 ─────────────────────────────────────────────

/**
 * 上次读到的档位（低于 70% 记为 `below`）。
 * 必须带这个哨兵：`evaluateQuota` 把基线也返回 tier '70'，只比 tier 就分不清
 * 「还没到预警线」和「刚踩到 70%」，第一次预警会被当成同档吞掉。
 */
const lastByScope = new Map<BudgetScope, string>();

function tierKey(entry: BudgetEntry): string {
  return entry.percent < 70 ? 'below' : entry.snapshot.tier;
}

/**
 * 本轮该提醒的档位：使用率 ≥70%、且相比上次读数发生了档位跃迁、且本会话未提醒过该档。
 * 预算取消（改成 0）后忘记旧档位，重新设预算时不会拿陈档做比较。
 */
export function collectBudgetAlerts(verdict: BudgetVerdict): BudgetEntry[] {
  const scopes = new Set(verdict.entries.map((e) => e.scope));
  for (const scope of [...lastByScope.keys()]) {
    if (!scopes.has(scope)) lastByScope.delete(scope);
  }
  const alerts: BudgetEntry[] = [];
  for (const entry of verdict.entries) {
    const key = tierKey(entry);
    const prev = lastByScope.get(entry.scope) ?? null;
    lastByScope.set(entry.scope, key);
    if (entry.percent < 70) continue;
    if (prev === key) continue;
    if (!claimNoticeKey(`budget:${entry.scope}:${entry.snapshot.tier}`)) continue;
    alerts.push(entry);
  }
  return alerts;
}

/** 预警正文（系统通知与界面提示用同一句） */
export function budgetAlertBody(entry: BudgetEntry): string {
  return budgetNoticeBodyZh(entry);
}

/** 测试与退出登录用：清空跃迁记忆与提醒记录 */
export function resetBudgetAlerts(): void {
  lastByScope.clear();
  resetNoticeClaims();
}

/** 档位中文名（界面标签复用，避免各处自造说法） */
export function budgetScopeLabel(scope: BudgetScope): string {
  return BUDGET_SCOPE_LABEL[scope];
}
