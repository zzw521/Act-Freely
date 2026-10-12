/**
 * 用量总览与报表契约测试（任务 #22）
 *
 * `usage:stats` / `usage:report` 曾只是常量：preload 放行、契约里有类型，
 * 但 Rust 无 command、三条通道无分支、页面不消费，而 `UsageStats` 里还挂着
 * `avgLatencyMs` / `estimatedCost` 两个**没有任何数据来源**的字段
 * （台账只记 token 与调用次数，未采集延迟与单价）。
 * 本文件从源码与行为两侧锁定链路完整，并守住「不伪造指标」这条线：
 * - Rust：stats/report 与明细共用同一周期过滤，错误码有可读文案
 * - 契约：幽灵字段不得回归（latency/cost/price 一律不出现在用量模块）
 * - 三通道：Tauri command、引擎 HTTP、Mock 本地台账都有分支且口径一致
 * - 页面：用量页真正消费总览与报表，不再是无人调用的常量
 */

import { describe, it, expect, beforeEach, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { errorMessageOf } from '../../../src/shared/error-messages';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

const win = globalThis.window as {
  tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> };
};

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const DAY = 86_400_000;
const rustUsage = () => read('../../../src-tauri/src/model/usage.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const usageLocal = () => read('../../../src/renderer/lib/usage-local.ts');
const storeSrc = () => read('../../../src/renderer/stores/useModelUsageStore.ts');
const panelSrc = () => read('../../../src/renderer/components/UsageOverviewPanel/index.tsx');
const modelHub = () => read('../../../src/renderer/pages/ModelHub/index.tsx');
const preloadAllow = () => read('../../../src/preload/index.ts');
const preloadDts = () => read('../../../src/preload/index.d.ts');
const engineServer = () => read('../../../engine/server.py');

/** snake_case 字段 → camelCase（Rust serde rename_all 的等价变换） */
function toCamel(name: string): string {
  return name.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());
}

/** 取 `pub struct X { ... }` 的字段名列表 */
function rustFields(src: string, structName: string): string[] {
  const body = src.split(`pub struct ${structName}`)[1]?.split('}')[0] ?? '';
  return [...body.matchAll(/^\s*pub (\w+):/gm)].map((m) => m[1]);
}

let usage: typeof import('../../../src/renderer/lib/usage-local');
let useModelUsageStore: typeof import('../../../src/renderer/stores/useModelUsageStore').useModelUsageStore;
let invokeChannel: (channel: string, ...args: unknown[]) => Promise<unknown>;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  usage = await import('../../../src/renderer/lib/usage-local');
  useModelUsageStore = (await import('../../../src/renderer/stores/useModelUsageStore'))
    .useModelUsageStore;
  invokeChannel = (channel, ...args) => win.tongtianAPI!.invoke(channel, ...args);
});

beforeEach(() => {
  usage.resetLocalUsage();
  useModelUsageStore.setState({
    period: 'monthly',
    stats: [],
    overview: null,
    overviewError: null,
    report: null,
    reportLoading: false,
    reportError: null,
    error: null,
    loading: false,
  });
});

// ── Rust 侧：同一台账的三种读法 ───────────────────────────────

