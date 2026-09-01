/**
 * 通天办公 - IM 频道会话状态管理
 *
 * 业务口径：
 * - 通过软件端发起的任务 → 归「任务」（workbenchStore.historyTasks）
 * - 通过 IM（钉钉/飞书/企业微信/微信）发起的会话 → 归「频道」（本 store）
 *
 * Mock 开关规范：
 * - 开发阶段（VITE_MOCK !== 'false'）：使用本地模拟会话数据
 * - 联调阶段（VITE_MOCK === 'false'）：预留真实接口位，当前降级返回空列表
 */

import { create } from 'zustand';

// ── 类型定义 ──────────────────────────────────────────────────────────

/** IM 渠道标识 */
export type ImChannelSource = 'dingtalk' | 'feishu' | 'wecom' | 'wechat';

/** 渠道展示元信息 */
export const IM_CHANNEL_META: Record<ImChannelSource, { label: string; color: string }> = {
  dingtalk: { label: '钉钉', color: '#3B82F6' },
  feishu: { label: '飞书', color: '#8B5CF6' },
  wecom: { label: '企业微信', color: '#10B981' },
  wechat: { label: '微信', color: '#22C55E' },
};

/** IM 发起的会话 */
export interface ChannelSession {
  id: string;
  /** 会话标题（IM 消息首句或任务摘要） */
  title: string;
  /** 来源渠道 */
  source: ImChannelSource;
  /** 发起时间戳 */
  updatedAt: number;
}

interface ImChannelState {
  /** IM 会话列表（频道页签数据源） */
  channelSessions: ChannelSession[];
  loading: boolean;
  loaded: boolean;

  /** 加载 IM 会话列表 */
  loadSessions: () => Promise<void>;
}

// ── Mock 数据（开发阶段） ─────────────────────────────────────────────

const mockEnabled = import.meta.env.VITE_MOCK !== 'false';

// 频道页签默认不提供示例会话，等待真实 IM 会话接入
const mockSessions: ChannelSession[] = [];

// ── Store 实现 ────────────────────────────────────────────────────────

export const useImChannelStore = create<ImChannelState>((set, get) => ({
  channelSessions: [],
  loading: false,
  loaded: false,

  loadSessions: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      if (mockEnabled) {
        // 开发阶段：本地模拟数据
        await new Promise((r) => setTimeout(r, 120));
        set({ channelSessions: mockSessions, loading: false, loaded: true });
        return;
      }
      // 联调阶段：真实接口接入位（IM Webhook 会话列表）
      set({ channelSessions: [], loading: false, loaded: true });
    } catch {
      // 会话加载失败不阻断主流程
      set({ loading: false });
    }
  },
}));
