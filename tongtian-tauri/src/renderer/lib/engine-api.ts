/**
 * 通天办公 - 浏览器直连引擎 API（联调/真实测试通道）
 *
 * 非 Tauri 容器且 VITE_MOCK=false 时启用：
 * - chat 相关方法直连引擎 HTTP/SSE（真实模型调用）
 * - 其余方法兜底空数据，保证 UI 不崩溃
 *
 * 引擎地址：VITE_ENGINE_URL 可覆盖，默认 http://127.0.0.1:18000
 */

import type { AgentPolicy, ChatEvent, PiiOutboundMode, PptxTemplatesPayload } from '../../shared/types';
import { appVersion } from '../../shared/app-version';
import type {
  AwarenessView,
  BuiltinTool,
  BuiltinToolList,
  ImChannelView,
  ImSession,
  MemoryEntry,
  SandboxEnvClearResult,
  SandboxEnvUsage,
} from '../../shared/contracts';
import { DECLARED_BUILTIN_TOOLS } from '../../shared/builtin-tools';
import { handleLocalSecurityChannel } from './security-local';
import { handleLocalScheduleChannel } from './schedule-local';
import { handleLocalAlignmentChannel } from './alignment-local';
import { handleLocalWorkspaceChannel } from './workspace-local';
import { handleLocalSkillChannel } from './skill-local';
import { handleLocalSuiteChannel } from './suite-local';
import { handleLocalMarketChannel } from './market-local';
import { handleLocalConversationChannel } from './conversation-local';
import { handleLocalMcpChannel } from './mcp-local';
import { handleLocalCollabChannel } from './collab-local';
import { handleLocalPermissionChannel } from './permissions-local';
import { extractReport, extractUsageRows, normalizePeriod, statsFromRows } from './usage-local';
import { ENGINE_DIRECT_URL } from './engine-endpoint';
import { engineFetch } from './engine-http';

const ENGINE_URL = ENGINE_DIRECT_URL;

/** 引擎用量明细（`model:usage` 与 `usage:stats` 共用同一次查询口径） */
async function fetchUsageRows(period: string) {
  const resp = await engineFetch(`${ENGINE_URL}/api/usage?period=${period}`);
  if (!resp.ok) {
    throw new Error(`USAGE_QUERY_FAILED: 引擎用量查询失败（HTTP ${resp.status}）`);
  }
  const data = (await resp.json()) as { rows?: unknown };
  return extractUsageRows(data);
}

/** 工作环境文件用量（引擎实测 {workDir}/environment） */
async function fetchEnvUsage(): Promise<SandboxEnvUsage> {
  const resp = await engineFetch(`${ENGINE_URL}/api/sandbox/environment/usage`);
  if (!resp.ok) {
    throw new Error(`SANDBOX_ENV_USAGE_FAILED: 工作环境用量统计失败（HTTP ${resp.status}）`);
  }
  return (await resp.json()) as SandboxEnvUsage;
}

/** 清理工作环境文件（只删 environment/ 内的内容，对话记录与产物保留） */
async function postEnvClear(): Promise<SandboxEnvClearResult> {
  const resp = await engineFetch(`${ENGINE_URL}/api/sandbox/environment/clear`, { method: 'POST' });
  if (!resp.ok) {
    throw new Error(`SANDBOX_ENV_CLEAR_FAILED: 工作环境清理失败（HTTP ${resp.status}）`);
  }
  return (await resp.json()) as SandboxEnvClearResult;
}

