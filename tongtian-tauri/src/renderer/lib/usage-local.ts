/**
 * 模型用量统计 · 本地台账与映射（无 Rust 通道共用实现）
 *
 * 三条通道的权威数据源：
 * - 桌面应用（联调/生产）：Rust command `model_usage` / `usage_stats` / `usage_report`
 *   （SQLite `model_usage` 表，跨重启保留）
 * - 浏览器直连引擎（VITE_MOCK=false）：引擎 `GET /api/usage`、`GET /api/usage/report`（引擎进程内台账）
 * - 开发 Mock：本模块进程内台账（刷新即重置，不触碰任何真实数据）
 *
 * 本模块同时提供「原始行 → ModelUsageStats」与「报表 → Markdown 文本」的共用映射，
 * 保证三条通道展示与导出的口径一致；
 * 时间分桶与 Rust/引擎同口径：UTC 日 `yyyy-mm-dd`、UTC 月 `yyyy-mm`。
 */

import type {
  ModelUsageRow,
  ModelUsageView,
  UsageCost,
  UsageDayPoint,
  UsagePeriod,
  UsageReport,
  UsageStats,
  UsageTotals,
} from '../../shared/contracts';
import type { ModelUsageStats } from '../components/ModelStatsPanel';

const PERIODS: UsagePeriod[] = ['daily', 'monthly', 'total'];

/** 周期校验：非法值抛错（与 Rust `USAGE_INVALID_PERIOD` 同语义，不静默降级） */
export function normalizePeriod(value: unknown): UsagePeriod {
  if (typeof value === 'string' && (PERIODS as string[]).includes(value)) {
    return value as UsagePeriod;
  }
  throw new Error(`USAGE_INVALID_PERIOD: ${String(value)}`);
}

/** UTC 日（与 Rust `model::usage::utc_day` 同口径） */
export function utcDayOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** UTC 月（与 Rust `model::usage::utc_month` 同口径） */
export function utcMonthOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 7);
}

// ── 进程内台账（仅开发 Mock 模式使用） ────────────────────────

interface LocalUsageRow {
  modelName: string;
  displayName: string;
  /** UTC 日，累加分桶键 */
  day: string;
  promptTokens: number;
  completionTokens: number;
  tokensUsed: number;
  calls: number;
  lastUsedAtMs: number;
  latencyMsTotal: number;
  latencySamples: number;
  costs: UsageCost[];
  pricedCalls: number;
}

const ledger = new Map<string, LocalUsageRow>();

function bucketKey(modelName: string, day: string): string {
  return `${modelName}\u0000${day}`;
}

function mergeCost(target: UsageCost[], source: UsageCost[] | undefined): UsageCost[] {
  for (const cost of source ?? []) {
    const hit = target.find((c) => c.currency === cost.currency);
    if (hit) hit.micros += cost.micros;
    else target.push({ ...cost });
  }
  return target.sort((a, b) => a.currency.localeCompare(b.currency));
}

/** 记一次模型调用（按「模型 × UTC 日」增量累加；模型名为空时忽略） */
export function recordLocalUsage(
  modelName: string,
  promptTokens: number,
  completionTokens: number,
  opts: {
    displayName?: string;
    atMs?: number;
    latencyMs?: number;
    costMicros?: number | null;
    currency?: string;
    priced?: boolean;
  } = {}
): void {
  const name = modelName.trim();
  if (!name) return;
  const atMs = opts.atMs ?? Date.now();
  const day = utcDayOf(atMs);
  const key = bucketKey(name, day);
  const row = ledger.get(key) ?? {
    modelName: name,
    displayName: opts.displayName ?? name,
    day,
    promptTokens: 0,
    completionTokens: 0,
    tokensUsed: 0,
    calls: 0,
    lastUsedAtMs: 0,
    latencyMsTotal: 0,
    latencySamples: 0,
    costs: [],
    pricedCalls: 0,
  };
  row.promptTokens += Math.max(0, promptTokens);
  row.completionTokens += Math.max(0, completionTokens);
  row.tokensUsed = row.promptTokens + row.completionTokens;
  row.calls += 1;
  row.lastUsedAtMs = atMs;
  if (opts.latencyMs !== undefined && opts.latencyMs !== null) {
    row.latencyMsTotal += Math.max(0, opts.latencyMs);
    row.latencySamples += 1;
  }
  if (opts.priced && typeof opts.costMicros === 'number' && opts.costMicros >= 0) {
    const currency = (opts.currency || 'CNY').trim().toUpperCase() || 'CNY';
    mergeCost(row.costs, [{ currency, micros: opts.costMicros }]);
    row.pricedCalls += 1;
  }
  if (opts.displayName) row.displayName = opts.displayName;
  ledger.set(key, row);
}

