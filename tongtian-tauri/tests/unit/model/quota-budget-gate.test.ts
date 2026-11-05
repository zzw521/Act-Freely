/**
 * 预算闸门测试（缺陷 D-14）
 *
 * 四档判定与文案在 shared/quota-gate，取数与跃迁提醒在 renderer/lib/budget-gate。
 * 本文件钉住三件最容易做错的事：
 * - 用户显式填 0 是「这一档不限」，不能被出厂默认 20 万盖回去
 * - 用量读不到时按放行处理（fail-open），但必须留下可见的失败档位，不拿 0 冒充余量
 * - 提醒只在档位跃迁时发一次，同档重复读数不轰炸
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { DEFAULT_BUDGET } from '../../../src/shared/contracts';
import type { AppSettingsData, UsageStats } from '../../../src/shared/contracts';
import {
  TIER_CONSEQUENCE_ZH,
  budgetBlockMessage,
  budgetEntrySummaryZh,
  budgetUnreadableNote,
  judgeBudgets,
  recoveryHintZh,
  remainingTokens,
} from '../../../src/shared/quota-gate';
import {
  budgetAlertBody,
  collectBudgetAlerts,
  normalizeBudget,
  readBudgetVerdict,
  resetBudgetAlerts,
} from '../../../src/renderer/lib/budget-gate';
import type { BudgetRead } from '../../../src/renderer/lib/budget-gate';

function statsWith(tokensUsed: number): UsageStats {
  return {
    period: 'monthly',
    totals: {
      promptTokens: tokensUsed,
      completionTokens: 0,
      tokensUsed,
      calls: 1,
      activeModels: 1,
      lastUsedAtMs: 0,
    },
    topModel: null,
  };
}

/** 可控的用量读取器：按档位返回用量，标记哪些档位故意失败 */
function makeReader(
  used: Partial<Record<'daily' | 'monthly', number>>,
  fail: ('daily' | 'monthly')[] = [],
): { reader: (p: 'daily' | 'monthly') => Promise<UsageStats>; calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    reader: (period) => {
      calls.push(period);
      if (fail.includes(period)) return Promise.reject(new Error('USAGE_STATS_FAILED: 台账不可读'));
      return Promise.resolve(statsWith(used[period] ?? 0));
    },
  };
}

function settingsWith(budget: AppSettingsData['budget']): AppSettingsData {
  return { budget } as unknown as AppSettingsData;
}

describe('预算判定：档位与剩余', () => {
  it('未设预算（≤0）返回剩余为 null，界面据此显示「不限」而不是 0', () => {
    expect(remainingTokens(5000, 0)).toBeNull();
    expect(remainingTokens(5000, -1)).toBeNull();
  });

  it('剩余不会为负', () => {
    expect(remainingTokens(300_000, 200_000)).toBe(0);
  });

  it('四档动作链与 Rust 侧一致（70 提示 / 85 降级 / 95 排队 / 100 阻断）', () => {
    const actions = [100_000, 140_000, 170_000, 190_000, 200_000].map((used) => {
      const v = judgeBudgets([{ scope: 'daily', usedTokens: used, quotaTokens: 200_000 }]);
      return v.worst?.snapshot.action;
    });
    expect(actions).toEqual(['notify', 'notify', 'degrade', 'queue', 'block']);
  });

  it('两档预算取更紧的一档作为整体状态', () => {
    const v = judgeBudgets([
      { scope: 'monthly', usedTokens: 900_000, quotaTokens: 1_000_000 }, // 90%
      { scope: 'daily', usedTokens: 195_000, quotaTokens: 200_000 }, // 97.5%
    ]);
    expect(v.worst?.scope).toBe('daily');
    expect(v.queued).toBe(true);
    expect(v.blocked).toBe(false);
    expect(v.entries.map((e) => e.scope)).toEqual(['daily', 'monthly']);
  });

  it('两档都没设预算时标记 unlimited', () => {
    const v = judgeBudgets([
      { scope: 'daily', usedTokens: 10, quotaTokens: 0 },
      { scope: 'monthly', usedTokens: 10, quotaTokens: 0 },
    ]);
    expect(v.unlimited).toBe(true);
    expect(v.worst).toBeNull();
    expect(v.blocked).toBe(false);
  });
});

