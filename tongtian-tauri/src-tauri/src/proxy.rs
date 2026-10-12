//! 网络代理设置的消费层（设置项 proxyMode / proxyUrl / proxyBypass）
//!
//! 此前「网络代理」只把 proxyMode 落进 settings_kv，没有任何消费方：Rust 侧出网
//! （模型连通性测试、MCP Streamable HTTP、云端账号）用默认 client，引擎子进程也只
//! 继承父进程环境。本模块把同一份设置同时喂给 reqwest client 与子进程环境变量，
//! 使设置在保存后立即生效（无需重启）。
//!
//! 本地回环永远直连：引擎跑在 127.0.0.1 随机端口，本地模型（Ollama/llama.cpp）也
//! 常监听 localhost，被代理拦截会直接断链，因此 bypass 里无条件并入 ALWAYS_BYPASS。

use std::sync::{Mutex, OnceLock};

use crate::store::settings;
use crate::store::Store;

pub const SETTING_KEY_MODE: &str = "proxyMode";
pub const SETTING_KEY_URL: &str = "proxyUrl";
pub const SETTING_KEY_BYPASS: &str = "proxyBypass";

/// 无论如何都不走代理的地址（引擎与本地模型的通信底线）
pub const ALWAYS_BYPASS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProxyMode {
    /// 不额外指定代理，由本机网络配置决定（缺省）
    System,
    /// 使用用户填写的代理地址
    Custom,
    /// 明确不使用代理：连子进程继承的代理变量一并清空
    Disabled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProxySpec {
    pub mode: ProxyMode,
    /// Custom 模式下的代理地址（已校验，形如 http://127.0.0.1:7890）
    pub url: Option<String>,
    /// 用户附加的忽略主机（不含 ALWAYS_BYPASS，展示口径以设置为准）
    pub bypass: Vec<String>,
}

impl Default for ProxySpec {
    fn default() -> Self {
        Self {
            mode: ProxyMode::System,
            url: None,
            bypass: Vec::new(),
        }
    }
}

impl ProxySpec {
    /// 缓存指纹：设置变化后重建 client / 重新注入子进程环境
    pub fn fingerprint(&self) -> String {
        format!(
            "{:?}|{}|{}",
            self.mode,
            self.url.clone().unwrap_or_default(),
            self.bypass.join(",")
        )
    }

    /// 完整忽略清单（用户附加项 + 本地回环，去重保序）
    pub fn effective_bypass(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for item in self.bypass.iter().map(|s| s.as_str()).chain(ALWAYS_BYPASS) {
            if !out.iter().any(|existing| existing == item) {
                out.push(item.to_string());
            }
        }
        out
    }

    /// 注入引擎子进程的代理环境变量（跟随系统时返回空，保持继承不臆造）
    pub fn env_vars(&self) -> Vec<(String, String)> {
        let names = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ];
        match self.mode {
            ProxyMode::System => Vec::new(),
            ProxyMode::Disabled => {
                let mut vars: Vec<(String, String)> =
                    names.iter().map(|n| (n.to_string(), String::new())).collect();
                vars.push(("NO_PROXY".into(), "*".into()));
                vars.push(("no_proxy".into(), "*".into()));
                vars
            }
            ProxyMode::Custom => {
                let url = self.url.clone().unwrap_or_default();
                let no_proxy = self.effective_bypass().join(",");
                let mut vars: Vec<(String, String)> =
                    names.iter().map(|n| (n.to_string(), url.clone())).collect();
                vars.push(("NO_PROXY".into(), no_proxy.clone()));
                vars.push(("no_proxy".into(), no_proxy));
                vars
            }
        }
    }

    /// 应用到 reqwest 构造器。
    /// 跟随系统 = 保留 reqwest 默认（自动探测系统代理）；
    /// 不使用 = 关闭自动探测且不指定代理；
    /// 自定义 = 关闭自动探测后只走用户填写的那一个（避免两个代理叠加）。
    pub fn apply(&self, builder: reqwest::ClientBuilder) -> Result<reqwest::ClientBuilder, String> {
        match self.mode {
            ProxyMode::System => Ok(builder),
            ProxyMode::Disabled => Ok(builder.no_proxy()),
            ProxyMode::Custom => {
                let url = self
                    .url
                    .as_deref()
                    .ok_or_else(|| "PROXY_URL_REQUIRED: 未填写代理地址".to_string())?;
                let no_proxy = reqwest::NoProxy::from_string(&self.effective_bypass().join(","))
                    .unwrap_or_default();
                let proxy = reqwest::Proxy::all(url)
                    .map_err(|e| format!("PROXY_INVALID_URL: {e}"))?
                    .no_proxy(Some(no_proxy));
                Ok(builder.no_proxy().proxy(proxy))
            }
        }
    }
}

