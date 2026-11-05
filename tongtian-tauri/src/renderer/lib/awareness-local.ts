/**
 * 意识通道 · 本地后端（Mock 通道权威实现）
 *
 * 桌面版（联调/生产）以 `~/.tongtianwork/awareness/main` 下的真实文件为事实来源
 * （Rust `awareness::*` 与引擎 `/api/awareness*` 都读写同一目录）。本模块只服务没有
 * 文件系统能力的通道（开发 Mock 模式 VITE_MOCK 未关闭、以及无 window 的测试环境），
 * 使设置页意识分区与长期记忆条目可自测。
 *
 * 与 Rust 同口径：同一份文件名白名单、同一套默认模板、同一条记忆行格式
 * （`- {id} · {source} · {createdAt} · {content}`）、同一组错误码。
 * 目录路径如实为 null —— 本地台账不落盘，不伪造一个用户机上不存在的路径。
 *
 * 数据保护：只写自己的 localStorage 键（`tt:awarenessProfile` / `tt:awarenessFiles`），
 * 删除仅限用户点名的那一条记忆条目。
 */

import type {
  AwarenessFileName,
  AwarenessPatch,
  AwarenessProfile,
  AwarenessStyleId,
  AwarenessView,
  MemoryCompactResult,
  MemoryDocSyncResult,
  MemoryEntityPage,
  MemoryEntry,
  MemoryExportResult,
  MemoryImportResult,
  MemoryKind,
  MemoryPackageExportResult,
  MemoryPackageImportResult,
  MemoryReviewAction,
  MemoryScope,
  MemoryStatus,
} from '../../shared/contracts';
import { AWARENESS_FILE_NAMES } from '../../shared/contracts';

/** 协作风格预设（与 Rust `awareness::STYLE_IDS`、设置页 SOUL.md 模板一致） */
export const AWARENESS_STYLES: AwarenessStyleId[] = ['efficient', 'deliberate', 'gentle', 'assertive'];
export const DEFAULT_STYLE: AwarenessStyleId = 'efficient';
/** 记忆条目字段分隔符（与 Rust `MEMORY_DELIM` 一致） */
export const MEMORY_DELIM = ' · ';
/** 未落盘/未编辑过的时间占位 */
export const DEFAULT_MARK = '系统默认';

/** 能力文件默认模板（与 Rust `default_content`、引擎 `AWARENESS_DEFAULTS` 逐字一致） */
export const AWARENESS_DEFAULTS: Record<AwarenessFileName, string> = {
  'SOUL.md':
    '# SOUL.md - 协作风格 - 高效极简\n\n用最少的话把事情做好。\n\n## 核心原则\n- **直接给结果。** 代码优于描述。能用一行说清的不用三段。\n- **不要复述问题。** 我知道我问了什么，直接回答。\n- **默认执行。** 方案明确时直接做，不用每步都确认。\n- **不确定时一句话确认。** 不要列出一堆选项让我选，给出你的判断，问我对不对。\n\n## 沟通风格\n- 回复尽可能简短\n- 跳过寒暄、过渡句和总结性废话\n- 代码注释只在逻辑不明显时添加\n- 用列表而非段落\n\n## 边界\n- 高风险操作前简短确认\n- 私密信息不外泄',
  'WORK_MANUAL.md':
    '# 工作手册\n\n## 工作规范\n- （在此补充你的规范）\n\n## 常用流程\n- （在此补充常用流程）',
  'USER_PROFILE.md': '# 用户画像\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- （待补充）\n\n## 习惯\n- （待补充）',
  'MEMORY.md': '# 长期记忆\n\n（暂无记忆条目，可在下方手动新增）',
};

const KEY_PROFILE = 'tt:awarenessProfile';
const KEY_FILES = 'tt:awarenessFiles';
/** 记忆元数据（归属/类型/引用次数）：行格式只有四字段，附加维度单独落本地台账 */
const KEY_META = 'tt:awarenessMemoryMeta';
/** Mock 通道的导出/导入备份（本地台账没有文件系统，路径如实标注为 localStorage） */
const KEY_EXPORT = 'tt:awarenessMemoryExport';
const KEY_BACKUP = 'tt:awarenessMemoryBackup';
const KEY_PACKAGE = 'tt:awarenessMemoryPackage';
export const LOCAL_EXPORT_PATH = 'localStorage://tt:awarenessMemoryExport';
export const LOCAL_BACKUP_PATH = 'localStorage://tt:awarenessMemoryBackup';
export const LOCAL_PACKAGE_PATH = 'localStorage://tt:awarenessMemoryPackage';

/** 迁移包格式（与 Rust `PACKAGE_FORMAT` / `PACKAGE_VERSION` 一致；Mock 存 JSON 而非 zip） */
export const MEMORY_PACKAGE_FORMAT = 'tongtian.hmpkg';
export const MEMORY_PACKAGE_VERSION = 1;

