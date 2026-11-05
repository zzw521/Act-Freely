/**
 * 单任务级预算判定测试（缺陷 D-14.1）
 *
 * Rust 侧 `task/quota.rs` 的 `snapshot_of` 与本文件的 `judgeTaskBudget` 是同一把尺子的两份实现，
 * 这里钉的是「这把尺子怎么量」：
 * - 拦停线是「已用 ≥ 上限」，不是「> 上限」——踩线即停
 * - 上限 0 = 不限：仍按任务计数，但永远不 blocked，也不许把 0 当成 100% 用满
 * - 四档语义复用日额度的 `evaluateQuota`，不允许为单任务另造一套阈值
 * - 暂停后的恢复是人工的：文案必须说「调高后点继续」，不能承诺自动恢复（D-14.2）
 */

import { describe, it, expect } from 'vitest';
import { DEFAULT_BUDGET } from '../../../src/shared/contracts';
import {
  evaluateQuota,
  judgeTaskBudget,
  taskBudgetNoticeBodyZh,
  taskBudgetRecoveryHintZh,
} from '../../../src/shared/quota-gate';

describe('单任务预算：拦停线', () => {
  it('踩到上限即 blocked（不是超了才停）', () => {
    expect(judgeTaskBudget('t1', 99, 100).blocked).toBe(false);
    expect(judgeTaskBudget('t1', 100, 100).blocked).toBe(true);
    expect(judgeTaskBudget('t1', 101, 100).blocked).toBe(true);
  });

  it('上限 0 = 不限：计数照记，但永不拦停', () => {
    const s = judgeTaskBudget('t1', 999_999, 0);
    expect(s.blocked).toBe(false);
    expect(s.limitTokens).toBe(0);
    expect(s.usedTokens).toBe(999_999);
    // 不限时的使用率不能算成 100%，否则界面会把「没设预算」显示成「已用满」
    expect(s.snapshot.quotaTokens).toBe(0);
    expect(s.snapshot.action).toBe('notify');
  });

  it('负数与小数按非负整数处理（不给闸门喂一个判不动的数字）', () => {
    expect(judgeTaskBudget('t1', -5, 100).usedTokens).toBe(0);
    expect(judgeTaskBudget('t1', 10, -1).limitTokens).toBe(0);
    expect(judgeTaskBudget('t1', 12.7, 13.2).usedTokens).toBe(12);
  });

  it('出厂不设单任务预算：拦停是用户自己选的', () => {
    expect(DEFAULT_BUDGET.perTaskTokens).toBe(0);
  });
});

describe('单任务预算：四档与日额度同一口径', () => {
  it.each([
    [69, '70', 'notify'],
    [70, '70', 'notify'],
    [85, '85', 'degrade'],
    [95, '95', 'queue'],
    [100, '100', 'block'],
  ])('已用 %i / 100 → %s 档（%s）', (used, tier, action) => {
    const s = judgeTaskBudget('t1', used, 100);
    expect(s.snapshot.tier).toBe(tier);
    expect(s.snapshot.action).toBe(action);
    // 与日额度判定逐字段一致：两份实现不许在阈值上分叉
    expect(s.snapshot).toEqual(evaluateQuota(used, 100));
  });
});

describe('单任务预算：文案不许承诺自动恢复', () => {
  it('预警正文给出上限、已用与档位数字', () => {
    const body = taskBudgetNoticeBodyZh(judgeTaskBudget('t1', 95_000, 100_000));
    expect(body).toContain('单任务预算');
    expect(body).toContain('100,000');
    expect(body).toContain('95,000');
    expect(body).toContain('95.0%');
  });

  it('未设上限时说「未设上限」，不编一个剩余额度', () => {
    const body = taskBudgetNoticeBodyZh(judgeTaskBudget('t1', 1_234, 0));
    expect(body).toContain('未设上限');
    expect(body).not.toContain('100%');
  });

  it('恢复方式是「调高 + 点继续」，且明说不会自动重置', () => {
    const hint = taskBudgetRecoveryHintZh();
    expect(hint).toContain('不会自动重置');
    expect(hint).toContain('模型与用量');
    expect(hint).toContain('单任务预算');
    expect(hint).toContain('继续');
    expect(hint).toContain('0 为不限');
    expect(hint).not.toContain('自动继续');
  });
});
