/**
 * 项目协作 · 事件订阅通道（缺陷 D-06）
 *
 * 前端只做参数搬运，因此钉住三件容易静默坏掉的事：
 * - `collab:watch*` 确实映射到 Rust 的 `collab_watch_*` command，且游标原样传下去
 *   （游标错传成 0 会把历史事件重放一遍，界面看起来「同步了」其实是回滚）
 * - 起订阅只带 projectId 与游标：凭据不进这条参数（access token 由 Rust 侧放进
 *   Authorization 头，渲染层碰不到，也不该出现在任何 IPC 载荷里）
 * - 事件监听的取消函数在监听还没注册上时也必须生效（快速切换项目 / 页面卸载）
 *
 * 浏览器与 Mock 通道的「不支持就是不假装」由 collab.test.ts 覆盖。
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  calls: [] as { cmd: string; args?: unknown }[],
  listeners: [] as { name: string; cb: (e: { payload: unknown }) => void }[],
  unlistened: [] as string[],
}));

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(async (cmd: string, args?: unknown) => {
    harness.calls.push({ cmd, args });
    if (cmd === 'collab_watch_start') {
      return { ok: true, projectId: 'prj-1', cursor: 0, state: 'connecting' };
    }
    return { ok: true };
  }),
}));
vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn(async (name: string, cb: (e: { payload: unknown }) => void) => {
    harness.listeners.push({ name, cb });
    return () => harness.unlistened.push(name);
  }),
}));

import { invoke } from '@tauri-apps/api/core';
import { tauriAPI } from '../../../src/renderer/lib/tauri-api';

type FakeWindow = {
  tongtianAPI?: unknown;
  __TAURI_INTERNALS__?: Record<string, unknown>;
};
const win = ((globalThis as Record<string, unknown>).window ??= {}) as FakeWindow;

let api: typeof import('../../../src/renderer/lib/api').api;

beforeAll(async () => {
  // 桌面通道：isTauri() 看 __TAURI_INTERNALS__，api.ts 取 window.tongtianAPI
  win.__TAURI_INTERNALS__ = {};
  win.tongtianAPI = tauriAPI;
  api = (await import('../../../src/renderer/lib/api')).api;
});

beforeEach(() => {
  harness.calls.length = 0;
  harness.listeners.length = 0;
  harness.unlistened.length = 0;
  vi.mocked(invoke).mockClear();
});

describe('订阅通道路由到 Rust command', () => {
  it('watchStart 把项目与游标按 Tauri 的驼峰参数名送下去', async () => {
    await api.collab.watch('prj-1', 42);
    expect(harness.calls).toEqual([{ cmd: 'collab_watch_start', args: { projectId: 'prj-1', sinceSeq: 42 } }]);
  });

  it('载荷里只有项目 id 与游标，不带任何凭据字段', async () => {
    await api.collab.watch('prj-1', 7);
    const args = harness.calls[0].args as Record<string, unknown>;
    expect(Object.keys(args).sort()).toEqual(['projectId', 'sinceSeq']);
  });

  it('unwatch 与 watchStatus 分别落到 stop / status，status 不发网络请求参数', async () => {
    await api.collab.unwatch('prj-1');
    await api.collab.watchStatus();
    expect(harness.calls.map((c) => c.cmd)).toEqual(['collab_watch_stop', 'collab_watch_status']);
    expect(harness.calls[0].args).toEqual({ projectId: 'prj-1' });
  });

  it('缺参数时按空项目 / 游标 0 传，而不是把 undefined 塞给 Rust', async () => {
    await tauriAPI.invoke('collab:watchStart');
    expect(harness.calls).toEqual([{ cmd: 'collab_watch_start', args: { projectId: '', sinceSeq: 0 } }]);
  });
});

describe('事件监听', () => {
  it('事件名与 Rust 侧 emit 的字面量一致', async () => {
    api.collab.onEvent(() => {});
    api.collab.onWatchState(() => {});
    expect(harness.listeners.map((l) => l.name)).toEqual(['collab-event', 'collab-watch-state']);
  });

  it('payload 原样交给回调', async () => {
    const got: unknown[] = [];
    api.collab.onEvent((e) => got.push(e));
    harness.listeners[0].cb({ payload: { projectId: 'prj-1', seq: 9, kind: 'task.status' } });
    expect(got).toEqual([{ projectId: 'prj-1', seq: 9, kind: 'task.status' }]);
  });

  it('监听还没注册上就取消：回调不再被调用，注册完成后立刻反注册', async () => {
    let hits = 0;
    const dispose = api.collab.onEvent(() => {
      hits += 1;
    });
    dispose();
    harness.listeners[0].cb({ payload: { projectId: 'prj-1', seq: 1 } });
    await Promise.resolve();
    await Promise.resolve();
    expect(hits).toBe(0);
    expect(harness.unlistened).toEqual(['collab-event']);
  });

  it('取消函数可重复调用而不抛（卸载竞态里不该冒出异常）', () => {
    const dispose = api.collab.onWatchState(() => {});
    expect(() => {
      dispose();
      dispose();
    }).not.toThrow();
  });
});
