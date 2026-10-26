/**
 * 通天办公 - 工作空间状态（当前工作空间 + 清单 + 创建/切换）
 *
 * 桌面端以 `{data_home}/workspaces/*` 目录为权威（Rust workspace_list / create），
 * 浏览器与 Mock 通道走 workspace-local 的本地台账。当前空间选择落 localStorage，
 * 供引擎端点解析、任务提交、安全策略按同一 workspace 对齐。
 */

import { create } from 'zustand';
import { IPC_CHANNELS } from '../../shared/contracts';
import type { WorkspaceInfo } from '../../shared/contracts';
import { DEFAULT_WORKSPACE } from '../lib/workspace-local';

export const DEFAULT_WORKSPACE_ID = DEFAULT_WORKSPACE;
const ACTIVE_WORKSPACE_KEY = 'tt:active-workspace';

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function readActiveId(): string {
  try {
    const raw = storage()?.getItem(ACTIVE_WORKSPACE_KEY)?.trim();
    return raw || DEFAULT_WORKSPACE_ID;
  } catch {
    return DEFAULT_WORKSPACE_ID;
  }
}

function persistActiveId(id: string): void {
  try {
    storage()?.setItem(ACTIVE_WORKSPACE_KEY, id);
  } catch {
    /* 存储不可用时本次选择仍在内存中生效 */
  }
}

function invoke<T>(channel: string, ...args: unknown[]): Promise<T> {
  const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
  if (!api) {
    return Promise.reject(
      new Error('IPC_CHANNEL_UNHANDLED: tongtianAPI 未注入，无法读取工作空间清单'),
    );
  }
  return api.invoke(channel, ...args) as Promise<T>;
}

interface WorkspaceState {
  workspaces: WorkspaceInfo[];
  activeId: string;
  loading: boolean;
  error: string | null;

  /** 拉取工作空间清单；当前选择已不存在时回落到 default */
  refresh: () => Promise<WorkspaceInfo[]>;
  /** 创建工作空间（幂等），成功后切换过去 */
  create: (workspaceId: string) => Promise<WorkspaceInfo>;
  /** 切换当前工作空间（只改选择，不动任何目录内容） */
  setActive: (workspaceId: string) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  activeId: readActiveId(),
  loading: false,
  error: null,

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const rows = await invoke<WorkspaceInfo[]>(IPC_CHANNELS.WORKSPACE_LIST);
      const list = Array.isArray(rows) ? rows.filter((w) => w && typeof w.id === 'string') : [];
      const current = get().activeId;
      const active = list.some((w) => w.id === current) ? current : DEFAULT_WORKSPACE_ID;
      if (active !== current) persistActiveId(active);
      set({ workspaces: list, activeId: active, loading: false });
      return list;
    } catch (e) {
      set({
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      });
      throw e;
    }
  },

  create: async (workspaceId) => {
    const created = await invoke<WorkspaceInfo>(IPC_CHANNELS.WORKSPACE_CREATE, { workspaceId });
    set({ activeId: created.id });
    persistActiveId(created.id);
    await get().refresh();
    return created;
  },

  setActive: (workspaceId) => {
    const id = workspaceId.trim();
    if (!id) return;
    set({ activeId: id });
    persistActiveId(id);
  },
}));

/** 非 React 调用点（任务提交、端点解析）读取当前工作空间 */
export function getActiveWorkspaceId(): string {
  return useWorkspaceStore.getState().activeId || DEFAULT_WORKSPACE_ID;
}
