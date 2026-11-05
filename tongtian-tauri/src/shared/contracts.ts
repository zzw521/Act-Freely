// ============================================================
// src/shared/contracts.ts — IPC request/response types
// ============================================================

import type {
  TaskStatus,
  StepType,
  SandboxLevel,
  WaitReason,
  TaskMeta,
  PiiOutboundMode,
  FilePermissionMode,
} from './types';

// ── #164 IPC 车道预留段（各窗口只改自己那份，本文件之后不再逐条加通道）──
// 通道名规则：`<lane>:xxx` 的冒号后半段就是 src-tauri/src/ipc/<lane>.rs 里 dispatch 的 method 名。
// 分派命令由 lib.rs 的 generate_handler! 一次挂齐，前端经 src/shared/ipc-lanes.ts 单点接线。
export { IPC_LANE_KNOWLEDGE, IPC_CHANNELS_KNOWLEDGE } from './contracts_knowledge';
export { IPC_LANE_ARTIFACT, IPC_CHANNELS_ARTIFACT } from './contracts_artifact';
export { IPC_LANE_WORKSPACE, IPC_CHANNELS_WORKSPACE } from './contracts_workspace';
export { IPC_LANE_EXEC, IPC_CHANNELS_EXEC } from './contracts_exec';
export { IPC_LANE_CONNECT, IPC_CHANNELS_CONNECT } from './contracts_connect';
export { IPC_LANE_ORCHESTRATE, IPC_CHANNELS_ORCHESTRATE } from './contracts_orchestrate';
export { IPC_LANE_VOICE, IPC_CHANNELS_VOICE } from './contracts_voice';

// ── Error Codes ──────────────────────────────────────────────

export enum ErrorCode {
  // Model errors
  MODEL_NOT_FOUND = 'MODEL_NOT_FOUND',
  MODEL_CONNECTION_FAILED = 'MODEL_CONNECTION_FAILED',
  MODEL_AUTH_FAILED = 'MODEL_AUTH_FAILED',
  MODEL_RATE_LIMITED = 'MODEL_RATE_LIMITED',
  MODEL_CONTEXT_OVERFLOW = 'MODEL_CONTEXT_OVERFLOW',
  MODEL_RESPONSE_INVALID = 'MODEL_RESPONSE_INVALID',
  /** 零配置本地模型：当前通道/操作无法探测本机运行时 */
  LOCAL_MODEL_UNSUPPORTED = 'LOCAL_MODEL_UNSUPPORTED',
  /** 探测真实执行了，但本机 Ollama 未启动或响应不合法 */
  LOCAL_MODEL_NOT_RUNNING = 'LOCAL_MODEL_NOT_RUNNING',
  /** 探测地址非法（非回环 / 非 http(s) / 带无关路径） */
  LOCAL_MODEL_INVALID = 'LOCAL_MODEL_INVALID',

  // File errors
  FILE_NOT_FOUND = 'FILE_NOT_FOUND',
  FILE_ACCESS_DENIED = 'FILE_ACCESS_DENIED',
  FILE_READ_FAILED = 'FILE_READ_FAILED',
  FILE_WRITE_FAILED = 'FILE_WRITE_FAILED',
  FILE_CONVERSION_FAILED = 'FILE_CONVERSION_FAILED',
  FILE_TOO_LARGE = 'FILE_TOO_LARGE',
  FILE_BACKUP_FAILED = 'FILE_BACKUP_FAILED',
  /** 暂存复制失败：附件没能进入运行目录，任务不应带着半套文件继续 */
  FILE_STAGE_FAILED = 'FILE_STAGE_FAILED',
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
  /** 系统专用设置项不允许经设置页写入（如引擎令牌） */
  SETTING_KEY_RESERVED = 'SETTING_KEY_RESERVED',

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
  /** 静态安全扫描判定为高风险：拒绝安装/启用，详情给出命中原因 */
  SKILL_SCAN_BLOCKED = 'SKILL_SCAN_BLOCKED',

  // Suite errors（专家套件包：plugin.json 校验与本地安装）
  SUITE_NOT_FOUND = 'SUITE_NOT_FOUND',
  SUITE_INCOMPATIBLE = 'SUITE_INCOMPATIBLE',
  SUITE_INSTALL_FAILED = 'SUITE_INSTALL_FAILED',
  /** 升级失败且旧版本也放不回原位：明细里带回收目录与目标路径，需要手工恢复 */
  SUITE_ROLLBACK_FAILED = 'SUITE_ROLLBACK_FAILED',

  // Plugin SDK（安装前预检与起始模板）
  PLUGIN_INVALID_PACKAGE = 'PLUGIN_INVALID_PACKAGE',
  PLUGIN_UNSUPPORTED = 'PLUGIN_UNSUPPORTED',

  // ── 在线技能 / 专家套件市场（可配置索引源） ──
  MARKET_UNSUPPORTED = 'MARKET_UNSUPPORTED',
  MARKET_INVALID_URL = 'MARKET_INVALID_URL',
  MARKET_NOT_CONFIGURED = 'MARKET_NOT_CONFIGURED',
  MARKET_FETCH_FAILED = 'MARKET_FETCH_FAILED',
  MARKET_HTTP_ERROR = 'MARKET_HTTP_ERROR',
  MARKET_INVALID_INDEX = 'MARKET_INVALID_INDEX',
  MARKET_ITEM_NOT_FOUND = 'MARKET_ITEM_NOT_FOUND',
  MARKET_HASH_MISMATCH = 'MARKET_HASH_MISMATCH',
  MARKET_TOO_LARGE = 'MARKET_TOO_LARGE',
  MARKET_INVALID_PUBLISHER = 'MARKET_INVALID_PUBLISHER',
  MARKET_UNTRUSTED_PUBLISHER = 'MARKET_UNTRUSTED_PUBLISHER',
  MARKET_PUBLISHER_KEY_CHANGED = 'MARKET_PUBLISHER_KEY_CHANGED',
  MARKET_SIGNATURE_REQUIRED = 'MARKET_SIGNATURE_REQUIRED',
  MARKET_SIGNATURE_INVALID = 'MARKET_SIGNATURE_INVALID',
  MARKET_TRUST_SAVE_FAILED = 'MARKET_TRUST_SAVE_FAILED',
  MARKET_ITEM_REVOKED = 'MARKET_ITEM_REVOKED',

  // ── 本地知识库（RAG） ──
  KNOWLEDGE_INVALID_ID = 'KNOWLEDGE_INVALID_ID',
  KNOWLEDGE_INVALID_NAME = 'KNOWLEDGE_INVALID_NAME',
  KNOWLEDGE_INVALID_SCOPE = 'KNOWLEDGE_INVALID_SCOPE',
  KNOWLEDGE_INVALID_DESC = 'KNOWLEDGE_INVALID_DESC',
  KNOWLEDGE_INVALID_DOC = 'KNOWLEDGE_INVALID_DOC',
  KNOWLEDGE_EXISTS = 'KNOWLEDGE_EXISTS',
  KNOWLEDGE_NOT_FOUND = 'KNOWLEDGE_NOT_FOUND',
  KNOWLEDGE_SAVE_FAILED = 'KNOWLEDGE_SAVE_FAILED',
  KNOWLEDGE_LIST_FAILED = 'KNOWLEDGE_LIST_FAILED',
  KNOWLEDGE_META_UNREADABLE = 'KNOWLEDGE_META_UNREADABLE',
  KNOWLEDGE_META_INVALID = 'KNOWLEDGE_META_INVALID',
  KNOWLEDGE_NO_DOCS = 'KNOWLEDGE_NO_DOCS',
  KNOWLEDGE_DOC_NOT_FOUND = 'KNOWLEDGE_DOC_NOT_FOUND',
  KNOWLEDGE_OPEN_FAILED = 'KNOWLEDGE_OPEN_FAILED',
  KNOWLEDGE_DIR_INVALID = 'KNOWLEDGE_DIR_INVALID',
  KNOWLEDGE_QUERY_EMPTY = 'KNOWLEDGE_QUERY_EMPTY',
  KNOWLEDGE_UNSUPPORTED = 'KNOWLEDGE_UNSUPPORTED',

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
  /** 系统级沙箱启动连接器子进程失败（沙箱开关打开时） */
  MCP_SANDBOX_FAILED = 'MCP_SANDBOX_FAILED',
  /** 仅浏览器直连通道使用：网页侧无法拉起子进程或持有连接器凭据 */
  MCP_UNSUPPORTED = 'MCP_UNSUPPORTED',

  // MCP OAuth（远程 Streamable HTTP 连接器；令牌只存系统凭据库）
  MCP_OAUTH_INVALID_CONFIG = 'MCP_OAUTH_INVALID_CONFIG',
  MCP_OAUTH_REQUIRED = 'MCP_OAUTH_REQUIRED',
  MCP_OAUTH_REAUTH_REQUIRED = 'MCP_OAUTH_REAUTH_REQUIRED',
  MCP_OAUTH_REJECTED = 'MCP_OAUTH_REJECTED',
  MCP_OAUTH_IN_PROGRESS = 'MCP_OAUTH_IN_PROGRESS',
  MCP_OAUTH_TIMEOUT = 'MCP_OAUTH_TIMEOUT',
  MCP_OAUTH_DENIED = 'MCP_OAUTH_DENIED',
  MCP_OAUTH_STATE_MISMATCH = 'MCP_OAUTH_STATE_MISMATCH',
  MCP_OAUTH_CALLBACK_INVALID = 'MCP_OAUTH_CALLBACK_INVALID',
  MCP_OAUTH_TOKEN_REJECTED = 'MCP_OAUTH_TOKEN_REJECTED',
  MCP_OAUTH_INVALID_RESPONSE = 'MCP_OAUTH_INVALID_RESPONSE',
  MCP_OAUTH_NETWORK = 'MCP_OAUTH_NETWORK',
  MCP_OAUTH_OPEN_FAILED = 'MCP_OAUTH_OPEN_FAILED',
  MCP_OAUTH_UNSUPPORTED = 'MCP_OAUTH_UNSUPPORTED',

  // MCP 常驻连接枢纽（引擎侧跨轮复用；失败时退回一次性连接，不中断任务）
  MCP_HUB_NOT_STARTED = 'MCP_HUB_NOT_STARTED',
  MCP_HUB_UNKNOWN_OP = 'MCP_HUB_UNKNOWN_OP',

  // 项目协作（跨用户 / 跨机器；桌面端 Rust → 账号服务）
  COLLAB_UNSUPPORTED = 'COLLAB_UNSUPPORTED',
  COLLAB_LOGIN_REQUIRED = 'COLLAB_LOGIN_REQUIRED',
  COLLAB_AUTH_EXPIRED = 'COLLAB_AUTH_EXPIRED',
  COLLAB_AUTH_REFRESH_FAILED = 'COLLAB_AUTH_REFRESH_FAILED',
  COLLAB_AUTH_NETWORK = 'COLLAB_AUTH_NETWORK',
  COLLAB_SERVER_INVALID = 'COLLAB_SERVER_INVALID',
  COLLAB_NETWORK = 'COLLAB_NETWORK',
  COLLAB_BAD_RESPONSE = 'COLLAB_BAD_RESPONSE',
  COLLAB_INVALID_INPUT = 'COLLAB_INVALID_INPUT',

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
  /** 引擎进程启动后立即退出（带退出码，通常是随包资源缺文件） */
  BRIDGE_ENGINE_EXITED = 'BRIDGE_ENGINE_EXITED',
  BRIDGE_SCRIPT_NOT_FOUND = 'BRIDGE_SCRIPT_NOT_FOUND',
  BRIDGE_NO_FREE_PORT = 'BRIDGE_NO_FREE_PORT',
  BRIDGE_WORKDIR_FAILED = 'BRIDGE_WORKDIR_FAILED',
  BRIDGE_EXE_PATH = 'BRIDGE_EXE_PATH',
  BRIDGE_SPAWN_FAILED = 'BRIDGE_SPAWN_FAILED',
  BRIDGE_COMM_FAILED = 'BRIDGE_COMM_FAILED',
  BRIDGE_CONTROL_FAILED = 'BRIDGE_CONTROL_FAILED',
  BRIDGE_CONTROL_PARSE = 'BRIDGE_CONTROL_PARSE',
  BRIDGE_CANCEL_PARSE = 'BRIDGE_CANCEL_PARSE',
  /** 引擎 chat/checkpoint 响应不是合法 JSON（`bridge/agent.rs`） */
  BRIDGE_CHAT_PARSE = 'BRIDGE_CHAT_PARSE',
  /** 指定工作空间没有运行中的引擎实例（`engine:endpoint` / 引擎 HTTP 直连） */
  ENGINE_NOT_RUNNING = 'ENGINE_NOT_RUNNING',

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
  /** 一句话解析：空输入 */
  SCHEDULE_PARSE_EMPTY = 'SCHEDULE_PARSE_EMPTY',
  /** 一句话解析：无法识别星期/日期等结构 */
  SCHEDULE_PARSE_FAILED = 'SCHEDULE_PARSE_FAILED',
  /** 一句话解析：没识别到执行时刻 */
  SCHEDULE_PARSE_NO_TIME = 'SCHEDULE_PARSE_NO_TIME',
  /** 一句话解析：识别到时刻但不合法 */
  SCHEDULE_PARSE_TIME = 'SCHEDULE_PARSE_TIME',
  /** 一句话解析：间隔调度（每 N 小时/分钟）暂不支持 */
  SCHEDULE_UNSUPPORTED_FREQ = 'SCHEDULE_UNSUPPORTED_FREQ',
  /** 一句话解析：月内日期只支持 1 号 */
  SCHEDULE_UNSUPPORTED_DAY = 'SCHEDULE_UNSUPPORTED_DAY',
  /** 推送配置：通道/目标必须成对填写且格式合法 */
  SCHEDULE_INVALID_NOTIFY = 'SCHEDULE_INVALID_NOTIFY',
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
  /** IPC 通道在当前运行通道没有实现（禁止静默返回空数组，调用方必须看到失败） */
  IPC_CHANNEL_UNHANDLED = 'IPC_CHANNEL_UNHANDLED',

