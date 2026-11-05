//! exec 车道（W4）：#165 D-14.5 / #150 D-48 prepare_fn 与按剩余窗口取页 / 沙箱与写暂存。
//! 只有 W4 改这份。新 method 与 `src/shared/contracts_exec.ts` 里的通道名一一对应。

use serde_json::Value;

use super::NOT_IMPLEMENTED;

pub const LANE: &str = "exec";

#[tauri::command]
pub async fn lane_exec(method: String, payload: Option<Value>) -> Result<Value, String> {
    dispatch(&method, payload).await
}

/// 车道分派：`update_*` 归 `crate::app_update`（D-12 更新回路，#159）。
///
/// 这里是 `async` 是有意的：更新要取清单、要流式下载，而 `reqwest` 在本仓库只开了
/// 异步面（没有 `blocking` feature，共享的依赖清单不为一个模块加特性）。
/// 认不得的方法继续按 `NOT_IMPLEMENTED` 如实报账，不返回「看着像成功」的空对象。
pub async fn dispatch(method: &str, payload: Option<Value>) -> Result<Value, String> {
    if let Some(result) = crate::app_update::dispatch(method, payload.as_ref()).await {
        return result;
    }
    Err(format!("{NOT_IMPLEMENTED}:{LANE}::{method}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_unimplemented_method_reports_honestly() {
        let err = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(dispatch("anything", None))
            .unwrap_err();
        assert!(err.starts_with(NOT_IMPLEMENTED), "{err}");
        assert!(err.contains(":exec::"), "{err}");
    }
}
