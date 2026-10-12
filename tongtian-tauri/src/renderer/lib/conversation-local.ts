/**
 * 会话正文通道 · 本地后端（Mock 与浏览器直连引擎共用权威实现）
 *
 * 桌面应用以 Rust command `chat_conversations` / `chat_messages`（SQLite chat_messages 表）为权威；
 * 本模块只服务两条非 Rust 通道，使「刷新后恢复会话列表与正文」在 Mock 模式下可自测：
 * - 开发 Mock 模式（VITE_MOCK 未关闭）
 * - 浏览器直连引擎模式（引擎进程无会话正文库）
 *
 * 与 Rust 同口径：同一套会话聚合规则（标题取首轮提问、按最近活跃倒序）、
 * 同样的「无记录即空」语义（不伪造历史会话）。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import { localConversations, localMessages, setLocalFeedback } from './conversation-store';

export interface LocalConversationResult {
  handled: boolean;
  value: unknown;
}

/** 非 Rust 通道的会话正文本地实现 */
export function handleLocalConversationChannel(
  channel: string,
  args: unknown[],
): LocalConversationResult {
  if (channel === IPC_CHANNELS.CHAT_CONVERSATIONS) {
    const limit = Number(args[0] ?? 0) || 50;
    return { handled: true, value: localConversations(limit) };
  }
  if (channel === IPC_CHANNELS.CHAT_MESSAGES) {
    const convId = String(args[0] ?? '');
    if (!convId) throw new Error('CHAT_INVALID_CONVERSATION: 会话标识不能为空');
    const limit = Number(args[1] ?? 0) || 200;
    return { handled: true, value: localMessages(convId, limit) };
  }
  if (channel === IPC_CHANNELS.CHAT_FEEDBACK) {
    const convId = String(args[0] ?? '');
    if (!convId) throw new Error('CHAT_INVALID_CONVERSATION: 会话标识不能为空');
    const turnIndex = Number(args[1]);
    if (!Number.isInteger(turnIndex) || turnIndex < 0) {
      throw new Error('CHAT_FEEDBACK_INVALID_VALUE: 轮次号不合法');
    }
    const raw = args[2];
    let kind: 'up' | 'down' | null;
    if (raw === 'up' || raw === 'down') kind = raw;
    else if (raw == null || raw === '') kind = null;
    else throw new Error(`CHAT_FEEDBACK_INVALID_VALUE: 只接受 up/down/取消，收到 ${String(raw)}`);
    setLocalFeedback(convId, turnIndex, kind);
    return { handled: true, value: { ok: true } };
  }
  return { handled: false, value: undefined };
}
