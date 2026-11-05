//! #164 IPC 车道：七条窗口各占一份，`lib.rs` 的注册面只在这里出现一次。
//!
//! 为什么不是「一条命令 = 一个 `#[tauri::command]`」：`generate_handler!` 是一张字面量清单，
//! 七个窗口同时往它插行，没有 git 的仓库里就是后写的覆盖先写的（还覆盖得无声无息）。
//! 所以每条车道预挂**一条分派命令**，方法表放在各自的车道文件里，谁的车道谁改。
//!
//! 口径：
//! - 车道名与 `engine/routes_<lane>.py`、`src/shared/contracts_<lane>.ts` 三处同名；
//! - 通道常量写进 `contracts_<lane>.ts`，`src/shared/contracts.ts` 只有一行 re-export；
//! - 前端经 `src/shared/ipc-lanes.ts` 的单一入口调用，不再往 `tauri-api.ts` 逐条加 `if`；
//! - 未实现的 method 一律如实返回 `IPC_LANE_NOT_IMPLEMENTED`，不返回「看着像成功」的空对象。

pub mod artifact;
pub mod connect;
pub mod exec;
pub mod knowledge;
pub mod orchestrate;
pub mod voice;
pub mod workspace;

/// 车道未实现方法时的统一错误码（不进 `ErrorCode` 枚举：那个枚举有渲染层文案映射契约，
/// 加一项就得同步改文案表，车道不该拿它当逃生口）。
pub const NOT_IMPLEMENTED: &str = "IPC_LANE_NOT_IMPLEMENTED";