describe('预算文案：原因与恢复方式', () => {
  const blocked = judgeBudgets([{ scope: 'daily', usedTokens: 200_000, quotaTokens: 200_000 }]);

  it('阻断说明包含用量、后果、恢复方式与错误码', () => {
    const msg = budgetBlockMessage(blocked);
    expect(msg).toContain('预算已用满');
    expect(msg).toContain('今日');
    expect(msg).toContain(TIER_CONSEQUENCE_ZH.block);
    expect(msg).toContain('恢复方式');
    expect(msg).toContain('QUOTA_EXCEEDED');
  });

  it('恢复方式给出可执行出口（等重置或调高预算），并区分日/月周期', () => {
    expect(recoveryHintZh('block', 'daily')).toContain('下一个 UTC 日');
    expect(recoveryHintZh('queue', 'monthly')).toContain('下月 1 日');
    expect(recoveryHintZh('block', 'daily')).toContain('调高今日预算');
    expect(recoveryHintZh('notify', 'daily')).not.toContain('调高');
  });

  it('摘要句同时给出已用/额度/剩余/百分比', () => {
    const entry = judgeBudgets([{ scope: 'daily', usedTokens: 180_000, quotaTokens: 200_000 }]).entries[0];
    const line = budgetEntrySummaryZh(entry);
    for (const token of ['180,000', '200,000', '20,000', '90.0%']) {
      expect(line).toContain(token);
    }
  });

  it('读数失败时的提示点明哪些档没判成，并说明按放行处理', () => {
    const note = budgetUnreadableNote(['daily', 'monthly']);
    expect(note).toContain('今日');
    expect(note).toContain('本月');
    expect(note).toContain('放行');
    expect(budgetUnreadableNote([])).toBe('');
  });
});

describe('预算设置归一化：0 是用户的选择', () => {
  it('缺键用出厂默认（日 20 万，月与单任务不限）', () => {
    expect(normalizeBudget(undefined)).toEqual({
      dailyTokens: DEFAULT_BUDGET.dailyTokens,
      monthlyTokens: DEFAULT_BUDGET.monthlyTokens,
      perTaskTokens: DEFAULT_BUDGET.perTaskTokens,
    });
    expect(normalizeBudget({ monthlyTokens: 500_000 })).toEqual({
      dailyTokens: DEFAULT_BUDGET.dailyTokens,
      monthlyTokens: 500_000,
      // 单任务档缺键按不限，不猜一个数字把人拦下
      perTaskTokens: 0,
    });
  });

  it('显式 0 保留为不限，不会被默认值盖回去', () => {
    expect(normalizeBudget({ dailyTokens: 0, monthlyTokens: 0 }).dailyTokens).toBe(0);
    // 单任务档同理：用户填 0 是「不设上限」，不能回落成出厂 20 万那种数字
    expect(normalizeBudget({ perTaskTokens: 0 }).perTaskTokens).toBe(0);
    expect(normalizeBudget({ perTaskTokens: 50_000 }).perTaskTokens).toBe(50_000);
  });

  it('非法输入归零或回落默认，不把 NaN 带进判定', () => {
    expect(normalizeBudget({ dailyTokens: Number.NaN }).dailyTokens).toBe(DEFAULT_BUDGET.dailyTokens);
    expect(normalizeBudget({ dailyTokens: -5 }).dailyTokens).toBe(0);
    expect(normalizeBudget({ dailyTokens: 1234.7 }).dailyTokens).toBe(1234);
    expect(normalizeBudget({ dailyTokens: '20000' as unknown as number }).dailyTokens).toBe(
      DEFAULT_BUDGET.dailyTokens,
    );
  });
});

