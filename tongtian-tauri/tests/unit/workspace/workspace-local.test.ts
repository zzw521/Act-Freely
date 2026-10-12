/**
 * 工作空间 · 本地权威实现单元测试（任务 #18）
 *
 * vitest 环境为 node（无 window/localStorage），此处注入 Map 版 localStorage 垫片后
 * 以 `vi.resetModules()` 动态导入，使每个用例拿到干净的模块级台账。
 *
 * 覆盖：
 * - 与 Rust `workspace` 的错误码契约一致性（扫描 Rust 源码，Rust 侧新增码未登记文案即失败）
 * - 标识校验与 Rust `normalize_id` 逐条同口径（长度/路径分隔符/字符集）
 * - default 恒在与「未落地不伪装」的 exists 语义
 * - 创建幂等（保留原 createdAtMs，不追加重复行）
 * - 通道分发器路由与未命中兜底
 * - localStorage 持久化、重启 hydrate、损坏数据降级、无存储时内存台账可用
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

type LocalWorkspaceModule = typeof import('../../../src/renderer/lib/workspace-local');

const KEY = 'tt:workspaces';

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

/** 移除 localStorage（模拟隐私模式/无存储环境） */
function uninstallStorage() {
  delete (globalThis as Record<string, unknown>).localStorage;
}

/** 重新加载模块，得到与当前 localStorage 同步的干净台账 */
async function freshModule(): Promise<LocalWorkspaceModule> {
  vi.resetModules();
  return import('../../../src/renderer/lib/workspace-local');
}

/** 空持久层 + 干净模块（多数用例的初始态） */
async function freshWithStorage(): Promise<LocalWorkspaceModule> {
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

/** 读取 Rust workspace 模块源码（契约扫描用） */
function rustSource(): string {
  return readFileSync(
    fileURLToPath(new URL('../../../src-tauri/src/workspace/mod.rs', import.meta.url)),
    'utf8',
  );
}

beforeEach(() => {
  installStorage().clear();
});

// ── 契约面：与 Rust 源码同口径 ────────────────────────────────

describe('工作空间：错误码契约与 Rust 源码一致', () => {
  const src = rustSource();
  const emitted = [...new Set(src.match(/WORKSPACE_[A-Z][A-Z_]*/g) ?? [])].sort();

  it('Rust workspace 源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 有用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('本地通道抛出的码均在 ErrorCode 枚举中登记', async () => {
    const m = await freshWithStorage();
    const msg = expectCode(() => m.normalizeWorkspaceId('含中文'), ErrorCode.WORKSPACE_INVALID_ID);
    expect(msg.split(':')[0]).toBe(ErrorCode.WORKSPACE_INVALID_ID);
  });

  it('字符集/长度上限与 Rust 校验口径一致', () => {
    expect(src).toContain('const MAX_ID_LEN: usize = 64;');
    expect(src).toContain('c.is_ascii_lowercase() || c.is_ascii_digit() || c == \'_\' || c == \'-\'');
    expect(src).toContain("id.contains('.') || id.contains('/') || id.contains('\\\\')");
  });
});

// ── 标识校验 ──────────────────────────────────────────────────

describe('normalizeWorkspaceId：合法标识归一', () => {
  it.each([
    ['Report-2026', 'report-2026'],
    ['  default  ', 'default'],
    ['a_b-C9', 'a_b-c9'],
  ])('%s → %s', async (raw, expected) => {
    const m = await freshWithStorage();
    expect(m.normalizeWorkspaceId(raw)).toBe(expected);
  });

  it.each([
    ['空串', ''],
    ['纯空白', '   '],
    ['null', null],
    ['undefined', undefined],
  ])('拒绝空标识（%s）', async (_label, raw) => {
    const m = await freshWithStorage();
    expectCode(() => m.normalizeWorkspaceId(raw), 'WORKSPACE_INVALID_ID');
  });

  it.each([
    ['路径穿越', '../escape'],
    ['含斜杠', 'a/b'],
    ['含反斜杠', 'a\\b'],
    ['含点号', 'a.b'],
    ['纯点号', '.'],
    ['含空格', 'a b'],
    ['含大写符号', 'a@b'],
    ['含中文', '工作区'],
    ['超长 65 字符', 'x'.repeat(65)],
  ])('拒绝非法标识（%s）', async (_label, raw) => {
    const m = await freshWithStorage();
    expectCode(() => m.normalizeWorkspaceId(raw), 'WORKSPACE_INVALID_ID');
  });

  it('64 字符边界通过', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeWorkspaceId('x'.repeat(64))).toBe('x'.repeat(64));
  });
});

// ── 列表：default 恒在 + 未落地语义 ──────────────────────────

describe('listLocalWorkspaces：诚实空态', () => {
  it('空台账仍返回 default，且标记未落地', async () => {
    const m = await freshWithStorage();
    const list = m.listLocalWorkspaces();
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      id: 'default',
      name: 'default',
      exists: false,
      running: false,
      status: 'stopped',
    });
  });

  it('创建后按标识字典序排列，default 不再重复', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('zeta', 2);
    m.createLocalWorkspace('alpha', 1);
    const ids = m.listLocalWorkspaces().map((w) => w.id);
    expect(ids).toEqual(['default', 'alpha', 'zeta']);
  });

  it('显式创建的 default 覆盖未落地占位，exists 为 true', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('default', 7);
    const list = m.listLocalWorkspaces();
    expect(list.filter((w) => w.id === 'default')).toHaveLength(1);
    expect(list[0]).toMatchObject({ id: 'default', exists: true, createdAtMs: 7 });
  });

  it('运行态如实为 stopped（浏览器通道无 Agent 子进程可查）', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('ops');
    for (const w of m.listLocalWorkspaces()) {
      expect(w.running).toBe(false);
      expect(w.status).toBe('stopped');
    }
  });
});