/** 内置工具清单：读引擎 Toolkit 注册表（实测值，不在前端另抄一份） */
async function fetchBuiltinTools(): Promise<BuiltinToolList> {
  const resp = await engineFetch(`${ENGINE_URL}/api/tools`);
  const body = await resp.json().catch(() => null);
  if (!resp.ok || !body || body.ok === false) {
    throw new Error(
      `TOOLS_LIST_FAILED: 内置工具清单读取失败（HTTP ${resp.status}${body?.detail ? `：${body.detail}` : ''}）`,
    );
  }
  const declared = new Map<string, { name: string; desc: string }>(
    DECLARED_BUILTIN_TOOLS.map((t) => [t.id, { name: t.name, desc: t.desc }]),
  );
  const tools = (body.tools as BuiltinTool[]).map((t) => ({
    ...t,
    // 引擎给的是函数名，展示名/说明按声明清单补齐；清单外的（如 MCP 工具）保留原名
    name: declared.get(t.id)?.name ?? t.name,
    desc: declared.get(t.id)?.desc || t.desc || '',
  }));
  return {
    source: 'engine',
    count: tools.length,
    tools,
    note: '清单读自运行中引擎的工具注册表（实测值）',
  };
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
  const resp = await engineFetch(`${ENGINE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    // 引擎的拒绝原因（MODEL_NOT_FOUND / MODEL_KEY_MISSING 等）必须原样带给用户，
    // 只报 HTTP 状态码会把「选错模型」误报成「引擎挂了」
    const data = (await resp.json().catch(() => ({}))) as { code?: string; message?: string; detail?: string };
    const detail = data.message || data.detail || `引擎请求失败（HTTP ${resp.status}）`;
    throw new Error(data.code ? `${data.code}: ${detail}` : detail);
  }
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
  const resp = await engineFetch(`${ENGINE_URL}/api/files/content?path=${encodeURIComponent(path)}`);
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
    throw new Error('FILE_UNSUPPORTED: 引擎侧未提供目录浏览接口，请在桌面版任我行中查看文件');
  }
  if (channel === 'file:openExternal') {
    // 浏览器直连/网页宿主没有系统默认应用通道，不能假装打开成功
    throw new Error('OPEN_EXTERNAL_UNSUPPORTED');
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
            : '引擎直连通道不解析 Office/PDF 正文，请在桌面版任我行中预览';
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
  throw new Error(
    `IPC_CHANNEL_UNHANDLED: 引擎直连通道未实现 ${channel}（已阻止静默返回空结果）`,
  );
}

/**
 * 意识通道（引擎与桌面版读写**同一个** `~/.tongtianwork/awareness/main` 目录）：
 * 引擎侧接口返回 `{ok:false, code, message}` 时还原成 `CODE: 文案` 抛出，
 * 由渲染层 errorTextOf 统一映射，不与桌面端的两套错误口径打架。
 */
async function callAwareness<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await engineFetch(`${ENGINE_URL}${path}`, init);
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
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: 引擎直连通道未实现 ${channel}（已阻止静默返回空结果）`,
      );
  }
}

/**
 * IM 频道通道：长连接只能由引擎进程持有，所以浏览器直连时问的正是权威方，
 * 不存在「前端自己维护一份频道状态」的第二真值。
 * 错误口径与桌面端一致（`CODE: detail` 抛出，交 errorTextOf 映射）。
 */
async function callIm<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await engineFetch(`${ENGINE_URL}${path}`, init).catch(() => null);
  if (!resp) {
    throw new Error('IM_ENGINE_UNREACHABLE: 本地引擎无响应，无法查看或绑定 IM 频道');
  }
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
  if (!resp.ok || data.ok === false) {
    const code = String(data.code ?? 'IM_ENGINE_ERROR');
    const message = String(data.message ?? `引擎频道接口失败（HTTP ${resp.status}）`);
    throw new Error(`${code}: ${message}`);
  }
  return data as T;
}

