/**
 * 工作空间 · 前端契约测试（任务 #18）
 *
 * 走真实分发路径：mock-api 注入 window.tongtianAPI → api.ts 的 workspaceAPI
 * → handleLocalWorkspaceChannel，验证页面（ScheduleView 新建弹窗）拿到的形状、
 * 错误语义，以及定时任务创建时 workspaceId 是否真实落到调度台账。
 */

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type WorkspaceApi = typeof import('../../../src/renderer/lib/api');

let api: WorkspaceApi;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = await import('../../../src/renderer/lib/api');
});

beforeEach(async () => {
  const local = await import('../../../src/renderer/lib/workspace-local');
  local.resetLocalWorkspaces();
});

describe('workspaceAPI · 列表（页面消费面）', () => {
  it('无台账时仍返回 default 占位，页面下拉不会空白', async () => {
    const list = await api.workspaceAPI.list();
    expect(list.map((w) => w.id)).toEqual(['default']);
    expect(list[0]).toMatchObject({ exists: false, running: false, status: 'stopped' });
  });

  it('创建后列表包含新工作空间且带本地目录路径', async () => {
    await api.workspaceAPI.create('Report');
    const list = await api.workspaceAPI.list();
    const ws = list.find((w) => w.id === 'report');
    expect(ws).toBeDefined();
    expect(ws?.exists).toBe(true);
    expect(ws?.rootPath).toContain('workspaces/report');
  });

  it('非法标识被拒且抛裸错误码前缀，页面用 errorTextOf 归一', async () => {
    await expect(api.workspaceAPI.create('../escape')).rejects.toThrow(/WORKSPACE_INVALID_ID/);
    expect((await api.workspaceAPI.list()).map((w) => w.id)).toEqual(['default']);
  });

  it('重复创建幂等，列表不产生重名行', async () => {
    await api.workspaceAPI.create('ops');
    await api.workspaceAPI.create('ops');
    expect((await api.workspaceAPI.list()).filter((w) => w.id === 'ops')).toHaveLength(1);
  });
});

describe('workspaceAPI · 与定时任务链路对齐', () => {
  it('schedule:create 透传 workspaceId，任务归属所选工作空间', async () => {
    await api.workspaceAPI.create('weekly');
    const created = (await (
      window as unknown as {
        tongtianAPI: { invoke: (c: string, ...a: unknown[]) => Promise<unknown> };
      }
    ).tongtianAPI.invoke('schedule:create', {
      name: '每周一复盘',
      description: '汇总上周任务',
      freq: 'weekly',
      timeOfDay: '08:00',
      weekday: 1,
      workspaceId: 'weekly',
    })) as { workspaceId: string; scheduleId: string };

    expect(created.workspaceId).toBe('weekly');
    const runs = (await (
      window as unknown as {
        tongtianAPI: { invoke: (c: string, ...a: unknown[]) => Promise<unknown> };
      }
    ).tongtianAPI.invoke('schedule:list')) as { workspaceId: string }[];
    expect(runs.find((t) => t.scheduleId === created.scheduleId)?.workspaceId).toBe('weekly');
  });

  it('未选工作空间时落库为 default（与 Rust 缺省同口径）', async () => {
    const created = (await (
      window as unknown as {
        tongtianAPI: { invoke: (c: string, ...a: unknown[]) => Promise<unknown> };
      }
    ).tongtianAPI.invoke('schedule:create', {
      name: '每日简报',
      description: '汇总今日待办',
      freq: 'daily',
      timeOfDay: '09:00',
    })) as { workspaceId: string };
    expect(created.workspaceId).toBe('default');
  });
});
