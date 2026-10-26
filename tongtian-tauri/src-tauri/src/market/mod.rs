//! 在线技能 / 专家套件市场客户端（可配置索引源）
//!
//! command：
//! - market_config_get / market_config_set：读取 / 保存两个索引地址
//! - market_skill_list / market_suite_list：拉取并校验索引，合并本机已安装状态
//! - market_skill_install / market_suite_install：按 itemId 重新拉索引，下载并校验后再复用
//!   本地的 skill::install_content_in / suite::install_bytes_in 落盘
//!
//! 安全边界：
//! - 远程地址只允许 HTTPS；仅本机回环允许 HTTP（自建/联调市场）
//! - 拒绝 URL 内嵌凭据、拒绝重定向（防止 302 跳到内网或未知主机）
//! - 远程主机解析结果必须全部是公网地址；下载地址同样复核，不允许索引指向本机/内网
//! - 索引、SKILL.md、套件包都有大小上限；下载内容必须与索引声明的 SHA-256 一致
//! - 安装只接受 itemId，下载地址始终由本机重新拉取索引得到，渲染层不能注入任意 URL
//! - 发布者签名：索引可声明 `publishers` 与条目 `publisherId` + Ed25519 `signature`；
//!   签名覆盖「kind / publisherId / itemId / version / sha256」，安装前验签
//! - 信任锚在本机：`market.trustedPublishers` 保存用户明确信任的发布者公钥；
//!   未信任或同一发布者换钥都会拒绝安装，索引自己声明公钥不能替代本机信任

use std::collections::{HashMap, HashSet};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use ring::signature::{UnparsedPublicKey, ED25519};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::State;

use crate::bridge::python_env;
use crate::security::audit;
use crate::store::{settings, Store};
use crate::{skill, suite, P0Services};

/// 市场索引源在 settings_kv 中的键（值为 JSON 对象，含两个独立地址）
const CONFIG_KEY: &str = "market.sources";
/// 本机信任的发布者公钥（值为 JSON 数组，用户明确添加后才会写入）
const TRUST_KEY: &str = "market.trustedPublishers";
const SIGNING_DOMAIN: &str = "tt-market-v1";
const MAX_PUBLISHERS: usize = 200;
const MAX_PUBLISHER_ID_CHARS: usize = 64;

const INDEX_MAX_BYTES: usize = 1024 * 1024;
const INDEX_MAX_ITEMS: usize = 500;
const SKILL_MAX_BYTES: u64 = skill::MAX_SKILL_MD_BYTES;
const SUITE_MAX_BYTES: u64 = suite::MAX_ZIP_BYTES as u64;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_URL_LEN: usize = 2048;

// ── 配置 ────────────────────────────────────────────────────────

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Sources {
    #[serde(default)]
    skill_index_url: Option<String>,
    #[serde(default)]
    suite_index_url: Option<String>,
}

/// 三态补丁：缺省不动、null 清除、字符串覆盖
#[derive(Debug, Clone)]
pub enum Patch<T> {
    Missing,
    Clear,
    Set(T),
}

impl<T> Default for Patch<T> {
    fn default() -> Self {
        Self::Missing
    }
}

impl<'de, T: Deserialize<'de>> Deserialize<'de> for Patch<T> {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Ok(match Option::<T>::deserialize(deserializer)? {
            Some(value) => Patch::Set(value),
            None => Patch::Clear,
        })
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigPatch {
    #[serde(default)]
    pub skill_index_url: Patch<String>,
    #[serde(default)]
    pub suite_index_url: Patch<String>,
}

fn read_sources(store: &Store) -> Sources {
    let mut sources = settings::get(store, CONFIG_KEY)
        .ok()
        .flatten()
        .and_then(|raw| serde_json::from_str::<Sources>(&raw).ok())
        .unwrap_or_default();
    // 历史空串按未配置处理，避免把空地址送去 URL 解析
    sources.skill_index_url = sources
        .skill_index_url
        .and_then(|v| non_empty(&v).map(str::to_owned));
    sources.suite_index_url = sources
        .suite_index_url
        .and_then(|v| non_empty(&v).map(str::to_owned));
    sources
}

fn config_view(store: &Store) -> Value {
    let sources = read_sources(store);
    json!({
        "supported": true,
        "skillIndexUrl": sources.skill_index_url,
        "suiteIndexUrl": sources.suite_index_url,
    })
}

fn empty_list() -> Value {
    json!({
        "supported": true,
        "configured": false,
        "indexUrl": Value::Null,
        "fetchedAtMs": 0,
        "signaturePolicy": Value::Null,
        "publishers": [],
        "items": [],
    })
}

fn non_empty(raw: &str) -> Option<&str> {
    let trimmed = raw.trim();
    (!trimmed.is_empty()).then_some(trimmed)
}

fn apply_patch(current: Option<String>, patch: Patch<String>) -> Result<Option<String>, String> {
    match patch {
        Patch::Missing => Ok(current),
        Patch::Clear => Ok(None),
        Patch::Set(raw) => match non_empty(&raw) {
            None => Ok(None),
            Some(url) => normalize_index_url(url).map(Some),
        },
    }
}

// ── URL 与网络边界 ──────────────────────────────────────────────

fn invalid_url(detail: &str) -> String {
    format!("MARKET_INVALID_URL: {detail}")
}

/// 索引地址：远程必须 HTTPS，本机回环允许 HTTP；不接受 URL 内嵌凭据
fn normalize_index_url(raw: &str) -> Result<String, String> {
    if raw.len() > MAX_URL_LEN {
        return Err(invalid_url("地址过长"));
    }
    let mut url = reqwest::Url::parse(raw)
        .map_err(|_| invalid_url("无法解析该地址，请填写完整的 http(s) URL"))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(invalid_url("地址里不能包含用户名或密码"));
    }
    url.set_fragment(None);
    if url.host_str().is_none() {
        return Err(invalid_url("地址缺少主机名"));
    }
    let local = host_is_loopback(&url);
    match url.scheme() {
        "https" => {}
        "http" if local => {}
        "http" => {
            return Err(invalid_url(
                "远程市场必须使用 HTTPS；本机调试可使用 http://127.0.0.1 或 http://localhost",
            ))
        }
        _ => return Err(invalid_url("只支持 http / https 地址")),
    }
    Ok(url.to_string())
}

/// 下载地址：索引在远端时只允许公网 HTTPS；索引在本机时只允许回环地址
fn parse_download_url(raw: &str, local_index: bool) -> Result<reqwest::Url, String> {
    if raw.len() > MAX_URL_LEN {
        return Err(invalid_url("下载地址过长"));
    }
    let mut url = reqwest::Url::parse(raw)
        .map_err(|_| invalid_url("索引中的下载地址不是完整 URL"))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(invalid_url("下载地址不能包含用户名或密码"));
    }
    url.set_fragment(None);
    if url.host_str().is_none() {
        return Err(invalid_url("下载地址缺少主机名"));
    }
    let local = host_is_loopback(&url);
    if local_index {
        if !local {
            return Err(invalid_url(
                "本机市场索引不能把下载地址指向外部主机，已拒绝",
            ));
        }
        if !matches!(url.scheme(), "http" | "https") {
            return Err(invalid_url("下载地址只支持 http / https"));
        }
        return Ok(url);
    }
    if local {
        return Err(invalid_url("远端市场索引不能把下载地址指向本机，已拒绝"));
    }
    if url.scheme() != "https" {
        return Err(invalid_url("远端市场的下载地址必须使用 HTTPS"));
    }
    Ok(url)
}

fn host_is_loopback(url: &reqwest::Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    match bare_host(host).parse::<IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        Err(_) => false,
    }
}

/// `Url::host_str` 会给 IPv6 主机带上方括号，解析 IpAddr 前先剥掉
fn bare_host(host: &str) -> &str {
    host.strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .unwrap_or(host)
}

fn host_label(url: &reqwest::Url) -> String {
    url.host_str().unwrap_or("未知主机").to_string()
}

fn ip_is_public(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => ipv4_is_public(v4),
        IpAddr::V6(v6) => ipv6_is_public(v6),
    }
}

fn ipv4_is_public(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    !(ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_documentation()
        || ip.is_unspecified()
        || ip.is_multicast()
        || o[0] == 0
        || (o[0] == 100 && (64..=127).contains(&o[1]))
        || (o[0] == 198 && (o[1] == 18 || o[1] == 19))
        || o[0] >= 240)
}

