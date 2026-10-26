/**
 * 通天办公 - 账号登录态状态管理（网页验证登录 · Loopback 方案）
 *
 * 数据源：Rust auth command（auth_status/auth_login/auth_logout）。
 * 默认连接综合服务端做真实账号验证：拉起浏览器网页登录并等 loopback 回调。
 * 仅当显式 TT_AUTH_MOCK=true 时为模拟用户直登（离线开发）。
 *
 * localStorage 兜底：引擎直连/IPC 异常时 status 返回空（已知陷阱），
 * 以本地缓存还原登录态显示，避免刷新丢失。
 */

import { create } from 'zustand';
import type { AuthProfile } from '../../shared/contracts';
import { authAPI } from '../lib/api';

/** localStorage 兜底键（引擎直连模式下 IPC 返回空的降级数据源） */
const STORAGE_KEY = 'tt.auth.profile';

interface AuthState {
  /** 是否已完成首次登录态查询 */
  initialized: boolean;
  loggedIn: boolean;
  profile: AuthProfile | null;
  /** 当前是否 Mock 开关（TT_AUTH_MOCK） */
  mock: boolean;
  /** 登录/退出流程进行中 */
  busy: boolean;
  error: string | null;

  /** 应用启动时查询登录态（幂等，失败降级 localStorage） */
  init: () => Promise<void>;
  /** 登录（Mock 直登 / 真实模式拉起网页），返回是否成功 */
  login: () => Promise<boolean>;
  /** 退出登录（清理 store 与本地缓存；本地任务/会话数据保留） */
  logout: () => Promise<boolean>;
}

/** 归一化 status 响应；IPC 兜底空响应（如引擎直连）返回 null */
export function normalizeAuthStatus(
  raw: unknown,
): { loggedIn: boolean; profile: AuthProfile | null; mock: boolean } | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.loggedIn !== 'boolean') return null;
  return {
    loggedIn: r.loggedIn,
    profile: (r.profile as AuthProfile | null) ?? null,
    mock: Boolean(r.mock),
  };
}

function readFallback(): AuthProfile | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as AuthProfile) : null;
  } catch {
    return null;
  }
}

function writeFallback(profile: AuthProfile | null): void {
  try {
    if (profile) localStorage.setItem(STORAGE_KEY, JSON.stringify(profile));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* 存储不可用时静默忽略 */
  }
}

export const useAuthStore = create<AuthState>((set, get) => ({
  initialized: false,
  loggedIn: false,
  profile: null,
  mock: true,
  busy: false,
  error: null,

  init: async () => {
    if (get().initialized) return;
    try {
      const status = normalizeAuthStatus(await authAPI.status());
      if (status) {
        set({
          initialized: true,
          loggedIn: status.loggedIn,
          profile: status.profile,
          mock: status.mock,
          error: null,
        });
        writeFallback(status.loggedIn ? status.profile : null);
        return;
      }
    } catch (e) {
      console.warn('[auth] 登录态查询失败，降级本地缓存', e);
    }
    // IPC 异常/空响应兜底：localStorage 缓存还原
    const cached = readFallback();
    set({ initialized: true, loggedIn: cached !== null, profile: cached });
  },

  login: async () => {
    if (get().busy) return false;
    set({ busy: true, error: null });
    try {
      const resp = await authAPI.login();
      if (!resp || typeof resp !== 'object' || !resp.profile) {
        throw new Error('登录响应格式异常');
      }
      set({ loggedIn: true, profile: resp.profile, mock: Boolean(resp.mock), busy: false });
      writeFallback(resp.profile);
      return true;
    } catch (e) {
      set({ busy: false, error: e instanceof Error ? e.message : String(e) });
      return false;
    }
  },

  logout: async () => {
    if (get().busy) return false;
    set({ busy: true, error: null });
    try {
      await authAPI.logout();
    } catch (e) {
      // 后端退出失败不阻断本地清理（远端吊销为尽力而为语义）
      console.warn('[auth] 退出请求失败，继续本地清理', e);
    }
    set({ loggedIn: false, profile: null, busy: false });
    writeFallback(null);
    return true;
  },
}));
