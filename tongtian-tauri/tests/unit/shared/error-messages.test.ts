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

describe('错误码文案：系统默认应用打开产物', () => {
  it('打开失败与通道不支持都有用户文案，不把裸码丢给用户', () => {
    for (const code of [ErrorCode.OPEN_FAILED, ErrorCode.OPEN_EXTERNAL_UNSUPPORTED]) {
      const msg = errorMessageOf(code);
      expect(msg).toBeTruthy();
      expect(msg).not.toBe(code);
      expect(msg).not.toContain('发生错误（');
    }
    expect(errorTextOf('OPEN_EXTERNAL_UNSUPPORTED')).toContain('桌面版');
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
  return rustCodesMatching(/BRIDGE_[A-Z][A-Z_]*/g, (c) => c !== 'BRIDGE_CHAT_HTTP_');
}

/** 递归收集 Rust 源码中的错误码字面量（可传过滤谓词） */
function rustCodesMatching(pattern: RegExp, keep: (c: string) => boolean = () => true): string[] {
  const root = fileURLToPath(new URL('../../../src-tauri/src', import.meta.url));
  const found = new Set<string>();
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.name.endsWith('.rs')) {
        for (const m of readFileSync(p, 'utf8').matchAll(pattern)) found.add(m[0]);
      }
    }
  };
  walk(root);
  return [...found].filter(keep).sort();
}

