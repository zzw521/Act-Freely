/**
 * 对齐整改通道 · 本地权威实现单元测试（任务 #16）
 *
 * vitest 环境为 node（无 window/localStorage），此处注入 Map 版 localStorage 垫片后
 * 以 `vi.resetModules()` 动态导入，使每个用例拿到干净的模块级台账。
 *
 * 覆盖：
 * - 与 Rust `align::{hooks,connector,archive,mod}` 的常量/错误码契约一致性（扫描 Rust 源码）
 * - Hook 配置逐条校验（六类错误码）与「未保存过即空」诚实语义
 * - 试运行在非桌面通道如实降级（HOOK_TEST_UNSUPPORTED），不假报 exitCode 0
 * - 连接器授权幂等 upsert、serverRef 沿用（COALESCE 同口径）、撤销前置校验
 * - 归档只写标记、重复归档刷新不追加脏行、清空需确认词 DELETE
 * - 电脑操控档位校验与持久化标记
 * - 通道分发器路由与未命中兜底
 * - localStorage 持久化与重启后 hydrate
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

type LocalAlignmentModule = typeof import('../../../src/renderer/lib/alignment-local');

/** 最近一次注入的 localStorage 垫片内容（供持久化用例直接断言） */
let currentStore = new Map<string, string>();

/** Map 版 localStorage 垫片（node 环境无原生实现） */
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

/** 重新加载模块，得到与当前 localStorage 同步的干净台账 */
async function freshModule(): Promise<LocalAlignmentModule> {
  vi.resetModules();
  return import('../../../src/renderer/lib/alignment-local');
}

/** 空持久层 + 干净模块（多数用例的初始态） */
async function freshWithStorage(): Promise<LocalAlignmentModule> {
  installStorage().clear();
  return freshModule();
}

/** 断言抛出的错误以指定错误码开头（与 Rust `Err("CODE: detail")` 同形态） */
function expectCode(fn: () => unknown, code: string) {
  let msg = '';
  try {
    fn();
  } catch (e) {
    msg = e instanceof Error ? e.message : String(e);
  }
  expect(msg, `期望抛出 ${code}，实际未抛错`).not.toBe('');
  expect(msg.startsWith(code)).toBe(true);
  return msg;
}

// ── 契约面：与 Rust 源码同口径 ────────────────────────────────

const rustHooks = () =>
  readFileSync(
    fileURLToPath(new URL('../../../src-tauri/src/align/hooks.rs', import.meta.url)),
    'utf8',
  );
const rustMod = () =>
  readFileSync(
    fileURLToPath(new URL('../../../src-tauri/src/align/mod.rs', import.meta.url)),
    'utf8',
  );

