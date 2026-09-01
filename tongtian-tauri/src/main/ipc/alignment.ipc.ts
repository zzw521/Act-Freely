// ═══════════════════════════════════════════════════════════
// src/main/ipc/alignment.ipc.ts — 千问对标整改 IPC Handlers
// spec §6.3：settings / hooks / awareness / connector / archive / computer-use
// ═══════════════════════════════════════════════════════════

import { ipcMain } from 'electron';
import { z } from 'zod';
import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  AppSettingsData,
  AwarenessProfile,
  ConnectorState,
  HookEventName,
  HookInput,
} from '../../shared/contracts';
import Logger from '../util/logger';
import Database from '../store/db';
import SettingsStore from '../store/settings-store';
import HookRunner, { ALL_HOOK_EVENTS } from '../hooks/hook-runner';
import AwarenessService from '../awareness/service';

const logger = Logger.getInstance();

type SafeHandler = (event: Electron.IpcMainInvokeEvent, ...args: unknown[]) => Promise<unknown> | unknown;

function safeHandler(module: string, handler: SafeHandler): SafeHandler {
  return async (event, ...args) => {
    try {
      return await handler(event, ...args);
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : String(error);
      logger.error('IPC handler error', module, { error: msg });
      throw error;
    }
  };
}

// ── Zod Schemas ─────────────────────────────────────────────

const hookEventSchema = z.enum(ALL_HOOK_EVENTS as [HookEventName, ...HookEventName[]]);

const hookConfigSchema = z.object({
  hooks: z.record(
    hookEventSchema,
    z.array(
      z.object({
        matcher: z.string().optional(),
        hooks: z.array(
          z.object({
            type: z.literal('command'),
            command: z.string().min(1),
            timeout: z.number().positive().optional(),
          }),
        ),
      }),
    ),
  ),
});

// ── Registration ────────────────────────────────────────────

