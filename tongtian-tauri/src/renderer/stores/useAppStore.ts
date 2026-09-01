/**
 * 通天办公 - 全局应用状态管理
 * 
 * 管理功能:
 * - 主题模式 (亮色/暗色)
 * - 硬开关状态 (合规模式)
 * - 侧边栏折叠状态
 * - 全局加载状态
 */

import { create } from 'zustand';
import type { PaletteMode } from '@mui/material';

// ── 类型定义 ──────────────────────────────────────────────────────────

interface AppState {
  /** 主题模式 */
  themeMode: PaletteMode;
  /** 硬开关状态 (true = 合规模式开启, 禁止外部网络) */
  hardSwitchOff: boolean;
  /** 侧边栏是否折叠 */
  sidebarCollapsed: boolean;
  /** 全局加载状态 */
  globalLoading: boolean;
  /** 全局加载提示文字 */
  loadingMessage: string;
  /** Snackbar 通知 */
  notification: {
    open: boolean;
    message: string;
    severity: 'success' | 'error' | 'warning' | 'info';
  } | null;

  // ── Actions ──────────────────────────────────────────────────────

  /** 切换主题模式 */
  toggleThemeMode: () => void;
  /** 设置主题模式 */
  setThemeMode: (mode: PaletteMode) => void;
  /** 切换硬开关 */
  toggleHardSwitch: () => void;
  /** 设置硬开关状态 */
  setHardSwitchOff: (off: boolean) => void;
  /** 切换侧边栏 */
  toggleSidebar: () => void;
  /** 设置侧边栏状态 */
  setSidebarCollapsed: (collapsed: boolean) => void;
  /** 设置全局加载 */
  setGlobalLoading: (loading: boolean, message?: string) => void;
  /** 显示通知 */
  showNotification: (message: string, severity?: 'success' | 'error' | 'warning' | 'info') => void;
  /** 关闭通知 */
  closeNotification: () => void;
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useAppStore = create<AppState>((set) => ({
  themeMode: 'light',
  hardSwitchOff: false,
  sidebarCollapsed: false,
  globalLoading: false,
  loadingMessage: '',
  notification: null,

  toggleThemeMode: () => {
    set((state) => ({
      themeMode: state.themeMode === 'light' ? 'dark' : 'light',
    }));
  },

  setThemeMode: (mode: PaletteMode) => {
    set({ themeMode: mode });
  },

  toggleHardSwitch: () => {
    set((state) => ({
      hardSwitchOff: !state.hardSwitchOff,
    }));
  },

  setHardSwitchOff: (off: boolean) => {
    set({ hardSwitchOff: off });
  },

  toggleSidebar: () => {
    set((state) => ({
      sidebarCollapsed: !state.sidebarCollapsed,
    }));
  },

  setSidebarCollapsed: (collapsed: boolean) => {
    set({ sidebarCollapsed: collapsed });
  },

  setGlobalLoading: (loading: boolean, message = '') => {
    set({ globalLoading: loading, loadingMessage: message });
  },

  showNotification: (message: string, severity = 'info') => {
    set({
      notification: { open: true, message, severity },
    });
  },

  closeNotification: () => {
    set({ notification: null });
  },
}));