/** 提取 Rust `pub const NAME: &[&str] = &[...]` 数组字面量成员 */
function rustStrArray(src: string, name: string): string[] {
  const re = new RegExp(`pub const ${name}:[^=]+=\\s*&?\\[([\\s\\S]*?)\\];`);
  const body = src.match(re)?.[1];
  if (!body) throw new Error(`Rust 源码未找到常量数组 ${name}`);
  return [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

describe('对齐整改：与 Rust 常量契约一致（Rust 侧改动未同步本层即失败）', () => {
  it('HOOK_EVENTS 与 Rust ALL_EVENTS 逐项同序', async () => {
    const { HOOK_EVENTS } = await freshModule();
    expect(HOOK_EVENTS).toEqual(rustStrArray(rustHooks(), 'ALL_EVENTS'));
    expect(HOOK_EVENTS).toHaveLength(12);
  });

  it('BLOCKABLE_EVENTS 与 Rust 一致（仅这三类支持 exit 2 阻塞）', async () => {
    const { BLOCKABLE_EVENTS } = await freshModule();
    expect(BLOCKABLE_EVENTS).toEqual(rustStrArray(rustHooks(), 'BLOCKABLE_EVENTS'));
  });

  it('超时上下界与 Rust DEFAULT/MAX_TIMEOUT_SEC 一致', async () => {
    const { DEFAULT_TIMEOUT_SEC, MAX_TIMEOUT_SEC } = await freshModule();
    const src = rustHooks();
    expect(DEFAULT_TIMEOUT_SEC).toBe(Number(src.match(/DEFAULT_TIMEOUT_SEC: u64 = (\d+)/)?.[1]));
    expect(MAX_TIMEOUT_SEC).toBe(Number(src.match(/MAX_TIMEOUT_SEC: u64 = (\d+)/)?.[1]));
  });

  it('电脑操控档位与 Rust COMPUTER_USE_POLICIES 一致', async () => {
    const { COMPUTER_USE_POLICIES, COMPUTER_USE_DEFAULT } = await freshModule();
    expect(COMPUTER_USE_POLICIES).toEqual(rustStrArray(rustMod(), 'COMPUTER_USE_POLICIES'));
    expect(COMPUTER_USE_DEFAULT).toBe(
      rustMod().match(/COMPUTER_USE_DEFAULT: &str = "([^"]+)"/)?.[1],
    );
  });

  it('六类 Hook 错误码在本层与 Rust 两侧同名存在', async () => {
    const src = rustHooks();
    const codes = [
      'HOOK_INVALID_EVENT',
      'HOOK_INVALID_GROUP',
      'HOOK_INVALID_MATCHER',
      'HOOK_INVALID_COMMAND',
      'HOOK_INVALID_TIMEOUT',
      'HOOK_CONFIG_INVALID',
    ];
    const thrown = new Set<string>();
    const probe = (m: () => unknown) => {
      try {
        m();
      } catch (e) {
        thrown.add(String(e instanceof Error ? e.message : e).split(':')[0]);
      }
    };
    const { validateHookConfig } = await freshModule();
    probe(() => validateHookConfig({ hooks: { NotAnEvent: [] } }));
    probe(() => validateHookConfig({ hooks: { Stop: 'x' } }));
    probe(() => validateHookConfig({ hooks: { Stop: [{ matcher: 7, hooks: [{ command: 'echo' }] }] } }));
    probe(() => validateHookConfig({ hooks: { Stop: [{ hooks: 'nope' }] } }));
    probe(() => validateHookConfig({ hooks: { Stop: [{ hooks: [{ command: '  ' }] }] } }));
    probe(() => validateHookConfig({ hooks: { Stop: [{ hooks: [{ command: 'echo', timeout: 999 }] }] } }));
    probe(() => validateHookConfig([]));

    expect(thrown.has('HOOK_CONFIG_INVALID')).toBe(true);
    for (const code of ['HOOK_INVALID_EVENT', 'HOOK_INVALID_GROUP', 'HOOK_INVALID_MATCHER', 'HOOK_INVALID_COMMAND', 'HOOK_INVALID_TIMEOUT']) {
      expect(thrown.has(code), `本层未抛出 ${code}`).toBe(true);
      expect(src.includes(code), `Rust 侧无 ${code}`).toBe(true);
    }
  });
});

// ── Hooks 校验与读写 ─────────────────────────────────────────

describe('Hooks：事件与命令校验', () => {
  it('合法事件原样返回，非法事件抛 HOOK_INVALID_EVENT 并列出可选值', async () => {
    const { ensureHookEvent } = await freshModule();
    expect(ensureHookEvent('PreToolUse')).toBe('PreToolUse');
    expect(ensureHookEvent('  Stop  ')).toBe('Stop');
    const msg = expectCode(() => ensureHookEvent('Nope'), 'HOOK_INVALID_EVENT');
    expect(msg).toContain('SessionStart');
    expectCode(() => ensureHookEvent(undefined), 'HOOK_INVALID_EVENT');
  });

  it('空命令一律拒绝（避免存下永不触发的假 Hook）', async () => {
    const { ensureHookCommand } = await freshModule();
    expect(ensureHookCommand('echo hi')).toBe('echo hi');
    expectCode(() => ensureHookCommand('   ', 'Stop'), 'HOOK_INVALID_COMMAND');
    expectCode(() => ensureHookCommand(null), 'HOOK_INVALID_COMMAND');
  });

  it('超时归一：非 1~600 的整数秒落默认档', async () => {
    const { clampHookTimeout, DEFAULT_TIMEOUT_SEC, MAX_TIMEOUT_SEC } = await freshModule();
    expect(clampHookTimeout(30)).toBe(30);
    expect(clampHookTimeout(0)).toBe(DEFAULT_TIMEOUT_SEC);
    expect(clampHookTimeout(-5)).toBe(DEFAULT_TIMEOUT_SEC);
    expect(clampHookTimeout(1.5)).toBe(DEFAULT_TIMEOUT_SEC);
    expect(clampHookTimeout(MAX_TIMEOUT_SEC + 1)).toBe(DEFAULT_TIMEOUT_SEC);
    expect(clampHookTimeout(undefined)).toBe(DEFAULT_TIMEOUT_SEC);
    expect(clampHookTimeout('abc')).toBe(DEFAULT_TIMEOUT_SEC);
  });

  it('配置校验通过形态：{hooks:{...}} 与裸事件映射均可', async () => {
    const { validateHookConfig } = await freshModule();
    expect(() => validateHookConfig({ hooks: {} })).not.toThrow();
    expect(() =>
      validateHookConfig({
        hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo ok', timeout: 5 }] }] },
      }),
    ).not.toThrow();
    expect(() => validateHookConfig({ Stop: [{ hooks: [{ command: 'echo ok' }] }] })).not.toThrow();
  });

  it('事件数统计只计有分组的键', async () => {
    const { countHookEvents } = await freshModule();
    expect(
      countHookEvents({ hooks: { Stop: [{ hooks: [{ command: 'a' }] }], SessionStart: [], PostToolUse: [{}] } }),
    ).toBe(2);
    expect(countHookEvents(null)).toBe(0);
  });
});

describe('Hooks：读写与诚实空态', () => {
  it('未保存过时返回空 hooks，不伪造默认配置', async () => {
    const { localHooks } = await freshWithStorage();
    expect(localHooks()).toEqual({ hooks: {} });
  });

  it('保存后读回同一份配置，返回 events 计数', async () => {
    const { saveLocalHooks, localHooks } = await freshWithStorage();
    const cfg = { hooks: { Stop: [{ hooks: [{ type: 'command', command: 'echo s' }] }] } };
    expect(saveLocalHooks(cfg)).toEqual({ ok: true, events: 1 });
    expect(localHooks()).toEqual(cfg);
  });

  it('非法配置不落盘，读回仍是上一份有效配置', async () => {
    const { saveLocalHooks, localHooks } = await freshWithStorage();
    saveLocalHooks({ hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] } });
    expectCode(() => saveLocalHooks({ hooks: { BadEvent: [] } }), 'HOOK_INVALID_EVENT');
    expect(localHooks()).toEqual({ hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] } });
  });
});

