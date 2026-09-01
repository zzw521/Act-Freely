// ============================================================
// src/main/agent/state-machine.ts — Agent task state machine
// ============================================================

import { TaskStatus } from '../../shared/types';
import Logger from '../util/logger';

// ── Types ────────────────────────────────────────────────────

export interface StateTransition {
  from: TaskStatus;
  to: TaskStatus;
  trigger: string;
  timestamp: string;
}

export interface TransitionResult {
  ok: boolean;
  previousStatus: TaskStatus;
  newStatus: TaskStatus;
  error?: string;
}

// ── Transition Rules ─────────────────────────────────────────

/**
 * Defines all valid state transitions for the Agent task lifecycle.
 * Each entry maps a source status to its allowed destination statuses
 * along with the trigger that causes the transition.
 */
const TRANSITION_RULES: Record<TaskStatus, Array<{ to: TaskStatus; trigger: string }>> = {
  [TaskStatus.IDLE]: [
    { to: TaskStatus.PLANNING, trigger: 'submit' },
  ],
  [TaskStatus.PLANNING]: [
    { to: TaskStatus.PENDING_REVIEW, trigger: 'plan_received' },
    { to: TaskStatus.FAILED, trigger: 'plan_failed' },
  ],
  [TaskStatus.PENDING_REVIEW]: [
    { to: TaskStatus.EXECUTING, trigger: 'approve' },
    { to: TaskStatus.DONE, trigger: 'reject' },
    { to: TaskStatus.PLANNING, trigger: 'modify' },
  ],
  [TaskStatus.EXECUTING]: [
    { to: TaskStatus.DELIVERING, trigger: 'all_steps_done' },
    { to: TaskStatus.FAILED, trigger: 'step_failed' },
    { to: TaskStatus.PAUSED, trigger: 'pause' },
  ],
  [TaskStatus.DELIVERING]: [
    { to: TaskStatus.DONE, trigger: 'delivery_confirmed' },
  ],
  [TaskStatus.DONE]: [],
  [TaskStatus.FAILED]: [
    { to: TaskStatus.IDLE, trigger: 'retry' },
  ],
  [TaskStatus.PAUSED]: [
    { to: TaskStatus.EXECUTING, trigger: 'resume' },
    { to: TaskStatus.ROLLED_BACK, trigger: 'rollback' },
  ],
  [TaskStatus.ROLLED_BACK]: [
    { to: TaskStatus.IDLE, trigger: 'retry' },
  ],
};

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── State Machine ────────────────────────────────────────────

/**
 * Manages the Agent task lifecycle state machine.
 *
 * State flow:
 *   idle → planning → pending_review → executing → delivering → done
 *                                                       ↘ failed
 *                                      executing ⇄ paused
 */
export class AgentStateMachine {
  private history: StateTransition[] = [];

  /**
   * Check whether a transition from the current status to the target
   * status is valid.
   */
  canTransition(from: TaskStatus, to: TaskStatus): boolean {
    const rules = TRANSITION_RULES[from];
    if (!rules) return false;
    return rules.some((r) => r.to === to);
  }

  /**
   * Attempt to transition a task from its current status to a new status.
   * Returns a result object indicating success or failure.
   */
  transition(currentStatus: TaskStatus, newStatus: TaskStatus, trigger: string): TransitionResult {
    if (!this.canTransition(currentStatus, newStatus)) {
      const msg = `Invalid transition: ${currentStatus} → ${newStatus} (trigger: ${trigger})`;
      logger.warn(msg, 'agent:state-machine');
      return { ok: false, previousStatus: currentStatus, newStatus: currentStatus, error: msg };
    }

    const transition: StateTransition = {
      from: currentStatus,
      to: newStatus,
      trigger,
      timestamp: new Date().toISOString(),
    };

    this.history.push(transition);
    logger.debug(
      `State transition: ${currentStatus} → ${newStatus} (${trigger})`,
      'agent:state-machine',
    );

    return { ok: true, previousStatus: currentStatus, newStatus };
  }

  /**
   * Get all valid next-states from the given status.
   */
  getAllowedTransitions(status: TaskStatus): TaskStatus[] {
    const rules = TRANSITION_RULES[status];
    return rules ? rules.map((r) => r.to) : [];
  }

  /**
   * Get the transition history for this machine instance.
   */
  getHistory(): ReadonlyArray<StateTransition> {
    return this.history;
  }

  /**
   * Clear transition history (e.g. when starting a new task).
   */
  clearHistory(): void {
    this.history = [];
  }

  /**
   * Determine the target status for a control action.
   * Returns null if the action is not recognized.
   */
  static resolveControlAction(
    currentStatus: TaskStatus,
    action: 'pause' | 'resume' | 'rollback_to',
  ): TaskStatus | null {
    switch (action) {
      case 'pause':
        return currentStatus === TaskStatus.EXECUTING ? TaskStatus.PAUSED : null;
      case 'resume':
        return currentStatus === TaskStatus.PAUSED ? TaskStatus.EXECUTING : null;
      case 'rollback_to':
        return TaskStatus.IDLE;
      default:
        return null;
    }
  }
}
