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
///
/// `exec*` / `network*` 是执行授权：默认关闭，旧策略行没有这些键时按关闭处理，
/// 不能因为反序列化失败就把整份策略判成损坏（那会掩盖已有白名单）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SandboxPolicy {
    pub workspace_id: String,
    pub execution_level: String,
    pub file_allowlist: Vec<String>,
    pub file_blocklist: Vec<String>,
    pub tool_allowlist: Vec<String>,
    pub tool_blocklist: Vec<String>,
    #[serde(default)]
    pub exec_enabled: bool,
    #[serde(default)]
    pub exec_allowlist: Vec<String>,
    #[serde(default)]
    pub network_enabled: bool,
    #[serde(default)]
    pub network_allowlist: Vec<String>,
    pub max_cpu_percent: i64,
    pub max_memory_mb: i64,
    pub max_disk_write_mbps: i64,
    pub updated_at_ms: i64,
}

/// 命令白名单条目上限（超过就说明不是「受控」，而是变相放行）
const MAX_EXEC_COMMANDS: usize = 64;
/// 联网白名单条目上限
const MAX_NETWORK_HOSTS: usize = 64;

/// 命令名归一化：只接受裸命令名（禁止路径、空格与 shell 元字符）。
/// 引擎用 `subprocess` 的 argv 方式执行，不经 shell；这里再拒绝路径，
/// 避免把 `C:/Windows/System32/cmd.exe` 当成「命令名」混进白名单。
fn normalize_command(raw: &str) -> Option<String> {
    let name = raw.trim();
    if name.is_empty() || name.len() > 64 {
        return None;
    }
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '+'))
    {
        return None;
    }
    Some(name.to_ascii_lowercase())
}

fn normalize_commands(list: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in list {
        if let Some(name) = normalize_command(item) {
            if !out.contains(&name) {
                out.push(name);
            }
        }
    }
    out
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
    if policy.exec_enabled && policy.exec_allowlist.len() > MAX_EXEC_COMMANDS {
        return Err(format!(
            "POLICY_INVALID_COMMAND: 命令白名单最多 {MAX_EXEC_COMMANDS} 项"
        ));
    }
    let exec_allowlist = normalize_commands(&policy.exec_allowlist);
    if policy.exec_allowlist.iter().any(|c| normalize_command(c).is_none()) {
        return Err(
            "POLICY_INVALID_COMMAND: 命令名只能包含字母、数字、下划线、连字符、点或加号，且不能带路径"
                .into(),
        );
    }
    if policy.network_enabled && policy.network_allowlist.len() > MAX_NETWORK_HOSTS {
        return Err(format!(
            "POLICY_INVALID_HOST: 联网白名单最多 {MAX_NETWORK_HOSTS} 项"
        ));
    }
    let network_allowlist = normalize_domains(&policy.network_allowlist);
    if policy.network_allowlist.iter().any(|h| normalize_domain(h).is_none()) {
        return Err("POLICY_INVALID_HOST: 主机名不能为空或包含空格".into());
    }
    let stored = SandboxPolicy {
        workspace_id: policy.workspace_id.trim().to_string(),
        execution_level: policy.execution_level.clone(),
        file_allowlist: policy.file_allowlist.clone(),
        file_blocklist: policy.file_blocklist.clone(),
        tool_allowlist: policy.tool_allowlist.clone(),
        tool_blocklist: policy.tool_blocklist.clone(),
        exec_enabled: policy.exec_enabled,
        exec_allowlist,
        network_enabled: policy.network_enabled,
        network_allowlist,
        max_cpu_percent: policy.max_cpu_percent,
        max_memory_mb: policy.max_memory_mb,
        max_disk_write_mbps: policy.max_disk_write_mbps,
        updated_at_ms: now_ms(),
    };
    let json = serde_json::to_string(&stored).map_err(|e| format!("POLICY_SAVE_FAILED: {e}"))?;
    put_json(store, &sandbox_key(&stored.workspace_id), &json)?;
    Ok(stored)
}

// ── 系统级沙箱 ──────────────────────────────────────────────────

pub const SYSTEM_SANDBOX_KEY: &str = "system_sandbox";

/// 读取系统级沙箱设置；未保存过按关闭处理
pub fn get_system_sandbox(
    store: &Store,
) -> Result<crate::sandbox::SystemSandboxConfig, String> {
    let Some(raw) = get_json(store, SYSTEM_SANDBOX_KEY)? else {
        return Ok(crate::sandbox::SystemSandboxConfig::default());
    };
    serde_json::from_str(&raw)
        .map_err(|e| format!("POLICY_CORRUPT: system_sandbox 解析失败（{e}）"))
}

