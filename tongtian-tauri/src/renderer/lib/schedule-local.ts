/**
 * 通天办公 - 定时任务本地实现（Mock 与浏览器直连引擎通道共用）
 *
 * 与 Rust `task::schedule` 保持同一口径：
 * - 频率枚举与中文标签归一（每小时/每天/每周/每月/间隔/一次性）
 * - 时刻为 24 小时制 HH:MM；Rust 侧按 UTC 存储与计算（无 chrono 依赖）
 * - 本层额外提供「本地时刻 ↔ UTC 时刻」换算，让界面按本地时区呈现与录入
 * - 下次执行时间算法与 Rust 完全同构（daily/weekly/monthly/hourly + interval/once）
 *
 * D-30 一次性任务的日期口径（两条通道都写清，不静默猜）：
 * - Rust 只有 UTC，相对说法（明天/下周三）按 `now` 的 UTC 日展开；
 * - 本层跑在浏览器，按用户本地日展开；界面读到 `runDateRelative` 后用
 *   `reanchorRelativeRunDate` 把 Rust 的结果平移回本地日，两条通道最终一致；
 * - 落库的 `runAtMs` 永远是绝对时刻（`localRunDateToUtcMs`），`timeOfDay` 仍存 UTC 时刻。
 *
 * 台账落地 `localStorage`（`tt:schedules`，Mock 开关规范：开发阶段不依赖真实后端，
 * 但刷新不丢已建任务）；Tauri 容器内不走本层，直接落 SQLite。
 * 注意：本层没有调度扫描线程，「到点自动执行 / 一次性任务跑完自动停用」只发生在
 * 真实桌面端（Rust `advance_next`），浏览器与 Mock 通道不模拟该行为。
 */

import type {
  KeepAwakeStatus,
  ScheduleRun,
  ScheduleRunStatus,
} from '../../shared/contracts';
import type {
  ScheduleCreateReqExtended,
  ScheduleFreqExtended,
  ScheduleParseResultExtended,
  ScheduleUpdateReqExtended,
  ScheduledTaskExtended,
} from '../../shared/contracts_connect';

/**
 * 本层的频率口径（D-30 扩展）：
 * - interval：每 N 分钟/小时/天（步长存分钟，1 分钟～一周），无固定时刻锚点
 * - once：绝对时刻执行一次，触发后自动停用
 * `contracts.ts` 的 `ScheduleFreq` 联合类型归 W4 独占，暂未放宽，因此本层用自己的
 * `ScheduleFreqExtended`（定义见 contracts_connect.ts），落库字段仍是同一批字符串值。
 */
export type LocalFreq = ScheduleFreqExtended;

export const SCHEDULE_FREQS: LocalFreq[] = ['hourly', 'daily', 'weekly', 'monthly'];

/** 界面频率下拉用这一份（含 D-30 新增的间隔/一次性） */
export const SCHEDULE_FREQS_ALL: LocalFreq[] = [
  'hourly',
  'daily',
  'weekly',
  'monthly',
  'interval',
  'once',
];

/** 间隔步长合法区间（分钟）：与 Rust `INTERVAL_MIN/MAX_MINUTES` 同值 */
export const INTERVAL_MIN_MINUTES = 1;
export const INTERVAL_MAX_MINUTES = 7 * 24 * 60;

export const WEEKDAY_LABELS: Record<number, string> = {
  1: '周一',
  2: '周二',
  3: '周三',
  4: '周四',
  5: '周五',
  6: '周六',
  7: '周日',
};

/** 界面下拉标签 → 频率枚举（与 Rust normalize_freq 同一份中文标签） */
export const FREQ_LABELS: Record<string, LocalFreq> = {
  每小时: 'hourly',
  每天: 'daily',
  每周: 'weekly',
  每月: 'monthly',
  间隔: 'interval',
  一次性: 'once',
};

export function freqLabel(freq: LocalFreq): string {
  const label = Object.keys(FREQ_LABELS).find((k) => FREQ_LABELS[k] === freq);
  return label ?? freq;
}

// ── 规范化（错误码与前端 error-messages 对齐） ────────────────

export function normalizeFreq(v: string): LocalFreq {
  const t = (v ?? '').trim();
  const mapped = FREQ_LABELS[t] ?? t.toLowerCase();
  if ((SCHEDULE_FREQS_ALL as string[]).includes(mapped)) return mapped as LocalFreq;
  throw new Error(`SCHEDULE_INVALID_FREQ: ${String(v)}`);
}

/** 间隔步长（分钟）规范化：仅 interval 必填，其余频率一律清空（与 Rust normalize_interval 同口径） */
export function normalizeInterval(
  freq: LocalFreq,
  intervalMinutes?: number | null,
): number | null {
  if (freq !== 'interval') return null;
  const n = intervalMinutes;
  if (typeof n === 'number' && Number.isInteger(n)) {
    if (n >= INTERVAL_MIN_MINUTES && n <= INTERVAL_MAX_MINUTES) return n;
    throw new Error(
      `SCHEDULE_INVALID_FREQ: 间隔需在 ${INTERVAL_MIN_MINUTES} 分钟到 ${INTERVAL_MAX_MINUTES} 分钟（一周）之间（收到 ${String(n)}）`,
    );
  }
  throw new Error('SCHEDULE_INVALID_FREQ: 间隔任务需给出间隔分钟数');
}

/** 一次性执行时刻（UTC 毫秒）规范化：仅 once 必填且必须晚于当前时刻 */
export function normalizeRunAt(
  freq: LocalFreq,
  runAtMs?: number | null,
  nowMs: number = Date.now(),
): number | null {
  if (freq !== 'once') return null;
  const t = runAtMs;
  if (typeof t === 'number' && Number.isFinite(t)) {
    if (t > nowMs) return t;
    throw new Error(
      `SCHEDULE_INVALID_TIME: 一次性任务的执行时间需晚于当前时刻（收到 ${String(t)}）`,
    );
  }
  throw new Error('SCHEDULE_INVALID_TIME: 一次性任务需给出执行日期与时刻');
}

