//! 出域控制与沙箱策略（PRD §4.7.1 出域合规 / §4.3.4 沙箱隔离）
//!
//! - 持久化在 `security_policies` 表（key → JSON），按 workspace 维度存沙箱策略
//! - `ensure_domain_allowed` 为真实执行点：模型连通性测试等出站调用前强制校验
//!   （硬开关关闭外部 API / 白名单外域名一律拒绝），让开关具备实际约束力
//! - 读取到损坏 JSON 时返回 `POLICY_CORRUPT`，不静默回落默认值（避免策略被悄悄放宽）

use serde::{Deserialize, Serialize};

use crate::store::Store;

pub const DOMAIN_CONTROL_KEY: &str = "domain_control";
pub const SANDBOX_KEY_PREFIX: &str = "sandbox:";

/// 允许的域名清单模式
pub const DOMAIN_MODES: &[&str] = &["whitelist", "blacklist"];
/// 允许的沙箱执行级别
pub const EXECUTION_LEVELS: &[&str] = &["L0", "L1", "L2"];

/// 本地回环主机（本地通道始终放行，不受白名单约束）
const LOCAL_HOSTS: &[&str] = &["localhost", "127.0.0.1", "::1", "0.0.0.0"];

/// 主机名是否为本地回环（合规闸门与出域判定共用同一份本地口径）
pub fn is_local_host(host: &str) -> bool {
    LOCAL_HOSTS.contains(&host)
}

/// 出域控制配置（与前端 `DomainControlConfig` 契约一致）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DomainControl {
    pub enabled: bool,
    pub mode: String,
    pub allowed_domains: Vec<String>,
    pub blocked_domains: Vec<String>,
    pub force_local_for_confidential: bool,
    pub hard_disable_external_api: bool,
    pub updated_at_ms: i64,
}

/// 沙箱策略（与前端 `SandboxPolicy` 契约一致，附加 updatedAtMs）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SandboxPolicy {
    pub workspace_id: String,
    pub execution_level: String,
    pub file_allowlist: Vec<String>,
    pub file_blocklist: Vec<String>,
    pub tool_allowlist: Vec<String>,
    pub tool_blocklist: Vec<String>,
    pub max_cpu_percent: i64,
    pub max_memory_mb: i64,
    pub max_disk_write_mbps: i64,
    pub updated_at_ms: i64,
}

/// 默认出域控制配置（未配置过时的语义默认：放行本地、不额外限制）
pub fn default_domain_control() -> DomainControl {
    DomainControl {
        enabled: false,
        mode: "whitelist".to_string(),
        allowed_domains: vec!["localhost".to_string(), "127.0.0.1".to_string()],
        blocked_domains: Vec::new(),
        force_local_for_confidential: true,
        hard_disable_external_api: false,
        updated_at_ms: 0,
    }
}

fn now_ms() -> i64 {
    crate::bridge::event_store::now_ms() as i64
}

fn get_json(store: &Store, key: &str) -> Result<Option<String>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare("SELECT json FROM security_policies WHERE key = ?1")
            .map_err(|e| format!("POLICY_READ_FAILED: {e}"))?;
        let mut rows = stmt
            .query([key])
            .map_err(|e| format!("POLICY_READ_FAILED: {e}"))?;
        match rows.next().map_err(|e| format!("POLICY_READ_FAILED: {e}"))? {
            Some(row) => Ok(Some(
                row.get::<_, String>(0)
                    .map_err(|e| format!("POLICY_READ_FAILED: {e}"))?,
            )),
            None => Ok(None),
        }
    })
}

fn put_json(store: &Store, key: &str, json: &str) -> Result<(), String> {
    let ts = now_ms();
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO security_policies (key, json, updated_at_ms) VALUES (?1, ?2, ?3)
             ON CONFLICT(key) DO UPDATE SET json = excluded.json, updated_at_ms = excluded.updated_at_ms",
            rusqlite::params![key, json, ts],
        )
        .map(|_| ())
        .map_err(|e| format!("POLICY_SAVE_FAILED: {e}"))
    })
}

// ── 域名归一化 ──────────────────────────────────────────────────

/// 域名归一化：去协议前缀、路径与端口，转小写。
/// 用户常直接粘贴 `https://api.openai.com/v1`，归一化后按主机名匹配。
/// 已知边界：IPv6 字面量（含冒号）会被截断，MVP 以 IPv4/域名为主。
fn normalize_domain(raw: &str) -> Option<String> {
    let lower = raw.trim().to_ascii_lowercase();
    if lower.is_empty() {
        return None;
    }
    let no_scheme = lower.split("://").last().unwrap_or(&lower);
    let no_path = no_scheme.split('/').next().unwrap_or(no_scheme);
    let host = no_path.split(':').next().unwrap_or(no_path);
    if host.is_empty() || host.contains(' ') {
        return None;
    }
    Some(host.to_string())
}

