//! knowledge 车道（W2）：#137 D-09 混合检索 / #143 D-38 技能注册表 / #138 D-26 产物版本历史。
//! 只有 W2 改这份。新 method 与 `src/shared/contracts_knowledge.ts` 里的通道名一一对应。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "knowledge";

#[tauri::command]
pub async fn lane_knowledge(method: String, payload: Option<Value>) -> Result<Value, String> {
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
        assert!(err.contains(":knowledge::"), "{err}");
    }
}
