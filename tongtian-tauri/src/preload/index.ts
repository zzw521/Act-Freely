// ============================================================
// src/preload/index.ts — Preload script with contextBridge
// ============================================================

import { contextBridge, ipcRenderer } from 'electron';
import { IPC_CHANNELS } from '../shared/contracts';
import type { IPCChannel } from '../shared/contracts';

// ── Whitelist of allowed channels ────────────────────────────

const ALLOWED_INVOKE_CHANNELS: Set<string> = new Set([
  IPC_CHANNELS.CHAT_SUBMIT,
  IPC_CHANNELS.CHAT_REVIEW,
  IPC_CHANNELS.CHAT_CONTROL,
  IPC_CHANNELS.CHAT_HISTORY,
  IPC_CHANNELS.MODEL_LIST,
  IPC_CHANNELS.MODEL_CREATE,
  IPC_CHANNELS.MODEL_UPDATE,
  IPC_CHANNELS.MODEL_DELETE,
  IPC_CHANNELS.MODEL_TEST,
  IPC_CHANNELS.MODEL_SET_DEFAULT,
  IPC_CHANNELS.MODEL_USAGE,
  IPC_CHANNELS.FILE_BROWSE,
  IPC_CHANNELS.FILE_PREVIEW,
  IPC_CHANNELS.FILE_READ,
  IPC_CHANNELS.FILE_WRITE,
  IPC_CHANNELS.FILE_CONVERT,
  IPC_CHANNELS.FILE_ORGANIZE,
  IPC_CHANNELS.WORKSPACE_LIST,
  IPC_CHANNELS.WORKSPACE_CREATE,
  IPC_CHANNELS.WORKSPACE_UPDATE,
  IPC_CHANNELS.WORKSPACE_DELETE,
  IPC_CHANNELS.WORKSPACE_SUSPEND,
  IPC_CHANNELS.WORKSPACE_RESUME,
  IPC_CHANNELS.WORKSPACE_DELEGATE,
  IPC_CHANNELS.SKILL_LIST,
  IPC_CHANNELS.SKILL_INSTALL,
  IPC_CHANNELS.SKILL_UNINSTALL,
  IPC_CHANNELS.SKILL_TOGGLE,
  IPC_CHANNELS.MCP_LIST_SERVERS,
  IPC_CHANNELS.MCP_ADD_SERVER,
  IPC_CHANNELS.MCP_REMOVE_SERVER,
  IPC_CHANNELS.MCP_CONNECT,
  IPC_CHANNELS.MCP_DISCONNECT,
  IPC_CHANNELS.MCP_LIST_TOOLS,
  IPC_CHANNELS.SANDBOX_GET_POLICY,
  IPC_CHANNELS.SANDBOX_UPDATE_POLICY,
  IPC_CHANNELS.AUDIT_QUERY,
  IPC_CHANNELS.AUDIT_EXPORT,
  IPC_CHANNELS.SCHEDULE_LIST,
  IPC_CHANNELS.SCHEDULE_CREATE,
  IPC_CHANNELS.SCHEDULE_UPDATE,
  IPC_CHANNELS.SCHEDULE_DELETE,
  IPC_CHANNELS.SCHEDULE_TOGGLE,
  IPC_CHANNELS.COMPLIANCE_STATUS,
  IPC_CHANNELS.COMPLIANCE_UPDATE,
  IPC_CHANNELS.USAGE_STATS,
  IPC_CHANNELS.USAGE_REPORT,
  IPC_CHANNELS.SYSTEM_INFO,
  IPC_CHANNELS.SYSTEM_HEALTH,
  // ── 千问对标整改新增 ──
  IPC_CHANNELS.SETTINGS_GET,
  IPC_CHANNELS.SETTINGS_SET,
  IPC_CHANNELS.HOOKS_GET,
  IPC_CHANNELS.HOOKS_SET,
  IPC_CHANNELS.HOOKS_TEST,
  IPC_CHANNELS.AWARENESS_GET,
  IPC_CHANNELS.AWARENESS_UPDATE,
  IPC_CHANNELS.AWARENESS_MEMORIES,
  IPC_CHANNELS.AWARENESS_MEMORY_ADD,
  IPC_CHANNELS.AWARENESS_MEMORY_DELETE,
  IPC_CHANNELS.CONNECTOR_LIST,
  IPC_CHANNELS.CONNECTOR_AUTHORIZE,
  IPC_CHANNELS.CONNECTOR_REVOKE,
  IPC_CHANNELS.TASK_ARCHIVE,
  IPC_CHANNELS.TASK_UNARCHIVE,
  IPC_CHANNELS.TASK_ARCHIVE_DELETE,
  IPC_CHANNELS.TASK_ARCHIVE_LIST,
  IPC_CHANNELS.TASK_ARCHIVE_CLEAR,
  IPC_CHANNELS.COMPUTER_USE_GET_POLICY,
  IPC_CHANNELS.COMPUTER_USE_SET_POLICY,
]);