describe('取数侧：预算 × 用量台账', () => {
  beforeEach(() => resetBudgetAlerts());

  it('两档都读：按各自预算判档', async () => {
    const { reader, calls } = makeReader({ daily: 195_000, monthly: 400_000 });
    const read: BudgetRead = await readBudgetVerdict(
      reader,
      settingsWith({ dailyTokens: 200_000, monthlyTokens: 1_000_000 }),
    );
    expect(calls.sort()).toEqual(['daily', 'monthly']);
    expect(read.verdict.worst?.scope).toBe('daily');
    expect(read.verdict.queued).toBe(true);
    expect(read.errors).toEqual({});
  });

  it('某档预算为 0 时连用量都不读（不限就是不看）', async () => {
    const { reader, calls } = makeReader({ daily: 10, monthly: 10 });
    const read = await readBudgetVerdict(reader, settingsWith({ dailyTokens: 0, monthlyTokens: 1_000 }));
    expect(calls).toEqual(['monthly']);
    expect(read.verdict.entries.map((e) => e.scope)).toEqual(['monthly']);
  });

  it('设置读不到时按出厂预算判定，闸门不失效', async () => {
    const { reader } = makeReader({ daily: DEFAULT_BUDGET.dailyTokens, monthly: 0 });
    const read = await readBudgetVerdict(reader, null);
    expect(read.budget.dailyTokens).toBe(DEFAULT_BUDGET.dailyTokens);
    expect(read.verdict.blocked).toBe(true);
  });

  it('用量读取失败：不阻断（fail-open）但记下失败档位与原因', async () => {
    const { reader } = makeReader({ daily: 999_999 }, ['daily']);
    const read = await readBudgetVerdict(reader, settingsWith({ dailyTokens: 200_000, monthlyTokens: 0 }));
    expect(read.verdict.blocked).toBe(false);
    expect(read.verdict.unreadable).toEqual(['daily']);
    // D-20：失败原因以用户文案记录，裸码不外露（引擎/Rust 带来的详情仍在括号里）
    expect(read.errors.daily).toContain('用量总览生成失败');
    expect(read.errors.daily).not.toContain('USAGE_STATS_FAILED');
    expect(read.verdict.unlimited).toBe(false);
  });

  it('正常档位与失败档位并存时，正常档仍然照常判定', async () => {
    const { reader } = makeReader({ daily: 200_000, monthly: 100 }, ['monthly']);
    const read = await readBudgetVerdict(
      reader,
      settingsWith({ dailyTokens: 200_000, monthlyTokens: 1_000_000 }),
    );
    expect(read.verdict.blocked).toBe(true);
    expect(read.verdict.unreadable).toEqual(['monthly']);
  });
});

describe('档位跃迁提醒：去抖与边界', () => {
  beforeEach(() => resetBudgetAlerts());

  const entryAt = (used: number) =>
    judgeBudgets([{ scope: 'daily', usedTokens: used, quotaTokens: 100_000 }]).entries[0];

  it('70% 以下不打扰，到 70% 提醒一次，同档再加量不重复', () => {
    expect(collectBudgetAlerts(judgeBudgets([entryToUsage(50_000)]))).toEqual([]);
    const first = collectBudgetAlerts(judgeBudgets([entryToUsage(70_000)]));
    expect(first).toHaveLength(1);
    expect(budgetAlertBody(first[0])).toContain('70.0%');
    expect(collectBudgetAlerts(judgeBudgets([entryToUsage(75_000)]))).toHaveLength(0);
    expect(entryAt(75_000).snapshot.action).toBe('notify');
  });

  it('跃迁到更高档位再次提醒，正文含恢复方式', () => {
    collectBudgetAlerts(judgeBudgets([entryToUsage(70_000)]));
    const escalated = collectBudgetAlerts(judgeBudgets([entryToUsage(100_001)]));
    expect(escalated).toHaveLength(1);
    expect(escalated[0].snapshot.action).toBe('block');
    expect(budgetAlertBody(escalated[0])).toContain('恢复方式');
  });

  it('日 / 月两档各自独立去抖', () => {
    const both = judgeBudgets([
      { scope: 'daily', usedTokens: 95_000, quotaTokens: 100_000 },
      { scope: 'monthly', usedTokens: 900_000, quotaTokens: 1_000_000 },
    ]);
    expect(collectBudgetAlerts(both)).toHaveLength(2);
    expect(collectBudgetAlerts(both)).toHaveLength(0);
  });

  it('预算被取消（改成 0）后重新设预算，不会拿陈档位做比较', () => {
    collectBudgetAlerts(judgeBudgets([entryToUsage(95_000)]));
    // 取消预算：该档不再参与判定
    collectBudgetAlerts(judgeBudgets([{ scope: 'daily', usedTokens: 96_000, quotaTokens: 0 }]));
    // 重新设预算，仍在 95% 档：本会话该档已提醒过，不重复轰炸
    const again = collectBudgetAlerts(judgeBudgets([entryToUsage(96_000)]));
    expect(again).toHaveLength(0);
    // 但跃迁到 100% 档必须提醒
    expect(collectBudgetAlerts(judgeBudgets([entryToUsage(100_000)]))).toHaveLength(1);
  });
});

/** 把 entry 还原成判定入参（日档、10 万预算） */
function entryToUsage(usedTokens: number) {
  return { scope: 'daily' as const, usedTokens, quotaTokens: 100_000 };
}
