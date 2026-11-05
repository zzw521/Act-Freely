//! workspace 车道（W3）：#157 D-29 工作区后端 / #140 D-13 快照接力 / #151 D-32 设计令牌。
//! 只有 W3 改这份。新 method 与 `src/shared/contracts_workspace.ts` 里的通道名一一对应。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "workspace";

#[tauri::command]
pub async fn lane_workspace(method: String, payload: Option<Value>) -> Result<Value, String> {
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
        assert!(err.contains(":workspace::"), "{err}");
    }
}
