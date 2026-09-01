// ============================================================
// src/shared/contracts.ts — IPC request/response types
// ============================================================

import type {
  TaskStatus,
  StepType,
  SandboxLevel,
  WaitReason,
  TaskMeta,
} from './types';

// ── Error Codes ──────────────────────────────────────────────

export enum ErrorCode {
  // Model errors
  MODEL_NOT_FOUND = 'MODEL_NOT_FOUND',
  MODEL_CONNECTION_FAILED = 'MODEL_CONNECTION_FAILED',
  MODEL_AUTH_FAILED = 'MODEL_AUTH_FAILED',
  MODEL_RATE_LIMITED = 'MODEL_RATE_LIMITED',
  MODEL_CONTEXT_OVERFLOW = 'MODEL_CONTEXT_OVERFLOW',
  MODEL_RESPONSE_INVALID = 'MODEL_RESPONSE_INVALID',

  // File errors
  FILE_NOT_FOUND = 'FILE_NOT_FOUND',
  FILE_ACCESS_DENIED = 'FILE_ACCESS_DENIED',
  FILE_WRITE_FAILED = 'FILE_WRITE_FAILED',
  FILE_CONVERSION_FAILED = 'FILE_CONVERSION_FAILED',
  FILE_TOO_LARGE = 'FILE_TOO_LARGE',
  FILE_BACKUP_FAILED = 'FILE_BACKUP_FAILED',

  // Task errors
  TASK_NOT_FOUND = 'TASK_NOT_FOUND',
  TASK_ALREADY_RUNNING = 'TASK_ALREADY_RUNNING',
  TASK_PLAN_REJECTED = 'TASK_PLAN_REJECTED',
  TASK_EXECUTION_FAILED = 'TASK_EXECUTION_FAILED',
  TASK_ROLLBACK_FAILED = 'TASK_ROLLBACK_FAILED',
  TASK_PAUSED = 'TASK_PAUSED',

  // Config errors
  CONFIG_INVALID = 'CONFIG_INVALID',
  CONFIG_MISSING = 'CONFIG_MISSING',
  CONFIG_PARSE_ERROR = 'CONFIG_PARSE_ERROR',

  // Workspace errors
  WORKSPACE_NOT_FOUND = 'WORKSPACE_NOT_FOUND',
  WORKSPACE_SUSPENDED = 'WORKSPACE_SUSPENDED',
  WORKSPACE_CREATION_FAILED = 'WORKSPACE_CREATION_FAILED',
  WORKSPACE_DELEGATE_FAILED = 'WORKSPACE_DELEGATE_FAILED',

  // Skill errors
  SKILL_NOT_FOUND = 'SKILL_NOT_FOUND',
  SKILL_INSTALL_FAILED = 'SKILL_INSTALL_FAILED',
  SKILL_EXECUTION_FAILED = 'SKILL_EXECUTION_FAILED',
  SKILL_INCOMPATIBLE = 'SKILL_INCOMPATIBLE',

  // MCP errors
  MCP_CONNECTION_FAILED = 'MCP_CONNECTION_FAILED',
  MCP_TOOL_NOT_FOUND = 'MCP_TOOL_NOT_FOUND',
  MCP_TOOL_BLOCKED = 'MCP_TOOL_BLOCKED',
  MCP_AUTH_FAILED = 'MCP_AUTH_FAILED',
  MCP_TIMEOUT = 'MCP_TIMEOUT',

  // Sandbox errors
  SANDBOX_VIOLATION = 'SANDBOX_VIOLATION',
  SANDBOX_RESOURCE_LIMIT = 'SANDBOX_RESOURCE_LIMIT',
  SANDBOX_FILE_BLOCKED = 'SANDBOX_FILE_BLOCKED',
  SANDBOX_TOOL_BLOCKED = 'SANDBOX_TOOL_BLOCKED',

  // Compliance errors
  COMPLIANCE_HARD_SWITCH = 'COMPLIANCE_HARD_SWITCH',
  COMPLIANCE_DATA_LEAK = 'COMPLIANCE_DATA_LEAK',
  COMPLIANCE_MODEL_NOT_ALLOWED = 'COMPLIANCE_MODEL_NOT_ALLOWED',