fn ipv6_is_public(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return ipv4_is_public(v4);
    }
    !(ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || ip.is_unique_local()
        || ip.is_unicast_link_local())
}

/// 解析主机并确认全部地址都是公网；回环主机只在 local_allowed 时放行
async fn ensure_target_allowed(url: &reqwest::Url, local_allowed: bool) -> Result<(), String> {
    if host_is_loopback(url) {
        return if local_allowed {
            Ok(())
        } else {
            Err(invalid_url("该市场地址指向本机，已拒绝"))
        };
    }
    let host = url
        .host_str()
        .ok_or_else(|| invalid_url("市场地址缺少主机名"))?;
    let host = bare_host(host);
    if let Ok(ip) = host.parse::<IpAddr>() {
        return if ip_is_public(ip) {
            Ok(())
        } else {
            Err(invalid_url("市场地址是内网或保留地址，已拒绝"))
        };
    }
    let port = url.port_or_known_default().unwrap_or(443);
    let addrs: Vec<SocketAddr> = tokio::net::lookup_host((host, port))
        .await
        .map_err(|_| "MARKET_FETCH_FAILED: 无法解析市场主机名，请检查网络或 DNS 设置".to_string())?
        .collect();
    if addrs.is_empty() {
        return Err("MARKET_FETCH_FAILED: 市场主机没有可用的解析结果".to_string());
    }
    if addrs.iter().any(|addr| !ip_is_public(addr.ip())) {
        return Err(invalid_url("市场主机解析到内网或保留地址，已拒绝"));
    }
    Ok(())
}

fn build_client(local: bool) -> Result<reqwest::Client, String> {
    let builder = reqwest::Client::builder()
        // 不让 302 把请求带到未经校验的主机；市场索引应直接填写最终地址
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .user_agent("TongTianOffice-Market/1.0");
    let builder = if local {
        // 本机市场永远直连，避免系统代理把回环请求带走
        builder.no_proxy()
    } else {
        crate::proxy::current().apply(builder)?
    };
    builder
        .build()
        .map_err(|e| format!("MARKET_FETCH_FAILED: 无法创建网络客户端（{e}）"))
}

fn network_reason(err: &reqwest::Error) -> &'static str {
    if err.is_timeout() {
        "连接或读取超时"
    } else if err.is_connect() {
        "无法建立连接"
    } else if err.is_body() {
        "读取响应失败"
    } else {
        "网络请求失败"
    }
}

