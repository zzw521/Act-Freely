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

interface LocalUsageRow extends ModelUsageRow {
  /** UTC 日，累加分桶键 */
  day: string;
}

const ledger = new Map<string, LocalUsageRow>();

function bucketKey(modelName: string, day: string): string {
  return `${modelName}\u0000${day}`;
}

/** 记一次模型调用（按「模型 × UTC 日」增量累加；模型名为空时忽略） */
export function recordLocalUsage(
  modelName: string,
  promptTokens: number,
  completionTokens: number,
  opts: { displayName?: string; atMs?: number } = {}
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
  };
  row.promptTokens += Math.max(0, promptTokens);
  row.completionTokens += Math.max(0, completionTokens);
  row.tokensUsed = row.promptTokens + row.completionTokens;
  row.calls += 1;
  row.lastUsedAtMs = atMs;
  if (opts.displayName) row.displayName = opts.displayName;
  ledger.set(key, row);
}

/** 按周期聚合（daily=今日 / monthly=本月 / total=全部），按总 token 倒序 */
export function localUsageRows(period: UsagePeriod, nowMs = Date.now()): ModelUsageRow[] {
  const day = utcDayOf(nowMs);
  const month = utcMonthOf(nowMs);
  const agg = new Map<string, ModelUsageRow>();
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
    };
    cur.promptTokens += row.promptTokens;
    cur.completionTokens += row.completionTokens;
    cur.tokensUsed += row.tokensUsed;
    cur.calls += row.calls;
    cur.lastUsedAtMs = Math.max(cur.lastUsedAtMs, row.lastUsedAtMs);
    agg.set(row.modelName, cur);
  }
  return [...agg.values()].sort(
    (a, b) => b.tokensUsed - a.tokensUsed || a.modelName.localeCompare(b.modelName)
  );
}

/** 本地台账视图（Mock 模式 `model:usage` 返回值） */
export function localUsageView(period: UsagePeriod, nowMs = Date.now()): ModelUsageView {
  return { period, rows: localUsageRows(period, nowMs) };
}

// ── 汇总 / 趋势 / 报表（三通道共用口径） ──────────────────────

/**
 * 明细行 → 周期汇总（与 Rust `model::usage::totals_of` 逐字段一致）。
 * 台账没有延迟与单价字段，故汇总不含平均耗时/成本估算。
 */
export function totalsOf(rows: ModelUsageRow[]): UsageTotals {
  return rows.reduce<UsageTotals>(
    (acc, r) => ({
      promptTokens: acc.promptTokens + r.promptTokens,
      completionTokens: acc.completionTokens + r.completionTokens,
      tokensUsed: acc.tokensUsed + r.tokensUsed,
      calls: acc.calls + r.calls,
      activeModels: acc.activeModels + 1,
      lastUsedAtMs: Math.max(acc.lastUsedAtMs, r.lastUsedAtMs),
    }),
    {
      promptTokens: 0,
      completionTokens: 0,
      tokensUsed: 0,
      calls: 0,
      activeModels: 0,
      lastUsedAtMs: 0,
    }
  );
}

/** 由明细行组装总览（浏览器直连引擎时用：`/api/usage` 行 → `usage:stats` 返回体） */
export function statsFromRows(period: UsagePeriod, rows: ModelUsageRow[]): UsageStats {
  return { period, totals: totalsOf(rows), topModel: rows[0]?.displayName || rows[0]?.modelName || null };
}