export function registerAlignmentIpc(): () => void {
  const registered: string[] = [];

  const handle = (channel: string, module: string, fn: SafeHandler) => {
    ipcMain.handle(channel, safeHandler(module, fn) as (event: Electron.IpcMainInvokeEvent, ...args: unknown[]) => Promise<unknown>);
    registered.push(channel);
  };

  // ── Settings ────────────────────────────────────────────

  handle(IPC_CHANNELS.SETTINGS_GET, 'ipc/settings', () => SettingsStore.getInstance().getAll());

  handle(IPC_CHANNELS.SETTINGS_SET, 'ipc/settings', (_e, args) => {
    const patch = (args ?? {}) as Partial<AppSettingsData>;
    return SettingsStore.getInstance().update(patch);
  });

  // ── Hooks ───────────────────────────────────────────────

  handle(IPC_CHANNELS.HOOKS_GET, 'ipc/hooks', () => HookRunner.getInstance().getConfig());

  handle(IPC_CHANNELS.HOOKS_SET, 'ipc/hooks', (_e, args) => {
    const config = hookConfigSchema.parse(args);
    HookRunner.getInstance().setConfig(config);
    return { ok: true };
  });

  handle(IPC_CHANNELS.HOOKS_TEST, 'ipc/hooks', async (_e, args) => {
    const { event, input, command, timeout } = z
      .object({
        event: hookEventSchema,
        input: z.record(z.unknown()).optional(),
        command: z.string().min(1),
        timeout: z.number().positive().optional(),
      })
      .parse(args);
    const hookInput: HookInput = {
      session_id: 'test-session',
      cwd: process.cwd(),
      hook_event_name: event,
      ...(input ?? {}),
    };
    return HookRunner.getInstance().testRun(event, hookInput, command, timeout ?? 60);
  });

  // ── Awareness ───────────────────────────────────────────

  handle(IPC_CHANNELS.AWARENESS_GET, 'ipc/awareness', () => AwarenessService.getInstance().getProfile());

  handle(IPC_CHANNELS.AWARENESS_UPDATE, 'ipc/awareness', (_e, args) => {
    const patch = z
      .object({
        mode: z.boolean().optional(),
        autoMemory: z.boolean().optional(),
        style: z.enum(['professional', 'friendly', 'concise', 'custom']).optional(),
        customPrompt: z.string().optional(),
        workManual: z.string().optional(),
      })
      .parse(args ?? {});
    return AwarenessService.getInstance().updateProfile(patch as Partial<AwarenessProfile>);
  });

  handle(IPC_CHANNELS.AWARENESS_MEMORIES, 'ipc/awareness', (_e, args) => {
    const { source } = z.object({ source: z.string().optional() }).parse(args ?? {});
    return AwarenessService.getInstance().listMemories(source);
  });

  handle(IPC_CHANNELS.AWARENESS_MEMORY_ADD, 'ipc/awareness', (_e, args) => {
    const { content } = z.object({ content: z.string().min(1).max(2000) }).parse(args);
    return AwarenessService.getInstance().addMemory(content, 'manual');
  });

  handle(IPC_CHANNELS.AWARENESS_MEMORY_DELETE, 'ipc/awareness', (_e, args) => {
    const { id } = z.object({ id: z.string() }).parse(args);
    return { ok: AwarenessService.getInstance().deleteMemory(id) };
  });

  // ── Connectors（授权状态持久化） ────────────────────────

  handle(IPC_CHANNELS.CONNECTOR_LIST, 'ipc/connector', () => {
    const db = Database.getInstance().getDb();
    const rows = db.prepare('SELECT * FROM connector_states ORDER BY connector_id').all() as Record<string, unknown>[];
    return rows.map((r): ConnectorState => ({
      connectorId: String(r.connector_id),
      category: (r.category as 'builtin' | 'market') ?? 'builtin',
      installed: Number(r.installed) === 1,
      authStatus: (r.auth_status as ConnectorState['authStatus']) ?? 'none',
      serverRef: r.server_ref ? String(r.server_ref) : undefined,
      updatedAt: String(r.updated_at ?? ''),
    }));
  });

  handle(IPC_CHANNELS.CONNECTOR_AUTHORIZE, 'ipc/connector', (_e, args) => {
    const { connectorId, category } = z
      .object({ connectorId: z.string().min(1), category: z.enum(['builtin', 'market']).optional() })
      .parse(args);
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO connector_states (connector_id, category, installed, auth_status, updated_at)
       VALUES (?, ?, 1, 'authorized', datetime('now'))
       ON CONFLICT(connector_id) DO UPDATE SET installed = 1, auth_status = 'authorized', updated_at = datetime('now')`,
    ).run(connectorId, category ?? 'builtin');
    logger.info('connector authorized', 'connector', { connectorId });
    return { connectorId, installed: true, authStatus: 'authorized' };
  });

  handle(IPC_CHANNELS.CONNECTOR_REVOKE, 'ipc/connector', (_e, args) => {
    const { connectorId } = z.object({ connectorId: z.string().min(1) }).parse(args);
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO connector_states (connector_id, category, installed, auth_status, updated_at)
       VALUES (?, 'builtin', 0, 'none', datetime('now'))
       ON CONFLICT(connector_id) DO UPDATE SET installed = 0, auth_status = 'none', updated_at = datetime('now')`,
    ).run(connectorId);
    logger.info('connector revoked', 'connector', { connectorId });
    return { ok: true };
  });

  // ── 任务归档 ────────────────────────────────────────────

  handle(IPC_CHANNELS.TASK_ARCHIVE_LIST, 'ipc/archive', () => {
    const db = Database.getInstance().getDb();
    const rows = db.prepare(
      `SELECT a.task_id, a.workspace_id, a.archived_at, COALESCE(t.title, '(已删除任务)') AS title
       FROM archived_tasks a LEFT JOIN tasks t ON t.id = a.task_id
       ORDER BY a.archived_at DESC`,
    ).all() as Record<string, unknown>[];
    return rows.map((r) => ({
      taskId: String(r.task_id),
      title: String(r.title),
      workspaceId: String(r.workspace_id),
      archivedAt: String(r.archived_at),
    }));
  });

  handle(IPC_CHANNELS.TASK_ARCHIVE, 'ipc/archive', (_e, args) => {
    const { taskId, workspaceId } = z
      .object({ taskId: z.string().min(1), workspaceId: z.string().optional() })
      .parse(args);
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO archived_tasks (task_id, workspace_id, archived_at) VALUES (?, ?, datetime('now'))
       ON CONFLICT(task_id) DO UPDATE SET archived_at = datetime('now')`,
    ).run(taskId, workspaceId ?? 'default');
    logger.info('task archived', 'archive', { taskId });
    return { ok: true };
  });

  handle(IPC_CHANNELS.TASK_UNARCHIVE, 'ipc/archive', (_e, args) => {
    const { taskId } = z.object({ taskId: z.string().min(1) }).parse(args);
    const db = Database.getInstance().getDb();
    db.prepare('DELETE FROM archived_tasks WHERE task_id = ?').run(taskId);
    return { ok: true };
  });

  handle(IPC_CHANNELS.TASK_ARCHIVE_DELETE, 'ipc/archive', (_e, args) => {
    // 彻底删除：从归档移除并删除任务本体（含级联删除 messages/file_ops）
    const { taskId } = z.object({ taskId: z.string().min(1) }).parse(args);
    const db = Database.getInstance().getDb();
    db.prepare('DELETE FROM archived_tasks WHERE task_id = ?').run(taskId);
    db.prepare('DELETE FROM tasks WHERE id = ?').run(taskId);
    logger.info('archived task deleted', 'archive', { taskId });
    return { ok: true };
  });

  handle(IPC_CHANNELS.TASK_ARCHIVE_CLEAR, 'ipc/archive', (_e, args) => {
    const { confirm } = z.object({ confirm: z.string() }).parse(args);
    if (confirm !== 'DELETE') return { ok: false, reason: 'confirm word mismatch' };
    const db = Database.getInstance().getDb();
    db.prepare('DELETE FROM archived_tasks').run();
    logger.info('archive cleared', 'archive');
    return { ok: true };
  });

  // ── 电脑操控策略（存于 settings） ───────────────────────

  handle(IPC_CHANNELS.COMPUTER_USE_GET_POLICY, 'ipc/computer-use', () => ({
    policy: SettingsStore.getInstance().getAll().computerUsePolicy,
  }));

  handle(IPC_CHANNELS.COMPUTER_USE_SET_POLICY, 'ipc/computer-use', (_e, args) => {
    const { policy } = z
      .object({ policy: z.enum(['ask_every_time', 'auto', 'disabled']) })
      .parse(args);
    SettingsStore.getInstance().update({ computerUsePolicy: policy });
    return { ok: true };
  });

  logger.info(`alignment IPC registered: ${registered.length} channels`, 'ipc/alignment');

  return () => {
    for (const channel of registered) {
      ipcMain.removeHandler(channel);
    }
    logger.info('alignment IPC unregistered', 'ipc/alignment');
  };
}
