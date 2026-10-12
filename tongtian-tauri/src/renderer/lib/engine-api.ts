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
import type {
  AwarenessView,
  MemoryEntry,
  SandboxEnvClearResult,
  SandboxEnvUsage,
} from '../../shared/contracts';
import { handleLocalSecurityChannel } from './security-local';
import { handleLocalScheduleChannel } from './schedule-local';
import { handleLocalAlignmentChannel } from './alignment-local';
import { handleLocalWorkspaceChannel } from './workspace-local';
import { handleLocalSkillChannel } from './skill-local';
import { handleLocalSuiteChannel } from './suite-local';
import { handleLocalConversationChannel } from './conversation-local';
import { handleLocalMcpChannel } from './mcp-local';
import { handleLocalPermissionChannel } from './permissions-local';
import { extractReport, extractUsageRows, normalizePeriod, statsFromRows } from './usage-local';

const ENGINE_URL: string = import.meta.env.VITE_ENGINE_URL ?? 'http://127.0.0.1:18000';

/** 引擎用量明细（`model:usage` 与 `usage:stats` 共用同一次查询口径） */
async function fetchUsageRows(period: string) {
  const resp = await fetch(`${ENGINE_URL}/api/usage?period=${period}`);
  if (!resp.ok) {
    throw new Error(`USAGE_QUERY_FAILED: 引擎用量查询失败（HTTP ${resp.status}）`);
  }
  const data = (await resp.json()) as { rows?: unknown };
  return extractUsageRows(data);
}

/** 工作环境文件用量（引擎实测 {workDir}/environment） */
async function fetchEnvUsage(): Promise<SandboxEnvUsage> {
  const resp = await fetch(`${ENGINE_URL}/api/sandbox/environment/usage`);
  if (!resp.ok) {
    throw new Error(`SANDBOX_ENV_USAGE_FAILED: 工作环境用量统计失败（HTTP ${resp.status}）`);
  }
  return (await resp.json()) as SandboxEnvUsage;
}

/** 清理工作环境文件（只删 environment/ 内的内容，对话记录与产物保留） */
async function postEnvClear(): Promise<SandboxEnvClearResult> {
  const resp = await fetch(`${ENGINE_URL}/api/sandbox/environment/clear`, { method: 'POST' });
  if (!resp.ok) {
    throw new Error(`SANDBOX_ENV_CLEAR_FAILED: 工作环境清理失败（HTTP ${resp.status}）`);
  }
  return (await resp.json()) as SandboxEnvClearResult;
}

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

const ENGINE_TEXT_MIME: Record<string, string> = {
  md: 'text/markdown',
  markdown: 'text/markdown',
  html: 'text/html',
  htm: 'text/html',
  txt: 'text/plain',
  log: 'text/plain',
  json: 'application/json',
  csv: 'text/csv',
  py: 'text/x-python',
  js: 'text/javascript',
  ts: 'text/typescript',
};

/** 引擎 /api/files/content 响应（ok=false 时 message 为中文原因） */
interface EngineFileContent {
  ok?: boolean;
  fileName?: string;
  mime?: string;
  content?: string;
  message?: string;
}

async function fetchEngineFile(path: string): Promise<EngineFileContent> {
  const resp = await fetch(`${ENGINE_URL}/api/files/content?path=${encodeURIComponent(path)}`);
  const data = (await resp.json().catch(() => ({}))) as EngineFileContent;
  if (!resp.ok && !data.message) {
    throw new Error(`FILE_READ_FAILED: 引擎文件读取失败（HTTP ${resp.status}）`);
  }
  return data;
}

/**
 * 文件通道（引擎侧只有文本读取能力）：
 * - file:read / file:preview → 真实调用 /api/files/content
 * - file:browse → 引擎无目录接口，如实拒绝（不返回空列表伪装「目录为空」）
 * - Office/PDF 正文只有桌面版 Rust 解析器抽得出，这里显式引导到桌面版
 */
async function handleEngineFileChannel(channel: string, args: unknown[]): Promise<unknown> {
  if (channel === 'file:browse') {
    throw new Error('FILE_UNSUPPORTED: 引擎侧未提供目录浏览接口，请在桌面版通天办公中查看文件');
  }
  if (channel === 'file:read') {
    const filePath = String(args[0] ?? '');
    const data = await fetchEngineFile(filePath);
    if (!data.ok) throw new Error(`FILE_READ_FAILED: ${data.message ?? '引擎未能读取该文件'}`);
    return { path: filePath, content: data.content ?? '' };
  }
  if (channel === 'file:preview') {
    const paths = (args[0] as string[]) ?? [];
    return Promise.all(
      paths.map(async (filePath) => {
        const ext = (filePath.toLowerCase().match(/\.([a-z0-9]+)$/) ?? [])[1] ?? '';
        const mimeType = ENGINE_TEXT_MIME[ext] ?? 'application/octet-stream';
        const data = await fetchEngineFile(filePath);
        if (data.ok) {
          return { path: filePath, content: data.content ?? '', mimeType, supported: true };
        }
        const reason =
          data.message && !data.message.includes('二进制')
            ? data.message
            : '引擎直连通道不解析 Office/PDF 正文，请在桌面版通天办公中预览';
        return {
          path: filePath,
          content: '',
          mimeType,
          supported: false,
          note: `FILE_CONVERSION_FAILED: ${reason}`,
        };
      }),
    );
  }
  return undefined;
}

/**
 * 意识通道（引擎与桌面版读写**同一个** `~/.tongtianwork/awareness/main` 目录）：
 * 引擎侧接口返回 `{ok:false, code, message}` 时还原成 `CODE: 文案` 抛出，
 * 由渲染层 errorTextOf 统一映射，不与桌面端的两套错误口径打架。
 */
