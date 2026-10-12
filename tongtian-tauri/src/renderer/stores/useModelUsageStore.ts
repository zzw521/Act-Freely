/**
 * 通天办公 - 模型用量状态管理
 * 
 * 管理功能:
 * - 模型 Token 使用统计（真实台账：Rust model_usage / 引擎 /api/usage / Mock 本地台账）
 * - 用量限额管理
 * - 使用周期切换（日/月/总计）
 * - 周期总览（usage:stats，顶部总览条）与按需报表（usage:report，趋势 + 导出文本）
 */

import { create } from 'zustand';
import type { ModelUsageStats } from '../components/ModelStatsPanel';
import type { UsagePeriod, UsageReport, UsageStats } from '../../shared/contracts';
import { errorTextOf } from '../../shared/error-messages';
import api from '../lib/api';
import { extractUsageRows, reportToMarkdown, toUsageStats } from '../lib/usage-local';

// ── 类型定义 ──────────────────────────────────────────────────────

export type { UsagePeriod };

interface ModelUsageState {
  /** 当前使用周期 */
  period: UsagePeriod;
  /** 模型用量统计列表 */
  stats: ModelUsageStats[];
  /** 周期总览（usage:stats；未加载为 null，不伪造零值） */
  overview: UsageStats | null;
  /** 按需生成的报表（usage:report；切换周期后清空，避免与新周期错配） */
  report: UsageReport | null;
  /** 加载状态 */
  loading: boolean;
  /** 报表生成状态 */
  reportLoading: boolean;
  /** 错误信息 */
  error: string | null;
  /** 总览加载错误 */
  overviewError: string | null;
  /** 报表生成错误 */
  reportError: string | null;

  // ── Actions ──────────────────────────────────────────────────────

  /** 切换使用周期 */
  setPeriod: (period: UsagePeriod) => void;
  /** 设置用量统计 */
  setStats: (stats: ModelUsageStats[]) => void;
  /** 更新单个模型用量 */
  updateModelUsage: (modelId: string, tokensUsed: number) => void;
  /** 设置限额 */
  setTokenLimit: (modelId: string, limit: number) => void;
  /** 设置加载状态 */
  setLoading: (loading: boolean) => void;
  /** 设置错误 */
  setError: (error: string | null) => void;
  /** 加载用量数据 */
  loadUsageStats: () => Promise<void>;
  /** 加载周期总览（顶部总览条） */
  loadOverview: () => Promise<void>;
  /** 生成用量报表（趋势 + 可导出文本，由用户动作触发） */
  loadReport: () => Promise<UsageReport | null>;
  /** 报表导出文本（Markdown；无报表时返回 null） */
  reportMarkdown: () => string | null;
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useModelUsageStore = create<ModelUsageState>((set, get) => ({
  period: 'monthly',
  stats: [],
  overview: null,
  report: null,
  loading: false,
  reportLoading: false,
  error: null,
  overviewError: null,
  reportError: null,

  setPeriod: (period: UsagePeriod) => {
    // 总览与报表都按周期生成，切换后一律作废：留旧值会与新周期错配
    set({ period, overview: null, overviewError: null, report: null, reportError: null });
    // 切换周期时重新加载数据（总览由用量页的总览面板按需拉取）
    get().loadUsageStats();
  },

  setStats: (stats: ModelUsageStats[]) => {
    set({ stats });
  },

  updateModelUsage: (modelId: string, tokensUsed: number) => {
    set((state) => ({
      stats: state.stats.map((stat) =>
        stat.modelId === modelId
          ? {
              ...stat,
              tokensUsed,
              usagePercent: stat.tokensLimit
                ? Math.min((tokensUsed / stat.tokensLimit) * 100, 100)
                : 0,
              lastUsedAt: new Date(),
            }
          : stat,
      ),
    }));
  },

  setTokenLimit: (modelId: string, limit: number) => {
    set((state) => ({
      stats: state.stats.map((stat) =>
        stat.modelId === modelId
          ? {
              ...stat,
              tokensLimit: limit,
              usagePercent: (stat.tokensUsed / limit) * 100,
            }
          : stat,
      ),
    }));
  },

  setLoading: (loading: boolean) => {
    set({ loading });
  },

  setError: (error: string | null) => {
    set({ error });
  },

  loadUsageStats: async () => {
    const { period } = get();
    set({ loading: true, error: null });

    try {
      // 真实用量台账：Tauri 走 Rust command（SQLite 持久化）/
      // 浏览器直连引擎走 /api/usage / 开发 Mock 走本地台账（Mock 开关规范）
      const resp = await api.model.usage(period);
      set({ stats: toUsageStats(extractUsageRows(resp), period) });
    } catch (err) {
      // 查询失败不保留旧周期数据，避免与当前周期错配
      set({
        stats: [],
        error: `加载用量数据失败: ${errorTextOf(err)}`,
      });
    } finally {
      set({ loading: false });
    }
  },

  loadOverview: async () => {
    const { period } = get();
    set({ overviewError: null });
    try {
      // 与每模型明细同一张台账，只是聚合读法（usage:stats）
      const view = await api.usage.stats(period);
      // 期间用户切换了周期：丢弃迟到的旧周期结果
      if (get().period !== period) return;
      set({ overview: view, overviewError: null });
    } catch (err) {
      set({ overview: null, overviewError: errorTextOf(err, '用量总览加载失败') });
    }
  },

  loadReport: async () => {
    const { period } = get();
    set({ reportLoading: true, reportError: null });
    try {
      const rep = await api.usage.report(period);
      if (get().period !== period) return null;
      set({ report: rep });
      return rep;
    } catch (err) {
      set({ report: null, reportError: errorTextOf(err, '用量报表生成失败') });
      return null;
    } finally {
      set({ reportLoading: false });
    }
  },

  reportMarkdown: () => {
    const { report } = get();
    return report ? reportToMarkdown(report) : null;
  },
}));
