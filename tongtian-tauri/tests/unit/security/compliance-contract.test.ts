/**
 * 合规通道契约测试（任务 #21）
 *
 * `compliance:status` / `compliance:update` 曾是「常量存在但无人实现」的死通道，
 * 而 `compliance:check` 更不存在：顶栏「合规模式」胶囊读的是一个没有任何写入点的
 * 本地假状态。本文件从源码 + 行为两侧锁定链路完整：
 * - Rust：`compliance::{status,update,check}` + command 注册 + 闸门留痕
 * - 契约：三个通道常量都有真实消费方；错误码只保留实际会抛出的那些
 * - 通道：Tauri 分支 / 本地权威分支 / Mock 与引擎直连路由
 * - 页面：合规概览消费 status 与 update；Workbench 提交前过闸门
 * - 口径：本地实现与 Rust 判定顺序一致（先端点出域，再内容分级）
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustCompliance = () => read('../../../src-tauri/src/security/compliance.rs');
const rustPolicy = () => read('../../../src-tauri/src/security/policy.rs');
const rustAudit = () => read('../../../src-tauri/src/security/audit.rs');
const rustMod = () => read('../../../src-tauri/src/security/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const securityLocal = () => read('../../../src/renderer/lib/security-local.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');
const store = () => read('../../../src/renderer/store/securityStore.ts');
const panel = () => read('../../../src/renderer/components/CompliancePanel/index.tsx');
const securityPage = () => read('../../../src/renderer/pages/Security/index.tsx');
const topBar = () => read('../../../src/renderer/components/Layout/TopBar.tsx');
const appStore = () => read('../../../src/renderer/stores/useAppStore.ts');
const mainLayout = () => read('../../../src/renderer/components/Layout/MainLayout.tsx');
const workbenchStore = () => read('../../../src/renderer/store/workbenchStore.ts');

/** 扫描 security 模块源码里以字符串字面量抛出的错误码（新增码缺文案即失败） */
function rustSecurityCodes(prefix: RegExp): string[] {
  const dir = fileURLToPath(new URL('../../../src-tauri/src/security', import.meta.url));
  const found = new Set<string>();
  for (const name of readdirSync(dir)) {
    if (!name.endsWith('.rs')) continue;
    for (const m of readFileSync(`${dir}/${name}`, 'utf8').matchAll(prefix)) found.add(m[0]);
  }
  return [...found].sort();
}

// ── Rust 权威实现 ────────────────────────────────────────────────

