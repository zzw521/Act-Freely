/**
 * 定时任务单元测试（调度算法 + 规范化 + 本地/UTC 换算 + 本地台账 + 通道分发 + store）
 *
 * vitest 环境为 node（无 window），此处手动 polyfill 后动态导入 mock-api 垫片与 store，
 * 覆盖真实分发路径（Mock 开关规范：无后端时前端本地台账即权威数据源）。
 *
 * 「下次执行时间」用例与 Rust `task::schedule` 的 `#[test]` 一一对应（同一基准时刻与期望值），
 * 用于锁定 TS 与 Rust 两套实现同口径；基准 2026-03-05T00:00:00Z 为周四。
 */

import { describe, it, expect, beforeEach, beforeAll } from 'vitest';

(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = {
  tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> };
};
const win = globalThis.window as WindowAPI;

/** 2026-03-05T00:00:00Z（周四） */
const BASE = Date.UTC(2026, 2, 5, 0, 0, 0);
const HOUR = 3_600_000;
const DAY = 86_400_000;

let sched: typeof import('../../../src/renderer/lib/schedule-local');
let useScheduleStore: typeof import('../../../src/renderer/stores/useScheduleStore').useScheduleStore;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  sched = await import('../../../src/renderer/lib/schedule-local');
  useScheduleStore = (await import('../../../src/renderer/stores/useScheduleStore')).useScheduleStore;
});

beforeEach(() => {
  sched.resetLocalSchedules();
  useScheduleStore.setState({
    tasks: [],
    runs: [],
    keepAwake: null,
    tasksLoading: false,
    runsLoading: false,
    error: null,
  });
});

// ── 频率/时刻/星期规范化 ──────────────────────────────────────

describe('schedule-local：输入规范化与错误码', () => {
  it('中文标签与英文枚举均可接受（大小写不敏感）', () => {
    expect(sched.normalizeFreq('每天')).toBe('daily');
    expect(sched.normalizeFreq('每周')).toBe('weekly');
    expect(sched.normalizeFreq('每月')).toBe('monthly');
    expect(sched.normalizeFreq('每小时')).toBe('hourly');
    expect(sched.normalizeFreq('DAILY')).toBe('daily');
  });

  it('非法频率抛 SCHEDULE_INVALID_FREQ（不静默降级为每天）', () => {
    expect(() => sched.normalizeFreq('每分钟')).toThrow(/SCHEDULE_INVALID_FREQ/);
    expect(() => sched.normalizeFreq('')).toThrow(/SCHEDULE_INVALID_FREQ/);
  });

  it('时刻补零并在 00:00–23:59 内校验', () => {
    expect(sched.normalizeTime('9:5')).toBe('09:05');
    expect(sched.normalizeTime(' 23:59 ')).toBe('23:59');
    expect(() => sched.normalizeTime('24:00')).toThrow(/SCHEDULE_INVALID_TIME/);
    expect(() => sched.normalizeTime('12:60')).toThrow(/SCHEDULE_INVALID_TIME/);
    expect(() => sched.normalizeTime('abc')).toThrow(/SCHEDULE_INVALID_TIME/);
  });

  it('星期仅每周必填且在 1–7 内', () => {
    expect(sched.normalizeWeekday('weekly', 3)).toBe(3);
    expect(sched.normalizeWeekday('daily', 3)).toBeNull();
    expect(sched.normalizeWeekday('daily', null)).toBeNull();
    expect(() => sched.normalizeWeekday('weekly', 0)).toThrow(/SCHEDULE_INVALID_WEEKDAY/);
    expect(() => sched.normalizeWeekday('weekly', null)).toThrow(/SCHEDULE_INVALID_WEEKDAY/);
  });

  it('名称去空白且拒空（超长截断到 60 字）', () => {
    expect(sched.normalizeName('  舆情监控 ')).toBe('舆情监控');
    expect(sched.normalizeName('x'.repeat(80))).toHaveLength(60);
    expect(() => sched.normalizeName('   ')).toThrow(/SCHEDULE_INVALID_NAME/);
  });
});

// ── 一句话解析（与 Rust `parse_natural` 同用例同期望值） ─────────