  // Bridge errors
  BRIDGE_PYTHON_NOT_FOUND = 'BRIDGE_PYTHON_NOT_FOUND',
  BRIDGE_AGENT_NOT_FOUND = 'BRIDGE_AGENT_NOT_FOUND',
  BRIDGE_STARTUP_FAILED = 'BRIDGE_STARTUP_FAILED',
  BRIDGE_COMMUNICATION_FAILED = 'BRIDGE_COMMUNICATION_FAILED',

  // Schedule errors
  SCHEDULE_NOT_FOUND = 'SCHEDULE_NOT_FOUND',
  SCHEDULE_CRON_INVALID = 'SCHEDULE_CRON_INVALID',
  SCHEDULE_EXECUTION_FAILED = 'SCHEDULE_EXECUTION_FAILED',

  // Generic
  INTERNAL_ERROR = 'INTERNAL_ERROR',
  VALIDATION_ERROR = 'VALIDATION_ERROR',

  // ── 三层架构演进云端必需码（P0，需求分析报告 M4 + 主 SPEC §5.3 补全） ──
  /** 组织预算耗尽（Gateway 权威台账） */
  BUDGET_EXHAUSTED = 'BUDGET_EXHAUSTED',
  /** 排队超时（QUEUE_TIMEOUT 10min，可配） */
  QUEUE_TIMEOUT = 'QUEUE_TIMEOUT',
  /** 租约丢失（Runner 崩溃/心跳超时 60s） */
  LEASE_LOST = 'LEASE_LOST',
  /** 附件上传被拒（病毒扫描/超大小上限） */
  FILE_REJECTED = 'FILE_REJECTED',
  /** 同步冲突（client_rev 与云端冲突） */
  SYNC_CONFLICT = 'SYNC_CONFLICT',
  /** 支付宽限期（宽限 3 天，期内新任务拒绝） */
  PAYMENT_GRACE = 'PAYMENT_GRACE',
  /** 数据分级阻断（Confidential 硬拒绝外发） */
  DATA_TIER_BLOCKED = 'DATA_TIER_BLOCKED',
  /** 提示词注入拦截（高危硬阻断） */
  INJECTION_BLOCKED = 'INJECTION_BLOCKED',
  /** 配额超限（四级配额 100% 阻断档） */
  QUOTA_EXCEEDED = 'QUOTA_EXCEEDED',
}

// ── App Error ────────────────────────────────────────────────

