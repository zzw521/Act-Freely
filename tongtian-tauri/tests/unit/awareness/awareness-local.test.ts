/**
 * 意识通道 · 本地权威实现单元测试（任务 #23）
 *
 * 覆盖：
 * - 与 Rust `awareness::` 的常量/错误码契约一致性（源码扫描）
 * - 文件名/风格校验错误码
 * - 本地台账读写（view/update/memories/add/delete）
 * - 记忆行格式 renderMemoryLine/parseMemoryLine roundtrip
 * - 内容归一与 ID 生成
 * - 通道分发器路由
 * - resetLocalAwareness 隔离性
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

type LocalAwarenessModule = typeof import('../../../src/renderer/lib/awareness-local');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RUST_SRC = path.resolve(HERE, '../../../src-tauri/src/awareness/mod.rs');

import { AWARENESS_FILE_NAMES } from '../../../src/shared/contracts';

let currentStore = new Map<string, string>();

function installStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const shim = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  (globalThis as Record<string, unknown>).localStorage = shim;
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};
  currentStore = store;
  return store;
}

async function freshModule(): Promise<LocalAwarenessModule> {
  vi.resetModules();
  return import('../../../src/renderer/lib/awareness-local');
}

// ── 契约一致性扫描 ──────────────────────────────────────────────

describe('Contract: Rust source constants match local module', () => {
  let rust: string;
  let mod: LocalAwarenessModule;

  beforeEach(async () => {
    installStorage();
    rust = readFileSync(RUST_SRC, 'utf8');
    mod = await freshModule();
  });

  it('file names in Rust FILE_NAMES match AWARENESS_FILE_NAMES', () => {
    const m = rust.match(/pub const FILE_NAMES[^=]*=\s*&?\[([\s\S]*?)\]/);
    expect(m).toBeTruthy();
    const rustNames = [...m![1]!.matchAll(/"([^"]+)"/g)].map((x) => x[1]);
    expect(rustNames.sort()).toEqual([...AWARENESS_FILE_NAMES].sort());
  });

  it('style IDs in Rust STYLE_IDS match AWARENESS_STYLES', () => {
    const m = rust.match(/pub const STYLE_IDS[^=]*=\s*&?\[([\s\S]*?)\]/);
    expect(m).toBeTruthy();
    const rustStyles = [...m![1]!.matchAll(/"([^"]+)"/g)].map((x) => x[1]);
    expect(rustStyles.sort()).toEqual(mod.AWARENESS_STYLES.slice().sort());
  });

  it('error codes from Rust exist in error-messages module', async () => {
    const emPath = path.resolve(HERE, '../../../src/shared/error-messages.ts');
    const emSrc = readFileSync(emPath, 'utf8');
    const codes = [
      'AWARENESS_FILE_INVALID',
      'AWARENESS_STYLE_INVALID',
      'AWARENESS_MEMORY_EMPTY',
      'AWARENESS_MEMORY_NOT_FOUND',
    ];
    for (const c of codes) {
      expect(emSrc).toContain(c);
    }
  });
});

// ── 校验函数 ─────────────────────────────────────────────────────

describe('Validation functions', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
  });

  it('ensureAwarenessFileName accepts valid names', () => {
    expect(mod.ensureAwarenessFileName('SOUL.md')).toBe('SOUL.md');
    expect(mod.ensureAwarenessFileName('MEMORY.md')).toBe('MEMORY.md');
  });

  it('ensureAwarenessFileName rejects invalid name with code', () => {
    expect(() => mod.ensureAwarenessFileName('evil.sh')).toThrow(/AWARENESS_FILE_INVALID/);
  });

  it('ensureAwarenessStyle accepts valid styles', () => {
    expect(mod.ensureAwarenessStyle('efficient')).toBe('efficient');
    expect(mod.ensureAwarenessStyle('assertive')).toBe('assertive');
  });

  it('ensureAwarenessStyle rejects invalid style with code', () => {
    expect(() => mod.ensureAwarenessStyle('passive')).toThrow(/AWARENESS_STYLE_INVALID/);
  });
});

// ── 记忆行格式 roundtrip ─────────────────────────────────────────

describe('Memory line roundtrip', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
  });

  it('render then parse recovers all fields', () => {
    const entry = { id: 'mem-1000', content: '用户喜欢 Python', source: 'manual' as const, createdAt: '2026-01-01T00:00:00.000Z' };
    const line = mod.renderMemoryLine(entry);
    const parsed = mod.parseMemoryLine(line);
    expect(parsed).toEqual(entry);
  });

  it('content containing delimiter survives roundtrip', () => {
    const entry = { id: 'mem-2000', content: `A${mod.MEMORY_DELIM}B`, source: 'auto' as const, createdAt: '2026-01-02T00:00:00.000Z' };
    const line = mod.renderMemoryLine(entry);
    const parsed = mod.parseMemoryLine(line);
    expect(parsed?.content).toBe(entry.content);
  });

  it('non-memory lines parse as null', () => {
    expect(mod.parseMemoryLine('# heading')).toBeNull();
    expect(mod.parseMemoryLine('- no delim here')).toBeNull();
  });
});

// ── normalizeMemoryContent / generateMemoryId ───────────────────

describe('normalizeMemoryContent', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
  });

  it('collapses whitespace and trims', () => {
    expect(mod.normalizeMemoryContent('  hello\n  world  ')).toBe('hello world');
  });

  it('empty after normalization yields empty', () => {
    expect(mod.normalizeMemoryContent('   \n  ')).toBe('');
  });
});

describe('generateMemoryId', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
  });

  it('produces mem-{ms}', () => {
    expect(mod.generateMemoryId(9999, [])).toBe('mem-9999');
  });

  it('appends suffix on collision', () => {
    expect(mod.generateMemoryId(9999, ['mem-9999'])).toBe('mem-9999-2');
    expect(mod.generateMemoryId(9999, ['mem-9999', 'mem-9999-2'])).toBe('mem-9999-3');
  });
});

// ── localAwarenessView / updateLocalAwareness ────────────────────

describe('Awareness view and update', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  it('initial view has defaults and dir=null', () => {
    const v = mod.localAwarenessView();
    expect(v.dir).toBeNull();
    expect(v.style).toBe('efficient');
    expect(v.files['SOUL.md']!.content).toBe(mod.AWARENESS_DEFAULTS['SOUL.md']);
    expect(v.files['SOUL.md']!.updatedAt).toBe(mod.DEFAULT_MARK);
  });

  it('update writes file content and bumps updatedAt', () => {
    const ts = 1_700_000_000_000;
    const v = mod.updateLocalAwareness({ mode: false, files: { 'WORK_MANUAL.md': 'MY CUSTOM' } }, ts);
    expect(v.mode).toBe(false);
    expect(v.files['WORK_MANUAL.md']!.content).toBe('MY CUSTOM');
    expect(v.files['WORK_MANUAL.md']!.updatedAt).toBe(new Date(ts).toISOString());
  });

  it('update rejects invalid file name', () => {
    expect(() => mod.updateLocalAwareness({ files: { 'bad.md': 'x' } as any })).toThrow(/AWARENESS_FILE_INVALID/);
  });

  it('update rejects invalid style', () => {
    expect(() => mod.updateLocalAwareness({ style: 'unknown' as any })).toThrow(/AWARENESS_STYLE_INVALID/);
  });
});

// ── addLocalMemory / deleteLocalMemory / localMemories ──────────

describe('Memory operations', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  it('add appends one entry and list returns newest-first', () => {
    const e1 = mod.addLocalMemory('第一条', undefined, 1_000);
    const e2 = mod.addLocalMemory('第二条', undefined, 2_000);
    const list = mod.localMemories();
    expect(list).toHaveLength(2);
    expect(list[0]!.id).toBe(e2.id);
    expect(list[1]!.id).toBe(e1.id);
  });

  it('add rejects empty content', () => {
    expect(() => mod.addLocalMemory('', undefined, 1_000)).toThrow(/AWARENESS_MEMORY_EMPTY/);
  });

  it('delete removes only the named entry', () => {
    const e1 = mod.addLocalMemory('保留', undefined, 1_000);
    const e2 = mod.addLocalMemory('删除', undefined, 2_000);
    mod.deleteLocalMemory(e2.id);
    const list = mod.localMemories();
    expect(list).toHaveLength(1);
    expect(list[0]!.id).toBe(e1.id);
  });

  it('delete reports NOT_FOUND for unknown id', () => {
    expect(() => mod.deleteLocalMemory('nonexistent')).toThrow(/AWARENESS_MEMORY_NOT_FOUND/);
  });
});

// ── 通道分发器 ───────────────────────────────────────────────────

describe('handleLocalAwarenessChannel dispatcher', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  it('routes awareness:get', () => {
    const r = mod.handleLocalAwarenessChannel('awareness:get', []);
    expect(r.handled).toBe(true);
    expect((r.value as any).dir).toBeNull();
  });

  it('routes awareness:update', () => {
    const r = mod.handleLocalAwarenessChannel('awareness:update', [{ mode: false }]);
    expect(r.handled).toBe(true);
    expect((r.value as any).mode).toBe(false);
  });

  it('routes awareness:memories', () => {
    const r = mod.handleLocalAwarenessChannel('awareness:memories', []);
    expect(r.handled).toBe(true);
    expect(Array.isArray(r.value)).toBe(true);
  });

  it('routes awareness:memoryAdd', () => {
    const r = mod.handleLocalAwarenessChannel('awareness:memoryAdd', [{ content: 'test entry' }]);
    expect(r.handled).toBe(true);
    expect((r.value as any).content).toBe('test entry');
  });

  it('routes awareness:memoryDelete', () => {
    mod.addLocalMemory('to delete', undefined, 1_000);
    const r = mod.handleLocalAwarenessChannel('awareness:memoryDelete', [{ id: 'mem-1000' }]);
    expect(r.handled).toBe(true);
    expect((r.value as any).ok).toBe(true);
  });

  it('returns unhandled for unknown channel', () => {
    const r = mod.handleLocalAwarenessChannel('unknown:channel', []);
    expect(r.handled).toBe(false);
  });
});

// ── resetLocalAwareness 隔离性 ──────────────────────────────────

describe('resetLocalAwareness', () => {
  it('restores initial state after operations', async () => {
    installStorage();
    const mod = await freshModule();
    mod.resetLocalAwareness();
    mod.addLocalMemory('tmp', undefined, 1_000);
    mod.resetLocalAwareness();
    expect(mod.localMemories()).toHaveLength(0);
    const v = mod.localAwarenessView();
    expect(v.files['MEMORY.md']!.content).toBe(mod.AWARENESS_DEFAULTS['MEMORY.md']);
  });
});
