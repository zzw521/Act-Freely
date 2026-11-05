// tests/unit/shared/error-surface-contract.test.ts — 跨进程错误口径契约（缺陷 D-20）
//
// D-20 的成因不是「没有错误码」，而是**同一个错误在三层里被解释三次**：Rust 以
// `Err("CODE: 详情")` 抛出，Tauri 把它变成 JS 里的**裸字符串**（不是 Error 实例），
// 渲染层却普遍写 `e instanceof Error ? e.message : String(e)`——分支走空，界面要么显示
// `[object Object]`，要么把 `AUTH_LOGIN_TIMEOUT: 等待登录超时` 这种裸码直接丢给用户。
//
// 本文件把口径钉成三条，且全是**全量扫描**，新增违规即失败：
//   1. Rust 与引擎侧凡是 `"CODE: 详情"` 形态的拒绝载荷，都必须有中文文案（不靠白名单）；
//   2. 渲染层不得再出现 `instanceof Error`，取文本一律走 shared 里的唯一实现；
//   3. 渲染层不得把错误对象/裸值插值进提示（`String(e)`、`${err}`）。

import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { errorMessageOf, errorTextOf, rawErrorText } from '../../../src/shared/error-messages';

function dir(rel: string): string {
  return fileURLToPath(new URL(rel, import.meta.url));
}

/** 递归列出源码文件（跳过构建产物与依赖） */
function sources(absDir: string, extensions: string[]): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(absDir)) {
    if (entry === 'node_modules' || entry === 'target' || entry === 'dist' || entry === '.venv' || entry === '__pycache__') continue;
    const full = join(absDir, entry);
    if (statSync(full).isDirectory()) out.push(...sources(full, extensions));
    else if (extensions.some((ext) => entry.endsWith(ext))) out.push(full);
  }
  return out;
}

const rustFiles = () => sources(dir('../../../src-tauri/src'), ['.rs']);
const engineFiles = () => sources(dir('../../../engine'), ['.py']);
const rendererFiles = () => sources(dir('../../../src/renderer'), ['.ts', '.tsx']);

/** 短路径 + 行号，失败时能直接跳到现场 */
function locate(file: string, pattern: RegExp): string {
  const lines = readFileSync(file, 'utf8').split('\n');
  const index = lines.findIndex((line) => pattern.test(line));
  const short = file.replace(/\\/g, '/').split('/').slice(-2).join('/');
  return `${short}:${index + 1}`;
}

describe('桌面（Rust）与引擎（Python）抛出的错误码全部有中文文案（全量扫描，无白名单）', () => {
  /**
   * 判定「这是一个错误码」：出现在字符串字面量开头、紧跟 ASCII 冒号。
   * 环境变量名（`TT_MODEL`）、PNG 块名（`IHDR`）、HTTP 方法（`POST`）都不带冒号，
   * 因此不会被误当错误码；而 Rust 的 `Err("CODE: 中文")`、`map_err(|e| format!("CODE: {e}"))`
   * 与 Python 的 `f"CODE: {…}"` 一律命中——三层里错误载荷只有这一种写法。
   */
  function scan(files: string[], pattern: RegExp): Map<string, string> {
    const found = new Map<string, string>();
    for (const file of files) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(pattern)) {
        if (!found.has(m[1])) {
          found.set(m[1], file.replace(/\\/g, '/').split('/').slice(-2).join('/'));
        }
      }
    }
    return found;
  }

  const rustCodes = () => scan(rustFiles(), /"([A-Z][A-Z0-9_]{2,}):/g);
  const engineCodes = () => scan(engineFiles(), /["']([A-Z][A-Z0-9_]{3,}):/g);

  /** 动态码：状态码直接拼进码里，靠 matchMessage 的前缀分支归一，不逐条登记 */
  const dynamicPrefixes = ['BRIDGE_CHAT_HTTP_', 'COLLAB_HTTP_', 'MODEL_HTTP_'];

  const emitted = new Map([...rustCodes(), ...engineCodes()]);

  it('两侧源码扫描都命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(rustCodes().size).toBeGreaterThan(300);
    expect(engineCodes().size).toBeGreaterThan(20);
  });

  it('每个码都能映射出中文文案（不出现「发生错误（CODE）」降级）', () => {
    const bare = [...emitted.entries()]
      .filter(([code]) => !dynamicPrefixes.some((prefix) => code.startsWith(prefix)))
      .filter(([code]) => errorMessageOf(code).includes('发生错误（'))
      .map(([code, where]) => `${code} <- ${where}`);
    expect(bare).toEqual([]);
  });

  it('文案是用户语言：不含错误码本身，且给出可操作信息', () => {
    const leaks = [...emitted.keys()]
      .filter((code) => !dynamicPrefixes.some((prefix) => code.startsWith(prefix)))
      .filter((code) => errorMessageOf(code).includes(code));
    expect(leaks).toEqual([]);
  });

  it('动态码前缀在 shared 里成文（新增动态码必须同时登记前缀）', () => {
    const source = readFileSync(dir('../../../src/shared/error-messages.ts'), 'utf8');
    for (const prefix of dynamicPrefixes) {
      expect(source).toContain(`'${prefix}'`);
      expect(errorMessageOf(`${prefix}503`)).toMatch(/HTTP 503/);
    }
  });

  it('中文详情经 errorTextOf 保留，不被文案覆盖掉', () => {
    const text = errorTextOf('STORE_OPEN_FAILED: 数据库被另一个实例占用（tongtian.db）');
    expect(text.startsWith('本地记录文件打不开')).toBe(true);
    expect(text).toContain('另一个实例占用');
  });
});