describe('Rust 用量总览与报表', () => {
  it('总览与趋势复用明细的周期过滤，杜绝三套口径', () => {
    const src = rustUsage();
    expect(src).toMatch(/fn period_filter\(/);
    const queryBody = src.split('pub fn query(')[1].split('pub fn stats(')[0];
    const pointsBody = src.split('fn daily_points(')[1].split('pub fn report(')[0];
    expect(queryBody).toContain('period_filter(&period, now)');
    expect(pointsBody).toContain('period_filter(period, now)');
    // 时间窗只允许有一处实现（复制第二份就会漂移）
    expect((src.match(/WHERE u\.day = \?1/g) ?? []).length).toBe(1);
  });

  it('用量模块不出现延迟/成本/单价等无来源指标', () => {
    // 台账只有 token 与调用次数：模型连通性测试的 latencyMs 属于另一条链路，不在此列
    const lower = rustUsage().toLowerCase();
    for (const ghost of ['latency', 'cost', 'price', 'avg_ms']) {
      expect(lower, `字段 ${ghost} 没有台账数据来源`).not.toContain(ghost);
    }
    expect(contracts()).not.toMatch(/avgLatencyMs|estimatedCost/);
    expect(sharedTypes()).not.toMatch(/interface UsageStats\b/);
  });

  it('返回体结构全部 camelCase 且字段与前端契约同名', () => {
    const src = rustUsage();
    const camelStructs = [...src.matchAll(/#\[serde\(rename_all = "camelCase"\)\]\s*pub struct (\w+)/g)]
      .map((m) => m[1]);
    expect(camelStructs).toEqual(
      expect.arrayContaining(['UsageRow', 'UsageTotals', 'StatsView', 'DayPoint', 'ReportView'])
    );
    expect(rustFields(src, 'UsageTotals').map(toCamel).sort()).toEqual(
      ['activeModels', 'calls', 'completionTokens', 'lastUsedAtMs', 'promptTokens', 'tokensUsed'].sort()
    );
    expect(rustFields(src, 'StatsView').map(toCamel).sort()).toEqual(['period', 'topModel', 'totals']);
    expect(rustFields(src, 'DayPoint').map(toCamel).sort()).toEqual(['calls', 'day', 'models', 'tokensUsed']);
    expect(rustFields(src, 'ReportView').map(toCamel).sort()).toEqual(
      ['days', 'generatedAtMs', 'models', 'period', 'totals'].sort()
    );
  });

  it('趋势查询按日分组、时间窗走参数绑定（不拼接字面量）', () => {
    const body = rustUsage().split('fn daily_points(')[1].split('pub fn report(')[0];
    expect(body).toContain('GROUP BY u.day');
    expect(body).toContain('COUNT(DISTINCT u.model_name)');
    expect(body).toContain('query_map([a], map)');
    expect(body).not.toMatch(/execute\(\s*format!/);
    // 报表失败使用自己的错误码，便于前端区分「明细失败」与「趋势失败」
    expect(body).toContain('USAGE_REPORT_FAILED');
  });

  it('command 已实现并注册，读法入口成对出现', () => {
    const lib = rustLib();
    expect(lib).toContain('async fn usage_stats(');
    expect(lib).toContain('async fn usage_report(');
    expect(lib).toContain('model::usage::stats(&p0.store, &period)');
    expect(lib).toContain('model::usage::report(&p0.store, &period)');
    expect(lib).toContain('usage_stats,');
    expect(lib).toContain('usage_report,');
  });

  it('Rust 用量错误码在 TS 文案表中都有可读文案', () => {
    const codes = [
      ...new Set([...(rustUsage() + rustLib()).matchAll(/(?<=")USAGE_[A-Z][A-Z_]*/g)].map((m) => m[0])),
    ];
    expect(codes.length).toBeGreaterThan(0);
    for (const code of codes) {
      expect(errorMessageOf(code), `错误码 ${code} 缺少可读文案`).not.toContain(code);
    }
  });
});

// ── 契约常量与三条通道 ───────────────────────────────────────

describe('契约与三通道接入', () => {
  it('契约里的用量类型不再只是摆设', () => {
    const src = contracts();
    for (const name of ['UsageTotals', 'UsageStats', 'UsageDayPoint', 'UsageReport']) {
      expect(src).toContain(`export interface ${name}`);
    }
    expect(src).toContain("USAGE_STATS: 'usage:stats'");
    expect(src).toContain("USAGE_REPORT: 'usage:report'");
    // 总览含 topModel 且明确可空（无用量不伪造第一名）
    expect(src).toMatch(/topModel: string \| null/);
  });

  it('api 封装暴露 usage.stats / usage.report 并注册到统一入口', () => {
    const src = libApi();
    expect(src).toContain('export const usageAPI');
    expect(src).toContain('IPC_CHANNELS.USAGE_STATS');
    expect(src).toContain('IPC_CHANNELS.USAGE_REPORT');
    expect(src).toContain('usage: usageAPI,');
  });

  it('Tauri 通道映射到 usage_stats / usage_report command', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'usage:stats')");
    expect(src).toContain("if (channel === 'usage:report')");
    expect(src).toContain("invoke('usage_stats', { period: args[0] as string })");
    expect(src).toContain("invoke('usage_report', { period: args[0] as string })");
  });

  it('浏览器直连引擎：总览按引擎明细行换算，报表走引擎报表端点', () => {
    const src = engineApi();
    expect(src).toContain("if (channel === 'usage:stats')");
    expect(src).toContain('statsFromRows(period, await fetchUsageRows(period))');
    expect(src).toContain('/api/usage/report?period=');
    expect(src).toContain('USAGE_REPORT_FAILED');
    // 引擎侧确实提供报表端点与按日/汇总算法（不是前端凭空拼）
    const py = engineServer();
    expect(py).toContain('@app.get("/api/usage/report")');
    expect(py).toContain('def days(self, period: str)');
    expect(py).toContain('def totals(self, period: str');
    expect(py).toContain('USAGE_LEDGER.days(p)');
  });

  it('开发 Mock：两条通道由本地台账接管（Mock 开关规范）', () => {
    const src = mockApi();
    expect(src).toContain("channel === 'usage:stats' || channel === 'usage:report'");
    expect(src).toContain("if (channel === 'usage:stats')");
    expect(src).toContain('statsFromRows(period, localUsageRows(period))');
    expect(src).toContain('localReportView(normalizePeriod(arg))');
  });

  it('preload 白名单与类型声明同步（不留旧签名误导调用方）', () => {
    expect(preloadAllow()).toContain('IPC_CHANNELS.USAGE_STATS');
    expect(preloadAllow()).toContain('IPC_CHANNELS.USAGE_REPORT');
    const dts = preloadDts();
    expect(dts).toContain('usageStats(period?: UsagePeriod): Promise<UsageStats>');
    expect(dts).toContain('usageReport(period?: UsagePeriod): Promise<UsageReport>');
    expect(dts).not.toContain('usageStats(modelId?: string)');
  });

  it('用量页有真实消费方：总览面板挂在用量页签下', () => {
    expect(modelHub()).toContain("import UsageOverviewPanel from '../../components/UsageOverviewPanel'");
    expect(modelHub()).toContain('{activeTab === 1 && <UsageOverviewPanel />}');
    const panel = panelSrc();
    expect(panel).toContain('loadOverview');
    expect(panel).toContain('loadReport');
    expect(panel).toContain('reportMarkdown');
    // 口径注释随行：避免后人再往报表里补「成本」
    expect(panel).toContain('未采集调用延迟与模型单价');
    expect(panel).toContain('生成报表');
    expect(storeSrc()).toContain('api.usage.stats(period)');
    expect(storeSrc()).toContain('api.usage.report(period)');
  });
});

// ── 本地实现：总览 / 趋势 / 报表口径 ──────────────────────────

describe('usage-local：汇总、趋势与报表', () => {
  it('空明细汇总为零值，不伪造活跃模型与最近使用时间', () => {
    expect(usage.totalsOf([])).toEqual({
      promptTokens: 0,
      completionTokens: 0,
      tokensUsed: 0,
      calls: 0,
      activeModels: 0,
      lastUsedAtMs: 0,
    });
    expect(usage.statsFromRows('daily', [])).toEqual({
      period: 'daily',
      totals: usage.totalsOf([]),
      topModel: null,
    });
  });

  it('totalsOf 与 Rust totals_of 同口径：逐项累加 + 时间取最大', () => {
    const rows = usage.extractUsageRows([
      { modelName: 'a', displayName: '模型A', promptTokens: 100, completionTokens: 50, calls: 2, lastUsedAtMs: 5 },
      { modelName: 'b', promptTokens: 20, completionTokens: 10, calls: 3, lastUsedAtMs: 9 },
    ]);
    expect(usage.totalsOf(rows)).toEqual({
      promptTokens: 120,
      completionTokens: 60,
      tokensUsed: 180,
      calls: 5,
      activeModels: 2,
      lastUsedAtMs: 9,
    });
    // topModel 用展示名，缺名回落模型名
    expect(usage.statsFromRows('total', rows).topModel).toBe('模型A');
    expect(usage.statsFromRows('total', rows.slice(1)).topModel).toBe('b');
  });

  it('本地趋势按 UTC 日升序分桶，当日模型数按去重统计', () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 60, 40, { atMs: now });
    usage.recordLocalUsage('qwen-max', 10, 10, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 25, 25, { atMs: now - DAY });
    usage.recordLocalUsage('qwen-plus', 999, 1, { atMs: now - 40 * DAY });

    const all = usage.localDayPoints('total', now);
    expect(all.map((p) => p.day)).toEqual([...all.map((p) => p.day)].sort());
    expect(all.length).toBe(3);
    expect(all[all.length - 1]).toEqual({ day: usage.utcDayOf(now), tokensUsed: 120, calls: 2, models: 2 });

    const daily = usage.localDayPoints('daily', now);
    expect(daily.length).toBe(1);
    expect(daily[0].tokensUsed).toBe(120);

    const monthly = usage.localDayPoints('monthly', now);
    const month = usage.utcMonthOf(now);
    expect(monthly.every((p) => p.day.startsWith(month))).toBe(true);
    expect(monthly.some((p) => p.day === usage.utcDayOf(now - 40 * DAY))).toBe(false);
  });

  it('报表内部自洽：趋势合计 == 明细合计 == totals', () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 60, 40, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 5, 5, { atMs: now - DAY });
    usage.recordLocalUsage('qwen-max', 2, 1, { atMs: now - DAY });

    const rep = usage.localReportView('total', now);
    const daySum = rep.days.reduce((s, d) => s + d.tokensUsed, 0);
    const modelSum = rep.models.reduce((s, m) => s + m.tokensUsed, 0);
    expect(daySum).toBe(rep.totals.tokensUsed);
    expect(modelSum).toBe(rep.totals.tokensUsed);
    expect(rep.period).toBe('total');
    expect(rep.generatedAtMs).toBe(now);
    expect(rep.totals.calls).toBe(3);
  });

  it('extractReport 归一化引擎返回体：缺字段按零处理，汇总一律按明细重算', () => {
    const rep = usage.extractReport(
      {
        ok: true,
        period: 'monthly',
        generatedAtMs: 111,
        days: [{ day: '2026-10-01', tokensUsed: '30', calls: 2, models: 1 }, { day: 'bad' }],
        models: [{ modelName: 'qwen-plus', promptTokens: 20, completionTokens: 10, calls: 2, lastUsedAtMs: 7 }],
        totals: { tokensUsed: 999999 },
      },
      'monthly',
      222
    );
    expect(rep.generatedAtMs).toBe(111);
    expect(rep.days).toEqual([
      { day: '2026-10-01', tokensUsed: 30, calls: 2, models: 1 },
      { day: 'bad', tokensUsed: 0, calls: 0, models: 0 },
    ]);
    // 引擎自带的 totals 不可信时按明细重算，保证三块口径一致
    expect(rep.totals).toEqual(usage.totalsOf(rep.models));

    const empty = usage.extractReport(null, 'daily', 333);
    expect(empty.days).toEqual([]);
    expect(empty.models).toEqual([]);
    expect(empty.generatedAtMs).toBe(333);
    expect(empty.totals.activeModels).toBe(0);
  });

  it('报表文本口径完整：周期、汇总、按日趋势、模型明细，且不出现成本', () => {
    const now = Date.UTC(2026, 9, 8, 12);
    usage.recordLocalUsage('qwen-plus', 1000, 500, { displayName: '通义千问', atMs: now });
    const md = usage.reportToMarkdown(usage.localReportView('monthly', now));
    expect(md).toContain('# 通天办公 模型用量报表（本月）');
    expect(md).toContain('## 按日趋势');
    expect(md).toContain('## 模型明细');
    expect(md).toContain('通义千问');
    expect(md).toContain('1,500');
    expect(md).toContain('台账未采集调用延迟与模型单价');
    expect(md).not.toMatch(/[¥$]/);
  });

  it('空台账报表只写「暂无用量记录」，不产出空表误导', () => {
    const md = usage.reportToMarkdown(usage.localReportView('daily'));
    expect(md).toContain('暂无用量记录。');
    expect(md).not.toContain('## 模型明细');
  });

  it('周期非法一律拒绝（与 Rust USAGE_INVALID_PERIOD 同语义，不静默降级）', () => {
    expect(() => usage.localReportView('hourly' as never)).toThrow(/USAGE_INVALID_PERIOD/);
    expect(() => usage.normalizePeriod(123)).toThrow(/USAGE_INVALID_PERIOD/);
  });
});

// ── Mock 通道端到端：常量不再是死通道 ─────────────────────────

describe('Mock 通道：usage:stats / usage:report 返回真实台账', () => {
  it('总览与本地换算结果一致（同一份台账、同一个映射）', async () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 100, 50, { displayName: '通义千问', atMs: now });
    usage.recordLocalUsage('qwen-max', 7, 3, { atMs: now });

    const resp = (await invokeChannel('usage:stats', 'total')) as ReturnType<
      typeof usage.statsFromRows
    >;
    expect(resp).toEqual(usage.statsFromRows('total', usage.localUsageRows('total', now)));
    expect(resp.totals.tokensUsed).toBe(160);
    expect(resp.totals.activeModels).toBe(2);
    expect(resp.topModel).toBe('通义千问');
  });

  it('报表含趋势、明细与汇总三块，且三者自洽', async () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 60, 40, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 5, 5, { atMs: now - DAY });

    const rep = (await invokeChannel('usage:report', 'total')) as ReturnType<
      typeof usage.localReportView
    >;
    expect(rep.period).toBe('total');
    expect(rep.days.length).toBe(2);
    expect(rep.days.reduce((s, d) => s + d.tokensUsed, 0)).toBe(rep.totals.tokensUsed);
    expect(rep.models[0].modelName).toBe('qwen-plus');
    expect(usage.reportToMarkdown(rep)).toContain('模型用量报表（总计）');
  });

  it('非法周期经通道返回可读文案', async () => {
    await expect(invokeChannel('usage:stats', 'hourly')).rejects.toThrow(/USAGE_INVALID_PERIOD/);
    await expect(invokeChannel('usage:report', 'hourly')).rejects.toThrow(/USAGE_INVALID_PERIOD/);
  });
});

