// src/shared/error-messages.ts — 错误码文案映射表（流 G4）
//
// 三层架构演进 P0：9 个云端必需错误码 + 既有 Bridge 错误码的 UI 文案。
// 未知错误码走默认分支（防白屏，中性文案不承诺时间）。

import { ErrorCode } from './contracts';

const ERROR_MESSAGES: Record<string, string> = {
  // ── 三层架构演进云端必需码 ──
  [ErrorCode.BUDGET_EXHAUSTED]: '预算已用尽，任务已暂停，恢复后将自动继续',
  [ErrorCode.QUEUE_TIMEOUT]: '排队超时，任务已失败，可稍后重试',
  [ErrorCode.LEASE_LOST]: '执行环境已重置，任务将重新排队',
  [ErrorCode.FILE_REJECTED]: '文件被拒绝（格式不支持或超出大小限制）',
  [ErrorCode.SYNC_CONFLICT]: '数据同步冲突，请在冲突列表中裁决',
  [ErrorCode.PAYMENT_GRACE]: '订阅处于支付宽限期，新任务暂不可用',
  [ErrorCode.DATA_TIER_BLOCKED]: '数据分级限制：敏感数据禁止外发，请使用本地模型',
  [ErrorCode.INJECTION_BLOCKED]: '检测到异常内容注入，已拦截该请求',
  [ErrorCode.QUOTA_EXCEEDED]: '配额已用尽，新任务被阻断',

  // ── 既有 Bridge/任务错误码 ──
  [ErrorCode.TASK_NOT_FOUND]: '任务不存在或已结束',
  [ErrorCode.TASK_ALREADY_RUNNING]: '该任务正在其他端执行，当前仅提供查看',
  [ErrorCode.MODEL_RATE_LIMITED]: '模型繁忙，正在重试',
  [ErrorCode.MODEL_CONNECTION_FAILED]: '模型连接失败，请检查配置',
  [ErrorCode.BRIDGE_ERROR]: '与 Agent 引擎通信失败',
  [ErrorCode.BRIDGE_PYTHON_NOT_FOUND]: '未检测到 Python 环境，请先安装 Python 3.10+',
  [ErrorCode.BRIDGE_START_TIMEOUT]: 'Agent 引擎启动超时',
  [ErrorCode.BRIDGE_SCRIPT_NOT_FOUND]: 'Agent 引擎脚本缺失，安装包资源不完整',
  [ErrorCode.BRIDGE_NO_FREE_PORT]: '本地端口 18000-19000 已占满，请重启应用后重试',
  [ErrorCode.BRIDGE_WORKDIR_FAILED]: '任务工作目录创建失败，请检查工作目录权限',
  [ErrorCode.BRIDGE_AGENT_NOT_FOUND]: '未安装 qwenpaw 引擎，请在设置页改用默认引擎或安装该引擎',
  [ErrorCode.BRIDGE_EXE_PATH]: '无法定位应用运行目录，请重新安装应用',
  [ErrorCode.BRIDGE_SPAWN_FAILED]: 'Agent 引擎进程启动失败，请检查 Python 环境后重试',
  [ErrorCode.BRIDGE_COMM_FAILED]: '与本地 Agent 引擎通信失败，引擎可能已退出，请重试任务',
  [ErrorCode.BRIDGE_CONTROL_FAILED]: '引擎拒绝了该控制指令，请稍后重试',
  [ErrorCode.BRIDGE_CONTROL_PARSE]: '引擎返回了无法解析的控制响应',
  [ErrorCode.BRIDGE_CANCEL_PARSE]: '引擎返回了无法解析的取消响应',
  [ErrorCode.BRIDGE_STARTUP_FAILED]: 'Agent 引擎启动失败',
  [ErrorCode.BRIDGE_COMMUNICATION_FAILED]: '与 Agent 引擎通信失败',
  EMPTY_GOAL: '任务描述为空',
  TASK_CANCELLED: '任务已取消',

  // ── 工作空间（workspace_list / workspace_create 抛出的码不得裸奔） ──
  [ErrorCode.WORKSPACE_INVALID_ID]:
    '工作空间标识非法：仅支持小写字母、数字、下划线与连字符，且不能包含路径分隔符',
  [ErrorCode.WORKSPACE_NOT_FOUND]: '工作空间不存在，请先创建或切换到其他工作空间',
  [ErrorCode.WORKSPACE_CREATION_FAILED]: '工作空间目录创建失败，请检查数据目录写入权限',
  [ErrorCode.WORKSPACE_SUSPENDED]: '该工作空间已挂起，恢复后才能执行任务',
  [ErrorCode.WORKSPACE_DELEGATE_FAILED]: '任务委派到其他工作空间失败，请稍后重试',
  [ErrorCode.WORK_DIR_INVALID]: '所选工作目录不存在或不是目录，任务未提交，请重新选择工作目录',

  // ── 系统集成（开机自启动） ──
  [ErrorCode.AUTOSTART_FAILED]: '开机自启动设置未能写入系统启动项，请检查权限后重试',
  [ErrorCode.AUTOSTART_UNSUPPORTED]: '当前系统不支持在应用内设置开机自启动，请在系统设置中手动配置',

  // ── 技能（Skills 目录事实来源） ──
  [ErrorCode.SKILL_NOT_FOUND]: '未找到该技能文件，请确认路径后重试',
  [ErrorCode.SKILL_INSTALL_FAILED]: '技能安装失败，请检查运行目录写入权限',
  [ErrorCode.SKILL_EXECUTION_FAILED]: '技能执行失败，请查看执行过程明细后重试',
  [ErrorCode.SKILL_INCOMPATIBLE]: '该技能包不符合安装要求（仅支持含 SKILL.md 的技能目录）',
  [ErrorCode.SKILL_UNSUPPORTED]:
    '当前运行通道不支持技能增删，请在桌面版通天办公中管理技能',

  // ── 专家套件（套件目录 manifest.json 事实来源） ──
  [ErrorCode.SUITE_NOT_FOUND]: '未找到该已安装套件，请刷新列表后重试',
  [ErrorCode.SUITE_INCOMPATIBLE]:
    '套件包不符合安装要求：ZIP 需在根目录或一级目录下含 plugin.json，且其中必须有 name 字段',
  [ErrorCode.SUITE_INSTALL_FAILED]: '套件安装失败，请检查运行目录写入权限后重试',

  // ── 安全与治理（PRD §4.7） ──
  [ErrorCode.POLICY_INVALID_MODE]: '出域控制模式非法，仅支持白名单 / 黑名单',
  [ErrorCode.POLICY_INVALID_LEVEL]: '沙箱执行级别非法，仅支持 L0 / L1 / L2',
  [ErrorCode.POLICY_INVALID_RANGE]: '沙箱资源上限越界，请检查 CPU / 内存 / 写盘数值',
  [ErrorCode.POLICY_INVALID_WORKSPACE]: '工作区标识非法，无法保存沙箱策略',
  [ErrorCode.POLICY_CORRUPT]: '策略数据已损坏，请重新保存后再试',
  [ErrorCode.POLICY_READ_FAILED]: '安全策略读取失败',
  [ErrorCode.POLICY_SAVE_FAILED]: '安全策略保存失败',
  [ErrorCode.POLICY_EXTERNAL_API_DISABLED]: '外部 API 已被硬开关禁用，仅允许本地模型处理',
  [ErrorCode.POLICY_DOMAIN_NOT_ALLOWED]: '该域名不在出域白名单内，请求已被拦截',
  [ErrorCode.POLICY_DOMAIN_BLOCKED]: '该域名命中出域黑名单，请求已被拦截',
  [ErrorCode.POLICY_INVALID_URL]: '无法解析目标地址，请检查 URL 配置',
  [ErrorCode.COMPLIANCE_DATA_LEAK]:
    '内容含机密或个人信息，按合规策略已阻止发往外部模型；请改用本地模型，或在安全合规页调整机密本地化设置',
  [ErrorCode.AUDIT_QUERY_FAILED]: '审计日志查询失败',
  [ErrorCode.AUDIT_WRITE_FAILED]: '审计日志写入失败',
  [ErrorCode.AUDIT_EXPORT_FAILED]: '审计日志导出失败',
  [ErrorCode.AUDIT_EXPORT_FORMAT_UNSUPPORTED]: '审计导出格式不支持，仅支持 CSV / JSON',

  // ── 安全工作环境（工作环境文件用量与清理） ──
  [ErrorCode.SANDBOX_ENV_USAGE_FAILED]: '工作环境用量统计失败，可稍后重试',
  [ErrorCode.SANDBOX_ENV_CLEAR_FAILED]: '工作环境文件清理失败，文件可能正被占用，请关闭相关任务后重试',
  [ErrorCode.SANDBOX_ENV_BASE_MISSING]: '数据目录无法访问，请检查工作目录权限后重试',
  [ErrorCode.SANDBOX_ENV_PATH_INVALID]: '工作环境路径无法解析，请重启应用后重试',
  [ErrorCode.SANDBOX_ENV_PATH_OUT_OF_SCOPE]: '清理路径超出工作环境目录范围，已被安全策略拦截',
  [ErrorCode.SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK]: '该条目是符号链接，为避免误删外部数据已跳过',

  // ── 模型用量统计 ──
  [ErrorCode.USAGE_INVALID_PERIOD]: '用量统计周期非法，仅支持 今日 / 本月 / 全部',
  [ErrorCode.USAGE_RECORD_FAILED]: '用量台账写入失败（已跳过，不影响任务执行）',
  [ErrorCode.USAGE_QUERY_FAILED]: '模型用量统计查询失败',
  [ErrorCode.USAGE_STATS_FAILED]: '用量总览生成失败，可先看下方各模型明细',
  [ErrorCode.USAGE_REPORT_FAILED]: '用量报表生成失败（按日趋势或明细读取受阻），可稍后重试',

  // ── 定时任务 ──
  [ErrorCode.SCHEDULE_NOT_FOUND]: '定时任务不存在或已被删除',
  [ErrorCode.SCHEDULE_CRON_INVALID]: '定时规则表达式非法，请检查 cron 配置',
  [ErrorCode.SCHEDULE_EXECUTION_FAILED]: '定时任务执行失败，请查看执行记录了解详情',
  [ErrorCode.SCHEDULE_INVALID_FREQ]: '定时频率非法，仅支持 每小时 / 每天 / 每周 / 每月',
  [ErrorCode.SCHEDULE_INVALID_TIME]: '执行时刻非法，请使用 24 小时制 HH:MM（00:00–23:59）',
  [ErrorCode.SCHEDULE_INVALID_WEEKDAY]: '每周任务需指定星期（1=周一 … 7=周日）',
  [ErrorCode.SCHEDULE_INVALID_NAME]: '任务名称不能为空',
  [ErrorCode.SCHEDULE_INVALID_ID]: '缺少定时任务标识，请从任务列表重新进入编辑',
  [ErrorCode.SCHEDULE_QUERY_FAILED]: '定时任务查询失败',
  [ErrorCode.SCHEDULE_SAVE_FAILED]: '定时任务保存失败',
  [ErrorCode.SCHEDULE_ALREADY_RUNNING]: '该定时任务正在执行中，请等待本次执行结束',
  [ErrorCode.SCHEDULE_KEEP_AWAKE_FAILED]: '保持系统唤醒未能生效（唤醒约束进程启动失败）',
  [ErrorCode.SCHEDULE_KEEP_AWAKE_UNSUPPORTED]: '当前平台暂不支持保持系统唤醒',

  // ── 网络代理（Rust proxy.rs，保存前整组校验） ──
  [ErrorCode.PROXY_URL_REQUIRED]: '选择「自定义」代理时需要填写代理地址，例如 http://127.0.0.1:7890',
  [ErrorCode.PROXY_INVALID_URL]: '代理地址不合法，需带协议与主机端口，例如 http://127.0.0.1:7890',
  [ErrorCode.PROXY_CLIENT_BUILD_FAILED]: '按当前代理配置创建网络连接失败，请检查代理地址后重试',

  // ── 系统权限（Rust security/permissions.rs，去授权打开系统隐私设置） ──
  [ErrorCode.PERMISSION_KEY_UNKNOWN]: '未知的系统权限项，请刷新列表后重试',
  [ErrorCode.PERMISSION_PANE_UNAVAILABLE]:
    '当前系统没有这项权限的开关入口，请在系统设置的隐私页面中管理',
  [ErrorCode.PERMISSION_OPEN_FAILED]: '无法打开系统设置面板，请手动进入系统设置的隐私页面',
  [ErrorCode.PERMISSION_UNSUPPORTED]:
    '当前运行通道无法打开系统隐私设置，请在桌面版通天办公中管理权限',

  // ── 对齐整改通道（Hooks / 连接器 / 归档 / 电脑操控） ──
  [ErrorCode.HOOK_INVALID_EVENT]: 'Hook 事件名不在支持清单内，请从下拉列表选择',
  [ErrorCode.HOOK_INVALID_COMMAND]: 'Hook 命令不能为空',
  [ErrorCode.HOOK_INVALID_TIMEOUT]: 'Hook 超时需在 1~600 秒之间',
  [ErrorCode.HOOK_INVALID_GROUP]: 'Hook 事件需配置为分组建制数组',
  [ErrorCode.HOOK_INVALID_MATCHER]: 'Hook matcher 需为字符串（留空或 * 表示匹配全部）',
  [ErrorCode.HOOK_CONFIG_INVALID]: 'Hook 配置结构非法，请重新填写',
  [ErrorCode.HOOK_SAVE_FAILED]: 'Hook 配置保存失败',
  [ErrorCode.HOOK_QUERY_FAILED]: 'Hook 配置读取失败',
  [ErrorCode.HOOK_TEST_FAILED]: 'Hook 试运行异常终止，请查看命令输出',
  [ErrorCode.HOOK_TEST_UNSUPPORTED]: '当前通道无法执行系统命令，请在桌面应用内试运行',

  [ErrorCode.CONNECTOR_INVALID_ID]: '连接器标识不能为空',
  [ErrorCode.CONNECTOR_INVALID_CATEGORY]: '连接器分类非法，仅支持内置 / 市场',
  [ErrorCode.CONNECTOR_NOT_FOUND]: '该连接器没有授权记录，无需撤销',
  [ErrorCode.CONNECTOR_SAVE_FAILED]: '连接器授权状态保存失败',
  [ErrorCode.CONNECTOR_QUERY_FAILED]: '连接器授权状态读取失败',

  [ErrorCode.ARCHIVE_INVALID_TASK_ID]: '任务标识为空，无法操作归档',
  [ErrorCode.ARCHIVE_TASK_NOT_FOUND]: '任务不存在或已结束，无法归档',
  [ErrorCode.ARCHIVE_CONFIRM_REQUIRED]: '清空归档清单需输入确认词 DELETE',
  [ErrorCode.ARCHIVE_SAVE_FAILED]: '归档标记写入失败',
  [ErrorCode.ARCHIVE_DELETE_FAILED]: '归档任务删除失败',
  [ErrorCode.ARCHIVE_QUERY_FAILED]: '归档清单读取失败',

  [ErrorCode.COMPUTER_USE_INVALID_POLICY]: '电脑操控档位非法，仅支持 每次询问 / 自动允许 / 禁用',

  // ── 意识（awareness:* 通道，Rust awareness 模块抛出） ──
  [ErrorCode.AWARENESS_DIR_FAILED]: '意识目录无法访问，请检查存储位置权限后重试',
  [ErrorCode.AWARENESS_LOAD_FAILED]: '意识内容读取失败，可稍后重试或打开文件夹直接查看文件',
  [ErrorCode.AWARENESS_SAVE_FAILED]: '意识内容保存失败，磁盘写入受阻',
  [ErrorCode.AWARENESS_FILE_INVALID]: '意识文件不在允许编辑范围内，仅支持协作风格 / 工作手册 / 用户画像 / 长期记忆',
  [ErrorCode.AWARENESS_STYLE_INVALID]: '协作风格预设非法，仅支持 高效极简 / 深思熟虑 / 温和耐心 / 果断执行',
  [ErrorCode.AWARENESS_MEMORY_EMPTY]: '记忆条目内容不能为空',
  [ErrorCode.AWARENESS_MEMORY_NOT_FOUND]: '该记忆条目已不存在，可能已被删除',

  // ── MCP 连接器（Rust mcp 模块抛出的码不得裸奔） ──
  [ErrorCode.MCP_CONNECTION_FAILED]: '未能连上该 MCP 连接器，请检查启动命令或服务地址后重试',
  [ErrorCode.MCP_TOOL_NOT_FOUND]: '连接器未提供该工具，可能是工具清单已更新，请重新连接',
  [ErrorCode.MCP_TOOL_BLOCKED]: '该 MCP 工具已被安全策略拦截',
  [ErrorCode.MCP_AUTH_FAILED]: 'MCP 连接器鉴权失败，请检查环境变量或请求头配置',
  [ErrorCode.MCP_TIMEOUT]: 'MCP 连接器握手超时，请调大超时时间或确认服务已就绪',
  [ErrorCode.MCP_INVALID_ID]: '缺少 MCP 连接器标识，请从连接器列表重新进入',
  [ErrorCode.MCP_INVALID_NAME]: '连接器名称不能为空，且不超过 60 个字符',
  [ErrorCode.MCP_INVALID_TRANSPORT]: '传输方式非法，仅支持 STDIO / Streamable HTTP / SSE',
  [ErrorCode.MCP_INVALID_COMMAND]: '启动命令非法，需为非空单行命令',
  [ErrorCode.MCP_INVALID_URL]: '服务地址非法，请填写 http(s):// 开头的完整地址',
  [ErrorCode.MCP_INVALID_HEADER]: '环境变量或请求头配置非法，键名需是无空白的标识符',
  [ErrorCode.MCP_SERVER_EXISTS]: '已存在同名连接器，请更换名称或先删除原有配置',
  [ErrorCode.MCP_SERVER_NOT_FOUND]: '该 MCP 连接器不存在或已被删除，请刷新列表',
  [ErrorCode.MCP_SAVE_FAILED]: 'MCP 连接器配置保存失败，请检查数据目录写入权限',
  [ErrorCode.MCP_QUERY_FAILED]: 'MCP 连接器清单读取失败',
  [ErrorCode.MCP_PROTOCOL_ERROR]: 'MCP 连接器响应不符合协议，未能返回可解析的 JSON-RPC 消息',
  [ErrorCode.MCP_TRANSPORT_UNSUPPORTED]: '该传输方式需要常驻事件流，请改用 Streamable HTTP 端点',
  [ErrorCode.MCP_INVALID_CONFIG]: '连接器配置 JSON 无法解析，请检查格式后重新粘贴',
  [ErrorCode.MCP_UNSUPPORTED]: '当前运行通道无法与 MCP 连接器握手，请在桌面版通天办公中完成连接',

  // ── 文件与文档预览（file_list / file_read / file_preview 抛出的码不得裸奔） ──
  [ErrorCode.FILE_NOT_FOUND]: '文件不存在或已被移动，请刷新目录后重试',
  [ErrorCode.FILE_ACCESS_DENIED]: '该路径不在工作区文件白名单内，无法读写',
  [ErrorCode.FILE_WRITE_FAILED]: '文件写入失败，请检查目标目录写入权限',
  [ErrorCode.FILE_READ_FAILED]: '文件读取失败，可能是二进制文件或编码异常',
  [ErrorCode.FILE_CONVERSION_FAILED]: '文档正文提取失败，可尝试用本地应用打开后另存为 docx/xlsx/pptx/pdf',
  [ErrorCode.FILE_TOO_LARGE]: '文件超出大小上限，请拆分后重试',
  [ErrorCode.FILE_BACKUP_FAILED]: '附件暂存失败，请检查运行目录写入权限',
  [ErrorCode.FILE_UNSUPPORTED]: '当前运行通道无法读取本机文档正文，请在桌面版通天办公中预览',
};

