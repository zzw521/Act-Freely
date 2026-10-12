/**
 * 模型用量统计单元测试（usage-local 映射/分桶 + store 真实通道 + Mock 记账）
 *
 * vitest 环境为 node（无 window），此处手动 polyfill 后动态导入
 * mock-api 垫片与 store，覆盖真实分发路径：
 * - 本地台账按「模型 × UTC 日」累加、周期过滤与倒序（与 Rust model::usage 同口径）
 * - 周期非法抛 USAGE_INVALID_PERIOD（不静默降级）
 * - 原始行 → ModelUsageStats 映射（未设置限额不回填 tokensLimit）
 * - store 经 Mock 通道读取真实台账（不含任何硬编码假模型）
 * - Mock 执行流（plan + 三步）真实记账，供用量页展示
 */

import { describe, it, expect, beforeEach, beforeAll, vi } from 'vitest';

(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = {
  tongtianAPI?: {
    invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
    chatSubmit: (req: { goal: string }) => Promise<{ taskId: string }>;
    chatReview: (taskId: string, action: string, patch?: unknown[]) => Promise<{ ok: boolean; status: string }>;
  };
};
const win = globalThis.window as WindowAPI;

const DAY = 86_400_000;

let usage: typeof import('../../../src/renderer/lib/usage-local');
let useModelUsageStore: typeof import('../../../src/renderer/stores/useModelUsageStore').useModelUsageStore;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  usage = await import('../../../src/renderer/lib/usage-local');
  useModelUsageStore = (await import('../../../src/renderer/stores/useModelUsageStore')).useModelUsageStore;
});

beforeEach(() => {
  usage.resetLocalUsage();
  // store 为模块级单例：用例间重置周期与状态，避免相互污染
  useModelUsageStore.setState({ period: 'monthly', stats: [], error: null, loading: false });
});

// ── 本地台账：累加与分桶 ──────────────────────────────────────

describe('usage-local：台账累加与周期分桶', () => {
  it('同模型同日累加 token 与调用次数', () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 100, 50, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 20, 10, { atMs: now });
    const [row] = usage.localUsageRows('daily', now);
    expect(row.modelName).toBe('qwen-plus');
    expect(row.promptTokens).toBe(120);
    expect(row.completionTokens).toBe(60);
    expect(row.tokensUsed).toBe(180);
    expect(row.calls).toBe(2);
    expect(row.lastUsedAtMs).toBe(now);
  });

  it('模型名为空（含纯空白）不记账', () => {
    usage.recordLocalUsage('   ', 100, 100);
    expect(usage.localUsageRows('total')).toEqual([]);
  });

  it('负值归零（引擎异常值不污染台账）', () => {
    usage.recordLocalUsage('qwen-plus', -5, -5);
    const [row] = usage.localUsageRows('total');
    expect(row.tokensUsed).toBe(0);
    expect(row.calls).toBe(1);
  });

  it('daily 只含今日；monthly 排除上月；total 全量', () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 10, 0, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 20, 0, { atMs: now - DAY });
    usage.recordLocalUsage('qwen-plus', 30, 0, { atMs: now - 40 * DAY });

    expect(usage.localUsageRows('daily', now)[0].tokensUsed).toBe(10);
    expect(usage.localUsageRows('monthly', now)[0].tokensUsed).toBeLessThan(60);
    expect(usage.localUsageRows('total', now)[0].tokensUsed).toBe(60);
    expect(usage.localUsageRows('total', now)[0].calls).toBe(3);
  });

  it('多模型按总 token 倒序', () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-max', 7, 3, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 120, 60, { atMs: now });
    const rows = usage.localUsageRows('total', now);
    expect(rows.map((r) => r.modelName)).toEqual(['qwen-plus', 'qwen-max']);
  });

  it('UTC 分桶口径与 Rust/引擎一致（yyyy-mm-dd / yyyy-mm）', () => {
    const ts = Date.UTC(2026, 0, 5, 23, 30);
    expect(usage.utcDayOf(ts)).toBe('2026-01-05');
    expect(usage.utcMonthOf(ts)).toBe('2026-01');
  });
});

describe('usage-local：周期校验', () => {
  it('合法周期原样返回', () => {
    expect(usage.normalizePeriod('daily')).toBe('daily');
    expect(usage.normalizePeriod('monthly')).toBe('monthly');
    expect(usage.normalizePeriod('total')).toBe('total');
  });

  it('非法/缺失周期抛 USAGE_INVALID_PERIOD（不静默降级）', () => {
    expect(() => usage.normalizePeriod('hourly')).toThrow(/USAGE_INVALID_PERIOD/);
    expect(() => usage.normalizePeriod(undefined)).toThrow(/USAGE_INVALID_PERIOD/);
    expect(() => usage.normalizePeriod('')).toThrow(/USAGE_INVALID_PERIOD/);
  });
});

