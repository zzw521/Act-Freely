// src/shared/types.ts - 扩展类型定义

/** 任务状态（14 态：原 9 态 + 云端演进增量 5 态，见 docs/changes/agent-platform-evolution/experts/03-business.md §2.1） */
export enum TaskStatus {
  IDLE = 'idle',
  PLANNING = 'planning',
  PENDING_REVIEW = 'pending_review',
  EXECUTING = 'executing',
  PAUSED = 'paused',
  DELIVERING = 'delivering',
  DONE = 'done',
  FAILED = 'failed',
  ROLLED_BACK = 'rolled_back',
  // ── 三层架构演进增量（P0，只追加不删改） ──
  /** 排队中（云端 Runner 池满；本地 local-first 退化为入队即出队 <100ms） */
  QUEUED = 'queued',
  /** 预算暂停（Gateway 返回 BUDGET_EXHAUSTED，恢复后按 FIFO 回 executing） */
  BUDGET_PAUSED = 'budget_paused',
  /** 自动重试等待（退避窗口，retry_count<3） */
  RETRY_PENDING = 'retry_pending',
  /** 用户取消（触发回滚栈处置） */
  CANCELLED = 'cancelled',
  /** 已归档（手动或 30 天自动） */
  ARCHIVED = 'archived',
}

/**
 * 等待原因子态：
 * - rate_limited / circuit_break：executing 子态（限流等待不设独立状态，避免状态爆炸）
 * - quota_wait / budget_wait / lease_wait：queued/retry_pending 成因
 * - user_pause / pending_review_ttl：暂停与审阅超期成因
 */
export type WaitReason =
  | 'rate_limited'
  | 'circuit_break'
  | 'quota_wait'
  | 'budget_wait'
  | 'lease_wait'
  | 'user_pause'
  | 'pending_review_ttl'
  | null;

/**
 * 任务元数据增量字段（三层架构演进 P0，全可选保持契约兼容）。
 * 对齐 03-business.md §2.1 的 9 个 Task 新增字段。
 */
export interface TaskMeta {
  waitReason?: WaitReason;
  /** 排队位次（queued 态展示） */
  queuePosition?: number;
  /** 已重试次数（上限 3，手动重试累计不重置） */
  retryCount?: number;
  /** 租约持有方（本地 local-first 恒为本设备） */
  leaseHolder?: string;
  /** 状态成因码（如 BUDGET_EXHAUSTED / QUEUE_TIMEOUT / LEASE_LOST / REJECTED） */
  statusReasonCode?: string;
  /** 归档时间（ISO 字符串） */
  archivedAt?: string;
  /** 乐观锁版本号（跨端写裁决，本地默认单调递增） */
  revision?: number;
}

/** 计划状态 */
export enum PlanStatus {
  DRAFT = 'draft',
  PENDING_REVIEW = 'pending_review',
  APPROVED = 'approved',
  REJECTED = 'rejected',
  EXECUTING = 'executing',
  DONE = 'done',
}

/** 步骤状态 */
export enum StepStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  SUCCESS = 'success',
  FAILED = 'failed',
  SKIPPED = 'skipped',
  ROLLED_BACK = 'rolled_back',
}

/** 步骤类型 */
export enum StepType {
  LLM_CALL = 'llm_call',
  FILE_READ = 'file_read',
  FILE_WRITE = 'file_write',
  FILE_CONVERT = 'file_convert',
  FILE_ORGANIZE = 'file_organize',
  USER_CONFIRM = 'user_confirm',
  SKILL_CALL = 'skill_call',
  MCP_TOOL = 'mcp_tool',
  WORKSPACE_DELEGATE = 'workspace_delegate',
}

/** 角色类型 */
export enum Role {
  USER = 'user',
  ASSISTANT = 'assistant',
  SYSTEM = 'system',
  TOOL = 'tool',
}

/** 模型源类型 */
export enum ModelSource {
  OPENAI_COMPAT = 'openai_compat',
  OLLAMA = 'ollama',
  PRESET_QWEN = 'preset_qwen',
  PRESET_DEEPSEEK = 'preset_deepseek',
  PRESET_OPENAI = 'preset_openai',
  CUSTOM = 'custom',
}

/** 文件操作类型 */
export enum FileOpType {
  READ = 'read',
  WRITE = 'write',
  CONVERT = 'convert',
  ORGANIZE = 'organize',
}

/** 文件操作状态 */
export enum FileOpStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  SUCCESS = 'success',
  FAILED = 'failed',
  ROLLED_BACK = 'rolled_back',
}

/** 数据分级敏感度 */
export enum DataSensitivity {
  PUBLIC = 'public',
  INTERNAL = 'internal',
  CONFIDENTIAL = 'confidential',
}

/** 沙箱执行级别 */
export enum SandboxLevel {
  L0 = 'L0',
  L1 = 'L1',
  L2 = 'L2',
}

// === 核心实体 ===

export interface Task {
  id: string;
  title: string;
  status: TaskStatus;
  userId: string;
  plan?: Plan;
  messages: Message[];
  fileOps: FileOp[];
  modelBinding?: string;
  createdAt: Date;
  updatedAt: Date;
}

export interface Plan {
  id: string;
  taskId: string;
  steps: Step[];
  status: PlanStatus;
  reviewed: boolean;
}

export interface Step {
  id: string;
  planId: string;
  name: string;
  type: StepType;
  input: Record<string, unknown>;
  status: StepStatus;
  result?: string;
  rollbackable: boolean;
  progress?: string; // 中间输出（流式）
}