/** `BRIDGE_CHAT_HTTP_{status}` 为动态码，按前缀归一 */
const BRIDGE_CHAT_HTTP_PREFIX = 'BRIDGE_CHAT_HTTP_';

function matchMessage(code: string): string | undefined {
  if (code.startsWith(BRIDGE_CHAT_HTTP_PREFIX)) {
    const status = code.slice(BRIDGE_CHAT_HTTP_PREFIX.length);
    return `Agent 引擎返回错误（HTTP ${status}）`;
  }
  return ERROR_MESSAGES[code];
}

/** 错误码 → 用户文案（未知码降级展示原始码，不白屏） */
export function errorMessageOf(code: string | undefined | null): string {
  if (!code) return '发生未知错误';
  return matchMessage(code) ?? `发生错误（${code}）`;
}

/**
 * Tauri command 拒绝值 → 用户文案。
 * Rust 侧 `Err(String)` 在 JS 里是裸字符串（非 Error 实例），格式为 `错误码: 诊断详情`，
 * 详情可能含盘符冒号（如 resource_dir="C:\..."），故只在第一个冒号处切分。
 */
export function errorTextOf(err: unknown, fallback = '发生未知错误'): string {
  const raw = typeof err === 'string' ? err : err instanceof Error ? err.message : '';
  if (!raw.trim()) return fallback;
  const sep = raw.indexOf(':');
  const code = (sep < 0 ? raw : raw.slice(0, sep)).trim();
  const detail = sep < 0 ? '' : raw.slice(sep + 1).trim();
  const mapped = matchMessage(code);
  if (!mapped) return raw; // 未映射的码：原样展示，保留诊断信息
  return detail ? `${mapped}（${detail}）` : mapped;
}
