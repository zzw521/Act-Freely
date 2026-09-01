/**
 * 通天办公 (Tauri 版) - tongtianAPI Mock 垫片
 *
 * 遵循开发规范 Mock 开关机制：
 * - 开发阶段：Rust 后端 command 尚未实现时，注入模拟数据完成 UI 自测
 * - 联调阶段：Tauri command 就绪后，此垫片自动让位（仅当 window.tongtianAPI 不存在时注入）
 *
 * 通过 VITE_MOCK=false 可强制关闭 Mock。
 *
 * 核心闭环补齐：两阶段协议（plan → review → execute）、ModelHub CRUD、
 * 文件能力、附件暂存、设置持久化全部提供全真模拟。
 */

import type { ChatEvent } from '../../shared/types';
import { isTauri, tauriAPI, createProxiedAPI } from './tauri-api';
import { engineAPI, ENGINE_DIRECT_URL } from './engine-api';
import type { LeaseInfo, LeaseTakeoverResp, QuotaSnapshot } from '../../shared/contracts';

type EventCallback = (event: ChatEvent) => void;

const listeners = new Set<EventCallback>();

function emit(event: ChatEvent) {
  listeners.forEach((cb) => cb(event));
}

// ── Mock 状态 ────────────────────────────────────────────

let mockSeq = 0;
let mockUsedTokens = 0;
const MOCK_QUOTA_TOKENS = 200_000; // 对齐报告 ADJ-R5-3 免费层 20 万/日

interface MockTaskState {
  taskId: string;
  title: string;
  status: string;
  cancelled: boolean;
  steps: Array<{ id: string; order: number; description: string; type: string }>;
  createdAt: number;
  /** 会话子目录（工作目录下按对话时间创建，存放会话内容与产出物） */
  sessionDir: string;
}

/** 按对话时间生成会话子目录名：YYYY-MM-DD_HH-MM-SS */
function sessionDirName(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
}

let activeMockTask: MockTaskState | null = null;
/** 历史任务（chat_history Mock 数据源） */
const mockHistory: MockTaskState[] = [];

/** 构造带单调 seq 的事件（与引擎 SSE data.seq 契约一致） */
function seqEvent(taskId: string, event: Record<string, unknown>): ChatEvent {
  mockSeq += 1;
  return { ...event, seq: mockSeq, taskId } as unknown as ChatEvent;
}

function mockQuotaSnapshot(): QuotaSnapshot {
  const pct = MOCK_QUOTA_TOKENS > 0 ? (mockUsedTokens / MOCK_QUOTA_TOKENS) * 100 : 0;
  const tier = pct >= 100 ? '100' : pct >= 95 ? '95' : pct >= 85 ? '85' : '70';
  const action = pct >= 100 ? 'block' : pct >= 95 ? 'queue' : pct >= 85 ? 'degrade' : 'notify';
  return {
    usedTokens: mockUsedTokens,
    quotaTokens: MOCK_QUOTA_TOKENS,
    tier: tier as QuotaSnapshot['tier'],
    action: action as QuotaSnapshot['action'],
  };
}

function defaultSteps(title: string) {
  return [
    { id: 'step-1', order: 1, description: `解析任务目标：${title.slice(0, 20)}`, type: 'llm_call' },
    { id: 'step-2', order: 2, description: '生成执行计划草稿', type: 'llm_call' },
    { id: 'step-3', order: 3, description: '产出交付文件', type: 'file_write' },
  ];
}

/** 阶段一：计划生成流（queued → planning → plan → pending_review） */
function schedulePlanFlow(task: MockTaskState) {
  const planId = `mock-plan-${task.taskId}`;
  const timeline: Array<[number, () => void]> = [
    [50, () => emit(seqEvent(task.taskId, { type: 'status_change', status: 'queued', queuePosition: 0 }))],
    [120, () => emit(seqEvent(task.taskId, { type: 'status_change', status: 'planning' }))],
    [700, () => emit(seqEvent(task.taskId, { type: 'plan', planId, steps: task.steps }))],
    [760, () => {
      task.status = 'pending_review';
      emit(seqEvent(task.taskId, { type: 'status_change', status: 'pending_review' }));
    }],
    [820, () => emit(seqEvent(task.taskId, { type: 'done' }))],
  ];
  timeline.forEach(([delay, fire]) => setTimeout(fire, delay));
}

