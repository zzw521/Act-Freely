//! 合规状态聚合、硬开关与提交前闸门（compliance:status / compliance:update / compliance:check）
//!
//! 权威数据不出三处：出域控制（`policy`）、模型台账（`model::service`）、审计（`audit`）。
//! 本模块只做聚合与最小 patch，不新增任何存储：
//! - `hardSwitchEnabled` 就是 `DomainControl.hard_disable_external_api`，真实拦截点仍是
//!   `policy::ensure_domain_allowed`（出站调用前校验），这里不另建一套开关
//! - 模型可达名单逐个走同一套出域判定，避免「展示口径」与「执行口径」分叉
//! - 模型台账读不到时标记 `allowlistEvaluated=false`，不把空名单伪装成「全部被拦」

use serde::Deserialize;
use serde_json::{json, Value};

use crate::model::service as models;
use crate::store::Store;

use super::{audit, policy};

/// 合规更新入参：只改传入的字段，其余保持库中现值
#[derive(Debug, Default, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CompliancePatch {
    pub hard_switch_enabled: Option<bool>,
    pub enabled: Option<bool>,
    pub mode: Option<String>,
    pub allowed_domains: Option<Vec<String>>,
    pub blocked_domains: Option<Vec<String>>,
    pub force_local_for_confidential: Option<bool>,
}

impl CompliancePatch {
    fn is_empty(&self) -> bool {
        self.hard_switch_enabled.is_none()
            && self.enabled.is_none()
            && self.mode.is_none()
            && self.allowed_domains.is_none()
            && self.blocked_domains.is_none()
            && self.force_local_for_confidential.is_none()
    }
}

/// 合规状态：硬开关 + 出域控制摘要 + 模型可达名单 + 最近一次数据分级检测
pub fn status(store: &Store) -> Result<Value, String> {
    let stored = policy::get_domain_control(store)?;
    let cfg = stored.clone().unwrap_or_else(policy::default_domain_control);
    let (allowed, blocked, evaluated) = route_models(store, &cfg);
    Ok(json!({
        "hardSwitchEnabled": cfg.hard_disable_external_api,
        "whitelistModelIds": allowed,
        "blockedModelIds": blocked,
        "allowlistEvaluated": evaluated,
        "domainControl": {
            "enabled": cfg.enabled,
            "mode": cfg.mode,
            "allowedDomains": cfg.allowed_domains,
            "blockedDomains": cfg.blocked_domains,
            "forceLocalForConfidential": cfg.force_local_for_confidential,
            "hardDisableExternalAPI": cfg.hard_disable_external_api,
        },
        "persisted": stored.is_some(),
        "lastDataCheck": last_data_check(store)?,
    }))
}

/// 按当前出域控制评估模型可达性（forceLocal 模型恒为本地执行，不受云端策略约束）
fn route_models(store: &Store, cfg: &policy::DomainControl) -> (Vec<String>, Vec<String>, bool) {
    let Ok(list) = models::list(store) else {
        return (Vec::new(), Vec::new(), false);
    };
    let mut allowed = Vec::new();
    let mut blocked = Vec::new();
    for m in list {
        if m.force_local || policy::eval_domain_allowed(cfg, &m.base_url).is_ok() {
            allowed.push(m.id);
        } else {
            blocked.push(m.id);
        }
    }
    (allowed, blocked, true)
}

/// 最近一次数据分级检测留痕（无留痕返回 null，不编造时间戳）
fn last_data_check(store: &Store) -> Result<Option<Value>, String> {
    let rows = audit::query(
        store,
        &audit::AuditFilters {
            action: Some(audit::ACTION_DATA_CHECK.to_string()),
            severity: None,
            date_range: None,
            limit: Some(1),
        },
    )?;
    Ok(rows.first().map(|r| {
        json!({
            "sensitivity": r.resource.clone().unwrap_or_default(),
            "severity": r.severity,
            "timestamp": r.timestamp,
            "summary": r.details,
        })
    }))
}

