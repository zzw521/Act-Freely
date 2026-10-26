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
  AcpConfigState,
  AcpServerStatus,
  BrowserUseCapabilities,
  BrowserUsePolicy,
  BrowserUseSession,
  ComputerUseCapabilities,
  ComputerUseCapture,
  ComputerUsePolicy,
  HookConfigStore,
  HookEventName,
  HookResult,
  HooksStatus,
  ImChannelsSnapshot,
  ImPreferences,
  MemoryEntry,
  ProxyStatus,
  RemoteDesktopCapabilities,
  RemoteDesktopFrame,
  RemoteDesktopSession,
  RemoteDesktopStartResp,
  RemoteDesktopStopResp,
  SystemCapability,
  SystemPermissionStatus,
  TerminalAiResult,
  TerminalCapabilities,
  TerminalReadResult,
  TerminalSessionInfo,
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
  autoStart: false,
  runInBackground: false,
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
  browserUsePolicy: 'ask_every_time',
  piiOutboundMode: 'off',
  toolApprovalMode: 'off',
  agentMaxIters: 8,
  agentAutoCompress: true,
  agentLazyMcpTools: true,
  secureEnv: false,
  workbenchModes: ['general', 'design', 'slides'],
  workbenchDefaultMode: 'general',
  pptxTemplateId: 'business-blue',
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

/**
 * 保存 Hook 配置。返回里带 `revoked`：配置里已删除的命令，其放行同时作废，
 * 页面必须把这件事说出来，否则用户会以为「删了再加回来」还要再点一次放行。
 */
export async function setHooks(
  config: HookConfigStore,
): Promise<{ ok: boolean; events: number; revoked: string[] }> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_SET, config)) as {
      ok: boolean;
      events: number;
      revoked: string[];
    };
  }
  return local<{ ok: boolean; events: number; revoked: string[] }>(
    IPC_CHANNELS.HOOKS_SET,
    [config],
  );
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

/**
 * `hooks:status`：执行面事实（配了几条 / 哪些事件真有触发点 / 哪些还没放行）。
 * 网关地址与 token 不下发到这里——渲染层只拿决策需要的部分。
 */
export async function getHooksStatus(): Promise<HooksStatus> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.HOOKS_STATUS)) as HooksStatus;
  }
  return local<HooksStatus>(IPC_CHANNELS.HOOKS_STATUS, []);
}

/** 放行一条 Hook 命令：只有放行过的命令才会在真实任务里由本机拉起 */
export async function grantHook(event: HookEventName, command: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.HOOKS_GRANT, { event, command });
    return;
  }
  local(IPC_CHANNELS.HOOKS_GRANT, [{ event, command }]);
}

/** 撤销放行：只删授权记录，配置原样保留 */
export async function revokeHook(command: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.HOOKS_REVOKE, { command });
    return;
  }
  local(IPC_CHANNELS.HOOKS_REVOKE, [{ command }]);
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

/**
 * 执行器真实能力视图：平台、档位、网关与不可用原因都以 Rust/本地权威实现为准。
 * 页面用它决定截屏自检是否可点，不能拿设置里保存的档位当「现在能用」。
 */
export async function getComputerUseCapabilities(): Promise<ComputerUseCapabilities> {
  const api = getAPI();
  if (api) {
    const r = (await api.invoke(IPC_CHANNELS.COMPUTER_USE_CAPABILITIES)) as
      | ComputerUseCapabilities
      | null;
    if (r && typeof r === 'object' && !Array.isArray(r)) return r;
    throw new Error('COMPUTER_USE_UNSUPPORTED: 桌面通道没有返回电脑操控能力视图');
  }
  return local<ComputerUseCapabilities>(IPC_CHANNELS.COMPUTER_USE_CAPABILITIES, []);
}

/**
 * 用户主动触发的截屏自检。
 * 桌面端由 Rust 按当前档位确认后执行（每次询问会弹系统确认框），
 * 图片只回传到当前页面内存用于预览，不落盘；浏览器/Mock 如实抛 COMPUTER_USE_UNSUPPORTED。
 */
export async function captureComputerUseScreen(): Promise<ComputerUseCapture> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.COMPUTER_USE_CAPTURE)) as ComputerUseCapture;
  }
  return local<ComputerUseCapture>(IPC_CHANNELS.COMPUTER_USE_CAPTURE, []);
}

