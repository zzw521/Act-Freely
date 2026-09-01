// ============================================================
// src/main/model/router.ts — Model Router (Singleton)
// ============================================================
//
// Three-layer routing strategy:
//   1. Sensitivity detection → force local model for CONFIDENTIAL data
//   2. Agent binding → use model bound to the workspace/agent
//   3. Global default → use the default model
//
// Fallback chain: primary model → backup models → error
// ═══════════════════════════════════════════════════════════

import { randomUUID } from 'crypto';
import Database from '../store/db';
import Logger from '../util/logger';
import { ModelSource, DataSensitivity } from '../../shared/types';
import type { ModelConfig } from '../../shared/types';

const MODULE = 'model-router';

// ── Types ────────────────────────────────────────────────────

/** Context passed to the router for model resolution */
export interface RouteContext {
  /** The input content to analyze for sensitivity */
  content: string;
  /** Optional workspace/agent binding ID */
  agentBinding?: string;
  /** Optional explicit model ID override */
  modelOverride?: string;
  /** Optional workspace ID for audit */
  workspaceId?: string;
}

/** Result of a model routing decision */
export interface RouteResult {
  /** The resolved model config */
  model: ModelConfig;
  /** The routing decision reason */
  reason: RouteReason;
  /** Audit trail entry */
  auditEvent: string;
}

export type RouteReason =
  | 'sensitive_forced_local'
  | 'agent_binding'
  | 'agent_binding_fallback'
  | 'global_default'
  | 'global_default_fallback'
  | 'explicit_override';

/** Sensitivity checker interface — implemented by ComplianceGuard (Section 4) */
export interface SensitivityChecker {
  classify(text: string): DataSensitivity;
}

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

// ── ModelRouter Class ────────────────────────────────────────

export class ModelRouter {
  private static instance: ModelRouter | null = null;
  private sensitivityChecker: SensitivityChecker | null = null;

  private constructor() {
    // Private constructor for singleton
  }

  static getInstance(): ModelRouter {
    if (!ModelRouter.instance) {
      ModelRouter.instance = new ModelRouter();
    }
    return ModelRouter.instance;
  }

  /** Reset singleton (for testing only) */
  static resetInstance(): void {
    ModelRouter.instance = null;
  }

  // ── Sensitivity Checker Integration ─────────────────────

  /**
   * Register a sensitivity checker (ComplianceGuard).
   * Called during app initialization once the compliance module is ready.
   */
  setSensitivityChecker(checker: SensitivityChecker | null): void {
    this.sensitivityChecker = checker;
    Logger.getInstance().info(
      checker ? 'Sensitivity checker registered' : 'Sensitivity checker removed',
      MODULE,
    );
  }

  // ── Core Routing Logic ──────────────────────────────────