/// 应用合规 patch：合并现值 → 校验落库 → 留痕 → 回读最新状态。
/// 返回 `(状态视图, 是否真实写入)`；空 patch 只读不写，不产生审计噪声。
pub fn update(store: &Store, patch: &CompliancePatch) -> Result<(Value, bool), String> {
    let base = policy::get_domain_control(store)?.unwrap_or_else(policy::default_domain_control);
    if patch.is_empty() {
        return Ok((status(store)?, false));
    }
    let merged = policy::DomainControl {
        enabled: patch.enabled.unwrap_or(base.enabled),
        mode: patch.mode.clone().unwrap_or(base.mode),
        allowed_domains: patch.allowed_domains.clone().unwrap_or(base.allowed_domains),
        blocked_domains: patch.blocked_domains.clone().unwrap_or(base.blocked_domains),
        force_local_for_confidential: patch
            .force_local_for_confidential
            .unwrap_or(base.force_local_for_confidential),
        hard_disable_external_api: patch
            .hard_switch_enabled
            .unwrap_or(base.hard_disable_external_api),
        updated_at_ms: base.updated_at_ms,
    };
    let stored = policy::set_domain_control(store, &merged)?;
    audit::record_quiet(
        store,
        audit::ACTION_CONFIG_CHANGE,
        if stored.hard_disable_external_api {
            audit::SEV_WARNING
        } else {
            audit::SEV_INFO
        },
        "compliance",
        &format!(
            "合规策略更新：硬开关={} 出域控制={} mode={} 白名单 {} 项 黑名单 {} 项",
            stored.hard_disable_external_api,
            stored.enabled,
            stored.mode,
            stored.allowed_domains.len(),
            stored.blocked_domains.len()
        ),
    );
    Ok((status(store)?, true))
}

/// 提交前合规闸门（compliance:check）：正文分级 + 目标端点出域校验。
///
/// 判定与出站调用点同源（复用 `policy::eval_domain_allowed` 与 `classify::classify`），
/// 保证「UI 说能不能发」和「真实调用会不会被拦」一致：
/// - 端点被出域控制/硬开关拒绝 → 透传 POLICY_* 错误码
/// - 机密/PII 内容 + 开启机密本地化 + 端点为外部 → COMPLIANCE_DATA_LEAK
/// - 端点未知（引擎内置模型，前端拿不到 baseUrl）→ 只做分级留痕，不臆断出域
pub fn check(store: &Store, text: &str, base_url: Option<&str>) -> Result<Value, String> {
    let cfg = policy::get_domain_control(store)?.unwrap_or_else(policy::default_domain_control);
    let scan = super::classify::classify(text);
    let url = base_url.map(|s| s.trim()).filter(|s| !s.is_empty());
    let host = url.map(policy::host_of).unwrap_or_default();
    let local = !host.is_empty() && policy::is_local_host(&host);

    // 端点出域校验（硬开关命中时同样在此拒绝，错误码与真实调用一致）
    if let Some(u) = url {
        if let Err(denied) = policy::eval_domain_allowed(&cfg, u) {
            record_gate(store, "blocked", &scan.sensitivity, &host, &denied);
            return Err(denied);
        }
    }

    // 机密内容禁止发往外部端点
    if scan.force_local && cfg.enabled && cfg.force_local_for_confidential && !host.is_empty() && !local
    {
        let err = format!(
            "COMPLIANCE_DATA_LEAK: 内容判定为{}，机密本地化已开启，禁止发往外部端点（{host}）",
            scan.sensitivity
        );
        record_gate(store, "blocked", &scan.sensitivity, &host, &err);
        return Err(err);
    }

    record_gate(store, "allowed", &scan.sensitivity, &host, &scan.summary);
    Ok(json!({
        "allowed": true,
        "sensitivity": scan.sensitivity,
        "forceLocal": scan.force_local,
        "hardSwitchEnabled": cfg.hard_disable_external_api,
        "endpointKnown": !host.is_empty(),
        "endpointHost": host,
        "endpointLocal": local,
    }))
}

