/**
 * 渲染层整改 API 封装（千问对标整改 spec §6.3）
 *
 * - Tauri 容器：经 window.tongtianAPI.invoke 走真实 Rust command（SQLite 持久化 + 系统行为落地）
 * - 纯浏览器（Mock / 引擎直连）：委托 `alignment-local`，与 Rust 同口径的本地权威实现
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  AppSettingsData,
  ArchiveRecord,
  AutostartStatus,
  AwarenessPatch,
  AwarenessProfile,
  AwarenessView,
  ComputerUsePolicy,
  ConnectorState,
  HookConfigStore,
  HookEventName,
  HookResult,
  MemoryEntry,
  ProxyStatus,
  SystemCapability,
  SystemPermissionStatus,
} from '../../shared/contracts';
import { handleLocalAlignmentChannel } from './alignment-local';
import { handleLocalAwarenessChannel } from './awareness-local';
import { localProxyStatus } from './proxy-local';
import { localAutostart } from './system-local';
import {
  localSystemPermissionStatus,
  openSystemPermissionUnsupported,
  probePermissionStates,
} from './permissions-local';

interface TongtianAPILike {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
}

function getAPI(): TongtianAPILike | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as { tongtianAPI?: TongtianAPILike }).tongtianAPI;
}

const isElectron = (): boolean => Boolean(getAPI());

// ── localStorage 回退（仅设置与记忆；其余整改通道归 alignment-local） ──

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

/** 非 Rust 通道的整改类本地实现（Mock / 浏览器直连引擎） */
function local<T>(channel: string, args: unknown[]): T {
  const res = handleLocalAlignmentChannel(channel, args);
  return res.value as T;
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
  proxyUrl: '',
  proxyBypass: '',
  privacyMode: false,
  voiceEnabled: false,
  snapshotEnabled: false,
  quickPickEnabled: false,
  computerUsePolicy: 'ask_every_time',
  secureEnv: false,
  workbenchModes: ['general', 'design', 'slides'],
  workbenchDefaultMode: 'general',
  shortcuts: {},
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

export async function setSettings(
  patch: Partial<AppSettingsData>,
  options?: { strict?: boolean },
): Promise<AppSettingsData> {
  const api = getAPI();
  if (api) {
    try {
      await api.invoke(IPC_CHANNELS.SETTINGS_SET, patch);
    } catch (e) {
      // strict：Rust 侧才是设置权威（代理等配置落库前会整组校验），
      // 校验失败必须让调用方看到，否则会假装保存成功
      if (options?.strict) throw e;
      // 引擎直连等无落地通道时忽略，交由本地兑底持久化
    }
  }
  // 本地 localStorage 兑底持久化：保证引擎直连等场景下刷新不丢设置
  const next = { ...lsGet('settings', DEFAULT_SETTINGS as AppSettingsData), ...patch };
  lsSet('settings', next);
  return next;
}

/**
 * 网络代理实际生效状态。
 * Tauri：Rust 返回真实配置（含仍以旧代理运行的引擎实例数）；
 * 浏览器/Mock：按本地设置推导，代理变量注入由引擎侧环境决定。
 */
export async function getProxyStatus(): Promise<ProxyStatus> {
  const api = getAPI();
  if (api) {
    try {
      const r = await api.invoke(IPC_CHANNELS.PROXY_STATUS);
      if (r && typeof r === 'object' && !Array.isArray(r)) return r as ProxyStatus;
    } catch {
      // 回退本地推导
    }
  }
  return localProxyStatus(await getSettings());
}

/**
 * 开机自启动的真实状态。
 * Tauri：Rust 读系统启动项（enabled 是注册表事实，不是设置里的意愿）；
 * 浏览器/Mock：无系统启动项可写，如实返回 supported=false + enabled=false，
 * 页面据此把开关置灰并说明「请在桌面版中设置」，不把勾选状态当生效状态。
 */
export async function getAutostartStatus(): Promise<AutostartStatus> {
  const api = getAPI();
  if (!api) return localAutostart(await getSettings());
  let raw: unknown;
  try {
    raw = await api.invoke(IPC_CHANNELS.SYSTEM_AUTOSTART_STATUS);
  } catch (e) {
    // 桌面端读注册表失败必须可见：静默降级成「未开启」会让用户以为系统里没有启动项
    throw e instanceof Error ? e : new Error('AUTOSTART_FAILED: 系统启动项状态读取失败');
  }
  // Mock / 引擎直连没有这个 command（invoke 兜底返回空值）→ 按本地事实推导
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) return raw as AutostartStatus;
  return localAutostart(await getSettings());
}

