/**
 * 通天办公 - Preload API 类型声明
 *
 * 为 window.tongtianAPI 提供完整的 TypeScript 类型支持。
 * 此文件由 src/preload/index.ts 在运行时通过 contextBridge 注入。
 */

interface TongTianAPI {
  /**
   * 类型安全的 IPC invoke（请求/响应模式）。
   * 向主进程发送消息并等待响应。
   */
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;

  /**
   * Fire-and-forget IPC send（无需响应）。
   */
  send(channel: string, ...args: unknown[]): void;

  /**
   * 监听主进程推送的事件（如流式 Chat 事件）。
   */
  on(channel: string, callback: (...args: unknown[]) => void): void;

  /**
   * 移除指定事件监听器。
   */
  removeListener(channel: string, callback: (...args: unknown[]) => void): void;

  /**
   * 移除指定频道的全部监听器。
   */
  removeAllListeners(channel: string): void;

  // ── 便捷方法 ────────────────────────────────────────────────────

  /** 提交对话目标，触发 Agent 流程 */
  chatSubmit(req: { taskId?: string; goal: string; modelBinding?: string }): Promise<unknown>;

  /** 审阅 Plan（批准 / 拒绝 / 修改） */
  chatReview(req: { planId: string; action: 'approve' | 'reject' | 'modify'; patch?: unknown }): Promise<unknown>;

  /** 控制任务（暂停 / 继续 / 回滚） */
  chatControl(req: { taskId: string; action: 'pause' | 'resume' | 'rollback_to'; stepId?: string }): Promise<unknown>;

  /** 监听 Chat 流式事件，返回取消监听函数 */
  onChatEvent(callback: (event: unknown) => void): () => void;
}

declare global {
  interface Window {
    tongtianAPI?: TongTianAPI;
  }
}

export {};
