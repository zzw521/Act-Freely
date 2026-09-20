// tests/unit/shared/error-messages.test.ts — 错误码文案映射测试（流 H2）
// 覆盖：9 个三层架构演进云端必需错误码全部有用户文案；未知码降级展示不白屏。

import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

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

describe('错误码文案：Bridge 码全覆盖（Rust 侧抛出的码不得裸奔）', () => {
  const bridgeCodes = Object.values(ErrorCode).filter((c) => c.startsWith('BRIDGE_'));

  it.each(bridgeCodes.map((c) => [c as string, c]))('%s 已映射为用户文案', (_name, code) => {
    const msg = errorMessageOf(code);
    expect(msg).toBeTruthy();
    expect(msg).not.toBe(code);
    expect(msg).not.toContain('发生错误（');
  });

  it('动态码 BRIDGE_CHAT_HTTP_{status} 按前缀归一并保留状态码', () => {
    expect(errorMessageOf('BRIDGE_CHAT_HTTP_500')).toContain('HTTP 500');
    expect(errorTextOf('BRIDGE_CHAT_HTTP_500')).toContain('HTTP 500');
  });

  // 契约面：扫描 Rust 侧真实抛出的 BRIDGE_* 码（新增码若未配文案，此测试失败）
  const emitted = rustBridgeCodes();

  it('Rust 源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });
});

/** 递归收集 src-tauri/src 下 Rust 源码里出现的 BRIDGE_* 错误码字面量 */
function rustBridgeCodes(): string[] {
  const root = fileURLToPath(new URL('../../../src-tauri/src', import.meta.url));
  const found = new Set<string>();
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.name.endsWith('.rs')) {
        for (const m of readFileSync(p, 'utf8').matchAll(/BRIDGE_[A-Z][A-Z_]*/g)) found.add(m[0]);
      }
    }
  };
  walk(root);
  // BRIDGE_CHAT_HTTP_ 为动态码前缀，由前缀分支单独覆盖
  return [...found].filter((c) => c !== 'BRIDGE_CHAT_HTTP_').sort();
}

describe('errorTextOf：Tauri command 拒绝值归一', () => {
  it('Rust Err(String) 以裸字符串拒绝时仍能映射（非 Error 实例）', () => {
    expect(errorTextOf('BRIDGE_START_TIMEOUT: Agent 健康检查超时（30s）')).toBe(
      'Agent 引擎启动超时（Agent 健康检查超时（30s））',
    );
  });

  it('Error 实例取 message 后映射', () => {
    expect(errorTextOf(new Error('BRIDGE_PYTHON_NOT_FOUND: 需要 Python 3.10+'))).toContain('未检测到 Python 环境');
  });

  it('详情含盘符冒号时只在第一个冒号处切分', () => {
    const text = errorTextOf('BRIDGE_SCRIPT_NOT_FOUND: engine/server.py 未找到（resource_dir=Some("C:\\tt\\resources"), exe=D:\\tt\\app.exe）');
    expect(text.startsWith('Agent 引擎脚本缺失')).toBe(true);
    expect(text).toContain('C:\\tt\\resources');
  });

  it('无详情时只出文案', () => {
    expect(errorTextOf('EMPTY_GOAL')).toBe('任务描述为空');
  });

  it('未映射内容原样透出，保留诊断信息', () => {
    expect(errorTextOf('connection refused by peer')).toBe('connection refused by peer');
  });

  it('空值/非字符串载荷走兜底文案', () => {
    expect(errorTextOf(undefined, '提交任务失败')).toBe('提交任务失败');
    expect(errorTextOf('', '提交任务失败')).toBe('提交任务失败');
    expect(errorTextOf('   ', '提交任务失败')).toBe('提交任务失败');
    expect(errorTextOf({ code: 'X' }, '提交任务失败')).toBe('提交任务失败');
  });
});