/// 代理模式解析（兼容中英文口径：设置页存的是中文选项）
pub fn parse_mode(raw: &str) -> ProxyMode {
    match raw.trim() {
        "自定义" | "custom" | "Custom" | "手动" => ProxyMode::Custom,
        "不使用" | "直连" | "off" | "none" | "disabled" | "Disabled" => ProxyMode::Disabled,
        _ => ProxyMode::System,
    }
}

/// 校验并归一代理地址（须带协议，不允许空主机）
pub fn validate_url(raw: &str) -> Result<String, String> {
    let url = raw.trim();
    if url.is_empty() {
        return Err("PROXY_URL_REQUIRED: 未填写代理地址".to_string());
    }
    let sep = url
        .find("://")
        .ok_or_else(|| "PROXY_INVALID_URL: 代理地址需带协议，如 http://127.0.0.1:7890".to_string())?;
    let scheme = &url[..sep];
    if !matches!(scheme, "http" | "https" | "socks5" | "socks5h" | "socks4" | "socks") {
        return Err(format!(
            "PROXY_INVALID_URL: 不支持的代理协议 {scheme}，仅支持 http / https / socks5 / socks4"
        ));
    }
    let host = url[sep + 3..].trim_end_matches('/');
    if host.is_empty() {
        return Err("PROXY_INVALID_URL: 代理地址缺少主机与端口，如 http://127.0.0.1:7890".to_string());
    }
    validate_authority(host)?;
    Ok(format!("{scheme}://{host}"))
}

/// 主机与端口形状校验。协议前缀合法但整体不可路由的地址（尾随路径、第二个协议、
/// 端口非数字、主机含非法字符）一律拒绝——否则落库后所有出网请求只会静默超时，
/// 而设置页已经告诉用户「已保存并生效」。
fn validate_authority(authority: &str) -> Result<(), String> {
    if authority.contains("://") {
        return Err("PROXY_INVALID_URL: 只能有一个协议前缀".to_string());
    }
    if authority.contains(char::is_whitespace) || authority.contains(['/', '?', '#']) {
        return Err("PROXY_INVALID_URL: 主机部分不能含空格或路径".to_string());
    }
    // 允许 http://user:pass@proxy:8080 这类带凭据的地址，只校验凭据之后的 authority
    let authority = authority.rsplit('@').next().unwrap_or(authority);
    let port = match authority.strip_prefix('[') {
        // IPv6 字面量必须带方括号：[::1]:7890
        Some(inner) => {
            let end = inner
                .find(']')
                .ok_or_else(|| "PROXY_INVALID_URL: IPv6 地址需用方括号包裹".to_string())?;
            if inner[..end].is_empty() {
                return Err("PROXY_INVALID_URL: IPv6 地址需用方括号包裹".to_string());
            }
            let tail = &inner[end + 1..];
            if tail.is_empty() {
                None
            } else if let Some(p) = tail.strip_prefix(':') {
                Some(p)
            } else {
                return Err("PROXY_INVALID_URL: IPv6 地址需用方括号包裹".to_string());
            }
        }
        None => match authority.rsplit_once(':') {
            Some((h, p)) => {
                validate_host(h)?;
                Some(p)
            }
            None => {
                validate_host(authority)?;
                None
            }
        },
    };
    if let Some(p) = port {
        if p.is_empty() || !p.chars().all(|c| c.is_ascii_digit()) {
            return Err("PROXY_INVALID_URL: 端口需是数字".to_string());
        }
        let n: u32 = p
            .parse()
            .map_err(|_| "PROXY_INVALID_URL: 端口需在 1-65535 之间".to_string())?;
        if n == 0 || n > 65535 {
            return Err("PROXY_INVALID_URL: 端口需在 1-65535 之间".to_string());
        }
    }
    Ok(())
}

fn validate_host(host: &str) -> Result<(), String> {
    let is_v6 = host.contains(':');
    if is_v6 {
        // 未加方括号的 IPv6 无法可靠地切出端口，要求用户写清楚
        return Err("PROXY_INVALID_URL: IPv6 地址需用方括号包裹".to_string());
    }
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
    {
        return Err("PROXY_INVALID_URL: 主机名含非法字符".to_string());
    }
    Ok(())
}

