// ============================================================
// src/main/model/hub.ts — Model Hub Manager (Singleton)
// ============================================================
//
// Central model management: CRUD, API Key encryption via Vault,
// connectivity health check, and default model designation.
// ═══════════════════════════════════════════════════════════

import { randomUUID } from 'crypto';
import Database from '../store/db';
import Vault from '../store/vault';
import Logger from '../util/logger';
import { ModelSource } from '../../shared/types';
import type { ModelConfig } from '../../shared/types';
import type { ModelTestReq, ModelTestResp } from '../../shared/contracts';

const MODULE = 'model-hub';

// ── Constants ────────────────────────────────────────────────

/** Health check timeout in milliseconds */
const HEALTH_CHECK_TIMEOUT_MS = 10000;

/** Test prompt for connectivity check */
const TEST_PROMPT = 'Say "ok" in one word.';

// ── DB row mapping ───────────────────────────────────────────

interface ModelConfigRow {
  id: string;
  name: string;
  source: string;
  base_url: string;
  api_key_encrypted: string;
  model_name: string;
  context_window: number;
  is_default: number;
  agent_binding: string;
  force_local: number;
  created_at: string;
}

function rowToModelConfig(row: ModelConfigRow): ModelConfig {
  return {
    id: row.id,
    name: row.name,
    source: row.source as ModelSource,
    baseUrl: row.base_url,
    apiKeyEncrypted: row.api_key_encrypted,
    modelName: row.model_name,
    contextWindow: row.context_window,
    isDefault: row.is_default === 1,
    agentBinding: row.agent_binding,
    forceLocal: row.force_local === 1,
    createdAt: row.created_at,
  };
}

// ── Input types ──────────────────────────────────────────────

export interface ModelCreateInput {
  name: string;
  source: ModelSource;
  baseUrl: string;
  apiKey: string; // Plain-text, will be encrypted
  modelName: string;
  contextWindow?: number;
  agentBinding?: string;
  forceLocal?: boolean;
}

export interface ModelUpdateInput {
  id: string;
  name?: string;
  baseUrl?: string;
  apiKey?: string; // If provided, will re-encrypt
  modelName?: string;
  contextWindow?: number;
  agentBinding?: string;
  forceLocal?: boolean;
}

// ── ModelHub Class ───────────────────────────────────────────

export class ModelHub {
  private static instance: ModelHub | null = null;

  private constructor() {
    // Private constructor for singleton
  }

  static getInstance(): ModelHub {
    if (!ModelHub.instance) {
      ModelHub.instance = new ModelHub();
    }
    return ModelHub.instance;
  }

  /** Reset singleton (for testing only) */
  static resetInstance(): void {
    ModelHub.instance = null;
  }

  // ── CRUD Operations ─────────────────────────────────────

  /**
   * List all model configurations.
   */
  list(): ModelConfig[] {
    const db = Database.getInstance().getDb();
    const rows = db.prepare('SELECT * FROM model_configs ORDER BY created_at DESC').all() as ModelConfigRow[];
    return rows.map(rowToModelConfig);
  }

  /**
   * Get a single model by ID.
   */
  getById(id: string): ModelConfig | null {
    const db = Database.getInstance().getDb();
    const row = db.prepare('SELECT * FROM model_configs WHERE id = ?').get(id) as
      ModelConfigRow | undefined;
    return row ? rowToModelConfig(row) : null;
  }

  /**
   * Add a new model configuration.
   * API Key is encrypted and stored in Vault.
   */
  async add(input: ModelCreateInput): Promise<ModelConfig> {
    const logger = Logger.getInstance();
    const id = randomUUID();
    const db = Database.getInstance().getDb();

    // Store API key in Vault
    const vault = Vault.getInstance();
    await vault.storeApiKey(id, input.apiKey);

    // Insert model config (api_key_encrypted stores a reference marker)
    db.prepare(`
      INSERT INTO model_configs (id, name, source, base_url, api_key_encrypted, model_name, context_window, is_default, agent_binding, force_local, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, datetime('now'))
    `).run(
      id,
      input.name,
      input.source,
      input.baseUrl,
      `vault:${id}`, // Reference to Vault-stored key
      input.modelName,
      input.contextWindow ?? 4096,
      input.agentBinding ?? '',
      input.forceLocal ? 1 : 0,
    );

    logger.info('Model added', MODULE, { id, name: input.name, source: input.source });

    const config = this.getById(id);
    if (!config) {
      throw new Error('Failed to retrieve newly created model');
    }
    return config;
  }

