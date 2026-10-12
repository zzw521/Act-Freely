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
import type {
  AuthLoginResp,
  AuthProfile,
  AuthStatus,
  LeaseInfo,
  LeaseTakeoverResp,
  QuotaSnapshot,
} from '../../shared/contracts';
import { EventStreamGuard } from './event-stream-guard';

/** 以 serverId 为唯一入参的 MCP 通道 → Rust command 名 */
const MCP_SERVER_ID_COMMANDS: Record<string, string> = {
  'mcp:removeServer': 'mcp_remove_server',
  'mcp:connect': 'mcp_connect',
  'mcp:disconnect': 'mcp_disconnect',
  'mcp:listTools': 'mcp_list_tools',
};

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
    if (channel === 'chat:conversations') {
      return invoke('chat_conversations', { limit: args[0] as number | undefined });
    }
    if (channel === 'chat:messages') {
      return invoke('chat_messages', {
        conversationId: args[0] as string,
        limit: args[1] as number | undefined,
      });
    }
    if (channel === 'chat:feedback') {
      return invoke('chat_feedback', {
        conversationId: args[0] as string,
        turnIndex: args[1] as number,
        // null 表示取消评价（Rust 侧删除该行评价，正文不动）
        feedback: (args[2] as string | null | undefined) ?? null,
      });
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
    if (channel === 'model:usage') {
      return invoke('model_usage', { period: args[0] as string });
    }
    // 用量总览 / 报表：与 model:usage 同一张 model_usage 台账，只是读法不同
    if (channel === 'usage:stats') {
      return invoke('usage_stats', { period: args[0] as string });
    }
    if (channel === 'usage:report') {
      return invoke('usage_report', { period: args[0] as string });
    }
    if (channel === 'settings:get') return invoke('settings_get');
    // 设置页以对象 patch 整体提交：逐键落库并触发系统行为联动（保持唤醒等）
    if (channel === 'settings:set') return invoke('settings_set_many', { patch: args[0] });
    if (channel === 'auth:status') return invoke('auth_status');
    if (channel === 'auth:login') return invoke('auth_login');
    if (channel === 'auth:logout') return invoke('auth_logout');
    // ── 安全与治理（PRD §4.7） ──
    if (channel === 'security:scanData') {
      return invoke('security_scan_data', { text: args[0] as string });
    }
    if (channel === 'security:getDomainControl') return invoke('security_get_domain_control');
    if (channel === 'security:setDomainControl') {
      return invoke('security_set_domain_control', { config: args[0] });
    }
    // 合规状态聚合 / 硬开关：与出域控制同一份存储，合规概览与出域控制表单看到的是同一个开关
    if (channel === 'compliance:status') return invoke('compliance_status');
    if (channel === 'compliance:update') {
      return invoke('compliance_update', { patch: args[0] });
    }
    if (channel === 'compliance:check') {
      return invoke('compliance_check', {
        text: args[0] as string,
        baseUrl: (args[1] as string | undefined) ?? null,
      });
    }
    if (channel === 'sandbox:getPolicy') {
      return invoke('sandbox_policy_get', { workspaceId: args[0] as string });
    }
    if (channel === 'sandbox:updatePolicy') {
      return invoke('sandbox_policy_set', { policy: args[0] });
    }
    if (channel === 'sandbox:envUsage') {
      return invoke('sandbox_env_usage');
    }
    if (channel === 'sandbox:envClear') {
      return invoke('sandbox_env_clear');
    }
    if (channel === 'audit:query') {
      return invoke('audit_log_query', { filters: args[0] ?? null });
    }
    if (channel === 'audit:export') {
      return invoke('audit_log_export', { format: args[0] as string, filters: args[1] ?? null });
    }
    // ── 定时任务（schedule_*：任务定义 + 执行记录 + 立即执行） ──
    if (channel === 'schedule:list') return invoke('schedule_list');
    if (channel === 'schedule:create') return invoke('schedule_create', { req: args[0] });
    if (channel === 'schedule:update') return invoke('schedule_update', { req: args[0] });
    if (channel === 'schedule:toggle') {
      return invoke('schedule_toggle', {
        scheduleId: args[0] as string,
        enabled: Boolean(args[1]),
      });
    }
    if (channel === 'schedule:delete') {
      return invoke('schedule_delete', { scheduleId: args[0] as string });
    }
    if (channel === 'schedule:runs') {
      return invoke('schedule_runs', {
        scheduleId: (args[0] as string | undefined) ?? null,
        limit: (args[1] as number | undefined) ?? null,
      });
    }
    if (channel === 'schedule:runNow') {
      return invoke('schedule_run_now', { scheduleId: args[0] as string });
    }
    // ── 工作空间（以 {data_home}/workspaces/* 目录为事实来源） ──
    if (channel === 'workspace:list') return invoke('workspace_list');
    if (channel === 'workspace:create') {
      const req = (args[0] ?? {}) as { workspaceId?: string; id?: string };
      return invoke('workspace_create', {
        workspaceId: req.workspaceId ?? req.id ?? '',
      });
    }
    // ── 技能（运行目录 skills/*/SKILL.md 为事实来源） ──
    if (channel === 'skill:root') return invoke('skill_root');
    if (channel === 'skill:list') return invoke('skill_list');
    if (channel === 'skill:install') return invoke('skill_install', { req: args[0] });
    if (channel === 'skill:uninstall') {
      const req = (args[0] ?? {}) as { skillId?: string; id?: string };
      return invoke('skill_uninstall', { skillId: req.skillId ?? req.id ?? '' });
    }
    if (channel === 'skill:toggle') {
      const req = (args[0] ?? {}) as { skillId?: string; id?: string; enabled?: boolean };
      return invoke('skill_toggle', {
        skillId: req.skillId ?? req.id ?? '',
        enabled: Boolean(req.enabled),
      });
    }
    // ── 专家套件（本地化安装：套件目录 manifest.json 为事实来源） ──
    if (channel === 'suite:root') return invoke('suite_root');
    if (channel === 'suite:list') return invoke('suite_list');
    if (channel === 'suite:install') return invoke('suite_install', { req: args[0] });
    if (channel === 'suite:uninstall') {
      const req = (args[0] ?? {}) as { suiteId?: string; id?: string };
      return invoke('suite_uninstall', { suiteId: req.suiteId ?? req.id ?? '' });
    }
    // ── MCP 连接器（配置落 SQLite mcp_servers，连接由 Rust 发起真实握手） ──
    if (channel === 'mcp:listServers') return invoke('mcp_list_servers');
    if (channel === 'mcp:addServer') return invoke('mcp_add_server', { req: args[0] });
    {
      // 其余四个通道只需连接器标识：removeServer / connect / disconnect / listTools
      const mcpCommand = MCP_SERVER_ID_COMMANDS[channel];
      if (mcpCommand) {
        const req = (args[0] ?? {}) as { serverId?: string; id?: string };
        return invoke(mcpCommand, { serverId: req.serverId ?? req.id ?? '' });
      }
    }
    // requested 由 Rust 从持久化设置读取（传入值仅 Mock/引擎直连通道使用）
    if (channel === 'keepAwake:status') return invoke('keep_awake_status');
    // 代理生效配置由 Rust 自读 settings_kv（传入值仅 Mock/引擎直连通道使用）
    if (channel === 'proxy:status') return invoke('proxy_status');
    // 开机自启：enabled 取系统启动项事实，requested 由 Rust 自读设置（入参仅本地通道用）
    if (channel === 'system:autostart') return invoke('system_autostart_status');
    // ── 系统权限（能力清单与设置面板深链由 Rust 按当前平台给出） ──
    if (channel === 'system:permission:status') return invoke('system_permission_status');
    if (channel === 'system:permission:open') {
      const req = (args[0] ?? {}) as { key?: string };
      return invoke('system_permission_open', { key: req.key ?? '' });
    }
    // ── 对齐整改通道（Hooks / 连接器授权 / 任务归档 / 电脑操控策略） ──
    if (channel === 'hooks:get') return invoke('hooks_get');
    if (channel === 'hooks:set') return invoke('hooks_set', { config: args[0] });
    if (channel === 'hooks:test') {
      const req = (args[0] ?? {}) as { event?: string; command?: string; timeout?: number };
      return invoke('hooks_test', {
        event: req.event ?? '',
        command: req.command ?? '',
        timeout: req.timeout ?? null,
      });
    }
    if (channel === 'connector:list') return invoke('connector_list');
    if (channel === 'connector:authorize') {
      const req = (args[0] ?? {}) as { connectorId?: string; category?: string; serverRef?: string };
      return invoke('connector_authorize', {
        connectorId: req.connectorId ?? '',
        category: req.category ?? null,
        serverRef: req.serverRef ?? null,
      });
    }
    if (channel === 'connector:revoke') {
      const req = (args[0] ?? {}) as { connectorId?: string };
      return invoke('connector_revoke', { connectorId: req.connectorId ?? '' });
    }
    if (channel === 'task:archive') {
      const req = (args[0] ?? {}) as { taskId?: string; workspaceId?: string };
      return invoke('task_archive', {
        taskId: req.taskId ?? '',
        workspaceId: req.workspaceId ?? null,
      });
    }
    if (channel === 'task:archiveList') return invoke('task_archive_list');
    if (channel === 'task:unarchive' || channel === 'task:archiveDelete') {
      const req = (args[0] ?? {}) as { taskId?: string };
      const taskId = req.taskId ?? '';
      return invoke(
        channel === 'task:unarchive' ? 'task_unarchive' : 'task_archive_delete',
        { taskId },
      );
    }
    if (channel === 'task:archiveClear') {
      const req = (args[0] ?? {}) as { confirm?: string };
      return invoke('task_archive_clear', { confirm: req.confirm ?? '' });
    }
    if (channel === 'computerUse:getPolicy') return invoke('computer_use_get_policy');
    if (channel === 'computerUse:setPolicy') {
      const req = (args[0] ?? {}) as { policy?: string };
      return invoke('computer_use_set_policy', { policy: req.policy ?? '' });
    }
    // ── 意识（awareness_*：画像 + 四个能力文件 + 长期记忆条目，落盘 ~/.tongtianwork/awareness/main） ──
    if (channel === 'awareness:get') return invoke('awareness_get');
    if (channel === 'awareness:update') return invoke('awareness_update', { patch: args[0] ?? {} });
    if (channel === 'awareness:memories') return invoke('awareness_memories');
    if (channel === 'awareness:memoryAdd') {
      const req = (args[0] ?? {}) as { content?: string; source?: string };
      return invoke('awareness_memory_add', {
        content: req.content ?? '',
        source: req.source ?? null,
      });
    }
    if (channel === 'awareness:memoryDelete') {
      const req = (args[0] ?? {}) as { id?: string };
      return invoke('awareness_memory_delete', { id: req.id ?? '' });
    }
    // 未登记的通道：列表类查询返回空数组（不再为已落地通道保留兜底值，避免假数据）
    return [];
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
      // 会话正文落库定位键（Rust chat_messages 按 会话+轮次 存正文）
      conversationId: req.conversationId,
      turnIndex: req.turnIndex,
      // 对话内选择项：曾在此被静默丢弃（选了也不生效），现在一路透传到引擎请求体
      modelBinding: req.modelBinding,
      modelConfig: req.modelConfig,
      workDir: req.workDir,
      history: req.history,
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

  // ── Auth（账号与登录态 · 网页验证登录 Loopback 方案） ──────
  /** 登录态视图（settings_kv profile + keyring token） */
  authStatus: (): Promise<AuthStatus> => invoke('auth_status') as Promise<AuthStatus>,

  /** 登录：Mock 直登；真实模式拉起浏览器网页登录 + loopback 回调 */
  authLogin: (): Promise<AuthLoginResp> => invoke('auth_login') as Promise<AuthLoginResp>,

  /** 退出：尽力远端吊销 + 清理 keyring/profile（本地数据保留） */
  authLogout: (): Promise<{ ok: boolean; wasLoggedIn: boolean }> =>
    invoke('auth_logout') as Promise<{ ok: boolean; wasLoggedIn: boolean }>,

  /** 订阅登录态变更事件（auth-event），返回取消订阅函数 */
  onAuthEvent: (callback: (event: { type: 'login' | 'logout'; profile?: AuthProfile }) => void): (() => void) => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listen<{ type: 'login' | 'logout'; profile?: AuthProfile }>('auth-event', (e) => {
      if (!disposed) callback(e.payload);
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

  // ── System ─────────────────────────────────────
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