describe('错误码文案：安全与治理码全覆盖（Rust security 模块抛出的码不得裸奔）', () => {
  // 只收字符串字面量中的码，避免把 POLICY_KEY 之类的常量名当成错误码
  const emitted = rustCodesMatching(/(?<=")(?:POLICY|AUDIT)_[A-Z][A-Z_]*/g);

  it('Rust security 源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的安全码均已登记文案', () => {
    const safety = Object.values(ErrorCode).filter(
      (c) => c.startsWith('POLICY_') || c.startsWith('AUDIT_'),
    );
    expect(safety.length).toBeGreaterThan(0);
    for (const code of safety) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });
});

describe('错误码文案：模型用量码全覆盖（Rust model::usage 抛出的码不得裸奔）', () => {
  const emitted = rustCodesMatching(/USAGE_[A-Z][A-Z_]*/g);

  it('Rust 用量模块源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的用量码均已登记文案', () => {
    const usage = Object.values(ErrorCode).filter((c) => c.startsWith('USAGE_'));
    expect(usage.length).toBeGreaterThan(0);
    for (const code of usage) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });
});

describe('错误码文案：定时任务码全覆盖（Rust task::schedule / keep_awake 抛出的码不得裸奔）', () => {
  // 只收「字符串字面量」中的码（lookbehind 要求紧跟双引号）：避免把常量名当成错误码
  const emitted = rustCodesMatching(/(?<=")SCHEDULE_[A-Z][A-Z_]*/g);

  it('Rust 定时任务源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
    // 常量名误入即视为扫描失效
    expect(emitted).not.toContain('SCHEDULE_RUN');
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的定时任务码均已登记文案', () => {
    const schedule = Object.values(ErrorCode).filter((c) => c.startsWith('SCHEDULE_'));
    expect(schedule.length).toBeGreaterThan(0);
    for (const code of schedule) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });

  it('关键拒绝路径文案可读（非法时刻/频率/重复执行）', () => {
    expect(errorMessageOf(ErrorCode.SCHEDULE_INVALID_TIME)).toContain('24 小时制');
    expect(errorMessageOf(ErrorCode.SCHEDULE_INVALID_FREQ)).toContain('每小时');
    expect(errorMessageOf(ErrorCode.SCHEDULE_ALREADY_RUNNING)).toContain('正在执行中');
  });
});

describe('错误码文案：技能码全覆盖（Rust skill 模块抛出的码不得裸奔）', () => {
  // 只收字符串字面量中的码，避免把 DISABLED_KEY 之类常量名当成错误码
  const emitted = rustCodesMatching(/(?<=")SKILL_[A-Z][A-Z_]*/g);

  it('Rust 技能源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
    expect(emitted).not.toContain('SKILL_LIST');
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的技能码均已登记文案', () => {
    const skills = Object.values(ErrorCode).filter((c) => c.startsWith('SKILL_'));
    expect(skills.length).toBeGreaterThan(0);
    for (const code of skills) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });

  it('关键拒绝路径文案指向可操作动作', () => {
    expect(errorMessageOf(ErrorCode.SKILL_INCOMPATIBLE)).toContain('SKILL.md');
    expect(errorMessageOf(ErrorCode.SKILL_UNSUPPORTED)).toContain('桌面版');
    expect(errorMessageOf(ErrorCode.SKILL_INSTALL_FAILED)).toContain('写入权限');
  });
});

describe('错误码文案：MCP 连接器码全覆盖（Rust mcp 模块抛出的码不得裸奔）', () => {
  // 只收字符串字面量中的码，避免把 TRANSPORT_STDIO 之类的常量名当成错误码
  const emitted = rustCodesMatching(/(?<=")MCP_[A-Z][A-Z_]*/g);

  it('Rust MCP 源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
    expect(emitted).not.toContain('MCP_LIST_SERVERS');
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的 MCP 码均已登记文案', () => {
    const mcp = Object.values(ErrorCode).filter((c) => c.startsWith('MCP_'));
    expect(mcp.length).toBeGreaterThan(0);
    for (const code of mcp) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });

  it('关键拒绝路径文案指向可操作动作', () => {
    expect(errorMessageOf(ErrorCode.MCP_INVALID_TRANSPORT)).toContain('STDIO');
    expect(errorMessageOf(ErrorCode.MCP_INVALID_URL)).toContain('http');
    expect(errorMessageOf(ErrorCode.MCP_SERVER_EXISTS)).toContain('同名');
    expect(errorMessageOf(ErrorCode.MCP_SERVER_NOT_FOUND)).toContain('刷新');
    expect(errorMessageOf(ErrorCode.MCP_TIMEOUT)).toContain('超时');
    expect(errorMessageOf(ErrorCode.MCP_UNSUPPORTED)).toContain('桌面');
  });
});

describe('错误码文案：文件与文档预览码全覆盖（Rust file 模块抛出的码不得裸奔）', () => {
  // 只收字符串字面量中的码，避免把 SANDBOX_FILE_BLOCKED 之类常量名当成错误码
  const emitted = rustCodesMatching(/(?<=")FILE_[A-Z][A-Z_]*/g);

  it('Rust 文件源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
    expect(emitted).toContain('FILE_CONVERSION_FAILED');
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('ErrorCode 枚举中的文件码均已登记文案', () => {
    const fileCodes = Object.values(ErrorCode).filter((c) => c.startsWith('FILE_'));
    expect(fileCodes.length).toBeGreaterThanOrEqual(9);
    for (const code of fileCodes) {
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
  });

  it('关键拒绝路径文案指向可操作动作', () => {
    expect(errorMessageOf(ErrorCode.FILE_NOT_FOUND)).toContain('刷新');
    expect(errorMessageOf(ErrorCode.FILE_ACCESS_DENIED)).toContain('白名单');
    expect(errorMessageOf(ErrorCode.FILE_CONVERSION_FAILED)).toContain('docx');
    expect(errorMessageOf(ErrorCode.FILE_TOO_LARGE)).toContain('拆分');
    expect(errorMessageOf(ErrorCode.FILE_UNSUPPORTED)).toContain('桌面');
  });

  it('转换失败携带中文原因时不丢诊断信息（原因内无 ASCII 冒号，原样拼进文案）', () => {
    const text = errorTextOf(
      'FILE_CONVERSION_FAILED: 旧版二进制 Office 格式（.doc/.xls/.ppt）暂无解析能力，请另存为 docx/xlsx/pptx 后预览',
    );
    expect(text.startsWith('文档正文提取失败')).toBe(true);
    expect(text).toContain('旧版二进制 Office 格式');
  });
});

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