  // ── 三层架构演进云端必需码（P0，需求分析报告 M4 + 主 SPEC §5.3 补全） ──
  /** 组织预算耗尽（Gateway 权威台账） */
  BUDGET_EXHAUSTED = 'BUDGET_EXHAUSTED',
  /** 单个任务的 Token 预算已用满（缺陷 D-14.1：执行面已停在下一轮模型调用之前） */
  BUDGET_TASK_EXCEEDED = 'BUDGET_TASK_EXCEEDED',
  /** 排队超时（QUEUE_TIMEOUT 10min，可配） */
  QUEUE_TIMEOUT = 'QUEUE_TIMEOUT',
  /** 租约丢失（Runner 崩溃/心跳超时 60s） */
  LEASE_LOST = 'LEASE_LOST',
  /** 附件上传被拒（病毒扫描/超大小上限） */
  FILE_REJECTED = 'FILE_REJECTED',
  /** 用系统默认应用打开文件失败（文件关联/权限） */
  OPEN_FAILED = 'OPEN_FAILED',
  /** 当前运行通道没有系统默认应用（浏览器直连 / Mock） */
  OPEN_EXTERNAL_UNSUPPORTED = 'OPEN_EXTERNAL_UNSUPPORTED',
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
  /** 受控执行白名单里的命令名非法（必须是裸命令名） */
  POLICY_INVALID_COMMAND = 'POLICY_INVALID_COMMAND',
  /** 通用 shell 缺少命令执行授权（必须同时开启） */
  POLICY_INVALID_SHELL = 'POLICY_INVALID_SHELL',
  /** 联网白名单里的主机名非法 */
  POLICY_INVALID_HOST = 'POLICY_INVALID_HOST',
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
  /** 当前平台没有系统级沙箱实现（如非 Windows），无法启用该级别 */
  POLICY_UNSUPPORTED = 'POLICY_UNSUPPORTED',
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
  /** 当前通道（浏览器预览/Mock）没有磁盘访问能力，用量与清理都不可用 */
  SANDBOX_ENV_UNSUPPORTED = 'SANDBOX_ENV_UNSUPPORTED',

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
  /** 用量上报失败（读取台账、请求服务端或解析响应受阻） */
  USAGE_UPLOAD_FAILED = 'USAGE_UPLOAD_FAILED',
  /** 未登录或账号资料缺少 uid，无法上报 */
  USAGE_UPLOAD_LOGIN_REQUIRED = 'USAGE_UPLOAD_LOGIN_REQUIRED',
  /** 上报时登录态失效，刷新重试后仍被拒 */
  USAGE_UPLOAD_AUTH_EXPIRED = 'USAGE_UPLOAD_AUTH_EXPIRED',

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
  /** Hook 放行台账写入失败 */
  HOOK_GRANT_FAILED = 'HOOK_GRANT_FAILED',
  /** Hook 放行撤销失败 */
  HOOK_REVOKE_FAILED = 'HOOK_REVOKE_FAILED',
  /** Hook 放行台账读取失败 */
  HOOK_GRANT_QUERY_FAILED = 'HOOK_GRANT_QUERY_FAILED',

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
  /** 当前平台/通道没有电脑操控执行器（仅 Windows 桌面版） */
  COMPUTER_USE_UNSUPPORTED = 'COMPUTER_USE_UNSUPPORTED',
  /** 档位为「禁用」，执行器拒绝截屏与操作 */
  COMPUTER_USE_DISABLED = 'COMPUTER_USE_DISABLED',
  /** 「每次询问」档位下用户取消了系统确认框 */
  COMPUTER_USE_DENIED_BY_USER = 'COMPUTER_USE_DENIED_BY_USER',
  /** 截屏失败（锁屏、安全桌面、GDI 取图失败） */
  COMPUTER_USE_CAPTURE_FAILED = 'COMPUTER_USE_CAPTURE_FAILED',
  /** 键鼠注入失败（目标窗口权限更高或处于安全桌面） */
  COMPUTER_USE_ACTION_FAILED = 'COMPUTER_USE_ACTION_FAILED',
  /** 动作参数非法（未知动作、缺少坐标、文本超限等） */
  COMPUTER_USE_INVALID_ACTION = 'COMPUTER_USE_INVALID_ACTION',
  /** 本机电脑操控网关拒绝了请求（token 缺失或不匹配） */
  COMPUTER_USE_GATE_UNAUTHORIZED = 'COMPUTER_USE_GATE_UNAUTHORIZED',
  /** 坐标不在虚拟桌面范围内 */
  COMPUTER_USE_OUT_OF_BOUNDS = 'COMPUTER_USE_OUT_OF_BOUNDS',
  /** 危险按键组合被拒绝（Win 键组合、任务管理器快捷键） */
  COMPUTER_USE_BLOCKED_COMBO = 'COMPUTER_USE_BLOCKED_COMBO',
  /** 网关请求体不是合法 JSON */
  COMPUTER_USE_BAD_JSON = 'COMPUTER_USE_BAD_JSON',

  // ── 本机画面（本机实时画面；只看不控） ──
  /** 当前平台没有桌面截屏执行器（仅 Windows 桌面版） */
  REMOTE_UNSUPPORTED = 'REMOTE_UNSUPPORTED',
  /** 电脑操控档位为禁用，本机画面无法开启 */
  REMOTE_DISABLED = 'REMOTE_DISABLED',
  /** 会话未开启或已结束 */
  REMOTE_NO_SESSION = 'REMOTE_NO_SESSION',
  /** 会话到期（单次最长 10 分钟） */
  REMOTE_SESSION_EXPIRED = 'REMOTE_SESSION_EXPIRED',

  // ── 浏览器 AI+（受控浏览器会话 + CDP 操作） ──
  /** 浏览器档位非法（仅 ask_every_time / auto / disabled） */
  BROWSER_USE_INVALID_POLICY = 'BROWSER_USE_INVALID_POLICY',
  /** 本机没有可用的 Edge / Chrome */
  BROWSER_USE_NOT_FOUND = 'BROWSER_USE_NOT_FOUND',
  /** 档位为「禁用」，执行器拒绝启动与操作 */
  BROWSER_USE_DISABLED = 'BROWSER_USE_DISABLED',
  /** 只允许 http/https 页面 */
  BROWSER_USE_BLOCKED_SCHEME = 'BROWSER_USE_BLOCKED_SCHEME',
  /** 动作参数非法（缺少 URL、无效序号等） */
  BROWSER_USE_INVALID_ACTION = 'BROWSER_USE_INVALID_ACTION',
  /** 「每次询问」档位下用户取消了确认框 */
  BROWSER_USE_DENIED_BY_USER = 'BROWSER_USE_DENIED_BY_USER',
  /** 当前系统没有可用的原生确认框 */
  BROWSER_USE_CONFIRM_UNAVAILABLE = 'BROWSER_USE_CONFIRM_UNAVAILABLE',
  /** 浏览器启动失败 */
  BROWSER_USE_LAUNCH_FAILED = 'BROWSER_USE_LAUNCH_FAILED',
  /** 浏览器启动超时（未写出 DevTools 端口） */
  BROWSER_USE_LAUNCH_TIMEOUT = 'BROWSER_USE_LAUNCH_TIMEOUT',
  /** 受控浏览器会话读写失败 */
  BROWSER_USE_SESSION_FAILED = 'BROWSER_USE_SESSION_FAILED',
  /** 用户主动结束受控浏览器失败 */
  BROWSER_USE_STOP_FAILED = 'BROWSER_USE_STOP_FAILED',
  /** 页面操作执行失败（CDP 拒绝或连接中断） */
  BROWSER_USE_ACTION_FAILED = 'BROWSER_USE_ACTION_FAILED',
  /** 本机浏览器网关拒绝了请求（token 缺失或不匹配） */
  BROWSER_USE_GATE_UNAUTHORIZED = 'BROWSER_USE_GATE_UNAUTHORIZED',
  /** 网关请求体不是合法 JSON */
  BROWSER_USE_BAD_JSON = 'BROWSER_USE_BAD_JSON',

  // ── 终端 AI+ 与 ACP 委派 ──
  /** 终端开关取值非法（仅 true / false） */
  TERMINAL_POLICY_INVALID = 'TERMINAL_POLICY_INVALID',
  /** 终端 AI+ 未启用（显式开启前不允许开会话或输入） */
  TERMINAL_DISABLED = 'TERMINAL_DISABLED',
  /** 当前运行通道没有本机 shell 进程（浏览器 / Mock 通道） */
  TERMINAL_UNSUPPORTED = 'TERMINAL_UNSUPPORTED',
  /** 工作目录不存在或不是目录 */
  TERMINAL_CWD_INVALID = 'TERMINAL_CWD_INVALID',
  /** 本应用没有这个会话记录（界面刷新或已关闭） */
  TERMINAL_SESSION_UNKNOWN = 'TERMINAL_SESSION_UNKNOWN',
  /** 会话不存在或已关闭（引擎侧） */
  TERMINAL_NOT_FOUND = 'TERMINAL_NOT_FOUND',
  /** 会话数达到上限 */
  TERMINAL_LIMIT = 'TERMINAL_LIMIT',
  /** shell 已退出 */
  TERMINAL_EXITED = 'TERMINAL_EXITED',
  /** 单次输入超过 64KB */
  TERMINAL_INPUT_TOO_LARGE = 'TERMINAL_INPUT_TOO_LARGE',
  /** 输入来源非法（仅 manual / ai） */
  TERMINAL_SOURCE_INVALID = 'TERMINAL_SOURCE_INVALID',
  /** AI 建议缺少问题描述 */
  TERMINAL_QUESTION_REQUIRED = 'TERMINAL_QUESTION_REQUIRED',
  /** shell 启动失败 */
  TERMINAL_SPAWN_FAILED = 'TERMINAL_SPAWN_FAILED',
  /** 写入 shell 失败 */
  TERMINAL_WRITE_FAILED = 'TERMINAL_WRITE_FAILED',
  /** 命令建议调用模型失败 */
  TERMINAL_AI_FAILED = 'TERMINAL_AI_FAILED',
  /** 终端会话表不可用 */
  TERMINAL_STATE_FAILED = 'TERMINAL_STATE_FAILED',
  /** 引擎终端接口失败且没有返回具体错误码 */
  TERMINAL_FAILED = 'TERMINAL_FAILED',
  /** ACP 审批策略非法（仅 deny / allow） */
  ACP_PERMISSION_INVALID = 'ACP_PERMISSION_INVALID',
  /** ACP 超时不在 60–1800 秒之间 */
  ACP_TIMEOUT_INVALID = 'ACP_TIMEOUT_INVALID',
  /** 启用 ACP 但没有启动命令 */
  ACP_COMMAND_REQUIRED = 'ACP_COMMAND_REQUIRED',
  /** 存量配置里启用了 ACP 却没有命令（按未启用处理） */
  ACP_COMMAND_MISSING = 'ACP_COMMAND_MISSING',
  /** 外部 Agent 启动命令找不到可执行文件 */
  ACP_COMMAND_NOT_FOUND = 'ACP_COMMAND_NOT_FOUND',
  /** 外部 Agent 启动失败 */
  ACP_SPAWN_FAILED = 'ACP_SPAWN_FAILED',
  /** 外部 Agent 在握手阶段退出 */
  ACP_PROCESS_EXITED = 'ACP_PROCESS_EXITED',
  /** 委派任务超时，进程树已结束 */
  ACP_TIMEOUT = 'ACP_TIMEOUT',
  /** 外部 Agent 返回 JSON-RPC 错误 */
  ACP_RPC_ERROR = 'ACP_RPC_ERROR',
  /** 外部 Agent 要求交互式认证（当前未实现） */
  ACP_AUTH_REQUIRED = 'ACP_AUTH_REQUIRED',
  /** 外部 Agent 请求终端能力（当前不支持） */
  ACP_TERMINAL_UNSUPPORTED = 'ACP_TERMINAL_UNSUPPORTED',
  /** 外部 Agent 越界读写工作目录外的文件 */
  ACP_PATH_DENIED = 'ACP_PATH_DENIED',

  // ── PII 出站脱敏与逐工具审批 ──
  /** 出站脱敏档位非法（仅 off / mask / block） */
  PII_MODE_INVALID = 'PII_MODE_INVALID',
  /** 档位读不出来时任务中止，不在未知档位下把内容发出去 */
  PRIVACY_CONFIG_UNAVAILABLE = 'PRIVACY_CONFIG_UNAVAILABLE',
  /** block 档在发给模型的文本里检测到 PII/明文密钥，任务中止 */
  PRIVACY_OUTBOUND_BLOCKED = 'PRIVACY_OUTBOUND_BLOCKED',
  /** 审批档位非法（仅 off / sensitive / all） */
  TOOL_APPROVAL_INVALID_POLICY = 'TOOL_APPROVAL_INVALID_POLICY',
  /** 审批已开启但本机确认网关不可用（任务中止，不静默绕过） */
  TOOL_APPROVAL_UNAVAILABLE = 'TOOL_APPROVAL_UNAVAILABLE',
  /** 审批中间件没有装上（执行中止） */
  TOOL_APPROVAL_GUARD_FAILED = 'TOOL_APPROVAL_GUARD_FAILED',
  /** 用户拒绝了本次工具调用 */
  TOOL_APPROVAL_DENIED_BY_USER = 'TOOL_APPROVAL_DENIED_BY_USER',
  /** 当前系统没有可用的原生确认框 */
  TOOL_APPROVAL_CONFIRM_UNAVAILABLE = 'TOOL_APPROVAL_CONFIRM_UNAVAILABLE',

  // ── 文件访问档位（每次询问 / 自动审批 / 完全访问） ──
  /** 档位非法（仅 ask_every_time / auto_approve / full_access），任务不受理 */
  FILE_PERMISSION_INVALID = 'FILE_PERMISSION_INVALID',
  /** 档位为「每次询问」时用户拒绝了这次写/删，操作未执行 */
  FILE_PERMISSION_DENIED_BY_USER = 'FILE_PERMISSION_DENIED_BY_USER',
  /** 档位不是「每次询问」却来敲门：确认框不该弹，弹了也不放行 */
  FILE_PERMISSION_ASK_NOT_REQUIRED = 'FILE_PERMISSION_ASK_NOT_REQUIRED',
  /** 系统确认框拉不起来（缺 zenity/kdialog、线程异常），这次写/删未执行 */
  FILE_PERMISSION_CONFIRM_UNAVAILABLE = 'FILE_PERMISSION_CONFIRM_UNAVAILABLE',
  /** 删除前的回收目录建不出来或不可写，文件保持原样 */
  TRASH_DIR_UNAVAILABLE = 'TRASH_DIR_UNAVAILABLE',
  /** 回收目录同秒重名过多，放弃这次删除 */
  TRASH_NAME_EXHAUSTED = 'TRASH_NAME_EXHAUSTED',
  /** 回收文件名取不出来（路径异常），不删原文件 */
  TRASH_NAME_UNAVAILABLE = 'TRASH_NAME_UNAVAILABLE',
  /** 复制进回收目录失败，文件未被删除（删除永远先备份） */
  TRASH_BACKUP_FAILED = 'TRASH_BACKUP_FAILED',

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

