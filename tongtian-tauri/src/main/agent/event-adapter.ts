// ============================================================
// src/main/agent/event-adapter.ts — Agent SSE → ChatEvent
// ============================================================

import { StepType } from '../../shared/types';
import { ErrorCode } from '../../shared/contracts';
import type { ChatEvent, ChatStepPreview } from '../../shared/contracts';
import type { AgentEvent } from '../bridge/sse-parser';
import Logger from '../util/logger';

// ── Constants ────────────────────────────────────────────────

const logger = Logger.getInstance();

// ── Step Type Mapping ────────────────────────────────────────

/**
 * Map Agent step type strings to the StepType enum.
 */
function mapStepType(raw: string): StepType {
  const mapping: Record<string, StepType> = {
    llm_call: StepType.LLM_CALL,
    file_read: StepType.FILE_READ,
    file_write: StepType.FILE_WRITE,
    file_convert: StepType.FILE_CONVERT,
    file_organize: StepType.FILE_ORGANIZE,
    user_confirm: StepType.USER_CONFIRM,
    skill_call: StepType.SKILL_CALL,
    mcp_tool: StepType.MCP_TOOL,
    workspace_delegate: StepType.WORKSPACE_DELEGATE,
  };
  return mapping[raw] || StepType.LLM_CALL;
}

// ── Event Adapter ────────────────────────────────────────────

/**
 * Convert a Agent AgentEvent into a ChatEvent for IPC forwarding.
 *
 * Returns null if the event cannot be mapped (should not happen in
 * normal operation; logged as a warning).
 */
export function adaptAgentEvent(event: AgentEvent, taskId: string): ChatEvent | null {
  const { type, data } = event;

  switch (type) {
    case 'plan':
      return adaptPlanEvent(data);

    case 'step_start':
      return adaptStepStartEvent(data);

    case 'step_progress':
      return adaptStepProgressEvent(data);

    case 'step_done':
      return adaptStepDoneEvent(data);

    case 'step_failed':
      return adaptStepFailedEvent(data);

    case 'deliver':
      return adaptDeliverEvent(data, taskId);

    case 'done':
      return { type: 'done', taskId };

    case 'error':
      return adaptErrorEvent(data);

    default:
      logger.warn(`Unknown agent event type: ${type}`, 'agent:event-adapter');
      return null;
  }
}

// ── Individual Adapters ──────────────────────────────────────

function adaptPlanEvent(data: Record<string, unknown>): ChatEvent {
  const planId = (data.planId ?? data.plan_id ?? '') as string;
  const rawSteps = (data.steps ?? []) as Array<Record<string, unknown>>;

  const steps: ChatStepPreview[] = rawSteps.map((s, index) => ({
    id: (s.id ?? s.step_id ?? `step-${index}`) as string,
    order: (s.order ?? index) as number,
    type: mapStepType((s.type ?? 'llm_call') as string),
    description: (s.description ?? '') as string,
  }));

  return { type: 'plan', planId, steps };
}

function adaptStepStartEvent(data: Record<string, unknown>): ChatEvent {
  return {
    type: 'step_start',
    stepId: (data.stepId ?? data.step_id ?? '') as string,
    stepType: mapStepType((data.type ?? 'llm_call') as string),
    description: (data.description ?? '') as string,
  };
}

function adaptStepProgressEvent(data: Record<string, unknown>): ChatEvent {
  return {
    type: 'step_progress',
    stepId: (data.stepId ?? data.step_id ?? '') as string,
    progress: typeof data.progress === 'number' ? data.progress : 0,
    message: (data.message ?? '') as string,
  };
}

function adaptStepDoneEvent(data: Record<string, unknown>): ChatEvent {
  return {
    type: 'step_done',
    stepId: (data.stepId ?? data.step_id ?? '') as string,
    output: (data.output ?? '') as string,
  };
}

function adaptStepFailedEvent(data: Record<string, unknown>): ChatEvent {
  return {
    type: 'step_failed',
    stepId: (data.stepId ?? data.step_id ?? '') as string,
    error: (data.error ?? data.message ?? 'Unknown error') as string,
  };
}

function adaptDeliverEvent(data: Record<string, unknown>, taskId: string): ChatEvent {
  const artifacts = Array.isArray(data.artifacts)
    ? (data.artifacts as string[])
    : [];

  return {
    type: 'deliver',
    taskId: (data.taskId ?? taskId) as string,
    summary: (data.summary ?? '') as string,
    artifacts,
  };
}

function adaptErrorEvent(data: Record<string, unknown>): ChatEvent {
  const rawCode = (data.code ?? 'INTERNAL_ERROR') as string;
  const code = (ErrorCode as Record<string, ErrorCode>)[rawCode] ?? ErrorCode.INTERNAL_ERROR;

  return {
    type: 'error',
    code,
    message: (data.message ?? 'An unknown error occurred') as string,
  };
}

// ── Batch Adapter ────────────────────────────────────────────

/**
 * Convert an array of AgentEvents to ChatEvents, filtering out
 * any that cannot be mapped.
 */
export function adaptAgentEvents(events: AgentEvent[], taskId: string): ChatEvent[] {
  const results: ChatEvent[] = [];
  for (const event of events) {
    const adapted = adaptAgentEvent(event, taskId);
    if (adapted) {
      results.push(adapted);
    }
  }
  return results;
}
