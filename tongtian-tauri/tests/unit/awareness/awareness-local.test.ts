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

  it('defaults to global and keeps workspace scope with its project id', () => {
    const global = mod.addLocalMemory('全局事实', undefined, 1_000);
    expect(global.scope).toBe('global');
    expect(global.workspaceId).toBe('');
    expect(global.kind).toBe('fact');
    expect(global.updatedAt).toBe(global.createdAt);
    const project = mod.addLocalMemory('项目事实', 'auto', 2_000, 'workspace', 'ws-a');
    expect(project.scope).toBe('workspace');
    expect(project.workspaceId).toBe('ws-a');
    expect(project.kind).toBe('task');
    const stored = mod.localMemories().find((e) => e.id === project.id);
    expect(stored?.workspaceId).toBe('ws-a');
    expect(stored?.occurrences).toBe(1);
  });

  it('rejects workspace scope without a valid workspace id', () => {
    expect(() => mod.addLocalMemory('x', undefined, 1_000, 'workspace', 'Bad Id')).toThrow(
      /MEMORY_INVALID_SCOPE/,
    );
    // 未知档位按全局处理（本地台账不新增第二种归属）
    expect(mod.addLocalMemory('y', undefined, 3_000, 'team').scope).toBe('global');
  });
});

// ── 复核 / 实体页 / 导出导入 / 维护（D-35 二期） ────────────────

function importEnvelope(entries: unknown[]): string {
  return JSON.stringify({
    format: 'tongtian.memories',
    version: 1,
    exportedAt: '2026-10-01T00:00:00Z',
    count: entries.length,
    entries,
  });
}

function packageEnvelope(entries: unknown[], version = 1): string {
  return JSON.stringify({
    format: 'tongtian.hmpkg',
    version,
    createdAt: '2026-10-01T00:00:00Z',
    count: entries.length,
    entries,
  });
}

function base64Of(text: string): string {
  return Buffer.from(text, 'utf8').toString('base64');
}

describe('Memory review and entities', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  function seed(): void {
    mod.importLocalMemories(
      importEnvelope([
        {
          id: 'mem-pending',
          content: '客户要求周报在周五 17:00 前提交',
          source: 'auto',
          kind: 'preference',
          scope: 'global',
          createdAt: '2026-10-01T00:00:00Z',
          status: 'pending',
        },
        {
          id: 'mem-pg',
          content: '数据库选型采用 PostgreSQL',
          source: 'auto',
          kind: 'fact',
          scope: 'workspace',
          workspaceId: 'ws-a',
          createdAt: '2026-10-02T00:00:00Z',
          status: 'active',
          subject: '数据库',
          predicate: '选型',
          object: 'PostgreSQL',
          confidence: 0.55,
        },
        {
          id: 'mem-mysql',
          content: '数据库选型采用 MySQL',
          source: 'auto',
          kind: 'fact',
          scope: 'workspace',
          workspaceId: 'ws-a',
          createdAt: '2026-10-03T00:00:00Z',
          status: 'pending',
          subject: '数据库',
          predicate: '选型',
          object: 'MySQL',
        },
      ]),
      10_000,
    );
  }

  it('confirm turns a pending entry active and records review time', () => {
    seed();
    const row = mod.reviewLocalMemory('mem-pending', 'confirm', 20_000);
    expect(row.status).toBe('active');
    expect(row.reviewedAt).toBe(new Date(20_000).toISOString());
  });

  it('reject only accepts pending and archives without deleting content', () => {
    seed();
    const row = mod.reviewLocalMemory('mem-pending', 'reject', 20_000);
    expect(row.status).toBe('archived');
    expect(mod.localMemories().some((m) => m.id === 'mem-pending')).toBe(true);
    expect(() => mod.reviewLocalMemory('mem-pg', 'reject', 20_000)).toThrow(
      /MEMORY_STATUS_INVALID/,
    );
  });

  it('restore only accepts archived and re-runs conflict resolution', () => {
    seed();
    expect(() => mod.reviewLocalMemory('mem-pg', 'restore', 20_000)).toThrow(
      /MEMORY_STATUS_INVALID/,
    );
    mod.reviewLocalMemory('mem-pg', 'archive', 21_000);
    const restored = mod.reviewLocalMemory('mem-pg', 'restore', 22_000);
    expect(restored.status).toBe('active');
    // 已生效的条目不能再次「恢复」（只有归档状态可恢复）
    expect(() => mod.reviewLocalMemory('mem-pg', 'restore', 23_000)).toThrow(
      /MEMORY_STATUS_INVALID/,
    );
  });

  it('confirming a newer fact soft-retires the older conflicting one', () => {
    seed();
    mod.reviewLocalMemory('mem-mysql', 'confirm', 30_000);
    const rows = mod.localMemories();
    const old = rows.find((m) => m.id === 'mem-pg')!;
    const next = rows.find((m) => m.id === 'mem-mysql')!;
    expect(old.status).toBe('archived');
    expect(old.supersededBy).toBe('mem-mysql');
    expect(next.status).toBe('active');
    // 被取代的条目不能直接恢复（避免两条同类事实同时生效）
    expect(() => mod.reviewLocalMemory('mem-pg', 'restore', 31_000)).toThrow(/MEMORY_SUPERSEDED/);
  });

  it('rejects unknown actions and missing ids with explicit codes', () => {
    seed();
    expect(() => mod.reviewLocalMemory('mem-pg', 'approve', 1)).toThrow(/MEMORY_STATUS_INVALID/);
    expect(() => mod.reviewLocalMemory('', 'confirm', 1)).toThrow(/MEMORY_NOT_FOUND/);
    expect(() => mod.reviewLocalMemory('mem-gone', 'confirm', 1)).toThrow(/MEMORY_NOT_FOUND/);
  });

  it('entities aggregate active facts by subject for the current project only', () => {
    seed();
    const before = mod.localMemoryEntities('ws-a');
    expect(before).toHaveLength(1);
    expect(before[0]!.subject).toBe('数据库');
    expect(before[0]!.entries.map((e) => e.id)).toEqual(['mem-pg']);
    // pending 不进入实体页；确认后旧事实退休，只剩新事实
    mod.reviewLocalMemory('mem-mysql', 'confirm', 30_000);
    const after = mod.localMemoryEntities('ws-a');
    expect(after).toHaveLength(1);
    expect(after[0]!.entries.map((e) => e.object)).toEqual(['MySQL']);
    // 其他项目看不到 ws-a 的事实
    expect(mod.localMemoryEntities('ws-b')).toHaveLength(0);
  });
});

