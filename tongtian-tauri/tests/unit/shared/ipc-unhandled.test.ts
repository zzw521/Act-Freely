/**
 * P1-6：未登记通道必须显式失败，禁止静默返回空结果。
 *
 * 三个运行通道（桌面 Tauri / Mock / 引擎直连）分别走真实分发函数：
 * - 未实现的 channel 抛 IPC_CHANNEL_UNHANDLED，错误里带通道名；
 * - 未实现的方法（Proxy 兜底）同样抛错；
 * - 业务上**真的**是空结果的通道仍原样返回（例如无事件的重放），
 *   避免把「确实没有数据」也误判成故障。
 */

import { describe, it, expect, beforeAll } from 'vitest';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type TauriModule = typeof import('../../../src/renderer/lib/tauri-api');
type EngineModule = { engineAPI: { invoke: (c: string, ...a: unknown[]) => Promise<unknown> } };
type InjectedApi = {
  invoke: (c: string, ...a: unknown[]) => Promise<unknown>;
  eventReplay: (taskId: string, fromSeq?: number) => Promise<{ taskId: string; events: unknown[] }>;
};

let tauri: TauriModule;
let mock: InjectedApi;
let engine: EngineModule;

beforeAll(async () => {
  tauri = await import('../../../src/renderer/lib/tauri-api');
  // Mock 垫片不导出对象，而是按运行环境把 proxiedAPI 注入 window.tongtianAPI
  await import('../../../src/renderer/lib/mock-api');
  mock = (window as unknown as { tongtianAPI: InjectedApi }).tongtianAPI;
  engine = (await import('../../../src/renderer/lib/engine-api')) as EngineModule;
});

describe('tauri-api：未登记通道与未实现方法', () => {
  it('invoke 未登记 channel → IPC_CHANNEL_UNHANDLED 且带通道名', async () => {
    await expect(tauri.tauriAPI.invoke('no:such-channel')).rejects.toThrow(
      /IPC_CHANNEL_UNHANDLED.*no:such-channel/,
    );
  });

  it('createProxiedAPI 未实现方法 → IPC_CHANNEL_UNHANDLED 且带方法名', async () => {
    const proxy = tauri.createProxiedAPI<Record<string, never>>({});
    await expect(
      (proxy as unknown as { missingMethod: () => Promise<unknown> }).missingMethod(),
    ).rejects.toThrow(/IPC_CHANNEL_UNHANDLED.*missingMethod/);
  });
});

describe('mock-api：未登记通道与未实现方法', () => {
  it('invoke 未登记 channel → IPC_CHANNEL_UNHANDLED', async () => {
    await expect(mock.invoke('no:such-channel')).rejects.toThrow(
      /IPC_CHANNEL_UNHANDLED.*no:such-channel/,
    );
  });

  it('确实为空的业务结果仍返回空列表（不是兜底）', async () => {
    const replayed = await mock.eventReplay('task-empty');
    expect(replayed).toEqual({ taskId: 'task-empty', events: [] });
  });
});

describe('engine-api：未登记通道与未实现方法', () => {
  it('invoke 未登记 channel → IPC_CHANNEL_UNHANDLED', async () => {
    await expect(engine.engineAPI.invoke('no:such-channel')).rejects.toThrow(
      /IPC_CHANNEL_UNHANDLED.*no:such-channel/,
    );
  });
});
