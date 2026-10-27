/**
 * 品牌外观契约测试
 *
 * 需求：默认名称改为「任我行」（署名：北京通天云计算有限公司），
 * 安装后允许在设置里上传 PNG 或改名称，二者可在同一次提交里完成，
 * 成功即用掉唯一一次机会 —— 之后应用内不再提供第二次修改。
 *
 * 边界：
 * - 只有桌面通道能改窗口标题、任务栏与托盘图标；窗口、浏览器通道如实禁用；
 * - Mock 通道只改内存，`applied` 全 false 并在 warnings 里说明，不假装改了系统图标；
 * - 快捷方式与应用文件名由安装包决定，运行期改不了，界面必须写明；
 * - 失败不静默：名称/图标校验错误带 BRAND_* 错误码，部分目标失败进 warnings。
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const tauriConf = () => JSON.parse(read('../../../src-tauri/tauri.conf.json'));
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const brandingApi = () => read('../../../src/renderer/lib/branding-api.ts');
const brandingStore = () => read('../../../src/renderer/stores/useBrandingStore.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const rustBranding = () => read('../../../src-tauri/src/system/branding.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('默认品牌：任我行 / 北京通天云计算有限公司', () => {
  it('安装包与应用内标题使用新默认名，可执行文件名保持 ASCII', () => {
    const conf = tauriConf();
    expect(conf.productName).toBe('任我行');
    // 二进制名保持 ASCII：开机自启、current_exe 与既有测试都依赖它
    expect(conf.mainBinaryName).toBe('tongtian-tauri');
    expect(conf.app.windows[0].title).toBe('任我行');
    expect(conf.bundle.publisher).toBe('北京通天云计算有限公司');
    expect(conf.bundle.copyright).toContain('北京通天云计算有限公司');
    expect(read('../../../src/renderer/index.html')).toContain('<title>任我行</title>');
  });

  it('前端默认值、Mock 与 Rust 三处默认名一致（不靠界面文案单点决定）', () => {
    expect(brandingApi()).toContain("defaultName: '任我行'");
    expect(brandingApi()).toContain("attribution: '北京通天云计算有限公司'");
    expect(mockApi()).toContain("defaultName: '任我行'");
    expect(mockApi()).toContain("attribution: '北京通天云计算有限公司'");
    expect(rustBranding()).toContain('DEFAULT_NAME: &str = "任我行"');
    expect(rustBranding()).toContain('ATTRIBUTION: &str = "北京通天云计算有限公司"');
  });
});

describe('IPC 两条通道都接通：contracts → tauri-api / mock → Rust command', () => {
  it('通道常量与前端消费方', () => {
    expect(contracts()).toContain("BRANDING_GET: 'branding:get'");
    expect(contracts()).toContain("BRANDING_CUSTOMIZE: 'branding:customize'");
    expect(tauriApi()).toContain("channel === 'branding:get'");
    expect(tauriApi()).toContain("invoke('branding_customize', { req: args[0] })");
    expect(mockApi()).toContain("channel.startsWith('branding:')");
  });

  it('Rust 侧注册命令并在启动时恢复自定义品牌', () => {
    const lib = rustLib();
    expect(lib).toContain('system::branding::branding_get');
    expect(lib).toContain('system::branding::branding_customize');
    // 重启后窗口标题/图标必须按落库值恢复，而不是只改当次内存
    expect(lib).toContain('branding::apply_startup');
    expect(rustBranding()).toContain('pub fn apply_startup');
  });
});

describe('branding-api：无桌面能力时回退默认值且明确拒绝修改', () => {
  it('没有 tongtianAPI 时返回默认品牌，platform=browser', async () => {
    const mod = await import('../../../src/renderer/lib/branding-api');
    const info = await mod.getBranding();
    expect(info.name).toBe('任我行');
    expect(info.attribution).toBe('北京通天云计算有限公司');
    expect(info.platform).toBe('browser');
    expect(info.customized).toBe(false);
  });

  it('没有 tongtianAPI 时修改直接抛 BRAND_UNSUPPORTED，不做假成功', async () => {
    const mod = await import('../../../src/renderer/lib/branding-api');
    await expect(mod.customizeBranding({ name: '新名字' })).rejects.toThrow(/BRAND_UNSUPPORTED/);
  });

  it('读取失败回退默认值；非法 platform 按 browser 处理', async () => {
    const mod = await import('../../../src/renderer/lib/branding-api');
    vi.stubGlobal('window', {
      tongtianAPI: {
        invoke: async () => {
          throw new Error('IPC_FAIL');
        },
      },
    });
    expect((await mod.getBranding()).name).toBe('任我行');

    vi.stubGlobal('window', {
      tongtianAPI: {
        invoke: async () => ({ name: '   ', platform: 'phone', customized: 'yes' }),
      },
    });
    const info = await mod.getBranding();
    expect(info.name).toBe('任我行');
    expect(info.platform).toBe('browser');
    expect(info.customized).toBe(false);
  });

  it('归一化保留桌面返回的 applied / warnings，未知字段不进入状态', async () => {
    const mod = await import('../../../src/renderer/lib/branding-api');
    vi.stubGlobal('window', {
      tongtianAPI: {
        invoke: async () => ({
          name: '我的工作台',
          customized: true,
          customizedAt: 1700000000000,
          platform: 'desktop',
          applied: { windowTitle: true, windowIcon: true, trayIcon: false },
          warnings: ['托盘图标更新失败：托盘未创建', 42],
          junk: 'x',
        }),
      },
    });
    const info = await mod.getBranding();
    expect(info.name).toBe('我的工作台');
    expect(info.platform).toBe('desktop');
    expect(info.applied).toEqual({ windowTitle: true, windowIcon: true, trayIcon: false });
    expect(info.warnings).toEqual(['托盘图标更新失败：托盘未创建']);
    expect(info).not.toHaveProperty('junk');
  });
});

describe('Rust：单次修改机会与图标校验', () => {
  it('用时间戳行锁住修改机会，第二次直接报错', () => {
    const src = rustBranding();
    expect(src).toContain('KEY_CUSTOMIZED_AT: &str = "appBrandCustomizedAt"');
    expect(src).toContain('pub fn ensure_customizable');
    expect(src).toContain('BRAND_ALREADY_CUSTOMIZED');
    // 先校验、后落盘：已用过机会时不得先写图标文件
    const customize = src.slice(src.indexOf('pub fn branding_customize'));
    expect(customize.indexOf('ensure_customizable')).toBeGreaterThan(-1);
    expect(customize.indexOf('ensure_customizable')).toBeLessThan(customize.indexOf('save_icon'));
  });

  it('名称：去空白、1–24 字符、拒绝控制字符', () => {
    const src = rustBranding();
    expect(src).toContain('MAX_NAME_CHARS: usize = 24');
    expect(src).toContain('BRAND_NAME_EMPTY');
    expect(src).toContain('BRAND_NAME_TOO_LONG');
    expect(src).toContain('BRAND_NAME_INVALID');
  });

  it('图标只收 PNG，限制 4MB 与 16–1024 边长，并落地为临时文件后原子改名', () => {
    const src = rustBranding();
    expect(src).toContain('MAX_ICON_BYTES: usize = 4 * 1024 * 1024');
    expect(src).toContain('MIN_EDGE: u32 = 16');
    expect(src).toContain('MAX_EDGE: u32 = 1024');
    expect(src).toContain('BRAND_ICON_NOT_PNG');
    expect(src).toContain('BRAND_ICON_TOO_SMALL');
    expect(src).toContain('BRAND_ICON_TOO_LARGE_DIM');
    expect(src).toContain('with_extension("png.tmp")');
    expect(src).toContain('fs::rename');
  });

  it('生效位置：窗口标题、窗口/任务栏图标、托盘图标，失败写 warnings 不静默', () => {
    const src = rustBranding();
    expect(src).toContain('set_title');
    expect(src).toContain('set_icon');
    expect(src).toContain('tray_by_id("tongtian-main")');
    expect(src).toContain('Applied');
    expect(src).toContain('warnings');
  });
});

describe('Mock 通道：只改内存，不冒充系统图标已更新', () => {
  it('返回 platform=mock，applied 全 false 且带 Mock 说明', () => {
    const src = mockApi();
    expect(src).toContain("platform: 'mock'");
    expect(src).toContain('applied: { windowTitle: false, windowIcon: false, trayIcon: false }');
    expect(src).toContain('Mock 模式：修改只保存在浏览器内存，未写入窗口标题或系统图标');
  });

  it('同样只有一次机会，且空提交被拒绝', () => {
    const src = mockApi();
    expect(src).toContain('BRAND_ALREADY_CUSTOMIZED');
    expect(src).toContain('BRAND_NO_CHANGE');
    expect(src).toContain('customizedAt: Date.now()');
  });
});

describe('默认品牌图：安装包图标与应用内 Logo', () => {
  it('安装包图标清单使用由品牌图生成的多尺寸文件', () => {
    const icons = tauriConf().bundle.icon;
    for (const icon of [
      'icons/32x32.png',
      'icons/128x128.png',
      'icons/128x128@2x.png',
      'icons/icon.icns',
      'icons/icon.ico',
    ]) {
      expect(icons).toContain(icon);
    }
  });

  it('MSI 使用 zh-CN 代码页，避免中文产品名触发 WiX LGHT0311', () => {
    expect(tauriConf().bundle.windows.wix.language).toBe('zh-CN');
  });

  it('品牌图与 favicon 资源存在且文件头正确', () => {
    const logo = readFileSync(
      fileURLToPath(new URL('../../../src/renderer/assets/brand-logo.png', import.meta.url)),
    );
    expect([...logo.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    expect(logo.readUInt32BE(16)).toBe(512);
    expect(logo.readUInt32BE(20)).toBe(512);

    const ico = readFileSync(
      fileURLToPath(new URL('../../../src/renderer/public/favicon.ico', import.meta.url)),
    );
    expect([...ico.subarray(0, 4)]).toEqual([0x00, 0x00, 0x01, 0x00]);
    expect(ico.length).toBeGreaterThan(1000);
  });

  it('未自定义时侧边栏与设置页展示默认品牌图，而不是首字方块', () => {
    expect(sidebar()).toContain("import brandLogo from '../../assets/brand-logo.png';");
    expect(sidebar()).toContain('src={brandLogo}');
    expect(sidebar()).not.toContain('[...brandName][0]');
    expect(settingsPage()).toContain("import brandLogo from '../../assets/brand-logo.png';");
    expect(settingsPage()).toContain('src={brandLogo}');
    expect(settingsPage()).not.toContain('[...name][0]');
  });

  it('页面 favicon 指向随包 favicon.ico', () => {
    expect(read('../../../src/renderer/index.html')).toContain('href="./favicon.ico"');
  });
});

describe('设置页「品牌外观」分区', () => {
  it('侧边栏与设置页都从同一个 store 读品牌', () => {
    expect(sidebar()).toContain('useBrandingStore');
    expect(sidebar()).toContain('brandName');
    expect(sidebar()).toContain('brandIcon');
    expect(settingsPage()).toContain("{ id: 'branding', label: '品牌外观'");
    expect(settingsPage()).toContain("case 'branding':");
    expect(brandingStore()).toContain("info: { ...DEFAULT_BRANDING }");
  });

  it('提交前二次确认，文案写明唯一一次且不可恢复', () => {
    const src = settingsPage();
    expect(src).toContain('确认使用唯一的修改机会？');
    expect(src).toContain('保存后立即生效，且应用内不再提供第二次修改或恢复默认的入口。');
    expect(src).toContain('保存并应用（仅一次）');
    expect(src).toContain('已于 {time} 使用，应用内不再提供第二次修改');
  });

  it('如实说明生效范围：不改快捷方式与应用文件名', () => {
    expect(settingsPage()).toContain('已安装的桌面快捷方式名称与应用文件名不会改变。');
  });

  it('只接受 PNG，并提示尺寸与体积上限', () => {
    const src = settingsPage();
    expect(src).toContain("accept=\"image/png\"");
    expect(src).toContain('只支持 PNG 图片，其它格式请先转换');
    expect(src).toContain('PNG，16×16 至 1024×1024 像素，不超过 4 MB');
    expect(src).toContain('文件不是有效的 PNG 图片');
  });

  it('浏览器通道禁用修改入口，Mock 通道额外提示不落系统', () => {
    expect(settingsPage()).toContain("const canEdit = info.platform !== 'browser' && !info.customized;");
    expect(settingsPage()).toContain('当前通道没有桌面窗口与托盘，请在 Windows 桌面版中修改');
    expect(settingsPage()).toContain("info.platform === 'mock'");
  });

  it('失败走统一错误映射，不吞掉 BRAND_* 错误码', () => {
    expect(settingsPage()).toContain('errorTextOf(e,');
    expect(brandingApi()).toContain('IPC_CHANNELS.BRANDING_CUSTOMIZE');
  });
});