describe('Memory export / import / compact', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  it('export writes an envelope and reports the local path', () => {
    mod.addLocalMemory('导出一条', undefined, 1_000);
    const result = mod.exportLocalMemories(2_000);
    expect(result.count).toBe(1);
    expect(result.path).toBe(mod.LOCAL_EXPORT_PATH);
    const stored = JSON.parse(currentStore.get('tt:awarenessMemoryExport')!);
    expect(stored.format).toBe('tongtian.memories');
    expect(stored.entries[0].content).toBe('导出一条');
  });

  it('import roundtrip restores entries and backs up first', () => {
    mod.addLocalMemory('导出前就有的条目', undefined, 1_000);
    const exported = mod.exportLocalMemories(2_000);
    expect(exported.count).toBe(1);
    const json = currentStore.get('tt:awarenessMemoryExport')!;
    mod.resetLocalAwareness();
    const result = mod.importLocalMemories(json, 3_000);
    expect(result.inserted).toBe(1);
    expect(result.total).toBe(1);
    expect(result.backupPath).toBe(mod.LOCAL_BACKUP_PATH);
    expect(mod.localMemories()[0]!.content).toBe('导出前就有的条目');
  });

  it('import dedups by scope + content and merges occurrences', () => {
    const entry = { id: 'mem-dup', content: '重复事实', source: 'auto', scope: 'global' };
    const first = mod.importLocalMemories(importEnvelope([entry]), 1_000);
    expect(first.inserted).toBe(1);
    const second = mod.importLocalMemories(importEnvelope([entry]), 2_000);
    expect(second.inserted).toBe(0);
    expect(second.merged).toBe(1);
    expect(mod.localMemories()).toHaveLength(1);
    expect(mod.localMemories()[0]!.occurrences).toBe(2);
  });

  it('import validates format, version and entries array', () => {
    expect(() => mod.importLocalMemories('', 1)).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() => mod.importLocalMemories('not json', 1)).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() =>
      mod.importLocalMemories(JSON.stringify({ format: 'other.tool', entries: [] }), 1),
    ).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() =>
      mod.importLocalMemories(JSON.stringify({ format: 'tongtian.memories', version: 9, entries: [] }), 1),
    ).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() =>
      mod.importLocalMemories(JSON.stringify({ format: 'tongtian.memories', version: 1 }), 1),
    ).toThrow(/MEMORY_IMPORT_INVALID/);
  });

  it('accepts a plain entries array and counts invalid rows as skipped', () => {
    const result = mod.importLocalMemories(
      JSON.stringify([{ content: '数组形式' }, { content: '   ' }]),
      1_000,
    );
    expect(result.total).toBe(2);
    expect(result.inserted).toBe(1);
    expect(result.skipped).toBe(1);
  });

  it('compact archives only old auto task summaries beyond the keep limit', () => {
    for (let i = 0; i < mod.COMPACT_KEEP_PER_SCOPE + 1; i += 1) {
      mod.addLocalMemory(`任务摘要 ${i}`, 'auto', 1_000 + i);
    }
    mod.addLocalMemory('人工录入不应被归档', 'manual', 5_000);
    const result = mod.compactLocalMemories(9_000);
    expect(result.archived).toBe(1);
    expect(result.keepPerScope).toBe(mod.COMPACT_KEEP_PER_SCOPE);
    const manual = mod.localMemories().find((m) => m.content === '人工录入不应被归档');
    expect(manual?.status).toBe('active');
    const archived = mod.localMemories().filter((m) => m.status === 'archived');
    expect(archived).toHaveLength(1);
    expect(archived[0]!.content).toBe('任务摘要 0');
  });
});

