//! 安全与治理（PRD §4.7 安全基线 P0）
//!
//! command：
//! - security_scan_data:          数据分级检测（PII/证件/商业机密/内部标记）+ 留痕
//! - security_get_domain_control: 出域控制配置读取（含 persisted 标记）
//! - security_set_domain_control: 出域控制配置保存（校验 + 归一化）
//! - compliance_status:           合规状态聚合（硬开关 / 模型可达名单 / 最近分级检测）
//! - compliance_update:           合规策略更新（与出域控制同一份存储）
//! - compliance_check:            提交前合规闸门（正文分级 + 端点出域校验，拒绝即阻断提交）
//! - sandbox_policy_get:          工作区沙箱策略读取（未配置返回 null）
//! - sandbox_policy_set:          工作区沙箱策略保存（边界校验）
//! - sandbox_env_usage:           工作环境文件磁盘用量（实测 workspaces/*/environment）
//! - sandbox_env_clear:           清理工作环境文件（只删 environment 内容，保留记录与产物）
//! - system_permission_status:    系统权限清单（当前平台可达的设置面板 + 可探测项标记）
//! - system_permission_open:      打开对应系统设置面板（平台无此开关时如实报错）
//! - audit_log_query:             审计日志查询（动作/严重度/时间窗过滤）
//! - audit_log_export:            审计日志导出（CSV/JSON 落运行目录 exports/）
//!
//! 出域开关的真实约束点在 `policy::ensure_domain_allowed`，模型连通性测试等
//! 出站调用前调用；本模块只负责策略的存取与校验。

pub mod audit;
pub mod classify;
pub mod compliance;
pub mod environment;
pub mod permissions;
pub mod pii;
pub mod policy;
pub mod skill_scan;
pub mod tool_approval;

use std::sync::Arc;

use serde_json::{json, Value};
use tauri::State;

use crate::bridge::python_env;
use crate::P0Services;

/// 分级检测：纯计算，但结果按 `data_check` 留痕（合规要求数据流向可追溯）
#[tauri::command]
pub async fn security_scan_data(
    p0: State<'_, Arc<P0Services>>,
    text: String,
) -> Result<Value, String> {
    let view = scan_view(&text);
    let sensitivity = view["sensitivity"].as_str().unwrap_or("public").to_string();
    let summary = view["summary"].as_str().unwrap_or_default().to_string();
    audit::record_quiet(
        &p0.store,
        audit::ACTION_DATA_CHECK,
        if sensitivity == "confidential" {
            audit::SEV_WARNING
        } else {
            audit::SEV_INFO
        },
        &sensitivity,
        &summary,
    );
    Ok(view)
}

/// 出域控制读取：未配置过时返回语义默认值并标记 `persisted: false`
#[tauri::command]
pub async fn security_get_domain_control(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let stored = policy::get_domain_control(&p0.store)?;
    Ok(domain_control_view(stored))
}

/// 出域控制保存：返回实际落库的配置（含归一化后的域名清单）
#[tauri::command]
pub async fn security_set_domain_control(
    p0: State<'_, Arc<P0Services>>,
    config: policy::DomainControl,
) -> Result<Value, String> {
    let stored = policy::set_domain_control(&p0.store, &config)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        "domain_control",
        &format!(
            "出域控制更新：enabled={} mode={} 白名单 {} 项 黑名单 {} 项 硬开关={}",
            stored.enabled,
            stored.mode,
            stored.allowed_domains.len(),
            stored.blocked_domains.len(),
            stored.hard_disable_external_api
        ),
    );
    Ok(json!({ "config": stored, "persisted": true }))
}

/// 合规状态聚合：硬开关 + 出域控制摘要 + 模型可达名单 + 最近一次分级检测
#[tauri::command]
pub async fn compliance_status(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    compliance::status(&p0.store)
}

/// 合规更新：与出域控制同一份存储，返回更新后的状态与是否发生写入
#[tauri::command]
pub async fn compliance_update(
    p0: State<'_, Arc<P0Services>>,
    patch: compliance::CompliancePatch,
) -> Result<Value, String> {
    let (status, changed) = compliance::update(&p0.store, &patch)?;
    Ok(json!({ "status": status, "changed": changed }))
}

