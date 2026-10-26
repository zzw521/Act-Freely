/**
 * 在线市场 · 前端通道、安全边界与页面接线契约测试（任务 #65）
 *
 * 两件事必须同时成立：
 * 1. 桌面端有真实可配置的市场客户端：索引地址落 settings_kv，下载只信索引里的 itemId，
 *    HTTPS / 公网 / 禁重定向 / SHA-256 / 大小上限都在 Rust 侧强制；
 * 2. 浏览器与引擎直连通道没有落盘能力时如实回报 supported=false，
 *    不展示假条目、不伪造安装成功。
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS, ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';
import { handleLocalMarketChannel } from '../../../src/renderer/lib/market-local';

function src(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const store = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: (i: number) => [...store.keys()][i] ?? null,
  get length() {
    return store.size;
  },
};
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type Api = typeof import('../../../src/renderer/lib/api');
let api: Api;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = await import('../../../src/renderer/lib/api');
});

// ── 本地通道：无落盘能力就如实降级 ─────────────────────────

describe('handleLocalMarketChannel · 如实降级', () => {
  it('未登记通道交回上层继续路由', () => {
    expect(handleLocalMarketChannel('chat:history', [])).toEqual({ handled: false });
  });

  it('configGet 声明 supported=false 且不编造索引地址', () => {
    const res = handleLocalMarketChannel('market:configGet', []);
    expect(res.handled).toBe(true);
    expect(res.value).toEqual({ supported: false, skillIndexUrl: null, suiteIndexUrl: null });
  });

  it('列表返回 supported=false / configured=false / 空条目', () => {
    for (const channel of ['market:skillList', 'market:suiteList']) {
      const res = handleLocalMarketChannel(channel, []);
      expect(res.handled).toBe(true);
      expect(res.value).toEqual({
        supported: false,
        configured: false,
        indexUrl: null,
        fetchedAtMs: 0,
        signaturePolicy: null,
        publishers: [],
        items: [],
      });
    }
  });

  it('信任查询如实声明不支持，写配置/安装/信任变更明确拒绝', async () => {
    expect(handleLocalMarketChannel('market:trustGet', [])).toEqual({
      handled: true,
      value: { supported: false, publishers: [] },
    });
    for (const channel of [
      'market:configSet',
      'market:skillInstall',
      'market:suiteInstall',
      'market:trustAdd',
      'market:trustRemove',
    ]) {
      const res = handleLocalMarketChannel(channel, [{}]);
      expect(res.handled).toBe(true);
      await expect(res.value as Promise<unknown>).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    }
  });
});

describe('marketAPI · 走 mock-api 真实分发', () => {
  it('浏览器通道 config 与列表如实声明不支持', async () => {
    const config = await api.marketAPI.config();
    expect(config.supported).toBe(false);
    expect(config.skillIndexUrl).toBeNull();
    expect(config.suiteIndexUrl).toBeNull();

    const skills = await api.marketAPI.listSkills();
    expect(skills).toMatchObject({ supported: false, configured: false, items: [] });
    const suites = await api.marketAPI.listSuites();
    expect(suites).toMatchObject({ supported: false, configured: false, items: [] });
  });

  it('安装与保存配置都以 MARKET_UNSUPPORTED 失败', async () => {
    await expect(api.marketAPI.installSkill('demo')).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    await expect(api.marketAPI.installSuite('demo')).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    await expect(api.marketAPI.setConfig({ skillIndexUrl: 'https://x.example/i.json' })).rejects.toThrowError(
      /^MARKET_UNSUPPORTED/,
    );
  });

  it('错误文案指向桌面版，不承诺浏览器可下载', () => {
    expect(errorMessageOf(ErrorCode.MARKET_UNSUPPORTED)).toContain('桌面版');
  });
});

// ── 通道契约 ───────────────────────────────────────────────

describe('市场通道与 Rust 注册', () => {
  it('九条通道登记在 IPC 契约里', () => {
    expect(IPC_CHANNELS.MARKET_CONFIG_GET).toBe('market:configGet');
    expect(IPC_CHANNELS.MARKET_CONFIG_SET).toBe('market:configSet');
    expect(IPC_CHANNELS.MARKET_SKILL_LIST).toBe('market:skillList');
    expect(IPC_CHANNELS.MARKET_SKILL_INSTALL).toBe('market:skillInstall');
    expect(IPC_CHANNELS.MARKET_SUITE_LIST).toBe('market:suiteList');
    expect(IPC_CHANNELS.MARKET_SUITE_INSTALL).toBe('market:suiteInstall');
    expect(IPC_CHANNELS.MARKET_TRUST_GET).toBe('market:trustGet');
    expect(IPC_CHANNELS.MARKET_TRUST_ADD).toBe('market:trustAdd');
    expect(IPC_CHANNELS.MARKET_TRUST_REMOVE).toBe('market:trustRemove');
  });

  it('桌面通道映射到九个 Rust command', () => {
    const tauriApi = src('../../../src/renderer/lib/tauri-api.ts');
    expect(tauriApi).toContain("if (channel === 'market:configGet') return invoke('market_config_get')");
    expect(tauriApi).toContain("invoke('market_config_set', { req: args[0] })");
    expect(tauriApi).toContain("invoke('market_skill_list')");
    expect(tauriApi).toContain("invoke('market_suite_list')");
    expect(tauriApi).toContain("invoke('market_skill_install', { req: { itemId:");
    expect(tauriApi).toContain("invoke('market_suite_install', { req: { itemId:");
    expect(tauriApi).toContain("if (channel === 'market:trustGet') return invoke('market_trust_get')");
    expect(tauriApi).toContain("invoke('market_trust_add', { req: args[0] })");
    expect(tauriApi).toContain("invoke('market_trust_remove', { req: args[0] })");
  });

  it('Rust 侧命令已注册，market 模块已挂载', () => {
    const lib = src('../../../src-tauri/src/lib.rs');
    expect(lib).toContain('mod market;');
    for (const name of [
      'market_config_get',
      'market_config_set',
      'market_skill_list',
      'market_skill_install',
      'market_suite_list',
      'market_suite_install',
      'market_trust_get',
      'market_trust_add',
      'market_trust_remove',
    ]) {
      expect(lib, `lib.rs 缺少 market::${name}`).toContain(`market::${name},`);
    }
  });

  it('Mock 与引擎直连通道都先走本地市场实现', () => {
    for (const rel of ['../../../src/renderer/lib/mock-api.ts', '../../../src/renderer/lib/engine-api.ts']) {
      const s = src(rel);
      expect(s).toContain("from './market-local'");
      expect(s).toMatch(
        /const market = handleLocalMarketChannel\(channel, args\);\s*\n\s*if \(market\.handled\) return market\.value;/,
      );
    }
  });

  it('api.ts 暴露 marketAPI 九个方法', () => {
    const s = src('../../../src/renderer/lib/api.ts');
    expect(s).toContain('export const marketAPI');
    for (const ch of [
      'MARKET_CONFIG_GET',
      'MARKET_CONFIG_SET',
      'MARKET_SKILL_LIST',
      'MARKET_SKILL_INSTALL',
      'MARKET_SUITE_LIST',
      'MARKET_SUITE_INSTALL',
      'MARKET_TRUST_GET',
      'MARKET_TRUST_ADD',
      'MARKET_TRUST_REMOVE',
    ]) {
      expect(s).toContain(`IPC_CHANNELS.${ch}`);
    }
  });
});

// ── Rust 安全不变量（源码契约） ────────────────────────────

describe('Rust 市场客户端安全边界', () => {
  const market = src('../../../src-tauri/src/market/mod.rs');

  it('索引地址可配置且落 settings_kv，不在代码里写死市场', () => {
    expect(market).toContain('const CONFIG_KEY: &str = "market.sources"');
    expect(market).toContain('settings::get(store, CONFIG_KEY)');
    expect(market).toContain('settings::set(&p0.store, CONFIG_KEY');
    expect(market).not.toMatch(/const\s+\w*INDEX_URL\s*:\s*&str\s*=\s*"https?:\/\//);
  });

  it('远程只允许 HTTPS，本机回环才允许 HTTP，且拒绝 URL 内嵌凭据', () => {
    expect(market).toContain('"http" if local => {}');
    expect(market).toContain('远程市场必须使用 HTTPS');
    expect(market).toContain('url.username().is_empty()');
    expect(market).toContain('url.password().is_some()');
  });

  it('禁止重定向，远程主机必须是公网地址', () => {
    expect(market).toContain('reqwest::redirect::Policy::none()');
    expect(market).toContain('async fn ensure_target_allowed');
    expect(market).toContain('ip_is_public');
    expect(market).toContain('市场主机解析到内网或保留地址');
  });

  it('下载必须校验 SHA-256，且有索引/技能/套件大小上限', () => {
    expect(market).toContain('verify_sha256(&bytes, &item.sha256)?');
    expect(market).toContain('MARKET_HASH_MISMATCH');
    expect(market).toContain('INDEX_MAX_BYTES');
    expect(market).toContain('SKILL_MAX_BYTES');
    expect(market).toContain('SUITE_MAX_BYTES');
  });

  it('安装只接受 itemId：下载地址由 Rust 重新拉索引得到', () => {
    expect(market).toContain('pub struct InstallItem');
    expect(market).toContain('pub item_id: String');
    expect(market).not.toMatch(/struct\s+InstallItem\s*\{[^}]*url/i);
    const api = src('../../../src/renderer/lib/api.ts');
    expect(api).toContain('installSkill(itemId: string)');
    expect(api).toContain("invoke(IPC_CHANNELS.MARKET_SKILL_INSTALL, { itemId })");
  });

  it('安装成功写审计，且只记来源主机不记查询串', () => {
    expect(market).toContain('audit::ACTION_MARKET_INSTALL');
    expect(market).toContain('pub fn source_host');
    expect(market).toContain('.and_then(|url| url.host_str().map(str::to_owned))');
    expect(src('../../../src-tauri/src/security/audit.rs')).toContain(
      'pub const ACTION_MARKET_INSTALL: &str = "market_install";',
    );
    expect(src('../../../src/renderer/components/AuditLog/index.tsx')).toContain(
      "market_install: '📦 市场安装'",
    );
  });

  it('安装复用现有技能/套件落盘逻辑，不另写一套解压与覆盖规则', () => {
    expect(market).toContain('skill::install_content_in');
    expect(market).toContain('suite::install_bytes_in');
    expect(src('../../../src-tauri/src/skill/mod.rs')).toContain('pub fn install_content_in');
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('pub fn install_bytes_in');
  });
});

// ── 页面接线 ───────────────────────────────────────────────

describe('市场窗口与页面接线', () => {
  const browser = src('../../../src/renderer/components/MarketBrowser/index.tsx');
  const extensions = src('../../../src/renderer/pages/Extensions/index.tsx');
  const suitePage = src('../../../src/renderer/pages/ExpertSuite/index.tsx');

  it('市场窗口读配置、保存索引、拉列表、按 itemId 安装', () => {
    expect(browser).toContain('await marketAPI.config()');
    expect(browser).toContain('await marketAPI.setConfig(patch)');
    expect(browser).toContain('await marketAPI.listSkills()');
    expect(browser).toContain('await marketAPI.listSuites()');
    expect(browser).toContain('await marketAPI.installSkill(item.id)');
    expect(browser).toContain('await marketAPI.installSuite(item.id)');
    // 安装入参只有 itemId，没有 url 字段
    expect(browser).not.toMatch(/install(?:Skill|Suite)\(\s*item\.(?:url|downloadUrl)/);
  });

  it('浏览器通道在市场窗口里如实提示，不展示条目', () => {
    expect(browser).toContain('当前运行通道没有市场下载与落盘能力');
    expect(browser).toContain('config !== null && !config.supported');
  });

  it('技能页有在线市场入口，并保留本地上传/对话创建', () => {
    expect(extensions).toContain("import MarketBrowser from '../../components/MarketBrowser'");
    expect(extensions).toContain('<MarketBrowser');
    expect(extensions).toContain('kind="skill"');
    expect(extensions).toContain('在线市场');
    expect(extensions).toContain('openSkillUploadDialog');
  });

  it('套件页优先真实市场，未配置时才回落示例目录', () => {
    expect(suitePage).toContain('await marketAPI.listSuites()');
    expect(suitePage).toContain("import MarketBrowser from '../../components/MarketBrowser'");
    expect(suitePage).toContain('kind="suite"');
    expect(suitePage).toContain('市场源');
    expect(suitePage).toContain("setCatalog(mockEnabled ? MOCK_SUITES : [])");
    // 已配置但拉取失败时不得拿示例冒充市场
    expect(suitePage).toContain('不拿示例条目冒充真实市场');
  });
});

// ── 错误码文案 ─────────────────────────────────────────────

describe('市场错误码文案全覆盖', () => {
  const codes = [
    ErrorCode.MARKET_UNSUPPORTED,
    ErrorCode.MARKET_INVALID_URL,
    ErrorCode.MARKET_NOT_CONFIGURED,
    ErrorCode.MARKET_FETCH_FAILED,
    ErrorCode.MARKET_HTTP_ERROR,
    ErrorCode.MARKET_INVALID_INDEX,
    ErrorCode.MARKET_ITEM_NOT_FOUND,
    ErrorCode.MARKET_HASH_MISMATCH,
    ErrorCode.MARKET_TOO_LARGE,
  ];

  it('每个市场错误码都有中文文案，不裸奔', () => {
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text, `${code} 缺少文案`).not.toBe(code);
      expect(text.length).toBeGreaterThan(6);
    }
  });

  it('哈希不一致的文案明确拒绝安装', () => {
    expect(errorMessageOf(ErrorCode.MARKET_HASH_MISMATCH)).toContain('拒绝安装');
    expect(errorMessageOf(ErrorCode.MARKET_INVALID_URL)).toContain('HTTPS');
  });
});