describe('Memory phase 3: model switch, doc sync and package', () => {
  let mod: LocalAwarenessModule;
  beforeEach(async () => {
    installStorage();
    mod = await freshModule();
    mod.resetLocalAwareness();
  });

  it('keeps memoryModelExtract in the profile and resets it off', () => {
    expect(mod.localAwarenessView().memoryModelExtract).toBe(false);
    mod.updateLocalAwareness({ memoryModelExtract: true }, 1_000);
    expect(mod.localAwarenessView().memoryModelExtract).toBe(true);
    mod.resetLocalAwareness();
    expect(mod.localAwarenessView().memoryModelExtract).toBe(false);
  });

  it('doc sync reports file entries as existing without duplicating', () => {
    mod.addLocalMemory('已在台账里的条目', undefined, 1_000);
    const result = mod.syncLocalMemoryDoc();
    expect(result).toEqual({ fileCount: 1, imported: 0, existing: 1, skipped: 0 });
    expect(mod.localMemories()).toHaveLength(1);
  });

  it('package export reports the local path and import restores entries', () => {
    mod.addLocalMemory('迁移包条目', undefined, 1_000);
    const exported = mod.exportLocalMemoryPackage(2_000);
    expect(exported.count).toBe(1);
    expect(exported.path).toBe(mod.LOCAL_PACKAGE_PATH);
    const envelope = currentStore.get('tt:awarenessMemoryPackage')!;
    mod.resetLocalAwareness();
    const result = mod.importLocalMemoryPackage(base64Of(envelope), 3_000);
    expect(result.inserted).toBe(1);
    expect(result.packageVersion).toBe(1);
    // 本地导出把 createdAt 记为导出时刻，导入时回填成 exportedAt
    expect(result.exportedAt).toBe(new Date(2_000).toISOString());
    expect(result.backupPath).toBe(mod.LOCAL_BACKUP_PATH);
    expect(mod.localMemories()[0]!.content).toBe('迁移包条目');
  });

  it('package import validates base64, format and version', () => {
    expect(() => mod.importLocalMemoryPackage('', 1)).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() => mod.importLocalMemoryPackage('###', 1)).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() =>
      mod.importLocalMemoryPackage(
        base64Of(JSON.stringify({ format: 'other.tool', entries: [] })),
        1,
      ),
    ).toThrow(/MEMORY_IMPORT_INVALID/);
    expect(() =>
      mod.importLocalMemoryPackage(base64Of(packageEnvelope([], 9)), 1),
    ).toThrow(/MEMORY_IMPORT_INVALID/);
  });

  it('dispatcher routes doc sync and package channels', () => {
    expect(mod.handleLocalAwarenessChannel('awareness:memoryDocSync', []).handled).toBe(true);
    expect(mod.handleLocalAwarenessChannel('awareness:memoryExportPackage', []).handled).toBe(true);
    const envelope = base64Of(packageEnvelope([{ content: '包内条目' }]));
    const imported = mod.handleLocalAwarenessChannel('awareness:memoryImportPackage', [
      { data: envelope },
    ]);
    expect(imported.handled).toBe(true);
    expect((imported.value as any).inserted).toBe(1);
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

  it('routes awareness:memoryReview / entities / export / import / compact', () => {
    const added = mod.addLocalMemory('待复核', 'auto', 1_000);
    const review = mod.handleLocalAwarenessChannel('awareness:memoryReview', [
      { id: added.id, action: 'archive' },
    ]);
    expect(review.handled).toBe(true);
    expect((review.value as any).status).toBe('archived');
    expect(mod.handleLocalAwarenessChannel('awareness:memoryEntities', [{ workspaceId: 'ws-a' }]).handled).toBe(true);
    expect(mod.handleLocalAwarenessChannel('awareness:memoryExport', []).handled).toBe(true);
    expect(
      mod.handleLocalAwarenessChannel('awareness:memoryImport', [
        { json: importEnvelope([{ content: '导入一条' }]) },
      ]).handled,
    ).toBe(true);
    expect(mod.handleLocalAwarenessChannel('awareness:memoryCompact', []).handled).toBe(true);
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
