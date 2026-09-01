/**
 * 通天办公 - 模型状态管理
 * 
 * 管理功能:
 * - 可用模型列表
 * - 当前选中模型
 * - 模型连接状态
 * - 本地/云端模型切换
 */

import { create } from 'zustand';

// ── 类型定义 ──────────────────────────────────────────────────────────

export type ModelProvider = 'local' | 'cloud';
export type ModelStatus = 'connected' | 'disconnected' | 'connecting' | 'error';

export interface ModelConfig {
  id: string;
  name: string;
  provider: ModelProvider;
  /** 模型标识符 (如 qwen-72b, gpt-4 等) */
  modelIdentifier: string;
  /** API 端点 (云端模型) */
  endpoint?: string;
  /** 本地模型路径 */
  localPath?: string;
  /** 最大上下文长度 */
  maxContextLength: number;
  /** 是否支持工具调用 */
  supportsToolCalls: boolean;
  /** 是否支持视觉 */
  supportsVision: boolean;
}

interface ModelState {
  /** 可用模型列表 */
  models: ModelConfig[];
  /** 当前选中模型 ID */
  activeModelId: string | null;
  /** 模型连接状态 */
  status: ModelStatus;
  /** 错误信息 */
  error: string | null;

  // ── Actions ──────────────────────────────────────────────────────

  /** 设置可用模型列表 */
  setModels: (models: ModelConfig[]) => void;
  /** 设置活跃模型 */
  setActiveModel: (id: string) => void;
  /** 添加模型 */
  addModel: (model: ModelConfig) => void;
  /** 移除模型 */
  removeModel: (id: string) => void;
  /** 更新模型连接状态 */
  setStatus: (status: ModelStatus) => void;
  /** 设置错误 */
  setError: (error: string | null) => void;
  /** 获取当前活跃模型配置 */
  getActiveModel: () => ModelConfig | undefined;
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useModelStore = create<ModelState>((set, get) => ({
  models: [],
  activeModelId: null,
  status: 'disconnected',
  error: null,

  setModels: (models: ModelConfig[]) => {
    set({ models });
  },

  setActiveModel: (id: string) => {
    set({ activeModelId: id });
  },

  addModel: (model: ModelConfig) => {
    set((state) => ({
      models: [...state.models, model],
    }));
  },

  removeModel: (id: string) => {
    set((state) => ({
      models: state.models.filter((m) => m.id !== id),
      activeModelId: state.activeModelId === id ? null : state.activeModelId,
    }));
  },

  setStatus: (status: ModelStatus) => {
    set({ status });
  },

  setError: (error: string | null) => {
    set({ error, status: error ? 'error' : 'disconnected' });
  },

  getActiveModel: () => {
    const { models, activeModelId } = get();
    return models.find((m) => m.id === activeModelId);
  },
}));
