/**
 * 任务搜索与会话内查找（快捷键 Ctrl+G / Ctrl+F 的真实后端）
 *
 * 数据源就是 workbenchStore 里真实存在的会话与消息，不造假结果：
 * - 标题命中直接进列表；正文命中附带命中的轮次，供 Ctrl+F 定位与高亮
 * - 空查询返回最近的会话（命令面板常见的「最近」视图），而不是空列表
 * - 纯逻辑、不依赖 DOM，可在 node 环境下直接单测
 */

import { Role, type ChatEvent, type Message } from '../../shared/types';

/** 一轮对话的可搜索文本（用户提问 + 助手可见回复 + 步骤产出） */
export interface SearchableTurn {
  /** 轮次下标，与 ChatStream/turnEvents 的 0 基索引一致（data-turn 同口径） */
  turn: number;
  text: string;
}

/** 会话搜索命中项 */
export interface TaskSearchHit {
  convId: string;
  title: string;
  /** title：标题命中；content：仅正文命中 */
  matchedIn: 'title' | 'content';
  /** 命中上下文片段（带省略号）；标题命中且无正文命中时为最近一轮内容 */
  preview: string;
  /** 正文命中的轮次下标（与 data-turn 同口径，Ctrl+F 据此定位） */
  turns: number[];
  /** 会话最近活动时间（毫秒） */
  updatedAt: number;
  /** 是否仍在执行 */
  running: boolean;
}

/** 参与搜索的最小会话形状（与 ConversationState 结构兼容） */
export interface SearchableConversation {
  id: string;
  title: string;
  createdAt: number;
  isLoading: boolean;
  messages: Message[];
  turnEvents?: ChatEvent[][];
}

/** 归一查询串：去首尾空白 + 压缩内部空白，便于在正文里做子串匹配 */
function normalizeSpace(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

/** 事件里可读出的助手侧文本：deliver 的正文/摘要 + 步骤产出/描述 */
function eventText(event: ChatEvent): string {
  switch (event.type) {
    case 'deliver': {
      const finalText = (event as { final_text?: string }).final_text ?? '';
      // 交付物文件名在界面上是可见的（可点击的产物 chip），所以也要能被搜到
      return normalizeSpace(`${finalText} ${event.summary ?? ''} ${(event.artifacts ?? []).join(' ')}`);
    }
    case 'step_done':
      return normalizeSpace(event.output ?? '');
    case 'step_start':
      return normalizeSpace(event.description ?? '');
    case 'step_progress':
      return normalizeSpace(event.message ?? '');
    case 'step_failed':
      return normalizeSpace(event.error ?? '');
    case 'plan':
      return normalizeSpace((event.steps ?? []).map((s) => s.description).join(' '));
    default:
      return '';
  }
}

/**
 * 按轮次抽出可搜索文本。
 * 第 N 条用户消息与其后的事件组构成第 N 轮，轮次下标与 ChatStream 的 0 基索引一致。
 */
export function turnTexts(messages: Message[], turnEvents?: ChatEvent[][]): SearchableTurn[] {
  const userMsgs = messages.filter((m) => m.role === Role.USER);
  const groups = turnEvents ?? [];
  const count = Math.max(userMsgs.length, groups.length);
  const out: SearchableTurn[] = [];
  for (let i = 0; i < count; i += 1) {
    const asked = userMsgs[i] ? normalizeSpace(String(userMsgs[i].content ?? '')) : '';
    const answered = (groups[i] ?? []).map(eventText).filter(Boolean).join(' ');
    const text = normalizeSpace(`${asked} ${answered}`);
    if (text) out.push({ turn: i, text });
  }
  return out;
}

/** 生成命中片段：以第一次命中为中心取上下文，前后各 24 字 */
function makeSnippet(text: string, needle: string): string {
  const lowerText = text.toLowerCase();
  const at = lowerText.indexOf(needle);
  if (at < 0) return text.slice(0, 60);
  const start = Math.max(0, at - 24);
  const end = Math.min(text.length, at + needle.length + 24);
  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`;
}

/** 会话内查找：返回正文命中的轮次（大小写与空白不敏感） */
export function findMatchingTurns(turns: SearchableTurn[], query: string): number[] {
  const needle = normalizeSpace(query).toLowerCase();
  if (!needle) return [];
  return turns.filter((t) => t.text.toLowerCase().includes(needle)).map((t) => t.turn);
}

/**
 * 跨会话搜索任务。
 * 排序：标题命中优先，其次按最近活动时间倒序；空查询返回最近会话。
 */
export function searchConversations(
  convs: SearchableConversation[],
  query: string,
  limit = 20,
): TaskSearchHit[] {
  const needle = normalizeSpace(query).toLowerCase();
  const hits: TaskSearchHit[] = [];

  for (const conv of convs) {
    const turns = turnTexts(conv.messages, conv.turnEvents);
    const lastActivity = conv.messages.length
      ? new Date(conv.messages[conv.messages.length - 1].ts).getTime() || conv.createdAt
      : conv.createdAt;
    const titleHit = !needle || conv.title.toLowerCase().includes(needle);
    const matchedTurns = needle ? findMatchingTurns(turns, needle) : [];

    if (needle && !titleHit && matchedTurns.length === 0) continue;

    const contentTurn = needle
      ? turns.find((t) => t.text.toLowerCase().includes(needle))
      : turns[turns.length - 1];
    const preview = contentTurn
      ? needle
        ? makeSnippet(contentTurn.text, needle)
        : normalizeSpace(contentTurn.text).slice(0, 60)
      : '（尚无回复内容）';

    hits.push({
      convId: conv.id,
      title: conv.title || '新任务',
      matchedIn: titleHit ? 'title' : 'content',
      preview,
      turns: matchedTurns,
      updatedAt: lastActivity,
      running: conv.isLoading,
    });
  }

  hits.sort((a, b) => {
    if (a.matchedIn !== b.matchedIn) return a.matchedIn === 'title' ? -1 : 1;
    return b.updatedAt - a.updatedAt;
  });

  return hits.slice(0, limit);
}
