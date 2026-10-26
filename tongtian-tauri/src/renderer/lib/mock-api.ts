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

import type { ChatEvent, ChatSubmitReq } from '../../shared/types';
import { appVersion } from '../../shared/app-version';
import { declaredBuiltinToolList } from '../../shared/builtin-tools';
import { isTauri, tauriAPI, createProxiedAPI } from './tauri-api';
import { engineAPI, ENGINE_DIRECT_URL } from './engine-api';
import type { QuotaSnapshot } from '../../shared/contracts';
import { handleLocalSecurityChannel } from './security-local';
import { handleLocalScheduleChannel } from './schedule-local';
import { handleLocalAlignmentChannel } from './alignment-local';
import { handleLocalAwarenessChannel } from './awareness-local';
import { handleLocalConversationChannel } from './conversation-local';
import { handleLocalWorkspaceChannel } from './workspace-local';
import { handleLocalSkillChannel } from './skill-local';
import { handleLocalSuiteChannel } from './suite-local';
import { handleLocalMarketChannel } from './market-local';
import { handleLocalMcpChannel } from './mcp-local';
import { handleLocalCollabChannel } from './collab-local';
import { assertProxyPatchAcceptable, localProxyStatus } from './proxy-local';
import { localAutostart } from './system-local';
import { handleLocalPermissionChannel } from './permissions-local';
import {
  localReportView,
  localUsageRows,
  normalizePeriod,
  recordLocalUsage,
  statsFromRows,
} from './usage-local';

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
  /**
   * 本轮对话选中的模型（用量归属随选择，而不是选了「经济」仍记到默认模型头上）。
   * Auto / 未匹配到清单时回落默认模型，与引擎的 modelBinding 覆盖语义一致。
   */
  model?: { modelName: string; displayName: string };
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

/** 当前 Mock 默认模型（用量归属：与 Rust 侧按 model_configs.model_name 归属同口径） */
function mockActiveModel(): { modelName: string; displayName: string } {
  const m = mockModels.find((x) => x.isDefault) ?? mockModels[0];
  return { modelName: m?.modelName || 'mock-model', displayName: m?.name || 'Mock 模型' };
}

/** 对话内选择的模型 → 用量归属对象。
 * 优先级与引擎一致：档位绑定 > 已保存模型 id > 旧式 modelConfig > 默认模型。
 * id 找不到时如实报错，不拿清单首项冒充用户选择。 */
function resolveMockModel(
  modelBinding?: string,
  modelId?: string,
  legacyModelConfig?: { modelName?: string },
): { modelName: string; displayName: string } {
  const binding = (modelBinding ?? '').trim();
  if (binding) {
    const hit = mockModels.find((m) => m.modelName === binding || m.id === binding);
    return { modelName: binding, displayName: hit?.name ?? binding };
  }
  const selectedId = (modelId ?? '').trim();
  if (selectedId) {
    const hit = mockModels.find((m) => m.id === selectedId);
    if (!hit) throw new Error(`MODEL_NOT_FOUND: 未找到模型 ${selectedId}`);
    return { modelName: hit.modelName || selectedId, displayName: hit.name || hit.modelName };
  }
  const legacyName = (legacyModelConfig?.modelName ?? '').trim();
  if (legacyName) {
    const hit = mockModels.find((m) => m.modelName === legacyName);
    return { modelName: legacyName, displayName: hit?.name ?? legacyName };
  }
  return mockActiveModel();
}

/** Mock 用量记账：写入本地台账（Mock 通道权威数据源，供 model:usage 查询）。
 * 耗时与成本只用于开发预览演示口径；真实环境由引擎上报，价格来自模型配置。 */