/** 按周期聚合（daily=今日 / monthly=本月 / total=全部），按总 token 倒序 */
export function localUsageRows(period: UsagePeriod, nowMs = Date.now()): ModelUsageRow[] {
  const day = utcDayOf(nowMs);
  const month = utcMonthOf(nowMs);
  const agg = new Map<
    string,
    {
      modelName: string;
      displayName: string;
      promptTokens: number;
      completionTokens: number;
      tokensUsed: number;
      calls: number;
      lastUsedAtMs: number;
      latencyMsTotal: number;
      latencySamples: number;
      costs: UsageCost[];
      pricedCalls: number;
    }
  >();
  for (const row of ledger.values()) {
    if (period === 'daily' && row.day !== day) continue;
    if (period === 'monthly' && !row.day.startsWith(month)) continue;
    const cur = agg.get(row.modelName) ?? {
      modelName: row.modelName,
      displayName: row.displayName,
      promptTokens: 0,
      completionTokens: 0,
      tokensUsed: 0,
      calls: 0,
      lastUsedAtMs: 0,
      latencyMsTotal: 0,
      latencySamples: 0,
      costs: [],
      pricedCalls: 0,
    };
    cur.promptTokens += row.promptTokens;
    cur.completionTokens += row.completionTokens;
    cur.tokensUsed += row.tokensUsed;
    cur.calls += row.calls;
    cur.lastUsedAtMs = Math.max(cur.lastUsedAtMs, row.lastUsedAtMs);
    cur.latencyMsTotal += row.latencyMsTotal;
    cur.latencySamples += row.latencySamples;
    mergeCost(cur.costs, row.costs);
    cur.pricedCalls += row.pricedCalls;
    agg.set(row.modelName, cur);
  }
  return [...agg.values()]
    .map((r) => ({
      modelName: r.modelName,
      displayName: r.displayName,
      promptTokens: r.promptTokens,
      completionTokens: r.completionTokens,
      tokensUsed: r.tokensUsed,
      calls: r.calls,
      lastUsedAtMs: r.lastUsedAtMs,
      avgLatencyMs: r.latencySamples ? Math.round(r.latencyMsTotal / r.latencySamples) : null,
      latencySamples: r.latencySamples,
      costs: r.costs,
      pricedCalls: r.pricedCalls,
      unpricedCalls: Math.max(0, r.calls - r.pricedCalls),
    }))
    .sort((a, b) => b.tokensUsed - a.tokensUsed || a.modelName.localeCompare(b.modelName));
}

/** 本地台账视图（Mock 模式 `model:usage` 返回值） */
export function localUsageView(period: UsagePeriod, nowMs = Date.now()): ModelUsageView {
  return { period, rows: localUsageRows(period, nowMs) };
}

// ── 汇总 / 趋势 / 报表（三通道共用口径） ──────────────────────

/**
 * 明细行 → 周期汇总（与 Rust `model::usage::totals_of` 逐字段一致）。
 * 平均耗时按真实延迟样本数加权；成本按币种合并，未计价调用单独计数（不当 0 元）。
 */
export function totalsOf(rows: ModelUsageRow[]): UsageTotals {
  const totals: UsageTotals = {
    promptTokens: 0,
    completionTokens: 0,
    tokensUsed: 0,
    calls: 0,
    activeModels: 0,
    lastUsedAtMs: 0,
    avgLatencyMs: null,
    latencySamples: 0,
    costs: [],
    pricedCalls: 0,
    unpricedCalls: 0,
  };
  let weightedLatency = 0;
  for (const r of rows) {
    totals.promptTokens += r.promptTokens;
    totals.completionTokens += r.completionTokens;
    totals.tokensUsed += r.tokensUsed;
    totals.calls += r.calls;
    totals.activeModels += 1;
    totals.lastUsedAtMs = Math.max(totals.lastUsedAtMs, r.lastUsedAtMs);
    const samples = r.latencySamples ?? 0;
    totals.latencySamples! += samples;
    weightedLatency += (r.avgLatencyMs ?? 0) * samples;
    mergeCost(totals.costs!, r.costs);
    totals.pricedCalls! += r.pricedCalls ?? 0;
    totals.unpricedCalls! += r.unpricedCalls ?? 0;
  }
  totals.avgLatencyMs = totals.latencySamples! > 0 ? Math.round(weightedLatency / totals.latencySamples!) : null;
  return totals;
}

