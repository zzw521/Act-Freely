/**
 * 专家套件 · 浏览器通道本地台账单元测试（任务 #37）
 *
 * 浏览器通道写不了磁盘，但同一个 .zip 包要能真解析、真记账、真回读，
 * 因此用例覆盖：台账增删查、重复安装拒绝（不覆盖既有套件）、损坏 JSON 降级、
 * 存储不可用时的如实失败，以及通道分发器的路由与 supported=false 声明。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';
import { bytesToBase64Native, demoPackage } from './zip-fixture';

type LocalSuiteModule = typeof import('../../../src/renderer/lib/suite-local');

const LEDGER = 'tt:suites';
let store: Map<string, string>;

/** Map 版 localStorage 垫片（vitest 环境为 node，无原生实现） */
function installStorage(): Map<string, string> {
  const s = new Map<string, string>();
  const shim = {
    getItem: (k: string) => (s.has(k) ? s.get(k)! : null),
    setItem: (k: string, v: string) => void s.set(k, String(v)),
    removeItem: (k: string) => void s.delete(k),
    clear: () => s.clear(),
    key: (i: number) => [...s.keys()][i] ?? null,
    get length() {
      return s.size;
    },
  };
  (globalThis as Record<string, unknown>).localStorage = shim;
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};
  store = s;
  return s;
}

async function freshModule(): Promise<LocalSuiteModule> {
  installStorage();
  vi.resetModules();
  return import('../../../src/renderer/lib/suite-local');
}

let mod: LocalSuiteModule;

beforeEach(async () => {
  mod = await freshModule();
});

// ── 通道分发器 ─────────────────────────────────────────────

describe('handleLocalSuiteChannel · 路由与如实降级', () => {
  it('未登记通道 handled=false，交回上层继续路由', () => {
    expect(mod.handleLocalSuiteChannel('chat:history', [])).toEqual({ handled: false });
  });

  it('suite:root 声明不支持落盘，且不编造运行目录', () => {
    const res = mod.handleLocalSuiteChannel('suite:root', []);
    expect(res.handled).toBe(true);
    expect(res.value).toEqual({ supported: false, suitesRoot: null, trashRoot: null });
  });

  it('空台账时 suite:list 返回空数组（页面据此渲染真实空态）', () => {
    expect(mod.handleLocalSuiteChannel('suite:list', [])).toEqual({ handled: true, value: [] });
  });

  it('suite:install 的 value 是 Promise，卸载结果同步返回', async () => {
    const { zipBase64 } = demoPackage();
    const res = mod.handleLocalSuiteChannel('suite:install', [{ zipBase64 }]);
    expect(res.handled).toBe(true);
    await expect(res.value as Promise<unknown>).resolves.toMatchObject({ id: 'admin-assist' });
    const gone = mod.handleLocalSuiteChannel('suite:uninstall', [{ suiteId: 'admin-assist' }]);
    expect(gone.value).toMatchObject({ ok: true, removed: 'admin-assist' });
  });

  it('卸载入参兼容 suiteId 与 id 两种写法', async () => {
    const { zipBase64 } = demoPackage();
    await (mod.handleLocalSuiteChannel('suite:install', [{ zipBase64 }]).value as Promise<unknown>);
    expect(
      mod.handleLocalSuiteChannel('suite:uninstall', [{ id: 'admin-assist' }]).value,
    ).toMatchObject({ removed: 'admin-assist' });
  });
});

// ── 台账：安装 → 回读 → 卸载 ───────────────────────────────

