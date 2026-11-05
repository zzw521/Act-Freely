// ============================================================
// src/shared/contracts_knowledge.ts — knowledge 车道的通道预留段
// ============================================================
// 归属：W2（#137 D-09 知识库混合检索 / #143 D-38 技能统一注册表 / #138 D-26 产物版本历史）。
// 为什么要有这个文件：contracts.ts 与 src-tauri/src/lib.rs 一样是所有窗口的公共咽喉，
// 七个窗口往里加常量必然互相覆盖（本仓库没有 git）。#164 之后各窗口的 IPC 通道写自己这份，
// contracts.ts 里只有一行 re-export，lib.rs 里只有预挂的那条分派命令。
//
// 口径（三条都要守住，tests/unit/shared/ipc-lanes-contract.test.ts 会锁）：
//   1. 通道名形如 `knowledge:xxx`，冒号后半段就是 Rust 侧 dispatch 的 method 名，两边不许改名错开；
//   2. 只有本车道能改这份文件；新前缀（不是 `knowledge:`）要提分片给 W4；
//   3. 引擎端点写 engine/routes_knowledge.py，工具写 engine/toolkit_knowledge.py，别混进这里。

export const IPC_LANE_KNOWLEDGE = {
  channel: 'lane:knowledge',
  command: 'lane_knowledge',
} as const;

/** 本车道的通道常量。留空即「这块地盘有主，但还没人接线」，不是「谁都可以写」。 */
export const IPC_CHANNELS_KNOWLEDGE = {} as const satisfies Record<string, string>;
