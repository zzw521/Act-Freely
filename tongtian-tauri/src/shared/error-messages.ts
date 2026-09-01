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
  BRIDGE_ERROR: '与 Agent 引擎通信失败',
  BRIDGE_PYTHON_NOT_FOUND: '未检测到 Python 环境，请先安装 Python 3.10+',
  BRIDGE_START_TIMEOUT: 'Agent 引擎启动超时',
  EMPTY_GOAL: '任务描述为空',
  TASK_CANCELLED: '任务已取消',
};

/** 错误码 → 用户文案（未知码降级展示原始码，不白屏） */
export function errorMessageOf(code: string | undefined | null): string {
  if (!code) return '发生未知错误';
  return ERROR_MESSAGES[code] ?? `发生错误（${code}）`;
}
