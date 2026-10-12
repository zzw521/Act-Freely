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

  const next: ScheduledTask = {
    ...cur,
    name,
    description,
    freq,
    timeOfDay,
    weekday,
    workspaceId,
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