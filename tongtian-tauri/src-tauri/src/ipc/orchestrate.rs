//! orchestrate 车道（W6）：#148 D-34 团队协作 / #149 D-40.5 成员检查点 / #156 D-10 可视化工作流。
//! 只有 W6 改这份。新 method 与 `src/shared/contracts_orchestrate.ts` 里的通道名一一对应。
//! 派工与投递复用 D-47/D-48 已有的耐久收件箱与副作用队列语义，不许在这一层新造第二套保序逻辑。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "orchestrate";

#[tauri::command]
pub async fn lane_orchestrate(method: String, payload: Option<Value>) -> Result<Value, String> {
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
        assert!(err.contains(":orchestrate::"), "{err}");
    }
}
