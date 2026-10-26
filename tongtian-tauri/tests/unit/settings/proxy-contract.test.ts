/**
 * 网络代理生效链路契约测试（任务 #28）
 *
 * 「网络代理」原本只有一个二档下拉框：选完把 proxyMode 写进 settings_kv，
 * 没有任何消费方 —— Rust 出网用默认 reqwest client，引擎子进程只继承父进程环境，
 * 而 proxyUrl/忽略主机连输入框都没有。本文件从源码 + 行为两侧锁定链路为真：
 * - Rust：proxy.rs 解析/校验/发布，settings 落库前整组校验（不留半个生效配置）
 * - 生效：新启动引擎注入 HTTP_PROXY/HTTPS_PROXY/NO_PROXY；Rust 外网按配置重建 client
 * - 直连：桌面端 ↔ 引擎（127.0.0.1）永远不走代理，代理配错也不会自断链路
 * - 契约：PROXY_* 全部有文案，proxy:status 三通道都有消费方
 * - 页面：设置页三档 + 地址/忽略主机输入 + 保存可见错误 + 待重启引擎如实提示
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';
import {
  PROXY_SETTING_KEYS,
  assertProxyPatchAcceptable,
  computeProxySpec,
  effectiveBypass,
  localProxyStatus,
  parseProxyBypass,
  parseProxyMode,
  proxyEnvOf,
  touchesProxySetting,
  validateProxyUrl,
} from '../../../src/renderer/lib/proxy-local';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustProxy = () => read('../../../src-tauri/src/proxy.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const rustModel = () => read('../../../src-tauri/src/model/service.rs');
const rustMcp = () => read('../../../src-tauri/src/mcp/mod.rs');
const rustAuth = () => read('../../../src-tauri/src/auth/service.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

/** proxy.rs / lib.rs 里抛出的 PROXY_* 码（lookbehind 避免把引号带进结果） */
function rustProxyCodes(): string[] {
  const CODE = /(?<=")PROXY_[A-Z][A-Z_]*/g;
  const found = new Set<string>();
  for (const m of rustProxy().matchAll(CODE)) found.add(m[0]);
  for (const m of rustLib().matchAll(CODE)) found.add(m[0]);
  return [...found].sort();
}

// ── Rust 配置层 ──────────────────────────────────────────────────

describe('Rust 代理配置层', () => {
  it('三档模式各有独立语义，且兼容中英文口径', () => {
    const src = rustProxy();
    expect(src).toContain('pub enum ProxyMode');
    for (const variant of ['System', 'Custom', 'Disabled']) {
      expect(src).toContain(variant);
    }
    expect(src).toContain('"自定义" | "custom" | "Custom" | "手动" => ProxyMode::Custom');
    expect(src).toContain('ProxyMode::Disabled');
  });

  it('地址校验要求协议与主机，且只放行 reqwest 支持的协议', () => {
    const src = rustProxy();
    expect(src).contains('"http" | "https" | "socks5" | "socks5h" | "socks4" | "socks"');
    expect(src).toContain('PROXY_URL_REQUIRED');
    expect(src).toContain('PROXY_INVALID_URL');
  });

  it('本地回环无条件直连，避免代理配错把桌面端与引擎的链路切断', () => {
    const src = rustProxy();
    expect(src).toContain('pub const ALWAYS_BYPASS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];');
    expect(src).toContain('pub fn effective_bypass(&self) -> Vec<String>');
    expect(src).toContain('chain(ALWAYS_BYPASS)');
  });

  it('Custom 关闭系统代理自动探测，只走用户填写的那一个', () => {
    const src = rustProxy();
    // builder.no_proxy() 关掉自动探测，再挂用户代理，避免两个代理叠加
    expect(src).toContain('ProxyMode::Disabled => Ok(builder.no_proxy()),');
    expect(src).toContain('Ok(builder.no_proxy().proxy(proxy))');
    expect(src).toContain('reqwest::NoProxy::from_string');
  });

  it('client 按指纹缓存复用，并且返回所有权（设置变更后可替换）', () => {
    const src = rustProxy();
    expect(src).toContain('pub fn client_for(spec: &ProxySpec) -> Result<reqwest::Client, String>');
    expect(src).toContain('if *cached == fingerprint {');
    expect(src).toContain('PROXY_CLIENT_BUILD_FAILED');
    // 旧实现是 OnceLock + &'static，设置变更后无法替换
    expect(src).not.toContain("-> &'static reqwest::Client");
  });

  it('本机引擎通信有独立的直连 client，不受代理设置影响', () => {
    const src = rustProxy();
    expect(src).toContain('pub fn loopback_client() -> reqwest::Client');
    const agent = rustAgent();
    expect(agent).toContain('fn http_client() -> reqwest::Client {\n    crate::proxy::loopback_client()');
    expect(agent).toContain('http: crate::proxy::loopback_client(),');
  });
});

// ── 生效链路 ─────────────────────────────────────────────────────

describe('代理设置真实生效链路', () => {
  it('批量保存在落库前按「补丁叠加现值」整组校验，单键写入口已删除', () => {
    // 纯校验函数在 proxy.rs（可单测），调用点是唯一的批量写入口
    expect(rustProxy()).toContain(
      'pub fn spec_from_patch(store: &Store, patch: &serde_json::Map<String, serde_json::Value>) -> Result<(), String>',
    );
    const src = rustLib();
    expect(src).toContain('proxy::spec_from_patch(&p0.store, obj)?;');
    // 设置页只走批量 patch；没有调用方的单键 settings_set 已删除，避免出现第二条未接线写入口
    expect(src).not.toContain('proxy::spec_from_patch(&p0.store, &probe)?;');
    expect(src).not.toContain('async fn settings_set(');
    expect(src.indexOf('proxy::spec_from_patch(&p0.store, obj)?;')).toBeLessThan(
      src.indexOf('store::settings::set(&p0.store, k, &store::settings::encode_value(v))?;'),
    );
  });

  it('保存成功后整组发布，读取失败也不阻断（缺省跟随系统）', () => {
    const src = rustLib();
    expect(src).toContain('let spec = proxy::spec(&p0.store)?;');
    expect(src).toContain('proxy::publish(spec);');
    expect(src).toContain('proxy::SETTING_KEY_MODE | proxy::SETTING_KEY_URL | proxy::SETTING_KEY_BYPASS => {');
    expect(src).toContain('proxy::publish(proxy::spec_or_system(&services.store));');
  });

  it('启动新引擎时注入代理环境变量，并记录当时的配置指纹', () => {
    const src = rustAgent();
    expect(src).toContain('let proxy_spec = crate::proxy::current();');
    expect(src).toContain('.envs(proxy_spec.env_vars())');
    expect(src).toContain('proxy_fingerprint: proxy_spec.fingerprint(),');
    expect(src).toContain('    proxy_fingerprint: String,');
    expect(src).toContain('pub async fn proxy_stale_instances(bridge: &BridgeState) -> usize');
  });

  it('外网出网改用 proxy::client()（模型连通性 / 远端 MCP / 云端账号）', () => {
    expect(rustModel()).toContain('let client = crate::proxy::client();');
    // MCP 同时服务远端与本机端点：远端走代理，回环必须直连（系统代理常不 bypass 回环）
    expect(rustMcp()).toContain(
      'let client = if is_loopback_url(&job.url) {\n        crate::proxy::loopback_client()\n    } else {\n        crate::proxy::client()\n    };',
    );
    expect(rustAuth()).toContain('crate::proxy::client()');
    // 账号侧不应再有绕过代理配置的裸 client
    expect(rustAuth()).not.toContain('reqwest::Client::new()');
  });

  it('proxy_status 命令注册可调用，返回生效配置与待重启实例数', () => {
    const src = rustLib();
    expect(src).toContain('async fn proxy_status(');
    expect(src).toContain('"staleEngines": stale,');
    expect(src).toContain('"effectiveBypass": spec.effective_bypass(),');
    expect(src).toMatch(/invoke_handler\(tauri::generate_handler![\s\S]*proxy_status,[\s\S]*\)/);
  });
});

// ── 契约常量与三通道 ─────────────────────────────────────────────

describe('代理契约与三通道', () => {
  it('Rust 抛出的每个 PROXY_* 码都有对应文案', () => {
    for (const code of rustProxyCodes()) {
      expect(errorMessageOf(code), `${code} 缺文案`).not.toBe(code);
    }
    expect(rustProxyCodes().length).toBeGreaterThanOrEqual(3);
  });

  it('错误文本按首个冒号切分：文案 + Rust 侧诊断详情', () => {
    expect(errorTextOf('PROXY_URL_REQUIRED: 未填写代理地址', '兜底')).toBe(
      '选择「自定义」代理时需要填写代理地址，例如 http://127.0.0.1:7890（未填写代理地址）',
    );
    expect(errorTextOf(new Error('PROXY_INVALID_URL: 不支持的代理协议 ftp'), '兜底')).toBe(
      '代理地址不合法，需带协议与主机端口，例如 http://127.0.0.1:7890（不支持的代理协议 ftp）',
    );
    // 只有码没有详情时不追加空括号
    expect(errorTextOf('PROXY_URL_REQUIRED', '兜底')).toBe('选择「自定义」代理时需要填写代理地址，例如 http://127.0.0.1:7890');
  });

  it('代理三键进契约类型，默认值与 Rust 缺省模式一致', () => {
    const src = contracts();
    for (const key of ['proxyMode', 'proxyUrl', 'proxyBypass']) {
      expect(src).toContain(`  ${key}:`);
    }
    expect(src).toContain("PROXY_STATUS: 'proxy:status',");
    expect(src).toContain('export interface ProxyStatus');
    expect(src).toContain('staleEngines: number;');
    expect(alignmentApi()).toContain("proxyMode: '跟随系统',");
    expect(alignmentApi()).toContain("proxyUrl: '',");
    expect(alignmentApi()).toContain("proxyBypass: '',");
  });

  it('Tauri 通道映射到真实 command，浏览器通道走本地推导', () => {
    expect(tauriApi()).toContain("if (channel === 'proxy:status') return invoke('proxy_status');");
    expect(alignmentApi()).toContain('export async function getProxyStatus(): Promise<ProxyStatus>');
    expect(alignmentApi()).toContain('api.invoke(IPC_CHANNELS.PROXY_STATUS)');
    expect(alignmentApi()).toContain('return localProxyStatus(await getSettings());');
    expect(mockApi()).toContain("if (channel === 'proxy:status') return localProxyStatus(mockSettings);");
  });

  it('strict 保存让 Rust 的校验错误可见（默认口径仍为兼容引擎直连而吞异常）', () => {
    const src = alignmentApi();
    expect(src).toContain('options?: { strict?: boolean }');
    expect(src).toContain('if (options?.strict) throw e;');
    expect(settingsPage()).toContain('{ strict: true }');
  });
});

// ── 本地权威实现（Mock / 浏览器）与 Rust 同表 ─────────────────────

describe('proxy-local 与 Rust 同口径', () => {
  it('模式归一：中英文与空值都落到三档', () => {
    expect(parseProxyMode('自定义')).toBe('自定义');
    expect(parseProxyMode(' custom ')).toBe('自定义');
    expect(parseProxyMode('手动')).toBe('自定义');
    expect(parseProxyMode('不使用')).toBe('不使用');
    expect(parseProxyMode('直连')).toBe('不使用');
    expect(parseProxyMode('跟随系统')).toBe('跟随系统');
    expect(parseProxyMode(undefined)).toBe('跟随系统');
  });

  it('地址校验拒绝缺协议/空主机/不支持协议，并归一去尾斜杠', () => {
    expect(validateProxyUrl(' http://127.0.0.1:7890/ ')).toEqual({ url: 'http://127.0.0.1:7890' });
    expect(validateProxyUrl('socks5://10.0.0.2:1080')).toEqual({ url: 'socks5://10.0.0.2:1080' });
    expect(validateProxyUrl('')?.error).toMatch(/^PROXY_URL_REQUIRED/);
    expect(validateProxyUrl('127.0.0.1:7890')?.error).toMatch(/^PROXY_INVALID_URL/);
    expect(validateProxyUrl('ftp://127.0.0.1:21')?.error).toMatch(/^PROXY_INVALID_URL/);
    expect(validateProxyUrl('http://')?.error).toMatch(/^PROXY_INVALID_URL/);
    expect(validateProxyUrl('http://a b')?.error).toMatch(/^PROXY_INVALID_URL/);
  });

  it('协议前缀合法但不可路由的地址同样拒绝（浏览器/Mock 通道不谎称生效）', () => {
    for (const raw of [
      'http://proxy:8080127.0.0.1:8080ftp://10.0.0.1:21',
      'http://proxy.example.com:8080/path',
      'http://proxy.example.com:notaport',
      'http://proxy.example.com:65536',
      'http://proxy.example.com:0',
      'http://proxy.example.com:',
      'http://::1',
      'http://[]:7890',
      'http://[::17890',
    ]) {
      expect(validateProxyUrl(raw)?.error, raw).toMatch(/^PROXY_INVALID_URL/);
    }
    expect(validateProxyUrl('http://proxy.example.com')).toEqual({ url: 'http://proxy.example.com' });
    expect(validateProxyUrl('socks5://[::1]:1080')).toEqual({ url: 'socks5://[::1]:1080' });
    expect(validateProxyUrl('http://user:pass@proxy.corp:8080')).toEqual({
      url: 'http://user:pass@proxy.corp:8080',
    });
  });

  it('忽略主机解析：多分隔符、去空、去重', () => {
    expect(parseProxyBypass('localhost, .internal; 10.0.0.0/8 ,, .internal')).toEqual([
      'localhost',
      '.internal',
      '10.0.0.0/8',
    ]);
    expect(parseProxyBypass('   ')).toEqual([]);
  });

  it('回环始终直连，用户重复填写不产生第二份', () => {
    const list = effectiveBypass(['.internal', 'localhost']);
    expect(list[0]).toBe('.internal');
    for (const host of ['127.0.0.1', 'localhost', '::1']) expect(list).toContain(host);
    expect(list.filter((i) => i === 'localhost')).toHaveLength(1);
  });

  it('computeProxySpec：非自定义不携带地址，自定义缺地址如实报错', () => {
    expect(computeProxySpec({ proxyMode: '跟随系统' })).toEqual({
      spec: { mode: '跟随系统', url: null, bypass: [] },
    });
    expect(computeProxySpec({ proxyMode: '不使用', proxyUrl: 'http://x:1' })).toEqual({
      spec: { mode: '不使用', url: null, bypass: [] },
    });
    expect(computeProxySpec({ proxyMode: '自定义' })).toEqual({
      error: expect.stringMatching(/^PROXY_URL_REQUIRED/),
    });
    expect(computeProxySpec({ proxyMode: '自定义', proxyUrl: 'http://p:1', proxyBypass: '*.corp' })).toEqual({
      spec: { mode: '自定义', url: 'http://p:1', bypass: ['*.corp'] },
    });
    // 非字符串（历史脏数据）按空处理，不抛异常
    expect(computeProxySpec({ proxyMode: 1 as unknown as string }).spec.mode).toBe('跟随系统');
  });

  it('代理变量注入形状与 Rust env_vars 一致', () => {
    expect(proxyEnvOf({ mode: '跟随系统', url: null, bypass: [] })).toEqual({});
    const off = proxyEnvOf({ mode: '不使用', url: null, bypass: [] });
    expect(off.HTTP_PROXY).toBe('');
    expect(off.NO_PROXY).toBe('*');
    const on = proxyEnvOf({ mode: '自定义', url: 'http://proxy.corp:3128', bypass: ['.corp'] });
    expect(on.https_proxy).toBe('http://proxy.corp:3128');
    expect(on.NO_PROXY).toContain('.corp');
    expect(on.NO_PROXY).toContain('127.0.0.1');
    expect(Object.values(on).every((v) => v !== '')).toBe(true);
  });

  it('touchesProxySetting 只对代理三键敏感', () => {
    expect(PROXY_SETTING_KEYS).toEqual(['proxyMode', 'proxyUrl', 'proxyBypass']);
    expect(touchesProxySetting(['theme', 'lang'])).toBe(false);
    expect(touchesProxySetting(['theme', 'proxyUrl'])).toBe(true);
  });

  it('localProxyStatus：地址非法时 persisted 为 null 并退回跟随系统', () => {
    const ok = localProxyStatus({ proxyMode: '自定义', proxyUrl: 'http://p:1', proxyBypass: '*.corp' });
    expect(ok.active).toEqual({ mode: '自定义', url: 'http://p:1', bypass: ['*.corp'] });
    expect(ok.persisted).not.toBeNull();
    expect(ok.effectiveBypass).toContain('localhost');
    expect(ok.staleEngines).toBe(0);

    const bad = localProxyStatus({ proxyMode: '自定义', proxyUrl: '' });
    expect(bad.active.mode).toBe('跟随系统');
    expect(bad.persisted).toBeNull();
  });
});

// ── Mock 通道行为（设置整批拒绝） ────────────────────────────────

describe('Mock 通道 settings:set 的代理整组校验', () => {
  it('自定义缺地址 → 抛错且不写入；同批补齐地址 → 通过', () => {
    const current: Record<string, unknown> = { theme: 'light', proxyMode: '跟随系统' };
    expect(() => assertProxyPatchAcceptable(current, { theme: 'dark', proxyMode: '自定义' })).toThrow(
      /^PROXY_URL_REQUIRED/,
    );
    expect(() =>
      assertProxyPatchAcceptable(current, {
        proxyMode: '自定义',
        proxyUrl: 'http://127.0.0.1:7890',
        proxyBypass: '*.corp',
      }),
    ).not.toThrow();
    // 与代理无关的批量写入不受影响
    expect(() => assertProxyPatchAcceptable(current, { theme: 'dark', chatFontSize: 15 })).not.toThrow();
    // 非自定义模式不需要地址（清空地址也应放行）
    expect(() =>
      assertProxyPatchAcceptable(
        { proxyMode: '自定义', proxyUrl: 'http://p:1' },
        { proxyMode: '不使用', proxyUrl: '', proxyBypass: '' },
      ),
    ).not.toThrow();
    // 本批没改 mode，但库里是自定义且要把地址清空 → 仍按最终状态拒绝
    expect(() =>
      assertProxyPatchAcceptable({ proxyMode: '自定义', proxyUrl: 'http://p:1' }, { proxyUrl: '' }),
    ).toThrow(/^PROXY_URL_REQUIRED/);
  });

  it('Mock 通道把校验接在写入之前，并暴露 proxy:status 推导值', () => {
    const src = mockApi();
    expect(src).toContain('assertProxyPatchAcceptable(mockSettings, patch);');
    expect(
      src.indexOf("if (channel === 'proxy:status') return localProxyStatus(mockSettings);"),
    ).toBeGreaterThan(-1);
    expect(src).toMatch(/settings:set[\s\S]{0,400}assertProxyPatchAcceptable\(mockSettings, patch\);/);
    // 校验发生在写 mockSettings 之前
    expect(src.indexOf('assertProxyPatchAcceptable(mockSettings, patch);')).toBeLessThan(
      src.indexOf('mockSettings[k] = v;'),
    );
  });

  it('Mock 设置台账落 localStorage，刷新后已保存的偏好不丢', () => {
    const src = mockApi();
    expect(src).toContain("const MOCK_SETTINGS_LS_KEY = 'tt:settings';");
    expect(src).toContain('...readMockSettingsSnapshot(),');
    expect(src).toContain('persistMockSettings(mockSettings);');
    // 先写入台账再持久化，避免「保存成功但一刷新就回退」
    expect(src.indexOf('mockSettings[k] = v;')).toBeLessThan(
      src.indexOf('persistMockSettings(mockSettings);'),
    );
  });

  it('proxy:status 三通道都有实现，不存在无人消费的契约常量', () => {
    expect(contracts()).toContain("PROXY_STATUS: 'proxy:status',");
    expect(tauriApi()).toContain('proxy_status');
    expect(alignmentApi()).toContain('getProxyStatus');
    expect(mockApi()).toContain('localProxyStatus');
  });
});

// ── 设置页真实化 ─────────────────────────────────────────────────

describe('设置页网络代理', () => {
  it('三档可选，自定义时出现地址与忽略主机输入', () => {
    const src = settingsPage();
    expect(src).toContain("options={['跟随系统', '自定义', '不使用']}");
    expect(src).toContain("label=\"代理地址\"");
    expect(src).toContain("label=\"忽略主机（可选，逗号分隔）\"");
    expect(src).toContain("mode === '自定义' &&");
  });

  it('不再是只有一个下拉框的假联动：读取真实生效状态并展示', () => {
    const src = settingsPage();
    expect(src).toContain('const ProxySetting: React.FC = () => {');
    expect(src).toContain('setStatus(await getProxyStatus())');
    expect(src).toContain('当前生效');
    expect(src).toContain('<ProxySetting />');
    // 旧的写死两档 + 直接 updateAppSettings 的假联动已移除
    expect(src).not.toContain("options={['跟随系统', '自定义']} onChange");
  });

  it('保存走 strict 通道，错误经 errorTextOf 转成人话', () => {
    const src = settingsPage();
    expect(src).toContain('await setSettings({ proxyMode: mode, proxyUrl: url.trim(), proxyBypass: bypass.trim() }, { strict: true });');
    expect(src).toContain("errorTextOf(e, '网络代理设置保存失败')");
    expect(src).toContain("errorTextOf(e, '代理生效状态读取失败')");
  });

  it('地址缺失时禁用保存并说明原因；已运行引擎仍是旧代理时如实提示', () => {
    const src = settingsPage();
    expect(src).toContain("const needUrl = mode === '自定义' && url.trim() === ''");
    expect(src).toContain('!saving && !needUrl && void handleSave()');
    expect(src).toContain('请填写代理地址');
    expect(src).toContain('个运行中的引擎仍在使用旧代理');
  });
});