// ── store：总览与报表状态 ─────────────────────────────────────

describe('useModelUsageStore：总览与报表', () => {
  it('loadOverview 拉取周期总览', async () => {
    usage.recordLocalUsage('qwen-plus', 30, 20, { displayName: '千问', atMs: Date.now() });
    useModelUsageStore.setState({ period: 'total' });
    await useModelUsageStore.getState().loadOverview();

    const state = useModelUsageStore.getState();
    expect(state.overviewError).toBeNull();
    expect(state.overview?.totals.tokensUsed).toBe(50);
    expect(state.overview?.topModel).toBe('千问');
  });

  it('总览失败清空旧值并给出可读错误（不保留上一周期数据）', async () => {
    usage.recordLocalUsage('qwen-plus', 10, 10);
    await useModelUsageStore.getState().loadOverview();
    expect(useModelUsageStore.getState().overview).not.toBeNull();

    useModelUsageStore.setState({ period: 'hourly' as never });
    await useModelUsageStore.getState().loadOverview();

    const state = useModelUsageStore.getState();
    expect(state.overview).toBeNull();
    expect(state.overviewError).toContain('用量统计周期非法');
  });

  it('切换周期作废总览与报表，避免与新周期错配', () => {
    useModelUsageStore.setState({
      overview: { period: 'daily', totals: usage.totalsOf([]), topModel: null },
      report: usage.localReportView('daily'),
    });
    useModelUsageStore.getState().setPeriod('total');

    const state = useModelUsageStore.getState();
    expect(state.period).toBe('total');
    expect(state.overview).toBeNull();
    expect(state.report).toBeNull();
    expect(state.reportError).toBeNull();
  });

  it('loadReport 生成报表后 reportMarkdown 输出可导出的文本', async () => {
    usage.recordLocalUsage('qwen-plus', 40, 10, { displayName: '千问', atMs: Date.now() });
    useModelUsageStore.setState({ period: 'total' });

    const rep = await useModelUsageStore.getState().loadReport();
    expect(rep?.totals.tokensUsed).toBe(50);
    expect(useModelUsageStore.getState().reportLoading).toBe(false);
    expect(useModelUsageStore.getState().reportMarkdown()).toContain('模型用量报表（总计）');
  });

  it('报表生成失败时清空报表并给出可读错误', async () => {
    useModelUsageStore.setState({ period: 'weekly' as never });
    const rep = await useModelUsageStore.getState().loadReport();
    const state = useModelUsageStore.getState();
    expect(rep).toBeNull();
    expect(state.report).toBeNull();
    expect(state.reportError).toContain('用量统计周期非法');
    expect(state.reportLoading).toBe(false);
  });

  it('迟到的旧周期报表结果被丢弃（用户已切周期）', async () => {
    usage.recordLocalUsage('qwen-plus', 10, 10, { atMs: Date.now() });
    useModelUsageStore.setState({ period: 'total' });

    const pending = useModelUsageStore.getState().loadReport();
    useModelUsageStore.setState({ period: 'daily' });
    const rep = await pending;

    expect(rep).toBeNull();
    expect(useModelUsageStore.getState().report).toBeNull();
  });

  it('无报表时 reportMarkdown 返回 null（面板不显示空文本区）', () => {
    expect(useModelUsageStore.getState().reportMarkdown()).toBeNull();
  });

  it('切换周期后明细随新周期重查，总览/报表不残留旧周期读数', async () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 10, 0, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 90, 0, { atMs: now - 40 * DAY });

    useModelUsageStore.getState().setPeriod('total');
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats[0].tokensUsed).toBe(100);

    useModelUsageStore.getState().setPeriod('daily');
    // setPeriod 已把总览与报表清空，面板必须先拿到新周期的读数
    expect(useModelUsageStore.getState().overview).toBeNull();
    expect(useModelUsageStore.getState().report).toBeNull();
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats[0].tokensUsed).toBe(10);
  });
});

// ── 数据保护：只读台账，不写不改 ──────────────────────────────

describe('读法不改动台账', () => {
  it('总览与报表查询前后，本地台账行数不变（纯读）', async () => {
    usage.recordLocalUsage('qwen-plus', 10, 5, { atMs: Date.now() });
    const before = usage.localUsageRows('total');
    await invokeChannel('usage:stats', 'total');
    await invokeChannel('usage:report', 'total');
    expect(usage.localUsageRows('total')).toEqual(before);
  });

  it('Rust 侧总览与报表只做 SELECT，不写库（存量数据不受影响）', () => {
    const src = rustUsage().split('#[cfg(test)]')[0].split('pub fn stats(')[1];
    expect(src).not.toMatch(/INSERT INTO|UPDATE |DELETE FROM|DROP TABLE/);
  });
});
