/**
 * 通天办公 - 定时任务本地实现（Mock 与浏览器直连引擎通道共用）
 *
 * 与 Rust `task::schedule` 保持同一口径：
 * - 频率枚举与中文标签归一（每小时/每天/每周/每月）
 * - 时刻为 24 小时制 HH:MM；Rust 侧按 UTC 存储与计算（无 chrono 依赖）
 * - 本层额外提供「本地时刻 ↔ UTC 时刻」换算，让界面按本地时区呈现与录入
 * - 下次执行时间算法与 Rust 完全同构（daily/weekly/monthly/hourly）
 *
 * 台账落地 `localStorage`（`tt:schedules`，Mock 开关规范：开发阶段不依赖真实后端，
 * 但刷新不丢已建任务）；Tauri 容器内不走本层，直接落 SQLite。
 */

import type {
  KeepAwakeStatus,
  ScheduleCreateReq,
  ScheduleFreq,
  ScheduleParseResult,
  ScheduleRun,
  ScheduleRunStatus,
  ScheduledTask,
  ScheduleUpdateReq,
} from '../../shared/contracts';

export const SCHEDULE_FREQS: ScheduleFreq[] = ['hourly', 'daily', 'weekly', 'monthly'];

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
export const FREQ_LABELS: Record<string, ScheduleFreq> = {
  每小时: 'hourly',
  每天: 'daily',
  每周: 'weekly',
  每月: 'monthly',
};

export function freqLabel(freq: ScheduleFreq): string {
  const label = Object.keys(FREQ_LABELS).find((k) => FREQ_LABELS[k] === freq);
  return label ?? freq;
}

// ── 规范化（错误码与前端 error-messages 对齐） ────────────────