export interface AppError {
  code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

// ── Chat / Task ──────────────────────────────────────────────

export interface ChatSubmitReq {
  taskId?: string;
  goal: string;
  modelBinding?: string;
  /** 附件列表（交互补设计 §3 附件模式） */
  attachments?: import('./types').AttachmentMeta[];
}

export interface ChatReviewReq {
  planId: string;
  action: 'approve' | 'reject' | 'modify';
  patch?: Record<string, unknown>;
}

export interface ChatControlReq {
  taskId: string;
  action: 'pause' | 'resume' | 'rollback_to';
  stepId?: string;
}

/**
 * 对话事件流契约（与 types.ts 中 ChatEvent 保持结构一致，
 * 一致性由 tests/unit/shared/contract.test.ts 门禁保障）。
 * seq/taskId 为三层架构演进 P0 新增可选字段（AgentEvent 单调 seq，契约兼容变更）。
 */
export type ChatEvent =
  | { type: 'plan'; planId: string; steps: ChatStepPreview[]; seq?: number; taskId?: string }
  | { type: 'step_start'; stepId: string; stepType: StepType; description: string; seq?: number; taskId?: string }
  | { type: 'step_progress'; stepId: string; progress: number; message: string; seq?: number; taskId?: string }
  | { type: 'step_done'; stepId: string; output: string; usage?: { promptTokens?: number; completionTokens?: number }; seq?: number; taskId?: string }
  | { type: 'step_failed'; stepId: string; error: string; seq?: number; taskId?: string }
  | { type: 'deliver'; taskId: string; summary: string; artifacts: string[]; seq?: number }
  | { type: 'done'; taskId: string; seq?: number }
  | { type: 'error'; code: ErrorCode; message: string; seq?: number; taskId?: string }
  // ── 三层架构演进新增事件 ──
  | { type: 'status_change'; taskId: string; status: TaskStatus; waitReason?: WaitReason; statusReasonCode?: string; queuePosition?: number; seq?: number }
  | { type: 'quota_warning'; level: '70' | '85' | '95' | '100'; action: 'notify' | 'degrade' | 'queue' | 'block'; usedTokens: number; quotaTokens: number; seq?: number }
  | { type: 'heartbeat'; seq?: number };

export interface ChatStepPreview {
  id: string;
  order: number;
  type: StepType;
  description: string;
}

// ── Model ────────────────────────────────────────────────────

export interface ModelTestReq {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindow: number;
}

export interface ModelTestResp {
  ok: boolean;
  latencyMs: number;
  model: string;
  error?: string;
  availableModels?: string[];
}

// ── Workspace ────────────────────────────────────────────────

export interface WorkspaceCreateReq {
  name: string;
  agentConfig: {
    role: string;
    systemPrompt: string;
  };
  modelBinding?: string;
  skillIds?: string[];
  mcpServerIds?: string[];
}

export interface AgentDelegateReq {
  fromWorkspaceId: string;
  toWorkspaceId: string;
  messageType: string;
  payload: Record<string, unknown>;
}

export interface AgentDelegateResp {
  accepted: boolean;
  delegateTaskId?: string;
  reason?: string;
}

// ── Skill ────────────────────────────────────────────────────

export interface SkillInstallReq {
  source: 'marketplace' | 'local' | 'url';
  skillId?: string;
  localPath?: string;
  url?: string;
}

// ── Audit ────────────────────────────────────────────────────

export interface AuditQuery {
  startTime: string;
  endTime: string;
  eventTypes?: string[];
  workspaceId?: string;
  modelId?: string;
  limit?: number;
  offset?: number;
}

// ── Schedule ─────────────────────────────────────────────────

export interface ScheduleCreateReq {
  name: string;
  cronExpression: string;
  goal: string;
  modelBinding?: string;
}

export interface ScheduleUpdateReq {
  id: string;
  name?: string;
  cronExpression?: string;
  goal?: string;
  modelBinding?: string;
}

// ── MCP ──────────────────────────────────────────────────────

export interface MCPTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

// ── Usage & Stats ────────────────────────────────────────────

export interface UsageStats {
  modelId: string;
  modelName: string;
  totalPromptTokens: number;
  totalCompletionTokens: number;
  totalCalls: number;
  avgLatencyMs: number;
  estimatedCost: number;
}

// ── Compliance ───────────────────────────────────────────────

export interface ComplianceStatus {
  hardSwitchEnabled: boolean;
  whitelistModelIds: string[];
  lastDataCheck?: string;
}

// ── File System ──────────────────────────────────────────────

export interface TaskSummary {
  id: string;
  title: string;
  status: TaskStatus;
  createdAt: string;
  updatedAt: string;
  /** 三层架构演进增量元数据（全可选，老调用方零感知） */
  meta?: TaskMeta;
}

// ── 三层架构演进 P0：租约与配额契约 ────────────────────

/** 租约信息（lease 协议：心跳 10s / 租约 30s / 超时回收 60s / 空闲释放 5min，ADJ-R2-4） */
export interface LeaseInfo {
  taskId: string;
  leaseId: string;
  /** 持有方标识（本地 local-first 恒为本设备 ID） */
  holder: string;
  /** 最近心跳时间（ISO 字符串） */
  heartbeatAt: string;
  /** 租约到期时间（ISO 字符串） */
  leaseUntil: string;
  /** 是否空闲待释放 */
  idle: boolean;
}

/** 接管请求结果（禁止抢占，显式接管需对端确认，ADJ-R2-4） */
export interface LeaseTakeoverResp {
  needConfirmation: boolean;
  currentHolder?: string;
  message?: string;
}

/** 配额快照（四档预警 70/85/95/100%，动作链 降级→排队→阻断，ADJ-R5-4） */
export interface QuotaSnapshot {
  usedTokens: number;
  quotaTokens: number;
  /** 当前所处档位 */
  tier: '70' | '85' | '95' | '100';
  /** 档位对应动作：提示 / 降级 / 排队 / 阻断 */
  action: 'notify' | 'degrade' | 'queue' | 'block';
  /** 台账更新时间（ISO 字符串） */
  updatedAt?: string;
}

export interface FileInfo {
  name: string;
  path: string;
  isDirectory: boolean;
  size: number;
  modifiedAt: string;
}

export interface FilePreview {
  path: string;
  content: string;
  mimeType: string;
}

// ── Resource Limits ──────────────────────────────────────────

export interface ResourceLimits {
  maxCpuPercent: number;
  maxMemoryMb: number;
  maxDiskWriteMbps: number;
}

export interface QuotaStatus {
  modelId: string;
  usedTokens: number;
  quotaTokens: number;
  remainingTokens: number;
  resetAt: string | null;
}

export interface ResourceReport {
  cpuPercent: number;
  memoryUsedMb: number;
  diskWriteMbps: number;
  activeTasks: number;
  activeMcpConnections: number;
}

// ── Dangerous Operations ─────────────────────────────────────

export interface DangerousOpAlert {
  pattern: string;
  level: SandboxLevel;
  description: string;
}

// ── 千问对标整改：新增实体类型（spec §6.1） ─────────────────────

export type HookEventName =
  | 'SessionStart' | 'SessionEnd' | 'UserPromptSubmit'
  | 'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure'
  | 'Stop' | 'SubagentStart' | 'SubagentStop'
  | 'PreCompact' | 'Notification' | 'PermissionRequest';

export interface HookCommand {
  type: 'command';
  command: string;
  timeout?: number; // 秒，默认 60
}

export interface HookMatcherGroup {
  matcher?: string;
  hooks: HookCommand[];
}

export interface HookConfigStore {
  hooks: Partial<Record<HookEventName, HookMatcherGroup[]>>;
}

export interface HookInput {
  session_id: string;
  cwd: string;
  hook_event_name: HookEventName;
  [key: string]: unknown;
}

export interface HookResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  blocked: boolean;
  timedOut: boolean;
}

