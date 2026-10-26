/**
 * 通天办公 - 全局应用状态管理
 *
 * 管理功能:
 * - 主题模式 (亮色/暗色)
 * - 侧边栏折叠状态
 * - 工作台右侧任务监控面板显隐
 * - 全局任务搜索面板 / 会话内查找条显隐
 * - 全局加载状态
 *
 * 合规硬开关不在这里：它是安全策略，权威在 Rust 出域控制表，
 * 由 `store/securityStore` 经 `compliance:status/update` 通道读写。
 */

import { create } from 'zustand';
import type { PaletteMode } from '@mui/material';

// ── 类型定义 ──────────────────────────────────────────────────────────

interface AppState {
  /** 主题模式 */
  themeMode: PaletteMode;
  /** 侧边栏是否折叠 */
  sidebarCollapsed: boolean;
  /** 工作台右侧任务监控面板是否展开（快捷键 Ctrl + / 切换） */
  taskMonitorOpen: boolean;
  /** 全局任务搜索面板是否打开（快捷键 Ctrl + G） */
  taskSearchOpen: boolean;
  /** QuickPick 应用内快速任务窗是否打开（Alt+Space / 双击 Ctrl） */
  quickPickOpen: boolean;
  /** 当前会话内的查找条是否打开（快捷键 Ctrl + F） */
  findBarOpen: boolean;
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
  /** 切换侧边栏 */
  toggleSidebar: () => void;
  /** 设置侧边栏状态 */
  setSidebarCollapsed: (collapsed: boolean) => void;
  /** 切换任务监控面板 */
  toggleTaskMonitor: () => void;
  /** 设置任务监控面板显隐 */
  setTaskMonitorOpen: (open: boolean) => void;
  /** 设置全局任务搜索面板显隐 */
  setTaskSearchOpen: (open: boolean) => void;
  /** 设置 QuickPick 快速任务窗显隐 */
  setQuickPickOpen: (open: boolean) => void;
  /** 设置会话内查找条显隐 */
  setFindBarOpen: (open: boolean) => void;
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
  sidebarCollapsed: false,
  taskMonitorOpen: true,
  taskSearchOpen: false,
  quickPickOpen: false,
  findBarOpen: false,
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

  toggleSidebar: () => {
    set((state) => ({
      sidebarCollapsed: !state.sidebarCollapsed,
    }));
  },

  setSidebarCollapsed: (collapsed: boolean) => {
    set({ sidebarCollapsed: collapsed });
  },

  toggleTaskMonitor: () => {
    set((state) => ({ taskMonitorOpen: !state.taskMonitorOpen }));
  },

  setTaskMonitorOpen: (open: boolean) => {
    set({ taskMonitorOpen: open });
  },

  setTaskSearchOpen: (open: boolean) => {
    set({ taskSearchOpen: open });
  },

  setQuickPickOpen: (open: boolean) => {
    set({ quickPickOpen: open });
  },

  setFindBarOpen: (open: boolean) => {
    set({ findBarOpen: open });
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
