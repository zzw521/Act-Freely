/**
 * 会话正文存取（工作台「刷新/重启不丢对话」的读写层）
 *
 * 三条通道的权威数据源：
 * - 桌面应用：Rust command `chat_conversations` / `chat_messages`（SQLite `chat_messages` 表，
 *   提交任务时落用户提问、deliver 事件落助手正文，不设 TTL）
 * - 浏览器直连引擎 / 开发 Mock：本模块的 localStorage 台账（`tt:chatMessages`），
 *   与 Rust 同一形状、同一口径（会话键 + 轮次号 + 角色），刷新不丢数据
 *
 * 过程明细（计划/步骤）不在这里存：它来自事件流，只留存 24h（`bridge::event_store`），
 * 超过窗口的会话恢复出「问答正文 + 无过程明细」，此处不做任何补齐。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type { ChatMessageRow, ConversationRow } from '../../shared/contracts';
import type { ChatEvent, Message } from '../../shared/types';
import { Role } from '../../shared/types';

/** 本地台账键 */
const LEDGER_KEY = 'tt:chatMessages';
/** 台账保留行数上限（超出按最旧丢弃，避免 localStorage 无限增长） */
const PERSIST_ROW_LIMIT = 400;
/** 单条正文保留字符数（超出截断，防止超长回复挤爆本地存储配额） */
const CONTENT_LIMIT = 20000;

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function getAPI(): { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } })
    .tongtianAPI;
}

/** 桌面容器内 SQLite 才是正文权威（Rust 侧随任务流写入，渲染层不再另存一份影子台账） */
function isDesktopRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

// ── 本地台账（非桌面通道） ────────────────────────────────────

function readLedger(): ChatMessageRow[] {
  const s = storage();
  if (!s) return [];
  try {
    const raw = s.getItem(LEDGER_KEY);
    const rows = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(rows) ? rows.filter(isValidRow) : [];
  } catch {
    return [];
  }
}

function isValidRow(row: unknown): row is ChatMessageRow {
  const r = (row ?? {}) as Partial<ChatMessageRow>;
  return (
    typeof r.convId === 'string' &&
    r.convId.length > 0 &&
    typeof r.turnIndex === 'number' &&
    (r.role === 'user' || r.role === 'assistant') &&
    typeof r.content === 'string'
  );
}

function clipContent(content: string): string {
  return content.length > CONTENT_LIMIT ? content.slice(0, CONTENT_LIMIT) : content;
}

function writeLedger(rows: ChatMessageRow[]): void {
  const s = storage();
  if (!s) return;
  const trimmed = rows.length > PERSIST_ROW_LIMIT ? rows.slice(rows.length - PERSIST_ROW_LIMIT) : rows;
  try {
    s.setItem(LEDGER_KEY, JSON.stringify(trimmed));
  } catch {
    // 配额不足：先丢最旧的一半再试一次，仍失败则本次只保留内存态（不静默改写已有数据语义）
    try {
      s.setItem(LEDGER_KEY, JSON.stringify(trimmed.slice(Math.floor(trimmed.length / 2))));
    } catch {
      /* 忽略：本地台账退化为本轮内存态 */
    }
  }
}

let cache: ChatMessageRow[] | null = null;

function ledgerRows(): ChatMessageRow[] {
  if (!cache) cache = readLedger();
  return cache;
}

/**
 * 记录一条正文（同会话同轮同角色覆盖，与 Rust 主键语义一致）。
 * 桌面运行时由 Rust 落库，这里直接返回，避免两套正文各写一份。
 */
export function recordConversationMessage(row: ChatMessageRow): void {
  if (isDesktopRuntime()) return;
  const content = clipContent(row.content);
  if (!row.convId || !content.trim()) return;
  const rows = ledgerRows();
  const idx = rows.findIndex(
    (r) => r.convId === row.convId && r.turnIndex === row.turnIndex && r.role === row.role
  );
  const next: ChatMessageRow = {
    convId: row.convId,
    turnIndex: row.turnIndex,
    role: row.role,
    taskId: row.taskId,
    content,
    artifacts: row.role === 'assistant' ? [...(row.artifacts ?? [])] : [],
    tsMs: row.tsMs,
  };
  if (idx >= 0) {
    // 评价按 (会话, 轮次) 归属，覆盖正文时保留本轮已有评价（与 Rust chat_feedback 表同一语义）
    if (row.role === 'assistant' && rows[idx].feedback) next.feedback = rows[idx].feedback;
    rows[idx] = next;
  } else rows.push(next);
  rows.sort((a, b) => a.tsMs - b.tsMs);
  cache = rows;
  writeLedger(rows);
}