describe('渲染层错误取值单一实现', () => {
  it('渲染层不再出现 instanceof Error（Rust 的 Err(String) 不是 Error 实例）', () => {
    const offenders = rendererFiles()
      .filter((file) => /instanceof Error/.test(readFileSync(file, 'utf8')))
      .map((file) => locate(file, /instanceof Error/));
    expect(offenders).toEqual([]);
  });

  it('instanceof Error 只留在 shared 的唯一实现里', () => {
    const shared = sources(dir('../../../src/shared'), ['.ts', '.tsx']).filter((file) =>
      /instanceof Error/.test(readFileSync(file, 'utf8')),
    );
    expect(shared.map((file) => file.replace(/\\/g, '/').split('/').pop())).toEqual(['error-messages.ts']);
  });

  it('渲染层不把错误裸值插值进提示（String(e) / ${err} 一律禁止）', () => {
    const banned = [/String\(\s*(e|err|error)\s*\)/, /\$\{(err|error)\}/];
    const offenders = rendererFiles()
      .filter((file) => banned.some((pattern) => pattern.test(readFileSync(file, 'utf8'))))
      .map((file) => locate(file, banned[0]));
    expect(offenders).toEqual([]);
  });

  it('取文本的两个入口各有明确分工：展示走 errorTextOf，按码分支走 rawErrorText', () => {
    const files = rendererFiles();
    const display = files.filter((file) => /errorTextOf\(/.test(readFileSync(file, 'utf8')));
    const branch = files.filter((file) => /rawErrorText\(/.test(readFileSync(file, 'utf8')));
    expect(display.length).toBeGreaterThan(20);
    expect(branch.length).toBeGreaterThan(0);
    // 分支判定读的是裸码，绝不能用已经翻译成文案的返回值
    for (const file of branch) {
      const text = readFileSync(file, 'utf8');
      expect(text).not.toMatch(/errorTextOf\([^)]*\)\s*\.\s*(startsWith|match)\(/);
    }
  });
});

describe('PRD 8.2 错误口径与代码同源', () => {
  const prd = () => readFileSync(dir('../../../../docs/PRD-任我行.md'), 'utf8');

  it('契约条目写明两个入口与渲染层禁令', () => {
    const section = prd().split('### 8.2 关键契约')[1].split('\n### 8.3')[0];
    expect(section).toContain('`Err("CODE: 诊断详情")`');
    expect(section).toContain('errorTextOf');
    expect(section).toContain('rawErrorText');
    expect(section).toContain('`instanceof Error` 不允许出现在渲染层');
    expect(section).toContain('tests/unit/shared/error-surface-contract.test.ts');
  });
});

describe('Tauri 拒绝值在各形态下都能出中文原因', () => {
  it('裸字符串拒绝（Rust Err(String) 的真实形态）映射成文案', () => {
    expect(errorTextOf('WORKSPACE_NOT_FOUND: 目录已被移走')).toContain('工作空间不存在');
    expect(errorTextOf('WORKSPACE_NOT_FOUND: 目录已被移走')).toContain('目录已被移走');
  });

  it('Error 实例（引擎 HTTP 层抛出的形态）同样映射', () => {
    expect(errorTextOf(new Error('MODEL_NOT_FOUND: qwen-plus'))).toContain('该模型不在列表里');
  });

  it('此前会以裸码透出的码现在给出原因（D-20 验收：无裸字符串透出）', () => {
    for (const code of ['AUTH_LOGIN_TIMEOUT', 'TOOL_APPROVAL_DISABLED', 'SANDBOX_TEMP_FAILED', 'TASK_WINDOW_INVALID']) {
      const text = errorTextOf(`${code}: 细节`);
      expect(text).not.toContain(code);
      expect(text).toBeTruthy();
    }
  });

  it('rawErrorText 保留原始码供分支判定，且对非 Error 载荷返回空串而不是 [object Object]', () => {
    expect(rawErrorText('ARCHIVE_CONFIRM_REQUIRED: 确认词不匹配')).toBe('ARCHIVE_CONFIRM_REQUIRED: 确认词不匹配');
    expect(rawErrorText(new Error('SCHEDULE_INVALID_TIME: 时刻不合法'))).toBe('SCHEDULE_INVALID_TIME: 时刻不合法');
    expect(rawErrorText({ ok: false })).toBe('');
    expect(rawErrorText(undefined)).toBe('');
  });

  it('动态协作码：状态码进文案，不把 COLLAB_HTTP_500 丢给用户', () => {
    const text = errorTextOf('COLLAB_HTTP_500: 协作服务返回错误');
    expect(text).toContain('HTTP 500');
    expect(text).not.toContain('COLLAB_HTTP_500');
  });
});
