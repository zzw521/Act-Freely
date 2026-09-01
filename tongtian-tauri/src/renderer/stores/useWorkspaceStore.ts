/**
 * 通天办公 - 工作区状态管理
 * 
 * 管理功能:
 * - 工作区列表
 * - 当前活跃工作区
 * - 工作区创建/切换/删除
 * - 工作区文件树
 */

import { create } from 'zustand';

// ── 类型定义 ──────────────────────────────────────────────────────────

export interface Workspace {
  id: string;
  name: string;
  /** 工作区根目录路径 */
  rootPath: string;
  /** 工作区描述 */
  description?: string;
  /** 创建时间 */
  createdAt: number;
  /** 最后访问时间 */
  lastAccessedAt: number;
}

export interface FileNode {
  id: string;
  name: string;
  type: 'file' | 'directory';
  path: string;
  children?: FileNode[];
  /** 文件大小 (字节) */
  size?: number;
  /** 最后修改时间 */
  modifiedAt?: number;
}

interface WorkspaceState {
  /** 工作区列表 */
  workspaces: Workspace[];
  /** 当前活跃工作区 ID */
  activeWorkspaceId: string | null;
  /** 当前工作区的文件树 */
  fileTree: FileNode[];
  /** 文件树加载状态 */
  fileTreeLoading: boolean;

  // ── Actions ──────────────────────────────────────────────────────

  /** 设置工作区列表 */
  setWorkspaces: (workspaces: Workspace[]) => void;
  /** 创建新工作区 */
  addWorkspace: (workspace: Workspace) => void;
  /** 删除工作区 */
  removeWorkspace: (id: string) => void;
  /** 切换活跃工作区 */
  setActiveWorkspace: (id: string) => void;
  /** 设置文件树 */
  setFileTree: (tree: FileNode[]) => void;
  /** 设置文件树加载状态 */
  setFileTreeLoading: (loading: boolean) => void;
  /** 获取当前活跃工作区 */
  getActiveWorkspace: () => Workspace | undefined;
}

// ── Store 实现 ────────────────────────────────────────────────────────

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  activeWorkspaceId: null,
  fileTree: [],
  fileTreeLoading: false,

  setWorkspaces: (workspaces: Workspace[]) => {
    set({ workspaces });
  },

  addWorkspace: (workspace: Workspace) => {
    set((state) => ({
      workspaces: [...state.workspaces, workspace],
    }));
  },

  removeWorkspace: (id: string) => {
    set((state) => ({
      workspaces: state.workspaces.filter((w) => w.id !== id),
      activeWorkspaceId: state.activeWorkspaceId === id ? null : state.activeWorkspaceId,
    }));
  },

  setActiveWorkspace: (id: string) => {
    set({ activeWorkspaceId: id, fileTree: [] });
  },

  setFileTree: (tree: FileNode[]) => {
    set({ fileTree: tree, fileTreeLoading: false });
  },

  setFileTreeLoading: (loading: boolean) => {
    set({ fileTreeLoading: loading });
  },

  getActiveWorkspace: () => {
    const { workspaces, activeWorkspaceId } = get();
    return workspaces.find((w) => w.id === activeWorkspaceId);
  },
}));
