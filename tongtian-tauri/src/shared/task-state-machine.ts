// src/shared/task-state-machine.ts — 14 态任务状态机（流 B1）
//
// 依据：docs/changes/agent-platform-evolution/experts/03-business.md §2.1
// （14 态 + wait_reason 子态 + 35 条合法转移，需求分析报告 P0：先于 Phase B 冻结）
//
// 设计原则：
// 1. 纯函数、零依赖：renderer / 测试 / Rust 侧契约文档三方共用同一转移表
// 2. 本地 local-first 退化：queued 入队即出队（<100ms）、retry_pending 走本地退避，
//    转移表保持单一版本，云端接入时零改动切换
// 3. 非法转移显式拒绝（抛 IllegalTransitionError），不静默吞掉
// 4. 不动 src/main/agent/state-machine.ts（Electron 遗留，未被 Tauri 构建引用）

import { TaskStatus } from './types';
import type { WaitReason } from './types';

/** 状态机转移事件 */
export type TransitionEvent =
  | 'submit'             // (创建)→queued：chat_submit 受理
  | 'acquire_lease'      // queued→planning：获取 lease（本地立即成功）
  | 'cancel'             // →cancelled：用户取消
  | 'queue_timeout'      // queued→failed：QUEUE_TIMEOUT 10min
  | 'plan_ready'         // planning→pending_review：Plan 事件
  | 'retryable_error'    // planning/executing/delivering→retry_pending（retry_count<3）
  | 'fatal_error'        // →failed：不可重试错误（安全类不重试）
  | 'approve'            // pending_review→executing
  | 'reject'             // pending_review→done（status_reason_code=REJECTED）
  | 'modify'             // pending_review→planning（携带 StepPatch）
  | 'review_ttl'         // pending_review→archived：7 天未审阅
  | 'all_steps_done'     // executing→delivering
  | 'pause'              // executing→paused
  | 'budget_exhausted'   // executing→budget_paused：BUDGET_EXHAUSTED
  | 'lease_lost'         // executing→queued：心跳超时 60s，从最后 success Step 恢复
  | 'resume'             // paused→executing
  | 'rollback_to'        // paused→rolled_back
  | 'continue_from_rollback' // rolled_back→executing：从目标 Step 续跑
  | 'replan'             // rolled_back→planning：需重新规划
  | 'budget_restored'    // budget_paused→executing：按入队 FIFO 恢复
  | 'budget_ttl'         // budget_paused→failed：24h 未恢复
  | 'backoff_done'       // retry_pending→executing：退避结束
  | 'retry_exhausted'    // retry_pending→failed：retry_count≥3
  | 'deliver_done'       // delivering→done：产物入库成功
  | 'manual_retry'       // failed→queued：用户手动重试（retry_count 累计不重置）
  | 'archive';           // done/failed→archived：手动或 30 天自动

export interface TaskTransition {
  from: TaskStatus;
  event: TransitionEvent;
  to: TaskStatus;
}

/** 重试次数上限（转移 #6/#15/#30/#32 条件：retry_count<3） */
export const MAX_RETRY_COUNT = 3;

/** 本地 local-first 退化参数：queued 停留目标 <100ms（ADJ-R2-3 语义镜像） */
export const LOCAL_QUEUE_TARGET_MS = 100;

/**
 * 合法转移全集（35 条，编号对齐 03-business.md §2.1 表格）。
 * 转移 #20（executing.wait_reason 子态恢复）不是状态转移，
 * 由 setWaitReason(executing, null) 表达，避免状态爆炸。
 * 转移 #35 覆盖 done 与 failed 两个来源态，计 2 行。
 */
