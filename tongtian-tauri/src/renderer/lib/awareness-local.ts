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
  MemoryEntry,
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
  'USER_PROFILE.md': '# 用户画像\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- （自动维护中）\n\n## 习惯\n- （自动维护中）',
  'MEMORY.md': '# 长期记忆\n\n（暂无记忆条目，对话中值得记住的事实将自动沉淀于此）',
};

const KEY_PROFILE = 'tt:awarenessProfile';
const KEY_FILES = 'tt:awarenessFiles';

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
  updatedAt: '',
};
let files: FileLedger = {};

function hydrate(): void {
  const stored = readJson<Partial<AwarenessProfile>>(KEY_PROFILE);
  if (stored) {
    profile = {
      mode: typeof stored.mode === 'boolean' ? stored.mode : String(stored.mode) === 'true',
      autoMemory:
        typeof stored.autoMemory === 'boolean' ? stored.autoMemory : String(stored.autoMemory) === 'true',
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
}

hydrate();

/** 测试用：复位内存台账并清空本地持久层（不触碰任何真实意识文件） */
export function resetLocalAwareness(): void {
  profile = { mode: true, autoMemory: true, style: DEFAULT_STYLE, updatedAt: '' };
  files = {};
  for (const key of [KEY_PROFILE, KEY_FILES]) {
    try {
      storage()?.removeItem(key);
    } catch {
      /* 存储不可用时内存台账已复位，本次复位视为成功 */
    }
  }
}

// ── 记忆条目行格式（与 Rust render_line/parse_line 同规则） ────

export function renderMemoryLine(entry: MemoryEntry): string {
  return `- ${entry.id}${MEMORY_DELIM}${entry.source}${MEMORY_DELIM}${entry.createdAt}${MEMORY_DELIM}${entry.content}`;
}

/** 解析一行；非四字段结构（用户自己写的正文、默认占位段）返回 null 并原样保留 */
export function parseMemoryLine(line: string): MemoryEntry | null {
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

function memoryRows(): MemoryEntry[] {
  return memoryMarkdown()
    .split('\n')
    .map(parseMemoryLine)
    .filter((e): e is MemoryEntry => e !== null);
}

function sortMemories(rows: MemoryEntry[]): MemoryEntry[] {
  return [...rows].sort((a, b) => (a.createdAt === b.createdAt ? b.id.localeCompare(a.id) : b.createdAt.localeCompare(a.createdAt)));
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
  return sortMemories(memoryRows());
}

/** `awareness:memoryAdd` 的本地实现：只追加一条，正文与既有条目不动 */
export function addLocalMemory(
  content: unknown,
  source?: unknown,
  nowMs: number = Date.now(),
): MemoryEntry {
  const text = normalizeMemoryContent(String(content ?? ''));
  if (!text) throw new Error('AWARENESS_MEMORY_EMPTY: 记忆条目内容不能为空');
  const raw = String(source ?? '').trim();
  const entry: MemoryEntry = {
    id: generateMemoryId(nowMs, memoryRows().map((e) => e.id)),
    content: text,
    source: (['auto', 'reflection'] as string[]).includes(raw)
      ? (raw as MemoryEntry['source'])
      : 'manual',
    createdAt: new Date(nowMs).toISOString(),
  };
  const stamp = entry.createdAt;
  files['MEMORY.md'] = {
    content: `${memoryMarkdown().replace(/\n+$/, '')}\n${renderMemoryLine(entry)}\n`,
    updatedAt: stamp,
  };
  profile = { ...profile, updatedAt: stamp };
  writeJson(KEY_PROFILE, profile);
  writeJson(KEY_FILES, files);
  return entry;
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
  writeJson(KEY_FILES, files);
  return true;
}

/** 把意识通道落成的 ISO 时间转成本地可读串（非 ISO 的历史文本原样返回） */
export function fmtAwarenessUpdatedAt(value?: string | null): string {
  if (!value) return DEFAULT_MARK;
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
        value: addLocalMemory(payload.content, payload.source),
      };
    case 'awareness:memoryDelete':
      return {
        handled: true,
        value: { ok: deleteLocalMemory(payload.id), id: String(payload.id ?? '').trim() },
      };
    default:
      return { handled: false };
  }
}
