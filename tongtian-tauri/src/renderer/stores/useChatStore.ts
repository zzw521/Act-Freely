/**
 * 通天办公 - 对话状态管理
 * 
 * 管理功能:
 * - 会话列表 (创建/切换/删除)
 * - 消息收发 (发送/接收/历史加载)
 * - 当前会话状态 (空闲/思考/错误)
 * - 流式响应处理
 */

import { create } from 'zustand';

// ── 类型定义 ──────────────────────────────────────────────────────────

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: number;
  /** 流式响应是否完成 */
  streaming?: boolean;
  /** Token 使用量 */
  tokenUsage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  modelId: string;
  createdAt: number;
  updatedAt: number;
}

export type ChatStatus = 'idle' | 'thinking' | 'streaming' | 'error';

interface ChatState {
  /** 会话列表 */
  conversations: Conversation[];
  /** 当前活跃会话 ID */
  activeConversationId: string | null;
  /** 对话状态 */
  status: ChatStatus;
  /** 错误信息 */
  error: string | null;

  // ── Actions ──────────────────────────────────────────────────────

  /** 创建新会话 */
  createConversation: (modelId: string) => string;
  /** 切换活跃会话 */
  setActiveConversation: (id: string) => void;
  /** 删除会话 */
  deleteConversation: (id: string) => void;
  /** 添加消息到当前会话 */
  addMessage: (message: Message) => void;
  /** 更新消息 (流式更新) */
  updateMessage: (messageId: string, content: string) => void;
  /** 设置对话状态 */
  setStatus: (status: ChatStatus) => void;
  /** 设置错误 */
  setError: (error: string | null) => void;
  /** 清空当前会话消息 */
  clearMessages: () => void;
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  activeConversationId: null,
  status: 'idle',
  error: null,

  createConversation: (modelId: string) => {
    const id = `conv-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const now = Date.now();
    const newConversation: Conversation = {
      id,
      title: '新对话',
      messages: [],
      modelId,
      createdAt: now,
      updatedAt: now,
    };

    set((state) => ({
      conversations: [newConversation, ...state.conversations],
      activeConversationId: id,
    }));

    return id;
  },

  setActiveConversation: (id: string) => {
    set({ activeConversationId: id });
  },

  deleteConversation: (id: string) => {
    set((state) => {
      const conversations = state.conversations.filter((c) => c.id !== id);
      const activeConversationId =
        state.activeConversationId === id
          ? (conversations[0]?.id ?? null)
          : state.activeConversationId;
      return { conversations, activeConversationId };
    });
  },

  addMessage: (message: Message) => {
    set((state) => ({
      conversations: state.conversations.map((conv) =>
        conv.id === state.activeConversationId
          ? {
              ...conv,
              messages: [...conv.messages, message],
              updatedAt: Date.now(),
              title: conv.messages.length === 0 && message.role === 'user'
                ? message.content.slice(0, 30) + (message.content.length > 30 ? '...' : '')
                : conv.title,
            }
          : conv
      ),
    }));
  },

  updateMessage: (messageId: string, content: string) => {
    set((state) => ({
      conversations: state.conversations.map((conv) =>
        conv.id === state.activeConversationId
          ? {
              ...conv,
              messages: conv.messages.map((msg) =>
                msg.id === messageId ? { ...msg, content } : msg
              ),
            }
          : conv
      ),
    }));
  },

  setStatus: (status: ChatStatus) => {
    set({ status });
  },

  setError: (error: string | null) => {
    set({ error, status: error ? 'error' : 'idle' });
  },

  clearMessages: () => {
    set((state) => ({
      conversations: state.conversations.map((conv) =>
        conv.id === state.activeConversationId
          ? { ...conv, messages: [], updatedAt: Date.now() }
          : conv
      ),
    }));
  },
}));
