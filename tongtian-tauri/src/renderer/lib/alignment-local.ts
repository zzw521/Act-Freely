/**
 * 千问对标整改通道 · 本地后端（Mock 与浏览器直连引擎共用权威实现）
 *
 * 桌面应用（联调/生产）以 Rust command + SQLite 为权威；本模块只服务两条非 Rust 通道，
 * 使 Mock 开关规范下 /hooks、/archive、/computer-use 可自测：
 * - 开发 Mock 模式（VITE_MOCK 未关闭）
 * - 浏览器直连引擎模式（VITE_MOCK=false，引擎侧无对齐整改存储）
 *
 * 与 Rust `align::{hooks,archive}` 同口径：同一份事件枚举、同一套校验错误码、
 * 同样的「无记录即空」语义（不伪造已保存的 Hook 配置）。
 * 浏览器无法执行系统命令，试运行如实抛 HOOK_TEST_UNSUPPORTED，而不是假报 exitCode 0。
 *
 * 数据保护：台账只写 localStorage（`tt:hooks` / `tt:hookGrants` / `tt:archived` /
 * `tt:computerUsePolicy`），删除仅限用户点名的条目；清空归档只清标记，不动任务本体。
 */

import type {
  AcpConfigState,
  AcpServerStatus,
  ArchiveRecord,
  BrowserUseCapabilities,
  BrowserUsePolicy,
  ComputerUseCapabilities,
  ComputerUsePolicy,
  RemoteDesktopCapabilities,
  RemoteDesktopSession,
  RemoteDesktopStopResp,
  TerminalCapabilities,
  HookConfigStore,
  HookEventName,
  HookGrant,
  HooksStatus,
} from '../../shared/contracts';

// ── 与 Rust 对齐的常量 ────────────────────────────────────────

/** 12 类 Hook 事件（与 Rust `align::hooks::ALL_EVENTS`、contracts.ts 完全一致） */
export const HOOK_EVENTS: HookEventName[] = [
  'SessionStart',
  'SessionEnd',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'PostToolUseFailure',
  'Stop',
  'SubagentStart',
  'SubagentStop',
  'PreCompact',
  'Notification',
  'PermissionRequest',
];

/** 支持阻塞（exit code 2）的事件（与 Rust `BLOCKABLE_EVENTS` 一致） */
export const BLOCKABLE_EVENTS: HookEventName[] = ['PreToolUse', 'Stop', 'SubagentStop'];

/**
 * 引擎执行链路里真实存在触发节点的事件（与 Rust `align::hooks::FIRED_EVENTS` 一致）。
 * 页面据此说明「配了就会跑」的范围，不能把没触发点的事件也宣称为生效。
 */
export const FIRED_HOOK_EVENTS: HookEventName[] = [
  'SessionStart',
  'SessionEnd',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'PostToolUseFailure',
  'PreCompact',
  'Stop',
  'SubagentStart',
  'SubagentStop',
];

/** 当前执行链路没有触发点的事件（与 Rust `UNFIRED_EVENTS` 一致） */
export const UNFIRED_HOOK_EVENTS: HookEventName[] = [
  'Notification',
  'PermissionRequest',
];

export const DEFAULT_TIMEOUT_SEC = 60;
export const MAX_TIMEOUT_SEC = 600;

export const COMPUTER_USE_POLICIES: ComputerUsePolicy[] = ['ask_every_time', 'auto', 'disabled'];
export const COMPUTER_USE_DEFAULT: ComputerUsePolicy = 'ask_every_time';

export const BROWSER_USE_POLICIES: BrowserUsePolicy[] = ['ask_every_time', 'auto', 'disabled'];
export const BROWSER_USE_DEFAULT: BrowserUsePolicy = 'ask_every_time';

export const CLEAR_CONFIRM_WORD = 'DELETE';

const DEFAULT_WORKSPACE = 'default';

// ── localStorage 台账 ────────────────────────────────────────