describe('Rust 合规通道', () => {
  it('status/update/check 三个函数齐备且不做独立存储', () => {
    const src = rustCompliance();
    expect(src).toContain('pub fn status(store: &Store) -> Result<Value, String>');
    expect(src).toContain('pub fn update(store: &Store, patch: &CompliancePatch)');
    expect(src).toContain('pub fn check(store: &Store, text: &str, base_url: Option<&str>)');
    // 聚合视图：权威仍出 policy / model / audit 三处
    expect(src).toContain('policy::get_domain_control(store)');
    expect(src).toContain('super::classify::classify(text)');
    expect(src).toContain('audit::query(');
  });

  it('patch 结构 camelCase 且拒绝未知字段（未知键不会被静默丢弃）', () => {
    const src = rustCompliance();
    expect(src).toMatch(/rename_all\s*=\s*"camelCase",\s*deny_unknown_fields/);
    for (const f of [
      'hard_switch_enabled',
      'enabled',
      'mode',
      'allowed_domains',
      'blocked_domains',
      'force_local_for_confidential',
    ]) {
      expect(src).toContain(`pub ${f}: Option<`);
    }
  });

  it('空 patch 只读不写，不产生审计噪声', () => {
    const body = rustCompliance().split('pub fn update(')[1].split('/// 提交前合规闸门')[0];
    expect(body).toContain('if patch.is_empty()');
    expect(body).toContain('return Ok((status(store)?, false))');
  });

  it('check 判定顺序：先端点出域，再内容分级', () => {
    const body = rustCompliance().split('pub fn check(')[1].split('/// 闸门留痕')[0];
    const domainAt = body.indexOf('policy::eval_domain_allowed(&cfg, u)');
    const leakAt = body.indexOf('COMPLIANCE_DATA_LEAK');
    expect(domainAt).toBeGreaterThan(-1);
    expect(leakAt).toBeGreaterThan(domainAt);
    // 本地回环口径复用同一份判定，不另起一套
    expect(body).toContain('policy::is_local_host(&host)');
    expect(rustPolicy()).toContain('pub fn is_local_host(host: &str) -> bool');
  });

  it('闸门允许与拒绝都留痕，拒绝升 warning（数据流向可追溯）', () => {
    expect(rustAudit()).toContain('pub const ACTION_COMPLIANCE_CHECK: &str = "compliance_check"');
    const body = rustCompliance().split('fn record_gate(')[1].split('#[cfg(test)]')[0];
    expect(body).toContain('audit::ACTION_COMPLIANCE_CHECK');
    expect(body).toContain('audit::SEV_WARNING');
    expect(body).toContain('audit::record_quiet(');
  });

  it('三个 command 均已注册', () => {
    const lib = rustLib();
    expect(lib).toContain('security::compliance_status,');
    expect(lib).toContain('security::compliance_update,');
    expect(lib).toContain('security::compliance_check,');
    const mod = rustMod();
    expect(mod).toContain('pub async fn compliance_status');
    expect(mod).toContain('pub async fn compliance_update');
    expect(mod).toContain('pub async fn compliance_check');
    expect(mod).toContain('compliance::check(&p0.store, &text, base_url.as_deref())');
  });

  it('Rust 抛出的 COMPLIANCE_* 码在 TS 文案表中都有对应', () => {
    const codes = rustSecurityCodes(/(?<=")COMPLIANCE_[A-Z][A-Z_]*/g);
    expect(codes).toContain('COMPLIANCE_DATA_LEAK');
    for (const code of codes) {
      expect(errorMessageOf(code), `${code} 缺文案`).not.toContain('发生错误（');
    }
  });
});

// ── 契约与三通道 ─────────────────────────────────────────────────

describe('契约常量与前端通道', () => {
  it('三个合规通道常量都存在且被消费（无死常量）', () => {
    expect(IPC_CHANNELS.COMPLIANCE_STATUS).toBe('compliance:status');
    expect(IPC_CHANNELS.COMPLIANCE_UPDATE).toBe('compliance:update');
    expect(IPC_CHANNELS.COMPLIANCE_CHECK).toBe('compliance:check');
    for (const key of ['COMPLIANCE_STATUS', 'COMPLIANCE_UPDATE', 'COMPLIANCE_CHECK']) {
      expect(contracts(), `${key} 未定义`).toContain(`'compliance:`);
    }
    expect(store()).toContain('IPC_CHANNELS.COMPLIANCE_STATUS');
    expect(store()).toContain('IPC_CHANNELS.COMPLIANCE_UPDATE');
    expect(libApi()).toContain('IPC_CHANNELS.COMPLIANCE_CHECK');
  });

  it('契约类型齐备：聚合视图 / patch / 更新响应 / 闸门明细', () => {
    const src = contracts();
    for (const t of [
      'export interface ComplianceDomainControl',
      'export interface ComplianceStatus',
      'export interface CompliancePatch',
      'export interface ComplianceUpdateResp',
      'export interface ComplianceCheckResp',
    ]) {
      expect(src).toContain(t);
    }
    expect(src).toContain('allowlistEvaluated: boolean');
    expect(src).toContain('endpointKnown: boolean');
    expect(src).toContain('changed: boolean');
  });

  it('Tauri 通道把三个 channel 映射到对应 command', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'compliance:status') return invoke('compliance_status')");
    expect(src).toContain("return invoke('compliance_update', { patch: args[0] })");
    expect(src).toContain("if (channel === 'compliance:check')");
    expect(src).toContain("return invoke('compliance_check', {");
  });

  it('本地权威通道实现三个 channel（Mock 与浏览器直连共用）', () => {
    const src = securityLocal();
    expect(src).toContain("if (channel === 'compliance:status')");
    expect(src).toContain("if (channel === 'compliance:update')");
    expect(src).toContain("if (channel === 'compliance:check')");
    // 与 Rust 同口径的出域判定（硬开关 → 白名单 → 黑名单 → 本地回环）
    expect(src).toContain('function evalDomainAllowed(');
    expect(src).toContain('POLICY_EXTERNAL_API_DISABLED');
    expect(src).toContain('POLICY_DOMAIN_NOT_ALLOWED');
    expect(src).toContain('POLICY_DOMAIN_BLOCKED');
  });

  it('Mock 与引擎直连都把 compliance: 前缀交给本地实现', () => {
    expect(mockApi()).toContain("channel.startsWith('compliance:')");
    expect(engineApi()).toContain('handleLocalSecurityChannel(channel, args)');
  });

  it('api 封装暴露 compliance 域（status/update/check）', () => {
    const src = libApi();
    expect(src).toContain('export const complianceAPI');
    expect(src).toContain('status(): Promise<ComplianceStatus>');
    expect(src).toContain('update(patch: CompliancePatch)');
    expect(src).toContain('check(goal: string, baseUrl?: string)');
    expect(src).toContain('compliance: complianceAPI,');
  });
});

// ── UI 真实消费 ──────────────────────────────────────────────────