describe('parseNaturalSchedule：一句话 → 表单字段', () => {
  it('每天 + 下午时刻 + 名称去噪', () => {
    const p = sched.parseNaturalSchedule('每天上午9点汇总品牌舆情');
    expect(p.freq).toBe('daily');
    expect(p.timeOfDay).toBe('09:00');
    expect(p.weekday).toBeNull();
    expect(p.name).toBe('汇总品牌舆情');
    expect(p.note).toBeNull();

    const q = sched.parseNaturalSchedule('每天下午 3 点半提醒我提交日报');
    expect(q.timeOfDay).toBe('15:30');
    expect(q.name).toBe('提交日报');
  });

  it('每周星期与每月 1 号', () => {
    const w = sched.parseNaturalSchedule('每周一上午 9:00 生成周报');
    expect(w.freq).toBe('weekly');
    expect(w.weekday).toBe(1);
    expect(w.timeOfDay).toBe('09:00');
    expect(w.name).toBe('生成周报');

    const sun = sched.parseNaturalSchedule('每周日晚上8点备份数据');
    expect(sun.weekday).toBe(7);
    expect(sun.timeOfDay).toBe('20:00');
    expect(sun.name).toBe('备份数据');

    const m = sched.parseNaturalSchedule('每月1号凌晨2点清理缓存');
    expect(m.freq).toBe('monthly');
    expect(m.timeOfDay).toBe('02:00');
    expect(m.name).toBe('清理缓存');
  });

  it('每小时只取分钟字段', () => {
    const p = sched.parseNaturalSchedule('每小时的第30分钟检查服务');
    expect(p.freq).toBe('hourly');
    expect(p.timeOfDay).toBe('00:30');
    expect(p.name).toBe('检查服务');

    const q = sched.parseNaturalSchedule('每小时 15 分同步数据');
    expect(q.timeOfDay).toBe('00:15');
    expect(q.name).toBe('同步数据');
  });

  it('未写频率按每天处理并显式给出 note', () => {
    const p = sched.parseNaturalSchedule('上午10点开晨会');
    expect(p.freq).toBe('daily');
    expect(p.timeOfDay).toBe('10:00');
    expect(p.name).toBe('开晨会');
    expect(p.note).toBe('未写频率，已按每天处理');
  });

  it('夜里/中午的边界换算', () => {
    expect(sched.parseNaturalSchedule('每天晚上12点归档').timeOfDay).toBe('00:00');
    expect(sched.parseNaturalSchedule('每天中午1点午会').timeOfDay).toBe('13:00');
    expect(sched.parseNaturalSchedule('每天中午12点午餐').timeOfDay).toBe('12:00');
    expect(() => sched.parseNaturalSchedule('每天24:00 不允许')).toThrow(/SCHEDULE_PARSE_TIME/);
  });

  it('不支持的间隔/日期与缺失时刻如实拒绝（不猜一个值）', () => {
    expect(() => sched.parseNaturalSchedule('每2小时同步一次')).toThrow(/SCHEDULE_UNSUPPORTED_FREQ/);
    expect(() => sched.parseNaturalSchedule('每隔30分钟检查')).toThrow(/SCHEDULE_UNSUPPORTED_FREQ/);
    expect(() => sched.parseNaturalSchedule('每月15号发工资')).toThrow(/SCHEDULE_UNSUPPORTED_DAY/);
    expect(() => sched.parseNaturalSchedule('每天汇总舆情')).toThrow(/SCHEDULE_PARSE_NO_TIME/);
    expect(() => sched.parseNaturalSchedule('   ')).toThrow(/SCHEDULE_PARSE_EMPTY/);
  });
});

// ── 执行结果推送配置 ──────────────────────────────────────────

describe('normalizeNotify：通道与目标成对校验', () => {
  it('均空即关闭推送；合法值去空白并小写通道', () => {
    expect(sched.normalizeNotify(undefined, undefined)).toEqual({
      notifyChannel: '',
      notifyTarget: '',
    });
    expect(sched.normalizeNotify(' Slack ', ' C123 ')).toEqual({
      notifyChannel: 'slack',
      notifyTarget: 'C123',
    });
  });

  it('只填一侧或通道非法即拒 SCHEDULE_INVALID_NOTIFY', () => {
    expect(() => sched.normalizeNotify('', 'C123')).toThrow(/SCHEDULE_INVALID_NOTIFY/);
    expect(() => sched.normalizeNotify('slack', '')).toThrow(/SCHEDULE_INVALID_NOTIFY/);
    expect(() => sched.normalizeNotify('sl/ack', 'C1')).toThrow(/SCHEDULE_INVALID_NOTIFY/);
    expect(() => sched.normalizeNotify('slack', 'x'.repeat(129))).toThrow(
      /SCHEDULE_INVALID_NOTIFY/,
    );
  });
});

