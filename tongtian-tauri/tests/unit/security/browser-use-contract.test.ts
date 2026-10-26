/**
 * 浏览器 AI+ 契约测试（#74）
 *
 * 锁定五件事：
 * - 通道名、Tauri 适配、Rust invoke_handler 三处一致；
 * - runtime 下发 browserUse 段，token 只交给本机引擎；
 * - 引擎只在就绪时注册工具，子 Agent 不继承浏览器控制能力；
 * - Rust/引擎抛出的 BROWSER_USE_* 码都有中文文案；
 * - 浏览器/Mock 通道如实返回「没有受控浏览器」，启动不会被假报成功；
 * - 页面消费真实能力视图并保留审计/边界说明。
 */

import { beforeAll, beforeEach, describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustBrowser = () => read('../../../src-tauri/src/browser/mod.rs');
const rustGate = () => read('../../../src-tauri/src/bridge/browser_gate.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const alignmentLocal = () => read('../../../src/renderer/lib/alignment-local.ts');
const panel = () => read('../../../src/renderer/pages/BrowserUsePanel/index.tsx');
const engineServer = () => read('../../../engine/server.py');
const engineBrowser = () => read('../../../engine/browser_use.py');
const appRoutes = () => read('../../../src/renderer/App.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

describe('浏览器 AI+ 通道与 command 接线', () => {
  it('通道名固定', () => {
    expect(IPC_CHANNELS.BROWSER_USE_GET_POLICY).toBe('browserUse:getPolicy');
    expect(IPC_CHANNELS.BROWSER_USE_SET_POLICY).toBe('browserUse:setPolicy');
    expect(IPC_CHANNELS.BROWSER_USE_CAPABILITIES).toBe('browserUse:capabilities');
    expect(IPC_CHANNELS.BROWSER_USE_START).toBe('browserUse:start');
    expect(IPC_CHANNELS.BROWSER_USE_STOP).toBe('browserUse:stop');
  });

  it('Tauri 适配映射到真实 command，Rust 已注册', () => {
    const api = tauriApi();
    for (const [channel, command] of [
      ['browserUse:getPolicy', 'browser_use_get_policy'],
      ['browserUse:capabilities', 'browser_use_capabilities'],
      ['browserUse:start', 'browser_use_start'],
      ['browserUse:stop', 'browser_use_stop'],
    ]) {
      expect(api).toContain(`channel === '${channel}'`);
      expect(api).toContain(`invoke('${command}')`);
      expect(rustLib()).toContain(`browser::${command}`);
    }
    expect(api).toContain("channel === 'browserUse:setPolicy'");
    expect(api).toContain("invoke('browser_use_set_policy'");
  });

  it('runtime 下发 browserUse 段，token 只在可用时出现', () => {
    const runtime = rustRuntime();
    expect(runtime).toContain('browser_use_block(store)');
    expect(runtime).toContain('gateToken');
    // 渲染层没有拿 token 的通道：能力视图只回 gate 布尔值
    expect(rustBrowser()).not.toContain('"gateToken"');
    expect(rustBrowser()).toContain('"gate": gate');
  });

  it('执行能力收进设置，旧路由重定向且侧边栏不再单列', () => {
    expect(appRoutes()).toContain('<Route path="/browser-use" element={<Navigate to="/settings?s=browser-use" replace />} />');
    expect(sidebar()).not.toContain("path: '/browser-use'");
    const settings = settingsPage();
    expect(settings).toContain("{ id: 'browser-use', label: '浏览器 AI+'");
    expect(settings).toContain('return <BrowserUsePanel embedded />;');
  });

  it('引擎工具与放行网关成对出现，子 Agent 不继承', () => {
    const server = engineServer();
    expect(server).toContain('bu.BrowserUse.from_runtime');
    const engine = engineBrowser();
    for (const tool of [
      'browser_open',
      'browser_snapshot',
      'browser_click',
      'browser_type',
      'browser_scroll',
      'browser_screenshot',
    ]) {
      expect(engine).toContain(`def ${tool}`);
    }
    expect(engine).toContain('x-tt-browser-token');
    const childCall = server.slice(
      server.indexOf('def _make_child_agent'),
      server.indexOf('subagent_runner = sar.SubagentRunner'),
    );
    expect(childCall).toContain('build_agent(');
    expect(childCall).not.toContain('browser=');
  });

  it('引擎侧也执行协议白名单与密码框拒绝', () => {
    const engine = engineBrowser();
    expect(engine).toContain('def normalize_url');
    expect(engine).toContain('只允许打开 http/https');
    expect(engine).toContain('密码框不通过浏览器 AI+ 输入');
    expect(engine).toContain('Page.setDownloadBehavior');
  });
});

describe('浏览器 AI+ 错误码都有中文文案', () => {
  it('Rust 执行器/网关抛出的 BROWSER_USE_* 全部可映射', () => {
    const codes = new Set<string>();
    for (const src of [rustBrowser(), rustGate()]) {
      for (const m of src.matchAll(/(?<=")BROWSER_USE_[A-Z][A-Z_]*/g)) codes.add(m[0]);
    }
    expect(codes.size).toBeGreaterThanOrEqual(9);
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text).not.toContain(code);
      expect(text.length).toBeGreaterThan(4);
    }
    expect(ErrorCode.BROWSER_USE_DENIED_BY_USER).toBe('BROWSER_USE_DENIED_BY_USER');
    expect(ErrorCode.BROWSER_USE_BLOCKED_SCHEME).toBe('BROWSER_USE_BLOCKED_SCHEME');
  });
});

describe('浏览器/Mock 通道的浏览器能力视图', () => {
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};

  type AlignmentApi = typeof import('../../../src/renderer/lib/alignment-api');
  let a: AlignmentApi;

  beforeAll(async () => {
    await import('../../../src/renderer/lib/mock-api');
    a = await import('../../../src/renderer/lib/alignment-api');
  });

  beforeEach(async () => {
    const local = await import('../../../src/renderer/lib/alignment-local');
    local.resetLocalAlignment();
  });

  it('能力视图如实为不可用，不用保存的档位冒充可启动', async () => {
    const caps = await a.getBrowserUseCapabilities();
    expect(caps.supported).toBe(false);
    expect(caps.platform).toBe('browser');
    expect(caps.browser).toBeNull();
    expect(caps.gate).toBe(false);
    expect(caps.running).toBe(false);
    expect(caps.policy).toBe('ask_every_time');
    expect(caps.persisted).toBe(false);
    expect(caps.reason).toContain('没有受控浏览器进程与本机网关');

    await a.setBrowserUsePolicy('auto');
    const after = await a.getBrowserUseCapabilities();
    expect(after.policy).toBe('auto');
    expect(after.persisted).toBe(true);
    expect(after.supported).toBe(false);
  });

  it('本地通道启动/结束如实报错，不假装成功', async () => {
    await expect(a.startBrowserUseSession()).rejects.toThrow(/BROWSER_USE_NOT_FOUND/);
    await expect(a.stopBrowserUseSession()).rejects.toThrow(/BROWSER_USE_STOP_FAILED/);
    expect(alignmentLocal()).toContain('startLocalBrowserUseSession');
  });

  it('非法档位与 Rust 同错误码', async () => {
    await expect(a.setBrowserUsePolicy('always' as never)).rejects.toThrow(
      /BROWSER_USE_INVALID_POLICY/,
    );
  });
});

describe('浏览器 AI+ 页面消费真实能力视图', () => {
  it('读取能力、写策略、启动/结束会话', () => {
    const src = panel();
    expect(src).toContain('getBrowserUseCapabilities');
    expect(src).toContain('setBrowserUsePolicy');
    expect(src).toContain('startBrowserUseSession');
    expect(src).toContain('stopBrowserUseSession');
    expect(src).toContain('data-testid="browser-use-start"');
    expect(src).toContain('data-testid="browser-use-stop"');
  });

  it('保留诚实边界：独立配置目录、协议白名单、审计脱敏、单任务上限', () => {
    const src = panel();
    expect(src).toContain('不读取你日常浏览器的 Cookie 与登录态');
    expect(src).toContain('只允许 http/https');
    expect(src).toContain('输入正文、页面正文与截图内容不写入审计');
    expect(src).toContain('操作 80 次、快照 40 次、截图 20 次');
    expect(src).toContain('子 Agent 不继承浏览器控制能力');
    expect(src).toContain('密码框不接受 AI 输入');
    expect(src).toContain('当前通道没有受控浏览器');
  });
});