const ALLOWED_SEND_CHANNELS: Set<string> = new Set([
  IPC_CHANNELS.CHAT_SUBMIT,
  IPC_CHANNELS.CHAT_CONTROL,
  IPC_CHANNELS.SYSTEM_INFO,
  IPC_CHANNELS.SYSTEM_HEALTH,
]);

const ALLOWED_EVENT_CHANNELS: Set<string> = new Set([
  IPC_CHANNELS.CHAT_EVENT,
]);

// ── Validation ───────────────────────────────────────────────

function validateChannel(channel: string, allowed: Set<string>): void {
  if (!allowed.has(channel)) {
    throw new Error(`IPC channel "${channel}" is not allowed`);
  }
}

// ── Convenience wrappers ─────────────────────────────────────

function chatSubmit(req: { taskId?: string; goal: string; modelBinding?: string }): Promise<unknown> {
  return ipcRenderer.invoke(IPC_CHANNELS.CHAT_SUBMIT, req);
}

function chatReview(req: { planId: string; action: 'approve' | 'reject' | 'modify'; patch?: unknown }): Promise<unknown> {
  return ipcRenderer.invoke(IPC_CHANNELS.CHAT_REVIEW, req);
}

function chatControl(req: { taskId: string; action: 'pause' | 'resume' | 'rollback_to'; stepId?: string }): Promise<unknown> {
  return ipcRenderer.invoke(IPC_CHANNELS.CHAT_CONTROL, req);
}

function onChatEvent(callback: (event: unknown) => void): () => void {
  const handler = (_event: Electron.IpcRendererEvent, ...args: unknown[]) => {
    callback(args[0]);
  };
  ipcRenderer.on(IPC_CHANNELS.CHAT_EVENT, handler);
  return () => {
    ipcRenderer.removeListener(IPC_CHANNELS.CHAT_EVENT, handler);
  };
}

// ── Exposed API ──────────────────────────────────────────────

const tongtianAPI = {
  /**
   * Type-safe IPC invoke for request/response channels.
   * Sends a message to the main process and waits for a response.
   */
  invoke: (channel: string, ...args: unknown[]): Promise<unknown> => {
    validateChannel(channel, ALLOWED_INVOKE_CHANNELS);
    return ipcRenderer.invoke(channel, ...args);
  },

  /**
   * Fire-and-forget IPC send (no response expected).
   */
  send: (channel: string, ...args: unknown[]): void => {
    validateChannel(channel, ALLOWED_SEND_CHANNELS);
    ipcRenderer.send(channel, ...args);
  },

  /**
   * Listen for events from the main process (e.g. streaming chat events).
   */
  on: (channel: string, callback: (...args: unknown[]) => void): void => {
    validateChannel(channel, ALLOWED_EVENT_CHANNELS);
    ipcRenderer.on(channel, (_event, ...args: unknown[]) => {
      callback(...args);
    });
  },

  /**
   * Remove a specific event listener.
   */
  removeListener: (channel: string, callback: (...args: unknown[]) => void): void => {
    validateChannel(channel, ALLOWED_EVENT_CHANNELS);
    ipcRenderer.removeListener(channel, callback as (...args: unknown[]) => void);
  },

  /**
   * Remove all listeners for a given channel.
   */
  removeAllListeners: (channel: string): void => {
    validateChannel(channel, ALLOWED_EVENT_CHANNELS);
    ipcRenderer.removeAllListeners(channel);
  },

  // ── 便捷方法 ──────────────────────────────────────────────
  chatSubmit,
  chatReview,
  chatControl,
  onChatEvent,
};

// ── Expose via contextBridge ─────────────────────────────────

contextBridge.exposeInMainWorld('tongtianAPI', tongtianAPI);
