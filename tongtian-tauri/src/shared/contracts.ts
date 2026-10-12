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
  FILE_READ_FAILED = 'FILE_READ_FAILED',
  FILE_WRITE_FAILED = 'FILE_WRITE_FAILED',
  FILE_CONVERSION_FAILED = 'FILE_CONVERSION_FAILED',
  FILE_TOO_LARGE = 'FILE_TOO_LARGE',
  FILE_BACKUP_FAILED = 'FILE_BACKUP_FAILED',
  /** 仅浏览器直连/Mock 通道使用：网页侧无法读取本机文档正文 */
  FILE_UNSUPPORTED = 'FILE_UNSUPPORTED',

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
  WORKSPACE_INVALID_ID = 'WORKSPACE_INVALID_ID',
  WORKSPACE_NOT_FOUND = 'WORKSPACE_NOT_FOUND',
  WORKSPACE_SUSPENDED = 'WORKSPACE_SUSPENDED',
  WORKSPACE_CREATION_FAILED = 'WORKSPACE_CREATION_FAILED',
  WORKSPACE_DELEGATE_FAILED = 'WORKSPACE_DELEGATE_FAILED',
  /** 提交任务时自选的工作目录不存在或不是目录（拒绝提交，而不是换个目录默默产出） */
  WORK_DIR_INVALID = 'WORK_DIR_INVALID',

  // 系统集成（开机自启动：写系统启动项失败必须可见，不能让开关亮着但启动项没写）
  AUTOSTART_FAILED = 'AUTOSTART_FAILED',
  AUTOSTART_UNSUPPORTED = 'AUTOSTART_UNSUPPORTED',

  // Skill errors
  SKILL_NOT_FOUND = 'SKILL_NOT_FOUND',
  SKILL_INSTALL_FAILED = 'SKILL_INSTALL_FAILED',
  SKILL_EXECUTION_FAILED = 'SKILL_EXECUTION_FAILED',
  SKILL_INCOMPATIBLE = 'SKILL_INCOMPATIBLE',
  SKILL_UNSUPPORTED = 'SKILL_UNSUPPORTED',

  // Suite errors（专家套件包：plugin.json 校验与本地安装）
  SUITE_NOT_FOUND = 'SUITE_NOT_FOUND',
  SUITE_INCOMPATIBLE = 'SUITE_INCOMPATIBLE',
  SUITE_INSTALL_FAILED = 'SUITE_INSTALL_FAILED',

  // MCP errors
  MCP_CONNECTION_FAILED = 'MCP_CONNECTION_FAILED',
  MCP_TOOL_NOT_FOUND = 'MCP_TOOL_NOT_FOUND',
  MCP_TOOL_BLOCKED = 'MCP_TOOL_BLOCKED',
  MCP_AUTH_FAILED = 'MCP_AUTH_FAILED',
  MCP_TIMEOUT = 'MCP_TIMEOUT',
  MCP_INVALID_ID = 'MCP_INVALID_ID',
  MCP_INVALID_NAME = 'MCP_INVALID_NAME',
  MCP_INVALID_TRANSPORT = 'MCP_INVALID_TRANSPORT',
  MCP_INVALID_COMMAND = 'MCP_INVALID_COMMAND',
  MCP_INVALID_URL = 'MCP_INVALID_URL',
  MCP_INVALID_HEADER = 'MCP_INVALID_HEADER',
  MCP_SERVER_EXISTS = 'MCP_SERVER_EXISTS',
  MCP_SERVER_NOT_FOUND = 'MCP_SERVER_NOT_FOUND',
  MCP_SAVE_FAILED = 'MCP_SAVE_FAILED',
  MCP_QUERY_FAILED = 'MCP_QUERY_FAILED',
  MCP_PROTOCOL_ERROR = 'MCP_PROTOCOL_ERROR',
  MCP_TRANSPORT_UNSUPPORTED = 'MCP_TRANSPORT_UNSUPPORTED',
  MCP_INVALID_CONFIG = 'MCP_INVALID_CONFIG',
  /** 仅浏览器直连通道使用：网页侧无法拉起子进程或持有连接器凭据 */
  MCP_UNSUPPORTED = 'MCP_UNSUPPORTED',

  // Sandbox errors
  SANDBOX_VIOLATION = 'SANDBOX_VIOLATION',
  SANDBOX_RESOURCE_LIMIT = 'SANDBOX_RESOURCE_LIMIT',
  SANDBOX_FILE_BLOCKED = 'SANDBOX_FILE_BLOCKED',
  SANDBOX_TOOL_BLOCKED = 'SANDBOX_TOOL_BLOCKED',

  // Compliance errors（出域/硬开关命中沿用 POLICY_* 码，闸门只补内容侧语义）
  COMPLIANCE_DATA_LEAK = 'COMPLIANCE_DATA_LEAK',

  // Bridge errors
  BRIDGE_PYTHON_NOT_FOUND = 'BRIDGE_PYTHON_NOT_FOUND',
  BRIDGE_AGENT_NOT_FOUND = 'BRIDGE_AGENT_NOT_FOUND',
  BRIDGE_STARTUP_FAILED = 'BRIDGE_STARTUP_FAILED',
  BRIDGE_COMMUNICATION_FAILED = 'BRIDGE_COMMUNICATION_FAILED',
  // Rust `bridge/agent.rs` 与 `bridge/python_env.rs` 实际抛出的码（`CODE: 详情`）
  BRIDGE_ERROR = 'BRIDGE_ERROR',
  BRIDGE_START_TIMEOUT = 'BRIDGE_START_TIMEOUT',
  BRIDGE_SCRIPT_NOT_FOUND = 'BRIDGE_SCRIPT_NOT_FOUND',
  BRIDGE_NO_FREE_PORT = 'BRIDGE_NO_FREE_PORT',
  BRIDGE_WORKDIR_FAILED = 'BRIDGE_WORKDIR_FAILED',
  BRIDGE_EXE_PATH = 'BRIDGE_EXE_PATH',
  BRIDGE_SPAWN_FAILED = 'BRIDGE_SPAWN_FAILED',
  BRIDGE_COMM_FAILED = 'BRIDGE_COMM_FAILED',
  BRIDGE_CONTROL_FAILED = 'BRIDGE_CONTROL_FAILED',
  BRIDGE_CONTROL_PARSE = 'BRIDGE_CONTROL_PARSE',
  BRIDGE_CANCEL_PARSE = 'BRIDGE_CANCEL_PARSE',

  // Schedule errors
  SCHEDULE_NOT_FOUND = 'SCHEDULE_NOT_FOUND',
  SCHEDULE_CRON_INVALID = 'SCHEDULE_CRON_INVALID',
  SCHEDULE_EXECUTION_FAILED = 'SCHEDULE_EXECUTION_FAILED',
  // Rust `task::schedule` 实际抛出的码（`CODE: 详情`）
  SCHEDULE_INVALID_FREQ = 'SCHEDULE_INVALID_FREQ',
  SCHEDULE_INVALID_TIME = 'SCHEDULE_INVALID_TIME',
  SCHEDULE_INVALID_WEEKDAY = 'SCHEDULE_INVALID_WEEKDAY',
  SCHEDULE_INVALID_NAME = 'SCHEDULE_INVALID_NAME',
  SCHEDULE_INVALID_ID = 'SCHEDULE_INVALID_ID',
  SCHEDULE_QUERY_FAILED = 'SCHEDULE_QUERY_FAILED',
  SCHEDULE_SAVE_FAILED = 'SCHEDULE_SAVE_FAILED',
  SCHEDULE_ALREADY_RUNNING = 'SCHEDULE_ALREADY_RUNNING',
  /** 保持系统唤醒：唤醒约束进程启动/保持失败 */
  SCHEDULE_KEEP_AWAKE_FAILED = 'SCHEDULE_KEEP_AWAKE_FAILED',
  /** 保持系统唤醒：当前平台不支持 */
  SCHEDULE_KEEP_AWAKE_UNSUPPORTED = 'SCHEDULE_KEEP_AWAKE_UNSUPPORTED',

  // ── 网络代理（Rust proxy.rs 抛出，settings_set* 落库前整组校验） ──
  /** 自定义代理但未填写代理地址 */
  PROXY_URL_REQUIRED = 'PROXY_URL_REQUIRED',
  /** 代理地址不合法（缺协议/主机端口缺失/协议不支持） */
  PROXY_INVALID_URL = 'PROXY_INVALID_URL',
  /** 按当前代理配置构造 HTTP client 失败 */
  PROXY_CLIENT_BUILD_FAILED = 'PROXY_CLIENT_BUILD_FAILED',

  // ── 系统权限（Rust security/permissions.rs，去授权动作） ──
  /** 权限项标识不在平台能力清单内 */
  PERMISSION_KEY_UNKNOWN = 'PERMISSION_KEY_UNKNOWN',
  /** 当前系统没有对应隐私设置面板，无法打开 */
  PERMISSION_PANE_UNAVAILABLE = 'PERMISSION_PANE_UNAVAILABLE',
  /** 调用系统设置面板失败 */
  PERMISSION_OPEN_FAILED = 'PERMISSION_OPEN_FAILED',
  /** 当前运行通道（浏览器/Mock）无法拉起系统设置 */
  PERMISSION_UNSUPPORTED = 'PERMISSION_UNSUPPORTED',

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

  // ── 安全与治理（PRD §4.7，Rust security::policy / security::audit 抛出） ──
  /** 域名过滤模式非法（仅 whitelist / blacklist） */
  POLICY_INVALID_MODE = 'POLICY_INVALID_MODE',
  /** 沙箱执行级别非法（仅 L0 / L1 / L2） */
  POLICY_INVALID_LEVEL = 'POLICY_INVALID_LEVEL',
  /** 沙箱资源上限越界（CPU/内存/写盘） */
  POLICY_INVALID_RANGE = 'POLICY_INVALID_RANGE',
  /** 工作区标识为空或非法 */
  POLICY_INVALID_WORKSPACE = 'POLICY_INVALID_WORKSPACE',
  /** 已落库策略 JSON 损坏（拒绝按默认值放行） */
  POLICY_CORRUPT = 'POLICY_CORRUPT',
  /** 策略读取失败 */
  POLICY_READ_FAILED = 'POLICY_READ_FAILED',
  /** 策略保存失败 */
  POLICY_SAVE_FAILED = 'POLICY_SAVE_FAILED',
  /** 硬开关已开：外部 API 全部禁用 */
  POLICY_EXTERNAL_API_DISABLED = 'POLICY_EXTERNAL_API_DISABLED',
  /** 目标域名不在白名单内 */
  POLICY_DOMAIN_NOT_ALLOWED = 'POLICY_DOMAIN_NOT_ALLOWED',
  /** 目标域名命中黑名单 */
  POLICY_DOMAIN_BLOCKED = 'POLICY_DOMAIN_BLOCKED',
  /** 无法解析出目标域名 */
  POLICY_INVALID_URL = 'POLICY_INVALID_URL',
  /** 审计日志查询失败 */
  AUDIT_QUERY_FAILED = 'AUDIT_QUERY_FAILED',
  /** 审计日志写入失败 */
  AUDIT_WRITE_FAILED = 'AUDIT_WRITE_FAILED',
  /** 审计日志导出失败 */
  AUDIT_EXPORT_FAILED = 'AUDIT_EXPORT_FAILED',
  /** 审计导出格式不支持（仅 csv / json） */
  AUDIT_EXPORT_FORMAT_UNSUPPORTED = 'AUDIT_EXPORT_FORMAT_UNSUPPORTED',

  // ── 安全工作环境（Rust security::environment / 引擎 /api/sandbox/environment 抛出） ──
  /** 工作环境用量统计失败 */
  SANDBOX_ENV_USAGE_FAILED = 'SANDBOX_ENV_USAGE_FAILED',
  /** 工作环境文件清理失败（目录被占用或权限受阻） */
  SANDBOX_ENV_CLEAR_FAILED = 'SANDBOX_ENV_CLEAR_FAILED',
  /** 数据根目录不存在或不可读，无法定位工作环境目录 */
  SANDBOX_ENV_BASE_MISSING = 'SANDBOX_ENV_BASE_MISSING',
  /** 目标路径无法解析为真实路径 */
  SANDBOX_ENV_PATH_INVALID = 'SANDBOX_ENV_PATH_INVALID',
  /** 目标路径越出 workspaces/{id}/environment 允许范围 */
  SANDBOX_ENV_PATH_OUT_OF_SCOPE = 'SANDBOX_ENV_PATH_OUT_OF_SCOPE',
  /** 清理时命中符号链接，已跳过以免误删链接指向的外部数据 */
  SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK = 'SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK',

  // ── 模型用量统计（Rust model::usage 抛出） ──
  /** 用量统计周期非法（仅 daily / monthly / total） */
  USAGE_INVALID_PERIOD = 'USAGE_INVALID_PERIOD',
  /** 用量台账写入失败（仅内部日志，经 record_quiet 不阻断执行） */
  USAGE_RECORD_FAILED = 'USAGE_RECORD_FAILED',
  /** 用量统计查询失败 */
  USAGE_QUERY_FAILED = 'USAGE_QUERY_FAILED',
  /** 用量总览组装失败（聚合结果无法序列化给前端） */
  USAGE_STATS_FAILED = 'USAGE_STATS_FAILED',
  /** 用量报表查询/组装失败（按日趋势或明细读取失败） */
  USAGE_REPORT_FAILED = 'USAGE_REPORT_FAILED',

  // ── 对齐整改通道（Rust align::* 抛出：Hooks / 连接器 / 归档 / 电脑操控） ──
  /** Hook 事件名不在支持清单内 */
  HOOK_INVALID_EVENT = 'HOOK_INVALID_EVENT',
  /** Hook 命令为空或缺少命令定义 */
  HOOK_INVALID_COMMAND = 'HOOK_INVALID_COMMAND',
  /** Hook 超时秒数越界（1~600） */
  HOOK_INVALID_TIMEOUT = 'HOOK_INVALID_TIMEOUT',
  /** Hook 分组缺少 hooks 数组 */
  HOOK_INVALID_GROUP = 'HOOK_INVALID_GROUP',
  /** Hook matcher 类型非法 */
  HOOK_INVALID_MATCHER = 'HOOK_INVALID_MATCHER',
  /** Hook 配置结构非法（hooks 必须是事件名 → 分组建制数组） */
  HOOK_CONFIG_INVALID = 'HOOK_CONFIG_INVALID',
  /** Hook 配置保存失败 */
  HOOK_SAVE_FAILED = 'HOOK_SAVE_FAILED',
  /** Hook 配置读取失败 */
  HOOK_QUERY_FAILED = 'HOOK_QUERY_FAILED',
  /** Hook 试运行进程异常终止 */
  HOOK_TEST_FAILED = 'HOOK_TEST_FAILED',
  /** 本地通道无法执行系统命令 */
  HOOK_TEST_UNSUPPORTED = 'HOOK_TEST_UNSUPPORTED',

  /** 连接器标识为空 */
  CONNECTOR_INVALID_ID = 'CONNECTOR_INVALID_ID',
  /** 连接器分类非法（仅 builtin / market） */
  CONNECTOR_INVALID_CATEGORY = 'CONNECTOR_INVALID_CATEGORY',
  /** 连接器没有授权记录，无需撤销 */
  CONNECTOR_NOT_FOUND = 'CONNECTOR_NOT_FOUND',
  /** 连接器授权状态保存失败 */
  CONNECTOR_SAVE_FAILED = 'CONNECTOR_SAVE_FAILED',
  /** 连接器授权状态读取失败 */
  CONNECTOR_QUERY_FAILED = 'CONNECTOR_QUERY_FAILED',

  /** 归档任务标识为空 */
  ARCHIVE_INVALID_TASK_ID = 'ARCHIVE_INVALID_TASK_ID',
  /** 任务不存在，无法归档 */
  ARCHIVE_TASK_NOT_FOUND = 'ARCHIVE_TASK_NOT_FOUND',
  /** 清空归档需输入确认词 DELETE */
  ARCHIVE_CONFIRM_REQUIRED = 'ARCHIVE_CONFIRM_REQUIRED',
  /** 归档标记保存失败 */
  ARCHIVE_SAVE_FAILED = 'ARCHIVE_SAVE_FAILED',
  /** 归档任务彻底删除失败 */
  ARCHIVE_DELETE_FAILED = 'ARCHIVE_DELETE_FAILED',
  /** 归档清单读取失败 */
  ARCHIVE_QUERY_FAILED = 'ARCHIVE_QUERY_FAILED',

  /** 电脑操控档位非法（仅 ask_every_time / auto / disabled） */
  COMPUTER_USE_INVALID_POLICY = 'COMPUTER_USE_INVALID_POLICY',

  // ── 意识（awareness:*：画像 + 能力文件 + 长期记忆条目） ──
  /** 意识目录不可访问（读取/初始化落盘失败） */
  AWARENESS_DIR_FAILED = 'AWARENESS_DIR_FAILED',
  /** 意识文件读取失败 */
  AWARENESS_LOAD_FAILED = 'AWARENESS_LOAD_FAILED',
  /** 意识文件保存失败（含文件名不在白名单） */
  AWARENESS_SAVE_FAILED = 'AWARENESS_SAVE_FAILED',
  /** 意识文件名非法（仅四个能力文件可写） */
  AWARENESS_FILE_INVALID = 'AWARENESS_FILE_INVALID',
  /** 协作风格预设非法（仅四个 SOUL.md 模板 id） */
  AWARENESS_STYLE_INVALID = 'AWARENESS_STYLE_INVALID',
  /** 长期记忆条目内容为空 */
  AWARENESS_MEMORY_EMPTY = 'AWARENESS_MEMORY_EMPTY',
  /** 长期记忆条目不存在（删除时按 id 未命中） */
  AWARENESS_MEMORY_NOT_FOUND = 'AWARENESS_MEMORY_NOT_FOUND',
}