  // ── 长期记忆库（桌面 memories 表：跨任务、按项目归属、去重合并） ──
  /** 记忆归属非法（仅 global / workspace，项目记忆须带合法工作区 id） */
  MEMORY_INVALID_SCOPE = 'MEMORY_INVALID_SCOPE',
  /** 记忆类型非法（仅 task / fact / preference / decision） */
  MEMORY_INVALID_KIND = 'MEMORY_INVALID_KIND',
  /** 记忆内容为空 */
  MEMORY_CONTENT_EMPTY = 'MEMORY_CONTENT_EMPTY',
  /** 记忆内容超出单条上限 */
  MEMORY_CONTENT_TOO_LONG = 'MEMORY_CONTENT_TOO_LONG',
  /** 记忆条目不存在（删除时按 id 未命中） */
  MEMORY_NOT_FOUND = 'MEMORY_NOT_FOUND',
  /** 记忆写入/合并失败 */
  MEMORY_SAVE_FAILED = 'MEMORY_SAVE_FAILED',
  /** 记忆列表读取失败 */
  MEMORY_LIST_FAILED = 'MEMORY_LIST_FAILED',
  /** 单条记忆读取失败 */
  MEMORY_LOAD_FAILED = 'MEMORY_LOAD_FAILED',
  /** 复核动作非法（仅 confirm / reject / archive / restore） */
  MEMORY_STATUS_INVALID = 'MEMORY_STATUS_INVALID',
  /** 该记忆已被更新的同类事实取代，不能直接确认/恢复 */
  MEMORY_SUPERSEDED = 'MEMORY_SUPERSEDED',
  /** 结构化事实字段非法（主体/属性/取值超长） */
  MEMORY_INVALID_FACT = 'MEMORY_INVALID_FACT',
  /** 内容疑似包含凭据，拒绝写入记忆库 */
  MEMORY_SECRET_BLOCKED = 'MEMORY_SECRET_BLOCKED',
  /** 导入文件不是合法导出格式（format/version/entries 校验失败） */
  MEMORY_IMPORT_INVALID = 'MEMORY_IMPORT_INVALID',
  /** 导出记忆到文件失败（目录不可写/磁盘满） */
  MEMORY_EXPORT_FAILED = 'MEMORY_EXPORT_FAILED',
  /** 浏览器直连没有桌面记忆库（复核/实体页/导入导出只在桌面版可用） */
  MEMORY_DESKTOP_ONLY = 'MEMORY_DESKTOP_ONLY',

