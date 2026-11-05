/**
 * 文件访问档位（真实目录里的创建 / 更新 / 删除）
 *
 * 三条轴要分清，别把它们混成一个开关：
 * - `toolApprovalMode`（off/sensitive/all）管「要不要逐个工具问」；
 * - 本模块的 `FilePermissionMode` 管「能写到哪、能不能删」；
 * - 合规硬开关管「能不能出网」。
 *
 * 语义（与用户可见的三档一一对应）：
 * - ask_every_time 每次询问：每次写删都走逐工具审批网关，越界也照旧拒绝；
 * - auto_approve   自动审批：只在会话绑定的工作目录内自动放行，越出目录仍拒绝；
 * - full_access    完全访问：可越出绑定目录（家目录 / 系统目录等仍硬拒），且只有这一档允许删除。
 *
 * 两件事在本模块里定死，供 UI、store 与契约测试共用同一份实现：
 * 1. 非法值一律归一到最保守档 `ask_every_time`——存量脏值不能变成「完全访问」；
 * 2. `full_access` 必须带着确认时间才落地：没确认过的完全访问在读取时就降级回每次询问，
 *    这样任何绕过弹窗写入台账的代码路径都不会真的放开边界。
 */

import type { FilePermissionMode } from '../../shared/types';

export interface FilePermissionDef {
  id: FilePermissionMode;
  /** 用户可见的档位名（中文原文即 i18n 键） */
  name: string;
  /** 一句话说明这档实际放开或收紧了什么 */
  desc: string;
  /** 放开目录边界并允许删除的那一档：UI 必须走红色确认 */
  danger: boolean;
}

export const FILE_PERMISSION_DEFS: readonly FilePermissionDef[] = [
  {
    id: 'ask_every_time',
    name: '每次询问',
    desc: '每次创建、更新、删除都先问你一次；越出工作目录仍然拒绝。',
    danger: false,
  },
  {
    id: 'auto_approve',
    name: '自动审批',
    desc: '会话绑定的工作目录内直接放行；写到目录外仍然拒绝，删除仍然需要完全访问。',
    danger: false,
  },
  {
    id: 'full_access',
    name: '完全访问',
    desc: '可以读写工作目录之外的本机路径，并允许删除文件；家目录与系统关键目录仍然硬拒。',
    danger: true,
  },
] as const;

export const FILE_PERMISSION_LABELS: Record<FilePermissionMode, string> = {
  ask_every_time: '每次询问',
  auto_approve: '自动审批',
  full_access: '完全访问',
};

export const DEFAULT_FILE_PERMISSION: FilePermissionMode = 'ask_every_time';

const BY_ID = FILE_PERMISSION_DEFS.reduce(
  (acc, def) => {
    acc[def.id] = def;
    return acc;
  },
  {} as Record<FilePermissionMode, FilePermissionDef>,
);

export function filePermissionDef(mode: FilePermissionMode): FilePermissionDef {
  return BY_ID[mode];
}

/** 归一任意来源的档位值：非法/缺失一律回到最保守档，绝不默认放开 */
export function normalizeFileMode(raw: unknown): FilePermissionMode {
  const value = String(raw ?? '').trim();
  return value === 'auto_approve' || value === 'full_access' || value === 'ask_every_time'
    ? (value as FilePermissionMode)
    : DEFAULT_FILE_PERMISSION;
}

/**
 * 「完全访问」只有留下确认时刻才算用户读过红色警告：没有 `confirmedAtMs` 一律回最保守档。
 * 台账读取与新任务页的草稿共用这一条，免得同一条降级规则被两处各自解释一遍。
 */
export function filePermissionAfterConfirm(
  mode: FilePermissionMode,
  confirmedAtMs?: number | null,
): FilePermissionMode {
  if (mode === 'full_access' && typeof confirmedAtMs !== 'number') {
    return DEFAULT_FILE_PERMISSION;
  }
  return mode;
}

// ── 每会话台账 ───────────────────────────────────────────────
//
// 不放进 tt:chatMessages 那条消息台账：隐私模式会故意不写消息台账，档位若寄身其中就会
// 在「用户选择不留痕」的会话里泄漏；也不放 SQLite 会话表，那要动 schema 与 Rust。
// 浏览器与桌面两条通道都在 localStorage 上表现一致，这与既有策略键（tt:computerUsePolicy 等）同源。

const KEY_CONVERSATION_FILE_PERMISSION = 'tt:conversationFilePermission';

interface Entry {
  mode: FilePermissionMode;
  /** 仅在完全访问档写入：用户读过红色警告并确认的时刻 */
  confirmedAtMs?: number;
}

type Ledger = Record<string, Entry>;

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function readLedger(): Ledger {
  const s = storage();
  if (!s) return {};
  try {
    const raw = s.getItem(KEY_CONVERSATION_FILE_PERMISSION);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === 'object' ? (parsed as Ledger) : {};
  } catch {
    return {};
  }
}

function writeLedger(ledger: Ledger): void {
  const s = storage();
  if (!s) return;
  try {
    s.setItem(KEY_CONVERSATION_FILE_PERMISSION, JSON.stringify(ledger));
  } catch {
    /* 存储不可用/超限不改变本次结果：档位仍活在本次运行的 store 里 */
  }
}

/**
 * 写入某会话的档位。`confirmedAtMs` 缺省时按当前时刻记；完全访问档没有确认时刻会被降级存储，
 * 免得「程序写入了完全访问」被当成「用户批准过完全访问」。
 */
export function setConversationFilePermission(
  convId: string,
  mode: FilePermissionMode,
  options?: { confirmedAtMs?: number | null },
): void {
  const key = String(convId ?? '').trim();
  if (!key) return;
  const normalized = normalizeFileMode(mode);
  const ledger = readLedger();
  const ack = options?.confirmedAtMs ?? (normalized === 'full_access' ? null : undefined);
  if (normalized === 'full_access') {
    if (typeof ack !== 'number') {
      ledger[key] = { mode: DEFAULT_FILE_PERMISSION };
      writeLedger(ledger);
      return;
    }
    ledger[key] = { mode: normalized, confirmedAtMs: ack };
    writeLedger(ledger);
    return;
  }
  ledger[key] = { mode: normalized };
  writeLedger(ledger);
}

/** 读会话档位；没记录返回 null（由调用方回落全局默认） */
export function getConversationFilePermission(convId: string): FilePermissionMode | null {
  const key = String(convId ?? '').trim();
  if (!key) return null;
  const entry = readLedger()[key];
  if (!entry) return null;
  // 完全访问必须留过确认痕迹，否则视为未表态
  return filePermissionAfterConfirm(normalizeFileMode(entry.mode), entry.confirmedAtMs);
}

/** 会话归档或删除时清掉记录，避免留下无人引用的越权档位 */
export function dropConversationFilePermission(convId: string): void {
  const key = String(convId ?? '').trim();
  if (!key) return;
  const ledger = readLedger();
  if (!(key in ledger)) return;
  delete ledger[key];
  writeLedger(ledger);
}

/** 最终生效档位：会话选择 > 全局默认 > 最保守档 */
export function resolveFilePermission(
  convId: string | null | undefined,
  globalDefault?: unknown,
): FilePermissionMode {
  const stored = convId ? getConversationFilePermission(convId) : null;
  if (stored) return stored;
  const fallback = normalizeFileMode(globalDefault);
  return fallback;
}
