/**
 * 系统权限区契约测试（任务 #30）
 *
 * 设置页「系统权限」原本是一块纯装饰：7 行状态写死为 已授权/未授权/无需授权，
 * 「去授权」Box 连 onClick 都没有 —— 用户看到的可信状态其实一个字都没问过系统。
 * 本文件从源码 + 行为两侧锁定这条链路为真：
 * - 权威表：Rust `security/permissions.rs` 与 `lib/permissions-local.ts` 同表同口径
 *   （同一个 key、同一条解释文案、同一套「当前平台有没有这个开关」的判定）
 * - 状态：只取系统能真实回答的探测（通知 / 麦克风），问不到的一律不谎报
 * - 动作：桌面版由 tauri-plugin-opener 拉起当前平台的隐私设置面板；
 *   浏览器/Mock/引擎直连如实抛 PERMISSION_UNSUPPORTED
 * - 契约：PERMISSION_* 全部有文案，两条通道都有消费方（不留只连不用）
 * - 页面：不再出现写死的状态数组，也不存在没有 onClick 的「去授权」
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';
import {
  CAPABILITIES,
  detectOs,
  handleLocalPermissionChannel,
  localSystemPermissionStatus,
  paneFor,
  permissionLabel,
  viewOf,
} from '../../../src/renderer/lib/permissions-local';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustPermissions = () => read('../../../src-tauri/src/security/permissions.rs');
const rustMod = () => read('../../../src-tauri/src/security/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const tsLocal = () => read('../../../src/renderer/lib/permissions-local.ts');
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

interface Row {
  key: string;
  label: string;
  purpose: string;
  probe: string | null;
  macos: string | null;
  windows: string | null;
  note: string;
}

/** Rust Capability {...} 字面量 → 行 */
function rustRows(): Row[] {
  const BLOCK =
    /Capability \{\s*key: "([^"]+)",\s*label: "([^"]+)",\s*purpose: "([^"]+)",\s*probe: (?:Some\(PROBE_([A-Z]+)\)|None),\s*macos_pane: (?:None|Some\("([^"]*)"\)),\s*windows_pane: (?:None|Some\("([^"]*)"\)),\s*no_pane_note: "([^"]*)",/g;
  return [...rustPermissions().matchAll(BLOCK)].map((m) => ({
    key: m[1],
    label: m[2],
    purpose: m[3],
    probe: m[4] ? m[4].toLowerCase() : null,
    macos: m[5] ?? null,
    windows: m[6] ?? null,
    note: m[7],
  }));
}

/** TS 表 → 行（同一份数据的镜像，用于逐字段比对） */
function tsRows(): Row[] {
  const BLOCK =
    /key: '([^']+)',\s*label: '([^']+)',\s*purpose: '([^']+)',\s*probe: (?:'([a-z]+)'|null),\s*macosPane: (?:'([^']+)'|null),\s*windowsPane: (?:'([^']+)'|null),\s*noPaneNote: '([^']+)'/g;
  return [...tsLocal().matchAll(BLOCK)].map((m) => ({
    key: m[1],
    label: m[2],
    purpose: m[3],
    probe: m[4] ?? null,
    macos: m[5] ?? null,
    windows: m[6] ?? null,
    note: m[7],
  }));
}

// ── 权威表：Rust 与本地实现同口径 ────────────────────────────────

describe('权限能力表 Rust / 本地同口径', () => {
  it('两侧都收录 7 项，键顺序一致', () => {
    expect(rustRows().map((r) => r.key)).toEqual(tsRows().map((r) => r.key));
    expect(CAPABILITIES.map((r) => r.key)).toEqual(tsRows().map((r) => r.key));
    expect(CAPABILITIES).toHaveLength(7);
  });

  it('逐字段一致：标签、用途、探测通道、双平台面板与解释文案', () => {
    const a = rustRows();
    const b = tsRows();
    expect(a).toHaveLength(b.length);
    a.forEach((row, i) => {
      expect(row).toEqual(b[i]);
    });
  });

  it('面板 URI 只有系统设置深链，不含外链', () => {
    for (const row of tsRows()) {
      for (const pane of [row.macos, row.windows]) {
        if (pane === null) continue;
        expect(pane.startsWith('ms-settings:') || pane.startsWith('x-apple.systempreferences:')).toBe(
          true,
        );
      }
    }
  });

  it('只有通知与麦克风声明可探测，其余不许谎称问得到', () => {
    expect(tsRows().filter((r) => r.probe).map((r) => r.key)).toEqual(['microphone', 'notifications']);
    expect(CAPABILITIES.filter((c) => c.probe).map((c) => c.key)).toEqual(['microphone', 'notifications']);
  });

  it('paneFor 与 Rust pane_for 同分支：macOS 全量、Windows 只给真实存在的开关', () => {
    const disk = CAPABILITIES.find((c) => c.key === 'disk')!;
    const mic = CAPABILITIES.find((c) => c.key === 'microphone')!;
    expect(paneFor('macos', disk)).toBe('x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles');
    expect(paneFor('windows', disk)).toBeNull();
    expect(paneFor('windows', mic)).toBe('ms-settings:privacy-microphone');
    expect(paneFor('linux', mic)).toBeNull();
    // Windows 上有面板的恰好三项，其余必须带如实解释
    const withPane = tsRows().filter((r) => r.windows).map((r) => r.key);
    expect(withPane).toEqual(['microphone', 'notifications', 'location']);
    expect(rustMod()).toMatch(/permissions::status\(std::env::consts::OS\)/);
  });

  it('viewOf：有面板时不塞解释，无面板时必须给解释', () => {
    const noPane = viewOf('windows', diskish());
    expect(noPane.pane).toBeNull();
    expect(noPane.note).toContain('Windows 无此开关');
    const withPane = viewOf('macos', micish());
    expect(withPane.pane).toContain('Privacy_Microphone');
    expect(withPane.note).toBeNull();
  });

  it('detectOs 与 Rust std::env::consts::OS 同标签', () => {
    expect(detectOs('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')).toBe('macos');
    expect(detectOs('Mozilla/5.0 (Windows NT 10.0; Win64; x64)')).toBe('windows');
    expect(detectOs('Mozilla/5.0 (X11; Linux x86_64)')).toBe('linux');
  });
});

function diskish() {
  return CAPABILITIES.find((c) => c.key === 'disk')!;
}
function micish() {
  return CAPABILITIES.find((c) => c.key === 'microphone')!;
}

// ── 错误码文案与通道消费方 ───────────────────────────────────────

describe('PERMISSION_* 契约收口', () => {
  it('Rust 与本地抛出的每个 PERMISSION_* 码都有用户文案，不裸奔', () => {
    const codes = new Set<string>();
    const src = rustPermissions() + tsLocal();
    for (const m of src.matchAll(/["'](PERMISSION_[A-Z_]+)/g)) codes.add(m[1]);
    expect([...codes].sort()).toEqual([
      'PERMISSION_KEY_UNKNOWN',
      'PERMISSION_OPEN_FAILED',
      'PERMISSION_PANE_UNAVAILABLE',
      'PERMISSION_UNSUPPORTED',
    ]);
    for (const code of codes) {
      expect(errorMessageOf(code)).not.toBe(`发生错误（${code}）`);
    }
  });

  it('ErrorCode 枚举收录 PERMISSION_UNSUPPORTED 且有文案', () => {
    expect(contracts()).toContain('PERMISSION_UNSUPPORTED = ');
    expect(errorMessageOf(ErrorCode.PERMISSION_UNSUPPORTED)).toContain('桌面版通天办公');
  });

  it('拒绝值按 errorTextOf 归一为「文案（详情）」', () => {
    expect(errorTextOf('PERMISSION_PANE_UNAVAILABLE: Windows 无此开关')).toContain(
      '当前系统没有这项权限的开关入口',
    );
    expect(errorTextOf(handleMessage())).toBe('当前运行通道无法打开系统隐私设置，请在桌面版通天办公中管理权限（浏览器通道无系统设置入口）');
  });

  it('两条通道都有消费方：IPC 常量 → tauri-api → Rust command 注册', () => {
    expect(IPC_CHANNELS.SYSTEM_PERMISSION_STATUS).toBe('system:permission:status');
    expect(IPC_CHANNELS.SYSTEM_PERMISSION_OPEN).toBe('system:permission:open');
    expect(tauriApi()).toContain("channel === 'system:permission:status'");
    expect(tauriApi()).toContain("invoke('system_permission_status')");
    expect(tauriApi()).toContain("invoke('system_permission_open', { key: req.key ?? '' })");
    expect(rustLib()).toContain('security::system_permission_status,');
    expect(rustLib()).toContain('security::system_permission_open,');
    expect(rustMod()).toContain('pub async fn system_permission_status');
    expect(rustMod()).toContain('pub async fn system_permission_open');
  });

  it('Mock 与引擎直连通道路由到本地实现，不落到「未知通道」', () => {
    expect(mockApi()).toContain('handleLocalPermissionChannel');
    expect(engineApi()).toContain('handleLocalPermissionChannel');
    expect(alignmentApi()).toContain('getSystemPermissionStatus');
    expect(alignmentApi()).toContain('openSystemPermissionSettings');
    expect(alignmentApi()).toContain('openSystemPermissionUnsupported');
  });
});

function handleMessage(): Error {
  try {
    handleLocalPermissionChannel(IPC_CHANNELS.SYSTEM_PERMISSION_OPEN, [{ key: 'microphone' }]);
  } catch (e) {
    return e as Error;
  }
  throw new Error('去授权在浏览器通道必须如实拒绝');
}

// ── 行为：本地通道 ──────────────────────────────────────────────

describe('本地通道行为', () => {
  it('status 通道返回清单且 openSupported=false（浏览器无系统设置入口）', async () => {
    const r = handleLocalPermissionChannel(IPC_CHANNELS.SYSTEM_PERMISSION_STATUS, []);
    expect(r.handled).toBe(true);
    const v = (await r.value) as Awaited<ReturnType<typeof localSystemPermissionStatus>>;
    expect(v.openSupported).toBe(false);
    expect(v.items).toHaveLength(7);
    expect(v.items.map((i) => i.key)).toEqual(CAPABILITIES.map((c) => c.key));
  });

  it('未收录的通道交回上游，不吞掉请求', () => {
    expect(handleLocalPermissionChannel('system:info', []).handled).toBe(false);
  });

  it('问不到状态时不进 states，页面才不会显示伪造的「已授权」', async () => {
    const v = await localSystemPermissionStatus();
    const probed = v.items.filter((i) => i.probe).map((i) => i.key);
    for (const key of Object.keys(v.states)) expect(probed).toContain(key);
    for (const item of v.items.filter((i) => !i.probe)) expect(v.states[item.key]).toBeUndefined();
  });

  it('浏览器通道没有落盘权限项：清单仍在，解释到位', async () => {
    const v = await localSystemPermissionStatus();
    expect(detectOs()).toBeTruthy();
    const allNoProbe = v.items.every((item) => (item.probe ? true : item.note !== null || item.pane !== null));
    expect(allNoProbe).toBe(true);
  });
});

// ── 页面：装饰已拆除 ────────────────────────────────────────────

describe('设置页系统权限区真实化', () => {
  const page = settingsPage;

  it('不再存在写死状态数组，也不再用 name 作 key', () => {
    expect(page()).not.toMatch(/status: '已授权'/);
    expect(page()).not.toMatch(/status: '无需授权'/);
    expect(page()).not.toMatch(/const permissions = \[/);
    expect(page()).not.toMatch(/key=\{p\.name\}/);
  });

  it('「去授权」是真实动作：onClick 触发 openSystemPermissionSettings', () => {
    expect(page()).toContain('<SystemPermissionSection />');
    expect(page()).toContain('onClick={() => void handleOpen(item)}');
    expect(page()).toContain('await openSystemPermissionSettings(item.key)');
    expect(page()).toMatch(/const SystemPermissionSection: React\.FC = \(\) => \{[\s\S]*?getSystemPermissionStatus\(\)/);
  });

  it('每个「去授权」文案都挂在可点击节点上', () => {
    const src = page();
    const clickable = src.indexOf('onClick={() => void handleOpen(item)}');
    const label = src.indexOf("'去授权'");
    expect(clickable).toBeGreaterThan(-1);
    expect(label).toBeGreaterThan(clickable);
    const between = src.slice(clickable, label);
    // 同一段按钮内：只有样式与「打开中…」态，没有跳到别的组件
    expect(between).toContain("bgcolor: 'var(--tt-text-1)'");
    expect(between).toContain("pending === item.key ? '打开中…'");
    expect(between).not.toContain('<SettingRow');
  });

  it('问不到时显示解释而不是彩色状态；失败与提示可见', () => {
    expect(page()).toContain('需在系统设置中管理');
    expect(page()).toContain('正在读取系统权限状态…');
    expect(page()).toContain('重新读取');
    expect(page()).toContain('errorTextOf(e, `${item.label}：授权入口打开失败`)');
    expect(page()).toContain('浏览器预览通道');
  });

  it('状态文案表只保留三个真实态，不再有「无需授权」这种含糊档', () => {
    expect(permissionLabel('granted')).toBe('已授权');
    expect(permissionLabel('denied')).toBe('已拒绝');
    expect(permissionLabel('prompt')).toBe('待授权');
    expect(permissionLabel('unknown')).toBeNull();
  });
});

// ── Mock 分发路径（window.tongtianAPI → alignment-api） ─────────

describe('Mock 分发路径', () => {
  type PermApi = typeof import('../../../src/renderer/lib/alignment-api');
  let mod: PermApi;

  beforeAll(async () => {
    (globalThis as Record<string, unknown>).window =
      (globalThis as Record<string, unknown>).window ?? {};
    await import('../../../src/renderer/lib/mock-api');
    mod = await import('../../../src/renderer/lib/alignment-api');
  });

  it('invoke 经 Mock 拿到清单，openSupported 不被桌面分支覆盖成 true', async () => {
    const v = await mod.getSystemPermissionStatus();
    expect(v.openSupported).toBe(false);
    expect(v.items.length).toBe(7);
  });

  it('去授权在 Mock 通道抛 PERMISSION_UNSUPPORTED，页面拿到可读文案', async () => {
    await expect(mod.openSystemPermissionSettings('microphone')).rejects.toThrow(/PERMISSION_UNSUPPORTED/);
    try {
      await mod.openSystemPermissionSettings('microphone');
    } catch (e) {
      expect(errorTextOf(e)).toContain('请在桌面版通天办公中管理权限');
    }
  });
});
