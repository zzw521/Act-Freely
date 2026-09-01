// ============================================================
// src/main/ipc/chat.ipc.ts — Chat IPC handlers with Zod validation
// ============================================================

import { ipcMain, BrowserWindow } from 'electron';
import { z } from 'zod';
import { IPC_CHANNELS } from '../../shared/contracts';
import type { ChatSubmitReq, ChatReviewReq, ChatControlReq, ChatEvent, TaskSummary } from '../../shared/contracts';
import { ErrorCode } from '../../shared/contracts';
import { getEngine } from '../agent/engine';
import Logger from '../util/logger';

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── Zod Schemas ──────────────────────────────────────────────

const chatSubmitSchema = z.object({
  taskId: z.string().uuid().optional(),
  goal: z.string().min(1, 'Goal cannot be empty').max(10000, 'Goal too long'),
  modelBinding: z.string().optional(),
});

const chatReviewSchema = z.object({
  planId: z.string().min(1, 'Plan ID is required'),
  action: z.enum(['approve', 'reject', 'modify']),
  patch: z.record(z.unknown()).optional(),
});

const chatControlSchema = z.object({
  taskId: z.string().min(1, 'Task ID is required'),
  action: z.enum(['pause', 'resume', 'rollback_to']),
  stepId: z.string().optional(),
});

const chatHistorySchema = z.object({
  limit: z.number().int().min(1).max(500).optional(),
});

// ── Helpers ──────────────────────────────────────────────────

/**
 * Parse and validate input using Zod. Returns parsed data or throws
 * a structured error that the renderer can handle.
 */
function validate<T>(schema: z.ZodType<T>, input: unknown, channel: string): T {
  const result = schema.safeParse(input);
  if (!result.success) {
    const issues = result.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ');
    logger.warn(`IPC validation failed on ${channel}: ${issues}`, 'ipc:chat');
    throw {
      code: ErrorCode.VALIDATION_ERROR,
      message: `Validation failed: ${issues}`,
    };
  }
  return result.data;
}

/**
 * Wrap an IPC handler with error logging and structured error response.
 */
function safeHandler<T>(
  channel: string,
  handler: (arg: unknown) => Promise<T>,
): (event: Electron.IpcMainInvokeEvent, arg: unknown) => Promise<T> {
  return async (_event, arg) => {
    try {
      return await handler(arg);
    } catch (err) {
      // Structured AppError — rethrow as-is
      if (err && typeof err === 'object' && 'code' in err && 'message' in err) {
        throw err;
      }
      const message = err instanceof Error ? err.message : String(err);
      logger.error(`IPC handler error on ${channel}`, 'ipc:chat', { error: message });
      throw {
        code: ErrorCode.INTERNAL_ERROR,
        message: `Internal error in ${channel}: ${message}`,
      };
    }
  };
}

// ── Event Forwarding ─────────────────────────────────────────

/**
 * Start forwarding ChatEvents from the Agent Engine to the renderer
 * process via the `chat:event` IPC channel.
 *
 * Returns a cleanup function that removes the listener.
 */
function startEventForwarding(mainWindow: BrowserWindow): () => void {
  const engine = getEngine();

  const forwardEvent = (_taskId: string, event: ChatEvent) => {
    if (mainWindow.isDestroyed()) return;
    try {
      mainWindow.webContents.send(IPC_CHANNELS.CHAT_EVENT, event);
    } catch (err) {
      logger.warn('Failed to forward chat event to renderer', 'ipc:chat', {
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  engine.on('chat-event', forwardEvent);

  return () => {
    engine.off('chat-event', forwardEvent);
  };
}

// ── Registration ─────────────────────────────────────────────

/**
 * Register all chat-related IPC handlers.
 *
 * Must be called once during app startup with the main BrowserWindow
 * reference so that streaming events can be forwarded to the renderer.
 */
export function registerChatIpc(mainWindow: BrowserWindow): void {
  logger.info('Registering chat IPC handlers', 'ipc:chat');

  // ── chat:submit ──────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.CHAT_SUBMIT,
    safeHandler('chat:submit', async (arg) => {
      const req = validate(chatSubmitSchema, arg, 'chat:submit') as ChatSubmitReq;
      const engine = getEngine();
      const result = await engine.submit(req);
      logger.info(`chat:submit → taskId=${result.taskId}`, 'ipc:chat');
      return result;
    }),
  );

  // ── chat:review ──────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.CHAT_REVIEW,
    safeHandler('chat:review', async (arg) => {
      const req = validate(chatReviewSchema, arg, 'chat:review') as ChatReviewReq;
      const engine = getEngine();
      const result = await engine.review(req);
      logger.info(`chat:review → planId=${req.planId} action=${req.action} ok=${result.ok}`, 'ipc:chat');
      return result;
    }),
  );

  // ── chat:control ─────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.CHAT_CONTROL,
    safeHandler('chat:control', async (arg) => {
      const req = validate(chatControlSchema, arg, 'chat:control') as ChatControlReq;
      const engine = getEngine();
      const result = await engine.control(req);
      logger.info(`chat:control → taskId=${req.taskId} action=${req.action} ok=${result.ok}`, 'ipc:chat');
      return result;
    }),
  );

  // ── chat:history ─────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.CHAT_HISTORY,
    safeHandler('chat:history', async (arg) => {
      const { limit } = validate(chatHistorySchema, arg ?? {}, 'chat:history');
      const engine = getEngine();
      const history = engine.getHistory(limit);
      return history as TaskSummary[];
    }),
  );

  // ── Streaming event forwarding ───────────────────────────

  const stopForwarding = startEventForwarding(mainWindow);

  // Cleanup on window close
  mainWindow.once('closed', () => {
    stopForwarding();
    ipcMain.removeHandler(IPC_CHANNELS.CHAT_SUBMIT);
    ipcMain.removeHandler(IPC_CHANNELS.CHAT_REVIEW);
    ipcMain.removeHandler(IPC_CHANNELS.CHAT_CONTROL);
    ipcMain.removeHandler(IPC_CHANNELS.CHAT_HISTORY);
    logger.info('Chat IPC handlers unregistered', 'ipc:chat');
  });
}
