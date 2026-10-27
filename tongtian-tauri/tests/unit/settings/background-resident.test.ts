/**
 * 后台常驻契约测试（P0 常驻服务化第一阶段）
 *
 * 目标：关闭主窗口后进程继续运行（托盘驻留），定时任务与进行中的 Agent 不中断。
 * 关键边界：
 * - 只有「设置开启 + 托盘真实创建成功 + 非主动退出」同时成立才隐藏/拦截退出，
 *   托盘失败时维持原行为（关窗退出），不把窗口藏进没有入口的进程；
 * - 托盘「退出」是显式退出（quitting 置位 + code=Some），不会被驻留逻辑拦回来；
 * - 开机自启在后台常驻开启时带 --hidden，并把参数差异计入 stale；
 * - 设置页以托盘事实决定开关能否操作，浏览器/Mock 如实报不支持。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustBackground = () => read('../../../src-tauri/src/background.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAuto = () => read('../../../src-tauri/src/system/autostart.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const settingsStore = () => read('../../../src/renderer/lib/app-settings-store.ts');
const settingsHook = () => read('../../../src/renderer/lib/use-app-settings.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

describe('Rust：关窗驻留决策', () => {
  it('三个条件同时成立才隐藏，托盘失败或主动退出都不拦', () => {
    const src = rustBackground();
    expect(src).toContain('pub fn should_hide_on_close(enabled: bool, tray_ready: bool, quitting: bool) -> bool');
    expect(src).toContain('enabled && tray_ready && !quitting');
    expect(src).toContain('pub fn should_prevent_exit(');
    // 显式退出（code=Some）必须放行，否则托盘「退出」关不掉应用
    expect(src).toContain('enabled && tray_ready && !quitting && code.is_none()');
  });

  it('--hidden 只做整段匹配，启动命令只在后台常驻时追加', () => {
    const src = rustBackground();
    expect(src).toContain('a.as_ref() == HIDDEN_ARG');
    expect(src).toContain('pub const HIDDEN_ARG: &str = "--hidden"');
    expect(src).toContain('format!("{exe} {HIDDEN_ARG}")');
  });

  it('进程级状态默认「托盘未就绪、未在退出」', () => {
    const src = rustBackground();
    expect(src).toContain('pub fn mark_tray_ready(&self)');
    expect(src).toContain('pub fn request_quit(&self)');
    // 托盘未建成时默认 false，是「关窗照旧退出」的兜底
    expect(src).toContain('tray_ready: AtomicBool');
    expect(src).toContain('quitting: AtomicBool');
  });
});

describe('Rust：托盘接管与退出路径', () => {
  it('托盘创建成功才置 ready；失败只告警，不进入常驻', () => {
    const lib = rustLib();
    const setup = lib.split('fn setup_background')[1].split('\n}\n\n/// 从托盘回到主窗口')[0];
    expect(setup).toContain('bg.mark_tray_ready();');
    expect(setup).toContain('托盘创建失败，关闭窗口仍将退出');
    // mark 只能出现在成功分支里：先 match 结果再置位
    const branches = setup.split('match result {')[1];
    const okBranch = branches.split('Err(e) =>')[0];
    const errBranch = branches.split('Err(e) =>')[1].split('// 关窗隐藏')[0];
    expect(okBranch).toContain('bg.mark_tray_ready();');
    expect(errBranch).not.toContain('mark_tray_ready');
    expect(setup).toContain('builder.build(app)?;');
  });

  it('托盘菜单提供显示与退出，退出先置位再 app.exit', () => {
    const lib = rustLib();
    const setup = lib.split('fn setup_background')[1].split('\n}\n\n/// 从托盘回到主窗口')[0];
    expect(setup).toContain('MenuItem::with_id(app, "show", "显示主窗口"');
    expect(setup).toContain('MenuItem::with_id(app, "quit", "退出应用"');
    expect(setup).toContain('quit_state.request_quit();');
    expect(setup).toContain('app.exit(0);');
    // 左键单击回到主窗口，右键才出菜单
    expect(setup).toContain('show_menu_on_left_click(false)');
    expect(setup).toContain('show_main_window(tray.app_handle())');
  });

  it('关窗拦截走 CloseRequested，且隐藏前再次核对托盘事实', () => {
    const lib = rustLib();
    const setup = lib.split('fn setup_background')[1].split('\n}\n\n/// 从托盘回到主窗口')[0];
    expect(setup).toContain('tauri::WindowEvent::CloseRequested { api, .. }');
    expect(setup).toContain('background::should_hide_on_close(enabled, state.tray_ready(), state.is_quitting())');
    expect(setup).toContain('api.prevent_close();');
    expect(setup).toContain('w.hide();');
  });

  it('最后窗口关闭触发 ExitRequested：仅驻留时拦截，显式退出放行', () => {
    const lib = rustLib();
    const runFn = lib.split('.run(|handle, event| match event {')[1].split('});')[0];
    expect(runFn).toContain('tauri::RunEvent::ExitRequested { code, api, .. }');
    expect(runFn).toContain('background::should_prevent_exit(enabled, ready, quitting, code)');
    expect(runFn).toContain('api.prevent_exit();');
    // 退出时仍释放唤醒约束，避免遗留孤儿进程
    expect(runFn).toContain('services.keep_awake.stop();');
  });

  it('system_info 暴露托盘事实，设置页才有据可依', () => {
    const lib = rustLib();
    expect(lib).toContain('"backgroundResident": bg.tray_ready(),');
    expect(lib).toContain('background::BackgroundState');
  });

  it('--hidden 启动时仅在「自带参数 + 设置开启 + 托盘就绪」后隐藏主窗口', () => {
    const lib = rustLib();
    const setup = lib.split('fn setup_background')[1].split('\n}\n\n/// 从托盘回到主窗口')[0];
    expect(setup).toContain('background::has_hidden_arg(std::env::args())');
    expect(setup).toContain('setting_bool(services, background::SETTING_KEY)');
    expect(setup).toContain('window.hide();');
  });
});

describe('Rust：开机自启与后台常驻联动', () => {
  it('自启命令带 --hidden，且参数差异计入 stale', () => {
    const src = rustAuto();
    expect(src).toContain('pub fn launch_command_for(background: bool)');
    expect(src).toContain('crate::background::command_for(&exe, background)');
    expect(src).toContain('启动项参数未更新');
    expect(src).toContain('let expected = launch_command_for(background);');
  });

  it('runInBackground 写入后重写已开启的启动项（否则仍是弹窗启动）', () => {
    const lib = rustLib();
    expect(lib).toContain('background::SETTING_KEY => {');
    expect(lib).toContain('if setting_bool(p0, system::autostart::SETTING_KEY) {');
    expect(lib).toContain('system::autostart::apply(true, bool_of_setting(value))?;');
  });
});

describe('前端：设置契约与界面事实', () => {
  it('设置类型与默认值都有 runInBackground', () => {
    expect(contracts()).toContain('runInBackground: boolean;');
    expect(alignmentApi()).toContain('runInBackground: false,');
  });

  it('设置页读取托盘事实，开关不可用时置灰并说明原因', () => {
    const src = settingsPage();
    expect(src).toContain('const BackgroundResidentSetting: React.FC = () => {');
    expect(src).toContain('<BackgroundResidentSetting />');
    const comp = src.split('const BackgroundResidentSetting')[1].split('const NotificationSettings')[0];
    expect(comp).toContain('systemAPI');
    expect(comp).toContain('.info()');
    expect(comp).toContain('Boolean(info.backgroundResident)');
    expect(comp).toContain('当前环境没有可用的系统托盘，关闭窗口仍会退出程序');
    expect(comp).toContain('disabled={!supported || saving}');
    // strict 保存失败要可见：不能让开关停在「开了但关窗照样退出」
    expect(comp).toContain('updateAppSettings({ runInBackground: enabled }, { strict: true })');
    expect(comp).toContain("errorTextOf(e, enabled ? '后台常驻未能开启' : '后台常驻未能关闭')");
  });

  it('strict 写入失败会回滚缓存（避免假开启）', () => {
    const src = settingsStore();
    const fn = src.split('export async function updateAppSettings')[1].split('\n}\n')[0];
    expect(fn).toContain('const previous = cache;');
    expect(fn).toContain('throw e;');
    expect(fn).toContain('delete next[key];');
  });

  it('hook 暴露 strict 选项，调用方可等待落盘结果', () => {
    const src = settingsHook();
    expect(src).toContain('options?: { strict?: boolean }');
    expect(src).toContain('Promise<void>');
  });

  it('浏览器/Mock 如实报 backgroundResident=false，不假装能常驻', () => {
    expect(mockApi()).toContain('backgroundResident: false,');
  });
});