  // ── IM 频道（引擎 /api/im/*：长连接只跑在引擎进程里） ──
  /** 引擎未运行：频道绑定与状态只有引擎知道，不做前端假清单 */
  IM_ENGINE_UNAVAILABLE = 'IM_ENGINE_UNAVAILABLE',
  /** 引擎在跑但接口无响应（进程已退出或端口失效） */
  IM_ENGINE_UNREACHABLE = 'IM_ENGINE_UNREACHABLE',
  /** 引擎频道接口返回 HTTP 层错误 */
  IM_ENGINE_ERROR = 'IM_ENGINE_ERROR',
  /** 引擎频道接口返回体无法解析 */
  IM_BAD_RESPONSE = 'IM_BAD_RESPONSE',
  /** 没有这个频道标识 */
  IM_CHANNEL_UNKNOWN = 'IM_CHANNEL_UNKNOWN',
  /** 频道标识非法（含路径注入字符） */
  IM_CHANNEL_INVALID = 'IM_CHANNEL_INVALID',
  /** 该 IM 的长连接尚未接入引擎，界面显示「尚未接入」 */
  IM_UNSUPPORTED = 'IM_UNSUPPORTED',
  /** 引擎运行时缺该频道的官方 SDK（如飞书 lark-oapi），通道不可用 */
  IM_DEPENDENCY_MISSING = 'IM_DEPENDENCY_MISSING',
  /** 未绑定凭据就尝试启动连接 */
  IM_NOT_CONFIGURED = 'IM_NOT_CONFIGURED',
  /** 缺少必填凭据 */
  IM_CREDENTIAL_MISSING = 'IM_CREDENTIAL_MISSING',
  /** 凭据格式明显不对（如 Slack App Token 不是 xapp- 开头） */
  IM_CREDENTIAL_INVALID = 'IM_CREDENTIAL_INVALID',
  /** 扫码登录查状态时没带二维码票据 */
  IM_LOGIN_TICKET_MISSING = 'IM_LOGIN_TICKET_MISSING',
  /** 凭据被 IM 服务端拒绝（真实的鉴权往返失败） */
  IM_AUTH_FAILED = 'IM_AUTH_FAILED',
  /** IM 服务端返回业务错误 */
  IM_API_ERROR = 'IM_API_ERROR',
  /** 长连接被对端关闭 */
  IM_CONNECTION_CLOSED = 'IM_CONNECTION_CLOSED',
  /** 建立长连接失败（网络/域名不可达） */
  IM_CONNECTION_FAILED = 'IM_CONNECTION_FAILED',
  /** 同一机器人只允许一条长连接，本条被新连接顶掉（引擎已停止自动重连，避免两边互踢） */
  IM_CONNECTION_KICKED = 'IM_CONNECTION_KICKED',
  /** 通道内部异常 */
  IM_CHANNEL_ERROR = 'IM_CHANNEL_ERROR',
  /** 消息已处理但回贴失败 */
  IM_REPLY_FAILED = 'IM_REPLY_FAILED',
  /** 偏好项非法（未知键或不是 true/false） */
  IM_PREFERENCE_INVALID = 'IM_PREFERENCE_INVALID',
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
export type { PiiOutboundMode } from './types';
export type { FilePermissionMode } from './types';

export interface ChatReviewReq {
  /** 计划审阅时必填；从检查点恢复（resume）时不需要计划 */
  planId?: string;
  action: 'approve' | 'reject' | 'modify' | 'resume';
  patch?: Record<string, unknown>;
}

/**
 * 对话事件流契约（与 types.ts 中 ChatEvent 保持结构一致，
 * 一致性由 tests/unit/shared/contract.test.ts 门禁保障）。
 * seq/taskId 为三层架构演进 P0 新增可选字段（AgentEvent 单调 seq，契约兼容变更）。
 */
export type ChatEvent =
  | { type: 'plan'; planId: string; steps: ChatStepPreview[]; seq?: number; taskId?: string }
  | {
      type: 'step_start';
      stepId: string;
      stepType: StepType;
      description: string;
      /** 引擎直连通道随事件带出的工具名；Rust 转发只留 stepId+description，界面需从描述兜底解析 */
      tool?: string;
      seq?: number;
      taskId?: string;
    }
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
  | { type: 'quota_warning'; level: '70' | '85' | '95' | '100'; action: 'notify' | 'degrade' | 'queue' | 'block'; usedTokens: number; quotaTokens: number; seq?: number; scope?: 'daily' | 'task'; taskId?: string }
  | {
      type: 'checkpoint';
      taskId: string;
      /** 是否可从最近一次模型调用前的检查点继续（仅引擎进程内有效） */
      resumable: boolean;
      reason: string;
      savedAt?: string;
      scope?: string;
      snapshotMessages?: number;
      restoredMessages?: number;
      executedTools?: number;
      sideEffects?: number;
      skippedDuplicates?: number;
      modelCalls?: number;
      mediaOmitted?: number;
      droppedMessages?: number;
      captureError?: string;
      /** 本地 harness：一轮工具调用被中断时，还没拿到结果的数量与工具名（不含参数） */
      pendingToolCalls?: number;
      pendingTools?: string[];
      seq?: number;
    }
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

/** 工作区后端（本机目录已实现；容器/SSH 明确未提供） */
export interface WorkspaceBackendCapability {
  id: string;
  label: string;
  supported: boolean;
  isolation: string;
  reason: string;
}

export type WorkspaceDataRootSource = 'env' | 'appdata' | 'home' | 'cwd' | 'browser';

/** 工作区能力视图（Rust workspace_capabilities 契约镜像） */
export interface WorkspaceCapabilities {
  activeBackend: string;
  backends: WorkspaceBackendCapability[];
  /** 数据根：桌面端为真实路径；浏览器通道为 localStorage */
  dataRoot: string;
  dataRootExists: boolean;
  dataRootSource: WorkspaceDataRootSource;
  dataRootVariable: string;
  customRoot: {
    supported: boolean;
    variable: string;
    requiresRestart: boolean;
    reason: string;
  };
  deleteSupported: boolean;
  maxIdLen: number;
}

/**
 * 引擎 HTTP 端点（Rust `bridge::agent::endpoint_of` 契约镜像）。
 * 端口由进程分配，前端不得再写死 18000：第二个工作空间的实例可能在 18001。
 */
export interface EngineEndpoint {
  workspaceId: string;
  /** `http://127.0.0.1:{port}` */
  baseUrl: string;
  port: number;
  status: AgentInstanceStatus;
  /** 桌面壳注入的本机引擎令牌；渲染层直连 /api 时随请求携带 */
  token?: string;
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

/** 技能静态扫描结论（Rust `security::skill_scan` 同口径；启发式，不是安全认证） */
export interface SkillScanFinding {
  code: string;
  severity: 'block' | 'warn';
  message: string;
  excerpt: string;
}

export interface SkillScanReport {
  level: 'clean' | 'warn' | 'block';
  summary: string;
  findings: SkillScanFinding[];
}

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
  /** 静态风险扫描；正文不可读时为 null/缺省 */
  scan?: SkillScanReport | null;
  /** 运行时拦截标记：高风险正文未进入模型上下文（历史技能可能带此标记） */
  scanBlocked?: boolean;
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

// ── 本地知识库（文档落磁盘，索引是派生物） ──────────────────

export interface KnowledgeDoc {
  name: string;
  size: number;
  updatedAt: string;
  /** 是否有抽取好的正文（缺失时引擎跳过，页面如实标出） */
  hasText: boolean;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  /** 归属：global=所有项目共享；workspace:<id>=只在指定工作区进入检索面 */
  scope: string;
  createdAt: string;
  updatedAt: string;
  /** 知识库目录绝对路径（桌面端真实落盘位置） */
  dir: string;
  docs: KnowledgeDoc[];
  missingText: string[];
}

export interface KnowledgeAddResult {
  base: KnowledgeBase;
  added: string[];
  errors: string[];
}

export interface KnowledgeSearchHit {
  doc: string;
  seq: number;
  text: string;
  score: number;
  baseId: string;
  baseName: string;
  /** 形如【知识库 / 文档 #1】，模型引用与页面展示共用 */
  citation: string;
}

export interface KnowledgeSearchResult {
  ok?: boolean;
  results: KnowledgeSearchHit[];
  notes?: string[];
}

// ── 专家团队（多专家运行时；注册表落 settings_kv.experts.registry） ──

export interface ExpertInfo {
  id: string;
  name: string;
  description: string;
  /** 负责人/子 Agent 的角色提示词 */
  prompt: string;
  modelId: string | null;
  modelName: string | null;
  /** 绑定模型不可用时的原因（保存后模型被删/缺密钥）；接口从不返回密钥 */
  modelError: string | null;
  /** 技能白名单：空数组表示不限制，沿用会话内已启用技能 */
  skills: string[];
  updatedAt: number;
}

/** 新建/更新专家（id 缺省为新建；传入不存在 id 会 EXPERT_NOT_FOUND，不静默新建） */
export interface ExpertSaveReq {
  id?: string | null;
  name: string;
  description: string;
  prompt: string;
  modelId?: string | null;
  skills: string[];
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

// ── 插件 SDK（套件包安装前预检与起始模板） ──────────────────────

/** 指令正文来源：content 字段 / file 指定文件 / commands/<名称>.md 约定 / 缺失 */
export type PluginBodySource = 'content' | 'file' | 'convention' | 'missing';

export interface PluginCommandCheck {
  name: string;
  description: string;
  bodySource: PluginBodySource;
  bodyChars: number;
  /** 正文前 120 字，供安装前确认 */
  preview: string;
  error: string | null;
}

export interface PluginManifestSummary {
  name: string | null;
  version: string | null;
  author: string | null;
  category: string | null;
  description: string | null;
}

/**
 * 安装前预检结果（只读，不改动已安装套件）。
 * errors 非空即 installable=false；warnings 是「能装但行为会打折」的说明。
 */
export interface PluginValidation {
  ok: boolean;
  installable: boolean;
  fileName: string;
  pluginId: string;
  manifest: PluginManifestSummary;
  commands: PluginCommandCheck[];
  skillFiles: string[];
  declaredSkillCount: number;
  /** 清单里本应用不消费的字段：不会执行，也不假装支持 */
  ignoredFields: string[];
  warnings: string[];
  errors: string[];
  fileCount: number;
  totalBytes: number;
}

export interface PluginTemplateFile {
  path: string;
  content: string;
}

export interface PluginTemplate {
  files: PluginTemplateFile[];
  notes: string[];
}

export interface PluginValidateReq {
  fileName?: string;
  zipBase64?: string;
}

// ── 在线技能 / 套件市场（桌面端下载并复用本地安装路径） ──────────

/** 市场索引源配置（两个来源独立，可只配置其中一种） */
export interface MarketSourceConfig {
  /** 当前通道是否具备市场下载与落盘能力 */
  supported: boolean;
  skillIndexUrl: string | null;
  suiteIndexUrl: string | null;
}

/** 修改市场源：字段缺省表示不动，null / 空串表示清除 */
export interface MarketSourcePatch {
  skillIndexUrl?: string | null;
  suiteIndexUrl?: string | null;
}

/** 索引签名策略：required 时未签名条目在拉取阶段就会被拒绝 */
export type MarketSignaturePolicy = 'required' | 'optional';

/** 索引声明的发布者（公钥是公开信息；是否信任由本机信任库决定） */
export interface MarketPublisher {
  id: string;
  name: string | null;
  /** base64（无填充）Ed25519 公钥，32 字节 */
  publicKey: string;
  /** SHA-256 公钥指纹（64 位小写十六进制） */
  fingerprint: string;
  trusted: boolean;
  /** 本机信任过同一 id 的另一把公钥；需要先移除旧信任 */
  keyChanged: boolean;
}

/** 本机信任的发布者（只在本机 settings 中保存） */
export interface MarketTrustedPublisher {
  id: string;
  name: string | null;
  publicKey: string;
  fingerprint: string;
  addedAtMs: number;
}

export interface MarketTrustList {
  supported: boolean;
  publishers: MarketTrustedPublisher[];
}

export interface MarketTrustAddReq {
  publisherId: string;
  publicKey: string;
  name?: string | null;
}

export interface MarketTrustRemoveReq {
  publisherId: string;
}

/** 市场条目公共字段（不向渲染层暴露下载地址，安装只传 itemId） */
export interface MarketItemBase {
  id: string;
  name: string;
  description: string | null;
  version: string | null;
  author: string | null;
  /** 条目主页；仅展示，不在应用内自动打开 */
  homepage: string | null;
  /** 索引声明的 SHA-256（64 位小写十六进制），下载后必须一致 */
  sha256: string;
  sizeBytes: number | null;
  /** 本机是否已安装同标识条目 */
  installed: boolean;
  /** 索引声明该条目（或该版本）已被撤销：装不上，已装的在检查更新后转禁用 */
  revoked: boolean;
  revokedReason: string | null;
  /** 发布者与签名状态；未签名条目由索引策略决定能否安装 */
  publisherId: string | null;
  publisherName: string | null;
  publisherFingerprint: string | null;
  signed: boolean;
  publisherTrusted: boolean;
  publisherKeyChanged: boolean;
}

export interface MarketSkillItem extends MarketItemBase {}

export interface MarketSuiteItem extends MarketItemBase {
  category: string | null;
  publishedAtMs: number | null;
  skillCount: number | null;
  commandCount: number | null;
  dataLinkCount: number | null;
}

/** 市场列表结果：未配置时 configured=false，不当作错误 */
export interface MarketList<T> {
  supported: boolean;
  configured: boolean;
  indexUrl: string | null;
  /** 拉取成功时间；未配置时为 0 */
  fetchedAtMs: number;
  /** 未配置时为 null */
  signaturePolicy: MarketSignaturePolicy | null;
  /** 撤销清单（索引顶层 revokedUrl）是否读到；读不到时只代表「本轮没拿到外部清单」 */
  revokedSourceOk: boolean;
  revokedSourceError: string | null;
  publishers: MarketPublisher[];
  items: T[];
}

export interface MarketInstallReq {
  itemId: string;
}

/** 本机已安装条目的更新/撤销状态；只有这六种，不臆造「最新」 */
export type MarketInstalledStatus =
  | 'upToDate'
  | 'updateAvailable'
  | 'ahead'
  | 'unknownVersion'
  | 'delisted'
  | 'revoked';

/** 已安装清单条目（不联网也能读到的本机事实） */
export interface MarketInstalledItem {
  kind: 'skill' | 'suite';
  id: string;
  name: string | null;
  installedVersion: string | null;
  sha256: string | null;
  sourceHost: string | null;
  installedAtMs: number;
  /** market = 由在线市场安装并落清单；scanned = 本机扫盘补齐，不参与撤销比对 */
  provenance: 'market' | 'scanned';
  revoked: boolean;
  revokedReason: string | null;
  disabled: boolean;
}

export interface MarketInstalledList {
  supported: boolean;
  items: MarketInstalledItem[];
  /** 上次检查更新时间；从未检查过时为 null */
  lastCheckAtMs: number | null;
}

/** 检查更新 + 应用撤销的报告条目 */
export interface MarketUpdateItem extends MarketInstalledItem {
  latestVersion: string | null;
  status: MarketInstalledStatus;
  updateAvailable: boolean;
  /** 已被撤销并在执行面拦掉 */
  blocked: boolean;
}

export interface MarketUpdateReport {
  supported: boolean;
  checkedAtMs: number;
  /** 本轮实际读取了几个索引源（0/1/2）；未配置的来源不会伪造结论 */
  sourcesConfigured: number;
  /** 逐来源的读取失败说明；非空不代表整体失败 */
  errors: string[];
  items: MarketUpdateItem[];
  summary: {
    updateAvailable: number;
    revoked: number;
    delisted: number;
    disabledApplied: number;
  };
}

/**
 * 单条升级的回执：在安装回执（技能目录 / 套件目录的现状）之上，额外给出
 * 「旧版本去了哪」与「市场这一版是什么」。升级不物理删除旧版本，回收目录路径
 * 必须回给页面，用户才有手工放回的办法。
 */
export type MarketUpgradeResult<T> = T & {
  upgraded: true;
  /** 旧版本被移入的回收目录绝对路径 */
  previousVersionTrash: string;
  /** 本次落下的市场版本（技能目录本身不存版本，只能由市场条目给出） */
  marketVersion: string | null;
};

/** 一键升级的逐条结果：失败条只带原因，不影响同批其余条目 */
export interface MarketUpgradeItemResult {
  kind: 'skill' | 'suite';
  id: string;
  name: string | null;
  ok: boolean;
  /** 本机清单里升级前的版本；从未落过清单时为 null */
  fromVersion: string | null;
  /** 成功时为落下的新版本；失败时为 null */
  toVersion: string | null;
  /** 成功时：旧版本的回收目录 */
  previousVersionTrash?: string | null;
  /**
   * 成功时：升级后该条目是否启用。撤销后被禁用的条目**不会**因为升级自动放开，
   * 开关仍由用户在技能页决定（页面要如实显示这个状态）
   */
  enabled?: boolean;
  /** 失败时：Rust 侧原样错误串（带 MARKET_* / SKILL_* / SUITE_* 前缀） */
  error?: string;
}

/** 一键升级报告：只处理检查更新判定为 updateAvailable 的条目 */
export interface MarketUpgradeReport {
  supported: boolean;
  /** 本次批量前置的检查更新时间戳 */
  checkedAtMs: number;
  attempted: number;
  upgraded: number;
  failed: number;
  /** 检查更新阶段的逐源读取失败，非空不代表升级失败 */
  errors: string[];
  items: MarketUpgradeItemResult[];
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
  /** 当前是否处于 Mock 开关（TT_AUTH_MOCK=true） */
  mock: boolean;
  /** 账号 / 协作服务地址（默认综合服务端 http://127.0.0.1:8000） */
  serverUrl?: string;
}

/** 账号服务连通性探测（auth_server_probe 返回；不抛错，原因在 error） */
export interface AuthServerProbe {
  baseUrl: string;
  reachable: boolean;
  service: string | null;
  version: string | null;
  error: string | null;
}

/** 登录结果（auth_login 返回） */
export interface AuthLoginResp {
  ok: boolean;
  profile: AuthProfile;
  mock: boolean;
}

// ── 项目协作（跨用户 / 跨机器 · 人工触发任务交接） ──
//
// 共享边界：项目元数据、成员与角色、任务、评论、结果摘要与产物文件名。
// 不共享：对话、产物正文、技能、凭据、运行时状态。

export type CollabRole = 'owner' | 'admin' | 'member' | 'viewer';
export type CollabTaskStatus = 'todo' | 'doing' | 'review' | 'done';
export type CollabResultStatus = 'review' | 'done';

/** 协作服务可用性（collab_status 返回；不含任何令牌） */
export interface CollabStatus {
  supported: boolean;
  /** 账号 / 协作服务地址（TT_AUTH_SERVER_URL） */
  baseUrl: string;
  /** 地址是否合法（远程必须 HTTPS，本机回环可用 HTTP） */
  serverValid: boolean;
  /** 是否持有可刷新的真实登录态（Mock 登录为 false） */
  loggedIn: boolean;
}

export interface CollabMember {
  uid: string;
  nickname: string;
  role: CollabRole;
  joinedAtMs: number;
}

export interface CollabTaskCounts {
  todo: number;
  doing: number;
  review: number;
  done: number;
}

export interface CollabProject {
  id: string;
  name: string;
  description: string;
  ownerUid: string;
  myRole: CollabRole;
  memberCount: number;
  taskCounts: CollabTaskCounts;
  seq: number;
  createdAtMs: number;
  updatedAtMs: number;
}

export interface CollabTaskResult {
  summary: string;
  /** 只含文件名，不含本机路径 */
  artifacts: string[];
  machine: string;
  by: string;
  byNickname: string;
  atMs: number;
}

export interface CollabTask {
  id: string;
  projectId: string;
  title: string;
  detail: string;
  status: CollabTaskStatus;
  assignee: { uid: string; nickname: string } | null;
  createdBy: { uid: string; nickname: string };
  /** 乐观并发版本：更新与回报都必须带上，冲突时服务端返回 PROJECT_CONFLICT */
  version: number;
  result: CollabTaskResult | null;
  createdAtMs: number;
  updatedAtMs: number;
}

export interface CollabComment {
  id: string;
  taskId: string | null;
  author: { uid: string; nickname: string };
  body: string;
  createdAtMs: number;
}

export interface CollabInvite {
  id: string;
  email: string;
  role: CollabRole;
  invitedBy: { uid: string; nickname: string };
  expiresAtMs: number;
  createdAtMs: number;
}

/** 项目快照（seq 为事件序号，可用于轮询时判断是否有变化） */
export interface CollabSnapshot {
  ok: true;
  project: CollabProject;
  members: CollabMember[];
  tasks: CollabTask[];
  comments: CollabComment[];
  /** 仅 owner/admin 可见；只含待接受邀请，不含邀请码 */
  invites: CollabInvite[];
  seq: number;
}

export interface CollabProjectListResp {
  ok: true;
  projects: CollabProject[];
}

export interface CollabInviteCreatedResp {
  ok: true;
  invite: CollabInvite;
  /** 明文邀请码只在此次响应返回一次，服务端只存摘要 */
  inviteCode: string;
  project: CollabProject;
}

export interface CollabTaskResp {
  ok: true;
  task: CollabTask;
  seq: number;
}

export interface CollabCommentResp {
  ok: true;
  comment: CollabComment;
  seq: number;
}

// ── 项目事件订阅（缺陷 D-06：实时推送替掉 10 秒轮询） ──────────

/**
 * 桌面端从账号服务项目事件流转发出来的一条事件。
 * seq 是服务端全库自增：只能比大小，不能假设相邻（中间的可能属于别的项目）。
 */
export interface CollabEvent {
  projectId: string;
  seq: number;
  kind: string;
  actorUid: string | null;
  payload: unknown;
  createdAtMs: number | null;
}

/** 订阅自身的状态；界面用它决定「实时」还是退回轮询，降级时必须看得见 */
export interface CollabWatchStateEvent {
  projectId: string;
  state: 'connecting' | 'streaming' | 'degraded' | 'closed' | 'stopped';
  /** 已交付到事件流的游标（重连从这里续） */
  cursor: number;
  reason: string;
}

export interface CollabWatchStartResp {
  ok: true;
  projectId: string;
  cursor: number;
  state: string;
}

export interface CollabWatchStopResp {
  ok: true;
  stopped: boolean;
}

export interface CollabWatchStatusResp {
  ok: true;
  watches: Array<{ projectId: string; cursor: number; stopping: boolean }>;
}

// ── 工单层（缺陷 D-47 · 授权工作单元与待人工状态机） ──────────
//
// 与服务端 `app/tickets.py` 的视图同构。「等人」只由结构化字段判定：
// stage ∈ {awaiting_answer, awaiting_review, blocked} 才进「等你的工单」，
// 停止是唯一撤权动作；判定都在服务端，前端只是如实展示与提交动作。

export type AgentTicketStage =
  | 'input_undelivered'
  | 'queued'
  | 'working'
  | 'awaiting_answer'
  | 'awaiting_review'
  | 'idle'
  | 'blocked'
  | 'closed';

export type AgentReportKind = 'progress' | 'waiting' | 'ready' | 'blocked' | 'failed' | 'dropped';
export type AgentAssignmentEndStatus = 'finished' | 'dropped' | 'lost';
export type AgentMandateIntensity = 'explicit' | 'inferred';

export interface AgentMandate {
  id: string;
  ticketId: string;
  seq: number;
  brief: string;
  scope: string;
  outOfScope: string;
  intensity: AgentMandateIntensity;
  intensityLabel: string;
  sourceKind: string;
  sourceRef: string;
  policyVersion: number;
  digest: string;
  status: 'active' | 'revoked';
  statusLabel: string;
  createdBy: string;
  createdAtMs: number;
  revokedAtMs: number;
  revokedBy: string;
  revokeReason: string;
}

export interface AgentAssignmentReadStep {
  seq: number;
  atMs: number;
  by: string;
}

export interface AgentAssignment {
  id: string;
  ticketId: string;
  mandateId: string;
  seq: number;
  workerId: string;
  holderUid: string;
  workspaceKind: string;
  workspaceRef: string;
  checkpointRef: string;
  machine: string;
  status: 'live' | 'ended';
  recoveryNote: string;
  readSteps: AgentAssignmentReadStep[];
  startedAtMs: number;
  endedAtMs: number;
  exitReason: string;
}

export interface AgentReport {
  id: string;
  ticketId: string;
  assignmentId: string;
  kind: AgentReportKind;
  kindLabel: string;
  summary: string;
  detail: string;
  /** 只含文件名，不含本机路径 */
  artifacts: string[];
  handedBack: boolean;
  contextSeq: number;
  createdBy: string;
  createdAtMs: number;
}

export interface AgentAttention {
  id: string;
  ticketId: string;
  reportId: string;
  kind: 'answer' | 'review';
  kindLabel: string;
  question: string;
  reason: string;
  options: string[];
  status: 'open' | 'answered' | 'withdrawn';
  answer: string;
  answeredBy: string;
  answeredAtMs: number;
  createdAtMs: number;
}

export interface AgentSubscription {
  id: string;
  ticketId: string;
  targetKind: string;
  targetRef: string;
  lastFannedSeq: number;
  createdAtMs: number;
  closedAtMs: number;
}

export interface AgentTicketView {
  id: string;
  projectId: string;
  projectName: string;
  title: string;
  detail: string;
  stage: AgentTicketStage;
  stageLabel: string;
  /** 服务端判定：这三种 stage 才需要人动手 */
  needsHuman: boolean;
  intensity: AgentMandateIntensity;
  createdBy: string;
  createdByNickname: string;
  createdFrom: string;
  version: number;
  closedAtMs: number;
  closedReason: string;
  createdAtMs: number;
  updatedAtMs: number;
}

/** 「等你的工单」列表项：视图 + 当前开放问题 + 授权摘要 */
export interface AgentTicketListItem extends AgentTicketView {
  attention: AgentAttention | null;
  mandateBrief: string;
  mandateSeq: number;
}

/** 扇出给在跑执行体的上下文条目（只补视野，不给授权） */
export interface AgentTicketContextItem {
  seq: number;
  kind: string;
  actorUid: string | null;
  payload: unknown;
  createdAtMs: number;
  subscriptionId: string;
}

export interface AgentMyTicketsResp {
  ok: true;
  tickets: AgentTicketListItem[];
  count: number;
}

export interface AgentTicketListResp {
  ok: true;
  tickets: AgentTicketView[];
  count: number;
}

export interface AgentTicketDetailResp {
  ok: true;
  ticket: AgentTicketView;
  mandate: AgentMandate | null;
  assignments: AgentAssignment[];
  reports: AgentReport[];
  attentions: AgentAttention[];
  subscriptions: AgentSubscription[];
  contexts: AgentTicketContextItem[];
}

export interface AgentTicketCreateResp {
  ok: true;
  ticket: AgentTicketView;
  mandate: AgentMandate;
  subscription: AgentSubscription | null;
  seq: number;
}

/** 建单入参（与 Rust build_ticket_create / 服务端 TicketCreateIn 白名单一致） */
export interface AgentTicketCreateIn {
  title: string;
  brief: string;
  detail?: string;
  scope?: string;
  outOfScope?: string;
  intensity?: AgentMandateIntensity;
  sourceKind?: string;
  /** intensity=inferred 时必填：从哪条事件推断出的授权 */
  sourceRef?: string;
  policyVersion?: number;
  subscribeTaskId?: string;
}

/** 领取入参（mandateId 必填；digest 允许缺省） */
export interface AgentAssignmentClaimIn {
  mandateId: string;
  workerId?: string;
  workspaceKind?: string;
  workspaceRef?: string;
  checkpointRef?: string;
  machine?: string;
  recoveryNote?: string;
  expectedMandateDigest?: string;
}

/** 报告入参（kind 六态；waiting 必须带 question） */
export interface AgentReportIn {
  kind: AgentReportKind;
  summary: string;
  assignmentId?: string;
  detail?: string;
  artifacts?: string[];
  question?: string;
  reason?: string;
  options?: string[];
  contextSeq?: number;
}

// ── Super-agent 主动候选（缺陷 D-46 · 影子段：只记账与判定，零自动执行） ──
//
// 类型是桌面 Rust `superagent` 模块的契约镜像（`src-tauri/src/superagent/*`）。
// 这一层的产物只有「这条环境事件本可接单」，没有任何执行入口：不要在这里加
// 「采纳并开工」这类字段，也不要在界面拼第二套显示名（`kindLabel` / `strengthLabel`
// / `modeLabel` / `statusLabel` 都由 Rust 给，和判定理由用的是同一份文案）。
// 表名一律不出现在渲染层（`tests/unit/storage/ownership-contract.test.ts` 全量扫描）。

/** 事件入口类型（与 Rust `SOURCE_KIND_*` 同集合；`im_inbound` 本段只登记不读取） */
export type SuperagentSourceKind =
  | 'project_event'
  | 'schedule'
  | 'condition'
  | 'workspace_file'
  | 'im_inbound';

/** 巡检档位：`auto` 需要工单层与执行体隔离先落地，Rust 侧会拒绝写入 */
export type SuperagentAcceptMode = 'off' | 'observe' | 'auto';

/** 判定强度：`explicit` 是唯一可以直接开工的强度，`inferred` 只在启用规则命中时给出 */
export type SuperagentStrength = 'explicit' | 'inferred' | 'not_admitted';

/** 判定结果（observe 档下阳性只记 `wouldAccept`，不会出现 `admitted`） */
export type SuperagentOutcome = 'not_admitted' | 'would_accept' | 'admitted' | 'mode_off';

/** 一条准入规则：留空的字段表示「不限」，`enabled` 为假时永远不命中 */
export interface SuperagentRule {
  id: string;
  enabled: boolean;
  kinds: string[];
  actors: string[];
  textAny: string[];
  threadAny: string[];
  minChars: number;
}

/** 一个事件入口实例的授权（按 `(kind, instance)` 精确匹配，不做整类放行） */
export interface SuperagentSourcePolicy {
  kind: SuperagentSourceKind;
  instance: string;
  /** 是否参与准入；与「是否在读取」是两回事，两个都要显式打开 */
  enabled: boolean;
  label: string;
  allowUsers: string[];
  bots: string[];
  rules: SuperagentRule[];
}

/** 准入策略文档（有版本、可哈希；内容没变不新增版本） */
export interface SuperagentPolicyDoc {
  acceptMode: SuperagentAcceptMode;
  dailyEvalCap: number;
  /**
   * 第二层（模型研判）的当日预算：管「今天最多问几次模型」。
   * 缺省由 Rust 补（`DEFAULT_ASSESS_CAP`），写 `0` 是显式关闭第二层——
   * 与「没配模型」不同：那是状态，这是决定。前端不传时 Rust 按缺省处理。
   */
  assessMaxPerDay?: number;
  sources: SuperagentSourcePolicy[];
}

/** 事件入口账本行：游标是这一个入口自己的读取位置 */
export interface SuperagentSource {
  id: string;
  kind: SuperagentSourceKind;
  /** 具体实例（项目 id / 会话 id / 目录），本机类入口允许为空 */
  instance: string;
  label: string;
  enabled: boolean;
  cursor: number;
  config: Record<string, unknown>;
  lastError: string | null;
  createdAtMs: number;
  updatedAtMs: number;
  /** Rust 给的显示名 */
  kindLabel: string;
}

/** 一条入账的环境事件 */
export interface SuperagentTrigger {
  id: string;
  sourceId: string;
  /** 上游幂等标识：同一入口内唯一，重复投递只有一行 */
  externalId: string;
  /** 上游事件类型（如 `task.commented`），不是入口类型 */
  kind: string;
  text: string;
  actor: string;
  threadKey: string;
  /** 来源声明的事实：这条事件是否直接指向本应用（正文里的 @ 不算） */
  addressedAgent: boolean;
  sourceSeq: number;
  arrivedAtMs: number;
  payload: unknown;
}

/** 主动候选：判定为「本可接单」的事件 + 依据 + 当时生效的策略版本 */
export interface SuperagentCandidate {
  decisionId: string;
  triggerId: string;
  policyVersion: number;
  strength: SuperagentStrength;
  outcome: SuperagentOutcome;
  wouldAccept: boolean;
  /** 判定依据（哪个入口、哪条规则给的强度） */
  basis: string;
  matchedRules: string[];
  reason: string;
  decidedAtMs: number;
  sourceId: string;
  sourceKind: SuperagentSourceKind;
  sourceInstance: string;
  sourceLabel: string;
  externalId: string;
  eventKind: string;
  text: string;
  actor: string;
  threadKey: string;
  addressedAgent: boolean;
  arrivedAtMs: number;
  payload: unknown;
  sourceKindLabel: string;
  strengthLabel: string;
  // ── 第二层（模型研判）的结论：`assessStatus` 为空串 = 这一条还没问过模型 ──
  // 结论只是「值不值得现在开工」的判断，同样不带来任何动作；显示名由 Rust 给（assess::*_label）。
  assessStatus: string;
  assessStatusLabel: string;
  assessVerdict: string;
  assessVerdictLabel: string;
  assessReason: string;
  assessModel: string;
  /** 这一次研判的 token 合计（计量口径，界面只显示不估算） */
  assessTokens: number;
  /** 失败原因（中文，不含凭据）；成功时为空串 */
  assessError: string;
  assessedAtMs: number;
  /**
   * 这一行现在排在哪个策略版本的研判队列里。与 `policyVersion` 可以不同：
   * `policyVersion` 是「依据哪一版策略判出来的」（留痕，不改写），
   * 这一列是「待办挂在哪一版」，策略升版时由 Rust 重排。
   */
  assessQueueVersion: number;
  /** 因策略升版被重排的时刻；0 = 从没被重排过 */
  requeuedAtMs: number;
  /** 重排说明（Rust 给的一句话；没重排过就没有这一项） */
  requeueLabel?: string;
}

/** 判定留痕（含阴性：为什么没做也要看得见） */
export interface SuperagentDecision {
  id: string;
  triggerId: string;
  policyVersion: number;
  strength: SuperagentStrength;
  outcome: SuperagentOutcome;
  wouldAccept: boolean;
  basis: string;
  matchedRules: string[];
  reason: string;
  decidedAtMs: number;
  strengthLabel: string;
}

/** 台账概览（数字全部来自本机账本，界面不自己数） */
export interface SuperagentStats {
  ok: true;
  acceptMode: string;
  modeLabel: string;
  /** 当前档位是否就是缺省档位（缺省即影子模式） */
  modeIsDefault: boolean;
  policyVersion: number;
  dailyEvalCap: number;
  /** 第二层（模型研判）的当日预算，来自策略；0 表示已显式关闭 */
  assessCap: number;
  decidedToday: number;
  /** 今日已研判（成功或失败都算花了钱）的条数 */
  assessedToday: number;
  /** 历史上判成功过的条数 */
  assessed: number;
  /** 当前策略版本下还欠多少条没判（按研判队列键统计，策略升版后重排的候选也在这里） */
  assessPending: number;
  /** 历史上被策略升版重排过的候选条数（回答「改了策略有没有把候选弄丢」） */
  requeued: number;
  sources: number;
  enabledSources: number;
  triggers: number;
  decisions: number;
  candidates: number;
  note: string;
}

/** 策略视图：可选项与「`auto` 为什么还不能用」都在这份里，界面不写死 */
export interface SuperagentPolicyView {
  version: number;
  doc: SuperagentPolicyDoc;
  modeLabel: string;
  /** Rust 允许的档位（本段只有 off / observe） */
  allowedModes: string[];
  autoAvailable: boolean;
  autoBlockedBy: string;
  sourceKinds: Array<{ kind: string; label: string; readable: boolean }>;
}

export interface SuperagentSavePolicyResp {
  version: number;
  /** 内容与当前版本一致时不新增版本，`changed` 为假 */
  changed: boolean;
  doc: SuperagentPolicyDoc;
  modeLabel: string;
  /** 升版时被重排到新队列的待研判候选条数（`changed` 为假时是 0） */
  requeued: number;
  /** 重排的中文说明；没有重排时 Rust 不给这一项 */
  requeueNote?: string | null;
}

export interface SuperagentPolicyVersion {
  version: number;
  digest: string;
  source: string;
  createdAtMs: number;
  doc: SuperagentPolicyDoc;
  modeLabel: string;
}

/** 一轮巡检里单个入口的结果 */
export interface SuperagentPumpRun {
  sourceId: string;
  kind: SuperagentSourceKind;
  instance: string;
  fetched: number;
  inserted: number;
  decided: number;
  candidates: number;
  /** ok / waiting / idle_login_required / fetch_failed / ingest_failed / capped */
  status: string;
  statusLabel: string;
  kindLabel: string;
  error: string | null;
}

/**
 * 一轮模型研判（第二层）的摘要。
 *
 * 口径写死在这里：这一层只回答「值不值得现在开工」并把结论记进判定行，
 * 不创建任务、不起工作区——所以摘要里没有任何「执行了多少」的字段。
 * `blocked` / `unavailable` 只活在这一轮摘要里：它们表示「这次没花成钱」，
 * 不会被写进判定行，候选因此还留在待研判队列里。
 */
export interface SuperagentAssessSummary {
  ok: boolean;
  policyVersion: number;
  /** 策略里的当日预算 */
  assessCap: number;
  /** 今日已研判数（分子来自账本，重启不丢） */
  capUsedToday: number;
  /** 这一轮最多判几条 */
  batch: number;
  pending: number;
  attempted: number;
  written: number;
  /**
   * 本轮因策略升版被作废的结论条数：模型调用已经发出、token 已记进配额与用量，
   * 但那份结论按旧策略语境问出来的，不写进判定行；候选已按新版本重排，下一轮再判。
   */
  superseded: number;
  worth: number;
  notWorth: number;
  unsure: number;
  failed: number;
  blocked: number;
  unavailable: number;
  /** 实际用的模型显示名；没发起调用时为 null */
  model: string | null;
  /** 整轮跳过的原因键（off / cap_off / cap_reached / no_pending / read_failed / quota_blocked / model_unavailable） */
  skipped: string | null;
  skipReason: string | null;
  lastError: string | null;
  note: string;
  tookMs?: number;
  startedAtMs?: number;
}

/** 巡检摘要（后台循环投的事件与「立即巡检」的返回同形状） */
export interface SuperagentPumpSummary {
  ok: boolean;
  startedAtMs?: number;
  tookMs?: number;
  acceptMode?: string;
  modeLabel?: string;
  policyVersion?: number;
  dailyEvalCap?: number;
  capUsed?: number;
  fetched?: number;
  inserted?: number;
  decided?: number;
  candidates?: number;
  /** 本轮跳过的原因键（如 `no_enabled_source`） */
  skipped?: string;
  skippedLabel?: string;
  /** 后台循环在这一轮顺手跑的模型研判（第二层）摘要；手动「立即巡检」不带这一项 */
  assessment?: SuperagentAssessSummary;
  sources: SuperagentPumpRun[];
  error?: string;
  note?: string;
}

/** 候选清单导出结果：路径由 Rust 拼（本机运行时目录下的 exports），界面只显示不拼接 */
export interface SuperagentExportResult {
  path: string;
  count: number;
  format: 'json' | 'csv';
  /** 影子模式声明，随文件一起落盘，界面也原样显示 */
  note: string;
}

export interface CollabTaskPatch {
  version: number;
  title?: string;
  detail?: string;
  status?: CollabTaskStatus;
  /** null 表示取消负责人 */
  assigneeUid?: string | null;
}

// ── Schedule ─────────────────────────────────────────────────

/** 定时频率（Rust `task::schedule::normalize_freq` 同口径，兼容界面中文标签）
 *
 * `interval` / `once` 由 D-30（W5 #139）落地后上收进主契约：Rust 早就按同一份 camelCase
 * 序列化，缺省即 undefined，所以这里是把已存在的后端事实登记进契约，不是新造口径。
 * `contracts_connect.ts` 的 D-30 扩展段自此只是别名，渲染层引用由 W5 自己收敛。
 */
export type ScheduleFreq = 'hourly' | 'daily' | 'weekly' | 'monthly' | 'interval' | 'once';

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
  /** 执行完成后推送的 IM 通道（空字符串＝不推送） */
  notifyChannel: string;
  /** 推送目标会话标识（Slack 频道 ID / 钉钉等已有会话 ID） */
  notifyTarget: string;
  /** 间隔步长（分钟），仅 freq=interval 有值；老任务为 NULL 而不是 0（0 会被读成「每 0 分钟」） */
  intervalMinutes?: number | null;
  /** 一次性任务的绝对执行时刻（UTC 毫秒），仅 freq=once 有值 */
  runAtMs?: number | null;
}

/** 一句话解析结果（Rust `task::schedule::ParsedSchedule` 契约镜像） */
export interface ScheduleParseResult {
  freq: ScheduleFreq;
  timeOfDay: string;
  weekday?: number | null;
  name: string;
  /** 解析时的默认假设说明（例如「未写频率，已按每天处理」） */
  note?: string | null;
  /** 间隔步长（分钟），仅 freq=interval */
  intervalMinutes?: number | null;
  /** 一次性任务的本地日期（yyyy-mm-dd，未做时区换算）；仅 freq=once */
  runDate?: string | null;
  /** runDate 来自「明天 / 下周三」这类相对说法：界面需按本地日重锚，否则 UTC+8 凌晨会差一天 */
  runDateRelative?: boolean;
  /** 中文回显（「每 2 小时执行一次」/「2026-03-06 09:00 执行一次（本地时间，确认后落库）」），先预览再确认用 */
  preview?: string;
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
  notifyChannel?: string;
  notifyTarget?: string;
  /** freq=interval 时的步长（分钟） */
  intervalMinutes?: number | null;
  /** freq=once 时的绝对执行时刻（UTC 毫秒） */
  runAtMs?: number | null;
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
  /** 传空串表示关闭推送（同时清空目标）；不传表示保持原值 */
  notifyChannel?: string;
  notifyTarget?: string;
  /** freq=interval 时的步长（分钟）；换成别的频率时置 NULL，留着旧步长会让界面显示一个不生效的配置 */
  intervalMinutes?: number | null;
  /** freq=once 时的绝对执行时刻（UTC 毫秒） */
  runAtMs?: number | null;
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

/** OAuth 授权状态（Rust `mcp::oauth::AUTH_*`；只反映系统凭据库里有无可用令牌） */
export type McpAuthStatus = 'none' | 'authorized' | 'expired';

/** 远程连接器 OAuth 公共客户端配置（非敏感；不含令牌或 client secret） */
export interface McpOAuthConfig {
  authorizationUrl: string;
  tokenUrl: string;
  clientId: string;
  scopes?: string[];
}

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
  /** 仅 Streamable HTTP 支持；配置后需在「连接」前完成授权 */
  auth?: McpOAuthConfig;
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
  /** 非敏感 OAuth 配置（仅 Streamable HTTP） */
  auth?: McpOAuthConfig;
  /** none / authorized / expired */
  authStatus: McpAuthStatus;
  createdAtMs: number;
  updatedAtMs: number;
}

// ── Usage & Stats ────────────────────────────────────────────

/** 用量统计周期（Rust `model::usage::normalize_period` 同口径） */
export type UsagePeriod = 'daily' | 'monthly' | 'total';

/** 按币种分桶的成本（micros；1 货币单位 = 1,000,000 micros） */
export interface UsageCost {
  currency: string;
  micros: number;
}

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
  /** 平均端到端耗时；没有延迟样本（旧数据）时为 null */
  avgLatencyMs?: number | null;
  latencySamples?: number;
  /** 按币种分开的成本；未配置单价的调用不计入 */
  costs?: UsageCost[];
  pricedCalls?: number;
  unpricedCalls?: number;
}

/** 用量查询视图（Rust `model_usage` command / 引擎 `/api/usage` 返回体） */
export interface ModelUsageView {
  period: UsagePeriod;
  rows: ModelUsageRow[];
}

/**
 * 用量汇总（`usage:stats` / `usage:report` 的 totals）。
 * 延迟按调用数加权；成本按币种分开，未配单价的调用不计入成本。
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
  /** 加权平均耗时；没有延迟样本时为 null */
  avgLatencyMs?: number | null;
  latencySamples?: number;
  costs?: UsageCost[];
  pricedCalls?: number;
  unpricedCalls?: number;
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
  avgLatencyMs?: number | null;
  latencySamples?: number;
  costs?: UsageCost[];
  pricedCalls?: number;
  unpricedCalls?: number;
}