async function handleEngineImChannel(channel: string, args: unknown[]): Promise<unknown> {
  const req = (args[0] ?? {}) as {
    kind?: string;
    credentials?: Record<string, string>;
    target?: string;
    text?: string;
  };
  const kind = encodeURIComponent(req.kind ?? '');
  switch (channel) {
    case 'im:channels':
      return callIm<{ channels?: ImChannelView[]; sessions?: ImSession[] }>('/api/im/channels');
    case 'im:configure':
      return callIm(`/api/im/${kind}/config`, jsonPost({ credentials: req.credentials ?? {} }));
    case 'im:start':
      return callIm(`/api/im/${kind}/start`, jsonPost({}));
    case 'im:stop':
      return callIm(`/api/im/${kind}/stop`, jsonPost({}));
    case 'im:forget':
      return callIm(`/api/im/${kind}/forget`, jsonPost({}));
    case 'im:send':
      return callIm(
        `/api/im/${kind}/send`,
        jsonPost({ target: req.target ?? '', text: req.text ?? '' }),
      );
    case 'im:preferencesGet':
      return callIm('/api/im/preferences');
    case 'im:preferencesSet': {
      const patch = ((args[0] as { preferences?: Record<string, boolean> })?.preferences) ?? {};
      return callIm('/api/im/preferences', jsonPost({ preferences: patch }));
    }
    default:
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: 引擎直连通道未实现 ${channel}（已阻止静默返回空结果）`,
      );
  }
}

/**
 * 模型 CRUD 通道：浏览器直连时引擎的 models.json 是权威数据源。
 * 读取视图不含明文密钥；写入时 apiKey 缺省=保留，空串=清除。
 */
async function callModelApi<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await engineFetch(`${ENGINE_URL}${path}`, init);
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
  if (!resp.ok || data.ok === false) {
    const code = String(data.code ?? 'MODEL_ENGINE_ERROR');
    const message = String(data.message ?? `引擎模型接口失败（HTTP ${resp.status}）`);
    throw new Error(`${code}: ${message}`);
  }
  return data as T;
}

async function handleEngineModelChannel(channel: string, args: unknown[]): Promise<unknown> {
  const req = (args[0] ?? {}) as Record<string, unknown>;
  switch (channel) {
    case 'model:list': {
      const data = await callModelApi<{ models?: unknown[] }>('/api/models');
      return data.models ?? [];
    }
    case 'model:create': {
      const data = await callModelApi<{ model?: unknown }>('/api/models', jsonPost(req));
      return data.model;
    }
    case 'model:update': {
      const data = await callModelApi<{ model?: unknown }>('/api/models/update', jsonPost(req));
      return data.model;
    }
    case 'model:delete':
      await callModelApi('/api/models/delete', jsonPost(req));
      return { ok: true };
    case 'model:setDefault':
      await callModelApi('/api/models/default', jsonPost(req));
      return { ok: true };
    case 'model:test':
      return callModelApi('/api/models/test', jsonPost(req));
    case 'model:detectLocal':
      return callModelApi('/api/models/detect-local', jsonPost({ baseUrl: req.baseUrl ?? '' }));
    default:
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: 引擎直连通道未实现 ${channel}（已阻止静默返回空结果）`,
      );
  }
}

