/**
 * 通天办公 - 模型用量状态管理
 * 
 * 管理功能:
 * - 模型 Token 使用统计
 * - 用量限额管理
 * - 使用周期切换（日/月/总计）
 */

import { create } from 'zustand';
import type { ModelUsageStats } from '../components/ModelStatsPanel';
import { errorTextOf } from '../../shared/error-messages';

// ── 类型定义 ──────────────────────────────────────────────────────

export type UsagePeriod = 'daily' | 'monthly' | 'total';

interface ModelUsageState {
  /** 当前使用周期 */
  period: UsagePeriod;
  /** 模型用量统计列表 */
  stats: ModelUsageStats[];
  /** 加载状态 */
  loading: boolean;
  /** 错误信息 */
  error: string | null;

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
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useModelUsageStore = create<ModelUsageState>((set, get) => ({
  period: 'monthly',
  stats: [],
  loading: false,
  error: null,

  setPeriod: (period: UsagePeriod) => {
    set({ period });
    // 切换周期时重新加载数据
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
      // TODO: 调用 IPC 接口获取用量数据
      // const stats = await window.tongtianAPI.getModelUsage(period);
      // set({ stats });

      // 临时模拟数据
      const mockStats: ModelUsageStats[] = [
        {
          modelId: 'model-1',
          modelName: 'QwenPaw',
          tokensUsed: 125000,
          tokensLimit: 500000,
          usagePercent: 25,
          lastUsedAt: new Date(),
          period,
        },
        {
          modelId: 'model-2',
          modelName: 'GPT-4',
          tokensUsed: 890000,
          tokensLimit: 1000000,
          usagePercent: 89,
          lastUsedAt: new Date(Date.now() - 86400000), // 1天前
          period,
        },
        {
          modelId: 'model-3',
          modelName: '本地模型',
          tokensUsed: 45000,
          tokensLimit: undefined,
          usagePercent: 0,
          lastUsedAt: new Date(Date.now() - 172800000), // 2天前
          period,
        },
      ];

      set({ stats: mockStats });
    } catch (err) {
      set({
        error: `加载用量数据失败: ${errorTextOf(err)}`,
      });
    } finally {
      set({ loading: false });
    }
  },
}));