/// 忽略主机清单解析（逗号/空格/分号分隔，去空去重）
pub fn parse_bypass(raw: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for token in raw.split([',', ' ', ';', '\n']) {
        let item = token.trim().trim_matches(',');
        if item.is_empty() {
            continue;
        }
        if !out.iter().any(|existing| existing == item) {
            out.push(item.to_string());
        }
    }
    out
}

/// 该设置键是否属于代理配置组
pub fn is_related_key(key: &str) -> bool {
    matches!(key, SETTING_KEY_MODE | SETTING_KEY_URL | SETTING_KEY_BYPASS)
}

fn read_str(store: &Store, key: &str) -> Result<String, String> {
    Ok(settings::get(store, key)?
        .map(|v| settings::decode_value(&v))
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_default())
}

/// 由原始设置值组装代理配置（纯函数，便于保存前整组校验）
pub fn spec_from(mode_raw: &str, url_raw: &str, bypass_raw: &str) -> Result<ProxySpec, String> {
    let mode = parse_mode(mode_raw);
    if mode != ProxyMode::Custom {
        return Ok(ProxySpec {
            mode,
            url: None,
            bypass: Vec::new(),
        });
    }
    Ok(ProxySpec {
        mode,
        url: Some(validate_url(url_raw)?),
        bypass: parse_bypass(bypass_raw),
    })
}

/// 从 settings_kv 组装代理配置（自定义但地址非法时报错，不静默降级）
pub fn spec(store: &Store) -> Result<ProxySpec, String> {
    spec_from(
        &read_str(store, SETTING_KEY_MODE)?,
        &read_str(store, SETTING_KEY_URL)?,
        &read_str(store, SETTING_KEY_BYPASS)?,
    )
}

/// 用「本次待写入的补丁 + 库里已有的其余键」试算代理配置。
/// settings_set_many 一次可写多键且顺序不定，故落库前先按最终状态校验，
/// 避免自定义模式已落库、地址却缺失而留下半个生效配置。
pub fn spec_from_patch(store: &Store, patch: &serde_json::Map<String, serde_json::Value>) -> Result<(), String> {
    if !patch.keys().any(|k| is_related_key(k)) {
        return Ok(());
    }
    let pick = |key: &str, fallback: String| -> String {
        match patch.get(key) {
            Some(v) => v.as_str().map(String::from).unwrap_or_default(),
            None => fallback,
        }
    };
    let mode_raw = pick(SETTING_KEY_MODE, read_str(store, SETTING_KEY_MODE)?);
    let url_raw = pick(SETTING_KEY_URL, read_str(store, SETTING_KEY_URL)?);
    let bypass_raw = pick(SETTING_KEY_BYPASS, read_str(store, SETTING_KEY_BYPASS)?);
    spec_from(&mode_raw, &url_raw, &bypass_raw).map(|_| ())
}

/// 读取失败时按「跟随系统」处理：启动不因一处设置读不动而受阻
pub fn spec_or_system(store: &Store) -> ProxySpec {
    spec(store).unwrap_or_default()
}

// ── 生效中的配置（供 reqwest client 复用，避免每处出网都读库） ──────

static ACTIVE: OnceLock<Mutex<ProxySpec>> = OnceLock::new();

fn active_slot() -> &'static Mutex<ProxySpec> {
    ACTIVE.get_or_init(|| Mutex::new(ProxySpec::default()))
}

fn lock_active() -> std::sync::MutexGuard<'static, ProxySpec> {
    active_slot()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// 发布生效配置（保存设置或启动恢复时调用）
pub fn publish(spec: ProxySpec) {
    *lock_active() = spec;
}

/// 当前生效配置
pub fn current() -> ProxySpec {
    lock_active().clone()
}

/// 设置页口径的模式名（与持久化的中文选项一致）
pub fn mode_label(mode: ProxyMode) -> &'static str {
    match mode {
        ProxyMode::System => "跟随系统",
        ProxyMode::Custom => "自定义",
        ProxyMode::Disabled => "不使用",
    }
}

// ── 出网 client（按指纹复用，设置变更后自动重建） ────────────────

static CLIENT: OnceLock<Mutex<Option<(String, reqwest::Client)>>> = OnceLock::new();