describe('usage-local：原始行 → 组件契约映射', () => {
  it('extractUsageRows 兼容 { rows } 视图与裸数组', () => {
    const row = { modelName: 'qwen-plus', promptTokens: 10, completionTokens: 5, tokensUsed: 15, calls: 1, lastUsedAtMs: 1 };
    expect(usage.extractUsageRows({ period: 'daily', rows: [row] })).toHaveLength(1);
    expect(usage.extractUsageRows([row])).toHaveLength(1);
    expect(usage.extractUsageRows(null)).toEqual([]);
  });

  it('缺失 tokensUsed 时按 prompt+completion 兜底', () => {
    const rows = usage.extractUsageRows([{ modelName: 'qwen-plus', promptTokens: 3, completionTokens: 4 }]);
    expect(rows[0].tokensUsed).toBe(7);
    expect(rows[0].calls).toBe(0);
  });

  it('未设置限额不回填 tokensLimit（不伪造配额）', () => {
    const stats = usage.toUsageStats(
      [{ modelName: 'qwen-plus', displayName: '通义千问', promptTokens: 10, completionTokens: 5, tokensUsed: 15, calls: 1, lastUsedAtMs: 1700000000000 }],
      'daily'
    );
    expect(stats[0]).toMatchObject({
      modelId: 'qwen-plus',
      modelName: '通义千问',
      tokensUsed: 15,
      usagePercent: 0,
      period: 'daily',
    });
    expect(stats[0].tokensLimit).toBeUndefined();
    expect(stats[0].lastUsedAt?.getTime()).toBe(1700000000000);
  });

  it('displayName 缺省回落为模型名；设置限额时计算百分比并封顶 100', () => {
    const rows = [
      { modelName: 'qwen-plus', promptTokens: 0, completionTokens: 0, tokensUsed: 150, calls: 1, lastUsedAtMs: 0 },
    ];
    expect(usage.toUsageStats(rows, 'total')[0].modelName).toBe('qwen-plus');
    const limited = usage.toUsageStats(rows, 'total', { 'qwen-plus': 100 });
    expect(limited[0].tokensLimit).toBe(100);
    expect(limited[0].usagePercent).toBe(100);
  });
});

// ── store：经 Mock 通道读取真实台账 ────────────────────────────

describe('useModelUsageStore：真实通道（不再返回假数据）', () => {
  it('台账为空返回空列表（旧实现的 3 条硬编码模型已移除）', async () => {
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats).toEqual([]);
    expect(useModelUsageStore.getState().error).toBeNull();
  });

  it('经 model:usage 通道读回台账行并映射为组件契约', async () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 100, 50, { displayName: '通义千问（DashScope）', atMs: now });
    await useModelUsageStore.getState().loadUsageStats();

    const state = useModelUsageStore.getState();
    expect(state.error).toBeNull();
    expect(state.stats).toHaveLength(1);
    expect(state.stats[0].modelId).toBe('qwen-plus');
    expect(state.stats[0].modelName).toBe('通义千问（DashScope）');
    expect(state.stats[0].tokensUsed).toBe(150);
    expect(state.stats[0].period).toBe(state.period);
  });

  it('切换周期重新查询（total 含历史，daily 只看今日）', async () => {
    const now = Date.now();
    usage.recordLocalUsage('qwen-plus', 10, 0, { atMs: now });
    usage.recordLocalUsage('qwen-plus', 90, 0, { atMs: now - 40 * DAY });

    useModelUsageStore.getState().setPeriod('total');
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats[0].tokensUsed).toBe(100);

    useModelUsageStore.getState().setPeriod('daily');
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats[0].tokensUsed).toBe(10);
  });

  it('周期非法：错误文案映射为 USAGE_INVALID_PERIOD 文案且清空旧数据', async () => {
    usage.recordLocalUsage('qwen-plus', 10, 0);
    await useModelUsageStore.getState().loadUsageStats();
    expect(useModelUsageStore.getState().stats).toHaveLength(1);

    // 绕过类型约束模拟非法周期（后端拒绝而非静默降级）
    useModelUsageStore.setState({ period: 'hourly' as never });
    await useModelUsageStore.getState().loadUsageStats();

    const state = useModelUsageStore.getState();
    expect(state.stats).toEqual([]);
    expect(state.error).toContain('用量统计周期非法');
    expect(state.loading).toBe(false);
  });
});

// ── Mock 执行流：真实记账 ─────────────────────────────────────

describe('Mock 执行流：用量真实进入本地台账', () => {
  it('plan + 三步执行的 token 用量累加到当前默认模型', async () => {
    vi.useFakeTimers();
    try {
      const task = await win.tongtianAPI!.chatSubmit({ goal: '整理季度报表' });
      await win.tongtianAPI!.chatReview(task.taskId, 'approve');
      await vi.advanceTimersByTimeAsync(5000);

      const rows = usage.localUsageRows('total');
      expect(rows).toHaveLength(1);
      // 计划 600/300 + step 800/400、1200/600、900/500
      expect(rows[0].promptTokens).toBe(3500);
      expect(rows[0].completionTokens).toBe(1800);
      expect(rows[0].tokensUsed).toBe(5300);
      expect(rows[0].calls).toBe(4);
      expect(rows[0].displayName).toBeTruthy();

      // 与用量页数据源同源：store 能读到该记账结果
      await useModelUsageStore.getState().loadUsageStats();
      expect(useModelUsageStore.getState().stats[0].tokensUsed).toBe(5300);
    } finally {
      vi.useRealTimers();
    }
  });
});