export interface HookAggregate {
  blocked: boolean;
  blockReason?: string;
  results: HookResult[];
}

export interface AwarenessProfile {
  mode: boolean;
  autoMemory: boolean;
  style: 'professional' | 'friendly' | 'concise' | 'custom';
  customPrompt: string;
  workManual: string;
  updatedAt: string;
}

export interface MemoryEntry {
  id: string;
  content: string;
  source: 'auto' | 'manual' | 'reflection';
  sessionId?: string;
  createdAt: string;
}

export interface ConnectorState {
  connectorId: string;
  category: 'builtin' | 'market';
  installed: boolean;
  authStatus: 'none' | 'pending' | 'authorized';
  serverRef?: string;
  updatedAt: string;
}

export interface ArchiveRecord {
  taskId: string;
  title: string;
  workspaceId: string;
  archivedAt: string;
}

export type ComputerUsePolicy = 'ask_every_time' | 'auto' | 'disabled';

/** 应用级设置（spec §6.1 AppSettingsExtension，持久化于 settings kv） */
export interface AppSettingsData {
  language: string;
  theme: string;
  uiStyle: string;
  chatFont: string;
  chatFontSize: number;
  chatWidth: string;
  previewMode: string;
  promptSuggestion: boolean;
  expandToolCalls: boolean;
  imShowToolProcess: boolean;
  autoStart: boolean;
  keepAwake: boolean;
  desktopNotify: boolean;
  badgeUnread: boolean;
  soundNotify: boolean;
  proxyMode: string;
  privacyMode: boolean;
  voiceEnabled: boolean;
  snapshotEnabled: boolean;
  quickPickEnabled: boolean;
  computerUsePolicy: ComputerUsePolicy;
  secureEnv: boolean;
  workbenchModes: string[];
  workbenchDefaultMode: string;
  experiments: Record<string, boolean>;
  [key: string]: unknown;
}

// ── IPC Channel Names ────────────────────────────────────────