// ── Remote Desktop（本机实时画面；只看不控） ────────────────

export async function getRemoteDesktopCapabilities(): Promise<RemoteDesktopCapabilities> {
  const api = getAPI();
  if (api) {
    const r = (await api.invoke(IPC_CHANNELS.REMOTE_DESKTOP_CAPABILITIES)) as
      | RemoteDesktopCapabilities
      | null;
    if (r && typeof r === 'object' && !Array.isArray(r)) return r;
    throw new Error('REMOTE_UNSUPPORTED: 桌面通道没有返回远程桌面能力视图');
  }
  return local<RemoteDesktopCapabilities>(IPC_CHANNELS.REMOTE_DESKTOP_CAPABILITIES, []);
}

export async function getRemoteDesktopStatus(): Promise<RemoteDesktopSession> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.REMOTE_DESKTOP_STATUS)) as RemoteDesktopSession;
  }
  return local<RemoteDesktopSession>(IPC_CHANNELS.REMOTE_DESKTOP_STATUS, []);
}

/** 开启会话：Rust 按电脑操控档位确认后签发 10 分钟会话（只看不控） */
export async function startRemoteDesktop(): Promise<RemoteDesktopStartResp> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.REMOTE_DESKTOP_START)) as RemoteDesktopStartResp;
  }
  return local<RemoteDesktopStartResp>(IPC_CHANNELS.REMOTE_DESKTOP_START, []);
}

/** 取一帧：间隔不足返回 throttled（不重复截屏）；到期抛 REMOTE_SESSION_EXPIRED */
export async function fetchRemoteDesktopFrame(): Promise<RemoteDesktopFrame> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.REMOTE_DESKTOP_FRAME)) as RemoteDesktopFrame;
  }
  return local<RemoteDesktopFrame>(IPC_CHANNELS.REMOTE_DESKTOP_FRAME, []);
}

export async function stopRemoteDesktop(): Promise<RemoteDesktopStopResp> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.REMOTE_DESKTOP_STOP)) as RemoteDesktopStopResp;
  }
  return local<RemoteDesktopStopResp>(IPC_CHANNELS.REMOTE_DESKTOP_STOP, []);
}

// ── Browser Use ─────────────────────────────────────────────

export async function getBrowserUsePolicy(): Promise<BrowserUsePolicy> {
  const api = getAPI();
  if (api) {
    const res = (await api.invoke(IPC_CHANNELS.BROWSER_USE_GET_POLICY)) as {
      policy: BrowserUsePolicy;
    };
    return res.policy;
  }
  return local<{ policy: BrowserUsePolicy }>(IPC_CHANNELS.BROWSER_USE_GET_POLICY, []).policy;
}

export async function setBrowserUsePolicy(policy: BrowserUsePolicy): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.BROWSER_USE_SET_POLICY, { policy });
    return;
  }
  local(IPC_CHANNELS.BROWSER_USE_SET_POLICY, [{ policy }]);
}

/**
 * 受控浏览器真实能力视图：浏览器、档位、网关与会话状态以 Rust/本地权威实现为准。
 * 「保存了档位」不等于「现在能用」，页面必须看这份视图。
 */
export async function getBrowserUseCapabilities(): Promise<BrowserUseCapabilities> {
  const api = getAPI();
  if (api) {
    const r = (await api.invoke(IPC_CHANNELS.BROWSER_USE_CAPABILITIES)) as
      | BrowserUseCapabilities
      | null;
    if (r && typeof r === 'object' && !Array.isArray(r)) return r;
    throw new Error('BROWSER_USE_SESSION_FAILED: 桌面通道没有返回浏览器能力视图');
  }
  return local<BrowserUseCapabilities>(IPC_CHANNELS.BROWSER_USE_CAPABILITIES, []);
}

/** 启动受控浏览器（用户主动；档位为禁用时 Rust 拒绝） */
export async function startBrowserUseSession(): Promise<BrowserUseSession> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.BROWSER_USE_START)) as BrowserUseSession;
  }
  return local<BrowserUseSession>(IPC_CHANNELS.BROWSER_USE_START, []);
}