function recordMockUsage(
  promptTokens: number,
  completionTokens: number,
  model?: { modelName: string; displayName: string },
): void {
  const { modelName, displayName } = model ?? mockActiveModel();
  const hit = mockModels.find((m) => m.modelName === modelName);
  const inputPrice = hit?.priceInputPerM;
  const outputPrice = hit?.priceOutputPerM;
  const priced =
    typeof inputPrice === 'number' && typeof outputPrice === 'number';
  const costMicros = priced
    ? Math.max(0, Math.round(promptTokens * inputPrice + completionTokens * outputPrice))
    : null;
  const latencyMs = 300 + Math.round((promptTokens + completionTokens) * 0.4);
  recordLocalUsage(modelName, promptTokens, completionTokens, {
    displayName,
    latencyMs,
    costMicros,
    currency: hit?.priceCurrency || 'CNY',
    priced,
  });
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
    // 计划生成的真实 token 用量（与引擎同位置：plan 之后、pending_review 之前）
    [730, () => {
      const modelName = task.model?.modelName ?? mockActiveModel().modelName;
      recordMockUsage(600, 300, task.model);
      emit(seqEvent(task.taskId, { type: 'usage', usage: { promptTokens: 600, completionTokens: 300, model: modelName } }));
    }],
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
    [1100, () => {
      recordMockUsage(800, 400, task.model);
      emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-1', output: '目标解析完成', usage: { promptTokens: 800, completionTokens: 400 } }));
    }],
    [1200, () => {
      // 配额预警示例：跨越 70% 档触发 quota_warning
      mockUsedTokens = Math.max(mockUsedTokens, Math.floor(MOCK_QUOTA_TOKENS * 0.72));
      const snap = mockQuotaSnapshot();
      emit(seqEvent(task.taskId, { type: 'quota_warning', level: snap.tier, action: snap.action, usedTokens: snap.usedTokens, quotaTokens: snap.quotaTokens }));
    }],
    [1500, () => emit(seqEvent(task.taskId, { type: 'step_start', stepId: 'step-2', stepType: 'llm_call', description: '生成执行计划草稿' }))],
    [2300, () => {
      recordMockUsage(1200, 600, task.model);
      emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-2', output: '处理完成', usage: { promptTokens: 1200, completionTokens: 600 } }));
    }],
    [2600, () => emit(seqEvent(task.taskId, { type: 'step_start', stepId: 'step-3', stepType: 'file_write', description: '产出交付文件' }))],
    [3400, () => {
      recordMockUsage(900, 500, task.model);
      emit(seqEvent(task.taskId, { type: 'step_done', stepId: 'step-3', output: '文件已写入工作区', usage: { promptTokens: 900, completionTokens: 500 } }));
    }],
    [3800, () => emit(seqEvent(task.taskId, {
      type: 'deliver',
      summary: '任务完成',
      // 助手回复正文：界面气泡取 final_text（与引擎/Rust deliver 同字段），Mock 同样给真正文
      // `[test:html]` 是演示/验收标记：给一段 html 代码块，用于验证「生成式 UI」开关的沙箱渲染
      final_text: task.title.includes('[test:html]')
        ? [
            '下面是一个可交互的示例组件：',
            '',
            '```html',
            '<!doctype html>',
            '<html><body style="font-family:system-ui;padding:16px">',
            '<h3>生成式 UI 沙箱示例</h3>',
            '<button id="b">点我 +1</button> <span id="n">0</span>',
            '<script>let n=0;document.getElementById("b").onclick=()=>{n++;document.getElementById("n").textContent=n;}</script>',
            '</body></html>',
            '```',
          ].join('\n')
        : `已按目标「${task.title}」完成分析与处理，交付文件见下方附件。`,
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
  priceInputPerM?: number | null;
  priceOutputPerM?: number | null;
  priceCurrency?: string;
  fallbackIds?: string[];
  createdAt: string;
  updatedAt: string;
}

/** 写入请求：单价走嵌套 prices（与 Rust/引擎契约一致），空值表示清空/未配置 */
type MockModelWrite = Partial<Omit<MockModel, 'priceInputPerM' | 'priceOutputPerM' | 'priceCurrency'>> & {
  apiKey?: string;
  prices?: {
    inputPerM?: number | null;
    outputPerM?: number | null;
    currency?: string;
  } | null;
};

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
    priceInputPerM: 0.8,
    priceOutputPerM: 2,
    priceCurrency: 'CNY',
    fallbackIds: ['mock-model-2'],
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
    priceInputPerM: 0,
    priceOutputPerM: 0,
    priceCurrency: 'CNY',
    fallbackIds: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
];

/** `api.model.remove/setDefault` 传的是 { id }，旧 Mock 曾把整个对象当 id 比较 */
function argModelId(arg: unknown): string {
  if (typeof arg === 'string') return arg.trim();
  if (arg && typeof arg === 'object') {
    const id = (arg as { id?: unknown }).id;
    if (typeof id === 'string') return id.trim();
  }
  return '';
}

function handleModelChannel(channel: string, arg: unknown): unknown {
  const now = new Date().toISOString();
  if (channel === 'model:list') {
    return mockModels;
  }
  // 用量统计：本地台账（Mock 通道权威数据源；与 Rust model_usage 同结构）
  if (channel === 'model:usage') {
    const period = normalizePeriod(arg);
    return { period, rows: localUsageRows(period) };
  }
  // 用量总览 / 报表：同一本地台账，映射函数与引擎直连通道共用（口径不漂移）
  if (channel === 'usage:stats') {
    const period = normalizePeriod(arg);
    return statsFromRows(period, localUsageRows(period));
  }
  if (channel === 'usage:report') {
    return localReportView(normalizePeriod(arg));
  }
  if (channel === 'model:create') {
    const req = (arg ?? {}) as MockModelWrite;
    const fallbackIds = (req.fallbackIds ?? []).slice(0, 3);
    if (fallbackIds.some((id) => !mockModels.some((m) => m.id === id))) {
      throw new Error('MODEL_FALLBACK_NOT_FOUND: 未找到转移目标');
    }
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
      priceInputPerM: req.prices?.inputPerM ?? null,
      priceOutputPerM: req.prices?.outputPerM ?? null,
      priceCurrency: (req.prices?.currency || 'CNY').toUpperCase(),
      fallbackIds,
      createdAt: now,
      updatedAt: now,
    };
    if (model.isDefault) mockModels = mockModels.map((m) => ({ ...m, isDefault: false }));
    mockModels.push(model);
    return model;
  }
  if (channel === 'model:update') {
    const req = (arg ?? {}) as MockModelWrite;
    if (!req.id) throw new Error('MODEL_INVALID: 缺少模型 id');
    if (!mockModels.some((m) => m.id === req.id)) {
      throw new Error(`MODEL_NOT_FOUND: 未找到模型 ${req.id}`);
    }
    if (req.fallbackIds) {
      if (req.fallbackIds.length > 3) {
        throw new Error('MODEL_FALLBACK_LIMIT: 故障转移链最多 3 个模型');
      }
      if (req.fallbackIds.includes(req.id)) {
        throw new Error('MODEL_FALLBACK_SELF: 故障转移链不能指向自身');
      }
      if (new Set(req.fallbackIds).size !== req.fallbackIds.length) {
        throw new Error('MODEL_FALLBACK_DUPLICATE: 故障转移链重复');
      }
      if (req.fallbackIds.some((id) => !mockModels.some((m) => m.id === id))) {
        throw new Error('MODEL_FALLBACK_NOT_FOUND: 未找到转移目标');
      }
    }
    // 密钥字段不得随 patch 落进清单（Mock 也不保留明文，只留 hasApiKey）
    const { apiKey: keyInput, prices, ...patch } = req;
    // 默认模型最多一个
    if (patch.isDefault === true) {
      mockModels = mockModels.map((m) => ({ ...m, isDefault: false }));
    }
    mockModels = mockModels.map((m) =>
      m.id === req.id
        ? {
            ...m,
            ...patch,
            ...(prices !== undefined
              ? {
                  priceInputPerM: prices?.inputPerM ?? null,
                  priceOutputPerM: prices?.outputPerM ?? null,
                  priceCurrency: (prices?.currency || m.priceCurrency || 'CNY').toUpperCase(),
                }
              : {}),
            apiKeyEncrypted: '',
            hasApiKey: keyInput !== undefined ? Boolean(keyInput) : m.hasApiKey,
            updatedAt: now,
          }
        : m,
    );
    return mockModels.find((m) => m.id === req.id) ?? null;
  }
  if (channel === 'model:delete') {
    const id = argModelId(arg);
    if (!id) throw new Error('MODEL_INVALID: 缺少模型 id');
    if (!mockModels.some((m) => m.id === id)) {
      throw new Error(`MODEL_NOT_FOUND: 未找到模型 ${id}`);
    }
    mockModels = mockModels
      .filter((m) => m.id !== id)
      .map((m) =>
        m.fallbackIds?.includes(id)
          ? { ...m, fallbackIds: m.fallbackIds.filter((v) => v !== id) }
          : m,
      );
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
  // 本机 Ollama 探测是真实外部依赖：Mock 通道不假装探测过，如实说明
  if (channel === 'model:detectLocal') {
    return {
      supported: false,
      available: false,
      endpoint: 'http://127.0.0.1:11434',
      openaiBaseUrl: 'http://127.0.0.1:11434/v1',
      latencyMs: 0,
      models: [],
      error:
        'LOCAL_MODEL_UNSUPPORTED: Mock 通道不探测本机 Ollama；桌面版会真实请求 /api/tags',
    };
  }
  if (channel === 'model:setDefault') {
    const id = argModelId(arg);
    if (!id) throw new Error('MODEL_INVALID: 缺少模型 id');
    if (!mockModels.some((m) => m.id === id)) {
      throw new Error(`MODEL_NOT_FOUND: 未找到模型 ${id}`);
    }
    mockModels = mockModels.map((m) => ({ ...m, isDefault: m.id === id }));
    return { ok: true, defaultId: id };
  }
  throw new Error(
    `IPC_CHANNEL_UNHANDLED: Mock 通道未实现 ${channel}（已阻止静默返回空结果）`,
  );
}

// ── 文件/附件/设置 Mock ──────────────────────────────────

const MOCK_SETTINGS_LS_KEY = 'tt:settings';

function readMockSettingsSnapshot(): Record<string, unknown> {
  try {
    if (typeof localStorage === 'undefined') return {};
    const raw = localStorage.getItem(MOCK_SETTINGS_LS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function persistMockSettings(settings: Record<string, unknown>): void {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(MOCK_SETTINGS_LS_KEY, JSON.stringify(settings));
    }
  } catch {
    // 受限环境（node/隐私模式）只保留内存台账
  }
}

/**
 * 设置台账。与真实 Rust `settings_kv` 同语义：保存即持久，刷新不丢
 * （否则开发预览里改完主题一刷新就回退，无法自测设置的生效链路）。
 */
const mockSettings: Record<string, unknown> = {
  theme: 'light',
  language: 'zh-CN',
  ...readMockSettingsSnapshot(),
};

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
  if (channel === 'auth:serverProbe') {
    // 浏览器 Mock 没有 Rust 侧网络能力，如实返回不可达而不是编一个假连接
    return {
      baseUrl: 'http://127.0.0.1:8000',
      reachable: false,
      service: null,
      version: null,
      error: 'AUTH_SERVER_UNREACHABLE: 浏览器 Mock 模式不连接账号服务，请在桌面版验证真实登录',
    };
  }
  return null;
}

// Mock 侧 MIME 判定与 Rust `mime_of` 同表（前端按扩展名展示，不参与安全裁定）
const MIME_BY_EXT: Record<string, string> = {
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.csv': 'text/csv',
  '.json': 'application/json',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.pdf': 'application/pdf',
  '.doc': 'application/msword',
  '.xls': 'application/vnd.ms-excel',
  '.ppt': 'application/vnd.ms-powerpoint',
};

function handleFileChannel(channel: string, args: unknown[]): unknown {
  if (channel === 'file:openExternal') {
    throw new Error('OPEN_EXTERNAL_UNSUPPORTED');
  }
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
    return paths.map((p) => {
      const lower = p.toLowerCase();
      const textLike = /\.(md|txt|csv|json)$/.test(lower);
      if (textLike) {
        return {
          path: p,
          content: '# 示例文档\n\n这是 Mock 模式下的文件正文。',
          mimeType: MIME_BY_EXT[lower.slice(lower.lastIndexOf('.'))] ?? 'text/plain',
          supported: true,
        };
      }
      // 本机文档正文只有桌面版（Rust + office 解析）拿得到，这里不伪造「已提取」
      return {
        path: p,
        content: '',
        mimeType: MIME_BY_EXT[lower.slice(lower.lastIndexOf('.'))] ?? 'application/octet-stream',
        supported: false,
        note: 'FILE_UNSUPPORTED: 当前运行通道无法读取本机文档正文，请在桌面版通天办公中预览',
      };
    });
  }
  throw new Error(
    `IPC_CHANNEL_UNHANDLED: Mock 通道未实现 ${channel}（已阻止静默返回空结果）`,
  );
}

/** 常用 channel 的 Mock 返回值 */
const channelMocks: Record<string, unknown> = {
  // 版本号取构建期注入的 package.json 版本（桌面通道由 Rust system_info 给 CARGO_PKG_VERSION）
  // 浏览器/Mock 没有托盘进程：后台常驻能力如实为 false，设置页据此置灰
  'system:info': {
    name: 'tongtian-tauri',
    platform: 'mock',
    version: appVersion(),
    arch: '',
    backgroundResident: false,
  },
};

const mockAPI = {
  // ── 通用 IPC 接口 ──────────────────────────────────────
  invoke: async (channel: string, ...args: unknown[]): Promise<unknown> => {
    console.info('[mock-api] invoke', channel);
    if (channel.startsWith('model:')) return handleModelChannel(channel, args[0]);
    // 用量总览/报表与模型用量同源，交给同一个本地台账处理函数
    if (channel === 'usage:stats' || channel === 'usage:report') {
      return handleModelChannel(channel, args[0]);
    }
    if (channel.startsWith('file:')) return handleFileChannel(channel, args);
    if (
      channel.startsWith('security:') ||
      channel.startsWith('compliance:') ||
      channel.startsWith('sandbox:') ||
      channel.startsWith('audit:')
    ) {
      // 与浏览器直连引擎通道共用同一份本地实现（无 Rust 后端时的 Mock 开关兜底）
      const local = handleLocalSecurityChannel(channel, args);
      if (local.handled) return local.value;
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: Mock 通道未实现 ${channel}（已阻止静默返回空结果）`,
      );
    }
    // 定时任务：与浏览器直连引擎通道共用同一份本地实现（任务定义/执行记录/立即执行）
    if (channel.startsWith('schedule:') || channel === 'keepAwake:status') {
      const local = handleLocalScheduleChannel(channel, args);
      if (local.handled) return local.value;
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: Mock 通道未实现 ${channel}（已阻止静默返回空结果）`,
      );
    }
    // 对齐整改通道（Hooks/连接器/归档/电脑操控）：与引擎直连共用本地权威实现
    {
      const local = handleLocalAlignmentChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 工作空间：与引擎直连共用本地台账（桌面端由 Rust 以磁盘目录为权威）
    {
      const local = handleLocalWorkspaceChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 技能：浏览器通道无落盘能力，列表如实为空、写操作明确拒绝（桌面端由 Rust 扫描 skills 目录）
    {
      const local = handleLocalSkillChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 专家套件：浏览器通道不写盘，但同一个 .zip 包照解析、台账照维护
    {
      const local = handleLocalSuiteChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 在线市场：需要下载 + 落盘，浏览器通道如实回报 supported=false（桌面端由 Rust 实现）
    {
      const market = handleLocalMarketChannel(channel, args);
      if (market.handled) return market.value;
    }
    // MCP 连接器：浏览器通道无握手能力，走本地登记台账（连接如实拒绝，不伪造已连接/工具清单）
    {
      const local = handleLocalMcpChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 项目协作：需要桌面端凭据库与账号服务直连能力，Mock/浏览器通道如实回报不支持
    {
      const local = handleLocalCollabChannel(channel);
      if (local.handled) return local.value;
    }
    // 意识（画像/能力文件/长期记忆）：浏览器通道无落盘能力，走本地台账（桌面端由 Rust 读写意识目录）
    {
      const local = handleLocalAwarenessChannel(channel, args);
      if (local.handled) return local.value;
    }
    // 会话正文（刷新恢复）：浏览器通道无 SQLite，走本地台账，与 Rust 同口径
    {
      const local = handleLocalConversationChannel(channel, args);
      if (local.handled) return local.value;
    }
    // IM 频道：长连接只有引擎进程能持有，浏览器没有可信状态可展示。
    // 这里不编频道清单也不做假连接——真实验证请用引擎直连（VITE_MOCK=false），
    // 外部 IM 服务侧的 Mock 开关在引擎（TT_SLACK_API_BASE 指向假 Slack，链路照跑）。
    if (channel.startsWith('im:')) {
      throw new Error(
        'IM_ENGINE_UNAVAILABLE: Mock 模式没有引擎进程，无法持有 IM 长连接；请用 VITE_MOCK=false 启动引擎直连模式',
      );
    }
    // 专家团队：Mock 通道没有 Rust 注册表，不编假专家，明确告知不可用
    if (channel.startsWith('experts:')) {
      throw new Error(
        'EXPERTS_UNSUPPORTED: Mock 模式没有专家注册表，请用桌面版或引擎直连通道',
      );
    }
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
    if (channel === 'proxy:status') return localProxyStatus(mockSettings);
    // 内置工具：演示通道给声明清单（与引擎注册表同源，契约测试锁定），不另编一份
    if (channel === 'agent:builtinTools') return declaredBuiltinToolList('mock');
    // 浏览器预览环境写不了系统启动项：如实报 supported=false，勾了也不算已生效
    if (channel === 'system:autostart') return localAutostart(mockSettings);
    // 系统权限：清单同 Rust 表，状态只问系统能回答的项；浏览器不能拉起系统设置
    {
      const local = handleLocalPermissionChannel(channel, args);
      if (local.handled) return local.value;
    }
    if (channel === 'settings:set') {
      // 设置页以对象 patch 整体提交（与 Rust settings_set_many 同口径）
      const patch = (args[0] ?? {}) as Record<string, unknown>;
      // 代理三键按「补丁叠加现值」整组校验后再落库，与 Rust 一致：
      // 自定义却没填地址 → 整批拒绝，不留半个生效配置
      assertProxyPatchAcceptable(mockSettings, patch);
      Object.entries(patch).forEach(([k, v]) => {
        mockSettings[k] = v;
      });
      persistMockSettings(mockSettings);
      return { ok: true, keys: Object.keys(patch) };
    }
    if (channel.startsWith('auth:')) return handleAuthChannel(channel);
    if (channel in channelMocks) return channelMocks[channel];
    throw new Error(
      `IPC_CHANNEL_UNHANDLED: Mock 通道未实现 ${channel}（已阻止静默返回空结果）`,
    );
  },
  send: () => undefined,
  on: () => undefined,
  removeListener: () => undefined,
  removeAllListeners: () => undefined,

  // ── Chat（两阶段协议 Mock） ────────────────────────────
  chatSubmit: async (req: ChatSubmitReq) => {
    // 配额闸门 Mock：100% 档阻断新提交
    if (mockQuotaSnapshot().action === 'block') {
      throw new Error('QUOTA_EXCEEDED: 配额已用尽，新任务被阻断');
    }
    const now = Date.now();
    const taskId = `mock-task-${now}`;
    // 会话子目录：工作目录（未选则 Mock 缺省）下按对话时间创建
    const baseDir =
      req?.workDir ||
      `/mock/workspaces/${req?.workspaceId?.trim() || 'default'}/sessions`;
    const task: MockTaskState = {
      taskId,
      title: (req?.goal ?? '').slice(0, 30),
      status: 'planning',
      cancelled: false,
      steps: defaultSteps(req?.goal ?? '新任务'),
      createdAt: now,
      sessionDir: `${baseDir}/${sessionDirName(now)}`,
      // 对话内选择的模型随提交生效（用量归属与选择一致）
      model: resolveMockModel(req?.modelBinding, req?.modelId),
    };
    activeMockTask = task;
    schedulePlanFlow(task);
    return { taskId, quota: mockQuotaSnapshot() };
  },

  /** 审阅（approve → 执行流；reject → done(REJECTED)；modify → 重新计划） */
  chatReview: async (taskId: string, action: 'approve' | 'reject' | 'modify' | 'resume', patch?: unknown[]) => {
    const task = activeMockTask ?? mockHistory.find((t) => t.taskId === taskId);
    if (!task) return { ok: false, status: 'TASK_NOT_FOUND' };
    if (action === 'resume') {
      // Mock 通道不模拟引擎进程内的检查点；明确告知不可用，不伪造恢复成功
      emit(seqEvent(taskId, {
        type: 'error',
        code: 'CHECKPOINT_UNAVAILABLE',
        message: 'Mock 模式没有真实引擎的检查点，无法续跑（请连接真实引擎验证）',
      }));
      emit(seqEvent(taskId, { type: 'done' }));
      return { ok: false, status: 'CHECKPOINT_UNAVAILABLE' };
    }
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
  /** 重放 Mock：无持久化，返回空窗口（windowExpired 语义） */
  eventReplay: async (taskId: string) => ({ taskId, events: [] }),
  /** 检查点查询 Mock：没有真实引擎进程，如实返回空（不伪造可续跑入口） */
  chatCheckpointQuery: async () => ({ checkpoints: {} }),
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
};

/**
 * 未实现的方法统一兜底：显式抛 IPC_CHANNEL_UNHANDLED。
 * 已显式实现的方法优先使用；空数组会让页面把「没实现」当成「没有数据」。
 */
const proxiedAPI = new Proxy(mockAPI, {
  get(target, prop: string) {
    if (prop in target) {
      return (target as unknown as Record<string, unknown>)[prop];
    }
    return async (...args: unknown[]) => {
      console.info('[mock-api] unhandled method', prop, args);
      throw new Error(
        `IPC_CHANNEL_UNHANDLED: Mock API 未实现 ${String(prop)}（已阻止静默返回空结果）`,
      );
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
          console.info('[engine-api] unhandled method', prop, args);
          throw new Error(
            `IPC_CHANNEL_UNHANDLED: 引擎直连 API 未实现 ${String(prop)}（已阻止静默返回空结果）`,
          );
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
