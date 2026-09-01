// ═══════════════════════════════════════════════════════════
// src/main/ipc/workspace.ipc.ts — Workspace IPC Handler
// ═══════════════════════════════════════════════════════════

import { ipcMain } from 'electron';
import { z } from 'zod';
import { IPC_CHANNELS } from '../../shared/contracts';
import type { WorkspaceCreateReq } from '../../shared/contracts';
import Logger from '../util/logger';
import { WorkspaceManager } from '../workspace/manager';
import { QuotaController } from '../workspace/quota';

// ── Zod Schemas ─────────────────────────────────────────────

const workspaceCreateSchema = z.object({
  name: z.string().min(1).max(100),
  agentConfig: z.object({
    role: z.string(),
    systemPrompt: z.string(),
  }),
  modelBinding: z.string().optional(),
  skillIds: z.array(z.string()).optional(),
  mcpServerIds: z.array(z.string()).optional(),
});

const workspaceIdSchema = z.object({
  workspaceId: z.string(),
});

// ── Types ───────────────────────────────────────────────────

type SafeHandler = (event: Electron.IpcMainInvokeEvent, ...args: any[]) => Promise<any>;

// ── Helpers ─────────────────────────────────────────────────

const logger = Logger.getInstance();

function safeHandler(handler: SafeHandler): SafeHandler {
  return async (event, ...args) => {
    try {
      return await handler(event, ...args);
    } catch (error: any) {
      logger.error('IPC handler error', {
        module: 'ipc/workspace',
        error: error.message,
        stack: error.stack,
      });
      throw error;
    }
  };
}

// ── IPC Handlers ────────────────────────────────────────────

export function registerWorkspaceIpc(): () => void {
  const handlers: Array<{ channel: string; handler: SafeHandler }> = [];

  // ── workspace:create ────────────────────────────────────

  const createHandler: SafeHandler = async (_event, args: unknown) => {
    const input = workspaceCreateSchema.parse(args) as WorkspaceCreateReq;
    const manager = WorkspaceManager.getInstance();
    const quota = QuotaController.getInstance();

    const workspace = await manager.create(input);

    // Initialize quota for new workspace
    quota.initWorkspace(workspace.id);

    return workspace;
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_CREATE, safeHandler(createHandler));
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_CREATE, handler: createHandler });

  // ── workspace:list ──────────────────────────────────────

  const listHandler: SafeHandler = async () => {
    const manager = WorkspaceManager.getInstance();
    return manager.list();
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_LIST, listHandler);
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_LIST, handler: listHandler });

  // ── workspace:switch ────────────────────────────────────

  const switchHandler: SafeHandler = async (_event, args: unknown) => {
    const { workspaceId } = workspaceIdSchema.parse(args);
    const manager = WorkspaceManager.getInstance();
    return manager.switch(workspaceId);
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_UPDATE, safeHandler(switchHandler));
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_UPDATE, handler: switchHandler });

  // ── workspace:destroy ───────────────────────────────────

  const destroyHandler: SafeHandler = async (_event, args: unknown) => {
    const { workspaceId } = workspaceIdSchema.parse(args);
    const manager = WorkspaceManager.getInstance();
    const quota = QuotaController.getInstance();

    const result = await manager.destroy(workspaceId);
    quota.destroyWorkspace(workspaceId);

    return result;
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_DELETE, safeHandler(destroyHandler));
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_DELETE, handler: destroyHandler });

  // ── workspace:suspend ───────────────────────────────────

  const suspendHandler: SafeHandler = async (_event, args: unknown) => {
    const { workspaceId } = workspaceIdSchema.parse(args);
    const manager = WorkspaceManager.getInstance();
    return manager.suspend(workspaceId);
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_SUSPEND, safeHandler(suspendHandler));
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_SUSPEND, handler: suspendHandler });

  // ── workspace:resume ─────────────────────────────────────

  const resumeHandler: SafeHandler = async (_event, args: unknown) => {
    const { workspaceId } = workspaceIdSchema.parse(args);
    const manager = WorkspaceManager.getInstance();
    return manager.resume(workspaceId);
  };

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_RESUME, safeHandler(resumeHandler));
  handlers.push({ channel: IPC_CHANNELS.WORKSPACE_RESUME, handler: resumeHandler });

  // ── Cleanup ─────────────────────────────────────────────

  logger.info('Workspace IPC handlers registered', { module: 'ipc/workspace' });

  return () => {
    for (const { channel } of handlers) {
      ipcMain.removeHandler(channel);
    }
    logger.info('Workspace IPC handlers unregistered', { module: 'ipc/workspace' });
  };
}

export default registerWorkspaceIpc;