describe('UI 消费与假状态清理', () => {
  it('store 有合规状态槽位，且失败可见（不静默回落成「一切正常」）', () => {
    const src = store();
    expect(src).toContain('compliance: ComplianceStatus | null');
    expect(src).toContain('complianceError: string | null');
    expect(src).toContain('loadCompliance: async () =>');
    expect(src).toContain('updateCompliance: async (patch) =>');
    expect(src).toContain('complianceLoading: false,');
    // 出域控制保存后同步聚合视图，两处不打架
    expect(src).toContain('await useSecurityStore.getState().loadCompliance();');
  });

  it('出域控制配置类型直接复用契约，不再双份字段声明', () => {
    expect(store()).toContain('export type DomainControlConfig = ComplianceDomainControl;');
    expect(store()).not.toMatch(/interface DomainControlConfig\s*\{[\s\S]*?hardDisableExternalAPI/);
  });

  it('合规概览面板读 status、写 update，且明示未评估语义', () => {
    const src = panel();
    expect(src).toContain('useSecurityStore()');
    expect(src).toContain('updateCompliance({ hardSwitchEnabled: !hardSwitchOn })');
    expect(src).toContain('compliance?.allowlistEvaluated');
    expect(src).toContain('未评估');
    expect(src).toContain('errorTextOf(err)');
  });

  it('安全页挂载合规概览，并在进入该页签时重新拉取', () => {
    const src = securityPage();
    expect(src).toContain("import CompliancePanel from '../../components/CompliancePanel'");
    expect(src).toContain('label="合规概览"');
    expect(src).toContain('<CompliancePanel />');
    expect(src).toContain('if (newValue === 4)');
    expect(src).toContain('loadCompliance()');
  });

  it('顶栏假开关状态已删除，改为读合规通道', () => {
    expect(appStore()).not.toContain('hardSwitchOff');
    expect(appStore()).not.toContain('toggleHardSwitch');
    expect(appStore()).not.toContain('setHardSwitchOff');
    const src = topBar();
    expect(src).not.toContain('hardSwitchOff');
    expect(src).toContain('useSecurityStore((s) => s.compliance)');
    expect(src).toContain('updateCompliance({ hardSwitchEnabled: !hardSwitchOn })');
    // 未加载时不谎报状态
    expect(src).toContain('合规状态未知');
  });

  it('全局通知有了真实渲染点（showNotification 不再是无人消费的状态）', () => {
    const src = mainLayout();
    expect(src).toContain('useAppStore((s) => s.notification)');
    expect(src).toContain('<Snackbar');
    expect(src).toContain('closeNotification');
  });

  it('Workbench 提交与重新生成前都过合规闸门', () => {
    const src = workbenchStore();
    expect(src.match(/await api\.compliance\.check\(/g)?.length).toBe(2);
    expect(src).toContain('await api.compliance.check(goal, modelConfig?.baseUrl);');
  });
});

// ── 错误码收敛 ───────────────────────────────────────────────────

describe('合规错误码收敛（不留无人抛出的码）', () => {
  const all = Object.values(ErrorCode) as string[];

  it('硬开关/模型拦截沿用 POLICY_* 码，不再重复登记 COMPLIANCE_*', () => {
    expect(all).not.toContain('COMPLIANCE_HARD_SWITCH');
    expect(all).not.toContain('COMPLIANCE_MODEL_NOT_ALLOWED');
    expect(all).toContain('COMPLIANCE_DATA_LEAK');
  });

  it('闸门拒绝原因映射为可读文案并保留诊断信息', () => {
    const text = errorTextOf(
      'COMPLIANCE_DATA_LEAK: 内容判定为confidential，机密本地化已开启，禁止发往外部端点（api.openai.com）',
    );
    expect(text).toContain('已阻止发往外部模型');
    expect(text).toContain('api.openai.com');
    expect(errorMessageOf('POLICY_EXTERNAL_API_DISABLED')).toContain('硬开关');
  });
});

// ── 行为：本地权威真实分发 ───────────────────────────────────────

async function loadLocal() {
  vi.resetModules();
  return import('../../../src/renderer/lib/security-local');
}

const ID_CARD_TEXT = '客户身份证 11010119900307123X 请处理';

describe('本地合规通道行为（与 Rust 同判定顺序）', () => {
  let handle: (channel: string, args: unknown[]) => { handled: boolean; value?: unknown };

  beforeEach(async () => {
    const mod = await loadLocal();
    handle = mod.handleLocalSecurityChannel;
  });

  function call(channel: string, ...args: unknown[]): any {
    const res = handle(channel, args);
    expect(res.handled, `${channel} 应由本地安全实现接管`).toBe(true);
    return res.value;
  }

  it('默认状态：未落库、名单未评估、无分级留痕', () => {
    const v = call('compliance:status');
    expect(v.hardSwitchEnabled).toBe(false);
    expect(v.persisted).toBe(false);
    expect(v.allowlistEvaluated).toBe(false);
    expect(v.whitelistModelIds).toEqual([]);
    expect(v.lastDataCheck).toBeNull();
    expect(v.domainControl.mode).toBe('whitelist');
  });

  it('空 patch 只读不写；未知字段与非法 mode 抛真实错误码', () => {
    expect(call('compliance:update', {})).toEqual({
      status: expect.objectContaining({ persisted: false }),
      changed: false,
    });
    expect(() => call('compliance:update', { mode: 'greylist' })).toThrow(/POLICY_INVALID_MODE/);
    expect(call('compliance:update', { hardSwitchEnabled: true }).changed).toBe(true);
  });

  it('硬开关与安全页出域控制共用同一份状态（互相可见）', () => {
    call('compliance:update', { hardSwitchEnabled: true });
    const view = call('security:getDomainControl');
    expect(view.config.hardDisableExternalAPI).toBe(true);
    expect(view.persisted).toBe(true);

    // 反方向：出域表单写入后，合规视图立即反映
    call('security:setDomainControl', {
      enabled: true,
      mode: 'blacklist',
      allowedDomains: [],
      blockedDomains: ['evil.example'],
      forceLocalForConfidential: true,
      hardDisableExternalAPI: false,
    });
    const v = call('compliance:status');
    expect(v.hardSwitchEnabled).toBe(false);
    expect(v.domainControl.mode).toBe('blacklist');
    expect(v.domainControl.blockedDomains).toEqual(['evil.example']);
  });

  it('check：端点未知时只留痕分级，不臆断出域', () => {
    const v = call('compliance:check', ID_CARD_TEXT, undefined);
    expect(v.allowed).toBe(true);
    expect(v.endpointKnown).toBe(false);
    expect(v.sensitivity).toBe('confidential');
    expect(v.forceLocal).toBe(true);
  });

  it('check：机密内容发往外部端点被拒（COMPLIANCE_DATA_LEAK）', () => {
    call('compliance:update', {
      enabled: true,
      mode: 'whitelist',
      allowedDomains: ['https://api.openai.com/v1'],
    });
    expect(() => call('compliance:check', ID_CARD_TEXT, 'https://api.openai.com/v1')).toThrow(
      /COMPLIANCE_DATA_LEAK/,
    );
    // 同一端点走本地回环则放行
    expect(call('compliance:check', ID_CARD_TEXT, 'http://localhost:11434/v1').allowed).toBe(true);
  });

  it('check：出域判定先于内容分级（与真实出站拦截点一致）', () => {
    call('compliance:update', {
      enabled: true,
      mode: 'whitelist',
      allowedDomains: ['api.deepseek.com'],
    });
    expect(() => call('compliance:check', '写一份周报', 'https://api.openai.com/v1')).toThrow(
      /POLICY_DOMAIN_NOT_ALLOWED/,
    );
    expect(call('compliance:check', '写一份周报', 'https://api.deepseek.com/v1').endpointHost).toBe(
      'api.deepseek.com',
    );
  });

  it('check：硬开关命中外部端点（POLICY_EXTERNAL_API_DISABLED），本地仍可用', () => {
    call('compliance:update', { hardSwitchEnabled: true });
    expect(() => call('compliance:check', '整理客户名单', 'https://api.deepseek.com/v1')).toThrow(
      /POLICY_EXTERNAL_API_DISABLED/,
    );
    expect(call('compliance:check', '整理客户名单', 'http://127.0.0.1:11434/v1').endpointLocal).toBe(
      true,
    );
  });

  it('闸门留痕进审计：拒绝记 warning，可在审计页按动作筛出', () => {
    call('compliance:update', { hardSwitchEnabled: true });
    expect(() => call('compliance:check', '整理客户名单', 'https://api.deepseek.com/v1')).toThrow();
    call('compliance:check', '写一份周报', 'http://localhost:11434/v1');
    const logs = call('audit:query', { action: 'compliance_check' }) as any[];
    expect(logs.length).toBe(2);
    // 审计列表按新→旧返回：最后写入的放行记录排在最前
    expect(logs[0].resource).toBe('allowed');
    expect(logs[0].severity).toBe('info');
    const blocked = logs.find((l) => l.resource === 'blocked');
    expect(blocked.severity).toBe('warning');
    expect(blocked.details).toContain('POLICY_EXTERNAL_API_DISABLED');
  });

  it('数据分级检测留痕被合规概览读到（lastDataCheck 同源）', () => {
    call('security:scanData', ID_CARD_TEXT);
    const v = call('compliance:status');
    expect(v.lastDataCheck.sensitivity).toBe('confidential');
    expect(v.lastDataCheck.summary).toContain('数据分级检测');
  });
});
