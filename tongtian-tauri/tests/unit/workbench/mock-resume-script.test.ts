/**
 * Mock 的断点续跑剧本（D-15 界面验收的数据源）
 *
 * 真实检查点只存在于引擎进程（或本机磁盘快照），Mock 没有引擎。带 [test:failstep] 的
 * 任务走「工具步骤失败 → resumable 检查点 → 单步续跑」剧本，让重试入口、重放预览与
 * 勾选确认在无后端的环境里能被完整跑通。本文件锁住它的口径：
 * - 只有这条剧本下发检查点，普通任务依然如实返回不可用；
 * - 续跑只重放失败的那一步，已成功的写入步骤不重做；
 * - 快照消费一次即作废，第二次 resume 仍回 CHECKPOINT_UNAVAILABLE。
 */

import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import type { ChatEvent } from '../../../src/shared/types';

/** 只声明本文件用到的四个通道方法，避免测试里到处写类型断言 */
interface MockApiSurface {
  chatSubmit(req: { goal: string }): Promise<{ taskId: string }>;
  chatReview(taskId: string, action: 'approve' | 'reject' | 'modify' | 'resume'): Promise<{ ok: boolean; status: string }>;
  chatCheckpointQuery(taskIds: string[]): Promise<{ checkpoints: Record<string, unknown> }>;
  onChatEvent(cb: (event: ChatEvent) => void): () => void;
}

let api: MockApiSurface;

function picked(events: ChatEvent[], type: string): ChatEvent[] {
  return events.filter((e) => e.type === type);
}

const field = <K extends string>(event: ChatEvent, key: K) =>
  (event as unknown as Record<K, unknown>)[key];

beforeAll(async () => {
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};
  await import('../../../src/renderer/lib/mock-api');
  api = (globalThis.window as unknown as { tongtianAPI: MockApiSurface }).tongtianAPI;
});

afterEach(() => {
  vi.useRealTimers();
});

/** 提交并批准一条任务，返回事件收集器与 taskId */
async function startTask(goal: string) {
  const events: ChatEvent[] = [];
  const off = api.onChatEvent((event) => events.push(event));
  const { taskId } = await api.chatSubmit({ goal });
  await vi.advanceTimersByTimeAsync(800);
  await api.chatReview(taskId, 'approve');
  return { events, off, taskId };
}

describe('Mock 断点续跑剧本', () => {
  it('剧本任务：失败步骤、resumable 检查点与失败收尾按顺序落到事件流', async () => {
    vi.useFakeTimers();
    const { events, off, taskId } = await startTask('[test:failstep] 整理季度汇报');
    await vi.advanceTimersByTimeAsync(3000);

    expect(picked(events, 'step_failed').map((e) => field(e, 'stepId'))).toEqual(['step-3']);
    expect(picked(events, 'step_start').map((e) => field(e, 'tool'))).toEqual([
      'view_text_file',
      'write_text_file',
      'generate_pptx',
    ]);

    const checkpoint = picked(events, 'checkpoint')[0];
    expect(field(checkpoint, 'resumable')).toBe(true);
    expect(field(checkpoint, 'taskId')).toBe(taskId);
    // Mock 没有进程外持久化：如实标 same-process，界面才会提示「引擎重启后无法恢复」
    expect(field(checkpoint, 'scope')).toBe('same-process');
    expect(picked(events, 'status_change').map((e) => field(e, 'status'))).toContain('failed');
    off();
  });

  it('续跑只重放失败那一步，快照消费后再次 resume 如实回报不可用', async () => {
    vi.useFakeTimers();
    const { events, off, taskId } = await startTask('[test:failstep] 生成汇报 PPT');
    await vi.advanceTimersByTimeAsync(3000);
    events.length = 0;

    const query = await api.chatCheckpointQuery([taskId, 'mock-task-不存在']);
    expect(Object.keys(query.checkpoints)).toEqual([taskId]);

    const resumed = await api.chatReview(taskId, 'resume');
    await vi.advanceTimersByTimeAsync(2000);

    expect(resumed.ok).toBe(true);
    // 只有失败的那一步重新执行：已成功的读取与写入步骤不再出现 step_start
    expect(picked(events, 'step_start').map((e) => field(e, 'stepId'))).toEqual(['step-3']);
    expect(picked(events, 'step_done').map((e) => field(e, 'stepId'))).toEqual(['step-3']);
    expect(field(picked(events, 'deliver')[0], 'final_text')).toContain('只重新执行了失败的 generate_pptx');

    // 快照已消费：查询为空、再 resume 明确不可用，界面不会留一个跑不起来的入口
    expect(Object.keys((await api.chatCheckpointQuery([taskId])).checkpoints)).toEqual([]);
    expect(await api.chatReview(taskId, 'resume')).toEqual({ ok: false, status: 'CHECKPOINT_UNAVAILABLE' });
    off();
  });

  it('普通任务不受剧本影响：不下发检查点，resume 依然回报不可用', async () => {
    vi.useFakeTimers();
    const { events, off, taskId } = await startTask('整理本周销售数据');
    await vi.advanceTimersByTimeAsync(4500);

    expect(picked(events, 'checkpoint')).toEqual([]);
    expect(picked(events, 'step_failed')).toEqual([]);
    const resumed = await api.chatReview(taskId, 'resume');
    expect(resumed.ok).toBe(false);
    expect(resumed.status).toBe('CHECKPOINT_UNAVAILABLE');
    off();
  });
});
