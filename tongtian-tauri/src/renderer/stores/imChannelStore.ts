/**
 * 通天办公 - IM 频道会话状态管理
 *
 * 业务口径：
 * - 通过软件端发起的任务 → 归「任务」（workbenchStore.historyTasks）
 * - 通过 IM 发起的会话 → 归「频道」（本 store，数据来自引擎会话台账）
 *
 * 真值来源只有一个：引擎 `im:channels`。这里不维护第二份频道清单，
 * 也不在前端写死「支持哪些 IM」——否则界面宣称的能力会超过引擎的真实能力。
 */

import { create } from 'zustand';
import type { ImChannelView, ImSession } from '../../shared/contracts';
import { getImChannels } from '../lib/alignment-api';

/** IM 渠道标识：由引擎清单下发，前端不穷举 */
export type ImChannelSource = string;

const FALLBACK_META = { label: 'IM 频道', color: '#94A3B8' };

/** 已知渠道的展示色；引擎新增渠道时自动落到中性色，不需要前端同步改表 */
const KNOWN_META: Record<string, { label: string; color: string }> = {
  dingtalk: { label: '钉钉', color: '#3B82F6' },
  feishu: { label: '飞书', color: '#8B5CF6' },
  wecom: { label: '企业微信', color: '#10B981' },
  wechat: { label: '微信', color: '#22C55E' },
  slack: { label: 'Slack', color: '#611F69' },
};

/** 渠道展示元信息（label 优先取引擎下发的中文名，前端表只补颜色） */
export function imChannelMeta(source: string, engineLabel?: string) {
  const known = KNOWN_META[source];
  return {
    label: engineLabel || known?.label || source || FALLBACK_META.label,
    color: known?.color || FALLBACK_META.color,
  };
}

/** IM 发起的会话 */
export interface ChannelSession {
  id: string;
  /** 会话标题（首条入站消息首行，引擎侧截断） */
  title: string;
  /** 来源渠道 */
  source: ImChannelSource;
  /** 最近一次消息时间戳 */
  updatedAt: number;
  /** 台账内消息条数（入站+回贴；单会话上限由引擎侧常量决定） */
  messageCount: number;
}

interface ImChannelState {
  /** IM 会话列表（侧边栏频道页签数据源） */
  channelSessions: ChannelSession[];
  /** 引擎通道清单 + 真实连接状态 */
  channels: ImChannelView[];
  loading: boolean;
  loaded: boolean;
  /** 最近一次拉取失败的原始错误（`CODE: detail`），页面用 errorTextOf 展示 */
  error: string;

  loadSessions: () => Promise<void>;
}

function toSessionRows(sessions: ImSession[]): ChannelSession[] {
  return sessions.map((s) => ({
    id: s.id,
    title: s.title,
    source: s.source,
    updatedAt: s.updatedAtMs,
    messageCount: s.messages?.length ?? 0,
  }));
}

/** 并发/轮询下只采纳最后一次结果，避免旧响应覆盖新状态 */
let seq = 0;

export const useImChannelStore = create<ImChannelState>((set, get) => ({
  channelSessions: [],
  channels: [],
  loading: false,
  loaded: false,
  error: '',

  loadSessions: async () => {
    const my = ++seq;
    set({ loading: true });
    try {
      const snap = await getImChannels();
      if (my !== seq) return;
      set({
        channelSessions: toSessionRows(snap.sessions),
        channels: snap.channels,
        loading: false,
        loaded: true,
        error: '',
      });
    } catch (err) {
      if (my !== seq) return;
      // 引擎未运行/无响应是真实状态，如实保留错误供页面展示，不清空已有清单
      set({
        loading: false,
        loaded: true,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  },
}));

export type { ImChannelState };