/// 闸门留痕：允许与拒绝都记录，保证数据流向可追溯（拒绝记 warning）
fn record_gate(store: &Store, verdict: &str, sensitivity: &str, host: &str, detail: &str) {
    let summary = if host.is_empty() {
        format!("合规闸门 {verdict}：等级={sensitivity}；端点未提供")
    } else {
        format!("合规闸门 {verdict}：等级={sensitivity} 端点={host}")
    };
    audit::record_quiet(
        store,
        audit::ACTION_COMPLIANCE_CHECK,
        if verdict == "blocked" {
            audit::SEV_WARNING
        } else {
            audit::SEV_INFO
        },
        verdict,
        &format!("{summary}；{detail}"),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::service::ModelAddReq;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-compliance-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn req(name: &str, base_url: &str, force_local: bool) -> ModelAddReq {
        ModelAddReq {
            name: name.to_string(),
            source: "openai_compat".to_string(),
            base_url: base_url.to_string(),
            api_key: None,
            model_name: "m".to_string(),
            context_window: 8192,
            is_default: false,
            agent_binding: String::new(),
            force_local,
            ..Default::default()
        }
    }

    fn add(store: &Store, name: &str, base_url: &str, force_local: bool) -> String {
        models::add(store, req(name, base_url, force_local))
            .unwrap()
            .id
    }

    fn patch(raw: Value) -> Result<CompliancePatch, serde_json::Error> {
        serde_json::from_value(raw)
    }

    fn ids(v: &Value, key: &str) -> Vec<String> {
        v[key]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap().to_string())
            .collect()
    }

    fn config_changes(store: &Store) -> Vec<audit::AuditEntry> {
        audit::query(
            store,
            &audit::AuditFilters {
                action: Some(audit::ACTION_CONFIG_CHANGE.to_string()),
                ..Default::default()
            },
        )
        .unwrap()
    }

    #[test]
    fn status_without_config_is_flagged_not_persisted() {
        let (store, _dir) = temp_store("default");
        let v = status(&store).unwrap();
        assert_eq!(v["hardSwitchEnabled"], false);
        assert_eq!(v["persisted"], false);
        assert_eq!(v["domainControl"]["mode"], "whitelist");
        assert_eq!(v["lastDataCheck"], Value::Null);
    }

    #[test]
    fn empty_patch_reads_status_without_writing_or_audit() {
        let (store, _dir) = temp_store("empty-patch");
        let (v, changed) = update(&store, &CompliancePatch::default()).unwrap();
        assert!(!changed);
        assert_eq!(v["persisted"], false);
        assert!(config_changes(&store).is_empty(), "空 patch 不应产生审计留痕");
    }

    #[test]
    fn hard_switch_patch_persists_and_blocks_external_models() {
        let (store, _dir) = temp_store("hard-switch");
        let cloud = add(&store, "云端A", "https://api.openai.com/v1", false);
        let local = add(&store, "本地A", "http://127.0.0.1:11434/v1", false);
        let forced = add(&store, "强制本地", "https://api.blocked.example/v1", true);
        let before = status(&store).unwrap();
        assert_eq!(before["allowlistEvaluated"], true);
        assert_eq!(before["whitelistModelIds"].as_array().unwrap().len(), 3);

        let (v, changed) = update(&store, &patch(json!({ "hardSwitchEnabled": true })).unwrap()).unwrap();
        assert!(changed);
        assert_eq!(v["hardSwitchEnabled"], true);
        assert_eq!(v["persisted"], true);
        assert_eq!(ids(&v, "blockedModelIds"), vec![cloud.clone()]);
        let mut expect_allowed = vec![local.clone(), forced.clone()];
        expect_allowed.sort();
        let mut got_allowed = ids(&v, "whitelistModelIds");
        got_allowed.sort();
        assert_eq!(got_allowed, expect_allowed);
        // 硬开关开启记 warning，便于审计页筛出高危配置变更
        let rows = config_changes(&store);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].severity, audit::SEV_WARNING);
        assert!(rows[0].details.as_ref().unwrap().contains("硬开关=true"));
    }

    #[test]
    fn whitelist_mode_blocks_models_outside_domain_list() {
        let (store, _dir) = temp_store("whitelist");
        let a = add(&store, "云端A", "https://api.openai.com/v1", false);
        let b = add(&store, "云端B", "https://api.deepseek.com/v1", false);
        let (v, _) = update(
            &store,
            &patch(json!({
                "enabled": true,
                "mode": "whitelist",
                "allowedDomains": ["https://api.deepseek.com/v1"]
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(ids(&v, "whitelistModelIds"), vec![b]);
        assert_eq!(ids(&v, "blockedModelIds"), vec![a]);
        // 域名按主机名归一化后入库（展示口径与执行口径一致）
        assert_eq!(v["domainControl"]["allowedDomains"][0], "api.deepseek.com");
    }

    #[test]
    fn patch_keeps_unspecified_fields() {
        let (store, _dir) = temp_store("merge");
        update(
            &store,
            &patch(json!({ "enabled": true, "mode": "blacklist", "blockedDomains": ["evil.example"] }))
                .unwrap(),
        )
        .unwrap();
        let (v, _) = update(&store, &patch(json!({ "hardSwitchEnabled": true })).unwrap()).unwrap();
        assert_eq!(v["domainControl"]["mode"], "blacklist");
        assert_eq!(v["domainControl"]["blockedDomains"][0], "evil.example");
        assert_eq!(v["domainControl"]["enabled"], true);
        assert_eq!(v["hardSwitchEnabled"], true);
    }

    #[test]
    fn invalid_mode_is_rejected_by_policy_validation() {
        let (store, _dir) = temp_store("bad-mode");
        let err = update(&store, &patch(json!({ "mode": "greylist" })).unwrap()).unwrap_err();
        assert!(err.starts_with("POLICY_INVALID_MODE"), "{err}");
        assert_eq!(status(&store).unwrap()["persisted"], false);
    }

    #[test]
    fn unknown_patch_field_is_rejected_not_silently_dropped() {
        let (store, _dir) = temp_store("unknown-field");
        let bad: Result<CompliancePatch, _> = serde_json::from_value(json!({ "hardSwitch": true }));
        assert!(bad.is_err());
        let (v, changed) = update(&store, &CompliancePatch::default()).unwrap();
        assert!(!changed);
        assert_eq!(v["hardSwitchEnabled"], false);
    }

    #[test]
    fn last_data_check_follows_latest_scan_trace() {
        let (store, _dir) = temp_store("data-check");
        assert_eq!(status(&store).unwrap()["lastDataCheck"], Value::Null);
        audit::record(&store, audit::ACTION_DATA_CHECK, audit::SEV_INFO, "public", "未发现敏感内容").unwrap();
        audit::record(
            &store,
            audit::ACTION_DATA_CHECK,
            audit::SEV_WARNING,
            "confidential",
            "命中身份证号",
        )
        .unwrap();
        let v = status(&store).unwrap();
        assert_eq!(v["lastDataCheck"]["sensitivity"], "confidential");
        assert_eq!(v["lastDataCheck"]["severity"], "warning");
    }

    #[test]
    fn empty_model_ledger_is_evaluated_not_unevaluated() {
        let (store, _dir) = temp_store("no-models");
        let v = status(&store).unwrap();
        // 台账可读但为空：名单为空且 evaluated=true，与「读不到台账」区分开
        assert_eq!(v["allowlistEvaluated"], true);
        assert!(v["whitelistModelIds"].as_array().unwrap().is_empty());
    }

    fn gate_traces(store: &Store) -> Vec<audit::AuditEntry> {
        audit::query(
            store,
            &audit::AuditFilters {
                action: Some(audit::ACTION_COMPLIANCE_CHECK.to_string()),
                ..Default::default()
            },
        )
        .unwrap()
    }

    #[test]
    fn check_allows_public_text_and_records_trace() {
        let (store, _dir) = temp_store("check-public");
        let v = check(&store, "帮我写一份周报模板", Some("https://api.openai.com/v1")).unwrap();
        assert_eq!(v["allowed"], true);
        assert_eq!(v["sensitivity"], "public");
        assert_eq!(v["endpointKnown"], true);
        assert_eq!(v["endpointHost"], "api.openai.com");
        assert_eq!(v["endpointLocal"], false);
        let rows = gate_traces(&store);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].resource.as_deref(), Some("allowed"));
        assert_eq!(rows[0].severity, audit::SEV_INFO);
    }

    #[test]
    fn check_blocks_external_endpoint_when_hard_switch_on() {
        let (store, _dir) = temp_store("check-hard");
        update(&store, &patch(json!({ "hardSwitchEnabled": true })).unwrap()).unwrap();
        let err = check(&store, "整理客户名单", Some("https://api.deepseek.com/v1")).unwrap_err();
        assert!(err.starts_with("POLICY_EXTERNAL_API_DISABLED"), "{err}");
        // 闸门与真实出站同一套判定：拒绝必须留痕且升级为 warning
        let rows = gate_traces(&store);
        assert_eq!(rows[0].resource.as_deref(), Some("blocked"));
        assert_eq!(rows[0].severity, audit::SEV_WARNING);
        // 本地回环不受硬开关影响
        let v = check(&store, "整理客户名单", Some("http://127.0.0.1:11434/v1")).unwrap();
        assert_eq!(v["endpointLocal"], true);
    }

    #[test]
    fn check_blocks_confidential_text_sent_to_external_endpoint() {
        let (store, _dir) = temp_store("check-leak");
        // 端点本身出域放行，才能单独检验内容侧的机密本地化规则
        update(
            &store,
            &patch(json!({
                "enabled": true,
                "mode": "whitelist",
                "allowedDomains": ["api.openai.com"]
            }))
            .unwrap(),
        )
        .unwrap();
        let err = check(
            &store,
            "客户身份证 11010119900307123X 请处理",
            Some("https://api.openai.com/v1"),
        )
        .unwrap_err();
        assert!(err.starts_with("COMPLIANCE_DATA_LEAK"), "{err}");
        assert_eq!(gate_traces(&store).len(), 1);
    }

    #[test]
    fn check_allows_confidential_text_on_local_endpoint() {
        let (store, _dir) = temp_store("check-local-confidential");
        update(&store, &patch(json!({ "enabled": true })).unwrap()).unwrap();
        let v = check(
            &store,
            "客户身份证 11010119900307123X 请处理",
            Some("http://localhost:11434/v1"),
        )
        .unwrap();
        assert_eq!(v["sensitivity"], "confidential");
        assert_eq!(v["forceLocal"], true);
        assert_eq!(v["allowed"], true);
    }

    #[test]
    fn check_honors_whitelist_before_content_rules() {
        let (store, _dir) = temp_store("check-whitelist");
        update(
            &store,
            &patch(json!({
                "enabled": true,
                "mode": "whitelist",
                "allowedDomains": ["api.deepseek.com"]
            }))
            .unwrap(),
        )
        .unwrap();
        let err = check(&store, "写周报", Some("https://api.openai.com/v1")).unwrap_err();
        assert!(err.starts_with("POLICY_DOMAIN_NOT_ALLOWED"), "{err}");
        assert!(check(&store, "写周报", Some("https://api.deepseek.com/v1")).is_ok());
    }

    #[test]
    fn check_without_endpoint_only_records_classification() {
        let (store, _dir) = temp_store("check-no-endpoint");
        // 引擎内置模型拿不到 baseUrl：只做分级留痕，不臆断是否出域
        let v = check(&store, "客户身份证 11010119900307123X", None).unwrap();
        assert_eq!(v["endpointKnown"], false);
        assert_eq!(v["sensitivity"], "confidential");
        let rows = gate_traces(&store);
        assert_eq!(rows[0].resource.as_deref(), Some("allowed"));
        assert!(rows[0].details.as_ref().unwrap().contains("端点未提供"));
    }

    #[test]
    fn check_honors_force_local_disabled_policy() {
        let (store, _dir) = temp_store("check-force-off");
        update(
            &store,
            &patch(json!({
                "enabled": true,
                "mode": "whitelist",
                "allowedDomains": ["api.openai.com"],
                "forceLocalForConfidential": false
            }))
            .unwrap(),
        )
        .unwrap();
        let v = check(
            &store,
            "客户身份证 11010119900307123X",
            Some("https://api.openai.com/v1"),
        )
        .unwrap();
        // 关闭机密本地化后不再阻断（策略由用户显式放宽，闸门只如实执行）
        assert_eq!(v["allowed"], true);
        assert_eq!(v["forceLocal"], true);
    }
}
