// ============================================================
// src/main/model/circuit-breaker.ts — Circuit Breaker Pattern
// ============================================================
//
// State machine: CLOSED → OPEN → HALF_OPEN → CLOSED
//
// - CLOSED: Normal operation, tracking error rate in a sliding window
// - OPEN: Model is tripped, all calls are rejected for the cooldown period
// - HALF_OPEN: After cooldown, allow a single probe call to test recovery
//
// Configuration:
//   - Window size: 5 minutes
//   - Error rate threshold: 15%
//   - Cooldown (recovery) period: 3 minutes
//   - Rate-limit exponential backoff: 1s → 2s → 4s → 8s (max 4 retries)
// ═══════════════════════════════════════════════════════════

import Logger from '../util/logger';

const MODULE = 'circuit-breaker';

// ── Constants ────────────────────────────────────────────────

/** Sliding window duration in milliseconds (5 minutes) */
const WINDOW_MS = 5 * 60 * 1000;

/** Error rate threshold to trigger circuit open (15%) */
const ERROR_RATE_THRESHOLD = 0.15;

/** Minimum number of calls in window before evaluating error rate */
const MIN_CALLS_FOR_EVALUATION = 5;

/** Cooldown / recovery period in milliseconds (3 minutes) */
const COOLDOWN_MS = 3 * 60 * 1000;

/** Max exponential backoff retries for rate-limited requests */
const MAX_BACKOFF_RETRIES = 4;

/** Base backoff delay in milliseconds (1 second) */
const BASE_BACKOFF_MS = 1000;

// ── Types ────────────────────────────────────────────────────

export enum CircuitState {
  CLOSED = 'CLOSED',
  OPEN = 'OPEN',
  HALF_OPEN = 'HALF_OPEN',
}

/** A single call record in the sliding window */
interface CallRecord {
  timestamp: number;
  success: boolean;
  latencyMs: number;
  isRateLimited: boolean;
}

/** Per-model circuit breaker state */
interface ModelCircuit {
  modelId: string;
  state: CircuitState;
  calls: CallRecord[];
  openedAt: number | null;
  halfOpenAttempts: number;
  lastTransitionAt: number;
}

/** Statistics snapshot for a model circuit */
export interface CircuitStats {
  modelId: string;
  state: CircuitState;
  totalCalls: number;
  successCount: number;
  failureCount: number;
  errorRate: number;
  avgLatencyMs: number;
  openedAt: string | null;
}

// ── CircuitBreaker Class ─────────────────────────────────────

export class CircuitBreaker {
  private static instance: CircuitBreaker | null = null;
  private circuits: Map<string, ModelCircuit>;

  private constructor() {
    this.circuits = new Map();
  }

  static getInstance(): CircuitBreaker {
    if (!CircuitBreaker.instance) {
      CircuitBreaker.instance = new CircuitBreaker();
    }
    return CircuitBreaker.instance;
  }

  /** Reset singleton (for testing only) */
  static resetInstance(): void {
    CircuitBreaker.instance = null;
  }

  // ── Public API ──────────────────────────────────────────

  /**
   * Check if a model call is allowed (circuit not open).
   * Automatically transitions OPEN → HALF_OPEN after cooldown.
   */
  isAllowed(modelId: string): boolean {
    const circuit = this.getOrCreateCircuit(modelId);

    switch (circuit.state) {
      case CircuitState.CLOSED:
        return true;

      case CircuitState.OPEN:
        return this.checkCooldownTransition(circuit);

      case CircuitState.HALF_OPEN:
        // Allow one probe attempt
        return circuit.halfOpenAttempts < 1;

      default:
        return true;
    }
  }

  /**
   * Record the outcome of a model call.
   */
  recordCall(modelId: string, success: boolean, latencyMs: number, isRateLimited = false): void {
    const circuit = this.getOrCreateCircuit(modelId);
    const now = Date.now();

    const record: CallRecord = {
      timestamp: now,
      success,
      latencyMs,
      isRateLimited,
    };

    circuit.calls.push(record);

    // Prune old calls outside the window
    this.pruneCalls(circuit, now);

    // Evaluate state transitions
    if (circuit.state === CircuitState.HALF_OPEN) {
      circuit.halfOpenAttempts++;
      if (success) {
        this.transitionTo(circuit, CircuitState.CLOSED);
        Logger.getInstance().info('Circuit closed after successful probe', MODULE, {
          modelId,
        });
      } else {
        this.transitionTo(circuit, CircuitState.OPEN);
        Logger.getInstance().warn('Circuit re-opened after failed probe', MODULE, {
          modelId,
        });
      }
    } else if (circuit.state === CircuitState.CLOSED) {
      this.evaluateErrorRate(circuit, now);
    }
  }

  /**
   * Get the current state of a model's circuit.
   */
  getState(modelId: string): CircuitState {
    const circuit = this.circuits.get(modelId);
    if (!circuit) return CircuitState.CLOSED;

    // Check if cooldown has passed
    if (circuit.state === CircuitState.OPEN) {
      this.checkCooldownTransition(circuit);
    }

    return circuit.state;
  }