// ── App Error ────────────────────────────────────────────────

export interface AppError {
  code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

// ── Chat / Task ──────────────────────────────────────────────

/**
 * 提交请求契约以 `shared/types.ts` 的 ChatSubmitReq 为唯一来源（含 workDir / history /
 * modelConfig / conversationId 等随提交下发的选择项）。此处只转发，
 * 曾在此重复定义过一份窄版本，导致新增字段在桌面通道被类型检查放过又被运行时丢掉。
 */
export type { ChatSubmitReq } from './types';

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
  | {
      type: 'deliver';
      taskId: string;
      summary: string;
      artifacts: string[];
      /** 模型最终回复正文（引擎/Rust 透传；无正文时界面按摘要降级） */
      final_text?: string;
      seq?: number;
    }
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

/** Agent 实例状态（Rust `bridge::agent::InstanceStatus` 以 lowercase 序列化） */
export type AgentInstanceStatus = 'starting' | 'ready' | 'busy' | 'stopped' | 'error';

/**
 * 工作空间视图（Rust `workspace::dir_info` 契约镜像）
 * 目录即事实来源：`{data_home}/workspaces/{id}`，不存在新表，因此也不引入名字/描述字段。
 */
export interface WorkspaceInfo {
  id: string;
  /** 展示名，当前与标识一致 */
  name: string;
  /** 目录是否已落地（default 在首次启动 Agent 前可能尚未创建） */
  exists: boolean;
  createdAtMs: number;
  /** 目录内条目数（仅用于判断是否空工作空间） */
  entryCount: number;
  rootPath: string;
  /** Agent 实例运行态（由 bridge_status 合并；无实例时 stopped） */
  running: boolean;
  status: AgentInstanceStatus;
  port?: number;
}

/** 工作空间创建入参（标识即目录名，须通过 Rust 同口径校验） */
export interface WorkspaceCreateByIdReq {
  workspaceId: string;
}

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

/** 已安装技能视图（Rust `skill::dir_info` 同口径：目录即事实来源） */
export interface InstalledSkill {
  id: string;
  /** frontmatter name，缺失时为技能目录名 */
  name: string;
  /** frontmatter description / 正文首段；无内容为 null */
  description: string | null;
  source: 'installed';
  /** 启停状态（禁用清单落 settings_kv，技能文件保持不动） */
  enabled: boolean;
  /** SKILL.md 是否可读（超限或 IO 失败为 false，页面提示重新安装） */
  readable: boolean;
  installedAtMs: number;
  /** 技能目录绝对路径（桌面端为运行目录下 skills/{id}） */
  path: string;
}

export interface SkillInstallReq {
  source: 'marketplace' | 'local' | 'url';
  skillId?: string;
  localPath?: string;
  url?: string;
}

/** 技能运行目录信息（桌面端为真实落盘路径；浏览器通道无落盘能力） */
export interface SkillRootInfo {
  /** 当前通道是否具备技能落盘与增删能力 */
  supported: boolean;
  /** 技能目录绝对路径；浏览器通道为 null */
  skillsRoot: string | null;
  /** 卸载回收目录；浏览器通道为 null */
  trashRoot: string | null;
}

// ── 专家套件（岗位工作流包）───────────────────────────────────

/** 套件指令（plugin.json 的 commands 归一后的形状） */
export interface SuiteCommand {
  name: string;
  description?: string | null;
}

/** 已安装套件视图（Rust `suite::dir_info` 同口径：manifest.json 即事实来源） */
export interface InstalledSuite {
  id: string;
  /** plugin.json 的 name（中文展示名保留原样） */
  name: string;
  version: string;
  author?: string | null;
  category?: string | null;
  description?: string | null;
  /** 套件指令：对话输入 / 时的候选 */
  commands: SuiteCommand[];
  /** plugin.json 声明与包内 SKILL.md 取大者 */
  skillCount: number;
  dataLinkCount: number;
  /** 套件目录内文件总数（含归一的 manifest.json） */
  fileCount: number;
  installedAtMs: number;
  /** 套件目录绝对路径（浏览器通道为本地台账标识，无磁盘路径） */
  path: string;
  /** manifest.json 读不出（缺失/损坏）时为 false，页面提示重新安装 */
  readable: boolean;
  /** 套件无独立启停开关：装上即可用，卸载即消失 */
  enabled: boolean;
}

/** 套件安装入参：只接受本地 .zip 字节（base64），广场无远端来源时如实拒绝 */
export interface SuiteInstallReq {
  fileName?: string;
  zipBase64?: string;
}

/** 套件运行目录信息（桌面端为真实落盘路径；浏览器通道为本地台账） */
export interface SuiteRootInfo {
  /** 当前通道是否具备套件落盘与增删能力 */
  supported: boolean;
  suitesRoot: string | null;
  trashRoot: string | null;
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

// ── Auth（账号与登录态 · 网页验证登录 Loopback 方案） ──

/** 账号资料（auth_status/auth_login 返回） */
export interface AuthProfile {
  uid: string;
  nickname: string;
  email?: string;
  /** 套餐文案（如“个人免费版”） */
  plan?: string;
  /** Mock 模式模拟用户标记 */
  mock?: boolean;
}

/** 登录态视图（auth_status 返回） */
export interface AuthStatus {
  loggedIn: boolean;
  profile: AuthProfile | null;
  /** 当前是否处于 Mock 开关（TT_AUTH_MOCK） */
  mock: boolean;
}

/** 登录结果（auth_login 返回） */
export interface AuthLoginResp {
  ok: boolean;
  profile: AuthProfile;
  mock: boolean;
}

// ── Schedule ─────────────────────────────────────────────────

/** 定时频率（Rust `task::schedule::normalize_freq` 同口径，兼容界面中文标签） */
export type ScheduleFreq = 'hourly' | 'daily' | 'weekly' | 'monthly';

/** 星期（1=周一 … 7=周日，仅 weekly 使用） */
export type ScheduleWeekday = 1 | 2 | 3 | 4 | 5 | 6 | 7;

/** 定时任务（Rust `task::schedule::ScheduleRow` 契约镜像） */
export interface ScheduledTask {
  scheduleId: string;
  name: string;
  description: string;
  freq: ScheduleFreq;
  /** 24 小时制 HH:MM（口径与用量台账一致，均为 UTC） */
  timeOfDay: string;
  weekday?: number | null;
  enabled: boolean;
  workspaceId: string;
  createdAtMs: number;
  updatedAtMs: number;
  /** 下次计划执行时间；禁用为 null */
  nextRunAtMs?: number | null;
  lastRunAtMs?: number | null;
  lastStatus?: ScheduleRunStatus | null;
  lastTaskId?: string | null;
}

/** 执行记录状态（Rust `task::schedule::RUN_*` 同口径） */
export type ScheduleRunStatus = 'running' | 'succeeded' | 'failed' | 'blocked';

export interface ScheduleRun {
  runId: string;
  scheduleId: string;
  taskId?: string | null;
  startedAtMs: number;
  finishedAtMs?: number | null;
  status: ScheduleRunStatus;
  summary?: string | null;
}

/** 保持系统唤醒实际状态（requested=持久化意愿，active=约束真实生效） */
export interface KeepAwakeStatus {
  requested: boolean;
  active: boolean;
  supported: boolean;
}

export interface ScheduleCreateReq {
  name: string;
  description?: string;
  freq: string;
  timeOfDay: string;
  weekday?: number | null;
  workspaceId?: string;
}

/** 编辑定时任务（缺省字段沿用原配置，与 Rust `UpdateSchedule` 同口径） */
export interface ScheduleUpdateReq {
  scheduleId: string;
  name?: string;
  description?: string;
  freq?: string;
  timeOfDay?: string;
  weekday?: number | null;
  workspaceId?: string;
}

// ── MCP ──────────────────────────────────────────────────────

export interface MCPTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

/** 连接器传输方式（Rust `mcp::normalize_transport` 归一后的落库取值） */
export type McpTransport = 'stdio' | 'streamable_http' | 'sse';

/** 连接器运行状态（Rust `mcp::STATUS_*`） */
export type McpStatus = 'offline' | 'connected' | 'failed';

/** 添加连接器请求（Rust `mcp::AddServer`，camelCase 同构） */
export interface McpAddReq {
  name: string;
  transport?: string;
  command?: string;
  args?: string[];
  url?: string;
  /** 环境变量：仅 stdio 起子进程时注入，值不回传渲染层 */
  env?: Record<string, string>;
  /** 请求头：仅 HTTP 握手时注入，值不回传渲染层 */
  headers?: Record<string, string>;
  timeoutSecs?: number;
}

/** 连接器登记行（Rust `mcp::ServerRow` 序列化结果） */
export interface McpServer {
  serverId: string;
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  url: string;
  /** 环境变量名清单（Rust 只回传键名，绝不回传值） */
  envKeys: string[];
  /** 请求头名清单（Rust 只回传键名，绝不回传值） */
  headerKeys: string[];
  timeoutSecs: number;
  status: McpStatus;
  lastError?: string;
  tools: MCPTool[];
  connected: boolean;
  probedAtMs?: number;
  createdAtMs: number;
  updatedAtMs: number;
}

// ── Usage & Stats ────────────────────────────────────────────

/** 用量统计周期（Rust `model::usage::normalize_period` 同口径） */
export type UsagePeriod = 'daily' | 'monthly' | 'total';

/** 单模型用量行（Rust model_usage 表 / 引擎 `/api/usage` 同结构） */
export interface ModelUsageRow {
  modelName: string;
  /** 展示名（model_configs 配置名，缺省回落为 modelName） */
  displayName?: string;
  promptTokens: number;
  completionTokens: number;
  tokensUsed: number;
  calls: number;
  lastUsedAtMs: number;
}

/** 用量查询视图（Rust `model_usage` command / 引擎 `/api/usage` 返回体） */
export interface ModelUsageView {
  period: UsagePeriod;
  rows: ModelUsageRow[];
}

/**
 * 用量汇总（`usage:stats` / `usage:report` 的 totals）。
 * 台账只记录 token 与调用次数，**未采集延迟与单价**，故不含平均耗时/成本估算。
 */
export interface UsageTotals {
  promptTokens: number;
  completionTokens: number;
  tokensUsed: number;
  calls: number;
  /** 周期内产生过用量的模型数 */
  activeModels: number;
  /** 周期内最后一次调用时间（无用量为 0） */
  lastUsedAtMs: number;
}

/** 用量总览（Rust `usage_stats` command 返回体，供用量页顶部总览条） */
export interface UsageStats {
  period: UsagePeriod;
  totals: UsageTotals;
  /** 用量最高模型的展示名（无用量为 null） */
  topModel: string | null;
}

/** 按日趋势点（day 为 UTC 日 `yyyy-mm-dd`） */
export interface UsageDayPoint {
  day: string;
  tokensUsed: number;
  calls: number;
  /** 当日活跃模型数 */
  models: number;
}

/** 用量报表（Rust `usage_report` command 返回体：趋势 + 明细 + 汇总，自包含） */
export interface UsageReport {
  period: UsagePeriod;
  generatedAtMs: number;
  days: UsageDayPoint[];
  models: ModelUsageRow[];
  totals: UsageTotals;
}

// ── Compliance ───────────────────────────────────────────────

/** 出域控制摘要（与 Rust `security::policy::DomainControl` 字段同源） */
export interface ComplianceDomainControl {
  enabled: boolean;
  mode: 'whitelist' | 'blacklist';
  allowedDomains: string[];
  blockedDomains: string[];
  forceLocalForConfidential: boolean;
  hardDisableExternalAPI: boolean;
}

/**
 * 合规状态（compliance:status 响应）。
 * 权威数据不出出域控制 + 模型台账 + 审计三处，本结构只是聚合视图。
 */
export interface ComplianceStatus {
  /** 硬开关：一键禁用所有外部（云端）模型 API */
  hardSwitchEnabled: boolean;
  /** 按当前出域策略评估为「可达」的模型 id */
  whitelistModelIds: string[];
  /** 被策略阻断的模型 id（含硬开关命中） */
  blockedModelIds: string[];
  /**
   * 模型名单是否真实评估过。
   * 非桌面通道没有模型台账，此处为 false —— 空名单表示「未评估」，不是「全部被拦」。
   */
  allowlistEvaluated: boolean;
  domainControl: ComplianceDomainControl;
  /** 出域控制是否已落库（false 时为语义默认值） */
  persisted: boolean;
  /** 最近一次数据分级检测留痕；从未检测过为 null */
  lastDataCheck?: {
    sensitivity: string;
    severity: string;
    timestamp: string;
    summary?: string | null;
  } | null;
}

/** 合规更新入参：只改传入字段，其余保持现值（compliance:update） */
export interface CompliancePatch {
  hardSwitchEnabled?: boolean;
  enabled?: boolean;
  mode?: 'whitelist' | 'blacklist';
  allowedDomains?: string[];
  blockedDomains?: string[];
  forceLocalForConfidential?: boolean;
}

/** compliance:update 响应：更新后的状态 + 是否真实写入（空 patch 只读不写） */
export interface ComplianceUpdateResp {
  status: ComplianceStatus;
  changed: boolean;
}

/**
 * compliance:check 响应（提交前合规闸门放行时的判定明细）。
 * 拒绝时不走这里：通道以 `CODE: 原因` 抛错，由 errorTextOf 统一映射文案。
 */
export interface ComplianceCheckResp {
  allowed: true;
  /** 正文分级结果：public / internal / confidential */
  sensitivity: string;
  /** 分级判定是否要求本地通道（命中 PII 或机密） */
  forceLocal: boolean;
  hardSwitchEnabled: boolean;
  /** 端点是否可知：引擎内置模型拿不到 baseUrl 时为 false，此时不做臆断 */
  endpointKnown: boolean;
  endpointHost: string;
  endpointLocal: boolean;
}

// ── 安全工作环境：工作环境文件的磁盘事实 ──────────────────────

/** 单个工作空间的工作环境目录用量 */
export interface SandboxEnvWorkspaceUsage {
  workspaceId: string;
  /** environment 目录的绝对路径 */
  path: string;
  bytes: number;
  files: number;
}

/**
 * sandbox:envUsage 响应。
 * bytes/files 为磁盘实测值：桌面端汇总所有工作空间，引擎直连只有引擎自己那一份。
 */
export interface SandboxEnvUsage {
  supported: boolean;
  bytes: number;
  files: number;
  /** 读不动的条目数（权限占用等），不计入 bytes/files */
  unreadable?: number;
  workspaceCount: number;
  workspaces: SandboxEnvWorkspaceUsage[];
  /** 统计根目录（桌面端为 {data_home}/workspaces） */
  rootPath: string;
  envDirName: string;
}

/** 单个工作空间实际释放的量 */
export interface SandboxEnvWorkspaceCleared {
  workspaceId: string;
  freedBytes: number;
  freedFiles: number;
}

/** sandbox:envClear 响应；failed 非空表示有条目没删掉（如被进程占用） */
export interface SandboxEnvClearResult {
  freedBytes: number;
  freedFiles: number;
  cleared: SandboxEnvWorkspaceCleared[];
  failed: Array<{ workspaceId?: string; path?: string; error: string }>;
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
  /** 是否拿到可信正文；false 时 content 可能为空，原因见 note */
  supported: boolean;
  /** Rust 侧原样回传的说明（含 `CODE: 详情` 形态），前端直接展示 */
  note?: string;
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

/** 协作风格预设 id（与设置页 SOUL.md 模板一一对应；正文本身落盘在 SOUL.md） */
export type AwarenessStyleId = 'efficient' | 'deliberate' | 'gentle' | 'assertive';

/** 意识目录下的能力文件名（白名单，Rust / 引擎 / 本地台账三处一致） */
export type AwarenessFileName = 'SOUL.md' | 'WORK_MANUAL.md' | 'USER_PROFILE.md' | 'MEMORY.md';

export const AWARENESS_FILE_NAMES: AwarenessFileName[] = [
  'SOUL.md',
  'WORK_MANUAL.md',
  'USER_PROFILE.md',
  'MEMORY.md',
];

/** 单个意识文件的磁盘视图 */
export interface AwarenessFileEntry {
  content: string;
  /** 最近修改时间（本地化展示串；未落盘时为「系统默认」） */
  updatedAt: string;
}

export interface AwarenessProfile {
  mode: boolean;
  autoMemory: boolean;
  style: AwarenessStyleId;
  /** 最近一次保存意识配置的 ISO 时间 */
  updatedAt: string;
}

/** `awareness:get` 返回体：画像 + 意识目录真实视图（目录/文件读取失败时字段缺省） */
export interface AwarenessView extends AwarenessProfile {
  /** 意识目录绝对路径（本地台账通道无文件系统时为 null） */
  dir: string | null;
  files: Partial<Record<AwarenessFileName, AwarenessFileEntry>>;
}

/** `awareness:update` 补丁：画像字段与意识文件正文（正文按文件名白名单落盘） */
export interface AwarenessPatch {
  mode?: boolean;
  autoMemory?: boolean;
  style?: AwarenessStyleId;
  files?: Partial<Record<AwarenessFileName, string>>;
}

export interface MemoryEntry {
  /** 落盘在 MEMORY.md 行首的稳定标识 */
  id: string;
  content: string;
  source: 'auto' | 'manual' | 'reflection';
  /** ISO 时间（落盘与排序口径） */
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
  /** 自定义代理地址（proxyMode=自定义 时必填，形如 http://127.0.0.1:7890） */
  proxyUrl: string;
  /** 忽略代理的主机清单（逗号分隔；本地回环始终直连，无需填写） */
  proxyBypass: string;
  privacyMode: boolean;
  voiceEnabled: boolean;
  snapshotEnabled: boolean;
  quickPickEnabled: boolean;
  computerUsePolicy: ComputerUsePolicy;
  secureEnv: boolean;
  workbenchModes: string[];
  workbenchDefaultMode: string;
  /** 快捷键改键台账：能力 id → 组合（如 'Ctrl+Shift+K'）；缺项用出厂默认 */
  shortcuts: Record<string, string>;
  experiments: Record<string, boolean>;
  [key: string]: unknown;
}

/** 代理配置快照（Rust proxy.rs 的 ProxySpec，mode 为设置页口径中文标签） */
export interface ProxyConfigSnapshot {
  mode: '跟随系统' | '自定义' | '不使用';
  url: string | null;
  bypass: string[];
}

/** `proxy:status` 返回：当前实际生效配置 + 库内配置 + 待重启的引擎实例数 */
export interface ProxyStatus {
  active: ProxyConfigSnapshot;
  /** 库内配置不可用时为 null（如自定义但地址缺失，此时 active 仍是上一次成功的配置） */
  persisted: ProxyConfigSnapshot | null;
  /** 实际直连的主机清单（用户附加项 + 本地回环） */
  effectiveBypass: string[];
  /** 仍以旧代理运行的引擎实例数（引擎在启动时注入代理变量，需重启才切换） */
  staleEngines: number;
}

/**
 * 开机自启动状态（`system:autostart`）。
 * `requested` 是设置里的意愿，`enabled` 是系统启动项事实 —— 两栏分开，
 * 界面才能指出「开关开着但启动项没写」这类不一致，而不是拿意愿当事实展示。
 */
export interface AutostartStatus {
  /** 当前平台是否支持由应用写入启动项（Windows=true） */
  supported: boolean;
  requested: boolean;
  enabled: boolean;
  /** 系统启动项里登记的命令（未登记为 null） */
  command: string | null;
  /** 当前程序路径：与 command 不同说明启动项指向旧构建 */
  currentCommand: string;
  /** 启动项存在但指向的不是当前程序（如开发态换过构建目录） */
  stale: boolean;
  valueName: string;
  runKey: string;
}

/**
 * 会话正文行（Rust `chat_messages` 表 / 浏览器本地台账同一形状）。
 * 一行 = 一个会话某一轮某个角色的正文；事件流只留 24h，正文不设 TTL。
 */
export interface ChatMessageRow {
  convId: string;
  turnIndex: number;
  role: 'user' | 'assistant';
  /** 本轮任务 id（过程明细事件按它在事件流里取回） */
  taskId: string;
  content: string;
  /** 交付物文件名清单（仅 assistant 行有值） */
  artifacts: string[];
  tsMs: number;
  /** 本轮回复的评价（仅 assistant 行有值；''=未评价） */
  feedback?: '' | 'up' | 'down';
}

/** 会话摘要行（`chat:conversations` 返回；标题由渲染层从 firstGoal 截出） */
export interface ConversationRow {
  convId: string;
  /** 首轮用户提问（库内截断到 60 字） */
  firstGoal: string;
  turnCount: number;
  createdAtMs: number;
  updatedAtMs: number;
  /** 最近一轮的任务 id（归档标记按它匹配） */
  lastTaskId: string;
}

/**
 * 系统权限项（Rust `security::permissions::view_of` 同构）。
 * 清单只描述「平台有没有这个开关、面板在哪、状态能不能真实探测」，
 * 状态本身由 `SystemPermissionStatus.states` 给出，不存在写死的「已授权」。
 */
export interface SystemCapability {
  key: string;
  label: string;
  purpose: string;
  /** 前端可真实探测的通道；null 表示代码无法得知，只能去系统设置管理 */
  probe: 'notification' | 'microphone' | null;
  /** 当前平台可达的设置面板深链；null 表示该平台没有这个开关 */
  pane: string | null;
  /** pane 为 null 时的如实解释（例如「Windows 全局快捷键无需授权」） */
  note: string | null;
}

/** 系统真实回答的权限状态；unknown 表示这一项问不到 */
export type PermissionState = 'granted' | 'denied' | 'prompt' | 'unknown';

/** `system:permission:status` 返回：清单 + 可探测项的真实状态 + 能否打开面板 */
export interface SystemPermissionStatus {
  os: string;
  items: SystemCapability[];
  /** capability key → 真实状态；问不到的键不出现在这里 */
  states: Record<string, PermissionState>;
  /** 只有桌面版能拉起系统设置面板 */
  openSupported: boolean;
}

// ── IPC Channel Names ────────────────────────────────────────


export const IPC_CHANNELS = {
  // Chat / Task
  CHAT_SUBMIT: 'chat:submit',
  CHAT_REVIEW: 'chat:review',
  CHAT_CONTROL: 'chat:control',
  CHAT_HISTORY: 'chat:history',
  /** 会话列表（正文聚合，重启后重建侧边栏任务） */
  CHAT_CONVERSATIONS: 'chat:conversations',
  /** 单个会话的正文（逐轮问答） */
  CHAT_MESSAGES: 'chat:messages',
  /** 一轮回复的点赞/点踩（按 (会话, 轮次) 归属，null 为取消） */
  CHAT_FEEDBACK: 'chat:feedback',

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

  // Workspace
  WORKSPACE_LIST: 'workspace:list',
  WORKSPACE_CREATE: 'workspace:create',

  // Skill
  SKILL_ROOT: 'skill:root',
  SKILL_LIST: 'skill:list',
  SKILL_INSTALL: 'skill:install',
  SKILL_UNINSTALL: 'skill:uninstall',
  SKILL_TOGGLE: 'skill:toggle',

  // 专家套件（岗位工作流包，本地化安装）
  SUITE_ROOT: 'suite:root',
  SUITE_LIST: 'suite:list',
  SUITE_INSTALL: 'suite:install',
  SUITE_UNINSTALL: 'suite:uninstall',

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
  SANDBOX_ENV_USAGE: 'sandbox:envUsage',
  SANDBOX_ENV_CLEAR: 'sandbox:envClear',

  // Audit
  AUDIT_QUERY: 'audit:query',
  AUDIT_EXPORT: 'audit:export',

  // Security（数据分级 + 出域控制，PRD §4.7）
  SECURITY_SCAN_DATA: 'security:scanData',
  SECURITY_GET_DOMAIN_CONTROL: 'security:getDomainControl',
  SECURITY_SET_DOMAIN_CONTROL: 'security:setDomainControl',

  // Schedule
  SCHEDULE_LIST: 'schedule:list',
  SCHEDULE_CREATE: 'schedule:create',
  SCHEDULE_UPDATE: 'schedule:update',
  SCHEDULE_DELETE: 'schedule:delete',
  SCHEDULE_TOGGLE: 'schedule:toggle',
  SCHEDULE_RUNS: 'schedule:runs',
  SCHEDULE_RUN_NOW: 'schedule:runNow',
  KEEP_AWAKE_STATUS: 'keepAwake:status',
  /** 开机自启动实际状态（Windows 读注册表启动项，非「设置里的意愿」） */
  SYSTEM_AUTOSTART_STATUS: 'system:autostart',
  PROXY_STATUS: 'proxy:status',
  SYSTEM_PERMISSION_STATUS: 'system:permission:status',
  SYSTEM_PERMISSION_OPEN: 'system:permission:open',

  // Compliance
  COMPLIANCE_STATUS: 'compliance:status',
  COMPLIANCE_UPDATE: 'compliance:update',
  COMPLIANCE_CHECK: 'compliance:check',

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

  // ── 账号与登录态（网页验证登录，TT_AUTH_MOCK 开关） ──
  AUTH_STATUS: 'auth:status',
  AUTH_LOGIN: 'auth:login',
  AUTH_LOGOUT: 'auth:logout',
} as const;

export type IPCChannel = typeof IPC_CHANNELS[keyof typeof IPC_CHANNELS];