export const TRANSITIONS: readonly TaskTransition[] = [
  // #1 (创建)→queued：以 IDLE 表达创建前态
  { from: TaskStatus.IDLE, event: 'submit', to: TaskStatus.QUEUED },
  // #2
  { from: TaskStatus.QUEUED, event: 'acquire_lease', to: TaskStatus.PLANNING },
  // #3
  { from: TaskStatus.QUEUED, event: 'cancel', to: TaskStatus.CANCELLED },
  // #4
  { from: TaskStatus.QUEUED, event: 'queue_timeout', to: TaskStatus.FAILED },
  // #5
  { from: TaskStatus.PLANNING, event: 'plan_ready', to: TaskStatus.PENDING_REVIEW },
  // #6（guard：retryCount<3，否则路由到 #7）
  { from: TaskStatus.PLANNING, event: 'retryable_error', to: TaskStatus.RETRY_PENDING },
  // #7
  { from: TaskStatus.PLANNING, event: 'fatal_error', to: TaskStatus.FAILED },
  // #8
  { from: TaskStatus.PLANNING, event: 'cancel', to: TaskStatus.CANCELLED },
  // #9
  { from: TaskStatus.PENDING_REVIEW, event: 'approve', to: TaskStatus.EXECUTING },
  // #10
  { from: TaskStatus.PENDING_REVIEW, event: 'reject', to: TaskStatus.DONE },
  // #11
  { from: TaskStatus.PENDING_REVIEW, event: 'modify', to: TaskStatus.PLANNING },
  // #12
  { from: TaskStatus.PENDING_REVIEW, event: 'review_ttl', to: TaskStatus.ARCHIVED },
  // #13
  { from: TaskStatus.EXECUTING, event: 'all_steps_done', to: TaskStatus.DELIVERING },
  // #14
  { from: TaskStatus.EXECUTING, event: 'pause', to: TaskStatus.PAUSED },
  // #15（guard：retryCount<3）
  { from: TaskStatus.EXECUTING, event: 'retryable_error', to: TaskStatus.RETRY_PENDING },
  // #16
  { from: TaskStatus.EXECUTING, event: 'fatal_error', to: TaskStatus.FAILED },
  // #17
  { from: TaskStatus.EXECUTING, event: 'budget_exhausted', to: TaskStatus.BUDGET_PAUSED },
  // #18
  { from: TaskStatus.EXECUTING, event: 'lease_lost', to: TaskStatus.QUEUED },
  // #19
  { from: TaskStatus.EXECUTING, event: 'cancel', to: TaskStatus.CANCELLED },
  // #21
  { from: TaskStatus.PAUSED, event: 'resume', to: TaskStatus.EXECUTING },
  // #22
  { from: TaskStatus.PAUSED, event: 'rollback_to', to: TaskStatus.ROLLED_BACK },
  // #23
  { from: TaskStatus.PAUSED, event: 'cancel', to: TaskStatus.CANCELLED },
  // #24
  { from: TaskStatus.ROLLED_BACK, event: 'continue_from_rollback', to: TaskStatus.EXECUTING },
  // #25
  { from: TaskStatus.ROLLED_BACK, event: 'replan', to: TaskStatus.PLANNING },
  // #26
  { from: TaskStatus.BUDGET_PAUSED, event: 'budget_restored', to: TaskStatus.EXECUTING },
  // #27
  { from: TaskStatus.BUDGET_PAUSED, event: 'budget_ttl', to: TaskStatus.FAILED },
  // #28
  { from: TaskStatus.BUDGET_PAUSED, event: 'cancel', to: TaskStatus.CANCELLED },
  // #29
  { from: TaskStatus.RETRY_PENDING, event: 'backoff_done', to: TaskStatus.EXECUTING },
  // #30
  { from: TaskStatus.RETRY_PENDING, event: 'retry_exhausted', to: TaskStatus.FAILED },
  // #31
  { from: TaskStatus.DELIVERING, event: 'deliver_done', to: TaskStatus.DONE },
  // #32（guard：retryCount<3）
  { from: TaskStatus.DELIVERING, event: 'retryable_error', to: TaskStatus.RETRY_PENDING },
  // #33
  { from: TaskStatus.DELIVERING, event: 'fatal_error', to: TaskStatus.FAILED },
  // #34
  { from: TaskStatus.FAILED, event: 'manual_retry', to: TaskStatus.QUEUED },
  // #35（done/failed 各一行）
  { from: TaskStatus.DONE, event: 'archive', to: TaskStatus.ARCHIVED },
  { from: TaskStatus.FAILED, event: 'archive', to: TaskStatus.ARCHIVED },
] as const;

/** 终态集合：不再有出边（archived/cancelled/done 中 done 仅保留 archive 边） */
export const TERMINAL_STATUSES: readonly TaskStatus[] = [
  TaskStatus.CANCELLED,
  TaskStatus.ARCHIVED,
];

/** 转移上下文（guard 判定用） */
export interface TransitionContext {
  /** 当前已重试次数；retryable_error 时 ≥MAX_RETRY_COUNT 路由到 failed（#6/#15/#32 条件） */
  retryCount?: number;
}

export class IllegalTransitionError extends Error {
  constructor(
    public readonly from: TaskStatus,
    public readonly event: TransitionEvent,
  ) {
    super(`非法状态转移: ${from} --[${event}]--> ?（不在 35 条合法转移内）`);
    this.name = 'IllegalTransitionError';
  }
}

function findTransition(from: TaskStatus, event: TransitionEvent): TaskTransition | undefined {
  return TRANSITIONS.find((t) => t.from === from && t.event === event);
}

/**
 * 判定转移是否合法。
 * retryable_error 在 retryCount≥3 时视为非法（应走 fatal_error/retry_exhausted 路径）。
 */
export function canTransition(
  from: TaskStatus,
  event: TransitionEvent,
  ctx?: TransitionContext,
): boolean {
  if (event === 'retryable_error' && (ctx?.retryCount ?? 0) >= MAX_RETRY_COUNT) {
    return false;
  }
  return findTransition(from, event) !== undefined;
}

/**
 * 执行转移，返回目标状态；非法转移抛 IllegalTransitionError。
 * retryable_error 且 retryCount≥3 时按 #7 语义路由到 failed（重试耗尽）。
 */
export function transition(
  from: TaskStatus,
  event: TransitionEvent,
  ctx?: TransitionContext,
): TaskStatus {
  if (event === 'retryable_error' && (ctx?.retryCount ?? 0) >= MAX_RETRY_COUNT) {
    // 重试耗尽：planning/executing/delivering 均允许落到 failed（#7/#16/#33 兜底语义）
    const exhaustedSources: TaskStatus[] = [
      TaskStatus.PLANNING,
      TaskStatus.EXECUTING,
      TaskStatus.DELIVERING,
    ];
    if (exhaustedSources.includes(from)) {
      return TaskStatus.FAILED;
    }
  }
  const t = findTransition(from, event);
  if (!t) {
    throw new IllegalTransitionError(from, event);
  }
  return t.to;
}

/** executing 子态管理（转移 #20）：限流等待不设独立状态，以 wait_reason 表达 */
export function applyWaitReason(
  status: TaskStatus,
  reason: WaitReason,
): { status: TaskStatus; waitReason: WaitReason } {
  if (status !== TaskStatus.EXECUTING) {
    throw new IllegalTransitionError(status, 'pause'); // 仅 executing 允许子态标注
  }
  return { status, waitReason: reason };
}

/** 是否终态（无后续出边） */
export function isTerminal(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** 某状态的全部合法事件（供 UI 动态渲染可用操作） */
export function availableEvents(from: TaskStatus): TransitionEvent[] {
  return TRANSITIONS.filter((t) => t.from === from).map((t) => t.event);
}