export const IPC_CHANNELS = {
  // Chat / Task
  CHAT_SUBMIT: 'chat:submit',
  CHAT_REVIEW: 'chat:review',
  CHAT_CONTROL: 'chat:control',
  CHAT_HISTORY: 'chat:history',
  CHAT_EVENT: 'chat:event',

  // Model
  MODEL_LIST: 'model:list',
  MODEL_CREATE: 'model:create',
  MODEL_UPDATE: 'model:update',
  MODEL_DELETE: 'model:delete',
  MODEL_TEST: 'model:test',
  MODEL_SET_DEFAULT: 'model:setDefault',
  MODEL_USAGE: 'model:usage',

  // File
  FILE_BROWSE: 'file:browse',
  FILE_PREVIEW: 'file:preview',
  FILE_READ: 'file:read',
  FILE_WRITE: 'file:write',
  FILE_CONVERT: 'file:convert',
  FILE_ORGANIZE: 'file:organize',

  // Workspace
  WORKSPACE_LIST: 'workspace:list',
  WORKSPACE_CREATE: 'workspace:create',
  WORKSPACE_UPDATE: 'workspace:update',
  WORKSPACE_DELETE: 'workspace:delete',
  WORKSPACE_SUSPEND: 'workspace:suspend',
  WORKSPACE_RESUME: 'workspace:resume',
  WORKSPACE_DELEGATE: 'workspace:delegate',

  // Skill
  SKILL_LIST: 'skill:list',
  SKILL_INSTALL: 'skill:install',
  SKILL_UNINSTALL: 'skill:uninstall',
  SKILL_TOGGLE: 'skill:toggle',

  // MCP
  MCP_LIST_SERVERS: 'mcp:listServers',
  MCP_ADD_SERVER: 'mcp:addServer',
  MCP_REMOVE_SERVER: 'mcp:removeServer',
  MCP_CONNECT: 'mcp:connect',
  MCP_DISCONNECT: 'mcp:disconnect',
  MCP_LIST_TOOLS: 'mcp:listTools',

  // Sandbox
  SANDBOX_GET_POLICY: 'sandbox:getPolicy',
  SANDBOX_UPDATE_POLICY: 'sandbox:updatePolicy',

  // Audit
  AUDIT_QUERY: 'audit:query',
  AUDIT_EXPORT: 'audit:export',

  // Schedule
  SCHEDULE_LIST: 'schedule:list',
  SCHEDULE_CREATE: 'schedule:create',
  SCHEDULE_UPDATE: 'schedule:update',
  SCHEDULE_DELETE: 'schedule:delete',
  SCHEDULE_TOGGLE: 'schedule:toggle',

  // Compliance
  COMPLIANCE_STATUS: 'compliance:status',
  COMPLIANCE_UPDATE: 'compliance:update',

  // Usage
  USAGE_STATS: 'usage:stats',
  USAGE_REPORT: 'usage:report',

  // System
  SYSTEM_INFO: 'system:info',
  SYSTEM_HEALTH: 'system:health',

  // ── 千问对标整改新增（spec §6.3） ──
  SETTINGS_GET: 'settings:get',
  SETTINGS_SET: 'settings:set',
  HOOKS_GET: 'hooks:get',
  HOOKS_SET: 'hooks:set',
  HOOKS_TEST: 'hooks:test',
  AWARENESS_GET: 'awareness:get',
  AWARENESS_UPDATE: 'awareness:update',
  AWARENESS_MEMORIES: 'awareness:memories',
  AWARENESS_MEMORY_ADD: 'awareness:memoryAdd',
  AWARENESS_MEMORY_DELETE: 'awareness:memoryDelete',
  CONNECTOR_LIST: 'connector:list',
  CONNECTOR_AUTHORIZE: 'connector:authorize',
  CONNECTOR_REVOKE: 'connector:revoke',
  TASK_ARCHIVE: 'task:archive',
  TASK_UNARCHIVE: 'task:unarchive',
  TASK_ARCHIVE_DELETE: 'task:archiveDelete',
  TASK_ARCHIVE_LIST: 'task:archiveList',
  TASK_ARCHIVE_CLEAR: 'task:archiveClear',
  COMPUTER_USE_GET_POLICY: 'computerUse:getPolicy',
  COMPUTER_USE_SET_POLICY: 'computerUse:setPolicy',

  // ── 三层架构演进 P0 新增（14态状态机/租约/配额/事件重放） ──
  CHAT_CANCEL: 'chat:cancel',
  TASK_STATE: 'task:state',
  QUOTA_STATUS: 'quota:status',
  LEASE_INFO: 'lease:info',
  LEASE_TAKEOVER: 'lease:takeover',
  EVENT_REPLAY: 'event:replay',
} as const;

export type IPCChannel = typeof IPC_CHANNELS[keyof typeof IPC_CHANNELS];