/// 保存系统级沙箱设置：平台没有实现时拒绝「启用」，不落一份永远不生效的已开启状态
pub fn set_system_sandbox(
    store: &Store,
    config: &crate::sandbox::SystemSandboxConfig,
) -> Result<crate::sandbox::SystemSandboxConfig, String> {
    if config.enabled && !crate::sandbox::platform_supported() {
        return Err(format!(
            "POLICY_UNSUPPORTED: 当前平台（{}）没有系统级沙箱实现，无法启用",
            std::env::consts::OS
        ));
    }
    let stored = crate::sandbox::SystemSandboxConfig {
        enabled: config.enabled,
        network: config.network,
        updated_at_ms: now_ms(),
    };
    let json = serde_json::to_string(&stored).map_err(|e| format!("POLICY_SAVE_FAILED: {e}"))?;
    put_json(store, SYSTEM_SANDBOX_KEY, &json)?;
    Ok(stored)
}

// ── 路径规则（沙箱文件白/黑名单） ──────────────────────────────

/// 路径文本归一化：统一为 `/`、去掉尾部斜杠；Windows 下转小写（文件系统不区分大小写）。
pub fn normalize_path_text(raw: &str) -> String {
    let s = raw.trim().replace('\\', "/");
    let trimmed = if !s.is_empty() && s.chars().all(|c| c == '/') {
        "/"
    } else {
        s.trim_end_matches('/')
    };
    if cfg!(windows) {
        trimmed.to_ascii_lowercase()
    } else {
        trimmed.to_string()
    }
}

/// 轻量 glob：`*` 不跨目录、`**` 跨目录、`?` 单个非分隔字符。
fn glob_match(pattern: &str, text: &str) -> bool {
    fn rec(p: &[u8], t: &[u8]) -> bool {
        let Some(&head) = p.first() else {
            return t.is_empty();
        };
        match head {
            b'*' => {
                let double = p.get(1) == Some(&b'*');
                let rest = if double { &p[2..] } else { &p[1..] };
                if rec(rest, t) {
                    return true;
                }
                let mut i = 0;
                while i < t.len() {
                    if !double && t[i] == b'/' {
                        break;
                    }
                    i += 1;
                    if rec(rest, &t[i..]) {
                        return true;
                    }
                }
                false
            }
            b'?' => !t.is_empty() && t[0] != b'/' && rec(&p[1..], &t[1..]),
            c => !t.is_empty() && t[0] == c && rec(&p[1..], &t[1..]),
        }
    }
    rec(pattern.as_bytes(), text.as_bytes())
}

/// 单条规则是否命中路径。
///
/// - 无通配符：精确匹配，或作为目录前缀（`C:/Windows` 命中其下所有文件）
/// - `dir/**`：命中该目录本身及其下所有内容
/// - 其余按 glob 语义匹配（`*` 不跨目录）
pub fn path_matches_rule(rule: &str, path: &str) -> bool {
    let r = normalize_path_text(rule);
    if r.is_empty() {
        return false;
    }
    let p = normalize_path_text(path);
    if let Some(base) = r.strip_suffix("/**") {
        if p == base || p.starts_with(&format!("{base}/")) {
            return true;
        }
    }
    if r.contains('*') || r.contains('?') {
        return glob_match(&r, &p);
    }
    p == r || p.starts_with(&format!("{r}/")) || (r == "/" && p.starts_with('/'))
}