/** 用量报表（Rust `usage_report` command 返回体：趋势 + 明细 + 汇总，自包含） */
export interface UsageReport {
  period: UsagePeriod;
  generatedAtMs: number;
  days: UsageDayPoint[];
  models: ModelUsageRow[];
  totals: UsageTotals;
}

/**
 * 桌面台账上报结果（D-43：`usage:upload`）。
 * `uploaded=false` 表示本地没有待上报的行，不是失败；未登录/服务不可达走错误。
 */
export interface UsageUploadResult {
  uploaded: boolean;
  rows: number;
  accepted: number;
  deviceId: string;
  message: string;
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
  /** supported=false 时的原因（如浏览器预览环境读不到磁盘），由界面如实显示 */
  note?: string;
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

/** 系统级沙箱设置：`enabled` 是用户意图，不等于已生效 */
export interface SystemSandboxConfig {
  enabled: boolean;
  network: boolean;
  updatedAtMs: number;
}

/** 系统级沙箱状态：请求 / 平台支持 / 实际生效三栏分开 */
export interface SystemSandboxStatus {
  requested: boolean;
  supported: boolean;
  effective: boolean;
  platform: string;
  profile: string;
  network: boolean;
  profileReady: boolean;
  /** 未生效原因（平台不支持、profile 创建失败等），由界面如实显示 */
  error: string | null;
}

/** sandbox:systemGet / systemSet 响应 */
export interface SystemSandboxView {
  config: SystemSandboxConfig;
  status: SystemSandboxStatus;
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

// ── 三层架构演进 P0：配额事件契约（租约只在 Rust 内核，不跨进程查询） ──

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

/**
 * Token 预算（缺陷 D-14）：渲染层按用量台账判档的阈值。
 * 0 或负数表示该档不设预算（不阻断、不预警）。
 */
export interface BudgetSettings {
  /** 单日（UTC 日）Token 预算 */
  dailyTokens: number;
  /** 当月（UTC 月）Token 预算 */
  monthlyTokens: number;
  /**
   * 单个任务的 Token 预算（缺陷 D-14.1）。0 表示不限。
   * 用满后执行面停在下一轮模型调用之前，任务转入「预算暂停」；
   * 调高后需要在任务上点「继续」唤回，不会自动唤回。
   */
  perTaskTokens: number;
}

/**
 * 出厂默认预算：日档对齐 Rust `DEFAULT_DAILY_QUOTA_TOKENS`（免费层 20 万/日，ADJ-R5-3），
 * 月档默认不设——没有权威依据的月度数字不如不填，由用户自己按用量设定。
 * 单任务档同样默认不设：拦停是有代价的动作，出厂就拦等于替用户做主。
 */
export const DEFAULT_BUDGET: BudgetSettings = {
  dailyTokens: 200_000,
  monthlyTokens: 0,
  perTaskTokens: 0,
};

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

/**
 * 一条放行记录：配置 ≠ 允许执行，只有放行过的命令才会在真实任务里被本机拉起。
 * 撤权只删这条记录，`hooks` 配置原样保留。
 */
export interface HookGrant {
  command: string;
  event: HookEventName;
  grantedAtMs: number;
  runCount: number;
  lastRunAtMs: number | null;
}

/** `hooks:status`：执行面事实（配了几条、哪些事件真有触发点、哪些还没放行） */
export interface HooksStatus {
  configured: number;
  mountedEvents: HookEventName[];
  firedEvents: HookEventName[];
  unfiredEvents: HookEventName[];
  /** 用户配了、但当前执行链路没有触发点的事件（页面必须逐条说明，不能让它看起来配了就会跑） */
  mountedUnfired: HookEventName[];
  ungranted: string[];
  /** 系统级隔离已生效：Hook 命令会以容器令牌启动，且只接受「程序 + 参数」的单条命令（无本机执行通道时为 false） */
  sandboxed?: boolean;
  grants: HookGrant[];
  /** 浏览器/引擎直连通道没有本机执行网关时，如实写明原因（桌面通道不带此键） */
  gateError?: string;
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
  /** 自动记忆是否先进入待审队列（确认后才参与召回）；手动录入不受影响 */
  memoryReview: boolean;
  /** 是否在任务收尾额外跑一次模型事实抽取（默认关闭，会多一次模型调用） */
  memoryModelExtract: boolean;
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
  memoryReview?: boolean;
  memoryModelExtract?: boolean;
  files?: Partial<Record<AwarenessFileName, string>>;
}

/** 长期记忆归属：全局（所有项目可见）或单个项目 */
export type MemoryScope = 'global' | 'workspace';
/** 长期记忆类型：任务摘要 / 事实 / 偏好 / 决策 */
export type MemoryKind = 'task' | 'fact' | 'preference' | 'decision';
/**
 * 记忆状态：active 生效（参与召回）/ pending 待审（不参与召回）/ archived 已归档（驳回或退休）
 * 归档只改状态不删正文，证据与来源始终保留。
 */
export type MemoryStatus = 'active' | 'pending' | 'archived';

export interface MemoryEntry {
  /** 记忆库主键（mem-{uuid}） */
  id: string;
  content: string;
  source: 'auto' | 'manual' | 'reflection';
  /** 首次写入的 ISO 时间 */
  createdAt: string;
  /** 归属档位：global 或当前项目 */
  scope: MemoryScope;
  /** 项目维度的工作区 id；全局记忆为空串 */
  workspaceId: string;
  kind: MemoryKind;
  /** 产生该记忆的任务（手动录入或旧条目导入时为 null） */
  taskId: string | null;
  /** 证据任务标题（任务已清理时为 null） */
  taskTitle: string | null;
  /** 最近一次写入/合并的 ISO 时间 */
  updatedAt: string;
  /** 最近一次被检索注入的 ISO 时间 */
  lastUsedAt: string | null;
  /** 被检索注入的次数 */
  useCount: number;
  /** 相同内容出现的次数（重复任务合并时累加） */
  occurrences: number;
  /** 生效 / 待审 / 已归档（旧数据一律按 active 展示） */
  status?: MemoryStatus;
  /** 最近一次复核（确认 / 驳回）的 ISO 时间 */
  reviewedAt?: string | null;
  /** 被哪条新事实取代（软退休时写入） */
  supersededBy?: string | null;
  /** 结构化事实：主体 / 属性 / 取值；普通摘要三者为空串 */
  subject?: string;
  predicate?: string;
  object?: string;
  /** 0–1，规则抽取低于人工录入；旧数据视为 1 */
  confidence?: number;
}

/** `awareness:memoryReview` 的动作：确认 / 驳回 / 退休 / 恢复 */
export type MemoryReviewAction = 'confirm' | 'reject' | 'archive' | 'restore';

/** 实体页：按事实主体聚合的活跃条目（由 MemoryEntry.subject 派生，不单独建表） */
export interface MemoryEntityPage {
  subject: string;
  count: number;
  kinds: MemoryKind[];
  /** 该主体下最近一条记忆的 ISO 时间 */
  latest: string;
  entries: MemoryEntry[];
}

/** `awareness:memoryExport` 返回体：导出文件落在运行目录 exports/ 下 */
export interface MemoryExportResult {
  path: string;
  count: number;
}

/** `awareness:memoryImport` 返回体：逐条去重，导入前已自动备份 */
export interface MemoryImportResult {
  inserted: number;
  merged: number;
  skipped: number;
  total: number;
  /** 导入前的自动备份文件路径（导入失败前的数据保护） */
  backupPath: string;
}

/** `awareness:memoryCompact` 返回体 */
export interface MemoryCompactResult {
  archived: number;
  keepPerScope: number;
}

/** `awareness:memoryDocSync` 返回体：MEMORY.md 人工对账（只增不删，可重复执行） */
export interface MemoryDocSyncResult {
  /** MEMORY.md 里解析出的结构化行数 */
  fileCount: number;
  /** 本次新导入库的条数 */
  imported: number;
  /** 文件里有、库里已存在（按内容哈希命中）的条数 */
  existing: number;
  /** 格式不可用或写入失败的条数 */
  skipped: number;
}

/** `awareness:memoryExportPackage` 返回体：迁移包落在运行目录 exports/ 下 */
export interface MemoryPackageExportResult {
  path: string;
  count: number;
}

/** `awareness:memoryImportPackage` 返回体：迁移包元信息 + 常规导入统计 */
export interface MemoryPackageImportResult extends MemoryImportResult {
  /** 迁移包 manifest 的版本号 */
  packageVersion: number;
  /** 迁移包创建时间（ISO；缺失时为 null） */
  exportedAt: string | null;
}

export interface ArchiveRecord {
  taskId: string;
  title: string;
  workspaceId: string;
  archivedAt: string;
}

export type ComputerUsePolicy = 'ask_every_time' | 'auto' | 'disabled';

/** 执行器能力（当前只有 Windows 同时具备截屏与输入） */
export interface ComputerUseFeatures {
  screenshot: boolean;
  input: boolean;
}

/**
 * `computerUse:capabilities` 返回体（Rust `computer_use_capabilities` 契约镜像）。
 * `reason` 为空才代表执行器真的可用；浏览器/Mock 通道如实返回 supported=false。
 */
export interface ComputerUseCapabilities {
  supported: boolean;
  platform: string;
  features: ComputerUseFeatures;
  policy: ComputerUsePolicy;
  /** 档位是否已落库（false 表示当前是默认档） */
  persisted: boolean;
  /** 本机回环网关是否就绪（引擎侧工具依赖它） */
  gate: boolean;
  /** 不可用原因；可用时为空字符串 */
  reason: string;
  /** 已保存档位损坏时的原始错误（fail-closed 为 disabled） */
  policyError?: string | null;
}

/** `computerUse:capture` 自检截图返回体；图片只在内存中回传，不落盘 */
export interface ComputerUseCapture {
  ok: boolean;
  mime: string;
  imageBase64: string;
  width: number;
  height: number;
  originalWidth: number;
  originalHeight: number;
  /** 虚拟桌面左上角在系统坐标里的位置（多显示器时可能为负） */
  originX: number;
  originY: number;
  /** 是否因超出最长边上限而等比降采样 */
  scaled: boolean;
}

// ── 本机画面（本机实时画面） ─────────────────────────────────

/** 跨主机能力边界：当前版本只做本机画面，远程转发明确不支持 */
export interface RemoteCrossHostCapability {
  supported: boolean;
  reason: string;
}

/** `remote:capabilities` 返回体（Rust `remote_desktop_capabilities` 契约镜像） */
export interface RemoteDesktopCapabilities {
  supported: boolean;
  platform: string;
  /** 本机实时画面是否可用（与 supported 同口径） */
  localView: boolean;
  frameIntervalMs: number;
  sessionMaxMs: number;
  /** 不可用原因；可用时为空字符串 */
  reason: string;
  crossHost: RemoteCrossHostCapability;
}

/** 会话状态；active=false 时 remainingMs 恒为 0 */
export interface RemoteDesktopSession {
  active: boolean;
  startedAtMs: number;
  expiresAtMs: number;
  remainingMs: number;
  frames: number;
  frameIntervalMs: number;
  sessionMaxMs: number;
}

export interface RemoteDesktopStartResp {
  ok: boolean;
  /** true 表示复用已开启的会话（未重复弹确认框） */
  reused: boolean;
  session: RemoteDesktopSession;
}

export interface RemoteDesktopStopResp {
  ok: boolean;
  active: boolean;
  frames: number;
  durationMs: number;
  session: RemoteDesktopSession;
}

/** `remote:frame` 返回体：节流时不带图片；图片只在内存中回传，不落盘 */
export interface RemoteDesktopFrame {
  ok: boolean;
  throttled: boolean;
  waitMs?: number;
  mime?: string;
  imageBase64?: string;
  width?: number;
  height?: number;
  originalWidth?: number;
  originalHeight?: number;
  originX?: number;
  originY?: number;
  scaled?: boolean;
  capturedAtMs?: number;
  session: RemoteDesktopSession;
}

export type BrowserUsePolicy = 'ask_every_time' | 'auto' | 'disabled';

/** 逐工具审批档位：off 不审批；sensitive 副作用工具；all 所有工具 */
export type ToolApprovalMode = 'off' | 'sensitive' | 'all';

/** 受控浏览器的 CDP 能力（Rust 侧按平台给出） */
export interface BrowserUseFeatures {
  snapshot: boolean;
  click: boolean;
  type: boolean;
  scroll: boolean;
  screenshot: boolean;
  navigation: boolean;
}

/** `browserUse:capabilities` 返回体（Rust `browser_use_capabilities` 契约镜像） */
export interface BrowserUseCapabilities {
  supported: boolean;
  platform: string;
  /** 实际使用的浏览器名称/路径；未安装时为空 */
  browser: string | null;
  executable: string | null;
  features: BrowserUseFeatures;
  policy: BrowserUsePolicy;
  persisted: boolean;
  /** 引擎消费的本机回环网关是否就绪 */
  gate: boolean;
  /** 受控浏览器进程是否在运行 */
  running: boolean;
  /** DevTools 端口；未运行时为空 */
  port: number | null;
  /** 独立配置目录（不碰用户日常浏览器） */
  profileDir: string;
  reason: string;
  policyError?: string | null;
}

/** `browserUse:start` / `browserUse:stop` 返回体 */
export interface BrowserUseSession {
  ok: boolean;
  launched?: boolean;
  stopped?: boolean;
  pid?: number;
  port?: number;
  browser?: string;
  profileDir?: string;
}

// ── 终端 AI+ 与 ACP 委派 ─────────────────────────────────────

/** ACP 委派的审批策略：deny=一律拒绝外部 Agent 的权限请求；allow=自动放行 */
export type AcpPermission = 'deny' | 'allow';

/** `acp:getConfig` / 运行时 `runtime.acp` 的配置镜像 */
export interface AcpConfigView {
  enabled: boolean;
  command: string;
  permission: AcpPermission;
  timeoutSeconds: number;
  persisted: boolean;
  auditPath?: string;
  /** 存量配置不可用时的可读原因（此时 enabled 必为 false） */
  error?: string;
}

/** `acp:getConfig` 额外给出可选策略清单 */
export interface AcpConfigState extends AcpConfigView {
  permissions?: AcpPermission[];
}

/** `acp:status`：ACP 服务端启动命令（外部 IDE/终端接入口） */
export interface AcpServerStatus {
  enabled: boolean;
  /** Python + acp_server.py 的启动命令；缺 Python 或脚本时为空 */
  command: string | null;
  engineUrl: string | null;
  engineRunning: boolean;
  python: string | null;
  reason: string;
  protocolVersion: number;
  note: string;
}

/** `terminal:capabilities`：开关、平台、shell 与 ACP 配置一次说清 */
export interface TerminalCapabilities {
  enabled: boolean;
  persisted: boolean;
  policyError?: string | null;
  platform: string;
  /** 实际 shell 名称；浏览器通道为空串 */
  shell: string;
  /** 管道模式（不支持全屏交互程序） */
  pipe: boolean;
  maxSessions: number;
  inputLimit: number;
  reason: string;
  engineRunning: boolean;
  engineUrl: string | null;
  acp: AcpConfigView;
}

/** 打开终端会话的返回体（引擎 + Rust 同口径） */
export interface TerminalSessionInfo {
  ok: boolean;
  sessionId: string;
  shell: string;
  cwd: string;
  /** 可从此偏移量开始读；更早的输出不在缓冲里 */
  offset: number;
  pipe: boolean;
  note: string;
}

/** 增量读取返回：chunk 为 offset 之后的新输出 */
export interface TerminalReadResult {
  ok: boolean;
  chunk: string;
  offset: number;
  running: boolean;
  exitCode: number | null;
  /** 传进来的 offset 已被环形缓冲覆盖 */
  truncated: boolean;
  note: string;
  cwd: string;
}

/** AI 命令建议：最多 5 条，单行、可复制；空 commands 表示模型没有把握 */
export interface TerminalSuggestionCommand {
  command: string;
  explanation: string;
}

export interface TerminalSuggestion {
  summary: string;
  commands: TerminalSuggestionCommand[];
  risk: 'low' | 'medium' | 'high' | 'unknown';
  /** 模型输出不符合约定 JSON 时的说明（此时 commands 必为空） */
  parseError: string;
}

/** `terminal:ai` 返回体：只给建议，永不执行 */
export interface TerminalAiResult {
  ok: boolean;
  suggestion: TerminalSuggestion;
  model: string;
  piiMode: string;
  redactions: string[];
  executed: false;
}

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
  /**
   * 「在 IM 频道中展示工具调用执行过程」**不在这里**：回贴正文由引擎拼装，
   * 该开关的事实来源是引擎 `im_preferences.json`（见 ImPreferences / im:preferencesGet）。
   */
  autoStart: boolean;
  /** 关闭主窗口后进程继续驻留托盘（托盘不可用时不生效，见 system:info.backgroundResident） */
  runInBackground: boolean;
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
  browserUsePolicy: BrowserUsePolicy;
  /** 出站脱敏档位（引擎模型调用前处理文本；默认 off） */
  piiOutboundMode: PiiOutboundMode;
  /** 逐工具审批档位（副作用工具执行前系统确认；默认 off） */
  toolApprovalMode: ToolApprovalMode;
  /**
   * 文件访问默认档位（真实目录里的创建/更新/删除；缺会话自己的选择时用这一档，默认 ask_every_time）。
   * 与上面的逐工具审批是两轴：那条管「要不要问」，这条管「能写到哪、能不能删」。
   */
  filePermissionDefault: FilePermissionMode;
  /** 单轮工具循环上限（1~32；达到上限时请求模型总结进展并结束本轮） */
  agentMaxIters: number;
  /** 自动压缩长上下文（默认开启；摘要会额外调用一次模型） */
  agentAutoCompress: boolean;
  /** 连接器工具按需载入（默认开启；需要时先 search_tools 搜索启用） */
  agentLazyMcpTools: boolean;
  secureEnv: boolean;
  workbenchModes: string[];
  workbenchDefaultMode: string;
  /** 幻灯片模板 id（内置主题 id 或本机导入模板 id；缺省用内置商务蓝） */
  pptxTemplateId: string;
  /**
   * Token 预算（日/月/单任务三档；缺键用 `DEFAULT_BUDGET`）。
   * 只由渲染层闸门消费：Rust 的 `quota-ledger.json` 另有自己的一本账，两侧不互通（见 D-14 已知边界）。
   */
  budget?: BudgetSettings;
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
 * 内置工具清单（`agent:builtinTools`）。
 * `source` 说明这份清单怎么来的：engine=运行中引擎的注册表实测，
 * declared=与引擎同源维护的声明值（引擎没跑时），mock=浏览器演示通道。
 */
export interface BuiltinTool {
  id: string;
  name: string;
  desc: string;
  source: 'builtin' | 'mcp' | string;
}

export interface BuiltinToolList {
  source: 'engine' | 'declared' | 'mock' | string;
  count: number;
  tools: BuiltinTool[];
  note?: string;
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
 * 品牌外观（`branding:get` / `branding:customize`）。
 * 单次自定义：名称与 PNG 图标可在同一次提交里一起改；成功即锁定修改机会。
 * `applied` 是本次真实作用到的位置（桌面端由 Rust 回填），
 * 已安装快捷方式与应用文件名由安装包写入，运行期改不了 —— 界面必须如实说明。
 */
export interface BrandingInfo {
  /** 安装包默认名称（任我行） */
  defaultName: string;
  /** 署名/开发者（北京通天云计算有限公司） */
  attribution: string;
  /** 当前显示名称（默认或自定义） */
  name: string;
  /** 自定义图标 data URL；无自定义图标为 null */
  iconDataUrl: string | null;
  /** 修改机会是否已使用 */
  customized: boolean;
  /** 修改机会使用时间（epoch ms） */
  customizedAt: number | null;
  /** desktop=Rust 通道；mock=浏览器 Mock；browser=引擎直连等无桌面能力通道 */
  platform: 'desktop' | 'mock' | 'browser';
  /** customize 返回值：本次实际生效的位置 */
  applied?: { windowTitle: boolean; windowIcon: boolean; trayIcon: boolean };
  /** customize 返回值：部分目标失败的真实原因（不静默吞掉） */
  warnings?: string[];
}

export interface BrandingCustomizeReq {
  name?: string | null;
  /** 原始 base64（可带 data URI 前缀） */
  iconBase64?: string | null;
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
  /** 本轮所属工作区（缺省 default，与 tasks.workspaceId 同口径） */
  workspaceId: string;
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
  /** 会话所属工作区（取首轮提问归属；按项目过滤时使用） */
  workspaceId: string;
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

// ── IM 频道（引擎 /api/im/*；长连接只在引擎进程里） ────────────────

/**
 * 凭据字段元数据由引擎下发，界面按清单渲染。
 * `secret` 恒为 true，且这里从不带值——只有 `present` 说明引擎侧已登记，
 * 所以界面不能回填明文，也不显示"当前值"。
 */
export interface ImCredentialField {
  key: string;
  label: string;
  placeholder: string;
  required: boolean;
  helpUrl: string;
  note: string;
  secret: boolean;
  present: boolean;
}

/** 引擎 ChannelRuntime 的真实状态机取值 */
export type ImChannelState =
  | 'not_configured'
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'failed'
  | 'stopped';

/** 引擎 `ChannelRuntime._set_state` 记录的一次失败（码 + 详情 + 时刻） */
export interface ImChannelLastError {
  code: string;
  detail: string;
  atMs: number;
}

/** 单个通道的状态视图（`im:channels` 数组元素，与引擎 `ChannelRuntime.view()` 同构） */
export interface ImChannelView {
  kind: string;
  label: string;
  /** false 时界面为禁用态，`unsupportedReason` 给出原因 */
  supported: boolean;
  unsupportedReason: string;
  configured: boolean;
  state: ImChannelState;
  running: boolean;
  /** 仅 state=connected 时有值，与 state 同源 */
  connectedAtMs: number | null;
  reconnects: number;
  lastError: ImChannelLastError | null;
  account: Record<string, string>;
  setupUrl: string;
  setupNote: string;
  /** 'qr' = 扫码授权（个人微信）；空串 = 提交凭据 */
  loginMode: string;
  fields: ImCredentialField[];
  sessions: number;
}

/** 扫码登录第一步的返回：二维码内容与票据（凭据不在这里） */
export interface ImLoginStart {
  ticket: string;
  qrContent: string;
  expiresInSeconds: number;
}

/** 扫码登录一次状态查询；confirmed 只表示引擎已落盘并起连接，不回传令牌 */
export interface ImLoginPoll {
  status: string;
  message: string;
  needVerifyCode?: boolean;
  configured?: boolean;
}

export interface ImSessionMessage {
  direction: 'in' | 'out';
  text: string;
  atMs: number;
}

/** IM 会话台账行：从 IM 发起的任务在引擎侧的落盘记录 */
export interface ImSession {
  id: string;
  /** 通道 kind */
  source: string;
  title: string;
  conversationId: string;
  createdAtMs: number;
  updatedAtMs: number;
  messages: ImSessionMessage[];
}

export interface ImChannelsSnapshot {
  channels: ImChannelView[];
  sessions: ImSession[];
}

/**
 * IM 频道偏好：事实来源在引擎（回贴正文由引擎拼装），设置页读写的就是这个值。
 * showToolProcess 开启时，IM 回贴正文后附本次真实调用过的工具步骤。
 */
export interface ImPreferences {
  showToolProcess: boolean;
}

// ── IPC Channel Names ────────────────────────────────────────


export const IPC_CHANNELS = {
  // Chat / Task
  CHAT_SUBMIT: 'chat:submit',
  CHAT_REVIEW: 'chat:review',
  CHAT_HISTORY: 'chat:history',
  /** 会话列表（正文聚合，重启后重建侧边栏任务） */
  CHAT_CONVERSATIONS: 'chat:conversations',
  /** 单个会话的正文（逐轮问答） */
  CHAT_MESSAGES: 'chat:messages',
  /** 一轮回复的点赞/点踩（按 (会话, 轮次) 归属，null 为取消） */
  CHAT_FEEDBACK: 'chat:feedback',
  /** 改写某一轮提问（本轮回复与之后轮次作废，随后重新执行） */
  CHAT_EDIT_TURN: 'chat:editTurn',

  // Model
  MODEL_LIST: 'model:list',
  MODEL_CREATE: 'model:create',
  MODEL_UPDATE: 'model:update',
  MODEL_DELETE: 'model:delete',
  MODEL_TEST: 'model:test',
  MODEL_DETECT_LOCAL: 'model:detectLocal',
  MODEL_SET_DEFAULT: 'model:setDefault',
  MODEL_USAGE: 'model:usage',

  // File
  FILE_BROWSE: 'file:browse',
  FILE_PREVIEW: 'file:preview',
  FILE_READ: 'file:read',

  // Workspace
  WORKSPACE_LIST: 'workspace:list',
  WORKSPACE_CREATE: 'workspace:create',
  /** 后端抽象与数据根事实（本机可用；容器/SSH 明确未提供） */
  WORKSPACE_CAPABILITIES: 'workspace:capabilities',

  // Engine
  /** 按 workspace 解析引擎 HTTP 端点（第二个实例可能分配在 18001） */
  ENGINE_ENDPOINT: 'engine:endpoint',
  /**
   * 启动指定 workspace 的引擎实例（幂等：已在跑直接复用）。
   * 桌面端给「必须由引擎承载、但不等用户发消息」的页面用，例如 IM 频道；
   * 浏览器直连模式由外部进程托管，渲染层不得调用。
   */
  ENGINE_START: 'engine:start',

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

  // 插件 SDK（安装前预检 / 起始模板；浏览器通道如实拒绝）
  PLUGIN_VALIDATE: 'plugin:validate',
  PLUGIN_TEMPLATE: 'plugin:template',

  // 在线技能 / 套件市场（可配置索引源；安装只传条目标识）
  MARKET_CONFIG_GET: 'market:configGet',
  MARKET_CONFIG_SET: 'market:configSet',
  MARKET_SKILL_LIST: 'market:skillList',
  MARKET_SKILL_INSTALL: 'market:skillInstall',
  MARKET_SUITE_LIST: 'market:suiteList',
  MARKET_SUITE_INSTALL: 'market:suiteInstall',
  MARKET_TRUST_GET: 'market:trustGet',
  MARKET_TRUST_ADD: 'market:trustAdd',
  MARKET_TRUST_REMOVE: 'market:trustRemove',
  MARKET_UPDATE_CHECK: 'market:updateCheck',
  MARKET_INSTALLED_LIST: 'market:installedList',
  /** 升级单条 / 一键升级：校验链与安装同一条，落盘换成「旧版本进回收目录 + 新版本原子落位」 */
  MARKET_SKILL_UPGRADE: 'market:skillUpgrade',
  MARKET_SUITE_UPGRADE: 'market:suiteUpgrade',
  MARKET_UPGRADE_ALL: 'market:upgradeAll',

  // MCP
  MCP_LIST_SERVERS: 'mcp:listServers',
  MCP_ADD_SERVER: 'mcp:addServer',
  MCP_REMOVE_SERVER: 'mcp:removeServer',
  MCP_CONNECT: 'mcp:connect',
  MCP_DISCONNECT: 'mcp:disconnect',
  MCP_LIST_TOOLS: 'mcp:listTools',
  MCP_OAUTH_AUTHORIZE: 'mcp:oauthAuthorize',
  MCP_OAUTH_REVOKE: 'mcp:oauthRevoke',

  // Sandbox
  SANDBOX_GET_POLICY: 'sandbox:getPolicy',
  SANDBOX_UPDATE_POLICY: 'sandbox:updatePolicy',
  SANDBOX_ENV_USAGE: 'sandbox:envUsage',
  SANDBOX_ENV_CLEAR: 'sandbox:envClear',
  SANDBOX_SYSTEM_GET: 'sandbox:systemGet',
  SANDBOX_SYSTEM_SET: 'sandbox:systemSet',

  // Audit
  AUDIT_QUERY: 'audit:query',
  AUDIT_EXPORT: 'audit:export',

  // Security（数据分级 + 出域控制，PRD §4.7）
  SECURITY_SCAN_DATA: 'security:scanData',
  SECURITY_GET_DOMAIN_CONTROL: 'security:getDomainControl',
  SECURITY_SET_DOMAIN_CONTROL: 'security:setDomainControl',

  // Schedule
  SCHEDULE_LIST: 'schedule:list',
  /** 一句话解析（纯本地规则，不调用模型） */
  SCHEDULE_PARSE: 'schedule:parse',
  SCHEDULE_CREATE: 'schedule:create',
  SCHEDULE_UPDATE: 'schedule:update',
  SCHEDULE_DELETE: 'schedule:delete',
  SCHEDULE_TOGGLE: 'schedule:toggle',
  SCHEDULE_RUNS: 'schedule:runs',
  SCHEDULE_RUN_NOW: 'schedule:runNow',
  KEEP_AWAKE_STATUS: 'keepAwake:status',
  /** 开机自启动实际状态（Windows 读注册表启动项，非「设置里的意愿」） */
  SYSTEM_AUTOSTART_STATUS: 'system:autostart',
  /** 任务栏未读角标能力（Windows=overlay，macOS=count，其余不支持） */
  BADGE_SUPPORT: 'badge:support',
  /** 设置/清除任务栏未读角标；count=0 清除 */
  BADGE_SET: 'badge:set',
  /** 品牌外观：读取当前显示名/图标与是否已用掉修改机会 */
  BRANDING_GET: 'branding:get',
  /** 品牌外观：单次自定义名称与 PNG 图标（桌面端才写系统窗口/托盘图标） */
  BRANDING_CUSTOMIZE: 'branding:customize',
  /** 桌面端打开独立任务窗口（popWindow 实验特性的真实执行面） */
  TASK_OPEN_WINDOW: 'task:openWindow',
  PROXY_STATUS: 'proxy:status',
  /** 内置工具清单（引擎在跑时读实测注册表，否则读与引擎同源的声明清单） */
  AGENT_BUILTIN_TOOLS: 'agent:builtinTools',
  SYSTEM_PERMISSION_STATUS: 'system:permission:status',
  SYSTEM_PERMISSION_OPEN: 'system:permission:open',

  // Compliance
  COMPLIANCE_STATUS: 'compliance:status',
  COMPLIANCE_UPDATE: 'compliance:update',
  COMPLIANCE_CHECK: 'compliance:check',

  // Usage
  USAGE_STATS: 'usage:stats',
  USAGE_REPORT: 'usage:report',
  USAGE_UPLOAD: 'usage:upload',

  // System
  SYSTEM_INFO: 'system:info',

  // ── 千问对标整改新增（spec §6.3） ──
  SETTINGS_GET: 'settings:get',
  SETTINGS_SET: 'settings:set',
  HOOKS_GET: 'hooks:get',
  HOOKS_SET: 'hooks:set',
  HOOKS_TEST: 'hooks:test',
  HOOKS_GRANT: 'hooks:grant',
  HOOKS_REVOKE: 'hooks:revoke',
  HOOKS_STATUS: 'hooks:status',
  AWARENESS_GET: 'awareness:get',
  AWARENESS_UPDATE: 'awareness:update',
  AWARENESS_MEMORIES: 'awareness:memories',
  AWARENESS_MEMORY_ADD: 'awareness:memoryAdd',
  AWARENESS_MEMORY_DELETE: 'awareness:memoryDelete',
  /** 记忆复核：确认 / 驳回 / 归档 / 恢复（只改状态，不删正文） */
  AWARENESS_MEMORY_REVIEW: 'awareness:memoryReview',
  /** 实体页：按事实主体聚合当前项目的活跃条目 */
  AWARENESS_MEMORY_ENTITIES: 'awareness:memoryEntities',
  /** 导出记忆为 JSON（运行目录 exports/） */
  AWARENESS_MEMORY_EXPORT: 'awareness:memoryExport',
  /** 从 JSON 导入记忆（导入前自动备份） */
  AWARENESS_MEMORY_IMPORT: 'awareness:memoryImport',
  /** 维护：归档超过保留条数的旧自动任务摘要 */
  AWARENESS_MEMORY_COMPACT: 'awareness:memoryCompact',
  /** 与 MEMORY.md 对账：导入文件里有、库里没有的条目（文件不改） */
  AWARENESS_MEMORY_DOC_SYNC: 'awareness:memoryDocSync',
  /** 导出 .hmpkg 迁移包（zip：manifest + memories.json） */
  AWARENESS_MEMORY_EXPORT_PACKAGE: 'awareness:memoryExportPackage',
  /** 导入 .hmpkg 迁移包（base64 透传；导入前自动备份） */
  AWARENESS_MEMORY_IMPORT_PACKAGE: 'awareness:memoryImportPackage',
  TASK_ARCHIVE: 'task:archive',
  TASK_UNARCHIVE: 'task:unarchive',
  TASK_ARCHIVE_DELETE: 'task:archiveDelete',
  TASK_ARCHIVE_LIST: 'task:archiveList',
  TASK_ARCHIVE_CLEAR: 'task:archiveClear',
  COMPUTER_USE_GET_POLICY: 'computerUse:getPolicy',
  COMPUTER_USE_SET_POLICY: 'computerUse:setPolicy',
  /** 执行器真实能力视图（平台/档位/网关/原因） */
  COMPUTER_USE_CAPABILITIES: 'computerUse:capabilities',
  /** 用户主动触发的截屏自检（按当前档位确认，图片只回传界面） */
  COMPUTER_USE_CAPTURE: 'computerUse:capture',

  // ── 本机画面（本机实时画面；显式开启、10 分钟到期、只看不控） ──
  REMOTE_DESKTOP_CAPABILITIES: 'remote:capabilities',
  REMOTE_DESKTOP_STATUS: 'remote:status',
  REMOTE_DESKTOP_START: 'remote:start',
  REMOTE_DESKTOP_FRAME: 'remote:frame',
  REMOTE_DESKTOP_STOP: 'remote:stop',

  // ── 浏览器 AI+（受控浏览器会话；操作由引擎经本机网关 + CDP 执行） ──
  BROWSER_USE_GET_POLICY: 'browserUse:getPolicy',
  BROWSER_USE_SET_POLICY: 'browserUse:setPolicy',
  BROWSER_USE_CAPABILITIES: 'browserUse:capabilities',
  /** 用户主动启动受控浏览器（独立配置目录） */
  BROWSER_USE_START: 'browserUse:start',
  /** 用户主动结束本应用启动的那个受控浏览器进程 */
  BROWSER_USE_STOP: 'browserUse:stop',

  // ── 终端 AI+（显式开关 + 管道 shell 会话；AI 只给建议不执行） ──
  TERMINAL_GET_POLICY: 'terminal:getPolicy',
  /** 显式开启/关闭终端（开启是高风险管理动作，写安全审计） */
  TERMINAL_SET_POLICY: 'terminal:setPolicy',
  TERMINAL_CAPABILITIES: 'terminal:capabilities',
  TERMINAL_OPEN: 'terminal:open',
  TERMINAL_INPUT: 'terminal:input',
  TERMINAL_READ: 'terminal:read',
  TERMINAL_CLOSE: 'terminal:close',
  /** 根据最近输出给命令建议；只返回建议，永不执行 */
  TERMINAL_AI: 'terminal:ai',

  // ── ACP（外部编码 Agent 双向接入） ──
  ACP_GET_CONFIG: 'acp:getConfig',
  ACP_SET_CONFIG: 'acp:setConfig',
  /** ACP 服务端启动命令（把本应用 Agent 接给外部 IDE/终端） */
  ACP_STATUS: 'acp:status',

  // ── 本地知识库（RAG；检索自测直连引擎 /api/knowledge/search） ──
  KNOWLEDGE_LIST: 'knowledge:list',
  KNOWLEDGE_CREATE: 'knowledge:create',
  KNOWLEDGE_UPDATE: 'knowledge:update',
  KNOWLEDGE_DELETE: 'knowledge:delete',
  KNOWLEDGE_ADD_DOCS: 'knowledge:addDocs',
  KNOWLEDGE_REMOVE_DOC: 'knowledge:removeDoc',
  KNOWLEDGE_OPEN_DIR: 'knowledge:openDir',

  // ── 幻灯片模板库（内置主题 + 本机 .pptx 导入） ──
  PPTX_TEMPLATES_LIST: 'pptx:templates',
  PPTX_TEMPLATE_IMPORT: 'pptx:templateImport',
  PPTX_TEMPLATE_DELETE: 'pptx:templateDelete',

  // ── 专家团队（注册表 + 每专家模型绑定；模型密钥只在 Rust 侧解析） ──
  EXPERTS_LIST: 'experts:list',
  EXPERTS_SAVE: 'experts:save',
  EXPERTS_DELETE: 'experts:delete',

  // ── 三层架构演进 P0（只保留界面真实消费的取消与重放） ──
  CHAT_CANCEL: 'chat:cancel',
  EVENT_REPLAY: 'event:replay',

  // ── 账号与登录态（网页验证登录；默认连接综合服务端，TT_AUTH_MOCK 为离线开关） ──
  AUTH_STATUS: 'auth:status',
  AUTH_LOGIN: 'auth:login',
  AUTH_LOGOUT: 'auth:logout',
  AUTH_SERVER_PROBE: 'auth:serverProbe',

  // ── 项目协作（跨用户 / 跨机器；人触发任务交接，不做 Agent 自动接力） ──
  COLLAB_STATUS: 'collab:status',
  COLLAB_LIST_PROJECTS: 'collab:listProjects',
  COLLAB_CREATE_PROJECT: 'collab:createProject',
  COLLAB_PROJECT_DETAIL: 'collab:projectDetail',
  COLLAB_CREATE_INVITE: 'collab:createInvite',
  COLLAB_LIST_INVITES: 'collab:listInvites',
  COLLAB_ACCEPT_INVITE: 'collab:acceptInvite',
  COLLAB_UPDATE_MEMBER_ROLE: 'collab:updateMemberRole',
  COLLAB_REMOVE_MEMBER: 'collab:removeMember',
  COLLAB_TRANSFER_OWNER: 'collab:transferOwner',
  COLLAB_LEAVE_PROJECT: 'collab:leaveProject',
  COLLAB_CREATE_TASK: 'collab:createTask',
  COLLAB_UPDATE_TASK: 'collab:updateTask',
  COLLAB_ADD_COMMENT: 'collab:addComment',
  COLLAB_REPORT_RESULT: 'collab:reportResult',
  COLLAB_WATCH_START: 'collab:watchStart',
  COLLAB_WATCH_STOP: 'collab:watchStop',
  COLLAB_WATCH_STATUS: 'collab:watchStatus',

  // ── 工单层（缺陷 D-47 · 无授权工作单元与待人工状态机） ──
  // 桌面端只经 Rust 桥接访问服务端；每条通道对应 collab/tickets.rs 的一个命令。
  COLLAB_TICKET_MY: 'collab:ticketMyTickets',
  COLLAB_TICKET_LIST: 'collab:ticketList',
  COLLAB_TICKET_DETAIL: 'collab:ticketDetail',
  COLLAB_TICKET_CREATE: 'collab:ticketCreate',
  COLLAB_TICKET_SUBSCRIBE: 'collab:ticketSubscribe',
  COLLAB_TICKET_CLAIM: 'collab:ticketClaim',
  COLLAB_TICKET_END_ASSIGNMENT: 'collab:ticketEndAssignment',
  COLLAB_TICKET_REPORT: 'collab:ticketReport',
  COLLAB_TICKET_HANDBACK: 'collab:ticketHandback',
  COLLAB_TICKET_ANSWER: 'collab:ticketAnswer',
  COLLAB_TICKET_ACCEPT: 'collab:ticketAccept',
  COLLAB_TICKET_AMEND: 'collab:ticketAmend',
  COLLAB_TICKET_STOP: 'collab:ticketStop',
  COLLAB_TICKET_MARK_READ: 'collab:ticketMarkRead',

  // ── Super-agent 主动候选（缺陷 D-46 · 影子段：账本 + 策略 + 巡检，零自动执行） ──
  // 只有读取与授权配置两类通道，没有「采纳/开工」通道：影子模式的产物就是清单本身。
  SUPERAGENT_STATS: 'superagent:stats',
  SUPERAGENT_LIST_SOURCES: 'superagent:listSources',
  SUPERAGENT_REGISTER_SOURCE: 'superagent:registerSource',
  SUPERAGENT_SET_SOURCE_ENABLED: 'superagent:setSourceEnabled',
  SUPERAGENT_LIST_CANDIDATES: 'superagent:listCandidates',
  SUPERAGENT_LIST_DECISIONS: 'superagent:listDecisions',
  SUPERAGENT_LIST_TRIGGERS: 'superagent:listTriggers',
  SUPERAGENT_GET_TRIGGER: 'superagent:getTrigger',
  SUPERAGENT_GET_POLICY: 'superagent:getPolicy',
  SUPERAGENT_SAVE_POLICY: 'superagent:savePolicy',
  SUPERAGENT_POLICY_VERSIONS: 'superagent:policyVersions',
  SUPERAGENT_PUMP_NOW: 'superagent:pumpNow',
  /**
   * 立刻跑一轮模型研判（第二层）。
   * 这是「问模型值不值得开工」，不是「开工」：只写判定行，不创建任务。
   * 与「立即巡检」分开是因为前者会花模型的钱，要人明确点一下才动。
   */
  SUPERAGENT_ASSESS_NOW: 'superagent:assessNow',
  /** 把当前候选清单落成文件（影子模式的产物只有清单，导出就是它的交付） */
  SUPERAGENT_EXPORT_CANDIDATES: 'superagent:exportCandidates',

  // ── IM 频道（代理引擎 /api/im/*；Rust/浏览器直连两通道同源） ──
  IM_CHANNELS: 'im:channels',
  IM_CONFIGURE: 'im:configure',
  IM_LOGIN_START: 'im:loginStart',
  IM_LOGIN_POLL: 'im:loginPoll',
  IM_START: 'im:start',
  IM_STOP: 'im:stop',
  IM_FORGET: 'im:forget',
  /** 主动发一条文本（定时任务结果推送复用同一实现） */
  IM_SEND: 'im:send',
  IM_PREFERENCES_GET: 'im:preferencesGet',
  IM_PREFERENCES_SET: 'im:preferencesSet',
} as const;

export type IPCChannel = typeof IPC_CHANNELS[keyof typeof IPC_CHANNELS];