export interface Message {
  id: string;
  taskId: string;
  role: Role;
  content: string;
  attachments?: Record<string, unknown>;
  ts: Date;
}

export interface FileOp {
  id: string;
  taskId: string;
  op: FileOpType;
  srcPath: string;
  dstPath?: string;
  backupPath?: string;
  status: FileOpStatus;
  fileName?: string;
  fileSize?: number;
}

export interface ModelConfig {
  id: string;
  name: string;
  source: ModelSource;
  baseUrl: string;
  apiKeyEncrypted: string; // 持久化时加密
  modelName: string;
  contextWindow: number;
  isDefault: boolean;
  agentBinding: string;
  forceLocal: boolean;
  lastUsedAt?: Date;
  createdAt: Date;
  updatedAt: Date;
}

export interface ModelTestReq {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindow: number;
  /** 可选：已存模型 id（后端从 keyring 补齐密钥，列表卡片测试场景） */
  id?: string;
}

export interface ModelTestResp {
  ok: boolean;
  latencyMs: number;
  model: string;
  error?: string;
  availableModels?: string[];
}

export interface AuditLog {
  id: string;
  timestamp: Date;
  event: string;
  actor: string;
  details: Record<string, unknown>;
  taskId?: string;
  modelId?: string;
}

/** 审计日志条目（用于 UI 展示） */
export interface AuditLogEntry {
  id: string;
  timestamp: Date | string;
  action: string; // model_call, file_write, file_read, route_switch, config_change, user_login, etc.
  severity: 'error' | 'warning' | 'info' | 'debug';
  user?: string;
  resource?: string;
  details?: string;
  read?: boolean;
}

// === IPC 请求/响应类型 ===

/** 附件元数据（交互补设计 §3 契约补充） */
export interface AttachmentMeta {
  /** 原始文件名 */
  fileName: string;
  /** 暂存后路径（运行目录内） */
  stagedPath: string;
  sizeBytes: number;
  mime: string;
  /** 数据分级检测结果 */
  sensitivity: DataSensitivity | string;
}

export interface ChatSubmitReq {
  taskId?: string;
  goal: string;
  modelBinding?: string;
  /** 附件列表（附件模式，暂存后随任务提交） */
  attachments?: AttachmentMeta[];
  /** 工作目录：会话内容与产出物按对话时间建子目录存放于此 */
  workDir?: string;
  /** 此前轮次对话历史（保障连续对话上下文） */
  history?: Array<{ role: 'user' | 'assistant'; content: string }>;
  /** 自定义模型配置（models.json 条目；存在 baseUrl 时引擎真实调用该端点） */
  modelConfig?: { baseUrl: string; apiKey: string; modelName: string };
}

/** 步骤预览（用于 Plan 事件中的步骤概览） */
export interface ChatStepPreview {
  id: string;
  order: number;
  type: StepType;
  description: string;
}

/**
 * 对话事件流契约。
 * seq/taskId 为可选字段（三层架构演进 P0：AgentEvent 单调 seq，契约兼容变更），
 * 与 contracts.ts 中 ChatEvent 定义保持结构一致（见 tests/unit/shared/contract.test.ts 门禁）。
 */
export type ChatEvent =
  | { type: 'plan'; planId: string; steps: ChatStepPreview[]; seq?: number; taskId?: string }
  | { type: 'step_start'; stepId: string; stepType: StepType; description: string; seq?: number; taskId?: string }
  | { type: 'step_progress'; stepId: string; progress: number; message: string; seq?: number; taskId?: string }
  | { type: 'step_done'; stepId: string; output: string; usage?: { promptTokens?: number; completionTokens?: number }; seq?: number; taskId?: string }
  | { type: 'step_failed'; stepId: string; error: string; seq?: number; taskId?: string }
  | { type: 'deliver'; taskId: string; summary: string; artifacts: string[]; seq?: number }
  | { type: 'done'; taskId: string; seq?: number }
  | { type: 'error'; code: string; message: string; seq?: number; taskId?: string }
  // ── 三层架构演进新增事件 ──
  | { type: 'status_change'; taskId: string; status: TaskStatus; waitReason?: WaitReason; statusReasonCode?: string; queuePosition?: number; seq?: number }
  | { type: 'quota_warning'; level: '70' | '85' | '95' | '100'; action: 'notify' | 'degrade' | 'queue' | 'block'; usedTokens: number; quotaTokens: number; seq?: number }
  | { type: 'heartbeat'; seq?: number };

export interface ChatReviewReq {
  planId: string;
  action: 'approve' | 'reject' | 'modify';
  patch?: Partial<Step>[];
}

export interface ChatControlReq {
  taskId: string;
  action: 'pause' | 'resume' | 'rollback_to';
  stepId?: string;
}

/** 模型使用统计 */
export interface UsageStats {
  modelId: string;
  modelName: string;
  tokens?: number;
  calls?: number;
  latency?: number;
  timestamp?: string;
}

// === 安全相关 ===

export interface SandboxPolicy {
  workspaceId: string;
  executionLevel: 'L0' | 'L1' | 'L2';
  fileAllowlist: string[];
  fileBlocklist: string[];
  toolAllowlist: string[];
  toolBlocklist: string[];
  maxCpuPercent: number;
  maxMemoryMB: number;
  maxDiskWriteMbps: number;
}

export interface DataClassificationRule {
  pattern: RegExp;
  level: DataSensitivity;
  description: string;
}
