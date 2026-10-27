/**
 * 免审阅自动批准的时序契约
 *
 * 引擎的计划事件先到，Rust 要等计划流收尾才把任务写成 pending_review；
 * 立即批准会被 chat_review 的状态校验拒绝。此前这个错误被 catch 吞成静默，
 * 结果任务停在计划阶段、对话里没有任何回复。这里锁住修复：
 * - 「还没到待审阅」退避重试，直到批准被接受
 * - 非时序错误（配额/租约）立即上报，不空转
 * - 同一任务只批准一次（plan 与 pending_review 都会触发）
 * - 计划事件不再提前改成 executing，避免计划流的 done 把任务误收尾
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { approvePlanWhenReady } from '../../../src/renderer/store/workbenchStore';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const storeSrc = () => read('../../../src/renderer/store/workbenchStore.ts');

afterEach(() => {
  vi.useRealTimers();
});

describe('approvePlanWhenReady：批准时机', () => {
  it('计划流未收尾导致批准被拒 → 退避重试到 pending_review 后被接受', async () => {
    vi.useFakeTimers();
    let attempt = 0;
    const review = vi.fn(async () => {
      attempt += 1;
      if (attempt < 3) throw 'VALIDATION_ERROR: 仅 pending_review 可批准（当前 planning）';
      return { ok: true, status: 'executing' };
    });
    const onFailure = vi.fn();

    const pending = approvePlanWhenReady('task-approve-1', onFailure, review);
    await vi.advanceTimersByTimeAsync(400);
    await pending;

    expect(review).toHaveBeenCalledTimes(3);
    expect(onFailure).not.toHaveBeenCalled();
  });

  it('非时序错误立即上报，不空转重试', async () => {
    const review = vi.fn(async () => {
      throw 'TASK_ALREADY_RUNNING: 其他端持有活跃租约';
    });
    const onFailure = vi.fn();

    await approvePlanWhenReady('task-approve-2', onFailure, review);

    expect(review).toHaveBeenCalledTimes(1);
    expect(onFailure).toHaveBeenCalledWith(expect.stringContaining('其他端持有活跃租约'));
  });

  it('任务已取消/失败后，迟到的批准不再重试', async () => {
    const review = vi.fn(async () => {
      throw 'VALIDATION_ERROR: 仅 pending_review 可批准（当前 cancelled）';
    });
    const onFailure = vi.fn();

    await approvePlanWhenReady('task-approve-3', onFailure, review);

    expect(review).toHaveBeenCalledTimes(1);
  });

  it('同一任务重复触发只批准一次', async () => {
    const review = vi.fn(async () => ({ ok: true }));

    await Promise.all([
      approvePlanWhenReady('task-approve-4', vi.fn(), review),
      approvePlanWhenReady('task-approve-4', vi.fn(), review),
    ]);

    expect(review).toHaveBeenCalledTimes(1);
  });
});

describe('store：自动批准接线', () => {
  it('计划事件与 pending_review 都触发批准，且批准错误不再被吞掉', () => {
    const src = storeSrc();
    const plan = src.split("case 'plan': {")[1].split("case 'step_done':")[0];
    expect(plan).toContain('approvePlanWhenReady(');
    expect(plan).not.toContain('.catch(() => undefined)');
    // 计划流的 done 紧跟其后：提前改成 executing 会被它误判成任务收尾
    expect(plan).toContain('taskStatus: TaskStatus.PENDING_REVIEW');
    expect(plan).not.toContain('TaskStatus.EXECUTING');

    const status = src.split("case 'status_change': {")[1].split("case 'quota_warning':")[0];
    expect(status).toContain('approvePlanWhenReady(');
    expect(status).toContain('incoming === TaskStatus.PENDING_REVIEW');
  });

  it('批准失败写回会话：结束 loading 并落到 failed，不覆盖已终态任务', () => {
    const src = storeSrc();
    const reporter = src
      .split('reportAutoApproveFailure = (convId, message) => {')[1]
      .split('\n  };')[0];
    expect(reporter).toContain('isLoading: false');
    expect(reporter).toContain('taskStatus: TaskStatus.FAILED');
    expect(reporter).toContain('error: message');
    expect(reporter).toContain(
      'conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED',
    );
  });

  it('事件自带 taskId 时优先用它批准，避免提交响应未回填时批错上一轮任务', () => {
    const src = storeSrc();
    expect(src).toContain('eventTaskId?: string | null');
    expect(src.match(/const taskId = eventTaskId \?\? conv\.taskId;/g)?.length).toBe(2);
  });
});
