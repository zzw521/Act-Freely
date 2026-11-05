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

  it('检查更新与本机清单在不支持时返回空结论而不是伪造条目', () => {
    const check = handleLocalMarketChannel('market:updateCheck', []);
    expect(check.handled).toBe(true);
    expect(check.value).toMatchObject({
      supported: false,
      checkedAtMs: 0,
      sourcesConfigured: 0,
      items: [],
      summary: { updateAvailable: 0, revoked: 0, delisted: 0, disabledApplied: 0 },
    });
    // 降级时也要带 errors：不能把「这条通道做不到」显示成「检查过，一切正常」
    expect((check.value as { errors: string[] }).errors.join('')).toContain('MARKET_UNSUPPORTED');

    const installed = handleLocalMarketChannel('market:installedList', []);
    expect(installed.handled).toBe(true);
    expect(installed.value).toEqual({ supported: false, items: [], lastCheckAtMs: null });
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

  it('信任查询如实声明不支持，写配置/安装/升级/信任变更明确拒绝', async () => {
    expect(handleLocalMarketChannel('market:trustGet', [])).toEqual({
      handled: true,
      value: { supported: false, publishers: [] },
    });
    for (const channel of [
      'market:configSet',
      'market:skillInstall',
      'market:suiteInstall',
      'market:skillUpgrade',
      'market:suiteUpgrade',
      'market:upgradeAll',
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

  it('浏览器通道不会假装能升级：三条升级通道同样以 MARKET_UNSUPPORTED 失败', async () => {
    await expect(api.marketAPI.upgradeSkill('demo')).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    await expect(api.marketAPI.upgradeSuite('demo')).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    await expect(api.marketAPI.upgradeAll()).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
  });

  it('错误文案指向桌面版，不承诺浏览器可下载', () => {
    expect(errorMessageOf(ErrorCode.MARKET_UNSUPPORTED)).toContain('桌面版');
  });
});

// ── 通道契约 ───────────────────────────────────────────────

describe('市场通道与 Rust 注册', () => {
  it('十四条通道登记在 IPC 契约里', () => {
    expect(IPC_CHANNELS.MARKET_CONFIG_GET).toBe('market:configGet');
    expect(IPC_CHANNELS.MARKET_CONFIG_SET).toBe('market:configSet');
    expect(IPC_CHANNELS.MARKET_SKILL_LIST).toBe('market:skillList');
    expect(IPC_CHANNELS.MARKET_SKILL_INSTALL).toBe('market:skillInstall');
    expect(IPC_CHANNELS.MARKET_SUITE_LIST).toBe('market:suiteList');
    expect(IPC_CHANNELS.MARKET_SUITE_INSTALL).toBe('market:suiteInstall');
    expect(IPC_CHANNELS.MARKET_TRUST_GET).toBe('market:trustGet');
    expect(IPC_CHANNELS.MARKET_TRUST_ADD).toBe('market:trustAdd');
    expect(IPC_CHANNELS.MARKET_TRUST_REMOVE).toBe('market:trustRemove');
    // D-07 客户端段：检查更新 + 本机已安装清单
    expect(IPC_CHANNELS.MARKET_UPDATE_CHECK).toBe('market:updateCheck');
    expect(IPC_CHANNELS.MARKET_INSTALLED_LIST).toBe('market:installedList');
    // D-07.3 一键升级：单条（技能 / 套件）+ 批量
    expect(IPC_CHANNELS.MARKET_SKILL_UPGRADE).toBe('market:skillUpgrade');
    expect(IPC_CHANNELS.MARKET_SUITE_UPGRADE).toBe('market:suiteUpgrade');
    expect(IPC_CHANNELS.MARKET_UPGRADE_ALL).toBe('market:upgradeAll');
  });

  it('桌面通道映射到 Rust command', () => {
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
    expect(tauriApi).toContain("if (channel === 'market:updateCheck') return invoke('market_update_check')");
    expect(tauriApi).toContain("if (channel === 'market:installedList') return invoke('market_installed_list')");
    // 升级同样只传 itemId：下载地址仍由 Rust 重新拉索引得到，页面碰不到 url
    expect(tauriApi).toContain("invoke('market_skill_upgrade', { req: { itemId:");
    expect(tauriApi).toContain("invoke('market_suite_upgrade', { req: { itemId:");
    expect(tauriApi).toContain("if (channel === 'market:upgradeAll') return invoke('market_upgrade_all')");
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
      'market_update_check',
      'market_installed_list',
      'market_skill_upgrade',
      'market_suite_upgrade',
      'market_upgrade_all',
    ]) {
      expect(lib, `lib.rs 缺少 market::${name}`).toContain(`market::${name},`);
    }
  });

  it('启动时后台检查一次撤销，不阻塞窗口且失败不打扰用户', () => {
    const lib = src('../../../src-tauri/src/lib.rs');
    expect(lib).toContain('market::startup_check(market_store)');
    const market = src('../../../src-tauri/src/market/mod.rs');
    expect(market).toContain('pub async fn startup_check(store: std::sync::Arc<Store>)');
    expect(market).toContain('installed::check_updates(&store, &base).await');
    // 检查失败只落一条 info 审计：启动路径不能被网络打断
    expect(market).toContain('启动检查未完成（不影响本机使用）');
    expect(market).toContain('audit::ACTION_MARKET_REVOKE');
    expect(src('../../../src-tauri/src/security/audit.rs')).toContain(
      'pub const ACTION_MARKET_REVOKE: &str = "market_revoke";',
    );
    expect(src('../../../src/renderer/components/AuditLog/index.tsx')).toContain(
      "market_revoke: '⛔ 市场撤销'",
    );
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

  it('api.ts 暴露 marketAPI 十一个方法', () => {
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
      'MARKET_UPDATE_CHECK',
      'MARKET_INSTALLED_LIST',
      'MARKET_SKILL_UPGRADE',
      'MARKET_SUITE_UPGRADE',
      'MARKET_UPGRADE_ALL',
    ]) {
      expect(s).toContain(`IPC_CHANNELS.${ch}`);
    }
    expect(s).toContain('updateCheck()');
    expect(s).toContain('installedList()');
    expect(s).toContain('upgradeSkill(itemId: string)');
    expect(s).toContain('upgradeSuite(itemId: string)');
    expect(s).toContain('upgradeAll()');
    // 升级入参同样只有 itemId：下载地址只能由 Rust 重新拉索引得到
    expect(s).toContain('IPC_CHANNELS.MARKET_SKILL_UPGRADE, { itemId }');
    expect(s).toContain('IPC_CHANNELS.MARKET_SUITE_UPGRADE, { itemId }');
    expect(s).not.toMatch(/upgrade(Skill|Suite)\([^)]*(?:url|downloadUrl)/i);
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

  it('D-07.3 升级复用同一条校验链，落盘改成回收旧版本', () => {
    // 校验链由安装与升级共用一个函数，升级不是旁路
    expect(market).toContain('async fn verified_skill_item(');
    expect(market).toContain('async fn verified_suite_item(');
    expect(market.match(/verified_(?:skill|suite)_item\(/g)?.length).toBeGreaterThanOrEqual(4);
    // 升级只走一次索引：批量升级 N 条不能变成 2N+1 次请求
    expect(market.slice(market.indexOf('async fn upgrade_item('))).toContain('verified_skill_item(&url, raw_id, &trust)');
    // 落盘：技能与套件各自把旧版本移进回收目录，且失败时放回
    expect(src('../../../src-tauri/src/skill/mod.rs')).toContain('pub fn upgrade_content_in(');
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('pub fn upgrade_bytes_in(');
    expect(src('../../../src-tauri/src/skill/mod.rs')).toContain('fn restore_after_failed_upgrade(');
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('fn restore_after_failed_upgrade(');
    // 升级成功后本机清单跟着走新版本，并清掉该条目的撤销标记
    expect(market.slice(market.indexOf('async fn upgrade_item('))).toContain('installed::note_install(');
    // 升级写独立审计动作，页面能区分「装」和「升」
    expect(market).toContain('audit::ACTION_MARKET_UPGRADE');
    expect(src('../../../src-tauri/src/security/audit.rs')).toContain(
      'pub const ACTION_MARKET_UPGRADE: &str = "market_upgrade";',
    );
    expect(src('../../../src/renderer/components/AuditLog/index.tsx')).toContain(
      "market_upgrade: '📦 市场升级'",
    );
    // 批量升级先检查、只吃 updateAvailable，单条失败不牵连其余
    const all = market.slice(market.indexOf('pub async fn upgrade_all('));
    expect(all).toContain('installed::check_updates(store, base).await?');
    expect(all).toContain('"updateAvailable"');
    expect(all).toContain('failed');
  });

  it('市场安装仍然不覆盖既有条目（升级才是覆盖式的那条路）', () => {
    expect(src('../../../src-tauri/src/skill/mod.rs')).toContain('不覆盖既有技能');
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('不覆盖既有套件');
    // 升级对「本机没装过」的条目直接拒绝，不借升级之名装新东西
    expect(src('../../../src-tauri/src/skill/mod.rs')).toContain('尚未安装，无法升级；请改用安装');
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('尚未安装，无法升级；请改用安装');
  });
});

// ── D-07 客户端段：已安装清单 → 检查更新 → 撤销不可执行 ───────

describe('已安装清单与撤销拦截（D-07 客户端段）', () => {
  const installed = src('../../../src-tauri/src/market/installed.rs');
  const market = src('../../../src-tauri/src/market/mod.rs');
  const runtime = src('../../../src-tauri/src/bridge/runtime.rs');
  const skillMod = src('../../../src-tauri/src/skill/mod.rs');
  const suiteMod = src('../../../src-tauri/src/suite/mod.rs');

  it('三份本机状态分键存放，撤销标记独立于用户开关', () => {
    expect(installed).toContain('const REGISTRY_KEY: &str = "market.installed"');
    expect(installed).toContain('const REVOKED_KEY: &str = "market.revoked"');
    expect(installed).toContain('const LAST_CHECK_KEY: &str = "market.lastCheckAtMs"');
    // 执行面读的是撤销标记，不是用户能拨的禁用清单
    expect(installed).toContain('pub fn blocked_ids(store: &Store, kind: &str) -> Vec<String>');
  });

  it('安装成功落清单、卸载清记录，撤销命中才写标记', () => {
    expect(market).toContain('installed::note_install(');
    expect(skillMod).toContain('crate::market::installed::forget(&p0.store, crate::market::KIND_SKILL, id)?');
    expect(suiteMod).toContain(
      'crate::market::installed::forget(&p0.store, crate::market::KIND_SUITE, id)?',
    );
    expect(installed).toContain('pub fn note_install(');
    expect(installed).toContain('pub fn forget(');
  });

  it('撤销只作用于市场安装的记录，本机扫盘补齐的同名条目不误伤', () => {
    expect(installed).toContain('PROVENANCE_MARKET: &str = "market"');
    expect(installed).toContain('const PROVENANCE_SCANNED: &str = "scanned"');
    expect(installed).toContain('r.provenance.as_deref() == Some(PROVENANCE_MARKET)');
    // 版本对不上时不阻断：拿不确定的版本去禁用属于误伤
    expect(installed).toContain('(Some(_), None) => false');
  });

  it('检查更新会真的把撤销落到禁用清单并写审计', () => {
    expect(installed).toContain('skill::set_disabled(store, &mark.item_id, true)');
    expect(installed).toContain('suite::set_disabled(store, &mark.item_id, true)');
    expect(installed).toContain('audit::ACTION_MARKET_REVOKE');
    // 状态词表固定六种，比不了就说不清，不写「已最新」
    for (const status of [
      '"upToDate"',
      '"updateAvailable"',
      '"ahead"',
      '"unknownVersion"',
      '"delisted"',
      '"revoked"',
    ]) {
      expect(installed, `缺少状态 ${status}`).toContain(status);
    }
  });

  it('已撤销条目在安装口被拒，列表页与安装口径一致', () => {
    expect(market).toContain('fn ensure_not_revoked(');
    expect(market).toContain('MARKET_ITEM_REVOKED');
    // 技能与套件两条安装路径都要过这道闸门
    expect(market.match(/ensure_not_revoked\(/g)?.length).toBeGreaterThanOrEqual(2);
    expect(market).toContain('fn revoked_decl_for(');
  });

  it('用户开关不能把已撤销的包重新放回执行面', () => {
    expect(skillMod).toContain('fn ensure_can_enable(store: &Store, dir: &Path, id: &str)');
    expect(skillMod).toContain('crate::market::installed::revoked_reason_for(');
    expect(skillMod).toContain('MARKET_ITEM_REVOKED');
    // 既有的安全扫描闸门仍在同一函数里，没被市场改动绕过
    expect(skillMod).toContain('ensure_enable_allowed(dir, id)');
  });

  it('运行时装配按撤销标记把技能与套件一起摘掉', () => {
    expect(runtime).toContain('crate::market::installed::blocked_ids(s, crate::market::KIND_SKILL)');
    expect(runtime).toContain('crate::market::installed::blocked_ids(s, crate::market::KIND_SUITE)');
    expect(runtime).toContain('已被市场撤销，本轮未进入执行面');
    expect(runtime).toContain('ids.extend(blocked_suites.iter().cloned());');
    expect(suiteMod).toContain('套件「{id}」已禁用（用户停用或市场撤销），本轮未进入执行面');
  });

  it('撤销声明三个来源合并，读不到时如实带出而不是当作没有撤销', () => {
    expect(market).toContain('"revokedVersions"');
    expect(market).toContain('.get("revokedUrl")');
    expect(market).toContain('async fn fetch_revoked_decls');
    // 撤销清单走与索引相同的地址校验，不能借 revokedUrl 跳到内网
    expect(market.slice(market.indexOf('async fn fetch_revoked_decls')))
      .toContain('ensure_target_allowed(&url, local).await?');
    expect(market).toContain('revokedSourceOk');
    expect(market).toContain('revokedSourceError');
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

  it('窗口里接线了检查更新与本机清单，且降级时不伪造状态', () => {
    expect(browser).toContain('await marketAPI.updateCheck()');
    expect(browser).toContain('await marketAPI.installedList()');
    expect(browser).toContain('检查更新');
    expect(browser).toContain('上次检查');
    // 撤销条目在列表里就显示出来，并把安装入口换成不可安装
    expect(browser).toContain('item.revoked');
    expect(browser).toContain('不可安装');
    expect(browser).toContain('已撤销');
    // 从未检查过时显示「从未」，而不是编一个时间
    expect(browser).toContain("if (!ms || !Number.isFinite(ms)) return '从未'");
    // 本机清单读不到时留空并说明，不拿示例条目冒充
    expect(browser).toContain('本机清单读不到不影响市场浏览');
    // 版本比不了时如实说比不了（六种状态词表在前端同样只报查得出来的）
    expect(browser).toContain('unknownVersion: { label: ');
    expect(browser).not.toMatch(/status === 'unknownVersion'[\s\S]{0,80}已是最新/);
  });

  it('D-07.3：页面有单条升级与全部升级入口，旧的「先卸载再安装」口径已作废', () => {
    expect(browser).toContain('await marketAPI.upgradeSkill(item.id)');
    expect(browser).toContain('await marketAPI.upgradeSuite(item.id)');
    expect(browser).toContain('await marketAPI.upgradeAll()');
    expect(browser).toContain('全部升级');
    expect(browser).toContain("'升级'");
    // 升级入参同样只有 itemId
    expect(browser).not.toMatch(/upgrade(?:Skill|Suite)\(\s*item\.(?:url|downloadUrl)/);
    // 教用户「先卸载再安装」的提示不能留下：那是升级还没做时的临时口径
    expect(browser).not.toContain('先卸载再安装即可完成更新');
    expect(browser).not.toContain('需先卸载再安装');
    // 升级不替用户把被撤销/禁用后的开关拨回去：回执里的禁用状态要显示出来
    expect(browser).toContain('该条目仍是禁用状态');
    // 批量升级的失败条目要说清「失败的仍停在原版本」，不能一句「部分失败」糊过去
    expect(browser).toContain('失败的仍停在原版本');
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
    ErrorCode.MARKET_ITEM_REVOKED,
    ErrorCode.MARKET_UNTRUSTED_PUBLISHER,
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
