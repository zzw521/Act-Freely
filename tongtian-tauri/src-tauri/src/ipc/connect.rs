//! connect 车道（W5）：#147 D-39 MCP 连接器 / #146 D-41 IM 媒体与群策略 / #139 D-30 定时 / #145 D-36 浏览器。
//! 只有 W5 改这份。新 method 与 `src/shared/contracts_connect.ts` 里的通道名一一对应。
//! 连接器长连接的约束（每条连接独占一个 slot 任务、有界等待只用 asyncio.timeout）在引擎侧，
//! 这一层的分派不许自己起线程或持有跨轮状态。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "connect";

#[tauri::command]
pub async fn lane_connect(method: String, payload: Option<Value>) -> Result<Value, String> {
    dispatch(&method, payload)
}

pub fn dispatch(method: &str, _payload: Option<Value>) -> Result<Value, String> {
    match method {
        _ => Err(format!("{NOT_IMPLEMENTED}:{LANE}::{method}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_unimplemented_method_reports_honestly() {
        let err = dispatch("anything", None).unwrap_err();
        assert!(err.starts_with(NOT_IMPLEMENTED), "{err}");
        assert!(err.contains(":connect::"), "{err}");
    }
}
