/**
 * 对齐整改通道 · 本地权威实现单元测试（任务 #16）
 *
 * vitest 环境为 node（无 window/localStorage），此处注入 Map 版 localStorage 垫片后
 * 以 `vi.resetModules()` 动态导入，使每个用例拿到干净的模块级台账。
 *
 * 覆盖：
 * - 与 Rust `align::{hooks,archive,mod}` 的常量/错误码契约一致性（扫描 Rust 源码）
 * - Hook 配置逐条校验（六类错误码）与「未保存过即空」诚实语义
 * - 试运行在非桌面通道如实降级（HOOK_TEST_UNSUPPORTED），不假报 exitCode 0
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

  it('有触发点/无触发点事件与 Rust FIRED_EVENTS、UNFIRED_EVENTS 逐项一致', async () => {
    const { FIRED_HOOK_EVENTS, UNFIRED_HOOK_EVENTS, HOOK_EVENTS } = await freshModule();
    expect(FIRED_HOOK_EVENTS).toEqual(rustStrArray(rustHooks(), 'FIRED_EVENTS'));
    expect(UNFIRED_HOOK_EVENTS).toEqual(rustStrArray(rustHooks(), 'UNFIRED_EVENTS'));
    // 两类必须互斥且并起来正好覆盖 12 类，否则页面会漏说或多说「配了会不会跑」
    expect(FIRED_HOOK_EVENTS.some((e) => UNFIRED_HOOK_EVENTS.includes(e))).toBe(false);
    expect([...FIRED_HOOK_EVENTS, ...UNFIRED_HOOK_EVENTS].sort()).toEqual([...HOOK_EVENTS].sort());
  });

  it('页面事件表的 fired 标记与 Rust FIRED_EVENTS 一致（防止文案宣称不存在的触发点）', async () => {
    const { FIRED_HOOK_EVENTS } = await freshModule();
    const src = readFileSync(
      fileURLToPath(new URL('../../../src/renderer/pages/HooksManager/index.tsx', import.meta.url)),
      'utf8',
    );
    const rows = [...src.matchAll(/\{\s*name:\s*'([^']+)'[^}]*?fired:\s*(true|false)\s*\}/g)];
    expect(rows.length, '未解析到事件表定义').toBe(12);
    const firedInPage = rows.filter((m) => m[2] === 'true').map((m) => m[1]);
    expect(firedInPage.sort()).toEqual([...FIRED_HOOK_EVENTS].sort());
  });

  it('命令文本改写后旧放行不会残留（本地 prune 与 Rust prune_grants 同口径）', async () => {
    const src = rustHooks();
    expect(src).toContain('pub fn prune_grants');
    expect(rustMod()).toContain('prune_grants(');
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
    expect(saveLocalHooks(cfg)).toEqual({ ok: true, events: 1, revoked: [] });
    expect(localHooks()).toEqual(cfg);
  });

  it('非法配置不落盘，读回仍是上一份有效配置', async () => {
    const { saveLocalHooks, localHooks } = await freshWithStorage();
    saveLocalHooks({ hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] } });
    expectCode(() => saveLocalHooks({ hooks: { BadEvent: [] } }), 'HOOK_INVALID_EVENT');
    expect(localHooks()).toEqual({ hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] } });
  });
});

describe('Hooks：放行台账（配置 ≠ 执行）', () => {
  it('放行需合法事件与非空命令', async () => {
    const { grantLocalHooks } = await freshWithStorage();
    expect(grantLocalHooks('PreToolUse', ' echo guard.sh ')).toEqual({ ok: true, granted: true });
    expectCode(() => grantLocalHooks('Nope', 'echo'), 'HOOK_INVALID_EVENT');
    expectCode(() => grantLocalHooks('Stop', '   '), 'HOOK_INVALID_COMMAND');
  });

  it('放行按命令原文去重：重复放行只刷新记录，不追加第二条', async () => {
    const m = await freshWithStorage();
    m.grantLocalHooks('PreToolUse', 'echo guard.sh');
    const first = m.localHookGrants()[0];
    m.grantLocalHooks('Stop', 'echo guard.sh');
    const ledger = m.localHookGrants();
    expect(ledger).toHaveLength(1);
    expect(ledger[0]).toMatchObject({ command: 'echo guard.sh', event: 'Stop', runCount: 0 });
    expect(ledger[0].grantedAtMs).toBeGreaterThanOrEqual(first.grantedAtMs);
  });

  it('撤销放行只删授权，配置原样保留（撤权 ≠ 删配置）', async () => {
    const m = await freshWithStorage();
    const cfg = { hooks: { PreToolUse: [{ hooks: [{ command: 'echo guard.sh' }] }] } };
    m.saveLocalHooks(cfg);
    m.grantLocalHooks('PreToolUse', 'echo guard.sh');
    expect(m.revokeLocalHooks('echo guard.sh')).toEqual({ ok: true, granted: false });
    expect(m.localHookGrants()).toEqual([]);
    expect(m.localHooks()).toEqual(cfg);
    expect(m.localHooksStatus().ungranted).toEqual(['echo guard.sh']);
  });

  it('配置里删掉命令后其放行一并作废，并回报作废清单', async () => {
    const m = await freshWithStorage();
    m.saveLocalHooks({
      hooks: { PreToolUse: [{ hooks: [{ command: 'echo a' }, { command: 'echo b' }] }] },
    });
    m.grantLocalHooks('PreToolUse', 'echo a');
    m.grantLocalHooks('PreToolUse', 'echo b');
    const res = m.saveLocalHooks({ hooks: { PreToolUse: [{ hooks: [{ command: 'echo a' }] }] } });
    expect(res).toEqual({ ok: true, events: 1, revoked: ['echo b'] });
    expect(m.localHookGrants().map((g) => g.command)).toEqual(['echo a']);
  });

  it('删掉再原样加回不会残留旧放行（放行只认命令文本，不认「同一条 Hook」）', async () => {
    const m = await freshWithStorage();
    const cfg = { hooks: { PreToolUse: [{ hooks: [{ command: 'echo a' }] }] } };
    m.saveLocalHooks(cfg);
    m.grantLocalHooks('PreToolUse', 'echo a');
    expect(m.localHooksStatus().ungranted).toEqual([]);
    m.saveLocalHooks({ hooks: {} });
    m.saveLocalHooks(cfg);
    expect(m.localHooksStatus().ungranted).toEqual(['echo a']);
  });

  it('执行面概览：挂载事件、无触发点事件、未放行清单与通道降级说明', async () => {
    const m = await freshWithStorage();
    m.saveLocalHooks({
      hooks: {
        PreToolUse: [{ hooks: [{ command: 'echo a' }] }],
        Notification: [{ hooks: [{ command: 'echo n' }] }],
      },
    });
    m.grantLocalHooks('PreToolUse', 'echo a');
    const s = m.localHooksStatus();
    expect(s).toMatchObject({
      configured: 2,
      mountedEvents: ['PreToolUse', 'Notification'],
      mountedUnfired: ['Notification'],
      ungranted: ['echo n'],
    });
    expect(s.firedEvents).toHaveLength(10);
    expect(s.unfiredEvents).toHaveLength(2);
    expect(s.grants).toHaveLength(1);
    expect(s.gateError).toContain('不会执行');
  });

  it('放行台账整本返回，不因当前配置筛选而隐藏记录（与 Rust list_grants 同口径）', async () => {
    const m = await freshWithStorage();
    m.grantLocalHooks('PreToolUse', 'echo orphan');
    const s = m.localHooksStatus();
    expect(s.configured).toBe(0);
    expect(s.grants.map((g) => g.command)).toEqual(['echo orphan']);
  });

  it('放行台账写穿本地持久层，重启后仍在', async () => {
    await freshWithStorage();
    const first = await freshModule();
    first.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo s' }] }] } });
    first.grantLocalHooks('Stop', 'echo s');
    const second = await freshModule();
    expect(second.localHookGrants().map((g) => g.command)).toEqual(['echo s']);
    expect(second.localHooksStatus().ungranted).toEqual([]);
  });

  it('损坏的放行存档降级为空台账，不阻断页面', async () => {
    await freshWithStorage();
    currentStore.set('tt:hookGrants', JSON.stringify([{ command: '', event: 'Stop' }, null, 7]));
    const m = await freshModule();
    expect(m.localHookGrants()).toEqual([]);
    expect(m.localHooksStatus().grants).toEqual([]);
  });

  it('通道分发：hooks:grant / hooks:revoke / hooks:status 路由到本地台账', async () => {
    const m = await freshWithStorage();
    expect(m.handleLocalAlignmentChannel('hooks:grant', [{ event: 'PreToolUse', command: 'echo a' }])).toEqual({
      handled: true,
      value: { ok: true, granted: true },
    });
    const view = m.handleLocalAlignmentChannel('hooks:status', []) as { value: { grants: unknown[] } };
    expect(view.value.grants).toHaveLength(1);
    expect(m.handleLocalAlignmentChannel('hooks:revoke', [{ command: 'echo a' }])).toEqual({
      handled: true,
      value: { ok: true, granted: false },
    });
    const after = m.handleLocalAlignmentChannel('hooks:status', []) as { value: { grants: unknown[] } };
    expect(after.value.grants).toHaveLength(0);
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
      value: { ok: true, events: 1, revoked: [] },
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
  it('三类台账均在重启后还原（含归档顺序与策略 persisted 标记）', async () => {
    await freshWithStorage();
    const first = await freshModule();
    first.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo s' }] }] } });
    first.archiveLocalTask('t-old', '旧', 'ws', 1000);
    first.archiveLocalTask('t-new', '新', 'ws', 5000);
    first.setLocalComputerUsePolicy('auto');
    expect(currentStore.size).toBeGreaterThanOrEqual(3);

    const second = await freshModule();
    expect(second.localHooks()).toEqual({ hooks: { Stop: [{ hooks: [{ command: 'echo s' }] }] } });
    expect(second.localArchived().map((r) => r.taskId)).toEqual(['t-new', 't-old']);
    expect(second.localComputerUsePolicy()).toEqual({ policy: 'auto', persisted: true });
  });

  it('本地存档损坏时降级为空台账，不抛错阻断页面', async () => {
    await freshWithStorage();
    currentStore.set('tt:hooks', '{not json');
    currentStore.set('tt:archived', JSON.stringify([{ nope: 1 }, { taskId: 't-1', archivedAtMs: 7 }]));
    const { localHooks, localArchived } = await freshModule();
    expect(localHooks()).toEqual({ hooks: {} });
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
      revoked: [],
    });
    expect(m.localHooks().hooks.Stop).toBeDefined();
    expect(m.archiveLocalTask('t-mem', '内存', 'ws', 1).taskId).toBe('t-mem');
    expect(m.clearLocalArchive('DELETE')).toBe(1);
  });

  it('resetLocalAlignment 同时清内存与本地持久层', async () => {
    const m = await freshWithStorage();
    m.saveLocalHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo' }] }] } });
    m.archiveLocalTask('t-1', 'A', 'ws', 1);
    m.setLocalComputerUsePolicy('disabled');
    expect(currentStore.size).toBe(3);
    m.resetLocalAlignment();
    expect(m.localHooks()).toEqual({ hooks: {} });
    expect(m.localArchived()).toEqual([]);
    expect(m.localComputerUsePolicy()).toEqual({ policy: 'ask_every_time', persisted: false });
    expect(currentStore.size).toBe(0);
  });
});