/// 提交前合规闸门：正文分级 + 端点出域校验；被拒时返回带错误码的 Err
#[tauri::command]
pub async fn compliance_check(
    p0: State<'_, Arc<P0Services>>,
    text: String,
    base_url: Option<String>,
) -> Result<Value, String> {
    compliance::check(&p0.store, &text, base_url.as_deref())
}

/// 沙箱策略读取：未配置返回 null（由前端展示默认表单值，不伪造已保存数据）
#[tauri::command]
pub async fn sandbox_policy_get(
    p0: State<'_, Arc<P0Services>>,
    workspace_id: String,
) -> Result<Value, String> {
    let stored = policy::get_sandbox(&p0.store, &workspace_id)?;
    Ok(match stored {
        Some(p) => serde_json::to_value(p).unwrap_or(Value::Null),
        None => Value::Null,
    })
}

/// 沙箱策略保存
#[tauri::command]
pub async fn sandbox_policy_set(
    p0: State<'_, Arc<P0Services>>,
    policy: policy::SandboxPolicy,
) -> Result<Value, String> {
    let stored = policy::set_sandbox(&p0.store, &policy)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        &format!("sandbox:{}", stored.workspace_id),
        &format!(
            "沙箱策略更新：级别={} CPU≤{}% 内存≤{}MB 写盘≤{}MB/s",
            stored.execution_level,
            stored.max_cpu_percent,
            stored.max_memory_mb,
            stored.max_disk_write_mbps
        ),
    );
    Ok(serde_json::to_value(stored).unwrap_or(Value::Null))
}

/// 系统级沙箱读取：设置意图与真实生效状态分开返回（`requested` / `supported` / `effective`）
#[tauri::command]
pub async fn sandbox_system_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let config = policy::get_system_sandbox(&p0.store)?;
    Ok(system_sandbox_view(&config, crate::sandbox::status(&config)))
}

/// 系统级沙箱保存：启用前真实创建 AppContainer profile，失败就不落库
#[tauri::command]
pub async fn sandbox_system_set(
    p0: State<'_, Arc<P0Services>>,
    enabled: bool,
    network: bool,
) -> Result<Value, String> {
    if enabled {
        crate::sandbox::prepare(network)?;
    }
    let stored = policy::set_system_sandbox(
        &p0.store,
        &crate::sandbox::SystemSandboxConfig {
            enabled,
            network,
            updated_at_ms: 0,
        },
    )?;
    crate::sandbox::set_active(Some(stored.clone()).filter(|c| c.enabled));
    let status = crate::sandbox::status(&stored);
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        if stored.enabled { audit::SEV_WARNING } else { audit::SEV_INFO },
        "sandbox:system",
        &format!(
            "系统级沙箱{}：网络={} 实际生效={}（平台 {}）",
            if stored.enabled { "已启用" } else { "已关闭" },
            if stored.network { "允许" } else { "阻断" },
            if status.effective { "是" } else { "否" },
            status.platform
        ),
    );
    Ok(system_sandbox_view(&stored, status))
}

fn system_sandbox_view(
    config: &crate::sandbox::SystemSandboxConfig,
    status: crate::sandbox::SystemSandboxStatus,
) -> Value {
    json!({ "config": config, "status": status })
}

/// 工作环境文件用量：磁盘实测 `{data_home}/workspaces/*/environment`，无目录即 0
#[tauri::command]
pub async fn sandbox_env_usage() -> Result<Value, String> {
    Ok(environment::usage_in(&python_env::base_dir()))
}