/// 用指定配置构造（或复用）reqwest client。
/// Client 内部是 Arc，克隆代价极低，因此调用方持有所有权即可，
/// 不必像旧实现那样返回 `&'static`（那样设置变更后无法替换）。
pub fn client_for(spec: &ProxySpec) -> Result<reqwest::Client, String> {
    let fingerprint = spec.fingerprint();
    let slot = CLIENT.get_or_init(|| Mutex::new(None));
    let mut guard = slot.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some((cached, client)) = guard.as_ref() {
        if *cached == fingerprint {
            return Ok(client.clone());
        }
    }
    let client = spec
        .apply(reqwest::Client::builder())?
        .build()
        .map_err(|e| format!("PROXY_CLIENT_BUILD_FAILED: {e}"))?;
    *guard = Some((fingerprint, client.clone()));
    Ok(client)
}

/// 当前生效配置的出网 client。
/// 配置理论上已在落库前校验过，这里兜底走「不使用代理」：
/// 宁可直连，也不把流量交给一个用户没填成功的代理地址。
pub fn client() -> reqwest::Client {
    client_for(&current()).unwrap_or_else(|_| reqwest::Client::builder().no_proxy().build().unwrap_or_default())
}

static LOOPBACK: OnceLock<reqwest::Client> = OnceLock::new();