async function callAwareness<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${ENGINE_URL}${path}`, init);
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
  if (!resp.ok || data.ok === false) {
    const code = String(data.code ?? 'AWARENESS_LOAD_FAILED');
    const message = String(data.message ?? `引擎意识接口失败（HTTP ${resp.status}）`);
    throw new Error(`${code}: ${message}`);
  }
  return data as T;
}

const jsonPost = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

async function handleEngineAwarenessChannel(channel: string, args: unknown[]): Promise<unknown> {
  switch (channel) {
    case 'awareness:get':
      return callAwareness<AwarenessView>('/api/awareness');
    case 'awareness:update':
      return callAwareness<AwarenessView>('/api/awareness', jsonPost(args[0]));
    case 'awareness:memories': {
      const data = await callAwareness<{ entries?: MemoryEntry[] }>('/api/awareness/memories');
      return data.entries ?? [];
    }
    case 'awareness:memoryAdd': {
      const data = await callAwareness<{ entry?: MemoryEntry }>(
        '/api/awareness/memories',
        jsonPost(args[0]),
      );
      return data.entry;
    }
    case 'awareness:memoryDelete':
      return callAwareness('/api/awareness/memories/delete', jsonPost(args[0]));
    default:
      return undefined;
  }
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
    secureEnv?: boolean;
  }) => {
    const taskId = req.taskId || `task-${Date.now()}`;
    const resp = await postSSE('/api/chat', {
      message: req.goal,
      task_id: taskId,
      modelBinding: req.modelBinding,
      workDir: req.workDir,
      history: req.history,
      modelConfig: req.modelConfig,
      secureEnv: req.secureEnv,
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

  /** 通用通道兜底（引擎侧无实现的通道由本地权威层接管，未命中返回空列表） */
  invoke: async (channel: string, ...args: unknown[]) => {
    // 工作环境文件：引擎进程直接跑在本地磁盘上，用量与清理由引擎实测返回
    if (channel === 'sandbox:envUsage') return fetchEnvUsage();
    if (channel === 'sandbox:envClear') return postEnvClear();
    // 安全与治理：引擎侧无治理存储，走前端本地实现（分级/出域/沙箱/审计）
    const security = handleLocalSecurityChannel(channel, args);
    if (security.handled) return security.value;
    // 定时任务：引擎侧无调度服务，走前端本地实现（任务定义/执行记录/立即执行）
    const schedule = handleLocalScheduleChannel(channel, args);
    if (schedule.handled) return schedule.value;
    // 对齐整改通道：引擎侧无 Hook/连接器/归档/操控策略存储，走前端本地台账
    const alignment = handleLocalAlignmentChannel(channel, args);
    if (alignment.handled) return alignment.value;
    // 工作空间：引擎侧无工作区目录视图，走前端本地台账（与 Rust 磁盘口径同错误码）
    const workspace = handleLocalWorkspaceChannel(channel, args);
    if (workspace.handled) return workspace.value;
    // 技能：引擎侧无 SKILL.md 扫描能力，列表为空、写操作明确拒绝（不返回假技能）
    const skill = handleLocalSkillChannel(channel, args);
    if (skill.handled) return skill.value;
    // 专家套件：引擎进程无套件目录，走前端本地台账（同一个包照解析，桌面端由 Rust 落盘）
    const suite = handleLocalSuiteChannel(channel, args);
    if (suite.handled) return suite.value;
    // MCP 连接器：引擎侧无握手能力，走前端本地登记台账（连接一律如实拒绝，不伪造已连接）
    const mcp = handleLocalMcpChannel(channel, args);
    if (mcp.handled) return mcp.value;
    // 系统权限：引擎侧只有浏览器宿主，清单按当前平台如实给出，去授权拒绝
    const permission = handleLocalPermissionChannel(channel, args);
    if (permission.handled) return permission.value;
    // 会话正文（刷新恢复）：引擎进程无会话库，走前端本地台账（桌面端由 Rust 以 SQLite 为权威）
    const conversation = handleLocalConversationChannel(channel, args);
    if (conversation.handled) return conversation.value;
    // 意识（画像/能力文件/长期记忆）：引擎负责真实读写意识目录
    if (channel.startsWith('awareness:')) return handleEngineAwarenessChannel(channel, args);
    // 文件通道：引擎只提供文本读取，目录浏览与 Office/PDF 正文如实说明边界
    if (channel === 'file:read' || channel === 'file:preview' || channel === 'file:browse') {
      return handleEngineFileChannel(channel, args);
    }
    // 模型用量：引擎进程内台账为权威（与 Rust model_usage 同口径）
    if (channel === 'model:usage') {
      const period = normalizePeriod(args[0]);
      return { period, rows: await fetchUsageRows(period) };
    }
    // 用量总览：汇总口径由 usage-local 的 totalsOf 统一，与 Rust `totals_of` 一致
    if (channel === 'usage:stats') {
      const period = normalizePeriod(args[0]);
      return statsFromRows(period, await fetchUsageRows(period));
    }
    if (channel === 'usage:report') {
      const period = normalizePeriod(args[0]);
      const resp = await fetch(`${ENGINE_URL}/api/usage/report?period=${period}`);
      if (!resp.ok) {
        throw new Error(`USAGE_REPORT_FAILED: 引擎用量报表查询失败（HTTP ${resp.status}）`);
      }
      return extractReport(await resp.json(), period);
    }
    return [];
  },

  systemInfo: async () => ({ platform: 'engine-direct', version: '0.3.0' }),
  systemHealth: async () => ({ ok: true, details: { engine: ENGINE_URL } }),
};

export const ENGINE_DIRECT_URL = ENGINE_URL;