/** 由明细行组装总览（浏览器直连引擎时用：`/api/usage` 行 → `usage:stats` 返回体） */
export function statsFromRows(period: UsagePeriod, rows: ModelUsageRow[]): UsageStats {
  return { period, totals: totalsOf(rows), topModel: rows[0]?.displayName || rows[0]?.modelName || null };
}

/** 本地台账按 UTC 日分桶的趋势点（与 Rust `daily_points` 同周期过滤、同日升序） */
export function localDayPoints(period: UsagePeriod, nowMs = Date.now()): UsageDayPoint[] {
  const day = utcDayOf(nowMs);
  const month = utcMonthOf(nowMs);
  const buckets = new Map<
    string,
    {
      day: string;
      tokensUsed: number;
      calls: number;
      names: Set<string>;
      latencyMsTotal: number;
      latencySamples: number;
      costs: UsageCost[];
      pricedCalls: number;
    }
  >();
  for (const row of ledger.values()) {
    if (period === 'daily' && row.day !== day) continue;
    if (period === 'monthly' && !row.day.startsWith(month)) continue;
    const cur = buckets.get(row.day) ?? {
      day: row.day,
      tokensUsed: 0,
      calls: 0,
      names: new Set<string>(),
      latencyMsTotal: 0,
      latencySamples: 0,
      costs: [],
      pricedCalls: 0,
    };
    cur.tokensUsed += row.tokensUsed;
    cur.calls += row.calls;
    cur.names.add(row.modelName);
    cur.latencyMsTotal += row.latencyMsTotal;
    cur.latencySamples += row.latencySamples;
    mergeCost(cur.costs, row.costs);
    cur.pricedCalls += row.pricedCalls;
    buckets.set(row.day, cur);
  }
  return [...buckets.values()]
    .map((p) => ({
      day: p.day,
      tokensUsed: p.tokensUsed,
      calls: p.calls,
      models: p.names.size,
      avgLatencyMs: p.latencySamples ? Math.round(p.latencyMsTotal / p.latencySamples) : null,
      latencySamples: p.latencySamples,
      costs: p.costs,
      pricedCalls: p.pricedCalls,
      unpricedCalls: Math.max(0, p.calls - p.pricedCalls),
    }))
    .sort((a, b) => a.day.localeCompare(b.day));
}

/** 本地报表视图（Mock 模式 `usage:report` 返回值；非法周期同 Rust 一律拒绝） */
export function localReportView(period: UsagePeriod, nowMs = Date.now()): UsageReport {
  const p = normalizePeriod(period);
  const models = localUsageRows(p, nowMs);
  return { period: p, generatedAtMs: nowMs, days: localDayPoints(p, nowMs), models, totals: totalsOf(models) };
}

const PERIOD_LABEL: Record<UsagePeriod, string> = { daily: '今日', monthly: '本月', total: '总计' };

function fmtNum(n: number): string {
  return n.toLocaleString('en-US');
}

function fmtTime(ms: number): string {
  return ms ? new Date(ms).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '—';
}

/** micros → 货币单位（去掉尾随 0；极小额保留 6 位，不把真实成本舍成 0） */
function fmtMicros(micros: number): string {
  const unit = micros / 1_000_000;
  if (!Number.isFinite(unit) || unit <= 0) return '0';
  const fixed = unit < 0.01 ? unit.toFixed(6) : unit.toFixed(4);
  return fixed.replace(/0+$/, '').replace(/\.$/, '');
}

/** 成本按币种分列；没有计价记录时写「未计价」而不是 0 元 */
export function fmtCosts(costs: UsageCost[] | undefined, pricedCalls = 0): string {
  const list = costs ?? [];
  if (list.length) return list.map((c) => `${c.currency} ${fmtMicros(c.micros)}`).join(' / ');
  return pricedCalls > 0 ? '—' : '未计价';
}

