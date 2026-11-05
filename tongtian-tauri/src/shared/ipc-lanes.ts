// ============================================================
// src/shared/ipc-lanes.ts — #164 IPC 车道的前端单点入口（W4 领地）
// ============================================================
// 有了它，各窗口加一条 IPC 只需要动两处自己的文件：
//   src/shared/contracts_<lane>.ts  加通道常量
//   src-tauri/src/ipc/<lane>.rs     加一条 match 分支
// lib.rs 与 tauri-api.ts 都不必再打开（本仓库没有 git，两个窗口改同一行就是静默覆盖）。
//
// 规则：`<lane>:xxx` 的冒号后半段就是 Rust 侧 dispatch 的 method 名，两边不许改名错开；
// 车道未实现的方法如实返回 IPC_LANE_NOT_IMPLEMENTED，调用方按失败处理。

import { IPC_LANE_ARTIFACT, IPC_CHANNELS_ARTIFACT } from './contracts_artifact';
import { IPC_LANE_CONNECT, IPC_CHANNELS_CONNECT } from './contracts_connect';
import { IPC_LANE_EXEC, IPC_CHANNELS_EXEC } from './contracts_exec';
import { IPC_LANE_KNOWLEDGE, IPC_CHANNELS_KNOWLEDGE } from './contracts_knowledge';
import { IPC_LANE_ORCHESTRATE, IPC_CHANNELS_ORCHESTRATE } from './contracts_orchestrate';
import { IPC_LANE_VOICE, IPC_CHANNELS_VOICE } from './contracts_voice';
import { IPC_LANE_WORKSPACE, IPC_CHANNELS_WORKSPACE } from './contracts_workspace';

export interface IpcLane {
  /** 车道名，与 engine/routes_<lane>.py、src-tauri/src/ipc/<lane>.rs 同名 */
  lane: string;
  /** 分派命令名（lib.rs 的 generate_handler! 里预挂的那条） */
  command: string;
  /** 本车道已登记的通道（值形如 `<lane>:<method>`） */
  channels: Readonly<Record<string, string>>;
}

export const IPC_LANES: readonly IpcLane[] = [
  { lane: 'knowledge', command: IPC_LANE_KNOWLEDGE.command, channels: IPC_CHANNELS_KNOWLEDGE },
  { lane: 'artifact', command: IPC_LANE_ARTIFACT.command, channels: IPC_CHANNELS_ARTIFACT },
  { lane: 'workspace', command: IPC_LANE_WORKSPACE.command, channels: IPC_CHANNELS_WORKSPACE },
  { lane: 'exec', command: IPC_LANE_EXEC.command, channels: IPC_CHANNELS_EXEC },
  { lane: 'connect', command: IPC_LANE_CONNECT.command, channels: IPC_CHANNELS_CONNECT },
  {
    lane: 'orchestrate',
    command: IPC_LANE_ORCHESTRATE.command,
    channels: IPC_CHANNELS_ORCHESTRATE,
  },
  { lane: 'voice', command: IPC_LANE_VOICE.command, channels: IPC_CHANNELS_VOICE },
];

/** 通道 → 分派命令与 method；不是车道通道时返回 null，交给调用方按原路径处理。 */
export function laneDispatchOf(
  channel: string,
): { command: string; method: string; lane: string } | null {
  for (const lane of IPC_LANES) {
    for (const value of Object.values(lane.channels) as string[]) {
      if (value !== channel) continue;
      const method = channel.slice(channel.indexOf(':') + 1);
      if (!method || method === channel) return null;
      return { command: lane.command, method, lane: lane.lane };
    }
  }
  return null;
}

/** 某条车道的通道是否已登记（渲染层用它区分「没接」和「接了但 Rust 侧未实现」）。 */
export function isLaneChannel(channel: string): boolean {
  return laneDispatchOf(channel) !== null;
}
