/**
 * 装饰控件与版本号真实化（任务 #43）
 *
 * 这一批处理的是「看起来能点、其实什么都不做」和「写死在界面上的假事实」：
 * - 更新页写死 v0.1.0 (BETA) + 一个没有 onClick 的「检查更新」按钮
 * - 侧边栏一个渐变高亮、没有任何点击行为的「升级」胶囊
 * - 幻灯片模板列出三个「内置」模板（应用里没有这些文件）+ 一个假导入框
 * - 实验特性里「运行修复」按钮点了没反应
 * - 同一应用在 mock 通道报 0.1.0、engine 通道报 0.3.0
 * 现在：版本号向 Rust system_info 要（浏览器通道用构建期注入值），
 * 其余要么接真实动作，要么如实显示为不可操作并说明原因。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const settings = () => read('../../../src/renderer/pages/Settings/index.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const store = () => read('../../../src/renderer/store/workbenchStore.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const viteCfg = () => read('../../../vite.config.ts');
const appVersionSrc = () => read('../../../src/shared/app-version.ts');
const rustLib = () => read('../../../src-tauri/src/lib.rs');

describe('版本号只有一个来源', () => {
  it('桌面通道由 Rust 报 CARGO_PKG_VERSION（不是前端字符串）', () => {
    expect(rustLib()).toContain('"version": env!("CARGO_PKG_VERSION")');
  });

  it('浏览器两条通道改用构建期注入值，写死的 0.1.0 / 0.3.0 已清除', () => {
    expect(mockApi()).toContain("'system:info': {");
    expect(mockApi()).toContain("name: 'tongtian-tauri'");
    expect(mockApi()).toContain('version: appVersion()');
    expect(engineApi()).toContain('version: appVersion()');
    expect(mockApi()).not.toContain("version: '0.1.0'");
    expect(engineApi()).not.toContain("version: '0.3.0'");
    expect(viteCfg()).toContain('define: { __TT_APP_VERSION__: JSON.stringify(pkgVersion) }');
    expect(viteCfg()).toContain("readFileSync(path.resolve(__dirname, 'package.json')");
  });

  it('非 vite 环境（node 单测）不编一个像真的版本号', async () => {
    vi.resetModules();
    const mod = await import('../../../src/shared/app-version');
    expect(mod.appVersion()).toBe('dev（未经 vite 构建注入）');
    expect(appVersionSrc()).toContain('typeof __TT_APP_VERSION__ ===');
  });
});

describe('更新页不再有假按钮与写死版本', () => {
  it('写死的 v0.1.0 (BETA) 与无 onClick 的「检查更新」已移除', () => {
    const src = settings();
    expect(src).not.toContain('v0.1.0 (BETA)');
    expect(src).not.toContain('>检查更新<');
    expect(src).toContain('const UpdatePanel: React.FC = () => {');
  });

  it('挂载即向 system_info 要真实版本，读失败可见；无更新渠道如实说明', () => {
    const panel = settings().split('const UpdatePanel')[1].split('分区九：工作台')[0];
    expect(panel).toContain('await systemAPI.info()');
    expect(panel).toContain("errorTextOf(e, '应用信息读取失败')");
    expect(panel).toContain('正在读取应用信息…');
    expect(panel).toContain('应用未报告版本号');
    expect(panel).toContain('未配置更新渠道');
    expect(panel).toContain('获取新版安装包（NSIS setup.exe 或 MSI）后直接覆盖安装');
  });
});

describe('侧边栏与模板区不留诱导性假控件', () => {
  it('「升级」胶囊已删除，改为显示真实版本号', () => {
    const src = sidebar();
    expect(src).not.toContain('>升级<');
    expect(src).not.toContain('linear-gradient(120deg, #6366F1 0%, #8B5CF6 100%)');
    expect(src).toContain('await systemAPI.info()');
    expect(src).toContain('{appVersion ? `v${appVersion}` : ');
    // 读不到时不回落写死字符串
    expect(src).toContain('不回落一个写死的字符串');
  });

  it('三个假「内置」模板与假导入框已换成真实模板台账', () => {
    const src = settings();
    expect(src).not.toContain('商务蓝（内置）');
    expect(src).not.toContain('+ 导入自定义模板');
    // 真实实现：台账列表 + 选中 + 系统文件对话框导入 + 回收目录删除
    expect(src).toContain('const PptxTemplatesSection: React.FC');
    expect(src).toContain('usePptxTemplates()');
    expect(src).toContain('data-pptx-template={tpl.id}');
    expect(src).toContain('导入 .pptx');
    expect(src).toContain('浏览器直连没有本机文件访问能力，请在桌面版导入模板');
  });
});

describe('运行修复接上真实动作', () => {
  it('按钮真的调用 store 重建，并有确认、结果与失败提示', () => {
    const row = settings().split('const RepairHistoryRow')[1].split('分区十：安全工作环境')[0];
    expect(row).toContain('useWorkbenchStore.getState().repairConversationDisplay()');
    expect(row).toContain('setOpen(true)');
    expect(row).toContain('<Dialog open={open}');
    expect(row).toContain('已按正文库重建 ${r.rebuilt} 个会话');
    expect(row).toContain("errorTextOf(e, '历史消息显示修复失败')");
    expect(row).toContain('cursor: running ?');
  });

  it('store 的重建不丢数据：运行中与未落库的会话都保留，读库失败抛真实错误码', () => {
    const fn = store().split('repairConversationDisplay: async () =>')[1].split('\n  };')[0];
    expect(fn).toContain('fetchConversations(200)');
    expect(fn).toContain('CONVERSATION_STORE_UNAVAILABLE');
    expect(fn).toContain('if (existing?.isLoading)');
    expect(fn).toContain('keptUnsaved += 1;');
    expect(fn).toContain('if (!turns.some((t) => t.userContent)) continue;');
  });
});
