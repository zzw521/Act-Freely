// ============================================================
// src/shared/contracts_artifact.ts — artifact 车道的通道预留段
// ============================================================
// 归属：W4（#144 产物与发布 / #159 更新清单格式与签名校验、下载—校验—暂存—替换的失败回滚）。
// 口径与 knowledge 车道一致：`artifact:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。
// D-07 已关闭，「一键升级」那半边不在这里重做。

export const IPC_LANE_ARTIFACT = {
  channel: 'lane:artifact',
  command: 'lane_artifact',
} as const;

export const IPC_CHANNELS_ARTIFACT = {} as const satisfies Record<string, string>;