fn normalize_domains(list: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in list {
        if let Some(d) = normalize_domain(item) {
            if !out.contains(&d) {
                out.push(d);
            }
        }
    }
    out
}

// ── 出域控制 ────────────────────────────────────────────────────

/// 读取出域控制配置（未配置返回 None，由前端展示默认值）
pub fn get_domain_control(store: &Store) -> Result<Option<DomainControl>, String> {
    let Some(raw) = get_json(store, DOMAIN_CONTROL_KEY)? else {
        return Ok(None);
    };
    serde_json::from_str(&raw)
        .map(Some)
        .map_err(|e| format!("POLICY_CORRUPT: {DOMAIN_CONTROL_KEY} 配置解析失败（{e}）"))
}

/// 保存出域控制配置：校验 + 归一化 + 落库，返回实际存储的配置
pub fn set_domain_control(store: &Store, cfg: &DomainControl) -> Result<DomainControl, String> {
    if !DOMAIN_MODES.contains(&cfg.mode.as_str()) {
        return Err(format!("POLICY_INVALID_MODE: {}", cfg.mode));
    }
    let stored = DomainControl {
        enabled: cfg.enabled,
        mode: cfg.mode.clone(),
        allowed_domains: normalize_domains(&cfg.allowed_domains),
        blocked_domains: normalize_domains(&cfg.blocked_domains),
        force_local_for_confidential: cfg.force_local_for_confidential,
        hard_disable_external_api: cfg.hard_disable_external_api,
        updated_at_ms: now_ms(),
    };
    let json = serde_json::to_string(&stored).map_err(|e| format!("POLICY_SAVE_FAILED: {e}"))?;
    put_json(store, DOMAIN_CONTROL_KEY, &json)?;
    Ok(stored)
}

/// 从 URL 提取主机名（与 normalize_domain 同规则）
pub fn host_of(url: &str) -> String {
    normalize_domain(url).unwrap_or_default()
}

/// 出域校验（真实执行点）：被拒绝时返回带错误码的 Err
pub fn ensure_domain_allowed(store: &Store, url: &str) -> Result<(), String> {
    let cfg = get_domain_control(store)?.unwrap_or_else(default_domain_control);
    eval_domain_allowed(&cfg, url)
}

/// 按给定配置校验出域（不读库）：供合规状态批量评估模型可达性时复用同一套判定
pub fn eval_domain_allowed(cfg: &DomainControl, url: &str) -> Result<(), String> {
    let host = host_of(url);
    if host.is_empty() {
        return Err(format!("POLICY_INVALID_URL: 无法解析主机名（{url}）"));
    }

    // 硬开关：企业策略一键禁用所有外部 API（本地回环除外）
    if cfg.hard_disable_external_api && !is_local_host(&host) {
        return Err(format!("POLICY_EXTERNAL_API_DISABLED: 外部 API 已被策略禁用（{host}）"));
    }
    if !cfg.enabled || is_local_host(&host) {
        return Ok(());
    }
    match cfg.mode.as_str() {
        "whitelist" => {
            if cfg.allowed_domains.iter().any(|d| d == &host) {
                Ok(())
            } else {
                Err(format!("POLICY_DOMAIN_NOT_ALLOWED: {host} 不在出域白名单内"))
            }
        }
        _ => {
            if cfg.blocked_domains.iter().any(|d| d == &host) {
                Err(format!("POLICY_DOMAIN_BLOCKED: {host} 已被出域黑名单拦截"))
            } else {
                Ok(())
            }
        }
    }
}

// ── 沙箱策略 ────────────────────────────────────────────────────

fn sandbox_key(workspace_id: &str) -> String {
    format!("{SANDBOX_KEY_PREFIX}{workspace_id}")
}

/// 读取工作区沙箱策略（未配置返回 None）
pub fn get_sandbox(store: &Store, workspace_id: &str) -> Result<Option<SandboxPolicy>, String> {
    if workspace_id.trim().is_empty() {
        return Err("POLICY_INVALID_WORKSPACE: workspaceId 不能为空".into());
    }
    let Some(raw) = get_json(store, &sandbox_key(workspace_id))? else {
        return Ok(None);
    };
    serde_json::from_str(&raw)
        .map(Some)
        .map_err(|e| format!("POLICY_CORRUPT: sandbox 策略解析失败（{e}）"))
}

