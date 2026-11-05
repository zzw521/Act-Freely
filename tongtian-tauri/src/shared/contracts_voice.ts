// ============================================================
// src/shared/contracts_voice.ts — voice 车道的通道预留段
// ============================================================
// 归属：W7（#160 D-11 语音链路：ASR / TTS / 实时）。
// 口径：`voice:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。
// 分工文档按六车道拆，语音单列为第七条：W7 挤进 connect 就会和 W5 抢同一个文件。
// 真机麦克风与厂商端到端在验收前一律标未验收。

export const IPC_LANE_VOICE = {
  channel: 'lane:voice',
  command: 'lane_voice',
} as const;

export const IPC_CHANNELS_VOICE = {} as const satisfies Record<string, string>;
