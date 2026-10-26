/**
 * 通天办公 - 定时任务状态管理
 *
 * 三通道统一走 `window.tongtianAPI.invoke`：
 * - Tauri 容器 → Rust command（authority：任务定义/执行记录落 SQLite，调度由 Rust 扫描线程驱动）
 * - 浏览器直连引擎 → 前端本地台账（`lib/schedule-local.ts`）
 * - 开发 Mock → 同上（Mock 开关规范：无后端也可自测）
 *
 * 「保持系统唤醒」写入 settings（Tauri 下由 Rust 施加真实系统约束），
 * 再读回实际状态：supported=false / active=false 场景如实暴露，不假装已生效。
 */

import { create } from 'zustand';
import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  KeepAwakeStatus,
  ScheduleCreateReq,
  ScheduleParseResult,
  ScheduleRun,
  ScheduledTask,
  ScheduleUpdateReq,
} from '../../shared/contracts';
import { errorTextOf } from '../../shared/error-messages';
import { setSettings, getSettings } from '../lib/alignment-api';
import { extractRunRows, extractScheduleRows } from '../lib/schedule-local';

interface ScheduleState {
  tasks: ScheduledTask[];
  runs: ScheduleRun[];
  keepAwake: KeepAwakeStatus | null;
  tasksLoading: boolean;
  runsLoading: boolean;
  error: string | null;

  loadTasks: () => Promise<void>;
  loadRuns: (scheduleId?: string) => Promise<void>;
  /** 一句话解析（本地规则；失败时抛带错误码的异常） */
  parse: (text: string) => Promise<ScheduleParseResult>;
  create: (req: ScheduleCreateReq) => Promise<ScheduledTask>;
  update: (req: ScheduleUpdateReq) => Promise<ScheduledTask>;
  toggle: (scheduleId: string, enabled: boolean) => Promise<void>;
  remove: (scheduleId: string) => Promise<void>;
  runNow: (scheduleId: string) => Promise<void>;
  loadKeepAwake: () => Promise<void>;
  setKeepAwake: (enabled: boolean) => Promise<KeepAwakeStatus>;
  reset: () => void;
}

function getAPI() {
  const api = window.tongtianAPI;
  if (!api) {
    throw new Error('定时任务接口不可用（tongtianAPI 未注入）');
  }
  return api;
}

export const useScheduleStore = create<ScheduleState>((set) => ({
  tasks: [],
  runs: [],
  keepAwake: null,
  tasksLoading: false,
  runsLoading: false,
  error: null,

  loadTasks: async () => {
    set({ tasksLoading: true });
    try {
      const resp = await getAPI().invoke(IPC_CHANNELS.SCHEDULE_LIST);
      set({ tasks: extractScheduleRows(resp), tasksLoading: false, error: null });
    } catch (err) {
      set({ tasks: [], tasksLoading: false, error: `定时任务加载失败：${errorTextOf(err)}` });
    }
  },

  loadRuns: async (scheduleId) => {
    set({ runsLoading: true });
    try {
      const resp = await getAPI().invoke(IPC_CHANNELS.SCHEDULE_RUNS, scheduleId ?? null, 100);
      set({ runs: extractRunRows(resp), runsLoading: false, error: null });
    } catch (err) {
      set({ runs: [], runsLoading: false, error: `执行记录加载失败：${errorTextOf(err)}` });
    }
  },

  parse: async (text) => {
    const result = (await getAPI().invoke(
      IPC_CHANNELS.SCHEDULE_PARSE,
      text,
    )) as ScheduleParseResult;
    return result;
  },

  create: async (req) => {
    const task = (await getAPI().invoke(IPC_CHANNELS.SCHEDULE_CREATE, req)) as ScheduledTask;
    await useScheduleStore.getState().loadTasks();
    return task;
  },

  /** 编辑任务：缺省字段沿用原配置，启用中的任务由后端按当前时刻重算下次执行时间 */
  update: async (req) => {
    const task = (await getAPI().invoke(IPC_CHANNELS.SCHEDULE_UPDATE, req)) as ScheduledTask;
    await useScheduleStore.getState().loadTasks();
    return task;
  },

  toggle: async (scheduleId, enabled) => {
    await getAPI().invoke(IPC_CHANNELS.SCHEDULE_TOGGLE, scheduleId, enabled);
    await useScheduleStore.getState().loadTasks();
  },

  remove: async (scheduleId) => {
    await getAPI().invoke(IPC_CHANNELS.SCHEDULE_DELETE, scheduleId);
    await useScheduleStore.getState().loadTasks();
    await useScheduleStore.getState().loadRuns();
  },

  /** 立即执行：后端异步触发（真实调度链路），刷新列表即可看到新一轮执行记录 */
  runNow: async (scheduleId) => {
    await getAPI().invoke(IPC_CHANNELS.SCHEDULE_RUN_NOW, scheduleId);
    await useScheduleStore.getState().loadTasks();
    await useScheduleStore.getState().loadRuns();
  },

  /** 读取保持唤醒意愿（settings）与实际状态（真实约束是否生效） */
  loadKeepAwake: async () => {
    try {
      const settings = await getSettings();
      const want = Boolean(settings.keepAwake);
      const status = (await getAPI().invoke(
        IPC_CHANNELS.KEEP_AWAKE_STATUS,
        want,
      )) as KeepAwakeStatus;
      set({ keepAwake: { ...status, requested: status.requested ?? want }, error: null });
    } catch (err) {
      set({ keepAwake: null, error: `保持唤醒状态读取失败：${errorTextOf(err)}` });
    }
  },

  setKeepAwake: async (enabled) => {
    set({ error: null });
    // 落库并触发系统行为（Tauri：Rust 施加/释放真实唤醒约束）
    await setSettings({ keepAwake: enabled });
    const status = (await getAPI().invoke(
      IPC_CHANNELS.KEEP_AWAKE_STATUS,
      enabled,
    )) as KeepAwakeStatus;
    const resolved: KeepAwakeStatus = { ...status, requested: enabled };
    set({ keepAwake: resolved });
    if (enabled && !resolved.active) {
      set({
        error: resolved.supported
          ? '保持系统唤醒未能生效（唤醒约束进程启动失败）'
          : '当前平台暂不支持保持系统唤醒',
      });
    }
    return resolved;
  },

  reset: () => {
    set({ tasks: [], runs: [], keepAwake: null, tasksLoading: false, runsLoading: false, error: null });
  },
}));