/// 本地回环专用 client：引擎跑在 127.0.0.1 随机端口，这类请求永远直连。
/// 系统代理常不 bypass 回环，若让本机通信走代理会直接断链，因此这里显式关闭代理，
/// 不受「网络代理」设置影响。
pub fn loopback_client() -> reqwest::Client {
    LOOPBACK
        .get_or_init(|| {
            reqwest::Client::builder()
                .no_proxy()
                .build()
                .unwrap_or_default()
        })
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-proxy-{name}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        (Store::open(dir.join("t.db")).unwrap(), dir)
    }

    fn put(store: &Store, key: &str, value: serde_json::Value) {
        settings::set(store, key, &settings::encode_value(&value)).unwrap();
    }

    #[test]
    fn parse_mode_accepts_chinese_and_english() {
        assert_eq!(parse_mode("自定义"), ProxyMode::Custom);
        assert_eq!(parse_mode(" custom "), ProxyMode::Custom);
        assert_eq!(parse_mode("不使用"), ProxyMode::Disabled);
        assert_eq!(parse_mode("跟随系统"), ProxyMode::System);
        assert_eq!(parse_mode(""), ProxyMode::System);
    }

    #[test]
    fn validate_url_requires_scheme_and_host() {
        assert_eq!(
            validate_url(" http://127.0.0.1:7890/ ").unwrap(),
            "http://127.0.0.1:7890"
        );
        assert_eq!(
            validate_url("socks5://10.0.0.2:1080").unwrap(),
            "socks5://10.0.0.2:1080"
        );
        assert!(validate_url("").unwrap_err().starts_with("PROXY_URL_REQUIRED"));
        assert!(validate_url("127.0.0.1:7890")
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));
        assert!(validate_url("ftp://127.0.0.1:21")
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));
        assert!(validate_url("http://")
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));
        assert!(validate_url("http://a b")
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));
    }

    #[test]
    fn validate_url_rejects_unroutable_shapes_behind_a_valid_scheme() {
        // 协议前缀合法不等于地址可用：这些若放行，出网只会静默超时
        for raw in [
            "http://proxy:8080127.0.0.1:8080ftp://10.0.0.1:21",
            "http://proxy.example.com:8080/path",
            "http://proxy.example.com:8080?x=1",
            "http://proxy.example.com:notaport",
            "http://proxy.example.com:0",
            "http://proxy.example.com:65536",
            "http://proxy.example.com:",
            "http://::1",
            "http://[]:7890",
            "http://[::17890",
            "http://prox y:8080",
        ] {
            assert!(
                validate_url(raw).unwrap_err().starts_with("PROXY_INVALID_URL"),
                "{raw} 应被拒绝"
            );
        }
        // 端口可选、IPv6 需方括号、凭据不参与主机校验
        assert_eq!(validate_url("http://proxy.example.com").unwrap(), "http://proxy.example.com");
        assert_eq!(validate_url("https://proxy.corp:8443/").unwrap(), "https://proxy.corp:8443");
        assert_eq!(validate_url("socks5://[::1]:1080").unwrap(), "socks5://[::1]:1080");
        assert_eq!(
            validate_url("http://user:pass@proxy.corp:8080").unwrap(),
            "http://user:pass@proxy.corp:8080"
        );
        assert_eq!(validate_url("socks5h://10.0.0.2:1080").unwrap(), "socks5h://10.0.0.2:1080");
    }

    #[test]
    fn parse_bypass_splits_and_dedups() {
        assert_eq!(
            parse_bypass("localhost, .internal; 10.0.0.0/8 ,, .internal"),
            vec!["localhost", ".internal", "10.0.0.0/8"]
        );
        assert!(parse_bypass("   ").is_empty());
    }

    #[test]
    fn effective_bypass_always_includes_loopback() {
        let spec = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("http://127.0.0.1:7890".into()),
            bypass: vec![".internal".into()],
        };
        let list = spec.effective_bypass();
        assert_eq!(list[0], ".internal");
        for host in ALWAYS_BYPASS {
            assert!(list.iter().any(|i| i == host), "{host} 应始终直连");
        }
        // 用户重复填写回环不产生第二份
        let dup = ProxySpec {
            bypass: vec!["localhost".into(), "localhost".into()],
            ..spec
        };
        assert_eq!(dup.effective_bypass().iter().filter(|i| **i == "localhost").count(), 1);
    }

    #[test]
    fn env_vars_per_mode() {
        assert!(ProxySpec::default().env_vars().is_empty(), "跟随系统不臆造变量");

        let off = ProxySpec {
            mode: ProxyMode::Disabled,
            ..Default::default()
        };
        let vars = off.env_vars();
        assert!(vars.iter().any(|(k, v)| k == "HTTP_PROXY" && v.is_empty()));
        assert!(vars.iter().any(|(k, v)| k == "NO_PROXY" && v == "*"));

        let custom = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("http://proxy.corp:3128".into()),
            bypass: vec![".corp".into()],
        };
        let vars = custom.env_vars();
        assert!(vars.iter().all(|(_, v)| !v.is_empty()));
        assert!(vars.iter().any(|(k, v)| k == "HTTPS_PROXY" && v == "http://proxy.corp:3128"));
        let no_proxy = vars
            .iter()
            .find(|(k, _)| k == "NO_PROXY")
            .map(|(_, v)| v.clone())
            .unwrap();
        assert!(no_proxy.contains(".corp") && no_proxy.contains("127.0.0.1") && no_proxy.contains("localhost"));
    }

    #[test]
    fn fingerprint_tracks_mode_url_and_bypass() {
        let a = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("http://p1:1".into()),
            bypass: vec!["x".into()],
        };
        let b = ProxySpec {
            url: Some("http://p2:1".into()),
            ..a.clone()
        };
        assert_eq!(a.fingerprint(), a.fingerprint());
        assert_ne!(a.fingerprint(), b.fingerprint());
        assert_ne!(a.fingerprint(), ProxySpec::default().fingerprint());
    }

    #[test]
    fn spec_reads_settings_kv() {
        let (store, dir) = temp_store("read");
        assert_eq!(spec(&store).unwrap(), ProxySpec::default());

        put(&store, SETTING_KEY_MODE, json!("自定义"));
        // 自定义但未填地址：如实报错，不静默当作跟随系统
        assert!(spec(&store).unwrap_err().starts_with("PROXY_URL_REQUIRED"));

        put(&store, SETTING_KEY_URL, json!("http://10.0.0.9:7890"));
        put(&store, SETTING_KEY_BYPASS, json!("*.corp.internal"));
        let s = spec(&store).unwrap();
        assert_eq!(s.mode, ProxyMode::Custom);
        assert_eq!(s.url.as_deref(), Some("http://10.0.0.9:7890"));
        assert_eq!(s.bypass, vec!["*.corp.internal"]);

        put(&store, SETTING_KEY_MODE, json!("不使用"));
        let s = spec(&store).unwrap();
        assert_eq!(s.mode, ProxyMode::Disabled);
        assert!(s.url.is_none(), "非自定义模式不应携带地址");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn spec_from_patch_validates_final_state_before_write() {
        let (store, dir) = temp_store("patch");
        // 与代理无关的补丁直接放行
        let unrelated: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(r#"{"theme":"dark"}"#).unwrap();
        assert!(spec_from_patch(&store, &unrelated).is_ok());

        // 同一批只写 proxyMode=自定义、地址在另一批 → 落库前就应失败
        let mode_only: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(r#"{"proxyMode":"自定义"}"#).unwrap();
        assert!(spec_from_patch(&store, &mode_only)
            .unwrap_err()
            .starts_with("PROXY_URL_REQUIRED"));

        // 同批补齐地址 → 通过，且库里仍未写入（调用方拿到 Err 即中止）
        let both: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            r#"{"proxyMode":"自定义","proxyUrl":"http://10.0.0.9:7890","proxyBypass":"*.corp"}"#,
        )
        .unwrap();
        spec_from_patch(&store, &both).unwrap();
        assert!(spec(&store).unwrap().url.is_none(), "预校验不应产生写副作用");

        // 已落库的自定义配置，本批只改地址时按库内 mode 判定
        put(&store, SETTING_KEY_MODE, json!("自定义"));
        put(&store, SETTING_KEY_URL, json!("http://10.0.0.9:7890"));
        let url_only: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(r#"{"proxyUrl":"socks5://10.0.0.1:1080"}"#).unwrap();
        spec_from_patch(&store, &url_only).unwrap();
        let bad_url: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(r#"{"proxyUrl":"no-scheme"}"#).unwrap();
        assert!(spec_from_patch(&store, &bad_url)
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));

        // 切回跟随系统时不需要地址（清空地址也应放行）
        let clear: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            r#"{"proxyMode":"跟随系统","proxyUrl":"","proxyBypass":""}"#,
        )
        .unwrap();
        spec_from_patch(&store, &clear).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn apply_builds_client_for_each_mode() {
        let builder = reqwest::Client::builder();
        let client = ProxySpec::default().apply(builder).unwrap().build().unwrap();
        assert!(client
            .get("http://127.0.0.1:1/health")
            .build()
            .is_ok());

        let custom = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("http://127.0.0.1:7890".into()),
            bypass: vec![".corp".into()],
        };
        assert!(custom.apply(reqwest::Client::builder()).unwrap().build().is_ok());

        let missing = ProxySpec {
            mode: ProxyMode::Custom,
            url: None,
            bypass: Vec::new(),
        };
        assert!(missing
            .apply(reqwest::Client::builder())
            .unwrap_err()
            .starts_with("PROXY_URL_REQUIRED"));

        let bad = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("not a url".into()),
            bypass: Vec::new(),
        };
        assert!(bad
            .apply(reqwest::Client::builder())
            .unwrap_err()
            .starts_with("PROXY_INVALID_URL"));
    }

    #[test]
    fn publish_and_current_roundtrip() {
        let spec = ProxySpec {
            mode: ProxyMode::Disabled,
            url: None,
            bypass: vec!["corp".into()],
        };
        publish(spec.clone());
        assert_eq!(current(), spec);
        publish(ProxySpec::default());
        assert_eq!(current(), ProxySpec::default());
    }

    #[test]
    fn client_for_reuses_cache_by_fingerprint() {
        let spec = ProxySpec {
            mode: ProxyMode::Custom,
            url: Some("http://127.0.0.1:7890".into()),
            bypass: vec![".corp".into()],
        };
        let first = client_for(&spec).unwrap();
        let second = client_for(&spec).unwrap();
        assert!(first.get("http://127.0.0.1:1/").build().is_ok());
        assert!(second.get("http://127.0.0.1:1/").build().is_ok());

        // 换一个指纹（不同代理地址）→ 重建，仍可用
        let other = ProxySpec {
            url: Some("http://10.0.0.9:7890".into()),
            ..spec.clone()
        };
        assert!(client_for(&other).unwrap().get("http://127.0.0.1:1/").build().is_ok());

        // 自定义模式缺地址：如实报错（地址格式校验在 validate_url，落库前已拦下）
        let missing = ProxySpec {
            mode: ProxyMode::Custom,
            url: None,
            bypass: Vec::new(),
        };
        assert!(client_for(&missing)
            .unwrap_err()
            .starts_with("PROXY_URL_REQUIRED"));

        // 兜底 client 宁可直连，也不走未配置成功的代理
        publish(missing);
        assert!(client().get("http://127.0.0.1:1/").build().is_ok());
        publish(ProxySpec::default());

        assert_eq!(mode_label(ProxyMode::System), "跟随系统");
        assert_eq!(mode_label(ProxyMode::Custom), "自定义");
        assert_eq!(mode_label(ProxyMode::Disabled), "不使用");
    }

    #[test]
    fn spec_or_system_falls_back_on_invalid_custom() {
        let (store, dir) = temp_store("fallback");
        put(&store, SETTING_KEY_MODE, json!("自定义"));
        assert!(spec(&store).is_err());
        assert_eq!(spec_or_system(&store), ProxySpec::default());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
