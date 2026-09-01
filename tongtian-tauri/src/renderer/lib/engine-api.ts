/**
 * 通天办公 - 浏览器直连引擎 API（联调/真实测试通道）
 *
 * 非 Tauri 容器且 VITE_MOCK=false 时启用：
 * - chat 相关方法直连引擎 HTTP/SSE（真实模型调用）
 * - 其余方法兜底空数据，保证 UI 不崩溃
 *
 * 引擎地址：VITE_ENGINE_URL 可覆盖，默认 http://127.0.0.1:18000
 */

import type { ChatEvent } from '../../shared/types';

const ENGINE_URL: string = import.meta.env.VITE_ENGINE_URL ?? 'http://127.0.0.1:18000';

type EventCallback = (event: ChatEvent) => void;
const listeners = new Set<EventCallback>();

function emit(event: ChatEvent) {
  listeners.forEach((cb) => cb(event));
}

/** 解析 SSE 文本流并逐条发射事件 */
async function pumpSSE(body: ReadableStream<Uint8Array>): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let evType = '';
      let dataStr = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) evType = line.slice(6).trim();
        else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue; // keepalive 注释行 / 纯 id 行
      try {
        const data = JSON.parse(dataStr);
        // 引擎 SSE 的事件类型在 event: 行（data 内无 type），合并后发射
        const merged = { ...data, type: data.type ?? evType };
        if (merged.type) emit(merged as ChatEvent);
      } catch {
        // 解析失败忽略，不阻断流
      }
    }
  }
}

async function postSSE(path: string, body: unknown): Promise<Response> {
  const resp = await fetch(`${ENGINE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`引擎请求失败（HTTP ${resp.status}）`);
  return resp;
}

export const engineAPI = {
  /** 提交任务：SSE 事件流经全局监听分发 */
  chatSubmit: async (req: {
    goal: string;
    taskId?: string;
    attachments?: unknown[];
    modelBinding?: string;
    workDir?: string;
    history?: Array<{ role: 'user' | 'assistant'; content: string }>;
    modelConfig?: { baseUrl: string; apiKey: string; modelName: string };
  }) => {
    const taskId = req.taskId || `task-${Date.now()}`;
    const resp = await postSSE('/api/chat', {
      message: req.goal,
      task_id: taskId,
      modelBinding: req.modelBinding,
      workDir: req.workDir,
      history: req.history,
      modelConfig: req.modelConfig,
    });
    if (resp.body) void pumpSSE(resp.body);
    return { taskId };
  },

  /** 审阅（approve 返回 SSE 执行流，同样接入分发） */
  chatReview: async (taskId: string, action: 'approve' | 'reject' | 'modify', patch?: unknown[]) => {
    const resp = await postSSE('/api/chat/review', { task_id: taskId, action, patch });
    if (resp.body) void pumpSSE(resp.body);
    return { ok: true, status: action === 'approve' ? 'executing' : 'done' };
  },

  /** 任务控制：pause/resume/rollback_to */
  chatControl: async (req: { taskId: string; action: string; stepId?: string }) => {
    const resp = await postSSE('/api/chat/control', {
      task_id: req.taskId,
      action: req.action,
      step_id: req.stepId,
    });
    return (await resp.json()) as { ok: boolean; status?: string };
  },

  /** 取消任务 */
  chatCancel: async (taskId: string) => {
    const resp = await postSSE('/api/chat/cancel', { task_id: taskId });
    return (await resp.json()) as { taskId: string; cancelled: boolean };
  },

  /** 订阅事件流 */
  onChatEvent: (callback: EventCallback) => {
    listeners.add(callback);
    return () => {
      listeners.delete(callback);
    };
  },

  /** 历史任务（引擎侧暂未持久化，空列表兜底） */
  chatHistory: async () => [] as unknown[],

  /** 附件暂存：联调阶段引擎侧暂不落盘，返回透传 meta */
  fileStageAttachment: async (srcPath: string) => ({
    fileName: srcPath.split(/[/\\]/).pop() ?? 'attachment.bin',
    stagedPath: srcPath,
    sizeBytes: 0,
    mime: 'application/octet-stream',
    sensitivity: 'public',
  }),

  /** 通用通道兜底（skill:list 等保留基础数据） */
  invoke: async (channel: string) => {
    if (channel === 'skill:list') {
      return [{ id: 'local-cli', name: 'local-cli', description: '本地 CLI 与系统查询', injected: true }];
    }
    return [];
  },

  systemInfo: async () => ({ platform: 'engine-direct', version: '0.3.0' }),
  systemHealth: async () => ({ ok: true, details: { engine: ENGINE_URL } }),
};

export const ENGINE_DIRECT_URL = ENGINE_URL;