// ── 下次执行时间（与 Rust 测试同基准同期望值） ─────────────────

describe('nextRunAtMs：与 Rust 同口径的调度算法', () => {
  it('daily：当天未到点 → 当天；已过点 → 次日', () => {
    expect(sched.nextRunAtMs('daily', '08:00', null, BASE)).toBe(BASE + 8 * HOUR);
    expect(sched.nextRunAtMs('daily', '08:00', null, BASE + 12 * HOUR)).toBe(BASE + 32 * HOUR);
  });

  it('daily：恰好等于当前时刻推到次日（严格大于，避免死循环触发）', () => {
    expect(sched.nextRunAtMs('daily', '08:00', null, BASE + 8 * HOUR)).toBe(BASE + 32 * HOUR);
  });

  it('weekly：基准周四 → 目标周一 09:00 为 4 天后', () => {
    expect(sched.nextRunAtMs('weekly', '09:00', 1, BASE)).toBe(BASE + 4 * DAY + 9 * HOUR);
  });

  it('weekly：目标星期＝当天但时刻已过 → 顺延 7 天', () => {
    const noon = BASE + 12 * HOUR;
    expect(sched.nextRunAtMs('weekly', '09:00', 4, noon)).toBe(noon + 6 * DAY + 21 * HOUR);
  });

  it('monthly：取次月 1 日（含跨年）', () => {
    expect(sched.nextRunAtMs('monthly', '07:30', null, BASE)).toBe(
      Date.UTC(2026, 3, 1, 7, 30),
    );
    expect(sched.nextRunAtMs('monthly', '00:00', null, Date.UTC(2026, 11, 20))).toBe(
      Date.UTC(2027, 0, 1, 0, 0),
    );
  });

  it('hourly：只用分字段（时字段忽略）', () => {
    expect(sched.nextRunAtMs('hourly', '09:30', null, BASE + 20 * 60_000)).toBe(
      BASE + 30 * 60_000,
    );
    expect(sched.nextRunAtMs('hourly', '09:30', null, BASE + 45 * 60_000)).toBe(
      BASE + 90 * 60_000,
    );
  });

  it('时区无关：绝对时间戳与本地时区无关（同一 UTC 口径）', () => {
    const t = sched.nextRunAtMs('daily', '08:00', null, BASE);
    expect(new Date(t).toISOString()).toBe('2026-03-05T08:00:00.000Z');
  });
});

// ── 本地时刻 ↔ UTC 时刻 ──────────────────────────────────────

describe('本地时区换算：界面按本地录入/呈现，落库统一 UTC', () => {
  const at = new Date(2026, 2, 5, 0, 0, 0);

  it('本地 → UTC 后仍是同一绝对时刻', () => {
    for (const local of ['09:00', '00:00', '23:59']) {
      const utc = sched.localToUtcTime(local, at);
      const [h, m] = utc.split(':').map(Number);
      const back = new Date(Date.UTC(2026, 2, 5, h, m));
      expect(back.getHours()).toBe(Number(local.split(':')[0]));
      expect(back.getMinutes()).toBe(Number(local.split(':')[1]));
    }
  });

  it('UTC → 本地与本地 → UTC 互逆', () => {
    for (const local of ['00:00', '09:30', '23:59']) {
      expect(sched.utcToLocalTime(sched.localToUtcTime(local, at), at)).toBe(local);
    }
  });

  it('非法时刻换算同样拒绝（不返回脏值）', () => {
    expect(() => sched.localToUtcTime('25:00', at)).toThrow(/SCHEDULE_INVALID_TIME/);
    expect(() => sched.utcToLocalTime('7h', at)).toThrow(/SCHEDULE_INVALID_TIME/);
  });

  it('describeSchedule 按本地时刻描述各频率', () => {
    const utc = sched.localToUtcTime('09:00', at);
    expect(sched.describeSchedule('daily', utc, null)).toBe('每天 09:00');
    expect(sched.describeSchedule('weekly', utc, 1)).toBe('每周周一 09:00');
    expect(sched.describeSchedule('monthly', utc, null)).toBe('每月 1 日 09:00');
    expect(sched.describeSchedule('hourly', utc, null)).toBe('每小时第 0 分');
  });

  it('formatLocalDateTime 输出本地 y/m/d H:M', () => {
    const ms = new Date(2026, 8, 22, 9, 5).getTime();
    expect(sched.formatLocalDateTime(ms)).toBe('2026/9/22 09:05');
  });
});