describe('Hooks：试运行在非桌面通道如实降级', () => {
  it('抛 HOOK_TEST_UNSUPPORTED，而不是假报 exitCode 0', async () => {
    const { handleLocalAlignmentChannel } = await freshModule();
    const msg = expectCode(
      () => handleLocalAlignmentChannel('hooks:test', [{ event: 'Stop', command: 'echo hi' }]),
      'HOOK_TEST_UNSUPPORTED',
    );
    expect(msg).not.toContain('exitCode');
  });

  it('先校验事件与命令，非法入参不假装能执行', async () => {
    const { testLocalHook } = await freshModule();
    expectCode(() => testLocalHook('Nope', 'echo'), 'HOOK_INVALID_EVENT');
    expectCode(() => testLocalHook('Stop', ''), 'HOOK_INVALID_COMMAND');
  });
});

// ── 连接器授权 ───────────────────────────────────────────────

describe('连接器：授权/撤销（无记录即空，不伪造已授权）', () => {
  it('初始为空，授权后返回已授权视图', async () => {
    const { localConnectors, authorizeLocalConnector } = await freshWithStorage();
    expect(localConnectors()).toEqual([]);
    const view = authorizeLocalConnector('github', 'market', 'https://github.example/mcp');
    expect(view).toMatchObject({
      connectorId: 'github',
      category: 'market',
      installed: true,
      authStatus: 'authorized',
      serverRef: 'https://github.example/mcp',
    });
    expect(view.updatedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it('重复授权幂等更新，不产生脏行；列表按 id 排序', async () => {
    const { localConnectors, authorizeLocalConnector } = await freshWithStorage();
    authorizeLocalConnector('slack', 'builtin');
    authorizeLocalConnector('github', 'builtin');
    authorizeLocalConnector('slack', 'builtin');
    expect(localConnectors().map((c) => c.connectorId)).toEqual(['github', 'slack']);
    expect(localConnectors()).toHaveLength(2);
  });

  it('本次未给 serverRef 时沿用已有（与 Rust COALESCE 一致）', async () => {
    const { authorizeLocalConnector } = await freshWithStorage();
    authorizeLocalConnector('github', 'market', 'ref-1');
    expect(authorizeLocalConnector('github', 'market').serverRef).toBe('ref-1');
    expect(authorizeLocalConnector('github', 'market', 'ref-2').serverRef).toBe('ref-2');
  });

  it('分类缺省为 builtin，非法分类抛 CONNECTOR_INVALID_CATEGORY', async () => {
    const { authorizeLocalConnector } = await freshWithStorage();
    expect(authorizeLocalConnector('x').category).toBe('builtin');
    expectCode(() => authorizeLocalConnector('x', 'weird'), 'CONNECTOR_INVALID_CATEGORY');
    expectCode(() => authorizeLocalConnector('  ', 'builtin'), 'CONNECTOR_INVALID_ID');
  });

  it('撤销保留条目并降级为未授权；无授权记录时明确报错', async () => {
    const { authorizeLocalConnector, revokeLocalConnector, localConnectors } = await freshWithStorage();
    expectCode(() => revokeLocalConnector('github'), 'CONNECTOR_NOT_FOUND');
    authorizeLocalConnector('github', 'market', 'r');
    expect(revokeLocalConnector('github')).toMatchObject({ installed: false, authStatus: 'none' });
    expect(localConnectors()).toHaveLength(1);
    // 撤销后再授权可恢复
    expect(authorizeLocalConnector('github', 'market').authStatus).toBe('authorized');
  });
});

// ── 任务归档 ─────────────────────────────────────────────────

describe('归档：只写标记，不触碰任务本体', () => {
  it('空任务标识拒绝；标题缺失诚实标注；工作区缺省 default', async () => {
    const { archiveLocalTask } = await freshWithStorage();
    expectCode(() => archiveLocalTask(''), 'ARCHIVE_INVALID_TASK_ID');
    const item = archiveLocalTask('t-1');
    expect(item).toMatchObject({ taskId: 't-1', title: '(无标题任务)', workspaceId: 'default' });
  });

  it('归档时间为 ISO 字符串且与传入毫秒一致', async () => {
    const { archiveLocalTask } = await freshWithStorage();
    const ms = 1_700_000_000_000;
    expect(archiveLocalTask('t-2', '标题', 'ws-a', ms).archivedAt).toBe(new Date(ms).toISOString());
  });

  it('重复归档刷新时间而不追加脏行；清单按时间倒序', async () => {
    const { archiveLocalTask, localArchived } = await freshWithStorage();
    archiveLocalTask('t-1', 'A', 'ws', 1000);
    archiveLocalTask('t-2', 'B', 'ws', 2000);
    expect(localArchived().map((r) => r.taskId)).toEqual(['t-2', 't-1']);
    archiveLocalTask('t-1', 'A2', 'ws', 3000);
    expect(localArchived().map((r) => r.taskId)).toEqual(['t-1', 't-2']);
    expect(localArchived()[0].title).toBe('A2');
  });

  it('取消归档返回是否命中；彻底删除在本地通道同义于移除标记', async () => {
    const { archiveLocalTask, unarchiveLocalTask, deleteLocalArchivedTask } = await freshWithStorage();
    archiveLocalTask('t-1', 'A', 'ws', 1000);
    expect(unarchiveLocalTask('t-1')).toBe(true);
    expect(unarchiveLocalTask('t-1')).toBe(false);
    archiveLocalTask('t-3', 'C', 'ws', 1000);
    expect(deleteLocalArchivedTask('t-3')).toBe(true);
  });

  it('清空需确认词 DELETE，只清标记并返回条数', async () => {
    const { archiveLocalTask, clearLocalArchive, localArchived } = await freshWithStorage();
    archiveLocalTask('t-1', 'A', 'ws', 1000);
    archiveLocalTask('t-2', 'B', 'ws', 2000);
    expectCode(() => clearLocalArchive(''), 'ARCHIVE_CONFIRM_REQUIRED');
    expectCode(() => clearLocalArchive('delete'), 'ARCHIVE_CONFIRM_REQUIRED');
    expect(clearLocalArchive('DELETE')).toBe(2);
    expect(localArchived()).toEqual([]);
    expect(clearLocalArchive('DELETE')).toBe(0);
  });
});

// ── 电脑操控策略 ─────────────────────────────────────────────

describe('电脑操控策略：档位校验与持久化标记', () => {
  it('未设置过时返回默认档且 persisted=false（UI 据此区分「未配置」）', async () => {
    const { localComputerUsePolicy } = await freshWithStorage();
    expect(localComputerUsePolicy()).toEqual({ policy: 'ask_every_time', persisted: false });
  });

  it('保存合法档位后 persisted=true，读回一致', async () => {
    const { setLocalComputerUsePolicy, localComputerUsePolicy } = await freshWithStorage();
    expect(setLocalComputerUsePolicy('auto')).toBe('auto');
    expect(localComputerUsePolicy()).toEqual({ policy: 'auto', persisted: true });
    expect(setLocalComputerUsePolicy(' disabled ')).toBe('disabled');
    expect(localComputerUsePolicy().policy).toBe('disabled');
  });

  it('非法档位拒绝并保持原值', async () => {
    const { setLocalComputerUsePolicy, localComputerUsePolicy } = await freshWithStorage();
    setLocalComputerUsePolicy('auto');
    expectCode(() => setLocalComputerUsePolicy('always'), 'COMPUTER_USE_INVALID_POLICY');
    expect(localComputerUsePolicy().policy).toBe('auto');
  });
});

// ── 通道分发器 ───────────────────────────────────────────────

describe('分发器：handleLocalAlignmentChannel 路由', () => {
  it('未登记通道返回 handled=false，交由上层通道继续处理', async () => {
    const { handleLocalAlignmentChannel } = await freshModule();
    expect(handleLocalAlignmentChannel('model:list', [])).toEqual({ handled: false });
    expect(handleLocalAlignmentChannel('', [])).toEqual({ handled: false });
  });

  it('只读通道无需载荷即安全返回空态', async () => {
    const { handleLocalAlignmentChannel } = await freshWithStorage();
    expect(handleLocalAlignmentChannel('hooks:get', [])).toEqual({ handled: true, value: { hooks: {} } });
    expect(handleLocalAlignmentChannel('connector:list', [])).toEqual({ handled: true, value: [] });
    expect(handleLocalAlignmentChannel('task:archiveList', [])).toEqual({ handled: true, value: [] });
    expect(handleLocalAlignmentChannel('computerUse:getPolicy', [])).toEqual({
      handled: true,
      value: { policy: 'ask_every_time', persisted: false },
    });
  });

  it('写通道返回结构与 Rust command 对齐（ok + 关键字段）', async () => {
    const { handleLocalAlignmentChannel } = await freshWithStorage();
    expect(handleLocalAlignmentChannel('hooks:set', [{ hooks: { Stop: [{ hooks: [{ command: 'echo' }] }] } }])).toEqual({
      handled: true,
      value: { ok: true, events: 1 },
    });
    expect(handleLocalAlignmentChannel('connector:authorize', [{ connectorId: 'github', category: 'builtin' }])).toMatchObject({
      handled: true,
      value: { connectorId: 'github', installed: true },
    });
    expect(handleLocalAlignmentChannel('connector:revoke', [{ connectorId: 'github' }])).toEqual({
      handled: true,
      value: { ok: true, connectorId: 'github' },
    });
    expect(handleLocalAlignmentChannel('task:archive', [{ taskId: 't-9', title: 'T', workspaceId: 'ws' }])).toMatchObject({
      handled: true,
      value: { taskId: 't-9' },
    });
    expect(handleLocalAlignmentChannel('task:unarchive', [{ taskId: 't-9' }])).toEqual({
      handled: true,
      value: { ok: true, removed: true },
    });
    expect(handleLocalAlignmentChannel('task:archiveDelete', [{ taskId: 't-9' }])).toEqual({
      handled: true,
      value: { ok: true, removed: false },
    });
    expect(handleLocalAlignmentChannel('task:archiveClear', [{ confirm: 'DELETE' }])).toEqual({
      handled: true,
      value: { ok: true, cleared: 0 },
    });
    expect(handleLocalAlignmentChannel('computerUse:setPolicy', [{ policy: 'disabled' }])).toEqual({
      handled: true,
      value: { ok: true, policy: 'disabled' },
    });
  });
});

// ── 持久化与重启 ─────────────────────────────────────────────

describe('持久化：写穿 localStorage，重启后 hydrate', () => {
  it('四类台账均在重启后还原（含归档顺序与策略 persisted 标记）', async () => {
    await freshWithStorage();
    const first = await freshModule();
    first.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo s' }] }] } });
    first.authorizeLocalConnector('github', 'market', 'r');
    first.authorizeLocalConnector('slack', 'builtin');
    first.archiveLocalTask('t-old', '旧', 'ws', 1000);
    first.archiveLocalTask('t-new', '新', 'ws', 5000);
    first.setLocalComputerUsePolicy('auto');
    expect(currentStore.size).toBeGreaterThanOrEqual(4);

    const second = await freshModule();
    expect(second.localHooks()).toEqual({ hooks: { Stop: [{ hooks: [{ command: 'echo s' }] }] } });
    expect(second.localConnectors().map((c) => c.connectorId)).toEqual(['github', 'slack']);
    expect(second.localArchived().map((r) => r.taskId)).toEqual(['t-new', 't-old']);
    expect(second.localComputerUsePolicy()).toEqual({ policy: 'auto', persisted: true });
  });

  it('本地存档损坏时降级为空台账，不抛错阻断页面', async () => {
    await freshWithStorage();
    currentStore.set('tt:hooks', '{not json');
    currentStore.set('tt:connectors', JSON.stringify([{ connectorId: 'ok' }, null]));
    currentStore.set('tt:archived', JSON.stringify([{ nope: 1 }, { taskId: 't-1', archivedAtMs: 7 }]));
    const { localHooks, localConnectors, localArchived } = await freshModule();
    expect(localHooks()).toEqual({ hooks: {} });
    expect(localConnectors()).toHaveLength(1);
    const row = localArchived()[0];
    expect(row).toMatchObject({ taskId: 't-1', title: '(无标题任务)', workspaceId: 'default' });
    expect(row.archivedAt).toBe(new Date(7).toISOString());
  });

  it('无 localStorage 的受限环境仍可用（纯内存台账）', async () => {
    Reflect.deleteProperty(globalThis, 'localStorage');
    vi.resetModules();
    const m = await import('../../../src/renderer/lib/alignment-local');
    expect(m.localHooks()).toEqual({ hooks: {} });
    expect(m.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo' }] }] } })).toEqual({
      ok: true,
      events: 1,
    });
    expect(m.localHooks().hooks.Stop).toBeDefined();
    expect(m.archiveLocalTask('t-mem', '内存', 'ws', 1).taskId).toBe('t-mem');
    expect(m.clearLocalArchive('DELETE')).toBe(1);
  });

  it('resetLocalAlignment 同时清内存与本地持久层', async () => {
    const m = await freshWithStorage();
    m.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo' }] }] } });
    m.authorizeLocalConnector('github', 'builtin');
    m.archiveLocalTask('t-1', 'A', 'ws', 1);
    m.setLocalComputerUsePolicy('disabled');
    expect(currentStore.size).toBe(4);
    m.resetLocalAlignment();
    expect(m.localHooks()).toEqual({ hooks: {} });
    expect(m.localConnectors()).toEqual([]);
    expect(m.localArchived()).toEqual([]);
    expect(m.localComputerUsePolicy()).toEqual({ policy: 'ask_every_time', persisted: false });
    expect(currentStore.size).toBe(0);
  });
});
