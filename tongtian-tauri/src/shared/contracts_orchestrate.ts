// ============================================================
// src/shared/contracts_orchestrate.ts — orchestrate 车道的通道预留段
// ============================================================
// 归属：W6（#148 D-34 团队协作 / #149 D-40.5 成员运行时检查点 / #156 D-10 可视化工作流）。
// 口径：`orchestrate:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。
// 既有 `collab:*` 通道仍在 contracts.ts 里，不动。

export const IPC_LANE_ORCHESTRATE = {
  channel: 'lane:orchestrate',
  command: 'lane_orchestrate',
} as const;

export const IPC_CHANNELS_ORCHESTRATE = {} as const satisfies Record<string, string>;
