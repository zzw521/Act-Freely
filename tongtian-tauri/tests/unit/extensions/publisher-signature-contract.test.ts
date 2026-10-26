/**
 * 在线市场 · 发布者签名与信任锚契约测试（任务 #66 · P0 安全加固）
 *
 * 验收边界：
 * 1. 索引自报公钥不能自证身份：安装时必须命中本机信任库，且发布者 id 与公钥同时一致；
 * 2. 签名用 Ed25519 真实验签，覆盖 类型/发布者/条目/版本/SHA-256，展示字段不参与；
 * 3. SHA-256 先校验、签名后校验、最后才落盘，任何一步失败都不留下安装目录；
 * 4. 信任只由用户在桌面端显式添加/移除，拉索引不会自动写信任库；
 * 5. 浏览器 / Mock 通道没有信任库与落盘能力时如实拒绝，不伪造「已信任」。
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS, ErrorCode } from '../../../src/shared/contracts';
import type {
  MarketList,
  MarketPublisher,
  MarketSignaturePolicy,
  MarketSkillItem,
  MarketTrustAddReq,
  MarketTrustedPublisher,
  MarketTrustRemoveReq,
} from '../../../src/shared/contracts';
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

// ── 类型与通道契约 ─────────────────────────────────────────

describe('签名契约类型', () => {
  it('渲染层类型要求发布者指纹、信任状态与签名策略字段齐全', () => {
    const publisher: MarketPublisher = {
      id: 'acme',
      name: 'Acme',
      publicKey: 'A'.repeat(43),
      fingerprint: 'ab'.repeat(32),
      trusted: false,
      keyChanged: false,
    };
    const item: MarketSkillItem = {
      id: 'demo',
      name: 'Demo',
      description: null,
      version: '1.0.0',
      author: 'acme',
      homepage: null,
      sha256: 'ab'.repeat(32),
      sizeBytes: null,
      installed: false,
      publisherId: 'acme',
      publisherName: 'Acme',
      publisherFingerprint: publisher.fingerprint,
      signed: true,
      publisherTrusted: false,
      publisherKeyChanged: false,
    };
    const policy: MarketSignaturePolicy = 'required';
    const list: MarketList<MarketSkillItem> = {
      supported: true,
      configured: true,
      indexUrl: 'https://market.example/index.json',
      fetchedAtMs: 1,
      signaturePolicy: policy,
      publishers: [publisher],
      items: [item],
    };
    expect(list.signaturePolicy).toBe('required');
    expect(list.items[0].signed && !list.items[0].publisherTrusted).toBe(true);
  });

  it('本机信任条目的入参与返回结构独立于索引', () => {
    const add: MarketTrustAddReq = {
      publisherId: 'acme',
      publicKey: 'A'.repeat(43),
      name: 'Acme',
    };
    const remove: MarketTrustRemoveReq = { publisherId: 'acme' };
    const trusted: MarketTrustedPublisher = {
      id: 'acme',
      name: 'Acme',
      publicKey: add.publicKey,
      fingerprint: 'ab'.repeat(32),
      addedAtMs: 1,
    };
    expect(remove.publisherId).toBe(trusted.id);
  });

  it('三条信任通道登记在 IPC 契约里', () => {
    expect(IPC_CHANNELS.MARKET_TRUST_GET).toBe('market:trustGet');
    expect(IPC_CHANNELS.MARKET_TRUST_ADD).toBe('market:trustAdd');
    expect(IPC_CHANNELS.MARKET_TRUST_REMOVE).toBe('market:trustRemove');
  });

  it('六个签名/信任错误码都有明确取值', () => {
    expect(ErrorCode.MARKET_INVALID_PUBLISHER).toBe('MARKET_INVALID_PUBLISHER');
    expect(ErrorCode.MARKET_UNTRUSTED_PUBLISHER).toBe('MARKET_UNTRUSTED_PUBLISHER');
    expect(ErrorCode.MARKET_PUBLISHER_KEY_CHANGED).toBe('MARKET_PUBLISHER_KEY_CHANGED');
    expect(ErrorCode.MARKET_SIGNATURE_REQUIRED).toBe('MARKET_SIGNATURE_REQUIRED');
    expect(ErrorCode.MARKET_SIGNATURE_INVALID).toBe('MARKET_SIGNATURE_INVALID');
    expect(ErrorCode.MARKET_TRUST_SAVE_FAILED).toBe('MARKET_TRUST_SAVE_FAILED');
  });
});

// ── 本地通道：没有信任库就如实拒绝 ─────────────────────────

describe('浏览器 / Mock 通道的信任能力', () => {
  it('trustGet 声明 supported=false 且不返回任何信任条目', async () => {
    expect(handleLocalMarketChannel('market:trustGet', [])).toEqual({
      handled: true,
      value: { supported: false, publishers: [] },
    });
    await expect(api.marketAPI.trustGet()).resolves.toEqual({
      supported: false,
      publishers: [],
    });
  });

  it('添加与移除信任都以 MARKET_UNSUPPORTED 失败，不静默成功', async () => {
    const add = api.marketAPI.trustAdd({
      publisherId: 'acme',
      publicKey: 'A'.repeat(43),
    });
    const remove = api.marketAPI.trustRemove({ publisherId: 'acme' });
    await expect(add).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
    await expect(remove).rejects.toThrowError(/^MARKET_UNSUPPORTED/);
  });

  it('错误文案指向桌面端信任库', () => {
    expect(errorMessageOf(ErrorCode.MARKET_UNSUPPORTED)).toContain('桌面版');
    expect(errorMessageOf(ErrorCode.MARKET_UNTRUSTED_PUBLISHER)).toContain('市场窗口');
    expect(errorMessageOf(ErrorCode.MARKET_UNTRUSTED_PUBLISHER)).toContain('拒绝安装');
  });
});

// ── Rust：真实 Ed25519 验签 ────────────────────────────────

describe('Rust 发布者签名实现', () => {
  const market = src('../../../src-tauri/src/market/mod.rs');
  const cargo = src('../../../src-tauri/Cargo.toml');

  it('使用 ring 的 Ed25519 公钥验签，不引入自制算法', () => {
    expect(cargo).toContain('ring = "0.17"');
    expect(market).toContain('use ring::signature::{UnparsedPublicKey, ED25519}');
    expect(market).toContain('UnparsedPublicKey::new(&ED25519, &publisher.public_key)');
    expect(market).toContain('.verify(&payload, &signature_bytes)');
    expect(market).not.toContain('fn verify_ed25519_locally');
  });

  it('签名载荷覆盖域名/类型/发布者/条目/版本/SHA-256，不覆盖可改的展示字段', () => {
    expect(market).toContain('const SIGNING_DOMAIN: &str = "tt-market-v1";');
    expect(market).toContain(
      'format!("{SIGNING_DOMAIN}\\n{kind}\\n{publisher_id}\\n{item_id}\\n{version}\\n{sha256}").into_bytes()',
    );
    const payload = market.slice(
      market.indexOf('fn signing_payload('),
      market.indexOf('fn attach_signature('),
    );
    expect(payload).not.toContain('name');
    expect(payload).not.toContain('description');
    expect(payload).not.toContain('author');
  });

  it('公钥必须是 32 字节，签名必须是 64 字节，base64 规范化后再比较', () => {
    expect(market).toContain('fn normalize_public_key');
    expect(market).toContain('bytes.len() != 32');
    expect(market).toContain('公钥必须是 32 字节 Ed25519 公钥');
    expect(market).toContain('bytes.len() != 64');
    expect(market).toContain('signature 必须是 64 字节 Ed25519 签名');
    expect(market).toContain('STANDARD_NO_PAD.encode(&bytes)');
  });

  it('发布者标识规范化：小写、限长、字符集受限、不可重复', () => {
    expect(market).toContain('fn normalize_publisher_id');
    expect(market).toContain('MAX_PUBLISHER_ID_CHARS');
    expect(market).toContain('发布者标识只能包含字母、数字、点、下划线与连字符');
    expect(market).toContain('发布者标识不能以点或连字符开头');
    expect(market).toContain('发布者标识重复');
    expect(market).toContain('MAX_PUBLISHERS');
  });

  it('publisherId 与 signature 必须成对出现，未知发布者直接拒绝索引', () => {
    expect(market).toContain('publisherId 与 signature 必须同时提供');
    expect(market).toContain('引用了未知发布者');
    expect(market).toContain('signaturePolicy 只能是');
    expect(market).toContain('None | Some("optional") => SignaturePolicy::Optional');
    expect(market).toContain('Some("required") => SignaturePolicy::Required');
  });

  it('required 策略下未签名条目在解析阶段就被拒绝', () => {
    expect(market).toContain('没有发布者签名，但索引要求签名（signaturePolicy=required）');
    expect(market).toContain('MARKET_SIGNATURE_REQUIRED');
    expect(market).toContain('return Ok("未签名（索引未强制签名）".to_string())');
  });

  it('验签失败的错误可区分：未信任 / 换钥 / 缺签 / 签名不符', () => {
    expect(market).toContain('MARKET_UNTRUSTED_PUBLISHER: 发布者「{}」的公钥指纹 {} 尚未信任');
    expect(market).toContain('MARKET_PUBLISHER_KEY_CHANGED: 发布者「{}」的公钥已变化');
    expect(market).toContain('MARKET_SIGNATURE_REQUIRED: 索引要求发布者签名');
    expect(market).toContain('MARKET_SIGNATURE_INVALID: 发布者「{}」的签名与');
    expect(market).toContain('已拒绝安装');
  });

  it('SHA-256 先过、签名后过、最后才写盘', () => {
    for (const install of ['install_skill_from', 'install_suite_from']) {
      const start = market.indexOf(`pub async fn ${install}(`);
      const end =
        install === 'install_skill_from'
          ? market.indexOf('pub async fn install_suite_from(')
          : market.indexOf('#[tauri::command]', start);
      const body = market.slice(start, end);
      const sha = body.indexOf('verify_sha256(&bytes, &item.sha256)?;');
      const sig = body.indexOf('verify_artifact_signature(');
      const write = body.indexOf(
        install === 'install_skill_from' ? 'skill::install_content_in(' : 'suite::install_bytes_in(',
      );
      expect(sha, `${install} 缺少 SHA-256 校验`).toBeGreaterThan(-1);
      expect(sig, `${install} 缺少发布者验签`).toBeGreaterThan(sha);
      expect(write, `${install} 未在验签之后落盘`).toBeGreaterThan(sig);
    }
  });
});

// ── Rust：本机信任锚 ──────────────────────────────────────

describe('Rust 本机信任锚', () => {
  const market = src('../../../src-tauri/src/market/mod.rs');

  it('信任库独立落在 settings_kv，索引源配置与信任库互不覆盖', () => {
    expect(market).toContain('const TRUST_KEY: &str = "market.trustedPublishers";');
    expect(market).toContain('settings::get(store, TRUST_KEY)');
    expect(market).toContain('settings::set(store, TRUST_KEY, &encoded)');
    expect(market).toContain('const CONFIG_KEY: &str = "market.sources";');
    expect(market.match(/"market\.trustedPublishers"/g) ?? []).toHaveLength(1);
  });

  it('信任判定必须 id 与公钥同时一致，换钥不会自动继承信任', () => {
    expect(market).toContain('fn trust_status(');
    expect(market).toContain('Some(entry) if entry.public_key == publisher.public_key_b64 => PublisherTrust::Trusted');
    expect(market).toContain('Some(_) => PublisherTrust::KeyChanged');
    expect(market).toContain('if existing.public_key != public_key_b64');
    expect(market).toContain('MARKET_PUBLISHER_KEY_CHANGED: 本机已信任发布者');
  });

  it('拉取索引不会自动写信任库', () => {
    const fetchBlock = market.slice(
      market.indexOf('async fn fetch_index'),
      market.indexOf('// ── 发布者信任与验签'),
    );
    expect(fetchBlock.length).toBeGreaterThan(100);
    expect(fetchBlock).not.toContain('add_trusted_publisher');
    expect(fetchBlock).not.toContain('save_trust');
    expect(fetchBlock).not.toContain('trustedPublishers');
  });

  it('信任增删只由显式命令触发并写审计，移除是幂等的', () => {
    expect(market).toContain('pub async fn market_trust_add');
    expect(market).toContain('pub async fn market_trust_remove');
    expect(market).toContain('save_trust(&p0.store, &trust)?;');
    expect(market).toContain('信任市场发布者 {}（公钥指纹 {}）');
    expect(market).toContain('移除市场发布者信任：{}');
    expect(market).toContain('audit::ACTION_CONFIG_CHANGE');
    expect(market).toContain('let removed = remove_trusted_publisher(&mut trust, &req.publisher_id)?;');
    expect(market).toContain('if removed {');
  });

  it('信任列表回传指纹但不回传私钥类字段', () => {
    expect(market).toContain('"fingerprint": fingerprint_of_public_key_b64(&entry.public_key)');
    expect(market).toContain('json!({ "supported": true, "publishers": publishers })');
  });

  it('安装审计记录验签结果，不把未签名说成已校验', () => {
    expect(market).toContain('fn audit_install(');
    expect(market).toContain('signature: &str');
    expect(market).toContain('版本 {version}，{signature}');
    expect(market).toContain('未签名（索引未强制签名）');
    expect(market).toContain('发布者 {} 签名已校验');
  });
});

// ── 市场窗口：信任必须由用户点出来 ────────────────────────

describe('市场窗口的信任交互', () => {
  const browser = src('../../../src/renderer/components/MarketBrowser/index.tsx');

  it('展示发布者 id 与公钥指纹，提供信任/取消信任/移除旧信任', () => {
    expect(browser).toContain('发布者信任');
    expect(browser).toContain('指纹 {shortFingerprint(publisher.fingerprint)}');
    expect(browser).toContain('已信任');
    expect(browser).toContain('未信任');
    expect(browser).toContain('公钥已变化');
    expect(browser).toContain('取消信任');
    expect(browser).toContain('移除旧信任');
    expect(browser).toContain('公钥与本机已信任的不一致；请先移除旧信任，再核对新公钥后信任。');
  });

  it('信任写操作只出现在 handleTrust，且公钥来自当前索引条目', () => {
    const calls = browser.match(/marketAPI\.trust(?:Add|Remove)\(/g) ?? [];
    expect(calls).toHaveLength(2);
    expect(browser).toContain('await marketAPI.trustRemove({ publisherId: publisher.id })');
    expect(browser).toContain('await marketAPI.trustAdd({');
    expect(browser).toContain('publicKey: publisher.publicKey');
    expect(browser).toContain('name: publisher.name');

    const installBody = browser.slice(
      browser.indexOf('const handleInstall'),
      browser.indexOf('const handleTrust'),
    );
    expect(installBody.length).toBeGreaterThan(100);
    expect(installBody).not.toContain('trustAdd');
    expect(installBody).not.toContain('trustRemove');
  });

  it('未信任或换钥时安装按钮禁用，并给出先信任的提示', () => {
    expect(browser).toContain(
      'const blockedByTrust = item.signed && (!item.publisherTrusted || item.publisherKeyChanged);',
    );
    expect(browser).toContain('disabled={Boolean(installingId) || blockedByTrust}');
    expect(browser).toContain('请先在「发布者信任」中确认并信任该发布者');
    expect(browser).toContain("blockedByTrust ? '先信任发布者' : '安装'");
  });

  it('条目如实区分已签名/未签名，未签名不伪装成安全', () => {
    expect(browser).toContain("`已签名 · ${publisherLabel(item)}`");
    expect(browser).toContain("`未信任 · ${publisherLabel(item)}`");
    expect(browser).toContain("`公钥已变化 · ${publisherLabel(item)}`");
    expect(browser).toContain('label="未签名"');
  });

  it('索引要求签名时明确提示未签名条目不可安装', () => {
    expect(browser).toContain("signaturePolicy === 'required'");
    expect(browser).toContain('该索引要求所有条目带发布者签名；未信任的发布者不能安装。');
    expect(browser).toContain('签名可选；信任后安装会校验 Ed25519 签名，发布者换钥需要重新确认。');
    expect(browser).toContain('索引没有声明任何发布者，当前条目均为未签名。');
  });

  it('安装仍只传 itemId，信任状态不改变安装入参', () => {
    expect(browser).toContain('await marketAPI.installSkill(item.id)');
    expect(browser).toContain('await marketAPI.installSuite(item.id)');
    expect(browser).not.toMatch(/install(?:Skill|Suite)\(\s*item\.(?:url|downloadUrl|signature)/);
  });
});

// ── 文案：安全问题必须说清后果 ────────────────────────────

describe('签名与信任错误文案', () => {
  const codes = [
    ErrorCode.MARKET_INVALID_PUBLISHER,
    ErrorCode.MARKET_UNTRUSTED_PUBLISHER,
    ErrorCode.MARKET_PUBLISHER_KEY_CHANGED,
    ErrorCode.MARKET_SIGNATURE_REQUIRED,
    ErrorCode.MARKET_SIGNATURE_INVALID,
    ErrorCode.MARKET_TRUST_SAVE_FAILED,
  ];

  it('每个错误码都有中文文案，不裸奔', () => {
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text, `${code} 缺少文案`).not.toBe(code);
      expect(text.length).toBeGreaterThan(8);
    }
  });

  it('拒绝安装的场景明确写出「已拒绝安装」', () => {
    for (const code of [
      ErrorCode.MARKET_UNTRUSTED_PUBLISHER,
      ErrorCode.MARKET_PUBLISHER_KEY_CHANGED,
      ErrorCode.MARKET_SIGNATURE_REQUIRED,
      ErrorCode.MARKET_SIGNATURE_INVALID,
    ]) {
      expect(errorMessageOf(code), `${code} 未说明后果`).toContain('已拒绝安装');
    }
  });

  it('公钥与换钥文案都指向可执行的下一步', () => {
    expect(errorMessageOf(ErrorCode.MARKET_INVALID_PUBLISHER)).toContain('32 字节');
    expect(errorMessageOf(ErrorCode.MARKET_PUBLISHER_KEY_CHANGED)).toContain('移除旧信任');
    expect(errorMessageOf(ErrorCode.MARKET_SIGNATURE_INVALID)).toContain('篡改');
    expect(errorMessageOf(ErrorCode.MARKET_TRUST_SAVE_FAILED)).toContain('保存失败');
  });
});
