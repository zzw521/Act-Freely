/**
 * 预算可见性与告警接线测试（缺陷 D-14）
 *
 * 整改前 Rust 有配额台账、引擎也会返回 429，但渲染层既没有配置入口也没有档位展示：
 * 用户只在「任务突然交不出」时才知道超了。本文件把四段接线钉住，任何一段退回半成品都会红：
 * - 契约：预算进 AppSettingsData 与默认值，出厂日预算与 Rust 常量同数
 * - 配置：设置页有输入口，写入的是真键，保存即落 settings_kv
 * - 展示：用量页显示档位 / 剩余 / 原因 / 恢复方式，读数失败也如实说明
 * - 执行与提醒：提交前过闸门（100% 拒绝并给中文原因），任务完成落账后重读并跃迁提醒
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { DEFAULT_BUDGET } from '../../../src/shared/contracts';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const quotaGate = () => read('../../../src/shared/quota-gate.ts');
const alignApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const apiLib = () => read('../../../src/renderer/lib/api.ts');
const budgetGate = () => read('../../../src/renderer/lib/budget-gate.ts');
const notifyLocal = () => read('../../../src/renderer/lib/notify-local.ts');
const usageStore = () => read('../../../src/renderer/stores/useModelUsageStore.ts');
const workbench = () => read('../../../src/renderer/store/workbenchStore.ts');
const budgetPanel = () => read('../../../src/renderer/components/TokenBudgetPanel/index.tsx');
const modelSettings = () => read('../../../src/renderer/components/ModelSettings/index.tsx');
const usagePanel = () => read('../../../src/renderer/components/UsageOverviewPanel/index.tsx');
const rustQuota = () => read('../../../src-tauri/src/task/quota.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');

describe('契约：预算是设置的一部分，出厂值与 Rust 同口径', () => {
  it('BudgetSettings 与 DEFAULT_BUDGET 有单一来源，并进 AppSettingsData', () => {
    const src = contracts();
    expect(src).toContain('export interface BudgetSettings');
    expect(src).toContain('export const DEFAULT_BUDGET');
    const data = src.split('export interface AppSettingsData')[1].split('\n}')[0];
    expect(data).toContain('budget?: BudgetSettings');
  });

  it('出厂日预算 == Rust DEFAULT_DAILY_QUOTA_TOKENS（两侧数字不许各写各的）', () => {
    const rust = rustQuota().match(/DEFAULT_DAILY_QUOTA_TOKENS: u64 = ([\d_]+)/);
    expect(rust, 'Rust 侧常量不见了，改了名字要同步这里').toBeTruthy();
    const rustValue = Number(rust![1].replace(/_/g, ''));
    expect(DEFAULT_BUDGET.dailyTokens).toBe(rustValue);
    expect(DEFAULT_BUDGET.monthlyTokens).toBe(0);
  });

  it('本地默认值带上预算键（引擎直连刷新后不丢配置）', () => {
    const defaults = alignApi().split('const DEFAULT_SETTINGS')[1].split('};')[0];
    expect(defaults).toContain('budget:');
  });
});

describe('判定与文案：档位、原因、恢复方式只有一份', () => {
  it('四档后果与恢复方式在 quota-gate 里成文，界面不得自造说法', () => {
    const src = quotaGate();
    for (const action of ['notify', 'degrade', 'queue', 'block']) {
      expect(src).toContain(`${action}: '`);
    }
    expect(src).toContain('export function recoveryHintZh');
    expect(src).toContain('export function budgetBlockMessage');
    expect(src).toContain('恢复方式');
  });

  it('取数侧把「读不到用量」如实标成未判定，而不是当作还有余量', () => {
    const src = budgetGate();
    expect(src).toContain('unreadable');
    expect(src).toContain('errors[scope]');
    // 0 是用户的有效选择：只有缺键/非法值才回落默认
    expect(src).toContain('toNumber(budget?.dailyTokens, DEFAULT_BUDGET.dailyTokens)');
  });
});

describe('执行面：提交前过闸门，100% 拒绝', () => {
  it('chatAPI.submit 先 await 预算闸门，再发起提交', () => {
    const body = apiLib().split('export const chatAPI')[1].split('\n};')[0];
    const submit = body.split('async submit(')[1].split('\n  },')[0];
    expect(submit).toContain('await guardSubmitBudget()');
    expect(submit.indexOf('await guardSubmitBudget()')).toBeLessThan(
      submit.indexOf('getAPI().chatSubmit(req)'),
      '闸门必须在真正提交之前，不能提交完才判',
    );
  });

  it('闸门在被拦时抛出中文原因，读数失败时放行但给出可见提示', () => {
    const gate = apiLib().split('async function guardSubmitBudget')[1].split('\n}\n')[0];
    expect(gate).toContain('readBudgetVerdict');
    expect(gate).toContain('budgetBlockMessage');
    expect(gate).toContain('throw new Error');
    expect(gate).toContain('budgetUnreadableNote');
    expect(gate).toContain('showNotification');
  });

  it('恢复方式写明到点自动重置与调高预算两个出口', () => {
    const msg = quotaGate();
    expect(msg).toContain('下一个 UTC 日');
    expect(msg).toContain('下月 1 日');
    expect(msg).toContain('调高');
    expect(msg).toContain('模型与用量');
  });
});

describe('配置入口：设置页能填、填了落库', () => {
  it('模型与用量分区挂载预算面板', () => {
    expect(modelSettings()).toContain('<TokenBudgetPanel />');
    expect(modelSettings()).toContain("from '../TokenBudgetPanel'");
  });

  it('面板写入的是 settings 的 budget 键，保存走统一设置通道', () => {
    const src = budgetPanel();
    expect(src).toContain('updateAppSettings({ budget:');
    expect(src).toContain('dailyTokens:');
    expect(src).toContain('monthlyTokens:');
    expect(src).toContain('useAppSettings');
  });

  it('面板如实说明日额度与单任务预算都同步到 Rust 硬停，暂停后要人工唤回', () => {
    const src = budgetPanel();
    expect(src).toContain('quota-ledger.json');
    expect(src).toContain('同步写入桌面端 Rust');
    expect(src).toContain('单任务级预算');
    // 「还没有执行点」是整改前的状态，界面不许再这么写（执行点见 D-14 契约测试）
    expect(src).not.toContain('还没有执行点');
  });

  it('日额度保存与启动都应用到 Rust 配额台账，无需重启', () => {
    const rust = rustQuota();
    expect(rust).toContain('pub fn set_quota(&self, quota_tokens: u64)');
    expect(rust).toContain('g.quota_tokens = quota_tokens');
    const lib = rustLib();
    // 设置保存后的联动分支
    expect(lib).toContain('"budget" =>');
    expect(lib).toContain('p0.quota.set_quota(tokens)');
    // 启动时回放已保存的日额度（设置解码成 JSON 值后交给同一个解析函数）
    expect(lib).toContain('store::settings::decode_value(&v)');
    expect(lib).toContain('budget_daily_tokens(&raw)');
    expect(lib).toContain('services.quota.set_quota(tokens)');
  });
});

describe('可见性与告警：用量页看得见档位，跃迁时提醒', () => {
  it('用量页展示剩余与恢复方式，并说明读数失败的档位', () => {
    const src = usagePanel();
    expect(src).toContain('budgetEntrySummaryZh');
    expect(src).toContain('recoveryHintZh');
    expect(src).toContain('unreadable');
    expect(src).toContain('loadBudget');
  });

  it('预算读取与提醒挂在用量 store：界面刷新即重判，跃迁才发通知', () => {
    const src = usageStore();
    expect(src).toContain('readBudgetVerdict');
    expect(src).toContain('collectBudgetAlerts');
    expect(src).toContain('showNotification');
    expect(src).toContain("notifyTaskEvent('budget_warning'");
  });

  it('新增通知类型有标题，且不会误响提示音（提示音只留给任务完成）', () => {
    const src = notifyLocal();
    expect(src).toContain("'budget_warning'");
    expect(src).toContain('budget_warning: ');
    expect(src).toContain("soundNotify && kind === 'delivered'");
  });

  it('任务交付落账后重读预算，档位跃迁才会当场提醒', () => {
    const src = workbench();
    expect(src).toContain("if (kind === 'delivered') void useModelUsageStore.getState().loadBudget()");
  });
});
