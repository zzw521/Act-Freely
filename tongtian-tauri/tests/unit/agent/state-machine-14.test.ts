// tests/unit/agent/state-machine-14.test.ts — 14 态状态机转移表测试（流 B1）
// 覆盖 35 条合法转移 + 非法转移拒绝 + wait_reason 子态 + retry guard。

import { describe, it, expect } from 'vitest';
import { TaskStatus } from '../../../src/shared/types';
import {
  TRANSITIONS,
  MAX_RETRY_COUNT,
  canTransition,
  transition,
  applyWaitReason,
  isTerminal,
  availableEvents,
  IllegalTransitionError,
} from '../../../src/shared/task-state-machine';
import type { TransitionEvent } from '../../../src/shared/task-state-machine';

describe('14 态状态机：转移表完整性', () => {
  it('共 35 条合法转移（对齐 03-business.md §2.1）', () => {
    expect(TRANSITIONS).toHaveLength(35);
  });

  it('无重复的 (from, event) 对（转移确定性）', () => {
    const keys = TRANSITIONS.map((t) => `${t.from}|${t.event}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('全部 14 个状态枚举值可达（除 IDLE 为创建前态）', () => {
    const targets = new Set(TRANSITIONS.map((t) => t.to));
    const reachable = [
      'queued', 'planning', 'pending_review', 'executing', 'paused',
      'delivering', 'done', 'failed', 'rolled_back',
      'budget_paused', 'retry_pending', 'cancelled', 'archived',
    ];
    for (const s of reachable) {
      expect(targets.has(s as TaskStatus)).toBe(true);
    }
  });
});

describe('14 态状态机：35 条合法转移逐条验证', () => {
  const cases: Array<[number, TaskStatus, TransitionEvent, TaskStatus]> = [
    // #1
    [1, TaskStatus.IDLE, 'submit', TaskStatus.QUEUED],
    // #2
    [2, TaskStatus.QUEUED, 'acquire_lease', TaskStatus.PLANNING],
    // #3
    [3, TaskStatus.QUEUED, 'cancel', TaskStatus.CANCELLED],
    // #4
    [4, TaskStatus.QUEUED, 'queue_timeout', TaskStatus.FAILED],
    // #5
    [5, TaskStatus.PLANNING, 'plan_ready', TaskStatus.PENDING_REVIEW],
    // #6
    [6, TaskStatus.PLANNING, 'retryable_error', TaskStatus.RETRY_PENDING],
    // #7
    [7, TaskStatus.PLANNING, 'fatal_error', TaskStatus.FAILED],
    // #8
    [8, TaskStatus.PLANNING, 'cancel', TaskStatus.CANCELLED],
    // #9
    [9, TaskStatus.PENDING_REVIEW, 'approve', TaskStatus.EXECUTING],
    // #10
    [10, TaskStatus.PENDING_REVIEW, 'reject', TaskStatus.DONE],
    // #11
    [11, TaskStatus.PENDING_REVIEW, 'modify', TaskStatus.PLANNING],
    // #12
    [12, TaskStatus.PENDING_REVIEW, 'review_ttl', TaskStatus.ARCHIVED],
    // #13
    [13, TaskStatus.EXECUTING, 'all_steps_done', TaskStatus.DELIVERING],
    // #14
    [14, TaskStatus.EXECUTING, 'pause', TaskStatus.PAUSED],
    // #15
    [15, TaskStatus.EXECUTING, 'retryable_error', TaskStatus.RETRY_PENDING],
    // #16
    [16, TaskStatus.EXECUTING, 'fatal_error', TaskStatus.FAILED],
    // #17
    [17, TaskStatus.EXECUTING, 'budget_exhausted', TaskStatus.BUDGET_PAUSED],
    // #18
    [18, TaskStatus.EXECUTING, 'lease_lost', TaskStatus.QUEUED],
    // #19
    [19, TaskStatus.EXECUTING, 'cancel', TaskStatus.CANCELLED],
    // #21
    [21, TaskStatus.PAUSED, 'resume', TaskStatus.EXECUTING],
    // #22
    [22, TaskStatus.PAUSED, 'rollback_to', TaskStatus.ROLLED_BACK],
    // #23
    [23, TaskStatus.PAUSED, 'cancel', TaskStatus.CANCELLED],
    // #24
    [24, TaskStatus.ROLLED_BACK, 'continue_from_rollback', TaskStatus.EXECUTING],
    // #25
    [25, TaskStatus.ROLLED_BACK, 'replan', TaskStatus.PLANNING],
    // #26
    [26, TaskStatus.BUDGET_PAUSED, 'budget_restored', TaskStatus.EXECUTING],
    // #27
    [27, TaskStatus.BUDGET_PAUSED, 'budget_ttl', TaskStatus.FAILED],
    // #28
    [28, TaskStatus.BUDGET_PAUSED, 'cancel', TaskStatus.CANCELLED],
    // #29
    [29, TaskStatus.RETRY_PENDING, 'backoff_done', TaskStatus.EXECUTING],
    // #30
    [30, TaskStatus.RETRY_PENDING, 'retry_exhausted', TaskStatus.FAILED],
    // #31
    [31, TaskStatus.DELIVERING, 'deliver_done', TaskStatus.DONE],
    // #32
    [32, TaskStatus.DELIVERING, 'retryable_error', TaskStatus.RETRY_PENDING],
    // #33
    [33, TaskStatus.DELIVERING, 'fatal_error', TaskStatus.FAILED],
    // #34
    [34, TaskStatus.FAILED, 'manual_retry', TaskStatus.QUEUED],
    // #35
    [35, TaskStatus.DONE, 'archive', TaskStatus.ARCHIVED],
    [36, TaskStatus.FAILED, 'archive', TaskStatus.ARCHIVED],
  ];

  it.each(cases)(
    '转移 #%i: %s --[%s]--> %s',
    (_no, from, event, to) => {
      expect(canTransition(from, event)).toBe(true);
      expect(transition(from, event)).toBe(to);
    },
  );
});

describe('14 态状态机：非法转移拒绝', () => {
  it('终态 cancelled/archived 无出边', () => {
    expect(isTerminal(TaskStatus.CANCELLED)).toBe(true);
    expect(isTerminal(TaskStatus.ARCHIVED)).toBe(true);
    expect(availableEvents(TaskStatus.CANCELLED)).toHaveLength(0);
    expect(availableEvents(TaskStatus.ARCHIVED)).toHaveLength(0);
  });

  it('非法转移抛出 IllegalTransitionError', () => {
    expect(() => transition(TaskStatus.DONE, 'approve')).toThrow(IllegalTransitionError);
    expect(() => transition(TaskStatus.QUEUED, 'pause')).toThrow(IllegalTransitionError);
    expect(() => transition(TaskStatus.IDLE, 'archive')).toThrow(IllegalTransitionError);
    expect(canTransition(TaskStatus.CANCELLED, 'resume')).toBe(false);
  });

  it('executing 不可直接 archive（须先 done/failed）', () => {
    expect(canTransition(TaskStatus.EXECUTING, 'archive')).toBe(false);
  });
});

describe('14 态状态机：retry guard（重试上限 3）', () => {
  it('retryable_error 在 retryCount<3 时进 retry_pending', () => {
    expect(transition(TaskStatus.EXECUTING, 'retryable_error', { retryCount: 2 }))
      .toBe(TaskStatus.RETRY_PENDING);
  });

  it('retryable_error 在 retryCount≥3 时路由到 failed（重试耗尽）', () => {
    expect(transition(TaskStatus.EXECUTING, 'retryable_error', { retryCount: MAX_RETRY_COUNT }))
      .toBe(TaskStatus.FAILED);
    expect(transition(TaskStatus.PLANNING, 'retryable_error', { retryCount: 5 }))
      .toBe(TaskStatus.FAILED);
    expect(transition(TaskStatus.DELIVERING, 'retryable_error', { retryCount: 3 }))
      .toBe(TaskStatus.FAILED);
  });

  it('retry_exhausted 在非 retry_pending 态非法', () => {
    expect(() => transition(TaskStatus.EXECUTING, 'retry_exhausted')).toThrow(IllegalTransitionError);
  });

  it('手动重试 retry_count 累计不重置（#34 语义由调用方保持）', () => {
    // 状态机只负责转移合法性；retryCount 累计由 TaskRegistry 维护
    expect(transition(TaskStatus.FAILED, 'manual_retry')).toBe(TaskStatus.QUEUED);
  });
});

describe('14 态状态机：wait_reason 子态（转移 #20）', () => {
  it('仅 executing 允许标注 wait_reason', () => {
    const r = applyWaitReason(TaskStatus.EXECUTING, 'rate_limited');
    expect(r.status).toBe(TaskStatus.EXECUTING);
    expect(r.waitReason).toBe('rate_limited');
  });

  it('退避窗口结束清除子态（rate_limited→正常）', () => {
    const r = applyWaitReason(TaskStatus.EXECUTING, null);
    expect(r.waitReason).toBeNull();
  });

  it('非 executing 态标注子态抛错', () => {
    expect(() => applyWaitReason(TaskStatus.PAUSED, 'rate_limited')).toThrow(IllegalTransitionError);
  });
});

describe('14 态状态机：本地 local-first 退化路径', () => {
  it('完整本地闭环：submit→acquire_lease→approve→deliver（queued 即出队）', () => {
    let s = transition(TaskStatus.IDLE, 'submit');
    expect(s).toBe(TaskStatus.QUEUED);
    s = transition(s, 'acquire_lease'); // 本地配额恒有余量，<100ms
    expect(s).toBe(TaskStatus.PLANNING);
    s = transition(s, 'plan_ready');
    s = transition(s, 'approve');
    expect(s).toBe(TaskStatus.EXECUTING);
    s = transition(s, 'all_steps_done');
    s = transition(s, 'deliver_done');
    expect(s).toBe(TaskStatus.DONE);
  });

  it('预算暂停全链路：executing→budget_paused→budget_restored→executing', () => {
    let s = transition(TaskStatus.EXECUTING, 'budget_exhausted');
    expect(s).toBe(TaskStatus.BUDGET_PAUSED);
    s = transition(s, 'budget_restored');
    expect(s).toBe(TaskStatus.EXECUTING);
  });

  it('lease 丢失恢复链路：executing→queued→planning', () => {
    let s = transition(TaskStatus.EXECUTING, 'lease_lost');
    expect(s).toBe(TaskStatus.QUEUED);
    s = transition(s, 'acquire_lease');
    expect(s).toBe(TaskStatus.PLANNING);
  });
});