  /**
   * Resolve the best model for a given routing context.
   *
   * Routing priority:
   *   1. If content is CONFIDENTIAL and a local model exists → force local
   *   2. If explicit modelOverride is provided → use it
   *   3. If agentBinding matches a model → use bound model + fallback chain
   *   4. Otherwise → use global default + fallback chain
   *
   * @throws Error with code ALL_MODELS_UNAVAILABLE if no model can be resolved
   */
  resolve(ctx: RouteContext): RouteResult {
    const logger = Logger.getInstance();
    const allModels = this.loadAllModels();

    if (allModels.length === 0) {
      throw this.allModelsUnavailable('No models configured');
    }

    // Layer 1: Sensitivity detection → force local model
    const sensitivity = this.classifySensitivity(ctx.content);
    if (sensitivity === DataSensitivity.CONFIDENTIAL) {
      const localModel = this.findLocalModel(allModels);
      if (localModel) {
        logger.info('Sensitive data detected, forcing local model', MODULE, {
          modelId: localModel.id,
          modelName: localModel.name,
        });
        return {
          model: localModel,
          reason: 'sensitive_forced_local',
          auditEvent: 'route:sensitive_forced_local',
        };
      }
      // No local model available for confidential data
      throw this.allModelsUnavailable(
        'CONFIDENTIAL data requires a local model, but none is configured',
      );
    }

    // Explicit override
    if (ctx.modelOverride) {
      const override = allModels.find((m) => m.id === ctx.modelOverride);
      if (override) {
        logger.debug('Using explicit model override', MODULE, {
          modelId: override.id,
        });
        return {
          model: override,
          reason: 'explicit_override',
          auditEvent: 'route:explicit_override',
        };
      }
      logger.warn('Override model not found, falling through', MODULE, {
        modelId: ctx.modelOverride,
      });
    }

    // Layer 2: Agent binding
    if (ctx.agentBinding) {
      const boundModel = this.findBoundModel(allModels, ctx.agentBinding);
      if (boundModel) {
        logger.debug('Using agent-bound model', MODULE, {
          agentBinding: ctx.agentBinding,
          modelId: boundModel.id,
        });
        return {
          model: boundModel,
          reason: 'agent_binding',
          auditEvent: 'route:agent_binding',
        };
      }

      // Try fallback models bound to the same agent
      const fallback = this.findFallbackForBinding(allModels, ctx.agentBinding);
      if (fallback) {
        logger.info('Primary bound model unavailable, using fallback', MODULE, {
          agentBinding: ctx.agentBinding,
          fallbackModelId: fallback.id,
        });
        return {
          model: fallback,
          reason: 'agent_binding_fallback',
          auditEvent: 'route:agent_binding_fallback',
        };
      }

      logger.debug('No bound model found, falling through to default', MODULE, {
        agentBinding: ctx.agentBinding,
      });
    }

    // Layer 3: Global default
    const defaultModel = this.findDefaultModel(allModels);
    if (defaultModel) {
      logger.debug('Using global default model', MODULE, {
        modelId: defaultModel.id,
      });
      return {
        model: defaultModel,
        reason: 'global_default',
        auditEvent: 'route:global_default',
      };
    }

    // Fallback: use the first available model
    const firstAvailable = allModels[0];
    if (firstAvailable) {
      logger.warn('No default model set, using first available', MODULE, {
        modelId: firstAvailable.id,
      });
      return {
        model: firstAvailable,
        reason: 'global_default_fallback',
        auditEvent: 'route:global_default_fallback',
      };
    }

    throw this.allModelsUnavailable('All models are unavailable');
  }

  // ── Model Queries ───────────────────────────────────────

  /**
   * Get all configured models from the database.
   */
  loadAllModels(): ModelConfig[] {
    const db = Database.getInstance().getDb();
    const rows = db.prepare('SELECT * FROM model_configs').all() as ModelConfigRow[];
    return rows.map(rowToModelConfig);
  }

  /**
   * Get a single model by ID.
   */
  getModelById(id: string): ModelConfig | null {
    const db = Database.getInstance().getDb();
    const row = db.prepare('SELECT * FROM model_configs WHERE id = ?').get(id) as
      ModelConfigRow | undefined;
    return row ? rowToModelConfig(row) : null;
  }

  // ── Private Helpers ─────────────────────────────────────

  private classifySensitivity(content: string): DataSensitivity {
    if (!this.sensitivityChecker) {
      // No checker registered — default to PUBLIC (no enforcement)
      return DataSensitivity.PUBLIC;
    }
    return this.sensitivityChecker.classify(content);
  }

  private findLocalModel(models: ModelConfig[]): ModelConfig | null {
    // Prefer force_local flag, then Ollama source
    const forcedLocal = models.find((m) => m.forceLocal);
    if (forcedLocal) return forcedLocal;

    const ollama = models.find((m) => m.source === ModelSource.Ollama);
    return ollama ?? null;
  }

  private findBoundModel(models: ModelConfig[], binding: string): ModelConfig | null {
    return models.find((m) => m.agentBinding === binding) ?? null;
  }

  private findFallbackForBinding(
    _models: ModelConfig[],
    _binding: string,
  ): ModelConfig | null {
    // No dedicated fallback binding mechanism yet — return null to fall
    // through to Layer 3 (global default) when the bound model is not found.
    return null;
  }

  private findDefaultModel(models: ModelConfig[]): ModelConfig | null {
    return models.find((m) => m.isDefault) ?? null;
  }

  private allModelsUnavailable(reason: string): Error {
    const err = new Error(`ALL_MODELS_UNAVAILABLE: ${reason}`) as Error & { code: string };
    err.code = 'ALL_MODELS_UNAVAILABLE';
    return err;
  }
}

// ── Convenience export ───────────────────────────────────────

export function getRouter(): ModelRouter {
  return ModelRouter.getInstance();
}
