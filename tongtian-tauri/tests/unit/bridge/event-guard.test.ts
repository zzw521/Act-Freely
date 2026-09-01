// tests/unit/bridge/event-guard.test.ts — 前端事件流守卫测试（流 G2/H2）
// 覆盖：seq 去重、缺口检测、status_change 幂等、无 seq 直通。

import { describe, it, expect, vi } from 'vitest';
import { EventStreamGuard } from '../../../src/renderer/lib/event-stream-guard';
import type { ChatEvent } from '../../../src/shared/types';

function ev(partial: Record<string, unknown>): ChatEvent {
  return partial as unknown as ChatEvent;
}

describe('EventStreamGuard：seq 去重', () => {
  it('重复 seq 事件被丢弃（重放场景防重复渲染）', () => {
    const guard = new EventStreamGuard();
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 5 }))).not.toBeNull();
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 5 }))).toBeNull();
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 3 }))).toBeNull(); // 乱序旧事件
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 6 }))).not.toBeNull();
  });

  it('不同任务的 seq 游标相互独立', () => {
    const guard = new EventStreamGuard();
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 10 }))).not.toBeNull();
    expect(guard.accept(ev({ type: 'done', taskId: 't2', seq: 1 }))).not.toBeNull();
    expect(guard.accept(ev({ type: 'done', taskId: 't2', seq: 1 }))).toBeNull();
  });

  it('无 seq 事件直通（旧引擎/Mock 契约兼容）', () => {
    const guard = new EventStreamGuard();
    expect(guard.accept(ev({ type: 'done', taskId: 't1' }))).not.toBeNull();
    expect(guard.accept(ev({ type: 'done', taskId: 't1' }))).not.toBeNull();
  });
});

describe('EventStreamGuard：缺口检测', () => {
  it('seq 缺口触发 onGap 回调（可触发 eventReplay 补齐）', () => {
    const onGap = vi.fn();
    const guard = new EventStreamGuard({ onGap });
    guard.accept(ev({ type: 'step_start', taskId: 't1', seq: 1 }));
    guard.accept(ev({ type: 'step_done', taskId: 't1', seq: 4 })); // gap: 2,3 缺失
    expect(onGap).toHaveBeenCalledTimes(1);
    expect(onGap).toHaveBeenCalledWith('t1', 2, 4);
  });

  it('连续事件不触发 onGap', () => {
    const onGap = vi.fn();
    const guard = new EventStreamGuard({ onGap });
    guard.accept(ev({ type: 'step_start', taskId: 't1', seq: 1 }));
    guard.accept(ev({ type: 'step_done', taskId: 't1', seq: 2 }));
    guard.accept(ev({ type: 'done', taskId: 't1', seq: 3 }));
    expect(onGap).not.toHaveBeenCalled();
  });
});

describe('EventStreamGuard：status_change 幂等', () => {
  it('同状态重复推送仅保留首次', () => {
    const guard = new EventStreamGuard();
    const s1 = guard.accept(ev({ type: 'status_change', taskId: 't1', status: 'executing', seq: 1 }));
    const s2 = guard.accept(ev({ type: 'status_change', taskId: 't1', status: 'executing', seq: 2 }));
    const s3 = guard.accept(ev({ type: 'status_change', taskId: 't1', status: 'budget_paused', seq: 3 }));
    expect(s1).not.toBeNull();
    expect(s2).toBeNull();
    expect(s3).not.toBeNull();
  });
});

describe('EventStreamGuard：wrap 与清理', () => {
  it('wrap 仅放行通过守卫的事件', () => {
    const guard = new EventStreamGuard();
    const received: ChatEvent[] = [];
    const wrapped = guard.wrap((e) => received.push(e));
    wrapped(ev({ type: 'done', taskId: 't1', seq: 1 }));
    wrapped(ev({ type: 'done', taskId: 't1', seq: 1 })); // 重复
    expect(received).toHaveLength(1);
  });

  it('dispose 后游标重置（任务终态清理）', () => {
    const guard = new EventStreamGuard();
    guard.accept(ev({ type: 'done', taskId: 't1', seq: 9 }));
    guard.dispose('t1');
    // 清理后 seq=1 重新放行（新任务复用 taskId 场景）
    expect(guard.accept(ev({ type: 'done', taskId: 't1', seq: 1 }))).not.toBeNull();
  });
});