/** 本地台账按 UTC 日分桶的趋势点（与 Rust `daily_points` 同周期过滤、同日升序） */
export function localDayPoints(period: UsagePeriod, nowMs = Date.now()): UsageDayPoint[] {
  const day = utcDayOf(nowMs);
  const month = utcMonthOf(nowMs);
  const buckets = new Map<string, UsageDayPoint & { names: Set<string> }>();
  for (const row of ledger.values()) {
    if (period === 'daily' && row.day !== day) continue;
    if (period === 'monthly' && !row.day.startsWith(month)) continue;
    const cur = buckets.get(row.day) ?? { day: row.day, tokensUsed: 0, calls: 0, models: 0, names: new Set<string>() };
    cur.tokensUsed += row.tokensUsed;
    cur.calls += row.calls;
    cur.names.add(row.modelName);
    buckets.set(row.day, cur);
  }
  return [...buckets.values()]
    .map((p) => ({ day: p.day, tokensUsed: p.tokensUsed, calls: p.calls, models: p.names.size }))
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

/**
 * 报表 → Markdown 文本（三通道共用的导出格式，数字全部来自台账）。
 * 空台账明确写「暂无用量记录」，不产出空表误导。
 */
export function reportToMarkdown(report: UsageReport): string {
  const lines: string[] = [
    `# 通天办公 模型用量报表（${PERIOD_LABEL[report.period]}）`,
    '',
    `- 生成时间：${fmtTime(report.generatedAtMs)}`,
    `- 汇总：Token ${fmtNum(report.totals.tokensUsed)}（输入 ${fmtNum(report.totals.promptTokens)} / 输出 ${fmtNum(report.totals.completionTokens)}）`,
    `- 调用：${fmtNum(report.totals.calls)} 次 · 活跃模型 ${report.totals.activeModels} 个 · 最近使用 ${fmtTime(report.totals.lastUsedAtMs)}`,
    '- 口径：台账未采集调用延迟与模型单价，故本报表不提供耗时与成本估算',
  ];
  if (!report.models.length) {
    lines.push('', '暂无用量记录。');
    return lines.join('\n');
  }
  lines.push(
    '',
    '## 按日趋势',
    '',
    '| UTC 日 | Token | 调用 | 活跃模型 |',
    '| --- | ---: | ---: | ---: |'
  );
  for (const d of report.days) {
    lines.push(`| ${d.day} | ${fmtNum(d.tokensUsed)} | ${d.calls} | ${d.models} |`);
  }
  lines.push(
    '',
    '## 模型明细',
    '',
    '| 模型 | 输入 Token | 输出 Token | 合计 | 调用 | 最近使用 |',
    '| --- | ---: | ---: | ---: | ---: | --- |'
  );
  for (const m of report.models) {
    lines.push(
      `| ${m.displayName || m.modelName} | ${fmtNum(m.promptTokens)} | ${fmtNum(m.completionTokens)} | ` +
        `${fmtNum(m.tokensUsed)} | ${m.calls} | ${fmtTime(m.lastUsedAtMs)} |`
    );
  }
  return lines.join('\n');
}

/** 清空本地台账（测试用，避免用例间串数据） */
export function resetLocalUsage(): void {
  ledger.clear();
}

// ── 共用映射（三通道统一） ───────────────────────────────────

/** 趋势点兜底解析：引擎/HTTP 字段缺失按 0 计，不伪造数字 */
function toDayPoint(d: unknown): UsageDayPoint {
  const p = (d ?? {}) as Record<string, unknown>;
  return {
    day: String(p.day ?? ''),
    tokensUsed: Number(p.tokensUsed ?? 0) || 0,
    calls: Number(p.calls ?? 0) || 0,
    models: Number(p.models ?? 0) || 0,
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
    const prompt = Number(row.promptTokens ?? 0) || 0;
    const completion = Number(row.completionTokens ?? 0) || 0;
    return {
      modelName: String(row.modelName ?? ''),
      displayName: (row.displayName as string | undefined) ?? undefined,
      promptTokens: prompt,
      completionTokens: completion,
      tokensUsed: Number(row.tokensUsed ?? prompt + completion) || 0,
      calls: Number(row.calls ?? 0) || 0,
      lastUsedAtMs: Number(row.lastUsedAtMs ?? 0) || 0,
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
    };
  });
}