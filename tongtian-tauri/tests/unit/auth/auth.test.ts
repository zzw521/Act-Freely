/**
 * 账号登录态单元测试（useAuthStore + mock-api auth channel + 归一化）
 *
 * vitest 环境为 node（无 window/localStorage），此处手动 polyfill 后
 * 动态导入 mock-api 垫片与 store，覆盖真实分发路径：
 * - Mock 登录/退出语义（与 Rust TT_AUTH_MOCK 一致）
 * - IPC 空响应兜底（localStorage 还原，引擎直连陷阱防护）
 */

import { describe, it, expect, beforeAll } from 'vitest';

// ── polyfill（必须在动态导入前就绪） ──────────────────────────────

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};
(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } };
const win = globalThis.window as WindowAPI;

let useAuthStore: typeof import('../../../src/renderer/stores/useAuthStore').useAuthStore;
let normalizeAuthStatus: typeof import('../../../src/renderer/stores/useAuthStore').normalizeAuthStatus;

beforeAll(async () => {
  // 注入 Mock 垫片（window.tongtianAPI 缺失时自动挂载 mockAPI）
  await import('../../../src/renderer/lib/mock-api');
  const storeMod = await import('../../../src/renderer/stores/useAuthStore');
  useAuthStore = storeMod.useAuthStore;
  normalizeAuthStatus = storeMod.normalizeAuthStatus;
});

// ── normalizeAuthStatus：IPC 响应归一化 ──────────────────────────

describe('normalizeAuthStatus', () => {
  it('合法响应正常解析', () => {
    const r = normalizeAuthStatus({ loggedIn: true, profile: { uid: 'u1', nickname: '张三' }, mock: true });
    expect(r).not.toBeNull();
    expect(r!.loggedIn).toBe(true);
    expect(r!.profile?.uid).toBe('u1');
    expect(r!.mock).toBe(true);
  });

  it('引擎直连兜底空响应（数组/空值）返回 null', () => {
    expect(normalizeAuthStatus([])).toBeNull();
    expect(normalizeAuthStatus(null)).toBeNull();
    expect(normalizeAuthStatus(undefined)).toBeNull();
    expect(normalizeAuthStatus('error')).toBeNull();
  });

  it('缺少 loggedIn 字段的对象返回 null', () => {
    expect(normalizeAuthStatus({ profile: { uid: 'u1' } })).toBeNull();
  });

  it('profile 缺失时归一为 null', () => {
    const r = normalizeAuthStatus({ loggedIn: false });
    expect(r).toEqual({ loggedIn: false, profile: null, mock: false });
  });
});

// ── mock-api auth channel：登录/退出语义 ─────────────────────────

describe('mock-api auth channel', () => {
  it('初始状态未登录', async () => {
    const status = (await win.tongtianAPI!.invoke('auth:status')) as { loggedIn: boolean; mock: boolean };
    expect(status.loggedIn).toBe(false);
    expect(status.mock).toBe(true);
  });

  it('登录后返回模拟用户资料', async () => {
    const resp = (await win.tongtianAPI!.invoke('auth:login')) as { ok: boolean; profile: { uid: string; nickname: string } };
    expect(resp.ok).toBe(true);
    expect(resp.profile.nickname).toBe('张先生');
    expect(resp.profile.uid).toBe('tt-mock-8f3a2c91');

    const status = (await win.tongtianAPI!.invoke('auth:status')) as { loggedIn: boolean };
    expect(status.loggedIn).toBe(true);
  });

  it('退出后回到未登录且幂等', async () => {
    const first = (await win.tongtianAPI!.invoke('auth:logout')) as { wasLoggedIn: boolean };
    expect(first.wasLoggedIn).toBe(true);
    const status = (await win.tongtianAPI!.invoke('auth:status')) as { loggedIn: boolean };
    expect(status.loggedIn).toBe(false);
    // 幂等：重复退出返回 wasLoggedIn=false
    const second = (await win.tongtianAPI!.invoke('auth:logout')) as { wasLoggedIn: boolean };
    expect(second.wasLoggedIn).toBe(false);
  });
});

// ── useAuthStore：登录/退出/兜底分支 ─────────────────────────────

describe('useAuthStore', () => {
  it('init 查询登录态（Mock 初始未登录）', async () => {
    await useAuthStore.getState().init();
    const s = useAuthStore.getState();
    expect(s.initialized).toBe(true);
    expect(s.loggedIn).toBe(false);
  });

  it('login 成功更新状态与 localStorage 兜底', async () => {
    const ok = await useAuthStore.getState().login();
    expect(ok).toBe(true);
    const s = useAuthStore.getState();
    expect(s.loggedIn).toBe(true);
    expect(s.profile?.nickname).toBe('张先生');
    expect(s.busy).toBe(false);
    expect(storage.get('tt.auth.profile')).toContain('tt-mock-8f3a2c91');
  });

  it('logout 清理状态与 localStorage（本地数据保留语义）', async () => {
    const ok = await useAuthStore.getState().logout();
    expect(ok).toBe(true);
    const s = useAuthStore.getState();
    expect(s.loggedIn).toBe(false);
    expect(s.profile).toBeNull();
    expect(storage.has('tt.auth.profile')).toBe(false);
  });

  it('IPC 异常时 init 降级 localStorage 缓存还原', async () => {
    // 模拟已登录缓存 + IPC 不可用（引擎直连陷阱）
    storage.set('tt.auth.profile', JSON.stringify({ uid: 'tt-cache', nickname: '缓存用户' }));
    const saved = win.tongtianAPI;
    delete win.tongtianAPI;
    useAuthStore.setState({ initialized: false, loggedIn: false, profile: null });

    await useAuthStore.getState().init();
    const s = useAuthStore.getState();
    expect(s.loggedIn).toBe(true);
    expect(s.profile?.nickname).toBe('缓存用户');

    // 恢复现场
    win.tongtianAPI = saved;
    useAuthStore.setState({ initialized: false, loggedIn: false, profile: null });
    storage.delete('tt.auth.profile');
  });

  it('login 失败分支：IPC 抛错时 error 记录且不置登录态', async () => {
    const saved = win.tongtianAPI;
    win.tongtianAPI = { invoke: async () => { throw new Error('AUTH_LOGIN_TIMEOUT: 等待登录超时'); } };
    useAuthStore.setState({ initialized: true });

    const ok = await useAuthStore.getState().login();
    expect(ok).toBe(false);
    const s = useAuthStore.getState();
    expect(s.loggedIn).toBe(false);
    expect(s.error).toContain('AUTH_LOGIN_TIMEOUT');
    expect(s.busy).toBe(false);

    win.tongtianAPI = saved;
  });
});
