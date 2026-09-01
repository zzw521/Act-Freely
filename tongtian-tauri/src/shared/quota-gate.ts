// src/shared/quota-gate.ts — 配额四档闸门纯函数（流 F1）
//
// 依据：需求分析报告 ADJ-R5-4（预警档位 70/85/95/100%，超限动作顺序 降级→排队→阻断）
// 纯函数实现，renderer/Rust 镜像实现（src-tauri/src/task/quota.rs）共用同一阈值语义，
// 便于单元测试覆盖四档边界。

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
