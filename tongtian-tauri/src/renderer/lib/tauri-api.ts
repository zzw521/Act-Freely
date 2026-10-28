/**
 * 通天办公 (Tauri 版) - Tauri command/event 适配层
 *
 * 在 Tauri 容器内运行时，将 Rust command（invoke）与
 * 事件流（listen: chat-event / agent-status）适配为
 * 渲染进程统一的 tongtianAPI 接口。
 */

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { ChatEvent, CheckpointInfo, ChatSubmitReq } from '../../shared/types';
import type {
  AuthLoginResp,
  AuthProfile,
  AuthStatus,
} from '../../shared/contracts';
import { IPC_CHANNELS } from '../../shared/contracts';
import { EventStreamGuard } from './event-stream-guard';

/** 以 serverId 为唯一入参的 MCP 通道 → Rust command 名 */
const MCP_SERVER_ID_COMMANDS: Record<string, string> = {
  'mcp:removeServer': 'mcp_remove_server',
  'mcp:connect': 'mcp_connect',
  'mcp:disconnect': 'mcp_disconnect',
  'mcp:listTools': 'mcp_list_tools',
  'mcp:oauthAuthorize': 'mcp_oauth_authorize',
  'mcp:oauthRevoke': 'mcp_oauth_revoke',
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
    // 引擎端点按 workspace 解析真实端口（第二个实例可能在 18001，不能写死 18000）
    if (channel === IPC_CHANNELS.ENGINE_ENDPOINT) {
      return invoke('engine_endpoint', {
        workspaceId: (args[0] as string | undefined) ?? null,
      });
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
    if (channel === 'chat:editTurn') {
      return invoke('chat_edit_turn', {
        conversationId: args[0] as string,
        turnIndex: args[1] as number,
        content: args[2] as string,
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
    if (channel === 'file:openExternal') {
      return invoke('file_open_external', { path: args[0] as string });
    }
    if (channel === 'model:list') return invoke('model_list');
    if (channel === 'model:create') return invoke('model_add', { req: args[0] });
    if (channel === 'model:update') return invoke('model_update', { req: args[0] });
    if (channel === 'model:delete') {
      const id = (args[0] as { id?: string })?.id ?? args[0];
      return invoke('model_delete', { id });
    }
    if (channel === 'model:test') return invoke('model_test', { req: args[0] });
    if (channel === 'model:detectLocal') {
      return invoke('model_detect_local', { baseUrl: (args[0] as string | undefined) ?? null });
    }
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
    // 任务栏未读角标（Windows 图标叠加 / macOS Dock 计数）
    if (channel === 'badge:support') return invoke('badge_support');
    if (channel === 'badge:set') return invoke('badge_set', { count: Number(args[0] ?? 0) });
    // 品牌外观：单次自定义名称与 PNG 图标（返回本次真实生效范围）
    if (channel === 'branding:get') return invoke('branding_get');
    if (channel === 'branding:customize') return invoke('branding_customize', { req: args[0] });
    // 独立任务窗口（popWindow 实验特性）
    if (channel === 'task:openWindow') {
      return invoke('open_task_window', {
        conversationId: args[0] as string,
        title: (args[1] as string | undefined) ?? null,
      });
    }
    if (channel === 'auth:status') return invoke('auth_status');
    if (channel === 'auth:login') return invoke('auth_login');
    if (channel === 'auth:logout') return invoke('auth_logout');
    if (channel === 'auth:serverProbe') return invoke('auth_server_probe');
    // ── 项目协作（跨用户/跨机器；令牌只在 Rust 内存，渲染层拿不到） ──
    if (channel === 'collab:status') return invoke('collab_status');
    if (channel === 'collab:listProjects') return invoke('collab_list_projects');
    if (channel === 'collab:createProject') {
      const req = (args[0] ?? {}) as { name?: string; description?: string };
      return invoke('collab_create_project', {
        name: req.name ?? '',
        description: req.description ?? null,
      });
    }
    if (channel === 'collab:projectDetail') {
      return invoke('collab_project_detail', { projectId: args[0] as string });
    }
    if (channel === 'collab:createInvite') {
      const req = (args[0] ?? {}) as { projectId?: string; email?: string; role?: string };
      return invoke('collab_create_invite', {
        projectId: req.projectId ?? '',
        email: req.email ?? '',
        role: req.role ?? 'member',
      });
    }
    if (channel === 'collab:listInvites') {
      return invoke('collab_list_invites', { projectId: args[0] as string });
    }
    if (channel === 'collab:acceptInvite') {
      return invoke('collab_accept_invite', { token: args[0] as string });
    }
    if (channel === 'collab:updateMemberRole') {
      const req = (args[0] ?? {}) as { projectId?: string; uid?: string; role?: string };
      return invoke('collab_update_member_role', {
        projectId: req.projectId ?? '',
        uid: req.uid ?? '',
        role: req.role ?? '',
      });
    }
    if (channel === 'collab:removeMember') {
      const req = (args[0] ?? {}) as { projectId?: string; uid?: string };
      return invoke('collab_remove_member', {
        projectId: req.projectId ?? '',
        uid: req.uid ?? '',
      });
    }
    if (channel === 'collab:transferOwner') {
      const req = (args[0] ?? {}) as { projectId?: string; uid?: string };
      return invoke('collab_transfer_owner', {
        projectId: req.projectId ?? '',
        uid: req.uid ?? '',
      });
    }
    if (channel === 'collab:leaveProject') {
      return invoke('collab_leave_project', { projectId: args[0] as string });
    }
    if (channel === 'collab:createTask') {
      const req = (args[0] ?? {}) as {
        projectId?: string;
        title?: string;
        detail?: string;
        assigneeUid?: string | null;
      };
      return invoke('collab_create_task', {
        projectId: req.projectId ?? '',
        title: req.title ?? '',
        detail: req.detail ?? null,
        assigneeUid: req.assigneeUid ?? null,
      });
    }
    if (channel === 'collab:updateTask') {
      const req = (args[0] ?? {}) as { projectId?: string; taskId?: string; patch?: unknown };
      return invoke('collab_update_task', {
        projectId: req.projectId ?? '',
        taskId: req.taskId ?? '',
        patch: req.patch ?? {},
      });
    }
    if (channel === 'collab:addComment') {
      const req = (args[0] ?? {}) as { projectId?: string; taskId?: string; body?: string };
      return invoke('collab_add_comment', {
        projectId: req.projectId ?? '',
        taskId: req.taskId ?? '',
        body: req.body ?? '',
      });
    }
    if (channel === 'collab:reportResult') {
      const req = (args[0] ?? {}) as {
        projectId?: string;
        taskId?: string;
        version?: number;
        summary?: string;
        artifacts?: string[];
        machine?: string;
        status?: string;
      };
      return invoke('collab_report_result', {
        projectId: req.projectId ?? '',
        taskId: req.taskId ?? '',
        version: req.version ?? 0,
        summary: req.summary ?? '',
        artifacts: req.artifacts ?? [],
        machine: req.machine ?? '',
        status: req.status ?? 'review',
      });
    }
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
    if (channel === 'sandbox:systemGet') {
      return invoke('sandbox_system_get');
    }
    if (channel === 'sandbox:systemSet') {
      // 入参是 { enabled, network } 对象：按位置读会把对象当成 true，关不掉开关
      const req = (args[0] ?? {}) as { enabled?: boolean; network?: boolean };
      return invoke('sandbox_system_set', {
        enabled: Boolean(req.enabled),
        network: Boolean(req.network),
      });
    }
    if (channel === 'audit:query') {
      return invoke('audit_log_query', { filters: args[0] ?? null });
    }
    if (channel === 'audit:export') {
      return invoke('audit_log_export', { format: args[0] as string, filters: args[1] ?? null });
    }
    // ── 定时任务（schedule_*：任务定义 + 执行记录 + 立即执行） ──
    if (channel === 'schedule:list') return invoke('schedule_list');
    if (channel === 'schedule:parse') return invoke('schedule_parse', { text: String(args[0] ?? '') });
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
    // ── 插件 SDK（安装前预检读取 ZIP 元数据，仅桌面端可做） ──
    if (channel === 'plugin:validate') return invoke('plugin_validate', { req: args[0] });
    if (channel === 'plugin:template') {
      const req = (args[0] ?? {}) as { name?: string };
      return invoke('plugin_template', { name: req.name ?? null });
    }
    // ── 在线市场（索引可配置；HTTPS/公网校验、SHA-256 与落盘都在 Rust） ──
    if (channel === 'market:configGet') return invoke('market_config_get');
    if (channel === 'market:configSet') return invoke('market_config_set', { req: args[0] });
    if (channel === 'market:skillList') return invoke('market_skill_list');
    if (channel === 'market:skillInstall') {
      const req = (args[0] ?? {}) as { itemId?: string; id?: string };
      return invoke('market_skill_install', { req: { itemId: req.itemId ?? req.id ?? '' } });
    }
    if (channel === 'market:suiteList') return invoke('market_suite_list');
    if (channel === 'market:suiteInstall') {
      const req = (args[0] ?? {}) as { itemId?: string; id?: string };
      return invoke('market_suite_install', { req: { itemId: req.itemId ?? req.id ?? '' } });
    }
    if (channel === 'market:trustGet') return invoke('market_trust_get');
    if (channel === 'market:trustAdd') return invoke('market_trust_add', { req: args[0] });
    if (channel === 'market:trustRemove') return invoke('market_trust_remove', { req: args[0] });
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
    // 内置工具清单：引擎在跑时 Rust 读它的注册表，否则回落到同源声明值
    if (channel === 'agent:builtinTools') return invoke('agent_builtin_tools');
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
    if (channel === 'hooks:grant') {
      const req = (args[0] ?? {}) as { command?: string; event?: string };
      return invoke('hooks_grant', {
        command: req.command ?? '',
        event: req.event ?? '',
      });
    }
    if (channel === 'hooks:revoke') {
      const req = (args[0] ?? {}) as { command?: string };
      return invoke('hooks_revoke', { command: req.command ?? '' });
    }
    if (channel === 'hooks:status') return invoke('hooks_status');
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
    if (channel === 'computerUse:capabilities') return invoke('computer_use_capabilities');
    if (channel === 'computerUse:capture') return invoke('computer_use_capture');
    if (channel === 'remote:capabilities') return invoke('remote_desktop_capabilities');
    if (channel === 'remote:status') return invoke('remote_desktop_status');
    if (channel === 'remote:start') return invoke('remote_desktop_start');
    if (channel === 'remote:frame') return invoke('remote_desktop_frame');
    if (channel === 'remote:stop') return invoke('remote_desktop_stop');
    if (channel === 'browserUse:getPolicy') return invoke('browser_use_get_policy');
    if (channel === 'browserUse:setPolicy') {
      const req = (args[0] ?? {}) as { policy?: string };
      return invoke('browser_use_set_policy', { policy: req.policy ?? '' });
    }
    if (channel === 'browserUse:capabilities') return invoke('browser_use_capabilities');
    if (channel === 'browserUse:start') return invoke('browser_use_start');
    if (channel === 'browserUse:stop') return invoke('browser_use_stop');
    // ── 终端 AI+（显式开关 + 管道 shell；输入与建议分开留痕） ──
    if (channel === 'terminal:getPolicy') return invoke('terminal_get_policy');
    if (channel === 'terminal:setPolicy') {
      const req = (args[0] ?? {}) as { enabled?: boolean };
      return invoke('terminal_set_policy', { enabled: Boolean(req.enabled) });
    }
    if (channel === 'terminal:capabilities') return invoke('terminal_capabilities');
    if (channel === 'terminal:open') {
      const req = (args[0] ?? {}) as { cwd?: string; workspaceId?: string | null };
      return invoke('terminal_open', {
        cwd: req.cwd ?? '',
        workspaceId: req.workspaceId ?? null,
      });
    }
    if (channel === 'terminal:input') {
      const req = (args[0] ?? {}) as { sessionId?: string; data?: string; source?: string };
      return invoke('terminal_input', {
        sessionId: req.sessionId ?? '',
        data: req.data ?? '',
        source: req.source ?? 'manual',
      });
    }
    if (channel === 'terminal:read') {
      const req = (args[0] ?? {}) as { sessionId?: string; offset?: number };
      return invoke('terminal_read', {
        sessionId: req.sessionId ?? '',
        offset: req.offset ?? 0,
      });
    }
    if (channel === 'terminal:close') {
      const req = (args[0] ?? {}) as { sessionId?: string };
      return invoke('terminal_close', { sessionId: req.sessionId ?? '' });
    }
    if (channel === 'terminal:ai') {
      const req = (args[0] ?? {}) as {
        sessionId?: string;
        question?: string;
        modelId?: string | null;
      };
      return invoke('terminal_ai', {
        sessionId: req.sessionId ?? '',
        question: req.question ?? '',
        modelId: req.modelId ?? null,
      });
    }
    // ── ACP（外部编码 Agent 双向接入） ──
    if (channel === 'acp:getConfig') return invoke('acp_get_config');
    if (channel === 'acp:setConfig') {
      const req = (args[0] ?? {}) as {
        enabled?: boolean;
        command?: string;
        permission?: string;
        timeoutSeconds?: number;
      };
      return invoke('acp_set_config', {
        enabled: Boolean(req.enabled),
        command: req.command ?? '',
        permission: req.permission ?? 'deny',
        timeoutSeconds: req.timeoutSeconds ?? 600,
      });
    }
    if (channel === 'acp:status') return invoke('acp_status');
    // ── 本地知识库（knowledge_*：目录是事实来源；删除为软删除） ──
    if (channel === 'knowledge:list') return invoke('knowledge_list');
    if (channel === 'knowledge:create') {
      const req = (args[0] ?? {}) as { name?: string; description?: string };
      return invoke('knowledge_create', {
        name: req.name ?? '',
        description: req.description ?? null,
      });
    }
    if (channel === 'knowledge:update') {
      const req = (args[0] ?? {}) as {
        kbId?: string;
        name?: string;
        description?: string;
        enabled?: boolean;
      };
      return invoke('knowledge_update', {
        kbId: req.kbId ?? '',
        name: req.name ?? null,
        description: req.description ?? null,
        enabled: req.enabled ?? null,
      });
    }
    if (channel === 'knowledge:delete') {
      return invoke('knowledge_delete', {
        kbId: String((args[0] as { kbId?: string })?.kbId ?? ''),
      });
    }
    if (channel === 'knowledge:addDocs') {
      const req = (args[0] ?? {}) as { kbId?: string; paths?: string[] };
      return invoke('knowledge_add_docs', { kbId: req.kbId ?? '', paths: req.paths ?? [] });
    }
    if (channel === 'knowledge:removeDoc') {
      const req = (args[0] ?? {}) as { kbId?: string; doc?: string };
      return invoke('knowledge_remove_doc', { kbId: req.kbId ?? '', doc: req.doc ?? '' });
    }
    if (channel === 'knowledge:openDir') {
      return invoke('knowledge_open_dir', {
        kbId: (args[0] as { kbId?: string | null } | undefined)?.kbId ?? null,
      });
    }
    // ── 幻灯片模板库（内置主题 + 本机 .pptx 导入；删除为软删除） ──
    if (channel === 'pptx:templates') return invoke('pptx_templates_list');
    if (channel === 'pptx:templateImport') {
      return invoke('pptx_template_import', {
        path: String((args[0] as { path?: string } | undefined)?.path ?? ''),
      });
    }
    if (channel === 'pptx:templateDelete') {
      return invoke('pptx_template_delete', {
        templateId: String((args[0] as { templateId?: string } | undefined)?.templateId ?? ''),
      });
    }
    // ── 专家团队（注册表落 settings_kv；密钥只在 Rust 侧解析） ──
    if (channel === 'experts:list') return invoke('experts_list');
    if (channel === 'experts:save') return invoke('experts_save', { req: args[0] ?? {} });
    if (channel === 'experts:delete') {
      return invoke('experts_delete', {
        expertId: String((args[0] as { expertId?: string })?.expertId ?? ''),
      });
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
    // ── IM 频道（im_*：Rust 代理引擎 /api/im/*，长连接只在引擎进程里） ──
    if (channel === 'im:channels') return invoke('im_channels');
    if (channel === 'im:configure') {
      const req = (args[0] ?? {}) as { kind?: string; credentials?: Record<string, string> };
      return invoke('im_configure', {
        kind: req.kind ?? '',
        credentials: req.credentials ?? {},
      });
    }
    if (channel === 'im:start') return invoke('im_start', { kind: String((args[0] as { kind?: string })?.kind ?? '') });
    if (channel === 'im:stop') return invoke('im_stop', { kind: String((args[0] as { kind?: string })?.kind ?? '') });
    if (channel === 'im:forget') return invoke('im_forget', { kind: String((args[0] as { kind?: string })?.kind ?? '') });
    if (channel === 'im:send') {
      const req = (args[0] ?? {}) as { kind?: string; target?: string; text?: string };
      return invoke('im_send', {
        kind: req.kind ?? '',
        target: req.target ?? '',
        text: req.text ?? '',
      });
    }
    if (channel === 'im:preferencesGet') return invoke('im_preferences_get');
    if (channel === 'im:preferencesSet') {
      const req = (args[0] ?? {}) as { preferences?: Record<string, boolean> };
      return invoke('im_preferences_set', { preferences: req.preferences ?? {} });
    }
    // 未登记的通道必须显式失败：静默返回空数组会让页面把「没有数据」当成事实
    throw new Error(
      `IPC_CHANNEL_UNHANDLED: 桌面通道未实现 ${channel}（已阻止静默返回空结果）`,
    );
  },
  send: () => undefined,
  on: () => undefined,
  removeListener: () => undefined,
  removeAllListeners: () => undefined,

  // ── Chat（对接 Rust AgentBridge） ─────────────────────
  chatSubmit: (req: ChatSubmitReq): Promise<{ taskId: string }> => {
    return invoke('chat_submit', {
      // 当前选择的工作空间：不同 workspace 有独立引擎实例与落盘目录
      workspaceId: req.workspaceId,
      goal: req.goal,
      // 工作台模式：随任务下发，引擎据此装配系统提示与生成工具（不是纯前端样式）
      mode: req.mode,
      // 两阶段协议 + 附件模式（交互补设计 §3）
      attachments: req.attachments ?? [],
      // 会话正文落库定位键（Rust chat_messages 按 会话+轮次 存正文）
      conversationId: req.conversationId,
      turnIndex: req.turnIndex,
      // 对话内选择项：曾在此被静默丢弃（选了也不生效），现在一路透传到引擎请求体。
      // 只传模型 id，密钥由 Rust 从 keyring 补齐后随引擎请求下发。
      modelBinding: req.modelBinding,
      modelId: req.modelId,
      // 本轮负责人专家：只传注册表 id，提示词与模型绑定由 Rust/引擎解析
      expertId: req.expertId,
      workDir: req.workDir,
      history: req.history,
    }) as Promise<{ taskId: string }>;
  },

  // review/history 已接入 Rust 两阶段协议（核心闭环补齐）
  /** 审阅计划：approve/reject/modify（转移 #9/#10/#11） */
  chatReview: (taskId: string, action: 'approve' | 'reject' | 'modify' | 'resume', patch?: unknown[]): Promise<{ ok: boolean; status: string }> =>
    invoke('chat_review', { taskId, action, patch: patch ?? [] }) as Promise<{ ok: boolean; status: string }>,

  /** 历史任务（tasks 表，含 14 态 meta） */
  chatHistory: (limit?: number): Promise<unknown[]> =>
    invoke('chat_history', { limit }) as Promise<unknown[]>,

  /** 批量查询检查点摘要：刷新/重启后据此恢复续跑入口（引擎侧只返回摘要，不含正文与密钥） */
  chatCheckpointQuery: (
    taskIds: string[],
    workspaceId?: string,
  ): Promise<{ checkpoints: Record<string, CheckpointInfo> }> =>
    invoke('chat_checkpoint_query', { taskIds, workspaceId }) as Promise<{
      checkpoints: Record<string, CheckpointInfo>;
    }>,

  /** 附件暂存（交互补设计 §3.1） */
  fileStageAttachment: (srcPath: string, taskId?: string, workspaceId?: string): Promise<Record<string, unknown>> =>
    invoke('file_stage_attachment', { srcPath, taskId, workspaceId }) as Promise<Record<string, unknown>>,

  // ── 三层架构演进 P0 新增 command ──────────────────
  /** 取消任务（转移 #3/#8/#19/#23/#28 → cancelled） */
  chatCancel: (
    taskId: string,
    workspaceId?: string,
  ): Promise<{ taskId: string; cancelled: boolean; status?: string }> =>
    invoke('chat_cancel', { taskId, workspaceId }) as Promise<{
      taskId: string;
      cancelled: boolean;
      status?: string;
    }>,

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
};

/**
 * 未实现方法兜底：显式抛 IPC_CHANNEL_UNHANDLED。
 * 不能返回空数组 —— 页面会把「没实现」误读成「没有数据」，问题被静默吞掉。
 */
export function createProxiedAPI<T extends object>(base: T): T {
  return new Proxy(base, {
    get(target, prop: string) {
      if (prop in target) {
        return (target as unknown as Record<string, unknown>)[prop];
      }
      return async () => {
        throw new Error(
          `IPC_CHANNEL_UNHANDLED: 桌面 API 未实现 ${String(prop)}（已阻止静默返回空结果）`,
        );
      };
    },
  });
}
