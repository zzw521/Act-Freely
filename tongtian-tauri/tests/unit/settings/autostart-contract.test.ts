/**
 * 开机自启动契约测试（任务 #39）
 *
 * 这一行的原文案写着「登录电脑时自动启动通天办公（真实生效）」，但代码里只有
 * `updateAppSettings({ autoStart })` 往 settings_kv 写一个布尔值：没有消费方，
 * 系统启动项从来没被写过 —— 假承诺比装饰控件更糟。本文件锁定：
 * - Rust：system::autostart 以注册表为事实来源，写入后回读校验，非 Windows 报 UNSUPPORTED
 * - 联动：autoStart 落库即触发启动项写入/删除（与 keepAwake 同一套 side effect）
 * - 通道：Tauri 映射 / Mock 与浏览器如实 supported=false / 契约类型三栏（意愿、事实、路径）
 * - 页面：开关显示系统事实，读取失败与写入失败都可见，不支持时置灰而非可点
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAuto = () => read('../../../src-tauri/src/system/autostart.rs');
const rustMod = () => read('../../../src-tauri/src/system/mod.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

// ── Rust 权威实现 ────────────────────────────────────────────────

describe('Rust 开机自启动实现', () => {
  it('启动项按值名操作，绝不整键覆盖或删除（会连带干掉其他软件的启动项）', () => {
    const src = rustAuto();
    expect(src).toContain('pub const VALUE_NAME: &str = "TongTianOffice"');
    expect(src).toContain('r"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"');
    const args = src.split('pub fn reg_args')[1].split('\n}\n')[0];
    expect(args).toContain('"/v".into()');
    expect(args).toContain('VALUE_NAME');
    // 参数里出现通配即等于批量删除，源码层面先禁掉（真实 reg 调用留给手工验证）
    expect(args).not.toContain('*');
  });

  it('写入后回读校验，不一致即报错（不把 reg 的退出码 0 当成生效）', () => {
    const apply = rustAuto().split('pub fn apply(enabled: bool)')[1].split('\n}\n')[0];
    expect(apply).toContain('let now = registered()?;');
    expect(apply).toContain('AUTOSTART_FAILED: 启动项写入后回读不到，未确认生效');
    expect(apply).toContain('启动项未能删除');
    expect(apply).toContain('启动项已存在但指向其他程序');
  });

  it('回读解析按整段值名匹配，中文/空格路径都能取回', () => {
    const parse = rustAuto().split('pub fn parse_query')[1].split('\n}\n')[0];
    expect(parse).toContain('is_char_boundary');
    expect(parse).toContain('rest.find("REG_SZ")');
  });

  it('非 Windows 开启如实报 UNSUPPORTED，关闭按既成事实放行', () => {
    const apply = rustAuto().split('pub fn apply(enabled: bool)')[1].split('\n}\n')[0];
    expect(apply).toContain('if !enabled {');
    expect(apply).toContain('AUTOSTART_UNSUPPORTED');
  });

  it('autoStart 写入即联动启动项，命令已注册（设置项不再是写空的布尔）', () => {
    const lib = rustLib();
    expect(lib).toContain('system::autostart::SETTING_KEY => system::autostart::apply(bool_of_setting(value)),');
    expect(lib).toContain('system::system_autostart_status,');
    expect(lib).toContain('mod system;');
    expect(rustMod()).toContain('pub async fn system_autostart_status');
    expect(rustMod()).toContain('crate::setting_bool(&p0, autostart::SETTING_KEY)');
  });

  it('状态视图同时给意愿与事实（界面才能指出两者不一致）', () => {
    const status = rustAuto().split('pub fn status()')[1].split('\n}\n')[0];
    for (const key of ['supported', 'enabled', 'command', 'currentCommand', 'stale', 'valueName', 'runKey']) {
      expect(status).toContain(`"${key}"`);
    }
    // reg 对缺失值给退出码 1（个别版本 2），属正常的「未开启」，不能报成读取失败
    expect(rustAuto()).toContain('code_means_absent(Action::Query, code) => Ok(None)');
    expect(rustAuto()).toContain('code_means_absent(Action::Delete, c)');
  });
});

// ── 契约与三通道 ─────────────────────────────────────────────────

describe('契约常量、类型与通道', () => {
  it('通道常量存在且有真实消费方（不留死常量）', () => {
    expect(IPC_CHANNELS.SYSTEM_AUTOSTART_STATUS).toBe('system:autostart');
    expect(tauriApi()).toContain("if (channel === 'system:autostart') return invoke('system_autostart_status');");
    expect(alignmentApi()).toContain('IPC_CHANNELS.SYSTEM_AUTOSTART_STATUS');
    expect(mockApi()).toContain("if (channel === 'system:autostart') return localAutostart(mockSettings);");
  });

  it('契约类型带意愿/事实/路径三栏', () => {
    const src = contracts();
    expect(src).toContain('export interface AutostartStatus');
    const iface = src.split('export interface AutostartStatus')[1].split('\n}')[0];
    for (const field of ['supported: boolean', 'requested: boolean', 'enabled: boolean', 'command: string | null', 'currentCommand: string', 'stale: boolean']) {
      expect(iface).toContain(field);
    }
  });

  it('两个错误码都有登记与可读文案（裸码不丢给用户）', () => {
    for (const code of [ErrorCode.AUTOSTART_FAILED, ErrorCode.AUTOSTART_UNSUPPORTED]) {
      expect(Object.values(ErrorCode)).toContain(code);
      expect(errorMessageOf(code)).not.toContain('发生错误（');
    }
    expect(errorTextOf('AUTOSTART_FAILED: 启动项写入后回读不到')).toContain('开机自启动');
    expect(errorTextOf('AUTOSTART_UNSUPPORTED: 当前平台暂不支持')).toContain('系统设置');
  });

  it('API 封装读取失败不静默降级成「未开启」', () => {
    const src = alignmentApi();
    const fn = src.split('export async function getAutostartStatus')[1].split('\n}\n')[0];
    expect(src).toContain('import { localAutostart } from');
    expect(fn).toContain('throw e instanceof Error ? e :');
    // 只有拿不到桌面通道（Mock/引擎直连）才按本地事实推导
    expect(fn).toContain('return localAutostart(await getSettings());');
  });
});

// ── 页面真实消费 ─────────────────────────────────────────────────

describe('设置页开机自启动真实化', () => {
  it('「（真实生效）」的假承诺已从该行移除', () => {
    const src = settingsPage();
    expect(src).not.toContain('登录电脑时自动启动通天办公（真实生效）');
    expect(src).toContain('const AutoStartSetting: React.FC = () => {');
    expect(src).toContain('<AutoStartSetting />');
  });

  it('挂载即读系统事实，开关绑的是 enabled 而非本地勾选', () => {
    const src = settingsPage();
    const comp = src.split('const AutoStartSetting')[1].split('const SystemPanel')[0];
    expect(comp).toContain('setStatus(await getAutostartStatus());');
    expect(comp).toContain('void load();');
    expect(comp).toContain('checked={enabled}');
    expect(comp).toContain('const enabled = Boolean(status?.enabled);');
  });

  it('保存走 strict 并回读，失败可见；不支持时置灰而不是可点', () => {
    const comp = settingsPage().split('const AutoStartSetting')[1].split('const SystemPanel')[0];
    expect(comp).toContain("await setSettings({ autoStart: enabled }, { strict: true });");
    expect(comp).toContain('await load();');
    expect(comp).toContain("errorTextOf(e, enabled ? '开机自启动未能写入' : '开机自启动未能取消')");
    expect(comp).toContain('disabled={!supported || saving}');
  });

  it('状态文案覆盖四种真实情形（旧路径 / 意愿与事实不符 / 读取失败 / 未支持）', () => {
    const comp = settingsPage().split('const AutoStartSetting')[1].split('const SystemPanel')[0];
    expect(comp).toContain('启动项仍指向旧路径');
    expect(comp).toContain('设置里开着，但系统启动项里没有本程序');
    expect(comp).toContain('正在读取系统启动项…');
    expect(comp).toContain('当前环境无法管理系统启动项，请在桌面版中设置');
  });
});

// ── 行为：本地通道如实反映能力边界 ───────────────────────────────

async function loadLocal() {
  vi.resetModules();
  return import('../../../src/renderer/lib/system-local');
}

describe('system-local：浏览器/Mock 态的自启动事实', () => {
  let localAutostart: (input: Record<string, unknown>) => import('../../../src/shared/contracts').AutostartStatus;

  beforeEach(async () => {
    localAutostart = (await loadLocal()).localAutostart;
  });

  it('勾选过也只报 requested，enabled 恒为 false（不把意愿当生效）', () => {
    const on = localAutostart({ autoStart: true });
    expect(on).toMatchObject({ supported: false, requested: true, enabled: false, stale: false });
    expect(on.command).toBeNull();
  });

  it('裸字符串 "true" 与 JSON 布尔同口径（Rust settings_kv 的历史存法）', () => {
    expect(localAutostart({ autoStart: '"true"' }).requested).toBe(true);
    expect(localAutostart({ autoStart: 'false' }).requested).toBe(false);
    expect(localAutostart({}).requested).toBe(false);
  });

  it('常量与 Rust 侧一致（界面显示的是同一处启动项位置）', async () => {
    const mod = await loadLocal();
    const rust = rustAuto();
    expect(mod.AUTOSTART_VALUE_NAME).toBe('TongTianOffice');
    expect(rust).toContain(`"${mod.AUTOSTART_VALUE_NAME}"`);
    // Rust 用 raw string 存同一处路径（单反斜杠），两边必须逐字一致
    expect(rust).toContain(`r"${mod.AUTOSTART_RUN_KEY}"`);
  });
});
