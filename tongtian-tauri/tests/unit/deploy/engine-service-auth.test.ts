/**
 * 服务化模式 · 网页令牌与同源地址契约
 *
 * 覆盖：
 * 1. 生产构建默认同源（容器/静态托管不再写死 127.0.0.1:18000）
 * 2. 令牌只进 sessionStorage 与请求头，不进 URL/localStorage/构建产物
 * 3. 401 触发一次可见提示，桌面通道不弹网页令牌框
 * 4. 所有引擎 HTTP 调用都走统一鉴权入口（防止以后新增绕过点）
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

type HttpModule = typeof import('../../../src/renderer/lib/engine-http');
type EndpointModule = typeof import('../../../src/renderer/lib/engine-endpoint');

const win = globalThis as unknown as {
  window?: Record<string, unknown>;
  sessionStorage?: Storage;
};

function installSession(): Map<string, string> {
  const store = new Map<string, string>();
  const shim = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  win.sessionStorage = shim as unknown as Storage;
  win.window = win.window ?? {};
  return store;
}

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

beforeEach(() => {
  vi.resetModules();
  installSession().clear();
  delete (win.window as Record<string, unknown>).__TAURI_INTERNALS__;
});

afterEach(() => {
  delete win.sessionStorage;
  delete (win.window as Record<string, unknown>).__TAURI_INTERNALS__;
  vi.unstubAllGlobals();
});

describe('网页直连地址：生产构建默认同源', () => {
  let mod: EndpointModule;

  beforeEach(async () => {
    mod = await import('../../../src/renderer/lib/engine-endpoint');
  });

  it('显式 VITE_ENGINE_URL 优先，并去掉尾部斜杠', () => {
    expect(
      mod.resolveEngineDirectUrl({ configured: 'https://tt.example.com/engine/', dev: false }),
    ).toBe('https://tt.example.com/engine');
  });

  it('显式空串表示同源，交给页面 origin', () => {
    expect(mod.resolveEngineDirectUrl({ configured: '', dev: false, origin: 'https://tt.example.com' })).toBe(
      'https://tt.example.com',
    );
  });

  it('生产构建未配置时用页面 origin（容器/静态托管）', () => {
    expect(mod.resolveEngineDirectUrl({ dev: false, origin: 'http://10.0.0.8:18000' })).toBe(
      'http://10.0.0.8:18000',
    );
  });

  it('开发/测试仍回落引擎缺省端口', () => {
    expect(mod.resolveEngineDirectUrl({ dev: true, origin: 'http://localhost:7173' })).toBe(
      'http://127.0.0.1:18000',
    );
    expect(mod.resolveEngineDirectUrl({ dev: false })).toBe('http://127.0.0.1:18000');
    expect(mod.resolveEngineDirectUrl({ dev: false, origin: 'file:///app/index.html' })).toBe(
      'http://127.0.0.1:18000',
    );
  });
});

describe('engineFetch：令牌与 401 提示', () => {
  let http: HttpModule;

  async function fresh(): Promise<HttpModule> {
    vi.resetModules();
    return import('../../../src/renderer/lib/engine-http');
  }

  it('有令牌时附加 x-tt-engine-token，无令牌时不加头', async () => {
    http = await fresh();
    http.setEngineToken('secret-token');
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await http.engineFetch('http://127.0.0.1:18000/api/tools');
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get('x-tt-engine-token')).toBe('secret-token');

    http.clearEngineToken();
    await http.engineFetch('http://127.0.0.1:18000/api/tools');
    const headers2 = new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers);
    expect(headers2.get('x-tt-engine-token')).toBeNull();
  });

  it('保留调用方原有请求头', async () => {
    http = await fresh();
    http.setEngineToken('t');
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await http.engineFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get('content-type')).toBe('application/json');
    expect(headers.get('x-tt-engine-token')).toBe('t');
  });

  it('401 通知订阅者，输入新令牌后复位；稍后不再自动弹窗', async () => {
    http = await fresh();
    const listener = vi.fn();
    http.subscribeEngineUnauthorized(listener);
    vi.stubGlobal('fetch', vi.fn(async () => ({ status: 401 })));

    await http.engineFetch('/api/health');
    expect(listener).toHaveBeenCalledTimes(1);
    expect(http.isEngineUnauthorized()).toBe(true);

    http.dismissEngineUnauthorized();
    expect(http.isEngineUnauthorized()).toBe(false);
    // 本次页面会话不再重复提示
    await http.engineFetch('/api/health');
    expect(listener).toHaveBeenCalledTimes(2);

    http.setEngineToken('new-token');
    expect(http.getEngineToken()).toBe('new-token');
    expect(http.isEngineUnauthorized()).toBe(false);
  });

  it('桌面通道 401 不弹网页令牌框', async () => {
    (win.window as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    http = await fresh();
    const listener = vi.fn();
    http.subscribeEngineUnauthorized(listener);
    vi.stubGlobal('fetch', vi.fn(async () => ({ status: 401 })));
    await http.engineFetch('/api/tools');
    expect(listener).not.toHaveBeenCalled();
    expect(http.isEngineUnauthorized()).toBe(false);
  });

  it('令牌只进 sessionStorage：源码不写 localStorage，也不拼进 URL', () => {
    const src = read('../../../src/renderer/lib/engine-http.ts');
    expect(src).toContain('sessionStorage');
    expect(src).not.toMatch(/localStorage\s*\./);
    expect(src).toContain("headers.set('x-tt-engine-token'");
    expect(src).not.toMatch(/searchParams|URLSearchParams|\?token=/);
  });
});

describe('接线契约：引擎请求不得绕过统一入口', () => {
  it.each([
    ['lib/engine-api.ts'],
    ['lib/api.ts'],
    ['lib/chat-models.ts'],
    ['components/FilePreview/index.tsx'],
    ['components/ModelSettings/index.tsx'],
    ['pages/Settings/index.tsx'],
  ])('%s 使用 engineFetch，不直接 fetch 引擎', (rel) => {
    const src = read(`../../../src/renderer/${rel}`);
    expect(src).toContain('engineFetch');
    expect(src).not.toMatch(/\bawait fetch\(|\breturn fetch\(/);
  });

  it('令牌输入框挂在应用根节点，并说明存储位置与桌面边界', () => {
    const app = read('../../../src/renderer/App.tsx');
    expect(app).toContain("import EngineTokenDialog from './components/EngineTokenDialog'");
    expect(app).toContain('<EngineTokenDialog />');
    const dlg = read('../../../src/renderer/components/EngineTokenDialog/index.tsx');
    expect(dlg).toContain('sessionStorage');
    expect(dlg).toContain('TT_ENGINE_TOKEN');
    expect(dlg).toContain('桌面专属能力不可用');
    expect(dlg).not.toMatch(/localStorage\s*\./);
  });

  it('引擎令牌守卫与同源静态托管仍有服务端实现', () => {
    const server = read('../../../engine/server.py');
    expect(server).toContain('def service_bind_error');
    expect(server).toContain('x-tt-engine-token');
    expect(server).toContain('def mount_web');
  });

  it('账号 JWT、请求审计与用量归属在引擎侧真实接线', () => {
    const server = read('../../../engine/server.py');
    expect(server).toContain('def verify_engine_jwt');
    expect(server).toContain('def _authenticate_engine_request');
    expect(server).toContain('TT_ENGINE_JWT_SECRET');
    expect(server).toContain('TT_ENGINE_ALLOWED_USERS');
    expect(server).toContain('@app.get("/api/audit")');
    expect(server).toContain('def _usage_scope');
    expect(server).toMatch(/service_bind_error\(\s*args\.host[\s\S]{0,200}?_engine_jwt_secret\(\)/);
  });
});
