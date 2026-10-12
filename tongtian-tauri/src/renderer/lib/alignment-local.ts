/**
 * 千问对标整改通道 · 本地后端（Mock 与浏览器直连引擎共用权威实现）
 *
 * 桌面应用（联调/生产）以 Rust command + SQLite 为权威；本模块只服务两条非 Rust 通道，
 * 使 Mock 开关规范下 /hooks、/extensions?tab=connectors、/archive、/computer-use 可自测：
 * - 开发 Mock 模式（VITE_MOCK 未关闭）
 * - 浏览器直连引擎模式（VITE_MOCK=false，引擎侧无对齐整改存储）
 *
 * 与 Rust `align::{hooks,connector,archive}` 同口径：同一份事件枚举、同一套校验错误码、
 * 同样的「无记录即空」语义（不伪造已授权连接器、不伪造已保存的 Hook 配置）。
 * 浏览器无法执行系统命令，试运行如实抛 HOOK_TEST_UNSUPPORTED，而不是假报 exitCode 0。
 *
 * 数据保护：台账只写 localStorage（`tt:hooks` / `tt:connectors` / `tt:archived` /
 * `tt:computerUsePolicy`），删除仅限用户点名的条目；清空归档只清标记，不动任务本体。
 */

import type {
  ArchiveRecord,
  ComputerUsePolicy,
  ConnectorState,
  HookConfigStore,
  HookEventName,
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

export const DEFAULT_TIMEOUT_SEC = 60;
export const MAX_TIMEOUT_SEC = 600;

export const COMPUTER_USE_POLICIES: ComputerUsePolicy[] = ['ask_every_time', 'auto', 'disabled'];
export const COMPUTER_USE_DEFAULT: ComputerUsePolicy = 'ask_every_time';

export const CLEAR_CONFIRM_WORD = 'DELETE';

const DEFAULT_WORKSPACE = 'default';

// ── localStorage 台账 ────────────────────────────────────────

const KEY_HOOKS = 'tt:hooks';
const KEY_CONNECTORS = 'tt:connectors';
const KEY_ARCHIVED = 'tt:archived';
const KEY_COMPUTER_USE = 'tt:computerUsePolicy';

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
let connectorLedger: ConnectorState[] = [];
let archiveLedger: LocalArchiveItem[] = [];
let computerUseLedger: { policy: ComputerUsePolicy; persisted: boolean } = {
  policy: COMPUTER_USE_DEFAULT,
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
  const connectors = readJson<ConnectorState[]>(KEY_CONNECTORS);
  if (Array.isArray(connectors)) {
    connectorLedger = sortConnectors(
      connectors.filter(
        (c): c is ConnectorState =>
          !!c && typeof c.connectorId === 'string' && !!c.connectorId,
      ),
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
}

hydrate();

/** 测试用：复位内存台账并清空本地持久层（不触碰任何真实任务数据） */
export function resetLocalAlignment(): void {
  hooksLedger = { hooks: {} };
  connectorLedger = [];
  archiveLedger = [];
  computerUseLedger = { policy: COMPUTER_USE_DEFAULT, persisted: false };
  for (const key of [KEY_HOOKS, KEY_CONNECTORS, KEY_ARCHIVED, KEY_COMPUTER_USE]) {
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
export function saveLocalHooks(config: unknown): { ok: boolean; events: number } {
  validateHookConfig(config);
  hooksLedger = { hooks: hooksMap(config) as HookConfigStore['hooks'] };
  writeJson(KEY_HOOKS, hooksLedger);
  return { ok: true, events: countHookEvents(config) };
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

// ── 连接器授权 ───────────────────────────────────────────────

function validateConnectorId(connectorId: unknown): string {
  const id = String(connectorId ?? '').trim();
  if (!id) throw new Error('CONNECTOR_INVALID_ID: 连接器标识不能为空');
  return id;
}

function validateConnectorCategory(category: unknown): 'builtin' | 'market' {
  const raw = String(category ?? '').trim();
  if (!raw) return 'builtin';
  if (raw !== 'builtin' && raw !== 'market') {
    throw new Error(
      `CONNECTOR_INVALID_CATEGORY: 连接器分类非法「${raw}」，仅支持 builtin / market`,
    );
  }
  return raw;
}

function sortConnectors(rows: ConnectorState[]): ConnectorState[] {
  return [...rows].sort((a, b) => a.connectorId.localeCompare(b.connectorId));
}

export function localConnectors(): ConnectorState[] {
  return [...connectorLedger];
}

/** 授权（安装）：幂等 upsert，重复点击不产生脏数据 */
export function authorizeLocalConnector(
  connectorId: unknown,
  category?: unknown,
  serverRef?: unknown,
): ConnectorState {
  const id = validateConnectorId(connectorId);
  const cat = validateConnectorCategory(category);
  const ref = String(serverRef ?? '').trim();
  const existed = connectorLedger.find((c) => c.connectorId === id);
  const view: ConnectorState = {
    connectorId: id,
    category: cat,
    installed: true,
    authStatus: 'authorized',
    // 与 Rust COALESCE 一致：本次未给引用则沿用已有
    serverRef: ref || existed?.serverRef || undefined,
    updatedAt: new Date().toISOString(),
  };
  connectorLedger = sortConnectors([...connectorLedger.filter((c) => c.connectorId !== id), view]);
  writeJson(KEY_CONNECTORS, connectorLedger);
  return view;
}

/** 撤销授权：保留条目以显示「曾配置但未授权」，无授权记录时明确报错 */
export function revokeLocalConnector(connectorId: unknown): ConnectorState {
  const id = validateConnectorId(connectorId);
  const existed = connectorLedger.find((c) => c.connectorId === id);
  if (!existed) {
    throw new Error(`CONNECTOR_NOT_FOUND: 连接器「${id}」没有授权记录，无需撤销`);
  }
  const view: ConnectorState = {
    ...existed,
    installed: false,
    authStatus: 'none',
    updatedAt: new Date().toISOString(),
  };
  connectorLedger = connectorLedger.map((c) => (c.connectorId === id ? view : c));
  writeJson(KEY_CONNECTORS, connectorLedger);
  return view;
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
    case 'connector:list':
      return { handled: true, value: localConnectors() };
    case 'connector:authorize':
      return {
        handled: true,
        value: authorizeLocalConnector(payload.connectorId, payload.category, payload.serverRef),
      };
    case 'connector:revoke': {
      const view = revokeLocalConnector(payload.connectorId);
      return { handled: true, value: { ok: true, connectorId: view.connectorId } };
    }
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
    default:
      return { handled: false };
  }
}