/**
 * 系统权限清单与真实状态。
 * Tauri：Rust 按当前平台给出能力表与可达的设置面板（桌面版 openSupported=true），
 * 状态仍由 webview 向系统问（通知走 Notification，麦克风走 Permissions API）；
 * 浏览器/Mock：同一份表 + 同一套探测，问不到的项留给页面显示「需在系统设置中管理」。
 */
export async function getSystemPermissionStatus(): Promise<SystemPermissionStatus> {
  const api = getAPI();
  if (api) {
    try {
      const r = (await api.invoke(IPC_CHANNELS.SYSTEM_PERMISSION_STATUS)) as {
        os?: string;
        items?: SystemCapability[];
        openSupported?: boolean;
      };
      if (r && Array.isArray(r.items) && r.items.length) {
        return {
          os: r.os ?? 'unknown',
          items: r.items,
          states: await probePermissionStates(r.items),
          // Rust 才有系统设置入口；Mock 通道会显式带 openSupported=false
          openSupported: typeof r.openSupported === 'boolean' ? r.openSupported : true,
        };
      }
    } catch {
      // 回退本地清单（同表同口径）
    }
  }
  return localSystemPermissionStatus();
}

/**
 * 「去授权」：桌面版由 Rust 拉起当前平台的系统隐私设置面板。
 * 其他通道没有这个能力，如实抛 PERMISSION_UNSUPPORTED，页面不进入假成功。
 */
export async function openSystemPermissionSettings(
  key: string,
): Promise<{ key: string; opened: string; os: string }> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.SYSTEM_PERMISSION_OPEN, {
      key,
    })) as { key: string; opened: string; os: string };
  }
  return openSystemPermissionUnsupported();
}

// ── Hooks ───────────────────────────────────────────────────

export async function getHooks(): Promise<HookConfigStore> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_GET)) as HookConfigStore;
  }
  return local<HookConfigStore>(IPC_CHANNELS.HOOKS_GET, []);
}

export async function setHooks(config: HookConfigStore): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.HOOKS_SET, config);
    return;
  }
  local(IPC_CHANNELS.HOOKS_SET, [config]);
}

/**
 * Hook 试运行。
 * 桌面应用内由 Rust 真实拉起进程；浏览器通道如实抛 HOOK_TEST_UNSUPPORTED。
 */
export async function testHook(event: HookEventName, command: string): Promise<HookResult> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_TEST, { event, command })) as HookResult;
  }
  return local<HookResult>(IPC_CHANNELS.HOOKS_TEST, [{ event, command }]);
}

// ── Awareness（画像 + 四个能力文件 + 长期记忆条目） ────────────
//
// 桌面端由 Rust `awareness_*` 读写 ~/.tongtianwork/awareness/main 真实文件，
// 浏览器直连引擎由 `/api/awareness*` 读写同一目录，Mock 通道退化为本地台账
// （见 `awareness-local`）。本层只负责分发，不再自己保存第二份意识内容。

function localAwareness<T>(channel: string, args: unknown[]): T {
  const res = handleLocalAwarenessChannel(channel, args);
  return res.value as T;
}

/** `awareness:get`：画像 + 意识目录 + 四个能力文件正文 */
export async function getAwareness(): Promise<AwarenessView> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_GET)) as AwarenessView;
  }
  return localAwareness<AwarenessView>(IPC_CHANNELS.AWARENESS_GET, []);
}

/** `awareness:update`：保存画像开关/风格与能力文件正文，返回最新视图 */
export async function updateAwareness(patch: AwarenessPatch): Promise<AwarenessView> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_UPDATE, patch)) as AwarenessView;
  }
  return localAwareness<AwarenessView>(IPC_CHANNELS.AWARENESS_UPDATE, [patch]);
}

/** `awareness:memories`：长期记忆条目（新→旧） */
export async function listMemories(): Promise<MemoryEntry[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_MEMORIES, {})) as MemoryEntry[];
  }
  return localAwareness<MemoryEntry[]>(IPC_CHANNELS.AWARENESS_MEMORIES, []);
}

/** `awareness:memoryAdd`：新增一条长期记忆（内容为空时后端拒绝） */
export async function addMemory(
  content: string,
  source: MemoryEntry['source'] = 'manual',
): Promise<MemoryEntry> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_MEMORY_ADD, { content, source })) as MemoryEntry;
  }
  return localAwareness<MemoryEntry>(IPC_CHANNELS.AWARENESS_MEMORY_ADD, [{ content, source }]);
}

