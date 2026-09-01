/**
 * 渲染层整改 API 封装（千问对标整改 spec §6.3）
 *
 * - Electron 环境：经 window.tongtianAPI.invoke 走真实 IPC（sqlite 持久化 + 系统行为落地）
 * - 纯浏览器开发环境（vite dev）：回退到 localStorage 模拟，保证 UI 可开发调试
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  AppSettingsData,
  AwarenessProfile,
  ConnectorState,
  HookAggregate,
  HookConfigStore,
  HookEventName,
  HookResult,
  MemoryEntry,
} from '../../shared/contracts';

interface TongtianAPILike {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
}

function getAPI(): TongtianAPILike | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as { tongtianAPI?: TongtianAPILike }).tongtianAPI;
}

const isElectron = (): boolean => Boolean(getAPI());

// ── localStorage 回退（浏览器开发模式） ──────────────────────

function lsGet<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(`tt:${key}`);
    return raw ? { ...fallback, ...(JSON.parse(raw) as T) } : fallback;
  } catch {
    return fallback;
  }
}

function lsGetArray<T>(key: string, fallback: T[]): T[] {
  try {
    const raw = localStorage.getItem(`tt:${key}`);
    return raw ? (JSON.parse(raw) as T[]) : fallback;
  } catch {
    return fallback;
  }
}

function lsSet(key: string, value: unknown): void {
  try {
    localStorage.setItem(`tt:${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

// ── Settings ────────────────────────────────────────────────

const DEFAULT_SETTINGS: Partial<AppSettingsData> = {
  language: '中文',
  theme: '亮色',
  uiStyle: '默认',
  chatFont: '无衬线',
  chatFontSize: 14,
  chatWidth: '标准',
  previewMode: '应用内预览',
  promptSuggestion: true,
  expandToolCalls: false,
  imShowToolProcess: false,
  autoStart: false,
  keepAwake: true,
  desktopNotify: true,
  badgeUnread: true,
  soundNotify: false,
  proxyMode: '跟随系统',
  privacyMode: false,
  voiceEnabled: false,
  snapshotEnabled: false,
  quickPickEnabled: false,
  computerUsePolicy: 'ask_every_time',
  secureEnv: false,
  workbenchModes: ['general', 'design', 'slides'],
  workbenchDefaultMode: 'general',
  experiments: {},
};

export async function getSettings(): Promise<AppSettingsData> {
  const api = getAPI();
  if (api) {
    try {
      const r = await api.invoke(IPC_CHANNELS.SETTINGS_GET);
      // 引擎直连等通道可能返回空数组/空对象，无效则回退本地持久层
      if (r && typeof r === 'object' && !Array.isArray(r) && Object.keys(r).length > 0) {
        return r as AppSettingsData;
      }
    } catch {
      // ignore → 回退本地
    }
  }
  return lsGet('settings', DEFAULT_SETTINGS as AppSettingsData);
}

export async function setSettings(patch: Partial<AppSettingsData>): Promise<AppSettingsData> {
  const api = getAPI();
  if (api) {
    try {
      await api.invoke(IPC_CHANNELS.SETTINGS_SET, patch);
    } catch {
      // 引擎直连等无落地通道时忽略，交由本地兑底持久化
    }
  }
  // 本地 localStorage 兑底持久化：保证引擎直连等场景下刷新不丢设置
  const next = { ...lsGet('settings', DEFAULT_SETTINGS as AppSettingsData), ...patch };
  lsSet('settings', next);
  return next;
}

// ── Hooks ───────────────────────────────────────────────────

export async function getHooks(): Promise<HookConfigStore> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_GET)) as HookConfigStore;
  }
  return lsGet('hooks', { hooks: {} } as HookConfigStore);
}

export async function setHooks(config: HookConfigStore): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.HOOKS_SET, config);
    return;
  }
  lsSet('hooks', config);
}

export async function testHook(
  event: HookEventName,
  command: string,
): Promise<HookResult> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_TEST, { event, command })) as HookResult;
  }
  // 浏览器模拟：命令含 rm -rf 时模拟阻塞
  const blocked = /rm\s+-rf/.test(command);
  return {
    exitCode: blocked ? 2 : 0,
    stdout: '',
    stderr: blocked ? '危险命令已被阻止（模拟）' : '',
    blocked,
    timedOut: false,
  };
}

// ── Awareness ───────────────────────────────────────────────

const DEFAULT_PROFILE: AwarenessProfile = {
  mode: true,
  autoMemory: true,
  style: 'professional',
  customPrompt: '',
  workManual: '',
  updatedAt: '',
};

export async function getAwareness(): Promise<AwarenessProfile> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_GET)) as AwarenessProfile;
  }
  return lsGet('awareness', DEFAULT_PROFILE);
}

export async function updateAwareness(patch: Partial<AwarenessProfile>): Promise<AwarenessProfile> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_UPDATE, patch)) as AwarenessProfile;
  }
  const next = { ...lsGet('awareness', DEFAULT_PROFILE), ...patch };
  lsSet('awareness', next);
  return next;
}

export async function listMemories(): Promise<MemoryEntry[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_MEMORIES, {})) as MemoryEntry[];
  }
  return lsGetArray<MemoryEntry>('memories', []);
}

export async function addMemory(content: string): Promise<MemoryEntry> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_MEMORY_ADD, { content })) as MemoryEntry;
  }
  const entry: MemoryEntry = {
    id: `mem-${Date.now()}`,
    content,
    source: 'manual',
    createdAt: new Date().toISOString().slice(0, 16).replace('T', ' '),
  };
  lsSet('memories', [entry, ...lsGetArray<MemoryEntry>('memories', [])]);
  return entry;
}

export async function deleteMemory(id: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.AWARENESS_MEMORY_DELETE, { id });
    return;
  }
  lsSet('memories', lsGetArray<MemoryEntry>('memories', []).filter((m) => m.id !== id));
}

// ── Connectors ──────────────────────────────────────────────

export async function listConnectors(): Promise<ConnectorState[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.CONNECTOR_LIST)) as ConnectorState[];
  }
  return lsGetArray<ConnectorState>('connectors', []);
}

export async function authorizeConnector(connectorId: string, category: 'builtin' | 'market'): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.CONNECTOR_AUTHORIZE, { connectorId, category });
    return;
  }
  const list = lsGetArray<ConnectorState>('connectors', []).filter((c) => c.connectorId !== connectorId);
  list.push({ connectorId, category, installed: true, authStatus: 'authorized', updatedAt: new Date().toISOString() });
  lsSet('connectors', list);
}

export async function revokeConnector(connectorId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.CONNECTOR_REVOKE, { connectorId });
    return;
  }
  const list = lsGetArray<ConnectorState>('connectors', []).map((c) =>
    c.connectorId === connectorId ? { ...c, installed: false, authStatus: 'none' as const } : c,
  );
  lsSet('connectors', list);
}

// ── Archive ─────────────────────────────────────────────────

export interface ArchiveItem {
  taskId: string;
  title: string;
  workspaceId: string;
  archivedAt: string;
}

export async function listArchived(): Promise<ArchiveItem[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_LIST)) as ArchiveItem[];
  }
  return lsGetArray<ArchiveItem>('archived', []);
}

export async function unarchiveTask(taskId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TASK_UNARCHIVE, { taskId });
    return;
  }
  lsSet('archived', lsGetArray<ArchiveItem>('archived', []).filter((a) => a.taskId !== taskId));
}

/** 彻底删除归档任务（Electron 下同时删除任务本体） */
export async function deleteArchivedTask(taskId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_DELETE, { taskId });
    return;
  }
  lsSet('archived', lsGetArray<ArchiveItem>('archived', []).filter((a) => a.taskId !== taskId));
}

export async function clearArchived(confirm: string): Promise<boolean> {
  const api = getAPI();
  if (api) {
    const res = (await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_CLEAR, { confirm })) as { ok: boolean };
    return res.ok;
  }
  if (confirm === 'DELETE') {
    lsSet('archived', []);
    return true;
  }
  return false;
}

// ── Computer Use ────────────────────────────────────────────

export async function getComputerUsePolicy(): Promise<string> {
  const api = getAPI();
  if (api) {
    const res = (await api.invoke(IPC_CHANNELS.COMPUTER_USE_GET_POLICY)) as { policy: string };
    return res.policy;
  }
  return lsGet('computerUsePolicy', { policy: 'ask_every_time' }).policy;
}

export async function setComputerUsePolicy(policy: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.COMPUTER_USE_SET_POLICY, { policy });
    return;
  }
  lsSet('computerUsePolicy', { policy });
}