/** 导出格式（与 Rust `EXPORT_FORMAT` / `EXPORT_VERSION` 一致） */
export const MEMORY_EXPORT_FORMAT = 'tongtian.memories';
export const MEMORY_EXPORT_VERSION = 1;
/** 维护时每个归属保留的自动任务摘要条数（与 Rust `COMPACT_KEEP_PER_SCOPE` 一致） */
export const COMPACT_KEEP_PER_SCOPE = 200;
/** 复核动作（与 Rust `REVIEW_ACTIONS` 一致） */
export const MEMORY_REVIEW_ACTIONS: MemoryReviewAction[] = ['confirm', 'reject', 'archive', 'restore'];

/** MEMORY.md 行内可见的四字段（归属等维度不进文件，保存在元数据里） */
export type MemoryLineEntry = Pick<MemoryEntry, 'id' | 'content' | 'source' | 'createdAt'>;

interface MemoryMeta {
  scope: MemoryScope;
  workspaceId: string;
  kind: MemoryKind;
  updatedAt: string;
  lastUsedAt: string | null;
  useCount: number;
  occurrences: number;
  /** active / pending / archived；缺省视为 active（兼容二阶段之前写入的台账） */
  status?: MemoryStatus;
  reviewedAt?: string | null;
  supersededBy?: string | null;
  subject?: string;
  predicate?: string;
  object?: string;
  confidence?: number;
}

// ── 存储（localStorage 可用时持久，否则仅内存） ────────────────

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

// ── 校验（错误码与 Rust 完全一致） ────────────────────────────

export function ensureAwarenessFileName(name: unknown): AwarenessFileName {
  const raw = String(name ?? '').trim();
  if (!(AWARENESS_FILE_NAMES as string[]).includes(raw)) {
    throw new Error(
      `AWARENESS_FILE_INVALID: 意识文件「${raw}」不在可编辑清单内，仅支持 ${AWARENESS_FILE_NAMES.join(' / ')}`,
    );
  }
  return raw as AwarenessFileName;
}

export function ensureAwarenessStyle(style: unknown): AwarenessStyleId {
  const raw = String(style ?? '').trim();
  if (!(AWARENESS_STYLES as string[]).includes(raw)) {
    throw new Error(
      `AWARENESS_STYLE_INVALID: 协作风格「${raw}」非法，仅支持 ${AWARENESS_STYLES.join(' / ')}`,
    );
  }
  return raw as AwarenessStyleId;
}

// ── 台账状态 ─────────────────────────────────────────────────

type FileLedger = Partial<Record<AwarenessFileName, { content: string; updatedAt: string }>>;

let profile: AwarenessProfile = {
  mode: true,
  autoMemory: true,
  style: DEFAULT_STYLE,
  memoryReview: false,
  memoryModelExtract: false,
  updatedAt: '',
};
let files: FileLedger = {};
let memoryMeta: Record<string, MemoryMeta> = {};

function hydrate(): void {
  const stored = readJson<Partial<AwarenessProfile>>(KEY_PROFILE);
  if (stored) {
    profile = {
      mode: typeof stored.mode === 'boolean' ? stored.mode : String(stored.mode) === 'true',
      autoMemory:
        typeof stored.autoMemory === 'boolean' ? stored.autoMemory : String(stored.autoMemory) === 'true',
      memoryReview:
        typeof stored.memoryReview === 'boolean'
          ? stored.memoryReview
          : String(stored.memoryReview) === 'true',
      memoryModelExtract:
        typeof stored.memoryModelExtract === 'boolean'
          ? stored.memoryModelExtract
          : String(stored.memoryModelExtract) === 'true',
      style: (AWARENESS_STYLES as string[]).includes(String(stored.style))
        ? (stored.style as AwarenessStyleId)
        : DEFAULT_STYLE,
      updatedAt: String(stored.updatedAt ?? ''),
    };
  }
  const storedFiles = readJson<FileLedger>(KEY_FILES);
  if (storedFiles) {
    AWARENESS_FILE_NAMES.forEach((name) => {
      const entry = storedFiles[name];
      if (entry && typeof entry.content === 'string') {
        files[name] = { content: entry.content, updatedAt: String(entry.updatedAt ?? DEFAULT_MARK) };
      }
    });
  }
  const storedMeta = readJson<Record<string, MemoryMeta>>(KEY_META);
  if (storedMeta && typeof storedMeta === 'object') memoryMeta = storedMeta;
}

hydrate();

/** 测试用：复位内存台账并清空本地持久层（不触碰任何真实意识文件） */
export function resetLocalAwareness(): void {
  profile = {
    mode: true,
    autoMemory: true,
    style: DEFAULT_STYLE,
    memoryReview: false,
    memoryModelExtract: false,
    updatedAt: '',
  };
  files = {};
  memoryMeta = {};
  for (const key of [KEY_PROFILE, KEY_FILES, KEY_META, KEY_EXPORT, KEY_BACKUP, KEY_PACKAGE]) {
    try {
      storage()?.removeItem(key);
    } catch {
      /* 存储不可用时内存台账已复位，本次复位视为成功 */
    }
  }
}

