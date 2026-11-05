// ============================================================
// src/shared/contracts_workspace.ts — workspace 车道的通道预留段
// ============================================================
// 归属：W3（#157 D-29 工作区后端 / #140 D-13 应用快照与上下文接力 / #151 D-32 设计令牌）。
// 口径：`workspace:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。
// 渲染层新建组件/样式归 W3 自己，这里只放 IPC 通道常量。

export const IPC_LANE_WORKSPACE = {
  channel: 'lane:workspace',
  command: 'lane_workspace',
} as const;

export const IPC_CHANNELS_WORKSPACE = {} as const satisfies Record<string, string>;