/// 返回命中的第一条规则（用于审计说明），未命中返回 None
pub fn path_blocked(rules: &[String], path: &str) -> Option<String> {
    rules
        .iter()
        .find(|r| path_matches_rule(r, path))
        .cloned()
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
            exec_enabled: false,
            exec_allowlist: Vec::new(),
            network_enabled: false,
            network_allowlist: Vec::new(),
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
    fn test_exec_and_network_grants_are_default_off_and_normalized() {
        let (store, dir) = temp_store("sb-grants");
        // 旧策略行没有执行授权键：按关闭处理，不能判成损坏
        let mut legacy = serde_json::to_value(sample_sandbox("ws-a")).unwrap();
        legacy.as_object_mut().unwrap().remove("execEnabled");
        legacy.as_object_mut().unwrap().remove("execAllowlist");
        legacy.as_object_mut().unwrap().remove("networkEnabled");
        legacy.as_object_mut().unwrap().remove("networkAllowlist");
        put_json(&store, &sandbox_key("ws-a"), &legacy.to_string()).unwrap();
        let read = get_sandbox(&store, "ws-a").unwrap().unwrap();
        assert!(!read.exec_enabled && !read.network_enabled);
        assert!(read.exec_allowlist.is_empty());

        let mut p = sample_sandbox("ws-b");
        p.exec_enabled = true;
        p.exec_allowlist = vec!["Python".into(), "python".into(), "git".into()];
        p.network_enabled = true;
        p.network_allowlist = vec!["https://API.Example.com/v1".into(), "example.org".into()];
        let saved = set_sandbox(&store, &p).unwrap();
        // 命令名小写去重；主机去协议/路径、小写去重
        assert_eq!(saved.exec_allowlist, vec!["python", "git"]);
        assert_eq!(saved.network_allowlist, vec!["api.example.com", "example.org"]);
        let read = get_sandbox(&store, "ws-b").unwrap().unwrap();
        assert!(read.exec_enabled && read.network_enabled);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_exec_and_network_grants_reject_paths_and_bad_hosts() {
        let (store, dir) = temp_store("sb-grants-bad");
        let mut p = sample_sandbox("ws");
        p.exec_enabled = true;
        p.exec_allowlist = vec!["C:/Windows/System32/cmd.exe".into()];
        assert!(set_sandbox(&store, &p)
            .unwrap_err()
            .starts_with("POLICY_INVALID_COMMAND"));
        p.exec_allowlist = vec!["powershell -Command".into()];
        assert!(set_sandbox(&store, &p)
            .unwrap_err()
            .starts_with("POLICY_INVALID_COMMAND"));
        p.exec_enabled = false;
        p.exec_allowlist = Vec::new();
        p.network_enabled = true;
        p.network_allowlist = vec!["has space.com".into()];
        assert!(set_sandbox(&store, &p)
            .unwrap_err()
            .starts_with("POLICY_INVALID_HOST"));
        // 校验失败不落库
        assert!(get_sandbox(&store, "ws").unwrap().is_none());
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
    fn test_sandbox_corrupt_json_errors() {
        let (store, dir) = temp_store("sb-corrupt");
        put_json(&store, &sandbox_key("ws-a"), "not-json").unwrap();
        let err = get_sandbox(&store, "ws-a").unwrap_err();
        assert!(err.starts_with("POLICY_CORRUPT"), "{err}");
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
    fn test_path_rule_directory_prefix_and_glob() {
        // 无通配符：目录前缀语义（不是字符串前缀，WindowsX 不能被 Windows 命中）
        assert!(path_matches_rule("C:/Windows", "C:\\Windows\\System32\\drivers"));
        assert!(path_matches_rule("C:/Windows/", "C:/Windows"));
        assert!(!path_matches_rule("C:/Windows", "C:/WindowsX/file"));
        // dir/** 命中目录本身与全部后代
        assert!(path_matches_rule("D:/work/**", "D:/work"));
        assert!(path_matches_rule("D:/work/**", "D:/work/a/b.txt"));
        // 单星不跨目录，双星跨目录
        assert!(!path_matches_rule("D:/work/*.txt", "D:/work/a/b.txt"));
        assert!(path_matches_rule("D:/work/*.txt", "D:/work/a.txt"));
        assert!(path_matches_rule("D:/work/**/secret.txt", "D:/work/a/b/secret.txt"));
        assert!(path_matches_rule("D:/work/?.md", "D:/work/a.md"));
        assert!(!path_matches_rule("D:/work/?.md", "D:/work/ab.md"));
        // 空规则永不放宽
        assert!(!path_matches_rule("  ", "D:/work/a.md"));
        assert!(path_blocked(&[], "D:/work/a.md").is_none());
        assert_eq!(
            path_blocked(&["D:/work/**".into()], "D:/work/a.md").as_deref(),
            Some("D:/work/**")
        );
    }

    #[test]
    fn test_normalize_path_text() {
        let expected = if cfg!(windows) { "c:/users/x" } else { "C:/Users/x" };
        assert_eq!(normalize_path_text("C:\\Users\\x\\"), expected);
        assert_eq!(normalize_path_text("///"), "/");
        assert_eq!(normalize_path_text("  "), "");
        #[cfg(windows)]
        assert!(path_matches_rule("C:/Windows/System32", "c:\\windows\\system32\\drivers"));
    }

    #[test]
    fn test_normalize_domain_edges() {
        assert_eq!(normalize_domain("  HTTPS://API.OpenAI.com/v1  ").as_deref(), Some("api.openai.com"));
        assert_eq!(normalize_domain("localhost:11434").as_deref(), Some("localhost"));
        assert_eq!(normalize_domain(""), None);
        assert_eq!(normalize_domain("   "), None);
        assert_eq!(normalize_domain("has space.com"), None);
    }

    #[test]
    fn test_system_sandbox_defaults_off_then_roundtrip() {
        let (store, dir) = temp_store("system-sandbox");
        let default = get_system_sandbox(&store).unwrap();
        assert!(!default.enabled, "从未保存过时必须按关闭处理");

        let saved = set_system_sandbox(
            &store,
            &crate::sandbox::SystemSandboxConfig {
                enabled: false,
                network: true,
                updated_at_ms: 0,
            },
        )
        .unwrap();
        assert!(saved.updated_at_ms > 0, "保存时应写入时间戳");
        assert_eq!(get_system_sandbox(&store).unwrap(), saved);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_system_sandbox_rejects_enable_on_unsupported_platform() {
        if crate::sandbox::platform_supported() {
            return;
        }
        let (store, dir) = temp_store("system-sandbox-unsupported");
        let err = set_system_sandbox(
            &store,
            &crate::sandbox::SystemSandboxConfig {
                enabled: true,
                network: false,
                updated_at_ms: 0,
            },
        )
        .unwrap_err();
        assert!(err.starts_with("POLICY_UNSUPPORTED"), "{err}");
        assert!(!get_system_sandbox(&store).unwrap().enabled);
        let _ = std::fs::remove_dir_all(dir);
    }
}