/** 本地会话聚合（与 Rust `list_conversations` 同口径：标题取首轮提问、按最近活跃倒序） */
export function localConversations(limit = 50): ConversationRow[] {
  const byConv = new Map<string, ChatMessageRow[]>();
  for (const row of ledgerRows()) {
    const group = byConv.get(row.convId);
    if (group) group.push(row);
    else byConv.set(row.convId, [row]);
  }
  const out: ConversationRow[] = [];
  for (const [convId, rows] of byConv) {
    const users = rows.filter((r) => r.role === 'user').sort((a, b) => a.turnIndex - b.turnIndex);
    const first = users[0];
    const turns = new Set(rows.map((r) => r.turnIndex));
    const lastByTurn = [...rows].sort(
      (a, b) => b.turnIndex - a.turnIndex || String(a.role).localeCompare(String(b.role))
    )[0];
    out.push({
      convId,
      firstGoal: (first?.content ?? '').slice(0, 60),
      turnCount: turns.size,
      createdAtMs: Math.min(...rows.map((r) => r.tsMs)),
      updatedAtMs: Math.max(...rows.map((r) => r.tsMs)),
      lastTaskId: lastByTurn?.taskId ?? '',
    });
  }
  return out.sort((a, b) => b.updatedAtMs - a.updatedAtMs).slice(0, Math.max(0, limit));
}

/** 本地按会话读取正文（轮次升序，同轮 user 先于 assistant） */
export function localMessages(convId: string, limit = 200): ChatMessageRow[] {
  return ledgerRows()
    .filter((r) => r.convId === convId)
    .sort((a, b) => a.turnIndex - b.turnIndex || (a.role === 'user' ? -1 : 1) - (b.role === 'user' ? -1 : 1))
    .slice(0, Math.max(0, limit));
}

/** 本地台账写入一轮回复的评价（kind 为 null 即取消；评价随轮次，不新增正文行） */
export function setLocalFeedback(convId: string, turnIndex: number, kind: 'up' | 'down' | null): void {
  const rows = ledgerRows();
  const idx = rows.findIndex((r) => r.convId === convId && r.turnIndex === turnIndex && r.role === 'assistant');
  if (idx < 0) {
    throw new Error(`CHAT_FEEDBACK_TURN_MISSING: 会话 ${convId} 第 ${turnIndex} 轮没有回复正文，无法评价`);
  }
  if (kind) rows[idx] = { ...rows[idx], feedback: kind };
  else delete rows[idx].feedback;
  cache = rows;
  writeLedger(rows);
}

/**
 * 写入一轮回复的评价（三通道同一入口）：
 * 桌面端以 Rust `chat_feedback`（SQLite）为权威并把失败暴露给调用方，
 * 其余通道写本地台账 —— 不回退，避免同一次评价落出两份分歧数据。
 */
export async function saveFeedback(
  convId: string,
  turnIndex: number,
  kind: 'up' | 'down' | null
): Promise<void> {
  if (!convId) throw new Error('CHAT_INVALID_CONVERSATION: 会话标识不能为空');
  if (isDesktopRuntime()) {
    const api = getAPI();
    if (!api) throw new Error('CHAT_FEEDBACK_UNAVAILABLE: tongtianAPI 未注入，评价无法落库');
    await api.invoke(IPC_CHANNELS.CHAT_FEEDBACK, convId, turnIndex, kind);
    return;
  }
  setLocalFeedback(convId, turnIndex, kind);
}

/** 清空本地台账（测试用；只动本地 Mock 台账，不触碰 Rust 库内数据） */
export function resetConversationLedger(): void {
  cache = [];
  const s = storage();
  if (s) {
    try {
      s.removeItem(LEDGER_KEY);
    } catch {
      /* ignore */
    }
  }
}

// ── 三通道统一读取 ────────────────────────────────────────────

