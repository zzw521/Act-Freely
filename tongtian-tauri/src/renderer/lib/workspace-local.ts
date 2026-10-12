/**
 * 工作空间 · 本地权威实现（Mock 与浏览器直连引擎共用）
 *
 * 桌面应用以 `{data_home}/workspaces/*` 目录为事实来源（Rust `workspace::list_in`）；
 * 浏览器无法列目录，因此本层维护同名口径的本地台账（localStorage `tt:workspaces`）：
 * - `default` 恒在，未落地时 exists=false（不伪装成已创建的目录）
 * - 标识校验与 Rust `normalize_id` 逐条同错误码（WORKSPACE_INVALID_ID）
 * - 创建幂等，且不提供删除能力（删除会毁掉工作区文件，与数据保护规范冲突）
 * - 运行态如实为 stopped：浏览器通道没有 Agent 子进程可查
 */

import type { WorkspaceInfo } from '../../shared/contracts';

export const DEFAULT_WORKSPACE = 'default';
const MAX_ID_LEN = 64;
const KEY_WORKSPACES = 'tt:workspaces';

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

/** 标识校验（与 Rust `workspace::normalize_id` 同口径同错误码） */
export function normalizeWorkspaceId(raw: unknown): string {
  const id = String(raw ?? '')
    .trim()
    .toLowerCase();
  if (!id) throw new Error('WORKSPACE_INVALID_ID: 工作空间标识不能为空');
  if (id.length > MAX_ID_LEN) {
    throw new Error(
      `WORKSPACE_INVALID_ID: 工作空间标识过长（${id.length} 字符，上限 ${MAX_ID_LEN}）`,
    );
  }
  if (id.includes('.') || id.includes('/') || id.includes('\\')) {
    throw new Error(`WORKSPACE_INVALID_ID: 工作空间标识「${id}」含路径分隔符或点号`);
  }
  if (!/^[a-z0-9_-]+$/.test(id)) {
    throw new Error(
      `WORKSPACE_INVALID_ID: 工作空间标识「${id}」仅支持小写字母、数字、下划线与连字符`,
    );
  }
  return id;
}

interface LocalWorkspaceRow {
  id: string;
  createdAtMs: number;
}

let ledger: LocalWorkspaceRow[] = [];

function hydrate(): void {
  try {
    const rows = JSON.parse(storage()?.getItem(KEY_WORKSPACES) ?? '[]') as LocalWorkspaceRow[];
    if (Array.isArray(rows)) {
      ledger = rows
        .map((r) => ({ id: String(r?.id ?? ''), createdAtMs: Number(r?.createdAtMs) || 0 }))
        .filter((r) => Boolean(r.id));
    }
  } catch {
    ledger = [];
  }
}

function persist(): void {
  try {
    storage()?.setItem(KEY_WORKSPACES, JSON.stringify(ledger));
  } catch {
    /* 存储不可用时本次操作结果仍然有效 */
  }
}

hydrate();

function toView(row: LocalWorkspaceRow): WorkspaceInfo {
  return {
    id: row.id,
    name: row.id,
    exists: true,
    createdAtMs: row.createdAtMs,
    entryCount: 0,
    rootPath: `运行目录/workspaces/${row.id}`,
    running: false,
    status: 'stopped',
  };
}

/** 列出工作空间：default 恒在（未落地时 exists=false），其余按标识字典序 */
export function listLocalWorkspaces(): WorkspaceInfo[] {
  const rows = [...ledger].sort((a, b) => a.id.localeCompare(b.id));
  const views = rows.map(toView);
  if (!rows.some((r) => r.id === DEFAULT_WORKSPACE)) {
    views.unshift({
      id: DEFAULT_WORKSPACE,
      name: DEFAULT_WORKSPACE,
      exists: false,
      createdAtMs: 0,
      entryCount: 0,
      rootPath: `运行目录/workspaces/${DEFAULT_WORKSPACE}`,
      running: false,
      status: 'stopped',
    });
  }
  return views;
}

/** 创建工作空间（幂等：重复创建返回现状，不改时间戳也不覆盖） */
export function createLocalWorkspace(
  workspaceId: unknown,
  nowMs: number = Date.now(),
): WorkspaceInfo {
  const id = normalizeWorkspaceId(workspaceId);
  const existed = ledger.find((r) => r.id === id);
  if (existed) return toView(existed);
  const row: LocalWorkspaceRow = { id, createdAtMs: nowMs };
  ledger = [...ledger, row];
  persist();
  return toView(row);
}

/** 测试用：复位本地台账 */
export function resetLocalWorkspaces(): void {
  ledger = [];
  try {
    storage()?.removeItem(KEY_WORKSPACES);
  } catch {
    /* ignore */
  }
}

export interface LocalWorkspaceResult {
  handled: boolean;
  value?: unknown;
}

/** 工作空间通道本地实现：未命中返回 handled=false */
export function handleLocalWorkspaceChannel(
  channel: string,
  args: unknown[],
): LocalWorkspaceResult {
  const payload = (args[0] ?? {}) as Record<string, unknown>;
  switch (channel) {
    case 'workspace:list':
      return { handled: true, value: listLocalWorkspaces() };
    case 'workspace:create':
      return {
        handled: true,
        value: createLocalWorkspace(payload.workspaceId ?? payload.id),
      };
    default:
      return { handled: false };
  }
}
