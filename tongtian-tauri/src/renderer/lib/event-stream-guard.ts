// src/renderer/lib/event-stream-guard.ts — 事件流守卫（流 G2）
//
// 三层架构演进 P0：SSE seq 重放与断线补发在前端侧的消费保障。
// - per-task lastSeq 去重（重放场景防重复渲染，强制路径）
// - seq 缺口检测（记录并触发可选 replay 回调）
// - status_change 幂等（同状态覆盖）

import type { ChatEvent } from '../../shared/types';

export interface EventStreamGuardOptions {
  /** 检测到 seq 缺口时回调（可用于触发 event:replay 补齐） */
  onGap?: (taskId: string, expectedSeq: number, receivedSeq: number) => void;
}

interface TaskCursor {
  lastSeq: number;
  lastStatus?: string;
}

/**
 * 事件流守卫：包装 onChatEvent 回调，保证事件按 seq 单调消费。
 * 无 seq 的事件（旧引擎/Mock）直接放行，保持契约兼容。
 */
export class EventStreamGuard {
  private cursors = new Map<string, TaskCursor>();
  private readonly onGap?: EventStreamGuardOptions['onGap'];

  constructor(options?: EventStreamGuardOptions) {
    this.onGap = options?.onGap;
  }

  /** 获取 per-task 游标（taskId 缺省时用全局键） */
  private cursorOf(taskId: string): TaskCursor {
    let c = this.cursors.get(taskId);
    if (!c) {
      c = { lastSeq: 0 };
      this.cursors.set(taskId, c);
    }
    return c;
  }

  /**
   * 过滤事件：重复（seq ≤ lastSeq）丢弃；缺口触发 onGap；
   * status_change 同状态幂等去重。返回 null 表示应丢弃。
   */
  accept(event: ChatEvent): ChatEvent | null {
    const seq = (event as { seq?: number }).seq;
    const taskId =
      (event as { taskId?: string }).taskId ?? '__global__';

    if (seq === undefined || seq === null) {
      return event; // 无 seq：旧契约直通
    }

    const cursor = this.cursorOf(taskId);

    // 重复事件（重放/乱序旧事件）丢弃
    if (seq <= cursor.lastSeq) {
      return null;
    }

    // 缺口检测：lastSeq>0 且 seq > lastSeq+1
    if (cursor.lastSeq > 0 && seq > cursor.lastSeq + 1) {
      this.onGap?.(taskId, cursor.lastSeq + 1, seq);
    }
    cursor.lastSeq = seq;

    // status_change 幂等：同状态重复推送仅保留首次
    if (event.type === 'status_change') {
      const status = event.status as unknown as string;
      if (cursor.lastStatus === status) {
        return null;
      }
      cursor.lastStatus = status;
    }

    return event;
  }

  /** 包装回调：仅放行通过守卫的事件 */
  wrap(callback: (event: ChatEvent) => void): (event: ChatEvent) => void {
    return (event: ChatEvent) => {
      const passed = this.accept(event);
      if (passed) callback(passed);
    };
  }

  /** 任务终态后清理游标（防内存泄漏） */
  dispose(taskId: string): void {
    this.cursors.delete(taskId);
  }

  clear(): void {
    this.cursors.clear();
  }
}
