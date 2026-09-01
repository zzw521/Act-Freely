/**
 * 通天办公 (Tauri 版) - Tauri command/event 适配层
 *
 * 在 Tauri 容器内运行时，将 Rust command（invoke）与
 * 事件流（listen: chat-event / agent-status）适配为
 * 渲染进程统一的 tongtianAPI 接口。
 */

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { ChatEvent, ChatControlReq, ChatSubmitReq } from '../../shared/types';
import type { LeaseInfo, LeaseTakeoverResp, QuotaSnapshot } from '../../shared/contracts';
import { EventStreamGuard } from './event-stream-guard';

/** 全局事件流守卫（三层架构演进 P0：seq 去重/缺口检测，重放场景防重复渲染） */
const chatEventGuard = new EventStreamGuard({
  onGap: (taskId, expected, received) => {
    // 缺口提示：Rust 侧已在 stream_chat 内自动拉取重放，
    // 前端仅记录（连续 gap 时可触发 eventReplay 手动补齐）
    console.warn(`[event-guard] seq gap task=${taskId} expected=${expected} received=${received}`);
  },
});

/** 是否运行在 Tauri 容器内 */
export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** 尚未实现的 channel 的兜底返回值（多为列表查询） */
const CHANNEL_FALLBACK: Record<string, unknown> = {
  'model:usage': [],
  'workspace:list': [],
  'skill:list': [],
  'mcp:list': [],
  'schedule:list': [],
  'audit:query': [],
};