// ── 记忆条目行格式（与 Rust render_line/parse_line 同规则） ────

export function renderMemoryLine(entry: MemoryLineEntry): string {
  return `- ${entry.id}${MEMORY_DELIM}${entry.source}${MEMORY_DELIM}${entry.createdAt}${MEMORY_DELIM}${entry.content}`;
}

/** 解析一行；非四字段结构（用户自己写的正文、默认占位段）返回 null 并原样保留 */
export function parseMemoryLine(line: string): MemoryLineEntry | null {
  const body = line.trim();
  if (!body.startsWith('- ')) return null;
  const s = body.slice(2);
  const parts: string[] = [];
  let rem = s;
  for (let i = 0; i < 3; i++) {
    const idx = rem.indexOf(MEMORY_DELIM);
    if (idx === -1) break;
    parts.push(rem.slice(0, idx));
    rem = rem.slice(idx + MEMORY_DELIM.length);
  }
  parts.push(rem);
  if (parts.length !== 4) return null;
  const [id, source, createdAt, content] = parts.map((p) => p.trim());
  if (!id || !content) return null;
  if (!['auto', 'manual', 'reflection'].includes(source)) return null;
  return { id, content, source: source as MemoryEntry['source'], createdAt };
}

/** 记忆正文归一：压掉换行与连续空格，保证一条一行 */
export function normalizeMemoryContent(content: string): string {
  return content.split(/\s+/).join(' ').trim();
}