/** 平均耗时展示：没有延迟样本时写「—」 */
export function fmtLatency(ms: number | null | undefined, samples = 0): string {
  if (!samples || ms === null || ms === undefined) return '—';
  return `${fmtNum(ms)} ms`;
}

/**
 * 报表 → Markdown 文本（三通道共用的导出格式，数字全部来自台账）。
 * 空台账明确写「暂无用量记录」，不产出空表误导。
 */
export function reportToMarkdown(report: UsageReport): string {
  const t = report.totals;
  const latency = fmtLatency(t.avgLatencyMs, t.latencySamples ?? 0);
  const cost = fmtCosts(t.costs, t.pricedCalls ?? 0);
  const unpriced = t.unpricedCalls ?? 0;
  const lines: string[] = [
    `# 通天办公 模型用量报表（${PERIOD_LABEL[report.period]}）`,
    '',
    `- 生成时间：${fmtTime(report.generatedAtMs)}`,
    `- 汇总：Token ${fmtNum(t.tokensUsed)}（输入 ${fmtNum(t.promptTokens)} / 输出 ${fmtNum(t.completionTokens)}）`,
    `- 调用：${fmtNum(t.calls)} 次 · 活跃模型 ${t.activeModels} 个 · 最近使用 ${fmtTime(t.lastUsedAtMs)}`,
    `- 平均耗时：${latency}（按 ${fmtNum(t.latencySamples ?? 0)} 次真实耗时样本加权）`,
    `- 成本：${cost}（已计价 ${fmtNum(t.pricedCalls ?? 0)} 次${unpriced > 0 ? `；未计价 ${fmtNum(unpriced)} 次未计入` : ''}）`,
    '- 口径：Token 与耗时来自引擎上报的真实调用；成本 = Token × 调用时配置的单价；' +
      '未配置单价的调用标为「未计价」而非 0 元，不同币种不合并',
  ];
  if (!report.models.length) {
    lines.push('', '暂无用量记录。');
    return lines.join('\n');
  }
  lines.push(
    '',
    '## 按日趋势',
    '',
    '| UTC 日 | Token | 调用 | 活跃模型 | 平均耗时 | 成本 |',
    '| --- | ---: | ---: | ---: | ---: | --- |'
  );
  for (const d of report.days) {
    lines.push(
      `| ${d.day} | ${fmtNum(d.tokensUsed)} | ${d.calls} | ${d.models} | ` +
        `${fmtLatency(d.avgLatencyMs, d.latencySamples ?? 0)} | ${fmtCosts(d.costs, d.pricedCalls ?? 0)} |`
    );
  }
  lines.push(
    '',
    '## 模型明细',
    '',
    '| 模型 | 输入 Token | 输出 Token | 合计 | 调用 | 平均耗时 | 成本 | 最近使用 |',
    '| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |'
  );
  for (const m of report.models) {
    lines.push(
      `| ${m.displayName || m.modelName} | ${fmtNum(m.promptTokens)} | ${fmtNum(m.completionTokens)} | ` +
        `${fmtNum(m.tokensUsed)} | ${m.calls} | ${fmtLatency(m.avgLatencyMs, m.latencySamples ?? 0)} | ` +
        `${fmtCosts(m.costs, m.pricedCalls ?? 0)} | ${fmtTime(m.lastUsedAtMs)} |`
    );
  }
  return lines.join('\n');
}

/** 清空本地台账（测试用，避免用例间串数据） */
export function resetLocalUsage(): void {
  ledger.clear();
}

// ── 共用映射（三通道统一） ───────────────────────────────────

/** 渠道返回的耗时字段：缺失/非法一律 null，不伪造成 0 ms */
function optMs(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : null;
}

