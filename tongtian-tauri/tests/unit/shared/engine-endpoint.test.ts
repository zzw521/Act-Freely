/**
 * P1-5：引擎端口按 workspace 真实解析，不再把 18000 写死在前端。
 *
 * - 桌面端必须向 Rust 问 `engine:endpoint`（第二个实例可能是 18001）；
 * - 网页直连才用 VITE_ENGINE_URL / 缺省 18000；
 * - 只读动作拿不到端点时抛 ENGINE_NOT_RUNNING，由页面显示原因；
 * - 组件里不允许再直接引用 ENGINE_DIRECT_URL。
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type EndpointModule = typeof import('../../../src/renderer/lib/engine-endpoint');

const win = window as unknown as {
  __TAURI_INTERNALS__?: unknown;
  tongtianAPI?: { invoke: (c: string, ...a: unknown[]) => Promise<unknown> };
};

let mod: EndpointModule;

beforeEach(async () => {
  vi.resetModules();
  delete win.__TAURI_INTERNALS__;
  delete win.tongtianAPI;
  mod = await import('../../../src/renderer/lib/engine-endpoint');
});

afterEach(() => {
  delete win.__TAURI_INTERNALS__;
  delete win.tongtianAPI;
});

describe('浏览器直连：没有多实例可查，走唯一常量', () => {
  it('缺省 18000，并回填请求的 workspaceId', async () => {
    const ep = await mod.resolveEngineEndpoint('ops');
    expect(ep).toMatchObject({
      workspaceId: 'ops',
      baseUrl: 'http://127.0.0.1:18000',
      port: 18000,
      status: 'ready',
    });
  });

  it('baseUrl 去掉尾部斜杠，避免拼出 //api', async () => {
    expect(await mod.resolveEngineBaseUrl('ops')).toBe('http://127.0.0.1:18000');
  });
});

describe('桌面端：按 workspace 向 Rust 解析真实端口', () => {
  it('把 workspace 原样传给 engine:endpoint，并采用 Rust 返回的端口', async () => {
    win.__TAURI_INTERNALS__ = {};
    const invoke = vi.fn(async () => ({
      workspaceId: 'second',
      baseUrl: 'http://127.0.0.1:18001/',
      port: 18001,
      status: 'ready',
    }));
    win.tongtianAPI = { invoke };

    const ep = await mod.resolveEngineEndpoint('second');
    expect(invoke).toHaveBeenCalledWith('engine:endpoint', 'second');
    expect(ep.baseUrl).toBe('http://127.0.0.1:18001');
    expect(ep.port).toBe(18001);
  });

  it('Rust 报引擎未运行 → 如实抛 ENGINE_NOT_RUNNING', async () => {
    win.__TAURI_INTERNALS__ = {};
    win.tongtianAPI = {
      invoke: async () => {
        throw new Error('ENGINE_NOT_RUNNING: 工作空间「second」的引擎尚未启动');
      },
    };
    await expect(mod.resolveEngineEndpoint('second')).rejects.toThrow(/ENGINE_NOT_RUNNING/);
  });

  it('Rust 返回空端点 → 不伪造 18000，直接拒绝', async () => {
    win.__TAURI_INTERNALS__ = {};
    win.tongtianAPI = { invoke: async () => null };
    await expect(mod.resolveEngineEndpoint('second')).rejects.toThrow(/ENGINE_NOT_RUNNING/);
  });
});

describe('源码契约：组件不再写死引擎地址', () => {
  const read = (rel: string) =>
    readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');

  it.each([
    ['components/FilePreview/index.tsx'],
    ['components/ModelSettings/index.tsx'],
    ['pages/Settings/index.tsx'],
  ])('%s 走 resolveEngineBaseUrl，且不引用 ENGINE_DIRECT_URL', (rel) => {
    const src = read(`../../../src/renderer/${rel}`);
    expect(src).not.toContain('ENGINE_DIRECT_URL');
    expect(src).not.toMatch(/127\.0\.0\.1:18000/);
    expect(src).toContain('resolveEngineBaseUrl');
  });

  it('tauri-api 把 engine:endpoint 映射到 Rust engine_endpoint', () => {
    const src = read('../../../src/renderer/lib/tauri-api.ts');
    expect(src).toContain('channel === IPC_CHANNELS.ENGINE_ENDPOINT');
    expect(src).toContain("invoke('engine_endpoint'");
  });
});