/// 按上限流式读取响应体，避免无 Content-Length 时无限读入内存
async fn fetch_url_bytes(
    url: &reqwest::Url,
    max: usize,
    local: bool,
) -> Result<Vec<u8>, String> {
    let client = build_client(local)?;
    let resp = client
        .get(url.clone())
        .send()
        .await
        .map_err(|e| format!("MARKET_FETCH_FAILED: {0}（{1}）", network_reason(&e), host_label(url)))?;
    let status = resp.status();
    if status.is_redirection() {
        return Err(format!(
            "MARKET_HTTP_ERROR: 市场源返回重定向（HTTP {}），为避免跳到未经校验的地址已拒绝；请直接填写最终索引地址",
            status.as_u16()
        ));
    }
    if !status.is_success() {
        return Err(format!(
            "MARKET_HTTP_ERROR: 市场源返回 HTTP {}（{}）",
            status.as_u16(),
            host_label(url)
        ));
    }
    if let Some(len) = resp.content_length() {
        if len > max as u64 {
            return Err(format!(
                "MARKET_TOO_LARGE: 市场返回内容约 {}KB，超过 {}KB 上限",
                len / 1024,
                max / 1024
            ));
        }
    }
    let mut resp = resp;
    let mut out: Vec<u8> = Vec::new();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| format!("MARKET_FETCH_FAILED: {0}（{1}）", network_reason(&e), host_label(url)))?
    {
        if out.len().saturating_add(chunk.len()) > max {
            return Err(format!(
                "MARKET_TOO_LARGE: 市场返回内容超过 {}KB 上限",
                max / 1024
            ));
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

// ── 索引模型与解析 ──────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SignaturePolicy {
    Required,
    Optional,
}

impl SignaturePolicy {
    fn as_str(self) -> &'static str {
        match self {
            Self::Required => "required",
            Self::Optional => "optional",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawPublisher {
    id: String,
    #[serde(default)]
    name: Option<String>,
    public_key: String,
}

/// 索引声明的发布者。公钥只在内存与本机信任库里使用，列表只下发公钥与指纹。
#[derive(Debug, Clone)]
struct Publisher {
    id: String,
    name: Option<String>,
    public_key: Vec<u8>,
    public_key_b64: String,
    fingerprint: String,
}

/// 本机信任锚：发布者 id 与公钥必须同时匹配，换钥不会自动继承信任。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TrustedPublisher {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    pub public_key: String,
    #[serde(default)]
    pub added_at_ms: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PublisherTrust {
    Trusted,
    Untrusted,
    KeyChanged,
}

#[derive(Debug)]
struct ParsedIndex<T> {
    items: Vec<T>,
    publishers: Vec<Publisher>,
    policy: SignaturePolicy,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawSkillItem {
    id: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    author: Option<String>,
    #[serde(default)]
    homepage: Option<String>,
    skill_md_url: String,
    sha256: String,
    #[serde(default)]
    size_bytes: Option<u64>,
    #[serde(default)]
    publisher_id: Option<String>,
    #[serde(default)]
    signature: Option<String>,
}

#[derive(Debug, Clone)]
struct SkillItem {
    id: String,
    name: String,
    description: Option<String>,
    version: Option<String>,
    author: Option<String>,
    homepage: Option<String>,
    skill_md_url: String,
    sha256: String,
    size_bytes: Option<u64>,
    publisher: Option<Publisher>,
    signature: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawSuiteItem {
    id: String,
    name: String,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    author: Option<String>,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    homepage: Option<String>,
    zip_url: String,
    sha256: String,
    #[serde(default)]
    size_bytes: Option<u64>,
    #[serde(default)]
    published_at_ms: Option<u64>,
    #[serde(default)]
    skill_count: Option<u64>,
    #[serde(default)]
    command_count: Option<u64>,
    #[serde(default)]
    data_link_count: Option<u64>,
    #[serde(default)]
    publisher_id: Option<String>,
    #[serde(default)]
    signature: Option<String>,
}

#[derive(Debug, Clone)]
struct SuiteItem {
    id: String,
    name: String,
    description: Option<String>,
    version: Option<String>,
    author: Option<String>,
    category: Option<String>,
    homepage: Option<String>,
    zip_url: String,
    sha256: String,
    size_bytes: Option<u64>,
    published_at_ms: Option<u64>,
    skill_count: Option<u64>,
    command_count: Option<u64>,
    data_link_count: Option<u64>,
    publisher: Option<Publisher>,
    signature: Option<String>,
}

#[derive(Debug)]
struct FetchedIndex<T> {
    url: String,
    local: bool,
    items: Vec<T>,
    publishers: Vec<Publisher>,
    policy: SignaturePolicy,
}

fn parse_index_doc(bytes: &[u8], key: &str) -> Result<(Vec<Value>, Vec<Publisher>, SignaturePolicy), String> {
    let root: Value = serde_json::from_slice(bytes)
        .map_err(|e| format!("MARKET_INVALID_INDEX: 索引不是合法 JSON（{e}）"))?;
    let policy = match root.get("signaturePolicy").and_then(Value::as_str) {
        None | Some("optional") => SignaturePolicy::Optional,
        Some("required") => SignaturePolicy::Required,
        Some(other) => {
            return Err(format!(
                "MARKET_INVALID_INDEX: signaturePolicy 只能是 \"required\" 或 \"optional\"，收到「{other}」"
            ))
        }
    };
    let publishers = match root.get("publishers") {
        None => Vec::new(),
        Some(value) => {
            let raws: Vec<RawPublisher> = serde_json::from_value(value.clone())
                .map_err(|e| format!("MARKET_INVALID_INDEX: publishers 字段不合法（{e}）"))?;
            if raws.len() > MAX_PUBLISHERS {
                return Err(format!(
                    "MARKET_INVALID_INDEX: 发布者过多（{} 项，上限 {}）",
                    raws.len(),
                    MAX_PUBLISHERS
                ));
            }
            let mut seen = HashSet::new();
            let mut out = Vec::with_capacity(raws.len());
            for raw in raws {
                let id = normalize_publisher_id(&raw.id)
                    .map_err(|e| format!("MARKET_INVALID_INDEX: {e}"))?;
                if !seen.insert(id.clone()) {
                    return Err(format!("MARKET_INVALID_INDEX: 发布者标识重复：{id}"));
                }
                let (public_key, public_key_b64) = normalize_public_key(&raw.public_key)
                    .map_err(|e| format!("MARKET_INVALID_INDEX: 发布者「{id}」{e}"))?;
                out.push(Publisher {
                    id,
                    name: clean_text(raw.name, 120),
                    fingerprint: sha256_hex(&public_key),
                    public_key,
                    public_key_b64,
                });
            }
            out
        }
    };
    let items = root
        .get(key)
        .and_then(Value::as_array)
        // 兼容直接返回数组的静态索引
        .or_else(|| root.as_array())
        .ok_or_else(|| format!("MARKET_INVALID_INDEX: 索引缺少 {key} 数组"))?;
    if items.len() > INDEX_MAX_ITEMS {
        return Err(format!(
            "MARKET_INVALID_INDEX: 索引条目过多（{} 项，上限 {}）",
            items.len(),
            INDEX_MAX_ITEMS
        ));
    }
    Ok((items.clone(), publishers, policy))
}

fn clean_text(raw: Option<String>, max_chars: usize) -> Option<String> {
    raw.map(|v| v.trim().chars().take(max_chars).collect::<String>())
        .filter(|v| !v.is_empty())
}

fn parse_homepage(raw: Option<String>) -> Option<String> {
    let raw = clean_text(raw, MAX_URL_LEN)?;
    let url = reqwest::Url::parse(&raw).ok()?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return None;
    }
    if !url.username().is_empty() || url.password().is_some() {
        return None;
    }
    Some(url.to_string())
}

fn normalize_hash(raw: &str, index: usize, kind: &str) -> Result<String, String> {
    let hash = raw.trim().to_ascii_lowercase();
    if hash.len() != 64 || !hash.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(format!(
            "MARKET_INVALID_INDEX: 第 {} 个{kind}的 sha256 必须是 64 位十六进制字符串",
            index + 1
        ));
    }
    Ok(hash)
}

fn normalize_publisher_id(raw: &str) -> Result<String, String> {
    let id = raw.trim().to_ascii_lowercase();
    if id.is_empty() {
        return Err("发布者标识不能为空".to_string());
    }
    if id.chars().count() > MAX_PUBLISHER_ID_CHARS {
        return Err(format!("发布者标识过长（上限 {MAX_PUBLISHER_ID_CHARS} 字符）"));
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        return Err("发布者标识只能包含字母、数字、点、下划线与连字符".to_string());
    }
    if id.starts_with('.') || id.starts_with('-') {
        return Err("发布者标识不能以点或连字符开头".to_string());
    }
    Ok(id)
}

fn decode_base64(raw: &str) -> Option<Vec<u8>> {
    use base64::engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD};
    let compact: String = raw.chars().filter(|c| !c.is_whitespace()).collect();
    if compact.is_empty() {
        return None;
    }
    for engine in [&STANDARD, &STANDARD_NO_PAD, &URL_SAFE, &URL_SAFE_NO_PAD] {
        if let Ok(bytes) = engine.decode(compact.as_bytes()) {
            return Some(bytes);
        }
    }
    None
}

fn normalize_public_key(raw: &str) -> Result<(Vec<u8>, String), String> {
    let bytes = decode_base64(raw)
        .ok_or_else(|| "公钥不是合法的 base64".to_string())?;
    if bytes.len() != 32 {
        return Err(format!(
            "公钥必须是 32 字节 Ed25519 公钥（实际 {} 字节）",
            bytes.len()
        ));
    }
    let encoded = base64::engine::general_purpose::STANDARD_NO_PAD.encode(&bytes);
    Ok((bytes, encoded))
}

fn normalize_signature(raw: &str, index: usize, kind: &str) -> Result<String, String> {
    let bytes = decode_base64(raw).ok_or_else(|| {
        format!(
            "MARKET_INVALID_INDEX: 第 {} 个{kind}的 signature 不是合法 base64",
            index + 1
        )
    })?;
    if bytes.len() != 64 {
        return Err(format!(
            "MARKET_INVALID_INDEX: 第 {} 个{kind}的 signature 必须是 64 字节 Ed25519 签名",
            index + 1
        ));
    }
    Ok(base64::engine::general_purpose::STANDARD_NO_PAD.encode(bytes))
}

/// 签名覆盖范围：域名 + 类型 + 发布者 + 条目 + 版本 + 产物哈希。
/// 名称/描述等展示字段不参与，避免索引改文案就导致旧签名失效。
fn signing_payload(
    kind: &str,
    publisher_id: &str,
    item_id: &str,
    version: &str,
    sha256: &str,
) -> Vec<u8> {
    format!("{SIGNING_DOMAIN}\n{kind}\n{publisher_id}\n{item_id}\n{version}\n{sha256}").into_bytes()
}

fn attach_signature(
    index: usize,
    kind: &str,
    publisher_id: Option<String>,
    signature: Option<String>,
    publishers: &[Publisher],
    policy: SignaturePolicy,
) -> Result<(Option<Publisher>, Option<String>), String> {
    match (publisher_id, signature) {
        (None, None) => {
            if policy == SignaturePolicy::Required {
                return Err(format!(
                    "MARKET_INVALID_INDEX: 第 {} 个{kind}没有发布者签名，但索引要求签名（signaturePolicy=required）",
                    index + 1
                ));
            }
            Ok((None, None))
        }
        (Some(_), None) | (None, Some(_)) => Err(format!(
            "MARKET_INVALID_INDEX: 第 {} 个{kind}的 publisherId 与 signature 必须同时提供",
            index + 1
        )),
        (Some(raw_id), Some(raw_sig)) => {
            let id = normalize_publisher_id(&raw_id)
                .map_err(|e| format!("MARKET_INVALID_INDEX: 第 {} 个{kind}的发布者标识非法（{e}）", index + 1))?;
            let publisher = publishers
                .iter()
                .find(|p| p.id == id)
                .ok_or_else(|| {
                    format!(
                        "MARKET_INVALID_INDEX: 第 {} 个{kind}引用了未知发布者「{id}」",
                        index + 1
                    )
                })?
                .clone();
            let signature = normalize_signature(&raw_sig, index, kind)?;
            Ok((Some(publisher), Some(signature)))
        }
    }
}

fn item_id(raw: &str, index: usize, kind: &str, normalize: fn(&str) -> Result<String, String>) -> Result<String, String> {
    normalize(raw).map_err(|e| {
        let reason = e
            .split_once(": ")
            .map(|(_, detail)| detail)
            .unwrap_or(e.as_str());
        format!("MARKET_INVALID_INDEX: 第 {} 个{kind}的标识非法（{reason}）", index + 1)
    })
}

fn parse_skill_items(bytes: &[u8], local_index: bool) -> Result<ParsedIndex<SkillItem>, String> {
    let (raw_items, publishers, policy) = parse_index_doc(bytes, "skills")?;
    let mut seen = HashSet::new();
    let mut out = Vec::with_capacity(raw_items.len());
    for (idx, value) in raw_items.into_iter().enumerate() {
        let raw: RawSkillItem = serde_json::from_value(value)
            .map_err(|e| format!("MARKET_INVALID_INDEX: 第 {} 个技能字段不完整（{e}）", idx + 1))?;
        let id = item_id(&raw.id, idx, "技能", skill::normalize_id)?;
        if !seen.insert(id.clone()) {
            return Err(format!("MARKET_INVALID_INDEX: 技能标识重复：{id}"));
        }
        if let Some(size) = raw.size_bytes {
            if size > SKILL_MAX_BYTES {
                return Err(format!(
                    "MARKET_INVALID_INDEX: 技能「{id}」声明大小超过 {}KB 上限",
                    SKILL_MAX_BYTES / 1024
                ));
            }
        }
        // 列表阶段就校验下载地址形态，避免用户点安装才发现索引写错
        parse_download_url(&raw.skill_md_url, local_index)?;
        let (publisher, signature) = attach_signature(
            idx,
            "技能",
            raw.publisher_id,
            raw.signature,
            &publishers,
            policy,
        )?;
        let name = clean_text(raw.name, 200).unwrap_or_else(|| id.clone());
        out.push(SkillItem {
            id,
            name,
            description: clean_text(raw.description, 1000),
            version: clean_text(raw.version, 64),
            author: clean_text(raw.author, 120),
            homepage: parse_homepage(raw.homepage),
            skill_md_url: raw.skill_md_url,
            sha256: normalize_hash(&raw.sha256, idx, "技能")?,
            size_bytes: raw.size_bytes,
            publisher,
            signature,
        });
    }
    Ok(ParsedIndex {
        items: out,
        publishers,
        policy,
    })
}

fn parse_suite_items(bytes: &[u8], local_index: bool) -> Result<ParsedIndex<SuiteItem>, String> {
    let (raw_items, publishers, policy) = parse_index_doc(bytes, "suites")?;
    let mut seen = HashSet::new();
    let mut out = Vec::with_capacity(raw_items.len());
    for (idx, value) in raw_items.into_iter().enumerate() {
        let raw: RawSuiteItem = serde_json::from_value(value)
            .map_err(|e| format!("MARKET_INVALID_INDEX: 第 {} 个套件字段不完整（{e}）", idx + 1))?;
        let id = item_id(&raw.id, idx, "套件", suite::normalize_id)?;
        if !seen.insert(id.clone()) {
            return Err(format!("MARKET_INVALID_INDEX: 套件标识重复：{id}"));
        }
        if let Some(size) = raw.size_bytes {
            if size > SUITE_MAX_BYTES {
                return Err(format!(
                    "MARKET_INVALID_INDEX: 套件「{id}」声明大小超过 {}MB 上限",
                    SUITE_MAX_BYTES / 1024 / 1024
                ));
            }
        }
        parse_download_url(&raw.zip_url, local_index)?;
        let (publisher, signature) = attach_signature(
            idx,
            "套件",
            raw.publisher_id,
            raw.signature,
            &publishers,
            policy,
        )?;
        let name = clean_text(Some(raw.name), 200)
            .ok_or_else(|| format!("MARKET_INVALID_INDEX: 第 {} 个套件缺少展示名称", idx + 1))?;
        out.push(SuiteItem {
            id,
            name,
            description: clean_text(raw.description, 1000),
            version: clean_text(raw.version, 64),
            author: clean_text(raw.author, 120),
            category: clean_text(raw.category, 40),
            homepage: parse_homepage(raw.homepage),
            zip_url: raw.zip_url,
            sha256: normalize_hash(&raw.sha256, idx, "套件")?,
            size_bytes: raw.size_bytes,
            published_at_ms: raw.published_at_ms,
            skill_count: raw.skill_count,
            command_count: raw.command_count,
            data_link_count: raw.data_link_count,
            publisher,
            signature,
        });
    }
    Ok(ParsedIndex {
        items: out,
        publishers,
        policy,
    })
}

async fn fetch_index<T, F>(raw_url: &str, parse: F) -> Result<FetchedIndex<T>, String>
where
    F: FnOnce(&[u8], bool) -> Result<ParsedIndex<T>, String>,
{
    let normalized = normalize_index_url(raw_url)?;
    let url = reqwest::Url::parse(&normalized)
        .map_err(|_| invalid_url("无法解析市场索引地址"))?;
    let local = host_is_loopback(&url);
    ensure_target_allowed(&url, local).await?;
    let bytes = fetch_url_bytes(&url, INDEX_MAX_BYTES, local).await?;
    let parsed = parse(&bytes, local)?;
    Ok(FetchedIndex {
        url: normalized,
        local,
        items: parsed.items,
        publishers: parsed.publishers,
        policy: parsed.policy,
    })
}

async fn fetch_skill_index(raw_url: &str) -> Result<FetchedIndex<SkillItem>, String> {
    fetch_index(raw_url, parse_skill_items).await
}

async fn fetch_suite_index(raw_url: &str) -> Result<FetchedIndex<SuiteItem>, String> {
    fetch_index(raw_url, parse_suite_items).await
}

// ── 发布者信任与验签 ────────────────────────────────────────────

fn load_trust(store: &Store) -> Vec<TrustedPublisher> {
    let raw = settings::get(store, TRUST_KEY).ok().flatten();
    let mut out: Vec<TrustedPublisher> = raw
        .and_then(|text| serde_json::from_str::<Vec<TrustedPublisher>>(&text).ok())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|entry| {
            let id = normalize_publisher_id(&entry.id).ok()?;
            let (_, public_key) = normalize_public_key(&entry.public_key).ok()?;
            Some(TrustedPublisher {
                id,
                name: clean_text(entry.name, 120),
                public_key,
                added_at_ms: entry.added_at_ms,
            })
        })
        .collect();
    out.sort_by(|a, b| a.id.cmp(&b.id));
    out.dedup_by(|a, b| a.id == b.id);
    out
}

fn save_trust(store: &Store, list: &[TrustedPublisher]) -> Result<(), String> {
    let encoded = serde_json::to_string(list)
        .map_err(|e| format!("MARKET_TRUST_SAVE_FAILED: 发布者信任序列化失败（{e}）"))?;
    settings::set(store, TRUST_KEY, &encoded)
}

fn trust_status(publisher: &Publisher, trust: &[TrustedPublisher]) -> PublisherTrust {
    match trust.iter().find(|entry| entry.id == publisher.id) {
        None => PublisherTrust::Untrusted,
        Some(entry) if entry.public_key == publisher.public_key_b64 => PublisherTrust::Trusted,
        Some(_) => PublisherTrust::KeyChanged,
    }
}

fn short_fingerprint(fingerprint: &str) -> String {
    format!("{}…", &fingerprint[..fingerprint.len().min(16)])
}

fn fingerprint_of_public_key_b64(public_key: &str) -> String {
    decode_base64(public_key)
        .map(|bytes| sha256_hex(&bytes))
        .unwrap_or_default()
}

fn publisher_view(publisher: &Publisher, trust: &[TrustedPublisher]) -> Value {
    let status = trust_status(publisher, trust);
    json!({
        "id": publisher.id,
        "name": publisher.name,
        "publicKey": publisher.public_key_b64,
        "fingerprint": publisher.fingerprint,
        "trusted": status == PublisherTrust::Trusted,
        "keyChanged": status == PublisherTrust::KeyChanged,
    })
}

fn item_signature_view(publisher: &Option<Publisher>, trust: &[TrustedPublisher]) -> Value {
    match publisher {
        None => json!({
            "publisherId": Value::Null,
            "publisherName": Value::Null,
            "publisherFingerprint": Value::Null,
            "signed": false,
            "publisherTrusted": false,
            "publisherKeyChanged": false,
        }),
        Some(p) => {
            let status = trust_status(p, trust);
            json!({
                "publisherId": p.id,
                "publisherName": p.name,
                "publisherFingerprint": p.fingerprint,
                "signed": true,
                "publisherTrusted": status == PublisherTrust::Trusted,
                "publisherKeyChanged": status == PublisherTrust::KeyChanged,
            })
        }
    }
}

fn verify_artifact_signature(
    kind: &str,
    item_id: &str,
    version: &Option<String>,
    sha256: &str,
    publisher: &Option<Publisher>,
    signature: &Option<String>,
    policy: SignaturePolicy,
    trust: &[TrustedPublisher],
) -> Result<String, String> {
    let Some(publisher) = publisher else {
        if policy == SignaturePolicy::Required {
            return Err(format!(
                "MARKET_SIGNATURE_REQUIRED: 索引要求发布者签名，但{kind}「{item_id}」没有签名"
            ));
        }
        return Ok("未签名（索引未强制签名）".to_string());
    };
    match trust_status(publisher, trust) {
        PublisherTrust::Trusted => {}
        PublisherTrust::Untrusted => {
            return Err(format!(
                "MARKET_UNTRUSTED_PUBLISHER: 发布者「{}」的公钥指纹 {} 尚未信任；请先在市场窗口的「发布者信任」中确认并信任该公钥",
                publisher.id,
                short_fingerprint(&publisher.fingerprint)
            ))
        }
        PublisherTrust::KeyChanged => {
            let trusted = trust
                .iter()
                .find(|entry| entry.id == publisher.id)
                .map(|entry| short_fingerprint(&fingerprint_of_public_key_b64(&entry.public_key)))
                .unwrap_or_else(|| "未知".to_string());
            return Err(format!(
                "MARKET_PUBLISHER_KEY_CHANGED: 发布者「{}」的公钥已变化（已信任指纹 {}，当前 {}）；如确认是发布者轮换，请先移除旧信任再添加新公钥",
                publisher.id,
                trusted,
                short_fingerprint(&publisher.fingerprint)
            ));
        }
    }
    let raw_signature = signature
        .as_deref()
        .ok_or_else(|| format!("MARKET_SIGNATURE_REQUIRED: {kind}「{item_id}」缺少签名"))?;
    let signature_bytes = decode_base64(raw_signature)
        .filter(|bytes| bytes.len() == 64)
        .ok_or_else(|| format!("MARKET_SIGNATURE_INVALID: {kind}「{item_id}」的签名不是 64 字节 Ed25519 签名"))?;
    let payload = signing_payload(
        kind,
        &publisher.id,
        item_id,
        version.as_deref().unwrap_or(""),
        sha256,
    );
    UnparsedPublicKey::new(&ED25519, &publisher.public_key)
        .verify(&payload, &signature_bytes)
        .map_err(|_| {
            format!(
                "MARKET_SIGNATURE_INVALID: 发布者「{}」的签名与{kind}「{item_id}」当前内容不匹配，已拒绝安装",
                publisher.id
            )
        })?;
    Ok(format!(
        "发布者 {} 签名已校验（{}）",
        publisher.id,
        short_fingerprint(&publisher.fingerprint)
    ))
}

fn add_trusted_publisher(
    list: &mut Vec<TrustedPublisher>,
    publisher_id: &str,
    public_key: &str,
    name: Option<String>,
    now_ms: u64,
) -> Result<TrustedPublisher, String> {
    let id = normalize_publisher_id(publisher_id)
        .map_err(|e| format!("MARKET_INVALID_PUBLISHER: {e}"))?;
    let (_, public_key_b64) = normalize_public_key(public_key)
        .map_err(|e| format!("MARKET_INVALID_PUBLISHER: {e}"))?;
    if let Some(existing) = list.iter_mut().find(|entry| entry.id == id) {
        if existing.public_key != public_key_b64 {
            return Err(format!(
                "MARKET_PUBLISHER_KEY_CHANGED: 本机已信任发布者「{id}」的另一把公钥（指纹 {}）；如确认是发布者轮换，请先移除旧信任再添加",
                short_fingerprint(&fingerprint_of_public_key_b64(&existing.public_key))
            ));
        }
        if let Some(name) = clean_text(name, 120) {
            existing.name = Some(name);
        }
        return Ok(existing.clone());
    }
    let entry = TrustedPublisher {
        id,
        name: clean_text(name, 120),
        public_key: public_key_b64,
        added_at_ms: now_ms,
    };
    list.push(entry.clone());
    list.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(entry)
}

fn remove_trusted_publisher(list: &mut Vec<TrustedPublisher>, publisher_id: &str) -> Result<bool, String> {
    let id = normalize_publisher_id(publisher_id)
        .map_err(|e| format!("MARKET_INVALID_PUBLISHER: {e}"))?;
    let before = list.len();
    list.retain(|entry| entry.id != id);
    Ok(list.len() != before)
}

fn trust_view(list: &[TrustedPublisher]) -> Value {
    let publishers: Vec<Value> = list
        .iter()
        .map(|entry| {
            json!({
                "id": entry.id,
                "name": entry.name,
                "publicKey": entry.public_key,
                "fingerprint": fingerprint_of_public_key_b64(&entry.public_key),
                "addedAtMs": entry.added_at_ms,
            })
        })
        .collect();
    json!({ "supported": true, "publishers": publishers })
}

#[derive(Debug)]
struct InstallOutcome {
    row: Value,
    signature: String,
}

// ── 列表与安装 ──────────────────────────────────────────────────

fn installed_skill_ids(base: &Path) -> HashSet<String> {
    skill::list_in(base, &[])
        .into_iter()
        .filter_map(|row| row.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect()
}

fn installed_suite_ids(base: &Path) -> HashSet<String> {
    suite::list_in(base)
        .into_iter()
        .filter_map(|row| row.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect()
}

pub async fn list_skills(
    base: &Path,
    index_url: &str,
    trust: &[TrustedPublisher],
) -> Result<Value, String> {
    let index = fetch_skill_index(index_url).await?;
    let installed = installed_skill_ids(base);
    let items: Vec<Value> = index
        .items
        .iter()
        .map(|item| {
            let mut view = json!({
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "version": item.version,
                "author": item.author,
                "homepage": item.homepage,
                "sha256": item.sha256,
                "sizeBytes": item.size_bytes,
                "installed": installed.contains(&item.id),
            });
            merge_signature_fields(&mut view, item_signature_view(&item.publisher, trust));
            view
        })
        .collect();
    let publishers: Vec<Value> = index
        .publishers
        .iter()
        .map(|publisher| publisher_view(publisher, trust))
        .collect();
    Ok(json!({
        "supported": true,
        "configured": true,
        "indexUrl": index.url,
        "fetchedAtMs": crate::bridge::event_store::now_ms(),
        "signaturePolicy": index.policy.as_str(),
        "publishers": publishers,
        "items": items,
    }))
}

pub async fn list_suites(
    base: &Path,
    index_url: &str,
    trust: &[TrustedPublisher],
) -> Result<Value, String> {
    let index = fetch_suite_index(index_url).await?;
    let installed = installed_suite_ids(base);
    let items: Vec<Value> = index
        .items
        .iter()
        .map(|item| {
            let mut view = json!({
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "version": item.version,
                "author": item.author,
                "category": item.category,
                "homepage": item.homepage,
                "sha256": item.sha256,
                "sizeBytes": item.size_bytes,
                "installed": installed.contains(&item.id),
                "publishedAtMs": item.published_at_ms,
                "skillCount": item.skill_count,
                "commandCount": item.command_count,
                "dataLinkCount": item.data_link_count,
            });
            merge_signature_fields(&mut view, item_signature_view(&item.publisher, trust));
            view
        })
        .collect();
    let publishers: Vec<Value> = index
        .publishers
        .iter()
        .map(|publisher| publisher_view(publisher, trust))
        .collect();
    Ok(json!({
        "supported": true,
        "configured": true,
        "indexUrl": index.url,
        "fetchedAtMs": crate::bridge::event_store::now_ms(),
        "signaturePolicy": index.policy.as_str(),
        "publishers": publishers,
        "items": items,
    }))
}

fn merge_signature_fields(target: &mut Value, fields: Value) {
    if let (Some(target), Value::Object(fields)) = (target.as_object_mut(), fields) {
        for (key, value) in fields {
            target.insert(key, value);
        }
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn verify_sha256(bytes: &[u8], expected: &str) -> Result<(), String> {
    let actual = sha256_hex(bytes);
    if !actual.eq_ignore_ascii_case(expected.trim()) {
        return Err(format!(
            "MARKET_HASH_MISMATCH: 期望 {}…，实际 {}…",
            &expected[..expected.len().min(12)],
            &actual[..12]
        ));
    }
    Ok(())
}

fn find_item<'a, T: HasId>(items: &'a [T], item_id: &str) -> Result<&'a T, String> {
    items
        .iter()
        .find(|item| item.id() == item_id)
        .ok_or_else(|| "MARKET_ITEM_NOT_FOUND: 市场索引里找不到该条目，可能已下架".to_string())
}

trait HasId {
    fn id(&self) -> &str;
}

impl HasId for SkillItem {
    fn id(&self) -> &str {
        &self.id
    }
}

impl HasId for SuiteItem {
    fn id(&self) -> &str {
        &self.id
    }
}

pub async fn install_skill_from(
    base: &Path,
    index_url: &str,
    item_id: &str,
    trust: &[TrustedPublisher],
) -> Result<InstallOutcome, String> {
    let item_id = skill::normalize_id(item_id)?;
    let index = fetch_skill_index(index_url).await?;
    let item = find_item(&index.items, &item_id)?;
    let download = parse_download_url(&item.skill_md_url, index.local)?;
    ensure_target_allowed(&download, index.local).await?;
    let bytes = fetch_url_bytes(&download, SKILL_MAX_BYTES as usize, index.local).await?;
    verify_sha256(&bytes, &item.sha256)?;
    let signature = verify_artifact_signature(
        "skill",
        &item.id,
        &item.version,
        &item.sha256,
        &item.publisher,
        &item.signature,
        index.policy,
        trust,
    )?;
    let content = String::from_utf8(bytes)
        .map_err(|_| "MARKET_INVALID_INDEX: 下载的 SKILL.md 不是 UTF-8 文本".to_string())?;
    let row = skill::install_content_in(base, &item.id, &content)?;
    Ok(InstallOutcome { row, signature })
}

pub async fn install_suite_from(
    base: &Path,
    index_url: &str,
    item_id: &str,
    trust: &[TrustedPublisher],
) -> Result<InstallOutcome, String> {
    let item_id = suite::normalize_id(item_id)?;
    let index = fetch_suite_index(index_url).await?;
    let item = find_item(&index.items, &item_id)?;
    let download = parse_download_url(&item.zip_url, index.local)?;
    ensure_target_allowed(&download, index.local).await?;
    let bytes = fetch_url_bytes(&download, SUITE_MAX_BYTES as usize, index.local).await?;
    verify_sha256(&bytes, &item.sha256)?;
    let signature = verify_artifact_signature(
        "suite",
        &item.id,
        &item.version,
        &item.sha256,
        &item.publisher,
        &item.signature,
        index.policy,
        trust,
    )?;
    let row = suite::install_bytes_in(base, &bytes)?;
    Ok(InstallOutcome { row, signature })
}

/// 只取主机名用于审计，不带路径 / 查询串 / 凭据
pub fn source_host(raw: &str) -> String {
    reqwest::Url::parse(raw)
        .ok()
        .and_then(|url| url.host_str().map(str::to_owned))
        .unwrap_or_else(|| "未知来源".to_string())
}

fn audit_install(
    store: &Store,
    kind: &str,
    item_id: &str,
    version: &Option<String>,
    host: &str,
    signature: &str,
) {
    let version = version.as_deref().unwrap_or("未标注版本");
    let detail = format!("从在线市场安装{kind}，来源 {host}，版本 {version}，{signature}");
    audit::record_quiet(
        store,
        audit::ACTION_MARKET_INSTALL,
        audit::SEV_INFO,
        item_id,
        &detail,
    );
}

// ── Tauri command ──────────────────────────────────────────────

#[tauri::command]
pub async fn market_config_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    Ok(config_view(&p0.store))
}

#[tauri::command]
pub async fn market_config_set(
    p0: State<'_, Arc<P0Services>>,
    req: ConfigPatch,
) -> Result<Value, String> {
    let current = read_sources(&p0.store);
    let next = Sources {
        skill_index_url: apply_patch(current.skill_index_url, req.skill_index_url)?,
        suite_index_url: apply_patch(current.suite_index_url, req.suite_index_url)?,
    };
    let encoded = serde_json::to_string(&next)
        .map_err(|e| format!("MARKET_INVALID_INDEX: 市场配置序列化失败（{e}）"))?;
    settings::set(&p0.store, CONFIG_KEY, &encoded)?;
    let hosts = [
        next.skill_index_url.as_deref().map(source_host),
        next.suite_index_url.as_deref().map(source_host),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" / ");
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        "market",
        if hosts.is_empty() {
            "清除在线市场索引源".to_string()
        } else {
            format!("更新在线市场索引源：{hosts}")
        }
        .as_str(),
    );
    Ok(config_view(&p0.store))
}

#[tauri::command]
pub async fn market_skill_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let sources = read_sources(&p0.store);
    match sources.skill_index_url {
        Some(url) => list_skills(&python_env::base_dir(), &url, &load_trust(&p0.store)).await,
        None => Ok(empty_list()),
    }
}

