// tests/unit/model/quota-gate.test.ts — 配额四档闸门测试（流 F1/F4）
// 覆盖四档边界、动作链（降级→排队→阻断）、错误码联动。

import { describe, it, expect } from 'vitest';
import {
  evaluateQuota,
  usagePercent,
  shouldBlock,
  shouldQueue,
  tierChanged,
  blockErrorCodeFor,
  QUOTA_TIERS,
} from '../../../src/shared/quota-gate';

describe('配额闸门：四档边界', () => {
  it('低于 70% 为基线档（不触发预警）', () => {
    const s = evaluateQuota(69, 100);
    expect(s.tier).toBe('70');
    expect(s.action).toBe('notify');
  });

  it('恰 70% 触发 notify', () => {
    expect(evaluateQuota(70, 100).action).toBe('notify');
    expect(evaluateQuota(700_000, 1_000_000).tier).toBe('70');
  });

  it('恰 85% 触发 degrade', () => {
    const s = evaluateQuota(85, 100);
    expect(s.tier).toBe('85');
    expect(s.action).toBe('degrade');
  });

  it('恰 95% 触发 queue', () => {
    const s = evaluateQuota(95, 100);
    expect(s.tier).toBe('95');
    expect(s.action).toBe('queue');
  });

  it('恰 100% 触发 block', () => {
    const s = evaluateQuota(100, 100);
    expect(s.tier).toBe('100');
    expect(s.action).toBe('block');
  });

  it('超用（>100%）仍为 block 档', () => {
    expect(evaluateQuota(150, 100).action).toBe('block');
  });

  it('边界下沿：84.9% 停留在 70 档', () => {
    expect(evaluateQuota(849, 1000).tier).toBe('70');
  });

  it('边界下沿：94.9% 停留在 85 档', () => {
    expect(evaluateQuota(949, 1000).tier).toBe('85');
  });

  it('边界下沿：99.9% 停留在 95 档', () => {
    expect(evaluateQuota(999, 1000).tier).toBe('95');
  });
});

describe('配额闸门：异常输入', () => {
  it('quota<=0 视为无限制，不阻断', () => {
    const s = evaluateQuota(99999, 0);
    expect(shouldBlock(s)).toBe(false);
    expect(usagePercent(99999, 0)).toBe(0);
  });

  it('负值归零处理', () => {
    const s = evaluateQuota(-10, 100);
    expect(s.usedTokens).toBe(0);
    expect(s.action).toBe('notify');
  });
});

describe('配额闸门：动作链（降级→排队→阻断，ADJ-R5-4）', () => {
  it('动作链随用量单调升级', () => {
    const actions = [10, 70, 85, 95, 100].map((u) => evaluateQuota(u, 100).action);
    expect(actions).toEqual(['notify', 'notify', 'degrade', 'queue', 'block']);
  });

  it('shouldBlock 仅 100% 档为真', () => {
    expect(shouldBlock(evaluateQuota(100, 100))).toBe(true);
    expect(shouldBlock(evaluateQuota(99, 100))).toBe(false);
  });

  it('shouldQueue 在 ≥95% 档为真（block 档同样排队语义兜底）', () => {
    expect(shouldQueue(evaluateQuota(95, 100))).toBe(true);
    expect(shouldQueue(evaluateQuota(100, 100))).toBe(true);
    expect(shouldQueue(evaluateQuota(90, 100))).toBe(false);
  });

  it('四档阈值常量冻结（70/85/95/100）', () => {
    expect(QUOTA_TIERS.map((t) => t.threshold).sort((a, b) => a - b)).toEqual([70, 85, 95, 100]);
  });
});

describe('配额闸门：预警事件去抖与错误码联动', () => {
  it('档位跃迁触发预警，同档重复不触发', () => {
    const prev = evaluateQuota(70, 100);
    const same = evaluateQuota(72, 100);
    const next = evaluateQuota(85, 100);
    expect(tierChanged(prev, same)).toBe(false);
    expect(tierChanged(prev, next)).toBe(true);
  });

  it('首次快照仅在达到 70% 时触发预警', () => {
    expect(tierChanged(null, evaluateQuota(50, 100))).toBe(false);
    expect(tierChanged(null, evaluateQuota(75, 100))).toBe(true);
  });

  it('100% 阻断错误码：新提交 QUOTA_EXCEEDED，运行中 BUDGET_EXHAUSTED', () => {
    expect(blockErrorCodeFor(true)).toBe('QUOTA_EXCEEDED');
    expect(blockErrorCodeFor(false)).toBe('BUDGET_EXHAUSTED');
  });
});