/** `awareness:memoryDelete`：删除用户点名的那一条记忆条目 */
export async function deleteMemory(id: string): Promise<{ ok: boolean; id: string }> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.AWARENESS_MEMORY_DELETE, { id })) as { ok: boolean; id: string };
  }
  return localAwareness<{ ok: boolean; id: string }>(IPC_CHANNELS.AWARENESS_MEMORY_DELETE, [{ id }]);
}

// ── Connectors ──────────────────────────────────────────────

export async function listConnectors(): Promise<ConnectorState[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.CONNECTOR_LIST)) as ConnectorState[];
  }
  return local<ConnectorState[]>(IPC_CHANNELS.CONNECTOR_LIST, []);
}

export async function authorizeConnector(
  connectorId: string,
  category: 'builtin' | 'market',
  serverRef?: string,
): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.CONNECTOR_AUTHORIZE, { connectorId, category, serverRef });
    return;
  }
  local(IPC_CHANNELS.CONNECTOR_AUTHORIZE, [{ connectorId, category, serverRef }]);
}

export async function revokeConnector(connectorId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.CONNECTOR_REVOKE, { connectorId });
    return;
  }
  local(IPC_CHANNELS.CONNECTOR_REVOKE, [{ connectorId }]);
}

// ── Archive ─────────────────────────────────────────────────

export type ArchiveItem = ArchiveRecord;

/**
 * 归档任务（写归档标记，任务本体保留）。
 * title 供非 Rust 通道展示：桌面端 Rust 以 tasks 表标题为准，忽略该字段。
 */
export async function archiveTask(
  taskId: string,
  options?: { title?: string; workspaceId?: string },
): Promise<ArchiveItem> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TASK_ARCHIVE, {
      taskId,
      title: options?.title,
      workspaceId: options?.workspaceId,
    })) as ArchiveItem;
  }
  return local<ArchiveItem>(IPC_CHANNELS.TASK_ARCHIVE, [
    { taskId, title: options?.title, workspaceId: options?.workspaceId },
  ]);
}

export async function listArchived(): Promise<ArchiveItem[]> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_LIST)) as ArchiveItem[];
  }
  return local<ArchiveItem[]>(IPC_CHANNELS.TASK_ARCHIVE_LIST, []);
}

export async function unarchiveTask(taskId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TASK_UNARCHIVE, { taskId });
    return;
  }
  local(IPC_CHANNELS.TASK_UNARCHIVE, [{ taskId }]);
}

/** 彻底删除归档任务（Rust 侧同时删除该任务本体行） */
export async function deleteArchivedTask(taskId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_DELETE, { taskId });
    return;
  }
  local(IPC_CHANNELS.TASK_ARCHIVE_DELETE, [{ taskId }]);
}

/** 清空归档清单；确认词不合格时返回 false（保持清单不变，不抛穿页面） */
export async function clearArchived(confirm: string): Promise<boolean> {
  try {
    const api = getAPI();
    const res = api
      ? await api.invoke(IPC_CHANNELS.TASK_ARCHIVE_CLEAR, { confirm })
      : local<{ ok: boolean }>(IPC_CHANNELS.TASK_ARCHIVE_CLEAR, [{ confirm }]);
    return Boolean((res as { ok?: boolean } | null)?.ok);
  } catch (e) {
    // 确认词不合法：与 Rust 同口径视为「用户未完成确认」，而非异常
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.startsWith('ARCHIVE_CONFIRM_REQUIRED')) return false;
    throw e;
  }
}

// ── Computer Use ────────────────────────────────────────────

export async function getComputerUsePolicy(): Promise<ComputerUsePolicy> {
  const api = getAPI();
  if (api) {
    const res = (await api.invoke(IPC_CHANNELS.COMPUTER_USE_GET_POLICY)) as { policy: ComputerUsePolicy };
    return res.policy;
  }
  return local<{ policy: ComputerUsePolicy }>(IPC_CHANNELS.COMPUTER_USE_GET_POLICY, []).policy;
}

export async function setComputerUsePolicy(policy: ComputerUsePolicy): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.COMPUTER_USE_SET_POLICY, { policy });
    return;
  }
  local(IPC_CHANNELS.COMPUTER_USE_SET_POLICY, [{ policy }]);
}
