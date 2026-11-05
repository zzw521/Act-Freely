//! artifact 车道（W4）：#144 产物与发布 / #159 更新清单校验与失败回滚。
//! 只有 W4 改这份。新 method 与 `src/shared/contracts_artifact.ts` 里的通道名一一对应。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "artifact";

#[tauri::command]
pub async fn lane_artifact(method: String, payload: Option<Value>) -> Result<Value, String> {
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
        assert!(err.contains(":artifact::"), "{err}");
    }
}
