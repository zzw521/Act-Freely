/**
 * 定时任务本地台账持久化测试（开发/联调阶段刷新不丢数据）
 *
 * 本文件单独存在的原因：需要在安装 localStorage 假实现后重新加载模块，
 * 以验证「hydrate 恢复」路径；vitest 每个测试文件独立模块注册表，互不干扰。
 */

import { describe, it, expect, beforeEach } from 'vitest';

/** 2026-03-05T00:00:00Z（周四） */
const BASE = Date.UTC(2026, 2, 5, 0, 0, 0);
const HOUR = 3_600_000;

function installFakeStorage(): Map<string, string> {
  const store = new Map<string, string>();
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  return store;
}

let sched: typeof import('../../../src/renderer/lib/schedule-local');

beforeEach(async () => {
  installFakeStorage();
  const { vi } = await import('vitest');
  vi.resetModules();
  sched = await import('../../../src/renderer/lib/schedule-local');
});

describe('schedule-local：本地台账跨刷新持久化', () => {
  it('任务与执行记录写入本地存储，重新加载模块后完整恢复', async () => {
    sched.resetLocalSchedules();
    const t = sched.createLocalSchedule(
      { name: '每日舆情', description: '汇总品牌舆情', freq: 'daily', timeOfDay: '08:00' },
      BASE,
    );
    sched.recordLocalRun(t.scheduleId, 'succeeded', '执行完成', BASE + HOUR, 'task-1');

    // 模拟页面刷新：内存态丢弃后重新加载模块
    const { vi } = await import('vitest');
    vi.resetModules();
    const reloaded = await import('../../../src/renderer/lib/schedule-local');

    const tasks = reloaded.localSchedules();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].scheduleId).toBe(t.scheduleId);
    expect(tasks[0].name).toBe('每日舆情');
    expect(tasks[0].lastStatus).toBe('succeeded');
    expect(tasks[0].lastTaskId).toBe('task-1');
    expect(tasks[0].nextRunAtMs).toBe(BASE + 8 * HOUR);

    const runs = reloaded.localRuns();
    expect(runs).toHaveLength(1);
    expect(runs[0].summary).toBe('执行完成');

    // 序号一并恢复：同一毫秒新建也不会与历史 id 冲突
    const again = reloaded.createLocalSchedule(
      { name: '再来一个', description: 'd', freq: 'daily', timeOfDay: '09:00' },
      BASE,
    );
    expect(again.scheduleId).not.toBe(t.scheduleId);
    expect(reloaded.localSchedules()).toHaveLength(2);
  });

  it('本地数据损坏时以空台账启动，不抛错阻塞页面', async () => {
    const store = installFakeStorage();
    store.set('tt:schedules', '{ 不是合法 JSON');
    const { vi } = await import('vitest');
    vi.resetModules();
    const fresh = await import('../../../src/renderer/lib/schedule-local');
    expect(fresh.localSchedules()).toEqual([]);
    expect(fresh.localRuns()).toEqual([]);
  });

  it('编辑结果同样落本地存储（刷新后保留改名与所属工作空间）', async () => {
    const t = sched.createLocalSchedule(
      { name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' },
      BASE,
    );
    sched.updateLocalSchedule({ scheduleId: t.scheduleId, name: '品牌舆情监控', workspaceId: 'brand' }, BASE + HOUR);

    const { vi } = await import('vitest');
    vi.resetModules();
    const reloaded = await import('../../../src/renderer/lib/schedule-local');
    const [row] = reloaded.localSchedules();
    expect(row.name).toBe('品牌舆情监控');
    expect(row.workspaceId).toBe('brand');
  });

  it('删除任务后持久化同步收敛（刷新不回魂）', async () => {
    const t = sched.createLocalSchedule(
      { name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' },
      BASE,
    );
    sched.removeLocalSchedule(t.scheduleId);

    const { vi } = await import('vitest');
    vi.resetModules();
    const reloaded = await import('../../../src/renderer/lib/schedule-local');
    expect(reloaded.localSchedules()).toEqual([]);
  });
});