// ── 创建：幂等 + 持久化 ───────────────────────────────────────

describe('createLocalWorkspace：幂等与落盘', () => {
  it('返回的视图指向本地工作区目录', async () => {
    const m = await freshWithStorage();
    const ws = m.createLocalWorkspace('report', 1000);
    expect(ws).toEqual({
      id: 'report',
      name: 'report',
      exists: true,
      createdAtMs: 1000,
      entryCount: 0,
      rootPath: '运行目录/workspaces/report',
      running: false,
      status: 'stopped',
    });
  });

  it('重复创建返回现状，不改写时间戳也不追加行', async () => {
    const m = await freshWithStorage();
    const first = m.createLocalWorkspace('report', 1000);
    const second = m.createLocalWorkspace('report', 9999);
    expect(second.createdAtMs).toBe(first.createdAtMs);
    expect(JSON.parse(currentStore.get(KEY)!)).toHaveLength(1);
  });

  it('非法标识创建即抛错，台账与存储均不变', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('alpha', 1);
    const snapshot = currentStore.get(KEY);
    expectCode(() => m.createLocalWorkspace('../escape'), 'WORKSPACE_INVALID_ID');
    expect(m.listLocalWorkspaces().map((w) => w.id)).toEqual(['default', 'alpha']);
    expect(currentStore.get(KEY)).toBe(snapshot);
  });

  it('创建结果写入 localStorage，重载后 hydrate 恢复', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('ops', 5);
    const reloaded = await freshModule();
    expect(reloaded.listLocalWorkspaces().map((w) => w.id)).toEqual(['default', 'ops']);
  });
});

// ── 通道分发器 ────────────────────────────────────────────────

describe('handleLocalWorkspaceChannel：路由与兜底', () => {
  it('workspace:list 命中并返回数组', async () => {
    const m = await freshWithStorage();
    const res = m.handleLocalWorkspaceChannel('workspace:list', []);
    expect(res.handled).toBe(true);
    expect(res.value).toBeInstanceOf(Array);
    expect((res.value as unknown[]).length).toBeGreaterThan(0);
  });

  it('workspace:create 读取 workspaceId 载荷', async () => {
    const m = await freshWithStorage();
    const res = m.handleLocalWorkspaceChannel('workspace:create', [{ workspaceId: 'Beta' }]);
    expect(res.handled).toBe(true);
    expect((res.value as { id: string }).id).toBe('beta');
  });

  it('workspace:create 兼容 id 别名字段', async () => {
    const m = await freshWithStorage();
    const res = m.handleLocalWorkspaceChannel('workspace:create', [{ id: 'gamma' }]);
    expect((res.value as { id: string }).id).toBe('gamma');
  });

  it('无载荷时不误判为已处理成功', async () => {
    const m = await freshWithStorage();
    expectCode(
      () => m.handleLocalWorkspaceChannel('workspace:create', []),
      'WORKSPACE_INVALID_ID',
    );
  });

  it('未命中通道返回 handled=false，交由其他通道处理', async () => {
    const m = await freshWithStorage();
    expect(m.handleLocalWorkspaceChannel('schedule:list', [])).toEqual({ handled: false });
    expect(m.handleLocalWorkspaceChannel('workspace:delete', [])).toEqual({ handled: false });
  });
});

// ── 存储不可用 / 复位 ─────────────────────────────────────────

describe('存储降级与复位', () => {
  it('无 localStorage 时仍可用内存台账工作', async () => {
    uninstallStorage();
    const m = await freshModule();
    expect(m.createLocalWorkspace('mem', 1).id).toBe('mem');
    expect(m.listLocalWorkspaces().map((w) => w.id)).toEqual(['default', 'mem']);
  });

  it('损坏的持久化数据降级为空台账，不抛错', async () => {
    const store = installStorage();
    store.set(KEY, '{not-json');
    const m = await freshModule();
    expect(m.listLocalWorkspaces().map((w) => w.id)).toEqual(['default']);
  });

  it('非数组与缺字段行被过滤，保留可用行', async () => {
    const store = installStorage();
    store.set(KEY, JSON.stringify([{ id: 'ok', createdAtMs: 3 }, { id: '' }, null]));
    const m = await freshModule();
    const ids = m.listLocalWorkspaces().map((w) => w.id);
    expect(ids).toContain('ok');
    expect(ids).not.toContain('');
  });

  it('对象形态的 createdAtMs 归一为数字（缺省 0）', async () => {
    const store = installStorage();
    store.set(KEY, JSON.stringify([{ id: 'legacy' }]));
    const m = await freshModule();
    const row = m.listLocalWorkspaces().find((w) => w.id === 'legacy');
    expect(row?.createdAtMs).toBe(0);
  });

  it('reset 清空台账并移除存储键（不写入 null 残留）', async () => {
    const m = await freshWithStorage();
    m.createLocalWorkspace('ops', 1);
    m.resetLocalWorkspaces();
    expect(m.listLocalWorkspaces().map((w) => w.id)).toEqual(['default']);
    expect(currentStore.has(KEY)).toBe(false);
  });
});
