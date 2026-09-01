// ═══════════════════════════════════════════════════════════
// src/main/workspace/quota.ts — 工作区资源配额控制器
// ═══════════════════════════════════════════════════════════

import Logger from '../util/logger';

// ── Types ─────────────────────────────────────────────────

export interface QuotaLimits {
  maxConcurrentSteps: number;
  maxMemoryMb: number;
  maxConcurrentModelCalls: number;
  maxQueueDepth: number;
}

export interface QuotaUsage {
  activeSteps: number;
  memoryUsedMb: number;
  activeModelCalls: number;
  queueDepth: number;
}

export interface QuotaStatus {
  workspaceId: string;
  limits: QuotaLimits;
  usage: QuotaUsage;
  canAcceptNewStep: boolean;
  canAcceptNewTask: boolean;
  warnings: string[];
}

// ── Constants ─────────────────────────────────────────────

const DEFAULT_LIMITS: QuotaLimits = {
  maxConcurrentSteps: 5,
  maxMemoryMb: 2048,
  maxConcurrentModelCalls: 3,
  maxQueueDepth: 100,
};

const MEMORY_WARNING_THRESHOLD = 0.8; // 80% of limit triggers warning

// ── QuotaController ───────────────────────────────────────

export class QuotaController {
  private static instance: QuotaController | null = null;
  private logger = Logger.getInstance();
  private limitsMap = new Map<string, QuotaLimits>();
  private usageMap = new Map<string, QuotaUsage>();

  static getInstance(): QuotaController {
    if (!QuotaController.instance) {
      QuotaController.instance = new QuotaController();
    }
    return QuotaController.instance;
  }

  static resetInstance(): void {
    QuotaController.instance = null;
  }

  // ── Initialize workspace quota ────────────────────────

  initWorkspace(workspaceId: string, limits?: Partial<QuotaLimits>): void {
    this.limitsMap.set(workspaceId, {
      ...DEFAULT_LIMITS,
      ...limits,
    });

    this.usageMap.set(workspaceId, {
      activeSteps: 0,
      memoryUsedMb: 0,
      activeModelCalls: 0,
      queueDepth: 0,
    });

    this.logger.info('Quota initialized for workspace', {
      module: 'quota',
      workspaceId,
      limits: this.limitsMap.get(workspaceId),
    });
  }

  // ── Check Quota ───────────────────────────────────────

  canStartStep(workspaceId: string): boolean {
    const limits = this.limitsMap.get(workspaceId);
    const usage = this.usageMap.get(workspaceId);
    if (!limits || !usage) return false;

    return usage.activeSteps < limits.maxConcurrentSteps;
  }

  canAcceptTask(workspaceId: string): boolean {
    const limits = this.limitsMap.get(workspaceId);
    const usage = this.usageMap.get(workspaceId);
    if (!limits || !usage) return false;

    return usage.queueDepth < limits.maxQueueDepth;
  }

  canStartModelCall(workspaceId: string): boolean {
    const limits = this.limitsMap.get(workspaceId);
    const usage = this.usageMap.get(workspaceId);
    if (!limits || !usage) return false;

    return usage.activeModelCalls < limits.maxConcurrentModelCalls;
  }

  // ── Acquire / Release ─────────────────────────────────

  acquireStep(workspaceId: string): boolean {
    if (!this.canStartStep(workspaceId)) {
      this.logger.warn('Step quota exceeded', {
        module: 'quota',
        workspaceId,
      });
      return false;
    }

    const usage = this.usageMap.get(workspaceId)!;
    usage.activeSteps++;
    return true;
  }

  releaseStep(workspaceId: string): void {
    const usage = this.usageMap.get(workspaceId);
    if (!usage) return;

    usage.activeSteps = Math.max(0, usage.activeSteps - 1);
  }