/** 阶段二：执行流（approve 后：executing → steps → deliver → done） */
function scheduleExecutionFlow(task: MockTaskState) {
  task.status = 'executing';
  const timeline: Array<[number, () => void]> = [
    [80, () => emit(seqEvent(task.taskId, { type: 'status_change', status: 'executing' }))],
    [300, () => emit(seqEvent(task.taskId, { type: 'step_start', stepId: 'step-1', stepType: 'llm_call', description: task.steps[0]?.description ?? '执行步骤 1' }))],
    [1100, () => emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-1', output: '目标解析完成', usage: { promptTokens: 800, completionTokens: 400 } }))],
    [1200, () => {
      // 配额预警示例：跨越 70% 档触发 quota_warning
      mockUsedTokens = Math.max(mockUsedTokens, Math.floor(MOCK_QUOTA_TOKENS * 0.72));
      const snap = mockQuotaSnapshot();
      emit(seqEvent(task.taskId, { type: 'quota_warning', level: snap.tier, action: snap.action, usedTokens: snap.usedTokens, quotaTokens: snap.quotaTokens }));
    }],
    [1500, () => emit(seqEvent(task.taskId, { type: 'step_start', stepId: 'step-2', stepType: 'llm_call', description: '生成执行计划草稿' }))],
    [2300, () => emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-2', output: '处理完成', usage: { promptTokens: 1200, completionTokens: 600 } }))],
    [2600, () => emit(seqEvent(task.taskId, { type: 'step_start', stepId: 'step-3', stepType: 'file_write', description: '产出交付文件' }))],
    [3400, () => emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-3', output: '文件已写入工作区', usage: { promptTokens: 900, completionTokens: 500 } }))],
    [3800, () => emit(seqEvent(task.taskId, {
      type: 'deliver',
      summary: '任务完成',
      // 产出物落在会话子目录（工作目录/对话时间）
      artifacts: ['成果报告.md', '数据摘要.xlsx'].map((f) => `${task.sessionDir}/${f}`),
    }))],
    [4200, () => {
      if (task.cancelled) {
        task.status = 'cancelled';
        emit(seqEvent(task.taskId, { type: 'status_change', status: 'cancelled', statusReasonCode: 'USER_CANCELLED' }));
      } else {
        task.status = 'done';
      }
      emit(seqEvent(task.taskId, { type: 'done' }));
      mockHistory.unshift(task);
      activeMockTask = null;
    }],
  ];
  timeline.forEach(([delay, fire]) => setTimeout(fire, delay));
}

// ── ModelHub Mock（内存 CRUD，模拟 SQLite 持久化语义） ──────

interface MockModel {
  id: string;
  name: string;
  source: string;
  baseUrl: string;
  apiKeyEncrypted: string;
  modelName: string;
  contextWindow: number;
  isDefault: boolean;
  agentBinding: string;
  forceLocal: boolean;
  hasApiKey: boolean;
  createdAt: string;
  updatedAt: string;
}

let mockModels: MockModel[] = [
  {
    id: 'mock-model-1',
    name: '通义千问（DashScope）',
    source: 'preset_qwen',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    apiKeyEncrypted: '',
    modelName: 'qwen-plus',
    contextWindow: 131072,
    isDefault: true,
    agentBinding: '',
    forceLocal: false,
    hasApiKey: true,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
  {
    id: 'mock-model-2',
    name: '本地 Ollama',
    source: 'ollama',
    baseUrl: 'http://127.0.0.1:11434/v1',
    apiKeyEncrypted: '',
    modelName: 'qwen2.5:14b',
    contextWindow: 32768,
    isDefault: false,
    agentBinding: '',
    forceLocal: true,
    hasApiKey: false,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
];

function handleModelChannel(channel: string, arg: unknown): unknown {
  const now = new Date().toISOString();
  if (channel === 'model:list') {
    return mockModels;
  }
  if (channel === 'model:create') {
    const req = (arg ?? {}) as Partial<MockModel> & { apiKey?: string };
    const model: MockModel = {
      id: `mock-model-${Date.now()}`,
      name: req.name ?? '未命名模型',
      source: req.source ?? 'openai_compat',
      baseUrl: req.baseUrl ?? '',
      apiKeyEncrypted: '',
      modelName: req.modelName ?? '',
      contextWindow: req.contextWindow ?? 4096,
      isDefault: req.isDefault ?? false,
      agentBinding: req.agentBinding ?? '',
      forceLocal: req.forceLocal ?? false,
      hasApiKey: Boolean(req.apiKey),
      createdAt: now,
      updatedAt: now,
    };
    if (model.isDefault) mockModels = mockModels.map((m) => ({ ...m, isDefault: false }));
    mockModels.push(model);
    return model;
  }
  if (channel === 'model:update') {
    const req = (arg ?? {}) as Partial<MockModel> & { apiKey?: string };
    mockModels = mockModels.map((m) =>
      m.id === req.id
        ? {
            ...m,
            ...req,
            apiKeyEncrypted: '',
            hasApiKey: req.apiKey !== undefined ? Boolean(req.apiKey) : m.hasApiKey,
            updatedAt: now,
          }
        : m,
    );
    return mockModels.find((m) => m.id === req.id) ?? null;
  }
  if (channel === 'model:delete') {
    mockModels = mockModels.filter((m) => m.id !== arg);
    return { ok: true };
  }
  if (channel === 'model:test') {
    const req = (arg ?? {}) as { id?: string; modelName?: string };
    const model = mockModels.find((m) => m.id === req.id);
    return {
      ok: true,
      latencyMs: 120 + Math.floor(Math.random() * 200),
      model: model?.modelName ?? req.modelName ?? 'mock',
    };
  }
  if (channel === 'model:setDefault') {
    mockModels = mockModels.map((m) => ({ ...m, isDefault: m.id === arg }));
    return { ok: true };
  }
  return [];
}

// ── 文件/附件/设置 Mock ──────────────────────────────────

const mockSettings: Record<string, string> = { theme: 'light', language: 'zh-CN' };

// ── 账号登录态 Mock（与 Rust 侧 TT_AUTH_MOCK 语义一致） ──────

/** Mock 账号资料（与 Rust service::mock_profile 同步） */
const MOCK_AUTH_PROFILE = {
  uid: 'tt-mock-8f3a2c91',
  nickname: '张先生',
  email: 'mock@tongtian.local',
  plan: '个人免费版',
  mock: true,
};

/** 当前 Mock 登录态（初始未登录，便于验收登录/退出闭环） */
let mockAuthProfile: typeof MOCK_AUTH_PROFILE | null = null;

function handleAuthChannel(channel: string): unknown {
  if (channel === 'auth:status') {
    return { loggedIn: mockAuthProfile !== null, profile: mockAuthProfile, mock: true };
  }
  if (channel === 'auth:login') {
    // 模拟网页登录往返（真实模式为浏览器 + loopback 回调）
    mockAuthProfile = { ...MOCK_AUTH_PROFILE };
    return { ok: true, profile: mockAuthProfile, mock: true };
  }
  if (channel === 'auth:logout') {
    const wasLoggedIn = mockAuthProfile !== null;
    mockAuthProfile = null;
    return { ok: true, wasLoggedIn };
  }
  return null;
}

function handleFileChannel(channel: string, args: unknown[]): unknown {
  if (channel === 'file:browse') {
    return [
      { name: 'attachments', path: '/mock/workspaces/default/files/attachments', isDirectory: true, size: 0, modifiedAt: String(Date.now()) },
      { name: '示例文档.md', path: '/mock/workspaces/default/files/示例文档.md', isDirectory: false, size: 2048, modifiedAt: String(Date.now()) },
    ];
  }
  if (channel === 'file:read') {
    return { path: args[0], content: '# 示例文档\n\n这是 Mock 模式下的文件内容。' };
  }
  if (channel === 'file:preview') {
    const paths = (args[0] as string[]) ?? [];
    return paths.map((p) => ({
      path: p,
      content: p.endsWith('.md') || p.endsWith('.txt') ? 'Mock 预览内容' : '',
      mimeType: p.endsWith('.md') ? 'text/markdown' : 'application/octet-stream',
      supported: p.endsWith('.md') || p.endsWith('.txt'),
      note: p.endsWith('.md') || p.endsWith('.txt') ? undefined : 'FILE_CONVERSION_FAILED: 格式转换能力下批落地',
    }));
  }
  return [];
}

/** 常用 channel 的 Mock 返回值 */
const channelMocks: Record<string, unknown> = {
  'model:usage': [],
  'workspace:list': [],
  'skill:list': [
    { id: 'local-cli', name: 'local-cli', description: '本地 CLI 与系统查询', injected: true },
  ],
  'mcp:list': [],
  'schedule:list': [],
  'audit:query': [],
  'system:info': { platform: 'mock', version: '0.1.0' },
  'system:health': { ok: true, details: { mock: true } },
};

const mockAPI = {
  // ── 通用 IPC 接口 ──────────────────────────────────────
  invoke: async (channel: string, ...args: unknown[]): Promise<unknown> => {
    console.info('[mock-api] invoke', channel);
    if (channel.startsWith('model:')) return handleModelChannel(channel, args[0]);
    if (channel.startsWith('file:')) return handleFileChannel(channel, args);
    if (channel === 'chat:history') {
      return mockHistory.map((t) => ({
        id: t.taskId,
        title: t.title,
        status: t.status,
        createdAt: t.createdAt,
        updatedAt: t.createdAt,
        meta: { retryCount: 0, queuePosition: 0 },
      }));
    }
    if (channel === 'settings:get') return { ...mockSettings };
    if (channel === 'settings:set') {
      mockSettings[String(args[0])] = String(args[1]);
      return { ok: true };
    }
    if (channel.startsWith('auth:')) return handleAuthChannel(channel);
    return channelMocks[channel] ?? [];
  },
  send: () => undefined,
  on: () => undefined,
  removeListener: () => undefined,
  removeAllListeners: () => undefined,

  // ── Chat（两阶段协议 Mock） ────────────────────────────
  chatSubmit: async (req: { goal: string; workDir?: string }) => {
    // 配额闸门 Mock：100% 档阻断新提交
    if (mockQuotaSnapshot().action === 'block') {
      throw new Error('QUOTA_EXCEEDED: 配额已用尽，新任务被阻断');
    }
    const now = Date.now();
    const taskId = `mock-task-${now}`;
    // 会话子目录：工作目录（未选则 Mock 缺省）下按对话时间创建
    const baseDir = req?.workDir || '/mock/workspaces/default/sessions';
    const task: MockTaskState = {
      taskId,
      title: (req?.goal ?? '').slice(0, 30),
      status: 'planning',
      cancelled: false,
      steps: defaultSteps(req?.goal ?? '新任务'),
      createdAt: now,
      sessionDir: `${baseDir}/${sessionDirName(now)}`,
    };
    activeMockTask = task;
    schedulePlanFlow(task);
    return { taskId, quota: mockQuotaSnapshot() };
  },

  /** 审阅（approve → 执行流；reject → done(REJECTED)；modify → 重新计划） */
  chatReview: async (taskId: string, action: 'approve' | 'reject' | 'modify', patch?: unknown[]) => {
    const task = activeMockTask ?? mockHistory.find((t) => t.taskId === taskId);
    if (!task) return { ok: false, status: 'TASK_NOT_FOUND' };
    if (action === 'approve') {
      scheduleExecutionFlow(task);
      return { ok: true, status: 'executing' };
    }
    if (action === 'reject') {
      task.status = 'done';
      emit(seqEvent(taskId, { type: 'status_change', status: 'done', statusReasonCode: 'REJECTED' }));
      emit(seqEvent(taskId, { type: 'done' }));
      mockHistory.unshift(task);
      activeMockTask = null;
      return { ok: true, status: 'done' };
    }
    // modify：合并 patch 后重新产出 plan
    if (Array.isArray(patch)) {
      for (const p of patch as Array<{ id?: string; name?: string }>) {
        const step = task.steps.find((s) => s.id === p.id);
        if (step && p.name) step.description = p.name;
      }
    }
    emit(seqEvent(taskId, { type: 'plan', planId: `mock-plan-${taskId}`, steps: task.steps }));
    emit(seqEvent(taskId, { type: 'status_change', status: 'pending_review' }));
    emit(seqEvent(taskId, { type: 'done' }));
    return { ok: true, status: 'pending_review' };
  },

  /** 控制（pause/resume/rollback_to） */
  chatControl: async (req: { taskId: string; action: string; stepId?: string }) => {
    const task = activeMockTask;
    if (!task || task.taskId !== req.taskId) return { ok: false, status: 'TASK_NOT_FOUND' };
    if (req.action === 'pause') {
      task.status = 'paused';
      emit(seqEvent(req.taskId, { type: 'status_change', status: 'paused', waitReason: 'user_pause' }));
      return { ok: true, status: 'paused' };
    }
    if (req.action === 'resume') {
      task.status = 'executing';
      emit(seqEvent(req.taskId, { type: 'status_change', status: 'executing' }));
      return { ok: true, status: 'executing' };
    }
    if (req.action === 'rollback_to') {
      task.status = 'rolled_back';
      emit(seqEvent(req.taskId, { type: 'status_change', status: 'rolled_back', statusReasonCode: `ROLLBACK_TO:${req.stepId ?? ''}` }));
      emit(seqEvent(req.taskId, { type: 'done' }));
      mockHistory.unshift(task);
      activeMockTask = null;
      return { ok: true, status: 'rolled_back' };
    }
    return { ok: false, status: 'VALIDATION_ERROR' };
  },

  chatHistory: async () =>
    mockHistory.map((t) => ({
      id: t.taskId,
      title: t.title,
      status: t.status,
      createdAt: t.createdAt,
      updatedAt: t.createdAt,
      meta: { retryCount: 0, queuePosition: 0 },
    })),

  // 三层架构演进 P0 Mock：取消/任务状态/配额/租约/重放
  chatCancel: async (taskId: string) => {
    if (activeMockTask && activeMockTask.taskId === taskId) {
      activeMockTask.cancelled = true;
      activeMockTask.status = 'cancelled';
      emit(seqEvent(taskId, { type: 'status_change', status: 'cancelled', statusReasonCode: 'USER_CANCELLED' }));
      emit(seqEvent(taskId, { type: 'done' }));
      mockHistory.unshift(activeMockTask);
      activeMockTask = null;
      return { taskId, cancelled: true };
    }
    return { taskId, cancelled: false };
  },
  taskState: async (taskId: string) => ({
    taskId,
    workspaceId: 'default',
    status: activeMockTask?.taskId === taskId ? activeMockTask.status : 'done',
    retryCount: 0,
    queuePosition: 0,
    cancelRequested: false,
    revision: 1,
  }),
  quotaStatus: async () => mockQuotaSnapshot(),
  leaseInfo: async (): Promise<{ holder: string; leases: LeaseInfo[] }> => ({
    holder: 'local-device',
    leases: activeMockTask
      ? [{
          taskId: activeMockTask.taskId,
          leaseId: 'mock-lease-1',
          holder: 'local-device',
          heartbeatAt: new Date().toISOString(),
          leaseUntil: new Date(Date.now() + 30_000).toISOString(),
          idle: false,
        }]
      : [],
  }),
  leaseTakeover: async (): Promise<LeaseTakeoverResp> => ({
    needConfirmation: false,
    message: '本地单端：可直接获取租约',
  }),
  /** 重放 Mock：无持久化，返回空窗口（windowExpired 语义） */
  eventReplay: async (taskId: string) => ({ taskId, events: [] }),
  /** 附件暂存 Mock：返回 AttachmentMeta（不落盘） */
  fileStageAttachment: async (srcPath: string, taskId?: string) => {
    const fileName = srcPath.split(/[\\/]/).pop() ?? 'attachment.bin';
    return {
      fileName,
      stagedPath: `/mock/workspaces/default/files/attachments/${taskId ?? 'pending'}/${fileName}`,
      sizeBytes: 1024,
      mime: fileName.endsWith('.md') ? 'text/markdown' : 'application/octet-stream',
      sensitivity: 'public',
    };
  },

  onChatEvent: (callback: EventCallback) => {
    listeners.add(callback);
    return () => {
      listeners.delete(callback);
    };
  },

  // ── System ─────────────────────────────────────────────
  systemInfo: async () => channelMocks['system:info'],
  systemHealth: async () => channelMocks['system:health'],
};

/**
 * 未实现的方法统一兜底：返回空数组（多数页面为列表查询）。
 * 已显式实现的方法优先使用。
 */
const proxiedAPI = new Proxy(mockAPI, {
  get(target, prop: string) {
    if (prop in target) {
      return (target as unknown as Record<string, unknown>)[prop];
    }
    return async (...args: unknown[]) => {
      console.info('[mock-api] fallback method', prop, args);
      return [];
    };
  },
});

// ── 注入策略 ──────────────────────────────────────────────
// 1. Tauri 容器内 → 真实 Rust command/event（联调模式）
// 2. 非 Tauri 且 VITE_MOCK=false → 浏览器直连引擎（真实模型测试）
// 3. 非 Tauri 且未关闭 Mock → 模拟数据（开发自测）

const mockDisabled = import.meta.env.VITE_MOCK === 'false';

if (typeof window !== 'undefined' && !window.tongtianAPI) {
  if (isTauri()) {
    (window as unknown as { tongtianAPI: unknown }).tongtianAPI = createProxiedAPI(tauriAPI);
    console.info('[api] Tauri command 模式已启用');
  } else if (mockDisabled) {
    const engineProxied = new Proxy(engineAPI, {
      get(target, prop: string) {
        if (prop in target) {
          return (target as unknown as Record<string, unknown>)[prop];
        }
        return async (...args: unknown[]) => {
          console.info('[engine-api] fallback method', prop, args);
          return [];
        };
      },
    });
    (window as unknown as { tongtianAPI: unknown }).tongtianAPI = engineProxied;
    console.info(`[api] 引擎直连模式已启用（${ENGINE_DIRECT_URL}）`);
  } else {
    (window as unknown as { tongtianAPI: unknown }).tongtianAPI = proxiedAPI;
    console.info('[api] Mock 模式已启用（VITE_MOCK=false 可关闭）');
  }
}

export {};