export const engineAPI = {
  /** 提交任务：SSE 事件流经全局监听分发 */
  chatSubmit: async (req: {
    goal: string;
    taskId?: string;
    mode?: 'general' | 'design' | 'slides' | 'writing';
    attachments?: unknown[];
    modelBinding?: string;
    modelId?: string;
    /** 负责人专家：浏览器直连没有专家注册表，引擎会明确拒绝而不是静默降级 */
    expertId?: string;
    workDir?: string;
    history?: Array<{ role: 'user' | 'assistant'; content: string }>;
    secureEnv?: boolean;
    // 浏览器直连没有 Rust 侧 runtime 装配，脱敏档位只能随请求下发
    piiOutboundMode?: PiiOutboundMode;
    // 同上：效率策略走请求顶层，引擎只在 runtime 未携带 agentPolicy 时采用
    agentPolicy?: AgentPolicy;
    // 同上：幻灯片模板只带内置主题，引擎只在 runtime 未携带 pptxTemplates 时采用
    pptxTemplates?: PptxTemplatesPayload;
  }) => {
    const taskId = req.taskId || `task-${Date.now()}`;
    const resp = await postSSE('/api/chat', {
      message: req.goal,
      task_id: taskId,
      mode: req.mode,
      // 浏览器直连也带附件：此前这里漏了 attachments，选中的文件会被静默丢掉
      attachments: req.attachments,
      modelBinding: req.modelBinding,
      // 只传 id：端点与密钥由引擎从 models.json 解析，渲染层不再持有 apiKey
      modelId: req.modelId,
      expertId: req.expertId,
      workDir: req.workDir,
      history: req.history,
      secureEnv: req.secureEnv,
      piiOutboundMode: req.piiOutboundMode,
      agentPolicy: req.agentPolicy,
      pptxTemplates: req.pptxTemplates,
    });
    if (resp.body) void pumpSSE(resp.body);
    return { taskId };
  },

  /** 审阅（approve 返回 SSE 执行流，同样接入分发） */
  chatReview: async (taskId: string, action: 'approve' | 'reject' | 'modify' | 'resume', patch?: unknown[]) => {
    const resp = await postSSE('/api/chat/review', { task_id: taskId, action, patch });
    if (resp.body) void pumpSSE(resp.body);
    return { ok: true, status: action === 'approve' ? 'executing' : 'done' };
  },

  /** 取消任务 */
  chatCancel: async (taskId: string) => {
    const resp = await postSSE('/api/chat/cancel', { task_id: taskId });
    return (await resp.json()) as { taskId: string; cancelled: boolean; status?: string };
  },

  /** 事件重放：引擎侧按 seq 返回会话窗口内的原始事件（窗口外 events 为空） */
  eventReplay: async (taskId: string, fromSeq = 0) => {
    const resp = await engineFetch(
      `${ENGINE_URL}/api/events/replay?task_id=${encodeURIComponent(taskId)}&from_seq=${fromSeq}`,
    );
    if (!resp.ok) {
      throw new Error(`EVENT_REPLAY_FAILED: 引擎事件重放失败（HTTP ${resp.status}）`);
    }
    return (await resp.json()) as {
      taskId: string;
      events: unknown[];
      windowExpired?: boolean;
    };
  },

  /** 批量查询检查点摘要：刷新/重启后恢复续跑入口（引擎只返回摘要，不含正文与密钥） */
  chatCheckpointQuery: async (taskIds: string[]) => {
    const resp = await engineFetch(`${ENGINE_URL}/api/chat/checkpoint/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: taskIds }),
    });
    if (!resp.ok) {
      throw new Error(`CHECKPOINT_QUERY_FAILED: 检查点查询失败（HTTP ${resp.status}）`);
    }
    return (await resp.json()) as { checkpoints: Record<string, unknown> };
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
    // 内置工具：引擎直连时读它的真实注册表（不是前端抄一份清单）
    if (channel === 'agent:builtinTools') return fetchBuiltinTools();
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
    // 在线市场：引擎进程不能把技能/套件写进桌面运行目录，如实回报 supported=false
    const market = handleLocalMarketChannel(channel, args);
    if (market.handled) return market.value;
    // MCP 连接器：引擎侧无握手能力，走前端本地登记台账（连接一律如实拒绝，不伪造已连接）
    const mcp = handleLocalMcpChannel(channel, args);
    if (mcp.handled) return mcp.value;
    // 项目协作：需要桌面端的系统凭据库与持 Authorization 的本机客户端，浏览器通道如实拒绝
    const collab = handleLocalCollabChannel(channel);
    if (collab.handled) return collab.value;
    // 系统权限：引擎侧只有浏览器宿主，清单按当前平台如实给出，去授权拒绝
    const permission = handleLocalPermissionChannel(channel, args);
    if (permission.handled) return permission.value;
    // 会话正文（刷新恢复）：引擎进程无会话库，走前端本地台账（桌面端由 Rust 以 SQLite 为权威）
    const conversation = handleLocalConversationChannel(channel, args);
    if (conversation.handled) return conversation.value;
    // 意识（画像/能力文件/长期记忆）：引擎负责真实读写意识目录
    if (channel.startsWith('awareness:')) return handleEngineAwarenessChannel(channel, args);
    // IM 频道：长连接只在引擎进程里，前端不缓存状态，问的就是权威方
    if (channel.startsWith('im:')) return handleEngineImChannel(channel, args);
    // 专家团队：注册表在桌面端 SQLite，浏览器通道没有可读写的权威存储
    if (channel.startsWith('experts:')) {
      throw new Error(
        'EXPERTS_UNSUPPORTED: 专家团队需要桌面版注册表，浏览器直连通道不可用（请用桌面版管理）',
      );
    }
    // 文件通道：引擎只提供文本读取，目录浏览与 Office/PDF 正文如实说明边界
    if (channel === 'file:read' || channel === 'file:preview' || channel === 'file:browse' || channel === 'file:openExternal') {
      return handleEngineFileChannel(channel, args);
    }
    // 模型配置：浏览器直连时引擎 models.json 是权威（CRUD 全部真实落盘）
    if (channel.startsWith('model:') && channel !== 'model:usage') {
      return handleEngineModelChannel(channel, args);
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
      const resp = await engineFetch(`${ENGINE_URL}/api/usage/report?period=${period}`);
      if (!resp.ok) {
        throw new Error(`USAGE_REPORT_FAILED: 引擎用量报表查询失败（HTTP ${resp.status}）`);
      }
      return extractReport(await resp.json(), period);
    }
    // 引擎直连通道没有实现的通道必须显式失败，不能拿空数组冒充「没有数据」
    throw new Error(
      `IPC_CHANNEL_UNHANDLED: 引擎直连通道未实现 ${channel}（已阻止静默返回空结果）`,
    );
  },

  systemInfo: async () => ({ name: 'tongtian-tauri', platform: 'engine-direct', version: appVersion(), arch: '' }),
};

export { ENGINE_DIRECT_URL };
