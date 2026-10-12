//! 千问对标整改通道后端落地（spec §6.3：hooks / connector / archive / computer-use）
//!
//! command：
//! - hooks_get / hooks_set / hooks_test:            Hook 配置读写 + 真实试运行（拉起进程、stdin 喂事件 JSON、按秒超时）
//! - connector_list / authorize / revoke:           连接器授权状态持久化（installed + authStatus 落库）
//! - task_archive / list / unarchive / delete / clear: 任务归档标记（取消归档与清空均不动任务本体，除点名的彻底删除）
//! - computer_use_get_policy / computer_use_set_policy: 电脑操控策略（与设置页共用 settings_kv 同一键，避免双写源）
//!
//! 整改基准是 Electron 版同名 handler；本模块把同一语义搬到 SQLite + Rust 进程执行，
//! 使 Tauri 包里这些页面不再拿到空响应而静默失效。

pub mod archive;
pub mod connector;
pub mod hooks;

use std::sync::Arc;

use serde_json::{json, Value};
use tauri::State;

use crate::now_ms_u64;
use crate::security::audit;
use crate::store;
use crate::P0Services;

/// 电脑操控策略持久化键（与设置页 AppSettingsData.computerUsePolicy 同键同义）
pub const COMPUTER_USE_KEY: &str = "computerUsePolicy";
pub const COMPUTER_USE_POLICIES: &[&str] = &["ask_every_time", "auto", "disabled"];
pub const COMPUTER_USE_DEFAULT: &str = "ask_every_time";

// ── Hooks ───────────────────────────────────────────────────

/// Hook 配置读取（未保存过时返回空 hooks，由页面展示默认表单）
#[tauri::command]
pub async fn hooks_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    hooks::get(&p0.store).map_err(|e| format!("HOOK_QUERY_FAILED: {e}"))
}

/// Hook 配置保存：先校验（非法事件/空命令/越界超时即拒），通过才落库
#[tauri::command]
pub async fn hooks_set(
    p0: State<'_, Arc<P0Services>>,
    config: Value,
) -> Result<Value, String> {
    hooks::validate_config(&config)?;
    hooks::set(&p0.store, &config, now_ms_u64() as i64)?;
    let events = hooks::count_events(&config);
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        "hooks",
        &format!("Hook 配置保存：{events} 个事件挂载"),
    );
    Ok(json!({ "ok": true, "events": events }))
}

/// Hook 试运行：真实执行命令并回传 exitCode/stdout/stderr/超时标记
#[tauri::command]
pub async fn hooks_test(
    p0: State<'_, Arc<P0Services>>,
    event: String,
    command: String,
    timeout: Option<u64>,
) -> Result<Value, String> {
    hooks::ensure_event(&event)?;
    hooks::ensure_command(&command)?;
    let secs = hooks::clamp_timeout(timeout);
    let cwd = store::db::runtime_home().to_string_lossy().to_string();
    let input = hooks::test_input(&event, &cwd);
    let cmd = command.clone();
    let evt = event.clone();
    let run = tauri::async_runtime::spawn_blocking(move || hooks::run_command(&evt, &cmd, secs, &input))
        .await
        .map_err(|e| format!("HOOK_TEST_FAILED: 执行线程异常：{e}"))?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        if run.timed_out || run.exit_code != 0 {
            audit::SEV_WARNING
        } else {
            audit::SEV_INFO
        },
        "hooks",
        &format!(
            "Hook 试运行：event={event} exit={} timedOut={} blocked={}",
            run.exit_code, run.timed_out, run.blocked
        ),
    );
    Ok(run.to_value())
}

// ── 连接器授权 ──────────────────────────────────────────────

/// 连接器授权状态列表（无记录时为空数组，不伪造已授权项）
#[tauri::command]
pub async fn connector_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let rows = connector::list(&p0.store)?;
    Ok(serde_json::to_value(rows).unwrap_or_else(|_| Value::Array(vec![])))
}

/// 授权（安装）连接器
#[tauri::command]
pub async fn connector_authorize(
    p0: State<'_, Arc<P0Services>>,
    connector_id: String,
    category: Option<String>,
    server_ref: Option<String>,
) -> Result<Value, String> {
    let view = connector::authorize(
        &p0.store,
        &connector_id,
        category.as_deref(),
        server_ref.as_deref(),
        now_ms_u64() as i64,
    )?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        &view.connector_id,
        &connector::audit_summary(&view),
    );
    Ok(connector::ok_value(&view))
}

/// 撤销连接器授权（无授权记录时明确报错，不静默成功）
#[tauri::command]
pub async fn connector_revoke(
    p0: State<'_, Arc<P0Services>>,
    connector_id: String,
) -> Result<Value, String> {
    let id = connector_id.trim().to_string();
    connector::revoke(&p0.store, &connector_id, now_ms_u64() as i64)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_WARNING,
        &id,
        &connector::revoke_summary(&id),
    );
    Ok(json!({ "ok": true, "connectorId": id }))
}