function toNonNegInt(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

/** 成本数组兜底解析：非法项丢弃，同币种合并并按币种排序 */
function parseCosts(value: unknown): UsageCost[] {
  if (!Array.isArray(value)) return [];
  const out: UsageCost[] = [];
  for (const item of value) {
    const c = (item ?? {}) as Record<string, unknown>;
    const currency = String(c.currency ?? '').trim().toUpperCase();
    const micros = Number(c.micros);
    if (!currency || !Number.isFinite(micros) || micros < 0) continue;
    const hit = out.find((x) => x.currency === currency);
    if (hit) hit.micros += micros;
    else out.push({ currency, micros });
  }
  return out.sort((a, b) => a.currency.localeCompare(b.currency));
}

/** 趋势点兜底解析：引擎/HTTP 字段缺失按 0 计，不伪造数字 */
function toDayPoint(d: unknown): UsageDayPoint {
  const p = (d ?? {}) as Record<string, unknown>;
  const samples = toNonNegInt(p.latencySamples);
  const calls = toNonNegInt(p.calls);
  const priced = toNonNegInt(p.pricedCalls);
  return {
    day: String(p.day ?? ''),
    tokensUsed: toNonNegInt(p.tokensUsed),
    calls,
    models: toNonNegInt(p.models),
    avgLatencyMs: samples > 0 ? optMs(p.avgLatencyMs) : null,
    latencySamples: samples,
    costs: parseCosts(p.costs),
    pricedCalls: priced,
    unpricedCalls: p.unpricedCalls === undefined
      ? Math.max(0, calls - priced)
      : toNonNegInt(p.unpricedCalls),
  };
}

/**
 * 报表返回体 → `UsageReport`（引擎 `/api/usage/report` 与本地台账共用）。
 * totals 一律按明细重算，保证趋势、明细、汇总三者的口径一致。
 */
export function extractReport(resp: unknown, period: UsagePeriod, nowMs = Date.now()): UsageReport {
  const raw = (resp ?? {}) as Record<string, unknown>;
  const models = extractUsageRows(Array.isArray(raw.models) ? raw.models : []);
  return {
    period,
    generatedAtMs: Number(raw.generatedAtMs ?? nowMs) || nowMs,
    days: Array.isArray(raw.days) ? raw.days.map(toDayPoint) : [],
    models,
    totals: totalsOf(models),
  };
}

/** 从 command/HTTP 返回体提取用量行：支持 `{ rows }` 视图与裸数组兜底 */
export function extractUsageRows(resp: unknown): ModelUsageRow[] {
  const raw = Array.isArray(resp)
    ? resp
    : ((resp as ModelUsageView | null)?.rows ?? []);
  return raw.map((r) => {
    const row = r as Partial<ModelUsageRow> & Record<string, unknown>;
    const prompt = toNonNegInt(row.promptTokens);
    const completion = toNonNegInt(row.completionTokens);
    const calls = toNonNegInt(row.calls);
    const samples = toNonNegInt(row.latencySamples);
    const priced = toNonNegInt(row.pricedCalls);
    return {
      modelName: String(row.modelName ?? ''),
      displayName: (row.displayName as string | undefined) ?? undefined,
      promptTokens: prompt,
      completionTokens: completion,
      tokensUsed: row.tokensUsed === undefined ? prompt + completion : toNonNegInt(row.tokensUsed),
      calls,
      lastUsedAtMs: toNonNegInt(row.lastUsedAtMs),
      avgLatencyMs: samples > 0 ? optMs(row.avgLatencyMs) : null,
      latencySamples: samples,
      costs: parseCosts(row.costs),
      pricedCalls: priced,
      unpricedCalls:
        row.unpricedCalls === undefined ? Math.max(0, calls - priced) : toNonNegInt(row.unpricedCalls),
    };
  });
}

/**
 * 原始行 → ModelUsageStats（组件契约）。
 * 未设置限额时不回填 tokensLimit（面板按「无上限」展示，不伪造配额）。
 */
export function toUsageStats(
  rows: ModelUsageRow[],
  period: UsagePeriod,
  limits: Record<string, number> = {}
): ModelUsageStats[] {
  return rows.map((r) => {
    const limit = limits[r.modelName];
    return {
      modelId: r.modelName,
      modelName: r.displayName || r.modelName,
      tokensUsed: r.tokensUsed,
      tokensLimit: limit,
      usagePercent: limit ? Math.min((r.tokensUsed / limit) * 100, 100) : 0,
      lastUsedAt: r.lastUsedAtMs ? new Date(r.lastUsedAtMs) : undefined,
      period,
      avgLatencyMs: r.avgLatencyMs ?? null,
      costs: r.costs ?? [],
      pricedCalls: r.pricedCalls ?? 0,
      unpricedCalls: r.unpricedCalls ?? 0,
    };
  });
}