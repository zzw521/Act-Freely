// ============================================================
// src/main/agent/ipc-handler.ts — IPC handler for agent-engine
// ============================================================

import { ipcMain } from 'electron';
import { AgentEngine } from './engine';
import type {
  ChatSubmitReq,
  ChatReviewReq,
  ChatControlReq,
  ChatEvent,
  TaskSummary,
} from '../../shared/contracts';

// ── Constants ────────────────────────────────────────────────

const IPC_CHANNELS = {
  SUBMIT: 'agent:submit',
  REVIEW: 'agent:review',
  CONTROL: 'agent:control',
  GET_HISTORY: 'agent:get-history',
  GET_STATUS: 'agent:get-task-status',
  EVENT_REPLY: 'agent:event-reply',
} as const;

// ── IPC Handler ──────────────────────────────────────────────

export function setupAgentIpcHandler(engine: AgentEngine) {
  // Event forwarding to renderer
  engine.on('chat-event', (taskId, event) => {
    ipcMain.emit(IPC_CHANNELS.EVENT_REPLY, { taskId, event });
  });

  engine.on('task-status', (taskId, status) => {
    ipcMain.emit(IPC_CHANNELS.EVENT_REPLY, { taskId, type: 'status', status });
  });

  engine.on('error', (taskId, error) => {
    ipcMain.emit(IPC_CHANNELS.EVENT_REPLY, { taskId, type: 'error', error });
  });

  // Request handling
  ipcMain.handle(IPC_CHANNELS.SUBMIT, async (_, req: ChatSubmitReq) => {
    return engine.submit(req);
  });

  ipcMain.handle(IPC_CHANNELS.REVIEW, async (_, req: ChatReviewReq) => {
    return engine.review(req);
  });

  ipcMain.handle(IPC_CHANNELS.CONTROL, async (_, req: ChatControlReq) => {
    return engine.control(req);
  });

  ipcMain.handle(IPC_CHANNELS.GET_HISTORY, async (_, limit: number) => {
    return engine.getHistory(limit);
  });

  ipcMain.handle(IPC_CHANNELS.GET_STATUS, async (_, taskId: string) => {
    return engine.getTaskStatus(taskId);
  });
}