// ── 任务归档 ────────────────────────────────────────────────

/// 归档任务（写入归档标记；任务本体保持不动）
#[tauri::command]
pub async fn task_archive(
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
    workspace_id: Option<String>,
) -> Result<Value, String> {
    let view = archive::archive(
        &p0.store,
        &task_id,
        workspace_id.as_deref(),
        now_ms_u64() as i64,
    )?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_FILE_WRITE,
        audit::SEV_INFO,
        &view.task_id,
        &format!("任务归档：{}（{task_id}）", view.title),
    );
    Ok(archive::to_value(&view))
}

/// 归档清单
#[tauri::command]
pub async fn task_archive_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let rows = archive::list(&p0.store)?;
    Ok(serde_json::to_value(rows).unwrap_or_else(|_| Value::Array(vec![])))
}

/// 取消归档（只移除标记）
#[tauri::command]
pub async fn task_unarchive(
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
) -> Result<Value, String> {
    let removed = archive::unarchive(&p0.store, &task_id)?;
    if removed {
        audit::record_quiet(
            &p0.store,
            audit::ACTION_FILE_WRITE,
            audit::SEV_INFO,
            &task_id,
            "取消任务归档",
        );
    }
    Ok(json!({ "ok": true, "removed": removed }))
}

/// 彻底删除归档任务（仅删除用户点名的这一条：标记 + 任务本体行）
#[tauri::command]
pub async fn task_archive_delete(
    p0: State<'_, Arc<P0Services>>,
    task_id: String,
) -> Result<Value, String> {
    let title = archive::task_title(&p0.store, &task_id).unwrap_or_default();
    let removed = archive::delete_archived(&p0.store, &task_id)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_FILE_WRITE,
        audit::SEV_WARNING,
        &task_id,
        &format!("彻底删除归档任务：{title}（{task_id}）"),
    );
    Ok(json!({ "ok": true, "removed": removed }))
}

/// 清空归档清单（确认词 DELETE；任务本体保留）
#[tauri::command]
pub async fn task_archive_clear(
    p0: State<'_, Arc<P0Services>>,
    confirm: String,
) -> Result<Value, String> {
    let cleared = archive::clear_all(&p0.store, &confirm)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_FILE_WRITE,
        audit::SEV_WARNING,
        "archive",
        &format!("清空归档清单：移除 {cleared} 条归档标记（任务本体保留）"),
    );
    Ok(json!({ "ok": true, "cleared": cleared }))
}

// ── 电脑操控策略 ────────────────────────────────────────────

/// 读取电脑操控策略（未设置过时返回默认档，并标记 persisted=false）
#[tauri::command]
pub async fn computer_use_get_policy(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let stored = store::settings::get(&p0.store, COMPUTER_USE_KEY)?;
    let persisted = stored.is_some();
    let policy = stored
        .map(|v| v.trim().trim_matches('"').to_string())
        .filter(|v| COMPUTER_USE_POLICIES.contains(&v.as_str()))
        .unwrap_or_else(|| COMPUTER_USE_DEFAULT.to_string());
    Ok(json!({ "policy": policy, "persisted": persisted }))
}

/// 保存电脑操控策略（档位非法即拒）
#[tauri::command]
pub async fn computer_use_set_policy(
    p0: State<'_, Arc<P0Services>>,
    policy: String,
) -> Result<Value, String> {
    let v = policy.trim().trim_matches('"');
    if !COMPUTER_USE_POLICIES.contains(&v) {
        return Err(format!(
            "COMPUTER_USE_INVALID_POLICY: 电脑操控档位非法「{v}」，仅支持 {}",
            COMPUTER_USE_POLICIES.join(" / ")
        ));
    }
    store::settings::set(&p0.store, COMPUTER_USE_KEY, v)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        COMPUTER_USE_KEY,
        &format!("电脑操控策略更新：{v}"),
    );
    Ok(json!({ "ok": true, "policy": v }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_computer_use_policy_set_is_enumerated() {
        // 档位清单必须与 contracts.ts 的 ComputerUsePolicy 一致
        assert_eq!(COMPUTER_USE_POLICIES, ["ask_every_time", "auto", "disabled"]);
        assert!(COMPUTER_USE_POLICIES.contains(&COMPUTER_USE_DEFAULT));
    }

    #[test]
    fn test_computer_use_key_matches_settings_page_field() {
        // 与渲染层 AppSettingsData 的字段名保持同键（双写源会互相覆盖）
        assert_eq!(COMPUTER_USE_KEY, "computerUsePolicy");
    }

    #[test]
    fn test_hooks_command_surface_is_complete() {
        assert_eq!(hooks::ALL_EVENTS.len(), 12);
        assert_eq!(hooks::BLOCKABLE_EVENTS.len(), 3);
    }
}