  /**
   * Update an existing model configuration.
   */
  async update(input: ModelUpdateInput): Promise<ModelConfig> {
    const logger = Logger.getInstance();
    const existing = this.getById(input.id);
    if (!existing) {
      throw this.notFoundError(input.id);
    }

    const db = Database.getInstance().getDb();
    const vault = Vault.getInstance();

    // Update API key if provided
    if (input.apiKey) {
      await vault.storeApiKey(input.id, input.apiKey);
    }

    // Build dynamic update
    const updates: string[] = [];
    const params: unknown[] = [];

    if (input.name !== undefined) {
      updates.push('name = ?');
      params.push(input.name);
    }
    if (input.baseUrl !== undefined) {
      updates.push('base_url = ?');
      params.push(input.baseUrl);
    }
    if (input.modelName !== undefined) {
      updates.push('model_name = ?');
      params.push(input.modelName);
    }
    if (input.contextWindow !== undefined) {
      updates.push('context_window = ?');
      params.push(input.contextWindow);
    }
    if (input.agentBinding !== undefined) {
      updates.push('agent_binding = ?');
      params.push(input.agentBinding);
    }
    if (input.forceLocal !== undefined) {
      updates.push('force_local = ?');
      params.push(input.forceLocal ? 1 : 0);
    }

    if (updates.length > 0) {
      params.push(input.id);
      db.prepare(`UPDATE model_configs SET ${updates.join(', ')} WHERE id = ?`).run(...params);
    }

    logger.info('Model updated', MODULE, { id: input.id });

    return this.getById(input.id)!;
  }

  /**
   * Delete a model configuration and its API key.
   */
  async delete(id: string): Promise<{ ok: boolean }> {
    const logger = Logger.getInstance();
    const existing = this.getById(id);
    if (!existing) {
      throw this.notFoundError(id);
    }

    const db = Database.getInstance().getDb();
    const vault = Vault.getInstance();

    // Delete API key from Vault
    await vault.deleteApiKey(id);

    // Delete model config
    db.prepare('DELETE FROM model_configs WHERE id = ?').run(id);

    logger.info('Model deleted', MODULE, { id });
    return { ok: true };
  }

  // ── Default Model ───────────────────────────────────────

  /**
   * Set a model as the global default.
   * Clears the default flag from all other models first.
   */
  setDefault(id: string): ModelConfig {
    const existing = this.getById(id);
    if (!existing) {
      throw this.notFoundError(id);
    }

    const db = Database.getInstance().getDb();

    // Clear all defaults
    db.prepare('UPDATE model_configs SET is_default = 0').run();

    // Set the target as default
    db.prepare('UPDATE model_configs SET is_default = 1 WHERE id = ?').run(id);

    Logger.getInstance().info('Default model changed', MODULE, { id, name: existing.name });

    return this.getById(id)!;
  }

  /**
   * Clear the default model designation from all models.
   */
  clearDefault(): void {
    const db = Database.getInstance().getDb();
    db.prepare('UPDATE model_configs SET is_default = 0').run();
  }

  // ── Health Check ────────────────────────────────────────

  /**
   * Test connectivity to a model endpoint.
   * Sends a minimal request and measures latency.
   */
  async healthCheck(req: ModelTestReq): Promise<ModelTestResp> {
    const logger = Logger.getInstance();
    const startTime = Date.now();

    try {
      // Use a lightweight OpenAI-compatible API call
      const url = `${req.baseUrl.replace(/\/$/, '')}/chat/completions`;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT_MS);

      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(req.apiKey ? { Authorization: `Bearer ${req.apiKey}` } : {}),
        },
        body: JSON.stringify({
          model: req.modelName,
          messages: [{ role: 'user', content: TEST_PROMPT }],
          max_tokens: 5,
        }),
        signal: controller.signal,
      });

      clearTimeout(timeout);
      const latencyMs = Date.now() - startTime;

      if (!response.ok) {
        const errorBody = await response.text().catch(() => '');
        logger.warn('Health check failed', MODULE, {
          status: response.status,
          baseUrl: req.baseUrl,
          modelName: req.modelName,
        });

        return {
          ok: false,
          latencyMs,
          model: req.modelName,
          error: `HTTP ${response.status}: ${errorBody.slice(0, 200)}`,
        };
      }

      const data = await response.json() as Record<string, unknown>;

      logger.info('Health check passed', MODULE, {
        baseUrl: req.baseUrl,
        modelName: req.modelName,
        latencyMs,
      });

      return {
        ok: true,
        latencyMs,
        model: req.modelName,
        availableModels: data.data
          ? (data.data as Array<{ id: string }>).map((m) => m.id)
          : undefined,
      };
    } catch (err) {
      const latencyMs = Date.now() - startTime;
      const message = err instanceof Error ? err.message : String(err);

      logger.error('Health check error', MODULE, {
        baseUrl: req.baseUrl,
        modelName: req.modelName,
        error: message,
      });

      return {
        ok: false,
        latencyMs,
        model: req.modelName,
        error: message,
      };
    }
  }

  // ── Private Helpers ─────────────────────────────────────

  private notFoundError(id: string): Error {
    const err = new Error(`Model not found: ${id}`) as Error & { code: string };
    err.code = 'MODEL_NOT_FOUND';
    return err;
  }
}

// ── Convenience export ───────────────────────────────────────

export function getHub(): ModelHub {
  return ModelHub.getInstance();
}
