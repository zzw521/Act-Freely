// tests/unit/shared/error-messages.test.ts — 错误码文案映射测试（流 H2）
// 覆盖：9 个三层架构演进云端必需错误码全部有用户文案；未知码降级展示不白屏。

import { describe, it, expect } from 'vitest';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

describe('错误码文案：9 个云端必需码全覆盖', () => {
  const cloudCodes: ErrorCode[] = [
    ErrorCode.BUDGET_EXHAUSTED,
    ErrorCode.QUEUE_TIMEOUT,
    ErrorCode.LEASE_LOST,
    ErrorCode.FILE_REJECTED,
    ErrorCode.SYNC_CONFLICT,
    ErrorCode.PAYMENT_GRACE,
    ErrorCode.DATA_TIER_BLOCKED,
    ErrorCode.INJECTION_BLOCKED,
    ErrorCode.QUOTA_EXCEEDED,
  ];

  it.each(cloudCodes.map((c) => [c as string, c]))('%s 有非空文案', (_name, code) => {
    const msg = errorMessageOf(code);
    expect(msg).toBeTruthy();
    // 文案不等于原始码本身（已映射为用户语言）
    expect(msg).not.toBe(code);
    expect(msg).not.toContain('发生错误（');
  });

  it('配额/预算阻断文案区分明确', () => {
    expect(errorMessageOf(ErrorCode.QUOTA_EXCEEDED)).toContain('配额');
    expect(errorMessageOf(ErrorCode.BUDGET_EXHAUSTED)).toContain('预算');
  });
});

describe('错误码文案：降级展示（防白屏）', () => {
  it('未知错误码返回含原始码的降级文案', () => {
    const msg = errorMessageOf('SOME_UNKNOWN_CODE');
    expect(msg).toContain('SOME_UNKNOWN_CODE');
  });

  it('空/undefined 返回兜底文案', () => {
    expect(errorMessageOf(undefined)).toBe('发生未知错误');
    expect(errorMessageOf(null)).toBe('发生未知错误');
    expect(errorMessageOf('')).toBe('发生未知错误');
  });
});
