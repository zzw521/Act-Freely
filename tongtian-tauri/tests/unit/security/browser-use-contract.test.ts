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

// ── D-36（#145）：引擎侧新工具与桌面壳网关的口径 ──────────────────────
// 这一组不测行为（行为在 tests/integration/engine_browser_selftest.py），
// 只钉「跨语言的两半现在到底认到哪儿」：引擎能说什么动作、网关认哪些路径、
// 哪些动作今天必须由能力位挡住。Rust 侧扩表时这里会红——那是要的：
// 红的时候两边一起看，而不是引擎悄悄注册出一个必被拒绝的按钮。

describe('D-36 引擎工具与桌面壳网关的动作口径', () => {
  /** Rust `is_known_action` 认识的动作名（网关 `/approve` 的白名单） */
  const rustKnownActions = (): string[] => {
    const src = rustBrowser();
    const start = src.indexOf('pub fn is_known_action');
    expect(start).toBeGreaterThanOrEqual(0);
    const body = src.slice(start, src.indexOf('\n}', start));
    return [...body.matchAll(/"([a-z]+)"/g)].map((m) => m[1] as string);
  };

  /** Rust `features()` 报给 runtime 的能力位 */
  const rustFeatureKeys = (): string[] => {
    const src = rustBrowser();
    const start = src.indexOf('pub fn features() -> Value');
    expect(start).toBeGreaterThanOrEqual(0);
    const body = src.slice(start, src.indexOf('\n}', start));
    return [...body.matchAll(/"([a-z]+)": true/g)].map((m) => m[1] as string);
  };

  /** 引擎实际向 `/approve` 报过的动作名 */
  const engineApproveActions = (): string[] =>
    [...engineBrowser().matchAll(/self\._approve\(\s*"([a-z]+)"/g)].map((m) => m[1] as string);

  /** 引擎里「只有网关认识才注册」的那几个能力位 */
  const engineGatedFeatures = (): string[] => {
    const src = engineBrowser();
    const start = src.indexOf('FEATURE_ONLY_ACTIONS = {');
    expect(start).toBeGreaterThanOrEqual(0);
    const body = src.slice(start, src.indexOf('\n}', start));
    return [...body.matchAll(/^\s{4}"([a-z]+)": \(/gm)].map((m) => m[1] as string);
  };

  it('Rust 白名单仍是那 7 个；引擎报出去的动作要么在白名单里，要么在显式门控清单里', () => {
    const known = [...new Set(rustKnownActions())].sort();
    expect(known).toEqual(['click', 'open', 'screenshot', 'scroll', 'select', 'snapshot', 'type']);

    const gated = [...new Set(engineGatedFeatures())].sort();
    expect(gated).toEqual(['evaluate', 'hover', 'press', 'tabs']);

    for (const action of new Set(engineApproveActions())) {
      expect(known.includes(action) || gated.includes(action), action).toBe(true);
    }
    // 门控清单成立的前提：这几个动作今天确实不在 Rust 白名单里。
    // Rust 补上任意一个时本用例应转红，并把对应的 `describe_action` 文案一起补上。
    for (const action of gated) {
      expect(known, action).not.toContain(action);
    }
  });

  it('能力位缺失 ⇒ 工具不注册：引擎用 has_feature 挡，界面画不出必被拒绝的按钮', () => {
    const feats = rustFeatureKeys();
    expect(feats).toContain('navigation');
    for (const name of engineGatedFeatures()) {
      expect(feats, name).not.toContain(name);
    }
    const engine = engineBrowser();
    for (const name of ['hover', 'press', 'evaluate', 'tabs']) {
      expect(engine).toContain(`if self.has_feature("${name}"):`);
    }
    expect(engine).toContain('桌面壳的浏览器网关还没有登记它们');
  });

  it('网关路径：ensure/approve/status 两侧同名；tabs 只有引擎在喊，Rust 没有这条路', () => {
    const gate = rustGate();
    expect(gate).toContain('"/browser/ensure" | "/browser/approve" | "/browser/status"');
    expect(gate).not.toContain('/browser/tabs');
    const engine = engineBrowser();
    expect(engine).toContain('self._post("/tabs"');
    expect(engine).toContain('桌面壳的浏览器网关没有实现标签页接口');
    expect(engine).toContain('引擎不会假装成功');
    // `/ensure` 会把 tabId 带出去，但桌面半边现在既不读它也不按它取地址
    // ⇒ 切换标签页必须复核 targetId 真的变了。
    expect(engine).toContain('payload["tabId"] = self._tab_id');
    expect(gate).not.toContain('tabId');
    expect(engine).toContain('切换标签页没有生效');
  });

  it('DOM 分级：四档与默认档在引擎，档位仍走网关认识的 snapshot 动作', () => {
    const engine = engineBrowser();
    const block = engine.match(/SNAPSHOT_LEVELS = \(([^)]*)\)/);
    expect(block).not.toBeNull();
    const levels = [...(block?.[1] ?? '').matchAll(/"([a-z]+)"/g)].map((m) => m[1] as string);
    expect(levels).toEqual(['minimal', 'interactive', 'structured', 'full']);
    expect(engine).toContain('async def browser_snapshot(level: str = "interactive"):');
    expect(engine).toContain('await self._approve("snapshot", target, f"档位 {lvl}"');
    // 每档预算全部由既有常量导出，分级不另立数字
    const profiles = engine.slice(engine.indexOf('SNAPSHOT_LEVEL_PROFILES'), engine.indexOf('\n}', engine.indexOf('SNAPSHOT_LEVEL_PROFILES')));
    expect(profiles).toContain('MAX_ELEMENTS');
    expect(profiles).toContain('MAX_PAGE_TEXT');
    expect(profiles).not.toMatch(/: \d{2,}/);
  });

  it('录制只写语义步骤：正文不落盘、草案走技能注册表、引擎不自己转正', () => {
    const engine = engineBrowser();
    expect(engine).toContain('step["hasValue"] = bool(value)');
    expect(engine).toContain('drafts.add_draft(');
    expect(engine).not.toContain('promote_draft');
    expect(engine).toContain('def _looks_secret');
    expect(engine).toContain('def _record(');
    expect(engine).toContain('def _step_value');
    for (const name of ['browser_wait', 'browser_record', 'browser_replay']) {
      expect(engine).toContain(`async def ${name}(`);
    }
    const install = engine.slice(engine.indexOf('def install(self, toolkit'), engine.indexOf('def _open_tool'));
    for (const name of ['_wait_tool', '_record_tool', '_replay_tool']) {
      expect(install).toContain(`self.${name}(emit, task_id)`);
    }
  });

  it('browser_wait 不问网关也不碰页面：只轮询自己的就绪状态', () => {
    const engine = engineBrowser();
    const start = engine.indexOf('async def browser_wait(');
    expect(start).toBeGreaterThan(0);
    const body = engine.slice(start, engine.indexOf('async def ', start + 10));
    expect(body).not.toContain('self._approve(');
    expect(body).not.toContain('self._post(');
    expect(engine).toContain('MAX_WAIT_SECONDS = 10.0');
    expect(body).toContain('self._current_page()');
  });

  it('回放复用已放行的动作：不新增动作名，也不绕过确认框', () => {
    const engine = engineBrowser();
    const start = engine.indexOf('async def browser_replay(');
    expect(start).toBeGreaterThan(0);
    const body = engine.slice(start);
    expect(body.slice(0, body.indexOf('return browser_replay'))).not.toContain('self._approve(');
    for (const name of ['self._open_tool', 'self._click_tool', 'self._type_tool', 'self._scroll_tool']) {
      expect(body).toContain(`${name}(emit, task_id)`);
    }
    expect(engine).toContain('def _step_value');
    expect(engine).toContain('不会代填');
  });
});