#[tauri::command]
pub async fn market_suite_list(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    let sources = read_sources(&p0.store);
    match sources.suite_index_url {
        Some(url) => list_suites(&python_env::base_dir(), &url, &load_trust(&p0.store)).await,
        None => Ok(empty_list()),
    }
}

#[tauri::command]
pub async fn market_trust_get(p0: State<'_, Arc<P0Services>>) -> Result<Value, String> {
    Ok(trust_view(&load_trust(&p0.store)))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TrustAddReq {
    pub publisher_id: String,
    pub public_key: String,
    #[serde(default)]
    pub name: Option<String>,
}

#[tauri::command]
pub async fn market_trust_add(
    p0: State<'_, Arc<P0Services>>,
    req: TrustAddReq,
) -> Result<Value, String> {
    let mut trust = load_trust(&p0.store);
    let entry = add_trusted_publisher(
        &mut trust,
        &req.publisher_id,
        &req.public_key,
        req.name,
        crate::bridge::event_store::now_ms(),
    )?;
    save_trust(&p0.store, &trust)?;
    audit::record_quiet(
        &p0.store,
        audit::ACTION_CONFIG_CHANGE,
        audit::SEV_INFO,
        "market",
        &format!(
            "信任市场发布者 {}（公钥指纹 {}）",
            entry.id,
            short_fingerprint(&fingerprint_of_public_key_b64(&entry.public_key))
        ),
    );
    Ok(trust_view(&trust))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TrustRemoveReq {
    pub publisher_id: String,
}

#[tauri::command]
pub async fn market_trust_remove(
    p0: State<'_, Arc<P0Services>>,
    req: TrustRemoveReq,
) -> Result<Value, String> {
    let mut trust = load_trust(&p0.store);
    let removed = remove_trusted_publisher(&mut trust, &req.publisher_id)?;
    if removed {
        save_trust(&p0.store, &trust)?;
        audit::record_quiet(
            &p0.store,
            audit::ACTION_CONFIG_CHANGE,
            audit::SEV_WARNING,
            "market",
            &format!("移除市场发布者信任：{}", req.publisher_id.trim()),
        );
    }
    Ok(trust_view(&trust))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallItem {
    pub item_id: String,
}

#[tauri::command]
pub async fn market_skill_install(
    p0: State<'_, Arc<P0Services>>,
    req: InstallItem,
) -> Result<Value, String> {
    let sources = read_sources(&p0.store);
    let url = sources
        .skill_index_url
        .ok_or_else(|| "MARKET_NOT_CONFIGURED: 尚未配置技能市场索引地址".to_string())?;
    let index = fetch_skill_index(&url).await?;
    let item = find_item(&index.items, &skill::normalize_id(&req.item_id)?)?;
    let version = item.version.clone();
    let host = source_host(&index.url);
    let outcome = install_skill_from(
        &python_env::base_dir(),
        &url,
        &req.item_id,
        &load_trust(&p0.store),
    )
    .await?;
    audit_install(
        &p0.store,
        "技能",
        &req.item_id,
        &version,
        &host,
        &outcome.signature,
    );
    Ok(outcome.row)
}

#[tauri::command]
pub async fn market_suite_install(
    p0: State<'_, Arc<P0Services>>,
    req: InstallItem,
) -> Result<Value, String> {
    let sources = read_sources(&p0.store);
    let url = sources
        .suite_index_url
        .ok_or_else(|| "MARKET_NOT_CONFIGURED: 尚未配置套件市场索引地址".to_string())?;
    let index = fetch_suite_index(&url).await?;
    let item = find_item(&index.items, &suite::normalize_id(&req.item_id)?)?;
    let version = item.version.clone();
    let host = source_host(&index.url);
    let outcome = install_suite_from(
        &python_env::base_dir(),
        &url,
        &req.item_id,
        &load_trust(&p0.store),
    )
    .await?;
    audit_install(
        &p0.store,
        "专家套件",
        &req.item_id,
        &version,
        &host,
        &outcome.signature,
    );
    Ok(outcome.row)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::KeyPair as _;
    use std::io::{Read, Write};
    use std::net::TcpListener as StdTcpListener;

    fn temp_base(tag: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "tt-market-{tag}-{}-{}",
            std::process::id(),
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn sha(raw: &[u8]) -> String {
        sha256_hex(raw)
    }

    /// 极简 HTTP 服务：按路径返回固定响应并主动关闭连接（禁重定向测试同样复用）
    /// 复用已绑定端口（索引内容需要引用服务自身地址时先拿端口再启动）
    fn serve_n_on(
        listener: StdTcpListener,
        routes: Vec<(String, Vec<u8>, &'static str)>,
        requests: usize,
    ) -> (String, std::thread::JoinHandle<()>) {
        let addr = listener.local_addr().unwrap();
        let base = format!("http://{}", addr);
        let handle = std::thread::spawn(move || {
            for _ in 0..requests {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buf = [0u8; 4096];
                let n = stream.read(&mut buf).unwrap_or(0);
                let head = String::from_utf8_lossy(&buf[..n]).to_string();
                let path = head
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                let (body, content_type) = routes
                    .iter()
                    .find(|(p, _, _)| p == &path)
                    .map(|(_, b, t)| (b.clone(), *t))
                    .unwrap_or_else(|| (b"not found".to_vec(), "text/plain"));
                let status = if routes.iter().any(|(p, _, _)| p == &path) {
                    "200 OK"
                } else {
                    "404 Not Found"
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.write_all(&body);
                let _ = stream.flush();
            }
        });
        (base, handle)
    }

    fn skill_index(base: &str, sha: &str) -> Vec<u8> {
        format!(
            r#"{{"skills":[{{"id":"demo-skill","name":"演示技能","description":"测试用","version":"1.2.0","author":"tester","skillMdUrl":"{base}/demo.md","sha256":"{sha}"}}]}}"#
        )
        .into_bytes()
    }

    fn keypair() -> (ring::signature::Ed25519KeyPair, String) {
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = ring::signature::Ed25519KeyPair::generate_pkcs8(&rng).unwrap();
        let pair = ring::signature::Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap();
        let public =
            base64::engine::general_purpose::STANDARD_NO_PAD.encode(pair.public_key().as_ref());
        (pair, public)
    }

    fn sign_skill(
        pair: &ring::signature::Ed25519KeyPair,
        publisher_id: &str,
        item_id: &str,
        version: &str,
        sha: &str,
    ) -> String {
        let payload = signing_payload("skill", publisher_id, item_id, version, sha);
        base64::engine::general_purpose::STANDARD_NO_PAD.encode(pair.sign(&payload).as_ref())
    }

    fn signed_skill_index(
        base: &str,
        sha: &str,
        public_key: &str,
        signature: &str,
        policy: &str,
    ) -> Vec<u8> {
        format!(
            r#"{{"signaturePolicy":"{policy}","publishers":[{{"id":"acme","name":"Acme","publicKey":"{public_key}"}}],"skills":[{{"id":"demo-skill","name":"演示技能","description":"测试用","version":"1.2.0","author":"tester","skillMdUrl":"{base}/demo.md","sha256":"{sha}","publisherId":"acme","signature":"{signature}"}}]}}"#
        )
        .into_bytes()
    }

    fn trusted(publisher_id: &str, public_key: &str) -> TrustedPublisher {
        TrustedPublisher {
            id: publisher_id.to_string(),
            name: Some("Acme".to_string()),
            public_key: public_key.to_string(),
            added_at_ms: 1,
        }
    }

    #[test]
    fn ed25519_signature_is_real_and_rejects_tampering() {
        let (pair, public_key) = keypair();
        let payload = signing_payload("skill", "acme", "demo-skill", "1.2.0", &"a".repeat(64));
        let signature = pair.sign(&payload);
        let key_bytes = decode_base64(&public_key).unwrap();
        assert!(UnparsedPublicKey::new(&ED25519, &key_bytes)
            .verify(&payload, signature.as_ref())
            .is_ok());
        let mut tampered = payload.clone();
        tampered[0] ^= 0xff;
        assert!(UnparsedPublicKey::new(&ED25519, &key_bytes)
            .verify(&tampered, signature.as_ref())
            .is_err());
    }

    #[test]
    fn signed_index_requires_valid_publisher_and_signature() {
        let (_pair, public_key) = keypair();
        let hash = "a".repeat(64);
        let signature = "A".repeat(86);
        let signed = signed_skill_index("http://127.0.0.1:9", &hash, &public_key, &signature, "optional");
        let parsed = parse_skill_items(&signed, true).unwrap();
        assert_eq!(parsed.items[0].publisher.as_ref().unwrap().id, "acme");
        assert_eq!(parsed.publishers.len(), 1);
        assert_eq!(parsed.policy, SignaturePolicy::Optional);

        let unsigned = br#"{"signaturePolicy":"required","skills":[{"id":"a","skillMdUrl":"https://x.example/a.md","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}"#;
        let err = parse_skill_items(unsigned, false).unwrap_err();
        assert!(err.contains("要求签名"), "{err}");

        let unknown = br#"{"publishers":[],"skills":[{"id":"a","skillMdUrl":"https://x.example/a.md","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","publisherId":"ghost","signature":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}]}"#;
        let err = parse_skill_items(unknown, false).unwrap_err();
        assert!(err.contains("未知发布者"), "{err}");

        let bad_key = br#"{"publishers":[{"id":"acme","publicKey":"abc"}],"skills":[]}"#;
        let err = parse_skill_items(bad_key, false).unwrap_err();
        assert!(err.contains("32 字节"), "{err}");
    }

    #[test]
    fn trust_store_pins_exact_key_and_requires_rotation() {
        let (_pair_a, key_a) = keypair();
        let (_pair_b, key_b) = keypair();
        let mut list = Vec::new();
        let added = add_trusted_publisher(&mut list, "Acme", &key_a, Some("Acme".into()), 10).unwrap();
        assert_eq!(added.id, "acme");
        assert_eq!(list.len(), 1);
        // 同一公钥重复添加是幂等更新，不产生第二条
        add_trusted_publisher(&mut list, "acme", &key_a, Some("Acme Inc".into()), 20).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name.as_deref(), Some("Acme Inc"));
        // 同一发布者换钥必须显式移除旧信任
        let err = add_trusted_publisher(&mut list, "acme", &key_b, None, 30).unwrap_err();
        assert!(err.contains("MARKET_PUBLISHER_KEY_CHANGED"), "{err}");
        assert!(remove_trusted_publisher(&mut list, "acme").unwrap());
        assert!(list.is_empty());
        add_trusted_publisher(&mut list, "acme", &key_b, None, 40).unwrap();
        assert_eq!(list[0].public_key, key_b);
    }

    #[test]
    fn trust_state_is_exact_id_and_key() {
        let (_pair_a, key_a) = keypair();
        let (_pair_b, key_b) = keypair();
        let hash = "a".repeat(64);
        let signature = "A".repeat(86);
        let index = signed_skill_index("http://127.0.0.1:9", &hash, &key_a, &signature, "optional");
        let parsed = parse_skill_items(&index, true).unwrap();
        let publisher = parsed.items[0].publisher.clone();
        assert_eq!(trust_status(publisher.as_ref().unwrap(), &[]), PublisherTrust::Untrusted);
        assert_eq!(
            trust_status(publisher.as_ref().unwrap(), &[trusted("acme", &key_a)]),
            PublisherTrust::Trusted
        );
        assert_eq!(
            trust_status(publisher.as_ref().unwrap(), &[trusted("acme", &key_b)]),
            PublisherTrust::KeyChanged
        );
        let view = item_signature_view(&publisher, &[trusted("acme", &key_a)]);
        assert_eq!(view["signed"], true);
        assert_eq!(view["publisherTrusted"], true);
        assert_eq!(view["publisherId"], "acme");
    }

    #[tokio::test]
    async fn signed_skill_requires_trusted_key_and_verifies_signature() {
        let skill_md = b"---\nname: Demo Skill\ndescription: demo\n---\n\n# Signed\n".to_vec();
        let hash = sha(&skill_md);
        let (pair, public_key) = keypair();
        let signature = sign_skill(&pair, "acme", "demo-skill", "1.2.0", &hash);
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let index = signed_skill_index(&base_url, &hash, &public_key, &signature, "required");
        // list 1 次、未信任安装 2 次（索引+产物）、信任后安装 2 次
        let (url, server) = serve_n_on(
            listener,
            vec![
                ("/index.json".to_string(), index, "application/json"),
                ("/demo.md".to_string(), skill_md, "text/markdown"),
            ],
            5,
        );
        let index_url = format!("{url}/index.json");
        let base = temp_base("skill-signed");
        let listed = list_skills(&base, &index_url, &[]).await.unwrap();
        assert_eq!(listed["signaturePolicy"], "required");
        assert_eq!(listed["items"][0]["signed"], true);
        assert_eq!(listed["items"][0]["publisherTrusted"], false);
        assert_eq!(listed["publishers"][0]["fingerprint"].as_str().unwrap().len(), 64);

        let err = install_skill_from(&base, &index_url, "demo-skill", &[])
            .await
            .unwrap_err();
        assert!(err.contains("MARKET_UNTRUSTED_PUBLISHER"), "{err}");
        assert!(!base.join("skills/demo-skill").exists());

        let outcome = install_skill_from(
            &base,
            &index_url,
            "demo-skill",
            &[trusted("acme", &public_key)],
        )
        .await
        .unwrap();
        assert_eq!(outcome.row["id"], "demo-skill");
        assert!(outcome.signature.contains("acme"), "{}", outcome.signature);
        assert!(base.join("skills/demo-skill/SKILL.md").exists());
        server.join().unwrap();
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn tampered_signature_is_rejected_and_writes_nothing() {
        let skill_md = b"# Signed but tampered\n".to_vec();
        let hash = sha(&skill_md);
        let (pair, public_key) = keypair();
        // 签名针对另一份哈希：索引声明的 sha256 与签名覆盖内容不一致
        let signature = sign_skill(&pair, "acme", "demo-skill", "1.2.0", &"0".repeat(64));
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let index = signed_skill_index(&base_url, &hash, &public_key, &signature, "required");
        let (url, server) = serve_n_on(
            listener,
            vec![
                ("/index.json".to_string(), index, "application/json"),
                ("/demo.md".to_string(), skill_md, "text/markdown"),
            ],
            2,
        );
        let base = temp_base("skill-signature-bad");
        let err = install_skill_from(
            &base,
            &format!("{url}/index.json"),
            "demo-skill",
            &[trusted("acme", &public_key)],
        )
        .await
        .unwrap_err();
        assert!(err.contains("MARKET_SIGNATURE_INVALID"), "{err}");
        assert!(!base.join("skills/demo-skill").exists());
        server.join().unwrap();
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn trust_persists_in_settings_kv() {
        let mut dir = std::env::temp_dir();
        dir.push(format!(
            "tt-market-trust-{}-{}",
            std::process::id(),
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        let (_pair, public_key) = keypair();
        let entry = TrustedPublisher {
            id: "acme".to_string(),
            name: Some("Acme".to_string()),
            public_key,
            added_at_ms: 7,
        };
        save_trust(&store, std::slice::from_ref(&entry)).unwrap();
        let loaded = load_trust(&store);
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, "acme");
        assert_eq!(loaded[0].public_key, entry.public_key);
        assert_eq!(trust_view(&loaded)["publishers"][0]["fingerprint"].as_str().unwrap().len(), 64);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn url_rules_allow_loopback_http_only() {
        assert!(normalize_index_url("https://market.example.com/index.json").is_ok());
        assert!(normalize_index_url("http://127.0.0.1:8080/index.json").is_ok());
        assert!(normalize_index_url("http://localhost:8080/index.json").is_ok());
        assert!(normalize_index_url("http://market.example.com/index.json").is_err());
        assert!(normalize_index_url("https://user:pass@market.example.com/index.json").is_err());
        assert!(normalize_index_url("ftp://market.example.com/index.json").is_err());
        assert!(normalize_index_url("not a url").is_err());
    }

    #[test]
    fn download_url_cannot_cross_the_loopback_boundary() {
        let remote_index = false;
        let local_index = true;
        assert!(parse_download_url("https://cdn.example.com/skill.md", remote_index).is_ok());
        assert!(parse_download_url("http://cdn.example.com/skill.md", remote_index).is_err());
        assert!(parse_download_url("https://127.0.0.1/skill.md", remote_index).is_err());
        assert!(parse_download_url("https://10.0.0.2/skill.md", remote_index).is_ok()); // 形态合法，DNS 阶段再拦
        assert!(parse_download_url("http://127.0.0.1:9000/skill.md", local_index).is_ok());
        assert!(parse_download_url("https://cdn.example.com/skill.md", local_index).is_err());
    }

    #[test]
    fn private_and_reserved_ips_are_not_public() {
        for raw in [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "100.64.0.1",
            "198.18.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "192.0.2.1",
            "::1",
            "fe80::1",
            "fd00::1",
        ] {
            assert!(!ip_is_public(raw.parse().unwrap()), "{raw} 不应视为公网");
        }
        for raw in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"] {
            assert!(ip_is_public(raw.parse().unwrap()), "{raw} 应视为公网");
        }
    }

    #[test]
    fn index_rejects_bad_and_duplicate_items() {
        let bad_hash = br#"{"skills":[{"id":"a","skillMdUrl":"https://x.example/a.md","sha256":"abc"}]}"#;
        let err = parse_skill_items(bad_hash, false).unwrap_err();
        assert!(err.contains("MARKET_INVALID_INDEX"));

        let dup = br#"{"skills":[
            {"id":"a","skillMdUrl":"https://x.example/a.md","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            {"id":"a","skillMdUrl":"https://x.example/b.md","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
        ]}"#;
        assert!(parse_skill_items(dup, false).unwrap_err().contains("重复"));

        let unsorted_url = br#"{"skills":[{"id":"a","skillMdUrl":"http://x.example/a.md","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}"#;
        assert!(parse_skill_items(unsorted_url, false).unwrap_err().contains("HTTPS"));
    }

    #[test]
    fn suite_index_parses_optional_counts() {
        let body = r#"{"suites":[{"id":"finance-suite","name":"金融套件","version":"2.0.0","category":"财务","zipUrl":"https://x.example/f.zip","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","skillCount":3,"commandCount":2,"dataLinkCount":1}]}"#;
        let items = parse_suite_items(body.as_bytes(), false).unwrap().items;
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].skill_count, Some(3));
        assert_eq!(items[0].category.as_deref(), Some("财务"));
    }

    #[tokio::test]
    async fn fetch_index_rejects_redirects() {
        // 直接返回 302 的服务端
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 2048];
            let _ = stream.read(&mut buf);
            let _ = stream.write_all(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/elsewhere\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
        });
        let err = fetch_skill_index(&format!("http://{addr}/index.json"))
            .await
            .unwrap_err();
        handle.join().unwrap();
        assert!(err.contains("重定向"), "{err}");
    }

    #[tokio::test]
    async fn installs_skill_from_loopback_market_and_marks_installed() {
        let skill_md = b"---\nname: Demo Skill\ndescription: demo\n---\n\n# Demo\n".to_vec();
        let hash = sha(&skill_md);
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let index = skill_index(&base_url, &hash);
        // list / 安装拉索引 / 下载 / 再 list，共 4 次请求
        let (url, server) = serve_n_on(
            listener,
            vec![
                ("/index.json".to_string(), index, "application/json"),
                ("/demo.md".to_string(), skill_md.clone(), "text/markdown"),
            ],
            4,
        );
        let index_url = format!("{url}/index.json");
        let base = temp_base("skill-install");
        let listed = list_skills(&base, &index_url, &[]).await.unwrap();
        assert_eq!(listed["items"][0]["installed"], false);

        let row = install_skill_from(&base, &index_url, "demo-skill", &[])
            .await
            .unwrap()
            .row;
        assert_eq!(row["id"], "demo-skill");
        let installed = std::fs::read_to_string(base.join("skills/demo-skill/SKILL.md")).unwrap();
        assert!(installed.contains("# Demo"));
        assert!(list_skills(&base, &index_url, &[]).await.unwrap()["items"][0]["installed"]
            .as_bool()
            .unwrap());
        server.join().unwrap();
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn hash_mismatch_leaves_no_skill_directory() {
        let skill_md = b"# tampered\n".to_vec();
        let wrong = "0".repeat(64);
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let index = skill_index(&base_url, &wrong);
        // 安装会先拉索引再下载，共 2 次请求
        let (url, server) = serve_n_on(
            listener,
            vec![
                ("/index.json".to_string(), index, "application/json"),
                ("/demo.md".to_string(), skill_md, "text/markdown"),
            ],
            2,
        );
        let base = temp_base("skill-hash");
        let err = install_skill_from(&base, &format!("{url}/index.json"), "demo-skill", &[])
            .await
            .unwrap_err();
        assert!(err.contains("MARKET_HASH_MISMATCH"), "{err}");
        assert!(!base.join("skills/demo-skill").exists());
        server.join().unwrap();
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn source_host_never_includes_path_or_query() {
        assert_eq!(
            source_host("https://market.example.com/api/index.json?token=secret"),
            "market.example.com"
        );
    }
}
