/**
 * 应用设置共享缓存：挂载中的消费方必须同步看到设置变更。
 *
 * 之前的实现每个 hook 实例各自 useState，设置页切开关后侧栏/对话流要等刷新
 * 或重挂才生效。这里锁住「一次写入 → 所有订阅者同步拿到同一份合并结果」。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const STORE = '../../../src/renderer/lib/app-settings-store';
const ALIGNMENT_API = '../../../src/renderer/lib/alignment-api';

describe('应用设置共享缓存', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    vi.doUnmock(ALIGNMENT_API);
  });

  it('两个订阅者在一次写入后都收到同一份合并后的快照', async () => {
    const store = await import(STORE);
    const seenA: Array<Record<string, unknown>> = [];
    const seenB: Array<Record<string, unknown>> = [];
    const unA = store.subscribeAppSettings(() => seenA.push(store.getAppSettingsSnapshot() as Record<string, unknown>));
    const unB = store.subscribeAppSettings(() => seenB.push(store.getAppSettingsSnapshot() as Record<string, unknown>));

    // 等初次加载落地，避免把加载完成的通知算进本次断言
    await vi.waitFor(() => expect(store.getAppSettingsSnapshot().desktopNotify).toBe(true));
    seenA.length = 0;
    seenB.length = 0;

    store.updateAppSettings({ experiments: { filter: true } });

    expect(seenA).toHaveLength(1);
    expect(seenB).toHaveLength(1);
    expect(seenA[0]).toBe(store.getAppSettingsSnapshot());
    expect(seenA[0].experiments).toEqual({ filter: true });
    // 共享缓存保留初次加载的既有设置，不是只存本次 patch
    expect(seenA[0].language).toBe('中文');

    unA();
    unB();
    store.updateAppSettings({ theme: '暗色' });
    expect(seenA).toHaveLength(1);
    expect(seenB).toHaveLength(1);
  });

  it('初次加载回来前写入的本地修改不会被迟到的快照覆盖', async () => {
    let resolveGet: (value: Record<string, unknown>) => void = () => undefined;
    const pending = new Promise<Record<string, unknown>>((resolve) => {
      resolveGet = resolve;
    });
    vi.doMock(ALIGNMENT_API, () => ({
      getSettings: () => pending,
      setSettings: () => Promise.resolve({}),
    }));

    const store = await import(STORE);
    const unsubscribe = store.subscribeAppSettings(() => undefined);
    store.updateAppSettings({ experiments: { filter: true } });

    resolveGet({ language: '中文', desktopNotify: true, experiments: { filter: false } });
    await vi.waitFor(() => expect(store.getAppSettingsSnapshot().language).toBe('中文'));

    // 本地（更新、更新鲜）优先于迟到的服务端快照
    expect(store.getAppSettingsSnapshot().experiments).toEqual({ filter: true });
    unsubscribe();
  });

  it('strict 写入失败时回滚缓存并抛出，不把系统开关停在「假开启」', async () => {
    vi.doMock(ALIGNMENT_API, () => ({
      getSettings: () => Promise.resolve({ runInBackground: false }),
      setSettings: () => Promise.reject(new Error('SETTINGS_WRITE_FAILED')),
    }));
    const store = await import(STORE);
    store.subscribeAppSettings(() => undefined);
    await vi.waitFor(() => expect(store.getAppSettingsSnapshot().runInBackground).toBe(false));

    await expect(
      store.updateAppSettings({ runInBackground: true }, { strict: true }),
    ).rejects.toThrow('SETTINGS_WRITE_FAILED');
    expect(store.getAppSettingsSnapshot().runInBackground).toBe(false);
  });

  it('非 strict 写入失败保持原行为：缓存生效、错误不外抛', async () => {
    vi.doMock(ALIGNMENT_API, () => ({
      getSettings: () => Promise.resolve({ runInBackground: false }),
      setSettings: () => Promise.reject(new Error('SETTINGS_WRITE_FAILED')),
    }));
    const store = await import(STORE);
    store.subscribeAppSettings(() => undefined);
    await vi.waitFor(() => expect(store.getAppSettingsSnapshot().runInBackground).toBe(false));

    await expect(store.updateAppSettings({ runInBackground: true })).resolves.toBeUndefined();
    expect(store.getAppSettingsSnapshot().runInBackground).toBe(true);
  });
});