export function normalizeFreq(v: string): ScheduleFreq {
  const t = (v ?? '').trim();
  const mapped = FREQ_LABELS[t] ?? t.toLowerCase();
  if ((SCHEDULE_FREQS as string[]).includes(mapped)) return mapped as ScheduleFreq;
  throw new Error(`SCHEDULE_INVALID_FREQ: ${String(v)}`);
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

export function normalizeWeekday(freq: ScheduleFreq, weekday?: number | null): number | null {
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
  freq: ScheduleFreq,
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

/** 一句话解析（与 Rust `parse_natural` 同口径；浏览器/Mock 通道使用） */
export function parseNaturalSchedule(text: string): ScheduleParseResult {
  const raw = (text ?? '').trim();
  if (!raw) {
    throw new Error('SCHEDULE_PARSE_EMPTY: 请用一句话描述执行时间，例如「每周一上午 9 点生成周报」');
  }
  if (
    /(?:每|每隔)\s*[0-9一二三四五六七八九十两]+\s*(?:个)?\s*(?:小时|钟头)/.test(raw) ||
    /(?:每|每隔)\s*[0-9一二三四五六七八九十两]+\s*(?:分钟|分)/.test(raw)
  ) {
    throw new Error('SCHEDULE_UNSUPPORTED_FREQ: 暂不支持按「每 N 小时 / 每 N 分钟」间隔调度，请改用每小时、每天、每周或每月');
  }

  const tokens: string[] = [];
  let freq: ScheduleFreq | null = null;
  let weekday: number | null = null;

  const weekly = /每(?:周|星期|礼拜)\s*([一二三四五六日天七1-7])/.exec(raw);
  const monthlyDay = /每月\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]/.exec(raw);
  if (weekly) {
    const map: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7, 七: 7, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7 };
    weekday = map[weekly[1]] ?? null;
    if (weekday === null) throw new Error(`SCHEDULE_PARSE_FAILED: 无法识别星期（${raw}）`);
    freq = 'weekly';
    tokens.push(weekly[0]);
  } else if (monthlyDay) {
    const day = cnNumber(monthlyDay[1]);
    if (day !== 1) {
      throw new Error(`SCHEDULE_UNSUPPORTED_DAY: 当前调度只支持每月 1 日（收到 ${String(day ?? monthlyDay[1])} 号），请改用每周或每天`);
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

  let note: string | null = null;
  if (!freq) {
    note = '未写频率，已按每天处理';
    freq = 'daily';
  }

  const hit = findTime(raw, freq);
  if (!hit) {
    throw new Error(
      freq === 'hourly'
        ? 'SCHEDULE_PARSE_NO_TIME: 没识别到分钟，请补充「每小时的第 30 分」这类时间'
        : 'SCHEDULE_PARSE_NO_TIME: 没识别到执行时刻，请补充「上午 9 点」这类时间',
    );
  }
  if (hit.hour > 23 || hit.minute > 59) {
    throw new Error(`SCHEDULE_PARSE_TIME: 无法识别有效时刻（${hit.hour}:${hit.minute}）`);
  }
  tokens.push(hit.token);
  const period = /(凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间|晚间)/.exec(raw)?.[1];
  if (period && !hit.token.includes(period)) tokens.push(period);

  return {
    freq,
    timeOfDay: `${pad(hit.hour)}:${pad(hit.minute)}`,
    weekday,
    name: cleanScheduleName(raw, tokens),
    note,
  };
}

// ── 下次执行时间（与 Rust next_run_at_ms 同构，UTC 口径） ──────

export function nextRunAtMs(
  freq: ScheduleFreq,
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

/** 计划描述（本地时刻口径，列表与推荐卡片共用） */
export function describeSchedule(
  freq: ScheduleFreq,
  timeOfDayUtc: string,
  weekday?: number | null,
): string {
  const local = utcToLocalTime(timeOfDayUtc);
  switch (freq) {
    case 'hourly':
      return `每小时第 ${Number(local.split(':')[1])} 分`;
    case 'weekly':
      return `每周${WEEKDAY_LABELS[weekday ?? 1] ?? '周一'} ${local}`;
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

let ledger: ScheduledTask[] = [];
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
      tasks?: ScheduledTask[];
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

export function localSchedules(): ScheduledTask[] {
  return [...ledger].sort((a, b) => {
    const an = a.nextRunAtMs ?? Number.POSITIVE_INFINITY;
    const bn = b.nextRunAtMs ?? Number.POSITIVE_INFINITY;
    return an - bn || b.createdAtMs - a.createdAtMs;
  });
}

export function createLocalSchedule(req: ScheduleCreateReq, nowMs: number = Date.now()): ScheduledTask {
  const name = normalizeName(req.name);
  const freq = normalizeFreq(req.freq);
  const timeOfDay = normalizeTime(req.timeOfDay);
  const weekday = normalizeWeekday(freq, req.weekday);
  const notify = normalizeNotify(req.notifyChannel, req.notifyTarget);
  seq += 1;
  const task: ScheduledTask = {
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
    nextRunAtMs: nextRunAtMs(freq, timeOfDay, weekday, nowMs),
    lastRunAtMs: null,
    lastStatus: null,
    lastTaskId: null,
    ...notify,
  };
  ledger = [...ledger, task];
  persist();
  return task;
}

export function updateLocalSchedule(
  req: ScheduleUpdateReq,
  nowMs: number = Date.now(),
): ScheduledTask {
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

  const next: ScheduledTask = {
    ...cur,
    name,
    description,
    freq,
    timeOfDay,
    weekday,
    workspaceId,
    ...notify,
    updatedAtMs: nowMs,
    // 禁用任务编辑后仍不排期，避免「改完自己跑起来」
    nextRunAtMs: cur.enabled ? nextRunAtMs(freq, timeOfDay, weekday, nowMs) : null,
  };
  ledger = ledger.map((t, i) => (i === idx ? next : t));
  persist();
  return next;
}

export function toggleLocalSchedule(
  scheduleId: string,
  enabled: boolean,
  nowMs: number = Date.now(),
): ScheduledTask {
  const idx = ledger.findIndex((t) => t.scheduleId === scheduleId);
  if (idx < 0) throw new Error(`SCHEDULE_NOT_FOUND: ${scheduleId}`);
  const cur = ledger[idx];
  const next: ScheduledTask = {
    ...cur,
    enabled,
    updatedAtMs: nowMs,
    nextRunAtMs: enabled
      ? nextRunAtMs(cur.freq, cur.timeOfDay, cur.weekday, nowMs)
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
 * 记一条执行记录并回写任务的上次执行状态/下次执行时间（与 Rust finish_run 同构）
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
          nextRunAtMs: nextRunAtMs(t.freq, t.timeOfDay, t.weekday, nowMs),
        }
      : t,
  );
  persist();
  return run;
}

// ── 响应解析（Rust/引擎返回体 → 行数组） ──────────────────────

export function extractScheduleRows(resp: unknown): ScheduledTask[] {
  if (Array.isArray(resp)) return resp as ScheduledTask[];
  const rows = (resp as { rows?: unknown } | null)?.rows;
  return Array.isArray(rows) ? (rows as ScheduledTask[]) : [];
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
    return { handled: true, value: createLocalSchedule(args[0] as ScheduleCreateReq) };
  }
  if (channel === 'schedule:update') {
    return { handled: true, value: updateLocalSchedule(args[0] as ScheduleUpdateReq) };
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