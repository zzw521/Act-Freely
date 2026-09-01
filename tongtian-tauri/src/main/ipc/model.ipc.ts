// ============================================================
// src/main/ipc/model.ipc.ts — Model IPC handlers with Zod validation
// ============================================================

import { ipcMain } from 'electron';
import { z } from 'zod';
import { IPC_CHANNELS, ErrorCode } from '../../shared/contracts';
import type { ModelConfig, ModelSource } from '../../shared/types';
import type { ModelTestReq, UsageStats } from '../../shared/contracts';
import { getHub } from '../model/hub';
import { getObserver, type UsagePeriod } from '../model/observer';
import Logger from '../util/logger';

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── Zod Schemas ──────────────────────────────────────────────

const modelSourceEnum = z.enum([
  'openai_compat',
  'ollama',
  'preset_qwen',
  'preset_deepseek',
  'preset_openai',
]);

const modelCreateSchema = z.object({
  name: z.string().min(1, 'Name is required').max(100, 'Name too long'),
  source: modelSourceEnum,
  baseUrl: z.string().url('Invalid base URL'),
  apiKey: z.string().min(1, 'API Key is required'),
  modelName: z.string().min(1, 'Model name is required').max(100),
  contextWindow: z.number().int().positive().max(1000000).optional(),
  agentBinding: z.string().optional(),
  forceLocal: z.boolean().optional(),
});

const modelUpdateSchema = z.object({
  id: z.string().uuid('Invalid model ID'),
  name: z.string().min(1).max(100).optional(),
  baseUrl: z.string().url().optional(),
  apiKey: z.string().min(1).optional(),
  modelName: z.string().min(1).max(100).optional(),
  contextWindow: z.number().int().positive().max(1000000).optional(),
  agentBinding: z.string().optional(),
  forceLocal: z.boolean().optional(),
});

const modelIdSchema = z.object({
  id: z.string().uuid('Invalid model ID'),
});

const modelTestSchema = z.object({
  baseUrl: z.string().url('Invalid base URL'),
  apiKey: z.string(),
  modelName: z.string().min(1),
  contextWindow: z.number().int().positive().optional(),
});

const modelUsageSchema = z.object({
  period: z.enum(['day', 'week', 'month']),
});

// ── Helpers ──────────────────────────────────────────────────

function validate<T>(schema: z.ZodType<T>, input: unknown, channel: string): T {
  const result = schema.safeParse(input);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `${i.path.join('.')}: ${i.message}`)
      .join('; ');
    logger.warn(`IPC validation failed on ${channel}: ${issues}`, 'ipc:model');
    throw {
      code: ErrorCode.VALIDATION_ERROR,
      message: `Validation failed: ${issues}`,
    };
  }
  return result.data;
}

function safeHandler<T>(
  channel: string,
  handler: (arg: unknown) => Promise<T> | T,
): (event: Electron.IpcMainInvokeEvent, arg: unknown) => Promise<T> {
  return async (_event, arg) => {
    try {
      return await handler(arg);
    } catch (err) {
      if (err && typeof err === 'object' && 'code' in err && 'message' in err) {
        throw err;
      }
      const message = err instanceof Error ? err.message : String(err);
      logger.error(`IPC handler error on ${channel}`, 'ipc:model', { error: message });
      throw {
        code: ErrorCode.INTERNAL_ERROR,
        message: `Internal error in ${channel}: ${message}`,
      };
    }
  };
}

// ── Registration ─────────────────────────────────────────────

export function registerModelIpc(): void {
  logger.info('Registering model IPC handlers', 'ipc:model');

  // ── model:list ───────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_LIST,
    safeHandler('model:list', () => {
      const hub = getHub();
      return hub.list();
    }),
  );

  // ── model:create ─────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_CREATE,
    safeHandler('model:create', async (arg) => {
      const input = validate(modelCreateSchema, arg, 'model:create');
      const hub = getHub();
      const model = await hub.add(input);
      logger.info(`model:create → id=${model.id}`, 'ipc:model');
      return model;
    }),
  );

  // ── model:update ─────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_UPDATE,
    safeHandler('model:update', async (arg) => {
      const input = validate(modelUpdateSchema, arg, 'model:update');
      const hub = getHub();
      const model = await hub.update(input);
      logger.info(`model:update → id=${model.id}`, 'ipc:model');
      return model;
    }),
  );

  // ── model:delete ─────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_DELETE,
    safeHandler('model:delete', async (arg) => {
      const { id } = validate(modelIdSchema, arg, 'model:delete');
      const hub = getHub();
      const result = await hub.delete(id);
      logger.info(`model:delete → id=${id} ok=${result.ok}`, 'ipc:model');
      return result;
    }),
  );

  // ── model:test ───────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_TEST,
    safeHandler('model:test', async (arg) => {
      const req = validate(modelTestSchema, arg, 'model:test') as ModelTestReq;
      const hub = getHub();
      const result = await hub.healthCheck(req);
      logger.info(
        `model:test → model=${req.modelName} ok=${result.ok} latency=${result.latencyMs}ms`,
        'ipc:model',
      );
      return result;
    }),
  );

  // ── model:setDefault ─────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_SET_DEFAULT,
    safeHandler('model:setDefault', async (arg) => {
      const { id } = validate(modelIdSchema, arg, 'model:setDefault');
      const hub = getHub();
      const model = hub.setDefault(id);
      logger.info(`model:setDefault → id=${id}`, 'ipc:model');
      return model;
    }),
  );

  // ── model:usage ──────────────────────────────────────────

  ipcMain.handle(
    IPC_CHANNELS.MODEL_USAGE,
    safeHandler('model:usage', async (arg) => {
      const { period } = validate(modelUsageSchema, arg, 'model:usage');
      const observer = getObserver();
      const stats = observer.getStats(period as UsagePeriod);
      return stats;
    }),
  );

  // ── Cleanup helper ─────────────────────────────────────

  const channels = [
    IPC_CHANNELS.MODEL_LIST,
    IPC_CHANNELS.MODEL_CREATE,
    IPC_CHANNELS.MODEL_UPDATE,
    IPC_CHANNELS.MODEL_DELETE,
    IPC_CHANNELS.MODEL_TEST,
    IPC_CHANNELS.MODEL_SET_DEFAULT,
    IPC_CHANNELS.MODEL_USAGE,
  ];

  /**
   * Remove all model IPC handlers (for app shutdown).
   */
  return function unregisterModelIpc() {
    for (const channel of channels) {
      ipcMain.removeHandler(channel);
    }
    logger.info('Model IPC handlers unregistered', 'ipc:model');
  };
}