/** 兜底解析正文行：字段缺失/类型漂移一律丢弃，不组装半条假消息 */
export function toMessageRows(resp: unknown): ChatMessageRow[] {
  if (!Array.isArray(resp)) return [];
  const out: ChatMessageRow[] = [];
  for (const item of resp) {
    const r = (item ?? {}) as Record<string, unknown>;
    const role = r.role === 'user' ? 'user' : r.role === 'assistant' ? 'assistant' : null;
    // 正文类型漂移（非字符串）直接丢整行：不拿空串冒充一条消息
    if (!role || typeof r.convId !== 'string' || !r.convId || typeof r.content !== 'string') continue;
    out.push({
      convId: r.convId,
      turnIndex: Number(r.turnIndex ?? 0) || 0,
      role,
      taskId: String(r.taskId ?? ''),
      content: r.content,
      artifacts: Array.isArray(r.artifacts) ? (r.artifacts as unknown[]).map((a) => String(a)) : [],
      tsMs: Number(r.tsMs ?? 0) || 0,
      // 评价只认 up/down（Rust 未评价时返回空串），其余值一律按未评价处理
      ...(r.feedback === 'up' || r.feedback === 'down' ? { feedback: r.feedback } : {}),
    });
  }
  return out.filter(isValidRow);
}

export function toConversationRows(resp: unknown): ConversationRow[] {
  if (!Array.isArray(resp)) return [];
  const out: ConversationRow[] = [];
  for (const item of resp) {
    const r = (item ?? {}) as Record<string, unknown>;
    if (typeof r.convId !== 'string' || !r.convId) continue;
    out.push({
      convId: r.convId,
      firstGoal: String(r.firstGoal ?? ''),
      turnCount: Number(r.turnCount ?? 0) || 0,
      createdAtMs: Number(r.createdAtMs ?? 0) || 0,
      updatedAtMs: Number(r.updatedAtMs ?? 0) || 0,
      lastTaskId: String(r.lastTaskId ?? ''),
    });
  }
  return out;
}

/** 会话列表：桌面走 Rust，其余走本地台账 */
export async function fetchConversations(limit = 50): Promise<ConversationRow[]> {
  const api = getAPI();
  if (api && isDesktopRuntime()) {
    try {
      const rows = toConversationRows(await api.invoke(IPC_CHANNELS.CHAT_CONVERSATIONS, limit));
      if (rows.length) return rows;
    } catch {
      // Rust 通道不可用时回退本地台账，恢复路径不阻断启动
    }
  }
  return localConversations(limit);
}

/** 单会话正文：桌面走 Rust，其余走本地台账 */
export async function fetchConversationMessages(
  convId: string,
  limit = 200
): Promise<ChatMessageRow[]> {
  const api = getAPI();
  if (api && isDesktopRuntime()) {
    try {
      const rows = toMessageRows(await api.invoke(IPC_CHANNELS.CHAT_MESSAGES, convId, limit));
      if (rows.length) return rows;
    } catch {
      // 同列表：回退本地台账
    }
  }
  return localMessages(convId, limit);
}

// ── 恢复组装（纯函数，store 与测试共用） ──────────────────────

/** 一轮对话的恢复视图 */
export interface RestoredTurn {
  turnIndex: number;
  taskId: string;
  userContent: string;
  assistantContent: string;
  artifacts: string[];
  /** 本轮回复已有的评价（未评价为 null） */
  feedback: 'up' | 'down' | null;
  tsMs: number;
}

/** 正文行 → 轮次（缺失的一侧留空；只有一侧的轮次照样保留，不丢弃可见内容） */
export function groupTurns(rows: ChatMessageRow[]): RestoredTurn[] {
  const byTurn = new Map<number, { user?: ChatMessageRow; assistant?: ChatMessageRow }>();
  for (const row of rows) {
    const slot = byTurn.get(row.turnIndex) ?? {};
    slot[row.role] = row;
    byTurn.set(row.turnIndex, slot);
  }
  return [...byTurn.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([turnIndex, slot]) => {
      const user = slot.user;
      const assistant = slot.assistant;
      return {
        turnIndex,
        taskId: user?.taskId || assistant?.taskId || '',
        userContent: user?.content ?? '',
        assistantContent: assistant?.content ?? '',
        artifacts: assistant?.artifacts ?? [],
        feedback: assistant?.feedback === 'up' || assistant?.feedback === 'down' ? assistant.feedback : null,
        tsMs: Math.max(user?.tsMs ?? 0, assistant?.tsMs ?? 0),
      };
    });
}

