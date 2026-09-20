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