/** id 生成：`mem-{毫秒}`，同毫秒冲突时追加序号（与 Rust generate_memory_id 一致） */
export function generateMemoryId(nowMs: number, existing: string[]): string {
  const base = `mem-${nowMs}`;
  if (!existing.includes(base)) return base;
  let n = 2;
  while (existing.includes(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
}

/** MEMORY.md 当前正文（默认模板 + 结构化行，与 Rust 侧磁盘内容同构） */
function memoryMarkdown(): string {
  return files['MEMORY.md']?.content ?? AWARENESS_DEFAULTS['MEMORY.md'];
}

function memoryRows(): MemoryLineEntry[] {
  return memoryMarkdown()
    .split('\n')
    .map(parseMemoryLine)
    .filter((e): e is MemoryLineEntry => e !== null);
}

/** 行内四字段 + 本地元数据 → 与桌面记忆库同形的条目 */
function decorate(entry: MemoryLineEntry): MemoryEntry {
  const meta = memoryMeta[entry.id];
  return {
    ...entry,
    scope: meta?.scope === 'workspace' ? 'workspace' : 'global',
    workspaceId: meta?.workspaceId ?? '',
    kind: meta?.kind ?? (entry.source === 'auto' ? 'task' : 'fact'),
    taskId: null,
    taskTitle: null,
    updatedAt: meta?.updatedAt ?? entry.createdAt,
    lastUsedAt: meta?.lastUsedAt ?? null,
    useCount: meta?.useCount ?? 0,
    occurrences: meta?.occurrences ?? 1,
    status: meta?.status === 'pending' || meta?.status === 'archived' ? meta.status : 'active',
    reviewedAt: meta?.reviewedAt ?? null,
    supersededBy: meta?.supersededBy ?? null,
    subject: meta?.subject ?? '',
    predicate: meta?.predicate ?? '',
    object: meta?.object ?? '',
    confidence: meta?.confidence ?? 1,
  };
}

/** 缺失元数据时按行内四字段补齐（二阶段之前写入的台账也能被复核/归档） */
function ensureMeta(entry: MemoryLineEntry): MemoryMeta {
  const existing = memoryMeta[entry.id];
  if (existing) return existing;
  const meta: MemoryMeta = {
    scope: 'global',
    workspaceId: '',
    kind: entry.source === 'auto' ? 'task' : 'fact',
    updatedAt: entry.createdAt,
    lastUsedAt: null,
    useCount: 0,
    occurrences: 1,
    status: 'active',
    reviewedAt: null,
    supersededBy: null,
    subject: '',
    predicate: '',
    object: '',
    confidence: 1,
  };
  memoryMeta[entry.id] = meta;
  return meta;
}

function sortMemories(rows: MemoryEntry[]): MemoryEntry[] {
  const stamp = (row: MemoryEntry) => row.updatedAt || row.createdAt;
  return [...rows].sort((a, b) =>
    stamp(a) === stamp(b) ? b.id.localeCompare(a.id) : stamp(b).localeCompare(stamp(a)),
  );
}

// ── 视图 / 更新 / 记忆条目 ───────────────────────────────────

/** `awareness:get` 的本地视图（dir 为 null：本地台账没有文件系统，不伪造路径） */
export function localAwarenessView(): AwarenessView {
  const out: AwarenessView = {
    ...profile,
    dir: null,
    files: {},
  };
  AWARENESS_FILE_NAMES.forEach((name) => {
    const entry = files[name];
    out.files[name] = entry
      ? { content: entry.content, updatedAt: entry.updatedAt }
      : { content: AWARENESS_DEFAULTS[name], updatedAt: DEFAULT_MARK };
  });
  return out;
}

/**
 * `awareness:update` 的本地实现：先整体校验再落账（非法文件名/风格不会留下半成品），
 * 正文按名覆盖写入 —— 与 Rust `awareness::update` 一致，不做隐式合并。
 */
export function updateLocalAwareness(patch: AwarenessPatch, nowMs: number = Date.now()): AwarenessView {
  const next: AwarenessProfile = { ...profile };
  if (typeof patch.mode === 'boolean') next.mode = patch.mode;
  else if (patch.mode != null) next.mode = String(patch.mode).trim() === 'true';
  if (typeof patch.autoMemory === 'boolean') next.autoMemory = patch.autoMemory;
  else if (patch.autoMemory != null) next.autoMemory = String(patch.autoMemory).trim() === 'true';
  if (typeof patch.memoryReview === 'boolean') next.memoryReview = patch.memoryReview;
  else if (patch.memoryReview != null) next.memoryReview = String(patch.memoryReview).trim() === 'true';
  if (typeof patch.memoryModelExtract === 'boolean') next.memoryModelExtract = patch.memoryModelExtract;
  else if (patch.memoryModelExtract != null)
    next.memoryModelExtract = String(patch.memoryModelExtract).trim() === 'true';
  if (patch.style != null) next.style = ensureAwarenessStyle(patch.style);

  const filePatch = patch.files ?? {};
  const names = Object.keys(filePatch) as AwarenessFileName[];
  names.forEach(ensureAwarenessFileName);

  const stamp = new Date(nowMs).toISOString();
  next.updatedAt = stamp;
  names.forEach((name) => {
    files[name] = { content: String(filePatch[name] ?? ''), updatedAt: stamp };
  });

  profile = next;
  writeJson(KEY_PROFILE, profile);
  writeJson(KEY_FILES, files);
  return localAwarenessView();
}

/** `awareness:memories` 的本地实现（新→旧） */
export function localMemories(): MemoryEntry[] {
  return sortMemories(memoryRows().map(decorate));
}

/** `awareness:memoryAdd` 的本地实现：只追加一条，正文与既有条目不动 */
export function addLocalMemory(
  content: unknown,
  source?: unknown,
  nowMs: number = Date.now(),
  scope?: unknown,
  workspaceId?: unknown,
): MemoryEntry {
  const text = normalizeMemoryContent(String(content ?? ''));
  if (!text) throw new Error('AWARENESS_MEMORY_EMPTY: 记忆条目内容不能为空');
  const raw = String(source ?? '').trim();
  const scopeRaw = String(scope ?? '').trim();
  const normalizedScope: MemoryScope = scopeRaw === 'workspace' ? 'workspace' : 'global';
  const ws = normalizedScope === 'workspace' ? String(workspaceId ?? '').trim() : '';
  if (normalizedScope === 'workspace' && !/^[a-z0-9_-]{1,64}$/.test(ws)) {
    throw new Error(
      'MEMORY_INVALID_SCOPE: 项目记忆必须带合法工作区 id（小写字母/数字/-/_，64 字符以内）',
    );
  }
  const line: MemoryLineEntry = {
    id: generateMemoryId(nowMs, memoryRows().map((e) => e.id)),
    content: text,
    source: (['auto', 'reflection'] as string[]).includes(raw)
      ? (raw as MemoryEntry['source'])
      : 'manual',
    createdAt: new Date(nowMs).toISOString(),
  };
  const stamp = line.createdAt;
  files['MEMORY.md'] = {
    content: `${memoryMarkdown().replace(/\n+$/, '')}\n${renderMemoryLine(line)}\n`,
    updatedAt: stamp,
  };
  memoryMeta[line.id] = {
    scope: normalizedScope,
    workspaceId: ws,
    kind: line.source === 'auto' ? 'task' : 'fact',
    updatedAt: stamp,
    lastUsedAt: null,
    useCount: 0,
    occurrences: 1,
    // 手动录入直接生效（待审只针对自动候选，与 Rust memory_add 同口径）
    status: 'active',
    reviewedAt: null,
    supersededBy: null,
    subject: '',
    predicate: '',
    object: '',
    confidence: 1,
  };
  profile = { ...profile, updatedAt: stamp };
  writeJson(KEY_PROFILE, profile);
  writeJson(KEY_FILES, files);
  writeJson(KEY_META, memoryMeta);
  return decorate(line);
}

/** `awareness:memoryDelete` 的本地实现：只删点名的一行，未命中如实报错 */
export function deleteLocalMemory(id: unknown): true {
  const target = String(id ?? '').trim();
  const rows = memoryRows();
  if (!target || !rows.some((e) => e.id === target)) {
    throw new Error(`AWARENESS_MEMORY_NOT_FOUND: 记忆条目「${target}」已不存在，可能已被删除`);
  }
  const kept = memoryMarkdown()
    .replace(/\n+$/, '')
    .split('\n')
    .filter((line) => {
      const parsed = parseMemoryLine(line);
      return !(parsed && parsed.id === target);
    })
    .join('\n');
  files['MEMORY.md'] = { content: `${kept}\n`, updatedAt: new Date().toISOString() };
  delete memoryMeta[target];
  writeJson(KEY_FILES, files);
  writeJson(KEY_META, memoryMeta);
  return true;
}

// ── 复核 / 实体页 / 导出导入 / 维护（D-35 二期，口径与 Rust 对齐） ──

/** 软退休：同主体同属性的旧取值归档，指向新事实（与 Rust `resolve_conflicts` 同口径） */
function retireConflicts(keep: MemoryEntry, stamp: string): void {
  const subject = (keep.subject ?? '').trim().toLowerCase();
  const predicate = (keep.predicate ?? '').trim().toLowerCase();
  if (!subject || !predicate) return;
  const object = (keep.object ?? '').trim().toLowerCase();
  memoryRows().forEach((line) => {
    if (line.id === keep.id) return;
    const meta = ensureMeta(line);
    if (meta.status !== 'active') return;
    if ((meta.scope ?? 'global') !== keep.scope) return;
    if ((meta.workspaceId ?? '') !== keep.workspaceId) return;
    if ((meta.subject ?? '').trim().toLowerCase() !== subject) return;
    if ((meta.predicate ?? '').trim().toLowerCase() !== predicate) return;
    if ((meta.object ?? '').trim().toLowerCase() === object) return;
    memoryMeta[line.id] = {
      ...meta,
      status: 'archived',
      reviewedAt: stamp,
      updatedAt: stamp,
      supersededBy: keep.id,
    };
  });
}

/** `awareness:memoryReview` 的本地实现：只改状态，正文与证据保留 */
export function reviewLocalMemory(
  id: unknown,
  action: unknown,
  nowMs: number = Date.now(),
): MemoryEntry {
  const target = String(id ?? '').trim();
  if (!target) throw new Error('MEMORY_NOT_FOUND: 记忆条目标识不能为空');
  const act = String(action ?? '').trim();
  if (!(MEMORY_REVIEW_ACTIONS as string[]).includes(act)) {
    throw new Error(
      `MEMORY_STATUS_INVALID: 复核动作「${act}」非法，仅支持 ${MEMORY_REVIEW_ACTIONS.join(' / ')}`,
    );
  }
  const line = memoryRows().find((e) => e.id === target);
  if (!line) throw new Error(`MEMORY_NOT_FOUND: 记忆条目「${target}」已不存在，可能已被删除`);
  const meta = ensureMeta(line);
  const current: MemoryStatus = meta.status ?? 'active';
  if (meta.supersededBy && (act === 'confirm' || act === 'restore')) {
    throw new Error(
      'MEMORY_SUPERSEDED: 这条记忆已被更新的同类事实取代，不能直接恢复；如需保留请新增一条并确认',
    );
  }
  let next: MemoryStatus;
  if (act === 'confirm') {
    if (current !== 'pending' && current !== 'archived') {
      throw new Error(`MEMORY_STATUS_INVALID: 只有待审或已归档的记忆可确认（当前 ${current}）`);
    }
    next = 'active';
  } else if (act === 'reject') {
    if (current !== 'pending') {
      throw new Error(`MEMORY_STATUS_INVALID: 只有待审记忆可驳回（当前 ${current}）`);
    }
    next = 'archived';
  } else if (act === 'archive') {
    if (current === 'archived') {
      throw new Error('MEMORY_STATUS_INVALID: 这条记忆已经是归档状态');
    }
    next = 'archived';
  } else {
    if (current !== 'archived') {
      throw new Error(`MEMORY_STATUS_INVALID: 只有已归档记忆可恢复（当前 ${current}）`);
    }
    next = 'active';
  }
  const stamp = new Date(nowMs).toISOString();
  memoryMeta[target] = {
    ...meta,
    status: next,
    reviewedAt: stamp,
    updatedAt: stamp,
    supersededBy: next === 'active' ? null : meta.supersededBy ?? null,
  };
  if (next === 'active') retireConflicts(decorate(line), stamp);
  writeJson(KEY_META, memoryMeta);
  return decorate(line);
}

/** `awareness:memoryEntities` 的本地实现：按主体聚合当前项目可见的活跃事实 */
export function localMemoryEntities(workspaceId?: unknown): MemoryEntityPage[] {
  const ws = String(workspaceId ?? '').trim() || 'default';
  const groups = new Map<string, MemoryEntry[]>();
  localMemories().forEach((row) => {
    if ((row.status ?? 'active') !== 'active') return;
    if (!(row.scope === 'global' || row.workspaceId === ws)) return;
    const subject = (row.subject ?? '').trim();
    if (!subject) return;
    const bucket = groups.get(subject) ?? [];
    bucket.push(row);
    groups.set(subject, bucket);
  });
  const pages: MemoryEntityPage[] = [];
  groups.forEach((entries, subject) => {
    const sorted = sortMemories(entries);
    const latest = sorted[0]?.updatedAt ?? '';
    const kinds = Array.from(new Set(sorted.map((r) => r.kind))).sort();
    pages.push({
      subject,
      count: sorted.length,
      kinds: kinds as MemoryKind[],
      latest,
      entries: sorted.slice(0, 5),
    });
  });
  return pages.sort((a, b) =>
    a.count === b.count ? b.latest.localeCompare(a.latest) : b.count - a.count,
  );
}

interface ExportEnvelope {
  format: string;
  version: number;
  exportedAt: string;
  count: number;
  entries: MemoryEntry[];
}

function exportEnvelope(nowMs: number): ExportEnvelope {
  const entries = localMemories();
  return {
    format: MEMORY_EXPORT_FORMAT,
    version: MEMORY_EXPORT_VERSION,
    exportedAt: new Date(nowMs).toISOString(),
    count: entries.length,
    entries,
  };
}

/** `awareness:memoryExport` 的本地实现：本地台账没有文件系统，导出写入 localStorage */
export function exportLocalMemories(nowMs: number = Date.now()): MemoryExportResult {
  const payload = exportEnvelope(nowMs);
  writeJson(KEY_EXPORT, payload);
  return { path: LOCAL_EXPORT_PATH, count: payload.count };
}

/** 导入前备份当前台账；写入失败时至少能拿回备份 */
function backupLocalMemories(nowMs: number): string {
  writeJson(KEY_BACKUP, exportEnvelope(nowMs));
  return LOCAL_BACKUP_PATH;
}

/** 追加一条导入条目：保留原 id/时间/状态与结构化字段，供 `memories` 列表直接可见 */
function appendImportedEntry(entry: Partial<MemoryEntry>, nowMs: number): MemoryLineEntry {
  const content = normalizeMemoryContent(String(entry.content ?? ''));
  if (!content) throw new Error('AWARENESS_MEMORY_EMPTY: 记忆条目内容不能为空');
  const id = String(entry.id ?? '').trim() || generateMemoryId(nowMs, memoryRows().map((e) => e.id));
  const sourceRaw = String(entry.source ?? '').trim();
  const source = (['auto', 'reflection'] as string[]).includes(sourceRaw)
    ? (sourceRaw as MemoryEntry['source'])
    : 'manual';
  const createdAt = String(entry.createdAt ?? '').trim() || new Date(nowMs).toISOString();
  const line: MemoryLineEntry = { id, content, source, createdAt };
  files['MEMORY.md'] = {
    content: `${memoryMarkdown().replace(/\n+$/, '')}\n${renderMemoryLine(line)}\n`,
    updatedAt: new Date(nowMs).toISOString(),
  };
  const rawScope = String(entry.scope ?? '').trim();
  memoryMeta[id] = {
    scope: rawScope === 'workspace' ? 'workspace' : 'global',
    workspaceId: rawScope === 'workspace' ? String(entry.workspaceId ?? '').trim() : '',
    kind: (['task', 'fact', 'preference', 'decision'] as string[]).includes(String(entry.kind))
      ? (entry.kind as MemoryKind)
      : source === 'auto'
        ? 'task'
        : 'fact',
    updatedAt: String(entry.updatedAt ?? createdAt),
    lastUsedAt: entry.lastUsedAt ?? null,
    useCount: Number(entry.useCount ?? 0) || 0,
    occurrences: Number(entry.occurrences ?? 1) || 1,
    status: entry.status === 'pending' || entry.status === 'archived' ? entry.status : 'active',
    reviewedAt: entry.reviewedAt ?? null,
    supersededBy: entry.supersededBy ?? null,
    subject: String(entry.subject ?? '').trim(),
    predicate: String(entry.predicate ?? '').trim(),
    object: String(entry.object ?? '').trim(),
    confidence: typeof entry.confidence === 'number' ? entry.confidence : 1,
  };
  return line;
}

/** `awareness:memoryImport` 的本地实现：格式/版本校验 + 按内容去重 + 导入前备份 */
export function importLocalMemories(json: unknown, nowMs: number = Date.now()): MemoryImportResult {
  const raw = String(json ?? '').trim();
  if (!raw) throw new Error('MEMORY_IMPORT_INVALID: 导入内容为空');
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new Error(`MEMORY_IMPORT_INVALID: 不是合法 JSON：${(e as Error).message}`);
  }
  let entries: Partial<MemoryEntry>[];
  if (Array.isArray(parsed)) {
    entries = parsed as Partial<MemoryEntry>[];
  } else {
    const obj = (parsed ?? {}) as Record<string, unknown>;
    const format = String(obj.format ?? '').trim();
    if (format && format !== MEMORY_EXPORT_FORMAT) {
      throw new Error(`MEMORY_IMPORT_INVALID: 不是通天记忆导出文件（format=${format}）`);
    }
    const version = Number(obj.version ?? 0) || 0;
    if (version > MEMORY_EXPORT_VERSION) {
      throw new Error(
        `MEMORY_IMPORT_INVALID: 导出文件版本 ${version} 高于当前支持的 ${MEMORY_EXPORT_VERSION}，请先升级应用`,
      );
    }
    if (!Array.isArray(obj.entries)) {
      throw new Error('MEMORY_IMPORT_INVALID: 文件里没有 entries 数组');
    }
    entries = obj.entries as Partial<MemoryEntry>[];
  }
  if (entries.length > 10000) {
    throw new Error('MEMORY_IMPORT_INVALID: 单次最多导入 10000 条记忆');
  }
  const backupPath = backupLocalMemories(nowMs);
  const seen = new Set(
    localMemories().map(
      (row) => `${row.scope}|${row.workspaceId}|${normalizeMemoryContent(row.content).toLowerCase()}`,
    ),
  );
  let inserted = 0;
  let merged = 0;
  let skipped = 0;
  entries.forEach((entry) => {
    const content = normalizeMemoryContent(String(entry.content ?? ''));
    if (!content) {
      skipped += 1;
      return;
    }
    const scope = String(entry.scope ?? '').trim() === 'workspace' ? 'workspace' : 'global';
    const workspaceId = scope === 'workspace' ? String(entry.workspaceId ?? '').trim() : '';
    const key = `${scope}|${workspaceId}|${content.toLowerCase()}`;
    if (seen.has(key)) {
      // 重复条目累计出现次数，不制造第二行（与 Rust add 的合并口径一致）
      const existing = memoryRows().find(
        (line) => normalizeMemoryContent(line.content).toLowerCase() === content.toLowerCase(),
      );
      if (existing) {
        const meta = ensureMeta(existing);
        memoryMeta[existing.id] = {
          ...meta,
          occurrences: (meta.occurrences ?? 1) + 1,
          updatedAt: new Date(nowMs).toISOString(),
        };
      }
      merged += 1;
      return;
    }
    try {
      appendImportedEntry({ ...entry, scope, workspaceId }, nowMs);
      seen.add(key);
      inserted += 1;
    } catch {
      skipped += 1;
    }
  });
  profile = { ...profile, updatedAt: new Date(nowMs).toISOString() };
  writeJson(KEY_PROFILE, profile);
  writeJson(KEY_FILES, files);
  writeJson(KEY_META, memoryMeta);
  return { inserted, merged, skipped, total: entries.length, backupPath };
}

/** `awareness:memoryCompact` 的本地实现：每个归属只保留最近的自动任务摘要，其余软归档 */
export function compactLocalMemories(nowMs: number = Date.now()): MemoryCompactResult {
  const groups = new Map<string, MemoryEntry[]>();
  localMemories().forEach((row) => {
    if ((row.status ?? 'active') !== 'active') return;
    if (row.kind !== 'task' || row.source !== 'auto') return;
    const key = `${row.scope}|${row.workspaceId}`;
    const bucket = groups.get(key) ?? [];
    bucket.push(row);
    groups.set(key, bucket);
  });
  const stamp = new Date(nowMs).toISOString();
  let archived = 0;
  groups.forEach((rows) => {
    sortMemories(rows)
      .slice(COMPACT_KEEP_PER_SCOPE)
      .forEach((row) => {
        const meta = ensureMeta({ id: row.id, content: row.content, source: row.source, createdAt: row.createdAt });
        memoryMeta[row.id] = { ...meta, status: 'archived', reviewedAt: stamp, updatedAt: stamp };
        archived += 1;
      });
  });
  if (archived) writeJson(KEY_META, memoryMeta);
  return { archived, keepPerScope: COMPACT_KEEP_PER_SCOPE };
}

/**
 * `awareness:memoryDocSync` 的本地实现。
 * Mock 通道的记忆台账本来就以 MEMORY.md 行作为事实来源，因此不存在“文件有、库没有”；
 * 如实返回文件条数与已存在条数，不伪造导入数量。
 */
export function syncLocalMemoryDoc(): MemoryDocSyncResult {
  const count = memoryRows().length;
  return { fileCount: count, imported: 0, existing: count, skipped: 0 };
}

/** `awareness:memoryExportPackage` 的本地实现：JSON 代入包，路径如实标注为 localStorage */
export function exportLocalMemoryPackage(nowMs: number = Date.now()): MemoryPackageExportResult {
  const entries = localMemories();
  writeJson(KEY_PACKAGE, {
    format: MEMORY_PACKAGE_FORMAT,
    version: MEMORY_PACKAGE_VERSION,
    createdAt: new Date(nowMs).toISOString(),
    count: entries.length,
    entries,
  });
  return { path: LOCAL_PACKAGE_PATH, count: entries.length };
}

function decodeBase64Text(data: unknown): string {
  const raw = String(data ?? '').trim();
  if (!raw) throw new Error('MEMORY_IMPORT_INVALID: 导入内容为空');
  try {
    const binary = atob(raw);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch (e) {
    throw new Error(`MEMORY_IMPORT_INVALID: 迁移包不是合法 base64：${(e as Error).message}`);
  }
}

/** `awareness:memoryImportPackage` 的本地实现：解 base64 → 校验包格式 → 复用 JSON 导入 */
export function importLocalMemoryPackage(
  data: unknown,
  nowMs: number = Date.now(),
): MemoryPackageImportResult {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(decodeBase64Text(data)) as Record<string, unknown>;
  } catch (e) {
    if ((e as Error).message.startsWith('MEMORY_IMPORT_INVALID')) throw e;
    throw new Error(`MEMORY_IMPORT_INVALID: 迁移包不是合法 JSON：${(e as Error).message}`);
  }
  const format = String(parsed.format ?? '').trim();
  if (format && format !== MEMORY_PACKAGE_FORMAT && format !== MEMORY_EXPORT_FORMAT) {
    throw new Error(`MEMORY_IMPORT_INVALID: 不是通天记忆迁移包（format=${format}）`);
  }
  const version = Number(parsed.version ?? 0) || 0;
  if (version > MEMORY_PACKAGE_VERSION) {
    throw new Error(
      `MEMORY_IMPORT_INVALID: 迁移包版本 ${version} 高于当前支持的 ${MEMORY_PACKAGE_VERSION}，请先升级应用`,
    );
  }
  const entries = Array.isArray(parsed.entries) ? parsed.entries : [];
  const result = importLocalMemories(
    JSON.stringify({
      format: MEMORY_EXPORT_FORMAT,
      version: MEMORY_EXPORT_VERSION,
      entries,
    }),
    nowMs,
  );
  return {
    ...result,
    packageVersion: version,
    exportedAt: typeof parsed.createdAt === 'string' ? parsed.createdAt : null,
  };
}

/** 把意识通道落成的 ISO 时间转成本地可读串（非 ISO 的历史文本原样返回） */
export function fmtAwarenessUpdatedAt(value?: string | null): string {  if (!value) return DEFAULT_MARK;
  const t = Date.parse(value);
  if (Number.isNaN(t)) return value;
  return new Date(t).toLocaleString('zh-CN', { hour12: false });
}

// ── 通道兜底（Mock 通道与无 window 环境使用） ─────────────────

export interface LocalAwarenessResult {
  handled: boolean;
  value?: unknown;
}

/** 意识通道本地实现：返回 `{handled:false}` 时交由上层通道继续处理 */
export function handleLocalAwarenessChannel(
  channel: string,
  args: unknown[],
): LocalAwarenessResult {
  const payload = (args[0] ?? {}) as Record<string, unknown>;
  switch (channel) {
    case 'awareness:get':
      return { handled: true, value: localAwarenessView() };
    case 'awareness:update':
      return { handled: true, value: updateLocalAwareness(payload as AwarenessPatch) };
    case 'awareness:memories':
      return { handled: true, value: localMemories() };
    case 'awareness:memoryAdd':
      return {
        handled: true,
        value: addLocalMemory(
          payload.content,
          payload.source,
          Date.now(),
          payload.scope,
          payload.workspaceId,
        ),
      };
    case 'awareness:memoryDelete':
      return {
        handled: true,
        value: { ok: deleteLocalMemory(payload.id), id: String(payload.id ?? '').trim() },
      };
    case 'awareness:memoryReview':
      return {
        handled: true,
        value: reviewLocalMemory(payload.id, payload.action),
      };
    case 'awareness:memoryEntities':
      return { handled: true, value: localMemoryEntities(payload.workspaceId) };
    case 'awareness:memoryExport':
      return { handled: true, value: exportLocalMemories() };
    case 'awareness:memoryImport':
      return { handled: true, value: importLocalMemories(payload.json) };
    case 'awareness:memoryCompact':
      return { handled: true, value: compactLocalMemories() };
    case 'awareness:memoryDocSync':
      return { handled: true, value: syncLocalMemoryDoc() };
    case 'awareness:memoryExportPackage':
      return { handled: true, value: exportLocalMemoryPackage() };
    case 'awareness:memoryImportPackage':
      return { handled: true, value: importLocalMemoryPackage(payload.data) };
    default:
      return { handled: false };
  }
}