/** 轮次 → 评价表（正文库恢复出的 feedbackByTurn） */
export function feedbackOfTurns(turns: RestoredTurn[]): Record<number, 'up' | 'down'> {
  const map: Record<number, 'up' | 'down'> = {};
  for (const t of turns) {
    if (t.feedback) map[t.turnIndex] = t.feedback;
  }
  return map;
}

/** 会话标题（与提交时同一截取口径：首轮提问前 20 字） */
export function titleOf(firstGoal: string): string {
  return firstGoal.trim().slice(0, 20) || '新任务';
}

/** 轮次 → 用户气泡消息（ChatStream 按用户消息切轮次，故一轮恰好一条） */
export function restoredUserMessages(turns: RestoredTurn[]): Message[] {
  return turns
    .filter((t) => t.userContent)
    .map((t, i) => ({
      id: `msg-restored-${t.turnIndex}-${i}`,
      taskId: t.taskId,
      role: Role.USER,
      content: t.userContent,
      ts: new Date(t.tsMs || Date.now()),
    }));
}

/**
 * 轮次 → 事件分组（界面据此渲染助手回复）。
 * 正文来自正文库；过程明细仅当事件流仍在 24h 窗口内可得（`processByTask`）。
 */
export function restoredTurnEvents(
  turns: RestoredTurn[],
  processByTask: Record<string, ChatEvent[]> = {}
): ChatEvent[][] {
  return turns.map((t) => {
    const process = processByTask[t.taskId] ?? [];
    const deliver: ChatEvent | null =
      t.assistantContent || t.artifacts.length
        ? ({
            type: 'deliver',
            taskId: t.taskId,
            summary: '',
            artifacts: t.artifacts,
            final_text: t.assistantContent,
          } as ChatEvent)
        : null;
    return deliver ? [...process, deliver] : [...process];
  });
}

/** 轮次 → taskId → 轮次号映射（重新生成/事件路由按它定位轮次） */
export function taskTurnMapOf(turns: RestoredTurn[]): Record<string, number> {
  const map: Record<string, number> = {};
  for (const t of turns) {
    if (t.taskId) map[t.taskId] = t.turnIndex;
  }
  return map;
}

const PROCESS_KINDS = new Set(['step_start', 'step_progress', 'step_done', 'step_failed']);

/**
 * 事件重放返回体 → 过程明细事件（StoredEvent 的原始 data 是引擎 snake_case）。
 * 只取步骤类事件：plan 会触发自动批准、status_change 会改写任务状态，恢复时都不该重放。
 */
export function replayProcessEvents(resp: unknown): ChatEvent[] {
  const raw = (resp ?? {}) as { events?: unknown };
  const events = Array.isArray(raw.events) ? raw.events : [];
  const out: ChatEvent[] = [];
  for (const item of events) {
    const e = (item ?? {}) as Record<string, unknown>;
    const kind = String(e.kind ?? e.type ?? '');
    if (!PROCESS_KINDS.has(kind)) continue;
    const d = (e.data ?? {}) as Record<string, unknown>;
    const str = (...keys: string[]): string => {
      for (const k of keys) {
        if (typeof d[k] === 'string') return d[k] as string;
      }
      return '';
    };
    const stepId = str('stepId', 'step_id');
    const seq = typeof e.seq === 'number' ? e.seq : undefined;
    if (kind === 'step_start') {
      // 引擎 data 无 stepType/progress 字段（实时流同样如此），按界面用到的字段组装
      out.push({ type: 'step_start', stepId, description: str('description'), seq } as unknown as ChatEvent);
    } else if (kind === 'step_progress') {
      out.push({ type: 'step_progress', stepId, message: str('message'), seq } as unknown as ChatEvent);
    } else if (kind === 'step_done') {
      out.push({ type: 'step_done', stepId, output: str('output'), seq } as unknown as ChatEvent);
    } else if (kind === 'step_failed') {
      out.push({ type: 'step_failed', stepId, error: str('error'), seq } as unknown as ChatEvent);
    }
  }
  return out;
}