// ── 本地台账（Mock / 引擎直连通道的权威数据源） ─────────────────

describe('schedule-local：台账 CRUD/排序/执行回写', () => {
  const req = (name: string, freq = 'daily', timeOfDay = '08:00', weekday?: number) => ({
    name,
    description: `${name} 的描述`,
    freq,
    timeOfDay,
    weekday: weekday ?? null,
  });

  it('创建即时计算下次执行时间并默认启用', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    expect(t.scheduleId).toMatch(/^sched-/);
    expect(t.enabled).toBe(true);
    expect(t.freq).toBe('daily');
    expect(t.weekday).toBeNull();
    expect(t.nextRunAtMs).toBe(BASE + 8 * HOUR);
    expect(sched.localSchedules()).toHaveLength(1);
  });

  it('创建时校验输入（非法频率/时刻即拒，不落台账）', () => {
    expect(() => sched.createLocalSchedule(req('x', '每分钟'), BASE)).toThrow(
      /SCHEDULE_INVALID_FREQ/,
    );
    expect(() => sched.createLocalSchedule(req('x', 'daily', '99:99'), BASE)).toThrow(
      /SCHEDULE_INVALID_TIME/,
    );
    expect(() => sched.createLocalSchedule(req('x', 'weekly', '08:00'), BASE)).toThrow(
      /SCHEDULE_INVALID_WEEKDAY/,
    );
    expect(sched.localSchedules()).toHaveLength(0);
  });

  it('同一毫秒内创建的 id 不冲突（名单不丢项）', () => {
    sched.createLocalSchedule(req('a'), BASE);
    sched.createLocalSchedule(req('b'), BASE);
    const ids = sched.localSchedules().map((t) => t.scheduleId);
    expect(new Set(ids).size).toBe(2);
  });

  it('列表按下次执行时间升序', () => {
    sched.createLocalSchedule(req('晚', 'daily', '20:00'), BASE);
    sched.createLocalSchedule(req('早', 'daily', '08:00'), BASE);
    expect(sched.localSchedules().map((t) => t.name)).toEqual(['早', '晚']);
  });

  it('禁用清空下次执行时间，重新启用按当前时刻重算', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    const off = sched.toggleLocalSchedule(t.scheduleId, false, BASE);
    expect(off.enabled).toBe(false);
    expect(off.nextRunAtMs).toBeNull();
    const on = sched.toggleLocalSchedule(t.scheduleId, true, BASE + 12 * HOUR);
    expect(on.enabled).toBe(true);
    expect(on.nextRunAtMs).toBe(BASE + 32 * HOUR);
  });

  it('切换不存在的任务抛 SCHEDULE_NOT_FOUND', () => {
    expect(() => sched.toggleLocalSchedule('sched-missing', true, BASE)).toThrow(
      /SCHEDULE_NOT_FOUND/,
    );
  });

  // ── 编辑（与 Rust `task::schedule::update` 同口径） ─────────────

  it('编辑只改动给定字段，其余沿用原配置', () => {
    const t = sched.createLocalSchedule(
      { name: '每日舆情', description: '原描述', freq: 'daily', timeOfDay: '08:00', workspaceId: 'brand' },
      BASE,
    );
    const next = sched.updateLocalSchedule(
      { scheduleId: t.scheduleId, name: '品牌舆情监控' },
      BASE + 12 * HOUR,
    );
    expect(next.name).toBe('品牌舆情监控');
    expect(next.description).toBe('原描述');
    expect(next.freq).toBe('daily');
    expect(next.timeOfDay).toBe('08:00');
    expect(next.workspaceId).toBe('brand');
    expect(next.enabled).toBe(true);
    // 启用中的任务按编辑时刻重算：当天 08:00 已过 → 顺延次日
    expect(next.nextRunAtMs).toBe(BASE + DAY + 8 * HOUR);
    expect(next.updatedAtMs).toBe(BASE + 12 * HOUR);
    expect(sched.localSchedules()[0].name).toBe('品牌舆情监控');
  });

  it('编辑频率时星期按新频率口径重算（weekly 必填、其余清空）', () => {
    const t = sched.createLocalSchedule(req('每周股票', 'weekly', '08:00', 1), BASE);
    const toDaily = sched.updateLocalSchedule({ scheduleId: t.scheduleId, freq: 'daily' }, BASE);
    expect(toDaily.freq).toBe('daily');
    expect(toDaily.weekday).toBeNull();

    const toWeekly = sched.updateLocalSchedule(
      { scheduleId: t.scheduleId, freq: 'weekly', weekday: 3 },
      BASE,
    );
    expect(toWeekly.freq).toBe('weekly');
    expect(toWeekly.weekday).toBe(3);
    expect(toWeekly.nextRunAtMs).toBe(BASE + 6 * DAY + 8 * HOUR);

    // 未给星期时沿用原星期（本地时区周四 → 周四=4）
    const keepWeekday = sched.updateLocalSchedule({ scheduleId: t.scheduleId, timeOfDay: '09:00' }, BASE);
    expect(keepWeekday.freq).toBe('weekly');
    expect(keepWeekday.weekday).toBe(3);
  });

  it('非法编辑被拒且不改动存量任务', () => {
    const t = sched.createLocalSchedule(req('每周股票', 'weekly', '08:00', 1), BASE);
    // 无星期的每天任务切每周却不给星期：与创建同口径直接拒绝
    const d = sched.createLocalSchedule(req('每日舆情'), BASE);
    const beforeWeekly = { ...t };
    const beforeDaily = { ...d };
    expect(() => sched.updateLocalSchedule({ scheduleId: d.scheduleId, freq: 'weekly' }, BASE)).toThrow(
      /SCHEDULE_INVALID_WEEKDAY/,
    );
    expect(() => sched.updateLocalSchedule({ scheduleId: t.scheduleId, timeOfDay: '99:99' }, BASE)).toThrow(
      /SCHEDULE_INVALID_TIME/,
    );
    expect(() => sched.updateLocalSchedule({ scheduleId: t.scheduleId, name: '   ' }, BASE)).toThrow(
      /SCHEDULE_INVALID_NAME/,
    );
    const list = sched.localSchedules();
    expect(list).toHaveLength(2);
    expect(list.find((x) => x.scheduleId === t.scheduleId)).toEqual(beforeWeekly);
    expect(list.find((x) => x.scheduleId === d.scheduleId)).toEqual(beforeDaily);
  });

  it('禁用任务编辑后仍不排期（改完不会自己跑起来）', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    sched.toggleLocalSchedule(t.scheduleId, false, BASE);
    const next = sched.updateLocalSchedule(
      { scheduleId: t.scheduleId, name: '改名', description: '改描述' },
      BASE + 12 * HOUR,
    );
    expect(next.enabled).toBe(false);
    expect(next.nextRunAtMs).toBeNull();
  });

  it('编辑工作空间：空白回落 default，描述可独立修改', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    expect(sched.updateLocalSchedule({ scheduleId: t.scheduleId, workspaceId: '  ' }, BASE).workspaceId).toBe('default');
    expect(
      sched.updateLocalSchedule({ scheduleId: t.scheduleId, workspaceId: 'brand', description: '' }, BASE).workspaceId,
    ).toBe('brand');
  });

  it('编辑缺标识抛 SCHEDULE_INVALID_ID，标识不存在抛 SCHEDULE_NOT_FOUND', () => {
    expect(() => sched.updateLocalSchedule({ scheduleId: '  ' }, BASE)).toThrow(/SCHEDULE_INVALID_ID/);
    expect(() => sched.updateLocalSchedule({ scheduleId: 'sched-missing' }, BASE)).toThrow(/SCHEDULE_NOT_FOUND/);
  });

  // ── 执行结果推送（与 Rust create/update 同口径） ───────────────

  it('创建时落推送配置，未配置时两字段为空串', () => {
    const bare = sched.createLocalSchedule(req('无推送'), BASE);
    expect(bare.notifyChannel).toBe('');
    expect(bare.notifyTarget).toBe('');

    const pushed = sched.createLocalSchedule(
      {
        name: '带推送',
        description: '',
        freq: 'daily',
        timeOfDay: '08:00',
        notifyChannel: ' Slack ',
        notifyTarget: ' C123 ',
      },
      BASE,
    );
    expect(pushed.notifyChannel).toBe('slack');
    expect(pushed.notifyTarget).toBe('C123');
    expect(sched.localSchedules().find((t) => t.scheduleId === pushed.scheduleId)?.notifyTarget).toBe('C123');
  });

  it('编辑只改目标时沿用通道；切通道不继承旧目标；通道置空连目标一起关闭', () => {
    const t = sched.createLocalSchedule(
      {
        name: '带推送',
        description: '',
        freq: 'daily',
        timeOfDay: '08:00',
        notifyChannel: 'slack',
        notifyTarget: 'C123',
      },
      BASE,
    );

    const targetOnly = sched.updateLocalSchedule(
      { scheduleId: t.scheduleId, notifyTarget: 'C999' },
      BASE,
    );
    expect(targetOnly.notifyChannel).toBe('slack');
    expect(targetOnly.notifyTarget).toBe('C999');

    expect(() =>
      sched.updateLocalSchedule({ scheduleId: t.scheduleId, notifyChannel: 'dingtalk' }, BASE),
    ).toThrow(/SCHEDULE_INVALID_NOTIFY/);

    const cleared = sched.updateLocalSchedule(
      { scheduleId: t.scheduleId, notifyChannel: '', notifyTarget: '' },
      BASE,
    );
    expect(cleared.notifyChannel).toBe('');
    expect(cleared.notifyTarget).toBe('');
  });

  it('创建时推送配置不完整即拒，且不落台账', () => {
    expect(() =>
      sched.createLocalSchedule(
        {
          name: '半配置',
          description: '',
          freq: 'daily',
          timeOfDay: '08:00',
          notifyChannel: 'slack',
        },
        BASE,
      ),
    ).toThrow(/SCHEDULE_INVALID_NOTIFY/);
    expect(sched.localSchedules()).toHaveLength(0);
  });
  it('删除任务并连同其执行记录', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    sched.recordLocalRun(t.scheduleId, 'succeeded', 'ok', BASE + HOUR);
    expect(sched.removeLocalSchedule(t.scheduleId)).toBe(true);
    expect(sched.localSchedules()).toHaveLength(0);
    expect(sched.localRuns()).toHaveLength(0);
    expect(sched.removeLocalSchedule(t.scheduleId)).toBe(false);
  });

  it('执行记录回写上次状态并重算下次执行时间（按开始时间倒序）', () => {
    const t = sched.createLocalSchedule(req('每日舆情'), BASE);
    sched.recordLocalRun(t.scheduleId, 'succeeded', '第一次', BASE + 8 * HOUR, 'task-1');
    const run2 = sched.recordLocalRun(t.scheduleId, 'failed', '第二次', BASE + 2 * DAY, 'task-2');

    const [row] = sched.localSchedules();
    expect(row.lastStatus).toBe('failed');
    expect(row.lastTaskId).toBe('task-2');
    expect(row.lastRunAtMs).toBe(BASE + 2 * DAY);
    expect(row.nextRunAtMs).toBe(BASE + 2 * DAY + 8 * HOUR);

    const runs = sched.localRuns();
    expect(runs.map((r) => r.summary)).toEqual(['第二次', '第一次']);
    expect(runs[0].runId).toBe(run2.runId);
    expect(sched.localRuns(t.scheduleId)).toHaveLength(2);
    expect(sched.localRuns('sched-other')).toHaveLength(0);
  });
});