export function parseHhMm(v: string): [number, number] | null {
  const m = /^(\d{1,2}):(\d{1,2})$/.exec((v ?? '').trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return [h, min];
}

export function normalizeTime(v: string): string {
  const parsed = parseHhMm(v);
  if (!parsed) throw new Error(`SCHEDULE_INVALID_TIME: ${String(v)}`);
  return `${pad(parsed[0])}:${pad(parsed[1])}`;
}

export function normalizeWeekday(freq: LocalFreq, weekday?: number | null): number | null {
  if (freq !== 'weekly') return null;
  if (weekday != null && Number.isInteger(weekday) && weekday >= 1 && weekday <= 7) {
    return weekday;
  }
  throw new Error(`SCHEDULE_INVALID_WEEKDAY: ${String(weekday ?? '')}`);
}

export function normalizeName(v: string): string {
  const t = (v ?? '').trim();
  if (!t) throw new Error('SCHEDULE_INVALID_NAME: 任务名称不能为空');
  return t.slice(0, 60);
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** 推送配置归一（与 Rust `normalize_notify` 同口径） */
export function normalizeNotify(
  channel: string | undefined,
  target: string | undefined,
): { notifyChannel: string; notifyTarget: string } {
  const ch = (channel ?? '').trim().toLowerCase();
  const tg = (target ?? '').trim();
  if (!ch && !tg) return { notifyChannel: '', notifyTarget: '' };
  if (!ch) throw new Error('SCHEDULE_INVALID_NOTIFY: 已填写推送目标，但未选择推送通道');
  if (!tg) throw new Error('SCHEDULE_INVALID_NOTIFY: 已选择推送通道，但未填写目标会话');
  if (ch.length > 32 || !/^[a-z0-9_-]+$/.test(ch)) {
    throw new Error(`SCHEDULE_INVALID_NOTIFY: 推送通道不合法（${ch}）`);
  }
  if ([...tg].length > 128) {
    throw new Error('SCHEDULE_INVALID_NOTIFY: 推送目标过长（上限 128 字）');
  }
  return { notifyChannel: ch, notifyTarget: tg };
}

const CN_DIGITS: Record<string, number> = {
  零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9,
};

function cnNumber(v: string): number | null {
  const t = v.trim();
  if (/^\d+$/.test(t)) return Number(t);
  const ten = t.indexOf('十');
  if (ten >= 0) {
    const hi = ten === 0 ? 1 : CN_DIGITS[t.slice(0, ten)];
    const lo = ten === t.length - 1 ? 0 : CN_DIGITS[t.slice(ten + 1)];
    if (hi === undefined || lo === undefined) return null;
    return hi * 10 + lo;
  }
  return t.length === 1 ? CN_DIGITS[t] ?? null : null;
}

function applyPeriod(h: number, period?: string): number | null {
  let hour = h;
  if (period && h <= 12) {
    if (period === '凌晨' || period === '清晨' || period === '早晨' || period === '早上' || period === '上午') {
      hour = h === 12 ? 0 : h;
    } else if (period === '中午') {
      hour = h < 11 ? h + 12 : h;
    } else if (period === '下午' || period === '傍晚') {
      hour = h < 12 ? h + 12 : h;
    } else if (['晚上', '夜里', '夜间', '晚间'].includes(period)) {
      hour = h === 12 ? 0 : h < 12 ? h + 12 : h;
    }
  }
  return hour >= 0 && hour <= 23 ? hour : null;
}

function findTime(
  raw: string,
  freq: LocalFreq,
): { hour: number; minute: number; token: string } | null {
  const period = /(凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间|晚间)/.exec(raw)?.[1];
  const colon = /(\d{1,2})\s*[:：]\s*(\d{1,2})/.exec(raw);
  if (colon) {
    const h = Number(colon[1]);
    const hour = h > 12 ? h : applyPeriod(h, period);
    if (hour === null) return null;
    return { hour, minute: Number(colon[2]), token: colon[0] };
  }
  const point = /([0-9]{1,2}|[零一二三四五六七八九十两]{1,3})\s*[点时](?:\s*(半)|([0-9]{1,2})\s*分?)?/.exec(raw);
  if (point) {
    const h = cnNumber(point[1]);
    if (h === null) return null;
    const minute = point[2] ? 30 : point[3] ? Number(point[3]) : 0;
    const hour = h > 12 ? h : applyPeriod(h, period);
    if (hour === null) return null;
    return { hour, minute, token: point[0] };
  }
  if (freq === 'hourly') {
    const m = /([0-9]{1,2})\s*分(?:钟)?/.exec(raw);
    if (m) return { hour: 0, minute: Number(m[1]), token: m[0] };
  }
  return null;
}

function cleanScheduleName(raw: string, tokens: string[]): string {
  let s = raw;
  for (const t of tokens) {
    if (t) s = s.replace(t, '');
  }
  const leading = ['请帮我', '麻烦', '帮我', '请', '定时', '提醒我', '提醒', '我要', '我想', '需要', '的第', '第', '的'];
  const trailing = ['提醒我', '提醒', '执行', '任务'];
  for (let guard = 0; guard < 8; guard += 1) {
    const before = s;
    s = s.trim();
    for (const f of leading) if (s.startsWith(f)) s = s.slice(f.length).trimStart();
    for (const f of trailing) if (s.endsWith(f)) s = s.slice(0, -f.length).trimEnd();
    if (s === before) break;
  }
  const trimmed = s.replace(/^[\s，,。；;：:！!？?、\-—]+|[\s，,。；;：:！!？?、\-—]+$/g, '');
  return trimmed ? [...trimmed].slice(0, 60).join('') : '定时任务';
}

// ── D-30：间隔与日期说法（与 Rust `task::schedule` 同一组正则口径） ──

/** 「每 N 周」：既不是四种周期频率，间隔步长也不支持周为单位——如实拒，不猜成每周 */
const RE_INTERVAL_WEEKS =
  /(?:每|每隔)\s*[0-9一二三四五六七八九十两]+\s*(?:个)?\s*(?:周|星期|礼拜)/;
const RE_INTERVAL_HOURS =
  /(?:每|每隔)\s*([0-9一二三四五六七八九十两]+)\s*(?:个)?\s*(?:小时|钟头)/;
const RE_INTERVAL_MINUTES = /(?:每|每隔)\s*([0-9一二三四五六七八九十两]+)\s*(?:分钟|分)/;
const RE_INTERVAL_DAYS =
  /(?:每|每隔)\s*([0-9一二三四五六七八九十两]+)\s*(?:个)?\s*(?:天|日)/;
/** 紧跟在「每 N 小时」之后的余数分钟（「每 2 小时 30 分」的 30 分属于步长，不是时刻） */
const RE_MINUTE_AFTER = /^([0-9一二三四五六七八九十两]+)\s*(?:分钟|分)/;
const RE_DATE_ISO = /(\d{4})-(\d{1,2})-(\d{1,2})/;
/** 中文日期：11 月 5 日 / 2026 年 11 月 5 号（年份可缺省） */
const RE_DATE_CN =
  /(?:(\d{4})\s*年\s*)?([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]/;
const RE_RELATIVE_DAY = /(大后天|后天|明天|明日|今天|今日)/;
/** 「下周三」「下个星期日」：取严格晚于今天的下一次该星期 */
const RE_NEXT_WEEKDAY = /(?:下|下个|下下)(?:周|星期|礼拜)\s*([一二三四五六日天七1-7])/;
const RE_COLON_TIME_ALL = /(\d{1,2})\s*[:：]\s*(\d{1,2})/g;
const RE_POINT_TIME_ALL =
  /([0-9]{1,2}|[零一二三四五六七八九十两]{1,3})\s*[点时](?:\s*(半)|([0-9]{1,2})\s*分?)?/g;

const WEEKDAY_MAP: Record<string, number> = {
  一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7, 七: 7,
  '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
};

function pushUnique(list: string[], item: string | undefined | null): void {
  const t = (item ?? '').trim();
  if (t && !list.includes(t)) list.push(t);
}

/** now 所在的本地日（与 Rust 的 `now_ms/86400000` 同义，但按用户时区取日） */
function localDayIndex(ms: number): number {
  const d = new Date(ms);
  return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86_400_000);
}

function isoFromDayIndex(i: number): string {
  const d = new Date(i * 86_400_000);
  return `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad(d.getUTCMonth() + 1)}-${pad(
    d.getUTCDate(),
  )}`;
}

/** 年月日 → 天数并回读校验：`Date.UTC` 会把 2 月 30 日顺延到 3 月，不能当成合法输入 */
function dayIndexFromYmd(y: number, mo: number, d: number): number | null {
  if (!(mo >= 1 && mo <= 12) || !(d >= 1 && d <= 31)) return null;
  const ms = Date.UTC(y, mo - 1, d);
  if (Number.isNaN(ms)) return null;
  const back = new Date(ms);
  if (
    back.getUTCFullYear() !== y ||
    back.getUTCMonth() !== mo - 1 ||
    back.getUTCDate() !== d
  ) {
    return null;
  }
  return Math.floor(ms / 86_400_000);
}

interface DateHit {
  /** 本地日期 yyyy-mm-dd（未做时区换算，提交时再转绝对时刻） */
  iso: string;
  token: string;
  /** 年份是推测值（句中没写年份且本年已过点） */
  yearAssumed: boolean;
  /** 来自「今天/明天/下周三」这类相对说法 */
  relative: boolean;
}

/** 写了年份却已过期 → 报错，不排一个永远不会到点的任务；没写年份 → 本年、明年各试一次 */
function checkDate(
  y: number,
  mo: number,
  d: number,
  token: string,
  today: number,
  hasYear: boolean,
  raw: string,
): DateHit {
  const isoOf = (year: number) =>
    `${String(year).padStart(4, '0')}-${pad(mo)}-${pad(d)}`;
  if (!(mo >= 1 && mo <= 12) || !(d >= 1 && d <= 31)) {
    throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别日期（${raw}）`);
  }
  if (hasYear) {
    const days = dayIndexFromYmd(y, mo, d);
    if (days === null) throw new Error(`SCHEDULE_PARSE_FAILED: 日期不存在（${isoOf(y)}）`);
    if (days < today) {
      throw new Error(
        `SCHEDULE_INVALID_TIME: 一次性任务的日期已过期（${isoOf(y)}），请改为今后的日期`,
      );
    }
    return { iso: isoOf(y), token, yearAssumed: false, relative: false };
  }
  for (const cand of [y, y + 1]) {
    const days = dayIndexFromYmd(cand, mo, d);
    if (days !== null && days >= today) {
      // 顺延到明年时如实告知：2 月 29 日这类「本年不存在」的日期也算顺延
      return { iso: isoOf(cand), token, yearAssumed: cand !== y, relative: false };
    }
  }
  throw new Error(`SCHEDULE_PARSE_FAILED: 日期不存在（${isoOf(y)}）`);
}

function findDate(raw: string, nowMs: number): DateHit | null {
  const today = localDayIndex(nowMs);
  const ty = new Date(today * 86_400_000).getUTCFullYear();

  const iso = RE_DATE_ISO.exec(raw);
  if (iso) {
    return checkDate(
      Number(iso[1]),
      Number(iso[2]),
      Number(iso[3]),
      iso[0],
      today,
      true,
      raw,
    );
  }
  const cn = RE_DATE_CN.exec(raw);
  if (cn) {
    const hasYear = Boolean(cn[1]);
    return checkDate(
      hasYear ? Number(cn[1]) : ty,
      cnNumber(cn[2]) ?? 0,
      cnNumber(cn[3]) ?? 0,
      cn[0],
      today,
      hasYear,
      raw,
    );
  }
  const rel = RE_RELATIVE_DAY.exec(raw);
  if (rel) {
    const offset =
      rel[1] === '今天' || rel[1] === '今日'
        ? 0
        : rel[1] === '明天' || rel[1] === '明日'
          ? 1
          : rel[1] === '后天'
            ? 2
            : 3; // 大后天
    return {
      iso: isoFromDayIndex(today + offset),
      token: rel[0],
      yearAssumed: false,
      relative: true,
    };
  }
  const next = RE_NEXT_WEEKDAY.exec(raw);
  if (next) {
    const wd = WEEKDAY_MAP[next[1]];
    if (!wd) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别星期（${raw}）`);
    // 1970-01-01 为周四，1=周一 口径下偏移 +3 天（与 nextRunAtMs 同一算法）
    const todayWd = ((((today + 3) % 7) + 7) % 7) + 1;
    let delta = (((wd - todayWd) % 7) + 7) % 7;
    if (delta === 0) delta = 7; // 「下周一」在今天就是周一时指下一周，不是今天
    return {
      iso: isoFromDayIndex(today + delta),
      token: next[0],
      yearAssumed: false,
      relative: true,
    };
  }
  return null;
}

/** 中文回显：解析结果先给用户看这一句，确认后再落库（与 Rust `build_preview` 同文案） */
export function buildPreview(
  freq: LocalFreq,
  timeOfDay: string,
  weekday?: number | null,
  intervalMinutes?: number | null,
  runDate?: string | null,
): string {
  switch (freq) {
    case 'hourly':
      return `每小时第 ${timeOfDay.slice(3)} 分执行`;
    case 'weekly':
      // WEEKDAY_LABELS 自带「周」前缀（界面下拉共用），不能再拼一次
      return `${WEEKDAY_LABELS[weekday ?? 1] ?? '周一'} ${timeOfDay} 执行`;
    case 'monthly':
      return `每月 1 日 ${timeOfDay} 执行`;
    case 'interval': {
      const total = intervalMinutes ?? 0;
      if (total % 1440 === 0) return `每 ${total / 1440} 天执行一次`;
      const h = Math.floor(total / 60);
      const m = total % 60;
      if (h === 0) return `每 ${m} 分钟执行一次`;
      if (m === 0) return `每 ${h} 小时执行一次`;
      return `每 ${h} 小时 ${m} 分钟执行一次`;
    }
    case 'once':
      return runDate
        ? `${runDate} ${timeOfDay} 执行一次（本地时间，确认后落库）`
        : `${timeOfDay} 执行一次`;
    default:
      return `每天 ${timeOfDay} 执行`;
  }
}

/** 一句话解析（与 Rust `parse_natural_at` 同口径；浏览器/Mock 通道使用） */
export function parseNaturalSchedule(
  text: string,
  nowMs: number = Date.now(),
): ScheduleParseResultExtended {
  const raw = (text ?? '').trim();
  if (!raw) {
    throw new Error('SCHEDULE_PARSE_EMPTY: 请用一句话描述执行时间，例如「每周一上午 9 点生成周报」');
  }
  if (RE_INTERVAL_WEEKS.test(raw)) {
    throw new Error(
      'SCHEDULE_UNSUPPORTED_FREQ: 暂不支持「每 N 周」间隔；固定星期请用「每周三」这类说法，按天重复请用「每天」',
    );
  }

  const notes: string[] = [];
  const tokens: string[] = [];
  let freq: LocalFreq | null = null;
  let weekday: number | null = null;
  let intervalMinutes: number | null = null;
  let runDateRelative = false;

  // ── 间隔：每 N 小时（可带余数分钟）/ 每 N 分钟 / 每 N 天 ──
  const hours = RE_INTERVAL_HOURS.exec(raw);
  const minutes = RE_INTERVAL_MINUTES.exec(raw);
  const days = RE_INTERVAL_DAYS.exec(raw);
  if (hours) {
    const h = cnNumber(hours[1]);
    if (h === null) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别间隔小时数（${raw}）`);
    pushUnique(tokens, hours[0]);
    // 「每 2 小时 30 分」：紧跟在小时片段后的分钟数一起算进间隔，不能被当成时刻
    const at = raw.indexOf(hours[0]);
    const rest = at >= 0 ? raw.slice(at + hours[0].length).trimStart() : '';
    const after = RE_MINUTE_AFTER.exec(rest);
    let extra = 0;
    if (after) {
      const m = cnNumber(after[1]);
      if (m === null) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别间隔分钟数（${raw}）`);
      extra = m;
      pushUnique(tokens, after[0]);
    }
    intervalMinutes = normalizeInterval('interval', h * 60 + extra);
    freq = 'interval';
  } else if (minutes) {
    const m = cnNumber(minutes[1]);
    if (m === null) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别间隔分钟数（${raw}）`);
    intervalMinutes = normalizeInterval('interval', m);
    freq = 'interval';
    pushUnique(tokens, minutes[0]);
  } else if (days) {
    const d = cnNumber(days[1]);
    if (d === null) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别间隔天数（${raw}）`);
    // 「每 N 天」换算成分钟步长（不猜成 daily，也不拒）；上限一周由 normalizeInterval 把关
    intervalMinutes = normalizeInterval('interval', d * 1440);
    freq = 'interval';
    pushUnique(tokens, days[0]);
  }

  // ── 周期频率（与改造前同一优先级） ──
  const weekly = /每(?:周|星期|礼拜)\s*([一二三四五六日天七1-7])/.exec(raw);
  const monthlyDay = /每月\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]/.exec(raw);
  if (!freq) {
    if (weekly) {
      const wd = WEEKDAY_MAP[weekly[1]];
      if (!wd) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别星期（${raw}）`);
      freq = 'weekly';
      weekday = wd;
      tokens.push(weekly[0]);
    } else if (monthlyDay) {
      const day = cnNumber(monthlyDay[1]);
      if (day !== 1) {
        throw new Error(
          `SCHEDULE_UNSUPPORTED_DAY: 当前调度只支持每月 1 日（收到 ${String(day ?? monthlyDay[1])} 号），请改用每周或每天`,
        );
      }
      freq = 'monthly';
      tokens.push(monthlyDay[0]);
    } else if (/每(?:个)?小时/.test(raw)) {
      freq = 'hourly';
      tokens.push(/每(?:个)?小时/.exec(raw)![0]);
    } else if (/每月/.test(raw)) {
      freq = 'monthly';
      tokens.push('每月');
    } else if (/每(?:天|日)|天天/.test(raw)) {
      freq = 'daily';
      tokens.push(/每(?:天|日)|天天/.exec(raw)![0]);
    }
  }

  // ── 一次性日期：仅在没写出周期频率时才升级为 once ──
  // 写了「每天/每周…」又带具体日期时保持周期语义：把周期任务悄悄改成只跑一次，
  // 用户以为还在跑，比忽略那个日期更危险——保留周期并把日期回显在提示里。
  const dateHit = findDate(raw, nowMs);
  if (freq && dateHit) {
    notes.push(
      `同时写到了具体日期（${dateHit.iso}），已按周期任务处理；只执行一次请在表单里改为一次性任务`,
    );
  }
  let runDate: string | null = null;
  if (!freq && dateHit) {
    if (dateHit.yearAssumed) {
      notes.push(`未写年份，已按 ${dateHit.iso} 处理；不是这一年的话请在预览里改日期`);
    }
    tokens.push(dateHit.token);
    freq = 'once';
    runDateRelative = dateHit.relative;
    runDate = dateHit.iso;
  }

  if (!freq) {
    notes.push('未写频率，已按每天处理');
    freq = 'daily';
  }

  // ── 时刻 ──
  let hit = findTime(raw, freq);
  if (!hit) {
    if (freq === 'once') {
      notes.push('一次性任务未写时刻，已按 09:00 处理（可在预览里改）');
      hit = { hour: 9, minute: 0, token: '' };
    } else if (freq === 'interval') {
      hit = { hour: 0, minute: 0, token: '' };
    } else {
      throw new Error(
        freq === 'hourly'
          ? 'SCHEDULE_PARSE_NO_TIME: 没识别到分钟，请补充「每小时的第 30 分」这类时间'
          : 'SCHEDULE_PARSE_NO_TIME: 没识别到执行时刻，请补充「上午 9 点」这类时间',
      );
    }
  }
  // 间隔任务没有时刻锚点（从上次执行起算）：句子里的「上午 9 点」留着做名称清洗，
  // 但不写进 timeOfDay，否则界面会显示一个并不会按它触发的时刻。
  if (freq === 'interval' && (hit.hour !== 0 || hit.minute !== 0)) {
    notes.push(`间隔任务不按固定时刻触发，句中的 ${hit.token} 仅作说明`);
    hit = { hour: 0, minute: 0, token: hit.token };
  }
  if (hit.hour > 23 || hit.minute > 59) {
    throw new Error(`SCHEDULE_PARSE_TIME: 无法识别有效时刻（${hit.hour}:${hit.minute}）`);
  }
  pushUnique(tokens, hit.token);
  const period = /(凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间|晚间)/.exec(raw)?.[1];
  if (period && !hit.token.includes(period)) tokens.push(period);

  // ── 歧义提示：句子里出现多个时刻时如实说明取了哪一个 ──
  const timeWords: string[] = [];
  for (const m of raw.match(RE_COLON_TIME_ALL) ?? []) pushUnique(timeWords, m);
  for (const m of raw.match(RE_POINT_TIME_ALL) ?? []) pushUnique(timeWords, m);
  if (timeWords.length > 1) {
    notes.push(
      `识别到多个时刻（${timeWords.join('、')}），已取「${hit.token}」；要按另一个时刻执行请在预览里改`,
    );
  }

  const timeOfDay = `${pad(hit.hour)}:${pad(hit.minute)}`;
  return {
    freq,
    timeOfDay,
    weekday,
    name: cleanScheduleName(raw, tokens),
    note: notes.length ? notes.join('；') : null,
    intervalMinutes,
    runDate,
    runDateRelative,
    preview: buildPreview(freq, timeOfDay, weekday, intervalMinutes, runDate),
  };
}

// ── 下次执行时间（与 Rust next_run_at_ms / next_run_at_ms_full 同构，UTC 口径） ──

export function nextRunAtMs(
  freq: LocalFreq,
  timeOfDay: string,
  weekday: number | null | undefined,
  afterMs: number,
): number {
  const [h, m] = parseHhMm(timeOfDay) ?? [0, 0];
  const secs = Math.floor(afterMs / 1000);
  const days = Math.floor(secs / 86400);
  const tod = ((secs % 86400) + 86400) % 86400;
  const dayStart = days * 86400;
  const minuteOffset = h * 3600 + m * 60;
  let target: number;

  switch (freq) {
    case 'hourly': {
      let t = dayStart + Math.floor(tod / 3600) * 3600 + m * 60;
      while (t <= secs) t += 3600;
      target = t;
      break;
    }
    case 'weekly': {
      const wd = Math.min(7, Math.max(1, weekday ?? 1));
      // 1970-01-01 为周四，1=周一 口径下偏移 +3 天
      const todayWd = (((days + 3) % 7) + 7) % 7 + 1;
      const delta = (((wd - todayWd) % 7) + 7) % 7;
      let t = dayStart + delta * 86400 + minuteOffset;
      if (t <= secs) t += 7 * 86400;
      target = t;
      break;
    }
    case 'monthly': {
      const d = new Date(days * 86_400_000);
      const y = d.getUTCFullYear();
      const mo = d.getUTCMonth();
      const first = Date.UTC(y, mo + 1, 1) / 1000 + minuteOffset;
      target = first > secs ? first : Date.UTC(y, mo + 2, 1) / 1000 + minuteOffset;
      break;
    }
    default: {
      let t = dayStart + minuteOffset;
      if (t <= secs) t += 86400;
      target = t;
    }
  }
  return target * 1000;
}

/**
 * 含间隔与一次性的下次执行时间（与 Rust `next_run_at_ms_full` 同口径）。
 * 返回 null＝不再排期：一次性任务已过点，或间隔任务缺步长——
 * 绝不能退化成「每天」，那会把一次任务变成永久任务。
 */
export function nextRunAtMsFull(
  freq: LocalFreq,
  timeOfDay: string,
  weekday: number | null | undefined,
  intervalMinutes: number | null | undefined,
  runAtMs: number | null | undefined,
  afterMs: number,
): number | null {
  if (freq === 'interval') {
    const step = intervalMinutes ?? 0;
    if (step < INTERVAL_MIN_MINUTES) return null;
    return afterMs + step * 60_000;
  }
  if (freq === 'once') {
    const t = runAtMs ?? null;
    return t !== null && t > afterMs ? t : null;
  }
  return nextRunAtMs(freq, timeOfDay, weekday, afterMs);
}

/**
 * 相对说法的日期重锚：Rust 侧只有 UTC（本模块无 chrono），「明天」按 `now` 的 UTC 日展开；
 * 界面读到 `runDateRelative` 后按本地日重锚，避免 UTC+8 用户在凌晨～早晨说出「明天」时差一天。
 * 做法是只平移「距今 N 天」这个天数差，不动月日；绝对日期（relative=false）原样返回。
 */
export function reanchorRelativeRunDate(
  iso: string | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(iso.trim());
  if (!m) return iso;
  const target = dayIndexFromYmd(Number(m[1]), Number(m[2]), Number(m[3]));
  if (target === null) return iso;
  const utcToday = Math.floor(nowMs / 86_400_000);
  return isoFromDayIndex(localDayIndex(nowMs) + (target - utcToday));
}

/**
 * 把解析结果里的相对日期重锚到本地日（绝对日期原样返回），并同步改写预览文案。
 * Rust 与本地台账两条通道都经这一步，界面看到的永远是用户本地时区的那一天。
 */
export function resolveParsedRunDate(
  parsed: ScheduleParseResultExtended,
  nowMs: number = Date.now(),
): { runDate: string | null; preview: string } {
  const runDate = parsed.runDate
    ? parsed.runDateRelative
      ? reanchorRelativeRunDate(parsed.runDate, nowMs)
      : parsed.runDate
    : null;
  return {
    runDate,
    preview:
      parsed.preview ??
      buildPreview(
        parsed.freq,
        parsed.timeOfDay,
        parsed.weekday,
        parsed.intervalMinutes,
        runDate,
      ),
  };
}

// ── 本地时区换算（Rust 侧存 UTC，界面按本地呈现/录入） ─────────

/** 本地 HH:MM → UTC HH:MM（按当前时区偏移换算；中国等无夏令时时区精确） */
export function localToUtcTime(localHhMm: string, at: Date = new Date()): string {
  const parsed = parseHhMm(localHhMm);
  if (!parsed) throw new Error(`SCHEDULE_INVALID_TIME: ${String(localHhMm)}`);
  const d = new Date(at.getTime());
  d.setHours(parsed[0], parsed[1], 0, 0);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** UTC HH:MM → 本地 HH:MM（用于回显，与 localToUtcTime 互逆） */
export function utcToLocalTime(utcHhMm: string, at: Date = new Date()): string {
  const parsed = parseHhMm(utcHhMm);
  if (!parsed) throw new Error(`SCHEDULE_INVALID_TIME: ${String(utcHhMm)}`);
  const d = new Date(
    Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate(), parsed[0], parsed[1]),
  );
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 绝对时间戳 → 本地「2026/9/22 09:00」 */
export function formatLocalDateTime(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * 本地日期（yyyy-mm-dd）+ 本地时刻（HH:MM）→ 绝对时刻（UTC 毫秒）。
 * 一次性任务落库前的一步：`timeOfDay` 走 `localToUtcTime` 存 UTC 时刻，
 * `runAtMs` 则必须是真实绝对时刻，否则跨到 UTC 另一侧就会跑错一天。
 */
export function localRunDateToUtcMs(runDate: string, localHhMm: string): number {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec((runDate ?? '').trim());
  const hm = parseHhMm(localHhMm);
  if (!m || !hm) {
    throw new Error(
      `SCHEDULE_INVALID_TIME: 一次性任务需给出执行日期与时刻（收到 ${String(runDate)} ${String(localHhMm)}）`,
    );
  }
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), hm[0], hm[1], 0, 0);
  if (Number.isNaN(d.getTime())) {
    throw new Error(`SCHEDULE_INVALID_TIME: 无法识别执行日期（${String(runDate)}）`);
  }
  // Date 会把 2 月 30 日顺延到 3 月，回读校验，不把非法日期悄悄排出去
  if (
    d.getFullYear() !== Number(m[1]) ||
    d.getMonth() !== Number(m[2]) - 1 ||
    d.getDate() !== Number(m[3])
  ) {
    throw new Error(`SCHEDULE_INVALID_TIME: 日期不存在（${String(runDate)}）`);
  }
  return d.getTime();
}

/** 界面「间隔」输入的单位 */
export type IntervalUnit = 'minutes' | 'hours' | 'days';

export const INTERVAL_UNITS: Record<IntervalUnit, string> = {
  minutes: '分钟',
  hours: '小时',
  days: '天',
};

/** 间隔单位 → 分钟（界面录入用；非法数值如实拒，不夹到最近的合法值） */
export function intervalToMinutes(value: number, unit: IntervalUnit): number {
  if (!Number.isInteger(value)) {
    throw new Error(`SCHEDULE_INVALID_FREQ: 间隔需是整数（收到 ${String(value)}）`);
  }
  const per = unit === 'minutes' ? 1 : unit === 'hours' ? 60 : 1440;
  // freq 固定 interval，normalizeInterval 只会返回数值或抛错
  return normalizeInterval('interval', value * per) as number;
}

/** 分钟步长 → 界面回显的最佳单位（能整除天用天，能整除小时用小时，否则分钟） */
export function minutesToIntervalParts(
  minutes: number | null | undefined,
): { value: number; unit: IntervalUnit } {
  const total = minutes ?? 0;
  if (total > 0 && total % 1440 === 0) return { value: total / 1440, unit: 'days' };
  if (total > 0 && total % 60 === 0) return { value: total / 60, unit: 'hours' };
  return { value: total, unit: 'minutes' };
}

/** 绝对时刻 → 本地日期字符串 yyyy-mm-dd（一次性任务编辑回填用） */
export function localDateFromMs(ms: number | null | undefined): string {
  if (!ms) return '';
  const d = new Date(ms);
  return `${String(d.getFullYear()).padStart(4, '0')}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 计划描述（本地时刻口径，列表与推荐卡片共用） */
export function describeSchedule(
  freq: LocalFreq,
  timeOfDayUtc: string,
  weekday?: number | null,
  intervalMinutes?: number | null,
  runAtMs?: number | null,
): string {
  if (freq === 'interval') {
    const total = intervalMinutes ?? 0;
    if (total % 1440 === 0 && total > 0) return `每 ${total / 1440} 天`;
    const h = Math.floor(total / 60);
    const m = total % 60;
    if (h === 0) return `每 ${m} 分钟`;
    if (m === 0) return `每 ${h} 小时`;
    return `每 ${h} 小时 ${m} 分钟`;
  }
  if (freq === 'once') {
    return runAtMs ? `${formatLocalDateTime(runAtMs)} 执行一次` : '待指定时刻执行一次';
  }
  const local = utcToLocalTime(timeOfDayUtc);
  switch (freq) {
    case 'hourly':
      return `每小时第 ${Number(local.split(':')[1])} 分`;
    case 'weekly':
      // 「周三」这类标签自带「周」前缀，拼成「每周周三」是病句，去掉前缀再拼
      return `每周${(WEEKDAY_LABELS[weekday ?? 1] ?? '周一').slice(1)} ${local}`;
    case 'monthly':
      return `每月 1 日 ${local}`;
    default:
      return `每天 ${local}`;
  }
}

// ── 本地台账（Mock / 引擎直连通道权威数据源） ─────────────────

const LEDGER_KEY = 'tt:schedules';
/** 持久化上限：仅保留最近 200 条执行记录，避免本地存储无限增长 */
const PERSIST_RUN_LIMIT = 200;

let ledger: ScheduledTaskExtended[] = [];
let runs: ScheduleRun[] = [];
let seq = 0;

/** 无本地持久层（node/受限环境）时返回 null，台账退化为内存态 */
function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function persist(): void {
  const s = storage();
  if (!s) return;
  try {
    s.setItem(
      LEDGER_KEY,
      JSON.stringify({ tasks: ledger, runs: localRuns(undefined, PERSIST_RUN_LIMIT), seq }),
    );
  } catch {
    /* 存储不可用/超限不影响本次操作结果 */
  }
}

/** 启动时恢复上次会话的任务与执行记录（开发/联调阶段刷新不丢数据） */
function hydrate(): void {
  const s = storage();
  if (!s) return;
  try {
    const raw = s.getItem(LEDGER_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as {
      tasks?: ScheduledTaskExtended[];
      runs?: ScheduleRun[];
      seq?: number;
    };
    if (Array.isArray(parsed.tasks)) ledger = parsed.tasks;
    if (Array.isArray(parsed.runs)) runs = parsed.runs;
    if (typeof parsed.seq === 'number' && parsed.seq > seq) seq = parsed.seq;
  } catch {
    /* 本地数据损坏时以空台账启动，不阻塞页面 */
  }
}

hydrate();

export function resetLocalSchedules(): void {
  ledger = [];
  runs = [];
  seq = 0;
  persist();
}

export function localSchedules(): ScheduledTaskExtended[] {
  return [...ledger].sort((a, b) => {
    const an = a.nextRunAtMs ?? Number.POSITIVE_INFINITY;
    const bn = b.nextRunAtMs ?? Number.POSITIVE_INFINITY;
    return an - bn || b.createdAtMs - a.createdAtMs;
  });
}

export function createLocalSchedule(
  req: ScheduleCreateReqExtended,
  nowMs: number = Date.now(),
): ScheduledTaskExtended {
  const name = normalizeName(req.name);
  const freq = normalizeFreq(req.freq);
  const timeOfDay = normalizeTime(req.timeOfDay);
  const weekday = normalizeWeekday(freq, req.weekday);
  // 间隔步长 / 一次性时刻：非法即拒（越界间隔、过期时刻都不落库），其余频率一律清空
  const intervalMinutes = normalizeInterval(freq, req.intervalMinutes);
  const runAtMs = normalizeRunAt(freq, req.runAtMs, nowMs);
  const notify = normalizeNotify(req.notifyChannel, req.notifyTarget);
  seq += 1;
  const task: ScheduledTaskExtended = {
    scheduleId: `sched-${nowMs}-${seq}`,
    name,
    description: req.description ?? '',
    freq,
    timeOfDay,
    weekday,
    enabled: true,
    workspaceId: req.workspaceId ?? 'default',
    createdAtMs: nowMs,
    updatedAtMs: nowMs,
    nextRunAtMs: nextRunAtMsFull(freq, timeOfDay, weekday, intervalMinutes, runAtMs, nowMs),
    lastRunAtMs: null,
    lastStatus: null,
    lastTaskId: null,
    intervalMinutes,
    runAtMs,
    ...notify,
  };
  ledger = [...ledger, task];
  persist();
  return task;
}

export function updateLocalSchedule(
  req: ScheduleUpdateReqExtended,
  nowMs: number = Date.now(),
): ScheduledTaskExtended {
  const scheduleId = String(req.scheduleId ?? '').trim();
  if (!scheduleId) throw new Error('SCHEDULE_INVALID_ID: 缺少定时任务标识');
  const idx = ledger.findIndex((t) => t.scheduleId === scheduleId);
  if (idx < 0) throw new Error(`SCHEDULE_NOT_FOUND: ${scheduleId}`);
  const cur = ledger[idx];

  const name = req.name !== undefined ? normalizeName(req.name) : cur.name;
  const description =
    req.description !== undefined ? String(req.description).trim() : cur.description;
  const freq = req.freq !== undefined ? normalizeFreq(req.freq) : cur.freq;
  const timeOfDay = req.timeOfDay !== undefined ? normalizeTime(req.timeOfDay) : cur.timeOfDay;
  // 与 Rust 同口径：改频率或改星期时重算（weekly 必填，其余频率清空），否则沿用原值
  const weekday =
    req.freq !== undefined || req.weekday !== undefined
      ? normalizeWeekday(freq, req.weekday ?? cur.weekday)
      : cur.weekday ?? null;
  const workspaceId =
    req.workspaceId !== undefined
      ? (req.workspaceId.trim() || 'default')
      : cur.workspaceId || 'default';
  // 与 Rust 同口径：通道传空串＝关闭推送；切通道不继承旧目标
  let notify = {
    notifyChannel: cur.notifyChannel ?? '',
    notifyTarget: cur.notifyTarget ?? '',
  };
  if (req.notifyChannel !== undefined || req.notifyTarget !== undefined) {
    const ch = (req.notifyChannel !== undefined ? req.notifyChannel : cur.notifyChannel ?? '')
      .trim()
      .toLowerCase();
    if (!ch) {
      notify = { notifyChannel: '', notifyTarget: '' };
    } else {
      const tg =
        req.notifyTarget !== undefined
          ? req.notifyTarget.trim()
          : ch === (cur.notifyChannel ?? '')
            ? cur.notifyTarget ?? ''
            : '';
      notify = normalizeNotify(ch, tg);
    }
  }

  // 与 Rust 同口径：改频率或改字段任一发生即重算，换成别的频率就清空
  // （留着旧步长会让界面显示一个并不生效的配置）
  const intervalMinutes =
    req.freq !== undefined || req.intervalMinutes !== undefined
      ? normalizeInterval(freq, req.intervalMinutes ?? cur.intervalMinutes)
      : normalizeInterval(freq, cur.intervalMinutes);
  const runAtMs =
    req.freq !== undefined || req.runAtMs !== undefined
      ? normalizeRunAt(freq, req.runAtMs ?? cur.runAtMs, nowMs)
      : normalizeRunAt(freq, cur.runAtMs, nowMs);

  const next: ScheduledTaskExtended = {
    ...cur,
    name,
    description,
    freq,
    timeOfDay,
    weekday,
    workspaceId,
    intervalMinutes,
    runAtMs,
    ...notify,
    updatedAtMs: nowMs,
    // 禁用任务编辑后仍不排期，避免「改完自己跑起来」
    nextRunAtMs: cur.enabled
      ? nextRunAtMsFull(freq, timeOfDay, weekday, intervalMinutes, runAtMs, nowMs)
      : null,
  };
  ledger = ledger.map((t, i) => (i === idx ? next : t));
  persist();
  return next;
}

export function toggleLocalSchedule(
  scheduleId: string,
  enabled: boolean,
  nowMs: number = Date.now(),
): ScheduledTaskExtended {
  const idx = ledger.findIndex((t) => t.scheduleId === scheduleId);
  if (idx < 0) throw new Error(`SCHEDULE_NOT_FOUND: ${scheduleId}`);
  const cur = ledger[idx];
  // 与 Rust set_enabled 同口径：已过点的一次性任务不允许「重新启用」，
  // 否则会得到一个 nextRunAtMs 为空、界面显示已启用却永远不会跑的僵尸行
  if (enabled && cur.freq === 'once' && (cur.runAtMs ?? 0) <= nowMs) {
    throw new Error(
      `SCHEDULE_INVALID_TIME: 一次性任务「${cur.name}」原定执行时间已过，请先改日期再启用`,
    );
  }
  const next: ScheduledTaskExtended = {
    ...cur,
    enabled,
    updatedAtMs: nowMs,
    nextRunAtMs: enabled
      ? nextRunAtMsFull(cur.freq, cur.timeOfDay, cur.weekday, cur.intervalMinutes, cur.runAtMs, nowMs)
      : null,
  };
  ledger = ledger.map((t, i) => (i === idx ? next : t));
  persist();
  return next;
}

export function removeLocalSchedule(scheduleId: string): boolean {
  const existed = ledger.some((t) => t.scheduleId === scheduleId);
  ledger = ledger.filter((t) => t.scheduleId !== scheduleId);
  runs = runs.filter((r) => r.scheduleId !== scheduleId);
  persist();
  return existed;
}

export function localRuns(scheduleId?: string, limit = 100): ScheduleRun[] {
  const filtered = scheduleId ? runs.filter((r) => r.scheduleId === scheduleId) : runs;
  const max = limit > 0 ? Math.min(limit, 500) : 100;
  return [...filtered].sort((a, b) => b.startedAtMs - a.startedAtMs).slice(0, max);
}

/**
 * 记一条执行记录并回写任务的上次执行状态/下次执行时间（与 Rust finish_run 同构）。
 *
 * 口径说明：一次性任务的「跑完自动停用」发生在 Rust 的 `advance_next`（到点扫描链路），
 * 手动立即执行不经过它，所以这里同样不因执行而停用；本层是浏览器/Mock 通道，
 * 没有扫描线程，到点自动执行与自动停用只在真实桌面端成立（真机未验收）。
 */
export function recordLocalRun(
  scheduleId: string,
  status: ScheduleRunStatus,
  summary: string,
  nowMs: number = Date.now(),
  taskId?: string,
): ScheduleRun {
  seq += 1;
  const run: ScheduleRun = {
    runId: `run-${nowMs}-${seq}`,
    scheduleId,
    taskId: taskId ?? null,
    startedAtMs: nowMs,
    finishedAtMs: nowMs,
    status,
    summary,
  };
  runs = [...runs, run];
  ledger = ledger.map((t) =>
    t.scheduleId === scheduleId
      ? {
          ...t,
          lastRunAtMs: nowMs,
          lastStatus: status,
          lastTaskId: taskId ?? t.lastTaskId ?? null,
          updatedAtMs: nowMs,
          nextRunAtMs: nextRunAtMsFull(
            t.freq,
            t.timeOfDay,
            t.weekday,
            t.intervalMinutes,
            t.runAtMs,
            nowMs,
          ),
        }
      : t,
  );
  persist();
  return run;
}

// ── 响应解析（Rust/引擎返回体 → 行数组） ──────────────────────

export function extractScheduleRows(resp: unknown): ScheduledTaskExtended[] {
  if (Array.isArray(resp)) return resp as ScheduledTaskExtended[];
  const rows = (resp as { rows?: unknown } | null)?.rows;
  return Array.isArray(rows) ? (rows as ScheduledTaskExtended[]) : [];
}

export function extractRunRows(resp: unknown): ScheduleRun[] {
  if (Array.isArray(resp)) return resp as ScheduleRun[];
  const rows = (resp as { rows?: unknown } | null)?.rows;
  return Array.isArray(rows) ? (rows as ScheduleRun[]) : [];
}

// ── 通道兜底（Mock 与引擎直连共用，无 Rust 后端时启用） ────────

export interface LocalScheduleResult {
  handled: boolean;
  value?: unknown;
}

/**
 * 本地定时任务通道：无 Rust 后端（Mock / 浏览器直连引擎）时的权威实现。
 * 立即执行不调用真实引擎，记一条明确标注的 Mock 执行记录（不静默假装已执行）。
 */
export function handleLocalScheduleChannel(
  channel: string,
  args: unknown[],
): LocalScheduleResult {
  if (channel === 'schedule:list') {
    return { handled: true, value: localSchedules() };
  }
  if (channel === 'schedule:parse') {
    return { handled: true, value: parseNaturalSchedule(String(args[0] ?? '')) };
  }
  if (channel === 'schedule:create') {
    return { handled: true, value: createLocalSchedule(args[0] as ScheduleCreateReqExtended) };
  }
  if (channel === 'schedule:update') {
    return { handled: true, value: updateLocalSchedule(args[0] as ScheduleUpdateReqExtended) };
  }
  if (channel === 'schedule:toggle') {
    return {
      handled: true,
      value: toggleLocalSchedule(String(args[0]), Boolean(args[1])),
    };
  }
  if (channel === 'schedule:delete') {
    return { handled: true, value: { ok: true, removed: removeLocalSchedule(String(args[0])) } };
  }
  if (channel === 'schedule:runs') {
    const sid = typeof args[0] === 'string' && args[0] ? args[0] : undefined;
    return { handled: true, value: localRuns(sid, Number(args[1] ?? 0)) };
  }
  if (channel === 'schedule:runNow') {
    const scheduleId = String(args[0]);
    if (!localSchedules().some((t) => t.scheduleId === scheduleId)) {
      throw new Error(`SCHEDULE_NOT_FOUND: ${scheduleId}`);
    }
    recordLocalRun(
      scheduleId,
      'succeeded',
      'Mock 通道模拟执行（未调用真实引擎；关闭 Mock 后走 Rust 调度）',
    );
    return { handled: true, value: { ok: true, scheduleId, started: true } };
  }
  if (channel === 'keepAwake:status') {
    const requested = Boolean(args[0]);
    // 浏览器预览环境无法施加系统级唤醒约束，如实返回 supported=false
    const status: KeepAwakeStatus = { requested, active: false, supported: false };
    return { handled: true, value: status };
  }
  return { handled: false };
}