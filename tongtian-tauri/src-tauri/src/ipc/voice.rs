//! voice 车道（W7）：#160 D-11 语音链路（ASR / TTS / 实时）。
//! 只有 W7 改这份。新 method 与 `src/shared/contracts_voice.ts` 里的通道名一一对应。
//! 分工文档按六车道拆，语音单列为第七条：W7 挤进 connect 就会和 W5 抢同一个文件。
//! 真机麦克风与厂商端到端在验收前一律标未验收，假上游契约测试不算实机绿。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "voice";

#[tauri::command]
pub async fn lane_voice(method: String, payload: Option<Value>) -> Result<Value, String> {
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
        assert!(err.contains(":voice::"), "{err}");
    }
}