// ── 通道分发（Mock 与引擎直连共用） ───────────────────────────

describe('handleLocalScheduleChannel：通道分发与不静默语义', () => {
  it('未接管的通道如实返回 handled=false（交由调用方兜底）', () => {
    expect(sched.handleLocalScheduleChannel('skill:list', []).handled).toBe(false);
  });

  it('schedule:list 返回当前台账', () => {
    sched.createLocalSchedule({ name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' }, BASE);
    const res = sched.handleLocalScheduleChannel('schedule:list', []);
    expect(res.handled).toBe(true);
    expect(sched.extractScheduleRows(res.value)).toHaveLength(1);
  });

  it('schedule:update 经通道编辑台账（缺省字段沿用原配置）', () => {
    const created = sched.handleLocalScheduleChannel('schedule:create', [
      { name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' },
    ]).value as { scheduleId: string };
    const updated = sched.handleLocalScheduleChannel('schedule:update', [
      { scheduleId: created.scheduleId, freq: 'weekly', timeOfDay: '09:00', weekday: 2 },
    ]).value as { freq: string; weekday: number | null; name: string };
    expect(updated.name).toBe('每日舆情');
    expect(updated.freq).toBe('weekly');
    expect(updated.weekday).toBe(2);
    expect(sched.localSchedules()[0].timeOfDay).toBe('09:00');
  });

  it('schedule:create/toggle/delete 全链路生效', () => {
    const created = sched.handleLocalScheduleChannel('schedule:create', [
      { name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' },
    ]).value as { scheduleId: string; enabled: boolean };
    expect(created.enabled).toBe(true);
    sched.handleLocalScheduleChannel('schedule:toggle', [created.scheduleId, false]);
    expect(sched.localSchedules()[0].enabled).toBe(false);
    sched.handleLocalScheduleChannel('schedule:delete', [created.scheduleId]);
    expect(sched.localSchedules()).toHaveLength(0);
  });

  it('schedule:runNow 记一条明确标注的模拟执行记录（不假装已真实执行）', () => {
    const t = sched.createLocalSchedule({ name: '每日舆情', description: 'd', freq: 'daily', timeOfDay: '08:00' }, BASE);
    const res = sched.handleLocalScheduleChannel('schedule:runNow', [t.scheduleId]);
    expect(res.handled).toBe(true);

    const runs = sched.extractRunRows(sched.handleLocalScheduleChannel('schedule:runs', []).value);
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toBe('succeeded');
    expect(runs[0].summary).toContain('Mock');
    expect(sched.localSchedules()[0].lastStatus).toBe('succeeded');
  });

  it('schedule:runNow 对不存在的任务抛 SCHEDULE_NOT_FOUND', () => {
    expect(() => sched.handleLocalScheduleChannel('schedule:runNow', ['sched-missing'])).toThrow(
      /SCHEDULE_NOT_FOUND/,
    );
  });

  it('keepAwake:status 在浏览器通道如实返回 supported=false', () => {
    const res = sched.handleLocalScheduleChannel('keepAwake:status', [true]).value as {
      requested: boolean;
      active: boolean;
      supported: boolean;
    };
    expect(res).toEqual({ requested: true, active: false, supported: false });
  });
});

describe('extractScheduleRows/extractRunRows：兼容裸数组与 { rows } 视图', () => {
  it('裸数组与 { rows } 均可解析，非法输入返回空列表', () => {
    const row = { scheduleId: 'sched-1' };
    expect(sched.extractScheduleRows([row])).toHaveLength(1);
    expect(sched.extractScheduleRows({ rows: [row] })).toHaveLength(1);
    expect(sched.extractScheduleRows(null)).toEqual([]);
    expect(sched.extractScheduleRows({ rows: 'x' })).toEqual([]);

    const run = { runId: 'run-1' };
    expect(sched.extractRunRows([run])).toHaveLength(1);
    expect(sched.extractRunRows({ rows: [run] })).toHaveLength(1);
    expect(sched.extractRunRows(undefined)).toEqual([]);
  });
});

// ── store：经 Mock 通道读写真实台账 ───────────────────────────

describe('useScheduleStore：真实通道（不再是 toast 假创建）', () => {
  it('台账为空时返回空列表且无错误', async () => {
    await useScheduleStore.getState().loadTasks();
    await useScheduleStore.getState().loadRuns();
    expect(useScheduleStore.getState().tasks).toEqual([]);
    expect(useScheduleStore.getState().runs).toEqual([]);
    expect(useScheduleStore.getState().error).toBeNull();
  });

  it('create 落台账并经 loadTasks 读回（往返一致）', async () => {
    const created = await useScheduleStore.getState().create({
      name: '每日舆情',
      description: '汇总品牌舆情',
      freq: 'daily',
      timeOfDay: '08:00',
    });
    const state = useScheduleStore.getState();
    expect(state.tasks).toHaveLength(1);
    expect(state.tasks[0].scheduleId).toBe(created.scheduleId);
    expect(state.tasks[0].name).toBe('每日舆情');
    expect(state.tasks[0].nextRunAtMs).toBeGreaterThan(0);
  });

  it('创建非法输入时抛错（store 不吞掉错误）', async () => {
    await expect(
      useScheduleStore.getState().create({
        name: 'x',
        description: 'd',
        freq: '每分钟',
        timeOfDay: '08:00',
      }),
    ).rejects.toThrow(/SCHEDULE_INVALID_FREQ/);
  });

  it('toggle/remove 生效并同步列表', async () => {
    await useScheduleStore.getState().create({
      name: '每日舆情',
      description: 'd',
      freq: 'daily',
      timeOfDay: '08:00',
    });
    const id = useScheduleStore.getState().tasks[0].scheduleId;

    await useScheduleStore.getState().toggle(id, false);
    expect(useScheduleStore.getState().tasks[0].enabled).toBe(false);

    await useScheduleStore.getState().remove(id);
    expect(useScheduleStore.getState().tasks).toEqual([]);
  });

  it('update 编辑台账并同步列表（store 不吞掉错误）', async () => {
    const created = await useScheduleStore.getState().create({
      name: '每日舆情',
      description: 'd',
      freq: 'daily',
      timeOfDay: '08:00',
    });
    const updated = await useScheduleStore.getState().update({
      scheduleId: created.scheduleId,
      name: '品牌舆情监控',
      workspaceId: 'brand',
    });
    expect(updated.name).toBe('品牌舆情监控');
    expect(updated.workspaceId).toBe('brand');
    expect(useScheduleStore.getState().tasks[0].scheduleId).toBe(created.scheduleId);
    expect(useScheduleStore.getState().tasks[0].name).toBe('品牌舆情监控');

    await expect(
      useScheduleStore.getState().update({ scheduleId: 'sched-missing', name: 'x' }),
    ).rejects.toThrow(/SCHEDULE_NOT_FOUND/);
    await expect(useScheduleStore.getState().update({ scheduleId: '' })).rejects.toThrow(
      /SCHEDULE_INVALID_ID/,
    );
  });

  it('runNow 后执行记录可读回并在界面上有据可依', async () => {
    await useScheduleStore.getState().create({
      name: '每日舆情',
      description: 'd',
      freq: 'daily',
      timeOfDay: '08:00',
    });
    const id = useScheduleStore.getState().tasks[0].scheduleId;

    await useScheduleStore.getState().runNow(id);
    expect(useScheduleStore.getState().runs).toHaveLength(1);
    expect(useScheduleStore.getState().runs[0].scheduleId).toBe(id);
    expect(useScheduleStore.getState().tasks[0].lastStatus).toBe('succeeded');
  });

  it('parse 经通道返回解析结果，非法描述抛裸错误码（页面用 errorTextOf 归一）', async () => {
    const parsed = await useScheduleStore.getState().parse('每周一上午9点生成周报');
    expect(parsed.freq).toBe('weekly');
    expect(parsed.weekday).toBe(1);
    expect(parsed.timeOfDay).toBe('09:00');
    expect(parsed.name).toBe('生成周报');

    await expect(useScheduleStore.getState().parse('每2小时同步')).rejects.toThrow(
      /SCHEDULE_UNSUPPORTED_FREQ/,
    );
  });

  it('保持唤醒：只读回实际状态，浏览器通道如实标记未生效', async () => {
    await useScheduleStore.getState().loadKeepAwake();
    expect(useScheduleStore.getState().keepAwake).toEqual({
      requested: false,
      active: false,
      supported: false,
    });

    const status = await useScheduleStore.getState().setKeepAwake(true);
    expect(status.requested).toBe(true);
    expect(status.active).toBe(false);
    // 未真实生效必须显式提示，不假装成功
    expect(useScheduleStore.getState().error).toContain('暂不支持保持系统唤醒');

    await useScheduleStore.getState().setKeepAwake(false);
    expect(useScheduleStore.getState().keepAwake?.requested).toBe(false);
  });

  it('store 经 window.tongtianAPI 通道工作（非直连本地模块）', () => {
    expect(typeof win.tongtianAPI?.invoke).toBe('function');
  });
});