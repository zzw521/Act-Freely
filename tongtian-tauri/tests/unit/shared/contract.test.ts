// tests/unit/shared/contract.test.ts — 契约快照门禁（流 A3）
// 冻结三层架构演进 P0 契约：14 态状态机、9 个新错误码、seq 事件、lease/quota 类型、IPC channel。
// 契约漂移时本测试失败，作为 CI 门禁。

import { describe, it, expect } from 'vitest';
import { TaskStatus } from '../../../src/shared/types';
import type { ChatEvent as ChatEventTypes, WaitReason, TaskMeta } from '../../../src/shared/types';
import {
  ErrorCode,
  IPC_CHANNELS,
} from '../../../src/shared/contracts';
import type {
  ChatEvent as ChatEventContracts,
  LeaseInfo,
  LeaseTakeoverResp,
  QuotaSnapshot,
  TaskSummary,
} from '../../../src/shared/contracts';

// ── 类型级断言工具（编译期由 tsc --noEmit 校验） ──────────
type Equal<X, Y> = (<T>() => T extends X ? 1 : 2) extends <T>() => T extends Y ? 1 : 2 ? true : false;
type AssertTrue<T extends true> = T;

// ChatEvent 双定义结构一致性：contracts 版必须可整体赋值给 types 版（防漂移）
type ChatEventCompat = AssertTrue<
  [ChatEventContracts] extends [ChatEventTypes] ? true : false
>;
const _chatEventCompatCheck: ChatEventCompat = true;
void _chatEventCompatCheck;

describe('契约快照：TaskStatus 14 态', () => {
  it('包含原 9 态且未被删改', () => {
    const legacy = [
      'idle', 'planning', 'pending_review', 'executing', 'paused',
      'delivering', 'done', 'failed', 'rolled_back',
    ];
    for (const s of legacy) {
      expect(Object.values(TaskStatus)).toContain(s);
    }
  });

  it('包含三层架构演进增量 5 态', () => {
    const added = ['queued', 'budget_paused', 'retry_pending', 'cancelled', 'archived'];
    for (const s of added) {
      expect(Object.values(TaskStatus)).toContain(s);
    }
    expect(Object.values(TaskStatus)).toHaveLength(14);
  });
});

describe('契约快照：ErrorCode 云端必需码', () => {
  it('包含 9 个三层架构演进错误码', () => {
    const cloudCodes = [
      'BUDGET_EXHAUSTED', 'QUEUE_TIMEOUT', 'LEASE_LOST',
      'FILE_REJECTED', 'SYNC_CONFLICT', 'PAYMENT_GRACE',
      'DATA_TIER_BLOCKED', 'INJECTION_BLOCKED', 'QUOTA_EXCEEDED',
    ];
    for (const code of cloudCodes) {
      expect(Object.values(ErrorCode)).toContain(code);
      // 枚举键与字面值一致（防拼写漂移）
      expect((ErrorCode as Record<string, string>)[code]).toBe(code);
    }
  });
});

describe('契约快照：IPC_CHANNELS 新增通道', () => {
  it('包含取消/任务状态/配额/租约/重放通道', () => {
    expect(IPC_CHANNELS.CHAT_CANCEL).toBe('chat:cancel');
    expect(IPC_CHANNELS.TASK_STATE).toBe('task:state');
    expect(IPC_CHANNELS.QUOTA_STATUS).toBe('quota:status');
    expect(IPC_CHANNELS.LEASE_INFO).toBe('lease:info');
    expect(IPC_CHANNELS.LEASE_TAKEOVER).toBe('lease:takeover');
    expect(IPC_CHANNELS.EVENT_REPLAY).toBe('event:replay');
  });

  it('通道名无重复', () => {
    const values = Object.values(IPC_CHANNELS);
    expect(new Set(values).size).toBe(values.length);
  });
});

describe('契约快照：ChatEvent 新增事件与 seq 兼容字段', () => {
  it('status_change / quota_warning / heartbeat 事件可构造且 seq 可选', () => {
    const ev1: ChatEventContracts = {
      type: 'status_change',
      taskId: 't1',
      status: TaskStatus.BUDGET_PAUSED,
      waitReason: 'budget_wait',
      statusReasonCode: 'BUDGET_EXHAUSTED',
      seq: 42,
    };
    expect(ev1.seq).toBe(42);

    const ev2: ChatEventContracts = {
      type: 'quota_warning',
      level: '100',
      action: 'block',
      usedTokens: 1000,
      quotaTokens: 1000,
    };
    expect(ev2.seq).toBeUndefined(); // seq 可选，缺省合法

    const ev3: ChatEventContracts = { type: 'heartbeat' };
    expect(ev3.type).toBe('heartbeat');
  });

  it('既有事件追加 seq/taskId 为可选（老负载零感知）', () => {
    const legacyPayload: ChatEventContracts = { type: 'done', taskId: 't1' };
    expect(legacyPayload.seq).toBeUndefined();
  });
});

describe('契约快照：Lease / Quota / TaskMeta 类型', () => {
  it('LeaseInfo 字段齐备（对齐 ADJ-R2-4）', () => {
    const lease: LeaseInfo = {
      taskId: 't1',
      leaseId: 'lease-1',
      holder: 'local-device',
      heartbeatAt: new Date().toISOString(),
      leaseUntil: new Date().toISOString(),
      idle: false,
    };
    expect(lease.holder).toBe('local-device');
  });

  it('LeaseTakeoverResp 禁止抢占语义（需确认）', () => {
    const resp: LeaseTakeoverResp = { needConfirmation: true, currentHolder: 'web' };
    expect(resp.needConfirmation).toBe(true);
  });

  it('QuotaSnapshot 四档与动作链枚举收敛', () => {
    const snap: QuotaSnapshot = {
      usedTokens: 85,
      quotaTokens: 100,
      tier: '85',
      action: 'degrade',
    };
    expect(['70', '85', '95', '100']).toContain(snap.tier);
    expect(['notify', 'degrade', 'queue', 'block']).toContain(snap.action);
  });

  it('TaskSummary.meta 可选（老调用方零感知）', () => {
    const legacy: TaskSummary = {
      id: 't1',
      title: 'demo',
      status: TaskStatus.DONE,
      createdAt: '',
      updatedAt: '',
    };
    expect(legacy.meta).toBeUndefined();

    const enriched: TaskSummary = {
      ...legacy,
      meta: { waitReason: 'quota_wait', queuePosition: 3, retryCount: 1 } satisfies TaskMeta,
    };
    expect(enriched.meta?.queuePosition).toBe(3);
  });

  it('WaitReason 联合类型覆盖 executing 子态与等待成因', () => {
    const reasons: WaitReason[] = [
      'rate_limited', 'circuit_break',
      'quota_wait', 'budget_wait', 'lease_wait',
      'user_pause', 'pending_review_ttl',
      null,
    ];
    expect(reasons).toHaveLength(8);
  });
});
