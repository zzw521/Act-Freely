// ============================================================
// src/shared/contracts_connect.ts — connect 车道的通道预留段
// ============================================================
// 归属：W5（#147 D-39 MCP 远程连接器 / #146 D-41 IM 媒体与群策略 / #139 D-30 定时 / #145 D-36 浏览器）。
// 口径：`connect:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。
// 既有 `mcp:*`、`im:*`、`schedule:*` 通道仍在 contracts.ts 里（被各自契约钉住），不动。

import type {
  ScheduleCreateReq,
  ScheduleFreq,
  ScheduleParseResult,
  ScheduleUpdateReq,
  ScheduledTask,
} from './contracts';

export const IPC_LANE_CONNECT = {
  channel: 'lane:connect',
  command: 'lane_connect',
} as const;

export const IPC_CHANNELS_CONNECT = {} as const satisfies Record<string, string>;

// ============================================================
// D-30 定时任务：解析/存储扩展字段（W5 #139）
// ============================================================
// `contracts.ts` 归 W4 独占且被契约测试钉住，本轮只往本文件加类型，不动既有枚举。
// Rust 侧 `task::schedule` 早已按 camelCase 直接序列化这些字段（serde 宽容，缺省即 undefined），
// 因此这里是「给已存在的后端事实补前端类型」，不是新造口径。
// 待 W4 把 `ScheduleFreq` 放宽到含 interval/once 后，本段可回收进 contracts.ts。

/** 间隔（每 N 分钟/小时/天）与一次性（绝对时刻执行一次）两种新增频率 */
export type ScheduleFreqExtended = ScheduleFreq | 'interval' | 'once';

/** 覆盖 freq 用的写法：基类型窄于扩展值，直接 extends 会报赋值不兼容 */
export interface ScheduledTaskExtended extends Omit<ScheduledTask, 'freq'> {
  freq: ScheduleFreqExtended;
  /** 间隔步长（分钟），仅 freq=interval 有值 */
  intervalMinutes?: number | null;
  /** 一次性任务的绝对执行时刻（UTC 毫秒），仅 freq=once 有值 */
  runAtMs?: number | null;
}

export interface ScheduleParseResultExtended extends Omit<ScheduleParseResult, 'freq'> {
  freq: ScheduleFreqExtended;
  /** 间隔步长（分钟），仅 freq=interval */
  intervalMinutes?: number | null;
  /** 一次性任务的本地日期（yyyy-mm-dd，未做时区换算）；仅 freq=once */
  runDate?: string | null;
  /** runDate 来自「明天/下周三」这类相对说法：界面需按本地日重锚 */
  runDateRelative?: boolean;
  /** 中文回显，解析结果先预览再确认用 */
  preview?: string;
}

export interface ScheduleCreateReqExtended extends ScheduleCreateReq {
  intervalMinutes?: number | null;
  runAtMs?: number | null;
}

export interface ScheduleUpdateReqExtended extends ScheduleUpdateReq {
  intervalMinutes?: number | null;
  runAtMs?: number | null;
}