  /**
   * Get statistics for a model's circuit.
   */
  getStats(modelId: string): CircuitStats {
    const circuit = this.getOrCreateCircuit(modelId);
    const now = Date.now();
    this.pruneCalls(circuit, now);

    const totalCalls = circuit.calls.length;
    const failureCount = circuit.calls.filter((c) => !c.success).length;
    const successCount = totalCalls - failureCount;
    const errorRate = totalCalls > 0 ? failureCount / totalCalls : 0;
    const avgLatencyMs =
      totalCalls > 0
        ? circuit.calls.reduce((sum, c) => sum + c.latencyMs, 0) / totalCalls
        : 0;

    return {
      modelId,
      state: circuit.state,
      totalCalls,
      successCount,
      failureCount,
      errorRate,
      avgLatencyMs: Math.round(avgLatencyMs),
      openedAt: circuit.openedAt ? new Date(circuit.openedAt).toISOString() : null,
    };
  }

  /**
   * Get stats for all tracked models.
   */
  getAllStats(): CircuitStats[] {
    const stats: CircuitStats[] = [];
    for (const modelId of this.circuits.keys()) {
      stats.push(this.getStats(modelId));
    }
    return stats;
  }

  /**
   * Manually reset a circuit to CLOSED state.
   */
  reset(modelId: string): void {
    const circuit = this.circuits.get(modelId);
    if (circuit) {
      this.transitionTo(circuit, CircuitState.CLOSED);
      circuit.calls.length = 0;
      Logger.getInstance().info('Circuit manually reset', MODULE, { modelId });
    }
  }

  /**
   * Reset all circuits.
   */
  resetAll(): void {
    for (const modelId of this.circuits.keys()) {
      this.reset(modelId);
    }
  }

  // ── Exponential Backoff ─────────────────────────────────

  /**
   * Calculate backoff delay for a rate-limited retry.
   * Returns -1 if max retries exceeded.
   *
   * Pattern: 1s → 2s → 4s → 8s
   */
  static getBackoffDelay(attempt: number): number {
    if (attempt >= MAX_BACKOFF_RETRIES) return -1;
    return BASE_BACKOFF_MS * Math.pow(2, attempt);
  }

  /**
   * Get the max number of backoff retries allowed.
   */
  static get maxRetries(): number {
    return MAX_BACKOFF_RETRIES;
  }

  // ── Private Helpers ─────────────────────────────────────

  private getOrCreateCircuit(modelId: string): ModelCircuit {
    let circuit = this.circuits.get(modelId);
    if (!circuit) {
      circuit = {
        modelId,
        state: CircuitState.CLOSED,
        calls: [],
        openedAt: null,
        halfOpenAttempts: 0,
        lastTransitionAt: Date.now(),
      };
      this.circuits.set(modelId, circuit);
    }
    return circuit;
  }

  private pruneCalls(circuit: ModelCircuit, now: number): void {
    const cutoff = now - WINDOW_MS;
    circuit.calls = circuit.calls.filter((c) => c.timestamp >= cutoff);
  }

  private checkCooldownTransition(circuit: ModelCircuit): boolean {
    if (circuit.state !== CircuitState.OPEN || !circuit.openedAt) {
      return circuit.state !== CircuitState.OPEN;
    }

    const elapsed = Date.now() - circuit.openedAt;
    if (elapsed >= COOLDOWN_MS) {
      this.transitionTo(circuit, CircuitState.HALF_OPEN);
      circuit.halfOpenAttempts = 0;
      Logger.getInstance().info('Circuit entering half-open state', MODULE, {
        modelId: circuit.modelId,
        elapsedMs: elapsed,
      });
      return true; // Allow probe call
    }

    return false; // Still in cooldown
  }

  private evaluateErrorRate(circuit: ModelCircuit, _now: number): void {
    const totalCalls = circuit.calls.length;
    if (totalCalls < MIN_CALLS_FOR_EVALUATION) return;

    const failureCount = circuit.calls.filter((c) => !c.success).length;
    const errorRate = failureCount / totalCalls;

    if (errorRate >= ERROR_RATE_THRESHOLD) {
      this.transitionTo(circuit, CircuitState.OPEN);
      Logger.getInstance().warn('Circuit opened due to high error rate', MODULE, {
        modelId: circuit.modelId,
        errorRate: (errorRate * 100).toFixed(1) + '%',
        totalCalls,
        failureCount,
      });
    }
  }

  private transitionTo(circuit: ModelCircuit, newState: CircuitState): void {
    const oldState = circuit.state;
    circuit.state = newState;
    circuit.lastTransitionAt = Date.now();

    if (newState === CircuitState.OPEN) {
      circuit.openedAt = Date.now();
    } else if (newState === CircuitState.CLOSED) {
      circuit.openedAt = null;
      circuit.halfOpenAttempts = 0;
    }

    Logger.getInstance().debug('Circuit state transition', MODULE, {
      modelId: circuit.modelId,
      from: oldState,
      to: newState,
    });
  }
}

// ── Convenience export ───────────────────────────────────────

export function getCircuitBreaker(): CircuitBreaker {
  return CircuitBreaker.getInstance();
}