  acquireModelCall(workspaceId: string): boolean {
    if (!this.canStartModelCall(workspaceId)) {
      this.logger.warn('Model call quota exceeded', {
        module: 'quota',
        workspaceId,
      });
      return false;
    }

    const usage = this.usageMap.get(workspaceId)!;
    usage.activeModelCalls++;
    return true;
  }

  releaseModelCall(workspaceId: string): void {
    const usage = this.usageMap.get(workspaceId);
    if (!usage) return;

    usage.activeModelCalls = Math.max(0, usage.activeModelCalls - 1);
  }

  enqueueTask(workspaceId: string): boolean {
    if (!this.canAcceptTask(workspaceId)) {
      this.logger.warn('Queue depth quota exceeded', {
        module: 'quota',
        workspaceId,
      });
      return false;
    }

    const usage = this.usageMap.get(workspaceId)!;
    usage.queueDepth++;
    return true;
  }

  dequeueTask(workspaceId: string): void {
    const usage = this.usageMap.get(workspaceId);
    if (!usage) return;

    usage.queueDepth = Math.max(0, usage.queueDepth - 1);
  }

  // ── Memory Tracking ───────────────────────────────────

  updateMemoryUsage(workspaceId: string, memoryMb: number): void {
    const usage = this.usageMap.get(workspaceId);
    if (!usage) return;

    usage.memoryUsedMb = memoryMb;

    const limits = this.limitsMap.get(workspaceId)!;
    const ratio = memoryMb / limits.maxMemoryMb;

    if (ratio >= 1.0) {
      this.logger.error('Memory limit exceeded', {
        module: 'quota',
        workspaceId,
        memoryMb,
        limitMb: limits.maxMemoryMb,
      });
    } else if (ratio >= MEMORY_WARNING_THRESHOLD) {
      this.logger.warn('Memory usage high', {
        module: 'quota',
        workspaceId,
        memoryMb,
        limitMb: limits.maxMemoryMb,
        ratio: Math.round(ratio * 100) + '%',
      });
    }
  }

  // ── Status Query ──────────────────────────────────────

  getStatus(workspaceId: string): QuotaStatus {
    const limits = this.limitsMap.get(workspaceId) || DEFAULT_LIMITS;
    const usage = this.usageMap.get(workspaceId) || {
      activeSteps: 0,
      memoryUsedMb: 0,
      activeModelCalls: 0,
      queueDepth: 0,
    };

    const warnings: string[] = [];
    const memoryRatio = usage.memoryUsedMb / limits.maxMemoryMb;

    if (memoryRatio >= MEMORY_WARNING_THRESHOLD) {
      warnings.push(`Memory usage at ${Math.round(memoryRatio * 100)}%`);
    }
    if (usage.activeSteps >= limits.maxConcurrentSteps) {
      warnings.push('Concurrent step limit reached');
    }
    if (usage.queueDepth >= limits.maxQueueDepth * 0.9) {
      warnings.push('Queue nearly full');
    }

    return {
      workspaceId,
      limits,
      usage,
      canAcceptNewStep: usage.activeSteps < limits.maxConcurrentSteps,
      canAcceptNewTask: usage.queueDepth < limits.maxQueueDepth,
      warnings,
    };
  }

  // ── Update Limits ─────────────────────────────────────

  updateLimits(workspaceId: string, newLimits: Partial<QuotaLimits>): void {
    const current = this.limitsMap.get(workspaceId) || DEFAULT_LIMITS;
    this.limitsMap.set(workspaceId, { ...current, ...newLimits });

    this.logger.info('Quota limits updated', {
      module: 'quota',
      workspaceId,
      newLimits: this.limitsMap.get(workspaceId),
    });
  }

  // ── Cleanup ───────────────────────────────────────────

  destroyWorkspace(workspaceId: string): void {
    this.limitsMap.delete(workspaceId);
    this.usageMap.delete(workspaceId);

    this.logger.info('Quota destroyed for workspace', {
      module: 'quota',
      workspaceId,
    });
  }
}

export default QuotaController;