/** 结束本应用启动的受控浏览器；不会动用户日常浏览器 */
export async function stopBrowserUseSession(): Promise<BrowserUseSession> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.BROWSER_USE_STOP)) as BrowserUseSession;
  }
  return local<BrowserUseSession>(IPC_CHANNELS.BROWSER_USE_STOP, []);
}

// ── 终端 AI+ 与 ACP ─────────────────────────────────────────

/**
 * 终端真实能力视图：开关、shell、管道限制与 ACP 配置。
 * 「设置里保存了开关」不等于「现在能用」——浏览器/Mock 通道没有本机进程，
 * 这里会返回 enabled=false 与可读原因，页面据此禁用入口。
 */
export async function getTerminalCapabilities(): Promise<TerminalCapabilities> {
  const api = getAPI();
  if (api) {
    const r = (await api.invoke(IPC_CHANNELS.TERMINAL_CAPABILITIES)) as
      | TerminalCapabilities
      | null;
    if (r && typeof r === 'object' && !Array.isArray(r)) return r;
    throw new Error('TERMINAL_STATE_FAILED: 桌面通道没有返回终端能力视图');
  }
  return local<TerminalCapabilities>(IPC_CHANNELS.TERMINAL_CAPABILITIES, []);
}

/** 显式开启/关闭终端 AI+（高风险动作，Rust 侧写审计） */
export async function setTerminalEnabled(enabled: boolean): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TERMINAL_SET_POLICY, { enabled });
    return;
  }
  local(IPC_CHANNELS.TERMINAL_SET_POLICY, [{ enabled }]);
}

/** 在真实 shell 中开会话；cwd 必须是已存在目录 */
export async function openTerminal(
  cwd: string,
  workspaceId?: string,
): Promise<TerminalSessionInfo> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TERMINAL_OPEN, {
      cwd,
      workspaceId: workspaceId ?? null,
    })) as TerminalSessionInfo;
  }
  return local<TerminalSessionInfo>(IPC_CHANNELS.TERMINAL_OPEN, [{ cwd, workspaceId }]);
}

/**
 * 写入 shell。source 只用于审计口径：manual=用户键入/粘贴，ai=执行 AI 建议。
 * 两条路径都必须由用户在界面上主动触发，AI 自身没有写入能力。
 */
export async function writeTerminal(
  sessionId: string,
  data: string,
  source: 'manual' | 'ai' = 'manual',
): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TERMINAL_INPUT, { sessionId, data, source });
    return;
  }
  local(IPC_CHANNELS.TERMINAL_INPUT, [{ sessionId, data, source }]);
}

/** 增量读取：传入上次的 offset，只拿新输出 */
export async function readTerminal(
  sessionId: string,
  offset: number,
): Promise<TerminalReadResult> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TERMINAL_READ, {
      sessionId,
      offset,
    })) as TerminalReadResult;
  }
  return local<TerminalReadResult>(IPC_CHANNELS.TERMINAL_READ, [{ sessionId, offset }]);
}

export async function closeTerminal(sessionId: string): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.TERMINAL_CLOSE, { sessionId });
    return;
  }
  local(IPC_CHANNELS.TERMINAL_CLOSE, [{ sessionId }]);
}

/** 根据最近输出让模型给命令建议；永不执行，执行只由用户逐条确认 */
export async function suggestTerminalCommand(
  sessionId: string,
  question: string,
  modelId?: string,
): Promise<TerminalAiResult> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.TERMINAL_AI, {
      sessionId,
      question,
      modelId: modelId ?? null,
    })) as TerminalAiResult;
  }
  return local<TerminalAiResult>(IPC_CHANNELS.TERMINAL_AI, [{ sessionId, question, modelId }]);
}

/** ACP 客户端配置（外部编码 Agent 的启动命令与审批策略） */
export async function getAcpConfig(): Promise<AcpConfigState> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.ACP_GET_CONFIG)) as AcpConfigState;
  }
  return local<AcpConfigState>(IPC_CHANNELS.ACP_GET_CONFIG, []);
}

export async function setAcpConfig(input: {
  enabled: boolean;
  command: string;
  permission: string;
  timeoutSeconds: number;
}): Promise<void> {
  const api = getAPI();
  if (api) {
    await api.invoke(IPC_CHANNELS.ACP_SET_CONFIG, input);
    return;
  }
  local(IPC_CHANNELS.ACP_SET_CONFIG, [input]);
}

