// ============================================================
// src/main/agent/engine.ts — Agent engine main loop
// ============================================================

import crypto from 'crypto';
import { EventEmitter } from 'events';
import { TaskStatus, PlanStatus, Role } from '../../shared/types';
import type { Task, Message, Plan } from '../../shared/types';
import type { ChatSubmitReq, ChatReviewReq, ChatControlReq, ChatEvent, TaskSummary } from '../../shared/contracts';
import { ErrorCode } from '../../shared/contracts';
import Database from '../store/db';
import type { DatabaseLike } from '../store/db';
import Logger from '../util/logger';
import { getBridge } from '../bridge/agent-bridge';
import type { AgentEvent } from '../bridge/sse-parser';
import { AgentStateMachine } from './state-machine';
import { adaptAgentEvent } from './event-adapter';

// ── Types ────────────────────────────────────────────────────

export interface EngineEvents {
  'chat-event': (taskId: string, event: ChatEvent) => void;
  'task-status': (taskId: string, status: TaskStatus) => void;
  'error': (taskId: string, error: string) => void;
}

interface ActiveTask {
  taskId: string;
  workspaceId: string;
  stateMachine: AgentStateMachine;
  abortController: AbortController;
}

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── Helpers ──────────────────────────────────────────────────

function generateId(): string {
  return crypto.randomUUID();
}

function now(): string {
  return new Date().toISOString();
}

// ── Agent Engine ─────────────────────────────────────────────

export class AgentEngine extends EventEmitter {
  private activeTasks = new Map<string, ActiveTask>();

