// ============================================================
// src/preload/index.d.ts — Window interface augmentation
// ============================================================

import type {
  ChatSubmitReq,
  ChatReviewReq,
  ChatControlReq,
  ChatEvent,
  ModelTestReq,
  ModelTestResp,
  WorkspaceCreateReq,
  AgentDelegateReq,
  AgentDelegateResp,
  SkillInstallReq,
  AuditQuery,
  ScheduleCreateReq,
  ScheduleUpdateReq,
  MCPTool,
  AppError,
  UsageStats,
  ComplianceStatus,
  TaskSummary,
  FileInfo,
  FilePreview,
  ResourceReport,
} from '../shared/contracts';

import type {
  ModelConfig,
  Workspace,
  Skill,
  MCPServer,
  SandboxPolicy,
  ScheduleTask,
} from '../shared/types';

interface TongtianAPI {
  /**
   * Type-safe IPC invoke for request/response channels.
   */
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;

  /**
   * Fire-and-forget IPC send.
   */
  send(channel: string, ...args: unknown[]): void;

  /**
   * Listen for events from the main process.
   */
  on(channel: string, callback: (...args: unknown[]) => void): void;

  /**
   * Remove a specific event listener.
   */
  removeListener(channel: string, callback: (...args: unknown[]) => void): void;

  /**
   * Remove all listeners for a given channel.
   */
  removeAllListeners(channel: string): void;

  // ── Typed convenience methods ────────────────────────────

  // Chat
  chatSubmit(req: ChatSubmitReq): Promise<{ taskId: string; workspaceId?: string; status?: string }>;
  /** 审阅计划（两阶段协议，转移 #9/#10/#11） */
  chatReview(taskId: string, action: 'approve' | 'reject' | 'modify', patch?: unknown[]): Promise<{ ok: boolean; status: string }>;
  /** 任务控制（pause/resume/rollback_to，转移 #14/#21/#22） */
  chatControl(req: ChatControlReq): Promise<{ ok: boolean; status: string }>;
  /** 历史任务（tasks 表，含 14 态 meta） */
  chatHistory(limit?: number): Promise<unknown[]>;
  /** 取消任务（转移 #3/#8/#19/#23/#28） */
  chatCancel(taskId: string, workspaceId?: string): Promise<{ taskId: string; cancelled: boolean }>;
  /** 附件暂存（交互补设计 §3.1） */
  fileStageAttachment(srcPath: string, taskId?: string, workspaceId?: string): Promise<Record<string, unknown>>;
  onChatEvent(callback: (event: ChatEvent) => void): () => void;

  // Model
  modelList(): Promise<ModelConfig[]>;
  modelCreate(config: Partial<ModelConfig> & { apiKey: string }): Promise<ModelConfig>;
  modelUpdate(id: string, config: Partial<ModelConfig>): Promise<ModelConfig>;
  modelDelete(id: string): Promise<void>;
  modelTest(req: ModelTestReq): Promise<ModelTestResp>;
  modelSetDefault(id: string): Promise<void>;

  // File
  fileBrowse(dirPath: string): Promise<FileInfo[]>;
  filePreview(filePath: string): Promise<FilePreview>;
  fileRead(filePath: string): Promise<string>;
  fileWrite(filePath: string, content: string): Promise<void>;
  fileConvert(srcPath: string, dstPath: string, format: string): Promise<void>;
  fileOrganize(srcPaths: string[], dstDir: string, strategy: string): Promise<void>;

  // Workspace
  workspaceList(): Promise<Workspace[]>;
  workspaceCreate(req: WorkspaceCreateReq): Promise<Workspace>;
  workspaceUpdate(id: string, data: Partial<Workspace>): Promise<Workspace>;
  workspaceDelete(id: string): Promise<void>;
  workspaceDelegate(req: AgentDelegateReq): Promise<AgentDelegateResp>;

  // Skill
  skillList(): Promise<Skill[]>;
  skillInstall(req: SkillInstallReq): Promise<Skill>;
  skillUninstall(id: string): Promise<void>;
  skillToggle(id: string, enabled: boolean): Promise<void>;

  // MCP
  mcpListServers(): Promise<MCPServer[]>;
  mcpAddServer(config: Partial<MCPServer>): Promise<MCPServer>;
  mcpRemoveServer(id: string): Promise<void>;
  mcpConnect(id: string): Promise<void>;
  mcpDisconnect(id: string): Promise<void>;
  mcpListTools(serverId: string): Promise<MCPTool[]>;

  // Sandbox
  sandboxGetPolicy(workspaceId: string): Promise<SandboxPolicy>;
  sandboxUpdatePolicy(id: string, data: Partial<SandboxPolicy>): Promise<SandboxPolicy>;

  // Audit
  auditQuery(query: AuditQuery): Promise<unknown[]>;
  auditExport(query: AuditQuery, format: string): Promise<string>;

  // Schedule
  scheduleList(): Promise<ScheduleTask[]>;
  scheduleCreate(req: ScheduleCreateReq): Promise<ScheduleTask>;
  scheduleUpdate(req: ScheduleUpdateReq): Promise<ScheduleTask>;
  scheduleDelete(id: string): Promise<void>;
  scheduleToggle(id: string, enabled: boolean): Promise<void>;

  // Compliance
  complianceStatus(): Promise<ComplianceStatus>;
  complianceUpdate(data: Partial<ComplianceStatus>): Promise<ComplianceStatus>;

  // Usage
  usageStats(modelId?: string): Promise<UsageStats[]>;
  usageReport(startTime: string, endTime: string): Promise<string>;

  // System
  systemInfo(): Promise<Record<string, unknown>>;
  systemHealth(): Promise<Record<string, unknown>>;
}

declare global {
  interface Window {
    tongtianAPI: TongtianAPI;
  }
}

export {};