export const tauriAPI = {
  // ── 通用 IPC 接口 ──────────────────────────────────────
  invoke: async (channel: string, ...args: unknown[]): Promise<unknown> => {
    if (channel === 'system:info') {
      return invoke('system_info');
    }
    if (channel === 'system:health') {
      return invoke('agent_status');
    }
    if (channel === 'chat:history') {
      return invoke('chat_history', { limit: args[0] as number | undefined });
    }
    if (channel === 'file:browse') {
      return invoke('file_list', {
        dirPath: args[0] as string,
        workspaceId: args[1] as string | undefined,
      });
    }
    if (channel === 'file:read') {
      return invoke('file_read', {
        filePath: args[0] as string,
        workspaceId: args[1] as string | undefined,
      });
    }
    if (channel === 'file:preview') {
      return invoke('file_preview', {
        filePaths: args[0] as string[],
        workspaceId: args[1] as string | undefined,
      });
    }
    if (channel === 'model:list') return invoke('model_list');
    if (channel === 'model:create') return invoke('model_add', { req: args[0] });
    if (channel === 'model:update') return invoke('model_update', { req: args[0] });
    if (channel === 'model:delete') {
      const id = (args[0] as { id?: string })?.id ?? args[0];
      return invoke('model_delete', { id });
    }
    if (channel === 'model:test') return invoke('model_test', { req: args[0] });
    if (channel === 'model:setDefault') {
      const id = (args[0] as { id?: string })?.id ?? args[0];
      return invoke('model_set_default', { id });
    }
    if (channel === 'settings:get') return invoke('settings_get');
    if (channel === 'settings:set') return invoke('settings_set', { key: args[0], value: args[1] });
    return CHANNEL_FALLBACK[channel] ?? [];
  },
  send: () => undefined,
  on: () => undefined,
  removeListener: () => undefined,
  removeAllListeners: () => undefined,

  // ── Chat（对接 Rust AgentBridge） ─────────────────────
  chatSubmit: (req: ChatSubmitReq): Promise<{ taskId: string }> => {
    return invoke('chat_submit', {
      workspaceId: undefined, // 默认 workspace，后续接入 workspace 选择
      goal: req.goal,
      // 两阶段协议 + 附件模式（交互补设计 §3）
      attachments: req.attachments ?? [],
    }) as Promise<{ taskId: string }>;
  },

  // review/control/history 已接入 Rust 两阶段协议（核心闭环补齐）
  /** 审阅计划：approve/reject/modify（转移 #9/#10/#11） */
  chatReview: (taskId: string, action: 'approve' | 'reject' | 'modify', patch?: unknown[]): Promise<{ ok: boolean; status: string }> =>
    invoke('chat_review', { taskId, action, patch: patch ?? [] }) as Promise<{ ok: boolean; status: string }>,

  /** 任务控制：pause/resume/rollback_to（转移 #14/#21/#22） */
  chatControl: (req: ChatControlReq): Promise<{ ok: boolean; status: string }> =>
    invoke('chat_control', {
      taskId: req.taskId,
      action: req.action,
      stepId: req.stepId,
    }) as Promise<{ ok: boolean; status: string }>,

  /** 历史任务（tasks 表，含 14 态 meta） */
  chatHistory: (limit?: number): Promise<unknown[]> =>
    invoke('chat_history', { limit }) as Promise<unknown[]>,

  /** 附件暂存（交互补设计 §3.1） */
  fileStageAttachment: (srcPath: string, taskId?: string, workspaceId?: string): Promise<Record<string, unknown>> =>
    invoke('file_stage_attachment', { srcPath, taskId, workspaceId }) as Promise<Record<string, unknown>>,

  // ── 三层架构演进 P0 新增 command ──────────────────
  /** 取消任务（转移 #3/#8/#19/#23/#28 → cancelled） */
  chatCancel: (taskId: string, workspaceId?: string): Promise<{ taskId: string; cancelled: boolean }> =>
    invoke('chat_cancel', { taskId, workspaceId }) as Promise<{ taskId: string; cancelled: boolean }>,

  /** 任务 14 态状态（含 wait_reason/重试计数/成因码） */
  taskState: (taskId: string): Promise<Record<string, unknown>> =>
    invoke('task_state', { taskId }) as Promise<Record<string, unknown>>,

  /** 配额快照（四档预警 70/85/95/100%） */
  quotaStatus: (): Promise<QuotaSnapshot> => invoke('quota_status') as Promise<QuotaSnapshot>,

  /** 租约视图 */
  leaseInfo: (): Promise<{ holder: string; leases: LeaseInfo[] }> =>
    invoke('lease_info') as Promise<{ holder: string; leases: LeaseInfo[] }>,

  /** 接管请求（禁止抢占，需确认） */
  leaseTakeover: (taskId: string): Promise<LeaseTakeoverResp> =>
    invoke('lease_takeover', { taskId }) as Promise<LeaseTakeoverResp>,

  /** 事件重放（24h 持久化，seq > fromSeq） */
  eventReplay: (taskId: string, fromSeq = 0): Promise<{ taskId: string; events: unknown[] }> =>
    invoke('event_replay', { taskId, fromSeq }) as Promise<{ taskId: string; events: unknown[] }>,

  /** 订阅 chat-event 事件流（经 seq 守卫去重），返回取消订阅函数 */
  onChatEvent: (callback: (event: ChatEvent) => void): (() => void) => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const guarded = chatEventGuard.wrap(callback);

    listen<ChatEvent>('chat-event', (e) => {
      if (!disposed) guarded(e.payload);
    }).then((un) => {
      if (disposed) {
        un();
      } else {
        unlisten = un;
      }
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  },

  // ── System ─────────────────────────────────────────────
  systemInfo: () => invoke('system_info'),
  systemHealth: () => invoke('agent_status'),
};

/**
 * 未实现方法兜底：返回空数组（多数页面为列表查询），
 * 保证 UI 在 Rust 模块逐步落地期间不崩溃。
 */
export function createProxiedAPI<T extends object>(base: T): T {
  return new Proxy(base, {
    get(target, prop: string) {
      if (prop in target) {
        return (target as unknown as Record<string, unknown>)[prop];
      }
      return async () => [];
    },
  });
}
