/**
 * 项目协作 · 前端本地通道与执行回报队列单元测试
 *
 * 覆盖：
 * - 浏览器/Mock 通道如实回报 supported=false，写操作统一 COLLAB_UNSUPPORTED
 * - 本机执行绑定 → deliver 暂存 → done 回报（产物只取文件名）
 * - 回报失败进入待回报队列，重试成功或手动丢弃后清空
 */

import { describe, it, expect, beforeEach, beforeAll, vi } from 'vitest';

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } };
const win = globalThis.window as WindowAPI;

let api: typeof import('../../../src/renderer/lib/api').api;
let run: typeof import('../../../src/renderer/lib/collab-run');

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = (await import('../../../src/renderer/lib/api')).api;
  run = await import('../../../src/renderer/lib/collab-run');
});

beforeEach(() => {
  storage.clear();
  vi.restoreAllMocks();
});

describe('浏览器/Mock 通道', () => {
  it('协作状态如实回报 supported=false（不伪造登录态）', async () => {
    const status = (await win.tongtianAPI!.invoke('collab:status')) as {
      supported: boolean;
      loggedIn: boolean;
    };
    expect(status.supported).toBe(false);
    expect(status.loggedIn).toBe(false);
  });

  it('项目写操作统一以 COLLAB_UNSUPPORTED 拒绝', async () => {
    for (const channel of ['collab:listProjects', 'collab:createProject', 'collab:reportResult']) {
      await expect(win.tongtianAPI!.invoke(channel, {})).rejects.toThrow(/^COLLAB_UNSUPPORTED:/);
    }
  });
});

describe('本机执行回报', () => {
  const binding = {
    conversationId: 'c1',
    projectId: 'prj-1',
    projectName: '季度复盘',
    taskId: 'pt-1',
    taskTitle: '整理数据',
    version: 7,
    startedAtMs: 1000,
  };

  it('done 时回报摘要与产物文件名，成功后清除绑定', async () => {
    const spy = vi.spyOn(api.collab, 'reportResult').mockResolvedValue({ ok: true, task: {} as never, seq: 1 });
    run.bindCollabRun(binding);
    run.captureCollabDeliver('c1', '完成了季度数据整理', ['C:\\work\\季报.xlsx', 'summary.md']);

    await run.completeCollabRun('c1');

    expect(spy).toHaveBeenCalledTimes(1);
    const input = spy.mock.calls[0][0];
    expect(input.projectId).toBe('prj-1');
    expect(input.taskId).toBe('pt-1');
    expect(input.version).toBe(7);
    expect(input.status).toBe('review');
    expect(input.summary).toBe('完成了季度数据整理');
    expect(input.artifacts).toEqual(['季报.xlsx', 'summary.md']);
    expect(run.collabBindingOf('c1')).toBeNull();
  });

  it('无绑定的会话不回报', async () => {
    const spy = vi.spyOn(api.collab, 'reportResult').mockResolvedValue({ ok: true, task: {} as never, seq: 1 });
    run.captureCollabDeliver('other', '普通对话', []);
    await run.completeCollabRun('other');
    expect(spy).not.toHaveBeenCalled();
  });

  it('没有 deliver（失败/取消）时不回报，也不假装完成', async () => {
    const spy = vi.spyOn(api.collab, 'reportResult').mockResolvedValue({ ok: true, task: {} as never, seq: 1 });
    run.bindCollabRun({ ...binding, conversationId: 'c2' });
    await run.completeCollabRun('c2');
    expect(spy).not.toHaveBeenCalled();
    expect(run.collabBindingOf('c2')).toBeNull();
    expect(run.pendingReports()).toHaveLength(0);
  });

  it('先 deliver 后报错（如收尾失败）同样不回报成功', async () => {
    const spy = vi.spyOn(api.collab, 'reportResult').mockResolvedValue({ ok: true, task: {} as never, seq: 1 });
    run.bindCollabRun({ ...binding, conversationId: 'c5' });
    run.captureCollabDeliver('c5', '看起来完成了', ['x.md']);
    run.captureCollabFailure('c5');
    await run.completeCollabRun('c5');
    expect(spy).not.toHaveBeenCalled();
    expect(run.collabBindingOf('c5')).toBeNull();
  });

  it('回报失败进入待回报队列；重试成功清空', async () => {
    const spy = vi
      .spyOn(api.collab, 'reportResult')
      .mockRejectedValueOnce(new Error('PROJECT_CONFLICT: 任务已被其他人修改'));
    run.bindCollabRun({ ...binding, conversationId: 'c3' });
    run.captureCollabDeliver('c3', '结果正文', ['a.md']);
    await run.completeCollabRun('c3');

    const rows = run.pendingReports();
    expect(rows).toHaveLength(1);
    // 队列直接展示给用户，存的是本地化文案而不是裸错误码
    expect(rows[0].error).toContain('任务已被其他人修改');
    expect(rows[0].input.artifacts).toEqual(['a.md']);
    expect(run.collabBindingOf('c3')).toBeNull();

    spy.mockResolvedValueOnce({ ok: true, task: {} as never, seq: 2 });
    const result = await run.retryPendingReport(rows[0].id);
    expect(result.ok).toBe(true);
    expect(run.pendingReports()).toHaveLength(0);
  });

  it('重试仍失败时保留记录并更新错误；手动丢弃可移除', async () => {
    vi.spyOn(api.collab, 'reportResult').mockRejectedValue(new Error('COLLAB_NETWORK: 无法连接协作服务'));
    run.bindCollabRun({ ...binding, conversationId: 'c4' });
    run.captureCollabDeliver('c4', '结果正文', ['b.md']);
    await run.completeCollabRun('c4');
    const row = run.pendingReports()[0];
    expect(row).toBeTruthy();

    const result = await run.retryPendingReport(row.id);
    expect(result.ok).toBe(false);
    expect(run.pendingReports()).toHaveLength(1);

    run.discardPendingReport(row.id);
    expect(run.pendingReports()).toHaveLength(0);
  });

  it('标记进行中返回服务端新版本号', async () => {
    const spy = vi
      .spyOn(api.collab, 'updateTask')
      .mockResolvedValue({ ok: true, task: { version: 9 } as never, seq: 3 });
    await expect(run.markTaskDoing('prj-1', 'pt-1', 8)).resolves.toBe(9);
    expect(spy).toHaveBeenCalledWith('prj-1', 'pt-1', { version: 8, status: 'doing' });
  });
});