/// 清理工作环境文件：只删各工作空间 `environment/` 目录内的内容，
/// 对话记录（sessions/）与工作产物（files/）一律保留，并把释放量留痕。
#[tauri::command]
pub async fn sandbox_env_clear(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let result = environment::clear_in(&python_env::base_dir())?;
    let bytes = result["freedBytes"].as_u64().unwrap_or(0);
    let files = result["freedFiles"].as_u64().unwrap_or(0);
    let failures = result["failed"]
        .as_array()
        .map(|arr| arr.len())
        .unwrap_or(0);
    let first_failure = environment::first_failure(&result);
    audit::record_quiet(
        &p0.store,
        audit::ACTION_FILE_WRITE,
        if failures > 0 {
            audit::SEV_WARNING
        } else {
            audit::SEV_INFO
        },
        "workspaces/*/environment",
        &format!(
            "清理工作环境文件：释放 {} 字节 / {} 个文件{}{}",
            bytes,
            files,
            if failures > 0 {
                format!("，{} 项未删除", failures)
            } else {
                String::new()
            },
            first_failure
                .as_deref()
                .map(|e| format!("（首个失败：{e}）"))
                .unwrap_or_default()
        ),
    );
    Ok(result)
}

/// 系统权限清单：只给「当前平台真实可达的面板」与「可由前端探测的标记」，
/// 状态本身不在这编造（通知/麦克风由渲染层向系统真实询问）
#[tauri::command]
pub async fn system_permission_status() -> Result<Value, String> {
    Ok(permissions::status(std::env::consts::OS))
}

/// 打开某项能力对应的系统设置面板
#[tauri::command]
pub async fn system_permission_open(
    app: tauri::AppHandle,
    key: String,
) -> Result<Value, String> {
    permissions::open(&app, &key)
}

/// 审计日志查询
#[tauri::command]
pub async fn audit_log_query(
    p0: State<'_, Arc<P0Services>>,
    filters: Option<audit::AuditFilters>,
) -> Result<Value, String> {
    let f = filters.unwrap_or_default();
    let list = audit::query(&p0.store, &f)?;
    Ok(serde_json::to_value(list).unwrap_or(Value::Array(vec![])))
}

/// 审计日志导出（返回落盘路径与条数；仅新建文件，不触碰存量文件）
#[tauri::command]
pub async fn audit_log_export(
    p0: State<'_, Arc<P0Services>>,
    format: String,
    filters: Option<audit::AuditFilters>,
) -> Result<Value, String> {
    let f = filters.unwrap_or_default();
    let (path, count) = audit::export(&p0.store, &format, &f)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_FILE_WRITE,
        audit::SEV_INFO,
        &path,
        &format!("审计日志导出：{format} 共 {count} 条"),
    );
    Ok(json!({ "path": path, "count": count, "format": format }))
}

// ── 响应组装（纯函数，便于单测） ────────────────────────────────

fn scan_view(text: &str) -> Value {
    serde_json::to_value(classify::classify(text)).unwrap_or(Value::Null)
}

fn domain_control_view(stored: Option<policy::DomainControl>) -> Value {
    match stored {
        Some(cfg) => json!({ "config": cfg, "persisted": true }),
        None => json!({ "config": policy::default_domain_control(), "persisted": false }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_scan_view_shape() {
        let v = scan_view("客户身份证 11010119900307123X");
        assert_eq!(v["sensitivity"], "confidential");
        assert_eq!(v["pii"], true);
        assert_eq!(v["force_local"], true);
        assert!(v["findings"].is_array());
        assert!(v["summary"].as_str().unwrap().contains("本地模型"));
    }

    #[test]
    fn test_domain_control_view_default_is_flagged_not_persisted() {
        let v = domain_control_view(None);
        assert_eq!(v["persisted"], false);
        assert_eq!(v["config"]["mode"], "whitelist");
        assert_eq!(v["config"]["enabled"], false);
        assert_eq!(v["config"]["forceLocalForConfidential"], true);
    }

    #[test]
    fn test_domain_control_view_persisted_passthrough() {
        let mut cfg = policy::default_domain_control();
        cfg.enabled = true;
        cfg.allowed_domains = vec!["api.deepseek.com".into()];
        let v = domain_control_view(Some(cfg));
        assert_eq!(v["persisted"], true);
        assert_eq!(v["config"]["allowedDomains"][0], "api.deepseek.com");
    }
}