  /**
   * Submit a new goal: create Task → send via Bridge → receive Plan → wait review.
   */
  async submit(req: ChatSubmitReq): Promise<{ taskId: string }> {
    const db = Database.getInstance().getDb();
    const taskId = req.taskId || generateId();
    const timestamp = now();

    // Create task record (status = idle)
    db.prepare(
      `INSERT INTO tasks (id, title, status, user_id, plan_json, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    ).run(taskId, req.goal, TaskStatus.IDLE, 'default', null, timestamp, timestamp);

    // Persist user message
    const msgId = generateId();
    db.prepare(
      `INSERT INTO messages (id, task_id, role, content, attachments_json, ts)
       VALUES (?, ?, ?, ?, ?, ?)`,
    ).run(msgId, taskId, Role.USER, req.goal, '{}', timestamp);

    logger.info(`Task submitted: ${taskId}`, 'agent:engine', { goal: req.goal });

    // Transition: idle → planning
    const sm = new AgentStateMachine();
    this.transitionTask(db, taskId, sm, TaskStatus.PLANNING, 'submit');

    // Determine workspace — use first active workspace
    const bridge = getBridge();
    const workspaceIds = bridge.getActiveWorkspaceIds();
    if (workspaceIds.length === 0) {
      this.transitionTask(db, taskId, sm, TaskStatus.FAILED, 'no_workspace');
      this.emitEvent(taskId, {
        type: 'error',
        code: ErrorCode.WORKSPACE_NOT_FOUND,
        message: 'No active workspace available. Please create or resume a workspace.',
      });
      return { taskId };
    }
    const workspaceId = workspaceIds[0];

    // Register active task
    const abortController = new AbortController();
    this.activeTasks.set(taskId, { taskId, workspaceId, stateMachine: sm, abortController });

    // Send message to Agent via Bridge (non-blocking, process in background)
    this.processSubmission(taskId, workspaceId, req.goal, sm);

    return { taskId };
  }

  /**
   * Review a plan: approve / reject / modify.
   */
  async review(req: ChatReviewReq): Promise<{ ok: boolean }> {
    const db = Database.getInstance().getDb();

    // Find the task that owns this plan
    const task = this.findTaskByPlanId(db, req.planId);
    if (!task) {
      logger.warn(`Review failed: plan ${req.planId} not found`, 'agent:engine');
      return { ok: false };
    }

    const active = this.activeTasks.get(task.id);
    if (!active) {
      logger.warn(`Review failed: no active task for ${task.id}`, 'agent:engine');
      return { ok: false };
    }

    const { stateMachine: sm } = active;

    switch (req.action) {
      case 'approve': {
        // Update plan status in task's plan_json
        this.updatePlanStatus(db, task.id, req.planId, PlanStatus.APPROVED);
        this.transitionTask(db, task.id, sm, TaskStatus.EXECUTING, 'approve');

        // Notify Agent to start executing
        const bridge = getBridge();
        try {
          await bridge.sendMessage(active.workspaceId, `[PLAN_APPROVED] ${req.planId}`);
        } catch (err) {
          logger.error('Failed to notify Agent of plan approval', 'agent:engine', {
            error: err instanceof Error ? err.message : String(err),
          });
        }
        return { ok: true };
      }

      case 'reject': {
        this.updatePlanStatus(db, task.id, req.planId, PlanStatus.REJECTED);
        this.transitionTask(db, task.id, sm, TaskStatus.DONE, 'reject');

        // Persist assistant message noting rejection
        const msgId = generateId();
        db.prepare(
          `INSERT INTO messages (id, task_id, role, content, attachments_json, ts)
           VALUES (?, ?, ?, ?, ?, ?)`,
        ).run(msgId, task.id, Role.ASSISTANT, `Plan rejected: ${req.planId}`, '{}', now());

        this.emitEvent(task.id, { type: 'done', taskId: task.id });
        this.cleanupActiveTask(task.id);
        return { ok: true };
      }

      case 'modify': {
        // Update plan with patch and re-submit for planning
        this.updatePlanStatus(db, task.id, req.planId, PlanStatus.DRAFT);
        this.transitionTask(db, task.id, sm, TaskStatus.PLANNING, 'modify');

        const bridge = getBridge();
        try {
          const patchStr = JSON.stringify(req.patch || {});
          await bridge.sendMessage(
            active.workspaceId,
            `[PLAN_MODIFIED] ${req.planId} ${patchStr}`,
          );
          // Start listening for the updated plan
          this.listenForEvents(task.id, active.workspaceId, sm);
        } catch (err) {
          logger.error('Failed to notify Agent of plan modification', 'agent:engine', {
            error: err instanceof Error ? err.message : String(err),
          });
        }
        return { ok: true };
      }

      default:
        logger.warn(`Unknown review action: ${req.action}`, 'agent:engine');
        return { ok: false };
    }
  }

  /**
   * Control a running task: pause / resume / rollback.
   */
  async control(req: ChatControlReq): Promise<{ ok: boolean }> {
    const db = Database.getInstance().getDb();
    const taskRow = db.prepare('SELECT * FROM tasks WHERE id = ?').get(req.taskId) as
      Record<string, unknown> | undefined;

    if (!taskRow) {
      logger.warn(`Control failed: task ${req.taskId} not found`, 'agent:engine');
      return { ok: false };
    }

    const active = this.activeTasks.get(req.taskId);
    if (!active) {
      logger.warn(`Control failed: no active task for ${req.taskId}`, 'agent:engine');
      return { ok: false };
    }

    const currentStatus = taskRow.status as TaskStatus;
    const targetStatus = AgentStateMachine.resolveControlAction(currentStatus, req.action);

    if (!targetStatus) {
      logger.warn(
        `Control failed: action ${req.action} not valid for status ${currentStatus}`,
        'agent:engine',
      );
      return { ok: false };
    }

    this.transitionTask(db, req.taskId, active.stateMachine, targetStatus, req.action);

    // Forward control to Agent via Bridge
    const bridge = getBridge();
    try {
      await bridge.callTool(active.workspaceId, `task_${req.action}`, {
        taskId: req.taskId,
        stepId: req.stepId || null,
      });
    } catch (err) {
      logger.error(`Bridge call failed for control action ${req.action}`, 'agent:engine', {
        error: err instanceof Error ? err.message : String(err),
      });
    }

    // If rollback, emit event and cleanup
    if (req.action === 'rollback_to') {
      this.emitEvent(req.taskId, { type: 'done', taskId: req.taskId });
      this.cleanupActiveTask(req.taskId);
    }

    return { ok: true };
  }

  /**
   * Get task history with optional limit.
   */
  getHistory(limit = 50): TaskSummary[] {
    const db = Database.getInstance().getDb();
    const rows = db.prepare(
      `SELECT id, title, status, created_at, updated_at
       FROM tasks ORDER BY created_at DESC LIMIT ?`,
    ).all(limit) as Array<Record<string, unknown>>;

    return rows.map((row) => ({
      id: row.id as string,
      title: row.title as string,
      status: row.status as TaskStatus,
      createdAt: row.created_at as string,
      updatedAt: row.updated_at as string,
    }));
  }

  /**
   * Get the current status of a task.
   */
  getTaskStatus(taskId: string): TaskStatus | null {
    const db = Database.getInstance().getDb();
    const row = db.prepare('SELECT status FROM tasks WHERE id = ?').get(taskId) as
      { status: TaskStatus } | undefined;
    return row?.status ?? null;
  }

  /**
   * Cancel an active task (called during shutdown).
   */
  cancelTask(taskId: string): void {
    const active = this.activeTasks.get(taskId);
    if (active) {
      active.abortController.abort();
      this.cleanupActiveTask(taskId);
    }
  }

  /**
   * Cancel all active tasks (called during shutdown).
   */
  cancelAll(): void {
    for (const taskId of this.activeTasks.keys()) {
      this.cancelTask(taskId);
    }
  }

  // ── Private Methods ────────────────────────────────────────

  /**
   * Process a submitted goal by sending it to Agent and listening for events.
   */
  private async processSubmission(
    taskId: string,
    workspaceId: string,
    goal: string,
    sm: AgentStateMachine,
  ): Promise<void> {
    const db = Database.getInstance().getDb();
    const bridge = getBridge();

    try {
      // Send goal to Agent
      const eventStream = await bridge.sendMessage(workspaceId, goal);

      // Process SSE events
      for await (const agentEvent of eventStream) {
        // Check if task was cancelled
        const active = this.activeTasks.get(taskId);
        if (!active || active.abortController.signal.aborted) {
          logger.info(`Task ${taskId} was cancelled, stopping event processing`, 'agent:engine');
          break;
        }

        this.handleAgentEvent(taskId, sm, agentEvent);
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      logger.error(`Task ${taskId} submission failed`, 'agent:engine', { error: errMsg });

      this.transitionTask(db, taskId, sm, TaskStatus.FAILED, 'bridge_error');
      this.emitEvent(taskId, {
        type: 'error',
        code: ErrorCode.BRIDGE_COMMUNICATION_FAILED,
        message: errMsg,
      });
      this.cleanupActiveTask(taskId);
    }
  }

  /**
   * Listen for SSE events from Agent (used after plan modification).
   */
  private async listenForEvents(
    taskId: string,
    workspaceId: string,
    sm: AgentStateMachine,
  ): Promise<void> {
    // The modified plan will be re-sent by Agent as a new SSE stream
    // We just need to wait for the next sendMessage call to return events
    // This is handled by the processSubmission flow, so for modify we
    // simply let the existing flow continue
    logger.debug(`Listening for events on task ${taskId}`, 'agent:engine');
  }

  /**
   * Handle a single AgentEvent from the Agent SSE stream.
   */
  private handleAgentEvent(taskId: string, sm: AgentStateMachine, agentEvent: AgentEvent): void {
    const db = Database.getInstance().getDb();

    // Convert AgentEvent → ChatEvent
    const chatEvent = adaptAgentEvent(agentEvent, taskId);
    if (!chatEvent) return;

    // Drive state machine based on event type
    switch (agentEvent.type) {
      case 'plan': {
        // Store plan in task
        const planData = agentEvent.data;
        db.prepare('UPDATE tasks SET plan_json = ?, updated_at = ? WHERE id = ?').run(
          JSON.stringify(planData),
          now(),
          taskId,
        );
        this.transitionTask(db, taskId, sm, TaskStatus.PENDING_REVIEW, 'plan_received');
        this.emitEvent(taskId, chatEvent);
        break;
      }

      case 'step_start': {
        // Notify UI that a new step has started
        this.emitEvent(taskId, chatEvent);
        break;
      }

      case 'step_progress': {
        // Update progress percentage (optional)
        this.emitEvent(taskId, chatEvent);
        break;
      }

      case 'step_done': {
        // Step completed — persist output
        const { data } = agentEvent;
        db.prepare(
          'INSERT INTO task_steps (task_id, step_id, output, ts) VALUES (?, ?, ?, ?)',
        ).run(taskId, data.stepId, data.output, now());
        this.emitEvent(taskId, chatEvent);
        break;
      }

      case 'step_failed': {
        // Step failed — transition to failed
        const { data } = agentEvent;
        db.prepare(
          'INSERT INTO task_steps (task_id, step_id, output, ts) VALUES (?, ?, ?, ?)',
        ).run(taskId, data.stepId, `ERROR: ${data.error}`, now());
        
        this.transitionTask(db, taskId, sm, TaskStatus.FAILED, 'step_failed');
        this.emitEvent(taskId, chatEvent);
        this.cleanupActiveTask(taskId);
        return;
      }

      case 'deliver': {
        // All steps done, entering delivery phase
        this.transitionTask(db, taskId, sm, TaskStatus.DELIVERING, 'all_steps_done');
        
        // Persist delivery artifacts
        const { data } = agentEvent;
        db.prepare(
          'UPDATE tasks SET summary = ?, artifacts_json = ?, updated_at = ? WHERE id = ?',
        ).run(data.summary, JSON.stringify(data.artifacts), now(), taskId);
        
        this.emitEvent(taskId, chatEvent);
        break;
      }

      case 'done': {
        // Task fully completed
        // Transition through delivering → done if not already delivering
        const currentStatus = this.getTaskStatus(taskId);
        if (currentStatus === TaskStatus.DELIVERING) {
          this.transitionTask(db, taskId, sm, TaskStatus.DONE, 'delivery_confirmed');
        } else if (currentStatus === TaskStatus.EXECUTING) {
          // Direct done without explicit deliver event
          this.transitionTask(db, taskId, sm, TaskStatus.DELIVERING, 'all_steps_done');
          this.transitionTask(db, taskId, sm, TaskStatus.DONE, 'delivery_confirmed');
        }

        // Persist completion message
        const msgId = generateId();
        db.prepare(
          `INSERT INTO messages (id, task_id, role, content, attachments_json, ts)
           VALUES (?, ?, ?, ?, ?, ?)`,
        ).run(msgId, taskId, Role.ASSISTANT, 'Task completed', '{}', now());

        this.cleanupActiveTask(taskId);
        break;
      }

      case 'error': {
        this.transitionTask(db, taskId, sm, TaskStatus.FAILED, 'agent_error');
        this.cleanupActiveTask(taskId);
        break;
      }
    }

    // Emit ChatEvent for IPC forwarding
    this.emitEvent(taskId, chatEvent);
  }

  /**
   * Perform a state machine transition and persist the result.
   */
  private transitionTask(
    db: DatabaseLike,
    taskId: string,
    sm: AgentStateMachine,
    targetStatus: TaskStatus,
    trigger: string,
  ): void {
    const currentStatus = this.getTaskStatus(taskId);
    if (!currentStatus) return;

    const result = sm.transition(currentStatus, targetStatus, trigger);
    if (result.ok) {
      db.prepare('UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?').run(
        result.newStatus,
        now(),
        taskId,
      );
      this.emit('task-status', taskId, result.newStatus);
    }
  }

  /**
   * Emit a ChatEvent for the IPC layer to forward to the renderer.
   */
  private emitEvent(taskId: string, event: ChatEvent): void {
    this.emit('chat-event', taskId, event);
  }

  /**
   * Find the task that owns a given plan ID.
   */
  private findTaskByPlanId(
    db: DatabaseLike,
    planId: string,
  ): Task | null {
    // Plan ID is stored inside the plan_json of a task
    const rows = db.prepare(
      `SELECT id, title, status, user_id, plan_json, created_at, updated_at
       FROM tasks WHERE plan_json LIKE ?`,
    ).all(`%${planId}%`) as Array<Record<string, unknown>>;

    if (rows.length === 0) return null;

    const row = rows[0];
    return {
      id: row.id as string,
      title: row.title as string,
      status: row.status as TaskStatus,
      userId: row.user_id as string,
      planJson: row.plan_json as string | null,
      createdAt: row.created_at as string,
      updatedAt: row.updated_at as string,
    };
  }

  /**
   * Update the plan status within the task's plan_json field.
   */
  private updatePlanStatus(
    db: DatabaseLike,
    taskId: string,
    planId: string,
    status: PlanStatus,
  ): void {
    const row = db.prepare('SELECT plan_json FROM tasks WHERE id = ?').get(taskId) as
      { plan_json: string | null } | undefined;

    if (!row?.plan_json) return;

    try {
      const plan = JSON.parse(row.plan_json) as Record<string, unknown>;
      plan.status = status;
      plan.updatedAt = now();
      db.prepare('UPDATE tasks SET plan_json = ?, updated_at = ? WHERE id = ?').run(
        JSON.stringify(plan),
        now(),
        taskId,
      );
    } catch {
      logger.warn(`Failed to update plan status for task ${taskId}`, 'agent:engine');
    }
  }

  /**
   * Cleanup an active task entry after completion/failure.
   */
  private cleanupActiveTask(taskId: string): void {
    this.activeTasks.delete(taskId);
    logger.debug(`Active task cleaned up: ${taskId}`, 'agent:engine');
  }
}

// ── Singleton ────────────────────────────────────────────────

let engineInstance: AgentEngine | null = null;

export function getEngine(): AgentEngine {
  if (!engineInstance) {
    engineInstance = new AgentEngine();
  }
  return engineInstance;
}

export function resetEngine(): void {
  if (engineInstance) {
    engineInstance.cancelAll();
    engineInstance.removeAllListeners();
  }
  engineInstance = null;
}