/// 保存工作区沙箱策略：校验边界 + 落库，返回实际存储的策略
pub fn set_sandbox(store: &Store, policy: &SandboxPolicy) -> Result<SandboxPolicy, String> {
    if policy.workspace_id.trim().is_empty() {
        return Err("POLICY_INVALID_WORKSPACE: workspaceId 不能为空".into());
    }
    if !EXECUTION_LEVELS.contains(&policy.execution_level.as_str()) {
        return Err(format!("POLICY_INVALID_LEVEL: {}", policy.execution_level));
    }
    if !(1..=100).contains(&policy.max_cpu_percent) {
        return Err(format!("POLICY_INVALID_RANGE: maxCpuPercent={}", policy.max_cpu_percent));
    }
    if !(64..=32_768).contains(&policy.max_memory_mb) {
        return Err(format!("POLICY_INVALID_RANGE: maxMemoryMB={}", policy.max_memory_mb));
    }
    if !(1..=10_000).contains(&policy.max_disk_write_mbps) {
        return Err(format!(
            "POLICY_INVALID_RANGE: maxDiskWriteMbps={}",
            policy.max_disk_write_mbps
        ));
    }
    let stored = SandboxPolicy {
        workspace_id: policy.workspace_id.trim().to_string(),
        execution_level: policy.execution_level.clone(),
        file_allowlist: policy.file_allowlist.clone(),
        file_blocklist: policy.file_blocklist.clone(),
        tool_allowlist: policy.tool_allowlist.clone(),
        tool_blocklist: policy.tool_blocklist.clone(),
        max_cpu_percent: policy.max_cpu_percent,
        max_memory_mb: policy.max_memory_mb,
        max_disk_write_mbps: policy.max_disk_write_mbps,
        updated_at_ms: now_ms(),
    };
    let json = serde_json::to_string(&stored).map_err(|e| format!("POLICY_SAVE_FAILED: {e}"))?;
    put_json(store, &sandbox_key(&stored.workspace_id), &json)?;
    Ok(stored)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-policy-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    fn sample_sandbox(ws: &str) -> SandboxPolicy {
        SandboxPolicy {
            workspace_id: ws.to_string(),
            execution_level: "L1".to_string(),
            file_allowlist: vec!["C:/Users/x/Documents/**".to_string()],
            file_blocklist: vec!["C:/Windows/**".to_string()],
            tool_allowlist: Vec::new(),
            tool_blocklist: Vec::new(),
            max_cpu_percent: 50,
            max_memory_mb: 512,
            max_disk_write_mbps: 10,
            updated_at_ms: 0,
        }
    }

    #[test]
    fn test_domain_control_absent_then_roundtrip() {
        let (store, dir) = temp_store("dc");
        assert!(get_domain_control(&store).unwrap().is_none());

        let mut cfg = default_domain_control();
        cfg.enabled = true;
        cfg.allowed_domains = vec!["https://api.openai.com/v1".into(), "API.OpenAI.com".into()];
        let stored = set_domain_control(&store, &cfg).unwrap();
        // 归一化：去协议/路径、转小写、去重
        assert_eq!(stored.allowed_domains, vec!["api.openai.com"]);
        assert!(stored.updated_at_ms > 0);

        let read = get_domain_control(&store).unwrap().unwrap();
        assert!(read.enabled);
        assert_eq!(read.mode, "whitelist");
        assert!(read.force_local_for_confidential);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_domain_control_rejects_bad_mode() {
        let (store, dir) = temp_store("dc-mode");
        let mut cfg = default_domain_control();
        cfg.mode = "sometimes".into();
        let err = set_domain_control(&store, &cfg).unwrap_err();
        assert!(err.starts_with("POLICY_INVALID_MODE"), "{err}");
        // 非法配置不落库
        assert!(get_domain_control(&store).unwrap().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_domain_control_corrupt_json_errors() {
        let (store, dir) = temp_store("dc-corrupt");
        put_json(&store, DOMAIN_CONTROL_KEY, "not-json").unwrap();
        let err = get_domain_control(&store).unwrap_err();
        assert!(err.starts_with("POLICY_CORRUPT"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_ensure_domain_allowed_default_open() {
        let (store, dir) = temp_store("allow-default");
        // 未配置：放行（本地优先语义，不设隐性限制）
        assert!(ensure_domain_allowed(&store, "https://api.deepseek.com/v1").is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_ensure_domain_allowed_whitelist() {
        let (store, dir) = temp_store("allow-wl");
        let mut cfg = default_domain_control();
        cfg.enabled = true;
        cfg.allowed_domains = vec!["api.deepseek.com".into()];
        set_domain_control(&store, &cfg).unwrap();

        assert!(ensure_domain_allowed(&store, "https://api.deepseek.com/v1").is_ok());
        assert!(ensure_domain_allowed(&store, "http://127.0.0.1:11434").is_ok()); // 本地始终放行
        let err = ensure_domain_allowed(&store, "https://api.openai.com/v1").unwrap_err();
        assert!(err.starts_with("POLICY_DOMAIN_NOT_ALLOWED"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_ensure_domain_allowed_blacklist() {
        let (store, dir) = temp_store("allow-bl");
        let mut cfg = default_domain_control();
        cfg.enabled = true;
        cfg.mode = "blacklist".into();
        cfg.blocked_domains = vec!["evil.example.com".into()];
        set_domain_control(&store, &cfg).unwrap();
        assert!(ensure_domain_allowed(&store, "https://ok.example.com").is_ok());
        assert!(ensure_domain_allowed(&store, "https://evil.example.com").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_ensure_domain_allowed_hard_switch_blocks_all_external() {
        let (store, dir) = temp_store("allow-hard");
        let mut cfg = default_domain_control();
        cfg.hard_disable_external_api = true;
        set_domain_control(&store, &cfg).unwrap();
        let err = ensure_domain_allowed(&store, "https://api.deepseek.com").unwrap_err();
        assert!(err.starts_with("POLICY_EXTERNAL_API_DISABLED"), "{err}");
        // 即便未启用白名单，硬开关仍然拦住外部域名
        assert!(ensure_domain_allowed(&store, "http://localhost:11434").is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_ensure_domain_allowed_invalid_url() {
        let (store, dir) = temp_store("allow-bad");
        let err = ensure_domain_allowed(&store, "  ").unwrap_err();
        assert!(err.starts_with("POLICY_INVALID_URL"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_sandbox_roundtrip_and_isolation_between_workspaces() {
        let (store, dir) = temp_store("sb");
        assert!(get_sandbox(&store, "ws-a").unwrap().is_none());

        let saved = set_sandbox(&store, &sample_sandbox("ws-a")).unwrap();
        assert!(saved.updated_at_ms > 0);
        let read = get_sandbox(&store, "ws-a").unwrap().unwrap();
        assert_eq!(read.execution_level, "L1");
        assert_eq!(read.max_memory_mb, 512);
        // 另一工作区互不影响
        assert!(get_sandbox(&store, "ws-b").unwrap().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_sandbox_upsert_overwrites_same_key() {
        let (store, dir) = temp_store("sb-upsert");
        set_sandbox(&store, &sample_sandbox("ws-a")).unwrap();
        let mut p = sample_sandbox("ws-a");
        p.execution_level = "L2".into();
        set_sandbox(&store, &p).unwrap();
        assert_eq!(get_sandbox(&store, "ws-a").unwrap().unwrap().execution_level, "L2");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_sandbox_validation_errors() {
        let (store, dir) = temp_store("sb-bad");

        let mut p = sample_sandbox("");
        assert!(set_sandbox(&store, &p).unwrap_err().starts_with("POLICY_INVALID_WORKSPACE"));
        p = sample_sandbox("ws");
        p.execution_level = "L9".into();
        assert!(set_sandbox(&store, &p).unwrap_err().starts_with("POLICY_INVALID_LEVEL"));
        p = sample_sandbox("ws");
        p.max_cpu_percent = 0;
        assert!(set_sandbox(&store, &p).unwrap_err().starts_with("POLICY_INVALID_RANGE"));
        p = sample_sandbox("ws");
        p.max_memory_mb = 1;
        assert!(set_sandbox(&store, &p).unwrap_err().starts_with("POLICY_INVALID_RANGE"));
        p = sample_sandbox("ws");
        p.max_disk_write_mbps = 0;
        assert!(set_sandbox(&store, &p).unwrap_err().starts_with("POLICY_INVALID_RANGE"));
        // 校验失败不落库
        assert!(get_sandbox(&store, "ws").unwrap().is_none());
        assert!(get_sandbox(&store, "  ").unwrap_err().starts_with("POLICY_INVALID_WORKSPACE"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_normalize_domain_edges() {
        assert_eq!(normalize_domain("  HTTPS://API.OpenAI.com/v1  ").as_deref(), Some("api.openai.com"));
        assert_eq!(normalize_domain("localhost:11434").as_deref(), Some("localhost"));
        assert_eq!(normalize_domain(""), None);
        assert_eq!(normalize_domain("   "), None);
        assert_eq!(normalize_domain("has space.com"), None);
    }
}