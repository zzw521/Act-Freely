// ============================================================
// src/main/model/observer.ts — Model Usage Observer (Singleton)
// ============================================================
//
// Records every model call's token usage, latency, and success/failure.
// Persists to the model_usage table and provides aggregated statistics
// by day / week / month.
// ═══════════════════════════════════════════════════════════

import { randomUUID } from 'crypto';
import Database from '../store/db';
import Logger from '../util/logger';
import type { ModelUsage } from '../../shared/types';
import type { UsageStats } from '../../shared/contracts';

const MODULE = 'model-observer';

// ── Types ────────────────────────────────────────────────────

export type UsagePeriod = 'day' | 'week' | 'month';

/** Input for recording a model call */
export interface UsageRecord {
  modelId: string;
  workspaceId?: string;
  promptTokens: number;
  completionTokens: number;
  latencyMs: number;
  success: boolean;
}

// ── DB row mapping ───────────────────────────────────────────

interface ModelUsageRow {
  id: string;
  model_id: string;
  workspace_id: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  success: number;
  created_at: string;
}

function rowToModelUsage(row: ModelUsageRow): ModelUsage {
  return {
    id: row.id,
    modelId: row.model_id,
    workspaceId: row.workspace_id,
    promptTokens: row.prompt_tokens,
    completionTokens: row.completion_tokens,
    latencyMs: row.latency_ms,
    success: row.success === 1,
    createdAt: row.created_at,
  };
}

// ── Cost Estimation ──────────────────────────────────────────

/** Rough cost per 1K tokens in USD (placeholder rates) */
const COST_PER_1K_TOKENS: Record<string, number> = {
  // OpenAI-compatible defaults
  default_prompt: 0.001,
  default_completion: 0.002,
  // Ollama / local models are free
  local: 0,
};

function estimateCost(promptTokens: number, completionTokens: number): number {
  const promptCost = (promptTokens / 1000) * COST_PER_1K_TOKENS.default_prompt;
  const completionCost = (completionTokens / 1000) * COST_PER_1K_TOKENS.default_completion;
  return Math.round((promptCost + completionCost) * 10000) / 10000;
}

// ── UsageObserver Class ──────────────────────────────────────

export class UsageObserver {
  private static instance: UsageObserver | null = null;

  private constructor() {
    // Private constructor for singleton
  }

  static getInstance(): UsageObserver {
    if (!UsageObserver.instance) {
      UsageObserver.instance = new UsageObserver();
    }
    return UsageObserver.instance;
  }

  /** Reset singleton (for testing only) */
  static resetInstance(): void {
    UsageObserver.instance = null;
  }

  // ── Record ──────────────────────────────────────────────

  /**
   * Record a model call's usage data.
   */
  record(data: UsageRecord): string {
    const id = randomUUID();
    const db = Database.getInstance().getDb();

    db.prepare(`
      INSERT INTO model_usage (id, model_id, workspace_id, prompt_tokens, completion_tokens, latency_ms, success, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
    `).run(
      id,
      data.modelId,
      data.workspaceId ?? null,
      data.promptTokens,
      data.completionTokens,
      data.latencyMs,
      data.success ? 1 : 0,
    );

    Logger.getInstance().debug('Usage recorded', MODULE, {
      id,
      modelId: data.modelId,
      promptTokens: data.promptTokens,
      completionTokens: data.completionTokens,
      latencyMs: data.latencyMs,
      success: data.success,
    });

    return id;
  }

  // ── Query ───────────────────────────────────────────────

  /**
   * Get aggregated usage statistics for a given period.
   */
  getStats(period: UsagePeriod): UsageStats[] {
    const dateFilter = this.getDateFilter(period);
    const db = Database.getInstance().getDb();

    const rows = db.prepare(`
      SELECT
        mu.model_id,
        mc.name AS model_name,
        SUM(mu.prompt_tokens) AS total_prompt_tokens,
        SUM(mu.completion_tokens) AS total_completion_tokens,
        COUNT(*) AS total_calls,
        AVG(mu.latency_ms) AS avg_latency_ms,
        SUM(mu.success) AS success_count
      FROM model_usage mu
      LEFT JOIN model_configs mc ON mc.id = mu.model_id
      WHERE mu.created_at >= ?
      GROUP BY mu.model_id
      ORDER BY total_prompt_tokens DESC
    `).all(dateFilter) as Array<{
      model_id: string;
      model_name: string | null;
      total_prompt_tokens: number;
      total_completion_tokens: number;
      total_calls: number;
      avg_latency_ms: number;
      success_count: number;
    }>;

    return rows.map((row) => ({
      modelId: row.model_id,
      modelName: row.model_name ?? 'Unknown',
      totalPromptTokens: row.total_prompt_tokens,
      totalCompletionTokens: row.total_completion_tokens,
      totalCalls: row.total_calls,
      avgLatencyMs: Math.round(row.avg_latency_ms),
      estimatedCost: estimateCost(row.total_prompt_tokens, row.total_completion_tokens),
    }));
  }

  /**
   * Get usage stats for a specific model.
   */
  getModelStats(modelId: string, period: UsagePeriod): UsageStats | null {
    const stats = this.getStats(period);
    return stats.find((s) => s.modelId === modelId) ?? null;
  }

  /**
   * Get raw usage records for a model within a period.
   */
  getRecords(modelId: string, period: UsagePeriod, limit = 100): ModelUsage[] {
    const dateFilter = this.getDateFilter(period);
    const db = Database.getInstance().getDb();

    const rows = db.prepare(`
      SELECT * FROM model_usage
      WHERE model_id = ? AND created_at >= ?
      ORDER BY created_at DESC
      LIMIT ?
    `).all(modelId, dateFilter, limit) as ModelUsageRow[];

    return rows.map(rowToModelUsage);
  }

  /**
   * Get total usage summary (all-time).
   */
  getSummary(): {
    totalCalls: number;
    totalPromptTokens: number;
    totalCompletionTokens: number;
    totalCost: number;
  } {
    const db = Database.getInstance().getDb();
    const row = db.prepare(`
      SELECT
        COUNT(*) AS total_calls,
        COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
        COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens
      FROM model_usage
    `).get() as {
      total_calls: number;
      total_prompt_tokens: number;
      total_completion_tokens: number;
    };

    return {
      totalCalls: row.total_calls,
      totalPromptTokens: row.total_prompt_tokens,
      totalCompletionTokens: row.total_completion_tokens,
      totalCost: estimateCost(row.total_prompt_tokens, row.total_completion_tokens),
    };
  }

  // ── Private Helpers ─────────────────────────────────────

  private getDateFilter(period: UsagePeriod): string {
    const now = new Date();
    switch (period) {
      case 'day': {
        const start = new Date(now);
        start.setHours(0, 0, 0, 0);
        return start.toISOString();
      }
      case 'week': {
        const start = new Date(now);
        start.setDate(start.getDate() - 7);
        start.setHours(0, 0, 0, 0);
        return start.toISOString();
      }
      case 'month': {
        const start = new Date(now);
        start.setDate(1);
        start.setHours(0, 0, 0, 0);
        return start.toISOString();
      }
    }
  }
}

// ── Convenience export ───────────────────────────────────────

export function getObserver(): UsageObserver {
  return UsageObserver.getInstance();
}