/** ACP 服务端启动命令：让外部 IDE/终端把本应用当编码 Agent 用 */
export async function getAcpStatus(): Promise<AcpServerStatus> {
  const api = getAPI();
  if (api) {
    return (await api.invoke(IPC_CHANNELS.ACP_STATUS)) as AcpServerStatus;
  }
  return local<AcpServerStatus>(IPC_CHANNELS.ACP_STATUS, []);
}

// ── IM 频道 ─────────────────────────────────────────────────
//
// 频道清单、连接状态、会话台账一律以引擎为准：桌面端由 Rust 代理 `/api/im/*`，
// 浏览器引擎直连问同一个接口。渲染层不缓存第二份状态，也不在前端维护
// 「支持哪些 IM」的清单——那会让界面宣称的能力超过引擎真实能力。

/** `im:channels`：通道清单 + 每个通道的真实状态 + IM 发起的会话台账 */
export async function getImChannels(): Promise<ImChannelsSnapshot> {
  const api = getAPI();
  if (!api) {
    throw new Error('IM_ENGINE_UNAVAILABLE: 当前运行通道没有引擎进程，无法持有 IM 长连接');
  }
  const res = (await api.invoke(IPC_CHANNELS.IM_CHANNELS)) as Partial<ImChannelsSnapshot> | null;
  return { channels: res?.channels ?? [], sessions: res?.sessions ?? [] };
}

/** `im:configure`：登记凭据（引擎先真实校验，通过后自动起连接）；失败不落盘 */
export async function configureImChannel(
  kind: string,
  credentials: Record<string, string>,
): Promise<Record<string, string>> {
  const api = getAPI();
  if (!api) {
    throw new Error('IM_ENGINE_UNAVAILABLE: 当前运行通道没有引擎进程，无法绑定 IM 频道');
  }
  const res = (await api.invoke(IPC_CHANNELS.IM_CONFIGURE, { kind, credentials })) as {
    account?: Record<string, string>;
  } | null;
  return res?.account ?? {};
}

/** `im:start` / `im:stop` / `im:forget`：连接开关与解绑，返回引擎给出的新状态 */
async function imAction(
  channel: string,
  kind: string,
): Promise<string> {
  const api = getAPI();
  if (!api) {
    throw new Error('IM_ENGINE_UNAVAILABLE: 当前运行通道没有引擎进程，无法控制 IM 长连接');
  }
  const res = (await api.invoke(channel, { kind })) as { state?: string } | null;
  return res?.state ?? '';
}

export const startImChannel = (kind: string) => imAction(IPC_CHANNELS.IM_START, kind);
export const stopImChannel = (kind: string) => imAction(IPC_CHANNELS.IM_STOP, kind);
/** 解绑只删该通道凭据登记，IM 会话历史保留（存量数据不删） */
export const forgetImChannel = (kind: string) => imAction(IPC_CHANNELS.IM_FORGET, kind);

/**
 * `im:preferencesGet`：IM 偏好的当前值由引擎给出（回贴正文是引擎拼的），
 * 这里不缓存也不落 localStorage —— 否则设置页显示的会是「界面以为的开关」而不是生效中的开关。
 */
export async function getImPreferences(): Promise<ImPreferences> {
  const api = getAPI();
  if (!api) {
    throw new Error('IM_ENGINE_UNAVAILABLE: 当前运行通道没有引擎进程，IM 回贴偏好无从生效');
  }
  const res = (await api.invoke(IPC_CHANNELS.IM_PREFERENCES_GET)) as {
    preferences?: Partial<ImPreferences>;
  } | null;
  return { showToolProcess: Boolean(res?.preferences?.showToolProcess) };
}

/** `im:preferencesSet`：写成功后返回引擎落盘的值（写失败原样抛出，开关不提前拨过去） */
export async function setImPreferences(patch: Partial<ImPreferences>): Promise<ImPreferences> {
  const api = getAPI();
  if (!api) {
    throw new Error('IM_ENGINE_UNAVAILABLE: 当前运行通道没有引擎进程，IM 回贴偏好无法保存');
  }
  const res = (await api.invoke(IPC_CHANNELS.IM_PREFERENCES_SET, { preferences: patch })) as {
    preferences?: Partial<ImPreferences>;
  } | null;
  return { showToolProcess: Boolean(res?.preferences?.showToolProcess) };
}