const KEY_HOOKS = 'tt:hooks';
const KEY_HOOK_GRANTS = 'tt:hookGrants';
const KEY_ARCHIVED = 'tt:archived';
const KEY_COMPUTER_USE = 'tt:computerUsePolicy';
const KEY_BROWSER_USE = 'tt:browserUsePolicy';

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function readJson<T>(key: string): T | null {
  const s = storage();
  if (!s) return null;
  try {
    const raw = s.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  const s = storage();
  if (!s) return;
  try {
    s.setItem(key, JSON.stringify(value));
  } catch {
    /* 存储不可用/超限不影响本次操作结果 */
  }
}

// ── Hooks：校验与读写 ────────────────────────────────────────

/** 事件名校验（与 Rust ensure_event 同错误码） */
export function ensureHookEvent(event: unknown): HookEventName {
  const name = String(event ?? '').trim();
  if (!(HOOK_EVENTS as string[]).includes(name)) {
    throw new Error(
      `HOOK_INVALID_EVENT: 不支持的 Hook 事件「${name}」，可选值：${HOOK_EVENTS.join(' / ')}`,
    );
  }
  return name as HookEventName;
}

/** 命令校验（空命令一律拒绝，避免保存出永不触发的假 Hook） */
export function ensureHookCommand(command: unknown, event = ''): string {
  const cmd = String(command ?? '').trim();
  if (!cmd) {
    throw new Error(`HOOK_INVALID_COMMAND: 事件「${event}」存在空命令`);
  }
  return cmd;
}

/** 超时归一：非 1~600 的整数秒落默认档（与 Rust clamp_timeout 一致） */
export function clampHookTimeout(timeout: unknown): number {
  const secs = Number(timeout);
  if (!Number.isInteger(secs) || secs <= 0 || secs > MAX_TIMEOUT_SEC) return DEFAULT_TIMEOUT_SEC;
  return secs;
}

/**
 * 配置校验（与 Rust validate_config 逐条同口径）：
 * 事件名合法 → 分组为数组 → matcher 为字符串或省略 → 分组含 hooks 数组 → 命令非空 → 超时 1~600
 */
export function validateHookConfig(config: unknown): void {
  const holder = (config ?? {}) as Record<string, unknown>;
  const hooks = (holder.hooks ?? holder) as Record<string, unknown>;
  if (typeof hooks !== 'object' || hooks === null || Array.isArray(hooks)) {
    throw new Error('HOOK_CONFIG_INVALID: hooks 必须是事件名到分组数组的对象');
  }
  Object.entries(hooks).forEach(([event, groups]) => {
    ensureHookEvent(event);
    if (!Array.isArray(groups)) {
      throw new Error(`HOOK_INVALID_GROUP: 事件「${event}」的值必须是分组建制数组`);
    }
    groups.forEach((group) => {
      const g = (group ?? {}) as Record<string, unknown>;
      if ('matcher' in g && g.matcher != null && typeof g.matcher !== 'string') {
        throw new Error(`HOOK_INVALID_MATCHER: 事件「${event}」的 matcher 必须是字符串`);
      }
      if (!Array.isArray(g.hooks)) {
        throw new Error(`HOOK_INVALID_COMMAND: 事件「${event}」的分组缺少 hooks 数组`);
      }
      (g.hooks as Record<string, unknown>[]).forEach((cmd) => {
        ensureHookCommand(cmd?.command, event);
        if (cmd?.timeout != null) {
          const secs = Number(cmd.timeout);
          if (!Number.isInteger(secs) || secs <= 0 || secs > MAX_TIMEOUT_SEC) {
            throw new Error(
              `HOOK_INVALID_TIMEOUT: 事件「${event}」的 timeout 需在 1~${MAX_TIMEOUT_SEC} 秒之间`,
            );
          }
        }
      });
    });
  });
}

/** 取 hooks 映射（兼容 `{hooks:{...}}` 与裸 `{...}` 两种提交形态） */
function hooksMap(config: unknown): Record<string, unknown> {
  const holder = (config ?? {}) as Record<string, unknown>;
  return (holder.hooks ?? holder) as Record<string, unknown>;
}

/** 挂载事件数（仅统计有分组的键，与 Rust count_events 一致） */
export function countHookEvents(config: unknown): number {
  return Object.values(hooksMap(config)).filter(
    (groups) => Array.isArray(groups) && groups.length > 0,
  ).length;
}

// 内存态台账（无 localStorage 的 node/受限环境仍可用），启动时从本地恢复
let hooksLedger: HookConfigStore = { hooks: {} };
let hookGrantLedger: HookGrant[] = [];
let archiveLedger: LocalArchiveItem[] = [];
let computerUseLedger: { policy: ComputerUsePolicy; persisted: boolean } = {
  policy: COMPUTER_USE_DEFAULT,
  persisted: false,
};
let browserUseLedger: { policy: BrowserUsePolicy; persisted: boolean } = {
  policy: BROWSER_USE_DEFAULT,
  persisted: false,
};

/** 归档条目的最小合法形态（本地存档可能被旧版本或手工改写，逐条过滤而非整体抛错） */
function asArchiveItem(row: unknown): LocalArchiveItem | null {
  const r = row as Record<string, unknown> | null;
  if (!r || typeof r.taskId !== 'string' || !r.taskId) return null;
  const ms = Number(r.archivedAtMs);
  return {
    taskId: r.taskId,
    title: String(r.title ?? '(无标题任务)'),
    workspaceId: String(r.workspaceId ?? DEFAULT_WORKSPACE),
    archivedAt:
      typeof r.archivedAt === 'string' ? r.archivedAt : new Date(Number.isFinite(ms) ? ms : 0).toISOString(),
    archivedAtMs: Number.isFinite(ms) ? ms : 0,
  };
}

function hydrate(): void {
  const stored = readJson<HookConfigStore>(KEY_HOOKS);
  if (stored?.hooks && typeof stored.hooks === 'object') hooksLedger = { hooks: stored.hooks };
  const grants = readJson<HookGrant[]>(KEY_HOOK_GRANTS);
  if (Array.isArray(grants)) {
    hookGrantLedger = grants.filter(
      (g): g is HookGrant =>
        !!g && typeof g.command === 'string' && !!g.command && typeof g.event === 'string',
    );
  }
  const archived = readJson<unknown[]>(KEY_ARCHIVED);
  if (Array.isArray(archived)) {
    archiveLedger = sortArchived(archived.map(asArchiveItem).filter((r) => r !== null));
  }
  const policy = readJson<{ policy?: string }>(KEY_COMPUTER_USE)?.policy;
  const raw = String(policy ?? '').trim();
  if ((COMPUTER_USE_POLICIES as string[]).includes(raw)) {
    computerUseLedger = { policy: raw as ComputerUsePolicy, persisted: true };
  }
  const browserPolicy = readJson<{ policy?: string }>(KEY_BROWSER_USE)?.policy;
  const browserRaw = String(browserPolicy ?? '').trim();
  if ((BROWSER_USE_POLICIES as string[]).includes(browserRaw)) {
    browserUseLedger = { policy: browserRaw as BrowserUsePolicy, persisted: true };
  }
}

hydrate();

/** 测试用：复位内存台账并清空本地持久层（不触碰任何真实任务数据） */
export function resetLocalAlignment(): void {
  hooksLedger = { hooks: {} };
  hookGrantLedger = [];
  archiveLedger = [];
  computerUseLedger = { policy: COMPUTER_USE_DEFAULT, persisted: false };
  browserUseLedger = { policy: BROWSER_USE_DEFAULT, persisted: false };
  for (const key of [KEY_HOOKS, KEY_HOOK_GRANTS, KEY_ARCHIVED, KEY_COMPUTER_USE, KEY_BROWSER_USE]) {
    try {
      storage()?.removeItem(key);
    } catch {
      /* 存储不可用时内存台账已复位，本次复位视为成功 */
    }
  }
}

/** 读取 Hook 配置（未保存过时返回空 hooks，不伪造默认配置） */
export function localHooks(): HookConfigStore {
  return { hooks: hooksLedger.hooks ?? {} };
}

/** 保存 Hook 配置：校验通过才落地，返回与 Rust hooks_set 一致的结构 */
export function saveLocalHooks(config: unknown): { ok: boolean; events: number; revoked: string[] } {
  validateHookConfig(config);
  hooksLedger = { hooks: hooksMap(config) as HookConfigStore['hooks'] };
  writeJson(KEY_HOOKS, hooksLedger);
  // 与 Rust prune_grants 同口径：配置里没这条命令了，它的放行就作废
  const keep = configuredHookCommands(hooksLedger);
  const revoked = hookGrantLedger.map((g) => g.command).filter((c) => !keep.includes(c));
  if (revoked.length > 0) {
    hookGrantLedger = hookGrantLedger.filter((g) => keep.includes(g.command));
    writeJson(KEY_HOOK_GRANTS, hookGrantLedger);
  }
  return { ok: true, events: countHookEvents(config), revoked };
}

/**
 * 试运行：浏览器/引擎直连通道无系统命令执行能力，如实报错。
 * 桌面应用内由 Rust hooks_test 真实拉起进程。
 */
export function testLocalHook(event: unknown, command: unknown): never {
  ensureHookEvent(event);
  ensureHookCommand(command, String(event));
  throw new Error('HOOK_TEST_UNSUPPORTED: 当前通道无法执行系统命令，请在桌面应用内试运行');
}

/** 配置里出现过的命令原文（去重、保持配置顺序；与 Rust `configured_commands` 同口径） */
export function configuredHookCommands(config: unknown): string[] {
  const out: string[] = [];
  Object.values(hooksMap(config)).forEach((groups) => {
    (Array.isArray(groups) ? groups : []).forEach((group) => {
      const cmds = (group as { hooks?: { command?: unknown }[] }).hooks ?? [];
      cmds.forEach((cmd) => {
        const raw = String(cmd?.command ?? '').trim();
        if (raw && !out.includes(raw)) out.push(raw);
      });
    });
  });
  return out;
}

/** 放行台账（与 Rust hook_grants 同语义：按命令原文去重，重复放行只刷新时间） */
export function localHookGrants(): HookGrant[] {
  return hookGrantLedger.map((g) => ({ ...g }));
}

export function grantLocalHooks(event: unknown, command: unknown): { ok: true; granted: true } {
  const ev = ensureHookEvent(event);
  const cmd = ensureHookCommand(command, String(event));
  const now = Date.now();
  const existing = hookGrantLedger.find((g) => g.command === cmd);
  if (existing) {
    existing.event = ev;
    existing.grantedAtMs = now;
  } else {
    hookGrantLedger.push({
      command: cmd,
      event: ev,
      grantedAtMs: now,
      runCount: 0,
      lastRunAtMs: null,
    });
  }
  writeJson(KEY_HOOK_GRANTS, hookGrantLedger);
  return { ok: true, granted: true };
}

/** 撤销放行：只删授权记录，`hooks` 配置原样保留（撤权 ≠ 删配置） */
export function revokeLocalHooks(command: unknown): { ok: true; granted: false } {
  const cmd = String(command ?? '').trim();
  hookGrantLedger = hookGrantLedger.filter((g) => g.command !== cmd);
  writeJson(KEY_HOOK_GRANTS, hookGrantLedger);
  return { ok: true, granted: false };
}

/**
 * 执行面概览（与 Rust `hooks_status` 同口径）。
 * 本通道没有本机网关，所以放行只是意愿记录，脚本在这里永远不会被执行——
 * 页面据此显示如实的「当前通道不会执行」，而不是让用户以为点了放行就生效。
 */
export function localHooksStatus(): HooksStatus {
  const commands = configuredHookCommands(hooksLedger);
  const granted = new Set(hookGrantLedger.map((g) => g.command));
  const mounted = HOOK_EVENTS.filter((e) => {
    const groups = hooksMap(hooksLedger)[e];
    return Array.isArray(groups) && groups.length > 0;
  });
  return {
    configured: commands.length,
    mountedEvents: mounted,
    firedEvents: FIRED_HOOK_EVENTS,
    unfiredEvents: UNFIRED_HOOK_EVENTS,
    mountedUnfired: mounted.filter((e) => UNFIRED_HOOK_EVENTS.includes(e)),
    ungranted: commands.filter((c) => !granted.has(c)),
    // 与 Rust list_grants 同口径：返回整本放行台账，不因当前配置筛选而隐藏记录
    grants: localHookGrants(),
    gateError: '当前通道没有本机 Hook 执行网关，放行只是意愿记录，脚本不会执行',
  };
}

// ── 任务归档 ─────────────────────────────────────────────────

/** 本地归档条目（比 contracts 多带时间戳，排序与 Rust archived_at_ms 同口径） */
export interface LocalArchiveItem extends ArchiveRecord {
  archivedAtMs: number;
}

function validateTaskId(taskId: unknown): string {
  const id = String(taskId ?? '').trim();
  if (!id) throw new Error('ARCHIVE_INVALID_TASK_ID: 任务标识不能为空');
  return id;
}

function sortArchived(rows: LocalArchiveItem[]): LocalArchiveItem[] {
  return [...rows].sort(
    (a, b) => b.archivedAtMs - a.archivedAtMs || a.taskId.localeCompare(b.taskId),
  );
}

export function localArchived(): LocalArchiveItem[] {
  return [...archiveLedger];
}

/**
 * 归档一个任务（只写归档标记，任务本体与消息数据不动）。
 * Rust 侧标题取自 tasks 表；本层无任务表，标题由调用方给出，缺失时诚实标注。
 */
export function archiveLocalTask(
  taskId: unknown,
  title?: unknown,
  workspaceId?: unknown,
  nowMs: number = Date.now(),
): LocalArchiveItem {
  const id = validateTaskId(taskId);
  const ws = String(workspaceId ?? '').trim() || DEFAULT_WORKSPACE;
  const raw = String(title ?? '').trim();
  const item: LocalArchiveItem = {
    taskId: id,
    title: raw || '(无标题任务)',
    workspaceId: ws,
    archivedAt: new Date(nowMs).toISOString(),
    archivedAtMs: nowMs,
  };
  // 重复归档刷新时间，不追加脏行
  archiveLedger = sortArchived([item, ...archiveLedger.filter((r) => r.taskId !== id)]);
  writeJson(KEY_ARCHIVED, archiveLedger);
  return item;
}

/** 取消归档：只移除标记 */
export function unarchiveLocalTask(taskId: unknown): boolean {
  const id = validateTaskId(taskId);
  const existed = archiveLedger.some((r) => r.taskId === id);
  archiveLedger = archiveLedger.filter((r) => r.taskId !== id);
  writeJson(KEY_ARCHIVED, archiveLedger);
  return existed;
}

/** 彻底删除：本地通道无任务本体，仅移除该条归档标记 */
export function deleteLocalArchivedTask(taskId: unknown): boolean {
  return unarchiveLocalTask(taskId);
}

/** 清空归档清单：必须带确认词 DELETE，且只清标记 */
export function clearLocalArchive(confirm: unknown): number {
  if (String(confirm ?? '').trim() !== CLEAR_CONFIRM_WORD) {
    throw new Error(`ARCHIVE_CONFIRM_REQUIRED: 清空归档清单需输入确认词 ${CLEAR_CONFIRM_WORD}`);
  }
  const cleared = archiveLedger.length;
  archiveLedger = [];
  writeJson(KEY_ARCHIVED, archiveLedger);
  return cleared;
}

// ── 电脑操控策略 ─────────────────────────────────────────────

/** 读取策略（未设置过时返回默认档，并以 persisted 标记是否已持久化） */
export function localComputerUsePolicy(): { policy: ComputerUsePolicy; persisted: boolean } {
  return { ...computerUseLedger };
}

/** 保存策略：档位非法即拒（与 Rust computer_use_set_policy 同错误码） */
export function setLocalComputerUsePolicy(policy: unknown): ComputerUsePolicy {
  const raw = String(policy ?? '')
    .trim()
    .replace(/^"|"$/g, '');
  if (!(COMPUTER_USE_POLICIES as string[]).includes(raw)) {
    throw new Error(
      `COMPUTER_USE_INVALID_POLICY: 电脑操控档位非法「${raw}」，仅支持 ${COMPUTER_USE_POLICIES.join(' / ')}`,
    );
  }
  computerUseLedger = { policy: raw as ComputerUsePolicy, persisted: true };
  writeJson(KEY_COMPUTER_USE, { policy: computerUseLedger.policy });
  return computerUseLedger.policy;
}

/**
 * 浏览器/Mock 通道的电脑操控能力视图：如实返回「没有执行器」。
 * 页面据此置灰截屏自检并说明原因，而不是拿设置里的档位假装能截屏。
 */
export function localComputerUseCapabilities(): ComputerUseCapabilities {
  return {
    supported: false,
    platform: 'browser',
    features: { screenshot: false, input: false },
    policy: computerUseLedger.policy,
    persisted: computerUseLedger.persisted,
    gate: false,
    reason: '浏览器/Mock 通道没有系统截屏与键鼠注入能力，电脑操控只在 Windows 桌面版可用',
  };
}

/** 本地通道无法截屏：如实报错，不返回占位图 */
export function captureLocalComputerUseScreen(): never {
  throw new Error(
    'COMPUTER_USE_UNSUPPORTED: 当前通道没有系统截屏能力，请在 Windows 桌面版通天办公中自检',
  );
}

// ── 远程桌面（本机画面；浏览器/Mock 通道没有截屏能力） ──────

export const REMOTE_SESSION_MAX_MS = 10 * 60 * 1000;
export const REMOTE_FRAME_INTERVAL_MS = 700;

const REMOTE_INACTIVE: RemoteDesktopSession = {
  active: false,
  startedAtMs: 0,
  expiresAtMs: 0,
  remainingMs: 0,
  frames: 0,
  frameIntervalMs: REMOTE_FRAME_INTERVAL_MS,
  sessionMaxMs: REMOTE_SESSION_MAX_MS,
};

/** 如实返回「没有本机桌面执行器」；跨主机同样是明确不支持，而不是待实现 */
export function localRemoteDesktopCapabilities(): RemoteDesktopCapabilities {
  return {
    supported: false,
    platform: 'browser',
    localView: false,
    frameIntervalMs: REMOTE_FRAME_INTERVAL_MS,
    sessionMaxMs: REMOTE_SESSION_MAX_MS,
    reason: '浏览器/Mock 通道没有系统截屏能力，远程桌面只在 Windows 桌面版可用',
    crossHost: {
      supported: false,
      reason: '当前版本不做跨主机画面转发；需要中继服务与主机配对，尚未提供',
    },
  };
}

export function localRemoteDesktopStatus(): RemoteDesktopSession {
  return { ...REMOTE_INACTIVE };
}

export function startLocalRemoteDesktop(): never {
  throw new Error(
    'REMOTE_UNSUPPORTED: 当前通道没有本机桌面截屏能力，远程桌面只在 Windows 桌面版可用',
  );
}

export function frameLocalRemoteDesktop(): never {
  throw new Error(
    'REMOTE_UNSUPPORTED: 当前通道没有本机桌面截屏能力，远程桌面只在 Windows 桌面版可用',
  );
}

/** 结束是幂等的：本地本来就没有会话，如实返回「未在运行」 */
export function stopLocalRemoteDesktop(): RemoteDesktopStopResp {
  return { ok: true, active: false, frames: 0, durationMs: 0, session: { ...REMOTE_INACTIVE } };
}

// ── 浏览器 AI+ 策略 ─────────────────────────────────────────

export function localBrowserUsePolicy(): { policy: BrowserUsePolicy; persisted: boolean } {
  return { ...browserUseLedger };
}

/** 保存策略：档位非法即拒（与 Rust browser_use_set_policy 同错误码） */
export function setLocalBrowserUsePolicy(policy: unknown): BrowserUsePolicy {
  const raw = String(policy ?? '')
    .trim()
    .replace(/^"|"$/g, '');
  if (!(BROWSER_USE_POLICIES as string[]).includes(raw)) {
    throw new Error(
      `BROWSER_USE_INVALID_POLICY: 浏览器档位非法「${raw}」，仅支持 ${BROWSER_USE_POLICIES.join(' / ')}`,
    );
  }
  browserUseLedger = { policy: raw as BrowserUsePolicy, persisted: true };
  writeJson(KEY_BROWSER_USE, { policy: browserUseLedger.policy });
  return browserUseLedger.policy;
}

/**
 * 浏览器/Mock 通道的能力视图：如实返回「没有受控浏览器」。
 * 浏览器无法启动独立进程、也没有本机网关，页面据此置灰启动按钮并说明原因。
 */
export function localBrowserUseCapabilities(): BrowserUseCapabilities {
  return {
    supported: false,
    platform: 'browser',
    browser: null,
    executable: null,
    features: {
      snapshot: false,
      click: false,
      type: false,
      scroll: false,
      screenshot: false,
      navigation: false,
    },
    policy: browserUseLedger.policy,
    persisted: browserUseLedger.persisted,
    gate: false,
    running: false,
    port: null,
    profileDir: '',
    reason: '浏览器/Mock 通道没有受控浏览器进程与本机网关，浏览器 AI+ 只在桌面版可用',
  };
}

/** 本地通道没有受控浏览器：如实报错，不假装启动成功 */
export function startLocalBrowserUseSession(): never {
  throw new Error(
    'BROWSER_USE_NOT_FOUND: 当前通道没有受控浏览器执行器，请在桌面版通天办公中使用',
  );
}

export function stopLocalBrowserUseSession(): never {
  throw new Error('BROWSER_USE_STOP_FAILED: 当前通道没有受控浏览器会话');
}

// ── 终端 AI+（浏览器/Mock 通道如实不可用） ───────────────────

const LOCAL_TERMINAL_INPUT_LIMIT = 64 * 1024;

const localAcpConfig = (): AcpConfigState => ({
  enabled: false,
  command: '',
  permission: 'deny',
  timeoutSeconds: 600,
  persisted: false,
  permissions: ['deny', 'allow'],
  error: '当前通道没有桌面端设置存储，ACP 委派配置只在桌面版通天办公中生效',
});

/**
 * 浏览器/Mock 通道没有本机 shell 进程，也没有引擎的终端授权令牌。
 * 这里如实返回 pipe=false + 原因，页面据此置灰「打开终端」，不制造能用的假象。
 */
export function localTerminalCapabilities(): TerminalCapabilities {
  return {
    enabled: false,
    persisted: false,
    platform: 'browser',
    shell: '',
    pipe: false,
    maxSessions: 0,
    inputLimit: LOCAL_TERMINAL_INPUT_LIMIT,
    reason: '浏览器/Mock 通道没有本机 shell 进程，终端 AI+ 只在桌面版通天办公中可用',
    engineRunning: false,
    engineUrl: null,
    acp: localAcpConfig(),
  };
}

export function localTerminalPolicy(): { enabled: boolean; persisted: boolean } {
  return { enabled: false, persisted: false };
}

function terminalUnsupported(): never {
  throw new Error(
    'TERMINAL_UNSUPPORTED: 当前通道没有本机 shell 进程，终端 AI+ 只在桌面版通天办公中可用',
  );
}

export const setLocalTerminalEnabled = terminalUnsupported;
export const openLocalTerminal = terminalUnsupported;
export const writeLocalTerminal = terminalUnsupported;
export const readLocalTerminal = terminalUnsupported;
export const closeLocalTerminal = terminalUnsupported;
export const suggestLocalTerminal = terminalUnsupported;

export function setLocalAcpConfig(): never {
  throw new Error(
    'TERMINAL_UNSUPPORTED: 当前通道没有桌面端设置存储，ACP 配置只在桌面版通天办公中生效',
  );
}

/** 本地通道没有 Python/引擎进程可承接 ACP 服务端，如实说明而不是给一条假命令 */
export function localAcpStatus(): AcpServerStatus {
  return {
    enabled: false,
    command: null,
    engineUrl: null,
    engineRunning: false,
    python: null,
    reason: '浏览器/Mock 通道没有本机 Python 与引擎进程，无法启动 ACP 服务端',
    protocolVersion: 1,
    note: '',
  };
}

// ── 通道兜底（Mock 与引擎直连共用，无 Rust 后端时启用） ────────

export interface LocalAlignmentResult {
  handled: boolean;
  value?: unknown;
}

/** 对齐整改通道本地实现：返回 `{handled:false}` 时交由上层通道继续处理 */
export function handleLocalAlignmentChannel(
  channel: string,
  args: unknown[],
): LocalAlignmentResult {
  const payload = (args[0] ?? {}) as Record<string, unknown>;

  switch (channel) {
    case 'hooks:get':
      return { handled: true, value: localHooks() };
    case 'hooks:set':
      return { handled: true, value: saveLocalHooks(payload) };
    case 'hooks:test':
      return testLocalHook(payload.event, payload.command);
    case 'hooks:grant':
      return { handled: true, value: grantLocalHooks(payload.event, payload.command) };
    case 'hooks:revoke':
      return { handled: true, value: revokeLocalHooks(payload.command) };
    case 'hooks:status':
      return { handled: true, value: localHooksStatus() };
    case 'task:archive':
      return {
        handled: true,
        value: archiveLocalTask(payload.taskId, payload.title, payload.workspaceId),
      };
    case 'task:archiveList':
      return { handled: true, value: localArchived() };
    case 'task:unarchive':
      return {
        handled: true,
        value: { ok: true, removed: unarchiveLocalTask(payload.taskId) },
      };
    case 'task:archiveDelete':
      return {
        handled: true,
        value: { ok: true, removed: deleteLocalArchivedTask(payload.taskId) },
      };
    case 'task:archiveClear': {
      const cleared = clearLocalArchive(payload.confirm);
      return { handled: true, value: { ok: true, cleared } };
    }
    case 'computerUse:getPolicy':
      return { handled: true, value: localComputerUsePolicy() };
    case 'computerUse:setPolicy': {
      const policy = setLocalComputerUsePolicy(payload.policy);
      return { handled: true, value: { ok: true, policy } };
    }
    case 'computerUse:capabilities':
      return { handled: true, value: localComputerUseCapabilities() };
    case 'computerUse:capture':
      return captureLocalComputerUseScreen();
    case 'remote:capabilities':
      return { handled: true, value: localRemoteDesktopCapabilities() };
    case 'remote:status':
      return { handled: true, value: localRemoteDesktopStatus() };
    case 'remote:start':
      return { handled: true, value: startLocalRemoteDesktop() };
    case 'remote:frame':
      return { handled: true, value: frameLocalRemoteDesktop() };
    case 'remote:stop':
      return { handled: true, value: stopLocalRemoteDesktop() };
    case 'browserUse:getPolicy':
      return { handled: true, value: localBrowserUsePolicy() };
    case 'browserUse:setPolicy': {
      const policy = setLocalBrowserUsePolicy(payload.policy);
      return { handled: true, value: { ok: true, policy } };
    }
    case 'browserUse:capabilities':
      return { handled: true, value: localBrowserUseCapabilities() };
    case 'browserUse:start':
      return startLocalBrowserUseSession();
    case 'browserUse:stop':
      return stopLocalBrowserUseSession();
    case 'terminal:getPolicy':
      return { handled: true, value: localTerminalPolicy() };
    case 'terminal:setPolicy':
      return { handled: true, value: setLocalTerminalEnabled() };
    case 'terminal:capabilities':
      return { handled: true, value: localTerminalCapabilities() };
    case 'terminal:open':
      return { handled: true, value: openLocalTerminal() };
    case 'terminal:input':
      return { handled: true, value: writeLocalTerminal() };
    case 'terminal:read':
      return { handled: true, value: readLocalTerminal() };
    case 'terminal:close':
      return { handled: true, value: closeLocalTerminal() };
    case 'terminal:ai':
      return { handled: true, value: suggestLocalTerminal() };
    case 'acp:getConfig':
      return { handled: true, value: localAcpConfig() };
    case 'acp:setConfig':
      return { handled: true, value: setLocalAcpConfig() };
    case 'acp:status':
      return { handled: true, value: localAcpStatus() };
    default:
      return { handled: false };
  }
}