describe('installLocalSuite · 解析即记账', () => {
  it('安装后台账保存的是解析出来的真实内容，标识与桌面端同源', async () => {
    const { zipBase64 } = demoPackage('供应链协同套件');
    const row = await mod.installLocalSuite({ fileName: 'supply.zip', zipBase64 });
    expect(row).toMatchObject({
      id: 'suite-5b56b7ed',
      name: '供应链协同套件',
      version: 'v1.4.0',
      author: '@行政百宝箱',
      category: '行政',
      skillCount: 1,
      dataLinkCount: 1,
      fileCount: 4, // 包内 3 个文件 + 归一 manifest.json
      path: 'local://suites/suite-5b56b7ed',
      readable: true,
    });
    expect(row.commands.map((c) => c.name)).toEqual(['meeting-minutes', 'notice-draft']);
    expect(row.installedAtMs).toBeGreaterThan(0);
    // 台账只存归一清单，不含包内文件字节（浏览器通道不写盘，也不谎称写盘）
    const [recorded] = JSON.parse(store.get(LEDGER)!);
    expect(Object.keys(recorded).sort()).toEqual(
      [
        'author',
        'category',
        'commands',
        'dataLinkCount',
        'description',
        'enabled',
        'fileCount',
        'id',
        'installedAtMs',
        'name',
        'path',
        'readable',
        'skillCount',
        'version',
      ].sort(),
    );
    expect(zipBase64.length).toBeGreaterThan(JSON.stringify(recorded).length);
  });

  it('空载荷拒绝，不会先记账再报错', async () => {
    await expect(mod.installLocalSuite({ fileName: 'x.zip' })).rejects.toThrowError(
      /^SUITE_INCOMPATIBLE/,
    );
    expect(mod.listLocalSuites()).toEqual([]);
  });

  it('非法包同样不留半成品条目', async () => {
    await expect(
      mod.installLocalSuite({ zipBase64: bytesToBase64Native(new TextEncoder().encode('not-zip')) }),
    ).rejects.toThrowError(/^SUITE_INCOMPATIBLE/);
    expect(mod.listLocalSuites()).toEqual([]);
  });

  it('同名套件重复安装被拒，既有台账不被覆盖', async () => {
    const { zipBase64 } = demoPackage();
    const first = await mod.installLocalSuite({ zipBase64 });
    await expect(mod.installLocalSuite({ zipBase64 })).rejects.toThrowError(
      /^SUITE_INSTALL_FAILED: 套件「/,
    );
    const rows = mod.listLocalSuites();
    expect(rows).toHaveLength(1);
    expect(rows[0].installedAtMs).toBe(first.installedAtMs);
  });

  it('按安装时间倒序返回（与 Rust list_in 同序）', async () => {
    const a = await mod.installLocalSuite({ zipBase64: demoPackage('alpha-suite').zipBase64 });
    const b = await mod.installLocalSuite({ zipBase64: demoPackage('beta-suite').zipBase64 });
    a.installedAtMs = 1;
    b.installedAtMs = 2;
    store.set(LEDGER, JSON.stringify([a, b]));
    expect(mod.listLocalSuites().map((s) => s.id)).toEqual(['beta-suite', 'alpha-suite']);
  });

  it('data URL 前缀的 base64 可直接安装（FileReader 产物）', async () => {
    const row = await mod.installLocalSuite({
      zipBase64: `data:application/zip;base64,${demoPackage().zipBase64}`,
    });
    expect(row.id).toBe('admin-assist');
  });
});

describe('uninstallLocalSuite · 只移除不越界', () => {
  it('卸载后台账只剩其他套件', async () => {
    await mod.installLocalSuite({ zipBase64: demoPackage('alpha-suite').zipBase64 });
    await mod.installLocalSuite({ zipBase64: demoPackage('beta-suite').zipBase64 });
    const res = mod.uninstallLocalSuite('alpha-suite');
    expect(res).toEqual({
      ok: true,
      removed: 'alpha-suite',
      trashPath: 'local://suites_removed/alpha-suite',
    });
    expect(mod.listLocalSuites().map((s) => s.id)).toEqual(['beta-suite']);
  });

  it('未知标识与空标识报 SUITE_NOT_FOUND，台账不动', async () => {
    await mod.installLocalSuite({ zipBase64: demoPackage().zipBase64 });
    for (const bad of ['ghost', '', '   ']) {
      expect(() => mod.uninstallLocalSuite(bad)).toThrowError(/^SUITE_NOT_FOUND/);
    }
    expect(mod.listLocalSuites()).toHaveLength(1);
  });
});

// ── 存储异常：不静默降级成「已安装」 ───────────────────────

describe('localStorage 异常', () => {
  it('台账 JSON 损坏时视为空台账，读取不阻断页面', () => {
    store.set(LEDGER, '{不是数组');
    expect(mod.listLocalSuites()).toEqual([]);
  });

  it('写入被拒（配额/禁用）时报 SUITE_INSTALL_FAILED，不谎称已安装', async () => {
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
    };
    vi.resetModules();
    const broken = await import('../../../src/renderer/lib/suite-local');
    await expect(broken.installLocalSuite({ zipBase64: demoPackage().zipBase64 })).rejects.toThrowError(
      /^SUITE_INSTALL_FAILED: 套件台账写入失败/,
    );
  });

  it('无 localStorage 可用时读取降级为空，安装如实失败', async () => {
    delete (globalThis as Record<string, unknown>).localStorage;
    vi.resetModules();
    const none = await import('../../../src/renderer/lib/suite-local');
    expect(none.listLocalSuites()).toEqual([]);
    await expect(none.installLocalSuite({ zipBase64: demoPackage().zipBase64 })).rejects.toThrowError(
      /^SUITE_INSTALL_FAILED/,
    );
  });
});

// ── 错误码文案登记 ─────────────────────────────────────────

describe('套件错误码不得裸奔', () => {
  const codes = [
    ErrorCode.SUITE_NOT_FOUND,
    ErrorCode.SUITE_INCOMPATIBLE,
    ErrorCode.SUITE_INSTALL_FAILED,
  ];

  it.each(codes.map((c) => [c, c]))('%s 已登记用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('detail 拼接后仍可读，指引动作在文案里', () => {
    const text = errorTextOf('SUITE_INCOMPATIBLE: 套件包缺少 plugin.json，应位于根目录或单层目录内');
    expect(text).toContain(errorMessageOf(ErrorCode.SUITE_INCOMPATIBLE));
    expect(text).toContain('plugin.json');
    expect(errorMessageOf(ErrorCode.SUITE_INSTALL_FAILED)).toContain('写入权限');
    expect(errorMessageOf(ErrorCode.SUITE_NOT_FOUND)).toContain('刷新');
  });
});
